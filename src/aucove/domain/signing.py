# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Llaves públicas de la firma del proyecto (00-marco D-72; 11-trazabilidad §8.10; 03-esquemas REQ-SCHEMA-96).

Solo biblioteca estándar: lo usan ``config``, para las reglas de ``project.yaml``, y
``evidence``, que firma y verifica con ``cryptography`` (el extra ``evidence``). Una llave
pública se escribe como la da ``ssh-keygen``: ``ssh-ed25519 <base64>``, con un comentario
opcional. Su huella es ``SHA256:`` más el SHA-256 del blob en base64 sin relleno, la misma
que muestra ``ssh-keygen -l``.

Una llave vale desde ``since`` y hasta antes de ``until`` (``None`` mientras vale). Para
rotarla se agrega la nueva y se le pone ``until`` a la anterior; nunca se borra de la lista,
para que las firmas viejas se sigan verificando (REQ-TRACE-93). La misma lista sale de
``project.yaml: evidence_signing.keys`` o del archivo que publica el grupo, un YAML con solo
la clave ``keys``, y :func:`parse_keys` aplica las mismas reglas a las dos.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final

from .. import _meta

__all__ = [
    "FORMAT",
    "KEY_TYPE",
    "MODES",
    "NAMESPACE",
    "EvidenceKey",
    "KeyProblem",
    "PublicKeyError",
    "current_keys",
    "find_key",
    "fingerprint",
    "format_time",
    "key_blob",
    "parse_keys",
    "parse_public_key",
    "parse_time",
    "raw_key",
]

NAMESPACE: Final = f"{_meta.NAME}-evidence"  # espacio de nombres SSHSIG (11 §8.10)
KEY_TYPE: Final = "ssh-ed25519"
FORMAT: Final = "sshsig"
MODES: Final = ("ci", "admin", "none")
MAX_KEYS: Final = 50
ED25519_BYTES: Final = 32
MAX_COMMENT: Final = 200

_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
_TIME_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_B64_RE = re.compile(r"[A-Za-z0-9+/]+={0,2}")
_KEY_FIELDS = frozenset({"id", "public_key", "since", "until"})


