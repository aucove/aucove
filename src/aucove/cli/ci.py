# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Envoltura de los subcomandos internos ``ci`` (09-ci; 00-marco DC-23).

Reúne los ``COMMANDS`` de cada módulo de ``ops.registry.CI_MODULES`` y los expone
como ``ci <nombre>``. No hay lógica de negocio aquí (REQ-ARCH-13): cada subcomando
vive en ``ops``. Estos subcomandos no emiten el sobre ni tienen herramienta MCP.

Salidas: 0 bien, 1 error interno o valor inválido (falla cerrada), 3 error de uso;
nunca 2 (``argparse`` saldría con 2, así que sus errores se traducen a 3). Un
subcomando conocido cuyo módulo falta o no carga (p. ej. un paquete recortado)
sale con 1 y «no disponible en esta versión».

Cada ejecución crea un ``OpContext`` con ``surface='ci'`` y llama siempre a
``close()``, que guarda el estado del FakeForge en las pruebas. El contexto no
resuelve el proyecto desde el directorio actual, que en CI puede ser el checkout
del PR (09-ci §3.3): solo con una raíz explícita.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from typing import TextIO

from .. import _meta
from ..i18n import Translator, resolve_lang, translator
from ..ops.context import Invocation, OpContext, Project
from ..ops.registry import CI_MODULES, CiCommand
from ..ops.result import OpError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 3
ALLOWED_EXITS = frozenset({EXIT_OK, EXIT_ERROR, EXIT_USAGE})

# Subcomandos ci que conoce la spec (09-ci §3-§7, DC-23; review-context reservado; editor-review
# desde H3, D-56).
# Si el módulo que debería darlos falta, salen con 1 y «no disponible»; cualquier
# otro nombre es un error de uso.
KNOWN_COMMANDS: tuple[str, ...] = (
    "build-plan", "pin-plan", "comment", "base-report", "audit", "seal", "editor-review", "review-context",
)

_HELP_PREFIX = "ci."


class _UsageError(Exception):
    def __init__(self, parser: argparse.ArgumentParser, message: str) -> None:
        super().__init__(message)
        self.parser = parser
        self.message = message


class _ParserExit(Exception):
    def __init__(self, status: int, message: str | None) -> None:
        super().__init__(message or "")
        self.status = status
        self.message = message


