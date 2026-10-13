# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Lo que comparten ``review list``, ``review show`` y las revisiones H (05 §9.22 a §9.24).

La matriz de aprobación es la de 06 §3.2 (``domain.approvals``), siempre con la política de la
base (REQ-PERM-16): ``lead`` y ``deputy`` aprueban, el editor del artículo suma R2 con
``format: latex`` y ``mode: individual`` no pide aprobaciones.

``decide`` es ``review approve`` y ``review request-changes`` (05 §9.24), solo humanas:

- El PR está abierto y no es borrador (``E_STATE_PR_NOT_OPEN``, ``E_STATE_PR_DRAFT``); el actor
  no es su autor (``E_PERM_SELF_REVIEW``) y su rol lo permite: aprobar, un aprobador que cuenta;
  pedir cambios, también el dueño de una parte tocada (``E_PERM_NOT_REVIEWER``).
- Lee el PR, sus archivos, los checks de la cabeza y las revisiones. Si el actor ya dejó esa
  misma revisión sobre esa cabeza, termina con ``created:false`` sin pedir nada.
- La fase 2 de ``require_human`` muestra el resumen (cabeza, CI, archivos, lo que falta
  aprobar y las rutas de control) y pide el reto ``approve <pr>`` o ``request-changes <pr>``.
- ``recheck_head`` vuelve a leer el PR: si la cabeza cambió, ``E_STATE_PR_HEAD_CHANGED``.
- ``create_review`` (canónico): ``submit_review`` sobre la cabeza que se mostró, con la
  ``HumanConfirmation`` (08 REQ-FORGE-10). El núcleo nunca integra (REQ-OPS-46): aprobar
  devuelve ``merge_url``, la página del PR.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..domain.approvals import (
    Approval,
    ApprovalPolicy,
    ReviewState,
    evaluate_approvals,
    last_approval_at,
    review_role,
    touches_latex,
)
from ..domain.paths import InvalidPath, OutsideRepo, canonical, classify
from ..domain.permissions import is_control_path
from ..domain.text import same_login
from ..forge.base import ForgeValidationError
from .result import OpError, Result, WarningObj, make_warning, ok
from .steps import Steps

if TYPE_CHECKING:
    from datetime import datetime

    from ..config.base import BaseSnapshot
    from ..forge.base import ChangedFile, ForgeAdapter, PullRequest, Review
    from ..i18n import Translator
    from .context import OpContext

__all__ = [
    "BODY_MAX", "DECISION_STEPS", "MergeApproval", "approval_at_merge", "approval_policy", "approval_record", "decide",
    "files_url", "pr_touches_latex", "render_decision", "review_states",
]

DECISION_STEPS = ("recheck_head", "create_review")
BODY_MAX = 8000
# evento de submit_review -> (operación del reto de 06 §6, estado de la revisión)
_EVENTS = {"APPROVE": ("approve", "APPROVED"), "REQUEST_CHANGES": ("request-changes", "CHANGES_REQUESTED")}
_COUNTED = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")
_CI_STATE = {"success": "success", "failure": "failure", "pending": "pending"}


def approval_policy(snap: BaseSnapshot) -> ApprovalPolicy:
    """La política de 06 §3.2 desde ``project.yaml`` y ``roles.yaml`` de la base."""
    cfg, roles = snap.project, snap.roles
    approvers = tuple(x for x in (roles.lead, roles.deputy) if x)
    return ApprovalPolicy(approvers=approvers, editor=roles.editor, individual=cfg.mode == "individual",
                          latex=cfg.format == "latex")


def review_states(reviews: Sequence[Review]) -> list[ReviewState]:
    """Las revisiones de la forja como las lee ``evaluate_approvals``."""
    return [ReviewState(r.author, r.state, r.commit_sha, r.submitted_at) for r in reviews]


@dataclass(frozen=True)
class MergeApproval:
    """La aprobación de un PR como estaba al integrarse (06 §3.5; 12-mvp §5.3)."""

    approval: Approval
    no_deputy: bool  # 06 §3.4: PR del lead sin suplente, al que solo le faltaba la aprobación imposible
    approved_at: datetime | None  # la última aprobación que contaba


def pr_touches_latex(files: Sequence[ChangedFile]) -> bool:
    """Si el PR toca LaTeX (R2, 06 §3.2). Con ``MAX_PR_FILES`` archivos o más se supone que sí:
    la lista puede estar incompleta (06 REQ-PERM-15)."""
    from .rules.scope import MAX_PR_FILES

    return len(files) >= MAX_PR_FILES or touches_latex(p for f in files for p in (f.path, f.previous_path) if p)


