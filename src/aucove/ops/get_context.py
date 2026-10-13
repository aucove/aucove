# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove context`` · ``get_context`` (05 §9.7, REQ-OPS-43 y REQ-OPS-67).

Nunca falla por la red ni por la forja (REQ-OPS-43): usa el tablero de ``ops.board``
en vivo, la caché de ``forge-snapshot.json`` (sin ``--refresh`` no espera a la forja si
tiene menos de 10 min) o, sin caché, solo lo local (``source: "local"``: ``roles.yaml``
de la base local y la rama actual). En los dos últimos casos agrega ``W_NET_OFFLINE``.
``build(..., offline=True)`` es el parámetro interno de SessionStart: no toca la red.

``summary_md`` (REQ-OPS-67) es el mismo texto de ``.aucove/context.md``: Markdown en
el idioma del proyecto, medido en unidades UTF-16 y con tope ``context.max_chars``
(REQ-ARCH-50). Los bloques fijos (versión y antigüedad de los datos, identidad, rama,
tarea y rutas, avisos) nunca se recortan; los demás se quitan enteros en este orden:
notas de sesión, decisiones, tareas de otros, guía del artículo, plan, partes y tareas
propias (07 §4.1, ampliado con los bloques de REQ-OPS-67). Si se quitó alguno, la última
línea lo dice y remite a ``status``. Todo texto ``reviewed`` o ``untrusted`` va entre los
delimitadores de 10-seguridad REQ-SEC-5, tras el aviso fijo de datos (REQ-OPS-40).

``summary_md`` no cuenta para el tope del sobre (§7.1): tiene el suyo. El resto de
``data`` se recorta en el orden de §7.1 (``others_active``, ``decisions_recent``,
``my_tasks``).

Con ``--task <n> --phase <fase>`` agrega ``task_context``: el paquete de 04 §8.2, el mismo
de ``claim`` y ``submit`` (``ops.coordinator``). Necesita la forja para leer la tarea; sin
ella se omite con ``W_NET_OFFLINE``, porque ``context`` no falla por la red. Se recorta en el
orden de REQ-TASK-59 antes que las listas de §7.1.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from .. import _meta
from ..domain.marked import TITLE_MAX, SanitizeTally, data_block, marked, nonce_clashes, sanitize
from ..domain.permissions import WRITABLE
from ..domain.tasks import ACTIVE
from ..domain.text import same_login
from ..i18n import LANGS, Translator, translator
from .base_docs import BaseDocs, guide_excerpt, read_base_docs
from .board import Board, TaskView, expiring_warnings, load_board, owner_and_sections
from .coordinator import Target, build_package, common_tasks, trim_package
from .fit import envelope_size, fit
from .result import OpError, Result, WarningObj, make_warning, ok
from .setup import setup_pending
from .status import me_block

if TYPE_CHECKING:
    from .context import OpContext

__all__ = ["OP", "build", "render_text", "run", "summary_units"]

OP = "context"
DEFAULT_MAX_CHARS = 8000
TRIM_ORDER = ("others_active", "decisions_recent", "my_tasks")  # 05 §7.1
MAX_ACTIVE = 5  # tareas propias y de otros en el resumen (07 §4.1)
MAX_QUESTIONS = 10
OBJECTIVE_MAX = 600
QUESTION_MAX = 240
GUIDE_MAX = 2000
SUMMARY_MAX = 280  # REQ-SEC-8
CONTEXT_FILE = "context.md"
MAX_NONCE_TRIES = 64


def summary_units(text: str) -> int:
    """Unidades UTF-16 de ``text`` (REQ-ARCH-50, REQ-HOOK-18)."""
    return len(text.encode("utf-16-le")) // 2


def _int_setting(mapping: Mapping[str, object], key: str, default: int, low: int, high: int) -> int:
    value = mapping.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return min(max(value, low), high)


# -- datos ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Current:
    branch: str | None
    task: int | None
    state: str | None
    claimant: str | None
    sections: tuple[str, ...]
    title: str | None


def _current(ctx: OpContext, board: Board | None) -> _Current:
    from .check_path import current_task

    git = ctx.git()
    store = ctx.store()
    try:
        branch = git.current_branch()
    except Exception:  # noqa: BLE001 - HEAD ilegible: sin rama
        branch = None
    number = current_task(ctx, git, store) if branch is not None else None
    if number is None:
        return _Current(branch, None, None, None, (), None)
    row = board.task(number) if board is not None else None
    if row is not None:
        return _Current(branch, number, row.state, row.owner, row.sections, row.title)
    cached = store.task(number)
    if cached is not None:
        return _Current(branch, number, cached.state, cached.claimant, cached.sections, None)
    return _Current(branch, number, None, None, (), None)


