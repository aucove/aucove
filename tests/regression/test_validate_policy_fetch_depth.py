# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""El checkout de la política en ``validate.yml`` alcanza el respaldo de REQ-PERM-4.

06-permisos REQ-PERM-4: si ``roles.yaml`` o ``project.yaml`` de la punta de la base no
valida, también el CI DEBE usar la versión válida más reciente de los últimos 100 commits
de primer padre. ``read_policy_dir`` busca ese respaldo con ``rev-list --first-parent``
desde ``HEAD`` del checkout ``base/``. Con la profundidad por defecto de
``actions/checkout`` (1) el checkout solo tiene la punta: un ``roles.yaml`` roto en la
base hacía fallar todo PR con ``E_CONFIG_POLICY_UNAVAILABLE``, incluido el que lo arregla.

La prueba funcional clona un origen local con la profundidad que fija el workflow, igual
que ``actions/checkout``, y comprueba que el respaldo está justo en el borde de la ventana.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from aucove.config.base import FALLBACK_WINDOW, read_policy_dir
from aucove.ops.result import OpError
from tests.conftest import FIXTURES, GIT_OPTIONS, isolated_git_env, roles_fixture
from tests.render.test_template_workflows import action_of, jobs, steps_of

PROJECT = (FIXTURES / "config" / "project" / "minimal.yaml").read_bytes()
ROLES = roles_fixture("basic").encode("utf-8")
ROLES_V2 = roles_fixture("no-deputy").encode("utf-8")
BAD_ROLES = ROLES.replace(b"lead: ana", b"lead: zoe")  # cumple el esquema y rompe REQ-SCHEMA-24
POLICY_ARG = '--policy "$GITHUB_WORKSPACE/base"'


def base_checkouts() -> dict[str, dict[str, Any]]:
    """Checkout de ``base/`` (la política) de cada job de ``validate.yml``."""
    found: dict[str, dict[str, Any]] = {}
    for job_id, job in jobs("validate.yml").items():
        for step in steps_of(job):
            with_ = step.get("with") or {}
            if action_of(step) == "actions/checkout" and with_.get("path") == "base":
                assert job_id not in found, f"{job_id}: dos checkouts de base/"
                found[job_id] = with_
    return found


def depth_of(with_: Mapping[str, Any]) -> int:
    """``fetch-depth`` efectivo de ``actions/checkout`` (1 si no se indica; 0 es la historia completa)."""
    value = with_.get("fetch-depth", 1)
    assert isinstance(value, int) and not isinstance(value, bool), value
    return value


def test_every_job_that_reads_the_policy_checks_out_the_fallback_window() -> None:
    readers = {
        job_id
        for job_id, job in jobs("validate.yml").items()
        if any(POLICY_ARG in str(step.get("run", "")) for step in steps_of(job))
    }
    checkouts = base_checkouts()
    assert readers == {"pin", "validate"}
    assert readers <= set(checkouts)
    for job_id in sorted(readers):
        depth = depth_of(checkouts[job_id])
        assert depth == 0 or depth >= FALLBACK_WINDOW, (
            f"{job_id}: fetch-depth {depth} no alcanza los {FALLBACK_WINDOW} commits de REQ-PERM-4"
        )


# -- prueba funcional sobre un clon superficial ------------------------------------------


