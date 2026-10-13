# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove status`` · ``get_status`` (05 §9.6) y la pantalla de inicio (REQ-OPS-68).

``status`` siempre lee la forja (con ETag, 08 §7.4) a través del tablero de
``ops.board`` y copia la evidencia de los PR propios (REQ-OPS-66). Sin red responde con
``forge-snapshot.json`` (``source: "cache"``, ``cache_age_s`` y ``W_NET_OFFLINE``); sin
caché, ``E_NET_OFFLINE``.

Salida (``data_status``): ``me``, ``base``, ``tasks[]`` (``task_summary``) y ``page``. Las
tareas van por número (REQ-OPS-35); por defecto no se listan las integradas ni las
canceladas cerradas hace más de 7 días. ``--mine`` deja las que reclama la persona y las
que propuso y nadie reclama. Los títulos son ``marked_text`` ``untrusted`` de 120
caracteres (REQ-OPS-37, REQ-SEC-7); de los demás Issues y PR solo van campos
estructurados (REQ-OPS-39). El tope es de 12 000 caracteres (§7.1) y lo que no cabe sigue
en ``page.next_cursor`` (REQ-OPS-36).

Avisos: ``W_NET_OFFLINE``, ``W_OUTPUT_TRUNCATED``, ``W_VALIDATION_UNTRUSTED_SANITIZED``,
``W_CONFIG_POLICY_FALLBACK`` y un ``W_STATE_ANOMALY`` por cada tarea propia con anomalías
(REQ-TASK-14; las de todas las tareas van en ``task_summary.anomalies``).

La pantalla de inicio (``aucove`` sin argumentos) usa los mismos datos: ``home_extras``
agrega lo local (partes y dueños de ``roles.yaml`` de la base y las carpetas de
``.aucove/evidence/``) y ``render_home`` la dibuja solo con ASCII y sin color.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .. import _meta
from ..domain.marked import TITLE_MAX, SanitizeTally, marked
from ..domain.text import same_login
from ..i18n import Translator
from .board import Board, TaskView, copy_evidence, expiring_warnings, load_board, visible
from .fit import decode_cursor, encode_cursor, fit, snap_of
from .result import OpError, Result, WarningObj, make_warning, ok

if TYPE_CHECKING:
    from ..config.roles import Roles
    from .context import OpContext

__all__ = [
    "BUDGET",
    "OP",
    "HomeExtras",
    "home_extras",
    "me_block",
    "render_home",
    "render_text",
    "run",
    "task_summary",
]

OP = "status"
BUDGET = 12000  # 05 §7.1
STATE_CHOICES = ("proposed", "open", "claimed", "in_review", "changes_requested", "merged", "expired", "cancelled")
_SECTION_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_PLACEHOLDER_OFFSET = 10**6  # el cursor real nunca es más largo que el de este offset
HOME_MAX_TASKS = 10  # tareas por bloque en la pantalla de inicio
HOME_MAX_EVIDENCE = 10


def _bad(param: str, reason: str = "type") -> OpError:
    return OpError("E_VALIDATION_INPUT", details={"errors": [{"param": param, "reason": reason}]})


def _check_input(
    mine: object, task: object, states: object, section: object, cursor: object, max_chars: object
) -> tuple[bool, int | None, tuple[str, ...], str | None, str | None, int | None]:
    if not isinstance(mine, bool):
        raise _bad("mine")
    if task is not None and (isinstance(task, bool) or not isinstance(task, int) or task < 1):
        raise _bad("task")
    picked: tuple[str, ...] = ()
    if states is not None:
        if not isinstance(states, (list, tuple)) or not all(isinstance(s, str) for s in states):
            raise _bad("states")
        if any(s not in STATE_CHOICES for s in states):
            raise _bad("states", "enum")
        picked = tuple(dict.fromkeys(states))
    if section is not None and (not isinstance(section, str) or not _SECTION_ID.fullmatch(section)):
        raise _bad("section", "pattern")
    if cursor is not None and not isinstance(cursor, str):
        raise _bad("cursor")
    if max_chars is not None and (
        isinstance(max_chars, bool) or not isinstance(max_chars, int) or not 2000 <= max_chars <= 40000
    ):
        raise _bad("max_chars", "range")
    return mine, task, picked, section, cursor, max_chars


