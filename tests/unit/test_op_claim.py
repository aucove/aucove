# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Operación ``claim`` (05 §9.12; 04-tareas §2 y §3) y ``task propose`` (05 §9.10), con FakeForge."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from aucove import _meta
from aucove.config.schemas import validate_result
from aucove.domain.taskbody import parse_task_body
from aucove.forge.fake import Fault
from aucove.localstore.store import Store
from aucove.ops.claim import claim_message
from aucove.ops.result import Result
from aucove.ops.snapshot import parse_markers
from aucove.ops.task_propose import default_key
from tests.board_world import NOW, PLAN, PROJECT, World, at, marker, new_world

BRANCH = "task/11"


def envelope(result: Result) -> dict[str, Any]:
    env = result.to_dict()
    assert validate_result(env) == [], json.dumps(env, ensure_ascii=False, indent=1)
    return env


def steps_of(result: Result) -> dict[str, str]:
    data = result.data or {}
    return {s["id"]: s["status"] for s in data.get("steps", [])}


def claim_markers(world: World, n: int, author: str) -> list[dict[str, Any]]:
    return [data for c in world.forge.list_comments(n) if c.author == author
            for kind, data in parse_markers(c.body) if kind == "claim"]


def writes(world: World) -> list[str]:
    return [c.method for c in world.forge.calls.entries() if c.kind in ("E", "C")]


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = new_world(tmp_path)
    w.task(10, author="bruno", accepted=False, title="Propuesta")
    w.task(11, title="Describir el muestreo", plan_refs=["RQ-1"])
    w.task(12, claimant="carla", title="Otra de carla")
    w.task(13, claimant="bruno", sections=("results",), pr=50, title="Tabla 2")
    w.task(14, sections=("results",), title="De bruno")
    return w


# -- claim ----------------------------------------------------------------------------------


def test_claim_creates_the_ref_comment_and_context(world: World) -> None:
    world.forge.calls.clear()
    result = world.run("claim", task=11, checkout=False)
    env = envelope(result)
    data = env["data"]
    ref = world.forge.get_ref(BRANCH)
    assert ref is not None and data["claim_sha"] == ref.sha and data["created"] is True
    assert (data["claimant"], data["branch"], data["task"]["state"]) == ("carla", BRANCH, "claimed")
    commit = world.forge.get_commit(ref.sha)
    lines = commit.message.split("\n")
    assert lines[0] == f"{_meta.CLAIM_SUBJECT}11" and lines[2].startswith("claim-nonce ")
    assert lines[4:6] == ["Task: #11", "AI-Use: none"]
    assert world.forge.get_issue(11).assignees == ("carla",)  # type: ignore[union-attr]
    (claim,) = claim_markers(world, 11, "carla")
    assert claim["claim_sha"] == ref.sha and claim["sections"] == ["methods"] and claim["login"] == "carla"
    assert claim["spec_hash"].startswith("sha256:") and claim["renewed_at"] is None
    assert steps_of(result) == {
        "read_task": "done", "preconditions": "done", "claim_commit": "done", "create_ref": "done",
        "recheck": "done", "assign": "done", "claim_comment": "done", "renew": "skipped", "checkout": "done",
        "context": "done"}
    assert data["checkout"] == {"done": False, "reason": "checkout_false"}
    assert "W_STATE_CHECKOUT_SKIPPED" in [w["code"] for w in env["warnings"]]
    package = data["context"]
    assert package["phase"] == "claim" and package["task"]["number"] == 11
    assert package["task"]["spec"]["summary"]["text"] == "Resumen." and package["plan"][0]["id"] == "RQ-1"
    assert [t["number"] for t in package["active_tasks"]] == [12, 13]
    assert {"kind": "shared_section", "path": "sections/methods/", "tasks": [12]} in package["deterministic_findings"]
    assert [r["id"] for r in package["related_sections"]] == ["results"]
    assert len(json.dumps(env, ensure_ascii=False, separators=(",", ":"))) <= 8000
    cached = Store(world.repo.path).task(11)
    assert cached is not None and (cached.claimant, cached.state, cached.sections) == ("carla", "claimed", ("methods",))
    assert writes(world) == ["create_empty_commit", "create_ref", "set_assignees", "create_comment"]


