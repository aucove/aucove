# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Operaciones de varios pasos (05 §6.3 a §6.5; ops/steps.py) y marcadores de tarea (domain/markers.py)."""

from __future__ import annotations

import threading

import pytest

from aucove import _meta
from aucove.domain.markers import render_marker, replace_marker
from aucove.forge.base import ForgeError, ForgeUnavailable
from aucove.i18n import translator
from aucove.ops.context import Invocation, OpContext
from aucove.ops.result import OpError
from aucove.ops.snapshot import parse_markers
from aucove.ops.steps import Steps

IDS = ("read", "write", "mirror", "local")


def ctx(*, dry_run: bool = False, surface: str = "mcp", cancel: threading.Event | None = None) -> OpContext:
    inv = Invocation(root=None, lang="es", json=True, dry_run=dry_run, surface=surface, cancel=cancel)  # type: ignore[arg-type]
    return OpContext(inv)


def test_steps_record_status_in_order() -> None:
    steps = Steps(ctx(), "claim", IDS)
    assert steps.run("read", lambda: 7) == 7
    steps.skip("write", "already")
    assert steps.reflect("mirror", lambda: "ok") == "ok"
    assert steps.as_list() == [
        {"id": "read", "status": "done"},
        {"id": "write", "status": "skipped", "detail": "already"},
        {"id": "mirror", "status": "done"},
        {"id": "local", "status": "pending"},
    ]
    with pytest.raises(ValueError):
        Steps(ctx(), "claim", ("a", "a"))
    with pytest.raises(KeyError):
        steps.done("otro")


def test_a_failed_canonical_step_gives_failure_data() -> None:
    steps = Steps(ctx(), "claim", IDS)
    steps.run("read", lambda: None)

    def boom() -> None:
        raise ForgeUnavailable("caída", http_status=503)

    with pytest.raises(OpError) as info:
        steps.run("write", boom)
    assert info.value.code == "E_FORGE_UNAVAILABLE"
    assert info.value.data == {"failed_step": "write", "steps": [
        {"id": "read", "status": "done"}, {"id": "write", "status": "failed"},
        {"id": "mirror", "status": "pending"}, {"id": "local", "status": "pending"}]}


def test_an_operror_keeps_its_own_data_next_to_failure_data() -> None:
    steps = Steps(ctx(), "claim", IDS)

    def p3() -> None:
        raise OpError("E_PERM_SECTION_NOT_OWNED", details={"sections": ["x"]}, data={"options": ["split"]})

    with pytest.raises(OpError) as info:
        steps.run("read", p3)
    assert info.value.details == {"sections": ["x"]}
    assert info.value.data["options"] == ["split"] and info.value.data["failed_step"] == "read"  # type: ignore[index]


def test_a_failed_reflection_is_a_warning() -> None:
    steps = Steps(ctx(), "claim", IDS)

    def boom() -> None:
        raise ForgeError("permission", "sin permiso")

    assert steps.reflect("mirror", boom) is None
    assert steps.status("mirror") == "failed"
    (warning,) = steps.warnings(translator("es"))
    assert warning.code == "W_FORGE_REFLECTION_FAILED" and warning.details == {"step": "mirror"}
    with pytest.raises(OpError):  # un error del núcleo en un reflejo no se esconde
        steps.reflect("local", lambda: (_ for _ in ()).throw(OpError("E_STATE_BUSY")))


def test_cancellation_and_deadline_stop_at_the_next_step() -> None:
    event = threading.Event()
    steps = Steps(ctx(cancel=event), "claim", IDS)
    steps.run("read", lambda: None)
    event.set()
    with pytest.raises(OpError) as info:
        steps.run("write", lambda: pytest.fail("no debe correr"))
    assert info.value.code == "E_NET_TIMEOUT" and info.value.details == {"reason": "cancelled"}
    late = Steps(ctx(), "claim", IDS, deadline_s=-1)
    with pytest.raises(OpError) as info:
        late.run("read", lambda: None)
    assert info.value.details["reason"] == "deadline"  # type: ignore[index]
    ci = Steps(ctx(surface="ci"), "claim", IDS, deadline_s=-1)  # en CI rige el timeout del job
    assert ci.run("read", lambda: 1) == 1


def test_dry_run_plan() -> None:
    steps = Steps(ctx(dry_run=True), "claim", IDS)
    assert steps.dry_run
    steps.effect("forge_create", "task/12", "Crear la rama")
    result = steps.dry_result()
    assert result.ok and result.data == {"dry_run": True, "plan": [
        {"kind": "forge_create", "target": "task/12", "summary": "Crear la rama"}]}


def test_markers_round_trip_and_replace() -> None:
    marker = render_marker("claim", {"task": 12, "nota": "a -- b --> c"})
    lines = marker.split("\n")
    assert lines[0] == f"<!-- {_meta.NAME}:claim v1" and lines[2] == "-->" and "--" not in lines[1]
    assert parse_markers(marker) == [("claim", {"task": 12, "nota": "a -- b --> c"})]
    body = replace_marker("**Reclamo de #12**", "claim", {"task": 12, "v": 1})
    body = replace_marker(body, "assessment", {"task": 12})
    body = replace_marker(body, "claim", {"task": 12, "v": 2})
    assert [k for k, _ in parse_markers(body)] == ["assessment", "claim"]
    assert dict(parse_markers(body))["claim"] == {"task": 12, "v": 2}
    assert body.endswith("**Reclamo de #12**")
    with pytest.raises(ValueError):
        render_marker("otra", {})
