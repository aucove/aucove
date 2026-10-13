# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Operación ``check-path`` (05-operaciones §9.8; 06-permisos §2.1 y §2.4; D-34 n).

Evalúa cada ruta con ``domain.permissions.check_path`` en modo ``session``, sin forja
(REQ-OPS-44):

- La política es ``roles.yaml`` de la base local (``origin/<base>``, con el respaldo de
  REQ-PERM-4), que da ``OpContext.base()``.
- La identidad sale de la caché (``.aucove/cache/identity.json``). Sin identidad,
  cada ruta se deniega con ``E_PERM_NOT_A_MEMBER`` y un motivo localizado (D-34 n).
- La persona se resuelve con ``Roles.identify(login, id)`` (08-forja §6.3 y §6.4): si
  alguien de ``people`` declara ``forge_id``, decide el id. La política se evalúa con la
  grafía de ``people[].login``. Si la cuenta no es nadie de ``people`` (por ejemplo, el
  mismo login con otro id, REQ-FORGE-48), cada ruta se deniega con
  ``E_PERM_NOT_A_MEMBER`` sin evaluar el login de la caché. Si el id coincide y el login
  no, se agrega ``W_AUTH_LOGIN_CHANGED`` y la consulta sigue. ``basis.identity`` muestra
  siempre el login de la caché.
- ``W_CONFIG_POLICY_FALLBACK`` lleva el sha de cada archivo que usó el respaldo
  (``BaseSnapshot.fallback_details``; REQ-PERM-4).
- La tarea es la de ``--task`` o la de la rama actual ``task/<n>``; en la nube
  (``CLAUDE_CODE_REMOTE=true``), la ``<n>`` cuya caché tiene ``local_branch`` igual a la
  rama actual (06 §2.1). Sus datos salen de ``.aucove/cache/tasks/<n>.json``; si falta
  esa entrada, ``E_CONFIG_POLICY_UNAVAILABLE`` (con el ``hint`` de ``switch <n>``).
- Las rutas se normalizan como en 07-hooks §7 (:func:`prepare`): ``\\\\?\\UNC\\`` es una
  ruta UNC, una ruta relativa a una unidad (``D:foo``) se resuelve con el directorio
  actual solo si es su misma unidad, y una ruta relativa se toma desde el directorio
  actual si está dentro del proyecto y, si no, desde la raíz. Lo que no se puede resolver
  queda ``outside``.

