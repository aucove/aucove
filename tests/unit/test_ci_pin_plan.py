# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``ci pin-plan`` (09-ci §4.3, REQ-CI-45; 00-marco D-31 a)."""

from __future__ import annotations

import io
import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from aucove.cli import ci as cli_ci
from aucove.ops import ci_plan
from aucove.ops.ci_plan import PinDecision, PlanError, pin_decision

from tests.unit.test_ci_build_plan import project_yaml

BASE_CORE = ">=0.1.0,<0.2"


def write_tree(tmp: Path, name: str, text: str | None) -> Path:
    tree = tmp / name
    tree.mkdir()
    if text is not None:
        (tree / "project.yaml").write_text(text, encoding="utf-8", newline="\n")
    return tree


def base_dir(tmp: Path, *, core: str = BASE_CORE, core_pin: str = '"0.1.0"') -> Path:
    return write_tree(tmp, "base", project_yaml(core=core, core_pin=core_pin))


def pr_dir(tmp: Path, core_pin: str) -> Path:
    return write_tree(tmp, "pr", project_yaml(core_pin=core_pin))


def run_pin(policy: Path, tree: Path | None = None) -> tuple[int, str, str]:
    argv: list[str] = ["pin-plan", "--policy", str(policy)]
    if tree is not None:
        argv += ["--tree", str(tree)]
    out, err = io.StringIO(), io.StringIO()
    rc = cli_ci.run(argv, env={}, stdout=out, stderr=err)
    return rc, out.getvalue(), err.getvalue()


def test_same_core_pin_does_not_run(tmp_path: Path) -> None:
    rc, out, _err = run_pin(base_dir(tmp_path), pr_dir(tmp_path, '"0.1.0"'))
    assert (rc, out) == (0, "run=false\npin=0.1.0\n")


def test_fork_without_tree_does_not_run(tmp_path: Path) -> None:
    rc, out, err = run_pin(base_dir(tmp_path))
    assert (rc, out) == (0, "run=false\npin=0.1.0\n")
    assert err  # explica por qué no corre, en stderr


def test_new_pin_that_satisfies_core_runs(tmp_path: Path) -> None:
    rc, out, _err = run_pin(base_dir(tmp_path), pr_dir(tmp_path, '"0.1.3"'))
    assert (rc, out) == (0, "run=true\npin=0.1.3\n")


@pytest.mark.parametrize("pin", ['"0.2.0"', '"0.0.9"', '"1.0.0"'])
def test_new_pin_outside_core_does_not_run(tmp_path: Path, pin: str) -> None:
    rc, out, err = run_pin(base_dir(tmp_path), pr_dir(tmp_path, pin))
    assert (rc, out) == (0, "run=false\npin=0.1.0\n")
    assert "core" in err


def test_core_is_evaluated_with_pep440(tmp_path: Path) -> None:
    rc, out, _err = run_pin(base_dir(tmp_path, core="~=0.1.0"), pr_dir(tmp_path, '"0.1.9"'))
    assert (rc, out) == (0, "run=true\npin=0.1.9\n")


def test_prerelease_pins_run(tmp_path: Path) -> None:
    """00-marco D-66: la base fija la preliminar y el PR pasa a la versión final."""
    base = base_dir(tmp_path, core=">=0.1.0rc1,<0.2", core_pin='"0.1.0rc1"')
    rc, out, _err = run_pin(base, pr_dir(tmp_path, '"0.1.0"'))
    assert (rc, out) == (0, "run=true\npin=0.1.0\n")


def test_pep440_equal_but_textually_different_pin_runs(tmp_path: Path) -> None:
    # REQ-CI-45 compara el texto: la descarga usa ``v${PIN}`` literal (REQ-CI-12), así que
    # ``0.1.00`` es otra versión publicada (o ninguna) y el job ``pin`` debe probarla.
    rc, out, _err = run_pin(base_dir(tmp_path), pr_dir(tmp_path, '"0.1.00"'))
    assert (rc, out) == (0, "run=true\npin=0.1.00\n")


@pytest.mark.parametrize(
    "text",
    [
        project_yaml(core_pin="0.1.3"),  # sin comillas: no es la línea de REQ-ARCH-45
        project_yaml(core_pin='"0.1"'),
        project_yaml(core_pin='"0.1.3-rc1"'),
        project_yaml(core_pin='"0.1.3"') + 'core_pin: "0.1.4"\n',  # dos líneas core_pin
        "no es yaml: [\n",
        None,  # sin project.yaml en el árbol del PR
    ],
)
def test_invalid_or_missing_pr_pin_does_not_run(tmp_path: Path, text: str | None) -> None:
    tree = write_tree(tmp_path, "pr", text)
    rc, out, _err = run_pin(base_dir(tmp_path), tree)
    assert (rc, out) == (0, "run=false\npin=0.1.0\n")


def test_pr_project_yaml_symlink_is_not_followed(tmp_path: Path) -> None:
    target = tmp_path / "outside.yaml"
    target.write_text(project_yaml(core_pin='"0.1.3"'), encoding="utf-8")
    tree = write_tree(tmp_path, "pr", None)
    try:
        os.symlink(target, tree / "project.yaml")
    except (OSError, NotImplementedError):
        pytest.skip("este sistema no permite crear enlaces simbólicos")
    rc, out, _err = run_pin(base_dir(tmp_path), tree)
    assert (rc, out) == (0, "run=false\npin=0.1.0\n")


