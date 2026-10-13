# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove review list`` · ``list_reviews`` (05 §9.22; 06-permisos §2.3 y §3.2).

Lee el tablero de ``status`` (00-marco D-61; sin red, su caché con ``W_NET_OFFLINE``) y, por
cada PR abierto de una tarea, calcula con la matriz de 06 §3.2 el rol del actor: ``approver``
si su aprobación cuenta, ``owner`` si es dueño de una parte de la tarea (puede pedir cambios) y
``none`` si no, o si es el autor. Sin ``all`` lista los que lo requieren: no borradores, con un
rol o con la revisión pedida. ``--all`` solo lo usan el ``lead`` y el ``deputy``
(``E_PERM_ROLE_REQUIRED``).

R2 (el editor, 00-marco D-56) depende de si el PR toca LaTeX, y eso solo cambia el rol del
propio editor: entonces, con red, se leen los archivos del PR; sin red se supone que una tarea
con partes toca LaTeX. ``age_h`` cuenta desde la última actualización del PR. Pagina
``reviews`` con ``cursor`` y ``max_chars`` (REQ-OPS-36; tope de 8 000, 05 §7.1).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..domain.approvals import review_role, touches_latex
from ..domain.marked import TITLE_MAX, SanitizeTally, marked
from ..domain.text import same_login
from .board import FLAG_LABELS, load_board
from .fit import decode_cursor, encode_cursor, fit, snap_of
from .result import OpError, Result, WarningObj, make_warning, ok
from .reviews import approval_policy
from .save import is_staff

if TYPE_CHECKING:
    from ..i18n import Translator
    from .board import Loaded, PrView, TaskView
    from .context import OpContext

__all__ = ["BUDGET", "OP", "render_text", "run"]

OP = "review.list"
BUDGET = 8000  # 05 §7.1
_PLACEHOLDER_OFFSET = 10**6


def _bad(param: str, reason: str = "type") -> OpError:
    return OpError("E_VALIDATION_INPUT", details={"errors": [{"param": param, "reason": reason}]})


def _check_input(all_: object, cursor: object, max_chars: object) -> tuple[bool, str | None, int | None]:
    if not isinstance(all_, bool):
        raise _bad("all")
    if cursor is not None and not isinstance(cursor, str):
        raise _bad("cursor")
    if max_chars is not None and (
        isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 2000 <= max_chars <= 40000
    ):
        raise _bad("max_chars", "range")
    return all_, cursor, max_chars


def _age_h(pr: PrView, task: TaskView, now: datetime) -> float:
    raw = pr.updated_at or task.updated_at
    try:
        then = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else now
    except ValueError:
        then = now
    return round(max((now - then).total_seconds(), 0.0) / 3600, 1)


def _touches(ctx: OpContext, loaded: Loaded, pr: PrView, task: TaskView, exact: bool) -> bool:
    """Si el PR toca LaTeX: con red, por sus archivos; sin red, si la tarea tiene partes."""
    if exact and loaded.fetched is not None:
        return touches_latex(f.path for f in ctx.forge().list_pull_request_files(pr.number))
    return bool(task.sections)


