# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove claim <n>`` · ``claim_task`` (05 §9.12; protocolo de 04-tareas §2 y §3).

Pasos, en el orden de 05 §9.12:

1. ``read_task``: el tablero (para P4, P6 y el paquete) y la instantánea completa de la
   tarea; ``derive_state``. Si la tarea ya es del actor, es un reclamo repetido: saltan los
   pasos 2 a 5 y se reparan los reflejos (REQ-TASK-24). Si es de otra persona,
   ``E_CLAIM_ALREADY_CLAIMED``.
2. ``preconditions``: P1 a P6 de 04 §3. Con P3, ``data.foreign_sections`` y ``data.options``.
3. ``claim_commit``: el commit vacío de reclamo, con el mensaje exacto de 04 §2.2 paso 4.
   Su padre es la base o, con un traspaso vigente al actor, la cabeza del archivo (04 §4.3).
4. ``create_ref`` (canónico): la ref ``task/<n>`` en ``claim_sha``. Gana quien ve la ref en
   su propio ``claim_sha`` (REQ-TASK-20); tras un 201 se confirma con un GET (04 §2.3).
5. ``recheck``: si el Issue se cerró o volvió a ``state:proposed``, se compensa borrando la
   ref solo si un GET la confirma en ``claim_sha`` (REQ-OPS-26; en la web no se puede).
6. ``assign`` y ``claim_comment`` (reflejos) y 7. ``renew`` (reflejo, si estaba vencida).
8. ``checkout`` (local) y la caché de la tarea. 9. ``context``: el paquete de fase ``claim``.

``.claude/settings.local.json`` (07-hooks) lo regenera la ruta de hooks de H4; hasta
entonces ``claim`` no lo toca.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from .. import _meta
from ..domain.marked import TITLE_MAX, SanitizeTally, marked
from ..domain.markers import replace_marker
from ..domain.tasks import ACTIVE, derive_state
from ..domain.text import same_login
from ..forge.base import ForgeError, RefAlreadyExists
from ..i18n import LANGS, Translator, translator
from .board import owner_and_sections, refresh, write_task_cache
from .coordinator import Target, build_package, common_tasks, trim_package
from .fit import envelope_size
from .result import OpError, Result, WarningObj, make_warning, ok
from .snapshot import CI_ACTORS, TaskEvidence, authz_from_roles, collect_task, parse_markers, task_branch
from .steps import Steps
from .workspace import checkout_task

if TYPE_CHECKING:
    from ..config.base import BaseSnapshot
    from ..forge.base import Comment, ForgeAdapter
    from .board import Board
    from .context import OpContext

__all__ = ["BUDGET", "OP", "STEPS", "claim_message", "run"]

OP = "claim"
BUDGET = 8000  # 05 §7.1
STEPS = (
    "read_task", "preconditions", "claim_commit", "create_ref", "recheck",
    "assign", "claim_comment", "renew", "checkout", "context",
)
COUNTED = frozenset({"claimed", "changes_requested", "expired"})  # P4: in_review no cuenta


def claim_message(number: int) -> str:
    """Mensaje exacto del commit de reclamo (04 §2.2 paso 4): asunto, nonce y trailers."""
    return f"{_meta.CLAIM_SUBJECT}{number}\n\nclaim-nonce {uuid.uuid4()}\n\nTask: #{number}\nAI-Use: none\n"


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_input(task: object, checkout: object, fresh: object) -> int:
    def bad(param: str, reason: str = "type") -> OpError:
        return OpError("E_VALIDATION_INPUT", details={"errors": [{"param": param, "reason": reason}]})

    if isinstance(task, bool) or not isinstance(task, int) or task < 1:
        raise bad("task")
    if not isinstance(checkout, bool):
        raise bad("checkout")
    if not isinstance(fresh, bool):
        raise bad("fresh")
    return task


def _limit(cfg: Any, key: str, default: int) -> int:
    value = cfg.limits.get(key, default)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else default


# -- precondiciones (04 §3) -----------------------------------------------------------------


