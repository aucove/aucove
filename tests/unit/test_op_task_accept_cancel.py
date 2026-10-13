# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``task accept`` y ``task cancel`` (05 §9.11; 04-tareas §4.4): solo humanas, con la fase 2 de
``require_human`` tras las lecturas (06-permisos §6) y los pasos de REQ-TASK-40 y REQ-TASK-41."""

from __future__ import annotations

from pathlib import Path

import pytest

from aucove.domain.taskbody import render_task_body
from aucove.domain.tasks import Authz, derive_state
from aucove.forge.base import LabelEvent
from aucove.ops.snapshot import CI_ACTORS, collect_task
from tests.board_world import Typed, World, at, human, new_world

AUTHZ = Authz("ana", "bruno", CI_ACTORS, {})  # roles basic de board_world


def state(w: World, n: int) -> str:
    ev = collect_task(w.forge, n, AUTHZ, base_branch="main")
    assert ev is not None
    return derive_state(ev.snapshot).state


@pytest.fixture
def world(tmp_path: Path) -> World:
    return new_world(tmp_path)


# -- task accept ---------------------------------------------------------------------------------


def test_accept_removes_state_proposed_after_the_challenge(world: World) -> None:
    world.task(5, accepted=False, title="Revisar el muestreo")
    typed = Typed()
    world.forge.calls.clear()
    result = human(world, "task.accept", typed, task=5)
    calls = world.forge.calls.entries()
    assert result.ok, result.error
    assert result.data == {"task": {"number": 5, "url": world.forge.get_issue(5).url, "state": "open"},  # type: ignore[union-attr]
                           "changed": True}
    # 08 §7.5: 2 lecturas (el Issue y sus eventos) y una escritura
    assert [c.method for c in calls if c.kind == "L" and c.method != "identity"] == ["get_issue", "list_issue_events"]
    assert [c.method for c in calls if c.kind in ("E", "C")] == ["remove_label"]
    op, target, summary = typed.seen[0]
    assert (op, target) == ("accept", "5") and "Revisar el muestreo" in summary
    assert state(world, 5) == "open"


def test_accept_registers_a_missing_label(world: World) -> None:
    # REQ-TASK-40: si el Issue nunca tuvo state:proposed, se agrega y se quita
    body = render_task_body({"summary": "Resumen.", "sections": ["methods"], "plan_refs": [], "done": ["Hecho."]}, "es")
    world.forge.seed_issue(number=6, title="Sin etiqueta", body=body, labels=["task"], author="carla",
                           events=[LabelEvent(1, "labeled", "task", "carla", at(-60))])
    assert state(world, 6) == "proposed"
    result = human(world, "task.accept", task=6)
    assert result.ok, result.error
    assert [m for m, _ in world.forge.calls.writes()][-2:] == ["add_labels", "remove_label"]
    assert state(world, 6) == "open"


def test_accept_validates_p2_before_changing_anything(world: World) -> None:
    world.task(7, accepted=False, sections=("methods", "appendix"))
    typed = Typed()
    result = human(world, "task.accept", typed, task=7)
    assert result.error is not None and result.error.code == "E_VALIDATION_UNKNOWN_SECTION"
    assert result.error.details == {"sections": ["appendix"]}
    assert typed.seen == [] and world.forge.calls.writes() == []


def test_accept_is_for_lead_and_deputy(world: World) -> None:
    world.task(5, accepted=False)
    result = human(world, "task.accept", login="carla", task=5)
    assert result.error is not None and result.error.code == "E_PERM_ROLE_REQUIRED"
    assert human(world, "task.accept", login="bruno", task=5).ok  # el suplente


def test_accept_of_an_open_task_changes_nothing(world: World) -> None:
    world.task(5)
    typed = Typed()
    result = human(world, "task.accept", typed, task=5)
    assert result.ok and result.data["changed"] is False  # type: ignore[index]
    assert typed.seen == [] and world.forge.calls.writes() == []


def test_accept_of_a_claimed_task_is_an_invalid_transition(world: World) -> None:
    world.task(5, claimant="carla")
    result = human(world, "task.accept", task=5)
    assert result.error is not None and result.error.code == "E_STATE_INVALID_TRANSITION"
    assert result.error.details == {"task": 5, "state": "claimed"}


def test_a_wrong_challenge_changes_nothing(world: World) -> None:
    world.task(5, accepted=False)
    result = human(world, "task.accept", Typed("accept 6"), task=5)
    assert result.error is not None and result.error.code == "E_HUMAN_ONLY_NOT_CONFIRMED"
    assert world.forge.calls.writes() == [] and state(world, 5) == "proposed"


def test_accept_dry_run_only_needs_phase_1(world: World) -> None:
    world.task(5, accepted=False)
    typed = Typed()
    result = human(world, "task.accept", typed, task=5, dry_run=True)
    assert result.ok and result.data["dry_run"] is True  # type: ignore[index]
    assert [e["target"] for e in result.data["plan"]] == ["issue#5"]  # type: ignore[index]
    assert typed.seen == [] and world.forge.calls.writes() == []


def test_without_the_cli_there_is_no_phase_2(world: World) -> None:
    world.task(5, accepted=False)
    mcp = world.run("task.accept", login="ana", task=5)  # el servidor MCP ni la lista
    assert mcp.error is not None and mcp.error.code == "E_HUMAN_ONLY_NO_TTY"
    no_confirm = world.run("task.accept", login="ana", inv={"surface": "cli", "tty": True}, task=5)
    assert no_confirm.error is not None and no_confirm.error.code == "E_HUMAN_ONLY_NOT_CONFIRMED"
    assert world.forge.calls.writes() == []


# -- task cancel ---------------------------------------------------------------------------------


def test_cancel_closes_the_pr_archives_and_closes_the_issue(world: World) -> None:
    seeded = world.task(5, claimant="carla", pr=40, labels=("state:expired",))
    typed = Typed()
    world.forge.calls.clear()
    result = human(world, "task.cancel", typed, task=5, reason="Se fusiona con #6")
    calls = world.forge.calls.entries()
    assert result.ok, result.error
    data = result.data
    assert data["changed"] is True and data["task"]["state"] == "cancelled"  # type: ignore[index]
    assert (data["pr_closed"], data["ref_deleted"], data["archive_ref"]) == (  # type: ignore[index]
        40, True, "refs/tags/archive/task-5-1")
    # REQ-TASK-41: el orden de los pasos
    assert [c.method for c in calls if c.kind in ("E", "C")] == [
        "create_comment", "update_pull_request", "create_ref", "delete_ref", "remove_label", "set_assignees",
        "update_issue"]
    # 08 §7.5: L ≤ 10 (04 §1.1 de una tarea con ref y PR abierto, y las etiquetas de archivo), E ≤ 6, C ≤ 1
    reads = [c.method for c in calls if c.kind == "L" and c.method != "identity"]
    assert reads == ["get_issue", "find_pull_request", "get_ref", "list_issue_events", "list_comments",
                     "list_ref_activity", "compare", "list_reviews", "list_comments", "list_refs"]
    assert (len([c for c in calls if c.kind == "E"]), len([c for c in calls if c.kind == "C"])) == (6, 1)
    issue = world.forge.get_issue(5)
    assert issue is not None and (issue.state, issue.state_reason, issue.assignees) == ("closed", "not_planned", ())
    assert "state:expired" not in issue.labels
    assert world.forge.get_ref("archive/task-5-1", namespace="tags").sha == seeded["head"]  # type: ignore[union-attr]
    assert world.forge.get_ref("task/5") is None
    comment = world.forge.list_comments(40)[-1].body
    assert "@ana" in comment and "Se fusiona con #6" in comment
    pr = world.forge.get_pull_request(40)
    assert pr is not None and pr.state == "closed"
    op, target, summary = typed.seen[0]
    assert (op, target) == ("cancel", "5") and summary.count("\n- ") == 6  # el plan completo
    assert state(world, 5) == "cancelled"


def test_cancel_of_a_proposed_task(world: World) -> None:
    world.task(5, accepted=False)
    result = human(world, "task.cancel", task=5)
    assert result.ok, result.error
    assert (result.data["pr_closed"], result.data["ref_deleted"], result.data["archive_ref"]) == (  # type: ignore[index]
        None, False, None)
    assert [m for m, _ in world.forge.calls.writes()][-2:] == ["remove_label", "update_issue"]


def test_cancel_without_work_does_not_archive(world: World) -> None:
    world.task(5, claimant="carla", work=False)
    result = human(world, "task.cancel", task=5)
    assert result.ok and result.data["archive_ref"] is None and result.data["ref_deleted"] is True  # type: ignore[index]
    assert world.forge.list_refs("archive/task-5-", namespace="tags") == []


def test_cancel_completes_a_cancelled_task_with_a_ref(world: World) -> None:
    world.task(5, claimant="carla", issue_state="closed", closed_at=at(-10))
    typed = Typed()
    result = human(world, "task.cancel", typed, task=5)
    assert result.ok and result.data["changed"] is True  # type: ignore[index]
    assert world.forge.get_ref("task/5") is None and result.data["archive_ref"] == "refs/tags/archive/task-5-1"  # type: ignore[index]
    assert "update_issue" not in [m for m, _ in world.forge.calls.writes()]  # ya estaba cerrado
    again = human(world, "task.cancel", typed, task=5)
    assert again.ok and again.data["changed"] is False and len(typed.seen) == 1  # type: ignore[index]


def test_a_merged_task_is_not_cancelled(world: World) -> None:
    world.task(5, claimant="carla", pr=40, merged=True, merged_at=at(-5))
    result = human(world, "task.cancel", task=5)
    assert result.error is not None and result.error.code == "E_STATE_INVALID_TRANSITION"
    assert result.error.details == {"task": 5, "state": "merged"}


def test_cancel_dry_run_plans_without_writing(world: World) -> None:
    world.task(5, claimant="carla", pr=40)
    typed = Typed()
    world.forge.calls.clear()
    result = human(world, "task.cancel", typed, task=5, dry_run=True)
    assert result.ok and [e["target"] for e in result.data["plan"]] == [  # type: ignore[index]
        "pr#40", "refs/tags/archive/task-5-*", "task/5", "issue#5", "issue#5"]
    assert typed.seen == [] and world.forge.calls.writes() == []
