# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Estado calculado de una tarea: transcripción de 04-tareas §1.5.

``derive_state`` es la función pura de D-11 (REQ-TASK-3, REQ-TASK-4): recibe la
instantánea que arma ``build_task_snapshot`` (H3) y no hace E/S ni lee el reloj
(REQ-ARCH-18). Todas las fechas de una instantánea deben ser del mismo tipo
(con zona UTC, como las da la forja).

Diferencias con el texto de 04 §1.5, todas de robustez o de REQ-FORGE-46:

- ``same_login`` sale de ``domain.text``, no de ``forge``.
- Las comparaciones de logins de ``last_submission_at`` y ``review_outcome``
  usan ``same_login`` en vez de ``==`` o ``in`` (REQ-FORGE-46, REQ-SCHEMA-12).
- Los campos de ``Marker.data`` vienen de JSON escrito por personas: un
  ``login`` que no es cadena no identifica a nadie, unas ``sections`` que no son
  lista cuentan como vacías y un ``rounds`` mal formado cae a ``created_at``.
  Así ``derive_state`` es total.
- La constante del asunto del commit de reclamo vive solo en el módulo del
  nombre (``_meta.CLAIM_SUBJECT``, D-03); este módulo no la necesita.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, get_args

from .text import same_login

__all__ = [
    "ACTIVE",
    "ANOMALIES",
    "CI_LABELS",
    "CI_MARKER_WINDOW",
    "OWNER_PRIORITY",
    "STATES",
    "Authz",
    "Derived",
    "LabelEvent",
    "Marker",
    "Pull",
    "Review",
    "State",
    "TaskRef",
    "TaskSnapshot",
    "accepted",
    "accepted_by",
    "blocked_ok",
    "derive_state",
    "effective_sections",
    "expiry_markers",
    "label_ok",
    "last_submission_at",
    "live_claims",
    "resolve_owner",
    "review_outcome",
]

State = Literal[
    "proposed", "open", "claimed", "in_review",
    "changes_requested", "merged", "expired", "cancelled",
]
STATES: frozenset[str] = frozenset(get_args(State))
ACTIVE = frozenset({"claimed", "in_review", "changes_requested", "expired"})
OWNER_PRIORITY = ("activity", "claim_commit", "marker", "assignee")
CI_LABELS = frozenset({"state:expired", "flag:blocked"})
CI_MARKER_WINDOW = timedelta(minutes=10)  # etiqueta de CI <-> marcador `expiry` (§1.3)
_COUNTED_REVIEWS = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")

# Lista cerrada de anomalías que puede emitir derive_state (§1.5, §1.6; REQ-TASK-14).
ANOMALIES: frozenset[str] = frozenset({
    "forged_marker",
    "fork_pr_ignored",
    "reopened_after_merge",
    "issue_open_after_merge",
    "closed_without_merged_pr",
    "ref_on_closed_issue",
    "open_pr_on_closed_issue",
    "missing_acceptance",
    "assignee_without_ref",
    "stale_expired_label",
    "open_pr_without_ref",
    "ref_on_proposed",
    "ref_without_owner",
    "owner_sources_disagree",
    "claim_ref_replaced",
    "claim_without_marker",
    "spec_changed_after_claim",
    "assignees_mismatch",
    "unauthorized_label",
    "pr_closed_unmerged",
    "multiple_open_prs",
    "pr_wrong_base",
    "draft_pr",
    "submitted_outside_core",
})


@dataclass(frozen=True)
class LabelEvent:
    label: str
    action: str
    actor: str
    at: datetime


@dataclass(frozen=True)
class Marker:
    kind: str
    author: str
    created_at: datetime
    updated_at: datetime
    data: dict
    ci_verified: bool = False  # lo fija build_task_snapshot (§1.5)


@dataclass(frozen=True)
class Review:
    author: str
    state: str
    submitted_at: datetime | None


@dataclass(frozen=True)
class Pull:
    number: int
    state: str
    merged: bool
    draft: bool
    author: str
    base_ref: str
    same_repo: bool
    created_at: datetime
    reviews: tuple[Review, ...]  # orden cronológico (REST)
    markers: tuple[Marker, ...]