def test_claiming_twice_is_idempotent(world: World) -> None:
    first = world.run("claim", task=11, checkout=False)
    world.forge.calls.clear()
    second = world.run("claim", task=11, checkout=False)
    envelope(second)
    assert second.data["created"] is False and second.data["claim_sha"] == first.data["claim_sha"]  # type: ignore[index]
    statuses = steps_of(second)
    assert [statuses[s] for s in ("preconditions", "claim_commit", "create_ref", "recheck")] == ["skipped"] * 4
    assert writes(world) == ["set_assignees"]  # el comentario no cambió: no se reescribe
    assert len(claim_markers(world, 11, "carla")) == 1


def test_someone_else_s_claim(world: World) -> None:
    result = world.run("claim", task=12, login="bruno", checkout=False)
    env = envelope(result)
    assert env["error"]["code"] == "E_CLAIM_ALREADY_CLAIMED" and env["error"]["details"] == {"owner": "carla"}
    assert env["data"]["failed_step"] == "preconditions"


def test_only_open_tasks_can_be_claimed(world: World) -> None:
    result = world.run("claim", task=10, login="bruno", checkout=False)
    assert result.error.code == "E_STATE_INVALID_TRANSITION"  # type: ignore[union-attr]
    assert result.error.details == {"state": "proposed"}  # type: ignore[union-attr]
    assert world.run("claim", task=99, checkout=False).error.code == "E_STATE_NOT_A_TASK"  # type: ignore[union-attr]


def test_p3_lists_the_foreign_sections_and_the_options(world: World) -> None:
    result = world.run("claim", task=14, checkout=False)
    env = envelope(result)
    assert env["error"]["code"] == "E_PERM_SECTION_NOT_OWNED"
    data = env["data"]
    assert data["foreign_sections"] == [{"section": "results", "owner": "bruno"}]
    assert data["options"] == ["split", "owner_claims", "reassign_section"]
    assert world.forge.get_ref("task/14") is None


def test_p1_p2_and_unknown_sections(tmp_path: Path) -> None:
    w = new_world(tmp_path)
    w.forge.seed_user("zoe", permission="write")
    w.task(11)
    assert w.run("claim", task=11, login="zoe", checkout=False).error.code == "E_PERM_NOT_A_MEMBER"  # type: ignore[union-attr]
    w.task(15, sections=())
    assert w.run("claim", task=15, checkout=False).error.code == "E_CLAIM_NO_SECTIONS"  # type: ignore[union-attr]
    w.task(16, sections=("nada",))
    result = w.run("claim", task=16, checkout=False)
    assert result.error.code == "E_VALIDATION_UNKNOWN_SECTION"  # type: ignore[union-attr]
    assert result.error.details == {"sections": ["nada"]}  # type: ignore[union-attr]


def _limits(tmp_path: Path, text: str) -> World:
    w = new_world(tmp_path, extra_files={"project.yaml": PROJECT + text})
    w.task(11)
    w.task(12, claimant="carla")
    return w


def test_p4_claim_limit(tmp_path: Path) -> None:
    w = _limits(tmp_path, "limits:\n  max_active_claims: 1\n")
    result = w.run("claim", task=11, checkout=False)
    assert result.error.code == "E_CLAIM_LIMIT_REACHED"  # type: ignore[union-attr]
    assert result.error.details == {"limit": 1, "active": [12]}  # type: ignore[union-attr]


def test_p6_exclusive_sections_and_the_shared_warning(tmp_path: Path) -> None:
    busy = _limits(tmp_path / "a", "limits:\n  exclusive_sections: true\n")
    result = busy.run("claim", task=11, checkout=False)
    assert result.error.code == "E_CLAIM_SECTION_BUSY"  # type: ignore[union-attr]
    assert result.error.details == {"section": "methods", "tasks": [12]}  # type: ignore[union-attr]
    shared = _limits(tmp_path / "b", "")
    result = shared.run("claim", task=11, checkout=False)
    assert result.ok
    (warning,) = [w for w in result.warnings if w.code == "W_CLAIM_SECTION_SHARED"]
    assert warning.details == {"section": "methods", "tasks": [12]}


