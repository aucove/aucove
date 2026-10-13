# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Doble de pruebas de una TSA RFC 3161 con su PKI, su OCSP y su CRL (EV-05).

- ``make_pki`` genera con ``cryptography`` una raíz, una intermedia y el certificado de
  la TSA (claves P-256, ``timeStamping`` en una EKU crítica, OCSP en
  ``authorityInfoAccess`` y CRL en ``cRLDistributionPoints``), y ``catalog_for`` arma
  con ellas un catálogo de TSA como el del núcleo.
- ``FakeTsa`` responde peticiones RFC 3161 por ``httpx2.MockTransport`` con DER hecho a
  mano: ``TSTInfo``, ``SignedData`` de CMS y atributos firmados con
  ``signingCertificateV2``. Deja alterar la huella, el nonce, la política, el estado, el
  orden de los certificados, el atributo ESS o la PKI que firma, agregar certificados al
  SET y fallar por la red antes de responder.
- ``ocsp_response`` y ``crl`` arman respuestas de revocación firmadas por la emisora,
  por un respondedor delegado o por un extraño; ``Router`` reparte las URL del
  transporte falso y anota cada llamada.

Nada usa la red: las URL son de ``.test``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

import httpx2
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509 import ocsp
from cryptography.x509.oid import AuthorityInformationAccessOID, ExtendedKeyUsageOID, NameOID

from aucove.evidence.catalog import Catalog, der_children, der_tlv, parse_catalog

UTC = dt.timezone.utc
GEN_TIME = dt.datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)
NOT_BEFORE = dt.datetime(2026, 1, 1, tzinfo=UTC)
NOT_AFTER = dt.datetime(2036, 1, 1, tzinfo=UTC)
TSA_NOT_AFTER = dt.datetime(2030, 1, 1, tzinfo=UTC)
POLICY = "2.999.1.1"  # arco 2.999 de ejemplo (X.660)

SHA256_OID = "2.16.840.1.101.3.4.2.1"
_ECDSA_SHA256_OID = "1.2.840.10045.4.3.2"
_SIGNED_DATA_OID = "1.2.840.113549.1.7.2"
_TST_INFO_OID = "1.2.840.113549.1.9.16.1.4"
_CONTENT_TYPE_OID = "1.2.840.113549.1.9.3"
_MESSAGE_DIGEST_OID = "1.2.840.113549.1.9.4"
_SIGNING_CERT_V2_OID = "1.2.840.113549.1.9.16.2.47"

FailureSpec = int | type[httpx2.TransportError]
Handler = Callable[[httpx2.Request], httpx2.Response]


# -- DER mínimo para armar respuestas ----------------------------------------------------


