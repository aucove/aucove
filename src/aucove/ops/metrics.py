# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove metrics`` (05 §9.9; 12-mvp §5.2 y §5.3): las tablas del piloto.

Todo sale de la API REST de la forja (Issues, sus eventos de etiquetas y comentarios, PR,
revisiones y commits) y de la historia de primer padre de la rama base (REQ-MVP-52); nada de
telemetría externa.

- ``tasks``: una fila por Issue con la etiqueta ``task``, con las columnas de 12-mvp §5.3. El
  estado es el de ``derive_state`` al exportar (04 §1.5). La aprobación es la de 06 §3.2 con la
  política actual de la base y solo las revisiones anteriores a la integración, como en
  ``ci audit`` (``reviews.approval_at_merge``); el bypass registrado es el de 06 §3.4.
- ``events``: una fila por evento, en orden de fecha. ``role_added`` y ``role_removed`` salen de
  las versiones de ``roles.yaml`` en la base (solo personas activas): ``actor`` es la persona y
  ``actor_role``, su rol en esa versión. En los demás eventos, ``actor_role`` es el rol en el
  ``roles.yaml`` actual de la base. ``main_broken_*`` llevan el número del Issue en ``detail``.

``--output <f>`` escribe el CSV de ``--table`` (``tasks`` por defecto): UTF-8 con encabezado,
fechas ISO 8601 UTC, listas separadas por ``;`` y celdas vacías para lo que no aplica
(REQ-MVP-53). El sobre sigue yendo a stdout; sin ``--output`` no se escribe ningún archivo, y con
``--dry-run`` tampoco. Con ``--since`` y ``--until`` (``AAAA-MM-DD``, en UTC y ambos inclusivos)
entran las tareas creadas en ese intervalo y los eventos ocurridos en él; sin ``--until``, hasta
el momento de exportar.

Seudonimización (REQ-MVP-54): ``actor``, ``claimant`` y ``proposed_by`` van como ``P-`` y los 10
primeros hexadecimales de HMAC-SHA256(sal, login en minúsculas). La sal sale solo de
``AUCOVE_METRICS_SALT``: al menos 64 hexadecimales, en número par, y la clave del HMAC son los
bytes que codifican. Sin ella y sin ``--no-pseudonymize``, ``E_CONFIG_INVALID`` antes de leer
nada.

``data.metrics`` resume, en horas de calendario, lo que sale de las tablas sin el calendario del
piloto: M1, M2 (mediana y p90 por rango más cercano), M3, M6 (y los no detectados), M7, M8, M11
y M12. Lo que pide días hábiles, semanas activas o encuestas (M4, M5, M9, M10, M13 y M14) se
calcula en el análisis del piloto con el CSV.

La interfaz de la forja (08 §2) no trae el timeline de los Issues: ``submitted_at`` es
``pr_created_at`` si el PR no es borrador (no se ve el ``ready_for_review`` de un PR que nació
en borrador), ``claimed_at`` no tiene el respaldo del evento ``assigned``, y ``task_cancelled`` y
``main_broken_closed`` van sin ``actor``.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import math
import re
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .. import _meta
from ..domain.tasks import derive_state, live_claims
from ..domain.text import same_login
from .result import OpError, Result, WarningObj, fail, make_error, make_warning, ok
from .snapshot import TASK_LABEL, Prefetch, authz_from_roles, claim_subject, collect_task, task_branch

if TYPE_CHECKING:
    from ..config.base import BaseSnapshot
    from ..config.roles import Roles
    from ..forge.base import CommitInfo, ForgeAdapter, Issue, PullRequest, Review
    from ..i18n import Translator
    from ..vcs.git import Git
    from .context import OpContext
    from .snapshot import TaskEvidence

__all__ = [
    "EVENT_COLUMNS", "OP", "SALT_ENV", "TABLES", "TASK_COLUMNS", "Export", "collect", "pseudonym", "render_text",
    "role_of", "run", "summary",
]

