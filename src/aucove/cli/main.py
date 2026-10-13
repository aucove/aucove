# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""CLI generada del registro de operaciones (01 §3.4; 05 §3.4, §4 y §5).

El parser sale de ``ops.registry.REGISTRY`` (REQ-ARCH-21), o del registro que se
inyecte en las pruebas: no hay otra lista de operaciones. Los flags se derivan de
``Param`` (REQ-OPS-12): ``snake_case`` pasa a ``--kebab-case`` y las listas, a un
flag singular repetible, salvo ``cli_flag``. Esta envoltura no tiene lógica de
negocio: convierte los argumentos, llama a ``registry.invoke`` y escribe el sobre
(``--json``) o un resumen legible (``render_text`` del módulo, si lo exporta).

Salidas (05 §5): la del sobre (``Result.exit_code``), 3 para cualquier error de
uso (``E_VALIDATION_INPUT``, REQ-OPS-15), 130 ante Ctrl-C y 1 ante un fallo de la
propia envoltura. Nunca 2, que es de ``hook``.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import json
import os
import re
import sys
import unicodedata
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, NoReturn, TextIO

from .. import _meta
from ..domain.errors import exit_code as _exit_code
from ..i18n import LANGS, Translator, resolve_lang, translator
from ..ops.context import Invocation, OpContext, stdio_is_tty
from ..ops.inputs import input_error as _err
from ..ops.inputs import schema_errors as _schema_errors
from ..ops.registry import REGISTRY, OpSpec, Param, input_schema, invoke, load_op_module, param_schema
from ..ops.result import ErrorObj, OpError, Result, clip, dumps, fail, make_error

USAGE_CODE = "E_VALIDATION_INPUT"  # todo error de uso (REQ-OPS-15)
EXIT_INTERRUPTED = 130  # Ctrl-C (05 §5)
EXIT_INTERNAL = 1
MAX_INPUT_BYTES = 2**20  # tope de --input
MAX_DETAILS_CHARS = 2000  # tope de la línea de detalles legible

_SPEC_DEST = "_op_spec"
_PARAM_PREFIX = "p_"
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_WORD_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_INT_RE = re.compile(r"^[+-]?[0-9]+$")
# Nombres que chocan con las propiedades comunes de inputSchema o con invoke(spec, ctx, ...).
_RESERVED_PARAMS = frozenset({"project_root", "dry_run", "spec", "ctx"})
# Controles bidireccionales que un terminal podría usar para ocultar texto.
_BIDI = frozenset(range(0x202A, 0x202F)) | frozenset(range(0x2066, 0x206A))


# -- opciones globales (05 §4) -------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Global:
    dest: str
    flags: tuple[str, ...]
    kind: str  # 'flag', 'value', 'help' o 'version'
    key: str  # clave de ayuda en cli.toml
    metavar: str | None = None


_GLOBALS: tuple[_Global, ...] = (
    _Global("g_json", ("--json",), "flag", "cli.global.json"),
    _Global("g_dry_run", ("--dry-run",), "flag", "cli.global.dry_run"),
    _Global("g_lang", ("--lang",), "value", "cli.global.lang", "{" + ",".join(LANGS) + "}"),
    _Global("g_root", ("--root",), "value", "cli.global.root", "DIR"),
    _Global("g_input", ("--input",), "value", "cli.global.input", "FILE"),
    _Global("g_version", ("--version",), "version", "cli.global.version"),
    _Global("g_help", ("-h", "--help"), "help", "cli.global.help"),
)
_GLOBAL_FLAGS: frozenset[str] = frozenset(flag for g in _GLOBALS for flag in g.flags)


@dataclass(frozen=True, slots=True)
class HumanConfirmation:
    """Prueba de la fase 2 de ``require_human`` (06-permisos §6): solo la construye la CLI, tras
    el reto tecleado (08-forja §2)."""

    operation: str
    confirmed_at: datetime


# -- parser que nunca sale con 2 --------------------------------------------------


class _UsageError(Exception):
    """Error de uso que ``argparse`` habría convertido en ``SystemExit(2)``."""

    def __init__(self, parser: argparse.ArgumentParser, message: str) -> None:
        super().__init__(message)
        self.parser = parser
        self.message = message


class _HelpRequested(Exception):
    def __init__(self, parser: argparse.ArgumentParser) -> None:
        super().__init__("help")
        self.parser = parser


class _VersionRequested(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    """``ArgumentParser`` cuyos errores y salidas son excepciones (REQ-OPS-15)."""

    op_spec: OpSpec | None = None
    cmd_path: tuple[str, ...] = ()
    sub_action: Any = None  # argparse._SubParsersAction de este nivel, si tiene
    by_path: dict[tuple[str, ...], _Parser] | None = None  # solo en el parser raíz

    def error(self, message: str) -> NoReturn:
        raise _UsageError(self, message)

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        # Ninguna ruta de argparse debe terminar el proceso: nunca SystemExit(2).
        raise _UsageError(self, message or "")


class _HelpAction(argparse.Action):
    def __init__(self, option_strings: Sequence[str], dest: str, help: str | None = None) -> None:
        super().__init__(option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, nargs=0, help=help)

    def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace,
                 values: object, option_string: str | None = None) -> None:
        raise _HelpRequested(parser)


