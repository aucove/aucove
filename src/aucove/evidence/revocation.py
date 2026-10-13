# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Estado de revocación del certificado de una TSA: OCSP o, si no hay, CRL (11-trazabilidad §8.4).

Al sellar, :func:`capture` pide el OCSP a la URL de ``authorityInfoAccess`` del
certificado (``cryptography.x509.ocsp``, CertID con SHA-1 como en RFC 5019) y, si no
responde, no verifica o no cubre ``genTime``, baja la CRL de
``cRLDistributionPoints`` (DER o PEM; se guarda en DER). Se guarda solo una respuesta
que verifica: la firma es de la emisora o de un respondedor delegado con
``id-kp-OCSPSigning`` emitido por ella, y corresponde al certificado. Sin OCSP ni CRL
la prueba queda ``ok`` y :func:`evaluate` da ``weak``, nunca ``fail`` (REQ-TRACE-80).

:func:`evaluate` juzga la revocación en un instante (``genTime``): ``pass`` si muestra
el certificado vigente en ese momento (una respuesta posterior a ``genTime``, o una
cuya vigencia lo cubre), ``fail`` si estaba revocado, y ``weak`` si no hay datos o
no sirven. Una revocación posterior a ``genTime`` no invalida el sello si su motivo es
``unspecified``, ``affiliationChanged``, ``superseded`` o ``cessationOfOperation``; sin
motivo o con cualquier otro (``keyCompromise``…), sí (RFC 3161 §4). Un OCSP o una CRL
emitidos después del vencimiento del certificado (``thisUpdate`` posterior a
``notAfter``) dan ``weak`` (``after_expiry``) donde darían ``pass``: la emisora ya no
tiene que mantener el estado de un certificado vencido (05-operaciones §9.26).

Las URL salen de un certificado ya validado hasta una raíz del catálogo, y solo se
usan ``http`` y ``https``. ``cryptography`` se importa de forma perezosa (REQ-ARCH-99).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from .catalog import (
    TIMEOUT_S,
    TOOL_MISSING,
    Check,
    classify_http_error,
    clip,
    der_tlv,
    http_client,
    read_limited,
    skip_check,
)

if TYPE_CHECKING:
    import httpx2
    from cryptography import x509

RevocationType = Literal["ocsp", "crl"]
CaptureStatus = Literal["ok", "none", "revoked", "skip"]

OCSP_MAX_BYTES = 64 * 1024
CRL_MAX_BYTES = 4 * 1024 * 1024
MAX_URLS = 2  # por tipo; los certificados reales traen una

# Motivos con los que los sellos anteriores a la revocación siguen valiendo (RFC 3161 §4).
_BENIGN_REASONS = frozenset({"unspecified", "affiliation_changed", "superseded", "cessation_of_operation"})


@dataclass(frozen=True, slots=True)
class Revocation:
    """Respuesta OCSP o CRL capturada al sellar, en DER."""

    type: RevocationType
    der: bytes

    def file_name(self, source: str) -> str:
        """Ruta dentro de la carpeta del registro (``tsa/<id>.ocsp.der`` o ``.crl.der``)."""
        return f"tsa/{source}.{self.type}.der"


@dataclass(frozen=True, slots=True)
class Capture:
    """Resultado de :func:`capture`.

    ``status``: ``ok`` (``revocation`` muestra el certificado vigente en ``genTime``),
    ``none`` (sin OCSP ni CRL utilizables: la prueba vale y la verificación da
    ``weak``), ``revoked`` (estaba revocado: la prueba no se guarda) o ``skip`` (falta
    el extra).
    """

    status: CaptureStatus
    revocation: Revocation | None
    reason: str
    detail: str | None = None
    warnings: tuple[str, ...] = ()


def _load(cert: x509.Certificate | bytes) -> x509.Certificate:
    from cryptography import x509

    return cert if isinstance(cert, x509.Certificate) else x509.load_der_x509_certificate(cert)


def capture(
    cert: x509.Certificate | bytes,
    issuer: x509.Certificate | bytes,
    *,
    at: datetime,
    client: httpx2.Client | None = None,
    transport: httpx2.BaseTransport | None = None,
    timeout: float = TIMEOUT_S,
) -> Capture:
    """Captura el OCSP (o la CRL) del certificado ``cert`` emitido por ``issuer``.

    ``at`` es el ``genTime`` del sello. ``client`` reutiliza un cliente abierto; si no,
    se abre uno con ``transport`` (inyectable en las pruebas).
    """
    try:
        leaf, ca = _load(cert), _load(issuer)
    except ImportError:
        return Capture("skip", None, "missing_extra", "falta el extra evidence", (TOOL_MISSING,))
    if client is not None:
        return _capture(leaf, ca, at, client)
    with http_client(transport=transport, timeout=timeout) as own:
        return _capture(leaf, ca, at, own)


