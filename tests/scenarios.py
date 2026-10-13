# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Casos del conjunto dorado de A9 y escenarios de la regla ``scope``.

Cada caso de ``tests/fixtures/scope/cases/<id>.yaml`` describe un PR: la base (un árbol de
``tests/fixtures/scope/trees/<tree>/`` y un ``roles.yaml`` compartido de ``tests/fixtures/roles``), la tarea, el
PR, sus archivos, los pushes a la rama y lo que se espera. ``build_scenario`` lo convierte en:

- un repositorio git temporal con la base y la cabeza. Los commits se arman con la plomería
  de git (``update-index --index-info``, ``write-tree`` y ``commit-tree``), sin pasar por
  el árbol de trabajo, para que las rutas con barras invertidas, las variantes de
  mayúsculas y los enlaces simbólicos (modo ``120000``) queden en git tal cual, también en
  Windows. Los modos que ve la regla salen de ``git ls-tree`` (REQ-CI-24);
- ``base_dir``: un clon con ``main`` (el ``--policy`` de validate) y ``pr_dir``: el
  repositorio con todas las ramas y la cabeza del PR en ``HEAD`` (``None`` si el PR viene
  de un fork). Si la cabeza tiene rutas que el sistema de archivos no puede escribir (una
  barra invertida, un nombre que Windows no admite como ``aux.tex`` o ``x.tex.``, o dos
  rutas que solo difieren en mayúsculas), ``pr_dir`` queda solo con
  el índice, sin archivos en disco: la regla lee objetos de git, nunca el disco;
- un ``FakeForge`` sembrado con los mismos commits (mismos sha), el Issue de tarea con el
  cuerpo de 03 §8.2, la aceptación del ``lead``, el commit y el marcador de reclamo, la
  actividad de ``task/<n>``, el PR con su lista de archivos y las fallas inyectadas.

``scenario_rule_input`` arma el ``RuleInput`` del modo ``pr`` como lo haría el motor
y toma ``prior`` del caso; la prueba en proceso (``test_scope_golden``) lo usa tal
cual y la de punta a punta (``test_a9``) corre el motor completo y compara el prior calculado con el declarado.

Formato del caso (las claves desconocidas son error, para que una errata no pase):

.. code-block:: yaml

    id: s02-foreign-section            # igual al nombre del archivo
    description: texto
    covers: [ci-4.2:foreign-section]   # casos obligatorios que cubre (09 §4.2 y 12 A9)
    profile: enforced                  # o advisory
    base: {tree: basic, roles: basic, extra_files: {ruta: contenido}, advance: {ruta: contenido}}
    task: {number: 17, claimant: carla, sections: [methods], state: claimed}   # o null
    pr: {number: 42, author: carla, head_branch: task/17, same_repo: true, draft: false, merge_base: false}
    changes:
      - {status: modified, path: sections/results/results.tex, content: "..."}
    pushes: [{actor: carla, type: push}]
    faults: [{method: list_ref_activity, kind: http_5xx}]
    prior: {roles-sync: pass, generated-sync: pass}
    checks: [scope]
    expect: {valid: false, checks: {scope: fail}, codes: [E_PERM_SECTION_NOT_OWNED],
             paths: [sections/results/results.tex], warnings: [], mentions: [bruno]}

Semántica:

- ``base.advance`` agrega un commit a ``main`` después de crear la rama, y
  ``pr.merge_base`` (que exige ``advance``) fusiona ``main`` en la cabeza con un commit de
  fusión. La lista de archivos del PR es la de la API: el diff de tres puntos.
- ``task.state``: ``claimed`` (por defecto; con un PR abierto que no es borrador,
  ``derive_state`` da ``in_review``), ``proposed`` (sin aceptación), ``expired``
  (``state:expired`` puesta por el ``lead``; pide ``pr.draft``) o ``cancelled`` (Issue
  cerrado como ``not_planned``). ``claim_sections`` son las del marcador (por defecto,
  ``sections``) y ``body`` reemplaza el cuerpo generado.
- ``changes[].generated: true`` reemplaza el contenido por la regeneración de
  ``CODEOWNERS`` o ``task.yml`` desde el ``roles.yaml`` y el ``project.yaml`` de la cabeza.
  ``mode`` es el modo git del destino (``120000`` = enlace, con el destino como contenido).
  Un ``renamed`` sin ``content`` conserva el contenido, así que git lo ve como ``R100``.
- ``pushes``: eventos de ``list_ref_activity`` posteriores a la creación de la rama, que
  se siembra como ``branch_creation`` del reclamante (salvo que el primer evento sea un
  ``branch_creation``). Por defecto, un ``push`` del reclamante.