@dataclass(frozen=True)
class TaskRef:
    head_sha: str
    claim_commit_sha: str | None
    claim_commit_author: str | None
    created_by: str | None
    last_push_at: datetime | None  # H-TASK-2


@dataclass(frozen=True)
class Authz:
    lead: str
    deputy: str | None
    ci_actors: frozenset[str]
    section_owner: dict[str, str]

    def lead_like(self) -> set[str]:
        return {x for x in (self.lead, self.deputy) if x}


@dataclass(frozen=True)
class TaskSnapshot:
    number: int
    issue_author: str
    issue_state: str
    state_reason: str | None
    labels: frozenset[str]
    label_events: tuple[LabelEvent, ...]
    assignees: tuple[str, ...]
    sections: tuple[str, ...]  # TaskSpec actual (03-esquemas §8.3)
    spec_hash: str
    issue_markers: tuple[Marker, ...]
    ref: TaskRef | None
    pulls: tuple[Pull, ...]
    base_branch: str
    authz: Authz


@dataclass(frozen=True)
class Derived:
    state: State
    owner: str | None
    owner_source: str | None
    pr: int | None
    approved: bool
    anomalies: tuple[str, ...]
    accepted_by: str | None  # actor del último `unlabeled` válido de `state:proposed`


def _in(login: str | None, group: Iterable[str | None]) -> bool:
    return login is not None and any(x and same_login(login, x) for x in group)


def _last(events: Sequence[LabelEvent], label: str, action: str) -> LabelEvent | None:
    ev = [e for e in events if e.label == label and e.action == action]
    return ev[-1] if ev else None


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _parse_time(raw: object, like: datetime) -> datetime | None:
    """ISO 8601 de un marcador, con el mismo tipo de fecha que ``like``."""
    if not isinstance(raw, str):
        return None
    try:
        t = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if t.tzinfo is None and like.tzinfo is not None:
        return t.replace(tzinfo=timezone.utc)  # la forja da UTC
    if t.tzinfo is not None and like.tzinfo is None:
        return t.astimezone(timezone.utc).replace(tzinfo=None)
    return t


def accepted_by(s: TaskSnapshot) -> str | None:  # REQ-TASK-8
    ev = [e for e in s.label_events if e.label == "state:proposed"]
    if ev and ev[-1].action == "unlabeled" and _in(ev[-1].actor, s.authz.lead_like()):
        return ev[-1].actor  # sin `labeled` posterior
    return None


def accepted(s: TaskSnapshot) -> bool:
    return accepted_by(s) is not None


def expiry_markers(s: TaskSnapshot) -> tuple[list[Marker], list[str]]:
    ok: list[Marker] = []
    an: list[str] = []  # §1.3, fila marcador `expiry`
    for m in s.issue_markers:
        if m.kind != "expiry":
            continue
        if ((m.ci_verified and _in(m.author, s.authz.ci_actors))
                or _in(m.author, s.authz.lead_like())):
            ok.append(m)
        else:
            an.append("forged_marker")
    return ok, an


def label_ok(s: TaskSnapshot, label: str, allowed: set[str]) -> bool:
    ev = _last(s.label_events, label, "labeled")
    if label not in s.labels or ev is None:
        return False
    if _in(ev.actor, allowed):
        return True
    if label in CI_LABELS and _in(ev.actor, s.authz.ci_actors):  # CI: exige marcador verificado
        return any(m.ci_verified and abs(m.updated_at - ev.at) <= CI_MARKER_WINDOW
                   for m in expiry_markers(s)[0])
    return False


def blocked_ok(s: TaskSnapshot, owner: str | None) -> bool:  # §1.3, fila `flag:blocked`
    if label_ok(s, "flag:blocked", s.authz.lead_like()):
        return True
    ev = _last(s.label_events, "flag:blocked", "labeled")
    return ("flag:blocked" in s.labels and ev is not None and _in(ev.actor, {owner})
            and any(m.kind == "escalation" and same_login(m.author, owner) and m.created_at >= ev.at
                    for m in s.issue_markers))


