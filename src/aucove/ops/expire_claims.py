# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove expire-claims`` (05 §9.19; 04-tareas §4.2; 09-ci §6). Solo CLI, sin MCP.

Actor: la identidad del CI (08 REQ-FORGE-43), el ``lead`` o el ``deputy``; si no,
``E_PERM_ROLE_REQUIRED``. ``--now`` solo se admite con ``<PREFIJO>FORGE_ADAPTER=fake``
(``E_VALIDATION_INPUT``); sin él, el umbral se mide con la hora del servidor (la cabecera
``Date``, ``forge.server_time``), nunca con el reloj local. ``--policy`` toma la política de un
directorio, como ``validate --mode pr``. Sin plazo de 60 s (REQ-OPS-22: lo limita el job).

Lee en bloque los Issues abiertos con ``task``, las refs ``task/*`` y los PR, y de cada tarea
las lecturas de 04 §1.1 (``collect_task`` con ``Prefetch``). Por tarea, en el orden de 04 §4.2:

1. ``reconcile`` (reflejo): las reparaciones de REQ-TASK-15, sin comentarios (REQ-TASK-17):
   ``assign_owner``, ``clear_assignees``, ``remove_stale_expired`` y ``close_merged_issue``.
2. ``warn`` (reflejo): en la ventana de aviso y sin un aviso desde la última actividad, crea o
   actualiza el comentario propio con el marcador ``expiry`` (``phase: warning``) y menciona al
   dueño.
3. ``expire`` (canónico): superado ``limits.claim_expiry_days``, agrega ``state:expired`` y
   actualiza el comentario (``phase: expired``), con mención al dueño y al ``lead``. No borra la
   ref ni cambia asignados (REQ-TASK-35).
4. ``unexpire`` (reflejo): una tarea ``expired`` con actividad posterior a la etiqueta la pierde.

``last_activity_at`` es el máximo de la fecha del commit de reclamo, la del comentario de
reclamo del dueño (renovación), el último push (API de actividad) y, en ``changes_requested``,
la revisión que pidió cambios. Sin API de actividad, una ejecución que ve una cabeza distinta de
la del marcador cuenta como actividad y la guarda en el marcador, sin menciones (con
``idle_days: 0``, que no cuenta como aviso). Las tareas en ``in_review`` no vencen (REQ-TASK-33)
y las de ``flag:blocked`` válida se omiten (REQ-TASK-34). Entre dos escrituras espera al menos
1 s (REQ-OPS-21). Dos ejecuciones seguidas no cambian nada (REQ-TASK-36).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from .. import _meta
from ..domain.markers import replace_marker
from ..domain.tasks import Derived, Marker, TaskSnapshot, blocked_ok, derive_state, expiry_markers, review_outcome
from ..domain.text import same_login
from ..forge.base import ForgeError
from ..i18n import LANGS, translator
from .context import env_flag
from .fit import fit
from .registry import forge_error_code
from .result import OpError, Result, WarningObj, make_warning, ok
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
)
from .steps import Steps

if TYPE_CHECKING:
    from ..config.base import BaseSnapshot
    from ..forge.base import Comment, ForgeAdapter, PullRequest
    from ..i18n import Translator
    from .context import OpContext

__all__ = ["BUDGET", "OP", "STEPS", "WRITE_GAP_S", "run"]

OP = "expire-claims"
STEPS = ("reconcile", "warn", "expire", "unexpire")
BUDGET = 4000  # 05 §7.1, «Resto»
WRITE_GAP_S = 1.0  # REQ-OPS-21 y 08 REQ-FORGE-50
EXPIRED = "state:expired"
CLOCKED = frozenset({"claimed", "changes_requested", "expired"})  # REQ-TASK-33
TRIM = ("skipped", "repaired", "unexpired", "warned", "expired")
T = TypeVar("T")


def _sleep(seconds: float) -> None:
    time.sleep(seconds)  # las pruebas lo reemplazan


class _Writer:
    """Escrituras de la forja espaciadas al menos ``WRITE_GAP_S`` (REQ-OPS-21)."""

    def __init__(self) -> None:
        self._last: float | None = None

    def __call__(self, fn: Callable[[], T]) -> T:
        if self._last is not None:
            wait = WRITE_GAP_S - (time.monotonic() - self._last)
            if wait > 0:
                _sleep(wait)
        try:
            return fn()
        finally:
            self._last = time.monotonic()


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)


def _bad(param: str, reason: str) -> OpError:
    return OpError("E_VALIDATION_INPUT", details={"errors": [{"param": param, "reason": reason}]})


def _now_arg(ctx: OpContext, now: object) -> datetime | None:
    """``--now``: solo con la forja falsa (05 §9.19)."""
    if now is None:
        return None
    if env_flag(ctx.inv.env, "FORGE_ADAPTER") != "fake":
        raise _bad("now", "fake_only")
    moment = _parse(now)
    if moment is None:
        raise _bad("now", "format")
    return moment


def _policy(ctx: OpContext, policy: object) -> BaseSnapshot:
    """La política de ``--policy`` o la de la base del clon."""
    if policy is None:
        ctx.project()
        return ctx.base()
    if not isinstance(policy, str) or not policy:
        raise _bad("policy", "type")
    from ..config.base import read_policy_dir

    path = Path(policy) if Path(policy).is_absolute() else Path(ctx.inv.cwd) / policy
    if not path.is_dir():
        raise _bad("policy", "not_a_directory")
    snap = read_policy_dir(path)  # el idioma se resuelve después, con esta política
    ctx.use_policy(snap)
    return snap


def _run_id(env: Mapping[str, str]) -> int | None:
    raw = env.get("GITHUB_RUN_ID", "")
    return int(raw) if raw.isdigit() and int(raw) > 0 else None


def _labeled_at(s: TaskSnapshot, label: str) -> datetime | None:
    found = [e.at for e in s.label_events if e.label == label and e.action == "labeled"]
    return found[-1] if found else None


def _last_marker(s: TaskSnapshot) -> Marker | None:
    valid = expiry_markers(s)[0]
    return max(valid, key=lambda m: m.updated_at) if valid else None


def _last_activity(ev: TaskEvidence, d: Derived, now: datetime) -> tuple[datetime, bool]:
    """``last_activity_at`` de 04 §4.2 y si esta ejecución cuenta como actividad (sin API)."""
    s = ev.snapshot
    owner = d.owner
    signals: list[datetime] = []
    if ev.claim_commit is not None:
        signals.append(ev.claim_commit.author_date)
    signals += [m.updated_at for m in s.issue_markers if m.kind == "claim" and same_login(m.author, owner)]
    if s.ref is not None and s.ref.last_push_at is not None:
        signals.append(s.ref.last_push_at)
    pr = next((p for p in s.pulls if p.number == d.pr), None)
    if pr is not None and review_outcome(s, pr, owner)[0] == "changes_requested":
        signals += [r.submitted_at for r in pr.reviews if r.state == "CHANGES_REQUESTED" and r.submitted_at]
    record = False
    marker = _last_marker(s)
    if ev.activity is None and s.ref is not None and marker is not None:  # sin API de actividad
        if marker.data.get("head_sha") != s.ref.head_sha:
            signals.append(now)
            record = True
        else:
            recorded = _parse(marker.data.get("last_activity_at"))
            if recorded is not None:
                signals.append(recorded)
    return (max(signals) if signals else now), record


def _warned_since(s: TaskSnapshot, last: datetime) -> bool:
    """Hay un aviso válido posterior a la última actividad (un registro con ``idle_days: 0`` no
    es un aviso)."""
    for m in expiry_markers(s)[0]:
        idle = m.data.get("idle_days")
        if m.data.get("phase") in ("warning", "expired") and isinstance(idle, int) and idle > 0 \
                and m.updated_at >= last:
            return True
    return False


def _own_comment(comments: Sequence[Comment], me: str) -> Comment | None:
    mine = [c for c in comments if same_login(c.author, me) and any(k == "expiry" for k, _ in parse_markers(c.body))]
    return min(mine, key=lambda c: c.created_at) if mine else None


class _Run:
    """Estado de una ejecución: la forja, el reloj, los textos y lo que se informa."""

    def __init__(self, ctx: OpContext, forge: ForgeAdapter, snap: BaseSnapshot, me: str, now: datetime,
                 run_id: int | None, lang: Translator, t: Translator) -> None:
        self.ctx = ctx
        self.forge = forge
        self.snap = snap
        self.me = me
        self.now = now
        self.run_id = run_id
        self.lang = lang
        self.t = t
        self.write = _Writer()
        self.dry = ctx.inv.dry_run
        self.plan = Steps(ctx, OP, STEPS, deadline_s=None)
        self.warnings: list[WarningObj] = []
        limits = snap.project.limits
        self.threshold = int(limits["claim_expiry_days"])  # type: ignore[call-overload]
        self.window = self.threshold - int(limits["expiry_warning_days"])  # type: ignore[call-overload]
        self.data: dict[str, Any] = {"evaluated": 0, "threshold_days": self.threshold, "warned": [], "expired": [],
                                     "unexpired": [], "repaired": [], "skipped": []}

    def login(self, login: str) -> str:
        """``login`` con la grafía de ``people`` (REQ-SCHEMA-12)."""
        person = self.snap.roles.person(login)
        return person.login if person is not None else login

    def mention(self, login: str) -> str:
        return f"@{self.login(login)}"

    def reflect(self, steps: Steps, step: str, n: int, fn: Callable[[], T]) -> T | None:
        done = steps.reflect(step, fn)
        if step in steps.failed_reflections:
            steps.failed_reflections.remove(step)
            self.warnings.append(make_warning("W_FORGE_REFLECTION_FAILED", self.t, details={"step": step, "task": n}))
        return done

    def comment(self, ev: TaskEvidence, phase: str, idle: int, last: datetime, mentions: Sequence[str]) -> Comment:
        """Crea o actualiza el comentario propio con el marcador ``expiry`` (REQ-TASK-35)."""
        n = ev.issue.number
        s = ev.snapshot
        data = {"task": n, "phase": phase, "idle_days": idle, "last_activity_at": _iso(last),
                "head_sha": s.ref.head_sha if s.ref is not None else None, "run_id": self.run_id}
        key = "activity" if idle == 0 else phase
        who = " ".join(dict.fromkeys(mentions))
        text = self.lang(f"tasks.expiry.{key}", task=n, idle=idle, days=self.threshold, mentions=who,
                         left=max(self.threshold - idle, 0), name=_meta.NAME)
        body = replace_marker(text, "expiry", data)
        own = _own_comment(ev.comments, self.me)
        if own is not None:
            return self.write(lambda: self.forge.update_comment(own.id, body))
        return self.write(lambda: self.forge.create_comment(n, body, op_id=f"expiry-{n}-{key}-{int(last.timestamp())}"))


def _repairs(run: _Run, ev: TaskEvidence, d: Derived) -> list[tuple[str, Callable[[], object]]]:
    """Las reparaciones automáticas de REQ-TASK-15 que pide la tarea."""
    forge, n, an = run.forge, ev.issue.number, set(d.anomalies)
    out: list[tuple[str, Callable[[], object]]] = []
    if "assignees_mismatch" in an and d.owner is not None:
        owner = run.login(d.owner)
        out.append(("assign_owner", lambda: forge.set_assignees(n, [owner])))
    if "assignee_without_ref" in an:
        out.append(("clear_assignees", lambda: forge.set_assignees(n, [])))
    if "stale_expired_label" in an and EXPIRED in ev.issue.labels:
        out.append(("remove_stale_expired", lambda: forge.remove_label(n, EXPIRED)))
    if "issue_open_after_merge" in an:
        out.append(("close_merged_issue", lambda: forge.update_issue(n, state="closed", state_reason="completed")))
    return out


def _task(run: _Run, ev: TaskEvidence) -> None:
    """Los cuatro pasos de 04 §4.2 sobre una tarea."""
    n = ev.issue.number
    s = ev.snapshot
    d = derive_state(s)
    steps = Steps(run.ctx, OP, STEPS, deadline_s=None)
    if d.anomalies:
        run.warnings.append(make_warning("W_STATE_ANOMALY", run.t, details={"task": n, "anomalies": list(d.anomalies)}))
    repairs = _repairs(run, ev, d)
    if repairs and run.dry:
        for action, _fn in repairs:
            run.plan.effect("forge_update", f"issue#{n}", run.lang(f"tasks.effect.repair.{action}", task=n))
    elif repairs:
        def reconcile() -> None:
            for action, fn in repairs:
                run.write(fn)
                run.data["repaired"].append({"task": n, "action": action})

        run.reflect(steps, "reconcile", n, reconcile)
    if d.state not in CLOCKED:
        return
    run.data["evaluated"] += 1
    owner = d.owner
    if blocked_ok(s, owner):  # REQ-TASK-34
        run.data["skipped"].append({"task": n, "reason": "blocked"})
        return
    if owner is None:  # sin dueño identificable solo el lead decide (04 §1.6)
        run.data["skipped"].append({"task": n, "reason": "no_owner"})
        return
    last, record = _last_activity(ev, d, run.now)
    idle_s = (run.now - last).total_seconds() / 86400
    idle = max(int(idle_s), 0)
    entry = {"task": n, "claimed_by": run.login(owner), "last_activity": _iso(last)}
    if d.state == "expired":
        labeled = _labeled_at(s, EXPIRED)
        if labeled is not None and last > labeled:  # hubo actividad fuera del núcleo
            if run.dry:
                run.plan.effect("forge_update", f"issue#{n}", run.lang("tasks.effect.unexpire", task=n))
                return
            done = run.reflect(steps, "unexpire", n, lambda: run.write(lambda: run.forge.remove_label(n, EXPIRED)))
            if done is not None:
                run.data["unexpired"].append(entry)
                if record:
                    run.reflect(steps, "warn", n, lambda: run.comment(ev, _phase(s), 0, last, ()))
        return
    if idle_s >= run.threshold:
        if run.dry:
            run.plan.effect("forge_update", f"issue#{n}", run.lang("tasks.effect.expire", task=n))
            return

        def expire() -> None:
            run.write(lambda: run.forge.add_labels(n, [EXPIRED]))
            run.comment(ev, "expired", idle, last, (run.mention(owner), run.mention(run.snap.roles.lead)))

        steps.run("expire", expire)
        run.data["expired"].append(entry)
    elif idle_s >= run.window and not _warned_since(s, last):
        if run.dry:
            run.plan.effect("forge_update", f"issue#{n}", run.lang("tasks.effect.warn", task=n))
            return
        if run.reflect(steps, "warn", n, lambda: run.comment(ev, "warning", max(idle, 1), last,
                                                           (run.mention(owner),))) is not None:
            run.data["warned"].append(entry)
    elif record and not run.dry:  # sin API de actividad: la cabeza nueva queda en el marcador
        run.reflect(steps, "warn", n, lambda: run.comment(ev, _phase(s), 0, last, ()))


def _phase(s: TaskSnapshot) -> str:
    marker = _last_marker(s)
    phase = marker.data.get("phase") if marker is not None else None
    return phase if phase in ("warning", "expired") else "warning"


def run(ctx: OpContext, *, policy: object = None, now: object = None) -> Result:
    """``expire-claims [--policy <dir>] [--now <fecha-hora ISO>]``."""
    when = _now_arg(ctx, now)
    snap = _policy(ctx, policy)
    t = ctx.t()
    cfg, roles = snap.project, snap.roles
    warnings: list[WarningObj] = []
    if snap.fallback_sha is not None:
        warnings.append(make_warning("W_CONFIG_POLICY_FALLBACK", t, details=snap.fallback_details()))
    forge = ctx.forge()
    ident = forge.identity()
    if ident.kind == "ci":
        me, run_id = ident.login, _run_id(ctx.inv.env)
    else:
        me = roles.identify(ident.login, ident.id).login or ident.login
        if not is_staff(snap, me):
            raise OpError("E_PERM_ROLE_REQUIRED", details={"login": me, "required_role": ["lead", "deputy"]})
        run_id = None
    issues = forge.list_issues(labels=[TASK_LABEL], state="open")
    current = when or forge.server_time()
    if current is None:  # sin la cabecera Date no se mide con el reloj local (05 §9.19)
        raise OpError("E_FORGE_UNEXPECTED", details={"reason": "server_time"})
    refs = {r.branch: r for r in forge.list_refs("task/")}
    pulls: dict[str, list[PullRequest]] = {}
    for pr in forge.list_pull_requests(state="all"):
        pulls.setdefault(pr.head_branch, []).append(pr)
    lang = translator(cfg.language) if cfg.language in LANGS else t
    state = _Run(ctx, forge, snap, me, current, run_id, lang, t)
    authz = authz_from_roles(roles, ci_actors=CI_ACTORS)
    for issue in sorted(issues, key=lambda i: i.number):
        branch = task_branch(issue.number)
        pre = Prefetch(issue=issue, ref=refs.get(branch), pulls=tuple(pulls.get(branch, ())), reviews={})
        try:
            ev = collect_task(forge, issue.number, authz, base_branch=cfg.base_branch, prefetch=pre)
            if ev is not None:
                _task(state, ev)
        except ForgeError as exc:  # una lectura: la ejecución siguiente retoma (REQ-TASK-36)
            code, details = forge_error_code(exc)
            raise OpError(code, details={**details, "task": issue.number},
                          warnings=[*warnings, *state.warnings]) from exc
        except OpError as exc:  # el paso canónico expire falló: failure_data de esa tarea
            raise OpError(exc.code, details={**(exc.details or {}), "task": issue.number}, data=exc.data,
                          warnings=[*warnings, *state.warnings, *exc.warnings]) from exc
    warnings += state.warnings
    if state.dry:
        return state.plan.dry_result(warnings)
    data = state.data
    notice = make_warning("W_OUTPUT_TRUNCATED", t, details={"budget": BUDGET, "trimmed": list(TRIM),
                                                             "omitted": {k: len(data[k]) for k in TRIM}})
    trimmed = fit(OP, data, budget=BUDGET, trim_order=TRIM, warnings=warnings, truncated_warning=notice)
    if trimmed.trimmed:
        warnings.append(make_warning("W_OUTPUT_TRUNCATED", t, details=trimmed.warning_details(BUDGET)))
    return ok(OP, data, warnings)
