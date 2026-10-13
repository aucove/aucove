# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``switch`` (05 §9.16; 04-tareas §5): guardar, traer, cambiar de rama y sincronizar."""

from __future__ import annotations

from pathlib import Path

import pytest

from aucove import _meta
from aucove.localstore.store import Store
from tests.board_world import World
from tests.task_world import add_claim, claimed, clone, remote_head

P = _meta.ENV_PREFIX
METHODS = "sections/methods/methods.tex"


@pytest.fixture
def world(tmp_path: Path) -> World:
    """carla en task/11 y con task/12 reclamada (en la forja y en origin), sin rama local."""
    w = claimed(tmp_path)
    w.task(12, claimant="carla", sections=("methods",))
    add_claim(w, 12, checkout=False)
    w.repo.git("branch", "-q", "-D", "task/12")
    Store(w.repo.path).path("tasks/12.json").unlink()
    return w


def test_switches_and_writes_the_cache(world: World) -> None:
    result = world.run("switch", task=12)
    assert result.ok, result.error
    assert result.data == {"task": 12, "branch": "task/12", "previous_branch": "task/11",  # type: ignore[comparison-overlap]
                           "created_local": True}
    assert world.repo.git("rev-parse", "--abbrev-ref", "HEAD") == "task/12"
    assert world.repo.git("rev-parse", "--abbrev-ref", "task/12@{upstream}") == "origin/task/12"
    cached = Store(world.repo.path).task(12)
    assert cached is not None and (cached.claimant, cached.state, cached.sections) == ("carla", "claimed", ("methods",))
    back = world.run("switch", task=11)
    assert back.ok and back.data["created_local"] is False  # type: ignore[index]


def test_unsaved_work_of_the_current_task_is_saved_and_pushed(world: World) -> None:
    world.repo.write({METHODS: "% a medias\n"})
    result = world.run("switch", task=12)
    assert result.ok, result.error
    assert remote_head(world.repo, "task/11") == world.repo.git("rev-parse", "task/11")
    saved = world.repo.git("log", "-1", "--format=%B", "task/11")
    assert saved.startswith("Guarda los cambios antes de cambiar de tarea\n\nTask: #11")


def test_dirty_tree_on_another_branch_is_refused(world: World) -> None:
    world.repo.git("switch", "-q", "main")
    world.repo.write({"README.md": "x\n"})
    world.repo.git("add", "README.md")
    world.repo.write({"README.md": "y\n"})
    result = world.run("switch", task=12)
    assert result.error.code == "E_STATE_DIRTY_WORKTREE"  # type: ignore[union-attr]
    assert result.data["failed_step"] == "save_local"  # type: ignore[index]


def test_only_own_active_tasks(world: World) -> None:
    world.task(13, claimant="bruno", sections=("results",))
    result = world.run("switch", task=13)
    assert result.error.code == "E_CLAIM_NOT_HELD"  # type: ignore[union-attr]
    assert world.run("switch", task=12, env={"CLAUDE_CODE_REMOTE": "true"}).error.code == "E_STATE_UNSUPPORTED_DOOR"  # type: ignore[union-attr]


def test_a_missing_remote_ref(world: World) -> None:
    world.repo.git("push", "-q", "origin", "--delete", "task/12")
    result = world.run("switch", task=12)
    assert result.error.details == {"task": 12, "reason": "ref_missing"}  # type: ignore[union-attr]


def test_sync_fast_forwards_the_local_branch(world: World, tmp_path: Path) -> None:
    """04 §5 paso 4: la local está detrás del remoto (trabajo de la otra máquina): avanza."""
    assert world.run("switch", task=12).ok
    assert world.run("switch", task=11).ok
    laptop = clone(world, tmp_path, "laptop", branch="task/12")
    laptop.commit("En la otra máquina", files={"sections/methods/otra.tex": "% otra\n"})
    laptop.push("task/12")
    result = world.run("switch", task=12)
    assert result.ok and world.repo.head() == remote_head(world.repo, "task/12")


def test_offline_needs_the_local_branch(world: World) -> None:
    offline = {P + "OFFLINE": "1"}
    result = world.run("switch", task=12, env=offline)
    assert result.error.code == "E_NET_OFFLINE"  # type: ignore[union-attr]
    assert world.run("switch", task=12).ok and world.run("switch", task=11).ok
    result = world.run("switch", task=12, env=offline)
    assert result.ok and [w.code for w in result.warnings] == ["W_NET_OFFLINE"]
