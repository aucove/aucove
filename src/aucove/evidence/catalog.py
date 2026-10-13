# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Catálogo de TSA del núcleo y piezas comunes de los clientes de sellado (11-trazabilidad §8.4, §8.7).

``_data/tsa-catalog.json`` lista las TSA RFC 3161 con ``id``, ``name``, ``url``,
``policy_oids``, ``terms_url``, ``fingerprint_source`` (dónde publica la TSA su raíz),
``seal``, ``roots`` y ``certs``. Cada certificado va en PEM canónico (el de
``cryptography``, que para FreeTSA es byte a byte el archivo que publica, así que
``sha256sum`` del ``root.pem`` coincide con la huella de su página), con el SHA-256 de
su DER y su vigencia. Las raíces son las únicas anclas de confianza; ``certs`` guarda
cada certificado de firma e intermedio que tuvo la TSA, también los vencidos, para
verificar pruebas viejas aunque venza el artefacto (D-45). ``ci seal`` usa las entradas
con ``seal: true`` en orden (REQ-CI-53) y la verificación usa todas: retirar una TSA
nunca borra sus certificados (REQ-TRACE-83). Cambiar el catálogo exige una versión
nueva del núcleo. Este módulo lo lee con la biblioteca estándar: comprueba la forma y
que cada SHA-256 sea el del PEM, sin importar ``cryptography``.

Además reúne lo que comparten ``tsa``, ``revocation`` y ``ots``:

- :class:`Check`, el resultado de una comprobación (``$defs/evidence_check`` de
  result.schema.json, sin ``check`` ni ``time_local``, que pone quien llama), con un
  ``reason`` estable para localizar el texto y ``detail`` técnico sin sales ni rutas
  (REQ-OPS-65);
- la importación perezosa del extra ``evidence`` (REQ-ARCH-99): sin él, cada función
  pública responde ``skip`` con ``W_CONFIG_TOOL_MISSING`` y el módulo se sigue
  importando;
- el cliente HTTP (``httpx2`` con ``truststore`` o ``SSL_CERT_FILE``, proxies del
  entorno; REQ-ARCH-65), con el transporte inyectable para las pruebas, y la
  clasificación de sus errores.
"""

from __future__ import annotations

import base64
import binascii
import functools
import hashlib
import json
import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .. import _meta

if TYPE_CHECKING:
    import ssl

    import httpx2

TOOL_MISSING = "W_CONFIG_TOOL_MISSING"
TIMEOUT_S = 15.0  # timeout por solicitud (11 §8.4, REQ-CI-53)
CATALOG_VERSION = 1

CheckStatus = Literal["pass", "weak", "pending", "skip", "fail"]

_CATALOG_FILE = Path(__file__).resolve().parent.parent / "_data" / "tsa-catalog.json"
_ID_RE = re.compile(r"^[a-z0-9-]{1,32}$")  # el patrón de ``source`` en evidence-record/v1
_OID_RE = re.compile(r"^[0-2](?:\.(?:0|[1-9][0-9]*))+$")
_HEX256_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_PEM_RE = re.compile(
    r"^-----BEGIN CERTIFICATE-----\n((?:[A-Za-z0-9+/=]{1,64}\n)+)-----END CERTIFICATE-----\n$"
)
_ENTRY_KEYS = frozenset(
    {"id", "name", "url", "policy_oids", "terms_url", "fingerprint_source", "seal", "roots", "certs"}
)
_CERT_KEYS = frozenset({"subject", "sha256", "not_before", "not_after", "pem"})
_DETAIL_MAX = 300  # maxLength de detail y error en los esquemas


@dataclass(frozen=True, slots=True)
class Check:
    """Resultado de comprobar una prueba de fecha (05-operaciones §9.26).

    ``status``: ``pass``, ``weak`` (válida con menos confianza), ``pending`` (OTS sin
    bloque), ``skip`` (no se pudo comprobar) o ``fail``. ``source`` es el id de la TSA,
    ``calendars`` o ``bitcoin``. ``reason`` es un código estable (``ok``,
    ``no_revocation``, ``digest_mismatch``…) y ``detail``, una línea técnica de 300
    caracteres o menos, sin sales ni rutas.
    """

    status: CheckStatus
    source: str | None
    reason: str
    time_utc: datetime | None = None
    detail: str | None = None
    warnings: tuple[str, ...] = ()


def skip_check(source: str | None) -> Check:
    """``skip`` con ``W_CONFIG_TOOL_MISSING``: falta el extra ``evidence`` (REQ-ARCH-99)."""
    return Check("skip", source, "missing_extra", detail="falta el extra evidence", warnings=(TOOL_MISSING,))


def clip(text: str, limit: int = _DETAIL_MAX) -> str:
    """Una sola línea de ``limit`` caracteres o menos (``detail`` y ``error``)."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# -- catálogo ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CatalogCert:
    """Un certificado del catálogo: PEM canónico, SHA-256 del DER y vigencia (UTC)."""

    subject: str
    sha256: str
    not_before: str
    not_after: str
    pem: str

    def der(self) -> bytes:
        """Bytes DER del certificado (sin ``cryptography``)."""
        return _pem_to_der(self.pem)


