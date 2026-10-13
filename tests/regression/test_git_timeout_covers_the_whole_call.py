# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""El timeout de git acota la llamada entera (01 REQ-ARCH-64).

Antes, ``Git.run`` usaba ``subprocess.run(capture_output=True, timeout=...)``. En
Windows, al vencer el plazo CPython mata el proceso y luego llama a ``communicate()``
sin timeout, que espera a que se cierren las tuberías. Si el proceso lanzado tenía un
descendiente que las heredaba (el git real tras el envoltorio ``Git\\cmd\\git.exe``
que el instalador de Git for Windows pone en el PATH, el ``sh`` de un alias ``!``, un
remote helper...), la llamada duraba lo que durara ese descendiente, que además
quedaba huérfano. Con ``input``, la escritura en stdin tampoco tenía límite. En POSIX
la espera sí terminaba, pero el descendiente seguía vivo.

Las pruebas usan un descendiente que duerme ``_NAP_S`` segundos y luego escribe un
marcador: la llamada debe terminar mucho antes y el marcador no debe aparecer nunca
(el árbol de procesos se mató).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from aucove.vcs import git as git_mod
from aucove.vcs.git import Git, GitTimeout
from tests.conftest import GitRepo

_NAP_S = 3.0  # lo que duerme el descendiente
_TIMEOUT_S = 0.5
_BOUND_S = _TIMEOUT_S + git_mod._DRAIN_S  # nunca se llega: el árbol muere al vencer el plazo

# Hijo que lanza un nieto con sus mismas stdout y stderr (las tuberías del llamador) y
# se queda esperando. El nieto escribe el marcador si nadie lo mata antes.
_GRANDCHILD = "import pathlib, sys, time; time.sleep(float(sys.argv[2])); pathlib.Path(sys.argv[1]).write_text('x')"
_CHILD = (
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3]],"
    " stdout=sys.stdout, stderr=sys.stderr)\n"
    "time.sleep(float(sys.argv[3]))\n"
)


def _assert_never_appears(marker: Path, started: float) -> None:
    """Espera a que el descendiente hubiera escrito el marcador y comprueba que no lo hizo."""
    remaining = started + _NAP_S + 1.0 - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
    assert not marker.exists(), "el descendiente siguió vivo tras el timeout"


def test_timeout_is_bounded_when_a_grandchild_holds_the_pipes(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild.marker"
    cmd = [sys.executable, "-c", _CHILD, _GRANDCHILD, str(marker), str(_NAP_S)]
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        git_mod.run_bounded(cmd, cwd=tmp_path, env=os.environ, input=None, timeout=_TIMEOUT_S)
    elapsed = time.monotonic() - started
    assert elapsed < _BOUND_S, f"la llamada duró {elapsed:.1f} s con timeout de {_TIMEOUT_S} s"
    _assert_never_appears(marker, started)


def test_timeout_is_bounded_when_stdin_is_never_read(tmp_path: Path) -> None:
    marker = tmp_path / "unread-stdin.marker"
    # el proceso no lee stdin; 8 MiB no caben en el búfer de la tubería
    script = "import pathlib, sys, time; time.sleep(float(sys.argv[2])); pathlib.Path(sys.argv[1]).write_text('x')"
    cmd = [sys.executable, "-c", script, str(marker), str(_NAP_S)]
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        git_mod.run_bounded(cmd, cwd=tmp_path, env=os.environ, input=b"x" * (8 << 20), timeout=_TIMEOUT_S)
    elapsed = time.monotonic() - started
    assert elapsed < _BOUND_S, f"la llamada duró {elapsed:.1f} s con timeout de {_TIMEOUT_S} s"
    _assert_never_appears(marker, started)


def test_git_timeout_kills_the_shell_of_an_alias(git_repo: GitRepo) -> None:
    """El escenario real: ``git`` lanza ``sh``, y ``sh`` un proceso que hereda las tuberías."""
    marker = git_repo.path / "alias.marker"
    python = Path(sys.executable).as_posix()
    code = f"import pathlib, time; time.sleep({_NAP_S}); pathlib.Path('alias.marker').write_text('x')"
    alias = f"alias.slow=!'{python}' -c \"{code}\""
    g = Git(git_repo.path, env=git_repo.env, timeout_s=_TIMEOUT_S)
    started = time.monotonic()
    with pytest.raises(GitTimeout):
        g.run("-c", alias, "slow")
    elapsed = time.monotonic() - started
    assert elapsed < _BOUND_S, f"git duró {elapsed:.1f} s con timeout de {_TIMEOUT_S} s"
    _assert_never_appears(marker, started)
    # el repositorio sigue usable y la siguiente llamada no hereda nada
    assert g.run("rev-parse", "--is-inside-work-tree").stdout.strip() == b"true"
