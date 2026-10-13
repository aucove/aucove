# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Cliente OpenTimestamps sin red (``evidence.ots``; 11-trazabilidad §8.4, 05 §9.26).

Los calendarios son falsos (``httpx2.MockTransport``). Las pruebas guardadas salen de
``tests/fixtures/evidence/ots/make_ots.py``, que las arma con la biblioteca
``opentimestamps`` a partir de una semilla fija: una completa, anclada en un bloque de
Bitcoin de altura fija; la misma recortada con una atestación pendiente en un calendario
de la lista blanca, y la respuesta con que ese calendario la completa en ``upgrade``.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from collections.abc import Sequence
from urllib.parse import urlsplit

import httpx2
import pytest

pytest.importorskip("opentimestamps", reason="falta el extra evidence (el CI lo instala)")

from opentimestamps.core.notary import PendingAttestation  # noqa: E402
from opentimestamps.core.op import OpAppend, OpSHA1, OpSHA256  # noqa: E402
from opentimestamps.core.serialize import BytesDeserializationContext, BytesSerializationContext  # noqa: E402
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp  # noqa: E402

from aucove.evidence import ots  # noqa: E402
from aucove.evidence.catalog import TOOL_MISSING  # noqa: E402
from aucove.evidence.ots import BitcoinAttestation  # noqa: E402
from tests.conftest import FIXTURES  # noqa: E402
from tests.fixtures.evidence.ots import make_ots  # noqa: E402
from tests.fixtures.evidence.tsa.fake_tsa import Router  # noqa: E402

OTS_DIR = FIXTURES / "evidence" / "ots"
VECTORS = json.loads((OTS_DIR / "vectors.json").read_text(encoding="utf-8"))
COMPLETE = (OTS_DIR / "complete.ots").read_bytes()
PENDING = (OTS_DIR / "pending.ots").read_bytes()
REPLY = (OTS_DIR / "calendar-reply.bin").read_bytes()
VECTOR_DIGEST = bytes.fromhex(VECTORS["digest_sha256"])
ALICE = VECTORS["calendar"]
UPGRADE_URL = f"{ALICE}/timestamp/{VECTORS['commitment']}"

DATA = b'{"schema":"manifiesto de prueba"}'
DIGEST = hashlib.sha256(DATA).digest()
NONCE = bytes(range(16))
# Cada agregador por defecto deja la atestación pendiente en uno de sus calendarios.
PENDING_AT = {
    "https://a.pool.opentimestamps.org": ALICE,
    "https://b.pool.opentimestamps.org": "https://bob.btc.calendar.opentimestamps.org",
    "https://a.pool.eternitywall.com": "https://finney.calendar.eternitywall.com",
    "https://ots.btc.catallaxy.com": "https://btc.calendar.catallaxy.com",
}
PENDING_AT_URL = {
    "a": "https://a.pool.opentimestamps.org",
    "b": "https://b.pool.opentimestamps.org",
    "c": "https://a.pool.eternitywall.com",
    "d": "https://ots.btc.catallaxy.com",
}


def _bytes(stamp: Timestamp | DetachedTimestampFile) -> bytes:
    ctx = BytesSerializationContext()
    stamp.serialize(ctx)
    return bytes(ctx.getbytes())


class FakeCalendar:
    """Agregador: a ``POST /digest`` responde ``append`` + ``sha256`` + atestación pendiente."""

    def __init__(self, pending_uri: str, tag: bytes) -> None:
        self.pending_uri = pending_uri
        self.tag = tag
        self.digests: list[bytes] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        assert request.method == "POST" and request.url.path == "/digest"
        assert request.headers["Accept"] == "application/vnd.opentimestamps.v1"
        digest = request.read()
        self.digests.append(digest)
        stamp = Timestamp(digest)
        leaf = stamp.ops.add(OpAppend(self.tag)).ops.add(OpSHA256())
        leaf.attestations.add(PendingAttestation(self.pending_uri))
        return httpx2.Response(200, content=_bytes(stamp))


