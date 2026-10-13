# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Cachés del adaptador GitHub (08-forja §3.4, §6.2 y §7.4; árbol en 01-arquitectura §7.3).

- ``identity.json``: clave ``(forge, token_fp)``; vence a las 24 h (REQ-FORGE-45). Nunca
  guarda el token, solo su huella.
- ``capabilities.json``: clave ``(forge, repo, base_branch, token_fp, login)``; vence a las
  24 h (REQ-FORGE-17) y guarda cuándo se revalidó ``GET /repos/{o}/{r}`` (REQ-FORGE-20).
- ``http/``: respuestas ``GET`` 200 con ``ETag`` o ``Last-Modified``, con clave (URL con
  consulta, ``Accept``, versión de la API, huella del token), para reenviarlas con
  ``If-None-Match`` o ``If-Modified-Since`` (REQ-FORGE-52). LRU de 2.000 entradas o 50 MB
  (REQ-FORGE-53): en disco, el orden de uso es la fecha de modificación del archivo.

``forge`` no importa ``localstore`` (01 §3.1): el almacén en disco llega inyectado y solo
se usa su interfaz (``CacheBackend``: ``read``, ``write`` y ``path`` de
``localstore.Store``), que pone el sobre común y escribe de forma atómica (REQ-ARCH-57).
Sin almacén, como en el CI (REQ-FORGE-18, REQ-FORGE-53), todo vive en memoria. Una
entrada ilegible cuenta como ausente (REQ-ARCH-58) y un fallo del disco nunca es un
error: la caché es descartable.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .. import _meta
from .base import BaseRules, Capabilities, Identity, RepoRef

__all__ = [
    "CAPABILITIES_FILE",
    "CAPABILITIES_SCHEMA",
    "HTTP_DIR",
    "HTTP_MAX_BYTES",
    "HTTP_MAX_ENTRIES",
    "HTTP_MAX_ENTRY_BYTES",
    "IDENTITY_FILE",
    "IDENTITY_SCHEMA",
    "CacheBackend",
    "CachedCapabilities",
    "ForgeCaches",
    "HttpCache",
    "HttpEntry",
    "HttpKey",
    "iso_z",
    "parse_time",
]

IDENTITY_FILE = "identity.json"
CAPABILITIES_FILE = "capabilities.json"
HTTP_DIR = "http"
IDENTITY_SCHEMA = f"{_meta.SCHEMA_BASE}/identity/v1"
CAPABILITIES_SCHEMA = f"{_meta.SCHEMA_BASE}/capabilities/v1"
HTTP_MAX_ENTRIES = 2000  # REQ-FORGE-53
HTTP_MAX_BYTES = 50 * 10**6  # 50 MB
HTTP_MAX_ENTRY_BYTES = 2 * 2**20  # una respuesta mayor no se guarda

_KINDS = ("user", "bot", "ci")
_TOKEN_KINDS = ("fine_grained_pat", "classic_pat", "oauth", "app_user", "app_installation", "proxy", "unknown")
_SCOPES = ("repo_restricted", "owner_wide", "broad", "unknown", "ci")
_VISIBILITY = ("public", "private", "internal")
_OWNER_TYPES = ("User", "Organization")
_PERMISSIONS = ("admin", "maintain", "write", "triage", "read")
_SOURCES = ("ruleset", "classic", "none")


class CacheBackend(Protocol):
    """Lo que el adaptador usa de ``localstore.Store`` (01 §7.3)."""

    def read(self, rel: str, *, key: Mapping[str, object] | None = None) -> dict[str, Any] | None: ...

    def write(self, rel: str, data: Mapping[str, object], *, key: Mapping[str, object]) -> None: ...

    def path(self, rel: str) -> Path: ...


# -- fechas ----------------------------------------------------------------------------


