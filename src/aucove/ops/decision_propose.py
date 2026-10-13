# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove decision propose`` · ``propose_decision`` (05 §9.20; 03-esquemas §6; D-19).

Solo en ``task/<n>`` reclamada por el actor (la caché de la tarea, como ``save``); si no,
``E_STATE_NOT_ON_TASK_BRANCH``. Los ``plan_refs`` deben estar vigentes en ``plan.md`` de la base
(``E_VALIDATION_UNKNOWN_PLAN_ID``) y cada ID de ``supersedes`` debe ser un registro de la base;
con ``status: rejected``, ``supersedes`` va vacío (03 §6.2).

Pasos, en el orden de 05 §9.1:

1. ``render``: el registro de 03 §6 en ``memory/decisions/<fecha>-<n>-<slug>.md``, con la fecha
   local (REQ-SCHEMA-49), ``slugify(title)`` (REQ-SCHEMA-47), ``author`` = el actor y ``origin``
   = ``human`` solo desde la CLI con TTY y sin ``CLAUDE_CODE_CHILD_SESSION`` (10-seguridad
   REQ-SEC-14). El cuerpo lleva las secciones de 03 §6.3 en el idioma del proyecto y omite
   las alternativas si no hay.
2. ``write_file`` (canónico): nunca sobrescribe. Si el archivo ya existe con el mismo
   contenido, ``created:false``; con otro, ``E_VALIDATION_DECISION_EXISTS``.
3. ``commit`` (canónico) y 4. ``push``: como ``save`` (§9.14), solo con el registro. Sin red, el
   commit queda en local con ``W_NET_OFFLINE``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING

from ..domain.text import slugify
from ..i18n import LANGS, translator
from .context import stdio_is_tty
from .result import OpError, Result, WarningObj, make_warning, ok
from .rules.schemas import BODY_SECTIONS
from .save import (
    Change,
    check_changes,
    commit_trace,
    commit_work,
    integrate_and_push,
    resolve_work,
    scope_check,
    sync_remote,
    trace_warnings,
)
from .steps import Steps
from .task_propose import check_plan_refs
from .workspace import merge_in_progress

if TYPE_CHECKING:
    from .context import OpContext

__all__ = ["OP", "STEPS", "render", "run"]

OP = "decision.propose"
STEPS = ("render", "write_file", "commit", "push")
TITLE_MAX = 120
SUMMARY_MAX = 280
TEXT_MAX = 4000
ALTERNATIVE_MAX = 1000
LIST_MAX = {"alternatives": 10, "supersedes": 20, "plan_refs": 20}
ALTERNATIVES = {"es": "Alternativas consideradas", "en": "Alternatives considered"}  # 03 §6.3


def _bad(param: str, reason: str = "type") -> OpError:
    return OpError("E_VALIDATION_INPUT", details={"errors": [{"param": param, "reason": reason}]})