def tlv(tag: int, content: bytes) -> bytes:
    size = len(content)
    if size < 0x80:
        return bytes((tag, size)) + content
    raw = size.to_bytes((size.bit_length() + 7) // 8, "big")
    return bytes((tag, 0x80 | len(raw))) + raw + content


def seq(*items: bytes) -> bytes:
    return tlv(0x30, b"".join(items))


def set_of(*items: bytes, ordered: bool = True) -> bytes:
    """SET OF en orden DER (``ordered=False`` lo invierte, como lo manda Sectigo)."""
    return tlv(0x31, b"".join(sorted(items, reverse=not ordered)))


def integer(value: int) -> bytes:
    size = max(1, (value.bit_length() + 8) // 8)
    return tlv(0x02, value.to_bytes(size, "big", signed=True))


def oid(dotted: str) -> bytes:
    arcs = [int(p) for p in dotted.split(".")]
    out = bytearray()
    for arc in (40 * arcs[0] + arcs[1], *arcs[2:]):  # el primero también en base 128 (2.999…)
        chunk = [arc & 0x7F]
        arc >>= 7
        while arc:
            chunk.append(0x80 | (arc & 0x7F))
            arc >>= 7
        out += bytes(reversed(chunk))
    return tlv(0x06, bytes(out))


def octets(value: bytes) -> bytes:
    return tlv(0x04, value)


def gentime(value: dt.datetime) -> bytes:
    return tlv(0x18, value.astimezone(UTC).strftime("%Y%m%d%H%M%SZ").encode("ascii"))


def algorithm(dotted: str, *, null: bool = False) -> bytes:
    return seq(oid(dotted), b"\x05\x00") if null else seq(oid(dotted))


def parse_request(der: bytes) -> tuple[bytes, bytes, int | None, bool]:
    """``(OID del hash, huella, nonce, certReq)`` de un ``TimeStampReq``."""
    _, body, end = der_tlv(der, 0)
    fields = der_children(der, body, end)
    imprint = der_children(der, fields[1][2], fields[1][3])
    alg = der_children(der, imprint[0][2], imprint[0][3])[0]
    hash_oid = der[alg[2] : alg[3]]
    digest = der[imprint[1][2] : imprint[1][3]]
    nonce: int | None = None
    cert_req = False
    for tag, _, start, stop in fields[2:]:
        if tag == 0x02:
            nonce = int.from_bytes(der[start:stop], "big", signed=True)
        elif tag == 0x01:
            cert_req = der[start:stop] != b"\x00"
    return hash_oid, digest, nonce, cert_req


# -- PKI de prueba -----------------------------------------------------------------------


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Test TSA"), x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def make_cert(
    common_name: str,
    key: ec.EllipticCurvePrivateKey,
    issuer: x509.Certificate | None,
    issuer_key: ec.EllipticCurvePrivateKey | None,
    *,
    ca: bool,
    valid: tuple[dt.datetime, dt.datetime],
    eku: Sequence[x509.ObjectIdentifier] = (),
    eku_critical: bool = True,
    ocsp_url: str | None = None,
    crl_url: str | None = None,
    path_length: int | None = None,
) -> x509.Certificate:
    """Certificado de prueba; autofirmado si ``issuer`` es ``None``."""
    subject = _name(common_name)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer.subject if issuer is not None else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(valid[0])
        .not_valid_after(valid[1])
        .add_extension(x509.BasicConstraints(ca=ca, path_length=path_length if ca else None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=not ca, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=ca, crl_sign=ca,
                encipher_only=False, decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )
    if eku:
        builder = builder.add_extension(x509.ExtendedKeyUsage(list(eku)), critical=eku_critical)
    if ocsp_url:
        builder = builder.add_extension(
            x509.AuthorityInformationAccess(
                [x509.AccessDescription(AuthorityInformationAccessOID.OCSP, x509.UniformResourceIdentifier(ocsp_url))]
            ),
            critical=False,
        )
    if crl_url:
        builder = builder.add_extension(
            x509.CRLDistributionPoints(
                [x509.DistributionPoint([x509.UniformResourceIdentifier(crl_url)], None, None, None)]
            ),
            critical=False,
        )
    return builder.sign(issuer_key if issuer_key is not None else key, hashes.SHA256())


def new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


@dataclass
class Pki:
    """Raíz → intermedia → TSA, con las URL de OCSP y CRL del certificado de la TSA."""

    name: str
    root: x509.Certificate
    root_key: ec.EllipticCurvePrivateKey
    ca: x509.Certificate
    ca_key: ec.EllipticCurvePrivateKey
    tsa: x509.Certificate
    tsa_key: ec.EllipticCurvePrivateKey
    ocsp_url: str | None
    crl_url: str | None

    @property
    def tsa_url(self) -> str:
        return f"https://tsa.{self.name}.test/tsr"

    def entry(self, *, seal: bool = True, policy: str = POLICY, tsa_id: str | None = None) -> dict[str, object]:
        """Entrada del catálogo con la raíz y los certificados de esta PKI."""
        return {
            "id": tsa_id or self.name,
            "name": f"Test {self.name}",
            "url": self.tsa_url,
            "policy_oids": [policy],
            "terms_url": f"https://tsa.{self.name}.test/terms",
            "fingerprint_source": f"https://tsa.{self.name}.test/root",
            "seal": seal,
            "roots": [cert_entry(self.root)],
            "certs": [cert_entry(self.ca), cert_entry(self.tsa)],
        }


def make_pki(
    name: str,
    *,
    tsa_valid: tuple[dt.datetime, dt.datetime] = (NOT_BEFORE, TSA_NOT_AFTER),
    ca_valid: tuple[dt.datetime, dt.datetime] = (NOT_BEFORE, NOT_AFTER),
    root_valid: tuple[dt.datetime, dt.datetime] = (NOT_BEFORE, NOT_AFTER),
    ocsp: bool = True,
    crl: bool = True,
    eku_critical: bool = True,
) -> Pki:
    root_key, ca_key, tsa_key = new_key(), new_key(), new_key()
    root = make_cert(f"{name} Root", root_key, None, None, ca=True, valid=root_valid)
    ca = make_cert(f"{name} CA", ca_key, root, root_key, ca=True, valid=ca_valid, path_length=0)
    ocsp_url = f"http://ocsp.{name}.test" if ocsp else None
    crl_url = f"http://crl.{name}.test/ca.crl" if crl else None
    tsa = make_cert(
        f"{name} TSA", tsa_key, ca, ca_key, ca=False, valid=tsa_valid,
        eku=(ExtendedKeyUsageOID.TIME_STAMPING,), eku_critical=eku_critical,
        ocsp_url=ocsp_url, crl_url=crl_url,
    )
    return Pki(name, root, root_key, ca, ca_key, tsa, tsa_key, ocsp_url, crl_url)


def cert_entry(cert: x509.Certificate) -> dict[str, str]:
    der = cert.public_bytes(serialization.Encoding.DER)
    return {
        "subject": cert.subject.rfc4514_string(),
        "sha256": hashlib.sha256(der).hexdigest(),
        "not_before": cert.not_valid_before_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "not_after": cert.not_valid_after_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pem": cert.public_bytes(serialization.Encoding.PEM).decode("ascii"),
    }


def catalog_for(*entries: Mapping[str, object]) -> Catalog:
    return parse_catalog(json.dumps({"version": 1, "tsa": list(entries)}))


def der(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.DER)


# -- TSA falsa ----------------------------------------------------------------------------


@dataclass
class FakeTsa:
    """TSA RFC 3161 para ``httpx2.MockTransport``; cada atributo cambia una cosa de la respuesta."""

    pki: Pki
    gen_time: dt.datetime = GEN_TIME
    policy: str = POLICY
    status: int = 0
    alter_digest: bool = False
    alter_nonce: bool = False
    unsorted_certs: bool = False
    embed_ca: bool = True
    embed_tsa: bool = True
    ess: str = "v2"  # "v2", "none", "foreign" (huella de otro certificado) o "ca-first"
    signer: Pki | None = None  # firma con la TSA de otra PKI
    junk: bytes | None = None  # un «certificado» más en el SET, tal cual (para DER mal formado)
    extra_certs: tuple[bytes, ...] = ()  # más certificados en el SET, en DER (candidatos hostiles)
    failures: list[FailureSpec] = field(default_factory=list)  # fallos antes de responder
    requests: list[tuple[bytes, int | None, bool]] = field(default_factory=list)
    serial: int = 1

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        body = request.read()
        hash_oid, digest, nonce, cert_req = parse_request(body)
        self.requests.append((digest, nonce, cert_req))
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, int):
                return httpx2.Response(failure)
            raise failure("falla simulada", request=request)
        assert hash_oid == oid(SHA256_OID)[2:], "la petición debe usar SHA-256"
        return httpx2.Response(
            200, headers={"Content-Type": "application/timestamp-reply"}, content=self.token(digest, nonce)
        )

    def token(self, digest: bytes, nonce: int | None) -> bytes:
        """``TimeStampResp`` DER para ``digest`` y ``nonce``, con las alteraciones pedidas."""
        if self.status != 0:
            return seq(seq(integer(self.status)))
        pki = self.signer or self.pki
        if self.alter_digest:
            digest = hashlib.sha256(b"otra cosa").digest()
        if self.alter_nonce and nonce is not None:
            nonce += 1
        fields = [
            integer(1),
            oid(self.policy),
            seq(algorithm(SHA256_OID, null=True), octets(digest)),
            integer(self.serial),
            gentime(self.gen_time),
        ]
        if nonce is not None:
            fields.append(integer(nonce))
        tst_info = seq(*fields)
        self.serial += 1
        attrs = [
            seq(oid(_CONTENT_TYPE_OID), set_of(oid(_TST_INFO_OID))),
            seq(oid(_MESSAGE_DIGEST_OID), set_of(octets(hashlib.sha256(tst_info).digest()))),
        ]
        ess_ids = {
            "v2": [der(pki.tsa)],
            "foreign": [b"certificado ajeno"],
            "ca-first": [der(pki.ca), der(pki.tsa)],
        }.get(self.ess)
        if ess_ids is not None:
            cert_ids = seq(*(seq(octets(hashlib.sha256(d).digest())) for d in ess_ids))
            attrs.append(seq(oid(_SIGNING_CERT_V2_OID), set_of(seq(cert_ids))))
        signed_attrs = set_of(*attrs)
        signature = pki.tsa_key.sign(signed_attrs, ec.ECDSA(hashes.SHA256()))
        _, attrs_body, attrs_end = der_tlv(signed_attrs, 0)
        signer_info = seq(
            integer(1),
            seq(pki.tsa.issuer.public_bytes(), integer(pki.tsa.serial_number)),
            algorithm(SHA256_OID),
            tlv(0xA0, signed_attrs[attrs_body:attrs_end]),
            algorithm(_ECDSA_SHA256_OID),
            octets(signature),
        )
        embedded = ([der(pki.tsa)] if self.embed_tsa else []) + ([der(pki.ca)] if self.embed_ca else [])
        embedded += self.extra_certs
        if self.junk is not None:
            embedded.append(self.junk)
        certs = b"".join(sorted(embedded, reverse=self.unsorted_certs))
        signed_data = [
            integer(3),
            set_of(algorithm(SHA256_OID)),
            seq(oid(_TST_INFO_OID), tlv(0xA0, octets(tst_info))),
        ]
        if certs:
            signed_data.append(tlv(0xA0, certs))
        signed_data.append(set_of(signer_info))
        token = seq(oid(_SIGNED_DATA_OID), tlv(0xA0, seq(*signed_data)))
        return seq(seq(integer(0)), token)


# -- OCSP y CRL ----------------------------------------------------------------------------


def ocsp_response(
    pki: Pki,
    *,
    status: str = "good",
    this_update: dt.datetime = GEN_TIME - dt.timedelta(hours=1),
    next_update: dt.datetime | None = GEN_TIME + dt.timedelta(days=7),
    revoked_at: dt.datetime | None = None,
    reason: x509.ReasonFlags | None = None,
    signer: str = "issuer",  # "issuer", "delegate" o "stranger"
    cert: x509.Certificate | None = None,
) -> bytes:
    """Respuesta OCSP firmada del certificado de la TSA (u otro ``cert`` de la intermedia)."""
    statuses = {"good": ocsp.OCSPCertStatus.GOOD, "revoked": ocsp.OCSPCertStatus.REVOKED, "unknown": ocsp.OCSPCertStatus.UNKNOWN}
    builder = ocsp.OCSPResponseBuilder().add_response(
        cert=cert or pki.tsa,
        issuer=pki.ca,
        algorithm=hashes.SHA1(),
        cert_status=statuses[status],
        this_update=this_update,
        next_update=next_update,
        revocation_time=revoked_at if status == "revoked" else None,
        revocation_reason=reason if status == "revoked" else None,
    )
    if signer == "issuer":
        return builder.responder_id(ocsp.OCSPResponderEncoding.HASH, pki.ca).sign(pki.ca_key, hashes.SHA256()).public_bytes(serialization.Encoding.DER)
    key = new_key()
    # producedAt es la hora real de la firma: el respondedor vale de sobra alrededor de ella
    wide = (dt.datetime(2000, 1, 1, tzinfo=UTC), dt.datetime(2099, 1, 1, tzinfo=UTC))
    if signer == "delegate":
        responder = make_cert(f"{pki.name} OCSP", key, pki.ca, pki.ca_key, ca=False, valid=wide, eku=(ExtendedKeyUsageOID.OCSP_SIGNING,))
    else:
        responder = make_cert("stranger OCSP", key, None, None, ca=False, valid=wide, eku=(ExtendedKeyUsageOID.OCSP_SIGNING,))
    response = builder.responder_id(ocsp.OCSPResponderEncoding.HASH, responder).certificates([responder]).sign(key, hashes.SHA256())
    return response.public_bytes(serialization.Encoding.DER)


def ocsp_unsuccessful() -> bytes:
    return ocsp.OCSPResponseBuilder.build_unsuccessful(ocsp.OCSPResponseStatus.TRY_LATER).public_bytes(serialization.Encoding.DER)


def crl(
    pki: Pki,
    *,
    revoked: Sequence[tuple[int, dt.datetime, x509.ReasonFlags | None]] = (),
    last_update: dt.datetime = GEN_TIME - dt.timedelta(days=1),
    next_update: dt.datetime = GEN_TIME + dt.timedelta(days=30),
    key: ec.EllipticCurvePrivateKey | None = None,
    pem: bool = False,
) -> bytes:
    """CRL de la intermedia (firmada con ``key`` si se da, para simular una falsa)."""
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(pki.ca.subject)
        .last_update(last_update)
        .next_update(next_update)
    )
    for serial, when, reason in revoked:
        entry = x509.RevokedCertificateBuilder().serial_number(serial).revocation_date(when)
        if reason is not None:
            entry = entry.add_extension(x509.CRLReason(reason), critical=False)
        builder = builder.add_revoked_certificate(entry.build())
    signed = builder.sign(key or pki.ca_key, hashes.SHA256())
    return signed.public_bytes(serialization.Encoding.PEM if pem else serialization.Encoding.DER)


