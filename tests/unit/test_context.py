# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Contexto de operación, resolución del proyecto y repos de prueba aislados."""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aucove import _meta
from aucove.ops.context import (
    Invocation,
    OpContext,
    Project,
    context_factory,
    env_flag,
    resolve_project,
    utcnow,
)
from aucove.ops.context import testing as is_testing
from aucove.ops.result import OpError
from tests.conftest import GitRepo, isolated_git_env

P = _meta.ENV_PREFIX
PKG = _meta.NAME


def _inv(**kw: object) -> Invocation:
    base: dict[str, object] = {"root": None, "lang": None, "json": False, "dry_run": False, "surface": "cli", "env": {}}
    base.update(kw)
    return Invocation(**base)  # type: ignore[arg-type]


def _make_project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "project.yaml").write_text("convention: '0.1'\n", encoding="utf-8")
    (root / ".git").mkdir()
    return root


def test_utcnow_is_aware() -> None:
    assert utcnow().tzinfo is timezone.utc


def test_invocation_defaults() -> None:
    inv = Invocation(root=None, lang=None, json=True, dry_run=False, surface="mcp")
    assert inv.human_confirmed is False
    assert isinstance(inv.env, dict) and inv.cwd
    assert inv.now().tzinfo is timezone.utc


def test_resolve_project_walks_up(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    deep = root / "sections" / "methods"
    deep.mkdir(parents=True)
    project = resolve_project(str(deep))
    assert project == Project(root=root.resolve(), git_dir=(root / ".git").resolve(), common_dir=(root / ".git").resolve())
    assert resolve_project(str(root)) == project


def test_resolve_project_requires_both_markers(tmp_path: Path) -> None:
    only_yaml = tmp_path / "only_yaml"
    only_yaml.mkdir()
    (only_yaml / "project.yaml").write_text("x: 1\n", encoding="utf-8")
    only_git = tmp_path / "only_git"
    (only_git / ".git").mkdir(parents=True)
    assert resolve_project(str(only_yaml)) is None
    assert resolve_project(str(only_git)) is None
    # Un project.yaml sin .git dentro de un proyecto real no corta la búsqueda.
    outer = _make_project(tmp_path / "outer")
    inner = outer / "sub"
    inner.mkdir()
    (inner / "project.yaml").write_text("x: 1\n", encoding="utf-8")
    assert resolve_project(str(inner)).root == outer.resolve()  # type: ignore[union-attr]


def test_resolve_project_git_file_worktree(tmp_path: Path) -> None:
    main = _make_project(tmp_path / "main")
    wt_git = main / ".git" / "worktrees" / "wt"
    wt_git.mkdir(parents=True)
    (wt_git / "commondir").write_text("../..\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "project.yaml").write_text("x: 1\n", encoding="utf-8")
    (wt / ".git").write_text("gitdir: ../main/.git/worktrees/wt\n", encoding="utf-8")
    project = resolve_project(str(wt))
    assert project is not None
    assert project.root == wt.resolve()
    assert project.git_dir.resolve() == wt_git.resolve()
    assert project.common_dir.resolve() == (main / ".git").resolve()


def test_resolve_project_invalid_git_file(tmp_path: Path) -> None:
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "project.yaml").write_text("x: 1\n", encoding="utf-8")
    (proj / ".git").write_text("no es un gitdir\n", encoding="utf-8")
    assert resolve_project(str(proj)) is None


def test_resolve_project_real_worktree(git_repo: GitRepo) -> None:
    git_repo.commit("init", files={"project.yaml": "convention: '0.1'\n"})
    wt = git_repo.path.parent / "wt"
    git_repo.git("worktree", "add", "-q", "-b", "task/7", str(wt))
    project = resolve_project(str(wt / "project.yaml"))
    assert project is not None and project.root == wt.resolve()
    assert (wt / ".git").is_file()
    assert project.common_dir.resolve() == (git_repo.path / ".git").resolve()


def test_ctx_project_required(tmp_path: Path) -> None:
    ctx = OpContext(_inv(cwd=str(tmp_path)))
    assert ctx.project(required=False) is None
    with pytest.raises(OpError) as info:
        ctx.project()
    assert info.value.code == "E_CONFIG_NOT_A_PROJECT"


def test_ctx_project_root_relative_to_cwd(tmp_path: Path) -> None:
    root = _make_project(tmp_path / "proj")
    ctx = OpContext(_inv(root="proj", cwd=str(tmp_path)))
    assert ctx.project().root == root.resolve()  # type: ignore[union-attr]


def test_ctx_language(tmp_path: Path) -> None:
    assert OpContext(_inv(lang="es", cwd=str(tmp_path))).t().lang == "es"
    assert OpContext(_inv(env={P + "LANG": "es"}, cwd=str(tmp_path))).t().lang == "es"
    # Sin proyecto ni variables: en, y nunca el locale del sistema.
    assert OpContext(_inv(env={"LANG": "es_ES.UTF-8"}, cwd=str(tmp_path))).t().lang == "en"


def test_ctx_language_from_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _make_project(tmp_path / "proj")
    ctx = OpContext(_inv(cwd=str(root)))
    monkeypatch.setattr(ctx, "base", lambda: _Snapshot(_Cfg(language="es", forge_type="github")))
    assert ctx.t().lang == "es"
    broken = OpContext(_inv(cwd=str(root)))

    def fail_base() -> object:
        raise OpError("E_CONFIG_POLICY_UNAVAILABLE")

    monkeypatch.setattr(broken, "base", fail_base)
    assert broken.t().lang == "en"


@pytest.mark.parametrize(
    "tree_project",
    ["core: >=0.1.0,<0.2\n", "core: [\n", b"\xff\xfe no es utf-8\n"],
    ids=["unquoted_core", "unclosed_flow", "not_utf8"],
)
def test_ctx_base_does_not_read_the_tree_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tree_project: str | bytes
) -> None:
    """La rama base no sale del árbol (07-hooks REQ-HOOK-41): ``read_base`` la resuelve sola."""
    import importlib

    root = _make_project(tmp_path / "proj")
    data = tree_project if isinstance(tree_project, bytes) else tree_project.encode("utf-8")
    (root / "project.yaml").write_bytes(data)  # inválido o ilegible: no debe importar
    ctx = OpContext(_inv(cwd=str(root)))
    git = object()
    snapshot = _Snapshot(_Cfg())
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_read_base(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        return snapshot

    base_mod = importlib.import_module(f"{PKG}.config.base")
    project_mod = importlib.import_module(f"{PKG}.config.project")
    monkeypatch.setattr(base_mod, "read_base", fake_read_base)

    def no_parse(*args: object, **kwargs: object) -> object:
        raise AssertionError("OpContext.base() no debe analizar el project.yaml del árbol")

    monkeypatch.setattr(project_mod, "parse_project", no_parse)
    monkeypatch.setattr(ctx, "git", lambda: git)
    assert ctx.base() is snapshot
    assert ctx.base() is snapshot  # cacheada
    assert calls == [((git,), {"t": None})]  # sin base_branch: no lo decide el árbol


def test_env_helpers() -> None:
    env = {P + "OFFLINE": "1", P + "TESTING": "1"}
    assert env_flag(env, "OFFLINE") == "1"
    assert env_flag(env, "MISSING") is None
    assert is_testing(env) is True
    assert is_testing({P + "TESTING": "true"}) is False
    assert is_testing({}) is False


@dataclass
class _Cfg:
    language: str = "en"
    forge_type: str = "github"


@dataclass
class _Snapshot:
    project: _Cfg


class _FakeForgeStub:
    loads: list[Path] = []
    saves: list[Path] = []

    @classmethod
    def load(cls, path: Path) -> _FakeForgeStub:
        cls.loads.append(path)
        return cls()

    def save(self, path: Path) -> None:
        type(self).saves.append(path)


def _install_fake(monkeypatch: pytest.MonkeyPatch) -> type[_FakeForgeStub]:
    _FakeForgeStub.loads = []
    _FakeForgeStub.saves = []
    module = types.ModuleType(f"{PKG}.forge.fake")
    module.FakeForge = _FakeForgeStub  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, f"{PKG}.forge.fake", module)
    return _FakeForgeStub


