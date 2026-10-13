# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""El nombre de la rama base no sale del árbol de trabajo.

07-hooks REQ-HOOK-41: ``forge.base_branch`` se lee del ``project.yaml`` de la rama por
defecto de la forja (``refs/remotes/<remote>/HEAD``) o, si esa ref no existe, de
``capabilities.json``; sin ninguna de las dos, ``E_CONFIG_POLICY_UNAVAILABLE``. Con 06-permisos
REQ-PERM-1 y D-08, la política de ``check-path``, ``sync-roles``, ``doctor`` y ``validate`` en
modo ``tree`` no puede depender de lo que diga el ``project.yaml`` del árbol.

Antes, ``read_base`` aceptaba cualquier rama remota cuyo ``project.yaml`` se nombrara a sí
misma, y ``read_tree_base`` fallaba con ``E_CONFIG_INVALID`` si el ``project.yaml`` del árbol
no validaba (03-esquemas §2.4: dentro de ``validate`` es un hallazgo).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aucove import _meta
from aucove.config.base import read_base, read_tree_base
from aucove.ops.result import OpError
from aucove.vcs.git import Git
from tests.conftest import GitRepo, roles_fixture
from tests.unit import test_op_check_path as cp
from tests.unit.test_validate_modes import run_validate, seed_base

PROJECT = cp.PROJECT
ROLES = roles_fixture("basic")
SELF_NAMED = PROJECT.replace("  repo: example-org/demo\n", "  repo: example-org/demo\n  base_branch: carla-tmp\n")
CARLA_LEAD = ROLES.replace("lead: ana", "lead: carla").replace("role: lead", "role: researcher").replace(
    "  - login: carla\n    name: \"Carla Soto\"\n    role: researcher",
    "  - login: carla\n    name: \"Carla Soto\"\n    role: lead",
)
UNQUOTED_CORE = PROJECT.replace('core: ">=0.1.0,<0.2"', "core: >=0.1.0,<0.2")


def _push_self_named_branch(repo: GitRepo) -> None:
    """Rama remota ``carla-tmp`` cuyo ``project.yaml`` se declara base y da el rol lead a carla."""
    main = repo.git("rev-parse", "--abbrev-ref", "HEAD")
    repo.git("checkout", "-q", "-b", "carla-tmp")
    repo.commit("carla-tmp", files={"project.yaml": SELF_NAMED, "roles.yaml": CARLA_LEAD})
    repo.push("carla-tmp")
    repo.fetch()
    repo.git("checkout", "-q", main)
    repo.git("branch", "-q", "-D", "carla-tmp")


def _ensure_remote_head(repo: GitRepo) -> None:
    """``refs/remotes/origin/HEAD`` como lo deja ``git clone`` (git < 2.48 no lo crea al hacer fetch)."""
    repo.git("remote", "set-head", "origin", "main")


def test_carla_tmp_fixture_makes_carla_lead() -> None:
    from aucove.config.roles import parse_roles

    roles, findings = parse_roles(CARLA_LEAD, file="roles.yaml")
    assert roles is not None and roles.lead == "carla", findings


def test_read_base_ignores_a_branch_name_taken_from_the_tree(tmp_path: Path) -> None:
    repo = cp.new_repo(tmp_path)
    cp.publish_base(repo)
    _ensure_remote_head(repo)
    _push_self_named_branch(repo)
    snap = read_base(Git(repo.path, env=repo.env), "carla-tmp")
    assert snap.ref == "origin/main"
    assert snap.roles.lead == "ana"


def test_read_tree_base_does_not_read_the_tree_project(tmp_path: Path) -> None:
    repo = cp.new_repo(tmp_path)
    cp.publish_base(repo)
    _ensure_remote_head(repo)
    _push_self_named_branch(repo)
    repo.write({"project.yaml": SELF_NAMED})
    snap = read_tree_base(repo.path, env=repo.env)
    assert snap.ref == "origin/main" and snap.roles.lead == "ana"


def test_check_path_keeps_the_base_policy_when_the_tree_names_another_base(tmp_path: Path) -> None:
    """El escenario del hallazgo: carla en task/12 cambia el project.yaml del árbol."""
    repo = cp.new_repo(tmp_path)
    cp.publish_base(repo)
    _ensure_remote_head(repo)
    cp.seed_tasks(repo)
    cp.seed_identity(repo, "carla")
    _push_self_named_branch(repo)
    repo.git("checkout", "-q", "-b", "task/12")
    repo.write({"project.yaml": SELF_NAMED})
    result = cp.run_op(repo, "roles.yaml")
    cp.assert_valid(result)
    assert result.ok and result.data is not None, result.to_dict()
    entry = result.data["results"][0]  # type: ignore[index]
    assert entry["allowed"] is False and entry["reason_code"] == "E_PERM_LEAD_ONLY_PATH"
    assert result.data["basis"]["base_ref"] == "origin/main"  # type: ignore[index]


