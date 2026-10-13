# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""OCSP y CRL del certificado de una TSA (``evidence.revocation``; 11-trazabilidad §8.4, REQ-TRACE-80).

Captura al sellar (OCSP primero; si no hay, la CRL) y juicio en ``genTime``: ``pass``,
``fail`` solo con una revocación que invalida el sello y ``weak`` cuando no hay datos
que sirvan, nunca ``fail`` por falta de OCSP o CRL.
"""

from __future__ import annotations

import datetime as dt
import sys

import pytest
from cryptography import x509

from aucove.evidence import revocation
from aucove.evidence.catalog import TOOL_MISSING
from aucove.evidence.revocation import Revocation, capture, evaluate
from tests.fixtures.evidence.tsa import fake_tsa as ft

AT = ft.GEN_TIME
DAY = dt.timedelta(days=1)


@pytest.fixture(scope="module")
def pki() -> ft.Pki:
    return ft.make_pki("rev")


def _ocsp(pki: ft.Pki, **kwargs: object) -> Revocation:
    return Revocation("ocsp", ft.ocsp_response(pki, **kwargs))  # type: ignore[arg-type]


def _crl(pki: ft.Pki, **kwargs: object) -> Revocation:
    return Revocation("crl", ft.crl(pki, **kwargs))  # type: ignore[arg-type]


def _judge(pki: ft.Pki, rev: Revocation | None, at: dt.datetime = AT) -> tuple[str, str]:
    check = evaluate(rev, ft.der(pki.tsa), ft.der(pki.ca), at=at)
    return check.status, check.reason


# -- juicio en genTime -------------------------------------------------------------------------


def test_good_ocsp_from_the_issuer_or_a_delegate_passes(pki: ft.Pki) -> None:
    assert _judge(pki, _ocsp(pki)) == ("pass", "ok")
    assert _judge(pki, _ocsp(pki, signer="delegate")) == ("pass", "ok")


def test_ocsp_signed_by_a_stranger_or_for_another_certificate_does_not_count(pki: ft.Pki) -> None:
    assert _judge(pki, _ocsp(pki, signer="stranger")) == ("weak", "ocsp_signature")
    other = ft.make_cert("otro", ft.new_key(), pki.ca, pki.ca_key, ca=False, valid=(ft.NOT_BEFORE, ft.NOT_AFTER))
    assert _judge(pki, _ocsp(pki, cert=other)) == ("weak", "ocsp_mismatch")


def test_unusable_ocsp_is_weak(pki: ft.Pki) -> None:
    assert _judge(pki, _ocsp(pki, status="unknown")) == ("weak", "ocsp_unknown")
    assert _judge(pki, Revocation("ocsp", ft.ocsp_unsuccessful())) == ("weak", "ocsp_status")
    assert _judge(pki, Revocation("ocsp", b"basura")) == ("weak", "ocsp_invalid")


def test_freshness_is_judged_against_gen_time(pki: ft.Pki) -> None:
    stale = _ocsp(pki, this_update=AT - 10 * DAY, next_update=AT - DAY)
    assert _judge(pki, stale) == ("weak", "stale")
    later = _ocsp(pki, this_update=AT + DAY, next_update=AT + 8 * DAY)  # capturado después de genTime
    assert _judge(pki, later) == ("pass", "ok")
    assert _judge(pki, _ocsp(pki, this_update=AT - DAY, next_update=None)) == ("weak", "stale")


@pytest.mark.parametrize(
    ("revoked_at", "reason", "expected"),
    [
        (AT - DAY, x509.ReasonFlags.superseded, ("fail", "revoked")),  # antes de genTime: siempre falla
        (AT + DAY, x509.ReasonFlags.superseded, ("pass", "revoked_later")),  # RFC 3161 §4
        (AT + DAY, x509.ReasonFlags.cessation_of_operation, ("pass", "revoked_later")),
        (AT + DAY, x509.ReasonFlags.key_compromise, ("fail", "revoked")),  # la clave ya no da fe
        (AT + DAY, None, ("fail", "revoked")),  # sin motivo, todos los sellos caen
    ],
)
def test_revocation_rules_of_rfc_3161(
    pki: ft.Pki, revoked_at: dt.datetime, reason: x509.ReasonFlags | None, expected: tuple[str, str]
) -> None:
    ocsp = _ocsp(pki, status="revoked", revoked_at=revoked_at, reason=reason, this_update=AT + 2 * DAY)
    assert _judge(pki, ocsp) == expected
    crl = _crl(pki, revoked=[(pki.tsa.serial_number, revoked_at, reason)], last_update=AT + 2 * DAY)
    assert _judge(pki, crl) == expected


def test_crl_without_the_serial_passes_and_a_foreign_or_stale_one_is_weak(pki: ft.Pki) -> None:
    assert _judge(pki, _crl(pki)) == ("pass", "ok")
    assert _judge(pki, _crl(pki, revoked=[(12345, AT - DAY, None)])) == ("pass", "ok")
    assert _judge(pki, _crl(pki, key=ft.new_key())) == ("weak", "crl_signature")
    assert _judge(pki, _crl(pki, last_update=AT - 30 * DAY, next_update=AT - DAY)) == ("weak", "stale")
    assert _judge(pki, Revocation("crl", b"basura")) == ("weak", "crl_invalid")


def test_no_revocation_data_is_weak_never_fail(pki: ft.Pki) -> None:
    assert _judge(pki, None) == ("weak", "no_revocation")


def test_status_issued_after_the_certificate_expired_is_weak_not_pass(pki: ft.Pki) -> None:
    """D3 (05 §9.26): pasado ``notAfter`` la emisora ya no tiene que mantener el estado del
    certificado (puede sacarlo de la CRL), así que un «vigente» de entonces no da ``pass``. Una
    revocación que invalida el sello sigue dando ``fail``."""
    late = pki.tsa.not_valid_after_utc + DAY
    assert late > AT
    assert _judge(pki, _ocsp(pki, this_update=late, next_update=late + 7 * DAY)) == ("weak", "after_expiry")
    assert _judge(pki, _crl(pki, last_update=late, next_update=late + 30 * DAY)) == ("weak", "after_expiry")
    benign = _ocsp(pki, status="revoked", revoked_at=AT + DAY, reason=x509.ReasonFlags.superseded, this_update=late)
    assert _judge(pki, benign) == ("weak", "after_expiry")  # antes del vencimiento sería revoked_later
    fatal = _crl(pki, revoked=[(pki.tsa.serial_number, AT - DAY, x509.ReasonFlags.key_compromise)], last_update=late,
                 next_update=late + 30 * DAY)
    assert _judge(pki, fatal) == ("fail", "revoked")
    on_the_day = pki.tsa.not_valid_after_utc  # thisUpdate igual a notAfter: todavía cubre
    assert _judge(pki, _ocsp(pki, this_update=on_the_day, next_update=on_the_day + DAY)) == ("pass", "ok")


def test_file_names_follow_the_record_layout() -> None:
    assert Revocation("ocsp", b"").file_name("freetsa") == "tsa/freetsa.ocsp.der"
    assert Revocation("crl", b"").file_name("sectigo") == "tsa/sectigo.crl.der"


# -- captura al sellar ----------------------------------------------------------------------------


def _capture(pki: ft.Pki, router: ft.Router) -> revocation.Capture:
    return capture(pki.tsa, pki.ca, at=AT, transport=router.transport())


def test_capture_prefers_ocsp_and_does_not_download_the_crl(pki: ft.Pki) -> None:
    router = ft.serve(pki, ocsp_der=ft.ocsp_response(pki), crl_der=ft.crl(pki))
    got = _capture(pki, router)
    assert (got.status, got.revocation and got.revocation.type) == ("ok", "ocsp")
    assert router.count(pki.crl_url or "-") == 0


@pytest.mark.parametrize("ocsp_answer", [503, "unsuccessful", "stranger", "stale", None])
def test_capture_falls_back_to_the_crl(pki: ft.Pki, ocsp_answer: object) -> None:
    answers: dict[object, bytes | int | None] = {
        503: 503,
        "unsuccessful": ft.ocsp_unsuccessful(),
        "stranger": ft.ocsp_response(pki, signer="stranger"),
        "stale": ft.ocsp_response(pki, this_update=AT - 9 * DAY, next_update=AT - DAY),
        None: None,  # sin ruta: el respondedor no contesta
    }
    router = ft.serve(pki, ocsp_der=answers[ocsp_answer], crl_der=ft.crl(pki))
    got = _capture(pki, router)
    assert (got.status, got.revocation and got.revocation.type) == ("ok", "crl")


def test_capture_converts_a_pem_crl_to_der(pki: ft.Pki) -> None:
    """FreeTSA sirve su CRL en PEM; en el registro va en DER."""
    router = ft.serve(pki, ocsp_der=503, crl_der=ft.crl(pki, pem=True))
    got = _capture(pki, router)
    assert got.status == "ok" and got.revocation is not None
    assert got.revocation.der.startswith(b"\x30")
    assert x509.load_der_x509_crl(got.revocation.der).issuer == pki.ca.subject


def test_capture_reports_revocation_and_keeps_the_evidence(pki: ft.Pki) -> None:
    revoked = ft.ocsp_response(pki, status="revoked", revoked_at=AT - DAY, reason=x509.ReasonFlags.key_compromise)
    got = _capture(pki, ft.serve(pki, ocsp_der=revoked))
    assert (got.status, got.reason) == ("revoked", "revoked")
    assert got.revocation is not None and got.revocation.type == "ocsp"


def test_capture_without_any_source_is_none(pki: ft.Pki) -> None:
    got = _capture(pki, ft.Router())
    assert (got.status, got.revocation, got.reason) == ("none", None, "no_revocation")
    assert got.detail == "ocsp offline; crl offline"
    bare = ft.make_pki("bare-rev", ocsp=False, crl=False)
    router = ft.Router()
    assert _capture(bare, router).status == "none" and router.calls == []


def test_only_http_urls_from_the_certificate_are_used(pki: ft.Pki) -> None:
    key = ft.new_key()
    cert = ft.make_cert(
        "ldap-only", key, pki.ca, pki.ca_key, ca=False, valid=(ft.NOT_BEFORE, ft.NOT_AFTER),
        ocsp_url="ldap://ocsp.rev.test", crl_url="file:///etc/passwd",
    )
    router = ft.Router()
    got = capture(cert, pki.ca, at=AT, transport=router.transport())
    assert got.status == "none" and router.calls == []


def test_without_cryptography_capture_and_evaluate_skip(pki: ft.Pki, monkeypatch: pytest.MonkeyPatch) -> None:
    cert, ca = ft.der(pki.tsa), ft.der(pki.ca)
    monkeypatch.setitem(sys.modules, "cryptography", None)
    got = capture(cert, ca, at=AT, transport=ft.Router().transport())
    assert (got.status, got.warnings) == ("skip", (TOOL_MISSING,))
    check = evaluate(None, cert, ca, at=AT)
    assert (check.status, check.warnings) == ("skip", (TOOL_MISSING,))
