# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""El check ``python`` de ``doctor`` y un lanzador en el directorio actual.

``shutil.which(NAME, path=PATH)`` de Python 3.11 en Windows antepone siempre el directorio
actual, y en cualquier plataforma una entrada vacía o relativa de ``PATH`` (``.``, ``bin``)
también se resuelve contra él. Con un ``aucove.exe`` en el directorio de trabajo (por
ejemplo, commiteado en la raíz del proyecto), ``doctor`` daba ``ok`` con
``launcher='.\\aucove.EXE'`` aunque el lanzador no estuviera en el ``PATH`` con que los
hooks invocan al núcleo (01 REQ-ARCH-31 y REQ-ARCH-33). El lanzador se busca ahora solo en
las carpetas absolutas de ``PATH``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from aucove import _meta
from aucove.i18n import translator
from aucove.ops import doctor as op
from tests.unit import test_op_doctor as dr

pytestmark = dr.pytestmark  # sin git, doctor no se puede ejecutar

T = translator("es")
EXE = ".exe" if os.name == "nt" else ""


def _python_version() -> str:
    v = sys.version_info
    return f"{v.major}.{v.minor}.{v.micro}"


def _env(path_entries: list[str]) -> dict[str, str]:
    env = dr.clean_env(os.environ)
    env["PATH"] = os.pathsep.join(path_entries)
    return env


def _not_on_path() -> str:
    return T("doctor.python.warn_launcher", version=_python_version(), name=_meta.NAME)


# -- la función de búsqueda --------------------------------------------------------------


def test_find_launcher_ignores_the_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    here = dr.fake_launcher(tmp_path / "aqui")
    monkeypatch.chdir(here)
    empty = tmp_path / "vacio"
    empty.mkdir()
    assert op.find_launcher(_meta.NAME, path=str(empty)) is None


@pytest.mark.parametrize("entry", [".", "", "aqui", "./aqui"])
def test_find_launcher_ignores_relative_path_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str) -> None:
    dr.fake_launcher(tmp_path / "aqui")
    monkeypatch.chdir(tmp_path / "aqui" if entry in (".", "") else tmp_path)
    assert op.find_launcher(_meta.NAME, path=os.pathsep.join([entry, str(tmp_path / "vacio")])) is None


def test_find_launcher_finds_it_in_an_absolute_entry_before_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    here = dr.fake_launcher(tmp_path / "aqui")
    monkeypatch.chdir(here)
    folder = dr.fake_launcher(tmp_path / "bin")
    found = op.find_launcher(_meta.NAME, path=str(folder))
    assert found is not None and os.path.isabs(found)
    assert os.path.normcase(found) == os.path.normcase(str(folder / (_meta.NAME + EXE)))


@pytest.mark.skipif(os.name != "nt", reason="PATHEXT solo existe en Windows")
def test_find_launcher_follows_pathext_on_windows(tmp_path: Path) -> None:
    folder = dr.fake_launcher(tmp_path / "bat", suffix=".bat")
    found = op.find_launcher(_meta.NAME, path=str(folder), pathext=".COM;.EXE;.BAT")
    assert found is not None and found.lower().endswith(".bat")  # doctor avisa con warn_not_exe
    assert op.find_launcher(_meta.NAME, path=str(folder), pathext=".COM;.EXE") is None


@pytest.mark.skipif(os.name != "nt", reason="unidades solo en Windows")
def test_find_launcher_ignores_a_rooted_entry_without_drive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = dr.fake_launcher(tmp_path / "bin")
    monkeypatch.chdir(folder)
    rooted = os.path.splitdrive(str(folder))[1]  # '\\Users\\...\\bin': depende de la unidad actual
    assert op.find_launcher(_meta.NAME, path=rooted) is None


# -- el check python -----------------------------------------------------------------------


def test_launcher_committed_in_the_project_root_is_not_on_the_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """El escenario del hallazgo: aucove.exe en la raíz y el directorio actual en el repo."""
    repo = dr.new_repo(tmp_path)
    dr.fake_launcher(repo.path)
    monkeypatch.chdir(repo.path)
    env = dr.clean_env(repo.env)
    env["PATH"] = dr.GIT_DIR
    python = dr.checks_of(dr.run_op(repo.path, env, cwd=repo.path))["python"]
    assert (python["status"], python["message"]) == ("warn", _not_on_path())
    assert "uv tool update-shell" in str(python["fix"])  # REQ-ARCH-33


@pytest.mark.parametrize("entry", [".", ""])
def test_relative_path_entry_does_not_make_the_launcher_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str) -> None:
    here = dr.fake_launcher(tmp_path / "fuera")
    monkeypatch.chdir(here)
    python = dr.checks_of(dr.run_op(here, _env([entry, dr.GIT_DIR]), cwd=here))["python"]
    assert python["status"] == "warn"
    # Además, S4 (00-marco D-52): doctor avisa de la entrada que depende del directorio actual.
    # En Windows la entrada vacía no cuenta, porque Claude Code la ignora.
    if entry == "" and os.name == "nt":
        assert python["message"] == _not_on_path()
    else:
        shown = "`.`" if entry else '`""`'
        relative = T("doctor.python.warn_relative_path", entries=shown, name=_meta.NAME)
        assert python["message"] == f"{_not_on_path()} {relative}"


def test_launcher_on_the_path_is_reported_with_its_absolute_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    here = dr.fake_launcher(tmp_path / "fuera")
    monkeypatch.chdir(here)
    folder = dr.fake_launcher(tmp_path / "bin")
    python = dr.checks_of(dr.run_op(here, _env([str(folder), dr.GIT_DIR]), cwd=here))["python"]
    launcher = str(folder / (_meta.NAME + EXE))
    assert python["status"] == "ok"
    assert os.path.normcase(str(python["message"])) == os.path.normcase(
        T("doctor.python.ok", version=_python_version(), name=_meta.NAME, launcher=launcher)
    )