def test_a_lost_race_is_already_claimed(world: World) -> None:
    rival_sha = world.as_user("ana").create_empty_commit(world.base_fake, claim_message(11))

    def rival(_state: object) -> None:
        world.as_user("ana").create_ref(BRANCH, rival_sha)

    world.forge.faults.add(Fault("create_ref", "race", nth=1, race=rival))
    result = world.run("claim", task=11, checkout=False)
    env = envelope(result)
    assert env["error"]["code"] == "E_CLAIM_ALREADY_CLAIMED" and env["data"]["failed_step"] == "create_ref"
    assert world.forge.get_ref(BRANCH).sha == rival_sha  # type: ignore[union-attr]
    assert claim_markers(world, 11, "carla") == []  # el perdedor no deja nada (REQ-TASK-21)


def test_recheck_compensates_when_the_task_stops_being_claimable(world: World) -> None:
    def close(_state: object) -> None:
        world.as_user("ana").update_issue(11, state="closed", state_reason="not_planned")

    world.forge.faults.add(Fault("create_ref", "race", nth=1, race=close))
    result = world.run("claim", task=11, checkout=False)
    env = envelope(result)
    assert env["error"]["code"] == "E_STATE_INVALID_TRANSITION"
    assert env["error"]["details"] == {"reason": "changed_during_claim", "compensated": True}
    statuses = {s["id"]: s["status"] for s in env["data"]["steps"]}
    assert (statuses["create_ref"], statuses["recheck"], statuses["assign"]) == ("compensated", "failed", "pending")
    assert world.forge.get_ref(BRANCH) is None


def test_a_failed_reflection_keeps_the_claim(world: World) -> None:
    world.forge.faults.add(Fault("set_assignees", "http_5xx"))
    result = world.run("claim", task=11, checkout=False)
    envelope(result)
    assert result.ok and steps_of(result)["assign"] == "failed"
    (warning,) = [w for w in result.warnings if w.code == "W_FORGE_REFLECTION_FAILED"]
    assert warning.details == {"step": "assign"}
    assert world.forge.get_ref(BRANCH) is not None and claim_markers(world, 11, "carla")
    world.forge.faults.clear()
    again = world.run("claim", task=11, checkout=False)  # la siguiente ejecución repara el reflejo
    assert again.ok and steps_of(again)["assign"] == "done"
    assert world.forge.get_issue(11).assignees == ("carla",)  # type: ignore[union-attr]


def test_dry_run_writes_nothing(world: World) -> None:
    world.forge.calls.clear()
    result = world.run("claim", task=11, dry_run=True)
    env = envelope(result)
    kinds = [e["kind"] for e in env["data"]["plan"]]
    assert env["data"]["dry_run"] is True and kinds == [
        "forge_create", "forge_update", "forge_update", "git_checkout", "local_write"]
    assert writes(world) == [] and world.forge.get_ref(BRANCH) is None
    cached = Store(world.repo.path).task(11)  # las lecturas sí escriben la caché (REQ-OPS-30)
    assert cached is None or (cached.claimant, cached.state) == (None, "open")


def test_an_expired_claim_is_renewed(world: World) -> None:
    world.run("claim", task=11, checkout=False)
    ci = world.as_user("ana")
    ci.add_labels(11, ["state:expired"])
    result = world.run("claim", task=11, checkout=False)
    envelope(result)
    assert result.ok and steps_of(result)["renew"] == "done" and result.data["task"]["state"] == "claimed"  # type: ignore[index]
    assert "state:expired" not in world.forge.get_issue(11).labels  # type: ignore[union-attr]
    (claim,) = claim_markers(world, 11, "carla")
    assert claim["renewed_at"] == "2026-10-08T12:00:00Z"


def _handoff_world(tmp_path: Path, handoff_to: str) -> tuple[World, str]:
    """Tarea 11 liberada por el lead con traspaso a ``handoff_to`` y el trabajo archivado (04 §4.3)."""
    w = new_world(tmp_path)
    w.task(11)
    archived = w.as_user("ana").create_empty_commit(w.base_fake, "trabajo anterior")
    w.as_user("ana").create_ref("archive/task-11-1", archived, namespace="tags")
    release = {"task": 11, "released_by": "ana", "previous_owner": "dario", "reason": "traspaso",
               "archive_ref": "refs/tags/archive/task-11-1", "handoff_to": handoff_to,
               "released_at": "2026-10-08T10:00:00Z"}
    w.forge.seed_comment(11, author="ana", body=marker("release", release), created_at=at(-60))
    return w, archived


def _parents(world: World, sha: str) -> list[str]:
    return list(world.forge._s.commits[sha].parents)  # type: ignore[attr-defined]


