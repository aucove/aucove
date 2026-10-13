# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``release`` (05 §9.13; 04-tareas §4.1 y §4.3): archivo, borrado de la ref, reflejos y limpieza local."""

from __future__ import annotations

from pathlib import Path

import pytest

from aucove.forge.fake import Fault
from aucove.localstore.store import Store
from aucove.ops.snapshot import parse_markers
from tests.board_world import World
from tests.task_world import claimed, remote_head

METHODS = "sections/methods/methods.tex"


@pytest.fixture
def world(tmp_path: Path) -> World:
    return claimed(tmp_path)


def _release_marker(world: World, n: int = 11) -> dict[str, object]:
    found = [data for c in world.forge.list_comments(n) for kind, data in parse_markers(c.body) if kind == "release"]
    assert len(found) == 1
    return found[0]


def test_the_owner_releases_archives_and_cleans_up(world: World) -> None:
    head = world.forge.get_ref("task/11").sha  # type: ignore[union-attr]
    world.forge.calls.clear()
    result = world.run("release", task=11, reason="No alcanzo")
    calls = world.forge.calls.entries()
    assert result.ok, result.error
    # 08 §7.5: las 7 lecturas de una tarea con ref (snapshot.read_task) y la lista de archivos
    reads = [c.method for c in calls if c.kind == "L" and c.method != "identity"]
    assert reads == ["get_issue", "find_pull_request", "get_ref", "list_issue_events", "list_comments",
                     "list_ref_activity", "compare", "list_refs"]
    writes = [c.method for c in calls if c.kind in ("E", "C")]
    assert writes == ["create_ref", "delete_ref", "set_assignees", "create_comment"]
    data = result.data
    assert data["task"]["state"] == "open" and data["changed"] is True  # type: ignore[index]
    assert data["archive_ref"] == "refs/tags/archive/task-11-1"  # type: ignore[index]
    assert (data["ref_deleted"], data["unassigned"], data["local_branch_deleted"]) == (True, True, True)  # type: ignore[index]
    assert (data["pr_closed"], data["handoff_to"]) == (None, None)  # type: ignore[index]
    assert world.forge.get_ref("archive/task-11-1", namespace="tags").sha == head  # type: ignore[union-attr]
    assert world.forge.get_ref("task/11") is None
    assert world.forge.get_issue(11).assignees == ()  # type: ignore[union-attr]
    marker = _release_marker(world)
    assert (marker["released_by"], marker["previous_owner"], marker["reason"]) == ("carla", "carla", "No alcanzo")
    assert marker["archive_ref"] == "refs/tags/archive/task-11-1" and marker["handoff_to"] is None
    assert world.repo.git("rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert world.repo.git("branch", "--list", "task/11") == ""
    assert Store(world.repo.path).task(11).claimant is None  # type: ignore[union-attr]
    again = world.run("release", task=11)  # ya está open: sin cambios
    assert again.ok and again.data["changed"] is False  # type: ignore[index]
    assert len([c for c in world.forge.list_comments(11) if "release v1" in c.body]) == 1


def test_a_claim_without_work_is_not_archived(world: World) -> None:
    world.task(12, claimant="carla", sections=("methods",), work=False)
    result = world.run("release", task=12)
    assert result.ok and result.data["archive_ref"] is None  # type: ignore[index]
    assert result.data["ref_deleted"] is True  # type: ignore[index]
    assert world.forge.list_refs("archive/task-12-", namespace="tags") == []
    assert result.data["local_branch_deleted"] is False  # type: ignore[index]  # no hay rama local task/12


def test_an_open_pr_is_closed_and_an_existing_archive_reused(tmp_path: Path) -> None:
    w = claimed(tmp_path)
    w.task(12, claimant="carla", sections=("methods",), pr=30)
    head = w.forge.get_ref("task/12").sha  # type: ignore[union-attr]
    w.forge.create_ref("archive/task-12-1", head, namespace="tags")  # de un intento anterior
    result = w.run("release", task=12)
    assert result.ok and result.data["pr_closed"] == 30  # type: ignore[index]
    assert result.data["archive_ref"] == "refs/tags/archive/task-12-1"  # type: ignore[index]
    assert w.forge.get_pull_request(30).state == "closed"  # type: ignore[union-attr]


def test_the_lead_hands_the_task_over(world: World) -> None:
    result = world.run("release", task=11, login="ana", reason="Reasignación", handoff="dario")
    assert result.ok and result.data["handoff_to"] == "dario"  # type: ignore[index]
    marker = _release_marker(world)
    assert (marker["released_by"], marker["previous_owner"], marker["handoff_to"]) == ("ana", "carla", "dario")
    assert result.data["local_branch_deleted"] is False  # type: ignore[index]  # no es la dueña
    assert world.repo.git("rev-parse", "--abbrev-ref", "HEAD") == "task/11"


def test_permissions_and_doors(world: World) -> None:
    assert world.run("release", task=11, login="bruno").error.code == "E_CLAIM_NOT_HELD"  # type: ignore[union-attr]
    denied = world.run("release", task=11, handoff="dario")
    assert denied.error.code == "E_PERM_ROLE_REQUIRED"  # type: ignore[union-attr]
    bad = world.run("release", task=11, login="ana", handoff="zoe")
    assert bad.error.details["errors"] == [{"param": "handoff", "reason": "not_a_member"}]  # type: ignore[union-attr]
    web = world.run("release", task=11, env={"CLAUDE_CODE_REMOTE": "true"})
    assert web.error.code == "E_STATE_UNSUPPORTED_DOOR"  # type: ignore[union-attr]
    assert world.forge.get_ref("task/11") is not None


def test_unsaved_work_is_saved_and_pushed_before_archiving(world: World) -> None:
    world.repo.write({METHODS: "% antes de liberar\n"})
    result = world.run("release", task=11)
    assert result.ok, result.error
    pushed = remote_head(world.repo, "task/11")  # el origin de git; la forja falsa es aparte
    assert pushed is not None
    assert world.repo.git("log", "-1", "--format=%s", pushed) == "Guarda los cambios antes de liberar la tarea"
    assert result.data["local_branch_deleted"] is True  # type: ignore[index]


def test_a_failed_reflection_is_completed_by_the_next_run(world: World) -> None:
    world.forge.faults.add(Fault("set_assignees", "http_5xx"))
    result = world.run("release", task=11)
    assert result.ok and [w.code for w in result.warnings] == ["W_FORGE_REFLECTION_FAILED"]
    world.forge.faults.clear()
    again = world.run("release", task=11)
    assert again.ok and again.data["changed"] is False and again.data["unassigned"] is True  # type: ignore[index]
    assert world.forge.get_issue(11).assignees == ()  # type: ignore[union-attr]


def test_dry_run_plans_without_writing(world: World) -> None:
    world.forge.calls.clear()
    result = world.run("release", task=11, dry_run=True)
    assert result.ok and result.data["dry_run"] is True  # type: ignore[index]
    kinds = [e["kind"] for e in result.data["plan"]]  # type: ignore[index]
    assert kinds == ["forge_create", "forge_delete", "forge_update", "forge_create", "git_checkout"]
    assert not [c for c in world.forge.calls.entries() if c.kind in ("E", "C")]
    assert world.forge.get_ref("task/11") is not None
