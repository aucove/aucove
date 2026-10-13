# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Cliente RFC 3161: petición, verificación antes de guardar y verificación posterior (11-trazabilidad §8.4).

Petición: SHA-256 de los bytes sellados, nonce de 64 bits y ``certReq`` (los arma
``rfc3161-client``), por POST ``application/timestamp-query`` con ``httpx2`` y
truststore, timeout de 15 s y un reintento a los 15 s (Sectigo pide 15 s entre
solicitudes) solo ante fallos de red, 429 o 5xx; un error TLS no se reintenta
(REQ-ARCH-65). :func:`stamp` recorre las entradas con ``seal: true`` del catálogo en
orden y se queda con las dos primeras que den un sello válido (REQ-CI-53).

Antes de guardar un ``.tsr`` se comprueban, en este orden, el estado ``granted``, la
huella (SHA-256 y el mismo valor), el nonce, la política del catálogo, la cadena hasta
una raíz del catálogo válida en ``genTime``, la firma CMS y el atributo ESS
``signingCertificate`` como lo exige ``openssl ts -verify`` (así el ``chain.pem`` y el
``root.pem`` guardados sirven para el comando de ``VERIFY.md``); después se captura el
OCSP o la CRL (:mod:`.revocation`). Un certificado revocado descarta el sello; sin OCSP
ni CRL el sello vale y la verificación posterior da ``weak`` (REQ-TRACE-80).

La ruta de certificación la arma este módulo y no ``rfc3161-client``: su verificador
mete raíces e intermedias en el mismo almacén de confianza, así que un autofirmado
colado como intermedia (por ejemplo, en el ``chain.pem`` de un registro) serviría de
ancla. Aquí los certificados del sello, del registro y del catálogo son candidatos no
confiables: la ruta se construye firma por firma, con cada certificado vigente en
``genTime``, hasta una raíz del catálogo, y solo esa ruta llega al verificador CMS
(REQ-TRACE-88). Como los candidatos pueden ser hostiles (muchas «CA» con el mismo nombre
y la misma clave hacen que todas firmen a todas), la búsqueda tiene topes: los
candidatos se cuentan sin repetir (por su DER) y con más de ``MAX_CANDIDATES`` el sello
se rechaza (``chain``); cada verificación de firma gasta una de las
``MAX_SIGNATURE_CHECKS`` de la búsqueda, y un certificado que ya no llevó a ninguna raíz
desde una posición no se vuelve a explorar desde esa posición ni desde una más honda.

Algunas TSA (Sectigo) mandan los SET OF de ``SignedData`` sin el orden de DER, y
``rfc3161-client``, que parsea DER estricto, los rechaza. :func:`normalize_token` los
ordena en memoria (no están firmados) y el ``.tsr`` se guarda tal como llegó, que es
lo que lee ``openssl ts -verify``.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from . import revocation as _revocation
from .catalog import (
    TIMEOUT_S,
    TOOL_MISSING,
    Catalog,
    Check,
    TsaEntry,
    classify_http_error,
    clip,
    der_children,
    der_encode,
    der_tlv,
    http_client,
    load_catalog,
    read_limited,
    skip_check,
)
from .revocation import Revocation

if TYPE_CHECKING:
    import httpx2
    from cryptography import x509

WANTED = 2  # TSA por registro de integración (D-45, REQ-CI-53)
MAX_ATTEMPTS = 3  # TSA intentadas por run: con OTS, el registro admite 4 pruebas
RETRIES = 1
RETRY_DELAY_S = 15.0
TSR_MAX_BYTES = 256 * 1024
MAX_PATH = 6  # certificados en la ruta, raíz incluida
MAX_CANDIDATES = 32  # certificados candidatos distintos (sello, chain.pem y catálogo); con más, chain
MAX_SIGNATURE_CHECKS = 64  # verificaciones de firma (issued_by) por ruta; una ruta real usa unas pocas
SHA256_OID = "2.16.840.1.101.3.4.2.1"
_PEM_BEGIN = b"-----BEGIN CERTIFICATE-----"
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_GRANTED = 0

ProofStatus = Literal["ok", "error"]
SealStatus = Literal["complete", "partial", "failed"]