- ``expect.codes``, ``expect.paths`` y ``expect.mentions`` se refieren a los hallazgos de
  severidad ``error`` de ``scope``: los códigos distintos, las rutas no nulas y textos que
  deben aparecer en su mensaje o su pista. ``expect.warnings`` son los códigos de sus
  avisos. ``expect.valid`` es la conjunción de ``expect.checks``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from aucove import _meta
from aucove.config.base import read_policy_dir
from aucove.config.project import parse_project
from aucove.config.roles import parse_roles
from aucove.domain.paths import is_win32_alias
from aucove.domain.taskbody import TaskBodyError, parse_task_body, render_task_body
from aucove.forge.base import NULL_SHA, ChangedFile, LabelEvent, PullRequest, RefActivity, RepoRef
from aucove.forge.fake import FakeClock, FakeForge, Fault
from aucove.i18n import Translator
from aucove.ops.rules.base import Change, RuleInput
from aucove.render.generated import GENERATED_PATHS, generated_files
from aucove.vcs.git import Git
from aucove.vcs.tree import git_tree
from tests.conftest import FIXTURES, GIT_OPTIONS, roles_fixture

__all__ = [
    "BASE_BRANCH",
    "CI_LOGIN",
    "NOW",
    "PRIOR_DEFAULT",
    "REPO",
    "Scenario",
    "build_scenario",
    "case_paths",
    "cases_dir",
    "diff_changes",
    "load_case",
    "normalize_case",
    "scenario_rule_input",
    "trees_dir",
]

REPO = RepoRef("example-org", "demo")
BASE_BRANCH = "main"
CI_LOGIN = "github-actions[bot]"  # identidad del GITHUB_TOKEN en CI (08 REQ-FORGE-43)
T0 = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(hours=7)
PRIOR_DEFAULT: Mapping[str, str] = {"roles-sync": "pass", "generated-sync": "pass"}

STATUSES = ("added", "modified", "removed", "renamed")
TASK_STATES = ("claimed", "proposed", "expired", "cancelled")
PUSH_TYPES = ("push", "force_push", "branch_creation")
CHECK_STATUS = ("pass", "fail")
DEFAULT_PR_NUMBER = 42

# Opciones de git para armar los repositorios de los casos: además de las de aislamiento de las pruebas, se
# admiten rutas con '\' (core.protectNTFS) y se distinguen mayúsculas en el índice.
GIT_BUILD_OPTIONS: tuple[str, ...] = (
    *GIT_OPTIONS,
    "-c", "core.protectNTFS=false",
    "-c", "core.ignorecase=false",
    "-c", "core.symlinks=false",
)

_TOP = {"id", "description", "covers", "profile", "base", "task", "pr", "changes", "pushes", "faults",
        "prior", "checks", "expect"}
_BASE = {"tree", "roles", "extra_files", "advance"}
_TASK = {"number", "claimant", "sections", "claim_sections", "state", "author", "summary", "plan_refs", "body"}
_PR = {"number", "author", "head_branch", "same_repo", "draft", "merge_base", "title"}
_CHANGE = {"status", "path", "previous_path", "content", "mode", "generated"}
_PUSH = {"actor", "type"}
_FAULT = {"method", "kind", "nth"}
_EXPECT = {"valid", "checks", "codes", "paths", "warnings", "mentions"}
_MODES = {"100644", "100755", "120000"}

# Una ruta que el sistema de archivos local no puede escribir tal cual.
_UNPORTABLE = ("\\", ":", "*", "?", "\"", "<", ">", "|")


def cases_dir() -> Path:
    """Carpeta de los casos del conjunto dorado."""
    return FIXTURES / "scope" / "cases"


def trees_dir() -> Path:
    """Carpeta de los árboles base de los casos."""
    return FIXTURES / "scope" / "trees"


def case_paths() -> list[Path]:
    """Casos del conjunto dorado, en orden de nombre."""
    return sorted(cases_dir().glob("*.yaml"))


# -- carga y normalización ----------------------------------------------------------


def _keys(obj: object, allowed: set[str], where: str) -> dict[str, Any]:
    if not isinstance(obj, Mapping):
        raise ValueError(f"{where}: se esperaba un mapa")
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise ValueError(f"{where}: claves desconocidas {unknown}")
    return dict(obj)


