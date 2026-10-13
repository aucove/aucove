# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""T1: ``derive_state`` y sus auxiliares (04-tareas §1.2-1.6; 12-mvp §3.2).

- REQ-MVP-25: una fila por transición de 04 §1.2 (``TRANSITIONS``) y por
  combinación inválida de 04 §1.6 y §1.5 (``INVALID``); pruebas de cobertura de
  la tabla comprueban que están todas las transiciones, estados y anomalías.
- REQ-MVP-24: toda prueba corre con ``open`` y ``socket`` bloqueados por
  monkeypatch (fixture autouse ``_no_io``).
- Invariantes de Hypothesis: total, determinista y ``merged`` si y solo si hay
  un PR integrado del mismo repo.
"""

import ast
import builtins
import os
import socket
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings

from aucove.domain import tasks
from aucove.domain.tasks import (
    ACTIVE,
    ANOMALIES,
    STATES,
    Derived,
    Marker,
    Pull,
    Review,
    TaskSnapshot,
    accepted,
    accepted_by,
    derive_state,
    effective_sections,
    last_submission_at,
    resolve_owner,
)
from tests.factories import (
    CI_ACTOR,
    CLAIM_SHA,
    DEPUTY,
    LEAD,
    OLD_CLAIM_SHA,
    OTHER,
    OTHER_SPEC_HASH,
    OWNER,
    PR_NUMBER,
    IOBlocked,
    at,
    block_io,
    claim_marker,
    expiry_marker,
    io_guard,
    make_authz,
    marker,
    package_dirs,
    pull,
    release_marker,
    review,
    snap,
    snapshots,
    submission_marker,
    task_ref,
)


@pytest.fixture(autouse=True)
def _no_io(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    io_guard(request, monkeypatch)  # REQ-MVP-24


# --- Constructores de las filas ------------------------------------------------------

PROPOSED_EV = ("state:proposed", "labeled", "other")   # lo pone el formulario (cualquiera)
ACCEPT = ("state:proposed", "unlabeled", "lead")       # aceptación (REQ-TASK-8)
EXPIRE_CI = ("state:expired", "labeled", "ci")          # con marcador `expiry` verificado


def proposed(**kw: Any) -> TaskSnapshot:
    labels = {"task", "state:proposed", *kw.pop("extra_labels", ())}
    return snap(labels=labels, label_events=[PROPOSED_EV, *kw.pop("events", ())], **kw)


def opened(**kw: Any) -> TaskSnapshot:
    labels = {"task", *kw.pop("extra_labels", ())}
    return snap(labels=labels, label_events=[PROPOSED_EV, ACCEPT, *kw.pop("events", ())], **kw)


def claimed(**kw: Any) -> TaskSnapshot:
    kw.setdefault("ref", True)
    return opened(**kw)


def in_review(**kw: Any) -> TaskSnapshot:
    kw.setdefault("pr", "ready")
    return claimed(**kw)


def changes(**kw: Any) -> TaskSnapshot:
    kw.setdefault("reviews", ["CHANGES_REQUESTED"])
    return in_review(**kw)


def expired(base: Any = claimed, **kw: Any) -> TaskSnapshot:
    return base(extra_labels=["state:expired", *kw.pop("extra_labels", ())],
                events=[EXPIRE_CI, *kw.pop("events", ())], **kw)


def resubmitted_pr(*, updated: int = 50, last_round: int = 50) -> Pull:
    """PR con una segunda ronda de entrega en ``at(last_round)``."""
    sub = submission_marker(when=at(30), updated=at(updated),
                            rounds=[{"submitted_at": at(30).isoformat()},
                                    {"submitted_at": at(last_round).isoformat()}])
    return pull("ready", reviews=["CHANGES_REQUESTED"], markers=(sub,))


def pr_with_rounds(rounds: object, *, updated: int = 30) -> Pull:
    sub = submission_marker(when=at(30), updated=at(updated), rounds=rounds)
    return pull("ready", reviews=["CHANGES_REQUESTED"], markers=(sub,))


_ANY = object()


def row(case_id: str, s: TaskSnapshot, state: str, anomalies: tuple[str, ...] = (), *,
        owner: object = _ANY, source: object = _ANY, pr: object = _ANY,
        approved: object = _ANY, accepted_by: object = _ANY) -> Any:
    checks = {"owner": owner, "owner_source": source, "pr": pr, "approved": approved,
              "accepted_by": accepted_by}
    return pytest.param(s, state, anomalies, {k: v for k, v in checks.items() if v is not _ANY},
                        id=case_id)


def check(d: Derived, state: str, anomalies: tuple[str, ...], checks: dict[str, object]) -> None:
    assert d.state == state
    assert d.anomalies == anomalies
    for field, value in checks.items():
        assert getattr(d, field) == value, field


# --- Filas de estado (ejemplo ilustrativo de 12-mvp §3.2 y algunas más) --------------

STATE_ROWS = [
    row("proposed", snap(labels={"task", "state:proposed"}), "proposed"),
    row("open", snap(labels={"task"}, label_events=[("state:proposed", "unlabeled", "lead")]),
        "open", accepted_by=LEAD),
    row("open-deputy", snap(labels={"task"},
                            label_events=[("state:proposed", "unlabeled", "deputy")]),
        "open", accepted_by=DEPUTY),
    row("claimed", snap(labels={"task"}, ref=True), "claimed", ("missing_acceptance",)),
    row("claimed-accepted", claimed(), "claimed", owner=OWNER, source="activity", pr=None,
        approved=False, accepted_by=LEAD),
    row("in-review", snap(labels={"task"}, ref=True, pr="ready"), "in_review",
        ("missing_acceptance",), pr=PR_NUMBER),
    row("in-review-accepted", in_review(), "in_review", owner=OWNER, pr=PR_NUMBER,
        approved=False),
    row("changes", snap(labels={"task"}, ref=True, pr="ready", reviews=["CHANGES_REQUESTED"]),
        "changes_requested", ("missing_acceptance",)),
    row("merged", snap(labels={"task"}, pr="merged", issue="closed"), "merged",
        owner=OWNER, source="pr_author", pr=PR_NUMBER, approved=True),
    row("expired", snap(labels={"task", "state:expired"}, ref=True,
                        label_events=[("state:expired", "labeled", CI_ACTOR)]),
        "expired", ("missing_acceptance",), owner=OWNER),
    row("expired-lead", claimed(extra_labels=["state:expired"],
                                events=[("state:expired", "labeled", "lead")]), "expired"),
    row("expired-deputy", claimed(extra_labels=["state:expired"],
                                  events=[("state:expired", "labeled", "deputy")]), "expired"),
    row("cancelled", snap(labels={"task"}, issue="closed", state_reason="not_planned"),
        "cancelled", owner=None, pr=None),
    row("cancelled-duplicate", opened(issue="closed", state_reason="duplicate"), "cancelled"),
]


@pytest.mark.parametrize(("s", "state", "anomalies", "checks"), STATE_ROWS)
def test_states(s: TaskSnapshot, state: str, anomalies: tuple[str, ...],
                checks: dict[str, object]) -> None:
    check(derive_state(s), state, anomalies, checks)


# --- Transiciones de 04 §1.2 (antes -> acción -> después) -----------------------------

def transition(case_id: str, frm: str, to: str, before: TaskSnapshot,
               after: TaskSnapshot, anomalies: tuple[str, ...] = ()) -> Any:
    return pytest.param(frm, to, before, after, anomalies, id=case_id)


CLOSE = {"issue": "closed", "state_reason": "not_planned"}      # task cancel <n>
RELEASED = {"markers": (claim_marker(), release_marker())}       # release <n>: la ref se archiva

TRANSITIONS = [
    transition("accept", "proposed", "open", proposed(), opened()),
    transition("accept-deputy-web", "proposed", "open", proposed(),
               snap(labels={"task"},
                    label_events=[PROPOSED_EV, ("state:proposed", "unlabeled", "deputy")])),
    transition("cancel-proposed", "proposed", "cancelled", proposed(), proposed(**CLOSE)),
    transition("claim", "open", "claimed", opened(), claimed()),
    transition("cancel-open", "open", "cancelled", opened(), opened(**CLOSE)),
    transition("submit", "claimed", "in_review", claimed(), in_review()),
    transition("release-claimed", "claimed", "open", claimed(), opened(**RELEASED)),
    transition("expire-claimed", "claimed", "expired", claimed(), expired()),
    transition("cancel-claimed", "claimed", "cancelled", claimed(), opened(**CLOSE)),
    transition("request-changes", "in_review", "changes_requested", in_review(), changes()),
    transition("merge", "in_review", "merged", in_review(),
               in_review(pr="merged", issue="closed", state_reason="completed")),
    transition("release-in-review", "in_review", "open", in_review(),
               opened(pr="closed", **RELEASED)),
    transition("cancel-in-review", "in_review", "cancelled", in_review(),
               opened(pr="closed", **CLOSE)),
    transition("resubmit", "changes_requested", "in_review", changes(),
               claimed(pr=resubmitted_pr())),
    transition("expire-changes", "changes_requested", "expired", changes(), expired(changes)),
    transition("release-changes", "changes_requested", "open", changes(),
               opened(pr="closed", **RELEASED)),
    transition("cancel-changes", "changes_requested", "cancelled", changes(),
               opened(pr="closed", **CLOSE)),
    transition("renew-claimed", "expired", "claimed", expired(),
               claimed(events=[EXPIRE_CI, ("state:expired", "unlabeled", "owner")])),
    transition("renew-changes", "expired", "changes_requested", expired(changes),
               changes(events=[EXPIRE_CI, ("state:expired", "unlabeled", "owner")])),
    transition("release-expired", "expired", "open", expired(),
               opened(events=[EXPIRE_CI, ("state:expired", "unlabeled", "ci")], **RELEASED)),
    transition("cancel-expired", "expired", "cancelled", expired(), opened(**CLOSE)),
    transition("reopen-to-open", "cancelled", "open", opened(**CLOSE),
               opened(state_reason="reopened")),
    transition("reopen-to-proposed", "cancelled", "proposed", proposed(**CLOSE),
               proposed(state_reason="reopened")),
    transition("merged-is-terminal", "merged", "merged",
               in_review(pr="merged", issue="closed", state_reason="completed"),
               opened(pr="merged", state_reason="reopened", extra_labels=["state:expired"]),
               ("reopened_after_merge",)),
]

# Transiciones permitidas de la tabla de 04 §1.2 (merged es terminal).
REQUIRED_TRANSITIONS = {
    ("proposed", "open"), ("proposed", "cancelled"),
    ("open", "claimed"), ("open", "cancelled"),
    ("claimed", "in_review"), ("claimed", "open"), ("claimed", "expired"), ("claimed", "cancelled"),
    ("in_review", "changes_requested"), ("in_review", "merged"), ("in_review", "open"),
    ("in_review", "cancelled"),
    ("changes_requested", "in_review"), ("changes_requested", "expired"),
    ("changes_requested", "open"), ("changes_requested", "cancelled"),
    ("expired", "claimed"), ("expired", "changes_requested"), ("expired", "open"),
    ("expired", "cancelled"),
    ("cancelled", "open"), ("cancelled", "proposed"),
    ("merged", "merged"),
}


@pytest.mark.parametrize(("frm", "to", "before", "after", "anomalies"), TRANSITIONS)
def test_transitions(frm: str, to: str, before: TaskSnapshot, after: TaskSnapshot,
                     anomalies: tuple[str, ...]) -> None:
    b = derive_state(before)
    assert (b.state, b.anomalies) == (frm, ())
    a = derive_state(after)
    assert (a.state, a.anomalies) == (to, anomalies)


def test_every_transition_has_a_row() -> None:
    assert {(p.values[0], p.values[1]) for p in TRANSITIONS} == REQUIRED_TRANSITIONS


# --- Combinaciones inválidas (04 §1.5 y §1.6) -----------------------------------------

ESCALATION = marker("escalation", OWNER, when=at(20))
BLOCKED_BY = {name: ("flag:blocked", "labeled", name) for name in ("lead", "other", "owner", "ci")}
REVIEWER_OTHER = make_authz(section_owner={"methods": OWNER, "results": OTHER})
NAIVE = datetime(2026, 9, 29, 12, 30)

INVALID = [
    # 1. integrada
    row("merged-issue-open", in_review(pr="merged"), "merged", ("issue_open_after_merge",)),
    row("merged-reopened", in_review(pr="merged", state_reason="reopened"), "merged",
        ("reopened_after_merge",)),
    row("merged-ignores-labels-and-ref",
        claimed(pr="merged", issue="closed", extra_labels=["state:proposed", "state:expired"],
                events=[("state:expired", "labeled", "other")]), "merged"),
    row("merged-last-merged-pr",
        opened(issue="closed", pulls=[pull("merged", number=41, created_at=at(20)),
                                      pull("merged", number=43, author=OTHER)]),
        "merged", owner=OTHER, pr=43),
    row("merged-with-fork", in_review(pr="merged", issue="closed", pulls=[pull("fork", number=7)]),
        "merged", ("fork_pr_ignored",)),
    row("fork-merge-is-not-merged", opened(pr="fork_merged"), "open", ("fork_pr_ignored",)),
    row("merged-forged-expiry", in_review(pr="merged", issue="closed",
                                          markers=[expiry_marker(OTHER)]),
        "merged", ("forged_marker",)),
    # 2. cancelada
    row("closed-completed-without-pr", opened(issue="closed", state_reason="completed"),
        "cancelled", ("closed_without_merged_pr",)),
    row("closed-without-reason", opened(issue="closed"), "cancelled",
        ("closed_without_merged_pr",)),
    row("ref-on-closed-issue", claimed(**CLOSE), "cancelled", ("ref_on_closed_issue",)),
    row("open-pr-on-closed-issue", in_review(**CLOSE), "cancelled",
        ("ref_on_closed_issue", "open_pr_on_closed_issue")),
    row("closed-pr-on-closed-issue", opened(pr="closed", **CLOSE), "cancelled"),
    # 3. sin reclamo
    row("missing-acceptance", snap(labels={"task"}), "proposed", ("missing_acceptance",)),
    row("accepted-by-non-lead",
        snap(labels={"task"}, label_events=[PROPOSED_EV, ("state:proposed", "unlabeled", "other")]),
        "proposed", ("missing_acceptance",), accepted_by=None),
    row("accepted-case-insensitive",
        snap(labels={"task"}, label_events=[PROPOSED_EV, ("state:proposed", "unlabeled", "ANA")]),
        "open", accepted_by="ANA"),
    row("relabeled-after-acceptance", proposed(events=[ACCEPT, PROPOSED_EV]), "proposed",
        accepted_by=None),
    row("label-present-after-acceptance", proposed(events=[ACCEPT]), "proposed",
        accepted_by=LEAD),
    row("assignee-without-ref", opened(assignees=[OWNER]), "open", ("assignee_without_ref",)),
    row("stale-expired-label-open", opened(extra_labels=["state:expired"]), "open",
        ("stale_expired_label",)),
    row("open-pr-without-ref", opened(pr="ready"), "open", ("open_pr_without_ref",)),
    row("forged-expiry-open", opened(markers=[expiry_marker(OTHER)]), "open", ("forged_marker",)),
    row("verified-expiry-not-ci-actor", opened(markers=[expiry_marker(OTHER, ci_verified=True)]),
        "open", ("forged_marker",)),
    row("lead-expiry-marker-counts", claimed(markers=[expiry_marker(LEAD, ci_verified=False)]),
        "claimed"),
    # 4. con reclamo: aceptación y dueño
    row("ref-on-proposed", claimed(extra_labels=["state:proposed"]), "claimed",
        ("ref_on_proposed",)),
    row("ref-missing-acceptance", snap(labels={"task"}, ref=True), "claimed",
        ("missing_acceptance",)),
    row("ref-without-assignee", claimed(assignees=[]), "claimed", ("assignees_mismatch",),
        owner=OWNER, source="activity"),
    row("two-assignees", claimed(assignees=[OWNER, OTHER]), "claimed", ("assignees_mismatch",),
        owner=OWNER),
    row("assignee-case-insensitive", claimed(assignees=["Carla"]), "claimed", owner=OWNER),
    row("ref-without-owner", claimed(ref=task_ref(None), assignees=[]), "claimed",
        ("ref_without_owner",), owner=None, source=None),
    row("owner-from-assignee", claimed(ref=task_ref(None)), "claimed", ("claim_without_marker",),
        owner=OWNER, source="assignee"),
    row("two-assignees-never-decide", claimed(ref=task_ref(None), assignees=[OWNER, OTHER]),
        "claimed", ("ref_without_owner",), owner=None),
    row("owner-from-claim-commit", claimed(ref=task_ref(OWNER, created_by=None)), "claimed",
        ("claim_without_marker",), owner=OWNER, source="claim_commit"),
    row("owner-sources-disagree", claimed(ref=task_ref(OWNER, created_by=OTHER), claim=True),
        "claimed", ("owner_sources_disagree",), owner=None, source=None),
    row("owner-sources-agree-ignoring-case",
        claimed(ref=task_ref(OWNER, created_by="CARLA"), claim=True), "claimed",
        owner="CARLA", source="activity"),
    row("claim-ref-replaced",
        claimed(markers=[claim_marker(OTHER, when=at(5), claim_sha=OLD_CLAIM_SHA)]), "claimed",
        ("claim_ref_replaced",), owner=None),
    row("own-old-claim-is-not-replaced",
        claimed(markers=[claim_marker(OWNER, when=at(5), claim_sha=OLD_CLAIM_SHA)]), "claimed",
        owner=OWNER),
    row("claim-without-marker", claimed(claim=False), "claimed", ("claim_without_marker",),
        owner=OWNER),
    row("forged-claim-marker", claimed(claim=False, markers=[claim_marker(OTHER, login=OWNER)]),
        "claimed", ("claim_without_marker",)),
    row("claim-login-not-string", claimed(claim=False, markers=[claim_marker(OWNER, login=5)]),
        "claimed", ("claim_without_marker",)),
    row("claim-released-by-owner", claimed(markers=[release_marker(OWNER)]), "claimed",
        ("claim_without_marker",)),
    row("claim-released-by-lead", claimed(markers=[release_marker(LEAD)]), "claimed",
        ("claim_without_marker",)),
    row("release-by-other-ignored", claimed(markers=[release_marker(OTHER)]), "claimed"),
    row("release-before-claim-ignored", claimed(markers=[release_marker(OWNER, when=at(5))]),
        "claimed"),
    row("ref-without-claim-commit",
        claimed(ref=task_ref(OWNER, claim_commit_sha=None, claim_commit_author=None), claim=True),
        "claimed", owner=OWNER),
    row("earliest-valid-marker-owns",
        claimed(ref=task_ref(None), assignees=[], claim=True,
                markers=[claim_marker(OTHER, when=at(5))]),
        "claimed", ("assignees_mismatch",), owner=OTHER, source="marker"),
    row("spec-changed-after-claim",
        claimed(claim=False, markers=[claim_marker(OWNER, spec_hash=OTHER_SPEC_HASH)]),
        "claimed", ("spec_changed_after_claim",)),
    # 4. etiquetas
    row("blocked-by-lead", claimed(extra_labels=["flag:blocked"], events=[BLOCKED_BY["lead"]]),
        "claimed"),
    row("blocked-by-other", claimed(extra_labels=["flag:blocked"], events=[BLOCKED_BY["other"]]),
        "claimed", ("unauthorized_label",)),
    row("blocked-by-owner-with-escalation",
        claimed(extra_labels=["flag:blocked"], events=[BLOCKED_BY["owner"]], markers=[ESCALATION]),
        "claimed"),
    row("blocked-by-owner-without-escalation",
        claimed(extra_labels=["flag:blocked"], events=[BLOCKED_BY["owner"]]),
        "claimed", ("unauthorized_label",)),
    row("blocked-escalation-same-instant",
        claimed(extra_labels=["flag:blocked"],
                events=[("flag:blocked", "labeled", "owner", at(20))], markers=[ESCALATION]),
        "claimed"),
    row("blocked-escalation-before-label",
        claimed(extra_labels=["flag:blocked"],
                events=[("flag:blocked", "labeled", "owner", at(50))], markers=[ESCALATION]),
        "claimed", ("unauthorized_label",)),
    row("blocked-escalation-by-other",
        claimed(extra_labels=["flag:blocked"], events=[BLOCKED_BY["owner"]],
                markers=[marker("escalation", OTHER, when=at(20))]),
        "claimed", ("unauthorized_label",)),
    row("blocked-by-ci-verified",
        claimed(extra_labels=["flag:blocked"], events=[BLOCKED_BY["ci"]],
                markers=[expiry_marker(when=at(2))]),
        "claimed"),
    row("blocked-without-event", claimed(extra_labels=["flag:blocked"]), "claimed",
        ("unauthorized_label",)),
    row("blocked-without-owner",
        claimed(ref=task_ref(None), assignees=[], extra_labels=["flag:blocked"],
                events=[BLOCKED_BY["owner"]], markers=[ESCALATION]),
        "claimed", ("ref_without_owner", "unauthorized_label")),
    row("expired-by-other", claimed(extra_labels=["state:expired"],
                                    events=[("state:expired", "labeled", "other")]),
        "claimed", ("unauthorized_label",)),
    row("expired-ci-without-marker", expired(ci_expiry=False), "claimed", ("unauthorized_label",)),
    row("expired-ci-marker-out-of-window",
        expired(ci_expiry=False, markers=[expiry_marker(when=at(2) + timedelta(minutes=11))]),
        "claimed", ("unauthorized_label",)),
    row("expired-ci-marker-at-window-edge",
        expired(ci_expiry=False, markers=[expiry_marker(when=at(2) + timedelta(minutes=10))]),
        "expired"),
    row("expired-ci-marker-unverified",
        expired(ci_expiry=False, markers=[expiry_marker(when=at(2), ci_verified=False)]),
        "claimed", ("forged_marker", "unauthorized_label")),
    row("expired-ci-marker-by-lead-unverified",
        expired(ci_expiry=False, markers=[expiry_marker(LEAD, when=at(2), ci_verified=False)]),
        "claimed", ("unauthorized_label",)),
    row("expired-label-without-event", claimed(extra_labels=["state:expired"]), "claimed",
        ("unauthorized_label",)),
    row("expired-last-labeled-event-counts",
        claimed(extra_labels=["state:expired"], events=[("state:expired", "labeled", "lead"),
                                                        ("state:expired", "labeled", "other")]),
        "claimed", ("unauthorized_label",)),
    row("expired-on-in-review", expired(in_review), "in_review", ("stale_expired_label",)),
    row("expired-on-draft", expired(claimed, pr="draft"), "expired", ("draft_pr",)),
    row("stale-and-unauthorized", in_review(extra_labels=["state:expired"],
                                            events=[("state:expired", "labeled", "other")]),
        "in_review", ("unauthorized_label",)),
    # 4. PR
    row("pr-closed-unmerged", claimed(pr="closed"), "claimed", ("pr_closed_unmerged",), pr=None),
    row("multiple-open-prs",
        claimed(pulls=[pull("ready", number=44, created_at=at(35)), pull("ready")]),
        "in_review", ("multiple_open_prs",), pr=PR_NUMBER),
    row("pr-wrong-base", claimed(pr="wrong_base"), "claimed", ("pr_wrong_base",), pr=None),
    row("multiple-one-wrong-base", claimed(pr="ready", pulls=[pull("wrong_base", number=44)]),
        "in_review", ("multiple_open_prs",), pr=PR_NUMBER),
    row("draft-pr", claimed(pr="draft"), "claimed", ("draft_pr",), pr=PR_NUMBER),
    row("fork-pr-ignored", claimed(pr="fork"), "claimed", ("fork_pr_ignored",), pr=None),
    # 4. revisiones y entregas
    row("submitted-outside-core", claimed(pr=pull("ready", via_core=False)), "in_review",
        ("submitted_outside_core",)),
    row("outside-core-changes-after-open",
        claimed(pr=pull("ready", via_core=False, reviews=["CHANGES_REQUESTED"])),
        "changes_requested", ("submitted_outside_core",)),
    row("submission-by-other-ignored",
        claimed(pr=pull("ready", markers=(submission_marker(OTHER),))), "in_review",
        ("submitted_outside_core",)),
    row("submission-owner-case-insensitive",
        claimed(pr=pull("ready", markers=(submission_marker("Carla"),))), "in_review"),
    row("changes-before-submission", changes(reviews=[("lead", "CHANGES_REQUESTED", at(20))]),
        "in_review"),
    row("changes-by-non-reviewer", changes(reviews=[("other", "CHANGES_REQUESTED")]), "in_review"),
    row("owner-own-review-ignored", changes(reviews=[("owner", "CHANGES_REQUESTED")]), "in_review"),
    row("section-owner-review-counts",
        changes(sections=("methods", "results"), authz=REVIEWER_OTHER,
                reviews=[("other", "CHANGES_REQUESTED")]), "changes_requested"),
    row("approved-by-lead", in_review(reviews=[("lead", "APPROVED")]), "in_review",
        approved=True),
    row("approved-by-section-owner-only",
        in_review(sections=("methods", "results"), authz=REVIEWER_OTHER,
                  reviews=[("other", "APPROVED")]), "in_review", approved=False),
    row("approved-then-changes", in_review(reviews=[("lead", "APPROVED"),
                                                    ("lead", "CHANGES_REQUESTED")]),
        "changes_requested", approved=False),
    row("changes-then-dismissed", in_review(reviews=[("lead", "CHANGES_REQUESTED"),
                                                     ("lead", "DISMISSED")]),
        "in_review", approved=False),
    row("changes-then-approved", in_review(reviews=[("lead", "CHANGES_REQUESTED"),
                                                    ("lead", "APPROVED")]),
        "in_review", approved=True),
    row("approval-and-blocking-review", in_review(reviews=[("lead", "APPROVED"),
                                                           ("deputy", "CHANGES_REQUESTED")]),
        "changes_requested", approved=False),
    row("commented-review-ignored", in_review(reviews=[("lead", "COMMENTED")]), "in_review",
        approved=False),
    row("review-without-date", in_review(reviews=[Review(LEAD, "CHANGES_REQUESTED", None)]),
        "in_review"),
    row("reviewer-case-insensitive", in_review(reviews=[("ANA", "CHANGES_REQUESTED")]),
        "changes_requested"),
    row("approver-case-insensitive", in_review(reviews=[("Ana", "APPROVED")]), "in_review",
        approved=True),
    row("reviewer-spellings-are-one-reviewer",
        in_review(reviews=[("lead", "CHANGES_REQUESTED"), ("ANA", "APPROVED")]), "in_review",
        approved=True),
    row("resubmission-clamped-to-edit", claimed(pr=resubmitted_pr(updated=35, last_round=60)),
        "changes_requested"),
    row("rounds-empty", claimed(pr=pr_with_rounds([])), "changes_requested"),
    row("rounds-not-a-list", claimed(pr=pr_with_rounds({"submitted_at": at(50).isoformat()})),
        "changes_requested"),
    row("round-not-a-mapping", claimed(pr=pr_with_rounds(["round"])), "changes_requested"),
    row("round-bad-date", claimed(pr=pr_with_rounds([{"submitted_at": "yesterday"}])),
        "changes_requested"),
    row("round-date-not-string", claimed(pr=pr_with_rounds([{"submitted_at": 5}])),
        "changes_requested"),
    row("round-naive-date-is-utc",
        claimed(pr=pr_with_rounds([{"submitted_at": "2026-09-29T12:50:00"}], updated=50)),
        "in_review"),
    row("everything-at-once",
        snap(labels={"task", "state:proposed", "flag:blocked"},
             ref=task_ref(OWNER, created_by=OTHER),
             claim=True, assignees=[], markers=[expiry_marker(OTHER)],
             pulls=[pull("fork", number=7), pull("closed", number=8), pull("draft"),
                    pull("wrong_base", number=44)]),
        "claimed",
        ("forged_marker", "fork_pr_ignored", "ref_on_proposed", "owner_sources_disagree",
         "unauthorized_label", "pr_closed_unmerged", "multiple_open_prs", "draft_pr")),
]


@pytest.mark.parametrize(("s", "state", "anomalies", "checks"), INVALID)
def test_invalid_combinations(s: TaskSnapshot, state: str, anomalies: tuple[str, ...],
                              checks: dict[str, object]) -> None:
    check(derive_state(s), state, anomalies, checks)


def test_table_covers_every_state_and_anomaly() -> None:
    rows = STATE_ROWS + INVALID
    assert {p.values[1] for p in rows} == STATES
    seen = {a for p in rows for a in p.values[2]} | {a for p in TRANSITIONS for a in p.values[4]}
    assert seen == ANOMALIES


def test_active_states() -> None:
    assert ACTIVE == {"claimed", "in_review", "changes_requested", "expired"}
    assert ACTIVE < STATES


# --- Auxiliares -----------------------------------------------------------------------

def test_accepted_helpers() -> None:
    assert accepted(opened()) and accepted_by(opened()) == LEAD
    assert not accepted(proposed()) and accepted_by(proposed()) is None


def test_resolve_owner_without_ref_has_no_owner() -> None:
    assert resolve_owner(opened()) == (None, None, [])


def test_effective_sections_is_the_intersection() -> None:
    s = claimed(sections=("methods", "results"), claim=False,
                markers=[claim_marker(OWNER, sections=("methods", "introduction"))])
    assert effective_sections(s, OWNER) == (("methods",), [])
    # quitar secciones del Issue reduce el alcance; agregarlas no lo amplía
    assert effective_sections(replace(s, sections=("results",)), OWNER) == ((), [])


def test_effective_sections_without_marker_uses_task_spec() -> None:
    s = claimed(sections=("methods", "results"), claim=False)
    assert effective_sections(s, OWNER) == (("methods", "results"), [])
    assert effective_sections(claimed(), None) == (("methods",), [])
    assert effective_sections(opened(), OWNER) == (("methods",), [])


def test_effective_sections_matches_owner_ignoring_case() -> None:
    assert effective_sections(claimed(), "CARLA") == (("methods",), [])


@pytest.mark.parametrize("raw", ["methods", None, 3, ["methods", 7]])
def test_effective_sections_malformed_marker_sections(raw: object) -> None:
    s = claimed(claim=False, markers=[claim_marker(OWNER, sections=raw)])
    expected = ("methods",) if isinstance(raw, list) else ()
    assert effective_sections(s, OWNER) == (expected, [])


def test_effective_sections_spec_changed() -> None:
    s = claimed(claim=False, markers=[claim_marker(OWNER, spec_hash=OTHER_SPEC_HASH)])
    assert effective_sections(s, OWNER) == (("methods",), ["spec_changed_after_claim"])


def test_effective_sections_ignores_claims_for_an_old_ref() -> None:
    s = claimed(claim=False, markers=[claim_marker(OWNER, claim_sha=OLD_CLAIM_SHA,
                                                   sections=("introduction",))])
    assert effective_sections(s, OWNER) == (("methods",), [])


def _naive_pull(rounds: object) -> Pull:
    m = Marker("submission", OWNER, NAIVE, NAIVE + timedelta(minutes=30), {"rounds": rounds})
    return Pull(1, "open", False, False, OWNER, "main", True, NAIVE, (), (m,))


def test_last_submission_at_aligns_aware_dates_to_naive_snapshots() -> None:
    aware = datetime(2026, 9, 29, 14, 40, tzinfo=timezone(timedelta(hours=2)))
    t, via_core = last_submission_at(_naive_pull([{"submitted_at": aware.isoformat()}]), OWNER)
    assert via_core and t == datetime(2026, 9, 29, 12, 40) and t.tzinfo is None


def test_last_submission_at_naive_rounds_in_naive_snapshot() -> None:
    t, _ = last_submission_at(_naive_pull([{"submitted_at": "2026-09-29T12:45:00"}]), OWNER)
    assert t == datetime(2026, 9, 29, 12, 45)


def test_last_submission_at_aware_rounds_are_utc() -> None:
    sub = submission_marker(when=at(30), updated=at(90),
                            rounds=[{"submitted_at": "2026-09-29T12:45:00Z"}])
    p = pull("ready", markers=(sub,))
    assert last_submission_at(p, OWNER) == (at(45), True)


def test_last_submission_at_without_owner() -> None:
    assert last_submission_at(pull("ready"), None) == (at(30), False)


def test_last_submission_marker_wins() -> None:
    first = submission_marker(when=at(30), rounds=[{"submitted_at": at(30).isoformat()}])
    second = submission_marker(when=at(55), rounds=[{"submitted_at": at(55).isoformat()}])
    p = pull("ready", markers=(first, second))
    assert last_submission_at(p, OWNER) == (at(55), True)


def test_derived_is_hashable_and_comparable() -> None:
    d = derive_state(in_review())
    assert d == derive_state(in_review()) and hash(d) == hash(derive_state(in_review()))


# --- Invariantes (12-mvp §3.2) ----------------------------------------------------------

@settings(max_examples=600, database=None, deadline=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(snapshots())
def test_invariants(s: TaskSnapshot) -> None:
    d = derive_state(s)                   # total: nunca lanza
    state = d.state
    assert state in STATES
    assert derive_state(s) == d           # determinista
    assert (state == "merged") == any(p.merged and p.same_repo for p in s.pulls)
    assert set(d.anomalies) <= ANOMALIES
    if state not in ACTIVE | {"merged"}:
        assert (d.owner, d.owner_source, d.pr, d.approved) == (None, None, None, False)
    if d.approved:
        assert state in ("in_review", "merged")
    if state in ("in_review", "changes_requested"):
        assert d.pr is not None
    assert d.accepted_by == accepted_by(s)


@settings(max_examples=200, database=None, deadline=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(snapshots())
def test_effective_sections_never_widen_scope(s: TaskSnapshot) -> None:
    d = derive_state(s)
    sections, an = effective_sections(s, d.owner)
    assert set(sections) <= set(s.sections)
    assert an in ([], ["spec_changed_after_claim"])


# --- Pureza (REQ-TASK-4, REQ-ARCH-18, REQ-MVP-24) --------------------------------------

def test_io_is_blocked_during_tests() -> None:
    with pytest.raises(IOBlocked):
        open(os.devnull, encoding="utf-8")
    with pytest.raises(IOBlocked):
        socket.socket()
    with pytest.raises(IOBlocked):
        socket.create_connection(("127.0.0.1", 9))
    with pytest.raises(IOBlocked):
        os.open(os.devnull, os.O_RDONLY)


def test_guard_blocks_calls_from_package_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    block_io(monkeypatch, only_from=package_dirs())
    fake = os.path.join(package_dirs()[0], "domain", "_fake_probe.py")
    code = compile("open(path, encoding='utf-8')", fake, "exec")
    with pytest.raises(IOBlocked):
        exec(code, {"path": os.devnull})
    # Desde fuera del paquete la llamada se delega en el original.
    with builtins.open(os.devnull, encoding="utf-8") as fh:
        assert fh.read() == ""


_ALLOWED_IMPORTS = {"collections.abc", "dataclasses", "datetime", "typing", "hashlib", "re",
                    "unicodedata"}


@pytest.mark.parametrize("module", [tasks, sys.modules["aucove.domain.text"]])
def test_domain_modules_import_only_stdlib(module: Any) -> None:
    source = module.__loader__.get_source(module.__name__)  # usa _io.open_code, no open
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add("." * node.level + (node.module or ""))
    assert names <= _ALLOWED_IMPORTS | {".text"}
    assert all(n.startswith(".") or n.split(".")[0] in sys.stdlib_module_names for n in names)
