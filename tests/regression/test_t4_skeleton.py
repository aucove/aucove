# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""El esqueleto de T4 (12-mvp §3.5).

1. ``test_seed_local_state_round_trips_through_the_store`` (``tests/hooks/test_fixtures.py``)
   empezaba con ``pytest.importorskip`` de ``localstore.store``, un módulo que entrega H2.
   Si se renombraba o se borraba, la prueba salía como SKIPPED. Ahora el módulo se
   importa directamente y los archivos de T4 entran en el detector de ``importorskip``:
   con el módulo ausente, la prueba falla con ``ModuleNotFoundError`` y no se omite.
2. ``runner.resolve_launcher`` buscaba el lanzador con ``shutil.which``. En Python 3.11
   para Windows, ``shutil.which`` antepone siempre el directorio actual, y en cualquier
   plataforma una entrada vacía o relativa de ``PATH`` se resuelve contra él. REQ-MVP-30
   pide el lanzador instalado «resuelto desde el PATH», así que un ``aucove.exe`` que
   solo está en el directorio actual no cuenta. Ahora se usa ``find_launcher`` de
   ``doctor`` (solo carpetas absolutas, con ``PATHEXT``), tanto en el ``PATH`` como en el
   directorio de scripts del intérprete.
"""

from __future__ import annotations

import os
import sysconfig
from collections.abc import Iterator
from pathlib import Path

import pytest

from aucove import _meta
from tests.conftest import REPO_ROOT
from tests.hooks import runner
from tests.regression.test_internal_modules_without_importorskip import _fresh_import, _hide, internal_importorskips

# Archivos de código de T4.
T4_FILES: tuple[str, ...] = (
    "tests/hooks/runner.py",
    "tests/hooks/test_fixtures.py",
    "tests/skills/lint.py",
    "tests/skills/test_lint.py",
)

STORE = f"{_meta.NAME}.localstore.store"
TEST_FIXTURES = "tests.hooks.test_fixtures"
EXE = ".exe" if os.name == "nt" else ""


# -- 1. importorskip sobre localstore.store ------------------------------------------------


@pytest.mark.parametrize("rel", T4_FILES)
def test_t4_files_do_not_skip_on_internal_modules(rel: str) -> None:
    source = (REPO_ROOT / rel).read_text(encoding="utf-8")
    assert internal_importorskips(source) == [], (
        f"{rel}: importorskip de un módulo del framework convierte una regresión en skip"
    )


def test_seed_round_trip_fails_when_the_store_module_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, git_env: dict[str, str]
) -> None:
    _hide(monkeypatch, STORE)
    try:
        module = _fresh_import(monkeypatch, TEST_FIXTURES)
    except ModuleNotFoundError as exc:
        # Importado en la cabecera: la colección del archivo falla, no se omite.
        assert exc.name == STORE, exc
        return
    test = module.test_seed_local_state_round_trips_through_the_store  # type: ignore[attr-defined]
    try:
        test(tmp_path=tmp_path, git_env=git_env)
    except pytest.skip.Exception as exc:
        pytest.fail(f"la prueba de la caché local se omite en vez de fallar: {exc}")
    except ModuleNotFoundError as exc:
        assert exc.name == STORE, exc
    else:
        pytest.fail("la prueba de la caché local no detectó que falta el módulo")


# -- 2. el lanzador, solo desde carpetas absolutas -----------------------------------------


def _fake_launcher(directory: Path) -> Path:
    """Lanzador falso: ``aucove.exe`` vacío en Windows, script ejecutable en POSIX."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (_meta.NAME + EXE)
    if os.name == "nt":
        path.write_bytes(b"")
    else:
        path.write_bytes(b"#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    return path


def _empty(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@pytest.fixture
def scripts_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Sustituye el directorio de scripts del intérprete por una carpeta temporal vacía."""
    folder = _empty(tmp_path / "scripts")
    original = sysconfig.get_path

    def get_path(name: str, *args: object, **kwargs: object) -> str:
        return str(folder) if name == "scripts" else original(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner.sysconfig, "get_path", get_path)
    yield folder


def test_resolve_launcher_finds_it_in_an_absolute_path_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(_empty(tmp_path / "fuera"))
    launcher = _fake_launcher(tmp_path / "bin")
    found = runner.resolve_launcher(str(tmp_path / "bin"))
    assert found is not None and os.path.isabs(found)
    assert os.path.normcase(found) == os.path.normcase(str(launcher))


def test_resolve_launcher_ignores_the_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """El escenario del hallazgo: ``aucove.exe`` solo en el directorio actual."""
    monkeypatch.chdir(_fake_launcher(tmp_path / "aqui").parent)
    assert runner.resolve_launcher(str(_empty(tmp_path / "vacio"))) is None


@pytest.mark.parametrize("entry", [".", "", "aqui", "./aqui"])
def test_resolve_launcher_ignores_relative_path_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    _fake_launcher(tmp_path / "aqui")
    monkeypatch.chdir(tmp_path / "aqui" if entry in (".", "") else tmp_path)
    path = os.pathsep.join([entry, str(_empty(tmp_path / "vacio"))])
    assert runner.resolve_launcher(path) is None


def test_resolve_launcher_from_the_process_path_ignores_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scripts_dir: Path
) -> None:
    monkeypatch.setenv("PATH", str(_empty(tmp_path / "vacio")))
    monkeypatch.chdir(_fake_launcher(tmp_path / "aqui").parent)
    assert runner.resolve_launcher() is None


def test_resolve_launcher_falls_back_to_the_scripts_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scripts_dir: Path
) -> None:
    monkeypatch.setenv("PATH", str(_empty(tmp_path / "vacio")))
    monkeypatch.chdir(_fake_launcher(tmp_path / "aqui").parent)
    launcher = _fake_launcher(scripts_dir)
    found = runner.resolve_launcher()
    assert found is not None and os.path.normcase(found) == os.path.normcase(str(launcher))


def test_resolve_launcher_ignores_a_relative_scripts_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El directorio de scripts también se busca sin depender del directorio actual."""
    monkeypatch.setenv("PATH", str(_empty(tmp_path / "vacio")))
    _fake_launcher(tmp_path / "aqui")
    monkeypatch.chdir(tmp_path)
    original = sysconfig.get_path

    def get_path(name: str, *args: object, **kwargs: object) -> str:
        return "aqui" if name == "scripts" else original(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runner.sysconfig, "get_path", get_path)
    assert runner.resolve_launcher() is None
