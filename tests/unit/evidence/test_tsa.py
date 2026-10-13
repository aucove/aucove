# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Cliente RFC 3161 contra una TSA de prueba (``evidence.tsa``; 11-trazabilidad §8.4, REQ-CI-53).

La CA y la TSA se generan con ``cryptography`` (``tests/fixtures/evidence/tsa/fake_tsa.py``)
y responden por ``httpx2.MockTransport``: sin red y sin esperar los 15 s del reintento,
porque la espera se inyecta.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import ssl
import subprocess
import sys
import time

import httpx2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import ExtendedKeyUsageOID

pytest.importorskip("rfc3161_client", reason="falta el extra evidence (el CI lo instala)")

from aucove.evidence import tsa  # noqa: E402
from aucove.evidence.catalog import TOOL_MISSING, Catalog  # noqa: E402
from aucove.evidence.revocation import Revocation  # noqa: E402
from tests.conftest import REPO_ROOT  # noqa: E402
from tests.fixtures.evidence.tsa import fake_tsa as ft  # noqa: E402

UTC = dt.timezone.utc
DATA = b'{"schema":"manifiesto de prueba"}'
DIGEST = hashlib.sha256(DATA).digest()


class Sleeper:
    """Reloj inyectado: anota las esperas en vez de dormir."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture(scope="module")
def alpha() -> ft.Pki:
    return ft.make_pki("alpha")


@pytest.fixture(scope="module")
def beta() -> ft.Pki:
    return ft.make_pki("beta")


@pytest.fixture(scope="module")
def gamma() -> ft.Pki:
    return ft.make_pki("gamma")


def _good_routes(*pkis: ft.Pki, tsas: dict[str, ft.FakeTsa] | None = None) -> ft.Router:
    router = ft.Router()
    for pki in pkis:
        router.routes[pki.tsa_url] = (tsas or {}).get(pki.name) or ft.FakeTsa(pki)
        if pki.ocsp_url:
            router.routes[pki.ocsp_url] = ft.ocsp_response(pki)
        if pki.crl_url:
            router.routes[pki.crl_url] = ft.crl(pki)
    return router


def _stamp(catalog: Catalog, router: ft.Router, sleeper: Sleeper | None = None, **kwargs: object) -> tsa.TsaBatch:
    return tsa.stamp(DATA, catalog=catalog, transport=router.transport(), sleep=sleeper or Sleeper(), **kwargs)  # type: ignore[arg-type]


def _one(pki: ft.Pki, fake: ft.FakeTsa | None = None, **routes: bytes | int) -> tuple[tsa.TsaProof, ft.Router]:
    router = _good_routes(pki, tsas={pki.name: fake} if fake else None)
    for key, value in routes.items():
        router.routes[getattr(pki, f"{key}_url")] = value
    batch = _stamp(ft.catalog_for(pki.entry()), router)
    assert len(batch.proofs) == 1
    return batch.proofs[0], router


# -- sello verificado ------------------------------------------------------------------------


def test_stamp_verifies_and_keeps_tsr_chain_root_and_ocsp(alpha: ft.Pki) -> None:
    fake = ft.FakeTsa(alpha)
    proof, router = _one(alpha, fake)
    assert (proof.status, proof.reason, proof.error) == ("ok", "ok", None)
    assert proof.gen_time == ft.GEN_TIME
    assert proof.revocation is not None and proof.revocation.type == "ocsp"
    assert proof.file == "tsa/alpha.tsr"
    assert sorted(proof.files()) == ["tsa/alpha.chain.pem", "tsa/alpha.ocsp.der", "tsa/alpha.root.pem", "tsa/alpha.tsr"]
    assert x509.load_pem_x509_certificates(proof.chain_pem or b"") == [alpha.tsa, alpha.ca]
    assert proof.root_pem == alpha.entry()["roots"][0]["pem"].encode("ascii")  # type: ignore[index]
    # la petición: SHA-256 de los bytes, nonce de 64 bits y certReq
    [(digest, nonce, cert_req)] = fake.requests
    assert digest == DIGEST and cert_req is True
    assert nonce is not None and 0 <= nonce < 2**64
    assert len(router.calls) == 2 and router.calls[0] == alpha.tsa_url  # OCSP después; sin bajar la CRL
    assert router.calls[1].startswith(alpha.ocsp_url or "-")
    check = tsa.verify_token(
        proof.tsr or b"", DIGEST, source="alpha", catalog=ft.catalog_for(alpha.entry()),
        chain_pem=proof.chain_pem, revocation=proof.revocation, nonce=nonce,
    )
    assert (check.status, check.source, check.reason, check.time_utc) == ("pass", "alpha", "ok", ft.GEN_TIME)


def test_crl_is_captured_when_ocsp_does_not_answer(alpha: ft.Pki) -> None:
    proof, router = _one(alpha, ocsp=503)
    assert proof.status == "ok" and proof.revocation is not None and proof.revocation.type == "crl"
    assert proof.files()["tsa/alpha.crl.der"] == proof.revocation.der
    x509.load_der_x509_crl(proof.revocation.der)
    assert router.count(alpha.crl_url or "-") == 1
    check = tsa.verify_token(proof.tsr or b"", DIGEST, source="alpha", catalog=ft.catalog_for(alpha.entry()), revocation=proof.revocation)
    assert check.status == "pass"


def test_without_ocsp_nor_crl_the_stamp_is_kept_and_verifies_weak() -> None:
    bare = ft.make_pki("bare", ocsp=False, crl=False)
    proof, router = _one(bare)
    assert (proof.status, proof.reason, proof.revocation) == ("ok", "no_revocation", None)
    assert "no publica OCSP ni CRL" in (proof.note or "")
    assert "tsa/bare.tsr" in proof.files() and not any(k.endswith(".der") for k in proof.files())
    check = tsa.verify_token(proof.tsr or b"", DIGEST, source="bare", catalog=ft.catalog_for(bare.entry()))
    assert (check.status, check.reason) == ("weak", "no_revocation")


def test_unreachable_ocsp_and_crl_also_end_in_weak_not_fail(alpha: ft.Pki) -> None:
    router = ft.Router({alpha.tsa_url: ft.FakeTsa(alpha)})  # sin rutas de OCSP ni CRL: caídas
    proof = _stamp(ft.catalog_for(alpha.entry()), router).proofs[0]
    assert (proof.status, proof.revocation) == ("ok", None)
    assert "ocsp offline" in (proof.note or "") and "crl offline" in (proof.note or "")
    check = tsa.verify_token(proof.tsr or b"", DIGEST, source="alpha", catalog=ft.catalog_for(alpha.entry()))
    assert check.status == "weak"


# -- lo que se rechaza antes de guardar ---------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"alter_digest": True}, "digest_mismatch"),
        ({"alter_nonce": True}, "nonce_mismatch"),
        ({"policy": "2.999.9.9"}, "policy"),
        ({"status": 2}, "rejected"),
        ({"ess": "none"}, "ess"),
        ({"ess": "foreign"}, "ess"),
        ({"ess": "ca-first"}, "ess"),
    ],
)
def test_bad_responses_are_not_kept(alpha: ft.Pki, change: dict[str, object], reason: str) -> None:
    proof, _ = _one(alpha, ft.FakeTsa(alpha, **change))  # type: ignore[arg-type]
    assert (proof.status, proof.reason) == ("error", reason)
    assert proof.tsr is None and proof.files() == {} and proof.file is None
    assert proof.error is not None and proof.error.startswith(reason) and len(proof.error) <= 300


def test_altered_digest_or_nonce_fail_at_verification_too(alpha: ft.Pki) -> None:
    catalog = ft.catalog_for(alpha.entry())
    token = ft.FakeTsa(alpha).token(DIGEST, 42)
    assert tsa.verify_token(token, DIGEST, source="alpha", catalog=catalog, nonce=42).status == "weak"
    other = tsa.verify_token(token, hashlib.sha256(b"otro manifiesto").digest(), source="alpha", catalog=catalog)
    assert (other.status, other.reason, other.time_utc) == ("fail", "digest_mismatch", ft.GEN_TIME)
    assert tsa.verify_token(token, DIGEST, source="alpha", catalog=catalog, nonce=43).reason == "nonce_mismatch"
    assert tsa.verify_token(token, DIGEST, source="nadie", catalog=catalog).reason == "unknown_tsa"
    assert tsa.verify_token(b"\x30\x03\x02\x01\x00", DIGEST, source="alpha", catalog=catalog).reason == "invalid"


def test_a_foreign_pki_never_anchors_even_if_its_root_comes_in_chain_pem(alpha: ft.Pki) -> None:
    """El verificador de rfc3161-client confía en las intermedias: aquí no."""
    evil = ft.make_pki("evil")
    proof, _ = _one(alpha, ft.FakeTsa(alpha, signer=evil))
    assert (proof.status, proof.reason) == ("error", "chain")
    token = ft.FakeTsa(alpha, signer=evil).token(DIGEST, None)
    rogue_chain = b"".join(c.public_bytes(Encoding.PEM) for c in (evil.ca, evil.root))
    check = tsa.verify_token(token, DIGEST, source="alpha", catalog=ft.catalog_for(alpha.entry()), chain_pem=rogue_chain)
    assert (check.status, check.reason) == ("fail", "chain")


def test_malformed_certificates_in_the_token_are_invalid_not_a_crash(alpha: ft.Pki) -> None:
    proof, _ = _one(alpha, ft.FakeTsa(alpha, junk=ft.seq(ft.integer(1))))
    assert (proof.status, proof.reason) == ("error", "invalid")
    token = ft.FakeTsa(alpha, junk=ft.seq(ft.integer(1))).token(DIGEST, None)
    check = tsa.verify_token(token, DIGEST, source="alpha", catalog=ft.catalog_for(alpha.entry()))
    assert (check.status, check.reason) == ("fail", "invalid")


def test_tsa_certificate_without_a_critical_eku_is_rejected() -> None:
    lax = ft.make_pki("lax", eku_critical=False)
    proof, _ = _one(lax)
    assert (proof.status, proof.reason) == ("error", "chain")


def test_revoked_tsa_certificate_discards_the_stamp(alpha: ft.Pki) -> None:
    revoked = ft.ocsp_response(
        alpha, status="revoked", revoked_at=ft.GEN_TIME - dt.timedelta(days=1), reason=x509.ReasonFlags.key_compromise
    )
    proof, _ = _one(alpha, ocsp=revoked)
    assert (proof.status, proof.reason) == ("error", "revoked")
    assert "key_compromise" in (proof.error or "")


def test_certificate_expired_today_but_valid_at_gen_time_still_passes() -> None:
    """El sello vale en su ``genTime`` aunque el certificado de la TSA ya venció."""
    old = ft.make_pki(
        "old",
        root_valid=(dt.datetime(2019, 1, 1, tzinfo=UTC), dt.datetime(2040, 1, 1, tzinfo=UTC)),
        ca_valid=(dt.datetime(2019, 1, 1, tzinfo=UTC), dt.datetime(2040, 1, 1, tzinfo=UTC)),
        tsa_valid=(dt.datetime(2020, 1, 1, tzinfo=UTC), dt.datetime(2025, 1, 1, tzinfo=UTC)),
    )
    catalog = ft.catalog_for(old.entry())
    then = dt.datetime(2024, 6, 1, tzinfo=UTC)
    token = ft.FakeTsa(old, gen_time=then).token(DIGEST, None)
    ocsp = Revocation("ocsp", ft.ocsp_response(old, this_update=then, next_update=then + dt.timedelta(days=7)))
    check = tsa.verify_token(token, DIGEST, source="old", catalog=catalog, revocation=ocsp)
    assert (check.status, check.time_utc) == ("pass", then)
    late = ft.FakeTsa(old, gen_time=dt.datetime(2025, 6, 1, tzinfo=UTC)).token(DIGEST, None)
    assert tsa.verify_token(late, DIGEST, source="old", catalog=catalog).reason == "chain"


def test_chain_comes_from_the_catalog_when_the_token_brings_no_certificates(alpha: ft.Pki) -> None:
    proof, _ = _one(alpha, ft.FakeTsa(alpha, embed_tsa=False, embed_ca=False))
    assert proof.status == "ok"
    assert x509.load_pem_x509_certificates(proof.chain_pem or b"") == [alpha.tsa, alpha.ca]


def test_unsorted_certificate_sets_are_normalized_and_the_tsr_kept_as_received(alpha: ft.Pki) -> None:
    """Sectigo manda ``certificates`` sin el orden de DER; rfc3161-client exige DER."""
    raw = ft.FakeTsa(alpha, unsorted_certs=True).token(DIGEST, 7)
    normalized = tsa.normalize_token(raw)
    assert normalized != raw and len(normalized) == len(raw)
    assert tsa.normalize_token(normalized) == normalized
    proof, _ = _one(alpha, ft.FakeTsa(alpha, unsorted_certs=True))
    assert proof.status == "ok" and proof.tsr is not None and tsa.normalize_token(proof.tsr) != proof.tsr
    assert tsa.normalize_token(b"no es DER") == b"no es DER"


# -- ruta de certificación con candidatos hostiles (REQ-TRACE-88) ----------------------------


def _pem(certs: list[x509.Certificate]) -> bytes:
    return b"".join(c.public_bytes(Encoding.PEM) for c in certs)


def _hostile(k: int) -> tuple[ft.Pki, list[x509.Certificate]]:
    """r4: ``k`` «CA» autofirmadas con el mismo nombre y la misma clave, así que cada una firma
    a todas, y una TSA del atacante que cuelga de ellas; ninguna llega a una raíz del catálogo."""
    key = ft.new_key()
    valid = (ft.NOT_BEFORE, ft.NOT_AFTER)
    cas = [ft.make_cert("X CA", key, None, None, ca=True, valid=valid) for _ in range(k)]
    leaf_key = ft.new_key()
    leaf = ft.make_cert(
        "X TSA", leaf_key, cas[0], key, ca=False, valid=valid, eku=(ExtendedKeyUsageOID.TIME_STAMPING,)
    )
    return ft.Pki("x", cas[0], key, cas[0], key, leaf, leaf_key, None, None), cas


class CountingIssuedBy:
    """Cuenta las verificaciones de firma de la búsqueda de la ruta (``revocation.issued_by``)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        self._original = tsa._revocation.issued_by
        monkeypatch.setattr(tsa._revocation, "issued_by", self)

    def __call__(self, child: x509.Certificate, issuer: x509.Certificate) -> bool:
        self.calls += 1
        return bool(self._original(child, issuer))