def _calendars(**overrides: object) -> tuple[Router, dict[str, FakeCalendar]]:
    router = Router()
    fakes: dict[str, FakeCalendar] = {}
    for i, (url, pending) in enumerate(PENDING_AT.items()):
        fakes[url] = FakeCalendar(pending, bytes([i]) * 8)
        router.routes[url + "/digest"] = fakes[url]
    for key, target in overrides.items():
        router.routes[PENDING_AT_URL[key] + "/digest"] = target  # type: ignore[assignment]
    return router, fakes


# -- vectores generados -------------------------------------------------------------------------


def test_the_generator_reproduces_the_stored_vectors() -> None:
    vectors = make_ots.build()
    assert vectors.files() == {name: (OTS_DIR / name).read_bytes() for name in make_ots.FILES}
    assert make_ots.render_summary(vectors) == (OTS_DIR / "vectors.json").read_text(encoding="utf-8")
    assert vectors.digest == hashlib.sha256(VECTORS["data"].encode("ascii")).digest() == VECTOR_DIGEST
    for name, sha256 in VECTORS["files"].items():
        assert hashlib.sha256((OTS_DIR / name).read_bytes()).hexdigest() == sha256, name


# -- stamp ------------------------------------------------------------------------------------


def test_stamp_in_the_four_default_calendars_gives_a_pending_proof() -> None:
    router, fakes = _calendars()
    got = ots.stamp(DATA, transport=router.transport(), nonce=NONCE)
    assert (got.status, got.reason, got.error, got.file) == ("ok", "ok", None, "manifest.json.ots")
    assert got.calendars == ots.DEFAULT_CALENDARS
    # el calendario solo ve SHA-256(SHA-256(datos) || nonce), un valor opaco
    opaque = hashlib.sha256(DIGEST + NONCE).digest()
    assert all(fake.digests == [opaque] for fake in fakes.values())
    info = ots.read(got.ots or b"")
    assert (info.status, info.digest, info.complete) == ("ok", DIGEST, False)
    assert info.pending == tuple(sorted(PENDING_AT.values()))
    check = ots.verify(got.ots or b"", DIGEST)
    assert (check.status, check.source, check.reason) == ("pending", "calendars", "pending")
    again, _ = _calendars()
    assert ots.stamp(DATA, transport=again.transport(), nonce=NONCE).ots == got.ots  # determinista con el nonce
    detached = DetachedTimestampFile.deserialize(BytesDeserializationContext(got.ots or b""))
    assert detached.file_hash_op == OpSHA256() and detached.file_digest == DIGEST


def test_two_of_four_calendars_are_enough() -> None:
    router, _ = _calendars(b=503, d=b"esto no es una prueba")
    got = ots.stamp(DATA, transport=router.transport())
    assert got.status == "ok"
    assert got.calendars == ("https://a.pool.opentimestamps.org", "https://a.pool.eternitywall.com")
    assert len(ots.read(got.ots or b"").pending) == 2


def test_one_calendar_is_not_enough() -> None:
    router, _ = _calendars(b=503, c=503, d=503)
    got = ots.stamp(DATA, transport=router.transport())
    assert (got.status, got.reason, got.ots, got.file) == ("error", "calendars", None, None)
    assert got.error is not None and "respondieron 1 de 4" in got.error and "HTTP 503" in got.error


def test_answers_without_a_usable_attestation_do_not_count() -> None:
    def foreign(request: httpx2.Request) -> httpx2.Response:
        stamp = Timestamp(request.read())
        stamp.attestations.add(PendingAttestation("https://calendar.evil.example"))
        return httpx2.Response(200, content=_bytes(stamp))

    def trailing(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=FakeCalendar(ALICE, b"x" * 8)(request).content + b"\x00")

    router, _ = _calendars(b=foreign, c=trailing)
    router.routes.pop(PENDING_AT_URL["d"] + "/digest")  # sin ruta: caído
    got = ots.stamp(DATA, transport=router.transport())
    assert got.status == "error" and got.calendars == ("https://a.pool.opentimestamps.org",)
    assert "sin atestación utilizable" in (got.error or "") and "ilegible" in (got.error or "")


