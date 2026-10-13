# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Un candado local no queda huérfano si alguien lo lee al soltarlo (05 §6.4).

En Windows, ``unlink`` falla con ``PermissionError`` (winerror 32) mientras otro proceso o
hilo tiene abierto el archivo, y eso pasa cuando quien espera lo lee en ``_clear_stale``.
``local_locks`` silenciaba el error: el candado quedaba con el pid de un proceso vivo y las
operaciones siguientes devolvían ``E_STATE_BUSY`` hasta que pasaran 15 min (REQ-OPS-29).
"""

from __future__ import annotations

import dataclasses
import sys
import threading
import types
from collections.abc import Iterator
from pathlib import Path

import pytest

from aucove import _meta
from aucove.ops import context as context_mod
from aucove.ops.context import Invocation, OpContext
from aucove.ops.registry import REGISTRY, invoke
from aucove.ops.result import Result, ok

MODULE = "ops._test_lock_release_op"
SYNC = next(s for s in REGISTRY if s.id == "sync-roles")
SPEC = dataclasses.replace(SYNC, module=MODULE)


def _lock_path(root: Path) -> Path:
    return root / _meta.CACHE_DIR / "locks" / "repo.lock"


class _Op:
    """Operación de prueba con el candado ``repo``; ``during`` corre mientras lo tiene."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.during: list = []

    def run(self, ctx: OpContext, **kwargs: object) -> Result:
        for action in self.during:
            action()
        return ok("sync-roles", {"check_only": False, "changed": False, "files": [], "diff": "",
                                 "unknown_logins": []})


@pytest.fixture
def op(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[_Op]:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "project.yaml").write_text("convention: '0.1'\n", encoding="utf-8")
    (root / ".git").mkdir()
    impl = _Op(root)
    module = types.ModuleType(f"{_meta.NAME}.{MODULE}")
    module.run = impl.run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(context_mod, "LOCK_WAIT_S", 0.5)
    yield impl


def _invoke(root: Path) -> Result:
    inv = Invocation(root=str(root), lang="en", json=True, dry_run=False, surface="cli", env={}, cwd=str(root))
    return invoke(SPEC, OpContext(inv), check=False)


def test_reader_holding_the_lock_at_release_does_not_orphan_it(op: _Op) -> None:
    """Caso real: un lector tiene el candado abierto justo cuando la operación lo suelta."""
    readers: list[threading.Thread] = []

    def open_for_a_moment() -> None:
        fh = open(_lock_path(op.root), "rb")  # como ``path.read_bytes()`` de quien espera
        closer = threading.Timer(0.3, fh.close)
        closer.start()
        readers.append(closer)

    op.during.append(open_for_a_moment)
    assert _invoke(op.root).ok
    for reader in readers:
        reader.join(timeout=10)
    assert not _lock_path(op.root).exists()
    op.during.clear()
    second = _invoke(op.root)
    assert second.ok, second.to_dict()  # sin candado huérfano no hay E_STATE_BUSY


def test_transient_permission_error_on_unlink_is_retried(op: _Op, monkeypatch: pytest.MonkeyPatch) -> None:
    """Lo mismo en cualquier sistema: ``unlink`` del candado falla dos veces y luego funciona."""
    original = Path.unlink
    failures = {"left": 2}

    def flaky_unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name == "repo.lock" and failures["left"] > 0:
            failures["left"] -= 1
            raise PermissionError(13, "El proceso no tiene acceso al archivo", str(self))
        original(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    assert _invoke(op.root).ok
    assert failures["left"] == 0
    assert not _lock_path(op.root).exists()
    second = _invoke(op.root)
    assert second.ok, second.to_dict()


def test_lock_replaced_by_someone_else_is_not_removed(op: _Op) -> None:
    """Al soltar, solo se borra el candado si sigue siendo el propio (no cambia con el arreglo)."""
    foreign = b'{"pid":1,"host":"otra-maquina.invalid","op":"sync-roles","started_at":"2026-01-01T00:00:00.000000Z"}\n'

    def replace_lock() -> None:
        _lock_path(op.root).write_bytes(foreign)

    op.during.append(replace_lock)
    assert _invoke(op.root).ok
    assert _lock_path(op.root).read_bytes() == foreign
