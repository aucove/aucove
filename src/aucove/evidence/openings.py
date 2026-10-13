# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Compromisos con sal y aperturas privadas (11-trazabilidad §8.3; evidence-openings/v1).

Fórmulas (‖ es la concatenación de bytes; SHA-256 en hex minúsculas):

- ``p = SHA-256("evidence.path.v1" ‖ 0x00 ‖ sal ‖ ruta)``, con la ruta en los bytes
  exactos que da ``git diff-tree -z``, sin normalizar (una ruta NFD sigue en NFD);
- ``c = SHA-256("evidence.blob.v1" ‖ 0x00 ‖ sal ‖ blob)``, con los bytes del blob de
  git, nunca los del disco; ``c = null`` si el archivo se borró;
- ``a = SHA-256("evidence.actor.v1" ‖ 0x00 ‖ sal ‖ rol ‖ 0x00 ‖ valor en UTF-8)``.

Cada sal tiene 32 bytes de ``secrets.token_bytes`` y no se repite (REQ-TRACE-78). El
esquema guarda una sal por entrada, que comparten ``p`` y ``c`` (la separación de
dominio impide que coincidan), y una por actor.

Las aperturas son PRIVADAS: viajan con el registro (artefacto, comentario
``ci:evidence`` del repo privado y copia local) y nunca por MCP ni en la salida de
``evidence verify`` (REQ-OPS-65). Con la apertura de una entrada y el archivo se
prueba ese archivo sin revelar los demás. Aquí no se valida con JSON Schema (la capa
no importa ``jsonschema``): ``parse_openings`` solo comprueba la forma que necesita.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .. import _meta

__all__ = [
    "ACTOR_DOMAIN",
    "BLOB_DOMAIN",
    "MAX_ACTOR_CHARS",
    "MAX_ENTRIES",
    "MAX_PATH_BYTES",
    "OPENINGS_SCHEMA",
    "PATH_DOMAIN",
    "ROLES",
    "SALT_BYTES",
    "ActorOpening",
    "EntryCheck",
    "EntryOpening",
    "Openings",
    "OpeningsError",
    "actor_commitment",
    "blob_commitment",
    "blob_hasher",
    "blob_oid",
    "check_actor",
    "check_entry",
    "load_openings",
    "manifest_entries",
    "new_salt",
    "parse_openings",
    "path_commitment",
]

OPENINGS_SCHEMA = f"{_meta.SCHEMA_BASE}/evidence-openings/v1"
SALT_BYTES = 32
PATH_DOMAIN = b"evidence.path.v1"
BLOB_DOMAIN = b"evidence.blob.v1"
ACTOR_DOMAIN = b"evidence.actor.v1"
# Roles de actor, en el orden en que van en el manifiesto y en las aperturas.
ROLES: tuple[str, ...] = ("pr_author", "merged_by", "author", "committer")
MAX_ENTRIES = 10_000  # entradas de un manifiesto (esquema: maxItems)
# 'path' de una apertura, en bytes UTF-8 de la ruta tal como la da git (11 §8.3); con
# 4096 bytes o menos, también cumple el maxLength del esquema, que cuenta caracteres.
MAX_PATH_BYTES = 4096
MAX_ACTOR_CHARS = 320  # 'value' de un actor (esquema: maxLength)

_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_OID_RE = re.compile(r"[0-9a-f]{40}")
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*/[A-Za-z0-9._-]{1,100}")
_MAX_REPOSITORY = 140


class OpeningsError(ValueError):
    """Aperturas con otra forma que la de ``evidence-openings/v1``; ``pointer`` es su JSON Pointer."""

    def __init__(self, pointer: str, detail: str = "") -> None:
        self.reason = "invalid"
        self.pointer = pointer
        super().__init__(f"aperturas: valor inválido en {pointer!r}" + (f" ({detail})" if detail else ""))


# -- compromisos ------------------------------------------------------------------------


def new_salt() -> bytes:
    """Sal nueva: 32 bytes de ``secrets.token_bytes`` (REQ-TRACE-78)."""
    return secrets.token_bytes(SALT_BYTES)


def _salt(salt: bytes) -> bytes:
    if not isinstance(salt, (bytes, bytearray)) or len(salt) != SALT_BYTES:
        raise ValueError(f"la sal debe tener {SALT_BYTES} bytes")
    return bytes(salt)


def path_commitment(salt: bytes, path: bytes) -> str:
    """``p`` de una ruta dada en los bytes exactos de git (sin normalizar)."""
    if not isinstance(path, (bytes, bytearray)) or not path:
        raise ValueError("la ruta debe ser bytes no vacíos")
    return hashlib.sha256(PATH_DOMAIN + b"\0" + _salt(salt) + bytes(path)).hexdigest()