@dataclass(frozen=True, slots=True)
class TsaProof:
    """Sello de una TSA del catálogo, tal como lo guarda ``ci seal`` (11-trazabilidad §8.5).

    Con ``status == "ok"``: ``tsr`` son los bytes exactos de la respuesta, ``chain_pem``
    la ruta validada sin la raíz (certificado de la TSA primero), ``root_pem`` la raíz
    del catálogo que la ancla (PEM canónico: para FreeTSA, el archivo que publica),
    ``gen_time`` el ``genTime`` en UTC y ``revocation`` el OCSP o la CRL (``None`` si no
    hubo; ``note`` dice por qué). Con ``error``: ``reason`` es un código estable y
    ``error`` una línea de 300 caracteres o menos.
    """

    source: str
    status: ProofStatus
    reason: str
    tsr: bytes | None = None
    chain_pem: bytes | None = None
    root_pem: bytes | None = None
    gen_time: datetime | None = None
    revocation: Revocation | None = None
    error: str | None = None
    note: str | None = None

    @property
    def file(self) -> str | None:
        """``tsa/<id>.tsr`` (campo ``file`` de la prueba en evidence-record/v1), o ``None``."""
        return f"tsa/{self.source}.tsr" if self.status == "ok" else None

    def files(self) -> dict[str, bytes]:
        """Archivos de la carpeta del registro: ``.tsr``, cadena, raíz y OCSP o CRL."""
        if self.status != "ok" or self.tsr is None or self.chain_pem is None or self.root_pem is None:
            return {}
        out = {
            f"tsa/{self.source}.tsr": self.tsr,
            f"tsa/{self.source}.chain.pem": self.chain_pem,
            f"tsa/{self.source}.root.pem": self.root_pem,
        }
        if self.revocation is not None:
            out[self.revocation.file_name(self.source)] = self.revocation.der
        return out


@dataclass(frozen=True, slots=True)
class TsaBatch:
    """Los sellos RFC 3161 de un registro, en el orden en que se pidieron."""

    proofs: tuple[TsaProof, ...]
    wanted: int = WANTED
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> tuple[TsaProof, ...]:
        return tuple(p for p in self.proofs if p.status == "ok")

    @property
    def status(self) -> Literal["complete", "partial", "failed", "skip"]:
        """``skip`` sin el extra; si no, ``complete``, ``partial`` o ``failed`` según los ``ok``."""
        if TOOL_MISSING in self.warnings and not self.proofs:
            return "skip"
        count = len(self.ok)
        if count >= self.wanted:
            return "complete"
        return "partial" if count else "failed"


def seal_status(tsa_ok: int, ots_ok: bool, *, tsa_wanted: int = WANTED) -> SealStatus:
    """``status`` de ``ci seal`` para un registro (REQ-CI-55).

    ``complete`` si están todas las pruebas pedidas (``tsa_wanted`` TSA y OTS; en una
    entrega, ``tsa_wanted=0``), ``failed`` si ninguna salió ``ok`` y ``partial`` en
    medio: con todas las TSA caídas queda OTS y el registro es ``partial``.
    """
    if tsa_ok <= 0 and not ots_ok:
        return "failed"
    if tsa_ok >= tsa_wanted and ots_ok:
        return "complete"
    return "partial"


class _TokenError(Exception):
    def __init__(self, reason: str, detail: str, gen_time: datetime | None = None) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = clip(detail)
        self.gen_time = gen_time


@dataclass(frozen=True, slots=True)
class _Token:
    gen_time: datetime
    path: tuple[x509.Certificate, ...]  # TSA, intermedias, raíz del catálogo
    policy: str


# -- pedir sellos ----------------------------------------------------------------------


def stamp(
    data: bytes,
    *,
    catalog: Catalog | None = None,
    wanted: int = WANTED,
    max_attempts: int = MAX_ATTEMPTS,
    transport: httpx2.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = TIMEOUT_S,
    retry_delay: float = RETRY_DELAY_S,
) -> TsaBatch:
    """Sella ``data`` (los bytes exactos del manifiesto) con las primeras ``wanted`` TSA
    del catálogo con ``seal: true`` que den un sello válido; una solicitud por TSA (más
    el reintento) y ``max_attempts`` TSA como mucho. ``transport`` y ``sleep`` se
    inyectan en las pruebas. Sin el extra: ningún sello y ``W_CONFIG_TOOL_MISSING``."""
    try:
        _modules()
    except ImportError:
        return TsaBatch((), wanted, (TOOL_MISSING,))
    entries = (catalog if catalog is not None else load_catalog()).sealing()
    proofs: list[TsaProof] = []
    with http_client(transport=transport, timeout=timeout) as client:
        for entry in entries:
            if sum(p.status == "ok" for p in proofs) >= wanted or len(proofs) >= max_attempts:
                break
            proofs.append(_stamp_with(client, entry, data, sleep=sleep, retry_delay=retry_delay))
    return TsaBatch(tuple(proofs), wanted)


