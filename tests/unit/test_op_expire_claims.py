# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``expire-claims`` (05 §9.19; 04-tareas §4.2): avisos, vencimientos, la vuelta por actividad,
las reparaciones de REQ-TASK-15, la idempotencia (REQ-TASK-36) y el espaciado de REQ-OPS-21."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from aucove import _meta
from aucove.config.schemas import validate_result
from aucove.domain.tasks import Authz, derive_state
from aucove.forge.base import RefActivity
from aucove.forge.fake import Fault
from aucove.ops import expire_claims
from aucove.ops.context import Invocation, OpContext
from aucove.ops.registry import REGISTRY, invoke
from aucove.ops.result import Result
from aucove.ops.snapshot import CI_ACTORS, EXPIRE_WORKFLOW, collect_task, parse_markers
from tests.board_world import CI, NOW, World, new_world

P = _meta.ENV_PREFIX
AUTHZ = Authz("ana", "bruno", CI_ACTORS, {})
DAY = timedelta(days=1)


@pytest.fixture(autouse=True)
def no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(expire_claims, "WRITE_GAP_S", 0.0)


def push(w: World, n: int, when_days: float) -> None:
    head = w.forge.get_ref(f"task/{n}")
    assert head is not None
    w.forge.seed_ref_activity(f"task/{n}", [RefActivity("push", "carla", NOW + when_days * DAY, head.sha, head.sha)])


def state(w: World, n: int) -> str:
    ev = collect_task(w.forge, n, AUTHZ, base_branch="main")
    assert ev is not None
    return derive_state(ev.snapshot).state


def expiry(w: World, n: int) -> list[dict[str, object]]:
    return [data for c in w.forge.list_comments(n) for kind, data in parse_markers(c.body) if kind == "expiry"]


def run(w: World, login: str = "ana", **kwargs: object) -> Result:
    result = w.run("expire-claims", login=login, **kwargs)
    assert validate_result(result.to_dict()) == []
    return result


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    w = new_world(tmp_path)
    w.task(5, claimant="carla")  # último push: hace 5 horas
    w.task(6, claimant="carla")
    w.task(7, claimant="carla")
    w.task(8, claimant="carla", pr=42)  # in_review: no vence
    w.task(9, claimant="carla")
    w.as_user("ana").add_labels(9, ["flag:blocked"])  # bloqueo válido: lo puso el lead
    w.task(10)  # abierta: solo se reconcilia
    push(w, 6, 13)
    push(w, 7, 3)
    w.forge.clock.set(NOW + 15 * DAY)
    yield w


def test_warns_expires_and_skips(world: World) -> None:
    world.forge.calls.clear()
    result = run(world)
    assert result.ok, result.error
    data = result.data
    assert (data["evaluated"], data["threshold_days"]) == (4, 14)  # type: ignore[index]
    assert [e["task"] for e in data["expired"]] == [5] and [e["task"] for e in data["warned"]] == [7]  # type: ignore[index]
    assert data["skipped"] == [{"task": 9, "reason": "blocked"}] and data["unexpired"] == []  # type: ignore[index]
    assert data["expired"][0]["claimed_by"] == "carla"  # type: ignore[index]
    assert [m for m, _ in world.forge.calls.writes()] == ["add_labels", "create_comment", "create_comment"]
    assert state(world, 5) == "expired" and state(world, 7) == "claimed"
    (marker,) = expiry(world, 5)
    assert (marker["phase"], marker["idle_days"], marker["run_id"]) == ("expired", 15, None)
    assert marker["head_sha"] == world.forge.get_ref("task/5").sha  # type: ignore[union-attr]
    body = world.forge.list_comments(5)[-1].body
    assert "@carla @ana" in body and "`state:expired`" in body
    assert expiry(world, 7)[0]["phase"] == "warning" and "@ana" not in world.forge.list_comments(7)[-1].body
    assert world.forge.get_ref("task/5") is not None  # REQ-TASK-35: no borra la ref
    # REQ-TASK-36: otra ejecución no cambia nada
    world.forge.calls.clear()
    again = run(world)
    assert again.ok and world.forge.calls.writes() == []
    assert (again.data["expired"], again.data["warned"]) == ([], [])  # type: ignore[index]


def test_activity_after_the_label_unexpires(world: World) -> None:
    assert run(world).ok
    push(world, 5, 16)
    world.forge.clock.set(NOW + 17 * DAY)
    result = run(world)
    assert [e["task"] for e in result.data["unexpired"]] == [5]  # type: ignore[index]
    assert state(world, 5) == "claimed"


def test_ci_expiry_counts_with_its_verified_run(world: World) -> None:
    base = world.forge.get_ref("main")
    assert base is not None
    run_rec = world.forge.seed_workflow_run(EXPIRE_WORKFLOW, branch="main", sha=base.sha, conclusion=None,
                                            name="expire-claims", event="schedule", actor=CI)
    result = run(world, login=CI, env={"GITHUB_RUN_ID": str(run_rec.id)})
    assert result.ok, result.error
    assert expiry(world, 5)[0]["run_id"] == run_rec.id
    assert state(world, 5) == "expired"  # etiqueta de CI con marcador verificado (04 §1.3)