@dataclass(frozen=True, slots=True)
class TsaEntry:
    """Una TSA del catálogo (11-trazabilidad §8.7)."""

    id: str
    name: str
    url: str
    policy_oids: tuple[str, ...]
    terms_url: str
    fingerprint_source: str
    seal: bool
    roots: tuple[CatalogCert, ...]
    certs: tuple[CatalogCert, ...]


@dataclass(frozen=True, slots=True)
class Catalog:
    """Las TSA del catálogo, en el orden del archivo."""

    entries: tuple[TsaEntry, ...]

    def get(self, tsa_id: str) -> TsaEntry | None:
        """La entrada ``tsa_id`` (también si está retirada), o ``None``."""
        return next((e for e in self.entries if e.id == tsa_id), None)

    def sealing(self) -> tuple[TsaEntry, ...]:
        """Las entradas con ``seal: true``, en orden: las que usa ``ci seal``."""
        return tuple(e for e in self.entries if e.seal)

    def __iter__(self) -> Iterator[TsaEntry]:
        return iter(self.entries)


def catalog_path() -> Path:
    """Ruta del catálogo empaquetado (``<paquete>/_data/tsa-catalog.json``)."""
    return _CATALOG_FILE


def load_catalog(path: Path | None = None) -> Catalog:
    """Lee y valida el catálogo; ``ValueError`` si no tiene la forma esperada.

    Sin ``path``, el empaquetado, que se lee una sola vez por proceso.
    """
    if path is None:
        return _packaged_catalog()
    return parse_catalog(path.read_bytes().decode("utf-8"))


@functools.cache
def _packaged_catalog() -> Catalog:
    return parse_catalog(_CATALOG_FILE.read_bytes().decode("utf-8"))