def test_minimum_and_calendars_can_be_chosen() -> None:
    router, _ = _calendars()
    got = ots.stamp(DATA, calendars=("https://a.pool.opentimestamps.org",), minimum=1, transport=router.transport())
    assert got.status == "ok" and router.calls == ["https://a.pool.opentimestamps.org/digest"]


# -- lectura y verificación ---------------------------------------------------------------------


def test_a_complete_proof_reads_its_block_and_stays_pending() -> None:
    """v0.1 no coteja el bloque con un nodo y cualquiera puede escribir una atestación: nunca
    ``pass`` ni ``weak`` (05 §9.26). El detalle da la altura y la raíz para cotejarlas a mano."""
    info = ots.read(COMPLETE)
    assert (info.status, info.digest, info.pending, info.other) == ("ok", VECTOR_DIGEST, (), 0)
    assert info.bitcoin == (BitcoinAttestation(VECTORS["bitcoin_block"], VECTORS["merkle_root"]),)
    assert info.complete
    check = ots.verify(COMPLETE, VECTOR_DIGEST)
    assert (check.status, check.source, check.reason, check.time_utc) == ("pending", "bitcoin", "bitcoin_unverified", None)
    assert str(VECTORS["bitcoin_block"]) in (check.detail or "") and VECTORS["merkle_root"] in (check.detail or "")


def test_a_pending_proof_reads_its_calendar_and_verifies_pending() -> None:
    info = ots.read(PENDING)
    assert (info.status, info.digest, info.pending, info.bitcoin) == ("ok", VECTOR_DIGEST, (ALICE,), ())
    assert not info.complete
    check = ots.verify(PENDING, VECTOR_DIGEST)
    assert (check.status, check.source, check.reason) == ("pending", "calendars", "pending")


def test_wrong_digest_garbage_or_another_hash_fail() -> None:
    assert ots.verify(COMPLETE, DIGEST).reason == "digest_mismatch"
    garbage = ots.verify(b"no es una prueba", VECTOR_DIGEST)
    assert (garbage.status, garbage.reason) == ("fail", "invalid")
    assert ots.verify(COMPLETE[:-3], VECTOR_DIGEST).reason == "invalid"
    sha1 = DetachedTimestampFile(OpSHA1(), Timestamp(hashlib.sha1(DATA).digest()))
    sha1.timestamp.attestations.add(PendingAttestation(ALICE))
    assert ots.verify(_bytes(sha1), DIGEST).reason == "invalid"


# -- upgrade --------------------------------------------------------------------------------------


def test_upgrade_completes_the_pending_proof_with_the_calendar_reply() -> None:
    assert ots.verify(PENDING, VECTOR_DIGEST).status == "pending"
    router = Router({UPGRADE_URL: REPLY})
    got = ots.upgrade(PENDING, transport=router.transport())
    assert (got.status, got.reason, got.changed, got.warnings) == ("complete", "ok", True, ())
    assert router.calls == [UPGRADE_URL]
    info = ots.read(got.ots)
    assert info.bitcoin == ots.read(COMPLETE).bitcoin and info.pending == (ALICE,)
    check = ots.verify(got.ots, VECTOR_DIGEST)
    assert (check.status, check.source, check.reason) == ("pending", "bitcoin", "bitcoin_unverified")
    # la prueba resultante es la completa más la atestación pendiente, que se conserva
    with_pending, _ = make_ots.cut(COMPLETE, make_ots.CALENDAR_DEPTH, ALICE)
    merged = DetachedTimestampFile.deserialize(BytesDeserializationContext(with_pending))
    node = merged.timestamp
    for _ in range(make_ots.CALENDAR_DEPTH):
        [node] = node.ops.values()
    node.merge(Timestamp.deserialize(BytesDeserializationContext(REPLY), node.msg))
    assert got.ots == _bytes(merged)
    # una segunda pasada no pide nada: ya hay una atestación de Bitcoin
    router = Router({UPGRADE_URL: REPLY})
    again = ots.upgrade(got.ots, transport=router.transport())
    assert (again.status, again.changed, again.ots) == ("complete", False, got.ots) and router.calls == []


