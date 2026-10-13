# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``OpContext.base()`` no analiza el ``project.yaml`` del árbol.

Antes, ``OpContext.base()`` leía el ``project.yaml`` del árbol de trabajo, lo pasaba por
``parse_project`` y, si no validaba, lanzaba ``E_CONFIG_INVALID``; el ``base_branch`` que
sacaba de ahí lo ignoraba ``read_base``. Con eso, ``validate --mode tree`` sin ``--tree``
abortaba en vez de informar el hallazgo ``E_CONFIG_YAML`` con su línea (03-esquemas §2.4),
y ``check-path`` negaba la operación aunque la política de la base fuera válida. El nombre
de la rama base no sale del árbol (07-hooks REQ-HOOK-41): lo resuelve ``read_base`` desde
``origin/HEAD`` o ``capabilities.json``, y la política es la de la base (D-08).
"""

from __future__ import annotations

from pathlib import Path

from tests.conftest import GitRepo
from tests.unit import test_op_check_path as cp
from tests.unit.test_validate_modes import run_validate, seed_base

# ``core`` sin comillas: ``>`` abre un escalar plegado y el YAML deja de ser válido (línea 3).
UNQUOTED_CORE = cp.PROJECT.replace('core: ">=0.1.0,<0.2"', "core: >=0.1.0,<0.2")


def test_unquoted_core_changes_the_fixture() -> None:
    assert UNQUOTED_CORE != cp.PROJECT


def test_validate_tree_without_tree_reports_the_finding(git_repo: GitRepo) -> None:
    seed_base(git_repo)
    git_repo.git("remote", "set-head", "origin", "main")  # como lo deja ``git clone``
    git_repo.write({"project.yaml": UNQUOTED_CORE})
    result = run_validate(git_repo.path, git_repo.env, checks=["schemas"])
    envelope = result.to_dict()
    assert result.error is None or result.error.code == "E_VALIDATION_FAILED", envelope
    assert result.data is not None, envelope
    findings = [f for r in result.data["results"] for f in r["findings"]]  # type: ignore[index]
    yaml_errors = [f for f in findings if f["code"] == "E_CONFIG_YAML" and f["path"] == "project.yaml"]
    assert yaml_errors, envelope
    assert yaml_errors[0]["line"] == 3 and yaml_errors[0]["hint"], envelope


def test_check_path_uses_the_base_policy_with_an_invalid_tree_project(tmp_path: Path) -> None:
    repo = cp.new_repo(tmp_path)
    cp.publish_base(repo)
    repo.git("remote", "set-head", "origin", "main")
    cp.seed_tasks(repo)
    cp.seed_identity(repo, "ana")
    repo.git("checkout", "-q", "-b", "task/18")
    repo.write({"project.yaml": UNQUOTED_CORE})
    result = cp.run_op(repo, "project.yaml")
    assert result.error is None, result.to_dict()
    assert result.data["results"][0]["allowed"] is True, result.to_dict()  # type: ignore[index]