def test_check_path_keeps_the_base_policy_on_a_checkout_of_the_other_branch(tmp_path: Path) -> None:
    """Basta con hacer checkout de la rama que se nombra a sí misma."""
    repo = cp.new_repo(tmp_path)
    cp.publish_base(repo)
    _ensure_remote_head(repo)
    cp.seed_identity(repo, "carla")
    _push_self_named_branch(repo)
    repo.git("checkout", "-q", "-b", "local-tmp", "origin/carla-tmp")
    result = cp.run_op(repo, "roles.yaml")
    assert result.ok and result.data is not None, result.to_dict()
    assert result.data["results"][0]["allowed"] is False  # type: ignore[index]
    assert result.data["basis"]["base_ref"] == "origin/main"  # type: ignore[index]


def test_without_remote_head_nor_capabilities_the_policy_is_unavailable(tmp_path: Path) -> None:
    repo = cp.new_repo(tmp_path)
    cp.publish_base(repo)
    repo.git("update-ref", "-d", "--no-deref", "refs/remotes/origin/HEAD", check=False)
    repo.git("symbolic-ref", "--delete", "refs/remotes/origin/HEAD", check=False)
    with pytest.raises(OpError) as info:
        read_base(Git(repo.path, env=repo.env), "main")
    assert info.value.code == "E_CONFIG_POLICY_UNAVAILABLE"
    assert info.value.details == {"ref": "origin/HEAD", "reason": "ref_missing"}


def _capabilities(repo: GitRepo, base_branch: object, *, core: str = _meta.__version__) -> None:
    path = repo.path / _meta.CACHE_DIR / "cache" / "capabilities.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "cache_schema": 1,
        "core": core,
        "created_at": "2026-09-30T12:00:00Z",
        "key": {"forge": "github", "repo": "example-org/demo", "base_branch": base_branch,
                "token_fp": None, "login": "ana"},
        "data": {"default_branch": "main", "profile": "advisory"},
    }
    path.write_text(json.dumps(envelope), encoding="utf-8")


def test_capabilities_json_names_the_base_when_remote_head_is_missing(tmp_path: Path) -> None:
    repo = cp.new_repo(tmp_path)
    cp.publish_base(repo)
    repo.git("symbolic-ref", "--delete", "refs/remotes/origin/HEAD", check=False)
    _push_self_named_branch(repo)
    _capabilities(repo, "main")
    snap = read_base(Git(repo.path, env=repo.env), "carla-tmp")
    assert snap.ref == "origin/main" and snap.roles.lead == "ana"


@pytest.mark.parametrize(("branch", "core"), [("-x", _meta.__version__), ("main", "0.0.0-otra")])
def test_invalid_capabilities_json_is_absent(tmp_path: Path, branch: str, core: str) -> None:
    repo = cp.new_repo(tmp_path)
    cp.publish_base(repo)
    repo.git("symbolic-ref", "--delete", "refs/remotes/origin/HEAD", check=False)
    _capabilities(repo, branch, core=core)
    with pytest.raises(OpError) as info:
        read_base(Git(repo.path, env=repo.env))
    assert info.value.code == "E_CONFIG_POLICY_UNAVAILABLE"


def test_validate_tree_reports_the_tree_project_as_a_finding(git_repo: GitRepo) -> None:
    """``validate --mode tree --tree <dir>`` con ``core: >=0.1.0,<0.2`` sin comillas en el árbol."""
    seed_base(git_repo)
    _ensure_remote_head(git_repo)
    git_repo.write({"project.yaml": UNQUOTED_CORE})
    result = run_validate(git_repo.path, git_repo.env, tree=str(git_repo.path), checks=["schemas"])
    envelope = result.to_dict()
    assert result.error is None or result.error.code != "E_CONFIG_INVALID", envelope
    assert result.data is not None, envelope
    findings = [f for r in result.data["results"] for f in r["findings"]]  # type: ignore[index]
    yaml_errors = [f for f in findings if f["code"] == "E_CONFIG_YAML" and f["path"] == "project.yaml"]
    assert yaml_errors, envelope
    assert yaml_errors[0]["line"] == 3
    assert yaml_errors[0]["hint"]