OP = "metrics"
SCHEMA_VERSION = 1
SALT_ENV = _meta.ENV_PREFIX + "METRICS_SALT"  # REQ-MVP-54: la única fuente de la sal
TABLES = ("tasks", "events")
TASK_COLUMNS = (
    "schema_version", "repo", "profile", "task", "state", "sections", "proposed_by", "created_at", "accepted_at",
    "claimant", "claimed_at", "claims_count", "expired_count", "pr", "pr_created_at", "submitted_at",
    "first_review_at", "first_reviewer_role", "changes_requested_count", "approved_at", "merged_at",
    "merged_without_approval", "cancelled_at", "flag_overlap", "overlap_outcome", "flag_direction_change",
    "hook_denials", "t_delivery_h", "t_review_h",
)
EVENT_COLUMNS = ("schema_version", "repo", "occurred_at", "event", "actor", "actor_role", "task", "pr", "label", "detail")
PSEUDONYMIZED = frozenset({"actor", "claimant", "proposed_by"})
MAIN_BROKEN = "main-broken"  # 06 §4.6
ROLES_FILE = "roles.yaml"
MAX_ROLE_VERSIONS = 1000  # versiones de roles.yaml que se recorren en la base
PROPOSED = "state:proposed"
EXPIRED = "state:expired"
FLAG_OVERLAP = "flag:overlap"
ACK_OVERLAP = "ack:overlap"
FLAG_DIRECTION = "flag:direction-change"
_SALT = re.compile(r"(?:[0-9a-fA-F]{2}){32,}")
_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_HOOK_DENIALS = re.compile(r"<!-- " + re.escape(_meta.NAME) + r":metrics v1 hook_denials=([0-9]{1,9}) -->")
_REVIEW_EVENTS = {"APPROVED": "review_approved", "CHANGES_REQUESTED": "review_changes_requested"}
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _bad(param: str, reason: str = "type") -> OpError:
    return OpError("E_VALIDATION_INPUT", details={"errors": [{"param": param, "reason": reason}]})


