# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``init --repo`` desde una carpeta vacía (05 §9.3; 00-marco D-73; 12-mvp A6, A12 y A13).

La carpeta empieza vacía y el repo de la forja, sin ramas. ``init`` escribe ``origin`` como
``https://github.com/lab-x/estudio.git``, y git llega con esa URL a un repo bare local por
``url.<base>.insteadOf``, en el entorno (las pruebas no ejecutan ``git config``). El falso hace de
GitHub, con ``ana`` como administradora, como en ``test_op_init``.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from aucove import _meta
from aucove.forge.fake import FakeClock, FakeForge, Fault
from aucove.ops.result import Result
from tests.board_world import NOW, Typed, World, clean_env
from tests.conftest import GitRepo, isolated_git_env
from tests.unit.test_op_init import _no_wait as _no_wait  # noqa: F401 - fixture autouse
from tests.unit.test_op_init import LEAD, REPO, Terminal, init_world, protections, run_init, valid

SLUG = f"{REPO.owner}/{REPO.name}"
URL = f"https://github.com/{SLUG}.git"


def git(cwd: Path, env: Mapping[str, str], *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, env=dict(env), capture_output=True, check=True)
    return proc.stdout.decode("utf-8", "replace").strip()


def fresh_world(tmp: Path, *, default_branch: str = "main", folder: Path | None = None) -> World:
    """La carpeta vacía, el repo bare sin ramas al que lleva la URL de GitHub y el falso."""
    folder = folder if folder is not None else tmp / "estudio"
    folder.mkdir(parents=True, exist_ok=True)
    env = isolated_git_env(tmp / "git-env")
    origin = tmp / "origin.git"
    git(tmp, env, "init", "-q", "--bare", str(origin))
    env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.{origin.as_uri()}.insteadOf",
                "GIT_CONFIG_VALUE_0": URL})
    forge = FakeForge(REPO, viewer=LEAD, clock=FakeClock(NOW), profile="enforced", default_branch=default_branch)
    forge.seed_user(LEAD, permission="admin")
    forge.seed_admin(fresh=True)
    return World(repo=GitRepo(folder, origin, env), forge=forge, base_sha="", env=clean_env(env))


def run_repo(w: World, term: Terminal | None = None, **kwargs: object) -> Result:
    return run_init(w, term, repo=SLUG, **kwargs)


def folder_names(w: World) -> list[str]:
    return sorted(os.listdir(w.repo.path))


def remote_heads(w: World) -> str:
    return git(w.repo.path.parent, w.repo.env, "ls-remote", "--heads", str(w.repo.origin))


def test_init_repo_creates_the_project_in_an_empty_folder(tmp_path: Path) -> None:
    w = fresh_world(tmp_path)
    typed = Typed()
    result = valid(run_repo(w, typed=typed, language="es"))
    assert result.ok, result.error
    data = result.data
    assert data["repo"] == SLUG and data["profile"] == "enforced" and data["pushed"] is True
    files = {f["path"]: f["action"] for f in data["files"]}
    assert set(files.values()) == {"created"}
    assert {"project.yaml", "roles.yaml", ".mcp.json", ".github/CODEOWNERS", "LICENSE", "REUSE.toml"} <= set(files)
    assert typed.seen[0][:2] == ("init", SLUG)
    assert str(w.repo.path) in typed.seen[0][2] and URL in typed.seen[0][2]  # la carpeta y origin, en el resumen
    # git: un solo commit en origin/main con AI-Use: none, el árbol limpio y origin como en un clon
    assert w.repo.git("rev-list", "--count", "origin/main") == "1"
    assert w.repo.git("rev-parse", "HEAD") == w.repo.git("rev-parse", "origin/main")
    assert w.repo.git("log", "-1", "--format=%B").rstrip().endswith("AI-Use: none")
    assert w.repo.git("status", "--porcelain") == ""
    assert w.repo.git("symbolic-ref", "refs/remotes/origin/HEAD") == "refs/remotes/origin/main"
    config = (w.repo.path / ".git" / "config").read_text(encoding="utf-8")
    assert f"url = {URL}" in config and "merge = refs/heads/main" in config
    assert "refs/heads/main" in remote_heads(w)
    assert sorted(files) == sorted(w.repo.git("ls-files").splitlines())
    # la forja, como con init
    prot = protections(result)
    assert prot[f"ruleset:{_meta.NAME}-base"] == "applied" and prot["environment"] == "applied"
    assert {lab["action"] for lab in data["labels"]} == {"created"}
    assert data["setup_task"] is not None
    # A12: el árbol que deja pasa validate
    check = w.run("validate", login=LEAD)
    assert check.ok, check.error


