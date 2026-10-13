# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Subcomandos internos ``ci build-plan`` y ``ci pin-plan`` (09-ci §3.2 y §4.3; DC-23).

Los dos son deterministas, no emiten el sobre y escriben en stdout líneas
``clave=valor`` para ``$GITHUB_OUTPUT``. Validan cada valor antes de emitirlo y,
si alguno no es válido, no emiten nada y salen con 1 (falla cerrada, REQ-CI-8).
Salidas: 0 bien, 1 error o valor inválido, 3 error de uso; nunca 2.

- ``build-plan`` lee solo el ``project.yaml`` de la política (la base) y la lista de
  archivos cambiados; del directorio actual solo toma ``git rev-parse HEAD`` si no hay
  ``GITHUB_SHA``. El tope de tiempo vale de 1 a 12 minutos (00-marco D-33 o). La última
  salida, ``reason``, dice por qué no se compila: ``format-none`` (sin manuscrito, en los
  dos modos, REQ-CI-49) o ``no-changes`` (el PR no cambia entradas de la compilación);
  va vacía si se compila. Con ``format: none`` no lee ``build.*`` ni el commit: solo
  ``run``, ``language`` y ``reason`` llevan valor.
- ``pin-plan`` compara la línea ``core_pin`` del PR con la de la base y decide si el
  job ``pin`` instala la versión del PR (REQ-CI-45). Es de H2 (00-marco D-31 a).

