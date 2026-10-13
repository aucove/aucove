# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove record-assessment <n>`` · ``record_overlap_assessment`` (05 §9.17; 04-tareas §8.3 y §8.4).

Registra la evaluación de traslape que hizo la sesión (el núcleo nunca llama a un modelo, D-15).

Precondiciones: la tarea está en ``ACTIVE`` y el actor es su dueño (REQ-TASK-61). Con
``phase: "submission"``, el PR de la tarea está abierto (si no, ``E_STATE_INVALID_TRANSITION``) y
``head_sha`` es su cabeza actual (si no, ``E_VALIDATION_ASSESSMENT``, REQ-TASK-60).

Pasos, en el orden de 05 §9.17:

1. ``comment`` (canónico): reemplaza el marcador ``assessment`` de la fase en el comentario
   propio del destino (el de reclamo del Issue o el de entrega del PR), o lo crea, y rehace su
   texto (REQ-TASK-13 y REQ-TASK-62). Con la misma evaluación no cambia nada.
2. ``labels`` (reflejo): ``flag:overlap`` solo con al menos una cita válida (REQ-TASK-64; sin
   ninguna, el veredicto efectivo pasa a ``none`` con ``W_VALIDATION_UNCITED_OVERLAP``) y
   ``flag:direction-change`` solo con un ID vigente del plan (REQ-TASK-69; los demás se
   descartan con ``W_VALIDATION_UNKNOWN_PLAN_IDS``). Nunca quita etiquetas (REQ-TASK-65).
3. ``mention`` (reflejo): solo si se agregó una etiqueta que no estaba, un comentario nuevo que
   menciona al ``lead`` y a los dueños de las tareas citadas (REQ-TASK-67).

El dueño y el estado salen del tablero de ``status`` (con su caché de 10 min, como ``claim``;
00-marco D-61): así bastan pocas lecturas (08 §7.5). Lo que se escribe se lee en vivo.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ..domain.tasks import ACTIVE
from ..domain.text import same_login
from ..forge.base import ForgeNotFound
from ..i18n import LANGS, translator
from .board import load_board
from .comments import own_comment, rebuild
from .result import OpError, Result, WarningObj, make_warning, ok
from .snapshot import parse_markers, task_branch
from .steps import Steps
from .task_propose import plan_ids

if TYPE_CHECKING:
    from ..config.base import BaseSnapshot
    from ..forge.base import ForgeAdapter
    from .board import Board, TaskView
    from .context import OpContext

__all__ = ["OP", "STEPS", "run"]

OP = "record-assessment"
STEPS = ("comment", "labels", "mention")
OVERLAP = "flag:overlap"
DIRECTION = "flag:direction-change"
MERGED_DAYS = 30  # REQ-TASK-64: una tarea integrada en los últimos 30 días vale como cita


def _bad(param: str, reason: str = "type") -> OpError:
    return OpError("E_VALIDATION_INPUT", details={"errors": [{"param": param, "reason": reason}]})


def _check_input(task: object, phase: object, head_sha: object, verdict: object) -> tuple[int, str, str | None, str]:
    if isinstance(task, bool) or not isinstance(task, int) or task < 1:
        raise _bad("task")
    if phase not in ("claim", "submission"):
        raise _bad("phase", "enum")
    if verdict not in ("none", "possible", "clear"):
        raise _bad("verdict", "enum")
    if head_sha is not None and not isinstance(head_sha, str):
        raise _bad("head_sha")
    if phase == "submission" and not head_sha:
        raise _bad("head_sha", "required")
    return task, str(phase), head_sha, str(verdict)  # type: ignore[return-value]


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _task_row(board: Board, number: int) -> TaskView | None:
    return next((r for r in board.tasks if r.number == number), None)


