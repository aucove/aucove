# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``read_base`` no necesita el ``project.yaml`` del árbol.

Antes, ``validate --mode tree`` sin ``--tree`` y ``check-path`` fallaban con
``E_CONFIG_INVALID`` cuando el ``project.yaml`` del árbol no validaba, porque
``OpContext.base()`` lo analizaba solo para sacar ``base_branch``. Según 03-esquemas
§2.4, dentro de ``validate`` ese archivo es un hallazgo (``E_CONFIG_YAML`` con línea), y
07-hooks REQ-HOOK-41 dice que la rama base no sale del árbol.

El arreglo de ``OpContext.base()`` es llamar ``read_base(self.git(), t=self._t)``. Estas
pruebas fijan lo que ese arreglo necesita de config: ``read_base`` sin ``base_branch``
devuelve la política de la base aunque el ``project.yaml`` del árbol no exista o no sea YAML
válido, y no lo lee.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aucove.config.base import read_base
from aucove.i18n import translator
from aucove.vcs.git import Git
from tests.conftest import GitRepo
from tests.unit import test_op_check_path as cp

UNQUOTED_CORE = cp.PROJECT.replace('core: ">=0.1.0,<0.2"', "core: >=0.1.0,<0.2")


@pytest.fixture
def repo(tmp_path: Path) -> GitRepo:
    r = cp.new_repo(tmp_path)
    cp.publish_base(r)
    r.git("remote", "set-head", "origin", "main")  # como lo deja ``git clone``
    return r


def test_unquoted_core_is_really_invalid_yaml() -> None:
    from aucove.config.project import parse_project

    assert UNQUOTED_CORE != cp.PROJECT
    cfg, findings = parse_project(UNQUOTED_CORE, file="project.yaml")
    assert cfg is None and [f.code for f in findings] == ["E_CONFIG_YAML"]


@pytest.mark.parametrize("tree_project", [UNQUOTED_CORE, "core: [\n", None])
def test_read_base_without_branch_ignores_the_tree_project(repo: GitRepo, tree_project: str | None) -> None:
    """La llamada que debe hacer ``OpContext.base()``: sin ``base_branch`` y con ``t``."""
    if tree_project is None:
        (repo.path / "project.yaml").unlink()
    else:
        repo.write({"project.yaml": tree_project})
    snap = read_base(Git(repo.path, env=repo.env), t=translator("es"))
    assert snap.ref == "origin/main" and snap.fallback_sha is None
    assert snap.project.core_pin == "0.1.0" and snap.roles.lead == "ana"