La configuración y git se importan dentro de las funciones: ``cli/ci.py`` carga
este módulo para cualquier subcomando ``ci`` (REQ-ARCH-15).
"""

from __future__ import annotations

import os
import re
import stat
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TextIO

from ..domain.compat import satisfies
from .registry import CiCommand

if TYPE_CHECKING:
    import argparse

    from ..config.project import BuildConfig, ProjectConfig
    from .context import OpContext

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 3

MODES: tuple[str, ...] = ("pr", "base")
# Motor de build.engine -> opción de latexmk (09-ci §3.2).
ENGINE_FLAGS: Mapping[str, str] = {"pdflatex": "-pdf", "lualatex": "-pdflua", "xelatex": "-pdfxe"}
# build.shell_escape -> opción del motor (REQ-CI-14).
SHELL_FLAGS: Mapping[str, str] = {"disabled": "-no-shell-escape", "restricted": "-shell-restricted"}
TIMEOUT_MIN, TIMEOUT_MAX = 1, 12  # build.timeout_minutes (D-33 o; no el 1..60 de 09-ci §3.2)
RETENTION_MIN, RETENTION_MAX = 1, 90  # ci.pdf_retention_days (project.schema.json)
BASE_RETENTION = 90  # retención de los PDF de la base (REQ-CI-19)

# Orden exacto de las salidas de build-plan; reason, al final, es de D-43 (09-ci §3.2).
BUILD_PLAN_KEYS: tuple[str, ...] = (
    "run", "image", "engine_flag", "shell", "timeout", "main", "main_base",
    "sha7", "retention", "latexdiff", "language", "reason",
)
PIN_PLAN_KEYS: tuple[str, ...] = ("run", "pin")

# Motivos de una compilación omitida (salida reason; skip_reason de compile.yml, REQ-CI-49).
REASON_FORMAT_NONE = "format-none"  # project.yaml: format: none, sin manuscrito (D-43)
REASON_NO_CHANGES = "no-changes"  # modo pr: el PR no cambia ninguna entrada de la compilación
SKIP_REASONS: tuple[str, ...] = (REASON_FORMAT_NONE, REASON_NO_CHANGES)
# Con format: none solo estas salidas llevan valor; las de build.* van vacías (09-ci §3.2).
NONE_PLAN_VALUED: frozenset[str] = frozenset({"run", "language", "reason"})

MAIN_RE = re.compile(r"tex/[A-Za-z0-9._-]+\.tex")  # archivo directo en tex/ (09-ci §3.2)
# Imagen fijada por digest. Más estricta que el esquema: solo caracteres de una
# referencia de imagen y primer carácter alfanumérico, para que 'docker pull "$IMAGE"'
# nunca reciba una opción.
IMAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:-]*@sha256:[0-9a-f]{64}")
SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
PIN_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?")  # REQ-CI-45; pre-releases (D-66)
_OUTPUT_VALUE_RE = re.compile(r"[!-~]+")  # una sola línea ASCII imprimible, sin espacios

# Qué cambios del PR obligan a compilar (09-ci §3.2): las partes viven en sections/ (D-43).
BUILD_INPUT_PREFIXES: tuple[str, ...] = ("tex/", "sections/", "bib/")
BUILD_INPUT_FILES: frozenset[str] = frozenset(
    {"project.yaml", ".github/workflows/build.yml", ".github/workflows/compile.yml"}
)

MAX_POLICY_BYTES = 5 * 2**20  # límite de lectura de texto de REQ-CI-24
MAX_CHANGED_BYTES = 16 * 2**20

# Escapes de las rutas entre comillas de git (core.quotePath).
_GIT_ESCAPES: Mapping[str, int] = {
    "a": 0x07, "b": 0x08, "t": 0x09, "n": 0x0A, "v": 0x0B, "f": 0x0C, "r": 0x0D, '"': 0x22, "\\": 0x5C,
}
_OCTAL = frozenset("01234567")


class PlanError(ValueError):
    """Un valor del plan no es válido; ``key`` es la salida afectada."""

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.key = key


# -- utilidades comunes ------------------------------------------------------------


def say(ctx: OpContext, key: str, /, **kw: object) -> None:
    """Escribe en stderr el texto localizado de ``key`` (nunca en stdout: es ``$GITHUB_OUTPUT``)."""
    try:
        text = ctx.t()(key, **kw)
    except Exception:  # el traductor no lanza, pero ctx.t() podría
        text = key
    try:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()
    except (OSError, UnicodeError, ValueError):
        pass


def read_regular_file(path: Path, limit: int = MAX_POLICY_BYTES) -> bytes | None:
    """Bytes de ``path`` si es un archivo regular de ``limit`` bytes o menos; si no, ``None``.

    No sigue enlaces: ``lstat`` previo, ``O_NOFOLLOW`` donde existe y comprobación de que
    el descriptor abierto es el mismo archivo (REQ-CI-24).
    """
    try:
        before = os.lstat(path)
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except (OSError, ValueError):
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            after = os.fstat(fh.fileno())
            if not stat.S_ISREG(after.st_mode):
                return None
            if before.st_ino and after.st_ino and (before.st_ino, before.st_dev) != (after.st_ino, after.st_dev):
                return None
            data = fh.read(limit + 1)
    except OSError:
        return None
    if len(data) > limit:
        return None
    return data


def _decode(data: bytes) -> str | None:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _emit(out: TextIO, pairs: Sequence[tuple[str, str]]) -> None:
    out.write("".join(f"{key}={value}\n" for key, value in pairs))
    out.flush()


def _check_output(pairs: Sequence[tuple[str, str]], keys: Sequence[str], empty: Iterable[str] = ()) -> None:
    """Última barrera: las claves esperadas, en orden, y valores de una línea ASCII.

    Solo las claves de ``empty`` pueden ir vacías (``clave=`` deja la salida en ``''``).
    """
    if tuple(k for k, _v in pairs) != tuple(keys):
        raise PlanError("keys")
    allowed = frozenset(empty)
    for key, value in pairs:
        if not isinstance(value, str):
            raise PlanError(key)
        if value == "" and key in allowed:
            continue
        if _OUTPUT_VALUE_RE.fullmatch(value) is None:
            raise PlanError(key)


def _load_policy(
    ctx: OpContext, policy_dir: str, unreadable_key: str, invalid_key: str
) -> tuple[str, ProjectConfig] | None:
    """``(texto, ProjectConfig)`` del ``project.yaml`` de ``policy_dir``; ``None`` (ya informado) si falla."""
    from ..config.project import parse_project

    path = Path(policy_dir) / "project.yaml"
    data = read_regular_file(path)
    text = _decode(data) if data is not None else None
    if text is None:
        say(ctx, unreadable_key, path=str(path))
        return None
    cfg, findings = parse_project(text, file="project.yaml", t=ctx.t())
    if cfg is None:
        say(ctx, invalid_key)
        for f in findings[:10]:
            where = f.path or "project.yaml"
            if f.line is not None:
                where = f"{where}:{f.line}"
            say(ctx, "ci.plan.finding", where=where, code=f.code, message=f.message)
        return None
    return text, cfg


# -- build-plan --------------------------------------------------------------------


def unquote_git_path(line: str) -> str | None:
    """Ruta de una línea de ``git diff --name-only``; deshace las comillas de ``core.quotePath``.

    ``None`` si la línea empieza con comillas pero no es una ruta entre comillas válida.
    """
    if not line.startswith('"'):
        return line
    if len(line) < 2 or not line.endswith('"'):
        return None
    body = line[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == '"':
            return None
        if ch != "\\":
            out.extend(ch.encode("utf-8", "surrogateescape"))
            i += 1
            continue
        i += 1
        if i >= len(body):
            return None
        esc = body[i]
        if esc in _GIT_ESCAPES:
            out.append(_GIT_ESCAPES[esc])
            i += 1
        elif esc in _OCTAL and i + 3 <= len(body) and set(body[i:i + 3]) <= _OCTAL:
            value = int(body[i:i + 3], 8)
            if value > 0xFF:
                return None
            out.append(value)
            i += 3
        else:
            return None
    return out.decode("utf-8", "replace")


def is_build_input(path: str) -> bool:
    """``True`` si un cambio en ``path`` obliga a compilar en modo ``pr`` (09-ci §3.2)."""
    return path.startswith(BUILD_INPUT_PREFIXES) or path in BUILD_INPUT_FILES


def changes_need_build(lines: Iterable[str]) -> bool:
    """``run`` del modo ``pr``: algún archivo cambiado es de entrada de la compilación.

    Una línea que no se puede interpretar cuenta como cambio relevante: compilar de más
    no abre nada, compilar de menos dejaría pasar un PR sin compilarlo.
    """
    for raw in lines:
        line = raw.rstrip("\r")
        if not line:
            continue
        path = unquote_git_path(line)
        if path is None or is_build_input(path):
            return True
    return False


def read_changed(path: Path) -> list[str] | Literal["too_large"] | None:
    """Líneas de ``--changed``; ``None`` si no se puede leer y ``'too_large'`` si excede el tope."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_CHANGED_BYTES + 1)
    except (OSError, ValueError):
        return None
    if len(data) > MAX_CHANGED_BYTES:
        return "too_large"
    return data.decode("utf-8", "surrogateescape").split("\n")


