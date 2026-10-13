# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``decision propose`` (05 §9.20; 03-esquemas §6): el registro en la rama de la tarea, que nunca
sobrescribe, con el commit y el push de ``save``."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aucove import _meta
from aucove.config.schemas import validate
from aucove.config.yamlload import split_front_matter
from aucove.ops.rules.schemas import DECISION_PATH
from tests.board_world import World, decision
from tests.task_world import claimed, message, remote_head

P = _meta.ENV_PREFIX
ARGS = {"title": "Fusionar RQ-3 en RQ-1: ¿año base 2019?", "summary": "RQ-3 se mide dentro de RQ-1.",
        "context": "La encuesta no distingue los motivos.", "decision": "Se mide RQ-3 dentro de RQ-1.",
        "consequences": "- La parte de resultados pierde una subsección.", "plan_refs": ["RQ-1"]}
BASE_ID = "2026-10-01-3-medir-en-hogares"


@pytest.fixture
def world(tmp_path: Path) -> World:
    path, text = decision(BASE_ID, title="Medir en hogares", date="2026-10-01")
    return claimed(tmp_path, extra_files={path: text})


def _record(world: World, path: str) -> tuple[dict[str, object], str]:
    text = (world.repo.path / path).read_text(encoding="utf-8")
    front, body, findings = split_front_matter(text, file=path)
    assert not findings and isinstance(front, dict)
    assert validate("decision", front, file=path, source_text=text) == []
    return front, body


def test_writes_commits_and_pushes_the_record(world: World) -> None:
    result = world.run("decision.propose", **ARGS, alternatives=["Mantener RQ-3 aparte.", "Imputar el motivo."])
    assert result.ok, result.error
    data = result.data
    path = data["path"]  # type: ignore[index]
    m = DECISION_PATH.match(path)
    assert m is not None and m.group("n") == "11" and m.group("slug") == "fusionar-rq-3-en-rq-1-ano-base-2019"
    assert data["created"] is True and data["pushed"] is True  # type: ignore[index]
    assert re.fullmatch(r"[0-9a-f]{40}", str(data["commit"]))  # type: ignore[index]
    front, body = _record(world, path)
    assert (front["id"], front["task"], front["author"], front["status"]) == (
        Path(path).stem, 11, "carla", "accepted")
    assert front["origin"] == "agent"  # por MCP (REQ-SEC-14)
    assert (front["supersedes"], front["plan_refs"]) == ([], ["RQ-1"])
    headings = [line for line in body.splitlines() if line.startswith("## ")]
    assert headings == ["## Contexto", "## Decisión", "## Alternativas consideradas", "## Consecuencias"]
    assert "- Mantener RQ-3 aparte.\n- Imputar el motivo." in body
    text = message(world.repo)
    assert text.startswith("Decisión: Fusionar RQ-3") and "Task: #11" in text and "AI-Use: undeclared" in text
    assert remote_head(world.repo, "task/11") == world.repo.git("rev-parse", "HEAD")
    again = world.run("decision.propose", **ARGS, alternatives=["Mantener RQ-3 aparte.", "Imputar el motivo."])
    assert again.ok and again.data == {"path": path, "created": False, "commit": None, "pushed": False}  # type: ignore[index]


def test_never_overwrites_another_record(world: World) -> None:
    first = world.run("decision.propose", **ARGS)
    assert first.ok
    other = world.run("decision.propose", **{**ARGS, "decision": "Otra cosa."})
    assert other.error is not None and other.error.code == "E_VALIDATION_DECISION_EXISTS"
    assert other.data["failed_step"] == "write_file"  # type: ignore[index]
    assert "Otra cosa." not in (world.repo.path / first.data["path"]).read_text(encoding="utf-8")  # type: ignore[index]


def test_supersedes_and_status(world: World) -> None:
    result = world.run("decision.propose", **ARGS, supersedes=[BASE_ID])
    assert result.ok, result.error
    front, body = _record(world, result.data["path"])  # type: ignore[index]
    assert front["supersedes"] == [BASE_ID] and "## Alternativas consideradas" not in body
    unknown = world.run("decision.propose", **{**ARGS, "title": "Otra"}, supersedes=["2026-10-01-3-no-existe"])
    assert unknown.error is not None and unknown.error.code == "E_VALIDATION_INPUT"
    rejected = world.run("decision.propose", **{**ARGS, "title": "Otra"}, status="rejected", supersedes=[BASE_ID])
    assert rejected.error is not None and rejected.error.details == {
        "errors": [{"param": "supersedes", "reason": "rejected"}]}


def test_plan_refs_must_be_active(world: World) -> None:
    result = world.run("decision.propose", **{**ARGS, "plan_refs": ["RQ-2"]})
    assert result.error is not None and result.error.code == "E_VALIDATION_UNKNOWN_PLAN_ID"


def test_only_on_the_own_task_branch(world: World) -> None:
    world.repo.git("switch", "-q", "main")
    off = world.run("decision.propose", **ARGS)
    assert off.error is not None and off.error.code == "E_STATE_NOT_ON_TASK_BRANCH"
    world.repo.git("switch", "-q", "task/11")
    other = world.run("decision.propose", login="bruno", **ARGS)
    assert other.error is not None and other.error.code == "E_STATE_NOT_ON_TASK_BRANCH"
    assert other.error.details["reason"] == "not_owner"  # type: ignore[index]


def test_origin_human_only_from_a_terminal(world: World) -> None:
    result = world.run("decision.propose", inv={"surface": "cli", "tty": True}, **ARGS)
    assert result.ok and _record(world, result.data["path"])[0]["origin"] == "human"  # type: ignore[index]
    child = world.run("decision.propose", inv={"surface": "cli", "tty": True},
                      env={"CLAUDE_CODE_CHILD_SESSION": "1"}, **{**ARGS, "title": "Otra"})
    assert child.ok and _record(world, child.data["path"])[0]["origin"] == "agent"  # type: ignore[index]


def test_offline_commits_and_a_retry_pushes(world: World) -> None:
    result = world.run("decision.propose", env={P + "OFFLINE": "1"}, **ARGS)
    assert result.ok and result.data["pushed"] is False and result.data["commit"]  # type: ignore[index]
    assert [w.code for w in result.warnings] == ["W_NET_OFFLINE"]
    again = world.run("decision.propose", **ARGS)
    assert again.ok and again.data["created"] is False and again.data["pushed"] is True  # type: ignore[index]


def test_dry_run_writes_nothing(world: World) -> None:
    result = world.run("decision.propose", dry_run=True, **ARGS)
    assert result.ok and [e["kind"] for e in result.data["plan"]] == ["file_write", "git_commit", "git_push"]  # type: ignore[index]
    assert not list((world.repo.path / "memory" / "decisions").glob("*-11-*.md"))
