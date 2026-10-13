# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""El conjunto dorado de A9 no se omite cuando falta un módulo de H2.

``tests/unit/test_scope_golden.py`` llamaba a ``pytest.importorskip`` sobre
``ops.rules.roles_sync`` y ``ops.rules.catalog``, que H2 entrega. Con esos módulos ocultos,
la corrida daba 48 SKIPPED y ningún fallo, así que un renombre o un borrado dejaba el job en
verde (12-mvp §3.1 REQ-MVP-21: un skip no es un pase). Además, una regla declarada en
``expect.checks`` que no estaba en el catálogo salía como skip, aunque el caso dorado la
exige.

Ahora los dos módulos se importan en la cabecera (la colección del archivo falla si no
están) y una regla ausente del catálogo es un fallo. Se comprueba de forma estática, con
el detector ``internal_importorskips`` de ``test_internal_modules_without_importorskip``, y de
forma dinámica,
ocultando cada módulo y volviendo a importar el archivo.
"""

from __future__ import annotations

import ast
import importlib
import sys
from types import ModuleType

import pytest

from aucove import _meta
from tests.conftest import REPO_ROOT
from tests.regression.test_internal_modules_without_importorskip import internal_importorskips

GOLDEN = "tests.unit.test_scope_golden"
GOLDEN_FILE = REPO_ROOT / "tests" / "unit" / "test_scope_golden.py"
ROLES_SYNC = f"{_meta.NAME}.ops.rules.roles_sync"
CATALOG = f"{_meta.NAME}.ops.rules.catalog"


def _skip_calls(source: str) -> list[int]:
    """Líneas con ``pytest.skip(...)`` o ``skip(...)``: en el conjunto dorado no caben."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
        if name == "skip":
            lines.append(node.lineno)
    return lines


def test_golden_file_does_not_importorskip_internal_modules() -> None:
    source = GOLDEN_FILE.read_text(encoding="utf-8")
    assert internal_importorskips(source) == [], (
        "test_scope_golden.py: importorskip de un módulo del framework convierte una regresión en skip"
    )


def test_golden_file_does_not_skip() -> None:
    source = GOLDEN_FILE.read_text(encoding="utf-8")
    assert _skip_calls(source) == [], "test_scope_golden.py: un caso dorado no se omite, falla"


def _hide(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Deja ``name`` sin importar: el ``import`` lanza ``ModuleNotFoundError``."""
    monkeypatch.setitem(sys.modules, name, None)
    parent, _, child = name.rpartition(".")
    package = sys.modules.get(parent)
    if package is not None and hasattr(package, child):
        # ``from paquete import hijo`` usa el atributo del paquete si ya existe.
        monkeypatch.delattr(package, child)


def _fresh_golden(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Importa de nuevo el archivo dorado; ``monkeypatch`` deja ``sys.modules`` como estaba."""
    monkeypatch.setitem(sys.modules, GOLDEN, None)
    del sys.modules[GOLDEN]
    return importlib.import_module(GOLDEN)


@pytest.mark.parametrize(
    ("hidden", "test_name", "kwargs"),
    [
        (
            ROLES_SYNC,
            "test_declared_prior_is_what_roles_sync_and_generated_sync_compute",
            {"cid": "ci-01-own-sections-and-note"},
        ),
        (
            CATALOG,
            "test_other_declared_checks_give_the_expected_status",
            {"cid": "ci-01-own-sections-and-note", "check": "decisions-immutable"},
        ),
    ],
)
def test_golden_fails_when_an_internal_module_is_missing(
    monkeypatch: pytest.MonkeyPatch, hidden: str, test_name: str, kwargs: dict[str, str]
) -> None:
    _hide(monkeypatch, hidden)
    try:
        module = _fresh_golden(monkeypatch)
    except ModuleNotFoundError as exc:
        # Importado en la cabecera: la colección del archivo falla, no se omite.
        assert exc.name == hidden, exc
        return
    test = getattr(module, test_name)
    try:
        test(built=None, **kwargs)
    except pytest.skip.Exception as exc:
        pytest.fail(f"{test_name} se omite en vez de fallar sin {hidden}: {exc}")
    except ModuleNotFoundError as exc:
        assert exc.name == hidden, exc
    else:
        pytest.fail(f"{test_name} no detectó que falta {hidden}")


def test_a_declared_check_missing_from_the_catalog_fails() -> None:
    golden = importlib.import_module(GOLDEN)
    test = golden.test_other_declared_checks_give_the_expected_status
    try:
        # La regla no existe: la prueba falla antes de mirar el escenario (built).
        test(built=None, cid="ci-01-own-sections-and-note", check="no-such-rule")
    except pytest.skip.Exception as exc:
        pytest.fail(f"una regla del conjunto dorado ausente del catálogo se omite: {exc}")
    except pytest.fail.Exception as exc:
        assert "no-such-rule" in str(exc)
    else:
        pytest.fail("una regla ausente del catálogo no hizo fallar la prueba")
