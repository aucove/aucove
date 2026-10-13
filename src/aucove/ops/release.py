# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove release <n>`` · ``release_task`` (05 §9.13; 04-tareas §4.1 y §4.3).

Precondiciones: en la puerta web, ``E_STATE_UNSUPPORTED_DOOR`` (borrar una rama da 403,
REQ-TASK-78). ``--handoff`` solo existe en la CLI y solo lo usa el ``lead``
(``E_PERM_ROLE_REQUIRED``); la persona indicada debe estar en ``people``.

Pasos, en el orden de 05 §9.13:

1. ``preflight``: ``derive_state`` con lecturas acotadas (``snapshot.read_task``). La tarea
   está en ``ACTIVE`` y el actor es el dueño o el ``lead``; si no, ``E_CLAIM_NOT_HELD``. Si
   ya está ``open``, termina con ``changed:false`` y completa los reflejos que una
   liberación anterior dejó a medias (asignados, ``state:expired`` y el comentario).
2. ``save_local``: si el actor es el dueño, está en ``task/<n>`` y hay cambios, ``save`` con
   su push.
3. ``close_pr``: cierra el PR abierto de ``task/<n>``.
4. ``archive`` (canónico): si ``compare(<base>...task/<n>)`` trae un commit que no es de
   reclamo, ``refs/tags/archive/task-<n>-<k>`` en la cabeza, con ``k`` = 1 + las etiquetas
   que ya hay (con ``RefAlreadyExists``, ``k+1``). Una etiqueta que ya apunta a la cabeza se
   reutiliza (REQ-OPS-17). Si falla, ``delete_ref`` no se ejecuta (REQ-TASK-31).
5. ``delete_ref`` (canónico): borra ``refs/heads/task/<n>``.
6. ``unassign_and_labels`` (reflejo): vacía los asignados y quita ``state:expired``.
7. ``release_comment`` (reflejo): comentario con el marcador ``release`` (04 §4.1 paso 7).
8. ``local_cleanup``: si el actor es el dueño y está en ``task/<n>``, cambia a la base y borra
   la rama local (el trabajo quedó en el archivo). La caché de la tarea queda ``open``.