class _VersionAction(argparse.Action):
    def __init__(self, option_strings: Sequence[str], dest: str, help: str | None = None) -> None:
        super().__init__(option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, nargs=0, help=help)

    def __call__(self, parser: argparse.ArgumentParser, namespace: argparse.Namespace,
                 values: object, option_string: str | None = None) -> None:
        raise _VersionRequested()


def _formatter_class(t: Translator) -> type[argparse.HelpFormatter]:
    """Formateador con el prefijo ``uso:``/``usage:`` localizado."""
    usage_prefix = t.get("cli.help.usage_prefix") or "usage: "

    class _Formatter(argparse.HelpFormatter):
        def add_usage(self, usage: str | None, actions: Any, groups: Any, prefix: str | None = None) -> None:
            super().add_usage(usage, actions, groups, usage_prefix if prefix is None else prefix)

    return _Formatter


def _h(text: str | None) -> str | None:
    """Texto de ayuda sin ``%`` sueltos (argparse aplica ``%`` a la ayuda)."""
    return None if text is None else text.replace("%", "%%")


# -- derivación de flags (REQ-OPS-12) --------------------------------------------


def _dest(p: Param) -> str:
    return _PARAM_PREFIX + p.name


def _singular(name: str) -> str:
    return name[:-1] if len(name) > 1 and name.endswith("s") else name


def cli_flag(p: Param) -> str:
    """Flag de un parámetro: ``cli_flag`` o ``--kebab-case`` (singular en las listas)."""
    if p.cli_flag:
        return p.cli_flag
    base = _singular(p.name) if p.type == "array" else p.name
    return "--" + base.replace("_", "-")


def _metavar(p: Param) -> str:
    if p.choices:
        return "{" + ",".join(p.choices) + "}"
    base = _singular(p.name) if p.type == "array" else p.name
    return base.upper()


def _check_param(spec: OpSpec, p: Param) -> None:
    if not _NAME_RE.match(p.name) or p.name in _RESERVED_PARAMS:
        raise ValueError(f"{spec.id}: nombre de parámetro no admitido {p.name!r}")
    if p.type not in ("string", "integer", "boolean", "array", "object"):
        raise ValueError(f"{spec.id}.{p.name}: tipo desconocido {p.type!r}")
    if p.type == "object" and not (p.cli_input_only or p.cli_only):
        raise ValueError(f"{spec.id}.{p.name}: un objeto no es un flag (cli_input_only)")
    if p.cli_input_only and (p.positional or p.cli_only):
        raise ValueError(f"{spec.id}.{p.name}: cli_input_only no admite positional ni cli_only")
    if p.cli_file_for is not None and (not p.cli_only or p.type != "string"
                                       or not any(q.name == p.cli_file_for for q in spec.params)):
        raise ValueError(f"{spec.id}.{p.name}: cli_file_for inválido")
    if p.cli_negatable and p.type != "boolean":
        raise ValueError(f"{spec.id}.{p.name}: cli_negatable solo vale para booleanos")
    if p.cli_ask and (p.type != "string" or p.positional or p.cli_only):
        raise ValueError(f"{spec.id}.{p.name}: cli_ask solo vale para cadenas que no son posicionales")
    if p.cli_ask_unless is not None and (
        not p.cli_ask or not any(q.name == p.cli_ask_unless and q.type == "boolean" for q in spec.params)
    ):
        raise ValueError(f"{spec.id}.{p.name}: cli_ask_unless debe nombrar un booleano de la operación")
    if p.cli_lang and (p.type != "string" or not p.choices or not set(p.choices) <= set(LANGS)):
        raise ValueError(f"{spec.id}.{p.name}: cli_lang solo vale para una cadena con idiomas como opciones")
    if p.cli_flag is not None and not re.match(r"^--[a-z][a-z0-9-]*$", p.cli_flag):
        raise ValueError(f"{spec.id}.{p.name}: cli_flag inválido {p.cli_flag!r}")
    if p.cli_short is not None and (not re.match(r"^-[a-z]$", p.cli_short) or p.positional):
        raise ValueError(f"{spec.id}.{p.name}: cli_short inválido {p.cli_short!r}")
    if p.cli_csv and p.type != "array":
        raise ValueError(f"{spec.id}.{p.name}: cli_csv solo vale para listas")
    if p.positional and p.type == "boolean":
        raise ValueError(f"{spec.id}.{p.name}: un booleano no puede ser posicional")


def _add_globals(parser: _Parser, t: Translator) -> None:
    group = parser.add_argument_group(t("cli.help.globals"))
    for g in _GLOBALS:
        help_text = _h(t.get(g.key))
        if g.kind == "help":
            group.add_argument(*g.flags, action=_HelpAction, help=help_text)
        elif g.kind == "version":
            group.add_argument(*g.flags, action=_VersionAction, help=help_text)
        elif g.kind == "flag":
            group.add_argument(*g.flags, dest=g.dest, action="store_true", default=argparse.SUPPRESS, help=help_text)
        else:
            group.add_argument(*g.flags, dest=g.dest, metavar=g.metavar, default=argparse.SUPPRESS, help=help_text)