def _valid_citation(target: Mapping[str, object], *, number: int, board: Board, snap: BaseSnapshot,
                    forge: ForgeAdapter, now: datetime) -> bool:
    """REQ-TASK-64: una tarea activa o integrada hace menos de 30 días, un PR del repositorio,
    una decisión o una parte de la base. Citarse a sí misma no cuenta."""
    if "task" in target:
        cited = target["task"]
        row = _task_row(board, cited) if isinstance(cited, int) and cited != number else None
        if row is None:
            return False
        if row.state in ACTIVE:
            return True
        merged = _parse_time(row.pr.merged_at if row.pr is not None else None) or _parse_time(row.closed_at)
        return row.state == "merged" and merged is not None and now - merged <= timedelta(days=MERGED_DAYS)
    if "pr" in target:
        try:
            return isinstance(target["pr"], int) and forge.get_pull_request(target["pr"]) is not None
        except ForgeNotFound:
            return False
    if "decision" in target:
        return target["decision"] in snap.decisions
    if "section" in target:
        return target["section"] in snap.roles.section_owner()
    return False


def _same(old: Mapping[str, object] | None, new: Mapping[str, object]) -> bool:
    """La misma evaluación: todo igual salvo ``recorded_at``."""
    if old is None:
        return False
    strip = {k: v for k, v in new.items() if k != "recorded_at"}
    return {k: v for k, v in old.items() if k != "recorded_at"} == strip


