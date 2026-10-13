# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Las instrucciones publicadas instalan y verifican la versión del paquete.

01-arquitectura REQ-ARCH-30: toda instrucción de instalación publicada fija la versión exacta.
10-seguridad REQ-SEC-45 y REQ-SEC-52: con el ``requirements-lock.txt`` del Release de esa versión,
y la verificación de su rueda. Al subir ``__version__`` en ``_meta.py``, estas pruebas obligan a
actualizar el README y la guía de uso en el mismo commit (guía de mantenimiento, punto 10).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from aucove import _meta
from tests.conftest import REPO_ROOT

VERSION = _meta.__version__
NAME = _meta.NAME
FRAMEWORK_REPO = tomllib.loads((REPO_ROOT / "tools" / "template_vars.toml").read_text(encoding="utf-8"))["framework_repo"]
README = REPO_ROOT / "README.md"
PUBLISHED = [README, *sorted((REPO_ROOT / "docs").rglob("*.md"))]


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_readme_installs_the_package_version() -> None:
    readme = text(README)
    pins = re.findall(rf'uv tool install "{NAME}==([^"]+)"', readme)
    locks = re.findall(
        rf"https://github\.com/{re.escape(FRAMEWORK_REPO)}/releases/download/v([^/\s]+)/requirements-lock\.txt", readme
    )
    assert pins and set(pins) == {VERSION}
    assert locks and set(locks) == {VERSION}
    assert f"`{VERSION}`" in readme  # el estado nombra la versión publicada


def test_readme_verifies_the_wheel_of_the_package_version() -> None:
    readme = text(README)
    wheels = re.findall(rf"{NAME}-([0-9][^-\s]*)-py3-none-any\.whl", readme)
    assert wheels and set(wheels) == {VERSION}
    assert f"--repository https://github.com/{FRAMEWORK_REPO} " in readme
    assert f"-R {FRAMEWORK_REPO}" in readme


@pytest.mark.parametrize(
    "pattern",
    [
        rf"{NAME}(?:\[[a-z,]+\])?==([0-9][^\"'\s]*)",
        r"/releases/download/v([^/\s]+)/",
        rf"{NAME}-([0-9][^-\s]*)-py3-none-any\.whl",
        rf"--branch v(\S+) [^\n]*github\.com/{re.escape(FRAMEWORK_REPO)}\.git",
    ],
    ids=["pin", "lock", "wheel", "framework-clone"],
)
def test_published_instructions_only_name_the_package_version(pattern: str) -> None:
    """Ni el README ni la guía nombran otra versión del paquete (una anterior, al subirla)."""
    other = {}
    for path in PUBLISHED:
        hits = set(re.findall(pattern, text(path)))
        if hits - {VERSION}:
            other[path.relative_to(REPO_ROOT).as_posix()] = sorted(hits)
    assert other == {}
