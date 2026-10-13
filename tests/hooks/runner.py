# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Ejecutor de los fixtures de hooks: esqueleto de T4 (12-mvp §3.5).

- ``load_cases`` carga ``tests/fixtures/hooks/<evento>/<caso>.json`` y valida cada
  caso contra ``case.schema.json`` y contra lo que el esquema no expresa: que ``id``
  coincida con la ruta, que el evento exista (07-hooks §3.1), que ``hook_event_name``
  sea el de ese evento y que ``roles_base`` nombre un fixture de ``tests/fixtures/roles``. Un caso mal
  formado lanza ``CaseError``. ``test_fixtures.py`` llama a ``load_cases`` al
  importarse, así que un caso mal formado hace fallar la recolección.
- ``resolve_launcher`` busca el lanzador instalado en el PATH (REQ-MVP-30) con
  ``find_launcher`` de ``doctor``, que solo mira carpetas absolutas, e ``invoke`` lo
  ejecuta como subproceso sin shell, igual que la forma exec de 07-hooks (REQ-HOOK-1).
- ``evaluate`` aplica REQ-MVP-31 y las reglas de salida de 07-hooks (REQ-HOOK-6,
  REQ-HOOK-12, REQ-HOOK-13 y REQ-HOOK-14).
- Mientras ``aucove hook`` salga con 2 y el mensaje fijo de «no disponible» de
  ``entry`` (H2 y H3), ``skip_reason`` devuelve ``'aucove hook: H4'`` y los
  casos se omiten.

