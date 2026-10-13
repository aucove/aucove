# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``record-assessment`` (05 §9.17; 04-tareas §8.3 y §8.4): marcador, etiquetas y menciones."""

from __future__ import annotations

from pathlib import Path

import pytest

from aucove.ops.snapshot import parse_markers
from tests.board_world import World
from tests.task_world import claimed

OVERLAP = {"target": {"task": 12}, "kind": "scope", "reason": "Las dos reescriben la sección de muestreo."}
DIRECTION = {"affected_ids": ["RQ-1", "RQ-9"], "criteria": ["reformulates_claim"], "reason": "Cambia la pregunta."}


@pytest.fixture
def world(tmp_path: Path) -> World:
    """carla con la tarea 11 (methods) y bruno con la 12 (results), las dos activas."""
    w = claimed(tmp_path)
    w.task(12, claimant="bruno", sections=("results",))
    return w


def _record(world: World, **kwargs: object):  # type: ignore[no-untyped-def]
    args: dict[str, object] = {"task": 11, "phase": "claim", "verdict": "possible", "overlaps": [OVERLAP],
                               "direction": DIRECTION, "model": "claude-opus-5-5", "door": "claude-code"}
    args.update(kwargs)
    return world.run("record-assessment", **args)


def _assessment(body: str) -> dict[str, object]:
    (data,) = [d for k, d in parse_markers(body) if k == "assessment"]
    return data


def test_a_cited_overlap_labels_and_mentions(world: World) -> None:
    result = _record(world)
    assert result.ok, result.error
    data = result.data
    assert data["target"] == {"kind": "issue", "number": 11} and data["effective_verdict"] == "possible"  # type: ignore[index]
    assert data["labels_added"] == ["flag:overlap", "flag:direction-change"]  # type: ignore[index]
    assert data["comment"]["created"] is False  # type: ignore[index]  # el comentario de reclamo, editado
    assert [w.code for w in result.warnings] == ["W_VALIDATION_UNKNOWN_PLAN_IDS"]
    issue = world.forge.get_issue(11)
    assert {"flag:overlap", "flag:direction-change"} <= set(issue.labels)  # type: ignore[union-attr]
    comments = world.forge.list_comments(11)
    own = next(c for c in comments if c.id == data["comment"]["id"])  # type: ignore[index]
    kinds = [k for k, _ in parse_markers(own.body)]
    assert kinds == ["claim", "assessment"]
    marker = _assessment(own.body)
    assert marker["direction"]["affected_ids"] == ["RQ-1"]  # type: ignore[index]  # RQ-9 no existe
    assert marker["assessed_by"] == "carla" and marker["effective_verdict"] == "possible"
    assert "Posible traslape (consultivo)" in own.body and "Con #12" in own.body and "`RQ-1`" in own.body
    mention = comments[-1]
    assert mention.url == data["mention_comment_url"] and "@ana" in mention.body and "@bruno" in mention.body  # type: ignore[index]


def test_the_same_assessment_changes_nothing(world: World) -> None:
    assert _record(world).ok
    before = world.forge.list_comments(11)
    world.forge.calls.clear()
    again = _record(world, door="claude-code")
    assert again.ok
    assert again.data["labels_added"] == [] and again.data["mention_comment_url"] is None  # type: ignore[index]
    assert not [c for c in world.forge.calls.entries() if c.kind in ("E", "C")]
    assert len(world.forge.list_comments(11)) == len(before)


def test_without_a_valid_citation_the_verdict_is_none(world: World) -> None:
    result = _record(world, overlaps=[{"target": {"task": 99}, "kind": "scope", "reason": "x"}], direction=None)
    assert result.ok and result.data["effective_verdict"] == "none"  # type: ignore[index]
    assert result.data["labels_added"] == []  # type: ignore[index]
    assert [w.code for w in result.warnings] == ["W_VALIDATION_UNCITED_OVERLAP"]
    retired = _record(world, verdict="none", overlaps=[],
                      direction={"affected_ids": ["RQ-2"], "criteria": ["changes_conclusions"], "reason": "x"})
    assert retired.ok and retired.data["labels_added"] == []  # type: ignore[index]  # RQ-2 está retirado


def test_the_submission_phase_needs_the_current_pr_head(world: World) -> None:
    head = world.forge.get_ref("task/11").sha  # type: ignore[union-attr]
    missing = _record(world, phase="submission", head_sha=head)
    assert missing.error.code == "E_STATE_INVALID_TRANSITION"  # type: ignore[union-attr]
    world.forge.seed_pull_request(head_branch="task/11", head_sha=head, author="carla", number=30)
    stale = _record(world, phase="submission", head_sha="0" * 40)
    assert stale.error.code == "E_VALIDATION_ASSESSMENT"  # type: ignore[union-attr]
    assert stale.error.details == {"head_sha": head}  # type: ignore[union-attr]
    done = _record(world, phase="submission", head_sha=head, direction=None)
    assert done.ok and done.data["target"] == {"kind": "pr", "number": 30}  # type: ignore[index]
    assert done.data["comment"]["created"] is True  # type: ignore[index]
    assert "flag:overlap" in world.forge.get_pull_request(30).labels  # type: ignore[union-attr]


def test_only_the_owner_records(world: World) -> None:
    result = _record(world, login="bruno")
    assert result.error.code == "E_CLAIM_NOT_HELD"  # type: ignore[union-attr]
    bad = world.run("record-assessment", task=11, phase="submission", verdict="none")
    assert bad.error.code == "E_VALIDATION_INPUT"  # type: ignore[union-attr]  # sin head_sha
