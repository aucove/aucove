# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""El lanzador de las pruebas e2e y un ``aucove.exe`` en el directorio actual.

``tests/e2e/conftest.py::find_launcher`` usaba ``shutil.which(NAME)``. En Windows,
``shutil.which`` de Python 3.11 antepone siempre el directorio actual, y en cualquier
plataforma una entrada vacía o relativa de ``PATH`` (``.``) también se resuelve contra él.
Con un ``aucove.exe`` en el directorio desde el que se lanza pytest, la autoprueba del CI
(00-marco D-37) ejercía ese archivo en lugar del lanzador instalado. Ahora delega en
``aucove.ops.doctor.find_launcher``, que solo mira las carpetas absolutas de ``PATH``
(01 REQ-ARCH-33), y luego en el directorio de scripts del intérprete.
"""

from __future__ import annotations

import os
import sysconfig
from pathlib import Path

import pytest

from aucove import _meta
from tests.e2e import conftest as e2e

EXE = ".exe" if os.name == "nt" else ""


def _fake_launcher(folder: Path) -> Path:
    """Carpeta con un lanzador falso (``.exe`` en Windows, ejecutable en POSIX)."""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (_meta.NAME + EXE)
    target.write_bytes(b"")
    target.chmod(0o755)
    return target


@pytest.fixture
def scripts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Directorio de scripts del intérprete, vacío salvo que la prueba ponga un lanzador."""
    folder = tmp_path / "scripts"
    folder.mkdir()
    real = sysconfig.get_path

    def get_path(name: str, *args: object, **kwargs: object) -> str:
        return str(folder) if name == "scripts" else real(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sysconfig, "get_path", get_path)
    return folder


@pytest.mark.parametrize("entry", [None, ".", ""], ids=["sin-entrada", "punto", "vacia"])
def test_a_launcher_only_in_the_working_directory_is_not_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scripts: Path, entry: str | None
) -> None:
    here = _fake_launcher(tmp_path / "aqui").parent
    empty = tmp_path / "vacio"
    empty.mkdir()
    monkeypatch.chdir(here)
    entries = [str(empty)] if entry is None else [entry, str(empty)]
    monkeypatch.setenv("PATH", os.pathsep.join(entries))
    assert e2e.find_launcher() is None


def test_the_path_launcher_wins_over_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scripts: Path
) -> None:
    monkeypatch.chdir(_fake_launcher(tmp_path / "aqui").parent)
    installed = _fake_launcher(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(installed.parent))
    found = e2e.find_launcher()
    assert found is not None and os.path.isabs(found)
    assert os.path.normcase(found) == os.path.normcase(str(installed))


def test_the_scripts_directory_is_the_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scripts: Path) -> None:
    """Sin el venv activado, el lanzador está en el directorio de scripts del intérprete."""
    monkeypatch.chdir(_fake_launcher(tmp_path / "aqui").parent)
    empty = tmp_path / "vacio"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    installed = _fake_launcher(scripts)
    found = e2e.find_launcher()
    assert found is not None and os.path.normcase(found) == os.path.normcase(str(installed))