def _capture(leaf: x509.Certificate, ca: x509.Certificate, at: datetime, client: httpx2.Client) -> Capture:
    problems: list[str] = []
    for url in _ocsp_urls(leaf)[:MAX_URLS]:
        try:
            der = _fetch_ocsp(client, url, leaf, ca)
        except _FetchError as exc:
            problems.append(f"ocsp {exc}")
            continue
        verdict = _judge("ocsp", der, leaf, ca, at)
        if verdict.status == "pass":
            return Capture("ok", Revocation("ocsp", der), verdict.reason, verdict.detail)
        if verdict.status == "fail":
            return Capture("revoked", Revocation("ocsp", der), verdict.reason, verdict.detail)
        problems.append(f"ocsp {verdict.reason}")
    for url in _crl_urls(leaf)[:MAX_URLS]:
        try:
            der = _fetch_crl(client, url)
        except _FetchError as exc:
            problems.append(f"crl {exc}")
            continue
        verdict = _judge("crl", der, leaf, ca, at)
        if verdict.status == "pass":
            return Capture("ok", Revocation("crl", der), verdict.reason, verdict.detail)
        if verdict.status == "fail":
            return Capture("revoked", Revocation("crl", der), verdict.reason, verdict.detail)
        problems.append(f"crl {verdict.reason}")
    detail = "; ".join(problems) if problems else "el certificado no publica OCSP ni CRL"
    return Capture("none", None, "no_revocation", clip(detail))


def evaluate(
    revocation: Revocation | None,
    cert: x509.Certificate | bytes,
    issuer: x509.Certificate | bytes,
    *,
    at: datetime,
) -> Check:
    """Juzga la revocación de ``cert`` en ``at`` (``genTime``): ``pass``, ``weak`` o ``fail``."""
    try:
        leaf, ca = _load(cert), _load(issuer)
    except ImportError:
        return skip_check(None)
    if revocation is None:
        return Check("weak", None, "no_revocation", detail="sin OCSP ni CRL")
    return _judge(revocation.type, revocation.der, leaf, ca, at)


def _judge(kind: RevocationType, der: bytes, leaf: x509.Certificate, ca: x509.Certificate, at: datetime) -> Check:
    """OCSP o CRL en ``at``. Son datos de la red o de un registro: si algo no se puede
    leer, no sirven (``weak``), nunca una excepción suelta."""
    try:
        return _evaluate_ocsp(der, leaf, ca, at) if kind == "ocsp" else _evaluate_crl(der, leaf, ca, at)
    except Exception as exc:  # noqa: BLE001 - datos no confiables
        return Check("weak", None, f"{kind}_invalid", detail=clip(f"{kind.upper()} ilegible: {type(exc).__name__}"))


# -- URL de los certificados -------------------------------------------------------


def _http_urls(values: Sequence[str]) -> list[str]:
    return [v for v in values if v.startswith(("http://", "https://"))]


def _ocsp_urls(cert: x509.Certificate) -> list[str]:
    from cryptography import x509
    from cryptography.x509.oid import AuthorityInformationAccessOID

    try:
        aia = cert.extensions.get_extension_for_class(x509.AuthorityInformationAccess).value
    except x509.ExtensionNotFound:
        return []
    return _http_urls(
        [
            d.access_location.value
            for d in aia
            if d.access_method == AuthorityInformationAccessOID.OCSP
            and isinstance(d.access_location, x509.UniformResourceIdentifier)
        ]
    )


def _crl_urls(cert: x509.Certificate) -> list[str]:
    from cryptography import x509

    try:
        points = cert.extensions.get_extension_for_class(x509.CRLDistributionPoints).value
    except x509.ExtensionNotFound:
        return []
    urls: list[str] = []
    for point in points:
        for name in point.full_name or ():
            if isinstance(name, x509.UniformResourceIdentifier):
                urls.append(name.value)
    return _http_urls(urls)


# -- descarga ----------------------------------------------------------------------


class _FetchError(Exception):
    """Fallo de red o respuesta inútil al pedir OCSP o CRL (el texto va al resumen)."""


def _fetch_ocsp(client: httpx2.Client, url: str, cert: x509.Certificate, issuer: x509.Certificate) -> bytes:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.x509 import ocsp

    request = ocsp.OCSPRequestBuilder().add_certificate(cert, issuer, hashes.SHA1()).build()
    body = request.public_bytes(serialization.Encoding.DER)
    headers = {"Content-Type": "application/ocsp-request", "Accept": "application/ocsp-response"}
    return _fetch(client, "POST", url, body, headers, OCSP_MAX_BYTES)