def test_factory_ignores_adapter_without_testing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake(monkeypatch)
    monkeypatch.setitem(sys.modules, f"{PKG}.forge.github", None)  # adaptador REST ausente (H3)
    state = tmp_path / "state.json"
    ctx = OpContext(_inv(env={P + "FORGE_ADAPTER": "fake", P + "FORGE_FAKE_STATE": str(state)}))
    monkeypatch.setattr(ctx, "base", lambda: _Snapshot(_Cfg(forge_type="github")))
    with pytest.raises(OpError) as info:
        context_factory(ctx)
    assert info.value.code == "E_FORGE_CAPABILITY_MISSING"
    assert fake.loads == []
    ctx.close()
    assert fake.saves == []


def test_factory_unknown_forge_type(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = OpContext(_inv())
    monkeypatch.setattr(ctx, "base", lambda: _Snapshot(_Cfg(forge_type="gitlab")))
    with pytest.raises(OpError) as info:
        ctx.forge()
    assert info.value.code == "E_FORGE_CAPABILITY_MISSING"


def test_factory_github_uses_create_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    module = types.ModuleType(f"{PKG}.forge.github")
    module.create = lambda ctx: sentinel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, f"{PKG}.forge.github", module)
    ctx = OpContext(_inv())
    monkeypatch.setattr(ctx, "base", lambda: _Snapshot(_Cfg(forge_type="github")))
    assert ctx.forge() is sentinel
    assert ctx.forge() is sentinel  # cacheado