def test_a_handoff_reserves_the_task(tmp_path: Path) -> None:
    w, _archived = _handoff_world(tmp_path, "bruno")
    result = w.run("claim", task=11, checkout=False)  # carla es dueña de methods, pero no es la indicada
    assert result.error.code == "E_CLAIM_RESERVED"  # type: ignore[union-attr]
    assert result.error.details == {"handoff_to": "bruno", "until": "2026-10-15T11:00:00Z"}  # type: ignore[union-attr]
    later = w.run("claim", task=11, checkout=False, now=NOW + timedelta(days=8))
    assert later.ok  # el traspaso venció (limits.handoff_hold_days = 7)


def test_a_handoff_continues_the_archived_work(tmp_path: Path) -> None:
    w, archived = _handoff_world(tmp_path / "a", "carla")
    result = w.run("claim", task=11, checkout=False)
    assert result.ok and _parents(w, result.data["claim_sha"]) == [archived]  # type: ignore[index]
    fresh, _archived = _handoff_world(tmp_path / "b", "carla")
    again = fresh.run("claim", task=11, checkout=False, fresh=True)
    assert again.ok and _parents(fresh, again.data["claim_sha"]) == [fresh.base_fake]  # type: ignore[index]


def test_checkout_switches_to_the_task_branch(world: World) -> None:
    world.repo.git("branch", BRANCH)  # origin tiene task/11, como si el push del reclamo hubiera llegado
    world.repo.git("push", "-q", "origin", BRANCH)
    world.repo.git("branch", "-D", BRANCH)
    result = world.run("claim", task=11)
    assert result.ok and result.data["checkout"] == {"done": True, "reason": None}  # type: ignore[index]
    assert world.repo.git("branch", "--show-current").strip() == BRANCH
    assert world.repo.git("rev-parse", "--abbrev-ref", f"{BRANCH}@{{upstream}}").strip() == f"origin/{BRANCH}"


def test_a_dirty_worktree_skips_the_checkout(world: World) -> None:
    (world.repo.path / "plan.md").write_text("# cambiado\n", encoding="utf-8")
    result = world.run("claim", task=11)
    assert result.ok and result.data["checkout"] == {"done": False, "reason": "dirty_worktree"}  # type: ignore[index]
    assert world.repo.git("branch", "--show-current").strip() == "main"


def test_context_task_phase_returns_the_package(world: World) -> None:
    world.run("claim", task=11, checkout=False)
    result = world.run("context", task=11, phase="claim")
    env = envelope(result)
    package = env["data"]["task_context"]
    assert package["phase"] == "claim" and package["task"]["owner"] == "carla"
    assert package["task"]["state"] == "claimed" and package["task"]["sections"] == ["methods"]
    bad = world.run("context", task=11)
    assert bad.error.code == "E_VALIDATION_INPUT"  # type: ignore[union-attr]


# -- task propose ----------------------------------------------------------------------------


def test_propose_creates_the_issue_once(world: World) -> None:
    result = world.run("task.propose", title="  Nueva tarea ", sections=["methods"], body="Medir la tasa.",
                       done=["La tasa está medida.", "  Con su incertidumbre. "], plan_refs=["RQ-1"])
    env = envelope(result)
    data = env["data"]
    assert data["created"] is True and data["task"]["state"] == "proposed"
    assert data["idempotency_key"] == default_key("carla", "Nueva tarea", ["methods"])
    issue = world.forge.get_issue(data["task"]["number"])
    assert issue is not None and issue.title == "Nueva tarea"
    assert set(issue.labels) == {"task", "state:proposed"}
    spec = parse_task_body(issue.body)
    assert (spec["summary"], spec["sections"], spec["plan_refs"]) == ("Medir la tasa.", ["methods"], ["RQ-1"])
    assert spec["done"] == ["La tasa está medida.", "Con su incertidumbre."]  # el campo, no _No response_
    assert f"<!-- {_meta.NAME}:op v1 id=task-propose-{data['idempotency_key']} -->" in issue.body
    again = world.run("task.propose", title="nueva TAREA", sections=["methods"], body="Medir.", done=["Hecho."])
    assert again.data["created"] is False and again.data["task"]["number"] == data["task"]["number"]  # type: ignore[index]


