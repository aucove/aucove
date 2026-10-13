# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Operación ``sync-roles`` (05-operaciones §9.4; 06 §4.6; 00-marco D-34 n y D-35).

Lee ``roles.yaml`` y ``language`` de ``project.yaml`` del **árbol de trabajo**, que es
la entrada que se está cambiando, y regenera ``.github/CODEOWNERS`` y
``.github/ISSUE_TEMPLATE/task.yml`` con ``render.generated.generated_files``, la misma
función que usan la regla ``roles-sync`` y el render de la plantilla.

- ``--check`` no escribe y lo puede usar cualquiera: si hay diferencias falla con
  ``E_VALIDATION_ROLES_OUT_OF_SYNC``, con ``data`` completa y un diff.
- Para escribir, el actor es el ``lead`` o el ``deputy`` de la política de la rama base
  (D-08), según la identidad en caché: sin ella, ``E_AUTH_NOT_AUTHENTICATED`` con su
  ``hint``; con otra persona, ``E_PERM_ROLE_REQUIRED``. La persona se resuelve con
  ``Roles.identify(login, id)`` (08-forja §6.3 y §6.4): si ``people`` declara
  ``forge_id``, decide el id; una cuenta que no es nadie de ``people`` da
  ``E_PERM_NOT_A_MEMBER``, y un id que coincide con otro login agrega
  ``W_AUTH_LOGIN_CHANGED``.
- Lee el árbol con la misma vista y la misma función que la regla ``roles-sync`` en modo
  ``tree`` (``vcs.tree.worktree`` y ``tree_reader``): las entradas de git distinguen
  mayúsculas, así que ``.github/codeowners`` no cuenta como ``.github/CODEOWNERS``.
- Antes de escribir, y también con ``--dry-run`` (REQ-OPS-30), comprueba todos los
  destinos: ningún enlace en la ruta (``E_PERM_SYMLINK``), carpetas que sean carpetas,
  destinos que sean archivos regulares que se puedan reemplazar y la misma grafía que en
  git (``E_STATE_DIRTY_WORKTREE`` con ``details.reason``). Así no queda una escritura a
  medias. Si una escritura falla igual, el error lleva ``data`` con lo que sí se escribió.
- En esta versión no consulta la forja: ``unknown_logins`` queda vacío y se agrega
  ``W_FORGE_CAPABILITY_UNKNOWN`` (D-34 n).