def _handoff(ev: TaskEvidence, snap: BaseSnapshot, now: datetime) -> Mapping[str, object] | None:
    """Traspaso vigente (04 §4.3): el último marcador ``release`` del `lead` con ``handoff_to``."""
    roles = snap.roles
    lead_like = {x for x in (roles.lead, roles.deputy) if x}
    hold = timedelta(days=_limit(snap.project, "handoff_hold_days", 7))
    releases = [m for m in ev.snapshot.issue_markers if m.kind == "release"]
    if not releases:
        return None
    last = max(releases, key=lambda m: m.created_at)
    target = last.data.get("handoff_to")
    if not isinstance(target, str) or not any(same_login(last.author, x) for x in lead_like):
        return None
    claims_after = [m for m in ev.snapshot.issue_markers if m.kind == "claim" and m.created_at > last.created_at]
    if claims_after or now - last.created_at > hold:
        return None
    return {"handoff_to": target, "archive_ref": last.data.get("archive_ref"),
            "until": _iso(last.created_at + hold)}


def _preconditions(
    ev: TaskEvidence,
    board: Board,
    snap: BaseSnapshot,
    me: str,
    *,
    now: datetime,
    t: Translator,
    warnings: list[WarningObj],
) -> Mapping[str, object] | None:
    """P1 a P6 de 04 §3. Devuelve el traspaso vigente, si lo hay."""
    roles, cfg = snap.roles, snap.project
    person = roles.person(me)
    if person is None or person.role not in ("researcher", "lead") or not person.active:
        raise OpError("E_PERM_NOT_A_MEMBER", details={"login": me})  # P1
    staff = same_login(me, roles.lead) or same_login(me, roles.deputy)
    declared = ev.body.sections
    if not declared and not staff:
        raise OpError("E_CLAIM_NO_SECTIONS", details={"task": ev.issue.number})  # P2
    owners = roles.section_owner()
    unknown = [s for s in declared if s not in owners]
    if unknown:
        raise OpError("E_VALIDATION_UNKNOWN_SECTION", details={"sections": unknown})
    foreign = [{"section": s, "owner": owners[s]} for s in declared if not same_login(owners[s], me)]
    if foreign:  # P3, también para el lead
        options = ["split"]
        if len(foreign) == len(declared):
            options.append("owner_claims")
        options.append("reassign_section")
        raise OpError("E_PERM_SECTION_NOT_OWNED", details={"sections": [f["section"] for f in foreign]},
                      data={"foreign_sections": foreign, "options": options})
    n = ev.issue.number
    mine = sorted(r.number for r in board.tasks if r.number != n and r.state in COUNTED and same_login(r.owner, me))
    limit = _limit(cfg, "max_active_claims", 3)
    if len(mine) >= limit:  # P4
        raise OpError("E_CLAIM_LIMIT_REACHED", details={"limit": limit, "active": mine})
    handoff = _handoff(ev, snap, now)
    if handoff is not None and not same_login(me, str(handoff["handoff_to"])) and not same_login(me, roles.lead):
        raise OpError("E_CLAIM_RESERVED", details={"handoff_to": handoff["handoff_to"], "until": handoff["until"]})
    exclusive = cfg.limits.get("exclusive_sections", False) is True
    for sid in declared:
        others = sorted(r.number for r in board.tasks
                        if r.number != n and r.state in ACTIVE and sid in r.sections)
        if not others:
            continue
        if exclusive:  # P6
            raise OpError("E_CLAIM_SECTION_BUSY", details={"section": sid, "tasks": others})
        same_owner = sorted(r.number for r in board.tasks
                            if r.number in others and same_login(r.owner, me))
        if same_owner:  # REQ-TASK-30
            warnings.append(make_warning("W_CLAIM_SECTION_SHARED", t, details={"section": sid, "tasks": same_owner}))
    return handoff


# -- pasos con efectos -----------------------------------------------------------------------


def _parent(
    forge: ForgeAdapter,
    base: str,
    base_sha: str | None,
    handoff: Mapping[str, object] | None,
    me: str,
    *,
    fresh: bool,
) -> tuple[str, str]:
    """``(padre, base_sha)`` del commit de reclamo (04 §2.2 paso 4 y §4.3).

    ``base_sha`` es la punta de la base que leyó el tablero un momento antes; sin ella se lee.
    """
    if base_sha is None:
        base_ref = forge.get_ref(base)
        if base_ref is None:
            raise OpError("E_FORGE_NOT_FOUND", details={"ref": base})
        base_sha = base_ref.sha
    archive = handoff.get("archive_ref") if handoff is not None else None
    if not fresh and handoff is not None and same_login(me, str(handoff["handoff_to"])) and isinstance(archive, str):
        name = archive.removeprefix("refs/tags/")
        ref = forge.get_ref(name, namespace="tags")
        if ref is not None:
            return ref.sha, base_sha
    return base_sha, base_sha


