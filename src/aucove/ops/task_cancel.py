# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove task cancel <n> [--reason <texto>]`` (05 §9.11; 04-tareas §4.4; D-09). Solo humana.

Actor: ``lead`` o ``deputy`` (06-permisos §2.3); si no, ``E_PERM_ROLE_REQUIRED``. Lee la tarea con
las lecturas de 04 §1.1 (``snapshot.read_task``). Una tarea ``merged`` no se cancela
(``E_STATE_INVALID_TRANSITION``). Una ``cancelled`` termina con ``changed:false``, salvo que le
queden la ref o un PR abierto (``ref_on_closed_issue``, ``open_pr_on_closed_issue``): entonces
completa los pasos que faltan (REQ-TASK-41).

La fase 2 de ``require_human`` (el reto ``cancel <n>``, con el plan) va después de las lecturas
y antes del primer efecto. Pasos, en el orden de REQ-TASK-41:

1. ``close_pr``: comenta el motivo en el PR abierto de ``task/<n>`` y lo cierra.
2. ``archive`` (canónico): si ``compare(<base>...task/<n>)`` trae trabajo distinto del commit de
   reclamo, ``refs/tags/archive/task-<n>-<k>``, como en ``release`` (04 §4.1 paso 4). Si falla,
   ``delete_ref`` no se ejecuta (REQ-TASK-31).
3. ``delete_ref`` (canónico): borra ``task/<n>``; el trabajo quedó archivado.
4. ``remove_labels`` (reflejo): quita ``state:proposed`` y ``state:expired``.
5. ``unassign`` (reflejo): vacía los asignados.
6. ``close_issue`` (canónico): cierra el Issue con ``state_reason: not_planned``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..domain.tasks import derive_state
from ..i18n import LANGS, translator
from .board import write_task_cache
from .release import archive_work, has_work, open_pr
from .result import OpError, Result, WarningObj, make_warning, ok
from .snapshot import CI_ACTORS, authz_from_roles, read_task, task_branch
from .steps import Steps
from .task_accept import check_task, staff_actor

if TYPE_CHECKING:
    from ..forge.base import ForgeAdapter
    from ..i18n import Translator
    from .context import OpContext

__all__ = ["OP", "STEPS", "run"]

OP = "task.cancel"
STEPS = ("close_pr", "archive", "delete_ref", "remove_labels", "unassign", "close_issue")
REASON_MAX = 500
LABELS = ("state:proposed", "state:expired")


def _check_reason(reason: object) -> str | None:
    if reason is None:
        return None
    if not isinstance(reason, str):
        raise OpError("E_VALIDATION_INPUT", details={"errors": [{"param": "reason", "reason": "type"}]})
    if len(reason) > REASON_MAX:
        raise OpError("E_VALIDATION_INPUT", details={"errors": [{"param": "reason", "reason": "maxLength"}]})
    return reason.strip() or None


def _pr_comment(lang: Translator, n: int, me: str, why: str | None) -> str:
    text = lang("tasks.cancel.pr_comment", task=n, login=me)
    if why:
        text += "\n\n" + lang("tasks.cancel.reason", reason=why)
    return text


