# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``update`` (05 §9.15; 04-tareas §6): fusión de la base, conflictos, --continue, --abort y --escalate."""

from __future__ import annotations

from pathlib import Path

import pytest

from aucove import _meta
from aucove.ops.snapshot import parse_markers
from tests.board_world import World
from tests.conftest import GitRepo
from tests.task_world import clone, claimed, message, remote_head

P = _meta.ENV_PREFIX
METHODS = "sections/methods/methods.tex"


@pytest.fixture
def world(tmp_path: Path) -> World:
    return claimed(tmp_path)


def _advance_base(w: World, tmp: Path, files: dict[str, str], name: str = "base") -> GitRepo:
    """La base avanza en ``origin`` (como si se integrara otro PR)."""
    other = clone(w, tmp, name)
    other.commit("Integra otro PR", files=files)
    other.push("main")
    return other


def test_merges_the_base_and_pushes(world: World, tmp_path: Path) -> None:
    _advance_base(world, tmp_path, {"plan.md": "# Plan nuevo\n"})
    result = world.run("update")
    assert result.ok, result.error
    data = result.data
    assert (data["result"], data["behind_before"], data["pushed"]) == ("merged", 1, True)  # type: ignore[index]
    assert data["merge_commit"] == world.repo.head() == remote_head(world.repo, "task/11")  # type: ignore[index]
    assert message(world.repo) == f"Merge origin/main into task/11\n\nTask: #11"  # solo Task (11 §2.5)
    again = world.run("update")
    assert again.data["result"] == "up_to_date" and again.data["pushed"] is False  # type: ignore[index]


def test_unsaved_changes_are_saved_first(world: World, tmp_path: Path) -> None:
    _advance_base(world, tmp_path, {"plan.md": "# Plan nuevo\n"})
    world.repo.write({METHODS: "% sin guardar\n"})
    result = world.run("update")
    assert result.ok and result.data["result"] == "merged"  # type: ignore[index]
    saved = world.repo.git("log", "-1", "--format=%B", "HEAD^1")
    assert saved.startswith("Guarda los cambios antes de actualizar desde la base\n\nTask: #11\nSection: methods")
    assert saved.endswith("AI-Use: undeclared")


def test_an_own_section_conflict_is_left_to_the_agent(world: World, tmp_path: Path) -> None:
    _advance_base(world, tmp_path, {METHODS: "% base\n"})
    world.repo.commit("Mío", files={METHODS: "% mío\n"})
    result = world.run("update")
    assert result.error.code == "E_STATE_MERGE_CONFLICT"  # type: ignore[union-attr]
    data = result.data
    assert data["failed_step"] == "merge_base" and data["result"] == "conflict"  # type: ignore[index]
    assert data["resolvable"] is True and data["behind_before"] == 1  # type: ignore[index]
    assert data["conflicts"] == [{"path": METHODS, "class": "section", "resolution": "agent"}]  # type: ignore[index]
    # --continue con marcas de conflicto todavía no cierra la fusión
    stuck = world.run("update", **{"continue": True})
    assert stuck.error.code == "E_STATE_MERGE_CONFLICT"  # type: ignore[union-attr]
    world.repo.write({METHODS: "% mío y de la base\n"})
    done = world.run("update", **{"continue": True})
    assert done.ok and done.data["result"] == "merged" and done.data["pushed"] is True  # type: ignore[index]
    assert remote_head(world.repo, "task/11") == world.repo.head()
    assert not world.repo.git("rev-parse", "-q", "--verify", "MERGE_HEAD", check=False)


def test_out_of_scope_conflicts_take_the_base_and_keep_a_patch(world: World, tmp_path: Path) -> None:
    """auto_base: carla no es lead; su cambio en plan.md (hecho con git a mano) se descarta."""
    _advance_base(world, tmp_path, {"plan.md": "# Plan de la base\n"})
    world.repo.commit("A mano", files={"plan.md": "# Plan mío\n"})
    result = world.run("update")
    assert result.ok, result.error
    assert result.data["conflicts"] == [{"path": "plan.md", "class": "lead", "resolution": "auto_base"}]  # type: ignore[index]
    (warning,) = [w for w in result.warnings if w.code == "W_STATE_OUT_OF_SCOPE_DROPPED"]
    assert warning.details["paths"] == ["plan.md"]  # type: ignore[index]
    patch = world.repo.path / warning.details["patch"]  # type: ignore[index]
    assert patch.parent.parent.name == "dropped" and b"+# Plan m" in patch.read_bytes()
    assert (world.repo.path / "plan.md").read_text(encoding="utf-8") == "# Plan de la base\n"


