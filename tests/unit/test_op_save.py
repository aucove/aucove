# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``save`` (05 §9.14): alcance, commit con trailers, integración del remoto y push."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aucove import _meta
from aucove.localstore.store import Store
from aucove.ops.board import write_task_cache
from tests.board_world import NOW, World
from tests.task_world import REPO, claimed, clone, message, remote_head

P = _meta.ENV_PREFIX
METHODS = "sections/methods/methods.tex"


@pytest.fixture
def world(tmp_path: Path) -> World:
    return claimed(tmp_path)


def _forge_calls(world: World) -> list[str]:
    return [c.method for c in world.forge.calls.entries() if c.method not in ("identity", "capabilities")]


def test_save_commits_with_trailers_and_pushes(world: World) -> None:
    world.repo.write({METHODS: "% métodos\nNuevo párrafo.\n", "sections/methods/fig.tex": "% nueva\n"})
    world.forge.calls.clear()
    result = world.run("save", message="Agrega el diseño muestral", ai_use=["editing", "drafting"],
                       ai_tool="claude-code", ai_model="claude-opus-5-5")
    assert result.ok, result.error
    data = result.data
    assert data["task"] == 11 and data["branch"] == "task/11"  # type: ignore[index]
    assert data["commit"]["files"] == ["sections/methods/fig.tex", METHODS]  # type: ignore[index]
    assert (data["pushed"], data["ahead"], data["behind"]) == (True, 0, 0)  # type: ignore[index]
    assert message(world.repo) == (
        "Agrega el diseño muestral\n\nTask: #11\nSection: methods\nAI-Use: drafting, editing\n"
        "Assisted-by: claude-code:claude-opus-5-5"
    )
    assert remote_head(world.repo, "task/11") == world.repo.head() == data["commit"]["sha"]  # type: ignore[index]
    assert _forge_calls(world) == []  # en modo git, save no lee ni escribe la forja (08 §7.5)


def test_without_declaration_the_use_is_undeclared(world: World) -> None:
    world.repo.write({METHODS: "% cambio\n"})
    assert world.run("save", message="Cambio").ok
    assert message(world.repo).endswith("Task: #11\nSection: methods\nAI-Use: undeclared")


def test_the_agent_and_the_session_model_are_detected(world: World) -> None:
    sessions = world.repo.path / _meta.CACHE_DIR / "state" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "s-1.json").write_text(json.dumps({"session_id": "s-1", "tool": "claude-code",
                                                   "model": "claude-opus-5-5", "model_event": "SessionStart"}))
    world.repo.write({METHODS: "% con agente\n"})
    env = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s-1"}
    result = world.run("save", message="Con agente", ai_use=["code"], env=env)
    assert result.ok and message(world.repo).endswith("AI-Use: code\nAssisted-by: claude-code:claude-opus-5-5")
    # REQ-TRACE-17: otro modelo declarado para la misma herramienta: las dos entradas y un aviso
    world.repo.write({METHODS: "% otro modelo\n"})
    result = world.run("save", message="Otro", ai_use=["code"], ai_model="claude-fable-5-1", env=env)
    assert [w.code for w in result.warnings] == ["W_VALIDATION_MODEL_MISMATCH"]
    assert message(world.repo).endswith(
        "Assisted-by: claude-code:claude-fable-5-1\nAssisted-by: claude-code:claude-opus-5-5")
    # ai_use none: sin Assisted-by y con aviso si se detectó un agente (11 §2.6)
    world.repo.write({METHODS: "% a mano\n"})
    result = world.run("save", message="A mano", ai_use=["none"], env=env)
    assert [w.code for w in result.warnings] == ["W_VALIDATION_NONE_WITH_AGENT"]
    assert message(world.repo).endswith("AI-Use: none")


def test_out_of_scope_changes_are_denied_without_commit(world: World) -> None:
    head = world.repo.head()
    world.repo.write({METHODS: "% ok\n", "sections/results/results.tex": "% ajena\n", "plan.md": "# Otro plan\n"})
    result = world.run("save", message="Mezcla")
    assert not result.ok and result.error.code == "E_PERM_PATH_DENIED"  # type: ignore[union-attr]
    paths = {p["path"]: p["reason_code"] for p in result.error.details["paths"]}  # type: ignore[union-attr]
    assert paths == {"plan.md": "E_PERM_LEAD_ONLY_PATH", "sections/results/results.tex": "E_PERM_OUTSIDE_TASK"}
    assert result.data["failed_step"] == "scope_check" and world.repo.head() == head  # type: ignore[index]
    # limitado a --path, guarda solo lo permitido y deja lo demás sin guardar
    result = world.run("save", message="Solo métodos", paths=[METHODS])
    assert result.ok and result.data["commit"]["files"] == [METHODS]  # type: ignore[index]
    assert "plan.md" in world.repo.git("status", "--porcelain")


