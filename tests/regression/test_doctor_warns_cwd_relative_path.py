# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``doctor`` avisa de las entradas de ``PATH`` que dependen del directorio actual.

El spike S4 (00-marco D-52, 2026-10-07) mostró que Claude Code 2.1.289 busca el ``command`` de
un hook en forma exec solo en ``PATH``, sin el directorio actual, pero resuelve las entradas
relativas (``.``, ``sub``) contra el directorio del hook, dentro del proyecto: con una de ellas en
el ``PATH`` corre el ``aucove.exe`` del proyecto en lugar del instalado (10-seguridad TH-24).
En Windows una entrada vacía se ignora; fuera de Windows es el directorio actual (POSIX). El
check ``python`` agrega el aviso al mensaje que ya daba sobre el lanzador (01 REQ-ARCH-33).
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
WINDOWS = os.name == "nt"


def _python_version() -> str:
    v = sys.version_info
    return f"{v.major}.{v.minor}.{v.micro}"


def _relative(shown: str) -> str:
    return T("doctor.python.warn_relative_path", entries=shown, name=_meta.NAME)


def _env(path_entries: list[str]) -> dict[str, str]:
    env = dr.clean_env(os.environ)
    env["PATH"] = os.pathsep.join(path_entries)
    return env


# -- la función ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", [".", "..", "bin", os.path.join(".", "bin"), os.path.join("sub", "dir")])
def test_relative_entries_are_reported(tmp_path: Path, entry: str) -> None:
    assert op.cwd_path_entries(os.pathsep.join([str(tmp_path), entry, str(tmp_path / "bin")])) == [entry]


def test_absolute_entries_are_not_reported(tmp_path: Path) -> None:
    assert op.cwd_path_entries(os.pathsep.join([str(tmp_path), str(tmp_path / "bin")])) == []


def test_empty_entry_counts_only_outside_windows(tmp_path: Path) -> None:
    found = op.cwd_path_entries(os.pathsep.join([str(tmp_path), "", str(tmp_path / "bin")]))
    assert found == ([] if WINDOWS else [""])


@pytest.mark.skipif(not WINDOWS, reason="unidades solo en Windows")
@pytest.mark.parametrize("entry", ["C:bin", "\\bin"])
def test_drive_relative_and_rooted_entries_are_reported_on_windows(entry: str) -> None:
    assert op.cwd_path_entries(f"C:\\Windows;{entry}") == [entry]


@pytest.mark.skipif(not WINDOWS, reason="las comillas en PATH son de Windows")
def test_quoted_absolute_entry_is_not_reported(tmp_path: Path) -> None:
    assert op.cwd_path_entries(f'"{tmp_path}";.') == ["."]


def test_each_entry_is_reported_once_in_path_order(tmp_path: Path) -> None:
    path = os.pathsep.join(["sub", str(tmp_path), ".", "sub"])
    assert op.cwd_path_entries(path) == ["sub", "."]


# -- el check python ------------------------------------------------------------------------


def test_launcher_ok_and_a_relative_entry_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    here = tmp_path / "fuera"
    here.mkdir()
    monkeypatch.chdir(here)
    folder = dr.fake_launcher(tmp_path / "bin")
    python = dr.checks_of(dr.run_op(here, _env([".", str(folder), dr.GIT_DIR]), cwd=here))["python"]
    launcher = str(folder / (_meta.NAME + EXE))
    ok = T("doctor.python.ok", version=_python_version(), name=_meta.NAME, launcher=launcher)
    assert python["status"] == "warn"
    assert os.path.normcase(str(python["message"])) == os.path.normcase(f"{ok} {_relative('`.`')}")
    assert python["fix"] == T("doctor.python.fix_relative_path")


def test_launcher_missing_and_a_relative_entry_give_both_fixes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    here = tmp_path / "fuera"
    here.mkdir()
    monkeypatch.chdir(here)
    python = dr.checks_of(dr.run_op(here, _env(["sub", dr.GIT_DIR]), cwd=here))["python"]
    missing = T("doctor.python.warn_launcher", version=_python_version(), name=_meta.NAME)
    assert python["status"] == "warn"
    assert python["message"] == f"{missing} {_relative('`sub`')}"
    install = T("doctor.python.fix_launcher", name=_meta.NAME, pin=_meta.__version__)
    assert python["fix"] == f"{install} {T('doctor.python.fix_relative_path')}"


def test_launcher_committed_in_the_project_and_a_dot_entry_is_the_s4_risk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El caso ``dot_in_path`` de S4: con ``.`` en el ``PATH``, Claude Code correría este lanzador."""
    repo = dr.new_repo(tmp_path)
    dr.fake_launcher(repo.path)
    monkeypatch.chdir(repo.path)
    env = dr.clean_env(repo.env)
    env["PATH"] = os.pathsep.join([".", dr.GIT_DIR])
    python = dr.checks_of(dr.run_op(repo.path, env, cwd=repo.path))["python"]
    missing = T("doctor.python.warn_launcher", version=_python_version(), name=_meta.NAME)
    assert python["status"] == "warn"
    assert python["message"] == f"{missing} {_relative('`.`')}"  # el de la raíz no cuenta como instalado
    assert "uv tool update-shell" in str(python["fix"])


def test_many_relative_entries_are_shortened(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    here = tmp_path / "fuera"
    here.mkdir()
    monkeypatch.chdir(here)
    folder = dr.fake_launcher(tmp_path / "bin")
    relative = [f"r{i}" for i in range(7)]
    python = dr.checks_of(dr.run_op(here, _env([*relative, str(folder), dr.GIT_DIR]), cwd=here))["python"]
    assert python["status"] == "warn"
    assert _relative("`r0`, `r1`, `r2`, `r3`, `r4`, …") in str(python["message"])


def test_english_catalog_has_the_same_warning() -> None:
    en = translator("en")
    shown = en("doctor.python.warn_relative_path", entries="`.`", name=_meta.NAME)
    assert shown.startswith("Also, the `PATH`") and f"`{_meta.NAME}`" in shown
    assert en("doctor.python.fix_relative_path").startswith("Remove those entries")
