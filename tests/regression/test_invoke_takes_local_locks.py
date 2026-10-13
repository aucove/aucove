# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``invoke`` toma los candados locales que declara la operación (05 §6.4).

``sync-roles`` declaraba ``locks=('repo',)``, pero nadie creaba ``.aucove/locks/repo.lock``:
dos ``sync-roles`` simultáneos no se esperaban ni devolvían ``E_STATE_BUSY`` (REQ-OPS-28,
REQ-OPS-29). El candado se crea con ``O_CREAT|O_EXCL`` y guarda ``{pid, host, op,
started_at}``; la espera máxima es de 10 s, y uno de un proceso muerto en el mismo host o con
más de 15 min se reemplaza.
"""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import subprocess
import sys
import threading
import time
import types
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aucove import _meta
from aucove.ops import context as context_mod
from aucove.ops.context import Invocation, OpContext
from aucove.ops.registry import REGISTRY, invoke
from aucove.ops.result import Result, ok

MODULE = "ops._test_lock_op"
SYNC = next(s for s in REGISTRY if s.id == "sync-roles")
SPEC = dataclasses.replace(SYNC, module=MODULE)


class _Op:
    def __init__(self) -> None:
        self.seen_locks: list[dict[str, object] | None] = []
        self.intervals: list[tuple[float, float]] = []
        self.sleep = 0.0
        self.root: Path | None = None

    def run(self, ctx: OpContext, **kwargs: object) -> Result:
        start = time.monotonic()
        assert self.root is not None
        lock = self.root / _meta.CACHE_DIR / "locks" / "repo.lock"
        self.seen_locks.append(json.loads(lock.read_text(encoding="utf-8")) if lock.exists() else None)
        if self.sleep:
            time.sleep(self.sleep)
        self.intervals.append((start, time.monotonic()))
        return ok("sync-roles", {"check_only": bool(kwargs.get("check")), "changed": False, "files": [],
                                 "diff": "", "unknown_logins": []})


@pytest.fixture
def op(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[_Op]:
    impl = _Op()
    module = types.ModuleType(f"{_meta.NAME}.{MODULE}")
    module.run = impl.run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    root = tmp_path / "proj"
    root.mkdir()
    (root / "project.yaml").write_text("convention: '0.1'\n", encoding="utf-8")
    (root / ".git").mkdir()
    impl.root = root
    monkeypatch.setattr(context_mod, "LOCK_WAIT_S", 0.5, raising=False)
    yield impl


def _invoke(root: Path, **kwargs: object) -> Result:
    inv = Invocation(root=str(root), lang="en", json=True, dry_run=False, surface="cli", env={}, cwd=str(root))
    return invoke(SPEC, OpContext(inv), **kwargs)


def _lock_path(root: Path) -> Path:
    return root / _meta.CACHE_DIR / "locks" / "repo.lock"


def _hold(root: Path, *, pid: int, host: str, started: datetime) -> bytes:
    path = _lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps({"pid": pid, "host": host, "op": "sync-roles",
                          "started_at": started.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}).encode("utf-8") + b"\n"
    path.write_bytes(content)
    return content


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=60)
    return proc.pid


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_sync_roles_declares_and_takes_the_repo_lock(op: _Op) -> None:
    assert op.root is not None
    result = _invoke(op.root, check=False)
    assert result.ok, result.to_dict()
    held = op.seen_locks[0]
    assert held is not None and set(held) == {"pid", "host", "op", "started_at"}
    assert (held["pid"], held["host"], held["op"]) == (os.getpid(), socket.gethostname(), "sync-roles")
    assert not _lock_path(op.root).exists()  # se libera al terminar


def test_busy_lock_gives_e_state_busy(op: _Op) -> None:
    assert op.root is not None
    content = _hold(op.root, pid=os.getpid(), host=socket.gethostname(), started=_now())
    result = _invoke(op.root, check=False)
    assert result.error is not None and result.error.code == "E_STATE_BUSY" and result.error.retryable
    assert op.seen_locks == []
    assert _lock_path(op.root).read_bytes() == content  # el candado ajeno sigue intacto


def test_lock_of_another_host_is_respected(op: _Op) -> None:
    assert op.root is not None
    _hold(op.root, pid=_dead_pid(), host="otra-maquina.invalid", started=_now())
    assert _invoke(op.root).error.code == "E_STATE_BUSY"  # type: ignore[union-attr]


@pytest.mark.parametrize("case", ["dead-process", "older-than-15-min"])
def test_stale_lock_is_replaced(op: _Op, case: str) -> None:
    assert op.root is not None
    if case == "dead-process":
        _hold(op.root, pid=_dead_pid(), host=socket.gethostname(), started=_now())
    else:
        _hold(op.root, pid=os.getpid(), host="otra-maquina.invalid", started=_now() - timedelta(minutes=16))
    result = _invoke(op.root)
    assert result.ok, result.to_dict()
    assert op.seen_locks[0] is not None and op.seen_locks[0]["pid"] == os.getpid()
    assert not _lock_path(op.root).exists()


def test_concurrent_invocations_are_serialized(op: _Op, monkeypatch: pytest.MonkeyPatch) -> None:
    assert op.root is not None
    monkeypatch.setattr(context_mod, "LOCK_WAIT_S", 10.0, raising=False)
    op.sleep = 0.3
    results: list[Result] = []
    threads = [threading.Thread(target=lambda: results.append(_invoke(op.root))) for _ in range(2)]  # type: ignore[arg-type]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert [r.ok for r in results] == [True, True]
    (a0, a1), (b0, b1) = sorted(op.intervals)
    assert a1 <= b0  # la segunda esperó a que la primera soltara el candado


def test_check_does_not_need_the_lock(op: _Op) -> None:
    assert op.root is not None
    _hold(op.root, pid=os.getpid(), host=socket.gethostname(), started=_now())
    assert _invoke(op.root, check=True).ok  # --check no modifica el árbol (REQ-OPS-28)


def test_outside_a_project_is_not_a_project(op: _Op, tmp_path: Path) -> None:
    other = tmp_path / "otro"
    other.mkdir()
    result = _invoke(other)
    assert result.error is not None and result.error.code == "E_CONFIG_NOT_A_PROJECT"
    assert not (other / _meta.CACHE_DIR).exists()


def test_lock_order_is_repo_then_tasks_ascending() -> None:
    assert context_mod.lock_order(["task-12", "repo", "task-3", "repo"]) == ["repo", "task-3", "task-12"]
    for bad in ("../x", "task-0", "task-", "REPO", "task-1x"):
        with pytest.raises(ValueError):
            context_mod.lock_order([bad])


def test_unusable_lock_folder_fails_closed(op: _Op) -> None:
    assert op.root is not None
    cache = op.root / _meta.CACHE_DIR
    cache.mkdir()
    (cache / "locks").write_text("no soy una carpeta\n", encoding="utf-8")
    result = _invoke(op.root)
    assert result.error is not None and result.error.code == "E_STATE_BUSY"
    assert result.error.details == {"lock": "repo", "reason": "unavailable"}
    assert op.seen_locks == []


def test_pid_alive_sees_live_and_finished_processes() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert context_mod._pid_alive(proc.pid) is True
    finally:
        proc.kill()
        proc.wait(timeout=60)
    assert context_mod._pid_alive(proc.pid) is False
    assert context_mod._pid_alive(os.getpid()) is True