def _day(param: str, value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _DAY.fullmatch(value):
        raise _bad(param, "pattern")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise _bad(param, "range") from None


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else _utc(moment).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hours(end: datetime | None, start: datetime | None) -> float | None:
    if end is None or start is None:
        return None
    return round((_utc(end) - _utc(start)).total_seconds() / 3600, 2)


def _within(moment: datetime | None, lo: datetime | None, hi: datetime) -> bool:
    if moment is None:
        return False
    at = _utc(moment)
    return (lo is None or at >= lo) and at <= hi


def pseudonym(key: bytes, login: str) -> str:
    """``P-`` y los 10 primeros hexadecimales de HMAC-SHA256(sal, login en minúsculas) (REQ-MVP-54)."""
    return "P-" + hmac.new(key, login.lower().encode("utf-8"), hashlib.sha256).hexdigest()[:10]


def _salt_error(t: Translator, env: Mapping[str, str]) -> Result:
    """``E_CONFIG_INVALID`` con el mensaje y el ``hint`` de la sal (REQ-MVP-54)."""
    reason = "invalid" if env.get(SALT_ENV, "").strip() else "missing"
    err = make_error("E_CONFIG_INVALID", t, details={"variable": SALT_ENV, "reason": reason})
    return fail(OP, replace(err, message=t("metrics.salt.message", variable=SALT_ENV),
                            hint=t("metrics.salt.hint", variable=SALT_ENV)))


def role_of(roles: Roles, login: str | None) -> str | None:
    """``lead``, ``deputy`` o ``researcher`` según ``roles``; ``None`` si no es una persona activa."""
    person = roles.person(login)
    if person is None or not person.active:
        return None
    if same_login(person.login, roles.lead):
        return "lead"
    if same_login(person.login, roles.deputy):
        return "deputy"
    return "researcher"


# -- la recolección -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Export:
    """Las filas de las dos tablas, sin acotar por fechas ni seudonimizar."""

    tasks: list[dict[str, object]]
    events: list[dict[str, object]]
    broken: list[Issue]  # los Issues main-broken (M12)


@dataclass(frozen=True)
class _Label:
    label: str
    action: str  # labeled o unlabeled
    actor: str
    at: datetime
    pr: int | None  # None: en el Issue de la tarea


def _event(repo: str, at: datetime | None, event: str, actor: str | None, role: str | None, *,
           task: int | None = None, pr: int | None = None, label: str | None = None,
           detail: str | None = None) -> dict[str, object]:
    return {"schema_version": SCHEMA_VERSION, "repo": repo, "occurred_at": at, "event": event, "actor": actor or None,
            "actor_role": role, "task": task, "pr": pr, "label": label, "detail": detail}


def _claim_commit(commits: Iterable[CommitInfo], number: int) -> CommitInfo | None:
    subject = claim_subject(number)
    found = [c for c in commits if c.message.split("\n", 1)[0].strip() == subject]
    return found[-1] if found else None


def _overlap(labels: Sequence[_Label], flagged_now: bool) -> str | None:
    """``overlap_outcome`` de M6: ``confirmed``, ``missed``, ``pending``, ``dismissed`` o ``None``."""
    acks = [x.at for x in labels if x.label == ACK_OVERLAP and x.action == "labeled"]
    flags = [x.at for x in labels if x.label == FLAG_OVERLAP and x.action == "labeled"]
    if acks:
        return "confirmed" if any(f <= min(acks) for f in flags) else "missed"
    if flags:
        return "pending" if flagged_now else "dismissed"
    return None


def _hook_denials(body: str) -> int | None:
    found = _HOOK_DENIALS.findall(body or "")
    return int(found[-1]) if found else None


def _task(forge: ForgeAdapter, snap: BaseSnapshot, repo: str, profile: str, issue: Issue,
          evidence: TaskEvidence, pulls: Sequence[PullRequest],
          reviews: Mapping[int, Sequence[Review]]) -> tuple[dict[str, object], list[dict[str, object]]]:
    """La fila de la tarea y sus eventos."""
    from .reviews import approval_at_merge

    roles = snap.roles
    n = issue.number
    s = evidence.snapshot
    derived = derive_state(s)
    events: list[dict[str, object]] = []

    def add(at: datetime | None, event: str, actor: str | None, **kw: object) -> None:
        if at is not None:
            events.append(_event(repo, at, event, actor, role_of(roles, actor), task=n, **kw))  # type: ignore[arg-type]

    labels = [_Label(e.label, e.action, e.actor, e.at, None) for e in s.label_events]
    commits: dict[int, list[CommitInfo]] = {}
    for pr in pulls:
        labels += [_Label(e.label, e.event, e.actor, e.created_at, pr.number) for e in forge.list_issue_events(pr.number)]
        commits[pr.number] = forge.list_pull_request_commits(pr.number)
    labels.sort(key=lambda x: _utc(x.at))

    add(issue.created_at, "task_created", issue.author)
    proposed = [x for x in labels if x.label == PROPOSED and x.pr is None]
    accepted = proposed[-1] if proposed and proposed[-1].action == "unlabeled" and PROPOSED not in issue.labels else None
    if accepted is not None:
        add(accepted.at, "task_accepted", accepted.actor)
    claims = [m for m in s.issue_markers if m.kind == "claim" and same_login(m.data.get("login"), m.author)]  # type: ignore[arg-type]
    live = live_claims(s)
    claim_commit = evidence.claim_commit or next(
        (c for c in (_claim_commit(commits[p.number], n) for p in reversed(pulls)) if c is not None), None)
    for m in claims:
        add(m.created_at, "claimed", m.author)
    if not claims and claim_commit is not None:
        add(claim_commit.committer_date, "claimed", claim_commit.author_login)
    for m in s.issue_markers:
        if m.kind == "release":
            add(m.created_at, "released", m.author)
    for x in labels:
        if x.label == EXPIRED and x.action == "labeled" and x.pr is None:
            add(x.at, "expired", x.actor)
        add(x.at, "label_added" if x.action == "labeled" else "label_removed", x.actor, pr=x.pr, label=x.label)
    cancelled = issue.closed_at if issue.state == "closed" and issue.state_reason == "not_planned" else None
    add(cancelled, "task_cancelled", None)

    chosen = next((p for p in pulls if p.number == derived.pr), None) or (pulls[-1] if pulls else None)
    for pr in pulls:
        add(pr.created_at, "pr_opened", pr.author, pr=pr.number)
        if not pr.draft:
            add(pr.created_at, "submitted", pr.author, pr=pr.number)
        for c in commits[pr.number]:
            add(c.committer_date, "commit", c.author_login, pr=pr.number)
        for r in reviews.get(pr.number, ()):
            if r.state in _REVIEW_EVENTS:
                add(r.submitted_at, _REVIEW_EVENTS[r.state], r.author, pr=pr.number)
        if pr.merged:
            full = forge.get_pull_request(pr.number)  # merged_by solo viene en GET pulls/{n} (08 §4.2)
            add(pr.merged_at, "merged", full.merged_by if full is not None else None, pr=pr.number)

    row: dict[str, object] = {
        "schema_version": SCHEMA_VERSION, "repo": repo, "profile": profile, "task": n, "state": derived.state,
        "sections": list(evidence.body.sections), "proposed_by": issue.author, "created_at": issue.created_at,
        "accepted_at": accepted.at if accepted is not None else None,
        "claimant": (claims[-1].author if claims else claim_commit.author_login if claim_commit is not None
                     else issue.assignees[0] if len(issue.assignees) == 1 else None),
        "claimed_at": (max(live, key=lambda m: _utc(m.created_at)).created_at if live
                       else claim_commit.committer_date if claim_commit is not None else None),
        "claims_count": len(claims),
        "expired_count": sum(1 for x in labels if x.label == EXPIRED and x.action == "labeled" and x.pr is None),
        "cancelled_at": cancelled,
        "flag_overlap": any(x.label == FLAG_OVERLAP and x.action == "labeled" for x in labels),
        "overlap_outcome": _overlap(labels, FLAG_OVERLAP in issue.labels or any(FLAG_OVERLAP in p.labels for p in pulls)),
        "flag_direction_change": any(x.label == FLAG_DIRECTION and x.action == "labeled" for x in labels),
    }
    row.update(dict.fromkeys(("pr", "pr_created_at", "submitted_at", "first_review_at", "first_reviewer_role",
                              "changes_requested_count", "approved_at", "merged_at", "merged_without_approval",
                              "hook_denials")))
    if chosen is not None:
        revs = sorted(reviews.get(chosen.number, ()), key=lambda r: (_utc(r.submitted_at or _EPOCH), r.id))
        first = next((r for r in revs if r.state in _REVIEW_EVENTS and r.submitted_at is not None
                      and not same_login(r.author, chosen.author)), None)
        merge = approval_at_merge(snap, chosen, forge.list_pull_request_files(chosen.number), revs)
        row.update({
            "pr": chosen.number,
            "pr_created_at": chosen.created_at,
            "submitted_at": None if chosen.draft else chosen.created_at,
            "first_review_at": first.submitted_at if first is not None else None,
            "first_reviewer_role": role_of(roles, first.author) if first is not None else None,
            "changes_requested_count": sum(1 for r in revs if r.state == "CHANGES_REQUESTED"),
            "approved_at": merge.approved_at,
            "merged_at": chosen.merged_at if chosen.merged else None,
            "merged_without_approval": (not (merge.approval.ok or merge.no_deputy)) if chosen.merged else None,
            "hook_denials": _hook_denials(chosen.body),
        })
    row["t_delivery_h"] = _hours(row["merged_at"], row["claimed_at"])  # type: ignore[arg-type]
    row["t_review_h"] = _hours(row["first_review_at"], row["submitted_at"])  # type: ignore[arg-type]
    return row, events


def role_events(git: Git, rev: str, repo: str) -> list[dict[str, object]]:
    """``role_added`` y ``role_removed`` de las versiones de ``roles.yaml`` en la base.

    Una versión inválida no cambia quién está; la fecha es la del committer.
    """
    from ..config.roles import parse_roles

    proc = git.run("--literal-pathspecs", "log", "--first-parent", "--reverse", f"--max-count={MAX_ROLE_VERSIONS}",
                   "--format=%H %cI", rev, "--", ROLES_FILE, check=False)
    if proc.returncode != 0:
        return []
    previous: dict[str, tuple[str, str]] = {}
    out: list[dict[str, object]] = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        sha, _, stamp = line.partition(" ")
        try:
            when = _utc(datetime.fromisoformat(stamp.strip()))
        except ValueError:
            continue
        raw = git.show(sha, ROLES_FILE)
        current: dict[str, tuple[str, str]] = {}
        if raw is not None:
            roles, _findings = parse_roles(raw, file=ROLES_FILE)
            if roles is None:
                continue
            for p in roles.people:
                role = role_of(roles, p.login)
                if role is not None:
                    current[p.login.casefold()] = (p.login, role)
        out += [_event(repo, when, "role_added", login, role) for k, (login, role) in current.items() if k not in previous]
        out += [_event(repo, when, "role_removed", login, role) for k, (login, role) in previous.items() if k not in current]
        previous = current
    return out


def collect(forge: ForgeAdapter, git: Git, snap: BaseSnapshot) -> Export:
    """Lee la forja y la historia de la base y arma las filas de ``tasks`` y ``events``."""
    caps = forge.capabilities()
    repo = f"{caps.repo.owner}/{caps.repo.name}"
    authz = authz_from_roles(snap.roles)
    base = snap.project.base_branch
    refs = {r.branch: r for r in forge.list_refs("task/")}
    by_branch: dict[str, list[PullRequest]] = {}
    for pr in forge.list_pull_requests(state="all"):
        if pr.same_repo:
            by_branch.setdefault(pr.head_branch, []).append(pr)
    tasks: list[dict[str, object]] = []
    events: list[dict[str, object]] = []
    for issue in sorted(forge.list_issues(labels=[TASK_LABEL], state="all"), key=lambda i: i.number):
        branch = task_branch(issue.number)
        pulls = sorted(by_branch.get(branch, []), key=lambda p: (_utc(p.created_at or p.updated_at), p.number))
        reviews = {p.number: forge.list_reviews(p.number) for p in pulls}
        evidence = collect_task(forge, issue.number, authz, base_branch=base,
                                prefetch=Prefetch(issue=issue, ref=refs.get(branch), pulls=tuple(pulls), reviews=reviews))
        if evidence is None:
            continue
        row, own = _task(forge, snap, repo, caps.profile, issue, evidence, pulls, reviews)
        tasks.append(row)
        events += own
    broken = sorted(forge.list_issues(labels=[MAIN_BROKEN], state="all"), key=lambda i: i.number)
    for issue in broken:
        events.append(_event(repo, issue.created_at, "main_broken_opened", issue.author, role_of(snap.roles, issue.author),
                             detail=str(issue.number)))
        if issue.closed_at is not None:
            events.append(_event(repo, issue.closed_at, "main_broken_closed", None, None, detail=str(issue.number)))
    events += role_events(git, snap.sha or snap.ref, repo)
    events.sort(key=lambda e: (_utc(e["occurred_at"]), str(e["event"]), e["task"] or 0, e["pr"] or 0,  # type: ignore[arg-type]
                               str(e["label"] or ""), str(e["actor"] or "")))
    return Export(tasks, events, broken)


# -- el resumen y el CSV ------------------------------------------------------------------------


def _median(values: Sequence[float]) -> float | None:
    return round(float(statistics.median(values)), 2) if values else None


def _p90(values: Sequence[float]) -> float | None:
    """Percentil 90 por rango más cercano."""
    if not values:
        return None
    ordered = sorted(values)
    return round(float(ordered[math.ceil(0.9 * len(ordered)) - 1]), 2)


def _metric(metric_id: str, value: float | int | None, unit: str) -> dict[str, object]:
    return {"id": metric_id, "value": value, "unit": unit}


def summary(tasks: Sequence[Mapping[str, object]], broken: Sequence[Issue]) -> list[dict[str, object]]:
    """``data.metrics``: lo que sale de las tablas sin el calendario del piloto (12-mvp §5.2)."""

    def values(column: str, rows: Sequence[Mapping[str, object]] = tasks) -> list[float]:
        return [float(v) for r in rows if isinstance(v := r.get(column), (int, float)) and not isinstance(v, bool)]

    merged = [r for r in tasks if r.get("merged_at") is not None]
    outcomes = [r.get("overlap_outcome") for r in tasks]
    confirmed, dismissed, missed = (outcomes.count(x) for x in ("confirmed", "dismissed", "missed"))
    rounds = values("changes_requested_count", merged)
    closed = [h for i in broken if (h := _hours(i.closed_at, i.created_at)) is not None]
    return [
        _metric("M1", _median(values("t_delivery_h")), "h"),
        _metric("M2_median", _median(values("t_review_h")), "h"),
        _metric("M2_p90", _p90(values("t_review_h")), "h"),
        _metric("M3", round(statistics.fmean(rounds), 2) if rounds else None, "count"),
        _metric("M6", round(dismissed / (confirmed + dismissed), 2) if confirmed + dismissed else None, "ratio"),
        _metric("M6_missed", missed, "count"),
        _metric("M7", confirmed + missed, "count"),
        _metric("M8", _median(values("hook_denials")), "count"),
        _metric("M11", sum(1 for r in merged if r.get("merged_without_approval") is True), "count"),
        _metric("M12_count", len(broken), "count"),
        _metric("M12_median", _median(closed), "h"),
    ]


def _cell(column: str, value: object, key: bytes | None) -> str:
    if value is None or value == "":
        return ""
    if column in PSEUDONYMIZED:
        return pseudonym(key, str(value)) if key is not None else str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return _iso(value) or ""
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, (list, tuple)):
        return ";".join(str(x) for x in value)
    return str(value)