def test_repairs_of_req_task_15(world: World) -> None:
    lead = world.as_user("ana")
    lead.set_assignees(6, ["carla", "bruno"])  # assignees_mismatch (con uno solo, el asignado sería otra fuente)
    lead.set_assignees(10, ["carla"])  # assignee_without_ref
    world.task(11, labels=("state:expired",))  # stale_expired_label
    world.task(12, claimant="carla", pr=43, merged=True)  # issue_open_after_merge
    result = run(world)
    assert result.data["repaired"] == [  # type: ignore[index]
        {"task": 6, "action": "assign_owner"}, {"task": 10, "action": "clear_assignees"},
        {"task": 11, "action": "remove_stale_expired"}, {"task": 12, "action": "close_merged_issue"}]
    assert world.forge.get_issue(6).assignees == ("carla",)  # type: ignore[union-attr]
    assert world.forge.get_issue(10).assignees == ()  # type: ignore[union-attr]
    assert "state:expired" not in world.forge.get_issue(11).labels  # type: ignore[union-attr]
    issue = world.forge.get_issue(12)
    assert issue is not None and (issue.state, issue.state_reason) == ("closed", "completed")
    assert not [c for c in world.forge.list_comments(6) if "expiry" in c.body]  # REQ-TASK-17: sin comentarios


def test_without_the_activity_api_a_new_head_counts_once(tmp_path: Path) -> None:
    w = new_world(tmp_path)
    w.task(5, claimant="carla")
    w.forge.clock.set(NOW + 12 * DAY)
    assert [e["task"] for e in run(w).data["warned"]] == [5]  # type: ignore[index]
    w.forge.faults.add(Fault("list_ref_activity", "http_404"))  # la forja no da la actividad
    head = w.forge.get_ref("task/5")
    assert head is not None
    w.forge.seed_branch("task/5", w.forge.seed_commit({"README.md": b"z\n"}, [head.sha], "más", "carla"))
    w.forge.clock.set(NOW + 13 * DAY)
    first = run(w)
    marker = expiry(w, 5)[0]
    assert marker["idle_days"] == 0 and marker["head_sha"] == w.forge.get_ref("task/5").sha  # type: ignore[union-attr]
    assert first.data["warned"] == [] and first.data["expired"] == []  # type: ignore[index]
    w.forge.calls.clear()
    assert run(w).ok and w.forge.calls.writes() == []  # la misma cabeza no vuelve a contar


def test_only_ci_lead_or_deputy(world: World) -> None:
    result = run(world, login="carla")
    assert result.error is not None and result.error.code == "E_PERM_ROLE_REQUIRED"
    assert run(world, login="bruno").ok


def _with_now(w: World, value: str, env: dict[str, str] | None = None) -> Result:
    """``--now`` (``World.run`` usa ``now`` para el reloj de la invocación)."""
    spec = next(s for s in REGISTRY if s.id == "expire-claims")
    inv = Invocation(root=str(w.repo.path), lang="es", json=True, dry_run=False, surface="cli", tty=False,
                     env={**w.env, **(env or {})}, cwd=str(w.repo.path))
    return invoke(spec, OpContext(inv, forge=w.as_user("ana")), policy=None, now=value)


def test_now_only_with_the_fake_forge(world: World) -> None:
    result = _with_now(world, "2026-11-01T00:00:00Z")
    assert result.error is not None and result.error.details == {"errors": [{"param": "now", "reason": "fake_only"}]}
    world.forge.clock.set(NOW)
    fake = _with_now(world, "2026-10-23T12:00:00Z", {P + "TESTING": "1", P + "FORGE_ADAPTER": "fake"})
    assert fake.ok and [e["task"] for e in fake.data["expired"]] == [5]  # type: ignore[index]


def test_writes_are_spaced(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []
    monkeypatch.setattr(expire_claims, "WRITE_GAP_S", 1.0)
    monkeypatch.setattr(expire_claims, "_sleep", waits.append)
    world.forge.calls.clear()
    assert run(world).ok
    writes = len(world.forge.calls.writes())
    assert len(waits) == writes - 1 and all(0 < w <= 1.0 for w in waits)


def test_dry_run_plans_without_writing(world: World) -> None:
    world.forge.calls.clear()
    result = run(world, dry_run=True)
    assert result.ok and [(e["target"], e["kind"]) for e in result.data["plan"]] == [  # type: ignore[index]
        ("issue#5", "forge_update"), ("issue#7", "forge_update")]
    assert world.forge.calls.writes() == []


def test_reads_and_writes_per_task(tmp_path: Path) -> None:
    # 08 §7.5: por tarea L ≤ 8, E ≤ 3 y C ≤ 1; las listas en bloque se leen una vez
    w = new_world(tmp_path)
    w.task(5, claimant="carla", pr=42, reviewers=("ana",))
    w.task(6, claimant="carla")
    w.forge.clock.set(NOW + 15 * DAY)
    w.forge.calls.clear()
    assert run(w).ok
    reads = [c.method for c in w.forge.calls.entries() if c.kind == "L" and c.method != "identity"]
    assert reads[:3] == ["list_issues", "list_refs", "list_pull_requests"]
    per_task = reads[3:]
    assert per_task == ["list_issue_events", "list_comments", "list_ref_activity", "compare", "list_reviews",
                        "list_comments", "list_issue_events", "list_comments", "list_ref_activity", "compare"]
    writes = w.forge.calls.writes()
    assert [m for m, _ in writes] == ["add_labels", "create_comment"]  # solo vence la 6
