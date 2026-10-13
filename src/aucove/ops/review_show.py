# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove review show <pr>`` · ``prepare_review`` (05 §9.23; 06-permisos §3.2 y §3.3).

Solo lee. El actor debe tener un rol en el PR (aprobador de 06 §3.2 o dueño de una parte
tocada) o ser ``lead`` o ``deputy``; si no, ``E_PERM_NOT_REVIEWER``. Arma:

- ``files``: cada archivo del PR con su clase (02 §3.3), el dueño de su parte y si la tarea lo
  permite (``check_path`` en modo ``ci`` con la política de la base, 06 §2.4). Los
  ``generated`` quedan en ``null``: los valida el CI contra su regeneración. Lo que no se
  permite va también a ``scope_violations``.
- ``ci``: los checks de la cabeza; ``pdf_url``, el enlace del comentario ``build`` del CI.
- ``assessments``: las evaluaciones del dueño del reclamo, de fase ``claim`` (en el Issue) y
  ``submission`` (en el PR).
- ``approval``: ``evaluate_approvals`` sobre la cabeza actual (REQ-PERM-16), con lo que falta.
- ``human_actions``: la vista de archivos del PR en la web como vía preferente y el comando
  de la CLI, sin ninguna acción que pueda ejecutar el agente (REQ-OPS-45, REQ-PERM-46).

El cuerpo del PR va como ``marked_text`` de hasta 2 000 caracteres (REQ-OPS-39). Con el CI en
rojo agrega ``W_VALIDATION_CI_FAILING``. Pagina ``files`` (tope de 16 000, 05 §7.1); si aun así
no cabe, recorta ``assessments``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from .. import _meta
from ..config.base import to_policy
from ..domain.approvals import evaluate_approvals, review_role, touches_latex
from ..domain.marked import TITLE_MAX, SanitizeTally, marked
from ..domain.paths import InvalidPath, OutsideRepo, canonical, classify
from ..domain.permissions import check_change, is_control_path
from ..domain.tasks import derive_state
from ..domain.text import same_login
from ..forge.base import ForgeError, ForgeValidationError
from .board import FLAG_LABELS
from .ci_comment import build_pdf_url, find_ci_comment
from .fit import decode_cursor, encode_cursor, fit, snap_of
from .result import OpError, Result, WarningObj, make_warning, ok
from .reviews import approval_policy, files_url, review_states
from .save import is_staff
from .snapshot import (
    CI_ACTORS,
    TASK_LABEL,
    Prefetch,
    TaskEvidence,
    authz_from_roles,
    collect_task,
    parse_markers,
    task_branch,
    task_number,
    to_permission_task,
)

if TYPE_CHECKING:
    from ..forge.base import Checks, Comment, ForgeAdapter, PullRequest, Review
    from ..i18n import Translator
    from .context import OpContext

__all__ = ["BODY_MAX", "BUDGET", "OP", "render_text", "run"]

OP = "review.show"
BUDGET = 16000  # 05 §7.1
BODY_MAX = 2000  # REQ-OPS-39
REASON_MAX = 500
_PLACEHOLDER_OFFSET = 10**6
_CI_STATE = {"success": "success", "failure": "failure", "pending": "pending"}


def _bad(param: str, reason: str = "type") -> OpError:
    return OpError("E_VALIDATION_INPUT", details={"errors": [{"param": param, "reason": reason}]})


def _check_input(pr: object, cursor: object, max_chars: object) -> tuple[int, str | None, int | None]:
    if isinstance(pr, bool) or not isinstance(pr, int) or pr < 1:
        raise _bad("pr")
    if cursor is not None and not isinstance(cursor, str):
        raise _bad("cursor")
    if max_chars is not None and (
        isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 2000 <= max_chars <= 40000
    ):
        raise _bad("max_chars", "range")
    return pr, cursor, max_chars


def _task(forge: ForgeAdapter, pull: PullRequest, reviews: Sequence[Review], ctx: OpContext) -> TaskEvidence | None:
    """La tarea de la cabeza ``task/<n>`` con las lecturas de 04 §1.1 (el PR ya está leído)."""
    n = task_number(pull.head_branch) if pull.same_repo else None
    if n is None:
        return None
    try:
        issue = forge.get_issue(n)
    except ForgeValidationError:
        return None
    if issue is None or not any(x.lower() == TASK_LABEL for x in issue.labels):
        return None
    snap = ctx.base()
    prefetch = Prefetch(issue=issue, ref=forge.get_ref(task_branch(n)), pulls=(pull,), reviews={pull.number: reviews})
    return collect_task(forge, n, authz_from_roles(snap.roles, ci_actors=CI_ACTORS), base_branch=snap.project.base_branch,
                        prefetch=prefetch)