"""

from __future__ import annotations

import difflib
import json
import os
import stat
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from ..i18n import Translator
from ..vcs.links import link_probe
from ..vcs.tree import worktree
from .context import env_flag
from .registry import REGISTRY
from .result import OpError, Result, WarningObj, clip, fail, make_error, make_warning, ok
from .rules.roles_sync import FileState, Regeneration, compare_files, regenerate, tree_reader

if TYPE_CHECKING:
    from ..vcs.git import Git
    from ..vcs.tree import TreeView
    from .context import OpContext

OP = "sync-roles"
BUDGET: int = next(s.budget_chars or 4000 for s in REGISTRY if s.id == OP)
MAX_DETAIL_FINDINGS = 20  # hallazgos de config citados en details de E_CONFIG_INVALID
DIRTY = "E_STATE_DIRTY_WORKTREE"  # una entrada del árbol impide escribir un generado
_READONLY = getattr(stat, "FILE_ATTRIBUTE_READONLY", 1)  # NTFS: os.replace no lo reemplaza


def _warning(ctx: OpContext, code: str, details: dict[str, object] | None = None) -> WarningObj:
    return make_warning(code, ctx.t(), details=details)


def _authorize(ctx: OpContext, warnings: list[WarningObj]) -> None:
    """Identidad en caché y rol ``lead``/``deputy`` en la política de la base (REQ-OPS-3)."""
    ident = ctx.store().identity(token_fp=None)
    if ident is None:
        raise OpError("E_AUTH_NOT_AUTHENTICATED", details={"reason": "identity_cache_missing"})
    snap = ctx.base()
    if snap.fallback_sha is not None:  # REQ-PERM-4: con el sha usado de cada archivo
        warnings.append(_warning(ctx, "W_CONFIG_POLICY_FALLBACK", snap.fallback_details()))
    roles = snap.roles
    identity = roles.identify(ident.login, ident.id)  # 08-forja §6.3 y §6.4
    person = identity.person
    if person is not None and identity.login_changed:  # la operación continúa
        warnings.append(_warning(ctx, "W_AUTH_LOGIN_CHANGED", {"login": ident.login, "roles_login": person.login}))
    if person is None or not person.active:
        raise OpError("E_PERM_NOT_A_MEMBER", details={"login": ident.login}, warnings=warnings)
    who = person.login.lower()
    staff = {roles.lead.lower()} | ({roles.deputy.lower()} if roles.deputy else set())
    if who not in staff:
        raise OpError(
            "E_PERM_ROLE_REQUIRED",
            details={"login": person.login, "role": person.role, "required_role": ["lead", "deputy"]},
            warnings=warnings,
        )


def _invalid(regen: Regeneration, warnings: Sequence[WarningObj]) -> OpError:
    """``E_CONFIG_INVALID`` con la ruta y los hallazgos que lo explican.

    ``details`` es igual en todos los idiomas (REQ-OPS-6): de cada hallazgo van el código,
    la línea y el puntero, nunca su texto. ``details.code`` es el código del primer hallazgo
    ``error`` (00-marco §5.11).
    """
    details: dict[str, object] = {"path": regen.problem_path, "reason": regen.problem}
    errors = [f for f in regen.findings if f.severity == "error"][:MAX_DETAIL_FINDINGS]
    if errors:
        details["code"] = errors[0].code
        details["findings"] = [{"code": f.code, "line": f.line, "pointer": f.pointer} for f in errors]
    return OpError("E_CONFIG_INVALID", details=details, warnings=warnings)


def _lines(data: bytes | None) -> list[str]:
    if data is None:
        return []
    text = data.decode("utf-8", "replace").replace("\r\n", "\n")
    return [line if line.endswith("\n") else line + "\n" for line in text.splitlines(keepends=True)]


def unified_diff(states: Sequence[FileState]) -> str | None:
    """Diff unificado de lo actual a lo regenerado; ``None`` si no hay diferencias."""
    chunks: list[str] = []
    for s in states:
        if s.status == "same":
            continue
        before = f"a/{s.path}" if s.current is not None else "/dev/null"
        chunks.extend(difflib.unified_diff(_lines(s.current), _lines(s.expected), fromfile=before, tofile=f"b/{s.path}"))
    return "".join(chunks) or None


def _size(result: Result) -> int:
    """Tamaño de REQ-OPS-31: puntos de código de la serialización compacta."""
    return len(json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":")))


def fit_diff(build: Callable[[str | None, bool], Result], diff: str | None, budget: int = BUDGET) -> Result:
    """Recorta ``diff`` (marcador de REQ-OPS-34) hasta que el sobre quepa en ``budget``.

    ``build(diff, truncated)`` arma el resultado; con ``truncated`` agrega
    ``W_OUTPUT_TRUNCATED``.
    """
    result = build(diff, False)
    if diff is None or _size(result) <= budget:
        return result
    limit = max(0, len(diff) - (_size(result) - budget) - 64)
    while True:
        result = build(clip(diff, limit), True)
        excess = _size(result) - budget
        if excess <= 0 or limit == 0:
            return result
        limit = max(0, limit - excess - 16)


def _dirty(path: str, reason: str) -> OpError:
    return OpError(DIRTY, details={"path": path, "reason": reason})


def _same_entry(a: Path, b: Path) -> bool:
    """``True`` si ``a`` y ``b`` son la misma entrada del disco (otra grafía donde no hay mayúsculas)."""
    try:
        return os.path.samestat(os.lstat(a), os.lstat(b))
    except OSError:
        return False


def _blocked(root: Path, rel: str, entries: Mapping[str, str]) -> OpError | None:
    """Por qué no se puede escribir ``rel`` sin tocar otra entrada; ``None`` si se puede.

    - Un enlace o punto de reanálisis en la ruta: ``E_PERM_SYMLINK``.
    - Un componente que existe con otra grafía (``case_mismatch``), una carpeta que no lo es
      (``not_a_directory``), un destino que no es un archivo regular (``not_regular``) o que
      Windows no deja reemplazar (``read_only``): ``E_STATE_DIRTY_WORKTREE``.
    - Una entrada de git con otra grafía que en este disco es el mismo archivo: escribir no la
      renombra en el índice y la regla seguiría sin ver ``rel`` (``case_mismatch``).
    """
    if link_probe(root)(rel):
        return OpError("E_PERM_SYMLINK", details={"path": rel})
    segments = rel.split("/")
    current = root
    for i, seg in enumerate(segments):
        parent, current = current, current / seg
        shown = "/".join(segments[: i + 1])
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            break  # lo que falta se crea al escribir
        except OSError:
            return _dirty(shown, "not_regular")
        try:
            names = os.listdir(parent)
        except OSError:
            return _dirty("/".join(segments[:i]) or ".", "not_a_directory")
        if seg not in names:  # existe con otra grafía: el disco no distingue mayúsculas
            real = next((n for n in names if n.lower() == seg.lower()), seg)
            return _dirty("/".join([*segments[:i], real]), "case_mismatch")
        if i < len(segments) - 1:
            if not stat.S_ISDIR(st.st_mode):
                return _dirty(shown, "not_a_directory")
        elif not stat.S_ISREG(st.st_mode):
            return _dirty(rel, "not_regular")
        elif getattr(st, "st_file_attributes", 0) & _READONLY:
            return _dirty(rel, "read_only")
    target = root.joinpath(*segments)
    low = rel.lower()
    for other in sorted(entries):
        if other != rel and other.lower() == low and _same_entry(root.joinpath(*other.split("/")), target):
            return _dirty(other, "case_mismatch")
    return None


def check_targets(
    root: Path, paths: Sequence[str], entries: Mapping[str, str], warnings: Sequence[WarningObj] = ()
) -> None:
    """Comprueba todos los destinos antes de la primera escritura (y en ``--dry-run``); la usa ``init``."""
    for rel in paths:
        problem = _blocked(root, rel, entries)
        if problem is not None:
            raise OpError(problem.code, details=problem.details, warnings=warnings)


def _view(ctx: OpContext, root: Path) -> TreeView:
    """El árbol de trabajo como lo ve la regla ``roles-sync`` en modo ``tree``."""
    git: Git | None
    try:
        git = ctx.git()
    except OpError:
        git = None  # sin git, la vista recorre el disco
    return worktree(root, git=git)


def write_file(root: Path, rel: str, payload: bytes) -> None:
    """Escribe ``payload`` en ``root/rel`` de forma atómica, sin atravesar enlaces (también ``init``)."""
    if link_probe(root)(rel):
        raise OpError("E_PERM_SYMLINK", details={"path": rel})
    target = root.joinpath(*rel.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:12]}.tmp")
    try:
        with open(tmp, "xb") as fh:
            fh.write(payload)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()


def delete_file(root: Path, rel: str) -> None:
    """Borra ``root/rel`` sin atravesar enlaces y quita las carpetas que queden vacías (``init``)."""
    if link_probe(root)(rel):
        raise OpError("E_PERM_SYMLINK", details={"path": rel})
    target = root.joinpath(*rel.split("/"))
    try:
        target.unlink()
    except FileNotFoundError:
        return
    parent = target.parent
    while parent != root and parent.is_dir():
        try:
            parent.rmdir()  # solo si está vacía
        except OSError:
            break
        parent = parent.parent


def run(ctx: OpContext, *, check: bool = False) -> Result:
    """``sync-roles [--check]`` sobre el árbol de trabajo del proyecto."""
    if not isinstance(check, bool):
        raise OpError("E_VALIDATION_INPUT", details={"errors": [{"param": "check", "reason": "type"}]})
    t = ctx.t()
    project = ctx.project()
    assert project is not None
    root = project.root
    warnings: list[WarningObj] = []
    if not check:
        _authorize(ctx, warnings)
    tree = _view(ctx, root)
    read, exists = tree_reader(tree)
    regen = regenerate(read, t=t)
    if regen.expected is None:
        raise _invalid(regen, warnings)
    states = compare_files(read, regen.expected, exists=exists, normalize_eol=True)
    pending = [s for s in states if s.status != "same"]
    # D-34 (n): sin consultar la forja, los logins no se verifican (unknown_logins vacío)
    warnings.append(_warning(ctx, "W_FORGE_CAPABILITY_UNKNOWN", {"reason": "forge_not_consulted"}))
    if env_flag(ctx.inv.env, "OFFLINE") == "1":  # 05 §9.4, «sin red»
        warnings.append(_warning(ctx, "W_NET_OFFLINE"))
    diff = unified_diff(states)
    if not check:  # REQ-OPS-30: el plan comprueba los destinos igual que la ejecución real
        check_targets(root, [s.path for s in pending], tree.entries(), warnings)

    if not check and ctx.inv.dry_run:
        plan = [{"kind": "file_write", "target": s.path, "summary": t("roles.sync.plan.write", file=s.path)} for s in pending]
        return ok(OP, {"dry_run": True, "plan": plan}, warnings)

    written: set[str] = set()
    failure: OpError | None = None
    if not check:
        for s in pending:
            try:
                write_file(root, s.path, s.expected)
            except OpError as exc:  # un enlace que apareció después de comprobar
                failure = exc
                break
            except OSError:  # p. ej. disco lleno o archivo bloqueado: se informa lo escrito
                failure = OpError("E_INTERNAL_UNEXPECTED", details={"trace_id": uuid.uuid4().hex[:12]})
                break
            written.add(s.path)

    def action(s: FileState) -> str:
        if s.status == "same":
            return "unchanged"
        return "updated" if s.path in written else "would_update"

    files = [{"path": s.path, "action": action(s)} for s in states]
    changed = bool(pending)
    error = None
    if failure is not None:
        error = make_error(failure.code, t, details=failure.details)
    elif check and changed:
        error = make_error("E_VALIDATION_ROLES_OUT_OF_SYNC", t, details={"paths": [s.path for s in pending]})

    def build(text: str | None, truncated: bool) -> Result:
        data = {"check_only": check, "changed": changed, "files": files, "diff": text, "unknown_logins": []}
        extra = list(warnings)
        if truncated:
            extra.append(_warning(ctx, "W_OUTPUT_TRUNCATED", {"budget": BUDGET, "trimmed": ["diff"]}))
        if error is not None:
            return fail(OP, error, data=data, warnings=extra)
        return ok(OP, data, extra)

    return fit_diff(build, diff)


def render_text(result: Result, t: Translator, *, fmt: str = "text") -> str:
    """Vista legible de ``data_sync_roles``: una línea por archivo y el diff."""
    del fmt
    data = result.data or {}
    lines: list[str] = []
    for item in data.get("files", []):
        lines.append(t("roles.sync.render.file", action=t(f"roles.sync.render.{item.get('action')}"), file=item.get("path")))
    diff = data.get("diff")
    if isinstance(diff, str) and diff:
        lines.append(diff.rstrip("\n"))
    return "\n".join(lines)