def test_the_same_tree_as_from_the_published_template(tmp_path: Path) -> None:
    """00-marco D-73: la plantilla en memoria más el plan de init dan el árbol de «Use this template» e init."""
    fresh = fresh_world(tmp_path / "vacia")
    assert run_repo(fresh, language="es").ok
    usual = init_world(tmp_path / "plantilla")
    assert run_init(usual, language="es").ok
    assert fresh.repo.git("ls-tree", "-r", "HEAD") == usual.repo.git("ls-tree", "-r", "HEAD")


def test_a_second_run_takes_the_usual_path_and_changes_nothing(tmp_path: Path) -> None:
    w = fresh_world(tmp_path)
    first = run_repo(w, language="es")
    assert first.ok
    w.forge.calls.clear()
    again = valid(run_repo(w))
    assert again.ok and again.data["files"] == [] and again.data["pushed"] is False
    assert again.data["setup_task"] == first.data["setup_task"]
    assert w.forge.calls.writes() == []  # REQ-PERM-25: lee, compara y no escribe


def test_a_forge_failure_before_the_files_leaves_the_folder_empty(tmp_path: Path) -> None:
    """A13: la falla llega antes de escribir; repetir vuelve a empezar desde la carpeta vacía."""
    w = fresh_world(tmp_path)
    w.forge.faults.add(Fault("ensure_labels", "http_5xx", nth=1))
    failed = run_repo(w, language="es")
    assert not failed.ok and failed.data["failed_step"] == "labels"
    assert folder_names(w) == [] and remote_heads(w) == ""
    again = valid(run_repo(w, language="es"))
    assert again.ok and again.data["pushed"] is True


def test_a_failure_after_the_push_converges_by_repeating(tmp_path: Path) -> None:
    """A13: la carpeta ya es el proyecto con ese origin, así que repetir sigue el camino de siempre."""
    w = fresh_world(tmp_path)
    w.forge.faults.add(Fault("create_issue", "http_5xx", nth=1))
    failed = run_repo(w, language="es")
    assert not failed.ok and failed.data["failed_step"] == "setup_task"
    assert (w.repo.path / "project.yaml").is_file() and "refs/heads/main" in remote_heads(w)
    again = valid(run_repo(w))
    assert again.ok and again.data["files"] == [] and again.data["setup_task"] is not None


def test_a_rejected_push_leaves_the_folder_empty(tmp_path: Path) -> None:
    """05 §9.3: si la forja no acepta el push, init borra lo que creó; repetir vuelve a empezar."""
    w = fresh_world(tmp_path)
    hook = w.repo.origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'rechazado por la prueba' >&2\nexit 1\n", encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    failed = run_repo(w, language="es")
    assert not failed.ok and failed.error.code == "E_FORGE_PUSH_REJECTED"
    assert failed.data["failed_step"] == "push"
    assert folder_names(w) == [] and remote_heads(w) == ""
    hook.unlink()
    assert valid(run_repo(w, language="es")).ok


