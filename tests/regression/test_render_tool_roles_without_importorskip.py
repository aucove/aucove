# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``importorskip`` sobre ``config.roles`` en la prueba de la herramienta de render.

``test_sha_matches_the_roles_parser_when_available`` (``tests/render/test_render_template_tool.py``)
empezaba con ``pytest.importorskip("aucove.config.roles")``. ``config/roles.py`` es un
módulo que H2 entrega (01 §3.1), no una dependencia opcional: si se renombraba o dejaba de
importar, la prueba salía como SKIPPED y la suite seguía en verde. ``importorskip`` queda
solo para dependencias externas opcionales; los módulos del paquete se importan directamente.

Se comprueba de dos formas:

- estática: ningún ``importorskip`` de las pruebas de render apunta al paquete del
  framework (ni a ``tests``), ni usa un nombre que no se pueda resolver;
- dinámica: en un pytest aparte, con un plugin temporal fuera del repo que oculta
  ``config.roles``, la prueba del parser falla con ``ModuleNotFoundError`` (al recoger el
  archivo o al ejecutarla) y no se omite.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from aucove import _meta
from tests.conftest import REPO_ROOT

# Pruebas de render que leen roles.yaml.
RENDER_TESTS: tuple[str, ...] = (
    "tests/render/test_codeowners.py",
    "tests/render/test_task_form.py",
    "tests/render/test_template_render.py",
    "tests/render/test_manifest_spec.py",
    "tests/render/test_render_template_tool.py",
)

TOOL_TEST = "tests/render/test_render_template_tool.py"
# Subcadena de ``-k`` que elige la prueba del parser, con o sin el sufijo ``_when_available``.
PARSER_TEST_KEY = "sha_matches_the_roles_parser"

# Paquetes de primer nivel que son del propio repositorio: nunca son opcionales.
INTERNAL_TOPS = frozenset({_meta.NAME, "tests"})
ROLES_MODULE = f"{_meta.NAME}.config.roles"

PLUGIN_NAME = "hide_config_roles"
PLUGIN_SOURCE = f'''\
"""Plugin temporal de la prueba: oculta ``{ROLES_MODULE}``."""
import sys

_NAME = {ROLES_MODULE!r}
sys.modules[_NAME] = None  # ``import`` lanza ModuleNotFoundError
_parent, _, _child = _NAME.rpartition(".")
_package = sys.modules.get(_parent)
if _package is not None and hasattr(_package, _child):
    delattr(_package, _child)  # ``from paquete import hijo`` no debe ver el atributo
'''


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
        f'pytest.importorskip("{ROLES_MODULE}")\n'
        'pytest.importorskip(f"{_meta.NAME}.config.roles")\n'
        'pytest.importorskip("tests.conftest")\n'
        "pytest.importorskip(some_name)\n"
        'pytest.importorskip("packaging.specifiers")\n'
        'importorskip("jsonschema")\n'
    )
    assert [line for line, _ in internal_importorskips(code)] == [3, 4, 5, 6]


@pytest.mark.parametrize("rel", RENDER_TESTS)
def test_render_tests_do_not_skip_on_internal_modules(rel: str) -> None:
    source = (REPO_ROOT / rel).read_text(encoding="utf-8")
    assert internal_importorskips(source) == [], (
        f"{rel}: importorskip de un módulo del framework convierte una regresión en skip"
    )


def _pytest(args: list[str], plugin_dir: Path | None) -> subprocess.CompletedProcess[str]:
    """Corre pytest aparte en la raíz del repo, sin red ni caché, con el plugin si se pide."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTEST_")}
    if plugin_dir is not None:
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(plugin_dir), env.get("PYTHONPATH")]))
        args = ["-p", PLUGIN_NAME, *args]
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        check=False,
    )


def test_parser_test_is_selected_once() -> None:
    """Sin el plugin, ``-k`` elige exactamente la prueba del parser (control del caso dinámico)."""
    proc = _pytest([TOOL_TEST, "-k", PARSER_TEST_KEY, "--collect-only", "-q"], None)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    selected = [line for line in proc.stdout.splitlines() if line.startswith(f"{TOOL_TEST}::")]
    assert len(selected) == 1, proc.stdout
    assert PARSER_TEST_KEY in selected[0]


def test_parser_test_fails_when_config_roles_is_missing(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "plugin"  # fuera del repo
    plugin_dir.mkdir()
    (plugin_dir / f"{PLUGIN_NAME}.py").write_text(PLUGIN_SOURCE, encoding="utf-8", newline="\n")
    report = tmp_path / "report.xml"
    proc = _pytest([TOOL_TEST, "-k", PARSER_TEST_KEY, "-q", "-rA", f"--junitxml={report}"], plugin_dir)
    output = proc.stdout + proc.stderr

    cases = ET.parse(report).getroot().iter("testcase")
    skipped = failed = 0
    details: list[str] = []
    for case in cases:
        for child in case:
            if child.tag == "skipped":
                skipped += 1
            elif child.tag in {"failure", "error"}:
                failed += 1
                details.append(f"{child.get('message') or ''}\n{child.text or ''}")

    assert skipped == 0, f"la prueba del parser se omite en vez de fallar:\n{output}"
    assert failed >= 1 and proc.returncode not in (0, 5), f"no detectó que falta config.roles:\n{output}"
    text = "\n".join(details)
    assert "ModuleNotFoundError" in text, output
    assert ROLES_MODULE in text, output