def stamp_one(
    entry: TsaEntry,
    data: bytes,
    *,
    transport: httpx2.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = TIMEOUT_S,
    retry_delay: float = RETRY_DELAY_S,
) -> TsaProof:
    """Un sello de ``entry`` sobre ``data``, verificado antes de devolverlo."""
    try:
        _modules()
    except ImportError:
        return TsaProof(entry.id, "error", "missing_extra", error="missing_extra: falta el extra evidence")
    with http_client(transport=transport, timeout=timeout) as client:
        return _stamp_with(client, entry, data, sleep=sleep, retry_delay=retry_delay)


def _stamp_with(
    client: httpx2.Client,
    entry: TsaEntry,
    data: bytes,
    *,
    sleep: Callable[[float], None],
    retry_delay: float,
) -> TsaProof:
    rfc3161_client, _ = _modules()
    try:
        request = (
            rfc3161_client.TimestampRequestBuilder()
            .data(data)
            .hash_algorithm(rfc3161_client.HashAlgorithm.SHA256)
            .nonce(nonce=True)
            .cert_request(cert_request=True)
            .build()
        )
    except ValueError as exc:  # p. ej., datos vacíos
        return TsaProof(entry.id, "error", "invalid", error=clip(f"invalid: {exc}"))
    try:
        raw = _post(client, entry.url, request.as_bytes(), sleep=sleep, retry_delay=retry_delay)
        token = _check_token(raw, hashlib.sha256(data).digest(), entry, nonce=request.nonce, extra=())
    except _TokenError as exc:
        return TsaProof(entry.id, "error", exc.reason, gen_time=exc.gen_time, error=clip(f"{exc.reason}: {exc.detail}"))
    leaf, issuer = token.path[0], token.path[1]
    captured = _revocation.capture(leaf, issuer, at=token.gen_time, client=client)
    if captured.status == "revoked":
        detail = captured.detail or "certificado de la TSA revocado"
        return TsaProof(entry.id, "error", "revoked", gen_time=token.gen_time, error=clip(f"revoked: {detail}"))
    return TsaProof(
        entry.id,
        "ok",
        "ok" if captured.revocation is not None else captured.reason,
        tsr=raw,
        chain_pem=b"".join(_pem(c) for c in token.path[:-1]),
        root_pem=_pem(token.path[-1]),
        gen_time=token.gen_time,
        revocation=captured.revocation,
        note=None if captured.revocation is not None else captured.detail,
    )


def _post(
    client: httpx2.Client, url: str, body: bytes, *, sleep: Callable[[float], None], retry_delay: float
) -> bytes:
    """POST con un reintento ante fallos transitorios; ``_TokenError`` si no hay respuesta útil."""
    import httpx2

    headers = {"Content-Type": "application/timestamp-query", "Accept": "application/timestamp-reply"}
    last = _TokenError("http", "sin respuesta")
    for attempt in range(1 + RETRIES):
        if attempt:
            sleep(retry_delay)
        try:
            with client.stream("POST", url, content=body, headers=headers) as response:
                if response.status_code == 200:
                    return read_limited(response, TSR_MAX_BYTES)
                last = _TokenError("http", f"HTTP {response.status_code}")
                if response.status_code not in _RETRY_STATUS:
                    raise last
        except httpx2.HTTPError as exc:
            kind = classify_http_error(exc)
            last = _TokenError(kind, f"{kind} ({type(exc).__name__})")
            if kind == "tls":
                raise last from exc
        except ValueError as exc:  # respuesta demasiado grande
            raise _TokenError("invalid", str(exc)) from exc
    raise last


# -- verificar ---------------------------------------------------------------------------