def _fetch_crl(client: httpx2.Client, url: str) -> bytes:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    raw = _fetch(client, "GET", url, None, {"Accept": "application/pkix-crl"}, CRL_MAX_BYTES)
    try:
        # FreeTSA sirve la CRL en PEM aunque la anuncie como application/pkix-crl
        crl = x509.load_pem_x509_crl(raw) if raw.lstrip().startswith(b"-----BEGIN") else x509.load_der_x509_crl(raw)
    except ValueError as exc:
        raise _FetchError(f"CRL ilegible: {exc}") from exc
    return crl.public_bytes(serialization.Encoding.DER)


def _fetch(
    client: httpx2.Client, method: str, url: str, body: bytes | None, headers: dict[str, str], limit: int
) -> bytes:
    import httpx2

    try:
        with client.stream(method, url, content=body, headers=headers) as response:
            if response.status_code != 200:
                raise _FetchError(f"HTTP {response.status_code}")
            return read_limited(response, limit)
    except httpx2.HTTPError as exc:
        raise _FetchError(classify_http_error(exc)) from exc
    except ValueError as exc:
        raise _FetchError(str(exc)) from exc


# -- evaluación ----------------------------------------------------------------------


def _covers(at: datetime, this_update: datetime, next_update: datetime | None) -> bool:
    """La información de estado vale para ``at``: es posterior, o su vigencia lo cubre."""
    if this_update >= at:
        return True
    return next_update is not None and at <= next_update


def _after_expiry(this_update: datetime, cert: x509.Certificate) -> bool:
    """El OCSP o la CRL se emitieron después del vencimiento del certificado (``notAfter``)."""
    return this_update > cert.not_valid_after_utc


_AFTER_EXPIRY = "el OCSP o la CRL son posteriores al vencimiento del certificado"


def _revoked(revoked_at: datetime, reason: str | None, at: datetime, *, late: bool) -> Check:
    """Revocación de ``revoked_at`` con ``reason``; ``late``: la respuesta es posterior al
    vencimiento del certificado, así que una revocación benigna posterior no da ``pass``."""
    when = revoked_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    if revoked_at > at and reason in _BENIGN_REASONS:
        if late:
            return Check("weak", None, "after_expiry", detail=_AFTER_EXPIRY)
        return Check("pass", None, "revoked_later", detail=f"revocado después de genTime ({when}, {reason})")
    return Check("fail", None, "revoked", detail=f"certificado revocado el {when} (motivo: {reason or 'sin motivo'})")


def _evaluate_ocsp(der: bytes, cert: x509.Certificate, issuer: x509.Certificate, at: datetime) -> Check:
    from cryptography.x509 import ocsp

    try:
        response = ocsp.load_der_ocsp_response(der)
    except ValueError as exc:
        return Check("weak", None, "ocsp_invalid", detail=clip(f"OCSP ilegible: {exc}"))
    if response.response_status != ocsp.OCSPResponseStatus.SUCCESSFUL:
        return Check("weak", None, "ocsp_status", detail=f"OCSP {response.response_status.name}")
    single = _matching_single(response, cert, issuer)
    if single is None:
        return Check("weak", None, "ocsp_mismatch", detail="el OCSP no corresponde al certificado")
    if not _ocsp_signed_by_issuer_or_delegate(response, issuer):
        return Check("weak", None, "ocsp_signature", detail="la firma del OCSP no verifica")
    status = single.certificate_status
    late = _after_expiry(single.this_update_utc, cert)
    if status == ocsp.OCSPCertStatus.GOOD:
        if late:
            return Check("weak", None, "after_expiry", detail=_AFTER_EXPIRY)
        if _covers(at, single.this_update_utc, single.next_update_utc):
            return Check("pass", None, "ok", detail="OCSP: vigente en genTime")
        return Check("weak", None, "stale", detail="el OCSP no cubre genTime")
    if status == ocsp.OCSPCertStatus.REVOKED:
        reason = single.revocation_reason.name if single.revocation_reason is not None else None
        revoked_at = single.revocation_time_utc
        if revoked_at is None:  # pragma: no cover - cryptography siempre la da con REVOKED
            return Check("fail", None, "revoked", detail="certificado revocado")
        return _revoked(revoked_at, reason, at, late=late)
    return Check("weak", None, "ocsp_unknown", detail="el OCSP no conoce el certificado")


def _matching_single(response: Any, cert: x509.Certificate, issuer: x509.Certificate) -> Any:
    """La respuesta individual del certificado (número de serie y huellas de la emisora)."""
    name_der = issuer.subject.public_bytes()
    key_bits = subject_key_bits(issuer)
    for single in response.responses:
        if single.serial_number != cert.serial_number:
            continue
        try:
            name_hash = hashlib.new(single.hash_algorithm.name, name_der).digest()
            key_hash = hashlib.new(single.hash_algorithm.name, key_bits).digest()
        except ValueError:
            continue
        if single.issuer_name_hash == name_hash and single.issuer_key_hash == key_hash:
            return single
    return None