Una ruta denegada no es un error: la consulta termina con ``ok=true``.
"""

from __future__ import annotations

import ntpath
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ..config.base import to_policy
from ..domain.paths import InvalidPath, OutsideRepo, apply_case_map, canonical, classify
from ..domain.permissions import Decision, Policy, Task, check_path
from ..i18n import Translator
from ..vcs.links import case_map, link_probe
from .result import OpError, Result, WarningObj, clip, make_warning, ok

if TYPE_CHECKING:
    from ..localstore.store import Store
    from ..vcs.git import Git
    from .context import OpContext

__all__ = ["OP", "case_insensitive", "current_task", "prepare", "render_text", "run"]

OP = "check-path"
MAX_PATHS = 100
MAX_REASON = 300  # maxLength de results[].reason
_TASK_BRANCH = re.compile(r"^task/([1-9][0-9]{0,8})$")
_TASK_FILE = re.compile(r"^([1-9][0-9]{0,8})\.json$")
_LOGIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?$")
_SECTION = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_DRIVE_ABS = re.compile(r"^[A-Za-z]:[\\/]")
_DRIVE_REL = re.compile(r"^[A-Za-z]:(?![\\/])")  # 'D:foo': relativa al directorio actual de D:
_LONG_PREFIX = "\\\\?\\"  # rutas largas de Windows (07-hooks §7)
_LONG_UNC = "UNC\\"  # '\\?\UNC\servidor\recurso' es '\\servidor\recurso'

# Código de check_path -> clave del motivo en roles.toml (roles.check_path.reason.<clave>).
_REASON_KEYS: Mapping[str, str] = {
    "E_PERM_OUTSIDE_REPO": "outside_repo",
    "E_PERM_SYMLINK": "symlink",
    "E_PERM_NOT_A_MEMBER": "not_a_member",
    "E_PERM_LOCAL_PATH": "local_path",
    "E_PERM_GENERATED_PATH": "generated_path",
    "E_STATE_NOT_ON_TASK_BRANCH": "not_on_task_branch",
    "E_CLAIM_NOT_HELD": "claim_not_held",
    "E_STATE_INVALID_TRANSITION": "invalid_transition",
    "E_PERM_OUTSIDE_TASK": "outside_task_other",
    "E_PERM_APPEND_ONLY": "append_only",
    "E_PERM_SECTION_NOT_OWNED": "section_not_owned",
    "E_PERM_LEAD_ONLY_PATH": "lead_only_path",
    "E_PERM_EDITOR_ONLY_PATH": "editor_only_path",
    "E_CONFIG_POLICY_UNAVAILABLE": "policy_unavailable",
    "E_INTERNAL_UNEXPECTED": "internal",
}
_NOTES_DIR = "memory/notes/"


def _input_error(param: str, reason: str) -> OpError:
    return OpError("E_VALIDATION_INPUT", details={"errors": [{"param": param, "reason": reason}]})


def _check_input(paths: object, task: object) -> tuple[list[str], int | None]:
    """Mismas restricciones que el ``inputSchema`` (05 §9.8), por si llega sin validar."""
    if not isinstance(paths, (list, tuple)):
        raise _input_error("paths", "type" if paths is not None else "required")
    if not paths:
        raise _input_error("paths", "minItems")
    if len(paths) > MAX_PATHS:
        raise _input_error("paths", "maxItems")
    for item in paths:
        if not isinstance(item, str):
            raise _input_error("paths", "type")
        if not item:
            raise _input_error("paths", "minLength")
    if task is not None and (isinstance(task, bool) or not isinstance(task, int)):
        raise _input_error("task", "type")
    if isinstance(task, int) and task < 1:
        raise _input_error("task", "minimum")
    return list(paths), task


def case_insensitive(root: Path) -> bool:
    """``True`` si el sistema de archivos de ``root`` no distingue mayúsculas (``ci_fs``)."""
    name = root.name
    swapped = name.swapcase()
    if swapped != name:
        try:
            return os.path.samefile(root, root.with_name(swapped))
        except OSError:
            return False
    return sys.platform in ("win32", "darwin")


def _cwd_prefix(root: Path, cwd: str) -> str:
    """Directorio actual relativo a la raíz (``''`` si es la raíz o está fuera)."""
    try:
        rel = os.path.relpath(os.path.abspath(cwd), os.path.abspath(root))
    except ValueError:  # otra unidad en Windows
        return ""
    if rel == os.curdir or os.path.isabs(rel) or rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return ""
    return rel.replace(os.sep, "/")


def _strip_long_prefix(raw: str) -> str:
    """Quita ``\\\\?\\`` solo donde la ruta sigue siendo la misma (07-hooks §7).

    ``\\\\?\\UNC\\servidor\\recurso`` es ``\\\\servidor\\recurso`` y ``\\\\?\\C:\\x`` es
    ``C:\\x``. Cualquier otra ruta con ese prefijo (volúmenes, ``GLOBALROOT``,
    dispositivos) se deja como está: empieza por ``\\`` y queda absoluta, fuera de la raíz.
    """
    if not raw.startswith(_LONG_PREFIX):
        return raw
    rest = raw[len(_LONG_PREFIX):]
    if rest[: len(_LONG_UNC)].upper() == _LONG_UNC:
        return "\\\\" + rest[len(_LONG_UNC):]
    return rest if _DRIVE_ABS.match(rest) else raw


def prepare(raw: str, root: Path, cwd: str) -> str | None:
    """Entrada para ``canonical``; ``None`` si el destino no se puede saber (fuera de la raíz).

    - El prefijo ``\\\\?\\`` se trata con :func:`_strip_long_prefix`.
    - Una ruta absoluta (``/``, ``\\`` o ``X:\\``) pasa tal cual: ``canonical`` la compara con
      la raíz.
    - Una ruta relativa a una unidad (``D:foo``) es relativa al directorio actual de esa
      unidad, como en ``os.path.join`` de 07-hooks §7: si es la unidad de ``cwd``, se une a
      ``cwd`` y se normaliza; si no (u ``cwd`` no tiene unidad), el destino depende de un
      estado del proceso que no se conoce y la ruta queda fuera.
    - Cualquier otra ruta relativa se toma desde ``cwd`` si está dentro del proyecto y, si no,
      desde la raíz.
    """
    p = _strip_long_prefix(raw)
    if p.startswith(("/", "\\")) or _DRIVE_ABS.match(p):
        return p
    m = _DRIVE_REL.match(p)
    if m is not None:
        here = ntpath.abspath(cwd)
        if ntpath.splitdrive(here)[0].upper() != m.group(0).upper():
            return None
        return ntpath.normpath(ntpath.join(here, p))
    prefix = _cwd_prefix(root, cwd)
    return f"{prefix}/{p}" if prefix else p


def _classify(prepared: str | None, pol: Policy) -> tuple[str | None, str, str | None]:
    """``(ruta canónica, clase, sección)`` como las calcula ``check_path``; ``outside`` si no hay ruta."""
    if prepared is None:
        return None, "outside", None
    try:
        rel = canonical(prepared, pol.root, pol.ci_fs)
    except (InvalidPath, OutsideRepo):
        return None, "outside", None
    if pol.ci_fs:
        rel = apply_case_map(rel, pol.case_map)
    # El paso 1 ('.git' en cualquier segmento y sus alias) lo resuelve classify con R0, la
    # misma función que usa check_path: no hay una copia propia que se desincronice.
    info = classify(rel, frozenset(pol.sections), symlinked=False)
    return rel, info.cls, info.section


def _op_for(rel: str | None, root: Path) -> Literal["create", "modify"]:
    """``create`` si la ruta no existe en el árbol de trabajo; si no, ``modify`` (06 §2.4)."""
    if rel is None:
        return "modify"
    return "modify" if os.path.lexists(root.joinpath(*rel.split("/"))) else "create"


def _reason(t: Translator, d: Decision, cls: str, section: str | None, task: Task | None) -> str | None:
    """Motivo localizado de una denegación o de una confirmación (máx. 300 caracteres)."""
    if d.allow:
        return clip(t("roles.check_path.reason.confirm"), MAX_REASON) if d.confirm else None
    code = d.code or "E_INTERNAL_UNEXPECTED"
    key = _REASON_KEYS.get(code, "internal")
    if code == "E_PERM_OUTSIDE_TASK":
        if cls == "task" or (d.path or "").startswith(_NOTES_DIR):
            key = "outside_task_notes"
        elif section is not None:
            key = "outside_task_section"
    params: dict[str, object] = {
        "task": task.number if task is not None else "",
        "state": task.state if task is not None else "",
        "section": section or "",
        "owner": d.owner or "",
    }
    return clip(t(f"roles.check_path.reason.{key}", **params), MAX_REASON)


def _entry(
    raw: str,
    prepared: str | None,
    d: Decision,
    pol: Policy,
    t: Translator,
    task: Task | None,
) -> dict[str, object]:
    """Un elemento de ``data_check_path.results``."""
    rel, cls, section = _classify(prepared, pol)
    path_class = d.path_class or cls
    path = d.path if d.path and path_class != "outside" else raw
    if path_class == "outside":
        section = None
    else:
        section = d.section or section
    if section is not None and (section not in pol.sections or not _SECTION.match(section)):
        section = None
    owner = d.owner
    if owner is None and section is not None:
        owner = pol.shown(pol.sections[section])
    if owner is not None and not _LOGIN.match(owner):
        owner = None
    return {
        "path": path,
        "class": path_class,
        "allowed": d.allow,
        "reason_code": None if d.allow else (d.code or "E_INTERNAL_UNEXPECTED"),
        "section": section,
        "owner": owner,
        "confirm": bool(d.allow and d.confirm),
        "reason": _reason(t, d, path_class, section, task),
    }


def _task_by_local_branch(store: Store, branch: str) -> int | None:
    """En la nube: la tarea cuya caché tiene ``local_branch`` igual a la rama (06 §2.1)."""
    try:
        folder = store.path("tasks")
        names = [e.name for e in os.scandir(folder) if e.is_file(follow_symlinks=False)]
    except (OSError, ValueError):
        return None
    numbers = sorted(int(m.group(1)) for m in (_TASK_FILE.match(n) for n in names) if m)
    for n in numbers:
        cached = store.task(n)
        if cached is not None and cached.local_branch == branch:
            return n
    return None


def current_task(ctx: OpContext, git: Git, store: Store) -> int | None:
    """Tarea de la rama actual (``task/<n>``) o, en la nube, de la caché (06 §2.1)."""
    branch = git.current_branch()
    if branch is None:
        return None
    if ctx.inv.env.get("CLAUDE_CODE_REMOTE") == "true":
        return _task_by_local_branch(store, branch)
    m = _TASK_BRANCH.match(branch)
    return int(m.group(1)) if m else None


def _not_a_member(
    raw: str, prepared: str | None, pol: Policy, t: Translator, task: Task | None
) -> dict[str, object]:
    """La cuenta no es nadie de ``people``: la ruta se deniega con ``E_PERM_NOT_A_MEMBER``.

    No se llama a ``check_path`` con el login de la caché: ese login puede ser el de una
    persona de ``people`` cuyo ``forge_id`` es otro (08-forja REQ-FORGE-48).
    """
    rel, cls, section = _classify(prepared, pol)
    denial = Decision(False, "E_PERM_NOT_A_MEMBER", rel, cls, section)
    return _entry(raw, prepared, denial, pol, t, task)


def _no_identity(raw: str, prepared: str | None, pol: Policy, t: Translator) -> dict[str, object]:
    """D-34 (n): sin identidad en caché, toda ruta se deniega con ``E_PERM_NOT_A_MEMBER``."""
    item = _not_a_member(raw, prepared, pol, t, None)
    item["reason"] = clip(t("roles.check_path.reason.no_identity"), MAX_REASON)
    return item


def run(ctx: OpContext, *, paths: object = None, task: object = None) -> Result:
    """``check-path <ruta>... [--task <n>]``: decisión de ``check_path`` por ruta, sin forja."""
    raw_paths, task_arg = _check_input(paths, task)
    t = ctx.t()
    project = ctx.project()
    assert project is not None
    root = project.root
    snap = ctx.base()
    warnings: list[WarningObj] = []
    if snap.fallback_sha is not None:  # REQ-PERM-4: con el sha usado de cada archivo
        warnings.append(make_warning("W_CONFIG_POLICY_FALLBACK", t, details=snap.fallback_details()))
    git = ctx.git()
    ci_fs = case_insensitive(root)
    pol = to_policy(
        snap,
        root=str(root),
        ci_fs=ci_fs,
        case_map=case_map(git) if ci_fs else None,
        link_probe=link_probe(root),
    )
    store = ctx.store()
    ident = store.identity(token_fp=None)
    login = ident.login if ident is not None and _LOGIN.match(ident.login) else None
    member: str | None = None  # grafía de people[].login con que se evalúa la política
    if ident is not None and login is not None:
        identity = snap.roles.identify(login, ident.id)  # 08-forja §6.3 y §6.4
        member = identity.login
        if identity.login_changed:
            warnings.append(
                make_warning("W_AUTH_LOGIN_CHANGED", t, details={"login": login, "roles_login": member})
            )
    number = task_arg if task_arg is not None else current_task(ctx, git, store)

    task_obj: Task | None = None
    source = "local"
    if login is not None and number is not None:
        cached = store.task(number)
        if cached is None:
            raise OpError(
                "E_CONFIG_POLICY_UNAVAILABLE",
                details={"reason": "task_cache_missing", "task": number},
                warnings=warnings,
            )
        task_obj = Task(number, cached.claimant, cached.state, frozenset(cached.sections))
        source = "cache"

    results: list[dict[str, object]] = []
    for raw in raw_paths:
        prepared = prepare(raw, root, ctx.inv.cwd)
        if login is None:
            results.append(_no_identity(raw, prepared, pol, t))
            continue
        if member is None:  # la cuenta no es nadie de people (por login ni por forge_id)
            results.append(_not_a_member(raw, prepared, pol, t, task_obj))
            continue
        if prepared is None:  # destino desconocido: como una ruta fuera de la raíz (REQ-CONV-7)
            decision = Decision(False, "E_PERM_OUTSIDE_REPO", raw, "outside")
        else:
            rel, _cls, _sec = _classify(prepared, pol)
            decision = check_path(member, prepared, _op_for(rel, root), task_obj, pol=pol, mode="session")
        results.append(_entry(raw, prepared, decision, pol, t, task_obj))

    data = {
        "basis": {
            "base_ref": snap.ref,
            "base_sha": snap.sha if snap.sha is not None and _SHA.match(snap.sha) else None,
            "identity": login,
            "task": number,
            "source": source,
        },
        "allowed_all": all(bool(r["allowed"]) for r in results),
        "results": results,
    }
    return ok(OP, data, warnings)


def render_text(result: Result, t: Translator, *, fmt: str = "text") -> str:
    """Vista legible de ``data_check_path``: una línea por ruta y su motivo."""
    del fmt
    data = result.data or {}
    lines: list[str] = []
    basis = data.get("basis") or {}
    identity = basis.get("identity")
    lines.append(
        t(
            "roles.check_path.render.basis",
            ref=basis.get("base_ref", ""),
            identity=f"@{identity}" if identity else t("roles.check_path.render.unknown"),
            task=f"#{basis['task']}" if basis.get("task") else t("roles.check_path.render.none"),
        )
    )
    for item in data.get("results", []):
        if not item.get("allowed"):
            verdict = t("roles.check_path.render.denied")
        elif item.get("confirm"):
            verdict = t("roles.check_path.render.confirm")
        else:
            verdict = t("roles.check_path.render.allowed")
        line = t("roles.check_path.render.line", verdict=verdict, file=item.get("path"), cls=item.get("class"))
        reason = item.get("reason")
        if reason:
            code = item.get("reason_code")
            line += "\n  " + (f"[{code}] {reason}" if code else str(reason))
        lines.append(line)
    return "\n".join(lines)