def test_preconditions(world: World, tmp_path: Path) -> None:
    world.repo.write({METHODS: "% x\n"})
    assert world.run("save", message="x", login="bruno").error.code == "E_CLAIM_NOT_HELD"  # type: ignore[union-attr]
    assert world.run("save", message="  ").error.code == "E_VALIDATION_INPUT"  # type: ignore[union-attr]
    bad = world.run("save", message="x", ai_use=["none", "code"])
    assert bad.error.details["errors"] == [{"param": "ai_use", "reason": "solo"}]  # type: ignore[union-attr]
    world.repo.git("stash", "-q")
    world.repo.git("switch", "-q", "main")
    assert world.run("save", message="x").error.code == "E_STATE_NOT_ON_TASK_BRANCH"  # type: ignore[union-attr]
    world.repo.git("switch", "-q", "-c", "task/99")
    result = world.run("save", message="x")
    assert result.error.code == "E_CONFIG_POLICY_UNAVAILABLE"  # type: ignore[union-attr]
    assert result.error.details == {"reason": "task_cache_missing", "task": 99}  # type: ignore[union-attr]


def test_a_missing_remote_ref_is_not_recreated(world: World) -> None:
    """REQ-TASK-32: si la ref no está en el remoto, E_CLAIM_NOT_HELD y no se empuja."""
    world.repo.git("push", "-q", "origin", "--delete", "task/11")
    world.repo.write({METHODS: "% x\n"})
    head = world.repo.head()
    result = world.run("save", message="x")
    assert result.error.code == "E_CLAIM_NOT_HELD"  # type: ignore[union-attr]
    assert result.error.details == {"task": 11, "reason": "ref_missing"}  # type: ignore[union-attr]
    assert world.repo.head() == head and remote_head(world.repo, "task/11") is None


def test_a_replaced_claim_is_not_pushed_onto(world: World, tmp_path: Path) -> None:
    """La ref se volvió a crear con otro reclamo: no se integra ni se empuja encima."""
    other = clone(world, tmp_path, "bruno")
    other.git("switch", "-q", "-c", "task/11")
    other.commit(f"{_meta.CLAIM_SUBJECT}11\n\nclaim-nonce otro\n\nTask: #11\nAI-Use: none", allow_empty=True)
    other.git("push", "-q", "--force", "origin", "task/11")
    world.repo.write({METHODS: "% x\n"})
    result = world.run("save", message="x")
    assert result.error.details == {"task": 11, "reason": "claim_replaced"}  # type: ignore[union-attr]


def test_a_rewritten_base_stops_until_reclone(world: World, tmp_path: Path) -> None:
    """REQ-SEC-74: el oid anterior no es ancestro del nuevo; la ref de seguimiento no se mueve."""
    before = world.repo.git("rev-parse", "origin/main")
    other = clone(world, tmp_path, "rewrite")
    other.git("checkout", "-q", "--orphan", "nueva")
    other.commit("historia nueva", files={"README.md": "x\n"})
    other.git("push", "-q", "--force", "origin", "nueva:main")
    world.repo.write({METHODS: "% x\n"})
    for _ in range(2):  # repetir vuelve a fallar
        result = world.run("save", message="x")
        assert result.error.code == "E_STATE_BASE_REWRITTEN"  # type: ignore[union-attr]
        assert world.repo.git("rev-parse", "origin/main") == before
    assert "sections/methods/methods.tex" in world.repo.git("status", "--porcelain")  # sin commit


def test_offline_commits_locally_and_a_retry_pushes(world: World) -> None:
    world.repo.write({METHODS: "% sin red\n"})
    result = world.run("save", message="Sin red", env={P + "OFFLINE": "1"})
    assert result.ok and [w.code for w in result.warnings] == ["W_NET_OFFLINE"]
    assert (result.data["pushed"], result.data["ahead"]) == (False, 1)  # type: ignore[index]
    again = world.run("save", message="Otra vez")  # sin cambios: sube lo pendiente (05 §9.14)
    assert again.ok and again.data["commit"] is None and again.data["pushed"] is True  # type: ignore[index]
    assert remote_head(world.repo, "task/11") == world.repo.head()
    idle = world.run("save", message="Nada")
    assert (idle.data["commit"], idle.data["pushed"]) == (None, False)  # type: ignore[index]


def test_work_from_another_machine_is_merged_before_pushing(world: World, tmp_path: Path) -> None:
    """REQ-TASK-49: el remoto avanzó; se fusiona en la local, nunca se fuerza."""
    other = clone(world, tmp_path, "laptop", branch="task/11")
    other.commit("Desde la otra máquina", files={"sections/methods/otra.tex": "% otra\n"})
    other.push("task/11")
    world.repo.write({METHODS: "% aquí\n"})
    result = world.run("save", message="Desde aquí")
    assert result.ok and result.data["pushed"] is True  # type: ignore[index]
    head = world.repo.head()
    assert remote_head(world.repo, "task/11") == head
    assert len(world.repo.git("log", "-1", "--format=%P").split()) == 2  # commit de fusión
    assert message(world.repo).endswith("Task: #11")  # la fusión solo lleva Task (11 §2.5)
    assert (world.repo.path / "sections/methods/otra.tex").exists()