def run(ctx: OpContext, *, task: object = None, reason: object = None) -> Result:
    """``task cancel <n> [--reason <texto>]``."""
    n = check_task(task)
    why = _check_reason(reason)
    t = ctx.t()
    ctx.project()
    snap = ctx.base()
    cfg = snap.project
    warnings: list[WarningObj] = []
    if snap.fallback_sha is not None:
        warnings.append(make_warning("W_CONFIG_POLICY_FALLBACK", t, details=snap.fallback_details()))
    forge = ctx.forge()
    me = staff_actor(snap, forge)
    ev = read_task(forge, n, authz_from_roles(snap.roles, ci_actors=CI_ACTORS), base_branch=cfg.base_branch)
    if ev is None:
        raise OpError("E_STATE_NOT_A_TASK", details={"task": n})
    derived = derive_state(ev.snapshot)
    if derived.anomalies:
        warnings.append(make_warning("W_STATE_ANOMALY", t, details={"task": n, "anomalies": list(derived.anomalies)}))
    if derived.state == "merged":
        raise OpError("E_STATE_INVALID_TRANSITION", details={"task": n, "state": "merged"}, warnings=warnings)
    lang = translator(cfg.language) if cfg.language in LANGS else t
    branch = task_branch(n)
    pr = open_pr(ev)
    ref = ev.snapshot.ref
    steps = Steps(ctx, OP, STEPS)
    data: dict[str, object] = {"task": {"number": n, "url": ev.issue.url, "state": "cancelled"}, "changed": False,
                               "pr_closed": None, "ref_deleted": False, "archive_ref": None}
    if derived.state == "cancelled" and pr is None and ref is None:  # nada pendiente (REQ-TASK-41)
        for step in STEPS:
            steps.skip(step, "already_cancelled")
        return steps.dry_result(warnings) if steps.dry_run else ok(OP, data, warnings)
    archive = ref is not None and has_work(ev, n)
    labels = [x for x in LABELS if x in ev.issue.labels]
    issue_open = ev.issue.state == "open"
    plan: list[tuple[str, str, str]] = []
    if pr is not None:
        plan.append(("forge_update", f"pr#{pr.number}", lang("tasks.effect.close_pr", pr=pr.number)))
    if archive:
        plan.append(("forge_create", f"refs/tags/archive/task-{n}-*", lang("tasks.effect.archive", task=n)))
    if ref is not None:
        plan.append(("forge_delete", branch, lang("tasks.effect.delete_ref", task=n)))
    if labels:
        plan.append(("forge_update", f"issue#{n}",
                     lang("tasks.effect.remove_labels", task=n, labels=", ".join(f"`{x}`" for x in labels))))
    if ev.issue.assignees:
        plan.append(("forge_update", f"issue#{n}", lang("tasks.effect.unassign_only", task=n)))
    if issue_open:
        plan.append(("forge_update", f"issue#{n}", lang("tasks.effect.close_issue", task=n)))
    if steps.dry_run:
        for kind, target, summary in plan:
            steps.effect(kind, target, summary)
        return steps.dry_result(warnings)
    summary = "\n".join([lang("tasks.cancel.summary", task=n, title=ev.issue.title),
                         *(f"- {line}" for _kind, _target, line in plan)])
    ctx.confirm_human("cancel", str(n), summary)
    _apply(forge, steps, n, me, why, lang, data, pr_number=pr.number if pr is not None else None,
           head=ref.head_sha if ref is not None else None, archive=archive, labels=labels, issue_open=issue_open,
           assignees=bool(ev.issue.assignees))
    store = ctx.store()
    if store.task(n) is not None:  # la caché de la tarea deja de dar un reclamo (06 §2.1)
        write_task_cache(store, n, cfg.forge_repo, claimant=None, state="cancelled", sections=(), now=ctx.inv.now())
    warnings.extend(steps.warnings(t))
    return ok(OP, data, warnings)


def _apply(forge: ForgeAdapter, steps: Steps, n: int, me: str, why: str | None, lang: Translator,
           data: dict[str, object], *, pr_number: int | None, head: str | None, archive: bool, labels: list[str],
           issue_open: bool, assignees: bool) -> None:
    """Los seis pasos de REQ-TASK-41, en orden; un canónico que falla detiene el resto."""
    if pr_number is not None:
        def close() -> None:
            forge.create_comment(pr_number, _pr_comment(lang, n, me, why), op_id=f"cancel-{n}-{pr_number}")
            forge.update_pull_request(pr_number, state="closed")

        steps.run("close_pr", close)
        data["pr_closed"] = pr_number
    else:
        steps.skip("close_pr")
    if archive and head is not None:
        data["archive_ref"] = steps.run("archive", lambda: archive_work(forge, n, head))
    else:
        steps.skip("archive", "no_ref" if head is None else "claim_only")
    if head is not None:
        data["ref_deleted"] = bool(steps.run("delete_ref", lambda: forge.delete_ref(task_branch(n))))
    else:
        steps.skip("delete_ref", "no_ref")
    if labels:
        steps.reflect("remove_labels", lambda: [forge.remove_label(n, x) for x in labels])
    else:
        steps.skip("remove_labels")
    if assignees:
        steps.reflect("unassign", lambda: forge.set_assignees(n, []))
    else:
        steps.skip("unassign")
    if issue_open:
        steps.run("close_issue", lambda: forge.update_issue(n, state="closed", state_reason="not_planned"))
    else:
        steps.skip("close_issue", "closed")
    data["changed"] = True
