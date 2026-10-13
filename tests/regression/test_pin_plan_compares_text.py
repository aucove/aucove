# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``pin-plan`` compara el ``core_pin`` como texto (09-ci REQ-CI-45).

El job ``pin`` descarga ``.../v${PIN}/requirements-lock.txt`` con el texto literal del pin
(REQ-CI-12). Un ``core_pin`` del PR que difiere en el texto pero es igual según PEP 440
(``0.01.0`` frente a ``0.1.0``) difiere, cumple el formato y satisface ``core``: el job ``pin``
DEBE correr con ese texto. Si no corriera, el PR pasaría sin probar la versión y, ya
integrado, todos los workflows de la base intentarían bajar una versión que no existe.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from aucove.cli import ci as cli_ci
from aucove.ops.ci_plan import PinDecision, pin_decision

from tests.unit.test_ci_build_plan import project_yaml

BASE_CORE = ">=0.1.0,<0.2"
PEP440_EQUAL_BUT_TEXTUALLY_DIFFERENT = ["0.01.0", "0.1.00", "00.1.0"]


@pytest.mark.parametrize("pr_pin", PEP440_EQUAL_BUT_TEXTUALLY_DIFFERENT)
def test_textually_different_pin_runs_with_its_literal_text(pr_pin: str) -> None:
    assert pin_decision("0.1.0", BASE_CORE, pr_pin) == PinDecision(True, pr_pin, "run")


@pytest.mark.parametrize("pr_pin", PEP440_EQUAL_BUT_TEXTUALLY_DIFFERENT)
def test_cli_emits_the_literal_pr_pin(tmp_path: Path, pr_pin: str) -> None:
    base = tmp_path / "base"
    pr = tmp_path / "pr"
    base.mkdir()
    pr.mkdir()
    (base / "project.yaml").write_text(project_yaml(core_pin='"0.1.0"'), encoding="utf-8", newline="\n")
    (pr / "project.yaml").write_text(project_yaml(core_pin=f'"{pr_pin}"'), encoding="utf-8", newline="\n")
    out, err = io.StringIO(), io.StringIO()
    rc = cli_ci.run(["pin-plan", "--policy", str(base), "--tree", str(pr)], env={}, stdout=out, stderr=err)
    assert (rc, out.getvalue()) == (0, f"run=true\npin={pr_pin}\n")


def test_identical_text_still_does_not_run() -> None:
    assert pin_decision("0.1.0", BASE_CORE, "0.1.0") == PinDecision(False, "0.1.0", "same")
    assert pin_decision("0.01.0", BASE_CORE, "0.01.0") == PinDecision(False, "0.01.0", "same")


def test_textually_different_pin_outside_core_does_not_run() -> None:
    # La comparación textual no relaja la de ``core``: sigue siendo PEP 440.
    assert pin_decision("0.1.0", BASE_CORE, "00.2.0") == PinDecision(False, "0.1.0", "unsatisfied")