def test_a_conflict_with_the_other_machine_stops_with_the_merge_open(world: World, tmp_path: Path) -> None:
    other = clone(world, tmp_path, "laptop", branch="task/11")
    other.commit("Allá", files={METHODS: "% allá\n"})
    other.push("task/11")
    world.repo.write({METHODS: "% acá\n"})
    result = world.run("save", message="Acá")
    assert result.error.code == "E_STATE_MERGE_CONFLICT"  # type: ignore[union-attr]
    data = result.data
    assert data["failed_step"] == "integrate_remote" and data["resolvable"] is True  # type: ignore[index]
    assert data["conflicts"] == [{"path": METHODS, "class": "section", "resolution": "agent"}]  # type: ignore[index]
    assert world.repo.git("rev-parse", "-q", "--verify", "MERGE_HEAD", check=False)
    assert world.run("save", message="otra").error.code == "E_STATE_MERGE_IN_PROGRESS"  # type: ignore[union-attr]
    world.repo.write({METHODS: "% acá y allá\n"})
    finished = world.run("update", **{"continue": True})
    assert finished.ok and finished.data["pushed"] is True  # type: ignore[index]
    assert remote_head(world.repo, "task/11") == world.repo.head()


def test_an_expired_claim_is_renewed(tmp_path: Path) -> None:
    w = claimed(tmp_path, state="expired")
    w.repo.write({METHODS: "% renueva\n"})
    w.forge.calls.clear()
    result = w.run("save", message="Renueva")
    assert result.ok
    assert [c.method for c in w.forge.calls.entries() if c.kind in ("E", "C")] == ["remove_label"]
    assert "state:expired" not in w.forge.get_issue(11).labels  # type: ignore[union-attr]
    assert Store(w.repo.path).task(11).state == "claimed"  # type: ignore[union-attr]


def test_dry_run_changes_nothing(world: World) -> None:
    world.repo.write({METHODS: "% plan\n"})
    head = world.repo.head()
    result = world.run("save", message="Plan", dry_run=True)
    assert result.ok and result.data["dry_run"] is True  # type: ignore[index]
    assert [e["kind"] for e in result.data["plan"]] == ["git_commit", "git_push"]  # type: ignore[index]
    assert world.repo.head() == head and "methods.tex" in world.repo.git("status", "--porcelain")


def test_the_cloud_session_branch_pushes_to_the_task(tmp_path: Path) -> None:
    """04 §11.3: en la nube la sesión sigue en su rama, ligada a la tarea por local_branch."""
    w = claimed(tmp_path)
    w.repo.git("switch", "-q", "-c", "claude/sesion", "origin/main")
    write_task_cache(Store(w.repo.path), 11, REPO, claimant="carla", state="claimed", sections=("methods",),
                     now=NOW, local_branch="claude/sesion")
    w.repo.write({METHODS: "% desde la web\n"})
    result = w.run("save", message="Desde la web", env={"CLAUDE_CODE_REMOTE": "true"})
    assert result.ok and result.data["pushed"] is True  # type: ignore[index]
    assert remote_head(w.repo, "task/11") == w.repo.head()
    assert w.repo.git("rev-parse", "--abbrev-ref", "HEAD") == "claude/sesion"


def test_a_long_file_list_is_trimmed_to_the_budget(world: World) -> None:
    """05 §7.1: el sobre cabe en 4 000 caracteres; commit.files se recorta con W_OUTPUT_TRUNCATED."""
    from aucove.ops.fit import envelope_size

    world.repo.write({f"sections/methods/figuras/figura-con-un-nombre-largo-{i:03d}.tex": "% f\n" for i in range(120)})
    result = world.run("save", message="Figuras")
    assert result.ok
    (warning,) = [w for w in result.warnings if w.code == "W_OUTPUT_TRUNCATED"]
    kept = result.data["commit"]["files"]  # type: ignore[index]
    assert warning.details["omitted"] == {"commit.files": 120 - len(kept)} and 0 < len(kept) < 120  # type: ignore[index]
    assert envelope_size("save", result.data, result.warnings) <= 4000


def test_the_fake_forge_never_talks_to_a_real_remote(world: World) -> None:
    """REQ-FORGE-62: con la forja falsa, git no trae ni empuja contra un remoto que no sea local."""
    world.repo.git("remote", "set-url", "origin", "https://example.invalid/demo.git")
    world.repo.write({METHODS: "% x\n"})
    env = {P + "TESTING": "1", P + "FORGE_ADAPTER": "fake"}
    result = world.run("save", message="x", env=env)
    assert result.error.code == "E_CONFIG_INVALID"  # type: ignore[union-attr]
    assert result.error.details == {"field": "remote.origin.url", "reason": "fake_forge_remote"}  # type: ignore[union-attr]