def approval_at_merge(snap: BaseSnapshot, pull: PullRequest, files: Sequence[ChangedFile],
                      reviews: Sequence[Review]) -> MergeApproval:
    """``evaluate_approvals`` con la cabeza al integrar y solo las revisiones anteriores a la
    integración; en un PR abierto, todas. Lo comparten ``ci audit`` (09 REQ-CI-46) y ``metrics``."""
    touches = pr_touches_latex(files)
    merged_at = pull.merged_at
    states = [r for r in review_states(reviews)
              if merged_at is None or r.submitted_at is None or r.submitted_at <= merged_at]
    pol = approval_policy(snap)
    approval = evaluate_approvals(pull.author, pull.head_sha, touches, states, pol)
    no_deputy = (approval.unsatisfiable == ("no_deputy",) and not approval.missing and not approval.blocking
                 and same_login(pull.author, snap.roles.lead))
    return MergeApproval(approval, no_deputy, last_approval_at(pull.author, pull.head_sha, touches, states, pol))


def approval_record(forge: ForgeAdapter, snap: BaseSnapshot, number: int, *, merge_audit: str | None,
                    fetched_at: datetime) -> dict[str, object] | None:
    """El registro ``approval-record/v1`` del PR ``number`` (11-trazabilidad §4.3, REQ-TRACE-45).

    ``snap`` es la política del primer padre del commit de squash (REQ-TRACE-48): la de ``--before``
    en ``ci audit``. Lee el PR, sus archivos (si toca LaTeX) y sus revisiones; ``None`` si el PR no
    existe. ``source.api`` es la URL de la API de GitHub o, en otra forja, la del PR.
    """
    from ..domain.approval_record import Fetched, RolesAtBase, derive_approval_record

    pull = forge.get_pull_request(number)
    if pull is None:
        return None
    roles = snap.roles
    role_map = {p.login.casefold(): ("lead" if same_login(p.login, roles.lead)
                                     else "deputy" if same_login(p.login, roles.deputy) else "researcher")
                for p in roles.people if p.active}
    caps = forge.capabilities()
    api = (f"https://api.github.com/repos/{caps.repo.owner}/{caps.repo.name}/pulls/{number}" if caps.forge == "github"
           else pull.url)
    at_base = RolesAtBase(roles.source_sha12, role_map, approval_policy(snap),
                          pr_touches_latex(forge.list_pull_request_files(number)))
    return derive_approval_record(pull, forge.list_reviews(number), roles=at_base, profile=caps.profile,
                                  merge_audit=merge_audit, fetched=Fetched(caps.forge, api, fetched_at))


def files_url(pr_url: str) -> str:
    """La vista de archivos del PR en la web: la vía preferente para revisar (REQ-OPS-45)."""
    return pr_url.rstrip("/") + "/files"


def _bad(param: str, reason: str = "type") -> OpError:
    return OpError("E_VALIDATION_INPUT", details={"errors": [{"param": param, "reason": reason}]})


def _check_input(pr: object, body: object, *, required: bool) -> tuple[int, str]:
    if isinstance(pr, bool) or not isinstance(pr, int) or pr < 1:
        raise _bad("pr")
    if body is not None and not isinstance(body, str):
        raise _bad("body")
    text = (body or "").replace("\r\n", "\n").strip()
    if len(text) > BODY_MAX:
        raise _bad("body", "maxLength")
    if required and not text:  # pedir cambios exige decir cuáles (y GitHub, un cuerpo)
        raise _bad("body", "required")
    return pr, text


def _written(review: Review, head: str) -> dict[str, object]:
    return {"id": review.id, "state": review.state, "commit_id": review.commit_sha or head}


def _summary(t: Translator, pull: Any, event: str, ci: str, paths: Sequence[str], missing: Sequence[str]) -> str:
    """El resumen de la fase 2 (05 §9.24): el mismo contenido que destaca ``review show``."""
    key = "approve" if event == "APPROVE" else "request_changes"
    title = " ".join(pull.title.split())
    lines = [
        t(f"review.confirm.{key}", pr=pull.number, title=title, author=pull.author),
        t("review.show.head", sha=pull.head_sha[:12], ci=ci),
        t("review.confirm.files", n=len(paths)),
        t("review.show.approval_missing", logins=", ".join(f"@{x}" for x in missing)) if missing
        else t("review.show.approval_ok"),
    ]
    control = [p for p in paths if is_control_path(p)]
    if control:
        lines.append(t("review.show.control", paths=", ".join(control)))
    if ci == "failure":
        lines.append(t("review.confirm.ci_failing"))
    if event == "APPROVE":
        lines.append(t("review.confirm.merge_on_web", url=pull.url))
    return "\n".join(lines)