def verify_token(
    tsr: bytes,
    digest: bytes,
    *,
    source: str,
    catalog: Catalog | None = None,
    chain_pem: bytes | str | None = None,
    revocation: Revocation | None = None,
    nonce: int | None = None,
) -> Check:
    """Comprueba un ``.tsr`` contra ``digest`` (SHA-256 del manifiesto) y la TSA ``source``
    del catálogo (05-operaciones §9.26): huella, política, cadena válida en ``genTime``
    hasta una raíz del catálogo (con ``chain_pem`` del registro y los ``certs`` del
    catálogo) y firma. ``pass`` si ``revocation`` muestra el certificado vigente en
    ``genTime``, ``weak`` si no hay OCSP ni CRL útiles y ``fail`` en otro caso. No usa el
    reloj: un certificado vencido hoy pero vigente en ``genTime`` sigue valiendo."""
    try:
        _modules()
    except ImportError:
        return skip_check(source)
    entry = (catalog if catalog is not None else load_catalog()).get(source)
    if entry is None:
        return Check("fail", source, "unknown_tsa", detail=f"TSA fuera del catálogo: {clip(source, 40)}")
    try:
        token = _check_token(tsr, digest, entry, nonce=nonce, extra=_pem_chain(chain_pem))
    except _TokenError as exc:
        return Check("fail", source, exc.reason, time_utc=exc.gen_time, detail=exc.detail)
    verdict = _revocation.evaluate(revocation, token.path[0], token.path[1], at=token.gen_time)
    if verdict.status == "pass":
        return Check("pass", source, "ok", time_utc=token.gen_time, detail=verdict.detail)
    if verdict.status == "fail":
        return Check("fail", source, verdict.reason, time_utc=token.gen_time, detail=verdict.detail)
    return Check("weak", source, verdict.reason, time_utc=token.gen_time, detail=verdict.detail)


def _check_token(
    raw: bytes, digest: bytes, entry: TsaEntry, *, nonce: int | None, extra: Sequence[x509.Certificate]
) -> _Token:
    """Verifica un sello; ``_TokenError`` con el motivo si algo no cuadra.

    La respuesta viene de la red o de un registro: cualquier error al leerla (un
    certificado o una extensión mal formados) es ``invalid``, nunca una excepción suelta.
    """
    try:
        return _check_token_unguarded(raw, digest, entry, nonce=nonce, extra=extra)
    except _TokenError:
        raise
    except Exception as exc:  # noqa: BLE001 - datos no confiables
        raise _TokenError("invalid", f"sello ilegible: {type(exc).__name__}: {exc}") from exc


def _check_token_unguarded(
    raw: bytes, digest: bytes, entry: TsaEntry, *, nonce: int | None, extra: Sequence[x509.Certificate]
) -> _Token:
    rfc3161_client, x509 = _modules()
    try:
        response = rfc3161_client.decode_timestamp_response(normalize_token(raw))
    except ValueError as exc:
        raise _TokenError("invalid", f"respuesta RFC 3161 ilegible: {exc}") from exc
    if response.status != _GRANTED:
        texts = " ".join(response.status_string)
        raise _TokenError("rejected", f"PKIStatus {response.status} {texts}".strip())
    info = response.tst_info
    gen_time = _utc(info.gen_time)
    imprint = info.message_imprint
    if imprint.hash_algorithm.dotted_string != SHA256_OID:
        raise _TokenError("digest_mismatch", "la huella sellada no es SHA-256", gen_time)
    if imprint.message != digest:
        raise _TokenError("digest_mismatch", "la huella sellada no es la del manifiesto", gen_time)
    if nonce is not None and info.nonce != nonce:
        raise _TokenError("nonce_mismatch", "el nonce de la respuesta no es el de la petición", gen_time)
    policy = info.policy.dotted_string
    if policy not in entry.policy_oids:
        raise _TokenError("policy", f"política {policy} fuera del catálogo", gen_time)
    signers = list(response.signed_data.signer_infos)
    if len(signers) != 1:
        raise _TokenError("invalid", f"{len(signers)} firmantes en el sello", gen_time)
    signer = signers[0]
    # Candidatos sin repetir, por su DER: los del sello (en orden DER), los de chain.pem y
    # los del catálogo. Los del sello se cuentan antes de leerlos.
    found: dict[bytes, Any] = dict.fromkeys(sorted(response.signed_data.certificates))  # DER -> certificado o None
    embedded = frozenset(found)
    for cert in [*extra, *(x509.load_pem_x509_certificate(c.pem.encode("ascii")) for c in entry.certs)]:
        found.setdefault(_der(cert), cert)
    if len(found) > MAX_CANDIDATES:
        raise _TokenError("chain", f"{len(found)} certificados candidatos (máximo {MAX_CANDIDATES})", gen_time)
    pool = [cert if cert is not None else x509.load_der_x509_certificate(der) for der, cert in found.items()]
    leaf = next((c for c in pool if c.issuer == signer.issuer and c.serial_number == signer.serial_number), None)
    if leaf is None:
        raise _TokenError("chain", "falta el certificado de la TSA", gen_time)
    roots = [x509.load_pem_x509_certificate(c.pem.encode("ascii")) for c in entry.roots]
    path = build_path(leaf, pool, roots, gen_time)
    if path is None:
        raise _TokenError("chain", "la cadena no llega a una raíz del catálogo vigente en genTime", gen_time)
    builder = rfc3161_client.VerifierBuilder().add_root_certificate(path[-1])
    for intermediate in path[1:-1]:
        builder = builder.add_intermediate_certificate(intermediate)
    if _der(leaf) not in embedded:
        builder = builder.tsa_certificate(leaf)
    try:
        builder.build().verify(response, digest)
    except (rfc3161_client.VerificationError, ValueError) as exc:
        raise _TokenError("signature", f"la firma no verifica: {exc}", gen_time) from exc
    problem = _ess_problem(raw, path)
    if problem is not None:
        raise _TokenError("ess", problem, gen_time)
    return _Token(gen_time, tuple(path), policy)


