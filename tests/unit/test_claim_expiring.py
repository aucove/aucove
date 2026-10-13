# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``W_CLAIM_EXPIRING``: el reclamo vence en menos de 24 h (05 §12.2; 04 §4.2).

La tarea 11 de carla tiene su último push en ``at(-300)``: con ``claim_expiry_days`` de 14, vence a
las 14 días menos 5 horas de ``NOW``. ``LATE`` queda 9 horas antes; ``EARLY``, dos días antes.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from aucove import _meta
from aucove.ops.result import Result
from tests.board_world import NOW, World
from tests.task_world import claimed

LATE = NOW + timedelta(days=13, hours=10)
EARLY = NOW + timedelta(days=12)
METHODS = "sections/methods/methods.tex"


def expiring(result: Result) -> list[object]:
    return [w.details for w in result.warnings if w.code == "W_CLAIM_EXPIRING"]


@pytest.fixture
def world(tmp_path: Path) -> World:
    return claimed(tmp_path)


def test_status_warns_about_my_own_claim(world: World) -> None:
    result = world.run("status", login="carla", now=LATE)
    assert result.ok, result.error
    (details,) = expiring(result)
    assert details == {"task": 11, "expires_at": "2026-10-22T07:00:00Z"}
    assert expiring(world.run("status", login="carla", now=EARLY)) == []
    assert expiring(world.run("status", login="ana", now=LATE)) == []  # el reclamo es de carla


def test_context_warns_about_my_own_claim(world: World) -> None:
    assert [d["task"] for d in expiring(world.context(login="carla", now=LATE))] == [11]  # type: ignore[index]
    assert expiring(world.context(login="carla", now=EARLY)) == []


def test_an_offline_save_warns_with_the_cached_board(world: World) -> None:
    """Sin red el push no renueva el reclamo; la fecha sale de la caché que dejó status."""
    assert world.run("status", login="carla").ok  # deja la caché del tablero
    world.repo.write({METHODS: "% sin red\n"})
    result = world.run("save", message="Sin red", env={_meta.ENV_PREFIX + "OFFLINE": "1"}, now=LATE)
    assert result.ok, result.error
    assert [w.code for w in result.warnings] == ["W_NET_OFFLINE", "W_CLAIM_EXPIRING"]
    assert expiring(result) == [{"task": 11, "expires_at": "2026-10-22T07:00:00Z"}]


def test_a_save_that_pushes_renews_the_claim(world: World) -> None:
    assert world.run("status", login="carla").ok
    world.repo.write({METHODS: "% con red\n"})
    result = world.run("save", message="Con red", now=LATE)
    assert result.ok and result.data["pushed"] is True and expiring(result) == []  # type: ignore[index]


def test_without_a_cached_board_an_offline_save_says_nothing(world: World) -> None:
    world.repo.write({METHODS: "% sin red\n"})
    result = world.run("save", message="Sin red", env={_meta.ENV_PREFIX + "OFFLINE": "1"}, now=LATE)
    assert result.ok and expiring(result) == []
