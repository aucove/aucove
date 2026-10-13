# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Respuestas reales de FreeTSA y Sectigo, verificadas sin red (``evidence.tsa``; 11 §8.4, §8.6).

Se grabaron una sola vez, el 2026-10-06, sobre el SHA-256 de un vector fijo (ningún dato
de una persona): ``tests/fixtures/evidence/tsa/recorded/`` guarda la petición (``.tsq``),
la respuesta tal como llegó (``.tsr``) y la revocación capturada. FreeTSA no respondió
el OCSP y dio la CRL; Sectigo dio OCSP y CRL. Se verifican en su ``genTime`` con el
catálogo del núcleo, también cuando sus certificados ya vencieron.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

from aucove.evidence import revocation as revocation_mod
from aucove.evidence import tsa
from aucove.evidence.catalog import load_catalog
from aucove.evidence.revocation import Revocation
from tests.conftest import FIXTURES
from tests.fixtures.evidence.tsa.fake_tsa import parse_request

rfc3161_client = pytest.importorskip("rfc3161_client", reason="falta el extra evidence (el CI lo instala)")

RECORDED = FIXTURES / "evidence" / "tsa" / "recorded"
VECTORS = json.loads((RECORDED / "vectors.json").read_text(encoding="utf-8"))
DIGEST = bytes.fromhex(VECTORS["sha256"])
UTC = dt.timezone.utc
FAR_FUTURE = dt.datetime(2045, 1, 1, tzinfo=UTC)  # después de que venzan todos sus certificados


def _tsr(tsa_id: str) -> bytes:
    return (RECORDED / f"{tsa_id}.tsr").read_bytes()


def _revocation(tsa_id: str, kind: str) -> Revocation:
    return Revocation(kind, (RECORDED / f"{tsa_id}.{kind}.der").read_bytes())  # type: ignore[arg-type]


