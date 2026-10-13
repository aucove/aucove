# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Matriz de aprobación de 06-permisos §3.2 (``domain.approvals``), los archivos del editor
(02 §3.3, R6a) y el ``editor`` de ``roles.yaml`` (03 REQ-SCHEMA-95)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from aucove.config.roles import parse_roles
from aucove.domain.approvals import (
    ApprovalPolicy,
    ReviewState,
    editor_verdict,
    evaluate_approvals,
    qualifying_approvers,
    required_approvals,
    review_role,
    touches_latex,
)
from aucove.domain.paths import is_editor_path
from tests.conftest import roles_fixture

HEAD = "a" * 40
OLD = "b" * 40
T0 = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
GROUP = ApprovalPolicy(approvers=("ana", "bruno"), editor=None, individual=False, latex=True)
EDITOR = ApprovalPolicy(approvers=("ana", "bruno"), editor="dario", individual=False, latex=True)


def review(author: str, state: str, sha: str | None = HEAD, minutes: int = 0) -> ReviewState:
    return ReviewState(author, state, sha, T0 + timedelta(minutes=minutes))


def test_r1_is_lead_or_deputy_other_than_the_author() -> None:
    assert required_approvals("carla", True, GROUP) == [frozenset({"ana", "bruno"})]
    assert required_approvals("Ana", True, GROUP) == [frozenset({"bruno"})]  # sin distinguir mayúsculas
    no_deputy = ApprovalPolicy(("ana",), None, False, True)
    assert required_approvals("ana", False, no_deputy) == [frozenset()]
    assert evaluate_approvals("ana", HEAD, False, [], no_deputy).unsatisfiable == ("no_deputy",)
    assert required_approvals("carla", True, ApprovalPolicy(("ana",), None, True, True)) == []  # individual


def test_r2_is_the_declared_editor_when_latex_is_touched() -> None:
    assert required_approvals("carla", True, EDITOR) == [frozenset({"ana", "bruno"}), frozenset({"dario"})]
    assert required_approvals("carla", False, EDITOR) == [frozenset({"ana", "bruno"})]
    assert required_approvals("dario", True, EDITOR) == [frozenset({"ana", "bruno"})]  # el editor es el autor
    assert required_approvals("carla", True, GROUP) == [frozenset({"ana", "bruno"})]  # sin editor: lo es el lead
    no_latex = ApprovalPolicy(("ana", "bruno"), "dario", False, False)
    assert required_approvals("carla", True, no_latex) == [frozenset({"ana", "bruno"})]
    assert qualifying_approvers("carla", True, EDITOR) == {"ana", "bruno", "dario"}


def test_evaluate_counts_only_approvals_on_the_head() -> None:
    assert not evaluate_approvals("carla", HEAD, False, [review("ana", "APPROVED", OLD)], GROUP).ok
    result = evaluate_approvals("carla", HEAD, False, [review("ANA", "APPROVED")], GROUP)
    assert result.ok and result.missing == () and result.blocking == ()
    self_approval = evaluate_approvals("carla", HEAD, False, [review("carla", "APPROVED")], GROUP)
    assert not self_approval.ok and self_approval.required_from == ("ana", "bruno")
    dismissed = [review("ana", "APPROVED"), review("ana", "DISMISSED", minutes=5)]
    assert not evaluate_approvals("carla", HEAD, False, dismissed, GROUP).ok
    commented = [review("ana", "APPROVED"), review("ana", "COMMENTED", minutes=5)]  # no cuenta: sigue aprobado
    assert evaluate_approvals("carla", HEAD, False, commented, GROUP).ok


def test_changes_requested_blocks_only_from_qualifying_approvers() -> None:
    reviews = [review("ana", "APPROVED"), review("bruno", "CHANGES_REQUESTED", minutes=1)]
    result = evaluate_approvals("carla", HEAD, False, reviews, GROUP)
    assert not result.ok and result.blocking == ("bruno",)
    outsider = [review("ana", "APPROVED"), review("dario", "CHANGES_REQUESTED")]
    assert evaluate_approvals("carla", HEAD, False, outsider, GROUP).ok
    both = [review("ana", "APPROVED")]
    editor_missing = evaluate_approvals("carla", HEAD, True, both, EDITOR)
    assert not editor_missing.ok and editor_missing.missing == (("dario",),)
    assert evaluate_approvals("carla", HEAD, True, [*both, review("dario", "APPROVED")], EDITOR).ok


@pytest.mark.parametrize(("paths", "expected"), [
    (["sections/methods/methods.tex"], True),
    (["sections/methods/pending.bib"], True),
    (["bib/references.bib"], True),
    (["tex/guide.md"], True),
    (["sections/methods/figures/f1.pdf", "memory/notes/task-3.md"], False),
    (["plan.md"], False),
])
def test_touches_latex(paths: list[str], expected: bool) -> None:
    assert touches_latex(paths) is expected


