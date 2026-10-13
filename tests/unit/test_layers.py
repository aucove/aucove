# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Contratos de importación (01 §3.1, §3.5; REQ-ARCH-15, REQ-ARCH-18, REQ-ARCH-25)."""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from aucove import _meta
from tests.conftest import REPO_ROOT

SRC = REPO_ROOT / "src"
PKG_DIR = SRC / _meta.NAME
LAZY_FORGE = (f"{_meta.NAME}.forge.fake", f"{_meta.NAME}.forge.github")


@dataclass(frozen=True)
class ImportInfo:
    line: int
    modules: tuple[str, ...]  # módulos importados
    candidates: tuple[str, ...]  # módulos y posibles submódulos (from x import y -> x.y)
    in_function: bool
    type_checking: bool


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _is_package(module: str) -> bool:
    return (SRC.joinpath(*module.split(".")) / "__init__.py").is_file()


def resolve_from(module: str, node: ast.ImportFrom) -> str:
    """Módulo absoluto de un ``from ... import``, relativo a ``module`` si hace falta."""
    if not node.level:
        return node.module or ""
    package = module if _is_package(module) else module.rpartition(".")[0]
    parts = package.split(".")
    if node.level > 1:
        parts = parts[: len(parts) - (node.level - 1)]
    prefix = ".".join(parts)
    return f"{prefix}.{node.module}" if node.module else prefix


def imports_of(path: Path) -> list[ImportInfo]:
    module = _module_name(path)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[ImportInfo] = []

    def visit(node: ast.AST, in_function: bool, type_checking: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Import):
                names = tuple(a.name for a in child.names)
                out.append(ImportInfo(child.lineno, names, names, in_function, type_checking))
            elif isinstance(child, ast.ImportFrom):
                target = resolve_from(module, child)
                subs = tuple(f"{target}.{a.name}" for a in child.names)
                modules = (target,) if child.module else subs
                out.append(ImportInfo(child.lineno, modules, (target, *subs), in_function, type_checking))
            inside = in_function or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            guarded = type_checking or (
                isinstance(child, ast.If) and isinstance(child.test, ast.Name) and child.test.id == "TYPE_CHECKING"
            )
            visit(child, inside, guarded)

    visit(tree, False, False)
    return out


def _hits_lazy_forge(info: ImportInfo) -> bool:
    return any(c == lazy or c.startswith(lazy + ".") for c in info.candidates for lazy in LAZY_FORGE)


def test_resolver_handles_relative_imports() -> None:
    node = ast.parse("from ..forge.fake import FakeForge").body[0]
    assert isinstance(node, ast.ImportFrom)
    assert resolve_from(f"{_meta.NAME}.ops.context", node) == f"{_meta.NAME}.forge.fake"
    pkg_node = ast.parse("from .. import _meta").body[0]
    assert isinstance(pkg_node, ast.ImportFrom)
    assert resolve_from(f"{_meta.NAME}.i18n", pkg_node) == _meta.NAME
    info = imports_of(PKG_DIR / "i18n" / "__init__.py")
    assert any(f"{_meta.NAME}._meta" in i.modules for i in info)


def test_context_imports_concrete_forges_only_inside_functions() -> None:
    """REQ-ARCH-15: ops.context elige forge.fake y forge.github con import dentro de una función."""
    found = [i for i in imports_of(PKG_DIR / "ops" / "context.py") if _hits_lazy_forge(i)]
    assert {c for i in found for c in i.candidates} >= set(LAZY_FORGE)
    assert all(i.in_function for i in found), [(i.line, i.in_function) for i in found]


def test_no_other_ops_module_imports_concrete_forges() -> None:
    offenders = []
    for path in sorted((PKG_DIR / "ops").rglob("*.py")):
        if path == PKG_DIR / "ops" / "context.py":
            continue
        offenders += [f"{path.name}:{i.line}" for i in imports_of(path) if _hits_lazy_forge(i)]
    assert offenders == []


def _stdlib_only(package: str, allowed_internal: tuple[str, ...]) -> list[str]:
    offenders = []
    for path in sorted((PKG_DIR / package).rglob("*.py")):
        for info in imports_of(path):
            if info.type_checking:
                continue  # solo anotaciones; no se ejecuta
            for target in info.modules:
                top = target.split(".")[0]
                if top == _meta.NAME:
                    ok = any(target == a or target.startswith(a + ".") for a in allowed_internal)
                else:
                    ok = top in sys.stdlib_module_names or top == "__future__"
                if not ok:
                    offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{info.line}: {target}")
    return offenders


def test_domain_is_stdlib_only() -> None:
    """REQ-ARCH-18 y 01 §3.1: domain solo usa la biblioteca estándar (y el propio domain)."""
    assert _stdlib_only("domain", (f"{_meta.NAME}.domain", f"{_meta.NAME}._meta")) == []


def test_i18n_is_stdlib_only() -> None:
    assert _stdlib_only("i18n", (f"{_meta.NAME}.i18n", f"{_meta.NAME}._meta")) == []


def _lint_imports() -> str | None:
    scripts = Path(sys.executable).parent
    for name in ("lint-imports.exe", "lint-imports"):
        if (scripts / name).is_file():
            return str(scripts / name)
    return shutil.which("lint-imports")


def test_lint_imports_contracts_pass() -> None:
    """REQ-ARCH-25: los contratos layers y forbidden de pyproject.toml se cumplen."""
    exe = _lint_imports()
    if exe is None:
        pytest.skip("import-linter no está instalado")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(SRC), env.get("PYTHONPATH", "")) if p)
    proc = subprocess.run([exe, "--no-cache"], cwd=REPO_ROOT, env=env, capture_output=True, timeout=300)
    output = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    assert proc.returncode == 0, output