def test_factory_fake_with_testing_saves_on_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake(monkeypatch)
    state = tmp_path / "state.json"
    env = {P + "TESTING": "1", P + "FORGE_ADAPTER": "fake", P + "FORGE_FAKE_STATE": str(state)}
    ctx = OpContext(_inv(env=env))
    forge = ctx.forge()
    assert isinstance(forge, _FakeForgeStub) and ctx.forge() is forge
    assert fake.loads == [state]
    ctx.close()
    ctx.close()  # idempotente
    assert fake.saves == [state]


def test_factory_fake_without_state_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake(monkeypatch)
    ctx = OpContext(_inv(env={P + "TESTING": "1", P + "FORGE_ADAPTER": "fake"}))
    with pytest.raises(OpError) as info:
        ctx.forge()
    assert info.value.code == "E_FORGE_CAPABILITY_MISSING"


def test_forge_admin_uses_the_fake_view_or_github(monkeypatch: pytest.MonkeyPatch) -> None:
    """08 §2.2 (00-marco D-62): el falso da su vista; con GitHub, ``create_admin`` con el token tecleado."""
    from aucove.forge.base import RepoRef
    from aucove.forge.fake import FakeAdmin, FakeForge

    fake = FakeForge(RepoRef("lab", "articulo"), "ana")
    ctx = OpContext(_inv(), forge=fake)
    admin = ctx.forge_admin("tecleado")
    assert isinstance(admin, FakeAdmin) and admin.repo == fake.repo
    ctx.close()

    closed: list[str] = []

    class Admin:
        repo = RepoRef("lab", "articulo")

        def close(self) -> None:
            closed.append("closed")

    module = types.ModuleType(f"{PKG}.forge.github")
    module.create = lambda ctx: object()  # type: ignore[attr-defined]
    module.create_admin = lambda ctx, token: (token, Admin())[1]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, f"{PKG}.forge.github", module)
    ctx = OpContext(_inv())
    monkeypatch.setattr(ctx, "base", lambda: _Snapshot(_Cfg(forge_type="github")))
    assert isinstance(ctx.forge_admin("tecleado"), Admin)
    ctx.close()
    ctx.close()
    assert closed == ["closed"]  # close() cierra el adaptador de administración una vez


def test_injected_forge_is_not_saved(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install_fake(monkeypatch)
    injected = _FakeForgeStub()
    ctx = OpContext(_inv(), forge=injected)  # type: ignore[arg-type]
    assert ctx.forge() is injected
    ctx.close()
    assert fake.saves == []


# -- Repos de prueba aislados ---------------------------------------------------


def test_git_repo_is_isolated(git_repo: GitRepo) -> None:
    env = git_repo.env
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    global_config = Path(env["GIT_CONFIG_GLOBAL"])
    assert global_config.is_file() and global_config.read_bytes() == b""
    sha = git_repo.commit("first", files={"a.txt": "hola\r\nmundo\n"})
    assert (git_repo.path / "a.txt").read_bytes() == b"hola\nmundo\n"
    stamp = int(datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc).timestamp())
    assert git_repo.git("log", "-1", "--format=%an <%ae>|%cn|%at|%ct") == (
        f"Test Author <author@example.invalid>|Test Committer|{stamp}|{stamp}"
    )
    assert len(sha) == 40 and git_repo.head() == sha
    assert global_config.read_bytes() == b""  # nada escribió en la configuración global
    assert all(call[0] != "config" for call in git_repo.calls)
    with pytest.raises(AssertionError):
        git_repo.git("config", "user.name", "x")


def test_git_repo_is_deterministic(tmp_path: Path) -> None:
    shas = []
    for name in ("a", "b"):
        base = tmp_path / name
        base.mkdir()
        env = isolated_git_env(base / "env")
        repo = GitRepo(base / "repo", base / "origin.git", env)
        (base / "repo").mkdir()
        repo.git("init", "-q", "-b", "main")
        shas.append(repo.commit("same", files={"x.txt": "igual\n"}))
    assert shas[0] == shas[1]


def test_git_repo_branches_merge_and_push(git_repo: GitRepo) -> None:
    base = git_repo.commit("base", files={"a.txt": "a\n"})
    git_repo.branch("task/1")
    git_repo.checkout("task/1")
    git_repo.commit("work", files={"b.txt": "b\n"}, author="carla")
    assert git_repo.git("log", "-1", "--format=%an") == "carla"
    git_repo.checkout("main")
    merged = git_repo.merge_no_ff("task/1")
    assert git_repo.git("rev-parse", f"{merged}^1") == base
    git_repo.commit("rm", remove=["b.txt"])
    assert not (git_repo.path / "b.txt").exists()
    git_repo.push("main", "task/1")
    git_repo.fetch()
    assert git_repo.git("rev-parse", "origin/main") == git_repo.head()
    empty = git_repo.commit("vacío", allow_empty=True)
    assert git_repo.git("rev-parse", f"{empty}^1") != empty


def test_roles_fixture_helper() -> None:
    from tests.conftest import roles_fixture

    text = roles_fixture("basic")
    assert text.startswith("#") and "lead: ana" in text
    assert json.dumps(text)  # texto serializable, sin bytes raros