def git(env: Mapping[str, str], cwd: Path, *args: str, stdin: bytes | None = None) -> str:
    proc = subprocess.run(
        ["git", *GIT_OPTIONS, *args], cwd=cwd, env=dict(env), input=stdin, capture_output=True, check=False
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return proc.stdout.decode("utf-8", "replace")


def fast_import_stream(commits: Sequence[Mapping[str, bytes]]) -> bytes:
    """Historia lineal en ``refs/heads/main``: cada commit escribe los archivos indicados."""
    out: list[bytes] = []
    for index, files in enumerate(commits, start=1):
        message = f"c{index - 1}".encode()
        out.append(b"commit refs/heads/main\n")
        out.append(b"mark :%d\n" % index)
        out.append(b"committer Test Committer <committer@example.invalid> %d +0000\n" % (1790000000 + index))
        out.append(b"data %d\n%s\n" % (len(message), message))
        if index > 1:
            out.append(b"from :%d\n" % (index - 1))
        for path, data in files.items():
            out.append(b"M 100644 inline %s\ndata %d\n%s\n" % (path.encode(), len(data), data))
    out.append(b"done\n")
    return b"".join(out)


@pytest.fixture
def origin(tmp_path: Path) -> tuple[Path, dict[str, str], str]:
    """Origen con 103 commits: la última versión válida de roles.yaml queda en el borde de la ventana.

    c0 crea la política (fuera de la ventana), c3 deja un ``roles.yaml`` válido distinto
    (posición 99 de primer padre desde la punta, la última de la ventana), c4 lo rompe y
    c5..c102 no tocan nada. La punta tiene el ``roles.yaml`` roto.
    """
    env = isolated_git_env(tmp_path / "git-env")
    repo = tmp_path / "origin.git"
    git(env, tmp_path, "init", "-q", "--bare", "-b", "main", str(repo))
    commits: list[Mapping[str, bytes]] = [{"project.yaml": PROJECT, "roles.yaml": ROLES}, {}, {}]
    commits.append({"roles.yaml": ROLES_V2})
    commits.append({"roles.yaml": BAD_ROLES})
    commits.extend({} for _ in range(FALLBACK_WINDOW - 2))
    git(env, repo, "fast-import", "--quiet", stdin=fast_import_stream(commits))
    window = git(env, repo, "rev-list", "--first-parent", f"--max-count={FALLBACK_WINDOW}", "main").split()
    assert len(window) == FALLBACK_WINDOW
    edge = window[-1]
    assert git(env, repo, "show", f"{edge}:roles.yaml").encode() == ROLES_V2
    return repo, env, edge


def clone(origin_repo: Path, env: Mapping[str, str], dest: Path, depth: int) -> Path:
    """Como ``actions/checkout``: ``fetch-depth: 0`` es la historia completa."""
    args = ["clone", "-q", "--branch", "main"]
    if depth > 0:
        args += ["--depth", str(depth)]
    git(env, dest.parent, *args, origin_repo.as_uri(), str(dest))
    assert (dest / "roles.yaml").read_bytes() == BAD_ROLES
    return dest


def use_env(monkeypatch: pytest.MonkeyPatch, env: Mapping[str, str]) -> None:
    """``read_policy_dir`` ejecuta git con el entorno del proceso."""
    for key in list(os.environ):
        if key.upper().startswith("GIT_") and key not in env:
            monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("job_id", ["pin", "validate"])
def test_policy_fallback_works_with_the_workflow_checkout(
    origin: tuple[Path, dict[str, str], str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, job_id: str
) -> None:
    repo, env, edge = origin
    depth = depth_of(base_checkouts()[job_id])
    base = clone(repo, env, tmp_path / "base", depth)
    use_env(monkeypatch, env)
    snap = read_policy_dir(base)
    assert snap.fallback_sha == edge
    assert dict(snap.fallbacks) == {"roles.yaml": edge}
    assert snap.roles.lead == "ana"


def test_full_history_gives_the_same_fallback(
    origin: tuple[Path, dict[str, str], str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, env, edge = origin
    base = clone(repo, env, tmp_path / "base", 0)
    use_env(monkeypatch, env)
    assert read_policy_dir(base).fallback_sha == edge


@pytest.mark.parametrize("depth", [1, FALLBACK_WINDOW - 1])
def test_a_checkout_shorter_than_the_window_fails_closed(
    origin: tuple[Path, dict[str, str], str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, depth: int
) -> None:
    """Documenta el fallo cerrado: sin la versión válida en el clon, ``E_CONFIG_POLICY_UNAVAILABLE``."""
    repo, env, _edge = origin
    base = clone(repo, env, tmp_path / "base", depth)
    use_env(monkeypatch, env)
    with pytest.raises(OpError) as info:
        read_policy_dir(base)
    assert info.value.code == "E_CONFIG_POLICY_UNAVAILABLE"
    assert info.value.details["path"] == "roles.yaml"
    assert info.value.details["reason"] == "no_valid_version"