def _str_list(value: object, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError(f"{where}: se esperaba una lista de cadenas")
    return list(value)


def _files_map(value: object, where: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise ValueError(f"{where}: se esperaba un mapa ruta -> contenido")
    return dict(value)


def normalize_case(raw: Mapping[str, object]) -> dict[str, Any]:
    """Caso con todos los valores por defecto; ``ValueError`` si no tiene la forma de un caso."""
    case = _keys(raw, _TOP, "caso")
    cid = case.get("id")
    if not isinstance(cid, str) or not cid:
        raise ValueError("caso: falta id")
    profile = case.get("profile", "enforced")
    if profile not in ("enforced", "advisory"):
        raise ValueError(f"{cid}: profile inválido {profile!r}")

    base = _keys(case.get("base") or {}, _BASE, f"{cid}.base")
    base_n = {
        "tree": str(base.get("tree", "basic")),
        "roles": str(base.get("roles", "basic")),
        "extra_files": _files_map(base.get("extra_files"), f"{cid}.base.extra_files"),
        "advance": _files_map(base.get("advance"), f"{cid}.base.advance"),
    }

    task_n: dict[str, Any] | None = None
    if case.get("task") is not None:
        task = _keys(case["task"], _TASK, f"{cid}.task")
        number = task.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ValueError(f"{cid}.task: number inválido")
        state = task.get("state", "claimed")
        if state not in TASK_STATES:
            raise ValueError(f"{cid}.task: state inválido {state!r}")
        sections = _str_list(task.get("sections"), f"{cid}.task.sections")
        claim_sections = task.get("claim_sections")
        task_n = {
            "number": number,
            "claimant": task.get("claimant"),
            "sections": sections,
            "claim_sections": _str_list(claim_sections, f"{cid}.task.claim_sections")
            if claim_sections is not None
            else list(sections),
            "state": state,
            "author": task.get("author"),
            "summary": str(task.get("summary") or f"Tarea {number} del caso {cid}."),
            "plan_refs": _str_list(task.get("plan_refs"), f"{cid}.task.plan_refs"),
            "body": task.get("body"),
        }
        if task_n["claimant"] is not None and not isinstance(task_n["claimant"], str):
            raise ValueError(f"{cid}.task: claimant inválido")
        if task_n["body"] is not None and not isinstance(task_n["body"], str):
            raise ValueError(f"{cid}.task: body inválido")

    pr = _keys(case.get("pr"), _PR, f"{cid}.pr")
    if not isinstance(pr.get("author"), str) or not pr["author"]:
        raise ValueError(f"{cid}.pr: falta author")
    default_head = f"task/{task_n['number']}" if task_n else None
    head_branch = pr.get("head_branch", default_head)
    if not isinstance(head_branch, str) or not head_branch:
        raise ValueError(f"{cid}.pr: falta head_branch")
    pr_n = {
        "number": int(pr.get("number", DEFAULT_PR_NUMBER)),
        "author": pr["author"],
        "head_branch": head_branch,
        "same_repo": bool(pr.get("same_repo", True)),
        "draft": bool(pr.get("draft", False)),
        "merge_base": bool(pr.get("merge_base", False)),
        "title": str(pr.get("title") or f"PR del caso {cid}"),
    }
    if pr_n["merge_base"] and not base_n["advance"]:
        raise ValueError(f"{cid}.pr: merge_base exige base.advance")

    changes: list[dict[str, Any]] = []
    for i, item in enumerate(case.get("changes") or []):
        ch = _keys(item, _CHANGE, f"{cid}.changes[{i}]")
        status = ch.get("status")
        if status not in STATUSES:
            raise ValueError(f"{cid}.changes[{i}]: status inválido {status!r}")
        path = ch.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError(f"{cid}.changes[{i}]: falta path")
        prev = ch.get("previous_path")
        if (status == "renamed") != isinstance(prev, str):
            raise ValueError(f"{cid}.changes[{i}]: previous_path va solo (y siempre) con renamed")
        mode = ch.get("mode")
        if mode is not None and str(mode) not in _MODES:
            raise ValueError(f"{cid}.changes[{i}]: mode inválido {mode!r}")
        content = ch.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError(f"{cid}.changes[{i}]: content debe ser texto")
        generated = bool(ch.get("generated", False))
        if generated and (content is not None or path not in GENERATED_PATHS):
            raise ValueError(f"{cid}.changes[{i}]: generated va sin content y solo en {GENERATED_PATHS}")
        if status == "added" and content is None and not generated:
            raise ValueError(f"{cid}.changes[{i}]: added necesita content")
        if status == "removed" and (content is not None or mode is not None or generated):
            raise ValueError(f"{cid}.changes[{i}]: removed no lleva content, mode ni generated")
        changes.append(
            {
                "status": status,
                "path": path,
                "previous_path": prev,
                "content": content,
                "mode": str(mode) if mode is not None else None,
                "generated": generated,
            }
        )

    pushes: list[dict[str, Any]] | None = None
    if case.get("pushes") is not None:
        if not isinstance(case["pushes"], list):
            raise ValueError(f"{cid}.pushes: se esperaba una lista")
        pushes = []
        for i, item in enumerate(case["pushes"]):
            p = _keys(item, _PUSH, f"{cid}.pushes[{i}]")
            kind = p.get("type", "push")
            if kind not in PUSH_TYPES:
                raise ValueError(f"{cid}.pushes[{i}]: type inválido {kind!r}")
            actor = p.get("actor")
            if actor is not None and not isinstance(actor, str):
                raise ValueError(f"{cid}.pushes[{i}]: actor inválido")
            pushes.append({"actor": actor, "type": kind})

    faults: list[dict[str, Any]] = []
    for i, item in enumerate(case.get("faults") or []):
        f = _keys(item, _FAULT, f"{cid}.faults[{i}]")
        if not isinstance(f.get("method"), str) or not isinstance(f.get("kind"), str):
            raise ValueError(f"{cid}.faults[{i}]: faltan method o kind")
        faults.append({"method": f["method"], "kind": f["kind"], "nth": f.get("nth")})

    prior = dict(PRIOR_DEFAULT)
    for key, value in _keys(case.get("prior") or {}, set(PRIOR_DEFAULT), f"{cid}.prior").items():
        if value not in CHECK_STATUS:
            raise ValueError(f"{cid}.prior: {key} inválido {value!r}")
        prior[key] = value

    checks = _str_list(case.get("checks"), f"{cid}.checks") or ["scope"]
    expect = _keys(case.get("expect"), _EXPECT, f"{cid}.expect")
    exp_checks = expect.get("checks")
    if not isinstance(exp_checks, Mapping) or "scope" not in exp_checks:
        raise ValueError(f"{cid}.expect: checks debe incluir scope")
    if any(v not in CHECK_STATUS for v in exp_checks.values()):
        raise ValueError(f"{cid}.expect.checks: estado inválido")
    if not set(exp_checks) <= set(checks):
        raise ValueError(f"{cid}.expect.checks: {sorted(set(exp_checks) - set(checks))} no están en checks")
    valid = expect.get("valid")
    if not isinstance(valid, bool):
        raise ValueError(f"{cid}.expect: falta valid")
    if valid != all(v == "pass" for v in exp_checks.values()):
        raise ValueError(f"{cid}.expect: valid no coincide con checks")
    expect_n = {
        "valid": valid,
        "checks": dict(exp_checks),
        "codes": sorted(set(_str_list(expect.get("codes"), f"{cid}.expect.codes"))),
        "paths": sorted(set(_str_list(expect.get("paths"), f"{cid}.expect.paths"))),
        "warnings": sorted(set(_str_list(expect.get("warnings"), f"{cid}.expect.warnings"))),
        "mentions": _str_list(expect.get("mentions"), f"{cid}.expect.mentions"),
    }
    if (exp_checks["scope"] == "pass") == bool(expect_n["codes"]):
        raise ValueError(f"{cid}.expect: scope pass va sin codes, y scope fail con al menos uno")

    return {
        "id": cid,
        "description": str(case.get("description") or ""),
        "covers": _str_list(case.get("covers"), f"{cid}.covers"),
        "profile": profile,
        "base": base_n,
        "task": task_n,
        "pr": pr_n,
        "changes": changes,
        "pushes": pushes,
        "faults": faults,
        "prior": prior,
        "checks": checks,
        "expect": expect_n,
    }


def load_case(path: Path) -> dict[str, Any]:
    """Caso normalizado de ``path``; su ``id`` debe ser el nombre del archivo."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    case = normalize_case(raw)
    if case["id"] != Path(path).stem:
        raise ValueError(f"{path}: id {case['id']!r} distinto del nombre del archivo")
    return case


# -- escenario ------------------------------------------------------------------------


@dataclass
class Scenario:
    case: Mapping[str, object]
    base_dir: Path  # checkout de la base (--policy)
    pr_dir: Path | None  # checkout de la cabeza con todas las ramas; None si fork
    forge: FakeForge
    head_sha: str
    base_sha: str
    # Añadidos compatibles con los casos existentes:
    pr: PullRequest | None = None
    merge_base_sha: str | None = None  # base del diff de tres puntos
    claim_sha: str | None = None
    repo_dir: Path | None = None  # repositorio armado (también en el caso de fork)
    checked_out: bool = False  # pr_dir tiene los archivos de la cabeza en disco
    git_env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _CommitRec:
    sha: str
    files: Mapping[str, tuple[str, bytes]]
    parents: tuple[str, ...]
    message: str
    author: str
    when: datetime


def _at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


class _Builder:
    """Commits por plomería de git en ``path`` (sin árbol de trabajo)."""

    def __init__(self, path: Path, env: Mapping[str, str]) -> None:
        self.path = path
        self.env = dict(env)
        self.commits: list[_CommitRec] = []
        self._blobs: dict[bytes, str] = {}
        self._scratch = path.parent / (path.name + "-blobs")
        self._n = 0

    def run(self, *args: str, input: bytes | None = None, env: Mapping[str, str] | None = None,
            cwd: Path | None = None) -> subprocess.CompletedProcess[bytes]:
        run_env = dict(self.env)
        run_env.update(env or {})
        proc = subprocess.run(
            ["git", *GIT_BUILD_OPTIONS, *args],
            cwd=cwd or self.path,
            env=run_env,
            input=input,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git {args[0]} falló: {proc.stderr.decode('utf-8', 'replace').strip()}")
        return proc

    def git(self, *args: str, input: bytes | None = None, env: Mapping[str, str] | None = None,
            cwd: Path | None = None) -> str:
        proc = self.run(*args, input=input, env=env, cwd=cwd)
        return proc.stdout.decode("utf-8", "replace").rstrip("\n")

    def _hash(self, contents: Sequence[bytes]) -> None:
        missing = [c for c in dict.fromkeys(contents) if c not in self._blobs]
        if not missing:
            return
        self._scratch.mkdir(parents=True, exist_ok=True)
        names: list[str] = []
        for data in missing:
            self._n += 1
            p = self._scratch / f"{self._n}.bin"
            p.write_bytes(data)
            names.append(str(p))
        out = self.git("hash-object", "-w", "--no-filters", "--stdin-paths", input="\n".join(names).encode() + b"\n")
        for data, sha in zip(missing, out.split("\n"), strict=True):
            self._blobs[data] = sha

    def commit(
        self,
        files: Mapping[str, tuple[str, bytes]],
        parents: Sequence[str],
        message: str,
        author: str,
        when: datetime,
    ) -> str:
        self._hash([data for _mode, data in files.values()])
        index = self.path / ".git" / f"scenario-index-{len(self.commits)}"
        idx_env = {"GIT_INDEX_FILE": str(index)}
        lines = b"".join(
            f"{mode} {self._blobs[data]}\t{path}".encode("utf-8") + b"\0"
            for path, (mode, data) in sorted(files.items())
        )
        if lines:
            proc = self.run("update-index", "--add", "-z", "--index-info", input=lines, env=idx_env)
            if b"Ignoring path" in proc.stderr:  # git descarta con un aviso las rutas que no admite
                raise ValueError(f"git no admite una ruta: {proc.stderr.decode('utf-8', 'replace').strip()}")
        tree = self.git("write-tree", env=idx_env) if lines else self.git("mktree", input=b"")
        when_s = when.isoformat()
        ident = {
            "GIT_AUTHOR_NAME": author,
            "GIT_AUTHOR_EMAIL": f"{author}@example.invalid",
            "GIT_AUTHOR_DATE": when_s,
            "GIT_COMMITTER_NAME": author,
            "GIT_COMMITTER_EMAIL": f"{author}@example.invalid",
            "GIT_COMMITTER_DATE": when_s,
        }
        args = ["commit-tree", tree]
        for parent in parents:
            args += ["-p", parent]
        sha = self.git(*args, "-m", message, env=ident)
        self.commits.append(_CommitRec(sha, dict(files), tuple(parents), message, author, when))
        return sha

    def cleanup(self) -> None:
        shutil.rmtree(self._scratch, ignore_errors=True)


def _read_tree_dir(root: Path) -> dict[str, tuple[str, bytes]]:
    if not root.is_dir():
        raise ValueError(f"no existe el árbol {root}")
    out: dict[str, tuple[str, bytes]] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = ("100644", p.read_bytes())
    return out


def _regenerated(files: Mapping[str, tuple[str, bytes]]) -> dict[str, bytes]:
    """``CODEOWNERS`` y ``task.yml`` regenerados desde ``roles.yaml`` y ``project.yaml`` de ``files``."""
    roles, _ = parse_roles(files["roles.yaml"][1], file="roles.yaml")
    cfg, _ = parse_project(files["project.yaml"][1], file="project.yaml")
    if roles is None or cfg is None:
        raise ValueError("roles.yaml o project.yaml no son válidos: no se pueden regenerar los generados")
    return generated_files(
        lead=roles.lead,
        deputy=roles.deputy,
        sections=[(s.id, s.title, s.owner) for s in roles.sections],
        source_sha12=roles.source_sha12,
        lang=cfg.language,
    )


def _base_files(case: Mapping[str, Any]) -> dict[str, tuple[str, bytes]]:
    base = case["base"]
    files = _read_tree_dir(trees_dir() / base["tree"])
    files["roles.yaml"] = ("100644", roles_fixture(base["roles"]).encode("utf-8"))
    for path, data in _regenerated(files).items():
        files[path] = ("100644", data)
    for path, text in base["extra_files"].items():
        files[path] = ("100644", text.encode("utf-8"))
    return files


def _apply_changes(files: Mapping[str, tuple[str, bytes]], changes: Sequence[Mapping[str, Any]]) -> dict[str, tuple[str, bytes]]:
    out = dict(files)
    regenerate: list[str] = []
    for ch in changes:
        status, path = ch["status"], ch["path"]
        content = ch["content"].encode("utf-8") if ch["content"] is not None else None
        if status == "removed":
            if path not in out:
                raise ValueError(f"no se puede borrar {path}: no existe")
            del out[path]
        elif status == "renamed":
            prev = ch["previous_path"]
            if prev not in out or path in out:
                raise ValueError(f"renombre inválido {prev} -> {path}")
            mode, data = out.pop(prev)
            out[path] = (ch["mode"] or mode, content if content is not None else data)
        elif status == "added":
            if path in out:
                raise ValueError(f"no se puede agregar {path}: ya existe")
            out[path] = (ch["mode"] or "100644", content if content is not None else b"")
        else:  # modified
            if path not in out:
                raise ValueError(f"no se puede modificar {path}: no existe")
            mode, data = out[path]
            if content is None and ch["mode"] is None and not ch["generated"]:
                raise ValueError(f"modified sin cambio en {path}")
            out[path] = (ch["mode"] or mode, content if content is not None else data)
        if ch["generated"]:
            regenerate.append(path)
    if regenerate:
        expected = _regenerated(out)
        for path in regenerate:
            out[path] = ("100644", expected[path])
    return out


def _portable(paths: Sequence[str]) -> bool:
    """``True`` si las rutas caben tal cual en cualquier disco: sin caracteres que Windows no
    admite, sin segmentos que Win32 no lleva a un archivo con ese nombre exacto (``aux.tex``,
    ``nul``, ``x.tex.``, ``GIT~1``: ``is_win32_alias``) y sin dos rutas o carpetas que solo
    difieran en mayúsculas."""
    if any(c in p for p in paths for c in _UNPORTABLE):
        return False
    if any(is_win32_alias(seg) for p in paths for seg in p.split("/") if seg):
        return False
    spelling: dict[str, str] = {}
    for p in paths:
        segs = p.split("/")
        for i in range(1, len(segs) + 1):
            prefix = "/".join(segs[:i])
            if spelling.setdefault(prefix.lower(), prefix) != prefix:
                return False
    return True


def _claim_marker(data: Mapping[str, object]) -> str:
    """Marcador ``claim`` de 04 §1.4 (sin ``--`` dentro del JSON, REQ-TASK-11)."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("--", "\\u002d\\u002d")
    return f"<!-- {_meta.NAME}:claim v1\n{payload}\n-->"


def build_scenario(case: Mapping[str, object], tmp: Path, git_env: Mapping[str, str]) -> Scenario:
    """Repositorio, checkouts y ``FakeForge`` del caso, bajo ``tmp`` (que no debe existir o estar vacía)."""
    c = normalize_case(case)
    tmp = Path(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    repo_dir = tmp / "pr"
    base_dir = tmp / "base"
    repo_dir.mkdir()
    b = _Builder(repo_dir, git_env)
    b.git("init", "-q", "-b", BASE_BRANCH)

    task, pr = c["task"], c["pr"]
    base_files = _base_files(c)
    roles, _ = parse_roles(base_files["roles.yaml"][1], file="roles.yaml")
    cfg, _ = parse_project(base_files["project.yaml"][1], file="project.yaml")
    assert roles is not None and cfg is not None
    lead = roles.lead
    head_branch = pr["head_branch"]

    # -- git --------------------------------------------------------------------
    base_tip = b.commit(base_files, [], "base", lead, _at(0))
    main_tip = base_tip
    parent = base_tip
    claim_sha: str | None = None
    number = task["number"] if task else None
    claimant = task["claimant"] if task else None
    on_task_branch = task is not None and head_branch == f"task/{number}"
    if on_task_branch and claimant:
        claim_sha = b.commit(base_files, [base_tip], f"{_meta.CLAIM_SUBJECT}{number}", claimant, _at(180))
        parent = claim_sha
    head_files = _apply_changes(base_files, c["changes"])
    if c["changes"]:
        parent = b.commit(head_files, [parent], "cambios del caso", pr["author"], _at(240))
    merge_base_sha = base_tip
    if c["base"]["advance"]:
        advanced = dict(base_files)
        for path, text in c["base"]["advance"].items():
            advanced[path] = ("100644", text.encode("utf-8"))
        main_tip = b.commit(advanced, [base_tip], "avance de la base", lead, _at(270))
        if pr["merge_base"]:
            merged = dict(head_files)
            for path, text in c["base"]["advance"].items():
                if path in {ch["path"] for ch in c["changes"]} | {ch["previous_path"] for ch in c["changes"]}:
                    raise ValueError(f"{c['id']}: base.advance choca con el cambio de {path}")
                merged[path] = ("100644", text.encode("utf-8"))
            parent = b.commit(merged, [parent, main_tip], f"Merge branch '{BASE_BRANCH}' into {head_branch}",
                              pr["author"], _at(285))
            head_files = merged
            merge_base_sha = main_tip
    head_sha = parent

    # Sin fork, la cabeza es una rama; con fork, una ref aparte que solo mantiene los objetos.
    head_ref = f"refs/heads/{head_branch}" if pr["same_repo"] else f"refs/fork/{pr['number']}/head"
    b.git("update-ref", "--stdin", input=f"update refs/heads/{BASE_BRANCH} {main_tip}\nupdate {head_ref} {head_sha}\n".encode())
    if pr["same_repo"]:
        b.git("symbolic-ref", "HEAD", head_ref)
    checked_out = False
    if pr["same_repo"]:
        if _portable(list(head_files)):
            b.git("reset", "-q", "--hard", head_sha)
            checked_out = True
        else:
            b.git("read-tree", head_sha)  # solo el índice: la cabeza no cabe en este disco
    b.git("clone", "-q", "--no-hardlinks", "-b", BASE_BRANCH, str(repo_dir), str(base_dir), cwd=tmp)
    b.cleanup()

    # -- forja falsa ------------------------------------------------------------
    forge = FakeForge(REPO, viewer=lead, profile=c["profile"], clock=FakeClock(NOW), default_branch=BASE_BRANCH)
    for rec in b.commits:
        files = {p: data for p, (_m, data) in rec.files.items() if "\\" not in p}  # el falso no admite '\'
        forge.seed_commit(files, rec.parents, rec.message, rec.author, sha=rec.sha, date=rec.when)
    forge.seed_branch(BASE_BRANCH, main_tip)
    if pr["same_repo"]:
        forge.seed_branch(head_branch, head_sha)

    if task is not None:
        _seed_task(forge, c, lead, cfg.language, claim_sha, base_tip, head_sha)

    pull = forge.seed_pull_request(
        files=[ChangedFile(ch["path"], ch["status"], ch["previous_path"]) for ch in c["changes"]],
        number=pr["number"],
        title=pr["title"],
        head_branch=head_branch,
        head_sha=head_sha,
        base_branch=BASE_BRANCH,
        author=pr["author"],
        draft=pr["draft"],
        created_at=_at(300),
    )
    for f in c["faults"]:
        forge.faults.add(Fault(method=f["method"], kind=f["kind"], nth=f["nth"]))

    return Scenario(
        case=c,
        base_dir=base_dir,
        pr_dir=repo_dir if pr["same_repo"] else None,
        forge=forge,
        head_sha=head_sha,
        base_sha=main_tip,
        pr=pull,
        merge_base_sha=merge_base_sha,
        claim_sha=claim_sha,
        repo_dir=repo_dir,
        checked_out=checked_out,
        git_env=dict(git_env),
    )


def _seed_task(
    forge: FakeForge,
    c: Mapping[str, Any],
    lead: str,
    language: str,
    claim_sha: str | None,
    base_sha: str,
    head_sha: str,
) -> None:
    """Issue de tarea, aceptación, marcador de reclamo y actividad de ``task/<n>``."""
    task = c["task"]
    number, claimant, state = task["number"], task["claimant"], task["state"]
    author = task["author"] or claimant or lead
    spec = {"summary": task["summary"], "sections": task["sections"], "plan_refs": task["plan_refs"],
            "done": ["El PDF de CI compila."]}
    body = task["body"] if task["body"] is not None else render_task_body(spec, language)
    try:
        spec_hash = str(parse_task_body(body)["spec_hash"])
    except TaskBodyError:
        spec_hash = ""
    labels = ["task"]
    events = [
        LabelEvent(1, "labeled", "task", author, _at(60)),
        LabelEvent(2, "labeled", "state:proposed", author, _at(60)),
    ]
    if state == "proposed":
        labels.append("state:proposed")
    else:
        events.append(LabelEvent(3, "unlabeled", "state:proposed", lead, _at(120)))  # aceptación (REQ-TASK-8)
    if state == "expired":
        labels.append("state:expired")
        events.append(LabelEvent(4, "labeled", "state:expired", lead, _at(360)))
    forge.seed_issue(
        number=number,
        title=f"Tarea {number}",
        body=body,
        state="closed" if state == "cancelled" else "open",
        state_reason="not_planned" if state == "cancelled" else None,
        labels=labels,
        assignees=[claimant] if claimant else [],
        author=author,
        created_at=_at(60),
        updated_at=_at(360) if state in ("cancelled", "expired") else _at(120),
        closed_at=_at(360) if state == "cancelled" else None,
        events=events,
    )
    if claimant is None or claim_sha is None:
        return
    marker = _claim_marker(
        {
            "task": number,
            "login": claimant,
            "claim_sha": claim_sha,
            "base_sha": base_sha,
            "sections": task["claim_sections"],
            "spec_hash": spec_hash,
            "claimed_at": _at(180).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "renewed_at": None,
        }
    )
    forge.seed_comment(number, author=claimant, body=marker, created_at=_at(181))
    pushes = c["pushes"] if c["pushes"] is not None else [{"actor": claimant, "type": "push"}]
    events_ref: list[RefActivity] = []
    if not pushes or pushes[0]["type"] != "branch_creation":
        events_ref.append(RefActivity("branch_creation", claimant, _at(180), NULL_SHA, claim_sha))
    for i, p in enumerate(pushes):
        if p["type"] == "branch_creation":  # la crea el actor indicado, con el commit de reclamo
            events_ref.append(RefActivity("branch_creation", p["actor"], _at(180), NULL_SHA, claim_sha))
        else:
            events_ref.append(RefActivity(p["type"], p["actor"], _at(241 + i), claim_sha, head_sha))
    forge.seed_ref_activity(f"task/{number}", events_ref)


def scenario_rule_input(sc: Scenario, t: Translator, *, prior: Mapping[str, str] | None = None) -> RuleInput:
    """``RuleInput`` del modo ``pr`` del escenario, como lo arma el motor."""
    case: Mapping[str, Any] = sc.case
    policy = read_policy_dir(sc.base_dir, t=t)
    tree = base_tree = None
    entries: Mapping[str, str] = {}
    if sc.pr_dir is not None:
        git = Git(sc.pr_dir, env=sc.git_env)
        tree = git_tree(sc.pr_dir, sc.head_sha, git=git)
        base_tree = git_tree(sc.pr_dir, sc.merge_base_sha or sc.base_sha, git=git)
        entries = tree.entries()
    changes = tuple(
        Change(
            path=ch["path"],
            status=ch["status"],
            previous_path=ch["previous_path"],
            file_mode=None if ch["status"] == "removed" else entries.get(ch["path"]),
        )
        for ch in case["changes"]
    )
    declared: dict[str, Any] = dict(case.get("prior") or PRIOR_DEFAULT)
    if prior is not None:
        declared.update(prior)
    pr_case: Mapping[str, Any] = case["pr"]
    return RuleInput(
        mode="pr",
        t=t,
        now=NOW,
        policy=policy,
        tree=tree,
        base_tree=base_tree,
        changes=changes,
        pr=sc.pr if sc.pr is not None else sc.forge.get_pull_request(int(pr_case["number"])),
        head_ref=str(pr_case["head_branch"]),
        head_sha=sc.head_sha,
        base_ref=BASE_BRANCH,
        base_sha=sc.base_sha,
        forge=sc.forge.as_user(CI_LOGIN),
        prior=declared,
    )


_API_STATUS = {"A": "added", "M": "modified", "T": "modified", "D": "removed", "R": "renamed"}


def diff_changes(sc: Scenario) -> list[tuple[str, str, str | None]]:
    """``(status, path, previous_path)`` del diff de tres puntos según git, con los nombres de la API."""
    assert sc.repo_dir is not None
    proc = subprocess.run(
        ["git", *GIT_BUILD_OPTIONS, "diff", "--name-status", "-M", "-z", "--no-ext-diff",
         sc.merge_base_sha or sc.base_sha, sc.head_sha],
        cwd=sc.repo_dir,
        env=dict(sc.git_env),
        capture_output=True,
        check=True,
    )
    parts = proc.stdout.decode("utf-8").split("\0")
    out: list[tuple[str, str, str | None]] = []
    i = 0
    while i < len(parts) and parts[i]:
        letter = parts[i][0]
        if letter in ("R", "C"):
            out.append((_API_STATUS.get(letter, letter), parts[i + 2], parts[i + 1]))
            i += 3
        else:
            out.append((_API_STATUS.get(letter, letter), parts[i + 1], None))
            i += 2
    return out
