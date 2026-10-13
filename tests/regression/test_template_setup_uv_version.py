# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``astral-sh/setup-uv`` de la plantilla fija la versión de uv.

``astral-sh/setup-uv`` v10.2.0 sin ``version`` busca ``required-version`` en ``uv.toml`` o
``pyproject.toml`` de ``working-directory`` y, si no lo encuentra, instala ``latest``. Los
workflows de la plantilla ya ponen ``working-directory: ${{ runner.temp }}``, para que ningún
archivo del PR elija la versión (09-ci REQ-CI-2), pero no fijaban ``version``: cada run
instalaba la última uv publicada en ese momento, sin fijar ni verificar contra un sha256
fijado (10-seguridad REQ-SEC-47), en jobs como ``report`` de ``build.yml``, que tiene
``pull-requests: write``.

Con ``version``, setup-uv verifica el binario con el sha256 que trae su propio código
(``known-checksums.json`` del SHA fijado). El CI del framework fija la última versión de uv
que trae ese archivo para el mismo SHA de la acción; la plantilla usa la misma.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
import yaml

from tests.conftest import REPO_ROOT
from tests.render.test_template_workflows import WORKFLOW_FILES, action_of, jobs, steps_of

SETUP_UV = "astral-sh/setup-uv"
EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
FRAMEWORK_CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def setup_uv_steps(name: str) -> list[tuple[str, dict[str, Any]]]:
    return [
        (job_id, step)
        for job_id, job in jobs(name).items()
        for step in steps_of(job)
        if action_of(step) == SETUP_UV
    ]


def framework_setup_uv() -> set[tuple[str, str]]:
    """(``uses``, ``version``) de cada setup-uv del CI del framework."""
    doc = yaml.safe_load(FRAMEWORK_CI.read_text(encoding="utf-8"))
    found: set[tuple[str, str]] = set()
    for job in doc["jobs"].values():
        for step in job.get("steps") or []:
            if action_of(step) == SETUP_UV:
                found.add((str(step["uses"]), str((step.get("with") or {}).get("version"))))
    return found


@pytest.mark.parametrize("name", WORKFLOW_FILES)
def test_setup_uv_pins_an_exact_uv_version(name: str) -> None:
    found = setup_uv_steps(name)
    assert found, name
    for job_id, step in found:
        with_ = step.get("with") or {}
        version = with_.get("version")
        # Cadena exacta X.Y.Z: ni rangos, ni «latest», ni un número que YAML lea como float.
        assert isinstance(version, str) and EXACT_VERSION.match(version), f"{name}:{job_id}: {version!r}"
        assert "version-file" not in with_, f"{name}:{job_id}"
        assert with_.get("working-directory") == "${{ runner.temp }}", f"{name}:{job_id}"


def test_template_uses_the_uv_verified_for_the_pinned_action() -> None:
    template = {
        (str(step["uses"]), str((step.get("with") or {}).get("version")))
        for name in WORKFLOW_FILES
        for _, step in setup_uv_steps(name)
    }
    assert len(template) == 1, template  # una sola uv en toda la plantilla
    framework = framework_setup_uv()
    assert len(framework) == 1, framework
    # Mismo SHA de la acción que el CI del framework: misma uv, con sha256 en known-checksums.
    assert template == framework