def plan_values(
    build: BuildConfig,
    ci: Mapping[str, object],
    language: str,
    *,
    mode: str,
    run: bool,
    sha: str,
) -> list[tuple[str, str]]:
    """Salidas de build-plan en el orden de ``BUILD_PLAN_KEYS``; ``PlanError`` si alguna no es válida."""
    if mode not in MODES:
        raise PlanError("mode")
    image = build.image
    if not isinstance(image, str) or IMAGE_RE.fullmatch(image) is None:
        raise PlanError("image")
    engine_flag = ENGINE_FLAGS.get(build.engine) if isinstance(build.engine, str) else None
    if engine_flag is None:
        raise PlanError("engine_flag")
    shell = SHELL_FLAGS.get(build.shell_escape) if isinstance(build.shell_escape, str) else None
    if shell is None:
        raise PlanError("shell")
    timeout = build.timeout_minutes
    if type(timeout) is not int or not TIMEOUT_MIN <= timeout <= TIMEOUT_MAX:
        raise PlanError("timeout")
    main = build.main
    if not isinstance(main, str) or MAIN_RE.fullmatch(main) is None:
        raise PlanError("main")
    main_base = main[len("tex/"):-len(".tex")]
    if not isinstance(sha, str) or SHA_RE.fullmatch(sha) is None:
        raise PlanError("sha7")
    if mode == "pr":
        retention = ci.get("pdf_retention_days")
        if type(retention) is not int or not RETENTION_MIN <= retention <= RETENTION_MAX:
            raise PlanError("retention")
    else:
        retention = BASE_RETENTION
    latexdiff = ci.get("latexdiff")
    if not isinstance(latexdiff, bool):
        raise PlanError("latexdiff")
    if language not in ("es", "en"):
        raise PlanError("language")
    pairs = [
        ("run", "true" if run else "false"),
        ("image", image),
        ("engine_flag", engine_flag),
        ("shell", shell),
        ("timeout", str(timeout)),
        ("main", main),
        ("main_base", main_base),
        ("sha7", sha[:7]),
        ("retention", str(retention)),
        ("latexdiff", "true" if latexdiff else "false"),
        ("language", language),
        ("reason", "" if run else REASON_NO_CHANGES),  # con LaTeX, solo el modo pr omite
    ]
    _check_output(pairs, BUILD_PLAN_KEYS, empty=("reason",))
    return pairs


