# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove task accept <n>`` (05 §9.11; 04-tareas §4.4; D-09). Solo humana, sin MCP.

Actor: ``lead`` o ``deputy`` (06-permisos §2.3); si no, ``E_PERM_ROLE_REQUIRED``. Lee el Issue y
sus eventos (08 §7.5: 2 lecturas) y aplica ``derive_state`` a esa instantánea parcial: con el
Issue abierto y sin aceptación válida (``state:proposed`` puesta, o quitada por alguien sin
autoridad), la tarea está ``proposed`` (04 §1.5, REQ-TASK-8). Antes de cambiar nada valida P2
sobre el Issue (REQ-TASK-40): las partes que declara existen en ``roles.yaml`` de la base; si
no, ``E_VALIDATION_UNKNOWN_SECTION``.

La fase 2 de ``require_human`` (el reto ``accept <n>``) va después de las lecturas y antes del
único paso, ``remove_label`` (canónico), que quita ``state:proposed``. Si el Issue no la tiene,
la agrega y la quita, para que la aceptación quede registrada en los eventos (REQ-TASK-40).

Si la tarea ya está aceptada, o el Issue está cerrado, lee el resto de 04 §1.1 para saber su
estado: ``open`` termina con ``changed:false``; cualquier otro, ``E_STATE_INVALID_TRANSITION``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..domain.taskbody import analyze_task_body
from ..domain.tasks import LabelEvent, TaskSnapshot, derive_state
from ..forge.base import ForgeValidationError
from ..i18n import LANGS, translator
from .result import OpError, Result, WarningObj, make_warning, ok
from .save import is_staff
from .snapshot import CI_ACTORS, TASK_LABEL, authz_from_roles, read_task
from .steps import Steps

if TYPE_CHECKING:
    from ..config.base import BaseSnapshot
    from ..forge.base import ForgeAdapter, Issue
    from .context import OpContext

__all__ = ["OP", "PROPOSED", "STEPS", "check_task", "run", "staff_actor"]

OP = "task.accept"
STEPS = ("remove_label",)
PROPOSED = "state:proposed"


def check_task(task: object) -> int:
    """``task`` entero ≥ 1 o ``E_VALIDATION_INPUT``."""
    if isinstance(task, bool) or not isinstance(task, int) or task < 1:
        raise OpError("E_VALIDATION_INPUT", details={"errors": [{"param": "task", "reason": "type"}]})
    return task


def staff_actor(snap: BaseSnapshot, forge: ForgeAdapter) -> str:
    """Login del actor verificado en la forja; ``E_PERM_ROLE_REQUIRED`` si no es ``lead`` ni ``deputy``."""
    ident = forge.identity()
    login = snap.roles.identify(ident.login, ident.id).login or ident.login
    if not is_staff(snap, login):
        raise OpError("E_PERM_ROLE_REQUIRED", details={"login": login, "required_role": ["lead", "deputy"]})
    return login


def _issue(forge: ForgeAdapter, number: int) -> Issue:
    try:
        issue = forge.get_issue(number)
    except ForgeValidationError:  # el número es de un PR (08 REQ-FORGE-9)
        issue = None
    if issue is None or not any(x.lower() == TASK_LABEL for x in issue.labels):
        raise OpError("E_STATE_NOT_A_TASK", details={"task": number})
    return issue


def run(ctx: OpContext, *, task: object = None) -> Result:
    """``task accept <n>``."""
    n = check_task(task)
    t = ctx.t()
    ctx.project()
    snap = ctx.base()
    cfg, roles = snap.project, snap.roles
    warnings: list[WarningObj] = []
    if snap.fallback_sha is not None:
        warnings.append(make_warning("W_CONFIG_POLICY_FALLBACK", t, details=snap.fallback_details()))
    forge = ctx.forge()
    staff_actor(snap, forge)
    issue = _issue(forge, n)
    body = analyze_task_body(issue.body)
    partial = TaskSnapshot(
        number=n, issue_author=issue.author, issue_state=issue.state, state_reason=issue.state_reason,
        labels=frozenset(issue.labels),
        label_events=tuple(LabelEvent(e.label, e.event, e.actor, e.created_at) for e in forge.list_issue_events(n)),
        assignees=tuple(issue.assignees), sections=body.sections, spec_hash=body.spec_hash, issue_markers=(),
        ref=None, pulls=(), base_branch=cfg.base_branch, authz=authz_from_roles(roles, ci_actors=CI_ACTORS),
    )
    if derive_state(partial).state != "proposed":  # aceptada o cerrada: decide el estado completo
        return _not_proposed(ctx, forge, n, warnings)
    owners = roles.section_owner()
    unknown = [s for s in body.sections if s not in owners]
    if unknown:  # P2 (REQ-TASK-40): no cambia nada
        raise OpError("E_VALIDATION_UNKNOWN_SECTION", details={"sections": unknown})
    labeled = PROPOSED in issue.labels
    lang = translator(cfg.language) if cfg.language in LANGS else t
    steps = Steps(ctx, OP, STEPS)
    if steps.dry_run:
        if not labeled:
            steps.effect("forge_update", f"issue#{n}", lang("tasks.effect.add_proposed", task=n))
        steps.effect("forge_update", f"issue#{n}", lang("tasks.effect.accept", task=n))
        return steps.dry_result(warnings)
    ctx.confirm_human("accept", str(n), lang("tasks.accept.summary", task=n, title=issue.title))

    def accept() -> None:
        if not labeled:  # sin la etiqueta no habría un evento que registre la aceptación
            forge.add_labels(n, [PROPOSED])
        forge.remove_label(n, PROPOSED)

    steps.run("remove_label", accept)
    warnings.extend(steps.warnings(t))
    return ok(OP, {"task": {"number": n, "url": issue.url, "state": "open"}, "changed": True}, warnings)


def _not_proposed(ctx: OpContext, forge: ForgeAdapter, n: int, warnings: list[WarningObj]) -> Result:
    """Ya aceptada o con el Issue cerrado: decide el estado de las lecturas de 04 §1.1."""
    snap = ctx.base()
    ev = read_task(forge, n, authz_from_roles(snap.roles, ci_actors=CI_ACTORS), base_branch=snap.project.base_branch)
    if ev is None:
        raise OpError("E_STATE_NOT_A_TASK", details={"task": n})
    derived = derive_state(ev.snapshot)
    if derived.anomalies:
        warnings.append(make_warning("W_STATE_ANOMALY", ctx.t(),
                                     details={"task": n, "anomalies": list(derived.anomalies)}))
    if derived.state != "open":
        raise OpError("E_STATE_INVALID_TRANSITION", details={"task": n, "state": derived.state}, warnings=warnings)
    steps = Steps(ctx, OP, STEPS)
    steps.skip("remove_label", "already_open")
    if steps.dry_run:
        return steps.dry_result(warnings)
    return ok(OP, {"task": {"number": n, "url": ev.issue.url, "state": "open"}, "changed": False}, warnings)