class PublicKeyError(ValueError):
    """Llave pública que no es una ``ssh-ed25519`` bien formada (``reason``: ``type`` o ``invalid``)."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"llave pública: {reason}")
        self.reason = reason


def _string(blob: bytes, offset: int) -> tuple[bytes, int]:
    """Un ``string`` del formato de llaves de SSH (RFC 4251 §5): largo de 4 bytes y los datos."""
    if offset + 4 > len(blob):
        raise PublicKeyError("invalid")
    (size,) = struct.unpack(">I", blob[offset : offset + 4])
    start, end = offset + 4, offset + 4 + size
    if end > len(blob):
        raise PublicKeyError("invalid")
    return blob[start:end], end


def key_blob(raw: bytes) -> bytes:
    """Blob de la llave ``ssh-ed25519`` con los 32 bytes ``raw``."""
    if len(raw) != ED25519_BYTES:
        raise PublicKeyError("invalid")
    kind = KEY_TYPE.encode("ascii")
    return struct.pack(">I", len(kind)) + kind + struct.pack(">I", len(raw)) + raw


def raw_key(blob: bytes) -> bytes:
    """Los 32 bytes de una llave ``ssh-ed25519`` en su blob; ``PublicKeyError`` si es otra cosa."""
    kind, offset = _string(blob, 0)
    if kind != KEY_TYPE.encode("ascii"):
        raise PublicKeyError("type")
    raw, offset = _string(blob, offset)
    if len(raw) != ED25519_BYTES or offset != len(blob):
        raise PublicKeyError("invalid")
    return raw


def parse_public_key(text: object) -> bytes:
    """Blob de ``ssh-ed25519 <base64> [comentario]``; ``PublicKeyError`` si no es una llave así."""
    if not isinstance(text, str) or "\n" in text or "\r" in text:
        raise PublicKeyError("invalid")
    parts = text.split(" ", 2)
    if len(parts) < 2:
        raise PublicKeyError("invalid")
    if parts[0] != KEY_TYPE:
        raise PublicKeyError("type")
    if len(parts) == 3 and not 0 < len(parts[2]) <= MAX_COMMENT:
        raise PublicKeyError("invalid")
    if _B64_RE.fullmatch(parts[1]) is None:
        raise PublicKeyError("invalid")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError):
        raise PublicKeyError("invalid") from None
    raw_key(blob)
    if base64.b64encode(blob).decode("ascii") != parts[1]:  # una sola forma de escribir la misma llave
        raise PublicKeyError("invalid")
    return blob


def fingerprint(blob: bytes) -> str:
    """Huella ``SHA256:<base64 sin relleno>`` del blob, la de ``ssh-keygen -l``."""
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


def parse_time(text: object) -> datetime:
    """Fecha y hora RFC 3339 en UTC, sin fracciones (``2026-10-14T00:00:00Z``); ``ValueError`` si no."""
    if not isinstance(text, str) or _TIME_RE.fullmatch(text) is None:
        raise ValueError("fecha")
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def format_time(when: datetime) -> str:
    """``AAAA-MM-DDThh:mm:ssZ`` en UTC."""
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class EvidenceKey:
    """Una llave pública del proyecto con su intervalo de validez."""

    id: str
    blob: bytes
    since: datetime
    until: datetime | None = None

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.blob)

    @property
    def public_key(self) -> str:
        """``ssh-ed25519 <base64>``, sin comentario."""
        return f"{KEY_TYPE} {base64.b64encode(self.blob).decode('ascii')}"

    def valid_at(self, when: datetime) -> bool:
        """La llave vale en ``when``: desde ``since`` y antes de ``until``."""
        return self.since <= when and (self.until is None or when < self.until)


@dataclass(frozen=True)
class KeyProblem:
    """Un problema de la lista de llaves: el índice, el campo y el motivo estable.

    ``reason``: ``invalid`` (la forma no es la de 03-esquemas §3.2), ``type`` (no es una llave
    ``ssh-ed25519``), ``duplicate_id``, ``duplicate_key`` o ``until_not_after_since``. Con
    ``index`` en ``None``, el problema es de la lista misma (``too_many`` o ``invalid``).
    """

    index: int | None
    field: str
    reason: str

    @property
    def pointer(self) -> str:
        """JSON Pointer relativo a la lista (``/0/until``)."""
        if self.index is None:
            return ""
        return f"/{self.index}" + (f"/{self.field}" if self.field else "")


def parse_keys(items: object) -> tuple[tuple[EvidenceKey, ...], tuple[KeyProblem, ...]]:
    """Las llaves válidas de ``items`` y los problemas, con las reglas de REQ-SCHEMA-96.

    Valida también la forma de cada elemento, porque la lista publicada no pasa por el
    esquema de ``project.yaml``. Una llave con problemas no entra al resultado.
    """
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return (), (KeyProblem(None, "", "invalid"),)
    if len(items) > MAX_KEYS:
        return (), (KeyProblem(None, "", "too_many"),)
    keys: list[EvidenceKey] = []
    problems: list[KeyProblem] = []
    seen_ids: set[str] = set()
    seen_blobs: set[bytes] = set()
    for index, item in enumerate(items):
        if not isinstance(item, Mapping) or set(item) - _KEY_FIELDS:
            problems.append(KeyProblem(index, "", "invalid"))
            continue
        key_id, text = item.get("id"), item.get("public_key")
        if not isinstance(key_id, str) or _ID_RE.fullmatch(key_id) is None:
            problems.append(KeyProblem(index, "id", "invalid"))
            continue
        try:
            blob = parse_public_key(text)
        except PublicKeyError as exc:
            problems.append(KeyProblem(index, "public_key", exc.reason))
            continue
        try:
            since = parse_time(item.get("since"))
        except ValueError:
            problems.append(KeyProblem(index, "since", "invalid"))
            continue
        until: datetime | None = None
        if item.get("until") is not None:
            try:
                until = parse_time(item.get("until"))
            except ValueError:
                problems.append(KeyProblem(index, "until", "invalid"))
                continue
            if until <= since:
                problems.append(KeyProblem(index, "until", "until_not_after_since"))
                continue
        if key_id in seen_ids:
            problems.append(KeyProblem(index, "id", "duplicate_id"))
            continue
        if blob in seen_blobs:
            problems.append(KeyProblem(index, "public_key", "duplicate_key"))
            continue
        seen_ids.add(key_id)
        seen_blobs.add(blob)
        keys.append(EvidenceKey(key_id, blob, since, until))
    return tuple(keys), tuple(problems)


def current_keys(keys: Sequence[EvidenceKey], when: datetime) -> tuple[EvidenceKey, ...]:
    """Las llaves que valen en ``when``."""
    return tuple(k for k in keys if k.valid_at(when))


def find_key(keys: Sequence[EvidenceKey], blob: bytes) -> EvidenceKey | None:
    """La llave de la lista con ese blob, o ``None``."""
    return next((k for k in keys if k.blob == blob), None)