"""

from __future__ import annotations

import re
from datetime import timezone
from typing import TYPE_CHECKING

from ..domain.markers import replace_marker
from ..domain.tasks import ACTIVE, derive_state
from ..domain.text import same_login
from ..forge.base import RefAlreadyExists
from ..i18n import LANGS, translator
from .board import owner_and_sections, write_task_cache
from .result import OpError, Result, WarningObj, make_warning, ok
from .save import Work, changes, is_staff, save_and_push
from .snapshot import CI_ACTORS, TaskEvidence, authz_from_roles, claim_subject, read_task, task_branch
from .steps import Steps
from .workspace import dirty, merge_in_progress

if TYPE_CHECKING:
    from ..config.base import BaseSnapshot
    from ..domain.tasks import Pull
    from ..forge.base import ForgeAdapter
    from ..i18n import Translator
    from ..vcs.git import Git
    from .context import OpContext

__all__ = ["OP", "STEPS", "archive_work", "has_work", "open_pr", "run"]

OP = "release"
STEPS = (
    "preflight", "save_local", "close_pr", "archive", "delete_ref", "unassign_and_labels", "release_comment",
    "local_cleanup",
)
REASON_MAX = 500
EXPIRED = "state:expired"
ARCHIVE_TRIES = 10
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")


def _bad(param: str, reason: str = "type") -> OpError:
    return OpError("E_VALIDATION_INPUT", details={"errors": [{"param": param, "reason": reason}]})


def _check_input(task: object, reason: object, handoff: object) -> tuple[int, str | None, str | None]:
    if isinstance(task, bool) or not isinstance(task, int) or task < 1:
        raise _bad("task")
    if reason is not None:
        if not isinstance(reason, str):
            raise _bad("reason")
        if len(reason) > REASON_MAX:
            raise _bad("reason", "maxLength")
    if handoff is not None and (not isinstance(handoff, str) or not _LOGIN.fullmatch(handoff)):
        raise _bad("handoff", "pattern")
    text = reason.strip() if isinstance(reason, str) and reason.strip() else None
    return task, text, handoff  # type: ignore[return-value]


def _iso(ctx: OpContext) -> str:
    return ctx.inv.now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_pr(ev: TaskEvidence) -> Pull | None:
    """El PR abierto de la tarea, de la cabeza ``task/<n>`` de este repositorio."""
    found = [p for p in ev.snapshot.pulls if p.state == "open" and p.same_repo]
    return min(found, key=lambda p: p.created_at) if found else None


def has_work(ev: TaskEvidence, number: int) -> bool:
    """``compare(<base>...task/<n>)`` trae algún commit que no es de reclamo (04 §4.1 paso 4)."""
    subject = claim_subject(number)
    commits = ev.branch_commits
    if commits is None:  # sin comparación no se puede descartar trabajo: se archiva
        return True
    return any(c.message.split("\n", 1)[0].strip() != subject for c in commits)


def archive_work(forge: ForgeAdapter, number: int, head: str) -> str:
    """Etiqueta ``archive/task-<n>-<k>`` en ``head``; la que ya apunte ahí se reutiliza."""
    prefix = f"archive/task-{number}-"
    existing = forge.list_refs(prefix, namespace="tags")
    for ref in existing:
        if ref.sha == head:
            return f"refs/tags/{ref.branch}"
    k = len(existing) + 1
    for _ in range(ARCHIVE_TRIES):
        name = f"{prefix}{k}"
        try:
            forge.create_ref(name, head, namespace="tags")
            return f"refs/tags/{name}"
        except RefAlreadyExists as exc:
            if exc.existing.sha == head:
                return f"refs/tags/{name}"
            k += 1
    raise OpError("E_FORGE_CONFLICT", details={"ref": f"refs/tags/{prefix}{k}", "reason": "archive_taken"})


def _unassign(forge: ForgeAdapter, ev: TaskEvidence) -> bool:
    """Vacía los asignados y quita ``state:expired``; ``True`` si había asignados."""
    had = bool(ev.issue.assignees)
    if had:
        forge.set_assignees(ev.issue.number, [])
    if any(x.lower() == EXPIRED for x in ev.issue.labels):
        forge.remove_label(ev.issue.number, EXPIRED)
    return had


def _comment(forge: ForgeAdapter, lang: Translator, n: int, me: str, data: dict[str, object], key: str) -> None:
    visible = lang("tasks.release.comment", task=n, login=me)
    reason = data.get("reason")
    if isinstance(reason, str) and reason:
        visible += "\n\n" + lang("tasks.release.reason", reason=reason)
    if data.get("handoff_to"):
        visible += "\n\n" + lang("tasks.release.handoff", login=data["handoff_to"])
    forge.create_comment(n, replace_marker(visible, "release", data), op_id=f"release-{n}-{key}")


def _local_cleanup(ctx: OpContext, git: Git, snap: BaseSnapshot, branch: str, head: str | None) -> bool:
    """Paso ``local_cleanup``; ``True`` si borró la rama local ``task/<n>``."""
    git.run("update-ref", "-d", f"refs/remotes/origin/{branch}", check=False, read=False)
    local_tip = git.rev_parse(f"refs/heads/{branch}")
    if local_tip is None:
        return False
    if git.current_branch() == branch:
        if dirty(git):
            return False  # nada que perder sin guardar: la rama se queda
        base = snap.project.base_branch
        if git.rev_parse(f"refs/heads/{base}") is not None:
            proc = git.run("switch", "--quiet", base, check=False, read=False)
        else:
            proc = git.run("switch", "--quiet", "--track", "-c", base, f"origin/{base}", check=False, read=False)
        if proc.returncode != 0:
            return False
    elif head is None or not git.is_ancestor(local_tip, head):
        return False  # tiene commits que no están en el archivo: se conserva
    return git.run("branch", "-D", "--quiet", branch, check=False, read=False).returncode == 0


def _latest(ev: TaskEvidence, kind: str):  # type: ignore[no-untyped-def]
    found = [m for m in ev.snapshot.issue_markers if m.kind == kind]
    return max(found, key=lambda m: m.created_at) if found else None


def _already_open(ctx: OpContext, forge: ForgeAdapter, ev: TaskEvidence, steps: Steps, me: str,
                  data_in: dict[str, object], lang: Translator, warnings: list[WarningObj]) -> Result:
    """La tarea ya está ``open``: sin cambios, salvo los reflejos de una liberación a medias."""
    n = ev.issue.number
    for step in ("save_local", "close_pr", "archive", "delete_ref"):
        steps.skip(step, "already_open")
    unassigned = False
    if ev.issue.assignees or any(x.lower() == EXPIRED for x in ev.issue.labels):
        unassigned = bool(steps.reflect("unassign_and_labels", lambda: _unassign(forge, ev)))
    else:
        steps.skip("unassign_and_labels")
    claim, release = _latest(ev, "claim"), _latest(ev, "release")
    if claim is not None and (release is None or release.created_at < claim.created_at):
        prefix = f"archive/task-{n}-"

        def repair() -> None:
            tags = forge.list_refs(prefix, namespace="tags")
            last = max(tags, key=lambda r: int(r.branch[len(prefix):]) if r.branch[len(prefix):].isdigit() else 0,
                       default=None)
            data = {**data_in, "previous_owner": claim.data.get("login"),
                    "archive_ref": f"refs/tags/{last.branch}" if last is not None else None}
            _comment(forge, lang, n, me, data, str(claim.data.get("claim_sha") or "")[:16] or "open")

        steps.reflect("release_comment", repair)
    else:
        steps.skip("release_comment")
    steps.skip("local_cleanup", "already_open")
    warnings.extend(steps.warnings(ctx.t()))
    data: dict[str, object] = {
        "task": {"number": n, "url": ev.issue.url, "state": "open"},
        "changed": False,
        "unassigned": unassigned,
        "ref_deleted": False,
        "local_branch_deleted": False,
        "archive_ref": None,
        "pr_closed": None,
        "handoff_to": data_in.get("handoff_to"),
    }
    return ok(OP, data, warnings)


def run(ctx: OpContext, *, task: object = None, reason: object = None, handoff: object = None) -> Result:
    """``release <n> [--reason <texto>] [--handoff <login>]``."""
    n, why, handoff_in = _check_input(task, reason, handoff)
    if ctx.inv.env.get("CLAUDE_CODE_REMOTE") == "true":  # REQ-TASK-78
        raise OpError("E_STATE_UNSUPPORTED_DOOR", details={"door": "web", "op": OP, "task": n})
    t = ctx.t()
    project = ctx.project()
    assert project is not None
    snap = ctx.base()
    cfg, roles = snap.project, snap.roles
    store = ctx.store()
    warnings: list[WarningObj] = []
    if snap.fallback_sha is not None:
        warnings.append(make_warning("W_CONFIG_POLICY_FALLBACK", t, details=snap.fallback_details()))
    forge = ctx.forge()
    me = forge.identity().login
    is_lead = same_login(me, roles.lead)
    handoff_to: str | None = None
    if handoff_in is not None:
        if not is_lead:
            raise OpError("E_PERM_ROLE_REQUIRED", details={"required_role": "lead", "param": "handoff"})
        person = roles.person(handoff_in)
        if person is None or not person.active or person.role not in ("researcher", "lead"):
            raise _bad("handoff", "not_a_member")
        handoff_to = person.login
    lang = translator(cfg.language) if cfg.language in LANGS else t
    base = cfg.base_branch
    branch = task_branch(n)
    steps = Steps(ctx, OP, STEPS)

    def read() -> TaskEvidence:
        ev = read_task(forge, n, authz_from_roles(roles, ci_actors=CI_ACTORS), base_branch=base)
        if ev is None:
            raise OpError("E_STATE_NOT_A_TASK", details={"task": n})
        return ev

    ev = steps.run("preflight", read)
    derived = derive_state(ev.snapshot)
    if derived.anomalies:
        warnings.append(make_warning("W_STATE_ANOMALY", t, details={"task": n, "anomalies": list(derived.anomalies)}))
    marker = {"task": n, "released_by": me, "previous_owner": derived.owner, "reason": why,
              "archive_ref": None, "handoff_to": handoff_to, "released_at": _iso(ctx)}
    if derived.state == "open":
        if steps.dry_run:
            return steps.dry_result(warnings)
        return _already_open(ctx, forge, ev, steps, me, marker, lang, warnings)
    owner = derived.owner
    mine = owner is not None and same_login(owner, me)
    if derived.state not in ACTIVE:
        raise steps.failure("preflight", "E_CLAIM_NOT_HELD", details={"task": n, "state": derived.state})
    if not (mine or is_lead):
        raise steps.failure("preflight", "E_CLAIM_NOT_HELD", details={"task": n, "owner": owner})
    git = ctx.git()
    on_task = mine and git.current_branch() == branch
    if on_task and merge_in_progress(ctx):
        raise steps.failure("preflight", "E_STATE_MERGE_IN_PROGRESS")
    pending = changes(git, []) if on_task else []
    pr = open_pr(ev)
    head = ev.snapshot.ref.head_sha if ev.snapshot.ref is not None else None
    work_done = has_work(ev, n) or bool(pending)

    if steps.dry_run:
        if pending:
            steps.effect("git_commit", branch, lang("tasks.effect.save_commit", n=len(pending)))
            steps.effect("git_push", branch, lang("tasks.effect.push", task=n))
        if pr is not None:
            steps.effect("forge_update", f"pr#{pr.number}", lang("tasks.effect.close_pr", pr=pr.number))
        if work_done:
            steps.effect("forge_create", f"refs/tags/archive/task-{n}-*", lang("tasks.effect.archive", task=n))
        steps.effect("forge_delete", branch, lang("tasks.effect.delete_ref", task=n))
        steps.effect("forge_update", f"issue#{n}", lang("tasks.effect.unassign", task=n))
        steps.effect("forge_create", f"issue#{n}", lang("tasks.effect.release_comment"))
        if on_task:
            steps.effect("git_checkout", base, lang("tasks.effect.cleanup", task=n))
        return steps.dry_result(warnings)

    pushed = False
    if pending:
        _owner, sections = owner_and_sections(ev.snapshot, derived)
        work = Work(n, branch, branch, me, derived.state, tuple(sections), is_staff(snap, me))
        pushed = bool(steps.run("save_local", lambda: save_and_push(ctx, git, snap, work, t, warnings, key="release")))
    else:
        steps.skip("save_local", "clean" if on_task else "not_on_task")
    pr_closed: int | None = None
    if pr is not None:
        steps.run("close_pr", lambda: forge.update_pull_request(pr.number, state="closed"))
        pr_closed = pr.number
    else:
        steps.skip("close_pr")
    def archive() -> tuple[str | None, str | None]:
        current = head
        if pushed:  # la cabeza cambió con el save: se vuelve a leer
            ref = forge.get_ref(branch)
            current = ref.sha if ref is not None else head
        return (archive_work(forge, n, current) if current is not None else None), current

    archive_ref: str | None = None
    if work_done:
        archive_ref, head = steps.run("archive", archive)
    else:
        steps.skip("archive", "claim_only")
    ref_deleted = bool(steps.run("delete_ref", lambda: forge.delete_ref(branch)))
    unassigned = False
    if ev.issue.assignees or any(x.lower() == EXPIRED for x in ev.issue.labels):
        unassigned = bool(steps.reflect("unassign_and_labels", lambda: _unassign(forge, ev)))
    else:
        steps.skip("unassign_and_labels")
    marker["archive_ref"] = archive_ref
    claim_sha = ev.snapshot.ref.claim_commit_sha if ev.snapshot.ref is not None else None
    key = (claim_sha or head or "")[:16] or "open"
    steps.reflect("release_comment", lambda: _comment(forge, lang, n, me, marker, key))
    deleted_local = False
    if mine:
        deleted_local = bool(steps.run("local_cleanup", lambda: _local_cleanup(ctx, git, snap, branch, head)))
    else:
        steps.skip("local_cleanup", "not_owner")
    write_task_cache(store, n, cfg.forge_repo, claimant=None, state="open", sections=(), now=ctx.inv.now())
    warnings.extend(steps.warnings(t))
    data: dict[str, object] = {
        "task": {"number": n, "url": ev.issue.url, "state": "open"},
        "changed": True,
        "unassigned": unassigned,
        "ref_deleted": ref_deleted,
        "local_branch_deleted": deleted_local,
        "archive_ref": archive_ref,
        "pr_closed": pr_closed,
        "handoff_to": handoff_to,
    }
    return ok(OP, data, warnings)