# OID (contenido DER) de los atributos ESS y de sus algoritmos de hash.
_SIGNING_CERTIFICATE = bytes.fromhex("2a864886f70d010910020c")  # 1.2.840.113549.1.9.16.2.12
_SIGNING_CERTIFICATE_V2 = bytes.fromhex("2a864886f70d010910022f")  # 1.2.840.113549.1.9.16.2.47
_ESS_HASHES = {
    bytes.fromhex("2b0e03021a"): "sha1",
    bytes.fromhex("608648016503040201"): "sha256",
    bytes.fromhex("608648016503040202"): "sha384",
    bytes.fromhex("608648016503040203"): "sha512",
}


def ess_cert_ids(raw: bytes) -> list[list[tuple[str, bytes]]]:
    """Huellas de ``signingCertificate`` (SHA-1, RFC 2634) y ``signingCertificateV2``
    (RFC 5816) del firmante, una lista por atributo y en orden; ``ValueError`` si el
    sello no tiene la forma esperada."""
    data = normalize_token(raw)
    _, body, end = der_tlv(data, 0)
    parts = der_children(data, body, end)
    token = der_children(data, parts[1][2], parts[1][3])
    signed_data = der_children(data, token[1][2], token[1][3])[0]
    signer_infos = der_children(data, signed_data[2], signed_data[3])[-1]
    signer = der_children(data, signer_infos[2], signer_infos[3])[0]
    attrs = next((c for c in der_children(data, signer[2], signer[3]) if c[0] == 0xA0), None)
    if attrs is None:
        return []
    found: list[list[tuple[str, bytes]]] = []
    for _, _, start, stop in der_children(data, attrs[2], attrs[3]):
        attr_type, values = der_children(data, start, stop)[:2]
        oid = data[attr_type[2] : attr_type[3]]
        if oid not in (_SIGNING_CERTIFICATE, _SIGNING_CERTIFICATE_V2):
            continue
        signing_certificate = der_children(data, values[2], values[3])[0]
        cert_ids = der_children(data, signing_certificate[2], signing_certificate[3])[0]
        ids: list[tuple[str, bytes]] = []
        for _, _, id_start, id_stop in der_children(data, cert_ids[2], cert_ids[3]):
            fields = der_children(data, id_start, id_stop)
            algorithm = "sha1" if oid == _SIGNING_CERTIFICATE else "sha256"  # DEFAULT de ESSCertIDv2
            if fields[0][0] == 0x30:  # hashAlgorithm explícito
                alg_oid = der_children(data, fields[0][2], fields[0][3])[0]
                algorithm = _ESS_HASHES.get(data[alg_oid[2] : alg_oid[3]], "")
                fields = fields[1:]
            if not algorithm or fields[0][0] != 0x04:
                raise ValueError("ESSCertID con un hash desconocido")
            ids.append((algorithm, data[fields[0][2] : fields[0][3]]))
        found.append(ids)
    return found