def _create_ref(
    forge: ForgeAdapter, ev: TaskEvidence, claim_sha: str, me: str, *, recollect: Any
) -> bool:
    """Paso 5 de 04 §2.2 con los resultados de §2.3; ``True`` si este intento creó la ref."""
    branch = task_branch(ev.issue.number)
    try:
        forge.create_ref(branch, claim_sha)
    except RefAlreadyExists as exc:
        if exc.existing.sha == claim_sha:
            return True
        again = recollect()
        derived = derive_state(again.snapshot) if again is not None else None
        if derived is not None and derived.state in ACTIVE and same_login(derived.owner, me):
            return False
        owner = derived.owner if derived is not None else None
        raise OpError("E_CLAIM_ALREADY_CLAIMED", details={"owner": owner}) from exc
    confirm = forge.get_ref(branch)  # GET de confirmación tras el 201 (04 §2.3)
    if confirm is not None and confirm.sha != claim_sha:
        raise OpError("E_CLAIM_ALREADY_CLAIMED", details={"owner": None, "reason": "lost_after_201"})
    return True


def _recheck(forge: ForgeAdapter, steps: Steps, number: int, claim_sha: str, *, web: bool) -> None:
    """Paso 6 de 04 §2.2: si la tarea dejó de ser reclamable, compensa (REQ-OPS-26)."""
    issue = forge.get_issue(number)
    proposed = issue is not None and any(x.lower() == "state:proposed" for x in issue.labels)
    if issue is not None and issue.state == "open" and not proposed:
        return
    compensated = False
    if not web:  # en la web, borrar la rama da 403 (REQ-TASK-22)
        branch = task_branch(number)
        ref = forge.get_ref(branch)
        if ref is not None and ref.sha == claim_sha:
            forge.delete_ref(branch)
            compensated = True
            steps.compensated("create_ref")
    raise OpError("E_STATE_INVALID_TRANSITION",
                  details={"reason": "changed_during_claim", "compensated": compensated})


def _assign(forge: ForgeAdapter, number: int, me: str) -> None:
    issue = forge.set_assignees(number, [me])
    if not any(same_login(a, me) for a in issue.assignees):
        # la forja descarta en silencio una asignación sin permiso (04 §2.2 paso 7)
        raise ForgeError("permission", "assignment was dropped")


def _own_claim_comment(comments: Sequence[Comment], me: str) -> Comment | None:
    found = [c for c in comments if same_login(c.author, me) and any(k == "claim" for k, _ in parse_markers(c.body))]
    return found[-1] if found else None


def _claim_comment(
    forge: ForgeAdapter,
    ev: TaskEvidence,
    me: str,
    data: Mapping[str, object],
    lang: Translator,
) -> None:
    """Crea o actualiza en su sitio el comentario de reclamo (REQ-TASK-13)."""
    n = ev.issue.number
    own = _own_claim_comment(ev.comments, me)
    if own is not None:
        body = replace_marker(own.body, "claim", data)
        if body != own.body:
            forge.update_comment(own.id, body)
        return
    visible = lang("tasks.claim.comment", task=n, login=me, sections=", ".join(ev.body.sections) or "-")
    forge.create_comment(n, replace_marker(visible, "claim", data), op_id=f"claim-{n}-{str(data['claim_sha'])[:16]}")


def _marker_data(ev: TaskEvidence, me: str) -> Mapping[str, object] | None:
    """Datos del marcador ``claim`` vigente del actor, si lo tiene."""
    own = [m for m in ev.snapshot.issue_markers if m.kind == "claim" and same_login(m.author, me)]
    return own[-1].data if own else None


# -- operación ---------------------------------------------------------------------------------


