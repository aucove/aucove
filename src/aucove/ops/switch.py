# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove switch <n>`` · ``switch_task`` (05 §9.16; 04-tareas §5).

Precondiciones: en la puerta web, ``E_STATE_UNSUPPORTED_DOOR`` (una sesión por tarea,
REQ-TASK-77); con una fusión en curso, ``E_STATE_MERGE_IN_PROGRESS``.

Pasos, en el orden de 05 §9.16:

1. ``preflight``: ``<n>`` está en ``ACTIVE`` con dueño = actor según ``derive_state``; si no,
   ``E_CLAIM_NOT_HELD``. Sin red decide la caché de la tarea.
2. ``save_local``: si la rama actual es una tarea del actor y tiene cambios, ``save`` (con su
   push: REQ-TASK-45 pide dejar el trabajo en el remoto); en otra rama con cambios
   rastreados, ``E_STATE_DIRTY_WORKTREE``.
3. ``fetch``: trae ``task/<n>``. Si no está en el remoto, ``E_CLAIM_NOT_HELD``
   (``ref_missing``). Sin red sigue si la rama local existe (``W_NET_OFFLINE``) y, si no,
   ``E_NET_OFFLINE``.
4. ``checkout`` (canónico): ``git switch task/<n>``, creando la rama local que sigue a
   ``origin/task/<n>`` si falta.
5. ``sync``: avanza, empuja o fusiona según 04 §5 paso 4.

Al final escribe la caché de la tarea. ``.claude/settings.local.json`` (07-hooks) lo regenera
la ruta de hooks de H4, como en ``claim``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import _meta
from ..domain.tasks import ACTIVE, derive_state
from ..domain.text import same_login
from ..forge.base import ForgeError
from ..i18n import LANGS, translator
from ..vcs.git import is_offline
from .board import owner_and_sections, write_task_cache
from .check_path import current_task
from .gitwork import check_claim, counts, fetch_task, guard_remote
from .registry import forge_error_code
from .result import OpError, Result, WarningObj, make_warning, ok
from .save import Work, changes, integrate, push, resolve_work, save_and_push, scope_check, whoami
from .snapshot import CI_ACTORS, authz_from_roles, read_task, task_branch
from .steps import Steps
from .workspace import dirty, merge_in_progress

if TYPE_CHECKING:
    from ..config.base import BaseSnapshot
    from ..i18n import Translator
    from ..vcs.git import Git
    from .context import OpContext

__all__ = ["OP", "STEPS", "run"]

OP = "switch"
STEPS = ("preflight", "save_local", "fetch", "checkout", "sync")
_OFFLINE_CODES = frozenset({"E_NET_OFFLINE", "E_NET_TIMEOUT"})


def _bad(param: str, reason: str = "type") -> OpError:
    return OpError("E_VALIDATION_INPUT", details={"errors": [{"param": param, "reason": reason}]})


def _preflight(ctx: OpContext, snap: BaseSnapshot, n: int, me: str) -> tuple[str, tuple[str, ...]] | None:
    """``<n>`` en ``ACTIVE`` con dueño ``me``: ``(estado, partes)`` para la caché o, sin red, ``None``."""
    if is_offline(ctx.inv.env):
        return _offline_check(ctx, n, me)
    try:
        forge = ctx.forge()
        ev = read_task(forge, n, authz_from_roles(snap.roles, ci_actors=CI_ACTORS),
                       base_branch=snap.project.base_branch)
    except ForgeError as exc:
        if forge_error_code(exc)[0] in _OFFLINE_CODES:
            return _offline_check(ctx, n, me)
        raise
    if ev is None:
        raise OpError("E_CLAIM_NOT_HELD", details={"task": n, "reason": "not_a_task"})
    derived = derive_state(ev.snapshot)
    if derived.state not in ACTIVE or not same_login(derived.owner, me):
        raise OpError("E_CLAIM_NOT_HELD", details={"task": n, "state": derived.state, "owner": derived.owner})
    _owner, sections = owner_and_sections(ev.snapshot, derived)
    return derived.state, tuple(sections)


def _owns_current(ctx: OpContext, git: Git, me: str) -> bool:
    """La rama actual es una tarea del actor según su caché (05 §9.16 paso 2)."""
    store = ctx.store()
    current = current_task(ctx, git, store)
    cached = store.task(current) if current is not None else None
    return cached is not None and cached.claimant is not None and same_login(cached.claimant, me)


def _offline_check(ctx: OpContext, n: int, me: str) -> None:
    cached = ctx.store().task(n)
    if cached is not None and (cached.claimant is None or not same_login(cached.claimant, me)):
        raise OpError("E_CLAIM_NOT_HELD", details={"task": n, "reason": "not_owner"})
    return None