def _claims(s: TaskSnapshot) -> list[Marker]:
    """Marcadores `claim` no anulados cuyo `login` es el autor del comentario."""
    ll = s.authz.lead_like()
    releases = [m for m in s.issue_markers if m.kind == "release"]

    def voided(c: Marker) -> bool:
        return any(r.created_at > c.created_at
                   and (same_login(r.author, c.author) or _in(r.author, ll)) for r in releases)

    return [m for m in s.issue_markers if m.kind == "claim"
            and same_login(_str(m.data.get("login")), m.author) and not voided(m)]


def live_claims(s: TaskSnapshot) -> list[Marker]:
    """Los reclamos vigentes (04 §1.4), en orden: los marcadores `claim` sin `release` posterior."""
    return _claims(s)


def resolve_owner(s: TaskSnapshot) -> tuple[str | None, str | None, list[str]]:
    """Dueño de una tarea con ref (§1.5); sin ref no hay dueño ni anomalías."""
    if s.ref is None:
        return None, None, []
    live = _claims(s)
    cur = s.ref.claim_commit_sha
    valid = [m for m in live if cur is None or m.data.get("claim_sha") == cur]
    found: dict[str, str] = {}
    if s.ref.created_by:
        found["activity"] = s.ref.created_by
    if s.ref.claim_commit_author:
        found["claim_commit"] = s.ref.claim_commit_author
    if valid:
        found["marker"] = min(valid, key=lambda m: m.created_at).author  # el primero
    if len(s.assignees) == 1:
        found["assignee"] = s.assignees[0]
    if not found:
        return None, None, ["ref_without_owner"]
    source = next(k for k in OWNER_PRIORITY if k in found)
    owner = found[source]
    if any(not same_login(o, owner) for o in found.values()):
        return None, None, ["owner_sources_disagree"]  # solo el `lead` libera
    if cur is not None and any(m.data.get("claim_sha") != cur and not same_login(m.author, owner)
                               for m in live):
        return None, None, ["claim_ref_replaced"]  # la ref se rehízo sin `release`
    return owner, source, ([] if "marker" in found else ["claim_without_marker"])


def effective_sections(s: TaskSnapshot, owner: str | None) -> tuple[tuple[str, ...], list[str]]:
    """claim.sections ∩ TaskSpec.sections actuales (§3, REQ-TASK-27)."""
    cur = s.ref.claim_commit_sha if s.ref else None
    mine = [m for m in _claims(s) if _in(m.author, {owner})
            and (cur is None or m.data.get("claim_sha") == cur)]
    if not mine:
        return s.sections, []  # sin marcador: `claim_without_marker`
    c = min(mine, key=lambda m: m.created_at)
    raw = c.data.get("sections", [])
    declared = {x for x in raw if isinstance(x, str)} if isinstance(raw, (list, tuple)) else set()
    an = [] if c.data.get("spec_hash") == s.spec_hash else ["spec_changed_after_claim"]
    return tuple(x for x in s.sections if x in declared), an


def _submitted_at(m: Marker) -> datetime:
    """Fecha de la última ronda de un marcador `submission`; si falta, su creación."""
    rounds = m.data.get("rounds", [])
    if isinstance(rounds, list) and rounds and isinstance(rounds[-1], Mapping):
        t = _parse_time(rounds[-1].get("submitted_at"), m.created_at)
        if t is not None:
            return t
    return m.created_at


def last_submission_at(pr: Pull, owner: str | None) -> tuple[datetime, bool]:
    subs = [m for m in pr.markers if m.kind == "submission" and same_login(m.author, owner)]
    if not subs:
        return pr.created_at, False
    # nunca después de la última edición del marcador
    return min(_submitted_at(subs[-1]), subs[-1].updated_at), True