def _assessments(comments: Sequence[Comment], owner: str | None, tally: SanitizeTally) -> list[dict[str, Any]]:
    """Las evaluaciones del dueño del reclamo (04 §1.3: solo cuentan las suyas)."""
    out: list[dict[str, Any]] = []
    for c in comments:
        if not same_login(c.author, owner):
            continue
        for kind, data in parse_markers(c.body):
            phase = data.get("phase")
            if kind != "assessment" or phase not in ("claim", "submission"):
                continue
            verdict = data.get("effective_verdict", data.get("verdict"))
            direction = data.get("direction")
            ids = direction.get("affected_ids") if isinstance(direction, Mapping) else None
            overlaps = data.get("overlaps")
            reasons = [o.get("reason") for o in overlaps if isinstance(o, Mapping)] if isinstance(overlaps, list) else []
            if isinstance(direction, Mapping):
                reasons.append(direction.get("reason"))
            reason = next((r for r in reasons if isinstance(r, str) and r.strip()), "")
            out.append({
                "phase": phase,
                "overlap_verdict": verdict if verdict in ("none", "possible", "clear") else "none",
                "direction_verdict": "possible" if isinstance(ids, list) and ids else "none",
                "reason": marked("untrusted", f"comment#{c.id}.assessment", reason, limit=REASON_MAX, tally=tally),
                "url": c.url,
            })
    return out


def _ci(checks: Checks) -> dict[str, object]:
    return {"state": _CI_STATE.get(checks.overall, "unknown"),
            "checks": [{"name": r.name, "conclusion": r.conclusion} for r in checks.runs]}


def run(ctx: OpContext, *, pr: object = None, cursor: object = None, max_chars: object = None) -> Result:
    """``review show <pr> [--cursor <c>] [--max-chars <n>]``."""
    n, cursor_v, max_v = _check_input(pr, cursor, max_chars)
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
    files = forge.list_pull_request_files(n)
    pol = approval_policy(snap)
    owners = roles.section_owner()
    declared = frozenset(owners)
    root = str(project.root)
    classes: dict[str, tuple[str, str | None]] = {}
    for f in files:
        try:
            info = classify(canonical(f.path, root, False), declared, symlinked=False)
            classes[f.path] = (info.cls, info.section)
        except (InvalidPath, OutsideRepo):
            classes[f.path] = ("outside", None)
    touched = sorted({sec for cls, sec in classes.values() if cls == "section" and sec})
    touches = touches_latex([f.path for f in files] + [f.previous_path for f in files if f.previous_path])
    role = review_role(me, pull.author, touches, touched, owners, pol)
    if role == "none" and not is_staff(snap, me):
        raise OpError("E_PERM_NOT_REVIEWER", details={"pr": n, "login": me})
    reviews = forge.list_reviews(n)
    checks = forge.get_checks(pull.head_sha)
    comments = forge.list_comments(n)
    ev = _task(forge, pull, reviews, ctx)
    task_perm = to_permission_task(ev.snapshot)[0] if ev is not None else None
    owner = derive_state(ev.snapshot).owner if ev is not None else pull.author
    policy = to_policy(snap, root=root, ci_fs=False)
    tally = SanitizeTally()
    rows: list[dict[str, Any]] = []
    violations: list[dict[str, object]] = []
    for f in files:
        cls, sec = classes[f.path]
        allowed: bool | None = None
        if task_perm is not None and cls != "generated":  # los generated los valida el CI (06 §2.2)
            decisions = check_change(pull.author, f.status, f.path, f.previous_path, task_perm, pol=policy, mode="ci")
            allowed = all(d.allow for d in decisions)
            violations += [{"path": d.path or f.path, "reason_code": d.code} for d in decisions if not d.allow and d.code]
        rows.append({"path": f.path, "class": cls, "owner": owners.get(sec) if sec else None,
                     "additions": f.additions, "deletions": f.deletions, "allowed_for_task": allowed})
    approval = evaluate_approvals(pull.author, pull.head_sha, touches, review_states(reviews), pol)
    try:
        profile = forge.capabilities().profile
    except ForgeError:  # sin capacidades se informa el perfil completo
        profile = "enforced"
    build = find_ci_comment(comments, "build")
    web = files_url(pull.url)
    actions: list[dict[str, str]] = []
    if role == "approver":
        actions.append({"action": "approve", "web_url": web, "cli": f"{_meta.NAME} review approve {n}"})
    if role in ("approver", "owner"):
        actions.append({"action": "request_changes", "web_url": web, "cli": f"{_meta.NAME} review request-changes {n}"})
    labels = set(pull.labels) | (set(ev.issue.labels) if ev is not None else set())
    ci = _ci(checks)
    if ci["state"] == "failure":
        warnings.append(make_warning("W_VALIDATION_CI_FAILING", t, details={"pr": n, "head_sha": pull.head_sha}))
    assessments = _assessments(ev.comments if ev is not None else (), owner, tally) + _assessments(comments, owner, tally)
    ids = [r["path"] for r in rows]
    snap_id = snap_of(ids)
    offset = decode_cursor(cursor_v, op=OP, list_name="files", snap=snap_id) if cursor_v is not None else 0
    shown = rows[offset:]
    data: dict[str, object] = {
        "pr": {
            "number": n,
            "url": pull.url,
            "files_url": web,
            "title": marked("untrusted", f"pr#{n}.title", pull.title, limit=TITLE_MAX, tally=tally),
            "body": marked("untrusted", f"pr#{n}.body", pull.body, limit=BODY_MAX, tally=tally) if pull.body else None,
            "author": pull.author,
            "base": pull.base_branch,
            "head_sha": pull.head_sha,
            "draft": pull.draft,
        },
        "task": ev.issue.number if ev is not None else None,
        "files": shown,
        "scope_violations": violations,
        "ci": ci,
        "pdf_url": build_pdf_url(build.body) if build is not None else None,
        "assessments": assessments,
        "flags": sorted(x for x in labels if x in FLAG_LABELS),
        "reviews": [{"login": r.author, "state": r.state, "commit_id": r.commit_sha} for r in reviews],
        "approval": {"profile": profile, "required_from": list(approval.required_from), "satisfied": approval.ok},
        "human_actions": actions,
        "page": {"next_cursor": encode_cursor(OP, "files", _PLACEHOLDER_OFFSET, snap_id), "returned": len(shown),
                 "total": len(rows)},
    }
    sanitized = tally.details()
    if sanitized is not None:
        warnings.append(make_warning("W_VALIDATION_UNTRUSTED_SANITIZED", t, details=sanitized))
    budget = max_v or BUDGET
    trim = ["files", "assessments"]
    omitted = {"files": len(shown), "assessments": len(assessments)}
    placeholder = make_warning("W_OUTPUT_TRUNCATED", t, details={"budget": budget, "trimmed": trim, "omitted": omitted})
    trimmed = fit(OP, data, budget=budget, trim_order=trim, warnings=warnings, truncated_warning=placeholder)
    kept = data["files"]
    assert isinstance(kept, list)
    page = data["page"]
    assert isinstance(page, dict)
    page["returned"] = len(kept)
    more = bool(trimmed.omitted.get("files"))
    page["next_cursor"] = encode_cursor(OP, "files", offset + len(kept), snap_id) if more else None
    if trimmed.trimmed:
        warnings.append(make_warning("W_OUTPUT_TRUNCATED", t, details=trimmed.warning_details(budget)))
    return ok(OP, data, warnings)


