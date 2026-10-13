# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``hook`` nunca sale con 1 ni deja escapar una excepción (07-hooks §3.4).

En Claude Code, la salida 1 de un PreToolUse es un error que no bloquea: la acción pasa.
REQ-HOOK-13 exige salir solo con 0 o 2 y traducir toda excepción según la tabla de §3.4:
``pre-tool-use`` bloquea (2, REQ-HOOK-5), ``post-tool-use(-failure)`` retroalimenta (2,
«integridad no verificada») y los demás eventos degradan a 0 (REQ-HOOK-5, REQ-HOOK-24;
ConfigChange falla abierto). Un ``<evento>`` desconocido sale con 0 y sin salida (REQ-HOOK-9),
salvo en una versión sin módulo de hooks, donde se conserva su señal fija (2 y «no disponible»).
Antes, un error de importación que no fuera ``ImportError`` o un ``SystemExit`` dentro de
``run`` salían de ``entry.main`` sin capturar (código 1).
"""

from __future__ import annotations

import importlib.abc
import sys
import types
from collections.abc import Callable

import pytest

from aucove import _meta, entry

PKG = _meta.NAME
HOOKS = f"{PKG}.hooks"
HOOKS_MAIN = f"{PKG}.hooks.main"

FAIL_CLOSED = ["pre-tool-use", "post-tool-use", "post-tool-use-failure"]
DEGRADE = ["session-start", "post-model-switch", "stop", "session-end", "config-change", "refresh"]


class _RaisingFinder(importlib.abc.MetaPathFinder):
    """Hace que importar ``hooks`` o ``hooks.main`` lance ``exc`` (no un ImportError)."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def find_spec(self, fullname: str, path: object, target: object = None) -> None:
        if fullname in (HOOKS, HOOKS_MAIN):
            raise self.exc
        return None


@pytest.fixture
def broken_import(monkeypatch: pytest.MonkeyPatch) -> Callable[[BaseException], None]:
    def install(exc: BaseException) -> None:
        for name in (HOOKS, HOOKS_MAIN):
            monkeypatch.delitem(sys.modules, name, raising=False)
        monkeypatch.setattr(sys, "meta_path", [_RaisingFinder(exc), *sys.meta_path])

    return install


def _fake_run(monkeypatch: pytest.MonkeyPatch, run: object) -> None:
    module = types.ModuleType(HOOKS_MAIN)
    module.run = run  # type: ignore[attr-defined]
    parent = types.ModuleType(HOOKS)
    parent.main = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, HOOKS, parent)
    monkeypatch.setitem(sys.modules, HOOKS_MAIN, module)


IMPORT_ERRORS = [RuntimeError("secreto"), SyntaxError("secreto"), OSError("secreto"), ValueError("secreto")]


@pytest.mark.parametrize("exc", IMPORT_ERRORS, ids=lambda e: type(e).__name__)
@pytest.mark.parametrize("event", FAIL_CLOSED)
def test_import_failure_blocks_where_it_must(
    broken_import: Callable[[BaseException], None], capsys: pytest.CaptureFixture[str], event: str, exc: BaseException
) -> None:
    broken_import(exc)
    assert entry.main(["hook", event]) == 2
    out = capsys.readouterr()
    assert out.out == "" and out.err.strip() and "secreto" not in out.err


@pytest.mark.parametrize("exc", IMPORT_ERRORS, ids=lambda e: type(e).__name__)
@pytest.mark.parametrize("event", DEGRADE)
def test_import_failure_degrades_in_non_blocking_events(
    broken_import: Callable[[BaseException], None], capsys: pytest.CaptureFixture[str], event: str, exc: BaseException
) -> None:
    broken_import(exc)
    assert entry.main(["hook", event]) == 0
    out = capsys.readouterr()
    assert out.out == "" and "secreto" not in out.err


@pytest.mark.parametrize("args", [["no-such-event"], [], ["--door", "claude-code"]])
def test_unknown_event_exits_0_without_output(
    broken_import: Callable[[BaseException], None], capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    broken_import(RuntimeError("secreto"))
    assert entry.main(["hook", *args]) == 0
    out = capsys.readouterr()
    assert (out.out, out.err) == ("", "")


@pytest.mark.parametrize("event", DEGRADE)
def test_missing_module_degrades_in_non_blocking_events(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], event: str
) -> None:
    for name in (HOOKS, HOOKS_MAIN):
        monkeypatch.setitem(sys.modules, name, None)  # ModuleNotFoundError: versión sin hooks
    assert entry.main(["hook", event]) == 0
    assert "not available in this version" in capsys.readouterr().err


@pytest.mark.parametrize("args", [[], ["no-such-event"]])
def test_missing_module_keeps_the_c02_probe_signal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    # La sonda de T4 (tests/hooks/runner.py) distingue así una versión sin hooks.
    for name in (HOOKS, HOOKS_MAIN):
        monkeypatch.setitem(sys.modules, name, None)
    assert entry.main(["hook", *args]) == 2
    assert "not available in this version" in capsys.readouterr().err


RUN_FAILURES: list[BaseException] = [SystemExit(1), SystemExit(0), SystemExit(None), KeyboardInterrupt(), RuntimeError("secreto")]


@pytest.mark.parametrize("exc", RUN_FAILURES, ids=lambda e: f"{type(e).__name__}({getattr(e, 'code', '')})")
def test_run_failure_in_pre_tool_use_blocks(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], exc: BaseException
) -> None:
    def run(args: list[str]) -> int:
        raise exc

    _fake_run(monkeypatch, run)
    assert entry.main(["hook", "pre-tool-use"]) == 2
    assert "secreto" not in capsys.readouterr().err


@pytest.mark.parametrize("exc", RUN_FAILURES, ids=lambda e: f"{type(e).__name__}({getattr(e, 'code', '')})")
def test_run_failure_in_stop_degrades(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    def run(args: list[str]) -> int:
        raise exc

    _fake_run(monkeypatch, run)
    assert entry.main(["hook", "stop"]) == 0  # REQ-HOOK-24: ante cualquier error, 0


@pytest.mark.parametrize("value", [1, None, True, False, "2", 3, -1, 2.0])
def test_run_return_value_is_normalized(monkeypatch: pytest.MonkeyPatch, value: object) -> None:
    _fake_run(monkeypatch, lambda args: value)
    assert entry.main(["hook", "pre-tool-use"]) == 2
    assert entry.main(["hook", "post-tool-use"]) == 2
    assert entry.main(["hook", "session-start"]) == 0


@pytest.mark.parametrize("value", [0, 2])
@pytest.mark.parametrize("event", ["pre-tool-use", "session-start", "post-tool-use", "stop"])
def test_valid_return_values_pass_through(monkeypatch: pytest.MonkeyPatch, event: str, value: int) -> None:
    seen: list[list[str]] = []

    def run(args: list[str]) -> int:
        seen.append(list(args))
        return value

    _fake_run(monkeypatch, run)
    assert entry.main(["hook", event, "--door", "claude-code"]) == value
    assert seen == [[event, "--door", "claude-code"]]