def _ess_problem(raw: bytes, path: Sequence[x509.Certificate]) -> str | None:
    """Comprueba los atributos ESS como ``openssl ts -verify``: el primero identifica el
    certificado de la TSA y todos los demás están en la ruta. Así, la cadena y la raíz
    que se guardan sirven para el comando de ``VERIFY.md``."""
    from cryptography.hazmat.primitives.serialization import Encoding

    try:
        lists = ess_cert_ids(raw)
    except (ValueError, IndexError):
        return "atributo signingCertificate ilegible"
    if not lists:
        return "falta el atributo signingCertificate (RFC 3161 §2.4.1)"
    ders = [c.public_bytes(Encoding.DER) for c in path]
    for ids in lists:
        if not ids:
            return "lista ESSCertID vacía"
        for index, (algorithm, value) in enumerate(ids):
            where = [k for k, der in enumerate(ders) if hashlib.new(algorithm, der).digest() == value]
            if not where:
                return "un certificado del atributo signingCertificate no está en la cadena"
            if (index == 0) != (0 in where):
                return "el atributo signingCertificate no identifica primero al certificado de la TSA"
    return None


# -- ruta de certificación ---------------------------------------------------------------


class _Exhausted(Exception):
    """Se gastaron las ``MAX_SIGNATURE_CHECKS`` verificaciones de firma de una ruta."""


def build_path(
    leaf: x509.Certificate,
    pool: Sequence[x509.Certificate],
    roots: Sequence[x509.Certificate],
    at: datetime,
) -> list[x509.Certificate] | None:
    """Ruta ``[TSA, intermedias…, raíz]`` vigente en ``at`` hasta una de ``roots``, o ``None``.

    ``pool`` son candidatos no confiables. Cada eslabón: el nombre del emisor y la firma
    verifican, el emisor es CA (``basicConstraints``, ``pathLenConstraint`` y, si está,
    ``keyCertSign``) y todos están vigentes en ``at``. La hoja tiene ``timeStamping``
    en una ``extendedKeyUsage`` crítica (RFC 3161 §2.3).

    Con candidatos hostiles la búsqueda no explota (REQ-TRACE-88): ``pool`` se cuenta sin
    repetir (por su DER) y con más de ``MAX_CANDIDATES`` no hay ruta; cada verificación
    de firma, la única operación cara, gasta una de ``MAX_SIGNATURE_CHECKS`` y, al
    agotarlas, no hay ruta; y un certificado que no llegó a ninguna raíz desde la
    posición ``k`` queda agotado para ``k`` y para las más hondas, donde los topes de
    ``pathLenConstraint`` y de ``MAX_PATH`` solo son más estrictos.
    """
    if not _valid_at(leaf, at) or not _is_tsa_certificate(leaf):
        return None
    leaf_der = _der(leaf)
    candidates: dict[bytes, x509.Certificate] = {}
    for cert in pool:
        candidates.setdefault(_der(cert), cert)
    if len(candidates) > MAX_CANDIDATES:
        return None
    candidates.pop(leaf_der, None)
    usable = [(der, cert) for der, cert in candidates.items() if _valid_at(cert, at)]
    anchors = [root for root in roots if _valid_at(root, at)]
    budget = [MAX_SIGNATURE_CHECKS]
    exhausted: dict[bytes, int] = {}  # DER -> la posición menos honda desde la que no se llegó a una raíz

    def issued(child: x509.Certificate, issuer: x509.Certificate) -> bool:
        if child.issuer != issuer.subject:
            return False  # sin firma que verificar: no gasta presupuesto
        if budget[0] <= 0:
            raise _Exhausted
        budget[0] -= 1
        return _revocation.issued_by(child, issuer)

    def extend(path: list[tuple[bytes, x509.Certificate]]) -> list[x509.Certificate] | None:
        der, current = path[-1]
        below = len(path) - 1  # posición de current: intermedias entre la hoja y el próximo emisor
        for root in anchors:
            if _can_issue(root, below) and issued(current, root):
                return [cert for _key, cert in path] + [root]
        if len(path) + 1 < MAX_PATH:
            on_path = {key for key, _cert in path}
            for cand_der, candidate in usable:
                if cand_der in on_path or exhausted.get(cand_der, MAX_PATH) <= len(path):
                    continue
                if _can_issue(candidate, below) and issued(current, candidate):
                    found = extend([*path, (cand_der, candidate)])
                    if found is not None:
                        return found
        exhausted[der] = min(exhausted.get(der, MAX_PATH), below)
        return None

    try:
        return extend([(leaf_der, leaf)])
    except _Exhausted:
        return None