def me_block(login: str | None, roles: Roles) -> dict[str, object]:
    """``me`` del sobre: login, rol, suplencia y partes de la persona (``roles.yaml`` de la base)."""
    person = roles.person(login)
    return {
        "login": login,
        "role": person.role if person is not None else None,
        "is_deputy": same_login(login, roles.deputy),
        "sections": sorted(s.id for s in roles.sections if same_login(s.owner, login)),
    }


def task_summary(task: TaskView, *, expiry_days: int, tally: SanitizeTally | None = None) -> dict[str, object]:
    """``task_summary`` de una tarea del tablero (título saneado y recortado a 120)."""
    pr: dict[str, object] | None = None
    if task.pr is not None:
        pr = {
            "number": task.pr.number,
            "url": task.pr.url,
            "draft": task.pr.draft,
            "ci": task.pr.ci,
            "review": task.pr.review,
        }
    return {
        "number": task.number,
        "title": marked("untrusted", f"issue#{task.number}.title", task.title, limit=TITLE_MAX, tally=tally),
        "state": task.state,
        "sections": list(task.sections),
        "plan_refs": list(task.plan_refs),
        "claimed_by": task.owner,
        "branch": task.branch,
        "pr": pr,
        "flags": list(task.flags),
        "anomalies": list(task.anomalies),
        "updated_at": task.updated_at,
        "expires_at": task.expires_at(expiry_days),
    }


def _selected(
    board: Board, now: datetime, *, login: str | None, mine: bool, task: int | None,
    states: Sequence[str], section: str | None,
) -> list[TaskView]:
    out = []
    terminal_asked = task is not None or any(s in ("merged", "cancelled") for s in states)
    for row in board.tasks:
        if not terminal_asked and not visible(row, now):
            continue
        if task is not None and row.number != task:
            continue
        if states and row.state not in states:
            continue
        if section is not None and section not in row.sections:
            continue
        if mine and not (
            same_login(row.owner, login) or (row.owner is None and same_login(row.author, login))
        ):
            continue
        out.append(row)
    return sorted(out, key=lambda r: r.number)


def _expiry_days(cfg: Any) -> int:
    value = cfg.limits.get("claim_expiry_days", 14)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 14


def run(
    ctx: OpContext,
    *,
    mine: object = False,
    task: object = None,
    states: object = None,
    section: object = None,
    cursor: object = None,
    max_chars: object = None,
) -> Result:
    """``status [--mine] [--task <n>] [--state <estado>]... [--section <id>] [--cursor] [--max-chars]``."""
    mine_v, task_v, states_v, section_v, cursor_v, max_v = _check_input(mine, task, states, section, cursor, max_chars)
    t = ctx.t()
    ctx.project()
    snap = ctx.base()
    cfg, roles = snap.project, snap.roles
    store = ctx.store()
    now = ctx.inv.now()
    warnings: list[WarningObj] = []
    if snap.fallback_sha is not None:  # REQ-PERM-4
        warnings.append(make_warning("W_CONFIG_POLICY_FALLBACK", t, details=snap.fallback_details()))
    loaded = load_board(ctx, cfg=cfg, roles=roles, store=store, now=now, mode="live")
    if loaded is None:
        raise OpError("E_NET_OFFLINE")
    board = loaded.board
    if loaded.source == "cache":
        warnings.append(make_warning("W_NET_OFFLINE", t, details=loaded.offline_details(now)))
    if board.profile == "advisory":  # 12-mvp A5: el aviso en todas las salidas de status
        warnings.append(make_warning("W_FORGE_PROFILE_ADVISORY", t))
    if loaded.fetched is not None:
        copy_evidence(ctx, ctx.forge(), loaded.fetched, now=now)

    login = board.login
    rows = _selected(board, now, login=login, mine=mine_v, task=task_v, states=states_v, section=section_v)
    ids = [r.number for r in rows]
    snap_id = snap_of(ids)
    offset = decode_cursor(cursor_v, op=OP, list_name="tasks", snap=snap_id) if cursor_v is not None else 0
    page_rows = rows[offset:]
    tally = SanitizeTally()
    expiry = _expiry_days(cfg)
    warnings.extend(expiring_warnings(t, board.tasks, login, now, expiry))  # los reclamos propios
    tasks = [task_summary(r, expiry_days=expiry, tally=tally) for r in page_rows]
    for row in page_rows:
        if row.anomalies and same_login(row.owner, login):
            warnings.append(
                make_warning("W_STATE_ANOMALY", t, details={"task": row.number, "anomalies": list(row.anomalies)})
            )
    sanitized = tally.details()
    if sanitized is not None:
        warnings.append(make_warning("W_VALIDATION_UNTRUSTED_SANITIZED", t, details=sanitized))
    data: dict[str, object] = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": loaded.source,
        "cache_age_s": board.age_s(now) if loaded.source == "cache" else None,
        "me": me_block(login, roles),
        "base": dict(board.base.__dict__),
        "tasks": tasks,
        "page": {
            "next_cursor": encode_cursor(OP, "tasks", _PLACEHOLDER_OFFSET, snap_id),
            "returned": len(tasks),
            "total": len(rows),
        },
    }
    budget = max_v or BUDGET
    placeholder = make_warning("W_OUTPUT_TRUNCATED", t, details={"budget": budget, "trimmed": ["tasks"],
                                                                 "omitted": {"tasks": len(tasks)}})
    result = fit(OP, data, budget=budget, trim_order=["tasks"], warnings=warnings, truncated_warning=placeholder)
    kept = data["tasks"]
    assert isinstance(kept, list)
    page = data["page"]
    assert isinstance(page, dict)
    page["returned"] = len(kept)
    if result.omitted.get("tasks"):
        page["next_cursor"] = encode_cursor(OP, "tasks", offset + len(kept), snap_id)
        warnings.append(make_warning("W_OUTPUT_TRUNCATED", t, details=result.warning_details(budget)))
    else:
        page["next_cursor"] = None
    return ok(OP, data, warnings)