Es autónomo (no importa ``tests.conftest``) para que la prueba de recolección
pueda copiarlo a un directorio temporal junto con los fixtures.
"""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
import sysconfig
import tempfile
import time
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Literal, cast

import yaml
from jsonschema import Draft202012Validator

from aucove import _meta
from aucove.ops.doctor import find_launcher  # REQ-ARCH-33 sin el directorio actual

TESTS_DIR: Path = Path(__file__).resolve().parent.parent
REPO_ROOT: Path = TESTS_DIR.parent
FIXTURES: Path = TESTS_DIR / "fixtures"
CASES_DIR: Path = FIXTURES / "hooks"
SCHEMA_NAME = "case.schema.json"
SCHEMA_PATH: Path = CASES_DIR / SCHEMA_NAME
TEMPLATE_PROJECT: Path = REPO_ROOT / "template" / "base" / "project.yaml"
TEMPLATE_VARS: Path = REPO_ROOT / "tools" / "template_vars.toml"

OsName = Literal["windows", "macos", "linux"]
CaseClass = Literal["deny", "allow", "fault"]
DecisionName = Literal["deny", "allow"]

# Valor de <evento> de `aucove hook` -> hook_event_name de Claude Code (07-hooks §3.1).
EVENTS: Mapping[str, str] = {
    "session-start": "SessionStart",
    "post-model-switch": "PostModelSwitch",
    "pre-tool-use": "PreToolUse",
    "post-tool-use": "PostToolUse",
    "post-tool-use-failure": "PostToolUseFailure",
    "stop": "Stop",
    "session-end": "SessionEnd",
    "config-change": "ConfigChange",
}

# `timeout` de cada definición de hook (07-hooks §3.1); session-end no lo fija y se usa
# su presupuesto de 1,5 s. Si el hook tarda más, Claude Code lo cancela: en PreToolUse,
# la llamada pasa (REQ-HOOK-10), así que exceder el plazo es una falla del caso.
EVENT_TIMEOUT_S: Mapping[str, float] = {
    "session-start": 10.0,
    "post-model-switch": 5.0,
    "pre-tool-use": 5.0,
    "post-tool-use": 10.0,
    "post-tool-use-failure": 10.0,
    "stop": 5.0,
    "session-end": 1.5,
    "config-change": 5.0,
}

HOOK_H4_REASON: str = f"{_meta.NAME} hook: H4"
BLOCK_EXIT = 2  # bloqueo en PreToolUse (07-hooks §3.2)
# Parte ASCII del mensaje bilingüe fijo de entry cuando falta el módulo de hooks.
UNAVAILABLE_MARK = b"not available in this version"
PROBE_TIMEOUT_S = 30.0
REPO_PLACEHOLDER = "${REPO}"
BASE_BRANCH = "main"

# Opciones puntuales de git (nunca 'git config'), las mismas de tests/conftest.py.
GIT_OPTIONS: tuple[str, ...] = (
    "-c", "commit.gpgsign=false",
    "-c", "tag.gpgsign=false",
    "-c", "core.autocrlf=false",
    "-c", "core.eol=lf",
    "-c", "core.fsmonitor=false",
    "-c", "init.defaultBranch=main",
)

_VAR = re.compile(r"\{\{var:([a-z0-9_]+)\}\}")
_BOM = b"\xef\xbb\xbf"


class CaseError(ValueError):
    """Un fixture de hooks mal formado."""


class RunnerError(RuntimeError):
    """No se pudo preparar o ejecutar un caso."""


@dataclass(frozen=True, slots=True)
class HookSetup:
    identity: str | None                       # None = sin caché de identidad
    branch: str
    roles_base: Path                           # archivo de tests/fixtures/roles
    task_sections: Mapping[int, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class Expect:
    decision: DecisionName
    reason_mentions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HookCase:
    id: str
    event: str
    cls: CaseClass
    os: tuple[OsName, ...]
    setup: HookSetup
    stdin: str | Mapping[str, object]          # cadena = bytes crudos en UTF-8
    expect: Expect
    path: Path


@dataclass(frozen=True, slots=True)
class HookRun:
    argv: tuple[str, ...]
    returncode: int | None                     # None = excedió el timeout
    stdout: bytes
    stderr: bytes
    elapsed_s: float
    timeout_s: float


# --- carga y validación de los casos ---------------------------------------------------


@functools.cache
def _validator(schema_path: Path) -> Draft202012Validator:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise CaseError(f"clave repetida {key!r}")
        out[key] = value
    return out


def _read_json(path: Path, rel: str) -> object:
    data = path.read_bytes()
    if data.startswith(_BOM):
        raise CaseError(f"{rel}: lleva BOM")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CaseError(f"{rel}: no es UTF-8 ({exc.reason})") from None
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates)
    except json.JSONDecodeError as exc:
        raise CaseError(f"{rel}: JSON inválido ({exc.msg}, línea {exc.lineno})") from None
    except CaseError as exc:
        raise CaseError(f"{rel}: {exc}") from None


def _pointer(parts: Sequence[object]) -> str:
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def resolve_roles_base(value: str, fixtures: Path = FIXTURES) -> Path:
    """Archivo de ``tests/fixtures/roles`` que nombra ``roles_base``: ``basic`` o ``roles/basic.yaml``."""
    rel = value if value.startswith("roles/") else f"roles/{value}.yaml"
    path = fixtures.joinpath(*rel.split("/"))
    if not path.is_file():
        raise CaseError(f"roles_base {value!r}: no existe tests/fixtures/{rel}")
    return path


def case_files(cases_dir: Path = CASES_DIR) -> list[Path]:
    """Archivos ``.json`` de ``cases_dir`` salvo el esquema, en orden estable."""
    schema = cases_dir / SCHEMA_NAME
    return sorted(
        (p for p in cases_dir.rglob("*.json") if p.is_file() and p != schema),
        key=lambda p: p.relative_to(cases_dir).as_posix(),
    )


def parse_case(path: Path, cases_dir: Path = CASES_DIR) -> HookCase:
    """Carga y valida un caso; ``CaseError`` si está mal formado."""
    rel = path.relative_to(cases_dir).as_posix()
    raw = _read_json(path, rel)
    errors = sorted(
        _validator(cases_dir / SCHEMA_NAME).iter_errors(raw),
        key=lambda e: [str(p) for p in e.absolute_path],
    )
    if errors:
        detail = "; ".join(f"{_pointer(list(e.absolute_path))}: {e.message}" for e in errors[:5])
        raise CaseError(f"{rel}: no valida contra {SCHEMA_NAME}: {detail}")
    case = cast(dict[str, object], raw)
    parts = PurePosixPath(rel).parts
    if len(parts) != 2:
        raise CaseError(f"{rel}: los casos van en <evento>/<caso>.json")
    event, stem = parts[0], parts[1].removesuffix(".json")
    if event not in EVENTS:
        raise CaseError(f"{rel}: evento desconocido {event!r} (07-hooks §3.1)")
    if case["id"] != f"{event}/{stem}":
        raise CaseError(f"{rel}: id {case['id']!r} no coincide con la ruta ({event}/{stem})")
    stdin = case["stdin"]
    if isinstance(stdin, dict) and stdin.get("hook_event_name") != EVENTS[event]:
        raise CaseError(f"{rel}: hook_event_name debe ser {EVENTS[event]!r} para {event}")
    setup = cast(dict[str, object], case["setup"])
    expect = cast(dict[str, object], case["expect"])
    sections = cast(dict[str, list[str]], setup["task_sections"])
    return HookCase(
        id=str(case["id"]),
        event=event,
        cls=cast(CaseClass, case["class"]),
        os=tuple(cast(list[OsName], case["os"])),
        setup=HookSetup(
            identity=cast("str | None", setup["identity"]),
            branch=str(setup["branch"]),
            roles_base=resolve_roles_base(str(setup["roles_base"]), cases_dir.parent),
            task_sections={int(n): tuple(ids) for n, ids in sorted(sections.items(), key=lambda kv: int(kv[0]))},
        ),
        stdin=cast("str | Mapping[str, object]", stdin),
        expect=Expect(
            decision=cast(DecisionName, expect["decision"]),
            reason_mentions=tuple(cast(list[str], expect.get("reason_mentions", []))),
        ),
        path=path,
    )


def load_cases(cases_dir: Path = CASES_DIR) -> tuple[HookCase, ...]:
    """Todos los casos de ``cases_dir``; el primero mal formado lanza ``CaseError``."""
    if not (cases_dir / SCHEMA_NAME).is_file():
        raise CaseError(f"falta {SCHEMA_NAME} en {cases_dir}")
    return tuple(parse_case(path, cases_dir) for path in case_files(cases_dir))


# --- lanzador y ejecución sin shell ----------------------------------------------------


def resolve_launcher(path: str | None = None) -> str | None:
    """Ruta del lanzador instalado, resuelta desde el PATH (REQ-MVP-30).

    Usa ``find_launcher`` de ``doctor`` (01 REQ-ARCH-33), no ``shutil.which``: en
    Python 3.11 para Windows, ``shutil.which`` antepone siempre el directorio actual, y
    en cualquier plataforma una entrada vacía o relativa de ``PATH`` se resuelve contra
    él. Un ``aucove.exe`` que solo está en el directorio actual no está en el PATH,
    así que no cuenta. ``find_launcher`` busca solo en carpetas absolutas y, en Windows,
    respeta ``PATHEXT``.

    ``path`` sustituye al ``PATH`` del proceso. Sin ``path`` busca en el ``PATH`` y, si
    no está, en el directorio de scripts del intérprete en curso, con la misma regla:
    es donde la instalación deja el lanzador cuando el venv no está activado. Nunca usa
    ``python -m``.
    """
    found = find_launcher(_meta.NAME, path=path)
    if found is None and path is None:
        scripts = sysconfig.get_path("scripts")
        if scripts:
            found = find_launcher(_meta.NAME, path=scripts)
    return found


def invoke(
    exe: str,
    args: Sequence[str],
    stdin: bytes,
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_s: float,
) -> HookRun:
    """Ejecuta ``exe args`` como subproceso sin shell, con ``stdin`` como bytes."""
    argv = (exe, *args)
    start = time.monotonic()
    try:
        proc = subprocess.run(
            list(argv),
            input=stdin,
            cwd=str(cwd),
            env=dict(env),
            capture_output=True,
            timeout=timeout_s,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        return HookRun(argv, None, _as_bytes(exc.stdout), _as_bytes(exc.stderr), time.monotonic() - start, timeout_s)
    return HookRun(argv, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - start, timeout_s)


def _as_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    return value.encode("utf-8") if isinstance(value, str) else value


def is_unavailable(run: HookRun) -> bool:
    """True si ``aucove hook`` respondió con el bloqueo fijo de «no disponible»."""
    return run.returncode == BLOCK_EXIT and UNAVAILABLE_MARK in run.stderr


def probe(exe: str) -> str | None:
    """``HOOK_H4_REASON`` mientras ``aucove hook`` no esté disponible; si no, ``None``.

    Ejecuta ``aucove hook`` sin evento en un directorio vacío: en H2 y H3 sale con 2
    y el mensaje fijo de ``entry``; con los hooks de H4, un evento desconocido sale
    con 0 (REQ-HOOK-9). Exigir también el mensaje evita omitir los casos en silencio
    si H4 contestara con 2 por otro motivo.
    """
    with tempfile.TemporaryDirectory(prefix="hook-probe-") as tmp:
        run = invoke(exe, ("hook",), b"", cwd=Path(tmp), env=hook_env(os.environ), timeout_s=PROBE_TIMEOUT_S)
    return HOOK_H4_REASON if is_unavailable(run) else None


@functools.cache
def hooks_unavailable_reason(exe: str) -> str | None:
    """``probe`` una sola vez por lanzador y proceso."""
    return probe(exe)


def current_os(platform: str | None = None) -> OsName:
    name = sys.platform if platform is None else platform
    if name.startswith("win"):
        return "windows"
    if name == "darwin":
        return "macos"
    return "linux"


def skip_reason(case: HookCase, exe: str | None) -> str | None:
    """Motivo para omitir el caso aquí, o ``None`` si hay que ejecutarlo."""
    here = current_os()
    if here not in case.os:
        return f"el caso solo corre en {', '.join(case.os)}"
    if exe is None:
        return f"{_meta.NAME} no está en el PATH (REQ-MVP-30)"
    return hooks_unavailable_reason(exe)


# --- preparación del repositorio del caso -----------------------------------------------


def hook_env(base: Mapping[str, str]) -> dict[str, str]:
    """Entorno del hook: sin variables propias heredadas y sin red (REQ-HOOK-3)."""
    env = {k: v for k, v in base.items() if not k.upper().startswith(_meta.ENV_PREFIX)}
    env[_meta.ENV_PREFIX + "OFFLINE"] = "1"
    return env


def render_project_yaml(template: Path = TEMPLATE_PROJECT, variables: Path = TEMPLATE_VARS) -> str:
    """``project.yaml`` de la plantilla con los valores por defecto de ``tools/template_vars.toml``."""
    values = tomllib.loads(variables.read_text(encoding="utf-8"))

    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise RunnerError(f"{variables.name} no define {name!r}")
        return str(values[name])

    return _VAR.sub(value, template.read_text(encoding="utf-8"))


def section_ids(roles_text: str) -> list[str]:
    data = yaml.safe_load(roles_text)
    sections = data.get("sections", []) if isinstance(data, dict) else []
    return [str(s["id"]) for s in sections if isinstance(s, dict) and "id" in s]


def _git(cwd: Path, env: Mapping[str, str], *args: str) -> str:
    if args and args[0] == "config":
        raise RunnerError("el ejecutor no ejecuta git config")
    proc = subprocess.run(
        ["git", *GIT_OPTIONS, *args],
        cwd=str(cwd),
        env=dict(env),
        capture_output=True,
        timeout=60,
        check=False,
        shell=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise RunnerError(f"git {' '.join(args)} falló: {err}")
    return proc.stdout.decode("utf-8", "replace").strip()


def prepare_repo(case: HookCase, dest: Path, git_env: Mapping[str, str]) -> Path:
    """Repositorio del caso en ``dest/repo``, con ``origin`` bare en ``dest/origin.git``.

    La base (``main``) trae el ``project.yaml`` de la plantilla, el ``roles.yaml`` de
    ``roles_base`` y un ``.tex`` por sección declarada; después queda activa la rama
    ``setup.branch``. ``git_env`` es un entorno aislado con identidad.
    """
    repo = dest / "repo"
    origin = dest / "origin.git"
    repo.mkdir(parents=True)
    roles_text = case.setup.roles_base.read_text(encoding="utf-8")
    files = {"project.yaml": render_project_yaml(), "roles.yaml": roles_text}
    for sid in section_ids(roles_text):
        files[f"sections/{sid}/{sid}.tex"] = f"% {sid}\n"
    for rel, text in files.items():
        target = repo.joinpath(*rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.replace("\r\n", "\n").encode("utf-8"))
    _git(dest, git_env, "init", "-q", "--bare", "-b", BASE_BRANCH, str(origin))
    _git(repo, git_env, "init", "-q", "-b", BASE_BRANCH)
    _git(repo, git_env, "remote", "add", "origin", str(origin))
    _git(repo, git_env, "add", "-A")
    _git(repo, git_env, "commit", "-q", "--no-verify", "-m", "base")
    _git(repo, git_env, "push", "-q", "origin", BASE_BRANCH)
    if case.setup.branch != BASE_BRANCH:
        _git(repo, git_env, "checkout", "-q", "-b", case.setup.branch)
    return repo


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def seed_local_state(repo: Path, setup: HookSetup, *, now: datetime | None = None) -> None:
    """Siembra las cachés locales que lee el hook: identidad y tareas (01 §7.3).

    Solo se llama al ejecutar un caso, es decir, con los hooks de H4 disponibles.
    ``identity: null`` deja el repositorio sin caché de identidad (caso fault de 12 §3.5).
    """
    from aucove.localstore.store import IdentityCache, Store, TaskCache  # perezoso

    ts = datetime.now(timezone.utc) if now is None else now
    store = Store(repo)
    if setup.identity is not None:
        # La ruta de hooks no tiene el token: la identidad se lee con token_fp=None.
        store.write_identity(IdentityCache(setup.identity, None, _iso(ts + timedelta(days=1))), token_fp=None)
    for number, sections in setup.task_sections.items():
        store.write_task(
            TaskCache(
                number=number,
                claimant=setup.identity,
                state="claimed",
                sections=sections,
                local_branch=f"task/{number}",
                fetched_at=_iso(ts),
            )
        )


def render_stdin(stdin: str | Mapping[str, object], repo: Path) -> bytes:
    """Bytes de stdin: el objeto con ``${REPO}`` sustituido, o la cadena cruda tal cual."""
    if isinstance(stdin, str):
        return stdin.encode("utf-8")
    root = str(repo)

    def sub(value: object) -> object:
        if isinstance(value, str):
            return value.replace(REPO_PLACEHOLDER, root)
        if isinstance(value, list):
            return [sub(v) for v in value]
        if isinstance(value, dict):
            return {k: sub(v) for k, v in value.items()}
        return value

    return json.dumps(sub(dict(stdin)), ensure_ascii=False).encode("utf-8")


def run_case(case: HookCase, tmp: Path, git_env: Mapping[str, str], exe: str) -> HookRun:
    """Prepara el repositorio del caso y ejecuta ``aucove hook <evento>`` en él."""
    repo = prepare_repo(case, tmp / "case", git_env)
    seed_local_state(repo, case.setup)
    return invoke(
        exe,
        ("hook", case.event),
        render_stdin(case.stdin, repo),
        cwd=repo,
        env=hook_env(git_env),
        timeout_s=EVENT_TIMEOUT_S[case.event],
    )


# --- evaluación (REQ-MVP-31, 07-hooks §3.2) --------------------------------------------


def parse_stdout(stdout: bytes) -> tuple[dict[str, object] | None, str | None]:
    """(objeto, problema). REQ-HOOK-12: un objeto JSON en UTF-8 sin BOM con ``\\n`` final, o nada."""
    if stdout == b"":
        return None, None
    if stdout.startswith(_BOM):
        return None, "stdout lleva BOM (REQ-HOOK-12)"
    try:
        obj = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "stdout no es un objeto JSON (REQ-HOOK-12)"
    if not isinstance(obj, dict):
        return None, "stdout no es un objeto JSON (REQ-HOOK-12)"
    if not stdout.endswith(b"\n"):
        return obj, "stdout no termina en \\n (REQ-HOOK-12)"
    return obj, None


def _specific(obj: Mapping[str, object] | None) -> Mapping[str, object]:
    value = obj.get("hookSpecificOutput") if obj is not None else None
    return value if isinstance(value, dict) else {}


def permission_decision(obj: Mapping[str, object] | None) -> str | None:
    value = _specific(obj).get("permissionDecision")
    return value if isinstance(value, str) else None


def reason_text(obj: Mapping[str, object] | None, stderr: bytes) -> str:
    """Razón visible: ``permissionDecisionReason`` del JSON más stderr."""
    parts: list[str] = []
    reason = _specific(obj).get("permissionDecisionReason")
    if isinstance(reason, str):
        parts.append(reason)
    parts.append(stderr.decode("utf-8", "replace"))
    return "\n".join(parts)


def evaluate(case: HookCase, run: HookRun) -> list[str]:
    """Problemas del resultado frente a ``case.expect``; vacío = el caso pasa."""
    if run.returncode is None:
        return [f"excedió el timeout de {run.timeout_s:g} s del evento (07-hooks §3.1, REQ-HOOK-10)"]
    problems: list[str] = []
    code = run.returncode
    if code not in (0, BLOCK_EXIT):
        problems.append(f"salida {code}: el núcleo sale solo con 0 o 2 (REQ-HOOK-13)")
    obj, bad = parse_stdout(run.stdout)
    if bad is not None:
        problems.append(bad)
    decision = permission_decision(obj)
    if decision == "allow":
        problems.append('permissionDecision "allow": el núcleo nunca lo responde (REQ-HOOK-6)')
    if case.cls == "fault":
        if code != BLOCK_EXIT:
            problems.append(f"clase fault: el único resultado aceptado es la salida 2, no {code} (REQ-MVP-31)")
    elif case.expect.decision == "deny":
        if not (code == BLOCK_EXIT or (code == 0 and decision == "deny")):
            problems.append(
                f'se esperaba una denegación (salida 2 o JSON permissionDecision "deny"); '
                f"salida {code}, decisión {decision!r} (REQ-MVP-31, REQ-HOOK-14)"
            )
    elif code != 0 or decision is not None:
        problems.append(f"se esperaba que la acción siguiera (salida 0 sin decisión); salida {code}, decisión {decision!r}")
    if case.expect.reason_mentions:
        reason = reason_text(obj, run.stderr).casefold()
        missing = [m for m in case.expect.reason_mentions if m.casefold() not in reason]
        if missing:
            problems.append(f"la razón no menciona {missing}")
    return problems