@pytest.mark.parametrize(("path", "expected"), [
    ("tex/main.tex", True), ("tex/outline/methods.tex", True), ("tex/guide.md", True), ("bib/references.bib", True),
    ("tex/outline/sub/x.tex", False), ("tex/outline/Bad_Name.tex", True), ("tex/other.tex", False),
    ("TEX/MAIN.TEX", False), ("tex/outline/x.md", False),
    ("sections/methods/methods.tex", False),
])
def test_editor_paths(path: str, expected: bool) -> None:
    assert is_editor_path(path) is expected


def test_review_role() -> None:
    owners = {"methods": "carla", "results": "bruno", "introduction": "dario"}
    assert review_role("ana", "carla", False, ["methods"], owners, GROUP) == "approver"
    assert review_role("carla", "carla", False, ["methods"], owners, GROUP) == "none"  # el autor
    assert review_role("dario", "carla", False, ["introduction"], owners, GROUP) == "owner"
    assert review_role("dario", "carla", True, ["methods"], owners, EDITOR) == "approver"  # R2
    assert review_role("dario", "carla", False, ["methods"], owners, EDITOR) == "none"


def test_roles_editor_must_be_an_active_person() -> None:
    base = roles_fixture("basic")
    roles, findings = parse_roles(base + "editor: Dario\n")
    assert roles is not None and roles.editor == "dario" and roles.article_editor == "dario"
    assert [f.code for f in findings] == ["W_VALIDATION_LOGIN_CASE"]
    lead_by_default, _ = parse_roles(base)
    assert lead_by_default is not None and lead_by_default.editor is None and lead_by_default.article_editor == "ana"
    unknown, findings = parse_roles(base + "editor: zoe\n")
    assert unknown is None and [f.code for f in findings] == ["E_VALIDATION_UNKNOWN_PERSON"]
    inactive_text = base.replace("    account_type: none\n", "    account_type: none\n    active: false\n")
    inactive, findings = parse_roles(inactive_text + "editor: dario\n")
    assert inactive is None and [f.code for f in findings] == ["E_VALIDATION_INACTIVE_PERSON"]


# --- editor_verdict: el check aucove/editor-review (09-ci REQ-CI-58) ----------------------


@pytest.mark.parametrize(("author", "touches", "pol", "reason"), [
    ("carla", True, ApprovalPolicy(("ana", "bruno"), "dario", True, True), "individual"),
    ("carla", True, ApprovalPolicy(("ana", "bruno"), "dario", False, False), "format_none"),
    ("carla", True, GROUP, "no_editor"),
    ("Dario", True, EDITOR, "author_is_editor"),
    ("carla", False, EDITOR, "no_latex"),
])
def test_editor_verdict_passes_without_r2(author: str, touches: bool, pol: ApprovalPolicy, reason: str) -> None:
    """Sin R2 pasa aunque el editor haya pedido cambios: el motivo es el primero que aplica."""
    verdict = editor_verdict(author, HEAD, touches, [review("dario", "CHANGES_REQUESTED")], pol)
    assert (verdict.passed, verdict.reason) == (True, reason)


@pytest.mark.parametrize(("reviews", "passed", "reason"), [
    ([], False, "missing"),
    ([review("DARIO", "APPROVED")], True, "approved"),  # sin distinguir mayúsculas
    ([review("dario", "APPROVED", OLD)], False, "missing"),  # otra cabeza: un push pide otra aprobación
    ([review("dario", "APPROVED"), review("dario", "COMMENTED", minutes=5)], True, "approved"),
    ([review("dario", "APPROVED"), review("dario", "DISMISSED", minutes=5)], False, "missing"),
    ([review("dario", "APPROVED"), review("dario", "CHANGES_REQUESTED", minutes=5)], False, "changes_requested"),
    ([review("dario", "CHANGES_REQUESTED", OLD), review("dario", "APPROVED", minutes=5)], True, "approved"),
    # aunque esté vigente una petición de cambios de otra cabeza, la decide la última del editor
    ([review("dario", "CHANGES_REQUESTED", OLD, minutes=5)], False, "changes_requested"),
    # las demás revisiones no cuentan para R2
    ([review("ana", "APPROVED"), review("bruno", "APPROVED")], False, "missing"),
    ([review("dario", "APPROVED"), review("ana", "CHANGES_REQUESTED", minutes=5)], True, "approved"),
    ([review("dario", "PENDING", minutes=5), review("dario", "APPROVED")], True, "approved"),
])
def test_editor_verdict_is_the_last_editor_review(reviews: list[ReviewState], passed: bool, reason: str) -> None:
    verdict = editor_verdict("carla", HEAD, True, reviews, EDITOR)
    assert (verdict.passed, verdict.reason, verdict.editor) == (passed, reason, "dario")
