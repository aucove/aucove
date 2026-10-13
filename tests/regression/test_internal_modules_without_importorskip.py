# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``importorskip`` sobre módulos internos de H2 (12-mvp §3.1 REQ-MVP-21, §4).

``test_ci_base_report_opens_and_closes_the_issue`` (``tests/e2e/test_template_ci.py``)
empezaba con ``pytest.importorskip`` de ``ops.ci_base_report``, que es un módulo que H2
entrega. Si se renombraba o se borraba, la prueba salía como SKIPPED y el job quedaba en
verde. ``importorskip`` queda solo para dependencias externas opcionales (``hatchling``,
``packaging``, ``jsonschema``); los módulos del paquete se importan directamente.

Se comprueba de dos formas:

- estática: ningún ``importorskip`` de los archivos de ``E2E_FILES`` apunta al paquete del framework
  (ni a ``tests``), ni usa un nombre que no se pueda resolver;
- dinámica: con ``ops.ci_base_report`` ausente, la prueba de punta a punta falla con
  ``ModuleNotFoundError`` (al importar su módulo o al llamarla) y no se omite.
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest

from aucove import _meta
from tests.conftest import REPO_ROOT

# Las pruebas de punta a punta y su infraestructura.
E2E_FILES: tuple[str, ...] = (
    "tests/e2e/conftest.py",
    "tests/e2e/test_template_ci.py",
    "tests/e2e/test_workflow_drift.py",
    "tests/e2e/test_a9.py",
    "tests/unit/test_completeness.py",
)

# Paquetes de primer nivel que son del propio repositorio: nunca son opcionales.
INTERNAL_TOPS = frozenset({_meta.NAME, "tests"})

CI_BASE_REPORT = f"{_meta.NAME}.ops.ci_base_report"
TEMPLATE_CI = "tests.e2e.test_template_ci"


def _is_importorskip(func: ast.expr) -> bool:
    if isinstance(func, ast.Attribute):
        return func.attr == "importorskip"
    return isinstance(func, ast.Name) and func.id == "importorskip"


def _module_name(arg: ast.expr) -> str | None:
    """Valor del primer argumento, si es una constante o un f-string sobre ``_meta``."""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    try:
        value = eval(  # noqa: S307 - solo f-strings sobre _meta, sin builtins
            compile(ast.Expression(arg), "<importorskip>", "eval"),
            {"__builtins__": {}, "_meta": _meta},
        )
    except Exception:  # noqa: BLE001 - nombre no resoluble: se informa como sospechoso
        return None
    return value if isinstance(value, str) else None


def internal_importorskips(source: str) -> list[tuple[int, str]]:
    """``importorskip`` de ``source`` sobre módulos internos o no resolubles."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call) or not _is_importorskip(node.func) or not node.args:
            continue
        name = _module_name(node.args[0])
        if name is None:
            found.append((node.lineno, ast.unparse(node.args[0])))
        elif name.split(".", 1)[0] in INTERNAL_TOPS:
            found.append((node.lineno, name))
    return found


def test_detector_flags_internal_modules_only() -> None:
    code = (
        "import pytest\n"
        "from aucove import _meta\n"
        'pytest.importorskip(f"{_meta.NAME}.ops.ci_base_report")\n'
        f'pytest.importorskip("{_meta.NAME}.config.roles")\n'
        'pytest.importorskip("tests.e2e.conftest")\n'
        "pytest.importorskip(some_name)\n"
        'pytest.importorskip("packaging.specifiers")\n'
        'pytest.importorskip("hatchling")\n'
        'importorskip("jsonschema")\n'
    )
    assert [line for line, _ in internal_importorskips(code)] == [3, 4, 5, 6]


@pytest.mark.parametrize("rel", E2E_FILES)
def test_listed_files_do_not_skip_on_internal_modules(rel: str) -> None:
    source = (REPO_ROOT / rel).read_text(encoding="utf-8")
    assert internal_importorskips(source) == [], (
        f"{rel}: importorskip de un módulo del framework convierte una regresión en skip"
    )


def _hide(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Deja ``name`` sin importar: el ``import`` lanza ``ModuleNotFoundError``."""
    monkeypatch.setitem(sys.modules, name, None)
    parent, _, child = name.rpartition(".")
    package = sys.modules.get(parent)
    if package is not None and hasattr(package, child):
        # ``from paquete import hijo`` usa el atributo del paquete si ya existe.
        monkeypatch.delattr(package, child)


def _fresh_import(monkeypatch: pytest.MonkeyPatch, name: str) -> object:
    """Importa ``name`` de nuevo; al terminar, ``monkeypatch`` deja ``sys.modules`` como estaba."""
    monkeypatch.setitem(sys.modules, name, None)
    del sys.modules[name]
    parent, _, child = name.rpartition(".")
    package = sys.modules.get(parent)
    if package is not None:
        monkeypatch.setattr(package, child, None, raising=False)
    return importlib.import_module(name)


def test_base_report_e2e_fails_when_the_module_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _hide(monkeypatch, CI_BASE_REPORT)
    try:
        module = _fresh_import(monkeypatch, TEMPLATE_CI)
    except ModuleNotFoundError as exc:
        # Importado en la cabecera: la colección del archivo falla, no se omite.
        assert exc.name == CI_BASE_REPORT, exc
        return
    test = module.test_ci_base_report_opens_and_closes_the_issue  # type: ignore[attr-defined]
    try:
        test(launcher=None, ci=None, tmp_path=tmp_path)
    except pytest.skip.Exception as exc:
        pytest.fail(f"la prueba de ci base-report se omite en vez de fallar: {exc}")
    except ModuleNotFoundError as exc:
        assert exc.name == CI_BASE_REPORT, exc
    else:
        pytest.fail("la prueba de ci base-report no detectó que falta el módulo")