class _Parser(argparse.ArgumentParser):
    """``ArgumentParser`` que nunca llama a ``sys.exit`` (su código de uso sería 2)."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise _UsageError(self, message)

    def exit(self, status: int = 0, message: str | None = None) -> None:  # type: ignore[override]
        raise _ParserExit(status, message)


class CiContext(OpContext):
    """``OpContext`` de ``surface='ci'`` que no busca el proyecto en el directorio actual.

    Sin ``inv.root``, ``project()`` no encuentra proyecto: la política llega siempre por
    argumentos (``--policy``) y el directorio actual puede ser el árbol del PR.
    """

    def project(self, *, required: bool = True) -> Project | None:
        if self.inv.root:
            return super().project(required=required)
        if required:
            raise OpError("E_CONFIG_NOT_A_PROJECT")
        return None


def load_commands(modules: Sequence[str]) -> dict[str, CiCommand]:
    """Subcomandos de ``modules`` por nombre. Un módulo que no carga o no exporta ``COMMANDS`` válidos
    no aporta nada; un nombre repetido entre módulos se descarta (falla cerrada)."""
    found: dict[str, CiCommand] = {}
    repeated: set[str] = set()
    for module in modules:
        try:
            mod = importlib.import_module("." + module, _meta.NAME)
            commands = tuple(getattr(mod, "COMMANDS"))
            if not all(isinstance(c, CiCommand) and isinstance(c.name, str) and c.name for c in commands):
                continue
        except Exception:  # módulo ausente o roto: sus subcomandos quedan «no disponibles»
            continue
        for cmd in commands:
            if cmd.name in found:
                repeated.add(cmd.name)
            else:
                found[cmd.name] = cmd
    for name in repeated:
        found.pop(name, None)
    return found


def _localize(parser: argparse.ArgumentParser, t: Translator) -> None:
    """Sustituye por su texto los ``help`` y la descripción que son claves ``ci.*`` del catálogo."""
    if isinstance(parser.description, str) and parser.description.startswith(_HELP_PREFIX):
        parser.description = t(parser.description)
    for action in parser._actions:  # API estable de argparse, aunque con guion bajo
        if isinstance(action.help, str) and action.help.startswith(_HELP_PREFIX):
            action.help = t(action.help)


def build_parser(commands: Mapping[str, CiCommand], t: Translator) -> argparse.ArgumentParser:
    """Parser de ``ci`` con un subparser por subcomando disponible."""
    parser = _Parser(prog=f"{_meta.NAME} ci", description=t("ci.description"), allow_abbrev=False)
    sub = parser.add_subparsers(dest="ci_command", metavar="<command>", title=t("ci.commands"))
    for name in sorted(commands):
        cmd = commands[name]
        p = sub.add_parser(name, help=t(f"ci.command.{name}"), allow_abbrev=False)
        cmd.add_arguments(p)
        _localize(p, t)
        p.set_defaults(ci_main=cmd.main)
    return parser


def _write(stream: TextIO, text: str) -> None:
    try:
        stream.write(text if text.endswith("\n") else text + "\n")
        stream.flush()
    except (OSError, UnicodeError, ValueError):
        pass


def _prepare_streams(stdout: TextIO | None, stderr: TextIO | None) -> tuple[TextIO, TextIO]:
    """Flujos del proceso con LF y sin errores de codificación en Windows ($GITHUB_OUTPUT es LF)."""
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    if stdout is None:
        with contextlib.suppress(Exception):
            sys.stdout.reconfigure(newline="\n")  # type: ignore[union-attr]
    if stderr is None:
        with contextlib.suppress(Exception):
            sys.stderr.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
    return out, err


def _first_word(argv: Sequence[str]) -> str | None:
    return argv[0] if argv and not argv[0].startswith("-") else None


def run(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    modules: Sequence[str] | None = None,
) -> int:
    """Ejecuta ``ci <subcomando> ...``; devuelve 0, 1 o 3, nunca 2.

    ``modules`` sustituye a ``CI_MODULES`` (para las pruebas).
    """
    environ: dict[str, str] = dict(os.environ if env is None else env)
    out, err = _prepare_streams(stdout, stderr)
    t = translator(resolve_lang(cli=None, env=environ, project=None))
    args = list(argv)
    try:
        commands = load_commands(CI_MODULES if modules is None else modules)
        name = _first_word(args)
        if name is not None and name not in commands:
            if name in KNOWN_COMMANDS:
                _write(err, t("ci.unavailable", name=name, version=_meta.__version__))
                return EXIT_ERROR
            _write(err, t("ci.unknown", name=name))
            return EXIT_USAGE
        parser = build_parser(commands, t)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                ns = parser.parse_args(args)
        except _ParserExit as exc:  # --help y similares
            if exc.message:
                _write(err, exc.message)
            return EXIT_OK if exc.status == 0 else EXIT_USAGE
        except _UsageError as exc:
            _write(err, exc.parser.format_usage().rstrip("\n"))
            _write(err, t("ci.usage", message=exc.message))
            return EXIT_USAGE
        main = getattr(ns, "ci_main", None)
        if main is None:
            _write(err, parser.format_usage().rstrip("\n"))
            _write(err, t("ci.missing"))
            return EXIT_USAGE
        return _execute(main, ns, environ, out, err, t)
    except KeyboardInterrupt:
        return EXIT_ERROR
    except (Exception, SystemExit):  # la envoltura nunca propaga ni sale con 2
        _write(err, t("ci.internal", trace_id=uuid.uuid4().hex[:12]))
        return EXIT_ERROR


def _execute(
    main: object,
    ns: argparse.Namespace,
    env: Mapping[str, str],
    out: TextIO,
    err: TextIO,
    t: Translator,
) -> int:
    inv = Invocation(root=None, lang=None, json=False, dry_run=False, surface="ci", env=env, cwd=os.getcwd())
    ctx = CiContext(inv)
    rc: int = EXIT_ERROR
    try:
        with contextlib.redirect_stderr(err):
            result = main(ns, ctx, out)  # type: ignore[operator]
        rc = result if isinstance(result, int) and not isinstance(result, bool) else EXIT_ERROR
    except KeyboardInterrupt:
        rc = EXIT_ERROR
    except (Exception, SystemExit):  # sin el texto de la excepción, que puede traer datos del PR
        _write(err, t("ci.internal", trace_id=uuid.uuid4().hex[:12]))
        rc = EXIT_ERROR
    finally:
        try:
            ctx.close()
        except Exception:
            _write(err, t("ci.internal", trace_id=uuid.uuid4().hex[:12]))
            rc = EXIT_ERROR
    return rc if rc in ALLOWED_EXITS else EXIT_ERROR