def _text(name: str, value: object, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _bad(name, "minLength" if isinstance(value, str) else "type")
    if len(value) > limit:
        raise _bad(name, "maxLength")
    return value.replace("\r\n", "\n").strip()


def _strings(name: str, value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise _bad(name)
    if len(value) > LIST_MAX[name]:
        raise _bad(name, "maxItems")
    return list(value)


def render(*, lang: str, record_id: str, title: str, date: str, author: str, origin: str, task: int, status: str,
           summary: str, supersedes: Sequence[str], plan_refs: Sequence[str], context: str, decision: str,
           consequences: str, alternatives: Sequence[str]) -> str:
    """El registro de 03 §6.2 y §6.3, con LF y salto de línea final."""
    col = 0 if lang == "es" else 1
    front = [
        "---",
        f"id: {record_id}",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"date: {date}",
        f"author: {json.dumps(author, ensure_ascii=False)}",
        f"origin: {origin}",
        f"task: {task}",
        f"status: {status}",
        f"summary: {json.dumps(summary, ensure_ascii=False)}",
        f"supersedes: [{', '.join(supersedes)}]",
        f"plan_refs: [{', '.join(plan_refs)}]",
        "---",
    ]
    context_h, decision_h, consequences_h = (pair[col] for pair in BODY_SECTIONS)
    parts = [f"## {context_h}", context, f"## {decision_h}", decision]
    if alternatives:
        items = ["- " + alt.replace("\r\n", "\n").strip().replace("\n", "\n  ") for alt in alternatives]
        parts += [f"## {ALTERNATIVES['es' if col == 0 else 'en']}", "\n".join(items)]
    parts += [f"## {consequences_h}", consequences]
    return "\n".join(front) + "\n\n" + "\n\n".join(parts) + "\n"


def _origin(ctx: OpContext) -> str:
    """``human`` solo desde la CLI con TTY y sin sesión hija de Claude Code (REQ-SEC-14)."""
    inv = ctx.inv
    tty = inv.tty if inv.tty is not None else stdio_is_tty()
    if inv.surface == "cli" and tty and not inv.env.get("CLAUDE_CODE_CHILD_SESSION"):
        return "human"
    return "agent"


def run(ctx: OpContext, *, title: object = None, summary: object = None, context: object = None,
        decision: object = None, consequences: object = None, status: object = "accepted",
        alternatives: object = None, supersedes: object = None, plan_refs: object = None) -> Result:
    """``decision propose --title --summary --context --decision --consequences [--status] [...]``."""
    title_s = _text("title", title, TITLE_MAX)
    summary_s = _text("summary", summary, SUMMARY_MAX)
    context_s = _text("context", context, TEXT_MAX)
    decision_s = _text("decision", decision, TEXT_MAX)
    consequences_s = _text("consequences", consequences, TEXT_MAX)
    if status not in ("accepted", "rejected"):
        raise _bad("status", "enum")
    alts = _strings("alternatives", alternatives)
    if any(len(a) > ALTERNATIVE_MAX for a in alts):
        raise _bad("alternatives", "maxLength")
    alts = [a for a in alts if a.strip()]
    sup = list(dict.fromkeys(_strings("supersedes", supersedes)))
    refs = _strings("plan_refs", plan_refs)
    if status == "rejected" and sup:  # un rechazo no reemplaza a nadie (03 §6.2)
        raise _bad("supersedes", "rejected")
    t = ctx.t()
    project = ctx.project()
    assert project is not None
    root = project.root
    snap = ctx.base()
    cfg = snap.project
    git = ctx.git()
    warnings: list[WarningObj] = []
    if snap.fallback_sha is not None:
        warnings.append(make_warning("W_CONFIG_POLICY_FALLBACK", t, details=snap.fallback_details()))
    try:
        work = resolve_work(ctx, git, snap)
    except OpError as exc:
        if exc.code != "E_CLAIM_NOT_HELD":
            raise
        raise OpError("E_STATE_NOT_ON_TASK_BRANCH", details={**(exc.details or {}), "reason": "not_owner"}) from exc
    if merge_in_progress(ctx):
        raise OpError("E_STATE_MERGE_IN_PROGRESS")
    unknown = [s for s in sup if f"memory/decisions/{s}.md" not in snap.decisions]
    if unknown:  # REQ-SCHEMA-52: lo que se reemplaza está en la base
        raise OpError("E_VALIDATION_INPUT", details={"errors": [{"param": "supersedes", "reason": "unknown",
                                                                 "ids": unknown}]})
    refs = check_plan_refs(ctx, snap, refs)
    lang = cfg.language if cfg.language in LANGS else "en"
    steps = Steps(ctx, OP, STEPS)

    def render_step() -> tuple[str, str]:
        date = ctx.inv.now().astimezone().strftime("%Y-%m-%d")  # fecha local (REQ-SCHEMA-49)
        record_id = f"{date}-{work.number}-{slugify(title_s)}"
        text = render(lang=lang, record_id=record_id, title=title_s, date=date, author=work.me, origin=_origin(ctx),
                      task=work.number, status=str(status), summary=summary_s, supersedes=sup, plan_refs=refs,
                      context=context_s, decision=decision_s, consequences=consequences_s, alternatives=alts)
        return f"memory/decisions/{record_id}.md", text

    path, text = steps.run("render", render_step)
    target = root / path
    exists = target.exists()
    if exists:
        try:
            same = target.read_bytes() == text.encode("utf-8")
        except OSError:
            same = False
        if not same:
            raise steps.failure("write_file", "E_VALIDATION_DECISION_EXISTS", details={"path": path})
    else:
        check_changes(ctx, git, snap, work, [Change("A", path)])  # clase append (06 §2.4)
    msgs = translator(lang)
    if steps.dry_run:
        if not exists:
            steps.effect("file_write", path, msgs("tasks.effect.decision_file", path=path))
        steps.effect("git_commit", work.local, msgs("tasks.effect.decision_commit"))
        steps.effect("git_push", work.branch, msgs("tasks.effect.push", task=work.number))
        return steps.dry_result(warnings)

    def write_file() -> bool:
        if exists:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(target, "xb") as fh:  # nunca sobrescribe (05 §9.20)
                fh.write(text.encode("utf-8"))
        except FileExistsError:
            raise OpError("E_VALIDATION_DECISION_EXISTS", details={"path": path}) from None
        return True

    created = steps.run("write_file", write_file)
    trace = commit_trace(ctx, ai_use=None, ai_tool=None, ai_model=None)
    found = scope_check(ctx, git, snap, work, [path])
    commit_sha: str | None = None
    if found:
        done = steps.run("commit", lambda: commit_work(git, snap, work, found, [path],
                                                       message=msgs("tasks.decision.commit", title=title_s),
                                                       trace=trace))
        commit_sha = done.sha if done is not None else None
        warnings.extend(trace_warnings(trace, t))
    else:
        steps.skip("commit", "no_changes")

    def push_step() -> bool:
        remote = sync_remote(ctx, git, cfg.base_branch, work.number)
        if remote.offline:
            warnings.append(make_warning("W_NET_OFFLINE", t))
            return False
        return integrate_and_push(ctx, git, snap, work, t, warnings)

    pushed = bool(steps.run("push", push_step))
    warnings.extend(steps.warnings(t))
    return ok(OP, {"path": path, "created": created, "commit": commit_sha, "pushed": pushed}, warnings)
