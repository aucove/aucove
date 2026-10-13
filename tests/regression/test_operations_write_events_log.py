# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Cada operación queda en ``.aucove/log/events.jsonl`` (01 §11).

``obs.events.emit`` existía, pero nada lo llamaba: ni ``registry.invoke`` ni ``internal()``.
El ``hint`` de ``E_INTERNAL_UNEXPECTED`` pide reportar el ``trace_id`` «con el registro
``.aucove/log/events.jsonl``», que nunca recibía nada (REQ-ARCH-90). Un fallo al registrar
nunca cambia el resultado (REQ-ARCH-92) y el registro no lleva texto de excepciones (§11).
"""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from aucove import _meta
from aucove.obs.events import FIELDS
from aucove.ops.context import Invocation, OpContext
from aucove.ops.registry import OpSpec, invoke
from aucove.ops.result import OpError, Result, ok

MODULE = "ops._test_logged_op"
SPEC = OpSpec(id="doctor", cli=("doctor",), mcp_tool=None, human_only=False, effects="none",
              offline="local", module=MODULE)


@pytest.fixture
def set_run(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[Callable[..., Result]], None]]:
    monkeypatch.delenv(_meta.ENV_PREFIX + "LOG", raising=False)
    module = types.ModuleType(f"{_meta.NAME}.{MODULE}")
    monkeypatch.setitem(sys.modules, module.__name__, module)

    def install(run: Callable[..., Result]) -> None:
        module.run = run  # type: ignore[attr-defined]

    yield install


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "project.yaml").write_text("convention: '0.1'\n", encoding="utf-8")
    (root / ".git").mkdir()
    return root


def _invoke(root: Path) -> Result:
    inv = Invocation(root=str(root), lang="en", json=True, dry_run=False, surface="cli", env={}, cwd=str(root))
    return invoke(SPEC, OpContext(inv))


def _events(root: Path) -> list[dict[str, object]]:
    path = root / _meta.CACHE_DIR / "log" / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_successful_operation_is_logged(set_run: Callable[[Callable[..., Result]], None], project: Path) -> None:
    set_run(lambda ctx, **kw: ok("doctor", {}))
    assert _invoke(project).ok
    (event,) = _events(project)
    assert set(event) <= FIELDS
    assert (event["kind"], event["name"], event["result"]) == ("op", "doctor", "ok")
    assert "code" not in event and isinstance(event["ms"], int) and event["ts"]


def test_internal_error_is_logged_without_its_text(set_run: Callable[[Callable[..., Result]], None], project: Path) -> None:
    def run(ctx: OpContext, **kw: object) -> Result:
        raise RuntimeError("ghp_secreto en https://x.invalid/?token=1")

    set_run(run)
    result = _invoke(project)
    assert result.error is not None and result.error.code == "E_INTERNAL_UNEXPECTED"
    assert result.error.details is not None and result.error.details["trace_id"]
    (event,) = _events(project)
    assert (event["kind"], event["name"], event["result"], event["code"]) == ("op", "doctor", "error", "E_INTERNAL_UNEXPECTED")
    raw = (project / _meta.CACHE_DIR / "log" / "events.jsonl").read_text(encoding="utf-8")
    assert "secreto" not in raw and "token" not in raw


def test_op_error_code_is_logged(set_run: Callable[[Callable[..., Result]], None], project: Path) -> None:
    def run(ctx: OpContext, **kw: object) -> Result:
        raise OpError("E_STATE_BUSY")

    set_run(run)
    assert _invoke(project).error.code == "E_STATE_BUSY"  # type: ignore[union-attr]
    (event,) = _events(project)
    assert (event["result"], event["code"]) == ("error", "E_STATE_BUSY")


def test_logging_failure_does_not_change_the_result(set_run: Callable[[Callable[..., Result]], None], project: Path) -> None:
    (project / _meta.CACHE_DIR).write_text("no soy una carpeta\n", encoding="utf-8")
    set_run(lambda ctx, **kw: ok("doctor", {"x": 1}))
    result = _invoke(project)
    assert result.ok and result.data == {"x": 1}


def test_outside_a_project_nothing_is_written(set_run: Callable[[Callable[..., Result]], None], tmp_path: Path) -> None:
    set_run(lambda ctx, **kw: ok("doctor", {}))
    other = tmp_path / "otro"
    other.mkdir()
    assert _invoke(other).ok
    assert list(other.iterdir()) == []


def test_log_off_writes_nothing(
    set_run: Callable[[Callable[..., Result]], None], project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_meta.ENV_PREFIX + "LOG", "off")
    set_run(lambda ctx, **kw: ok("doctor", {}))
    assert _invoke(project).ok
    assert _events(project) == []