def decide(ctx: OpContext, *, op: str, event: str, pr: object, body: object) -> Result:
    """``review approve`` o ``review request-changes`` (05 §9.24)."""
    challenge, state = _EVENTS[event]
    n, text = _check_input(pr, body, required=event == "REQUEST_CHANGES")
    t = ctx.t()
    project = ctx.project()
    assert project is not None
    snap = ctx.base()
    roles = snap.roles
    warnings: list[WarningObj] = []
    if snap.fallback_sha is not None:
        warnings.append(make_warning("W_CONFIG_POLICY_FALLBACK", t, details=snap.fallback_details()))
    forge = ctx.forge()
    ident = forge.identity()
    me = roles.identify(ident.login, ident.id).login or ident.login
    try:
        pull = forge.get_pull_request(n)
    except ForgeValidationError:
        pull = None
    if pull is None:
        raise OpError("E_FORGE_NOT_FOUND", details={"pr": n})
    if pull.state != "open":
        raise OpError("E_STATE_PR_NOT_OPEN", details={"pr": n, "state": "merged" if pull.merged else "closed"})
    if pull.draft:
        raise OpError("E_STATE_PR_DRAFT", details={"pr": n})
    if same_login(pull.author, me):
        raise OpError("E_PERM_SELF_REVIEW", details={"pr": n, "login": me})
    files = forge.list_pull_request_files(n)
    paths = [f.path for f in files] + [f.previous_path for f in files if f.previous_path]
    owners = roles.section_owner()
    touched: set[str] = set()
    for path in paths:
        try:
            info = classify(canonical(path, str(project.root), False), frozenset(owners), symlinked=False)
        except (InvalidPath, OutsideRepo):
            continue
        if info.cls == "section" and info.section:
            touched.add(info.section)
    pol = approval_policy(snap)
    touches = touches_latex(paths)
    role = review_role(me, pull.author, touches, sorted(touched), owners, pol)
    if role != "approver" and not (event == "REQUEST_CHANGES" and role == "owner"):
        raise OpError("E_PERM_NOT_REVIEWER", details={"pr": n, "login": me, "role": role})
    checks = forge.get_checks(pull.head_sha)
    ci = _CI_STATE.get(checks.overall, "unknown")
    if ci == "failure":
        warnings.append(make_warning("W_VALIDATION_CI_FAILING", t, details={"pr": n, "head_sha": pull.head_sha}))
    reviews = forge.list_reviews(n)
    head = pull.head_sha
    steps = Steps(ctx, op, DECISION_STEPS)
    data: dict[str, object] = {"pr": n, "created": False}
    if event == "APPROVE":
        data["merge_url"] = pull.url
    mine = [r for r in reviews if same_login(r.author, me) and r.state in _COUNTED]
    if mine and mine[-1].state == state and mine[-1].commit_sha == head:  # la misma revisión ya está
        for step in DECISION_STEPS:
            steps.skip(step, "exists")
        if steps.dry_run:
            return steps.dry_result(warnings)
        return ok(op, {**data, "review": _written(mine[-1], head)}, warnings)
    if steps.dry_run:
        steps.effect("forge_create", f"pr#{n}", t(f"review.effect.{challenge.replace('-', '_')}", pr=n,
                                                       sha=head[:12]))
        return steps.dry_result(warnings)
    approval = evaluate_approvals(pull.author, head, touches, review_states(reviews), pol)
    proof = ctx.confirm_human(challenge, str(n), _summary(t, pull, event, ci, paths, approval.required_from))

    def recheck() -> None:
        again = forge.get_pull_request(n)
        if again is None or again.state != "open":
            raise OpError("E_STATE_PR_NOT_OPEN", details={"pr": n})
        if again.head_sha != head:  # aprobar lo que no se mostró no vale
            raise OpError("E_STATE_PR_HEAD_CHANGED", details={"pr": n, "shown": head, "current": again.head_sha})

    steps.run("recheck_head", recheck)
    review = steps.run("create_review", lambda: forge.submit_review(n, event=event, body=text, commit_sha=head,  # type: ignore[arg-type]
                                                                    confirmation=proof))
    return ok(op, {**data, "review": _written(review, head), "created": True}, warnings)


def render_decision(result: Result, t: Translator) -> str:
    """Vista legible de ``review approve`` y ``review request-changes``."""
    data = result.data if isinstance(result.data, dict) else {}
    review = data.get("review")
    if not result.ok or not isinstance(review, dict):
        return ""
    sha = str(review.get("commit_id", ""))[:12]
    if not data.get("created"):
        return t("review.done.exists", pr=data.get("pr"), sha=sha)
    if review.get("state") == "APPROVED":
        return t("review.done.approve", pr=data.get("pr"), sha=sha, url=data.get("merge_url", ""))
    return t("review.done.request_changes", pr=data.get("pr"), sha=sha)