def _add_params(parser: _Parser, spec: OpSpec, t: Translator) -> None:
    group = parser.add_argument_group(t("cli.help.options"))
    used = set(_GLOBAL_FLAGS)
    names: set[str] = set()
    for p in spec.params:
        _check_param(spec, p)
        if p.name in names:
            raise ValueError(f"{spec.id}: parámetro repetido {p.name!r}")
        names.add(p.name)
        if p.cli_input_only:
            continue  # solo por --input (05 §9.17)
        help_text = _h(t.get(f"cli.op.{spec.id}.param.{p.name}"))
        if p.positional:
            nargs = "*" if p.type == "array" else "?"
            group.add_argument(_dest(p), metavar=_metavar(p), nargs=nargs, default=argparse.SUPPRESS, help=help_text)
            continue
        flag = cli_flag(p)
        if flag in used or ("--no-" + flag[2:]) in used:
            raise ValueError(f"{spec.id}: el flag {flag} choca con otro")
        used.add(flag)
        flags = [flag]
        if p.cli_short is not None:
            if p.cli_short in used:
                raise ValueError(f"{spec.id}: el flag {p.cli_short} choca con otro")
            used.add(p.cli_short)
            flags.insert(0, p.cli_short)
        if p.type == "boolean":
            if p.default is True or p.cli_negatable:
                used.add("--no-" + flag[2:])
                group.add_argument(*flags, dest=_dest(p), action=argparse.BooleanOptionalAction,
                                   default=argparse.SUPPRESS, help=help_text)
            else:
                group.add_argument(*flags, dest=_dest(p), action="store_true", default=argparse.SUPPRESS,
                                   help=help_text)
        elif p.type == "array":
            group.add_argument(*flags, dest=_dest(p), action="append", metavar=_metavar(p),
                               default=argparse.SUPPRESS, help=help_text)
        else:
            group.add_argument(*flags, dest=_dest(p), metavar=_metavar(p), default=argparse.SUPPRESS, help=help_text)


def _add_child(parent: _Parser, word: str, path: tuple[str, ...], summary: str | None,
               t: Translator, formatter: type[argparse.HelpFormatter], *, spec: OpSpec | None = None) -> _Parser:
    if parent.sub_action is None:
        depth = len(path) - 1
        parent.sub_action = parent.add_subparsers(
            dest="command" if depth == 0 else f"command_{depth}",
            title=t("cli.help.commands"),
            parser_class=_Parser,
        )
    child = parent.sub_action.add_parser(
        word,
        help=_h(summary),  # siempre presente: toda operación registrada aparece en la ayuda
        description=summary,
        prog=f"{parent.prog} {word}",
        add_help=False,
        allow_abbrev=False,
        formatter_class=formatter,
    )
    assert isinstance(child, _Parser)
    child.cmd_path = path
    if spec is not None:  # primero los flags de la operación y después los globales
        child.op_spec = spec
        child.set_defaults(**{_SPEC_DEST: spec})
        _add_params(child, spec, t)
    _add_globals(child, t)
    return child


def build_parser(registry: Sequence[OpSpec], t: Translator) -> argparse.ArgumentParser:
    """Parser de la CLI generado de ``registry``, con la ayuda en el idioma de ``t``.

    ``ValueError`` si el registro no se puede expresar como CLI: palabras repetidas
    o que son a la vez operación y grupo, parámetros con nombres reservados o
    flags que chocan con las opciones globales.
    """
    formatter = _formatter_class(t)
    prog = _meta.NAME
    root = _Parser(
        prog=prog,
        description=t.get("cli.help.description", prog=prog),
        epilog=t.get("cli.help.epilog", prog=prog),
        add_help=False,
        allow_abbrev=False,
        formatter_class=formatter,
    )
    _add_globals(root, t)
    by_path: dict[tuple[str, ...], _Parser] = {(): root}
    for spec in registry:
        if not spec.cli or not all(_WORD_RE.match(w) for w in spec.cli):
            raise ValueError(f"{spec.id}: palabras de CLI no admitidas {spec.cli!r}")
        for depth in range(1, len(spec.cli)):
            path = spec.cli[:depth]
            node = by_path.get(path)
            if node is None:
                summary = t.get("cli.group." + ".".join(path) + ".summary")
                by_path[path] = _add_child(by_path[path[:-1]], path[-1], path, summary, t, formatter)
            elif node.op_spec is not None:
                raise ValueError(f"{spec.id}: {' '.join(path)} ya es una operación")
        if spec.cli in by_path:
            raise ValueError(f"{spec.id}: {' '.join(spec.cli)} ya está registrada")
        by_path[spec.cli] = _add_child(by_path[spec.cli[:-1]], spec.cli[-1], spec.cli,
                                       t.get(f"cli.op.{spec.id}.summary"), t, formatter, spec=spec)
    root.by_path = by_path
    return root


# -- conversión y validación de argumentos ----------------------------------------


def _to_int(raw: object) -> int | None:
    if isinstance(raw, str) and _INT_RE.match(raw):
        try:
            return int(raw)
        except ValueError:  # más dígitos de los que admite int()
            return None
    return None