def test_a_decision_added_on_both_sides_is_renamed(world: World, tmp_path: Path) -> None:
    """auto_rename (03 REQ-SCHEMA-47): la decisión propia pasa a -2 con su id."""
    name = "memory/decisions/2026-10-08-11-muestreo.md"
    body = "---\nid: 2026-10-08-11-muestreo\ntitle: \"{t}\"\n---\nCuerpo.\n"
    _advance_base(world, tmp_path, {name: body.format(t="De la base")})
    world.repo.commit("Decisión", files={name: body.format(t="Mía")})
    result = world.run("update")
    assert result.ok, result.error
    assert result.data["conflicts"] == [{"path": name, "class": "append", "resolution": "auto_rename"}]  # type: ignore[index]
    renamed = world.repo.path / "memory/decisions/2026-10-08-11-muestreo-2.md"
    assert renamed.read_text(encoding="utf-8").startswith("---\nid: 2026-10-08-11-muestreo-2\ntitle: \"Mía\"")
    assert "De la base" in (world.repo.path / name).read_text(encoding="utf-8")


def test_abort_and_escalate(world: World, tmp_path: Path) -> None:
    _advance_base(world, tmp_path, {METHODS: "% base\n"})
    world.repo.commit("Mío", files={METHODS: "% mío\n"})
    assert world.run("update").error.code == "E_STATE_MERGE_CONFLICT"  # type: ignore[union-attr]
    world.forge.calls.clear()
    result = world.run("update", abort=True, escalate="La macro \\tasa ya no existe")
    assert result.ok and result.data["result"] == "escalated"  # type: ignore[index]
    assert not world.repo.git("rev-parse", "-q", "--verify", "MERGE_HEAD", check=False)
    writes = [c.method for c in world.forge.calls.entries() if c.kind in ("E", "C")]
    assert writes == ["add_labels", "create_comment"]  # la etiqueta antes del marcador (04 §1.3)
    issue = world.forge.get_issue(11)
    assert "flag:blocked" in issue.labels  # type: ignore[union-attr]
    comment = world.forge.list_comments(11)[-1]
    (kind, data), = [m for m in parse_markers(comment.body) if m[0] == "escalation"]
    assert data["files"] == [METHODS] and data["reason"].startswith("La macro") and "@ana" in comment.body
    assert data["suspect_commits"] and data["base_sha"] == world.repo.git("rev-parse", "origin/main")
    # REQ-TASK-51: el siguiente update exitoso del dueño quita su flag:blocked
    world.repo.git("reset", "-q", "--hard", "origin/task/11")  # el responsable arregló la base
    unblocked = world.run("update")
    assert unblocked.ok
    assert "flag:blocked" not in world.forge.get_issue(11).labels  # type: ignore[union-attr]


def test_abort_without_merge_and_input_rules(world: World) -> None:
    assert world.run("update", abort=True).data["result"] == "aborted"  # type: ignore[index]
    bad = world.run("update", abort=True, **{"continue": True})
    assert bad.error.details["errors"] == [{"param": "abort", "reason": "exclusive"}]  # type: ignore[union-attr]
    bad = world.run("update", escalate="motivo")
    assert bad.error.details["errors"] == [{"param": "escalate", "reason": "requires_abort"}]  # type: ignore[union-attr]


def test_main_broken_warns_and_offline_stays_local(world: World, tmp_path: Path) -> None:
    world.forge.seed_issue(number=90, title="La base está rota", body="", labels=["main-broken"], author="ana")
    _advance_base(world, tmp_path, {"plan.md": "# Plan nuevo\n"})
    result = world.run("update")
    assert [w.code for w in result.warnings] == ["W_STATE_MAIN_BROKEN"]
    _advance_base(world, tmp_path, {"plan.md": "# Plan más nuevo\n"}, name="base2")
    offline = world.run("update", env={P + "OFFLINE": "1"})
    assert offline.ok and [w.code for w in offline.warnings] == ["W_NET_OFFLINE"]
    assert offline.data["result"] == "up_to_date" and offline.data["pushed"] is False  # type: ignore[index]