# -- texto ----------------------------------------------------------------------------------


def _line(t: Translator, item: Mapping[str, Any]) -> str:
    title = item.get("title") or {}
    text = _one_line(title.get("text", "")) if isinstance(title, Mapping) else ""
    pr = item.get("pr")
    pr_text = (
        t("status.render.pr", number=pr.get("number"), ci=pr.get("ci"), review=pr.get("review"))
        if isinstance(pr, Mapping)
        else ""
    )
    owner = item.get("claimed_by")
    return t(
        "status.render.task",
        number=item.get("number"),
        state=t(f"status.state.{item.get('state')}"),
        owner=f"@{owner}" if owner else "-",
        sections=", ".join(item.get("sections") or []) or "-",
        title=text,
        pr=pr_text,
    ).rstrip()


def render_text(result: Result, t: Translator, *, fmt: str = "text") -> str:
    """Vista legible de ``data_status``: base, identidad y una línea por tarea."""
    del fmt
    data = result.data or {}
    if "tasks" not in data:
        return ""
    me = data.get("me") or {}
    base = data.get("base") or {}
    lines = [
        t("status.render.me", login=f"@{me.get('login')}" if me.get("login") else "-", role=me.get("role") or "-"),
        t("status.render.base", branch=base.get("branch"), ci=base.get("ci"),
          sha=(base.get("sha") or "-")[:12]),
    ]
    if base.get("main_broken_issue"):
        lines.append(t("status.render.main_broken", number=base.get("main_broken_issue")))
    if data.get("source") == "cache":
        lines.append(t("status.render.cache", minutes=int(data.get("cache_age_s") or 0) // 60))
    tasks = data.get("tasks") or []
    if not tasks:
        lines.append(t("status.render.none"))
    for item in tasks:
        lines.append(_line(t, item))
        if item.get("anomalies"):
            lines.append("  " + t("status.render.anomalies", anomalies=", ".join(item["anomalies"])))
    page = data.get("page") or {}
    if page.get("next_cursor"):
        lines.append(t("status.render.more", cursor=page["next_cursor"]))
    return "\n".join(lines)


# -- pantalla de inicio (REQ-OPS-68) -----------------------------------------------------------


@dataclass(frozen=True)
class HomeExtras:
    """Lo local de la pantalla de inicio: partes, nombre del proyecto y evidencia copiada."""

    project: str | None
    parts: tuple[tuple[str, str, str], ...]  # (id, título, dueño)
    evidence: tuple[str, ...]  # carpetas <kind>-<commit>-a<n>, de la más reciente a la más vieja
    lang: str


def home_extras(ctx: OpContext) -> HomeExtras | None:
    """Datos locales para la pantalla de inicio; ``None`` fuera de un proyecto."""
    project = ctx.project(required=False)
    if project is None:
        return None
    snap = ctx.base()
    parts = tuple((s.id, s.title, s.owner) for s in snap.roles.sections)
    evidence: list[tuple[float, str]] = []
    try:
        folder = ctx.store().root / "evidence"
        with os.scandir(folder) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                    evidence.append((entry.stat(follow_symlinks=False).st_mtime, entry.name))
    except OSError:
        pass
    newest = tuple(name for _m, name in sorted(evidence, reverse=True))
    name = getattr(snap.project, "title", None) or getattr(snap.project, "name", None)
    return HomeExtras(project=name, parts=parts, evidence=newest, lang=snap.project.language)


_ASCII_MAP = {"ß": "ss", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ø": "o", "Ø": "O", "ł": "l", "Ł": "L",
              "đ": "d", "Đ": "D", "¿": "", "¡": "", "«": '"', "»": '"', "·": "-", "–": "-", "—": "-"}


def to_ascii(text: str) -> str:
    """Texto solo ASCII para la pantalla de inicio: sin acentos y, lo que no tiene
    equivalente, ``?``. Conserva los saltos de línea."""
    out = []
    for ch in unicodedata.normalize("NFKD", text):
        if unicodedata.category(ch) == "Mn":
            continue
        ch = _ASCII_MAP.get(ch, ch)
        out.append(ch if ch == "\n" or all(32 <= ord(c) < 127 for c in ch) else "?")
    return "".join(out)


def _one_line(text: str) -> str:
    """Un título en una sola línea: un salto no debe crear una línea falsa en la salida."""
    return " ".join(text.split())


def render_home(result: Result | None, extras: HomeExtras | None, t: Translator) -> str:
    """Pantalla de inicio: encabezado y, dentro de un proyecto, el tablero y el siguiente paso."""
    lines = [t("home.header", name=_meta.NAME, version=_meta.__version__), ""]
    if extras is None:
        lines.extend([t("home.outside"), t("home.outside_start", name=_meta.NAME)])
        return to_ascii("\n".join(lines))
    if extras.project:
        lines.append(t("home.project", project=extras.project))
    data = result.data if result is not None and result.ok and result.data else None
    if data is None:
        code = result.error.code if result is not None and result.error is not None else "-"
        lines.append(t("home.no_board", code=code, name=_meta.NAME))
    else:
        me = data.get("me") or {}
        lines.append(t("home.me", login=f"@{me.get('login')}" if me.get("login") else "-",
                       role=me.get("role") or "-"))
        if data.get("source") == "cache":
            lines.append(t("home.cache", minutes=int(data.get("cache_age_s") or 0) // 60))
        base = data.get("base") or {}
        if base.get("main_broken_issue"):
            lines.append(t("home.main_broken", number=base["main_broken_issue"]))
        tasks = [x for x in data.get("tasks") or [] if isinstance(x, Mapping)]
        login = me.get("login")
        open_tasks = [x for x in tasks if x.get("state") == "open"]
        active = [x for x in tasks if x.get("state") in ("claimed", "in_review", "changes_requested", "expired")]
        lines.append("")
        lines.append(t("home.open_title", n=len(open_tasks)))
        for item in open_tasks[:HOME_MAX_TASKS]:
            lines.append("  " + _home_task(t, item, owner=False))
        lines.append(t("home.active_title", n=len(active)))
        for item in active[:HOME_MAX_TASKS]:
            mark = "*" if same_login(item.get("claimed_by"), login) else " "
            lines.append(f" {mark}" + _home_task(t, item, owner=True))
        if extras.parts:
            lines.append(t("home.parts_title"))
            for sid, title, owner in extras.parts:
                lines.append("  " + t("home.part", id=sid, title=title, owner=owner))
        lines.append(t("home.evidence_title", n=len(extras.evidence), dir=_meta.CACHE_DIR))
        for folder in extras.evidence[:HOME_MAX_EVIDENCE]:
            lines.append("  " + folder)
    skill = "empezar" if extras.lang == "es" else "start"
    lines.extend(["", t("home.next", skill=skill, name=_meta.NAME)])
    return to_ascii("\n".join(lines))


def _home_task(t: Translator, item: Mapping[str, Any], *, owner: bool) -> str:
    title = item.get("title") or {}
    text = _one_line(title.get("text", "")) if isinstance(title, Mapping) else ""
    who = item.get("claimed_by")
    return t(
        "home.task_owner" if owner else "home.task",
        number=item.get("number"),
        state=t(f"status.state.{item.get('state')}"),
        owner=f"@{who}" if who else "-",
        sections=", ".join(item.get("sections") or []) or "-",
        title=text,
    )