def test_hostile_candidates_end_quickly_in_chain(alpha: ft.Pki, monkeypatch: pytest.MonkeyPatch) -> None:
    """Antes, unas K⁴ rutas (horas con K = 100); ahora, ``fail``/``chain`` en el
    presupuesto de verificaciones de firma, sin tocar los 32 candidatos del tope."""
    counter = CountingIssuedBy(monkeypatch)
    catalog = ft.catalog_for(alpha.entry())
    pki, cas = _hostile(24)  # 24 «CA», la TSA y 2 certificados del catálogo: 27 candidatos
    token = ft.FakeTsa(pki).token(DIGEST, None)
    start = time.perf_counter()
    check = tsa.verify_token(token, DIGEST, source="alpha", catalog=catalog, chain_pem=_pem(cas))
    assert time.perf_counter() - start < 1.0
    assert (check.status, check.reason) == ("fail", "chain")
    assert 0 < counter.calls <= tsa.MAX_SIGNATURE_CHECKS
    proof, _ = _one(alpha, ft.FakeTsa(alpha, signer=pki, extra_certs=tuple(ft.der(c) for c in cas[1:])))
    assert (proof.status, proof.reason) == ("error", "chain")  # al sellar, igual


def test_exhausted_certificates_are_not_explored_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin presupuesto que la corte, la búsqueda queda lineal en los candidatos: un certificado
    que no llegó a una raíz desde una posición no se vuelve a explorar desde esa ni desde una
    más honda."""
    monkeypatch.setattr(tsa, "MAX_SIGNATURE_CHECKS", 1_000_000)
    counter = CountingIssuedBy(monkeypatch)
    pki, cas = _hostile(30)
    root = ft.make_pki("ancla").root
    assert tsa.build_path(pki.tsa, cas, [root], ft.GEN_TIME) is None
    assert counter.calls <= 5 * len(cas)  # sin la memoria serían unos 30 · 29 · 28 · 27


def test_too_many_candidates_are_rejected_without_searching(alpha: ft.Pki, monkeypatch: pytest.MonkeyPatch) -> None:
    """Más de ``MAX_CANDIDATES`` certificados distintos (en el sello o en ``chain.pem``): ``chain`` sin buscar."""
    counter = CountingIssuedBy(monkeypatch)
    catalog = ft.catalog_for(alpha.entry())
    pki, cas = _hostile(tsa.MAX_CANDIDATES + 8)
    token = ft.FakeTsa(pki).token(DIGEST, None)
    check = tsa.verify_token(token, DIGEST, source="alpha", catalog=catalog, chain_pem=_pem(cas))
    assert (check.status, check.reason) == ("fail", "chain") and "chain.pem" in (check.detail or "")
    inside = ft.FakeTsa(pki, extra_certs=tuple(ft.der(c) for c in cas[1:])).token(DIGEST, None)
    check = tsa.verify_token(inside, DIGEST, source="alpha", catalog=catalog)
    assert (check.status, check.reason) == ("fail", "chain") and "candidatos" in (check.detail or "")
    assert tsa.build_path(pki.tsa, cas, [alpha.root], ft.GEN_TIME) is None
    assert counter.calls == 0


def test_repeated_and_impostor_candidates_still_find_the_real_chain(alpha: ft.Pki) -> None:
    """Los repetidos cuentan una vez (por su DER) y unas «CA» con el nombre de la real pero otra
    clave solo gastan presupuesto: la cadena real se encuentra igual."""
    catalog = ft.catalog_for(alpha.entry())
    token = ft.FakeTsa(alpha, embed_ca=False).token(DIGEST, None)
    impostors = [ft.make_cert("alpha CA", ft.new_key(), None, None, ca=True, valid=(ft.NOT_BEFORE, ft.NOT_AFTER))
                 for _ in range(20)]
    chain = _pem([*impostors, alpha.ca, alpha.ca, alpha.ca, alpha.tsa, alpha.tsa])
    check = tsa.verify_token(token, DIGEST, source="alpha", catalog=catalog, chain_pem=chain)
    assert (check.status, check.reason) == ("weak", "no_revocation")
    path = tsa.build_path(alpha.tsa, [*impostors, alpha.ca] * 3, [alpha.root], ft.GEN_TIME)
    assert path == [alpha.tsa, alpha.ca, alpha.root]


# -- disponibilidad: reintento, siguiente TSA y estado del registro --------------------------


def test_a_503_is_retried_once_after_waiting_15_seconds(alpha: ft.Pki) -> None:
    fake = ft.FakeTsa(alpha, failures=[503])
    router = _good_routes(alpha, tsas={"alpha": fake})
    sleeper = Sleeper()
    proof = _stamp(ft.catalog_for(alpha.entry()), router, sleeper).proofs[0]
    assert proof.status == "ok" and len(fake.requests) == 2 and sleeper.calls == [15.0]


@pytest.mark.parametrize(
    ("failures", "reason", "requests"),
    [
        ([400], "http", 1),
        ([httpx2.ReadTimeout, httpx2.ReadTimeout], "timeout", 2),
        ([httpx2.ConnectError, httpx2.ConnectError], "offline", 2),
        ([502, 502], "http", 2),
    ],
)
def test_failures_and_retries(alpha: ft.Pki, failures: list[ft.FailureSpec], reason: str, requests: int) -> None:
    fake = ft.FakeTsa(alpha, failures=list(failures))
    proof, _ = _one(alpha, fake)
    assert (proof.status, proof.reason) == ("error", reason)
    assert len(fake.requests) == requests


def test_tls_errors_are_not_retried(alpha: ft.Pki) -> None:
    calls: list[str] = []

    def broken_tls(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url))
        error = httpx2.ConnectError("tls", request=request)
        raise error from ssl.SSLCertVerificationError("certificate verify failed")

    sleeper = Sleeper()
    router = ft.Router({alpha.tsa_url: broken_tls})
    proof = _stamp(ft.catalog_for(alpha.entry()), router, sleeper).proofs[0]
    assert (proof.status, proof.reason) == ("error", "tls")
    assert len(calls) == 1 and sleeper.calls == []


def test_garbage_or_oversized_responses_are_invalid(alpha: ft.Pki) -> None:
    for body in (b"esto no es DER", b"\x30" + b"\x00" * (tsa.TSR_MAX_BYTES + 10)):
        router = ft.Router({alpha.tsa_url: body})
        proof = _stamp(ft.catalog_for(alpha.entry()), router).proofs[0]
        assert (proof.status, proof.reason) == ("error", "invalid")


def test_a_down_tsa_makes_the_next_one_in_the_catalog_be_used(alpha: ft.Pki, beta: ft.Pki, gamma: ft.Pki) -> None:
    catalog = ft.catalog_for(alpha.entry(), beta.entry(), gamma.entry())
    router = _good_routes(beta, gamma)  # alpha no tiene ruta: está caída
    sleeper = Sleeper()
    batch = _stamp(catalog, router, sleeper)
    assert [(p.source, p.status, p.reason) for p in batch.proofs] == [
        ("alpha", "error", "offline"), ("beta", "ok", "ok"), ("gamma", "ok", "ok"),
    ]
    assert batch.status == "complete" and [p.source for p in batch.ok] == ["beta", "gamma"]
    assert router.count(alpha.tsa_url) == 2 and sleeper.calls == [15.0]  # una solicitud y un reintento
    assert tsa.seal_status(len(batch.ok), True) == "complete"


def test_only_the_first_two_that_answer_are_asked(alpha: ft.Pki, beta: ft.Pki, gamma: ft.Pki) -> None:
    catalog = ft.catalog_for(alpha.entry(), beta.entry(), gamma.entry())
    router = _good_routes(alpha, beta, gamma)
    batch = _stamp(catalog, router)
    assert [p.source for p in batch.proofs] == ["alpha", "beta"]
    assert router.count(gamma.tsa_url) == 0
    assert router.count(alpha.tsa_url) == 1 and router.count(beta.tsa_url) == 1


def test_all_tsas_down_leaves_the_record_partial_with_ots(alpha: ft.Pki, beta: ft.Pki) -> None:
    batch = _stamp(ft.catalog_for(alpha.entry(), beta.entry()), ft.Router())
    assert batch.status == "failed" and batch.ok == ()
    assert [p.reason for p in batch.proofs] == ["offline", "offline"]
    assert tsa.seal_status(len(batch.ok), ots_ok=True) == "partial"  # queda OTS
    assert tsa.seal_status(len(batch.ok), ots_ok=False) == "failed"


def test_seal_status_table() -> None:
    assert tsa.seal_status(2, True) == "complete"
    assert tsa.seal_status(1, True) == "partial"
    assert tsa.seal_status(2, False) == "partial"
    assert tsa.seal_status(0, True) == "partial"
    assert tsa.seal_status(0, False) == "failed"
    assert tsa.seal_status(0, True, tsa_wanted=0) == "complete"  # entrega: solo OTS


def test_retired_tsas_are_never_contacted(alpha: ft.Pki, beta: ft.Pki) -> None:
    catalog = ft.catalog_for(alpha.entry(seal=False), beta.entry())
    router = _good_routes(alpha, beta)
    batch = _stamp(catalog, router)
    assert [p.source for p in batch.proofs] == ["beta"] and router.count(alpha.tsa_url) == 0
    assert batch.status == "partial"


def test_attempts_are_capped_so_the_record_keeps_at_most_four_proofs(alpha: ft.Pki, beta: ft.Pki, gamma: ft.Pki) -> None:
    delta = ft.make_pki("delta")
    catalog = ft.catalog_for(alpha.entry(), beta.entry(), gamma.entry(), delta.entry())
    batch = _stamp(catalog, _good_routes(delta))
    assert [p.source for p in batch.proofs] == ["alpha", "beta", "gamma"] and batch.ok == ()


def test_stamp_one_asks_a_single_tsa(alpha: ft.Pki) -> None:
    router = _good_routes(alpha)
    entry = ft.catalog_for(alpha.entry()).entries[0]
    proof = tsa.stamp_one(entry, DATA, transport=router.transport(), sleep=Sleeper())
    assert proof.status == "ok" and proof.source == "alpha"
    empty = tsa.stamp_one(entry, b"", transport=router.transport(), sleep=Sleeper())
    assert (empty.status, empty.reason) == ("error", "invalid")


# -- sin el extra (REQ-ARCH-99) ------------------------------------------------------------


def test_without_the_extra_every_entry_point_skips(alpha: ft.Pki, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "rfc3161_client", None)
    router = _good_routes(alpha)
    catalog = ft.catalog_for(alpha.entry())
    batch = _stamp(catalog, router)
    assert (batch.status, batch.proofs, batch.warnings) == ("skip", (), (TOOL_MISSING,))
    assert router.calls == []
    check = tsa.verify_token(b"", DIGEST, source="alpha", catalog=catalog)
    assert (check.status, check.warnings) == ("skip", (TOOL_MISSING,))
    proof = tsa.stamp_one(catalog.entries[0], DATA, transport=router.transport())
    assert (proof.status, proof.reason) == ("error", "missing_extra")


def test_the_module_imports_without_the_extra() -> None:
    code = (
        "import sys\n"
        "sys.modules['rfc3161_client'] = None\n"
        "sys.modules['opentimestamps'] = None\n"
        "from aucove.evidence import tsa, ots\n"
        "print(tsa.stamp(b'x').status, tsa.verify_token(b'', b'', source='freetsa').status, ots.stamp(b'x').status)\n"
    )
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "src"))
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert proc.stdout.decode().split() == ["skip", "skip", "skip"]