def render_text(result: Result, t: Translator, *, fmt: str = "text") -> str:
    """El resumen para la persona: el PR, el CI, lo que falta aprobar, las rutas de control
    (REQ-PERM-18) y la web como vía preferente para revisar (REQ-OPS-45)."""
    del fmt
    data = result.data if isinstance(result.data, dict) else {}
    pr = data.get("pr")
    if not isinstance(pr, dict):
        return ""
    title = " ".join(str((pr.get("title") or {}).get("text", "")).split())
    approval = data.get("approval") or {}
    lines = [
        t("review.show.header", pr=pr.get("number"), title=title, author=pr.get("author")),
        t("review.show.head", sha=str(pr.get("head_sha", ""))[:12], ci=(data.get("ci") or {}).get("state")),
        t("review.show.approval_ok") if approval.get("satisfied") else t(
            "review.show.approval_missing", logins=", ".join(f"@{x}" for x in approval.get("required_from") or []) or "-"),
    ]
    files = data.get("files") or []
    control = [f["path"] for f in files if is_control_path(f["path"])]
    if control:
        lines.append(t("review.show.control", paths=", ".join(control)))
    paths = {f["path"] for f in files}
    if "plan.md" in paths and not any(p.startswith("memory/decisions/") for p in paths):
        lines.append(t("review.show.plan_without_decision"))
    for v in data.get("scope_violations") or []:
        lines.append(t("review.show.violation", path=v["path"], code=v["reason_code"]))
    if data.get("pdf_url"):
        lines.append(t("review.show.pdf", url=data["pdf_url"]))
    for action in data.get("human_actions") or []:
        lines.append(t(f"review.show.action.{action['action']}", url=action["web_url"], cli=action["cli"]))
    page = data.get("page") or {}
    if page.get("next_cursor"):
        lines.append(t("review.show.more", cursor=page["next_cursor"]))
    return "\n".join(lines)