def _checkout(git: Git, branch: str) -> bool:
    """Paso ``checkout``; ``True`` si creó la rama local."""
    if git.current_branch() == branch:
        return False
    exists = git.rev_parse(f"refs/heads/{branch}") is not None
    if exists:
        proc = git.run("switch", "--quiet", branch, check=False, read=False, extra_env={"LC_ALL": "C"})
    else:
        proc = git.run("switch", "--quiet", "--track", "-c", branch, f"origin/{branch}", check=False, read=False,
                       extra_env={"LC_ALL": "C"})
    if proc.returncode != 0:
        low = proc.stderr.decode("utf-8", "replace").lower()
        if "would be overwritten" in low or "untracked working tree" in low or "local changes" in low:
            raise OpError("E_STATE_DIRTY_WORKTREE", details={"reason": "would_be_overwritten"})
        raise OpError("E_CONFIG_GIT_UNAVAILABLE", details={"reason": "switch_failed"})
    return not exists


def _sync(ctx: OpContext, git: Git, snap: BaseSnapshot, work: Work, t: Translator,
          warnings: list[WarningObj]) -> None:
    """04 §5 paso 4: detrás avanza, delante empuja y, si divergen, fusiona y empuja."""
    check_claim(git, work.number)
    ahead, behind = counts(git, f"refs/remotes/origin/{work.branch}")
    if behind:
        integrate(ctx, git, snap, work, t, warnings)
        ahead, _behind = counts(git, f"refs/remotes/origin/{work.branch}")
    if ahead:
        push(ctx, git, snap, work, t, warnings)


def run(ctx: OpContext, *, task: object = None) -> Result:
    """``switch <n>``."""
    if isinstance(task, bool) or not isinstance(task, int) or task < 1:
        raise _bad("task")
    n = task
    if ctx.inv.env.get("CLAUDE_CODE_REMOTE") == "true":  # una sesión por tarea (REQ-TASK-77)
        raise OpError("E_STATE_UNSUPPORTED_DOOR", details={"door": "web", "op": OP, "task": n})
    t = ctx.t()
    project = ctx.project()
    assert project is not None
    snap = ctx.base()
    git = ctx.git()
    store = ctx.store()
    warnings: list[WarningObj] = []
    if snap.fallback_sha is not None:
        warnings.append(make_warning("W_CONFIG_POLICY_FALLBACK", t, details=snap.fallback_details()))
    if merge_in_progress(ctx):
        raise OpError("E_STATE_MERGE_IN_PROGRESS")
    branch = task_branch(n)
    previous = git.current_branch()
    me = whoami(ctx, snap)
    steps = Steps(ctx, OP, STEPS)
    info = steps.run("preflight", lambda: _preflight(ctx, snap, n, me))
    offline = info is None

    lang = translator(snap.project.language) if snap.project.language in LANGS else t
    pending = changes(git, [])
    if pending and _owns_current(ctx, git, me):
        if steps.dry_run:
            steps.run("save_local", lambda: scope_check(ctx, git, snap, resolve_work(ctx, git, snap, me=me), []))
            steps.effect("git_commit", previous or "", lang("tasks.effect.save_commit", n=len(pending)))
        else:
            steps.run("save_local", lambda: save_and_push(ctx, git, snap, resolve_work(ctx, git, snap, me=me), t,
                                                          warnings, key="switch"))
    elif dirty(git):
        raise steps.failure("save_local", "E_STATE_DIRTY_WORKTREE", details={"branch": previous})
    else:
        steps.skip("save_local", "clean")

    def fetch() -> str:
        guard_remote(ctx, git)
        status = "offline" if offline or steps.dry_run else fetch_task(git, n)
        if status == "missing":
            raise OpError("E_CLAIM_NOT_HELD", details={"task": n, "reason": "ref_missing"})
        if status == "offline" and not steps.dry_run and git.rev_parse(f"refs/heads/{branch}") is None:
            raise OpError("E_NET_OFFLINE", details={"task": n, "reason": "local_branch_missing"})
        return status

    status = steps.run("fetch", fetch)
    if status == "offline" and not steps.dry_run:
        warnings.append(make_warning("W_NET_OFFLINE", t))
    if steps.dry_run:
        if previous != branch:
            steps.effect("git_checkout", branch, lang("tasks.effect.checkout", task=n))
        steps.effect("local_write", f"{_meta.CACHE_DIR}/cache/tasks/{n}.json", lang("tasks.effect.cache"))
        return steps.dry_result(warnings)

    created = steps.run("checkout", lambda: _checkout(git, branch))
    if info is not None:
        state, sections = info
        write_task_cache(store, n, snap.project.forge_repo, claimant=me, state=state, sections=sections,
                         now=ctx.inv.now())
    if status == "ok":
        work = resolve_work(ctx, git, snap, me=me)
        steps.run("sync", lambda: _sync(ctx, git, snap, work, t, warnings))
    else:
        steps.skip("sync", "offline")
    warnings.extend(steps.warnings(t))
    data: dict[str, object] = {"task": n, "branch": branch, "previous_branch": previous, "created_local": created}
    return ok(OP, data, warnings)