def test_pr_project_yaml_over_5_mib_is_ignored(tmp_path: Path) -> None:
    text = project_yaml(core_pin='"0.1.3"') + "#" * (5 * 2**20) + "\n"
    rc, out, _err = run_pin(base_dir(tmp_path), write_tree(tmp_path, "pr", text))
    assert (rc, out) == (0, "run=false\npin=0.1.0\n")


def test_read_regular_file_enforces_its_limit(tmp_path: Path) -> None:
    path = write_tree(tmp_path, "t", "x" * 100) / "project.yaml"
    assert ci_plan.read_regular_file(path, 99) is None
    assert ci_plan.read_regular_file(path, 100) == b"x" * 100
    assert ci_plan.read_regular_file(tmp_path / "missing", 100) is None


def test_pr_project_yaml_that_is_a_directory_is_ignored(tmp_path: Path) -> None:
    tree = write_tree(tmp_path, "pr", None)
    (tree / "project.yaml").mkdir()
    rc, out, _err = run_pin(base_dir(tmp_path), tree)
    assert (rc, out) == (0, "run=false\npin=0.1.0\n")


@pytest.mark.parametrize("core_pin", ["0.1.0", '"0.1"', '"x.y.z"'])
def test_base_without_a_valid_core_pin_fails_closed(tmp_path: Path, core_pin: str) -> None:
    policy = write_tree(tmp_path, "base", project_yaml(core_pin=core_pin))
    rc, out, err = run_pin(policy, pr_dir(tmp_path, '"0.1.3"'))
    assert (rc, out) == (1, "")
    assert err


def test_unreadable_or_invalid_base_fails_closed(tmp_path: Path) -> None:
    rc, out, _err = run_pin(tmp_path / "nope")
    assert (rc, out) == (1, "")
    policy = write_tree(tmp_path, "base", 'core_pin: "0.1.0"\nconvention: "0.1"\n')  # sin claves obligatorias
    rc, out, _err = run_pin(policy)
    assert (rc, out) == (1, "")


@pytest.mark.parametrize("argv", [["pin-plan"], ["pin-plan", "--tree", "x"], ["pin-plan", "--policy"]])
def test_usage_errors_exit_3(argv: Sequence[str]) -> None:
    out, err = io.StringIO(), io.StringIO()
    assert cli_ci.run(list(argv), env={}, stdout=out, stderr=err) == 3
    assert out.getvalue() == ""


@pytest.mark.parametrize(
    ("base", "core", "pr", "has_tree", "expected"),
    [
        ("0.1.0", BASE_CORE, None, False, PinDecision(False, "0.1.0", "fork")),
        ("0.1.0", BASE_CORE, "0.1.3", False, PinDecision(False, "0.1.0", "fork")),
        ("0.1.0", BASE_CORE, None, True, PinDecision(False, "0.1.0", "pr_pin_missing")),
        ("0.1.0", BASE_CORE, "0.1", True, PinDecision(False, "0.1.0", "pr_pin_missing")),
        ("0.1.0", BASE_CORE, "0.1.0", True, PinDecision(False, "0.1.0", "same")),
        ("0.1.0", BASE_CORE, "0.01.0", True, PinDecision(True, "0.01.0", "run")),  # se compara el texto
        ("0.1.0", BASE_CORE, "0.2.0", True, PinDecision(False, "0.1.0", "unsatisfied")),
        ("0.1.0", "not a specifier", "0.1.3", True, PinDecision(False, "0.1.0", "unsatisfied")),
        ("0.1.0", BASE_CORE, "0.1.3", True, PinDecision(True, "0.1.3", "run")),
        ("0.1.2", BASE_CORE, "0.1.0", True, PinDecision(True, "0.1.0", "run")),  # bajar de versión también
        # pre-releases (00-marco D-66)
        ("0.1.0rc1", ">=0.1.0rc1,<0.2", "0.1.0rc1", True, PinDecision(False, "0.1.0rc1", "same")),
        ("0.1.0rc1", ">=0.1.0rc1,<0.2", "0.1.0", True, PinDecision(True, "0.1.0", "run")),
        ("0.1.0", BASE_CORE, "0.1.1rc1", True, PinDecision(True, "0.1.1rc1", "run")),
        ("0.1.0", BASE_CORE, "0.1.0rc2", True, PinDecision(False, "0.1.0", "unsatisfied")),  # 0.1.0rc2 < 0.1.0
        ("0.1.0", BASE_CORE, "0.1.1-rc1", True, PinDecision(False, "0.1.0", "pr_pin_missing")),
    ],
)
def test_pin_decision(base: str, core: str, pr: str | None, has_tree: bool, expected: PinDecision) -> None:
    assert pin_decision(base, core, pr, has_tree=has_tree) == expected


def test_pin_decision_rejects_an_invalid_base_pin() -> None:
    with pytest.raises(PlanError):
        pin_decision("0.1", BASE_CORE, "0.1.3")