def subject_key_bits(cert: x509.Certificate) -> bytes:
    """Bits de ``subjectPublicKey`` sin el AlgorithmIdentifier: lo que hashea OCSP (RFC 6960 §4.1.1)."""
    from cryptography.hazmat.primitives import serialization

    spki = cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    # SubjectPublicKeyInfo ::= SEQUENCE { algorithm AlgorithmIdentifier, subjectPublicKey BIT STRING }
    _, body, end = der_tlv(spki, 0)
    _, _, algorithm_end = der_tlv(spki, body, end)
    _, bits, bits_end = der_tlv(spki, algorithm_end, end)
    return spki[bits + 1 : bits_end]  # sin el byte de bits sobrantes


def _ocsp_signed_by_issuer_or_delegate(response: Any, issuer: x509.Certificate) -> bool:
    """La firma del OCSP es de la emisora o de un respondedor delegado por ella (RFC 6960 §4.2.2.2)."""
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID

    def names_signer(candidate: x509.Certificate) -> bool:
        if response.responder_name is not None:
            return bool(response.responder_name == candidate.subject)
        return bool(response.responder_key_hash == hashlib.sha1(subject_key_bits(candidate)).digest())

    if names_signer(issuer):
        return _verify_signature(issuer, response)
    produced = response.produced_at_utc
    for candidate in response.certificates:
        if not names_signer(candidate):
            continue
        try:
            eku = candidate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        except x509.ExtensionNotFound:
            continue
        if ExtendedKeyUsageOID.OCSP_SIGNING not in eku:
            continue
        if not candidate.not_valid_before_utc <= produced <= candidate.not_valid_after_utc:
            continue
        if issued_by(candidate, issuer) and _verify_signature(candidate, response):
            return True
    return False


def issued_by(child: x509.Certificate, issuer: x509.Certificate) -> bool:
    """``child`` lleva el nombre de ``issuer`` como emisor y su firma verifica con su clave."""
    from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm

    if child.issuer != issuer.subject:
        return False
    try:
        child.verify_directly_issued_by(issuer)
    except (InvalidSignature, UnsupportedAlgorithm, ValueError, TypeError):
        return False
    return True


_RSA_PSS_OID = "1.2.840.113549.1.1.10"


def _verify_signature(signer: x509.Certificate, response: Any) -> bool:
    """Firma RSA (PKCS#1 v1.5 o PSS), ECDSA o EdDSA de la respuesta OCSP con la clave de ``signer``."""
    from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
    from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, padding, rsa

    key = signer.public_key()
    data = response.tbs_response_bytes
    try:
        if isinstance(key, (ed25519.Ed25519PublicKey, ed448.Ed448PublicKey)):
            key.verify(response.signature, data)
            return True
        algorithm = response.signature_hash_algorithm
        if isinstance(key, rsa.RSAPublicKey):
            if response.signature_algorithm_oid.dotted_string == _RSA_PSS_OID:
                pad: padding.AsymmetricPadding = padding.PSS(padding.MGF1(algorithm), padding.PSS.AUTO)
            else:
                pad = padding.PKCS1v15()
            key.verify(response.signature, data, pad, algorithm)
            return True
        if isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(response.signature, data, ec.ECDSA(algorithm))
            return True
    except (InvalidSignature, UnsupportedAlgorithm, ValueError, TypeError):
        return False
    return False


def _evaluate_crl(der: bytes, cert: x509.Certificate, issuer: x509.Certificate, at: datetime) -> Check:
    from cryptography import x509

    try:
        crl = x509.load_der_x509_crl(der)
    except ValueError as exc:
        return Check("weak", None, "crl_invalid", detail=clip(f"CRL ilegible: {exc}"))
    if crl.issuer != issuer.subject or not crl.is_signature_valid(issuer.public_key()):  # type: ignore[arg-type]
        return Check("weak", None, "crl_signature", detail="la CRL no es de la emisora o su firma no verifica")
    entry = crl.get_revoked_certificate_by_serial_number(cert.serial_number)
    late = _after_expiry(crl.last_update_utc, cert)
    if entry is None:
        if late:  # pasado el vencimiento, la emisora puede sacar el certificado de la CRL
            return Check("weak", None, "after_expiry", detail=_AFTER_EXPIRY)
        if _covers(at, crl.last_update_utc, crl.next_update_utc):
            return Check("pass", None, "ok", detail="CRL: vigente en genTime")
        return Check("weak", None, "stale", detail="la CRL no cubre genTime")
    try:
        reason: str | None = entry.extensions.get_extension_for_class(x509.CRLReason).value.reason.name
    except x509.ExtensionNotFound:
        reason = None
    return _revoked(entry.revocation_date_utc, reason, at, late=late)