def _gen_time(tsa_id: str) -> dt.datetime:
    return dt.datetime.strptime(VECTORS["tsa"][tsa_id]["gen_time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def test_the_vector_is_the_sha256_of_its_fixed_text() -> None:
    assert hashlib.sha256(VECTORS["data"].encode("ascii")).digest() == DIGEST


@pytest.mark.parametrize("tsa_id", ["freetsa", "sectigo"])
def test_recorded_requests_carry_sha256_a_64_bit_nonce_and_cert_req(tsa_id: str) -> None:
    hash_oid, digest, nonce, cert_req = parse_request((RECORDED / f"{tsa_id}.tsq").read_bytes())
    assert hash_oid == bytes.fromhex("608648016503040201")  # 2.16.840.1.101.3.4.2.1
    assert digest == DIGEST and cert_req is True
    assert nonce == VECTORS["tsa"][tsa_id]["nonce"] and 0 <= nonce < 2**64


@pytest.mark.parametrize(("tsa_id", "kind"), [("freetsa", "crl"), ("sectigo", "ocsp"), ("sectigo", "crl")])
def test_recorded_tokens_pass_with_their_revocation_data(tsa_id: str, kind: str) -> None:
    check = tsa.verify_token(
        _tsr(tsa_id), DIGEST, source=tsa_id, revocation=_revocation(tsa_id, kind), nonce=VECTORS["tsa"][tsa_id]["nonce"]
    )
    assert (check.status, check.source, check.reason) == ("pass", tsa_id, "ok")
    assert check.time_utc == _gen_time(tsa_id)


@pytest.mark.parametrize("tsa_id", ["freetsa", "sectigo"])
def test_recorded_tokens_without_revocation_data_are_weak(tsa_id: str) -> None:
    check = tsa.verify_token(_tsr(tsa_id), DIGEST, source=tsa_id)
    assert (check.status, check.reason) == ("weak", "no_revocation")


@pytest.mark.parametrize("tsa_id", ["freetsa", "sectigo"])
def test_recorded_tokens_fail_with_another_digest_nonce_or_tsa(tsa_id: str) -> None:
    other = "sectigo" if tsa_id == "freetsa" else "freetsa"
    assert tsa.verify_token(_tsr(tsa_id), b"\x00" * 32, source=tsa_id).reason == "digest_mismatch"
    nonce = VECTORS["tsa"][tsa_id]["nonce"] + 1
    assert tsa.verify_token(_tsr(tsa_id), DIGEST, source=tsa_id, nonce=nonce).reason == "nonce_mismatch"
    assert tsa.verify_token(_tsr(tsa_id), DIGEST, source=other).reason == "policy"
    flipped = bytearray(_tsr(tsa_id))
    flipped[-10] ^= 0x01  # un bit de la firma
    assert tsa.verify_token(bytes(flipped), DIGEST, source=tsa_id).status == "fail"


@pytest.mark.parametrize("tsa_id", ["freetsa", "sectigo"])
def test_recorded_tokens_still_verify_after_their_certificates_expire(
    tsa_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Como ``openssl ts -verify -attime <genTime>``: cuenta la hora de su época, no la de hoy.

    Con el reloj de los módulos en 2045 ya vencieron el certificado de FreeTSA (2040),
    el firmante de Sectigo (2037), su CA (2041) y la raíz USERTrust (2038).
    """

    class Future(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> Future:  # type: ignore[override]
            return cls.fromtimestamp(FAR_FUTURE.timestamp(), tz)

        @classmethod
        def utcnow(cls) -> Future:  # type: ignore[override]
            return cls.fromtimestamp(FAR_FUTURE.timestamp(), UTC).replace(tzinfo=None)

    monkeypatch.setattr(tsa, "datetime", Future)
    monkeypatch.setattr(revocation_mod, "datetime", Future)
    entry = load_catalog().get(tsa_id)
    assert entry is not None
    expired = [c for c in (*entry.roots, *entry.certs) if c.not_after < "2045"]
    assert expired, "el escenario necesita certificados vencidos en 2045"
    kind = VECTORS["tsa"][tsa_id]["revocation"]
    check = tsa.verify_token(_tsr(tsa_id), DIGEST, source=tsa_id, revocation=_revocation(tsa_id, kind))
    assert (check.status, check.time_utc) == ("pass", _gen_time(tsa_id))


def test_sectigo_certificate_set_is_not_der_until_normalized() -> None:
    raw = _tsr("sectigo")
    with pytest.raises(ValueError, match="InvalidSetOrdering"):
        rfc3161_client.decode_timestamp_response(raw)
    response = rfc3161_client.decode_timestamp_response(tsa.normalize_token(raw))
    assert response.tst_info.message_imprint.message == DIGEST
    assert tsa.normalize_token(_tsr("freetsa")) == _tsr("freetsa")  # FreeTSA ya manda DER


def test_ess_attributes_name_the_chain_kept_in_the_catalog() -> None:
    """Sectigo declara R37, R41 y la R46 firmada por USERTrust: por eso la raíz del
    catálogo es USERTrust y ``openssl ts -verify`` acepta ``chain.pem`` y ``root.pem``."""
    for tsa_id in ("freetsa", "sectigo"):
        entry = load_catalog().get(tsa_id)
        assert entry is not None
        certs = [x509.load_pem_x509_certificate(c.pem.encode("ascii")) for c in (*entry.certs, *entry.roots)]
        ders = [c.public_bytes(Encoding.DER) for c in certs]
        [ids] = tsa.ess_cert_ids(_tsr(tsa_id))
        for algorithm, value in ids:
            assert any(hashlib.new(algorithm, d).digest() == value for d in ders), tsa_id
    sectigo = tsa.ess_cert_ids(_tsr("sectigo"))[0]
    assert len(sectigo) == 3


def test_recorded_revocation_data_is_judged_at_gen_time() -> None:
    entry = load_catalog().get("sectigo")
    assert entry is not None
    by_subject = {c.subject.split(",")[0]: c.der() for c in entry.certs}
    signer = by_subject["CN=Sectigo Public Time Stamping Signer R37"]
    issuer = by_subject["CN=Sectigo Public Time Stamping CA R41"]
    ocsp = _revocation("sectigo", "ocsp")
    gen_time = _gen_time("sectigo")
    assert revocation_mod.evaluate(ocsp, signer, issuer, at=gen_time).status == "pass"
    # una respuesta posterior a genTime también muestra que estaba vigente
    assert revocation_mod.evaluate(ocsp, signer, issuer, at=dt.datetime(2026, 9, 1, tzinfo=UTC)).status == "pass"
    # pero no dice nada de un instante posterior a su nextUpdate (2026-10-12)
    late = revocation_mod.evaluate(ocsp, signer, issuer, at=dt.datetime(2026, 11, 1, tzinfo=UTC))
    assert (late.status, late.reason) == ("weak", "stale")
    # la respuesta de otro certificado no sirve
    other = revocation_mod.evaluate(ocsp, issuer, issuer, at=gen_time)
    assert (other.status, other.reason) == ("weak", "ocsp_mismatch")