def _allowed_paths(current: _Current, login: str | None, sections_owner: Mapping[str, str]) -> list[str]:
    """Patrones de 06-permisos §2.4 que la tarea actual le abre a la persona (modo session).

    Las partes efectivas de la tarea que son suyas y los archivos nuevos de
    ``memory/decisions/``. Las notas (``memory/notes/``) las escribe ``submit``, nunca la
    sesión; las rutas de ``lead`` de quien es responsable o suplente las cuenta el resumen.
    """
    if current.task is None or current.state not in WRITABLE or not same_login(current.claimant, login):
        return []
    mine = [s for s in current.sections if same_login(sections_owner.get(s), login)]
    return [*(f"sections/{s}/**" for s in mine), "memory/decisions/*.md"]


def _alerts(
    t: Translator,
    board: Board | None,
    login: str | None,
    *,
    now: datetime,
    cfg: Any,
    roles: Any,
    expiry_days: int,
    warning_days: int,
    pending: bool = False,
) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []

    def add(kind: str, number: int | None, key: str, **fmt: object) -> None:
        message = t(key, **fmt)
        out.append({"kind": kind, "task": number, "message": message[:300]})

    if board is not None:
        if board.base.main_broken_issue is not None:
            add("main_broken", None, "context.alert.main_broken", issue=board.base.main_broken_issue)
        for row in board.tasks:
            mine = same_login(row.owner, login)
            if mine and row.state == "expired":
                add("claim_expired", row.number, "context.alert.claim_expired", task=row.number, name=_meta.NAME)
            elif mine and row.state in ("claimed", "changes_requested"):
                expires = row.expires_at(expiry_days)
                when = datetime.fromisoformat(expires[:-1] + "+00:00") if expires else None
                if when is not None and when - now <= timedelta(days=warning_days):
                    add("claim_expiring", row.number, "context.alert.claim_expiring", task=row.number,
                        date=expires[:10] if expires else "")
            if mine and row.state == "changes_requested" and row.pr is not None:
                add("changes_requested", row.number, "context.alert.changes_requested", task=row.number,
                    pr=row.pr.number)
            if (
                row.pr is not None
                and row.pr.state == "open"
                and any(same_login(r, login) for r in row.pr.requested_reviewers)
            ):
                add("review_requested", row.number, "context.alert.review_requested", task=row.number,
                    pr=row.pr.number)
        if board.profile == "advisory":
            add("profile_advisory", None, "context.alert.profile_advisory")
    if roles.deputy is None and getattr(cfg, "mode", "group") == "group":
        add("deputy_missing", None, "context.alert.deputy_missing")
    if pending:  # REQ-OPS-69 (00-marco D-70): sin red también, porque solo lee la base local
        add("setup_pending", None, "context.alert.setup_pending", lead=f"@{roles.lead}",
            skill="empezar" if t.lang == "es" else "start")
    return out


# -- resumen en Markdown (REQ-OPS-67) --------------------------------------------------------


@dataclass
class _Block:
    """Bloque del resumen. ``rank`` 0 = nunca se recorta; mayor = se recorta antes."""

    rank: int
    lines: list[str]
    has_data: bool = False
    items: int = 1  # elementos que representa, para la línea de omitidos


class _Data:
    """Bloques de datos de REQ-SEC-5 con un nonce que no aparece en ningún contenido."""

    def __init__(self, nonce_factory: Callable[[], str]) -> None:
        self._factory = nonce_factory
        self.contents: list[tuple[str, str, str]] = []  # (trust, source, texto saneado)

    def add(self, trust: str, source: str, text: str) -> int:
        self.contents.append((trust, source, sanitize(text).text))
        return len(self.contents) - 1

    def render(self) -> Callable[[int], str]:
        """Renderiza los bloques con un nonce que no aparece en ningún contenido (REQ-SEC-5)."""
        for _attempt in range(MAX_NONCE_TRIES):
            nonce = self._factory()
            if not nonce_clashes(nonce, (c for _t, _s, c in self.contents)):
                break
        else:  # con secrets.token_hex(4) es imposible en la práctica
            raise RuntimeError("no se encontró un nonce libre")
        rendered = [data_block(c, trust=tr, source=s, nonce=nonce) for tr, s, c in self.contents]  # type: ignore[arg-type]
        return lambda i: rendered[i]


