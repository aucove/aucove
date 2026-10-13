# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Punto de entrada y despacho (01 §3.3; REQ-OPS-8, REQ-OPS-14, REQ-OPS-16)."""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from aucove import _meta, entry
from tests.conftest import REPO_ROOT

PKG = _meta.NAME


def _block(monkeypatch: pytest.MonkeyPatch, *modules: str) -> None:
    """Hace que importar ``modules`` dé ModuleNotFoundError (como si no existieran)."""
    for name in modules:
        monkeypatch.setitem(sys.modules, name, None)


def _fake_module(monkeypatch: pytest.MonkeyPatch, name: str, run: object) -> None:
    parent_name, _, child = name.rpartition(".")
    module = types.ModuleType(name)
    module.run = run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, module)
    if parent_name not in sys.modules:
        monkeypatch.setitem(sys.modules, parent_name, types.ModuleType(parent_name))
    monkeypatch.setattr(sys.modules[parent_name], child, module, raising=False)


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    assert entry.main(["--version"]) == 0
    out = capsys.readouterr()
    assert out.out == f"{_meta.__version__}\n"
    assert out.err == ""


def test_hook_without_module_fails_closed(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _block(monkeypatch, f"{PKG}.hooks", f"{PKG}.hooks.main")
    assert entry.main(["hook", "pre-tool-use"]) == 2
    err = capsys.readouterr().err
    assert "no disponible" in err and "not available" in err  # bilingüe
    assert "bloquea" in err and "blocked" in err


def test_hook_error_fails_closed(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def boom(args: list[str]) -> int:
        raise RuntimeError("secreto")

    _fake_module(monkeypatch, f"{PKG}.hooks.main", boom)
    assert entry.main(["hook", "pre-tool-use"]) == 2
    err = capsys.readouterr().err
    assert "secreto" not in err and "blocked" in err


def test_mcp_without_module_exits_1(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _block(monkeypatch, f"{PKG}.mcp", f"{PKG}.mcp.server")
    assert entry.main(["mcp", "--stdio"]) == 1
    err = capsys.readouterr().err
    assert "no disponible" in err and "not available" in err


def test_ci_and_cli_without_module_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    _block(monkeypatch, f"{PKG}.cli.ci", f"{PKG}.cli.main")
    assert entry.main(["ci", "build-plan"]) == 1
    assert entry.main(["doctor"]) == 1
    assert entry.main([]) == 1


def test_dispatch_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, list[str]] = {}

    def recorder(key: str) -> object:
        def run(args: list[str]) -> int:
            seen[key] = list(args)
            return 0

        return run

    _fake_module(monkeypatch, f"{PKG}.hooks.main", recorder("hook"))
    _fake_module(monkeypatch, f"{PKG}.mcp.server", recorder("mcp"))
    _fake_module(monkeypatch, f"{PKG}.cli.ci", recorder("ci"))
    _fake_module(monkeypatch, f"{PKG}.cli.main", recorder("cli"))
    assert entry.main(["hook", "session-start", "x"]) == 0
    assert entry.main(["mcp", "--stdio"]) == 0
    assert entry.main(["ci", "pin-plan", "--policy", "."]) == 0
    assert entry.main(["validate", "--json"]) == 0
    assert seen == {
        "hook": ["session-start", "x"],
        "mcp": ["--stdio"],
        "ci": ["pin-plan", "--policy", "."],
        "cli": ["validate", "--json"],
    }


def test_entry_top_level_imports_only_stdlib() -> None:
    tree = ast.parse((REPO_ROOT / "src" / PKG / "entry.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in sys.stdlib_module_names, alias.name
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # Solo _meta, que no importa nada (01 §3.1).
                assert node.module is None and [a.name for a in node.names] == ["_meta"]
            else:
                assert (node.module or "").split(".")[0] in sys.stdlib_module_names or node.module == "__future__"


def _launcher() -> Path | None:
    scripts = Path(sys.executable).parent
    for name in (PKG + ".exe", PKG):
        candidate = scripts / name
        if candidate.is_file():
            return candidate
    found = shutil.which(PKG)
    return Path(found) if found else None


def test_installed_launcher_prints_version() -> None:
    launcher = _launcher()
    if launcher is None:
        pytest.skip("el paquete no está instalado (pip install --no-deps -e .)")
    proc = subprocess.run([str(launcher), "--version"], capture_output=True, timeout=60)
    assert proc.returncode == 0
    assert proc.stdout.replace(b"\r\n", b"\n") == f"{_meta.__version__}\n".encode()
