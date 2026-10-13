# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Cachés del adaptador GitHub (08-forja §3.4, §6.2 y §7.4; ``forge/cache.py``)."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aucove import _meta
from aucove.forge.base import BaseRules, Capabilities, Identity, RepoRef
from aucove.forge.cache import (
    CAPABILITIES_FILE,
    IDENTITY_FILE,
    ForgeCaches,
    HttpCache,
    HttpEntry,
    capabilities_data,
    capabilities_from,
    identity_from,
)
from aucove.localstore.store import Store

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
REPO = RepoRef("lab", "articulo")


def _entry(body: bytes = b"{}", etag: str | None = '"e"') -> HttpEntry:
    return HttpEntry(etag, None, None, "application/json", body)


def _key(i: int) -> tuple[str, str, str, str]:
    return (f"https://api.github.com/x/{i}", "application/vnd.github+json", "2026-03-10", "abc123abc123")


def _ident(login: str = "ana", uid: int | None = 7) -> Identity:
    return Identity(login, uid, "user", "GH_TOKEN", "fine_grained_pat", "repo_restricted", None, None, NOW)


def _caps(**over: object) -> Capabilities:
    base = dict(
        forge="github",
        repo=REPO,
        visibility="public",
        owner_type="Organization",
        viewer_permission="admin",
        default_branch="main",
        rules_available=True,
        code_owners_enforceable=True,
        base_rules=BaseRules("ruleset", 1, True, True, True, ("ci/build",), True),
        profile="enforced",
        push_rulesets=False,
        checks_api=False,
        transport="http",
        write_mode="git",
        detected_at=NOW,
        evidence=("GET /repos/lab/articulo -> 200 visibility=public",),
    )
    base.update(over)
    return Capabilities(**base)  # type: ignore[arg-type]


# -- caché HTTP en memoria ----------------------------------------------------------------


def test_memory_lru_respects_entry_limit() -> None:
    cache = HttpCache(max_entries=2)
    cache.put(_key(1), _entry(b"1"))
    cache.put(_key(2), _entry(b"2"))
    assert cache.get(_key(1)) is not None  # 1 pasa a ser la más reciente
    cache.put(_key(3), _entry(b"3"))
    assert cache.get(_key(2)) is None
    assert cache.get(_key(1)) is not None and cache.get(_key(3)) is not None


def test_memory_lru_respects_byte_limit() -> None:
    cache = HttpCache(max_bytes=100)
    cache.put(_key(1), _entry(b"a" * 60))
    cache.put(_key(2), _entry(b"b" * 60))
    assert cache.get(_key(1)) is None and cache.get(_key(2)) is not None


def test_entry_without_validators_is_not_stored() -> None:
    cache = HttpCache()
    cache.put(_key(1), _entry(etag=None))
    assert cache.get(_key(1)) is None


# -- caché HTTP en disco -----------------------------------------------------------------


def test_disk_entry_survives_a_new_cache(tmp_path: Path) -> None:
    store = Store(tmp_path)
    HttpCache(store).put(_key(1), HttpEntry('"v1"', "Wed, 07 Oct 2026 10:00:00 GMT", '<u>; rel="next"', "x/y", b"\x00\xff"))
    got = HttpCache(Store(tmp_path)).get(_key(1))
    assert got == HttpEntry('"v1"', "Wed, 07 Oct 2026 10:00:00 GMT", '<u>; rel="next"', "x/y", b"\x00\xff")
    files = list((tmp_path / _meta.CACHE_DIR / "cache" / "http").glob("*.json"))
    assert len(files) == 1
    envelope = json.loads(files[0].read_text(encoding="utf-8"))
    assert envelope["key"] == {
        "url": _key(1)[0], "accept": _key(1)[1], "api_version": "2026-03-10", "token_fp": "abc123abc123"
    }  # clave de REQ-FORGE-52; nunca el token


def test_disk_lru_evicts_least_recently_used(tmp_path: Path) -> None:
    store = Store(tmp_path)
    cache = HttpCache(store, max_entries=2)
    cache.put(_key(1), _entry(b"1"))
    cache.put(_key(2), _entry(b"2"))
    old = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
    directory = tmp_path / _meta.CACHE_DIR / "cache" / "http"
    for path in directory.glob("*.json"):
        os.utime(path, (old, old))
    cache.touch(_key(1))  # 1 es la usada hace menos tiempo
    cache.put(_key(3), _entry(b"3"))
    assert cache.get(_key(2)) is None
    assert cache.get(_key(1)) is not None and cache.get(_key(3)) is not None


def test_disk_entry_with_another_key_is_absent(tmp_path: Path) -> None:
    store = Store(tmp_path)
    cache = HttpCache(store)
    cache.put(_key(1), _entry())
    other = (_key(1)[0], _key(1)[1], _key(1)[2], "ffffffffffff")
    assert cache.get(other) is None