def _age_line(t: Translator, source: str, age_s: int | None, reason: str | None) -> str:
    if source == "local":
        return t("context.md.age_local")
    minutes = (age_s or 0) // 60
    if reason not in (None, "offline"):
        return t("context.md.age_offline", minutes=minutes)
    return t("context.md.age", minutes=minutes)


def _task_text(row: TaskView, t: Translator) -> str:
    owner = f"@{row.owner}" if row.owner else "-"
    sections = ", ".join(row.sections) or "-"
    return t("context.md.task_item", number=row.number, title=row.title, state=t(f"status.state.{row.state}"),
             owner=owner, sections=sections)


def _summary(
    t: Translator,
    *,
    project_name: str,
    convention: str | None,
    source: str,
    age_s: int | None,
    reason: str | None,
    me: Mapping[str, Any],
    roles: Any,
    current: _Current,
    allowed: Sequence[str],
    alerts: Sequence[Mapping[str, object]],
    mine: Sequence[TaskView],
    others: Sequence[TaskView],
    docs: BaseDocs,
    latex: bool,
    max_chars: int,
    nonce_factory: Callable[[], str],
) -> str:
    data = _Data(nonce_factory)
    blocks: list[_Block] = []
    login = me.get("login")
    staff = bool(me.get("is_deputy")) or same_login(login, roles.lead)

    head = [
        t("context.md.title", project=project_name),
        "",
        t("context.md.versions", name=_meta.NAME, version=_meta.__version__, convention=convention or "-"),
        _age_line(t, source, age_s, reason),
        t("context.md.identity", login=f"@{login}" if login else "-", role=me.get("role") or "-",
          lead=f"@{roles.lead}", deputy=f"@{roles.deputy}" if roles.deputy else "-"),
    ]
    blocks.append(_Block(0, head))

    cur: list[str] = []
    if current.task is not None:
        cur.append(t("context.md.current_task", branch=current.branch or "-", task=current.task,
                     state=t(f"status.state.{current.state}") if current.state else "-",
                     owner=f"@{current.claimant}" if current.claimant else "-",
                     sections=", ".join(current.sections) or "-"))
        if allowed:
            shown = [p for p in allowed if not p.startswith("memory/decisions/")]
            cur.append(t("context.md.allowed", paths=", ".join(shown) or "-"))
        else:
            cur.append(t("context.md.allowed_none"))
    else:
        cur.append(t("context.md.no_task", branch=current.branch or "-",
                     skill="empezar" if t.lang == "es" else "start", name=_meta.NAME))
    if staff:
        cur.append(t("context.md.staff_paths"))
    blocks.append(_Block(0, cur))

    alert_lines = [t("context.md.alerts_title")]
    if alerts:
        alert_lines.extend(f"- {a['message']}" for a in alerts)
    else:
        alert_lines.append(t("context.md.alerts_none"))
    blocks.append(_Block(0, alert_lines))

    if current.task is not None and current.title is not None:
        i = data.add("untrusted", f"issue#{current.task}.title", f"#{current.task} {current.title}")
        blocks.append(_Block(0, [t("context.md.current_title"), f"\x00{i}"], has_data=True))

    if mine:
        lines = [t("context.md.mine_title")]
        for row in mine[:MAX_ACTIVE]:
            lines.append(f"\x00{data.add('untrusted', f'issue#{row.number}.title', _task_text(row, t))}")
        if len(mine) > MAX_ACTIVE:
            lines.append(t("context.md.more", n=len(mine) - MAX_ACTIVE))
        blocks.append(_Block(1, lines, has_data=True, items=len(mine)))

    if docs.questions or docs.objective:
        lines = [t("context.md.plan_title")]
        if docs.objective:
            lines.append(t("context.md.objective", text=sanitize(docs.objective).text[:OBJECTIVE_MAX]))
        for q in docs.questions[:MAX_QUESTIONS]:
            lines.append(f"- {q.id}: {sanitize(q.text).text[:QUESTION_MAX]}")
        if len(docs.questions) > MAX_QUESTIONS:
            lines.append(t("context.md.more", n=len(docs.questions) - MAX_QUESTIONS))
        blocks.append(_Block(3, lines))

    if roles.sections:
        lines = [t("context.md.parts_title")]
        for s in roles.sections:
            outline = docs.outline_of.get(s.id) if latex else None
            key = "context.md.part_latex" if outline else "context.md.part"
            lines.append(t(key, id=s.id, title=sanitize(s.title).text, owner=f"@{s.owner}", outline=outline or ""))
        blocks.append(_Block(2, lines))

    if latex and docs.guide is not None:
        excerpt = guide_excerpt(docs.guide, me.get("sections") or [])
        lines = [t("context.md.guide_title", path="tex/guide.md")]
        if excerpt:
            i = data.add("reviewed", "tex/guide.md", excerpt[:GUIDE_MAX])
            lines.append(f"\x00{i}")
            blocks.append(_Block(4, lines, has_data=True))
        else:
            lines.append(t("context.md.guide_none", path="tex/guide.md"))
            blocks.append(_Block(4, lines))

    if others:
        lines = [t("context.md.others_title")]
        for row in others[:MAX_ACTIVE]:
            lines.append(f"\x00{data.add('untrusted', f'issue#{row.number}.title', _task_text(row, t))}")
        if len(others) > MAX_ACTIVE:
            lines.append(t("context.md.more", n=len(others) - MAX_ACTIVE))
        blocks.append(_Block(5, lines, has_data=True, items=len(others)))

    if docs.decisions:
        lines = [t("context.md.decisions_title")]
        for d in docs.decisions:
            lines.append(f"- {d.date} {d.path} ({d.status})")
            body = f"{d.title[:TITLE_MAX]}\n{d.summary[:SUMMARY_MAX]}"
            lines.append(f"\x00{data.add('reviewed', d.path, body)}")
        blocks.append(_Block(6, lines, has_data=True, items=len(docs.decisions)))

    if docs.notes:
        lines = [t("context.md.notes_title")]
        lines.extend(f"- {p}" for p in docs.notes)
        blocks.append(_Block(7, lines, items=len(docs.notes)))

    rendered = data.render()

    def assemble(kept: Sequence[_Block], omitted: int) -> str:
        out: list[str] = []
        notice_done = False
        for block in kept:
            if block.has_data and not notice_done:
                out.extend(["", t("context.md.data_notice")])
                notice_done = True
            out.append("")
            for line in block.lines:
                out.append(rendered(int(line[1:])) if line.startswith("\x00") else line)
        if omitted:
            out.extend(["", t("context.md.omitted", n=omitted, name=_meta.NAME)])
        return "\n".join(out).strip("\n") + "\n"

    kept = list(blocks)
    omitted = 0
    text = assemble(kept, omitted)
    for rank in sorted({b.rank for b in blocks if b.rank > 0}, reverse=True):
        if summary_units(text) <= max_chars:
            break
        dropped = [b for b in kept if b.rank == rank]
        kept = [b for b in kept if b.rank != rank]
        omitted += sum(b.items for b in dropped)
        text = assemble(kept, omitted)
    if summary_units(text) > max_chars:  # solo con bloques fijos enormes: se corta al tope
        cut = text.encode("utf-16-le")[: max_chars * 2].decode("utf-16-le", "ignore")
        text = cut.rstrip("\n") + "\n" if cut else ""
        while summary_units(text) > max_chars:
            text = text[:-2] + "\n"
    return text