def _valid_at(cert: x509.Certificate, at: datetime) -> bool:
    return cert.not_valid_before_utc <= at <= cert.not_valid_after_utc


def _can_issue(cert: x509.Certificate, intermediates_below: int) -> bool:
    from cryptography import x509

    try:
        constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound:
        return False
    if not constraints.ca:
        return False
    if constraints.path_length is not None and constraints.path_length < intermediates_below:
        return False
    try:
        usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound:
        return True
    return bool(usage.key_cert_sign)


def _is_tsa_certificate(cert: x509.Certificate) -> bool:
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID

    try:
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage)
    except x509.ExtensionNotFound:
        return False
    if not eku.critical or ExtendedKeyUsageOID.TIME_STAMPING not in eku.value:
        return False
    try:
        usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound:
        return True
    return bool(usage.digital_signature or usage.content_commitment)


# -- utilidades --------------------------------------------------------------------------


def normalize_token(raw: bytes) -> bytes:
    """``TimeStampResp`` con los SET OF no firmados de ``SignedData`` en orden DER.

    Ordena ``digestAlgorithms``, ``certificates``, ``crls`` y ``signerInfos``; el
    ``TSTInfo`` y los atributos firmados no cambian. Si la estructura no es la esperada
    (por ejemplo, una respuesta de rechazo sin sello), devuelve ``raw`` tal cual.
    """
    try:
        _, body, end = der_tlv(raw, 0)
        if end != len(raw):
            return raw
        parts = der_children(raw, body, end)  # PKIStatusInfo, TimeStampToken
        if len(parts) != 2 or parts[1][0] != 0x30:
            return raw
        token = der_children(raw, parts[1][2], parts[1][3])  # contentType, [0] content
        if len(token) != 2 or token[1][0] != 0xA0:
            return raw
        inner = der_children(raw, token[1][2], token[1][3])  # SignedData
        if len(inner) != 1 or inner[0][0] != 0x30:
            return raw
        pieces: list[bytes] = []
        for tag, pos, start, stop in der_children(raw, inner[0][2], inner[0][3]):
            if tag in _UNSIGNED_SETS:
                items = sorted(raw[p:q] for _, p, _, q in der_children(raw, start, stop))
                pieces.append(der_encode(tag, b"".join(items)))
            else:
                pieces.append(raw[pos:stop])
    except ValueError:
        return raw
    signed_data = der_encode(0x30, b"".join(pieces))
    content_info = der_encode(0x30, raw[token[0][1] : token[0][3]] + der_encode(0xA0, signed_data))
    return der_encode(0x30, raw[parts[0][1] : parts[0][3]] + content_info)


# SET digestAlgorithms y signerInfos, [0] certificates y [1] crls de SignedData.
_UNSIGNED_SETS = frozenset({0x31, 0xA0, 0xA1})


def _modules() -> tuple[Any, Any]:
    """``rfc3161_client`` y ``cryptography.x509``; ``ImportError`` sin el extra."""
    import rfc3161_client
    from cryptography import x509

    return rfc3161_client, x509


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _pem(cert: x509.Certificate) -> bytes:
    from cryptography.hazmat.primitives.serialization import Encoding

    return cert.public_bytes(Encoding.PEM)


def _pem_chain(chain_pem: bytes | str | None) -> list[x509.Certificate]:
    """Certificados de un ``chain.pem`` (lista vacía si falta o no se puede leer).

    Con más bloques que ``MAX_CANDIDATES`` el sello se rechaza (``chain``) sin leerlos: la
    ruta no se buscaría con tantos candidatos.
    """
    from cryptography import x509

    if not chain_pem:
        return []
    data = chain_pem.encode("ascii", "replace") if isinstance(chain_pem, str) else chain_pem
    blocks = data.count(_PEM_BEGIN)
    if blocks > MAX_CANDIDATES:
        raise _TokenError("chain", f"chain.pem trae {blocks} certificados (máximo {MAX_CANDIDATES})")
    try:
        return x509.load_pem_x509_certificates(data)
    except ValueError:
        return []


def _der(cert: x509.Certificate) -> bytes:
    from cryptography.hazmat.primitives.serialization import Encoding

    return cert.public_bytes(Encoding.DER)
