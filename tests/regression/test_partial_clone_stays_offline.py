# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Sin red, un clon parcial no descarga objetos (01 REQ-ARCH-63, REQ-ARCH-51).

Antes, ``child_env`` no impedía la descarga perezosa de git. En un clon con
``--filter=blob:none``, ``Git.show`` de un blob que no estaba en local lo bajaba del
remoto promisor, también con ``AUCOVE_OFFLINE=1``: ``read_base`` y su respaldo de
06 REQ-PERM-4 abrían conexiones con credenciales en modo sin red. Y si la descarga no
era posible, ``show`` devolvía ``None``, como si el archivo no existiera, así que el
respaldo podía elegir una versión anterior de la política.

Ahora, sin red, git no abre ningún transporte (``GIT_NO_LAZY_FETCH=1`` y
``GIT_ALLOW_PROTOCOL`` vacío) y un objeto ausente es ``ObjectUnavailable``: la
lectura de la base falla cerrada con ``E_CONFIG_POLICY_UNAVAILABLE``. Con red, git
sigue descargando lo que falte dentro del timeout.

El remoto es un repositorio local (``file://``): no hay red real en las pruebas.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from aucove import _meta
from aucove.config.base import read_base
from aucove.ops.result import OpError
from aucove.vcs.git import Git, ObjectUnavailable
from aucove.vcs.tree import git_tree
from tests.conftest import FIXTURES, GIT_OPTIONS, GitRepo, isolated_git_env, roles_fixture

OFFLINE = _meta.ENV_PREFIX + "OFFLINE"
PROJECT = (FIXTURES / "config" / "project" / "minimal.yaml").read_text(encoding="utf-8")
ROLES = roles_fixture("basic")
BAD_ROLES = ROLES.replace("lead: ana", "lead: zoe")  # no valida: zoe no está en people
assert BAD_ROLES != ROLES
# válido, pero sin dario: una baja que la política vieja no tiene
REVOKED = ROLES.replace('  - login: dario\n    name: "Darío Luna"\n    role: researcher\n    account_type: none\n', "")
assert REVOKED != ROLES


@dataclass
class _Clone:
    path: Path
    env: dict[str, str]
    good: str  # último commit con un roles.yaml válido
    tip: str  # punta de main, con un roles.yaml que no valida

    def offline_env(self) -> dict[str, str]:
        return {**self.env, OFFLINE: "1"}

    def missing(self) -> int:
        """Objetos que faltan en local (``--missing=print`` nunca descarga)."""
        proc = subprocess.run(
            ["git", *GIT_OPTIONS, "rev-list", "--objects", "--missing=print", "--all"],
            cwd=self.path, env=self.env, capture_output=True, check=True,
        )
        return sum(1 for line in proc.stdout.splitlines() if line.startswith(b"?"))


@dataclass
class _Source:
    repo: GitRepo
    good: str
    tip: str


@pytest.fixture(scope="module")
def source(tmp_path_factory: pytest.TempPathFactory) -> _Source:
    """Historia de main, compartida por el módulo: los clones nunca la modifican."""
    tmp = tmp_path_factory.mktemp("p09-source")
    path = tmp / "repo"
    path.mkdir()
    repo = GitRepo(path, tmp / "unused-origin.git", isolated_git_env(tmp / "git-env"))
    repo.git("init", "-q", "-b", "main")
    good = repo.commit("valid", files={"project.yaml": PROJECT, "roles.yaml": ROLES})
    for i in range(2):
        repo.commit(f"note {i}", files={"notes.txt": f"{i}\n"})
    tip = repo.commit("invalid roles", files={"roles.yaml": BAD_ROLES})
    return _Source(repo, good, tip)


def _partial_clone(source: _Source, tmp_path: Path, filter_spec: str) -> _Clone:
    dst = tmp_path / "partial"
    env = dict(source.repo.env)
    subprocess.run(
        [
            "git", *GIT_OPTIONS, "clone", "-q", f"--filter={filter_spec}", "--no-checkout",
            "--upload-pack=git -c uploadpack.allowfilter=true upload-pack",
            source.repo.path.as_uri(), str(dst),
        ],
        cwd=tmp_path, env=env, capture_output=True, check=True,
    )
    clone = _Clone(dst, env, source.good, source.tip)
    assert clone.missing() > 0, "el clon debería ser parcial"
    return clone


@pytest.mark.parametrize("filter_spec", ["blob:none", "tree:0"])
def test_offline_read_base_fails_closed_without_fetching(source: _Source, tmp_path: Path, filter_spec: str) -> None:
    clone = _partial_clone(source, tmp_path, filter_spec)
    before = clone.missing()
    with pytest.raises(OpError) as info:
        read_base(Git(clone.path, env=clone.offline_env()), "main")
    assert info.value.code == "E_CONFIG_POLICY_UNAVAILABLE"
    assert clone.missing() == before, "git descargó objetos con el modo sin red"


@pytest.mark.parametrize("filter_spec", ["blob:none", "tree:0"])
def test_offline_show_reports_unavailable_content(source: _Source, tmp_path: Path, filter_spec: str) -> None:
    clone = _partial_clone(source, tmp_path, filter_spec)
    before = clone.missing()
    g = Git(clone.path, env=clone.offline_env())
    for rev in (clone.tip, clone.good):
        with pytest.raises(ObjectUnavailable) as info:
            g.show(rev, "roles.yaml")
        assert (info.value.rev, info.value.path) == (rev, "roles.yaml")
    assert g.show("refs/remotes/origin/nope", "roles.yaml") is None  # la revisión no existe
    if filter_spec == "blob:none":  # con los árboles en local se sabe qué no existe
        assert g.show(clone.tip, "absent.yaml") is None
    assert clone.missing() == before


def test_hooks_force_offline_without_the_variable(source: _Source, tmp_path: Path) -> None:
    clone = _partial_clone(source, tmp_path, "blob:none")
    before = clone.missing()
    g = Git(clone.path, env=clone.env, offline=True)  # p. ej. pre-tool-use (REQ-ARCH-51)
    with pytest.raises(ObjectUnavailable):
        g.show(clone.tip, "roles.yaml")
    assert clone.missing() == before


def test_offline_tree_view_fails_closed_on_a_missing_blob(source: _Source, tmp_path: Path) -> None:
    clone = _partial_clone(source, tmp_path, "blob:none")
    before = clone.missing()
    tree = git_tree(clone.path, clone.tip, git=Git(clone.path, env=clone.offline_env()))
    assert tree.entries()["roles.yaml"] == "100644"
    with pytest.raises(ObjectUnavailable):
        tree.read_bytes("roles.yaml")  # antes: None, y la regla se saltaba el archivo
    assert tree.read_bytes("absent.yaml") is None
    assert clone.missing() == before


def test_failed_fetch_never_falls_back_to_an_older_policy(git_repo: GitRepo, tmp_path: Path) -> None:
    """Un blob que no se pudo descargar no es «no existe»: no se elige una política vieja.

    La punta quita a dario (una baja). Antes, si la descarga de su ``roles.yaml``
    fallaba, ``show`` daba ``None`` y el respaldo de REQ-PERM-4 usaba la versión
    anterior, que estaba en local y todavía le daba permisos.
    """
    v1 = git_repo.commit("v1", files={"project.yaml": PROJECT, "roles.yaml": ROLES})
    tip = git_repo.commit("revoke dario", files={"roles.yaml": REVOKED})
    clone = _partial_clone(_Source(git_repo, v1, tip), tmp_path, "blob:none")
    for path in ("project.yaml", "roles.yaml"):  # los blobs de v1 bajan; el roles.yaml de la punta, no
        subprocess.run(
            ["git", *GIT_OPTIONS, "cat-file", "blob", f"{v1}:{path}"],
            cwd=clone.path, env=clone.env, capture_output=True, check=True,
        )
    before = clone.missing()
    no_fetch = {**clone.env, "GIT_ALLOW_PROTOCOL": "ssh"}  # con red, pero el remoto file:// no se alcanza
    with pytest.raises(ObjectUnavailable):
        Git(clone.path, env=no_fetch).show(tip, "roles.yaml")
    with pytest.raises(OpError) as info:
        read_base(Git(clone.path, env=no_fetch), "main")
    assert info.value.code == "E_CONFIG_POLICY_UNAVAILABLE"
    assert clone.missing() == before
    # con la descarga posible se lee la punta, sin respaldo
    snap = read_base(Git(clone.path, env=clone.env), "main")
    assert snap.fallbacks == {} and "dario" not in {p.login for p in snap.roles.people}


def test_online_partial_clone_still_reads_the_base(source: _Source, tmp_path: Path) -> None:
    """Con red no cambia nada: git descarga lo que falta y se usa el respaldo de REQ-PERM-4."""
    clone = _partial_clone(source, tmp_path, "blob:none")
    before = clone.missing()
    snap = read_base(Git(clone.path, env=clone.env), "main")
    assert snap.fallbacks.get("roles.yaml") == clone.good
    assert clone.missing() < before