def test_upgrade_while_the_calendar_has_no_block_yet() -> None:
    router = Router({ALICE: 404})
    got = ots.upgrade(PENDING, transport=router.transport())
    assert (got.status, got.changed, got.ots) == ("pending", False, PENDING)
    assert router.calls == [UPGRADE_URL]


def test_upgrade_never_contacts_calendars_outside_the_whitelist() -> None:
    foreign, _ = make_ots.cut(COMPLETE, make_ots.CALENDAR_DEPTH, "https://calendar.evil.example")
    router = Router({"https://calendar.evil.example": REPLY})
    got = ots.upgrade(foreign, transport=router.transport())
    assert (got.status, got.changed) == ("pending", False) and router.calls == []
    assert "fuera de la lista blanca" in (got.detail or "")


def test_upgrade_offline_or_with_garbage_keeps_the_proof() -> None:
    offline = ots.upgrade(PENDING, transport=Router().transport())
    assert (offline.status, offline.ots, offline.warnings) == ("pending", PENDING, ("W_NET_OFFLINE",))
    garbage = ots.upgrade(PENDING, transport=Router({ALICE: b"\xff\xff"}).transport())
    assert (garbage.status, garbage.changed, garbage.warnings) == ("pending", False, ())
    assert "ilegible" in (garbage.detail or "")
    broken = ots.upgrade(b"no es una prueba", transport=Router().transport())
    assert (broken.status, broken.reason) == ("error", "invalid")


def test_a_complete_proof_needs_no_network() -> None:
    router = Router()
    got = ots.upgrade(COMPLETE, transport=router.transport())
    assert (got.status, got.changed, got.ots) == ("complete", False, COMPLETE) and router.calls == []


# -- topes de upgrade: la prueba puede ser ajena (05 §9.26) ------------------------------------------


BOB = "https://bob.btc.calendar.opentimestamps.org"


def _with_pending(pending: Sequence[tuple[str, bytes]]) -> bytes:
    """Prueba de ``VECTOR_DIGEST`` con una atestación pendiente por ``(calendario, etiqueta)``:
    cada etiqueta da otra rama, con otro mensaje."""
    detached = DetachedTimestampFile(OpSHA256(), Timestamp(VECTOR_DIGEST))
    for uri, tag in pending:
        detached.timestamp.ops.add(OpAppend(tag)).ops.add(OpSHA256()).attestations.add(PendingAttestation(uri))
    return _bytes(detached)


def test_upgrade_stops_at_the_first_bitcoin_attestation() -> None:
    detached = DetachedTimestampFile.deserialize(BytesDeserializationContext(PENDING))
    node = detached.timestamp
    for _ in range(make_ots.CALENDAR_DEPTH):
        [node] = node.ops.values()
    node.attestations.add(PendingAttestation(BOB))  # otro calendario, que va después de ALICE
    router = Router({UPGRADE_URL: REPLY, BOB: 404})
    got = ots.upgrade(_bytes(detached), transport=router.transport())
    assert (got.status, got.changed) == ("complete", True) and router.calls == [UPGRADE_URL]


def test_a_shared_budget_asks_each_pair_once() -> None:
    """Las copias de un mismo registro traen la misma prueba: en una corrida se pide una vez,
    también cuando el calendario no respondió."""
    budget, router = ots.Budget(), Router({ALICE: 404})
    for _ in range(3):
        assert ots.upgrade(PENDING, transport=router.transport(), budget=budget).status == "pending"
    assert router.calls == [UPGRADE_URL] and budget.requests == 1
    budget, router = ots.Budget(), Router()  # sin rutas: la red simulada caída
    for _ in range(2):
        assert ots.upgrade(PENDING, transport=router.transport(), budget=budget).warnings == ("W_NET_OFFLINE",)
    assert router.calls == [UPGRADE_URL]


