# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``W_CONFIG_POLICY_FALLBACK`` lleva el sha usado de cada archivo.

06-permisos REQ-PERM-4: si ``project.yaml`` o ``roles.yaml`` de la punta de la base no
valida, se usa la versión válida más reciente de ese archivo y se avisa «con el sha
usado». Cuando cada archivo sale de un commit distinto, ``fallback_sha`` era el de
``project.yaml`` y se perdía el de ``roles.yaml``, que es el que decide la autorización.
"""

from __future__ import annotations

from aucove.config.base import read_base
from aucove.vcs.git import Git
from tests.conftest import GitRepo, roles_fixture
from tests.unit.test_base_snapshot import BAD_PROJECT, BAD_ROLES, PROJECT, _publish

ROLES = roles_fixture("basic")
ROLES_V2 = roles_fixture("no-deputy")


def test_each_file_keeps_its_own_fallback_sha(git_repo: GitRepo) -> None:
    c1 = _publish(git_repo, {"project.yaml": PROJECT, "roles.yaml": ROLES})
    c2 = _publish(git_repo, {"roles.yaml": ROLES_V2})
    _publish(git_repo, {"project.yaml": BAD_PROJECT, "roles.yaml": BAD_ROLES})
    snap = read_base(Git(git_repo.path, env=git_repo.env))
    assert dict(snap.fallbacks) == {"project.yaml": c1, "roles.yaml": c2}
    assert snap.fallback_sha == c2  # la versión de roles.yaml que decide la autorización
    assert snap.roles.deputy is None  # es la de c2 (no-deputy)
    assert snap.fallback_details() == {"sha": c2, "files": {"project.yaml": c1, "roles.yaml": c2}}


def test_only_project_falls_back(git_repo: GitRepo) -> None:
    c1 = _publish(git_repo, {"project.yaml": PROJECT, "roles.yaml": ROLES})
    _publish(git_repo, {"project.yaml": BAD_PROJECT})
    snap = read_base(Git(git_repo.path, env=git_repo.env))
    assert snap.fallback_sha == c1
    assert snap.fallback_details() == {"sha": c1, "files": {"project.yaml": c1}}


def test_without_fallback_there_are_no_details(git_repo: GitRepo) -> None:
    _publish(git_repo, {"project.yaml": PROJECT, "roles.yaml": ROLES})
    snap = read_base(Git(git_repo.path, env=git_repo.env))
    assert snap.fallback_sha is None and snap.fallback_details() is None