def _without_identity(w: World) -> None:
    w.env = {k: v for k, v in w.env.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
    w.env.update({"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_1": "user.useConfigOnly", "GIT_CONFIG_VALUE_1": "true"})


def _seed_remote(w: World, tmp: Path) -> None:
    seed = tmp / "seed"
    git(tmp, w.repo.env, "clone", "-q", str(w.repo.origin), str(seed))
    (seed / "README.md").write_text("x\n", encoding="utf-8")
    git(seed, w.repo.env, "add", "README.md")
    git(seed, w.repo.env, "commit", "-q", "-m", "README")
    git(seed, w.repo.env, "push", "-q", "origin", "HEAD:refs/heads/main")


@pytest.mark.parametrize(("case", "code", "reason"), [
    ("folder", "E_STATE_DIRTY_WORKTREE", "folder_not_empty"),
    ("repository", "E_CONFIG_REMOTE_MISMATCH", "not_empty"),
    ("identity", "E_CONFIG_GIT_UNAVAILABLE", "identity_missing"),
    ("other_repo", "E_CONFIG_REMOTE_MISMATCH", "repo_mismatch"),
])
def test_refusals_write_nothing(tmp_path: Path, case: str, code: str, reason: str) -> None:
    w = fresh_world(tmp_path)
    repo = SLUG
    if case == "folder":
        (w.repo.path / "notas.txt").write_text("x\n", encoding="utf-8")
    elif case == "repository":
        _seed_remote(w, tmp_path)
    elif case == "identity":
        _without_identity(w)
    else:
        repo = f"{REPO.owner}/otro"  # el falso es lab-x/estudio
    before = folder_names(w)
    heads = remote_heads(w)
    result = run_init(w, language="es", repo=repo)
    assert not result.ok and result.error.code == code and result.error.details["reason"] == reason
    assert folder_names(w) == before and remote_heads(w) == heads
    assert w.forge.calls.writes() == []
    if case == "folder":
        assert result.error.details["entries"] == ["notas.txt"]


def test_another_origin_is_refused(tmp_path: Path) -> None:
    w = fresh_world(tmp_path)
    assert run_repo(w, language="es").ok
    other = run_init(w, repo=f"{REPO.owner}/otro")
    assert other.error is not None and other.error.code == "E_CONFIG_REMOTE_MISMATCH"
    assert other.error.details == {"remote": "origin", "reason": "repo_mismatch"}


def test_system_files_do_not_count(tmp_path: Path) -> None:
    """Los ignora el ``.gitignore`` de la plantilla: la carpeta cuenta como vacía y quedan sin rastrear."""
    w = fresh_world(tmp_path)
    (w.repo.path / ".DS_Store").write_bytes(b"\0\1")
    assert valid(run_repo(w, language="es")).ok
    assert w.repo.git("status", "--porcelain") == "" and (w.repo.path / ".DS_Store").is_file()


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    w = fresh_world(tmp_path)
    result = valid(run_repo(w, language="es", dry_run=True))
    assert result.ok and result.data["dry_run"] is True
    plan = result.data["plan"]
    assert {e["kind"] for e in plan} >= {"file_write", "local_write", "git_commit", "git_push", "forge_update",
                                         "forge_create"}
    assert any(e["kind"] == "local_write" and e["target"] == ".git" and URL in e["summary"] for e in plan)
    assert any(e["kind"] == "file_write" and e["target"] == "project.yaml" for e in plan)
    assert folder_names(w) == [] and remote_heads(w) == ""
    assert w.forge.calls.writes() == []


def test_only_a_person_in_a_terminal(tmp_path: Path) -> None:
    """A6: sin TTY, en una sesión de agente o sin la confirmación, nada se escribe."""
    w = fresh_world(tmp_path)
    no_tty = w.run("init", login=LEAD, inv={"surface": "cli", "tty": False, "confirm": Typed()}, repo=SLUG)
    assert no_tty.error is not None and no_tty.error.code == "E_HUMAN_ONLY_NO_TTY"
    agent = w.run("init", login=LEAD, env={"CLAUDE_CODE_CHILD_SESSION": "1"},
                  inv={"surface": "cli", "tty": True, "confirm": Typed()}, repo=SLUG)
    assert agent.error is not None and agent.error.code == "E_HUMAN_ONLY_AGENT_ENV"
    refused = run_repo(w, typed=Typed("no"), language="es")
    assert refused.error is not None and refused.error.code == "E_HUMAN_ONLY_NOT_CONFIRMED"
    assert folder_names(w) == [] and remote_heads(w) == "" and w.forge.calls.writes() == []


def test_the_base_is_the_default_branch_of_the_empty_repository(tmp_path: Path) -> None:
    w = fresh_world(tmp_path, default_branch="trunk")
    result = valid(run_repo(w, language="en"))
    assert result.ok, result.error
    project = (w.repo.path / "project.yaml").read_text(encoding="utf-8")
    assert "  base_branch: trunk" in project
    assert w.repo.git("symbolic-ref", "HEAD") == "refs/heads/trunk"
    assert w.repo.git("symbolic-ref", "refs/remotes/origin/HEAD") == "refs/remotes/origin/trunk"
    seal = (w.repo.path / ".github" / "workflows" / "seal.yml").read_text(encoding="utf-8")
    assert "branches: [trunk]" in seal and "branches: [main]" not in seal
    assert protections(result)[f"ruleset:{_meta.NAME}-base"] == "applied"


def test_a_folder_inside_another_project(tmp_path: Path) -> None:
    """La carpeta vacía dentro de otro proyecto: init no lee ni escribe en el de afuera."""
    outer = init_world(tmp_path / "afuera")
    w = fresh_world(tmp_path / "adentro", folder=outer.repo.path / "nuevo")
    result = valid(run_repo(w, language="es"))
    assert result.ok, result.error
    assert Path(w.repo.git("rev-parse", "--show-toplevel")).resolve() == w.repo.path.resolve()
    assert outer.repo.git("status", "--porcelain") == "?? nuevo/"
    assert not (outer.repo.path / _meta.CACHE_DIR).exists()


def test_the_repo_argument(tmp_path: Path) -> None:
    w = fresh_world(tmp_path)
    bad = run_init(w, repo="sin-dueno")
    assert bad.error is not None and bad.error.code == "E_VALIDATION_INPUT"
    both = run_init(w, repo=SLUG, regenerate=True)
    assert both.error is not None and both.error.details["errors"][0]["with"] == ["repo"]
    assert folder_names(w) == [] and w.forge.calls.writes() == []