def run(ctx: OpContext, *, task: object, checkout: object = True, fresh: object = False) -> Result:
    """``claim <n> [--no-checkout] [--fresh]``."""
    n = _check_input(task, checkout, fresh)
    t = ctx.t()
    ctx.project()
    snap = ctx.base()
    cfg, roles = snap.project, snap.roles
    store = ctx.store()
    now = ctx.inv.now()
    forge = ctx.forge()
    warnings: list[WarningObj] = []
    if snap.fallback_sha is not None:
        warnings.append(make_warning("W_CONFIG_POLICY_FALLBACK", t, details=snap.fallback_details()))
    me = forge.identity().login
    try:
        caps = forge.capabilities()
    except ForgeError:
        caps = None
    # Sesión web de Claude Code (08 §8): ahí no se puede borrar una rama (REQ-TASK-22).
    web = ctx.inv.env.get("CLAUDE_CODE_REMOTE") == "true"
    authz = authz_from_roles(roles, ci_actors=CI_ACTORS)
    lang = translator(cfg.language) if cfg.language in LANGS else t
    base = cfg.base_branch
    branch = task_branch(n)
    steps = Steps(ctx, OP, STEPS)

    def collect() -> TaskEvidence | None:
        return collect_task(forge, n, authz, base_branch=base,
                            default_branch=caps.default_branch if caps is not None else None)

    def read() -> tuple[Board, TaskEvidence]:
        fetched = refresh(forge, cfg=cfg, roles=roles, store=store, now=now)
        prefetch = fetched.prefetch(n)
        ev = collect_task(forge, n, authz, base_branch=base, prefetch=prefetch,
                          default_branch=caps.default_branch if caps is not None else None) if prefetch else collect()
        if ev is None:
            raise OpError("E_STATE_NOT_A_TASK", details={"task": n})
        return fetched.board, ev

    board, ev = steps.run("read_task", read)
    derived = derive_state(ev.snapshot)
    if derived.anomalies:
        warnings.append(make_warning("W_STATE_ANOMALY", t, details={"task": n, "anomalies": list(derived.anomalies)}))
    repeated = derived.state in ACTIVE and same_login(derived.owner, me)
    if derived.state in ACTIVE and not repeated:
        raise steps.failure("preconditions", "E_CLAIM_ALREADY_CLAIMED", details={"owner": derived.owner})
    if not repeated and derived.state != "open":
        raise steps.failure("preconditions", "E_STATE_INVALID_TRANSITION", details={"state": derived.state})

    previous = _marker_data(ev, me)
    created = False
    if repeated:
        for step in ("preconditions", "claim_commit", "create_ref", "recheck"):
            steps.skip(step, "already_claimed")
        claim_sha = ev.snapshot.ref.claim_commit_sha if ev.snapshot.ref is not None else None
        if not claim_sha and previous is not None and isinstance(previous.get("claim_sha"), str):
            claim_sha = str(previous["claim_sha"])
        base_sha = previous.get("base_sha") if previous is not None else None
    else:
        handoff = steps.run("preconditions",
                            lambda: _preconditions(ev, board, snap, me, now=now, t=t, warnings=warnings))
        if steps.dry_run:
            return _dry_run(steps, n, me, wanted=bool(checkout), expired=False, warnings=warnings, lang=lang)

        def make_commit() -> tuple[str, str]:
            parent, base_sha = _parent(forge, base, board.base.sha, handoff, me, fresh=bool(fresh))
            return forge.create_empty_commit(parent, claim_message(n)), base_sha

        claim_sha, base_sha = steps.run("claim_commit", make_commit)
        created = steps.run("create_ref", lambda: _create_ref(forge, ev, str(claim_sha), me, recollect=collect))
        if created:
            steps.run("recheck", lambda: _recheck(forge, steps, n, str(claim_sha), web=web))
        else:  # la ref ya era del actor: un reclamo repetido que ganó otra invocación suya
            steps.skip("recheck", "already_claimed")
            again = collect()
            if again is not None:
                ev = again
                derived = derive_state(ev.snapshot)
                claim_sha = ev.snapshot.ref.claim_commit_sha if ev.snapshot.ref is not None else claim_sha
            previous = _marker_data(ev, me)
    if repeated and steps.dry_run:
        return _dry_run(steps, n, me, wanted=bool(checkout), expired=derived.state == "expired",
                        warnings=warnings, lang=lang, reflections_only=True)

    expired = derived.state == "expired"
    marker = {
        "task": n,
        "login": me,
        "claim_sha": claim_sha,
        "base_sha": base_sha if isinstance(base_sha, str) else None,
        "sections": list(ev.body.sections),
        "spec_hash": ev.body.spec_hash,
        "claimed_at": previous.get("claimed_at") if previous is not None and repeated else _iso(now),
        "renewed_at": _iso(now) if expired else (previous.get("renewed_at") if previous is not None else None),
    }
    steps.reflect("assign", lambda: _assign(forge, n, me))
    steps.reflect("claim_comment", lambda: _claim_comment(forge, ev, me, marker, lang))
    if expired:
        steps.reflect("renew", lambda: forge.remove_label(n, "state:expired"))
    else:
        steps.skip("renew")

    if expired:  # el estado tras renovar es el de base, sin la etiqueta
        labels = frozenset(x for x in ev.snapshot.labels if x != "state:expired")
        state = derive_state(dataclasses.replace(ev.snapshot, labels=labels)).state
    else:
        state = derived.state if repeated else "claimed"
    # Partes efectivas (04 §3): las del marcador ∩ las del Issue en un reclamo repetido; en
    # uno nuevo, las declaradas, que P3 ya comprobó que son del actor.
    sections = owner_and_sections(ev.snapshot, derived)[1] if repeated else tuple(ev.body.sections)
    done = steps.run("checkout", lambda: checkout_task(ctx, ctx.git(), n, wanted=bool(checkout)))
    if not done.done:
        warnings.append(make_warning("W_STATE_CHECKOUT_SKIPPED", t, details={"reason": done.reason}))
    write_task_cache(store, n, cfg.forge_repo, claimant=me, state=state, sections=sections, now=now,
                     local_branch=done.local_branch)

    tally = SanitizeTally()
    spec = ev.body.spec or {}
    target = Target(
        number=n, state=state, owner=me, sections=sections,
        plan_refs=tuple(spec.get("plan_refs", ())),  # type: ignore[arg-type]
        title=ev.issue.title, summary=str(spec.get("summary", "")),
        done=tuple(spec.get("done", ())),  # type: ignore[arg-type]
    )
    package = steps.run("context", lambda: build_package(ctx, forge, phase="claim", target=target, board=board,
                                                         snap=snap, tally=tally))
    data: dict[str, object] = {
        "task": {
            "number": n,
            "url": ev.issue.url,
            "title": marked("untrusted", f"issue#{n}.title", ev.issue.title, limit=TITLE_MAX, tally=tally),
            "state": state,
            "sections": list(sections),
        },
        "branch": branch,
        "created": created,
        "claim_sha": claim_sha,
        "claimant": me,
        "steps": steps.as_list(),
        "checkout": done.to_dict(),
        "context": package,
        "next": ["record-assessment"],
    }
    sanitized = tally.details()
    if sanitized is not None:
        warnings.append(make_warning("W_VALIDATION_UNTRUSTED_SANITIZED", t, details=sanitized))
    warnings.extend(steps.warnings(t))
    placeholder = make_warning("W_OUTPUT_TRUNCATED", t, details={"budget": BUDGET, "trimmed": ["context"]})
    keep = common_tasks(target, board)
    budget = BUDGET

    def fits() -> bool:
        return envelope_size(OP, data, [*warnings, placeholder]) <= budget

    if trim_package(package, fits=fits, keep=keep):
        warnings.append(make_warning("W_OUTPUT_TRUNCATED", t, details={
            "budget": budget, "trimmed": ["context"], "omitted": dict(package["omitted"])}))
    return ok(OP, data, warnings)


def _dry_run(
    steps: Steps,
    n: int,
    me: str,
    *,
    wanted: bool,
    expired: bool,
    warnings: list[WarningObj],
    lang: Translator,
    reflections_only: bool = False,
) -> Result:
    """Plan de ``--dry-run`` (REQ-OPS-30): lo que la ejecución real escribiría."""
    branch = task_branch(n)
    if not reflections_only:
        steps.effect("forge_create", branch, lang("tasks.effect.claim_ref", task=n))
    steps.effect("forge_update", f"issue#{n}", lang("tasks.effect.assign", task=n, login=me))
    steps.effect("forge_update", f"issue#{n}", lang("tasks.effect.claim_comment"))
    if expired:
        steps.effect("forge_update", f"issue#{n}", lang("tasks.effect.renew"))
    if wanted:
        steps.effect("git_checkout", branch, lang("tasks.effect.checkout", task=n))
    steps.effect("local_write", f"{_meta.CACHE_DIR}/cache/tasks/{n}.json", lang("tasks.effect.cache"))
    return steps.dry_result(warnings)