def render_csv(columns: Sequence[str], rows: Iterable[Mapping[str, object]], key: bytes | None) -> str:
    """El CSV de REQ-MVP-53: encabezado, fechas ISO 8601 UTC, listas con ``;`` y celdas vacías."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(c, row.get(c), key) for c in columns])
    return buf.getvalue()


# -- la operación -------------------------------------------------------------------------------


def run(ctx: OpContext, *, since: object = None, until: object = None, table: object = "tasks",
        output: object = None, no_pseudonymize: object = False) -> Result:
    """``metrics [--since <fecha>] [--until <fecha>] [--table tasks|events] [--output <f>] [--no-pseudonymize]``."""
    lo_day, hi_day = _day("since", since), _day("until", until)
    if table not in TABLES:
        raise _bad("table", "enum")
    if output is not None and (not isinstance(output, str) or not output.strip()):
        raise _bad("output")
    if not isinstance(no_pseudonymize, bool):
        raise _bad("no_pseudonymize")
    t = ctx.t()
    lo = datetime.combine(lo_day, time.min, tzinfo=timezone.utc) if lo_day is not None else None
    hi = datetime.combine(hi_day, time(23, 59, 59), tzinfo=timezone.utc) if hi_day is not None else _utc(ctx.inv.now())
    if lo is not None and lo > hi:
        raise _bad("until", "range")
    key: bytes | None = None
    if not no_pseudonymize:
        raw = ctx.inv.env.get(SALT_ENV, "").strip()
        if not _SALT.fullmatch(raw):
            return _salt_error(t, ctx.inv.env)
        key = bytes.fromhex(raw)
    ctx.project()
    snap = ctx.base()
    warnings: list[WarningObj] = []
    if snap.fallback_sha is not None:
        warnings.append(make_warning("W_CONFIG_POLICY_FALLBACK", t, details=snap.fallback_details()))
    export = collect(ctx.forge(), ctx.git(), snap)
    tasks = [r for r in export.tasks if _within(r["created_at"], lo, hi)]  # type: ignore[arg-type]
    if isinstance(output, str) and not ctx.inv.dry_run:
        if table == "tasks":
            text = render_csv(TASK_COLUMNS, tasks, key)
        else:
            text = render_csv(EVENT_COLUMNS, (e for e in export.events if _within(e["occurred_at"], lo, hi)), key)  # type: ignore[arg-type]
        try:
            (Path(ctx.inv.cwd) / output).write_text(text, encoding="utf-8", newline="")
        except OSError:
            raise _bad("output", "unwritable") from None
    broken = [i for i in export.broken if _within(i.created_at, lo, hi)]
    return ok(OP, {"since": _iso(lo), "until": _iso(hi), "metrics": summary(tasks, broken)}, warnings)


def render_text(result: Result, t: Translator, *, fmt: str = "text") -> str:
    """El intervalo y una línea por métrica."""
    del fmt
    data = result.data if isinstance(result.data, dict) else {}
    if "metrics" not in data:
        return ""
    since, until = data.get("since"), data.get("until") or "-"
    lines = [t("metrics.text.range", since=since, until=until) if since else t("metrics.text.range_all", until=until)]
    for m in data["metrics"]:
        value = m.get("value")
        shown = "-" if value is None else (f"{value:.2f}" if isinstance(value, float) else str(value))
        lines.append(t("metrics.text.line", id=m.get("id"), label=t(f"metrics.label.{m.get('id')}"), value=shown,
                       unit=t(f"metrics.unit.{m.get('unit')}")).rstrip())
    return "\n".join(lines)
