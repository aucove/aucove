# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""REQ-ARCH-19: el nombre literal solo aparece en ``_meta.py`` (D-03).

Revisa por AST los literales de cadena y de bytes (incluidas las partes fijas de
los f-strings) de cada ``.py`` de ``src/``, salvo ``_meta.py``. Los catálogos
(``.toml``) y los datos no son código y quedan fuera. Los comentarios tampoco
cuentan, y los docstrings se tratan como ellos: son documentación, no valores.
"""

from __future__ import annotations

import ast

from aucove import _meta
from tests.conftest import REPO_ROOT

SRC = REPO_ROOT / "src"


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """ids de las expresiones-cadena sueltas (docstrings y cadenas sin uso)."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        for stmt in body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str):
                ids.add(id(stmt.value))
    return ids


def offending_literals(source: str, name: str) -> list[tuple[int, str]]:
    """Literales de ``source`` que contienen ``name`` (sin distinguir mayúsculas)."""
    tree = ast.parse(source)
    skip = _docstring_nodes(tree)
    needle = name.lower()
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or id(node) in skip:
            continue
        value = node.value
        if isinstance(value, bytes):
            value = value.decode("latin-1")
        if isinstance(value, str) and needle in value.lower():
            found.append((node.lineno, value))
    return found


def test_detector_catches_literals_and_fstrings() -> None:
    name = _meta.NAME
    code = (
        f'"""Docstring con {name}."""\n'
        f'A = "{name}.result/v1"\n'
        f'B = f"{{x}}-{name.upper()}_LANG"\n'
        f'C = b"{name}"\n'
        "D = NAME + '-template'\n"
    )
    lines = sorted(line for line, _ in offending_literals(code, name))
    assert lines == [2, 3, 4]


def test_name_only_in_meta() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "_meta.py" and path.parent.name == _meta.NAME:
            continue
        for line, value in offending_literals(path.read_text(encoding="utf-8"), _meta.NAME):
            offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{line}: {value[:60]!r}")
    assert offenders == [], "\n".join(offenders)


def test_package_directory_is_the_name() -> None:
    assert (SRC / _meta.NAME / "_meta.py").is_file()
    assert [p.name for p in SRC.iterdir() if p.is_dir() and (p / "__init__.py").is_file()] == [_meta.NAME]
