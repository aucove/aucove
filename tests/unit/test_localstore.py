# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Caché local ``.aucove/`` (01 §7.3; REQ-ARCH-57, REQ-ARCH-58, REQ-ARCH-61)."""

from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aucove import _meta
from aucove.localstore import store as store_mod
from aucove.localstore.store import (
    CACHE_SCHEMA,
    GITIGNORE,
    IdentityCache,
    Store,
    TaskCache,
    atomic_write,
    ensure_root,
    main_checkout,
)
from tests.conftest import GitRepo

NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git no está disponible")


def _same(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    return root


@pytest.fixture
def store(project: Path) -> Store:
    return Store(project, now=lambda: NOW)


def _leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.rglob(".tmp-*"))


# -- ubicación (REQ-ARCH-61) ------------------------------------------------------------


def test_store_lives_in_the_checkout(project: Path, store: Store) -> None:
    assert store.root == project / _meta.CACHE_DIR
    assert store.cache_dir == project / _meta.CACHE_DIR / "cache"
    assert main_checkout(project) == project


def test_worktree_shares_the_main_checkout(tmp_path: Path) -> None:
    main = tmp_path / "main"
    wt_git = main / ".git" / "worktrees" / "wt"
    wt_git.mkdir(parents=True)
    (wt_git / "commondir").write_bytes(b"../..\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_bytes(f"gitdir: {wt_git}\n".encode())
    assert _same(main_checkout(wt), main)
    assert _same(Store(wt).root, main / _meta.CACHE_DIR)
    rel = tmp_path / "wt-rel"
    rel.mkdir()
    (rel / ".git").write_bytes(b"gitdir: ../main/.git/worktrees/wt\n")
    assert _same(main_checkout(rel), main)


def test_unusual_git_dirs_fall_back_to_project_root(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert main_checkout(plain) == plain
    sub = tmp_path / "super" / "sub"
    modules = tmp_path / "super" / ".git" / "modules" / "sub"
    modules.mkdir(parents=True)
    sub.mkdir(parents=True)
    (sub / ".git").write_bytes(b"gitdir: ../.git/modules/sub\n")
    assert _same(main_checkout(sub), sub)  # submódulo: commondir no es una carpeta .git
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_bytes(b"gitdir: ../does-not-exist\n")
    assert main_checkout(broken) == broken


@needs_git
def test_real_git_worktree(git_repo: GitRepo, tmp_path: Path) -> None:
    git_repo.commit("c1", files={"a.txt": "a\n"})
    wt = tmp_path / "linked"
    git_repo.git("worktree", "add", "-q", "-b", "wt", str(wt))
    assert _same(Store(wt).root, git_repo.path / _meta.CACHE_DIR)


# -- escritura y sobre común --------------------------------------------------------


def test_write_creates_gitignore_and_envelope(store: Store) -> None:
    store.write("tasks/17.json", {"number": 17, "título": "ñ"}, key={"repo": "o/r"})
    assert (store.root / ".gitignore").read_bytes() == b"*\n"
    raw = store.path("tasks/17.json").read_bytes()
    assert raw.endswith(b"\n") and not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw
    env = json.loads(raw.decode("utf-8"))
    assert env == {
        "cache_schema": CACHE_SCHEMA,
        "core": _meta.__version__,
        "created_at": "2026-09-30T12:00:00Z",
        "key": {"repo": "o/r"},
        "data": {"number": 17, "título": "ñ"},
    }
    assert list(env) == ["cache_schema", "core", "created_at", "key", "data"]
    assert store.read("tasks/17.json") == {"number": 17, "título": "ñ"}
    assert store.read("tasks/17.json", key={"repo": "o/r"}) == {"number": 17, "título": "ñ"}
    assert store.read("tasks/17.json", key={"repo": "o/other"}) is None
    assert store.read_envelope("tasks/17.json") == env
    assert _leftovers(store.root) == []


def test_write_replaces_and_normalizes_keys(store: Store) -> None:
    store.write("policy.json", {"v": 1}, key={"base_oid": "a" * 40, "paths": ("a", "b")})
    store.write("policy.json", {"v": 2}, key={"base_oid": "b" * 40, "paths": ("a", "b")})
    assert store.read("policy.json", key={"base_oid": "a" * 40, "paths": ("a", "b")}) is None
    assert store.read("policy.json", key={"base_oid": "b" * 40, "paths": ["a", "b"]}) == {"v": 2}
    assert _leftovers(store.root) == []


def test_gitignore_is_restored(store: Store) -> None:
    store.root.mkdir()
    (store.root / ".gitignore").write_bytes(b"!keep\n")
    store.write("identity.json", {}, key={})
    assert (store.root / ".gitignore").read_bytes() == b"*\n"


@pytest.mark.parametrize("rel", ["", "../x.json", "/abs.json", "a//b.json", "a/./b.json", "a\\b.json", "C:x.json", "a\0b"])
def test_invalid_entry_names(store: Store, rel: str) -> None:
    with pytest.raises(ValueError):
        store.write(rel, {}, key={})
    assert store.read(rel) is None


def test_non_json_data_is_a_contract_error(store: Store) -> None:
    with pytest.raises(TypeError):
        store.write("x.json", {"when": datetime.now()}, key={})
    with pytest.raises(ValueError):
        store.write("x.json", {"nan": float("nan")}, key={})


def test_filesystem_errors_are_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = Store(tmp_path / "no-such-project")
    missing.write("tasks/1.json", {"a": 1}, key={})  # sin checkout: no crea nada
    assert not (tmp_path / "no-such-project").exists()
    project = tmp_path / "p"
    project.mkdir()
    store = Store(project)

    def boom(path: Path, payload: bytes) -> bool:
        raise OSError("disco lleno")

    monkeypatch.setattr(store_mod, "atomic_write", boom)
    store.write("tasks/1.json", {"a": 1}, key={})
    assert store.read("tasks/1.json") is None


# -- REQ-ARCH-57: reintentos de os.replace en Windows --------------------------------------


class _FlakyReplace:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0
        self.real = os.replace

    def __call__(self, src: str, dst: object) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise PermissionError("en uso")
        self.real(src, dst)  # type: ignore[arg-type]


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(store_mod.time, "sleep", recorded.append)
    return recorded


def test_replace_retries_on_windows(store: Store, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    assert store.ensure()
    flaky = _FlakyReplace(failures=2)
    monkeypatch.setattr(store_mod, "_WINDOWS", True)
    monkeypatch.setattr(store_mod.os, "replace", flaky)
    store.write("identity.json", {"login": "ana"}, key={})
    assert flaky.calls == 3 and sleeps == [0.01, 0.01]
    monkeypatch.undo()
    assert store.read("identity.json") == {"login": "ana"}


def test_replace_gives_up_silently_after_three_retries(
    store: Store, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    store.write("identity.json", {"login": "old"}, key={})
    flaky = _FlakyReplace(failures=99)
    monkeypatch.setattr(store_mod, "_WINDOWS", True)
    monkeypatch.setattr(store_mod.os, "replace", flaky)
    store.write("identity.json", {"login": "new"}, key={})  # no lanza
    assert flaky.calls == 4 and sleeps == [0.01] * 3
    monkeypatch.undo()
    assert store.read("identity.json") == {"login": "old"}
    assert _leftovers(store.root) == []


def test_replace_does_not_retry_elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]) -> None:
    flaky = _FlakyReplace(failures=99)
    monkeypatch.setattr(store_mod, "_WINDOWS", False)
    monkeypatch.setattr(store_mod.os, "replace", flaky)
    assert atomic_write(tmp_path / "x.json", b"{}") is False
    assert flaky.calls == 1 and sleeps == []
    monkeypatch.undo()
    assert not (tmp_path / "x.json").exists() and _leftovers(tmp_path) == []
    assert atomic_write(tmp_path / "x.json", b"{}") is True
    assert (tmp_path / "x.json").read_bytes() == b"{}"


# -- ensure_root ante invocaciones simultáneas -----------------------------------------------
#
# En Windows, mientras otra invocación hace ``os.replace`` sobre ``.aucove/.gitignore``,
# abrir el archivo da ``PermissionError`` y el propio ``os.replace`` puede rendirse tras sus
# 3 reintentos. Ninguno de los dos casos significa que la caché no esté disponible: si el
# archivo queda con ``*``, ``ensure_root`` es ``True`` y ``local_locks`` no responde
# ``E_STATE_BUSY`` sin que nadie tenga el candado.


class _BusyReads:
    """``Path.read_bytes`` que lanza ``PermissionError`` en las primeras ``failures`` lecturas."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, failures: int) -> None:
        self.failures = failures
        self.calls = 0
        real = Path.read_bytes

        def read_bytes(path: Path) -> bytes:
            self.calls += 1
            if self.calls <= self.failures:
                raise PermissionError(13, "el archivo está en uso por otro proceso", str(path))
            return real(path)

        monkeypatch.setattr(store_mod.Path, "read_bytes", read_bytes)


def test_ensure_root_accepts_gitignore_left_by_another_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    root = tmp_path / _meta.CACHE_DIR
    writes: list[Path] = []

    def other_writer_wins(path: Path, payload: bytes) -> bool:
        writes.append(path)
        path.write_bytes(GITIGNORE)  # otra invocación lo dejó bien y nuestro os.replace se rindió
        return False

    monkeypatch.setattr(store_mod, "atomic_write", other_writer_wins)
    assert ensure_root(root) is True
    assert writes == [root / ".gitignore"]
    assert (root / ".gitignore").read_bytes() == GITIGNORE


def test_ensure_root_rereads_a_busy_gitignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    root = tmp_path / _meta.CACHE_DIR
    root.mkdir()
    (root / ".gitignore").write_bytes(GITIGNORE)
    busy = _BusyReads(monkeypatch, failures=1)
    assert ensure_root(root) is True
    assert busy.calls == 2 and sleeps == [0.01]


def test_ensure_root_restores_gitignore_after_a_busy_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    root = tmp_path / _meta.CACHE_DIR
    root.mkdir()
    (root / ".gitignore").write_bytes(b"!keep\n")
    _BusyReads(monkeypatch, failures=1)
    assert ensure_root(root) is True
    monkeypatch.undo()
    assert (root / ".gitignore").read_bytes() == GITIGNORE
    assert _leftovers(root) == []


def test_ensure_root_gives_up_when_gitignore_never_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    root = tmp_path / _meta.CACHE_DIR
    monkeypatch.setattr(store_mod, "atomic_write", lambda path, payload: False)
    assert ensure_root(root) is False  # el archivo falta: la caché no está lista
    assert sleeps == [0.01] * 10  # espera acotada: 10 relecturas de 10 ms
    sleeps.clear()
    (root / ".gitignore").write_bytes(b"!keep\n")
    assert ensure_root(root) is False  # difiere y no se pudo reemplazar
    assert sleeps == [0.01] * 10
    sleeps.clear()
    monkeypatch.undo()
    root2 = tmp_path / "other" / _meta.CACHE_DIR
    root2.parent.mkdir()
    monkeypatch.setattr(store_mod.time, "sleep", sleeps.append)
    busy = _BusyReads(monkeypatch, failures=99)
    assert ensure_root(root2) is False  # siempre ocupado: tampoco espera sin límite
    assert busy.calls == 11 and sleeps == [0.01] * 10


def test_ensure_root_does_not_wait_when_gitignore_is_right(
    store: Store, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    assert store.ensure()
    calls: list[Path] = []
    monkeypatch.setattr(store_mod, "atomic_write", lambda path, payload: calls.append(path) or True)
    assert store.ensure() is True
    assert calls == [] and sleeps == []  # ruta caliente: una lectura y nada más


def test_ensure_root_concurrent_calls_on_a_fresh_checkout(tmp_path: Path) -> None:
    threads, calls, rounds = 6, 5, 20
    for i in range(rounds):
        parent = tmp_path / f"checkout{i}"
        parent.mkdir()
        root = parent / _meta.CACHE_DIR
        barrier = threading.Barrier(threads)
        results: list[bool] = []
        errors: list[BaseException] = []

        def work() -> None:
            try:
                barrier.wait()
                for _ in range(calls):
                    results.append(ensure_root(root))
            except BaseException as exc:  # noqa: BLE001 - se informa abajo
                errors.append(exc)

        workers = [threading.Thread(target=work) for _ in range(threads)]
        for t in workers:
            t.start()
        for t in workers:
            t.join()
        assert errors == []
        assert results.count(False) == 0 and len(results) == threads * calls, f"ronda {i}"
        assert (root / ".gitignore").read_bytes() == GITIGNORE
        assert _leftovers(root) == []


# -- REQ-ARCH-58: una entrada inválida es una ausencia -----------------------------------


def _envelope(**overrides: object) -> dict[str, object]:
    env: dict[str, object] = {
        "cache_schema": CACHE_SCHEMA,
        "core": _meta.__version__,
        "created_at": "2026-09-30T12:00:00Z",
        "key": {},
        "data": {"ok": True},
    }
    env.update(overrides)
    return env


_CORRUPT: list[bytes] = [
    b"",
    b"{",
    b"\xff\xfe{}",
    b"[]",
    b"null",
    json.dumps(_envelope(cache_schema=2)).encode(),
    json.dumps(_envelope(cache_schema=True)).encode(),
    json.dumps(_envelope(core="0.0.0-other")).encode(),
    json.dumps(_envelope(created_at="ayer")).encode(),
    json.dumps(_envelope(created_at="2026-09-30T12:00:00")).encode(),
    json.dumps(_envelope(key=[])).encode(),
    json.dumps(_envelope(data=[1])).encode(),
    json.dumps({k: v for k, v in _envelope().items() if k != "data"}).encode(),
    b"[" * 100000,  # anidamiento que agota la recursión del decodificador
]


@pytest.mark.parametrize("raw", _CORRUPT, ids=[f"corrupt{i}" for i in range(len(_CORRUPT))])
def test_corrupt_entries_read_as_absent(store: Store, raw: bytes) -> None:
    path = store.path("tasks/3.json")
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    assert store.read("tasks/3.json") is None
    assert store.task(3) is None


def test_valid_hand_written_entry(store: Store) -> None:
    path = store.path("x.json")
    path.parent.mkdir(parents=True)
    path.write_bytes(json.dumps(_envelope()).encode())
    assert store.read("x.json") == {"ok": True}


def test_missing_oversized_and_directory_entries(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    assert store.read("tasks/9.json") is None
    store.write("big.json", {"x": "y" * 100}, key={})
    monkeypatch.setattr(store_mod, "MAX_ENTRY_BYTES", 50)
    assert store.read("big.json") is None
    monkeypatch.undo()
    store.path("dir.json").mkdir(parents=True)
    assert store.read("dir.json") is None


# -- tasks/<n>.json -----------------------------------------------------------------------


def test_task_roundtrip(store: Store) -> None:
    tc = TaskCache(12, "carla", "claimed", ("methods",), "task/12", "2026-09-30T11:00:00Z")
    store.write_task(tc)
    assert store.task(12) == tc
    assert store.task(13) is None
    open_task = TaskCache(5, None, "open", (), None, "2026-09-30T11:00:00+00:00")
    store.write_task(open_task, key={"repo": "o/r"})
    assert store.task(5) == open_task


@pytest.mark.parametrize(
    "change",
    [
        {"number": 13},
        {"number": True},
        {"claimant": ""},
        {"claimant": 7},
        {"state": "done"},
        {"sections": "methods"},
        {"sections": ["methods", 3]},
        {"local_branch": ""},
        {"fetched_at": "no-date"},
        {"fetched_at": None},
    ],
)
def test_task_with_invalid_shape_is_absent(store: Store, change: dict[str, object]) -> None:
    data: dict[str, object] = {
        "number": 12,
        "claimant": "carla",
        "state": "claimed",
        "sections": ["methods"],
        "local_branch": "task/12",
        "fetched_at": "2026-09-30T11:00:00Z",
    }
    data.update(change)
    store.write("tasks/12.json", data, key={})
    assert store.task(12) is None


@pytest.mark.parametrize("n", [0, -1, True, "12"])
def test_task_number_must_be_positive_int(store: Store, n: object) -> None:
    assert store.task(n) is None  # type: ignore[arg-type]


# -- identity.json (08 §6.2) ----------------------------------------------------------------


def test_identity_checks_fingerprint_and_expiry(project: Path) -> None:
    clock = {"now": NOW}
    store = Store(project, now=lambda: clock["now"])
    expires = (NOW + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.write_identity(
        IdentityCache("Ana-Garcia", 5829301, expires),
        token_fp="3f9a1c0b2d4e",
        extra={"kind": "user", "token_source": "GH_TOKEN"},
    )
    env = store.read_envelope("identity.json")
    assert env is not None and env["key"] == {"forge": "github", "token_fp": "3f9a1c0b2d4e"}
    assert "token" not in env["data"]
    ident = store.identity(token_fp="3f9a1c0b2d4e")
    assert ident == IdentityCache("Ana-Garcia", 5829301, expires)
    assert store.identity(token_fp=None) == ident  # sin huella: no se compara
    assert store.identity(token_fp="000000000000") is None
    clock["now"] = NOW + timedelta(hours=24)
    assert store.identity(token_fp="3f9a1c0b2d4e") is None  # venció


@pytest.mark.parametrize(
    "data",
    [
        {"login": "", "id": 1, "expires_at": "2027-01-01T00:00:00Z"},
        {"id": 1, "expires_at": "2027-01-01T00:00:00Z"},
        {"login": "ana", "id": True, "expires_at": "2027-01-01T00:00:00Z"},
        {"login": "ana", "id": 1, "expires_at": "2027-01-01T00:00:00"},
        {"login": "ana", "id": 1},
    ],
)
def test_identity_with_invalid_shape_is_absent(store: Store, data: dict[str, object]) -> None:
    store.write("identity.json", data, key={"forge": "github", "token_fp": None})
    assert store.identity(token_fp=None) is None


def test_identity_without_id(store: Store) -> None:
    store.write("identity.json", {"login": "bot", "id": None, "expires_at": "2027-01-01T00:00:00Z"}, key={})
    assert store.identity(token_fp=None) == IdentityCache("bot", None, "2027-01-01T00:00:00Z")
    assert store.identity(token_fp="abc") is None  # la entrada no tiene huella