def parse_catalog(text: str) -> Catalog:
    """Catálogo a partir de su JSON; ``ValueError`` con el motivo si no es válido."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"el catálogo de TSA no es JSON: {exc}") from exc
    if not isinstance(data, dict) or set(data) != {"version", "tsa"}:
        raise ValueError("el catálogo de TSA debe tener exactamente 'version' y 'tsa'")
    if data["version"] != CATALOG_VERSION:
        raise ValueError(f"versión de catálogo no admitida: {data['version']!r}")
    raw = data["tsa"]
    if not isinstance(raw, list) or not raw:
        raise ValueError("'tsa' debe ser una lista no vacía")
    entries = tuple(_entry(item, i) for i, item in enumerate(raw))
    ids = [e.id for e in entries]
    if len(set(ids)) != len(ids):
        raise ValueError("ids de TSA repetidos en el catálogo")
    return Catalog(entries)


def _entry(item: object, index: int) -> TsaEntry:
    where = f"tsa[{index}]"
    if not isinstance(item, dict) or set(item) != _ENTRY_KEYS:
        raise ValueError(f"{where}: claves esperadas {sorted(_ENTRY_KEYS)}")
    tsa_id = item["id"]
    if not isinstance(tsa_id, str) or not _ID_RE.match(tsa_id):
        raise ValueError(f"{where}: id inválido {tsa_id!r}")
    for key in ("name", "url", "terms_url", "fingerprint_source"):
        if not isinstance(item[key], str) or not item[key]:
            raise ValueError(f"{where}.{key}: debe ser texto no vacío")
    for key in ("url", "terms_url", "fingerprint_source"):
        if not item[key].startswith(("https://", "http://")):
            raise ValueError(f"{where}.{key}: debe ser una URL http(s)")
    oids = item["policy_oids"]
    if not isinstance(oids, list) or not oids or not all(isinstance(o, str) and _OID_RE.match(o) for o in oids):
        raise ValueError(f"{where}.policy_oids: lista no vacía de OID")
    if not isinstance(item["seal"], bool):
        raise ValueError(f"{where}.seal: debe ser booleano")
    roots = _certs(item["roots"], f"{where}.roots")
    if not roots:
        raise ValueError(f"{where}.roots: hace falta al menos una raíz")
    return TsaEntry(
        id=tsa_id,
        name=item["name"],
        url=item["url"],
        policy_oids=tuple(oids),
        terms_url=item["terms_url"],
        fingerprint_source=item["fingerprint_source"],
        seal=item["seal"],
        roots=roots,
        certs=_certs(item["certs"], f"{where}.certs"),
    )


def _certs(raw: object, where: str) -> tuple[CatalogCert, ...]:
    if not isinstance(raw, list):
        raise ValueError(f"{where}: debe ser una lista")
    out: list[CatalogCert] = []
    for i, item in enumerate(raw):
        at = f"{where}[{i}]"
        if not isinstance(item, dict) or set(item) != _CERT_KEYS:
            raise ValueError(f"{at}: claves esperadas {sorted(_CERT_KEYS)}")
        if not all(isinstance(item[k], str) for k in _CERT_KEYS):
            raise ValueError(f"{at}: todos los campos son texto")
        if not _HEX256_RE.match(item["sha256"]):
            raise ValueError(f"{at}.sha256: 64 dígitos hexadecimales en minúscula")
        for key in ("not_before", "not_after"):
            if not _UTC_RE.match(item[key]):
                raise ValueError(f"{at}.{key}: fecha UTC AAAA-MM-DDTHH:MM:SSZ")
        if item["not_before"] >= item["not_after"]:
            raise ValueError(f"{at}: vigencia vacía")
        der = _pem_to_der(item["pem"])  # ValueError si el PEM no es canónico
        if _sha256_hex(der) != item["sha256"]:
            raise ValueError(f"{at}.sha256: no es el SHA-256 del certificado")
        out.append(CatalogCert(**item))
    return tuple(out)


def _pem_to_der(pem: str) -> bytes:
    m = _PEM_RE.match(pem)
    if m is None:
        raise ValueError("PEM de certificado no canónico (un bloque, líneas de 64, LF)")
    try:
        return base64.b64decode("".join(m.group(1).split()), validate=True)
    except binascii.Error as exc:
        raise ValueError(f"PEM con base64 inválido: {exc}") from exc


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# -- transporte HTTP común (REQ-ARCH-65) ------------------------------------------


def ssl_context() -> ssl.SSLContext:
    """TLS con ``SSL_CERT_FILE`` si está definido; si no, el almacén del sistema (truststore)."""
    import ssl

    cafile = os.environ.get("SSL_CERT_FILE")
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    import truststore

    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def http_client(
    *, transport: httpx2.BaseTransport | None = None, timeout: float = TIMEOUT_S
) -> httpx2.Client:
    """Cliente HTTP de los sellos: timeout por fase, proxies del entorno y sin redirecciones.

    Con ``transport`` (las pruebas pasan un ``httpx2.MockTransport``) no se arma TLS ni
    se leen los proxies del entorno, que montarían otro transporte por encima.
    """
    import httpx2

    headers = {"User-Agent": f"{_meta.NAME}/{_meta.__version__}"}
    if transport is not None:
        return httpx2.Client(
            transport=transport, timeout=httpx2.Timeout(timeout), headers=headers, trust_env=False
        )
    return httpx2.Client(
        verify=ssl_context(), timeout=httpx2.Timeout(timeout), headers=headers, trust_env=True
    )


NetErrorKind = Literal["offline", "timeout", "tls", "http"]


def classify_http_error(exc: BaseException) -> NetErrorKind:
    """Clase de un error de red según REQ-ARCH-65: DNS, rechazo o timeout de conexión
    (``offline``), timeout de lectura (``timeout``), certificado (``tls``) o el resto
    (``http``)."""
    import ssl

    import httpx2

    seen: set[int] = set()
    cause: BaseException | None = exc
    while cause is not None and id(cause) not in seen:  # el error TLS viene encadenado
        seen.add(id(cause))
        if isinstance(cause, ssl.SSLCertVerificationError):
            return "tls"
        cause = cause.__cause__ or cause.__context__
    if isinstance(exc, (httpx2.ConnectTimeout, httpx2.ConnectError, httpx2.PoolTimeout)):
        return "offline"
    if isinstance(exc, httpx2.TimeoutException):
        return "timeout"
    return "http"


def read_limited(response: Any, limit: int) -> bytes:
    """Cuerpo de una respuesta en streaming, con un tope; ``ValueError`` si lo pasa."""
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_bytes():
        size += len(chunk)
        if size > limit:
            raise ValueError(f"respuesta de más de {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


# -- DER mínimo (solo longitudes definidas) ------------------------------------------


def der_tlv(data: bytes, pos: int, end: int | None = None) -> tuple[int, int, int]:
    """``(etiqueta, inicio del contenido, fin)`` del TLV DER en ``pos``; ``ValueError`` si
    no es válido (etiqueta de varios bytes, longitud indefinida o datos truncados)."""
    limit = len(data) if end is None else end
    if pos + 2 > limit:
        raise ValueError("TLV truncado")
    tag = data[pos]
    if tag & 0x1F == 0x1F:
        raise ValueError("etiqueta de varios bytes")
    first = data[pos + 1]
    start = pos + 2
    if first < 0x80:
        length = first
    else:
        count = first & 0x7F
        if count == 0 or count > 4 or start + count > limit:
            raise ValueError("longitud indefinida o inválida")
        length = int.from_bytes(data[start : start + count], "big")
        start += count
    if start + length > limit:
        raise ValueError("TLV truncado")
    return tag, start, start + length


def der_children(data: bytes, start: int, end: int) -> list[tuple[int, int, int, int]]:
    """``(etiqueta, inicio del TLV, inicio del contenido, fin)`` de cada hijo de un contenido."""
    out: list[tuple[int, int, int, int]] = []
    pos = start
    while pos < end:
        tag, body, stop = der_tlv(data, pos, end)
        out.append((tag, pos, body, stop))
        pos = stop
    return out


def der_encode(tag: int, content: bytes) -> bytes:
    """TLV DER con longitud mínima."""
    size = len(content)
    if size < 0x80:
        return bytes((tag, size)) + content
    raw = size.to_bytes((size.bit_length() + 7) // 8, "big")
    return bytes((tag, 0x80 | len(raw))) + raw + content


def entry_summary(entry: TsaEntry) -> Mapping[str, object]:
    """Datos públicos de una entrada, sin PEM (para resúmenes y ``VERIFY.md``)."""
    return {
        "id": entry.id,
        "name": entry.name,
        "url": entry.url,
        "policy_oids": list(entry.policy_oids),
        "fingerprint_source": entry.fingerprint_source,
        "seal": entry.seal,
        "roots": [{"subject": c.subject, "sha256": c.sha256, "not_after": c.not_after} for c in entry.roots],
    }