def blob_hasher(salt: bytes) -> Any:
    """SHA-256 alimentado con el prefijo de ``c``; se le agregan los bytes del blob por partes.

    Sirve para contenidos grandes: ``h = blob_hasher(sal); h.update(parte)...; h.hexdigest()``.
    """
    return hashlib.sha256(BLOB_DOMAIN + b"\0" + _salt(salt))


def blob_commitment(salt: bytes, data: bytes) -> str:
    """``c`` de los bytes de un blob de git."""
    hasher = blob_hasher(salt)
    hasher.update(data)
    return str(hasher.hexdigest())


def actor_commitment(salt: bytes, role: str, value: str) -> str:
    """``a`` de un actor: ``role`` es uno de ``ROLES`` y ``value`` se codifica en UTF-8 estricto."""
    if role not in ROLES:
        raise ValueError(f"rol desconocido: {role!r}")
    raw = value.encode("utf-8")  # un sustituto suelto es UnicodeEncodeError (ValueError)
    return hashlib.sha256(ACTOR_DOMAIN + b"\0" + _salt(salt) + role.encode("ascii") + b"\0" + raw).hexdigest()


def blob_oid(data: bytes) -> str:
    """Id de git (SHA-1) de un blob con estos bytes, como ``git hash-object`` sin filtros."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


# -- aperturas ------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryOpening:
    """Apertura de una entrada: ruta tal como la da git (decodificada como UTF-8), sal, blob y tamaño.

    ``blob`` y ``size`` son ``None`` si el archivo se borró.
    """

    path: str
    salt: bytes = field(repr=False)  # nunca en un repr ni en un log (REQ-OPS-65)
    blob: str | None
    size: int | None

    @property
    def path_bytes(self) -> bytes:
        """Bytes exactos de la ruta, los mismos que dio git."""
        return self.path.encode("utf-8")

    def p(self) -> str:
        """Compromiso de la ruta."""
        return path_commitment(self.salt, self.path_bytes)

    def c(self, data: bytes) -> str:
        """Compromiso de ``data`` con la sal de esta entrada."""
        return blob_commitment(self.salt, data)

    def to_json(self) -> dict[str, Any]:
        return {"path": self.path, "salt": self.salt.hex(), "blob": self.blob, "size": self.size}


@dataclass(frozen=True)
class ActorOpening:
    """Apertura de un actor: rol, valor (login o ``Nombre <correo>``) y sal."""

    role: str
    value: str
    salt: bytes = field(repr=False)  # nunca en un repr ni en un log (REQ-OPS-65)

    def a(self) -> str:
        """Compromiso del actor."""
        return actor_commitment(self.salt, self.role, self.value)

    def to_json(self) -> dict[str, Any]:
        return {"r": self.role, "value": self.value, "salt": self.salt.hex()}


@dataclass(frozen=True)
class Openings:
    """Archivo privado de aperturas (``evidence-openings/v1``).

    Las entradas van en el orden de las del manifiesto (por ``p``) y los actores en el
    de ``ROLES``. Puede ser un subconjunto: cada apertura se busca por su compromiso.
    """

    manifest_sha256: str
    repository: str
    entries: tuple[EntryOpening, ...]
    actors: tuple[ActorOpening, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": OPENINGS_SCHEMA,
            "manifest_sha256": self.manifest_sha256,
            "repository": self.repository,
            "entries": [entry.to_json() for entry in self.entries],
            "actors": [actor.to_json() for actor in self.actors],
        }

    def dumps(self) -> bytes:
        """Contenido de ``openings.json``: JSON indentado, UTF-8 y salto de línea final.

        Las rutas van tal cual (sin normalizar); ``json`` las devuelve idénticas.
        """
        return (json.dumps(self.to_json(), ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _need(cond: bool, pointer: str, detail: str = "") -> None:
    if not cond:
        raise OpeningsError(pointer, detail)


def _hex64(value: object) -> bool:
    return isinstance(value, str) and _HEX64_RE.fullmatch(value) is not None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_text(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _valid_path(value: object) -> bool:
    """Ruta no vacía, Unicode válido y de ``MAX_PATH_BYTES`` bytes o menos en UTF-8 (11 §8.3)."""
    if not isinstance(value, str):
        return False
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return False
    return 0 < size <= MAX_PATH_BYTES


def _entry(obj: object, pointer: str) -> EntryOpening:
    _need(isinstance(obj, Mapping), pointer)
    assert isinstance(obj, Mapping)
    path, salt, blob, size = obj.get("path"), obj.get("salt"), obj.get("blob"), obj.get("size")
    _need(_valid_path(path), pointer + "/path")
    _need(_hex64(salt), pointer + "/salt")
    _need(blob is None or (isinstance(blob, str) and _OID_RE.fullmatch(blob) is not None), pointer + "/blob")
    _need(size is None or (_is_int(size) and size >= 0), pointer + "/size")
    assert isinstance(path, str) and isinstance(salt, str)
    return EntryOpening(path=path, salt=bytes.fromhex(salt), blob=blob, size=size)


def _actor(obj: object, pointer: str) -> ActorOpening:
    _need(isinstance(obj, Mapping), pointer)
    assert isinstance(obj, Mapping)
    role, value, salt = obj.get("r"), obj.get("value"), obj.get("salt")
    _need(role in ROLES, pointer + "/r")
    _need(isinstance(value, str) and 0 < len(value) <= MAX_ACTOR_CHARS and _valid_text(value), pointer + "/value")
    _need(_hex64(salt), pointer + "/salt")
    assert isinstance(role, str) and isinstance(value, str) and isinstance(salt, str)
    return ActorOpening(role=role, value=value, salt=bytes.fromhex(salt))


def parse_openings(obj: object) -> Openings:
    """Aperturas desde su objeto JSON (``openings.json`` o el campo ``openings`` de un registro).

    Comprueba la forma que se usa (``schema``, huella, repositorio, tipos y límites) y
    lanza ``OpeningsError`` con el JSON Pointer del primer problema. Las claves de más se
    ignoran: la validación completa es la del esquema, fuera de esta capa.
    """
    _need(isinstance(obj, Mapping), "")
    assert isinstance(obj, Mapping)
    _need(obj.get("schema") == OPENINGS_SCHEMA, "/schema")
    sha, repository = obj.get("manifest_sha256"), obj.get("repository")
    _need(_hex64(sha), "/manifest_sha256")
    _need(
        isinstance(repository, str)
        and len(repository) <= _MAX_REPOSITORY
        and _REPOSITORY_RE.fullmatch(repository) is not None,
        "/repository",
    )
    entries, actors = obj.get("entries"), obj.get("actors")
    _need(isinstance(entries, list) and len(entries) <= MAX_ENTRIES, "/entries")
    _need(isinstance(actors, list) and len(actors) <= len(ROLES), "/actors")
    assert isinstance(entries, list) and isinstance(actors, list)
    assert isinstance(sha, str) and isinstance(repository, str)
    return Openings(
        manifest_sha256=sha,
        repository=repository,
        entries=tuple(_entry(item, f"/entries/{i}") for i, item in enumerate(entries)),
        actors=tuple(_actor(item, f"/actors/{i}") for i, item in enumerate(actors)),
    )


def load_openings(data: bytes) -> Openings:
    """Aperturas desde los bytes de ``openings.json`` (UTF-8 estricto).

    Como el registro, son datos de terceros: un entero de más de 4300 cifras o un
    anidamiento que agota la recursión es ``OpeningsError``, nunca una excepción suelta.
    """
    try:
        obj = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError) as exc:  # UnicodeDecodeError y JSONDecodeError son ValueError
        raise OpeningsError("", str(exc)[:120]) from None
    return parse_openings(obj)


# -- verificación ------------------------------------------------------------------


@dataclass(frozen=True)
class EntryCheck:
    """Resultado de abrir una entrada contra un manifiesto.

    - ``found``: el ``p`` recalculado está en el manifiesto.
    - ``content``: ``True`` o ``False`` si se pudo comparar el contenido (los bytes dados
      contra ``c``, o el borrado: ``c`` y ``blob`` nulos a la vez); ``None`` si no hubo
      bytes que comparar o si ``p`` no está.
    """

    p: str
    found: bool
    content: bool | None


def manifest_entries(manifest: Mapping[str, Any]) -> dict[str, str | None]:
    """``p -> c`` de las entradas de un manifiesto ya leído (las mal formadas se ignoran)."""
    out: dict[str, str | None] = {}
    entries = manifest.get("entries")
    for item in entries if isinstance(entries, list) else ():
        if isinstance(item, Mapping) and isinstance(item.get("p"), str):
            c = item.get("c")
            out.setdefault(item["p"], c if isinstance(c, str) else None)
    return out


def check_entry(
    opening: EntryOpening, entries: Mapping[str, str | None], data: bytes | None = None
) -> EntryCheck:
    """Abre ``opening`` contra ``entries`` (de ``manifest_entries``) y, si se dan, contra ``data``."""
    p = opening.p()
    if p not in entries:
        return EntryCheck(p=p, found=False, content=None)
    expected = entries[p]
    if expected is None or opening.blob is None:
        return EntryCheck(p=p, found=True, content=expected is None and opening.blob is None)
    if data is None:
        return EntryCheck(p=p, found=True, content=None)
    return EntryCheck(p=p, found=True, content=opening.c(data) == expected)


def check_actor(opening: ActorOpening, actors: Sequence[Mapping[str, Any]]) -> bool:
    """``True`` si el manifiesto tiene un actor con el mismo rol y el ``a`` que da la apertura."""
    try:
        a = opening.a()
    except ValueError:
        return False
    return any(isinstance(item, Mapping) and item.get("r") == opening.role and item.get("a") == a for item in actors)
