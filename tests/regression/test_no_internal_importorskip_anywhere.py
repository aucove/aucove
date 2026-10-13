# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``importorskip`` sobre módulos internos en cualquier archivo de ``tests/``.

``test_internal_modules_without_importorskip.py`` solo revisaba las pruebas de punta a punta, y
otras usaban ``pytest.importorskip`` sobre módulos que H2 entrega: ``aucove.config.roles``
(``tests/render/test_render_template_tool.py``), ``aucove.localstore.store``
(``tests/hooks/test_fixtures.py``) y ``aucove.ops.rules.roles_sync`` y
``aucove.ops.rules.catalog`` (``tests/unit/test_scope_golden.py``). Si el módulo se
renombra o se borra, la prueba sale como SKIPPED y el job queda en verde (12-mvp §3.1, §4).

Sus dueños ya los importan directamente, así que la comprobación recorre ahora todos los
``tests/**/*.py`` con el mismo detector: rechaza un ``importorskip`` cuyo módulo de primer
nivel es el paquete del framework o ``tests``, o cuyo nombre no se puede resolver. Las
dependencias externas opcionales (``packaging``, ``jsonschema``, ``hatchling``) siguen
permitidas.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aucove import _meta
from tests.conftest import REPO_ROOT
from tests.regression.test_internal_modules_without_importorskip import internal_importorskips

NAME = _meta.NAME


def offending(root: Path) -> list[tuple[str, int, str]]:
    """``(archivo, línea, módulo)`` de cada ``importorskip`` interno de ``root/tests/**/*.py``."""
    found: list[tuple[str, int, str]] = []
    for path in sorted((root / "tests").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        found.extend((rel, line, name) for line, name in internal_importorskips(path.read_text(encoding="utf-8")))
    return found


def test_no_test_file_skips_on_an_internal_module() -> None:
    assert offending(REPO_ROOT) == [], (
        "importorskip de un módulo del framework convierte una regresión en skip: impórtalo directamente"
    )


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    return tmp_path


# Los cuatro casos que motivaron la ampliación, más un archivo anidado y uno que no es de pruebas.
REINTRODUCED: tuple[tuple[str, str], ...] = (
    ("tests/render/test_render_template_tool.py", f'roles_mod = pytest.importorskip("{NAME}.config.roles")'),
    ("tests/hooks/test_fixtures.py", 'store_mod = pytest.importorskip(f"{_meta.NAME}.localstore.store")'),
    ("tests/unit/test_scope_golden.py", f'roles_sync = pytest.importorskip("{NAME}.ops.rules.roles_sync")'),
    ("tests/unit/test_scope_golden.py", f'catalog = pytest.importorskip("{NAME}.ops.rules.catalog")'),
    ("tests/contract/sub/test_y.py", f'pytest.importorskip("{NAME}.ops.validate")'),
    ("tests/factories.py", 'pytest.importorskip("tests.e2e.conftest")'),
)


@pytest.mark.parametrize(("rel", "line"), REINTRODUCED)
def test_detects_an_internal_importorskip_anywhere_under_tests(tmp_path: Path, rel: str, line: str) -> None:
    root = _tree(tmp_path, {rel: f"import pytest\nfrom {NAME} import _meta\n{line}\n"})
    found = offending(root)
    assert [(r, n) for r, n, _ in found] == [(rel, 3)], found


def test_external_modules_are_allowed(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {
            "tests/unit/test_ext.py": (
                "import pytest\n"
                'pytest.importorskip("packaging.specifiers")\n'
                'pytest.importorskip("jsonschema")\n'
                'pytest.importorskip("hatchling")\n'
            ),
            "tests/regression/test_doc.py": f'"""Menciona pytest.importorskip("{NAME}.x") solo como texto."""\n',
        },
    )
    assert offending(root) == []