def iso_z(moment: datetime) -> str:
    """``YYYY-MM-DDTHH:MM:SSZ`` en UTC; una fecha sin zona se toma como UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: object) -> datetime | None:
    """Fecha ISO 8601 con zona; ``None`` si no lo es."""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment.astimezone(timezone.utc) if moment.tzinfo is not None else None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _opt_str(value: object) -> bool:
    return value is None or isinstance(value, str)


def _opt_bool(value: object) -> bool:
    return value is None or isinstance(value, bool)


def _str_tuple(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return tuple(value)


# -- caché HTTP ------------------------------------------------------------------------

# (URL con consulta, Accept, versión de la API, huella del token): REQ-FORGE-52
HttpKey = tuple[str, str, str, str]


@dataclass(frozen=True)
class HttpEntry:
    """Respuesta guardada: validadores, ``Link`` (para paginar), cuerpo y ``X-OAuth-Scopes`` (§6.1)."""

    etag: str | None
    last_modified: str | None
    link: str | None
    content_type: str | None
    body: bytes
    oauth_scopes: str | None = None

    @property
    def size(self) -> int:
        texts = (self.etag, self.last_modified, self.link, self.content_type, self.oauth_scopes)
        return len(self.body) + sum(len(v or "") for v in texts)


def _http_rel(key: HttpKey) -> str:
    raw = json.dumps(list(key), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return f"{HTTP_DIR}/{hashlib.sha256(raw).hexdigest()[:32]}.json"


def _http_key_dict(key: HttpKey) -> dict[str, str]:
    url, accept, version, fp = key
    return {"url": url, "accept": accept, "api_version": version, "token_fp": fp}


class HttpCache:
    """LRU de respuestas ``GET`` (REQ-FORGE-52, REQ-FORGE-53); en disco si hay ``backend``."""

    def __init__(
        self,
        backend: CacheBackend | None = None,
        *,
        max_entries: int = HTTP_MAX_ENTRIES,
        max_bytes: int = HTTP_MAX_BYTES,
    ) -> None:
        self.backend = backend
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._memory: OrderedDict[HttpKey, HttpEntry] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key: HttpKey) -> HttpEntry | None:
        if self.backend is None:
            with self._lock:
                entry = self._memory.get(key)
                if entry is not None:
                    self._memory.move_to_end(key)
                return entry
        data = self.backend.read(_http_rel(key), key=_http_key_dict(key))
        if data is None:
            return None
        try:
            body = base64.b64decode(str(data.get("body", "")), validate=True)
        except (ValueError, TypeError):
            return None
        fields = [data.get(k) for k in ("etag", "last_modified", "link", "content_type", "oauth_scopes")]
        if not all(_opt_str(v) for v in fields):
            return None
        etag, last_modified, link, content_type, scopes = fields
        if etag is None and last_modified is None:
            return None
        return HttpEntry(etag, last_modified, link, content_type, body, scopes)  # type: ignore[arg-type]

    def touch(self, key: HttpKey) -> None:
        """Marca la entrada como usada (orden del LRU)."""
        if self.backend is None:
            with self._lock:
                if key in self._memory:
                    self._memory.move_to_end(key)
            return
        try:
            os.utime(self.backend.path(_http_rel(key)))
        except (OSError, ValueError):
            return

    def put(self, key: HttpKey, entry: HttpEntry) -> None:
        if entry.etag is None and entry.last_modified is None:
            return
        if len(entry.body) > HTTP_MAX_ENTRY_BYTES:
            return
        if self.backend is None:
            with self._lock:
                old = self._memory.pop(key, None)
                if old is not None:
                    self._bytes -= old.size
                self._memory[key] = entry
                self._bytes += entry.size
                while self._memory and (len(self._memory) > self.max_entries or self._bytes > self.max_bytes):
                    _, dropped = self._memory.popitem(last=False)
                    self._bytes -= dropped.size
            return
        data = {
            "etag": entry.etag,
            "last_modified": entry.last_modified,
            "link": entry.link,
            "content_type": entry.content_type,
            "oauth_scopes": entry.oauth_scopes,
            "body": base64.b64encode(entry.body).decode("ascii"),
        }
        try:
            self.backend.write(_http_rel(key), data, key=_http_key_dict(key))
        except (OSError, ValueError, TypeError):
            return
        self._evict()

    def drop(self, key: HttpKey) -> None:
        if self.backend is None:
            with self._lock:
                old = self._memory.pop(key, None)
                if old is not None:
                    self._bytes -= old.size
            return
        try:
            os.unlink(self.backend.path(_http_rel(key)))
        except (OSError, ValueError):
            return

    def _evict(self) -> None:
        """Borra las entradas usadas hace más tiempo hasta cumplir los dos topes."""
        assert self.backend is not None
        try:
            directory = self.backend.path(f"{HTTP_DIR}/x.json").parent
            files: list[tuple[float, int, str]] = []
            with os.scandir(directory) as it:
                for item in it:
                    if not item.name.endswith(".json") or not item.is_file(follow_symlinks=False):
                        continue
                    st = item.stat(follow_symlinks=False)
                    files.append((st.st_mtime, st.st_size, item.path))
        except (OSError, ValueError):
            return
        count = len(files)
        total = sum(size for _, size, _ in files)
        if count <= self.max_entries and total <= self.max_bytes:
            return
        files.sort()
        for _, size, path in files:
            if count <= self.max_entries and total <= self.max_bytes:
                break
            try:
                os.unlink(path)
            except OSError:
                continue
            count -= 1
            total -= size


# -- identidad (08 §6.2) -------------------------------------------------------------------


def identity_data(ident: Identity, fp: str, expires_at: datetime) -> dict[str, object]:
    return {
        "schema": IDENTITY_SCHEMA,
        "forge": "github",
        "token_fp": fp,
        "login": ident.login,
        "id": ident.id,
        "kind": ident.kind,
        "token_source": ident.token_source,
        "token_kind": ident.token_kind,
        "token_scope": ident.token_scope,
        "oauth_scopes": list(ident.oauth_scopes) if ident.oauth_scopes is not None else None,
        "previous_login": ident.previous_login,
        "fetched_at": iso_z(ident.fetched_at),
        "expires_at": iso_z(expires_at),
    }


def identity_from(data: Mapping[str, Any], fp: str) -> tuple[Identity, datetime] | None:
    """``(Identity, expires_at)`` de ``data``; ``None`` si su forma no es válida."""
    login, ident_id, kind = data.get("login"), data.get("id"), data.get("kind")
    source, token_kind, scope = data.get("token_source"), data.get("token_kind"), data.get("token_scope")
    previous = data.get("previous_login")
    scopes_raw = data.get("oauth_scopes")
    fetched, expires = parse_time(data.get("fetched_at")), parse_time(data.get("expires_at"))
    if data.get("token_fp") != fp or not isinstance(login, str) or not login:
        return None
    if ident_id is not None and not _is_int(ident_id):
        return None
    if kind not in _KINDS or token_kind not in _TOKEN_KINDS or scope not in _SCOPES:
        return None
    if not isinstance(source, str) or not source or not _opt_str(previous):
        return None
    scopes = None if scopes_raw is None else _str_tuple(scopes_raw)
    if scopes_raw is not None and scopes is None:
        return None
    if fetched is None or expires is None:
        return None
    ident = Identity(login, ident_id, kind, source, token_kind, scope, scopes, previous, fetched)  # type: ignore[arg-type]
    return ident, expires


# -- capacidades (08 §3.4) -----------------------------------------------------------------


@dataclass(frozen=True)
class CachedCapabilities:
    caps: Capabilities
    expires_at: datetime
    revalidated_at: datetime


def _rules_data(rules: BaseRules | None) -> dict[str, object] | None:
    if rules is None:
        return None
    return {
        "source": rules.source,
        "required_approvals": rules.required_approvals,
        "require_code_owner_review": rules.require_code_owner_review,
        "require_last_push_approval": rules.require_last_push_approval,
        "dismiss_stale_reviews_on_push": rules.dismiss_stale_reviews_on_push,
        "required_checks": list(rules.required_checks) if rules.required_checks is not None else None,
        "blocks_force_push": rules.blocks_force_push,
    }


def _rules_from(data: object) -> tuple[bool, BaseRules | None]:
    """``(válido, reglas)``; ``None`` es un valor válido."""
    if data is None:
        return True, None
    if not isinstance(data, dict) or data.get("source") not in _SOURCES:
        return False, None
    approvals = data.get("required_approvals")
    checks_raw = data.get("required_checks")
    checks = None if checks_raw is None else _str_tuple(checks_raw)
    flags = [data.get(k) for k in (
        "require_code_owner_review", "require_last_push_approval", "dismiss_stale_reviews_on_push", "blocks_force_push"
    )]
    if (approvals is not None and not _is_int(approvals)) or (checks_raw is not None and checks is None):
        return False, None
    if not all(_opt_bool(f) for f in flags):
        return False, None
    code_owner, last_push, dismiss, force = flags
    return True, BaseRules(data["source"], approvals, code_owner, last_push, dismiss, checks, force)  # type: ignore[arg-type]


def capabilities_data(caps: Capabilities, expires_at: datetime, revalidated_at: datetime) -> dict[str, object]:
    return {
        "schema": CAPABILITIES_SCHEMA,
        "detected_at": iso_z(caps.detected_at),
        "expires_at": iso_z(expires_at),
        "revalidated_at": iso_z(revalidated_at),
        "visibility": caps.visibility,
        "owner_type": caps.owner_type,
        "viewer_permission": caps.viewer_permission,
        "default_branch": caps.default_branch,
        "rules_available": caps.rules_available,
        "code_owners_enforceable": caps.code_owners_enforceable,
        "base_rules": _rules_data(caps.base_rules),
        "profile": caps.profile,
        "push_rulesets": caps.push_rulesets,
        "checks_api": caps.checks_api,
        "transport": caps.transport,
        "write_mode": caps.write_mode,
        "evidence": list(caps.evidence),
    }


def capabilities_from(data: Mapping[str, Any], repo: RepoRef) -> CachedCapabilities | None:
    """Capacidades guardadas; ``None`` si la forma no es válida."""
    detected, expires = parse_time(data.get("detected_at")), parse_time(data.get("expires_at"))
    revalidated = parse_time(data.get("revalidated_at")) or detected
    if detected is None or expires is None or revalidated is None:
        return None
    vis, owner, perm = data.get("visibility"), data.get("owner_type"), data.get("viewer_permission")
    default_branch, profile = data.get("default_branch"), data.get("profile")
    rules, owners, checks_api = data.get("rules_available"), data.get("code_owners_enforceable"), data.get("checks_api")
    push, transport, write_mode = data.get("push_rulesets"), data.get("transport"), data.get("write_mode")
    evidence = _str_tuple(data.get("evidence"))
    if vis not in _VISIBILITY or owner not in _OWNER_TYPES or perm not in _PERMISSIONS:
        return None
    if not isinstance(default_branch, str) or not default_branch or profile not in ("enforced", "advisory"):
        return None
    if not (_opt_bool(rules) and _opt_bool(owners) and _opt_bool(checks_api) and isinstance(push, bool)):
        return None
    if transport not in ("http", "gh") or write_mode not in ("git", "api") or evidence is None:
        return None
    ok, base_rules = _rules_from(data.get("base_rules"))
    if not ok:
        return None
    caps = Capabilities(
        forge="github",
        repo=repo,
        visibility=vis,  # type: ignore[arg-type]
        owner_type=owner,  # type: ignore[arg-type]
        viewer_permission=perm,  # type: ignore[arg-type]
        default_branch=default_branch,
        rules_available=rules,  # type: ignore[arg-type]
        code_owners_enforceable=owners,  # type: ignore[arg-type]
        base_rules=base_rules,
        profile=profile,  # type: ignore[arg-type]
        push_rulesets=push,
        checks_api=checks_api,  # type: ignore[arg-type]
        transport=transport,  # type: ignore[arg-type]
        write_mode=write_mode,  # type: ignore[arg-type]
        detected_at=detected,
        evidence=evidence,
    )
    return CachedCapabilities(caps, expires, revalidated)


# -- fachada ---------------------------------------------------------------------------------


class ForgeCaches:
    """Cachés de un adaptador: en disco con ``backend``; si no, solo en memoria."""

    def __init__(self, backend: CacheBackend | None = None) -> None:
        self.backend = backend
        self.http = HttpCache(backend)
        self._memory: dict[str, tuple[dict[str, object], dict[str, object]]] = {}
        self._lock = threading.Lock()

    def _read(self, rel: str, key: Mapping[str, object]) -> dict[str, Any] | None:
        if self.backend is None:
            with self._lock:
                stored = self._memory.get(rel)
            if stored is None or stored[0] != dict(key):
                return None
            return dict(stored[1])
        try:
            return self.backend.read(rel, key=key)
        except (OSError, ValueError, TypeError):
            return None

    def _write(self, rel: str, data: Mapping[str, object], key: Mapping[str, object]) -> None:
        if self.backend is None:
            with self._lock:
                self._memory[rel] = (dict(key), dict(data))
            return
        try:
            self.backend.write(rel, data, key=key)
        except (OSError, ValueError, TypeError):
            return

    def _drop(self, rel: str) -> None:
        if self.backend is None:
            with self._lock:
                self._memory.pop(rel, None)
            return
        try:
            os.unlink(self.backend.path(rel))
        except (OSError, ValueError):
            return

    # identidad
    def load_identity(self, fp: str, now: datetime, *, allow_expired: bool = False) -> Identity | None:
        data = self._read(IDENTITY_FILE, {"forge": "github", "token_fp": fp})
        found = identity_from(data, fp) if data is not None else None
        if found is None:
            return None
        ident, expires = found
        if not allow_expired and expires <= now:
            return None
        return ident

    def save_identity(self, ident: Identity, fp: str, expires_at: datetime) -> None:
        self._write(IDENTITY_FILE, identity_data(ident, fp, expires_at), {"forge": "github", "token_fp": fp})

    def drop_identity(self) -> None:
        self._drop(IDENTITY_FILE)

    # capacidades
    def load_capabilities(self, key: Mapping[str, object], repo: RepoRef) -> CachedCapabilities | None:
        data = self._read(CAPABILITIES_FILE, key)
        return capabilities_from(data, repo) if data is not None else None

    def save_capabilities(
        self, key: Mapping[str, object], caps: Capabilities, expires_at: datetime, revalidated_at: datetime
    ) -> None:
        self._write(CAPABILITIES_FILE, capabilities_data(caps, expires_at, revalidated_at), key)

    def drop_capabilities(self) -> None:
        self._drop(CAPABILITIES_FILE)