def test_upgrade_requests_are_capped_per_calendar_and_per_run() -> None:
    """r3: una prueba ajena con cientos de pendientes no dispara cientos de solicitudes."""
    many = _with_pending([(ALICE, i.to_bytes(3, "big")) for i in range(200)])
    router = Router({"https://": 404})
    got = ots.upgrade(many, transport=router.transport())
    assert got.status == "pending" and len(router.calls) == ots.REQUESTS_PER_CALENDAR == 4
    assert "196 atestaciones sin consultar" in (got.detail or "")
    hosts = [f"https://c{n}.calendar.opentimestamps.org" for n in range(6)]
    spread = _with_pending([(host, bytes([n, i])) for n, host in enumerate(hosts) for i in range(5)])
    router, budget = Router({"https://": 404}), ots.Budget()
    ots.upgrade(spread, transport=router.transport(), budget=budget)
    assert len(router.calls) == budget.requests == ots.REQUESTS_PER_RUN == 16
    assert max(Counter(urlsplit(url).netloc for url in router.calls).values()) == 4
    ots.upgrade(many, transport=router.transport(), budget=budget)  # la corrida ya no tiene solicitudes
    assert len(router.calls) == 16


def test_hostile_calendar_uris_are_out_of_the_whitelist_on_every_python() -> None:
    """r3: puerto o usuario, corchetes, ``@`` o ``http`` no son la URI de un calendario. Antes de
    Python 3.11.12 (CVE-2025-0938), ``urllib.parse`` aceptaba «[::1].calendar…» y la lista blanca
    del cliente la dejaba pasar: la forma se comprueba antes, igual en cada versión."""
    whitelist = ots._lib().UrlWhitelist(ots.CALENDAR_WHITELIST)
    for bad in (
        "https://x:y.calendar.opentimestamps.org",
        "https://[::1].calendar.opentimestamps.org",
        "https://a@b.calendar.opentimestamps.org",
        "https://alice.btc.calendar.opentimestamps.org:8443",
        "https://alice.btc.calendar.opentimestamps.org/x",
        "http://alice.btc.calendar.opentimestamps.org",
        "https://alice.btc.calendar.opentimestamps.org.evil.test",
    ):
        assert not ots._allowed(whitelist, bad), bad
    for good in PENDING_AT.values():
        assert ots._allowed(whitelist, good), good
    unparsable = "https://[::1].calendar.opentimestamps.org"
    assert ots._host(unparsable) == unparsable[:40]


def test_upgrade_survives_a_uri_that_is_not_a_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """r3: si una URI llegara a la solicitud sin ser una URL (``httpx2.InvalidURL``), la
    verificación no aborta ni pide nada (defensa detrás de la forma de ``_allowed``)."""
    monkeypatch.setattr(ots, "_allowed", lambda _allowed, _uri: True)
    hostile = _with_pending([("https://x:y.calendar.opentimestamps.org", b"\x01")])
    router = Router({"https://": 404})
    got = ots.upgrade(hostile, transport=router.transport())
    assert (got.status, got.changed, got.warnings) == ("pending", False, ()) and router.calls == []
    assert "URL inválida" in (got.detail or "")


# -- sin el extra (REQ-ARCH-99) ---------------------------------------------------------------------


def test_without_the_extra_every_entry_point_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "opentimestamps", None)
    router, _ = _calendars()
    stamp = ots.stamp(DATA, transport=router.transport())
    assert (stamp.status, stamp.warnings, stamp.ots) == ("skip", (TOOL_MISSING,), None)
    assert router.calls == []
    assert ots.read(COMPLETE).status == "skip"
    check = ots.verify(COMPLETE, VECTOR_DIGEST)
    assert (check.status, check.source, check.warnings) == ("skip", "calendars", (TOOL_MISSING,))
    upgraded = ots.upgrade(PENDING, transport=router.transport())
    assert (upgraded.status, upgraded.ots, upgraded.warnings) == ("skip", PENDING, (TOOL_MISSING,))
