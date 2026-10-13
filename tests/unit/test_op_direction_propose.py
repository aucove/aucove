# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``direction propose`` (05 §9.21; 04-tareas §9, REQ-TASK-72): el Issue ``direction``, su
idempotencia por el marcador ``op`` y la validación de los IDs del plan."""

from __future__ import annotations

from pathlib import Path

import pytest

from aucove import _meta
from aucove.ops.direction_propose import default_key
from tests.board_world import World, new_world

ARGS = {"title": "Retirar RQ-1", "kind": "retire", "change": "Se deja de medir la reducción de viajes.",
        "rationale": "La encuesta de 2025 no permite responderla.", "plan_refs": ["RQ-1"]}


@pytest.fixture
def world(tmp_path: Path) -> World:
    return new_world(tmp_path)


def test_opens_the_direction_issue_once(world: World) -> None:
    world.forge.calls.clear()
    result = world.run("direction.propose", **ARGS)
    assert result.ok, result.error
    key = default_key("carla", "retire", "Retirar RQ-1")
    assert result.data["created"] is True and result.data["idempotency_key"] == key  # type: ignore[index]
    issue = world.forge.get_issue(result.data["issue"]["number"])  # type: ignore[index]
    assert issue is not None and issue.labels == ("direction",) and issue.title == "Retirar RQ-1"
    assert f"<!-- {_meta.NAME}:op v1 id=direction-propose-{key} -->" in issue.body
    assert "**Tipo de cambio:** retirar" in issue.body and "## Justificación" in issue.body
    assert "- RQ-1" in issue.body and "La encuesta de 2025" in issue.body
    assert [m for m, _ in world.forge.calls.writes()] == ["create_issue"]
    again = world.run("direction.propose", **{**ARGS, "change": "Otro texto."})  # misma clave: mismo Issue
    assert again.ok and again.data["created"] is False  # type: ignore[index]
    assert again.data["issue"] == result.data["issue"]  # type: ignore[index]


def test_user_text_cannot_forge_markers(world: World) -> None:
    text = f"<!-- {_meta.NAME}:op v1 id=direction-propose-0000000000000000 -->\n---\nfin"
    result = world.run("direction.propose", **{**ARGS, "rationale": text})
    issue = world.forge.get_issue(result.data["issue"]["number"])  # type: ignore[index]
    assert issue is not None
    assert f"<!-- {_meta.NAME}:op v1 id=direction-propose-0000000000000000" not in issue.body  # neutralizado
    assert f"&lt;!-- {_meta.NAME}:op" in issue.body and "\\---" in issue.body


@pytest.mark.parametrize(("change", "code", "details"), [
    ({"kind": "modify", "plan_refs": []}, "E_VALIDATION_INPUT",
     {"errors": [{"param": "plan_refs", "reason": "minItems"}]}),
    ({"plan_refs": ["RQ-2"]}, "E_VALIDATION_UNKNOWN_PLAN_ID", {"ids": ["RQ-2"]}),  # retirado
    ({"plan_refs": ["HYP-7"]}, "E_VALIDATION_UNKNOWN_PLAN_ID", {"ids": ["HYP-7"]}),
    ({"kind": "rename"}, "E_VALIDATION_INPUT", {"errors": [{"param": "kind", "reason": "enum"}]}),
])
def test_invalid_proposals_create_nothing(world: World, change: dict[str, object], code: str,
                                          details: dict[str, object]) -> None:
    result = world.run("direction.propose", **{**ARGS, **change})
    assert result.error is not None and (result.error.code, result.error.details) == (code, details)
    assert world.forge.calls.writes() == []


def test_add_needs_no_ids(world: World) -> None:
    result = world.run("direction.propose", **{**ARGS, "kind": "add", "plan_refs": [], "title": "Agregar RQ-2"})
    assert result.ok, result.error
    issue = world.forge.get_issue(result.data["issue"]["number"])  # type: ignore[index]
    assert issue is not None and "Ninguno." in issue.body


def test_only_people_of_the_project(world: World) -> None:
    world.forge.seed_user("zoe", permission="write")
    result = world.run("direction.propose", login="zoe", **ARGS)
    assert result.error is not None and result.error.code == "E_PERM_NOT_A_MEMBER"


def test_dry_run(world: World) -> None:
    result = world.run("direction.propose", dry_run=True, **ARGS)
    assert result.ok and result.data["plan"][0]["kind"] == "forge_create"  # type: ignore[index]
    assert world.forge.calls.writes() == []