def test_corrupt_disk_entry_is_absent(tmp_path: Path) -> None:
    store = Store(tmp_path)
    cache = HttpCache(store)
    cache.put(_key(1), _entry())
    for path in (tmp_path / _meta.CACHE_DIR / "cache" / "http").glob("*.json"):
        path.write_text("{no json", encoding="utf-8")
    assert cache.get(_key(1)) is None  # REQ-ARCH-58


def test_drop_removes_the_entry(tmp_path: Path) -> None:
    for cache in (HttpCache(), HttpCache(Store(tmp_path))):
        cache.put(_key(1), _entry())
        cache.drop(_key(1))
        assert cache.get(_key(1)) is None


# -- identidad --------------------------------------------------------------------------


def test_identity_round_trip_on_disk(tmp_path: Path) -> None:
    caches = ForgeCaches(Store(tmp_path))
    caches.save_identity(_ident(), "fp0000000001", NOW + timedelta(hours=24))
    assert ForgeCaches(Store(tmp_path)).load_identity("fp0000000001", NOW) == _ident()
    assert ForgeCaches(Store(tmp_path)).load_identity("fp0000000002", NOW) is None  # otra huella
    # localstore lee la misma entrada (07-hooks y check-path), con el mismo reloj que la guardó
    stored = Store(tmp_path, now=lambda: NOW).identity(token_fp="fp0000000001")
    assert stored is not None and (stored.login, stored.id) == ("ana", 7)
    raw = (tmp_path / _meta.CACHE_DIR / "cache" / IDENTITY_FILE).read_text(encoding="utf-8")
    assert json.loads(raw)["data"]["schema"].endswith("/identity/v1")


def test_identity_expires_after_ttl() -> None:
    caches = ForgeCaches()
    caches.save_identity(_ident(), "fp", NOW + timedelta(hours=24))
    assert caches.load_identity("fp", NOW + timedelta(hours=25)) is None
    assert caches.load_identity("fp", NOW + timedelta(hours=25), allow_expired=True) == _ident()


@pytest.mark.parametrize(
    "patch",
    [{"login": ""}, {"id": "7"}, {"kind": "robot"}, {"token_scope": "all"}, {"oauth_scopes": "repo"}, {"fetched_at": "x"}],
)
def test_identity_with_bad_shape_is_absent(patch: dict[str, object]) -> None:
    caches = ForgeCaches()
    caches.save_identity(_ident(), "fp", NOW + timedelta(hours=24))
    data = dict(caches._memory[IDENTITY_FILE][1])
    data.update(patch)
    assert identity_from(data, "fp") is None


def test_drop_identity_and_capabilities(tmp_path: Path) -> None:
    caches = ForgeCaches(Store(tmp_path))
    caches.save_identity(_ident(), "fp", NOW + timedelta(hours=24))
    key = {"forge": "github", "repo": "lab/articulo", "base_branch": "main", "token_fp": "fp", "login": "ana"}
    caches.save_capabilities(key, _caps(), NOW + timedelta(hours=24), NOW)
    caches.drop_identity()
    caches.drop_capabilities()
    assert caches.load_identity("fp", NOW) is None
    assert caches.load_capabilities(key, REPO) is None


# -- capacidades ---------------------------------------------------------------------------


def test_capabilities_round_trip_and_key(tmp_path: Path) -> None:
    caches = ForgeCaches(Store(tmp_path))
    key = {"forge": "github", "repo": "lab/articulo", "base_branch": "main", "token_fp": "fp", "login": "ana"}
    caches.save_capabilities(key, _caps(), NOW + timedelta(hours=24), NOW)
    got = ForgeCaches(Store(tmp_path)).load_capabilities(key, REPO)
    assert got is not None and got.caps == _caps() and got.revalidated_at == NOW
    assert ForgeCaches(Store(tmp_path)).load_capabilities({**key, "login": "bruno"}, REPO) is None  # REQ-FORGE-17
    envelope = json.loads((tmp_path / _meta.CACHE_DIR / "cache" / CAPABILITIES_FILE).read_text(encoding="utf-8"))
    assert envelope["key"]["base_branch"] == "main"  # lo lee config.base sin red (07-hooks REQ-HOOK-41)


def test_capabilities_without_rules_round_trip() -> None:
    caps = _caps(rules_available=None, code_owners_enforceable=None, base_rules=None, profile="advisory", checks_api=None)
    data = capabilities_data(caps, NOW, NOW)
    found = capabilities_from(data, REPO)
    assert found is not None and found.caps == caps


@pytest.mark.parametrize(
    "patch",
    [
        {"visibility": "secret"},
        {"profile": "strict"},
        {"base_rules": {"source": "magic"}},
        {"base_rules": {"source": "ruleset", "required_approvals": "1"}},
        {"evidence": "x"},
        {"push_rulesets": None},
        {"detected_at": None},
    ],
)
def test_capabilities_with_bad_shape_are_absent(patch: dict[str, object]) -> None:
    data = capabilities_data(_caps(), NOW, NOW)
    data.update(patch)
    assert capabilities_from(data, REPO) is None