def _write_context_md(ctx: OpContext, text: str) -> None:
    """``.aucove/context.md`` (clase ``local``), atómico; un fallo no cambia el resultado."""
    from ..localstore.store import atomic_write, ensure_root

    try:
        root = ctx.store().root
        if ensure_root(root):
            atomic_write(root / CONTEXT_FILE, text.encode("utf-8"))
    except Exception:  # noqa: BLE001 - la caché y el resumen son descartables (REQ-ARCH-57)
        ctx.emit(kind="cache", name="context-md", result="error")


# -- operación -----------------------------------------------------------------------------


def _bad(param: str, reason: str = "type") -> OpError:
    return OpError("E_VALIDATION_INPUT", details={"errors": [{"param": param, "reason": reason}]})


def run(ctx: OpContext, *, refresh: object = False, task: object = None, phase: object = None) -> Result:
    """``context [--refresh] [--task <n> --phase claim|submission]``."""
    if not isinstance(refresh, bool):
        raise _bad("refresh")
    if task is not None and (isinstance(task, bool) or not isinstance(task, int) or task < 1):
        raise _bad("task")
    if phase is not None and phase not in ("claim", "submission"):
        raise _bad("phase", "enum")
    if (task is None) != (phase is None):  # 05 §9.7: van juntos
        raise _bad("phase" if phase is None else "task", "required")
    return build(ctx, refresh=refresh, offline=False, task=task, phase=phase)  # type: ignore[arg-type]