def test_propose_finds_a_plan_id_that_the_local_base_does_not_have_yet(world: World) -> None:
    """05 §9.10: tras integrar un cambio al plan, ``origin/main`` local queda atrás hasta el siguiente fetch.
    En una sesión de prueba (20/10/2026), ``propose_task`` rechazaba los IDs nuevos del plan."""
    args: dict[str, Any] = {"title": "X", "sections": ["methods"], "body": "X.", "done": ["Hecho."],
                            "plan_refs": ["RQ-3"]}
    stale = world.run("task.propose", **args)
    assert stale.error is not None and stale.error.code == "E_VALIDATION_UNKNOWN_PLAN_ID"
    plan = PLAN.replace("~~Pregunta vieja.~~\n", "~~Pregunta vieja.~~\n- RQ-3: ¿Y los fines de semana?\n")
    main = world.forge.get_ref("main")
    assert main is not None and "RQ-3" in plan
    world.forge.seed_branch("main", world.forge.seed_commit({"plan.md": plan.encode()}, [main.sha], "plan", "ana",
                                                            date=at(-10)))
    dry = world.run("task.propose", dry_run=True, **args)
    assert dry.ok, dry.error  # solo lee la forja: el ensayo da lo mismo que la ejecución (REQ-OPS-30)
    result = world.run("task.propose", **args)
    assert result.ok and result.data["created"] is True, result.error  # type: ignore[index]
    still = world.run("task.propose", **{**args, "title": "Y", "plan_refs": ["RQ-2"]})
    assert still.error is not None and still.error.details == {"ids": ["RQ-2"]}  # retirado en las dos


def test_propose_validates_against_the_base(world: World) -> None:
    unknown = world.run("task.propose", title="X", sections=["nada"], body="X.", done=["Hecho."])
    assert unknown.error.code == "E_VALIDATION_UNKNOWN_SECTION"  # type: ignore[union-attr]
    retired = world.run("task.propose", title="X", sections=["methods"], body="X.", done=["Hecho."], plan_refs=["RQ-2"])
    assert retired.error.code == "E_VALIDATION_UNKNOWN_PLAN_ID"  # type: ignore[union-attr]
    assert retired.error.details == {"ids": ["RQ-2"]}  # type: ignore[union-attr]
    world.forge.seed_user("zoe", permission="write")
    stranger = world.run("task.propose", login="zoe", title="X", sections=["methods"], body="X.", done=["Hecho."])
    assert stranger.error.code == "E_PERM_NOT_A_MEMBER"  # type: ignore[union-attr]
    keyed = world.run("task.propose", title="X", sections=["methods"], body="X.", done=["Hecho."], idempotency_key="ab" * 8)
    assert keyed.ok and keyed.data["idempotency_key"] == "ab" * 8  # type: ignore[index]


def test_propose_needs_a_summary_and_a_definition_of_done(world: World) -> None:
    """REQ-SCHEMA-69: sin resumen o sin criterio de terminado, la tarea sería inválida."""
    for kwargs, param, reason in (
        ({"done": ["Hecho."]}, "body", "required"),
        ({"body": "Resumen."}, "done", "type"),
        ({"body": "Resumen.", "done": ["  "]}, "done", "type"),
        ({"body": "Resumen.", "done": ["dos\nlíneas"]}, "done", "type"),
        ({"body": "Resumen.", "done": [f"c{i}" for i in range(21)]}, "done", "type"),
    ):
        result = world.run("task.propose", title="X", sections=["methods"], **kwargs)
        assert result.error is not None and result.error.code == "E_VALIDATION_INPUT", kwargs
        assert result.error.details == {"errors": [{"param": param, "reason": reason}]}, kwargs
    assert world.forge.calls.writes() == []


def test_the_summary_cannot_carry_a_form_heading(world: World) -> None:
    """03 §8.3: un `### Definition of done` en el resumen repetiría el campo y la tarea quedaría ilegible."""
    body = "Escribir el código.\n\n### Definition of done\n\n- Hay un script."
    result = world.run("task.propose", title="X", sections=["methods"], body=body, done=["Hecho."])
    assert result.error is not None and result.error.code == "E_VALIDATION_INPUT"
    assert result.error.details == {"errors": [{"param": "body", "reason": "form_heading"}]}
    other = world.run("task.propose", title="X", sections=["methods"], body="### Contexto\n\nOtro título.", done=["Hecho."])
    assert other.ok, other.error  # un ### que no es del formulario es contenido (03 §8.3)
