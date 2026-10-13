# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``tools/sync_schemas.py`` no vacía ``schemas/`` con un ``--spec-dir`` equivocado.

Si ``<spec>/schemas`` no existía, la herramienta usaba la propia carpeta y tomaba cualquier
``*.json`` válido (un ``package.json``) como el conjunto completo de esquemas: borraba los
esquemas del destino, copiaba ese archivo, reescribía ``SHA256SUMS`` y salía con 0.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT, load_tool

SCHEMAS = REPO_ROOT / "schemas"


def _snapshot(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir()) if p.is_file()}


@pytest.fixture
def dest(tmp_path: Path) -> Path:
    target = tmp_path / "dest_schemas"
    shutil.copytree(SCHEMAS, target)
    return target


def test_folder_with_a_package_json_is_rejected(tmp_path: Path, dest: Path) -> None:
    tool = load_tool("sync_schemas")
    other = tmp_path / "otro"
    other.mkdir()
    (other / "package.json").write_text('{"name": "x", "version": "1.0.0"}\n', encoding="utf-8")
    before = _snapshot(dest)
    assert tool.main(["--spec-dir", str(other), "--dest", str(dest)]) == 1
    assert _snapshot(dest) == before


def test_foreign_schemas_do_not_replace_the_known_ones(tmp_path: Path, dest: Path) -> None:
    tool = load_tool("sync_schemas")
    other = tmp_path / "otro" / "schemas"
    other.mkdir(parents=True)
    (other / "invoice.schema.json").write_text('{"type": "object"}\n', encoding="utf-8")
    before = _snapshot(dest)
    assert tool.main(["--spec-dir", str(other.parent), "--dest", str(dest)]) == 1
    assert _snapshot(dest) == before


def test_real_spec_layout_still_syncs(tmp_path: Path, dest: Path) -> None:
    tool = load_tool("sync_schemas")
    spec = tmp_path / "spec"
    shutil.copytree(SCHEMAS, spec / "schemas", ignore=shutil.ignore_patterns("SHA256SUMS"))
    (spec / "schemas" / "package.json").write_text("{}\n", encoding="utf-8")  # no es un esquema
    assert tool.main(["--spec-dir", str(spec), "--dest", str(dest)]) == 0
    assert _snapshot(dest) == _snapshot(SCHEMAS)
    assert tool.main(["--spec-dir", str(spec), "--dest", str(dest), "--check"]) == 0