def review_outcome(s: TaskSnapshot, pr: Pull, owner: str | None) -> tuple[str, bool, list[str]]:
    ll = s.authz.lead_like()
    candidates = ll | {s.authz.section_owner.get(x) for x in s.sections}
    who = {x for x in candidates if x and not same_login(x, owner)}
    latest: dict[str, Review] = {}
    for r in pr.reviews:
        if _in(r.author, who) and r.state in _COUNTED_REVIEWS:
            latest[r.author.strip().casefold()] = r
    t_sub, via_core = last_submission_at(pr, owner)
    an = [] if via_core else ["submitted_outside_core"]
    blocking = [r for r in latest.values()
                if r.state == "CHANGES_REQUESTED" and r.submitted_at and r.submitted_at > t_sub]
    approved = not blocking and any(r.state == "APPROVED" and _in(r.author, ll)
                                    for r in latest.values())
    return ("changes_requested" if blocking else "in_review"), approved, an


def derive_state(s: TaskSnapshot) -> Derived:
    an: list[str] = []
    ll = s.authz.lead_like()
    acc = accepted_by(s)
    an += expiry_markers(s)[1]  # `forged_marker`
    mine = [p for p in s.pulls if p.same_repo]
    if len(mine) != len(s.pulls):
        an.append("fork_pr_ignored")
    merged = [p for p in mine if p.merged]
    open_prs = [p for p in mine if p.state == "open"]

    if merged:  # 1. integrada
        if s.issue_state == "open":
            an.append("reopened_after_merge" if s.state_reason == "reopened"
                      else "issue_open_after_merge")
        return Derived("merged", merged[-1].author, "pr_author", merged[-1].number, True,
                       tuple(an), acc)
    if s.issue_state == "closed":  # 2. cancelada
        if s.state_reason not in ("not_planned", "duplicate"):
            an.append("closed_without_merged_pr")
        if s.ref is not None:
            an.append("ref_on_closed_issue")
        if open_prs:
            an.append("open_pr_on_closed_issue")
        return Derived("cancelled", None, None, None, False, tuple(an), acc)
    if s.ref is None:  # 3. sin reclamo
        if "state:proposed" in s.labels or acc is None:
            if "state:proposed" not in s.labels:
                an.append("missing_acceptance")
            return Derived("proposed", None, None, None, False, tuple(an), acc)
        if s.assignees:
            an.append("assignee_without_ref")
        if "state:expired" in s.labels:
            an.append("stale_expired_label")
        if open_prs:
            an.append("open_pr_without_ref")
        return Derived("open", None, None, None, False, tuple(an), acc)

    if "state:proposed" in s.labels:  # 4. con reclamo
        an.append("ref_on_proposed")
    elif acc is None:
        an.append("missing_acceptance")
    owner, source, oan = resolve_owner(s)
    an += oan
    an += effective_sections(s, owner)[1]  # `spec_changed_after_claim`
    if owner is not None and not (len(s.assignees) == 1 and same_login(s.assignees[0], owner)):
        an.append("assignees_mismatch")
    if "flag:blocked" in s.labels and not blocked_ok(s, owner):
        an.append("unauthorized_label")
    if any(p.state == "closed" and not p.merged for p in mine):
        an.append("pr_closed_unmerged")
    to_base = [p for p in open_prs if p.base_ref == s.base_branch]
    if len(open_prs) > 1:
        an.append("multiple_open_prs")
    if open_prs and not to_base:
        an.append("pr_wrong_base")
    pr = min(to_base, key=lambda p: p.created_at) if to_base else None

    approved = False
    if pr is None or pr.draft:
        if pr is not None:
            an.append("draft_pr")
        base_state = "claimed"
    else:
        base_state, approved, ran = review_outcome(s, pr, owner)
        an += ran
    exp_ok = label_ok(s, "state:expired", ll)  # CI solo con marcador verificado
    if "state:expired" in s.labels and not exp_ok:
        an.append("unauthorized_label")
    state: State
    if exp_ok and base_state in ("claimed", "changes_requested"):
        state = "expired"
    else:
        state = base_state  # type: ignore[assignment]
        if exp_ok:
            an.append("stale_expired_label")
    return Derived(state, owner, source, pr.number if pr else None, approved, tuple(an), acc)
