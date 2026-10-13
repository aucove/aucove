# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""La fábrica de la forja funciona sin un proyecto en el directorio actual.

Los workflows de la plantilla llaman a ``aucove ci comment`` sin ``--root`` y ``validate
--mode pr`` corre desde ``runner.temp`` (09-ci §3.3). ``context_factory`` exigía
``ctx.base()``, que sin proyecto lanza ``E_CONFIG_NOT_A_PROJECT``: fuera de la forja falsa,
``ci comment``, ``ci base-report`` y ``validate --mode pr`` nunca llegaban al adaptador
(09-ci REQ-CI-20, REQ-CI-27, REQ-SEC-16). Las e2e no lo veían porque ``TESTING=1`` y
``FORGE_ADAPTER=fake`` salen de la fábrica antes de leer la base.
"""

from __future__ import annotations

import io
import sys
import types
from pathlib import Path

import pytest

from aucove import _meta
from aucove.cli import ci as cli_ci
from aucove.cli.ci import CiContext
from aucove.config.base import read_policy_dir
from aucove.ops.context import ForgeTarget, Invocation, OpContext
from aucove.ops.result import OpError

from tests.conftest import FIXTURES, roles_fixture
from tests.unit.test_ci_build_plan import project_yaml

PKG = _meta.NAME
GITHUB = f"{PKG}.forge.github"
ENV = {"GITHUB_REPOSITORY": "example-org/demo", "GITHUB_TOKEN": "ghs_dummy"}
PR = 7


class _CommentRecorder:
    """Adaptador mínimo: lo que ``ci comment --kind build`` usa de la forja en un PR sin comentarios."""

    def __init__(self) -> None:
        self.created: list[tuple[int, str]] = []

    def list_comments(self, number: int, **_kw: object) -> list[object]:
        return []

    def create_comment(self, number: int, body: str, **_kw: object) -> None:
        self.created.append((number, body))


class _GithubStub:
    """Módulo ``forge.github`` de H3 reemplazado: ``create(ctx)`` devuelve ``forge``."""

    def __init__(self, forge: object) -> None:
        self.forge = forge
        self.targets: list[ForgeTarget] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        module = types.ModuleType(GITHUB)

        def create(ctx: OpContext) -> object:
            self.targets.append(ctx.forge_target())
            return self.forge

        module.create = create  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, GITHUB, module)


def _inv(surface: str, cwd: Path, env: dict[str, str]) -> Invocation:
    return Invocation(root=None, lang="en", json=False, dry_run=False, surface=surface, env=env, cwd=str(cwd))  # type: ignore[arg-type]


def test_ci_context_without_root_reaches_the_github_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _GithubStub(object())
    stub.install(monkeypatch)
    ctx = CiContext(_inv("ci", tmp_path, dict(ENV)))
    assert ctx.forge() is stub.forge
    assert stub.targets == [ForgeTarget("github", "example-org/demo")]


def test_validate_pr_from_runner_temp_reaches_the_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _GithubStub(object())
    stub.install(monkeypatch)
    ctx = OpContext(_inv("cli", tmp_path, dict(ENV)))  # tmp_path no es un proyecto
    assert ctx.project(required=False) is None
    assert ctx.forge() is stub.forge


@pytest.mark.parametrize("repo", [None, "", "demo", "a/b/c", "../demo", "example-org/..", "-x/demo"])
def test_without_project_nor_valid_repository_still_fails(tmp_path: Path, repo: str | None) -> None:
    env = {} if repo is None else {"GITHUB_REPOSITORY": repo}
    ctx = CiContext(_inv("ci", tmp_path, env))
    with pytest.raises(OpError) as info:
        ctx.forge()
    assert info.value.code == "E_CONFIG_NOT_A_PROJECT"


def test_policy_from_arguments_takes_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "project.yaml").write_text(project_yaml(), encoding="utf-8", newline="\n")
    (policy / "roles.yaml").write_text(roles_fixture("basic"), encoding="utf-8", newline="\n")
    stub = _GithubStub(object())
    stub.install(monkeypatch)
    ctx = CiContext(_inv("ci", tmp_path, {"GITHUB_REPOSITORY": "other-org/other"}))
    snap = read_policy_dir(policy)
    ctx.use_policy(snap)
    assert ctx.base() is snap
    assert ctx.forge_target() == ForgeTarget("github", "example-org/demo")
    assert ctx.forge() is stub.forge


def test_ci_comment_publishes_without_the_fake_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    forge = _CommentRecorder()
    stub = _GithubStub(forge)
    stub.install(monkeypatch)
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "diagnostics.json").write_bytes((FIXTURES / "diagnostics" / "build-report.sample.json").read_bytes())
    out, err = io.StringIO(), io.StringIO()
    argv = ["comment", "--kind", "build", "--pr", str(PR), "--language", "en", "--outcome", "failure",
            "--run-url", "https://github.com/example-org/demo/actions/runs/7", "--report-dir", str(reports)]
    monkeypatch.chdir(tmp_path)
    rc = cli_ci.run(argv, env=dict(ENV), stdout=out, stderr=err)
    assert rc == 0, err.getvalue()
    assert [n for n, _ in forge.created] == [PR]
    assert forge.created[0][1].splitlines()[0] == _meta.ci_marker("build")
    assert stub.targets == [ForgeTarget("github", "example-org/demo")]