def _convert_flags(spec: OpSpec, given: Mapping[str, object]) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Valores de los flags con el tipo del parámetro (argparse los entrega como texto)."""
    values: dict[str, object] = {}
    errors: list[dict[str, object]] = []
    for p in spec.params:
        if p.name not in given:
            continue
        raw = given[p.name]
        if p.type == "integer":
            number = _to_int(raw)
            if number is None:
                errors.append(_err(p.name, "type"))
            else:
                values[p.name] = number
        elif p.type == "array":
            items = list(raw) if isinstance(raw, (list, tuple)) else [raw]
            if p.cli_csv:  # --ai-use a,b (y también --ai-use a --ai-use b)
                items = [part.strip() for item in items for part in str(item).split(",") if part.strip()]
            item_type = ((p.schema or {}).get("items") or {})
            if isinstance(item_type, Mapping) and item_type.get("type") == "integer":
                converted: list[object] = []
                for index, item in enumerate(items):
                    number = _to_int(item)
                    if number is None:
                        errors.append(_err(p.name, "type", index=index))
                    converted.append(number)
                values[p.name] = converted
            else:
                values[p.name] = items
        elif p.type == "boolean":
            values[p.name] = bool(raw)
        else:
            values[p.name] = raw
    return values, errors


def _cli_schema(spec: OpSpec) -> dict[str, object]:
    """Esquema de los parámetros tal como llegan por flags (incluye los ``cli_only``)."""
    return {
        "type": "object",
        "properties": {p.name: param_schema(p) for p in spec.params},
        "required": [p.name for p in spec.params if p.required],
    }


def _reject_constant(name: str) -> object:
    raise ValueError(f"constante JSON no admitida: {name}")


# -- escritura -------------------------------------------------------------------


def _utf8_streams(*streams: object) -> None:
    """REQ-OPS-11: en Windows, stdout y stderr en UTF-8 antes de escribir."""
    if sys.platform != "win32":
        return
    for stream in streams:
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except (AttributeError, OSError, ValueError):
                pass


def _clean(text: str, *, multiline: bool = False) -> str:
    """Escapa los caracteres de control y bidireccionales antes de escribir en un terminal."""
    if multiline:
        text = text.replace("\r\n", "\n")
    if text.isprintable():
        return text
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if ch == "\t" or (ch == "\n" and multiline):
            out.append(ch)
        elif unicodedata.category(ch) == "Cc" or code in _BIDI:
            out.append(f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}")
        else:
            out.append(ch)
    return "".join(out)


def _one_line_details(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Copia de ``data`` con cada ``detail`` en una sola línea: ``\\n``, ``\\r`` y los demás
    controles, escapados como en ``_clean``.

    ``detail`` es un texto de una línea (``evidence_check``, ``step``) que puede citar datos
    ajenos, como la ruta de un archivo de un registro de evidencia. El ``render_text`` de la
    operación lo pone en su línea y ``_clean`` con ``multiline`` conserva los saltos, así que
    sin esto un dato así podría agregar líneas falsas a la salida legible.
    """

    def walk(value: object) -> object:
        if isinstance(value, Mapping):
            return {k: _clean(v) if k == "detail" and isinstance(v, str) else walk(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [walk(item) for item in value]
        return value

    out = walk(data)
    return out if isinstance(out, dict) else data


def _write_text(stream: TextIO | None, text: str) -> None:
    if stream is None:
        return
    if not text.endswith("\n"):
        text += "\n"
    try:
        try:
            stream.write(text)
        except UnicodeEncodeError:
            encoding = getattr(stream, "encoding", None) or "ascii"
            stream.write(text.encode(encoding, "backslashreplace").decode(encoding, "replace"))
        stream.flush()
    except (OSError, ValueError):  # tubería cerrada o flujo ya cerrado
        pass


def _write_bytes(stream: TextIO | None, data: bytes) -> None:
    """REQ-ARCH-53: la salida de máquina va en UTF-8 sobre ``stream.buffer`` si existe."""
    if stream is None:
        return
    try:
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
            stream.flush()
            buffer.write(data)
            buffer.flush()
        else:
            stream.write(data.decode("utf-8"))
            stream.flush()
    except (OSError, ValueError):
        pass


def _scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Mapping):
        return "{}"
    if isinstance(value, (list, tuple)):
        return "[]"
    return _clean(str(value))


def _is_leaf(value: object) -> bool:
    return not isinstance(value, (Mapping, list, tuple)) or not value


def _data_lines(value: object, depth: int) -> list[str]:
    """Vista legible genérica de ``data``: claves y valores tal como están en el sobre."""
    pad = "  " * depth
    if depth > 12:  # estructura anómala: se muestra compacta
        return [pad + _clean(json.dumps(value, ensure_ascii=False, default=str))]
    lines: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _is_leaf(item):
                lines.append(f"{pad}{_clean(str(key))}: {_scalar(item)}")
            else:
                lines.append(f"{pad}{_clean(str(key))}:")
                lines.extend(_data_lines(item, depth + 1))
    elif isinstance(value, (list, tuple)):
        for item in value:
            if _is_leaf(item):
                lines.append(f"{pad}- {_scalar(item)}")
            else:
                lines.append(f"{pad}-")
                lines.extend(_data_lines(item, depth + 1))
    else:
        lines.append(pad + _scalar(value))
    return lines


def _is_dry_run(data: object) -> bool:
    return isinstance(data, Mapping) and data.get("dry_run") is True and isinstance(data.get("plan"), list)


def _render_dry_run(result: Result, t: Translator) -> str:
    """``dry_run_data`` es común a todas las operaciones (05 §3.1), así que lo muestra la CLI."""
    assert isinstance(result.data, Mapping)
    lines = [t("cli.output.dry_run", op=result.op)]
    for effect in result.data.get("plan", []):
        if isinstance(effect, Mapping):
            lines.append("  - {} {}: {}".format(
                _scalar(effect.get("kind")), _scalar(effect.get("target")), _scalar(effect.get("summary"))))
        else:
            lines.append("  - " + _scalar(effect))
    return "\n".join(lines)


def render_generic(result: Result, t: Translator) -> str:
    """Resumen legible cuando el módulo de la operación no exporta ``render_text``."""
    lines = [t("cli.output.ok" if result.ok else "cli.output.failed", op=result.op)]
    if isinstance(result.data, Mapping):
        lines.extend(_data_lines(result.data, 1))
    return "\n".join(lines)


# -- ejecución --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Prescan:
    """Opciones globales leídas antes de analizar, para localizar ayuda y errores de uso."""

    json: bool
    lang: str | None
    root: str | None


def _prescan(argv: Sequence[str]) -> _Prescan:
    json_mode = False
    lang: str | None = None
    root: str | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            break
        if arg == "--json":
            json_mode = True
        elif arg in ("--lang", "--root") and i + 1 < len(argv):
            if arg == "--lang":
                lang = argv[i + 1]
            else:
                root = argv[i + 1]
            i += 1
        elif arg.startswith("--lang="):
            lang = arg[len("--lang="):]
        elif arg.startswith("--root="):
            root = arg[len("--root="):]
        i += 1
    return _Prescan(json=json_mode, lang=lang if lang in LANGS else None, root=root)


class _Cli:
    """Estado de una invocación: flujos, entorno y lo que ya se sabe de la operación."""

    def __init__(self, registry: Sequence[OpSpec], env: Mapping[str, str],
                 out: TextIO | None, err: TextIO | None, inp: TextIO | None) -> None:
        self.registry = tuple(registry)
        self.env = dict(env)
        self.out = out
        self.err = err
        self.inp = inp
        self.json = False
        self.lang: str | None = None
        self.root: str | None = None
        self.spec: OpSpec | None = None

    # -- idioma --

    def _context(self, *, dry_run: bool = False, attest_tty: bool = False) -> OpContext:
        human = self.spec is not None and self.spec.human_only
        inv = Invocation(root=self.root, lang=self.lang, json=self.json, dry_run=dry_run,
                         surface="cli", human_confirmed=False, env=self.env, attest_tty=attest_tty,
                         confirm=self._human_confirm if human else None,
                         phrase=self._type_phrase if human else None,
                         secret=self._ask_secret if human else None,
                         ask=self._ask_line if human else None)
        return OpContext(inv)

    def _human_confirm(self, op: str, target: str, summary: str) -> HumanConfirmation:
        """Fase 2 de ``require_human`` (06-permisos §6): el plan y el reto ``<op> <target>`` en
        stderr, y una sola línea de stdin. EOF o un texto distinto: ``E_HUMAN_ONLY_NOT_CONFIRMED``."""
        t = self.full_t()
        stream = self.err if self.err is not None else sys.stderr
        challenge = f"{op} {target}"
        _write_text(stream, _clean(summary, multiline=True))  # el título del Issue es texto ajeno
        try:
            stream.write(t("cli.confirm.type_exactly", challenge=challenge))
            stream.flush()
        except (OSError, ValueError):
            pass
        line = self.inp.readline() if self.inp is not None else ""
        if not line or line.strip() != challenge:  # un solo intento
            raise OpError("E_HUMAN_ONLY_NOT_CONFIRMED")
        return HumanConfirmation(op, datetime.now(timezone.utc))

    def _type_phrase(self, summary: str, phrase: str) -> bool:
        """Confirmación tecleada adicional de una operación H (``init``: la frase de 06-permisos
        REQ-PERM-40 o la del bypass de REQ-PERM-19): el texto en stderr y una línea de stdin."""
        t = self.full_t()
        stream = self.err if self.err is not None else sys.stderr
        _write_text(stream, _clean(summary, multiline=True))
        try:
            stream.write(t("cli.confirm.type_exactly", challenge=phrase))
            stream.flush()
        except (OSError, ValueError):
            pass
        line = self.inp.readline() if self.inp is not None else ""
        return " ".join(line.split()).casefold() == " ".join(phrase.split()).casefold()

    def _ask_line(self, prompt: str, default: str) -> str:
        """Una pregunta de una operación H (``init``): el texto en stderr y una línea de stdin; la
        línea vacía (o EOF) es ``default``."""
        stream = self.err if self.err is not None else sys.stderr
        try:
            stream.write(_clean(prompt, multiline=True))
            stream.flush()
        except (OSError, ValueError):
            pass
        line = self.inp.readline() if self.inp is not None else ""
        return line.strip() or default

    def _ask_secret(self, prompt: str) -> str:
        """Secreto sin eco (10-seguridad REQ-SEC-30): con una terminal real, ``getpass``; si no
        (pruebas), una línea de stdin. Nunca se escribe en ningún flujo."""
        stream = self.err if self.err is not None else sys.stderr
        if self.inp is sys.stdin and stdio_is_tty():
            import getpass

            try:
                return getpass.getpass(prompt, stream=stream)
            except (EOFError, KeyboardInterrupt):
                return ""
        try:
            stream.write(prompt)
            stream.flush()
        except (OSError, ValueError):
            pass
        line = self.inp.readline() if self.inp is not None else ""
        return line.strip()

    def _tty(self) -> bool:
        """stdin y stdout son una terminal (la confirmación de attest-v1 se pide solo así)."""
        return stdio_is_tty()

    def _ask_attest(self) -> bool:
        """Muestra attest-v1 en el idioma resuelto y pide confirmarla (11-trazabilidad §2.8)."""
        t = self.full_t()
        _write_text(self.out, t("cli.attest.text"))
        if self.out is not None:
            self.out.write(t("cli.attest.question"))
            self.out.flush()
        line = self.inp.readline() if self.inp is not None else ""
        return line.strip().casefold() in {"s", "si", "sí", "y", "yes"}

    def _ask(self, spec: OpSpec, p: Param) -> str | None:
        """Pide en stderr el parámetro ``p`` que falta y lee una línea de stdin (``Param.cli_ask``)."""
        t = self.full_t()
        stream = self.err if self.err is not None else sys.stderr
        try:
            stream.write(t(f"cli.ask.{spec.id}.{p.name}"))
            stream.flush()
        except (OSError, ValueError):
            pass
        line = self.inp.readline() if self.inp is not None else ""
        return line.strip() or None

    def _load_files(self, spec: OpSpec, values: dict[str, object]) -> list[dict[str, object]]:
        """``cli_file_for``: el contenido del archivo pasa a ser el valor del otro parámetro."""
        errors: list[dict[str, object]] = []
        for p in spec.params:
            if p.cli_file_for is None or p.name not in values:
                continue
            target = p.cli_file_for
            path = values.pop(p.name)
            if target in values:
                errors.append(_err(p.name, "exclusive", **{"with": [target]}))
                continue
            try:
                with open(str(path), "rb") as fh:
                    raw = fh.read(MAX_INPUT_BYTES + 1)
            except OSError:
                errors.append(_err(p.name, "unreadable"))
                continue
            if len(raw) > MAX_INPUT_BYTES:
                errors.append(_err(p.name, "too_large"))
                continue
            try:
                text = raw.decode("utf-8-sig").replace("\r\n", "\n")
                values[target] = json.loads(text, parse_constant=_reject_constant) if p.cli_file_json else text
            except (UnicodeDecodeError, ValueError, RecursionError):
                errors.append(_err(p.name, "json" if p.cli_file_json else "unreadable"))
        return errors

    def cheap_t(self) -> Translator:
        """Idioma sin leer el proyecto (``--lang`` y la variable de entorno)."""
        try:
            return translator(resolve_lang(cli=self.lang, env=self.env, project=None))
        except Exception:
            return translator("en")

    def full_t(self, ctx: OpContext | None = None) -> Translator:
        """Idioma resuelto como en las operaciones: también el de la base (REQ-ARCH-69)."""
        own = ctx is None
        if ctx is None:
            try:
                ctx = self._context()
            except Exception:
                return self.cheap_t()
        try:
            return ctx.t()
        except Exception:
            return self.cheap_t()
        finally:
            if own:
                with contextlib.suppress(Exception):
                    ctx.close()

    # -- salida --

    def print_error(self, error: ErrorObj, t: Translator) -> None:
        """REQ-OPS-10: ``error[<code>]: <message>`` y ``sugerencia:``/``hint:`` en stderr."""
        _write_text(self.err, f"error[{error.code}]: {_clean(error.message)}")
        if error.hint:
            _write_text(self.err, f"{t.get('cli.output.hint') or 'hint'}: {_clean(error.hint)}")
        if error.details:
            dumped = json.dumps(error.details, ensure_ascii=False, separators=(",", ":"), default=str)
            label = t.get("cli.output.details") or "details"
            _write_text(self.err, f"{label}: {_clean(clip(dumped, MAX_DETAILS_CHARS))}")

    def _human_text(self, result: Result, spec: OpSpec | None, t: Translator, fmt: str) -> str:
        if _is_dry_run(result.data):
            return _render_dry_run(result, t)
        render = None
        if spec is not None:
            try:
                render = getattr(load_op_module(spec), "render_text", None)
            except Exception:
                render = None
        if callable(render):
            try:
                text = render(replace(result, data=_one_line_details(result.data)), t, fmt=fmt)
            except Exception:  # un fallo del render no cambia el resultado
                text = None
            if isinstance(text, str):
                return _clean(text, multiline=True)
        return render_generic(result, t)

    def emit(self, result: Result, spec: OpSpec | None, t: Translator, *,
             fmt: str = "text", see_help: str | None = None) -> int:
        """Escribe el sobre (``--json``) o la salida legible y devuelve el código de salida."""
        if self.json:
            _write_bytes(self.out, dumps(result))
            return result.exit_code()
        if result.data is not None:
            text = self._human_text(result, spec, t, fmt)
            if text:
                _write_text(self.out, text)
        elif result.ok:
            _write_text(self.out, t("cli.output.ok", op=result.op))
        label = t.get("cli.output.warning") or "warning"
        for warning in result.warnings:
            _write_text(self.err, f"{label}[{warning.code}]: {_clean(warning.message)}")
        if result.error is not None:
            self.print_error(result.error, t)
        if see_help:
            _write_text(self.err, t("cli.output.see_help", prog=see_help))
        return result.exit_code()

    # -- errores de uso --

    def usage(self, spec: OpSpec | None, errors: list[dict[str, object]], prog: str) -> int:
        """``E_VALIDATION_INPUT`` con salida 3 (REQ-OPS-15, REQ-OPS-41).

        Sin operación identificada (falta, no existe o está reservada) no hay un
        ``op`` válido para el sobre (``$defs/op``), así que el error va solo a
        stderr, igual que MCP responde a una herramienta desconocida con un error
        de protocolo y no con un sobre.
        """
        details: dict[str, object] = {"errors": errors}
        if spec is None:
            t = self.full_t()
            self.print_error(make_error(USAGE_CODE, t, details=details), t)
            _write_text(self.err, t("cli.output.see_help", prog=prog))
            return _exit_code(False, USAGE_CODE)
        ctx = self._context()
        try:
            t = self.full_t(ctx)
            result = fail(spec.id, make_error(USAGE_CODE, t, details=details))
        finally:
            with contextlib.suppress(Exception):
                ctx.close()
        return self.emit(result, spec, t, see_help=prog)

    # -- flujo principal --

    def help(self, parser: argparse.ArgumentParser, first_t: Translator) -> int:
        t = self.full_t()
        target = parser
        if t.lang != first_t.lang:
            rebuilt = build_parser(self.registry, t)
            by_path = getattr(rebuilt, "by_path", None) or {}
            target = by_path.get(getattr(parser, "cmd_path", ()), rebuilt)
        _write_text(self.out, target.format_help())
        return 0

    def _read_input(self, source: str) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
        """Objeto JSON de ``--input <archivo|->`` (05 §4)."""
        try:
            if source == "-":
                raw = self._read_stdin()
            else:
                with open(source, "rb") as fh:
                    raw = fh.read(MAX_INPUT_BYTES + 1)
        except OSError:
            raw = None
        if raw is None:
            return None, [_err("input", "unreadable")]
        if len(raw) > MAX_INPUT_BYTES:
            return None, [_err("input", "too_large")]
        try:
            obj = json.loads(raw.decode("utf-8-sig"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError, RecursionError):
            return None, [_err("input", "json")]
        if not isinstance(obj, dict):
            return None, [_err("input", "type")]
        return obj, []

    def _read_stdin(self) -> bytes | None:
        stream = self.inp
        if stream is None:
            return None
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
            data = buffer.read(MAX_INPUT_BYTES + 1)
            return data if isinstance(data, bytes) else None
        text = stream.read(MAX_INPUT_BYTES + 1)
        return text.encode("utf-8") if isinstance(text, str) else None

    def main(self, argv: Sequence[str]) -> int:
        pre = _prescan(argv)
        self.json, self.lang, self.root = pre.json, pre.lang, pre.root
        first_t = self.cheap_t()
        parser = build_parser(self.registry, first_t)
        try:
            ns, extras = parser.parse_known_args(list(argv))
        except _VersionRequested:
            _write_text(self.out, _meta.__version__)
            return 0
        except _HelpRequested as req:
            return self.help(req.parser, first_t)
        except _UsageError as exc:
            spec = getattr(exc.parser, "op_spec", None)
            return self.usage(spec, [_err(None, "usage", message=exc.message)], exc.parser.prog)

        spec = getattr(ns, _SPEC_DEST, None)
        self.json = bool(getattr(ns, "g_json", False))
        self.root = getattr(ns, "g_root", None)
        lang = getattr(ns, "g_lang", None)
        self.lang = lang if lang in LANGS else None
        if not isinstance(spec, OpSpec):
            path: list[str] = []
            for depth in range(8):
                word = getattr(ns, "command" if depth == 0 else f"command_{depth}", None)
                if not isinstance(word, str):
                    break
                path.append(word)
            if not path and not extras:
                return self.home()  # sin operación: la pantalla de inicio (REQ-OPS-68)
            prog = " ".join([_meta.NAME, *path])
            return self.usage(None, [_err(None, "missing_command")], prog)
        self.spec = spec
        prog = " ".join((_meta.NAME, *spec.cli))

        errors: list[dict[str, object]] = []
        if lang is not None and lang not in LANGS:
            errors.append(_err("lang", "enum"))
        given = {p.name: getattr(ns, _dest(p)) for p in spec.params if hasattr(ns, _dest(p))}
        if extras:
            positional = [p for p in spec.params if p.positional and p.type == "array"]
            plain = all(not arg.startswith("-") or arg == "-" for arg in extras)
            if positional and plain:
                # argparse no mezcla posicionales tras un flag: 'a --task 3 b' deja 'b' suelto.
                name = positional[-1].name
                previous = given.get(name, [])
                given[name] = [*(previous if isinstance(previous, list) else []), *extras]
            else:
                errors.append(_err(None, "unrecognized", args=list(extras)))
        if errors:
            return self.usage(spec, errors, prog)

        dry_run = bool(getattr(ns, "g_dry_run", False))
        source = getattr(ns, "g_input", None)
        if source is not None:
            outside = sorted(name for name in given if name not in spec.input_with)
            if outside:
                return self.usage(spec, [_err("input", "exclusive", **{"with": outside})], prog)
            flagged, errors = _convert_flags(spec, given)
            obj, read_errors = self._read_input(source)
            errors += read_errors
            if obj is not None and not errors:
                for name, value in flagged.items():  # 05 §9.17: si el JSON los trae, deben coincidir
                    if name in obj and obj[name] != value:
                        errors.append(_err(name, "mismatch"))
                    obj[name] = value
                if not errors:
                    errors = _schema_errors(obj, input_schema(spec))
            if errors or obj is None:
                return self.usage(spec, errors, prog)
            values = dict(obj)
            project_root = values.pop("project_root", None)
            if isinstance(project_root, str):
                self.root = project_root  # REQ-OPS-4: project_root antes que --root
            dry_run = dry_run or bool(values.pop("dry_run", False))
        else:
            values, errors = _convert_flags(spec, given)
            if not errors:
                errors = self._load_files(spec, values)
            if not errors and (values or any(p.required for p in spec.params)):
                errors = _schema_errors(values, _cli_schema(spec))
            if errors:
                return self.usage(spec, errors, prog)

        # run(ctx, **kwargs) recibe todos los parámetros: los no indicados, con su default.
        kwargs = {p.name: values[p.name] if p.name in values else copy.deepcopy(p.default) for p in spec.params}
        fmt = "text"
        if any(p.name == "format" and "github" in p.choices for p in spec.params) and kwargs.get("format") == "github":
            fmt = "github"
        for p in spec.params:  # Param.cli_ask: lo que falta se pide en la terminal (05 §9.24)
            skip = p.cli_ask_unless is not None and kwargs.get(p.cli_ask_unless) is True
            if p.cli_ask and not skip and not kwargs.get(p.name) and not self.json and self._tty():
                answer = self._ask(spec, p)
                if answer is not None and p.choices and answer not in p.choices:
                    return self.usage(spec, [_err(p.name, "enum")], prog)
                kwargs[p.name] = answer
            chosen = kwargs.get(p.name)
            if p.cli_lang and chosen in LANGS and self.lang is None \
                    and self.env.get(_meta.ENV_PREFIX + "LANG") not in LANGS:
                self.lang = str(chosen)  # 01 REQ-ARCH-71: los mensajes, en el idioma elegido
        # 11-trazabilidad REQ-TRACE-22: sin --sign-off, la persona puede confirmar en la terminal.
        attested = spec.attest and kwargs.get("sign_off") is not True and not self.json and self._tty() \
            and self._ask_attest()
        ctx = self._context(dry_run=dry_run, attest_tty=bool(attested))
        if self.json:
            # REQ-OPS-9: con --json, stdout solo lleva el sobre; lo demás va a stderr.
            with contextlib.redirect_stdout(self.err if self.err is not None else sys.stderr):
                result = invoke(spec, ctx, **kwargs)
        else:
            result = invoke(spec, ctx, **kwargs)
        return self.emit(result, spec, self.full_t(ctx), fmt=fmt)

    def home(self) -> int:
        """Pantalla de inicio (05 REQ-OPS-68): ``aucove`` sin operación.

        Con ``--json`` es el sobre de ``status``. Fuera de un proyecto, solo el encabezado
        y cómo empezar. No llama a ningún modelo ni abre el agente (D-15, D-58).
        """
        spec = next((s for s in self.registry if s.id == "status"), None)
        if spec is None:  # un registro sin status (pruebas): como antes de H3
            return self.usage(None, [_err(None, "missing_command")], _meta.NAME)
        from ..ops.status import HomeExtras, home_extras, render_home

        defaults = {p.name: copy.deepcopy(p.default) for p in spec.params}
        if self.json:
            ctx = self._context()
            with contextlib.redirect_stdout(self.err if self.err is not None else sys.stderr):
                result = invoke(spec, ctx, **defaults)
            return self.emit(result, spec, self.full_t(ctx))
        probe = self._context()
        try:
            t = self.full_t(probe)
            try:
                extras = home_extras(probe)
            except Exception:  # noqa: BLE001 - sin base legible: el tablero dirá por qué
                extras = None
                inside = probe.project(required=False) is not None
            else:
                inside = extras is not None
        finally:
            with contextlib.suppress(Exception):
                probe.close()
        result = None
        if inside:
            ctx = self._context()
            result = invoke(spec, ctx, **defaults)
            t = self.full_t(ctx)
            if extras is None:
                extras = HomeExtras(project=None, parts=(), evidence=(), lang=t.lang)
        _write_text(self.out, _clean(render_home(result, extras, t), multiline=True))
        if result is None:
            return 0
        label = t.get("cli.output.warning") or "warning"
        for warning in result.warnings:
            _write_text(self.err, f"{label}[{warning.code}]: {_clean(warning.message)}")
        if result.error is not None:
            self.print_error(result.error, t)
        return result.exit_code()

    def crash(self, exc: BaseException) -> int:
        """Fallo de la propia envoltura: ``E_INTERNAL_UNEXPECTED`` sin el texto de ``exc``."""
        del exc  # su texto puede tener datos sensibles (como ops.result.internal)
        t = self.cheap_t()
        try:
            error = make_error("E_INTERNAL_UNEXPECTED", t, details={"trace_id": uuid.uuid4().hex[:12]})
            if self.spec is not None:
                self.emit(fail(self.spec.id, error), self.spec, t)
            else:  # sin operación identificada no hay sobre válido: solo stderr
                self.print_error(error, t)
        except Exception:
            pass
        return EXIT_INTERNAL


def run(
    argv: Sequence[str],
    *,
    registry: Sequence[OpSpec] | None = None,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    stdin: TextIO | None = None,
) -> int:
    """Ejecuta la CLI con ``argv`` (sin el nombre del programa) y devuelve el código de salida.

    ``registry`` por defecto es ``ops.registry.REGISTRY``; ``env``, ``os.environ``.
    Nunca devuelve 2 ni deja escapar ``SystemExit``.
    """
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    inp = sys.stdin if stdin is None else stdin
    _utf8_streams(out, err)
    cli = _Cli(REGISTRY if registry is None else registry, os.environ if env is None else env, out, err, inp)
    try:
        return cli.main(list(argv))
    except KeyboardInterrupt:
        _write_text(err, cli.cheap_t()("cli.output.interrupted"))
        return EXIT_INTERRUPTED
    except (Exception, SystemExit) as exc:
        return cli.crash(exc)
