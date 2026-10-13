# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``local_locks`` respondía ``E_STATE_BUSY`` sin que nadie tuviera el candado.

En Windows, varias invocaciones que crean a la vez ``.aucove/.gitignore`` chocan: abrir el
archivo mientras otra lo reemplaza da ``PermissionError``, y el ``os.replace`` propio puede
rendirse tras sus 3 reintentos de 10 ms (01 REQ-ARCH-57). ``ensure_root`` devolvía ``False``
aunque el archivo quedara con ``*``, y ``local_locks`` lo traducía a ``E_STATE_BUSY`` con
``{lock: repo, reason: unavailable}`` (05 §6.4, REQ-OPS-28). Con 3 invocaciones simultáneas
durante 80 iteraciones salían 6 errores así. Ahora ``ensure_root`` vuelve a leer el archivo
con reintentos cortos y solo devuelve ``False`` si al final falta o difiere.
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
from aucove.localstore import store as store_mod
from aucove.ops.context import Invocation, OpContext
from aucove.ops.registry import REGISTRY, invoke
from aucove.ops.result import Result, ok

MODULE = "ops._test_lock_op"
SPEC = dataclasses.replace(next(s for s in REGISTRY if s.id == "sync-roles"), module=MODULE)  # candado repo


def _run(ctx: OpContext, **kwargs: object) -> Result:
    return ok("sync-roles", {"check_only": False, "changed": False, "files": [], "diff": "", "unknown_logins": []})


def _checkout(base: Path) -> Path:
    root = base / "proj"
    root.mkdir(parents=True)
    (root / "project.yaml").write_text("convention: '0.1'\n", encoding="utf-8")
    (root / ".git").mkdir()
    return root


@pytest.fixture
def project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    module = types.ModuleType(f"{_meta.NAME}.{MODULE}")
    module.run = _run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    yield _checkout(tmp_path)


def _invoke(root: Path) -> Result:
    inv = Invocation(root=str(root), lang="en", json=True, dry_run=False, surface="cli", env={}, cwd=str(root))
    return invoke(SPEC, OpContext(inv), check=False)


def _gitignore(root: Path) -> Path:
    return root / _meta.CACHE_DIR / ".gitignore"


def test_busy_gitignore_does_not_make_the_lock_unavailable(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (project / _meta.CACHE_DIR).mkdir()
    _gitignore(project).write_bytes(store_mod.GITIGNORE)
    real = Path.read_bytes
    busy: list[Path] = []

    def read_bytes(path: Path) -> bytes:
        if path.name == ".gitignore" and not busy:
            busy.append(path)  # la primera lectura coincide con el os.replace de otra invocación
            raise PermissionError(13, "el archivo está en uso por otro proceso", str(path))
        return real(path)

    monkeypatch.setattr(store_mod.Path, "read_bytes", read_bytes)
    result = _invoke(project)
    assert result.ok, result.to_dict()
    assert busy == [_gitignore(project)]
    assert not (project / _meta.CACHE_DIR / "locks" / "repo.lock").exists()


def test_lost_replace_with_right_gitignore_does_not_make_the_lock_unavailable(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = store_mod.atomic_write

    def atomic_write(path: Path, payload: bytes) -> bool:
        if path.name == ".gitignore":
            real(path, payload)  # otra invocación lo dejó bien
            return False  # y el os.replace propio se rindió tras sus reintentos
        return real(path, payload)

    monkeypatch.setattr(store_mod, "atomic_write", atomic_write)
    result = _invoke(project)
    assert result.ok, result.to_dict()
    assert _gitignore(project).read_bytes() == store_mod.GITIGNORE


def test_cache_that_cannot_be_prepared_is_still_unavailable(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store_mod, "atomic_write", lambda path, payload: False)
    result = _invoke(project)
    assert result.error is not None and result.error.code == "E_STATE_BUSY"
    assert result.error.details == {"lock": "repo", "reason": "unavailable"}
    assert not _gitignore(project).exists()


def test_simultaneous_invocations_on_fresh_checkouts(project: Path, tmp_path: Path) -> None:
    errors: list[dict[str, object]] = []
    for i in range(30):
        root = _checkout(tmp_path / f"round{i}")
        results: list[Result] = []
        barrier = threading.Barrier(3)

        def go(root: Path = root, results: list[Result] = results, barrier: threading.Barrier = barrier) -> None:
            barrier.wait()
            results.append(_invoke(root))

        workers = [threading.Thread(target=go) for _ in range(3)]
        for t in workers:
            t.start()
        for t in workers:
            t.join(timeout=60)
        assert len(results) == 3
        errors.extend(r.to_dict() for r in results if not r.ok)
        assert _gitignore(root).read_bytes() == store_mod.GITIGNORE
    assert errors == []