def _task_context(
    ctx: OpContext, number: int, phase: str, board: Board | None, snap: Any, tally: SanitizeTally
) -> tuple[dict[str, Any], Target] | None:
    """Paquete de 04 §8.2 de la tarea ``number``; ``None`` si la forja no responde."""
    from ..domain.tasks import derive_state
    from ..forge.base import ForgeError
    from .snapshot import authz_from_roles, collect_task

    try:
        forge = ctx.forge()
        evidence = collect_task(forge, number, authz_from_roles(snap.roles), base_branch=snap.project.base_branch)
    except (ForgeError, OpError) as exc:
        if isinstance(exc, OpError) and exc.code == "E_STATE_NOT_A_TASK":
            raise
        return None
    if evidence is None:
        raise OpError("E_STATE_NOT_A_TASK", details={"task": number})
    derived = derive_state(evidence.snapshot)
    owner, sections = owner_and_sections(evidence.snapshot, derived)
    spec = evidence.body.spec or {}
    target = Target(
        number=number, state=derived.state, owner=owner, sections=sections,
        plan_refs=tuple(spec.get("plan_refs", ())),  # type: ignore[arg-type]
        title=evidence.issue.title, summary=str(spec.get("summary", "")),
        done=tuple(spec.get("done", ())),  # type: ignore[arg-type]
    )
    return build_package(ctx, forge, phase=phase, target=target, board=board, snap=snap, tally=tally), target