def no_manuscript_values(language: str) -> list[tuple[str, str]]:
    """Salidas de build-plan con ``format: none``: ``run=false`` y ``reason=format-none`` (REQ-CI-49).

    No se exigen ni se validan los valores de ``build.*`` (09-ci §3.2): esas salidas van
    vacías, igual que ``sha7``, ``retention`` y ``latexdiff``, que solo usan los pasos que
    compilan. ``language`` sí lleva valor: lo usa el resumen de ``ci comment``.
    """
    if language not in ("es", "en"):
        raise PlanError("language")
    values = {"run": "false", "language": language, "reason": REASON_FORMAT_NONE}
    pairs = [(key, values.get(key, "")) for key in BUILD_PLAN_KEYS]
    _check_output(pairs, BUILD_PLAN_KEYS, empty=(k for k in BUILD_PLAN_KEYS if k not in NONE_PLAN_VALUED))
    return pairs


def head_sha(ctx: OpContext) -> str | None:
    """``GITHUB_SHA`` si está definido; si no, ``git rev-parse HEAD`` en el directorio actual.

    Un ``GITHUB_SHA`` presente pero inválido no cae al respaldo: devuelve ``None``.
    """
    env_sha = ctx.inv.env.get("GITHUB_SHA")
    if env_sha:
        return env_sha if SHA_RE.fullmatch(env_sha) else None
    from ..vcs.git import Git, GitError, GitUnavailable

    try:
        sha = Git(Path(ctx.inv.cwd), env=ctx.inv.env).rev_parse("HEAD^{commit}")
    except (GitUnavailable, GitError, OSError, ValueError):
        return None
    return sha if sha and SHA_RE.fullmatch(sha) else None


def _add_build_plan_arguments(p: argparse.ArgumentParser) -> None:
    p.description = "ci.command.build-plan"
    p.add_argument("--mode", required=True, choices=MODES, help="ci.build_plan.arg_mode")
    p.add_argument("--policy", required=True, metavar="DIR", help="ci.build_plan.arg_policy")
    p.add_argument("--changed", metavar="FILE", help="ci.build_plan.arg_changed")


def build_plan_main(args: argparse.Namespace, ctx: OpContext, out: TextIO) -> int:
    """``ci build-plan``: escribe el plan de compilación de ``compile.yml``."""
    mode = args.mode
    if mode == "pr" and not args.changed:
        say(ctx, "ci.build_plan.changed_required")
        return EXIT_USAGE
    loaded = _load_policy(ctx, args.policy, "ci.build_plan.policy_unreadable", "ci.build_plan.policy_invalid")
    if loaded is None:
        return EXIT_ERROR
    _text, cfg = loaded
    if cfg.format == "none":  # sin manuscrito: aucove/build termina en éxito sin compilar (REQ-CI-49)
        try:
            pairs = no_manuscript_values(cfg.language)
        except PlanError as exc:
            say(ctx, "ci.plan.invalid_value", key=exc.key)
            return EXIT_ERROR
        say(ctx, "ci.build_plan.format_none")
        _emit(out, pairs)
        return EXIT_OK
    run = True
    if mode == "pr":
        changed = read_changed(Path(args.changed))
        if changed is None:
            say(ctx, "ci.build_plan.changed_unreadable", path=str(args.changed))
            return EXIT_ERROR
        if changed == "too_large":
            say(ctx, "ci.build_plan.changed_too_large")
        else:
            run = changes_need_build(changed)
    sha = head_sha(ctx)
    if sha is None:
        say(ctx, "ci.build_plan.no_sha")
        return EXIT_ERROR
    try:
        pairs = plan_values(
            cfg.build,
            cfg.ci,
            cfg.language,
            mode=mode,
            run=run,
            sha=sha,
        )
    except PlanError as exc:
        say(ctx, "ci.plan.invalid_value", key=exc.key)
        return EXIT_ERROR
    _emit(out, pairs)
    return EXIT_OK