def run(ctx: OpContext, *, all: object = False, cursor: object = None,  # noqa: A002 (nombre del parámetro)
        max_chars: object = None) -> Result:
    """``review list [--all] [--cursor <c>] [--max-chars <n>]``."""
    all_v, cursor_v, max_v = _check_input(all, cursor, max_chars)
    t = ctx.t()
    ctx.project()
    snap = ctx.base()
    cfg, roles = snap.project, snap.roles
    store = ctx.store()
    now = ctx.inv.now()
    warnings: list[WarningObj] = []
    if snap.fallback_sha is not None:
        warnings.append(make_warning("W_CONFIG_POLICY_FALLBACK", t, details=snap.fallback_details()))
    loaded = load_board(ctx, cfg=cfg, roles=roles, store=store, now=now, mode="live")
    if loaded is None:
        raise OpError("E_NET_OFFLINE")
    board = loaded.board
    if loaded.source == "cache":
        warnings.append(make_warning("W_NET_OFFLINE", t, details=loaded.offline_details(now)))
    if board.profile == "advisory":  # 12-mvp A5: el aviso en todas las salidas de review list
        warnings.append(make_warning("W_FORGE_PROFILE_ADVISORY", t))
    me = roles.identify(board.login).login or board.login or ""
    if all_v and not is_staff(snap, me):
        raise OpError("E_PERM_ROLE_REQUIRED", details={"login": me, "required_role": ["lead", "deputy"]})
    pol = approval_policy(snap)
    owners = roles.section_owner()
    editor_view = pol.latex and pol.editor is not None and same_login(me, pol.editor) and not is_staff(snap, me)
    tally = SanitizeTally()
    rows: list[dict[str, Any]] = []
    for task in board.tasks:
        pr = task.pr
        if pr is None or pr.state != "open":
            continue
        touches = _touches(ctx, loaded, pr, task, editor_view) if editor_view else bool(task.sections)
        role = review_role(me, pr.author, touches, task.sections, owners, pol)
        requested = any(same_login(me, x) for x in pr.requested_reviewers)
        if not all_v and (pr.draft or (role == "none" and not requested)):
            continue
        rows.append({
            "pr": pr.number,
            "url": pr.url,
            "task": task.number,
            "title": marked("untrusted", f"pr#{pr.number}.title", pr.title or task.title, limit=TITLE_MAX,
                            tally=tally),
            "author": pr.author,
            "sections": list(task.sections),
            "ci": pr.ci,
            "requested_from_me": requested,
            "role_in_review": role,
            "age_h": _age_h(pr, task, now),
            "flags": sorted(set(task.flags) | {x for x in pr.labels if x in FLAG_LABELS}),
        })
    # primero lo que espera al actor; luego lo más antiguo
    rows.sort(key=lambda r: (not (r["requested_from_me"] or r["role_in_review"] == "approver"), -r["age_h"], r["pr"]))
    ids = [r["pr"] for r in rows]
    snap_id = snap_of(ids)
    offset = decode_cursor(cursor_v, op=OP, list_name="reviews", snap=snap_id) if cursor_v is not None else 0
    shown = rows[offset:]
    sanitized = tally.details()
    if sanitized is not None:
        warnings.append(make_warning("W_VALIDATION_UNTRUSTED_SANITIZED", t, details=sanitized))
    data: dict[str, object] = {
        "reviews": shown,
        "page": {"next_cursor": encode_cursor(OP, "reviews", _PLACEHOLDER_OFFSET, snap_id), "returned": len(shown),
                 "total": len(rows)},
    }
    budget = max_v or BUDGET
    placeholder = make_warning("W_OUTPUT_TRUNCATED", t, details={"budget": budget, "trimmed": ["reviews"],
                                                                 "omitted": {"reviews": len(shown)}})
    trimmed = fit(OP, data, budget=budget, trim_order=["reviews"], warnings=warnings, truncated_warning=placeholder)
    kept = data["reviews"]
    assert isinstance(kept, list)
    page = data["page"]
    assert isinstance(page, dict)
    page["returned"] = len(kept)
    if trimmed.omitted.get("reviews"):
        page["next_cursor"] = encode_cursor(OP, "reviews", offset + len(kept), snap_id)
        warnings.append(make_warning("W_OUTPUT_TRUNCATED", t, details=trimmed.warning_details(budget)))
    else:
        page["next_cursor"] = None
    return ok(OP, data, warnings)


def render_text(result: Result, t: Translator, *, fmt: str = "text") -> str:
    """Una línea por PR: número, rol, CI, antigüedad y título."""
    del fmt
    data = result.data if isinstance(result.data, dict) else {}
    if "reviews" not in data:
        return ""
    lines = []
    for item in data["reviews"]:
        title = item.get("title") or {}
        lines.append(t("review.list.line", pr=item.get("pr"), role=t(f"review.role.{item.get('role_in_review')}"),
                       ci=item.get("ci"), age=item.get("age_h"), author=item.get("author"),
                       title=" ".join(str(title.get("text", "")).split())))
    if not lines:
        lines.append(t("review.list.none"))
    page = data.get("page") or {}
    if page.get("next_cursor"):
        lines.append(t("review.list.more", cursor=page["next_cursor"]))
    return "\n".join(lines)