def build(
    ctx: OpContext,
    *,
    refresh: bool,
    offline: bool,
    nonce_factory: Callable[[], str] | None = None,
    task: int | None = None,
    phase: str | None = None,
) -> Result:
    """Paquete de ``context``; ``offline=True`` es el modo de SessionStart (sin red)."""
    t = ctx.t()
    ctx.project()
    snap = ctx.base()
    cfg, roles = snap.project, snap.roles
    store = ctx.store()
    now = ctx.inv.now()
    warnings: list[WarningObj] = []
    if snap.fallback_sha is not None:
        warnings.append(make_warning("W_CONFIG_POLICY_FALLBACK", t, details=snap.fallback_details()))
    mode = "offline" if offline else ("refresh" if refresh else "auto")
    loaded = load_board(ctx, cfg=cfg, roles=roles, store=store, now=now, mode=mode)  # type: ignore[arg-type]
    board = loaded.board if loaded is not None else None
    source = loaded.source if loaded is not None else "local"
    reason = loaded.reason if loaded is not None else ("offline" if offline else "no_cache")
    age_s = board.age_s(now) if board is not None and source == "cache" else None
    if source == "local":
        warnings.append(make_warning("W_NET_OFFLINE", t, details={"reason": reason, "age_s": None}))
    elif loaded is not None and loaded.reason not in (None, "offline"):
        warnings.append(make_warning("W_NET_OFFLINE", t, details=loaded.offline_details(now)))
    if board is not None and board.profile == "advisory":  # 05 §12.2, además de la alerta del paquete
        warnings.append(make_warning("W_FORGE_PROFILE_ADVISORY", t))

    login = board.login if board is not None else None
    if login is None:
        ident = store.identity(token_fp=None)
        login = ident.login if ident is not None else None
    me = me_block(login, roles)
    current = _current(ctx, board)
    allowed = _allowed_paths(current, login, roles.section_owner())
    limits = cfg.limits
    expiry_days = _int_setting(limits, "claim_expiry_days", 14, 1, 90)
    warning_days = _int_setting(limits, "expiry_warning_days", 3, 1, 30)
    if board is not None:
        warnings.extend(expiring_warnings(t, board.tasks, login, now, expiry_days))
    settings = cfg.context
    max_chars = _int_setting(settings, "max_chars", DEFAULT_MAX_CHARS, 2000, 9000)
    wanted = _int_setting(settings, "recent_decisions", 5, 0, 10)
    latex = cfg.format == "latex"
    try:
        docs = read_base_docs(ctx.git(), snap, decisions=wanted, latex=latex)
    except OpError:
        docs = BaseDocs()
    plan = docs.plan_text.encode("utf-8") if docs.plan_text is not None else None
    pending = setup_pending(plan, roles, getattr(cfg, "mode", "group"))
    alerts = _alerts(t, board, login, now=now, cfg=cfg, roles=roles, expiry_days=expiry_days,
                     warning_days=warning_days, pending=pending)
    # El resumen es un archivo generado: va en el idioma del proyecto (REQ-OPS-67, REQ-ARCH-70).
    md_t = translator(cfg.language) if cfg.language in LANGS else t
    md_alerts = alerts if md_t.lang == t.lang else _alerts(
        md_t, board, login, now=now, cfg=cfg, roles=roles, expiry_days=expiry_days, warning_days=warning_days,
        pending=pending,
    )
    active = [r for r in (board.tasks if board is not None else ()) if r.state in ACTIVE]
    mine = [r for r in active if same_login(r.owner, login)]
    others = [r for r in active if not same_login(r.owner, login)]

    summary = _summary(
        md_t,
        project_name=getattr(cfg, "title", None) or cfg.name,
        convention=cfg.convention,
        source=source,
        age_s=age_s,
        reason=reason,
        me=me,
        roles=roles,
        current=current,
        allowed=allowed,
        alerts=md_alerts,
        mine=mine,
        others=others,
        docs=docs,
        latex=latex,
        max_chars=max_chars,
        nonce_factory=nonce_factory or (lambda: secrets.token_hex(4)),
    )
    _write_context_md(ctx, summary)

    tally = SanitizeTally()
    data: dict[str, object] = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "cache_age_s": age_s,
        "core_version": _meta.__version__,
        "convention": cfg.convention,
        "profile": board.profile if board is not None and board.profile else "advisory",
        "me": me,
        "current": {
            "branch": current.branch,
            "task": current.task,
            "task_state": current.state,
            "allowed_paths": allowed,
        },
        "my_tasks": [
            {
                "number": r.number,
                "title": marked("untrusted", f"issue#{r.number}.title", r.title, limit=TITLE_MAX, tally=tally),
                "state": r.state,
                "branch": r.branch,
            }
            for r in mine
        ],
        "others_active": [
            {
                "number": r.number,
                "title": marked("untrusted", f"issue#{r.number}.title", r.title, limit=TITLE_MAX, tally=tally),
                "sections": list(r.sections),
                "claimed_by": r.owner,
            }
            for r in others
        ],
        "decisions_recent": [
            {
                "path": d.path,
                "title": marked("reviewed", f"{d.path}#title", d.title, limit=TITLE_MAX, tally=tally),
                "date": d.date,
                "status": d.status[:40],
            }
            for d in docs.decisions
        ],
        "alerts": alerts,
        "summary_md": summary,
    }
    package: dict[str, Any] | None = None
    target: Target | None = None
    if task is not None and phase is not None:
        built = None if offline else _task_context(ctx, task, phase, board, snap, tally)
        if built is None:
            warnings.append(make_warning("W_NET_OFFLINE", t, details={"reason": "task_context", "task": task}))
        else:
            package, target = built
            data["task_context"] = package
    sanitized = tally.details()
    if sanitized is not None:
        warnings.append(make_warning("W_VALIDATION_UNTRUSTED_SANITIZED", t, details=sanitized))
    placeholder = make_warning("W_OUTPUT_TRUNCATED", t, details={
        "budget": max_chars, "trimmed": list(TRIM_ORDER), "omitted": {k: 1000 for k in TRIM_ORDER}})
    trimmed_package = False
    if package is not None and target is not None:

        def fits() -> bool:
            measured = {k: v for k, v in data.items() if k != "summary_md"}
            return envelope_size(OP, measured, [*warnings, placeholder]) <= max_chars

        trimmed_package = trim_package(package, fits=fits, keep=common_tasks(target, board))
    result = fit(OP, data, budget=max_chars, trim_order=TRIM_ORDER, warnings=warnings,
                 truncated_warning=placeholder, exclude=("summary_md",))
    if result.trimmed or trimmed_package:
        details = result.warning_details(max_chars)
        if trimmed_package:
            details["trimmed"] = ["task_context", *details["trimmed"]]  # type: ignore[list-item]
        warnings.append(make_warning("W_OUTPUT_TRUNCATED", t, details=details))
    return ok(OP, data, warnings)


def render_text(result: Result, t: Translator, *, fmt: str = "text") -> str:
    """En modo humano, la CLI imprime el resumen en Markdown (REQ-OPS-67)."""
    del fmt, t
    data = result.data or {}
    summary = data.get("summary_md")
    return summary.rstrip("\n") if isinstance(summary, str) else ""