# -- pin-plan ----------------------------------------------------------------------


PinReason = Literal["fork", "pr_pin_missing", "same", "unsatisfied", "run"]


@dataclass(frozen=True, slots=True)
class PinDecision:
    """Decisión del job ``pin``: ``pin`` es la versión con la que se evalúan las tres reglas."""

    run: bool
    pin: str
    reason: PinReason


def pin_decision(base_pin: str, core: str, pr_pin: str | None, *, has_tree: bool = True) -> PinDecision:
    """REQ-CI-45: ``run`` solo si el ``core_pin`` del PR difiere, tiene formato válido y satisface ``core``.

    Con ``run=false`` (sin árbol del PR, mismo pin, pin inválido o que no satisface ``core``),
    ``pin`` es el de la base: las reglas se evalúan con el núcleo de la base.

    «Difiere» se decide con el texto, no con la igualdad de PEP 440: los workflows bajan
    ``.../v${PIN}/requirements-lock.txt`` con el texto literal (REQ-CI-12), así que ``0.01.0``
    no es ``0.1.0`` y el job ``pin`` debe probarlo. Solo ``core`` se evalúa con PEP 440.
    """
    if PIN_RE.fullmatch(base_pin) is None:
        raise PlanError("pin")
    if not has_tree:
        return PinDecision(False, base_pin, "fork")
    if pr_pin is None or PIN_RE.fullmatch(pr_pin) is None:
        return PinDecision(False, base_pin, "pr_pin_missing")
    if pr_pin == base_pin:
        return PinDecision(False, base_pin, "same")
    if not satisfies(pr_pin, core):
        return PinDecision(False, base_pin, "unsatisfied")
    return PinDecision(True, pr_pin, "run")


def _pr_pin(tree: str) -> str | None:
    """Línea ``core_pin`` del ``project.yaml`` del PR, leído como datos y sin seguir enlaces."""
    from ..config.project import core_pin_line

    data = read_regular_file(Path(tree) / "project.yaml")
    text = _decode(data) if data is not None else None
    if text is None:
        return None
    return core_pin_line(text)


def _add_pin_plan_arguments(p: argparse.ArgumentParser) -> None:
    p.description = "ci.command.pin-plan"
    p.add_argument("--policy", required=True, metavar="DIR", help="ci.pin_plan.arg_policy")
    p.add_argument("--tree", metavar="DIR", help="ci.pin_plan.arg_tree")


def pin_plan_main(args: argparse.Namespace, ctx: OpContext, out: TextIO) -> int:
    """``ci pin-plan``: decide si el job ``pin`` instala el ``core_pin`` del PR (REQ-CI-45)."""
    from ..config.project import core_pin_line

    loaded = _load_policy(ctx, args.policy, "ci.pin_plan.base_unreadable", "ci.pin_plan.base_invalid")
    if loaded is None:
        return EXIT_ERROR
    text, cfg = loaded
    base_pin = core_pin_line(text)
    if base_pin is None or PIN_RE.fullmatch(base_pin) is None:
        say(ctx, "ci.pin_plan.base_pin_invalid")
        return EXIT_ERROR
    has_tree = bool(args.tree)
    pr_pin = _pr_pin(args.tree) if has_tree else None
    try:
        decision = pin_decision(base_pin, cfg.core, pr_pin, has_tree=has_tree)
        pairs = [("run", "true" if decision.run else "false"), ("pin", decision.pin)]
        _check_output(pairs, PIN_PLAN_KEYS)
        if PIN_RE.fullmatch(decision.pin) is None:
            raise PlanError("pin")
    except PlanError as exc:
        say(ctx, "ci.plan.invalid_value", key=exc.key)
        return EXIT_ERROR
    say(ctx, f"ci.pin_plan.{decision.reason}", pin=str(pr_pin), core=str(cfg.core))
    _emit(out, pairs)
    return EXIT_OK


COMMANDS: tuple[CiCommand, ...] = (
    CiCommand("build-plan", _add_build_plan_arguments, build_plan_main),
    CiCommand("pin-plan", _add_pin_plan_arguments, pin_plan_main),
)