def run(ctx: OpContext, *, task: object = None, phase: object = None, head_sha: object = None, verdict: object = None,
        overlaps: object = None, direction: object = None, model: object = None, door: object = None) -> Result:
    """``record-assessment <n> --phase claim|submission --input <archivo|->``."""
    n, phase_s, head, verdict_s = _check_input(task, phase, head_sha, verdict)
    overlap_items: list[dict[str, Any]] = [dict(o) for o in overlaps] if isinstance(overlaps, list) else []
    direction_obj: dict[str, Any] | None = dict(direction) if isinstance(direction, Mapping) else None
    t = ctx.t()
    ctx.project()
    snap = ctx.base()
    cfg, roles = snap.project, snap.roles
    store = ctx.store()
    now = ctx.inv.now()
    forge = ctx.forge()
    me = forge.identity().login
    lang = translator(cfg.language) if cfg.language in LANGS else t
    warnings: list[WarningObj] = []
    if snap.fallback_sha is not None:
        warnings.append(make_warning("W_CONFIG_POLICY_FALLBACK", t, details=snap.fallback_details()))
    loaded = load_board(ctx, cfg=cfg, roles=roles, store=store, now=now, mode="auto")
    if loaded is None:
        raise OpError("E_NET_OFFLINE")
    board = loaded.board
    row = _task_row(board, n)
    if row is None:
        raise OpError("E_STATE_NOT_A_TASK", details={"task": n})
    if row.state not in ACTIVE or not same_login(row.owner, me):
        raise OpError("E_CLAIM_NOT_HELD", details={"task": n, "owner": row.owner})

    pr = None
    if phase_s == "submission":
        pr = forge.find_pull_request(task_branch(n), state="open")
        if pr is None:
            raise OpError("E_STATE_INVALID_TRANSITION", details={"task": n, "reason": "no_open_pr"})
        if pr.head_sha != head:
            raise OpError("E_VALIDATION_ASSESSMENT", details={"head_sha": pr.head_sha})
    target_kind, target_number = ("pr", pr.number) if pr is not None else ("issue", n)

    valid = [o for o in overlap_items if isinstance(o.get("target"), Mapping)
             and _valid_citation(o["target"], number=n, board=board, snap=snap, forge=forge, now=now)]
    effective = verdict_s
    if verdict_s in ("possible", "clear") and not valid:
        effective = "none"
        warnings.append(make_warning("W_VALIDATION_UNCITED_OVERLAP", t))
    ids_ok: list[str] = []
    if direction_obj is not None:
        plan = plan_ids(ctx, snap)
        asked = [i for i in direction_obj.get("affected_ids", []) if isinstance(i, str)]
        ids_ok = [i for i in asked if plan.get(i) not in (None, "retired")]
        unknown = [i for i in asked if i not in ids_ok]
        if unknown:
            warnings.append(make_warning("W_VALIDATION_UNKNOWN_PLAN_IDS", t, details={"ids": unknown}))
        direction_obj["affected_ids"] = ids_ok
    data = {
        "task": n, "phase": phase_s, "head_sha": head, "verdict": verdict_s, "effective_verdict": effective,
        "overlaps": overlap_items, "direction": direction_obj, "assessed_by": me,
        "model": model if isinstance(model, str) else None, "door": door if isinstance(door, str) else None,
        "recorded_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    steps = Steps(ctx, OP, STEPS)
    wanted = ([OVERLAP] if effective in ("possible", "clear") else []) + ([DIRECTION] if ids_ok else [])
    if steps.dry_run:
        steps.effect("forge_update", f"{target_kind}#{target_number}", lang("tasks.effect.assessment", phase=phase_s))
        if wanted:
            steps.effect("forge_update", f"{target_kind}#{target_number}",
                         lang("tasks.effect.flags", labels=", ".join(wanted)))
        return steps.dry_result(warnings)

    kind = "claim" if phase_s == "claim" else "submission"

    def comment() -> tuple[int, str, bool]:
        comments = forge.list_comments(target_number)
        own = own_comment(comments, me, kind) or own_comment(comments, me, "assessment")
        previous = next((d for k, d in parse_markers(own.body) if k == "assessment"), None) if own else None
        if own is not None and _same(previous, data):
            return own.id, own.url, False  # la misma evaluación: no cambia nada
        body = rebuild(own.body if own is not None else None, "assessment", data, lang, n)
        if own is not None:
            done = forge.update_comment(own.id, body)
            return done.id, done.url, False
        key = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]
        made = forge.create_comment(target_number, body, op_id=f"assessment-{phase_s}-{n}-{key}")
        return made.id, made.url, True

    comment_id, comment_url, created = steps.run("comment", comment)
    added: list[str] = []
    if wanted:
        present = set(pr.labels) if pr is not None else set()
        if pr is None:
            issue = forge.get_issue(n)
            present = set(issue.labels) if issue is not None else set()
        missing = [x for x in wanted if x not in present]
        if missing and steps.reflect("labels", lambda: forge.add_labels(target_number, missing)) is not None:
            added = missing
        elif not missing:
            steps.skip("labels", "present")
    else:
        steps.skip("labels")
    mention_url: str | None = None
    if added:
        mentions = [roles.lead] + [
            r.owner for o in valid if "task" in o["target"]
            for r in [_task_row(board, o["target"]["task"])] if r is not None and r.owner
        ]
        unique = [m for i, m in enumerate(mentions) if m and not same_login(m, me)
                  and not any(same_login(m, x) for x in mentions[:i])]
        text = lang(f"tasks.assessment.mention_{kind}", mentions=" ".join(f"@{m}" for m in unique) or "-",
                    task=n, labels=", ".join(f"`{x}`" for x in added))
        tag = hashlib.sha256(",".join(added).encode()).hexdigest()[:12]  # el op_id no admite ':'
        made = steps.reflect("mention", lambda: forge.create_comment(
            target_number, text, op_id=f"assessment-mention-{phase_s}-{n}-{tag}"))
        mention_url = made.url if made is not None else None
    else:
        steps.skip("mention")
    warnings.extend(steps.warnings(t))
    result: dict[str, object] = {
        "task": n,
        "phase": phase_s,
        "target": {"kind": target_kind, "number": target_number},
        "comment": {"id": comment_id, "url": comment_url, "created": created},
        "effective_verdict": effective,
        "labels_added": added,
        "mention_comment_url": mention_url,
    }
    return ok(OP, result, warnings)