# -- transporte -----------------------------------------------------------------------------


@dataclass
class Router:
    """Reparte las peticiones por prefijo de URL; sin ruta, falla como sin red."""

    routes: dict[str, Handler | int | bytes] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        url = str(request.url)
        self.calls.append(url)
        for prefix, target in self.routes.items():
            if url.startswith(prefix):
                if isinstance(target, int):
                    return httpx2.Response(target)
                if isinstance(target, bytes):
                    return httpx2.Response(200, content=target)
                return target(request)
        raise httpx2.ConnectError("sin ruta (red simulada caída)", request=request)

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self)

    def count(self, prefix: str) -> int:
        return sum(url.startswith(prefix) for url in self.calls)


def serve(pki: Pki, tsa: FakeTsa | None = None, *, ocsp_der: bytes | int | None = None, crl_der: bytes | int | None = None) -> Router:
    """Router con la TSA de ``pki`` y, si se dan, sus respuestas de OCSP y CRL."""
    router = Router()
    router.routes[pki.tsa_url] = tsa if tsa is not None else FakeTsa(pki)
    if ocsp_der is not None and pki.ocsp_url:
        router.routes[pki.ocsp_url] = ocsp_der
    if crl_der is not None and pki.crl_url:
        router.routes[pki.crl_url] = crl_der
    return router
