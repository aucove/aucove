# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Cliente OpenTimestamps: stamp, ``.ots``, upgrade y lectura de atestaciones (11-trazabilidad §8.4).

:func:`stamp` hace lo mismo que ``ots stamp``: el SHA-256 de los bytes sellados, un
nonce de 16 bytes (``append`` y ``sha256``, para que el calendario solo vea un valor
opaco) y el envío de ese valor a los 4 calendarios por defecto del cliente oficial. La
prueba vale si responden al menos 2 (su mínimo por defecto); las respuestas se fusionan
en un árbol y se serializan como ``DetachedTimestampFile`` (``manifest.json.ots``).
Queda pendiente hasta que Bitcoin incluye la raíz del calendario.

:func:`upgrade` pide a cada calendario el árbol que sigue a cada atestación pendiente
y lo fusiona, como ``ots upgrade``. Solo contacta URI de la lista blanca del cliente
oficial (``*.calendar.opentimestamps.org``, ``*.calendar.eternitywall.com`` y
``*.calendar.catallaxy.com``): una prueba ajena nunca decide a dónde se conecta. Como la
prueba puede ser ajena, también se acota cuánto se pide (:class:`Budget`): cada par
(calendario, mensaje) una sola vez por corrida, como mucho 4 solicitudes por calendario y
16 en total, y ninguna más una vez que hay una atestación de Bitcoin.

:func:`verify` comprueba que la prueba compromete el SHA-256 del manifiesto y lee sus
atestaciones con :func:`read`: ``pending`` si solo hay pendientes, y también con la de
un bloque de Bitcoin (``bitcoin_unverified``). v0.1 no coteja ese bloque con un nodo ni
con exploradores, y cualquiera puede escribir una atestación falsa: por eso no cuenta
como prueba de fecha (05-operaciones §9.26). El detalle da la altura y la raíz de Merkle
para cotejarlas a mano.

``opentimestamps`` (LGPL-3.0-or-later, trae ``python-bitcoinlib``) solo da la
serialización y las operaciones; la red va por ``httpx2`` con el transporte inyectable
(:func:`.catalog.http_client`), no por el ``urllib`` del paquete. Todo se importa de
forma perezosa (REQ-ARCH-99).
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from .catalog import TIMEOUT_S, TOOL_MISSING, Check, classify_http_error, clip, http_client, read_limited, skip_check

if TYPE_CHECKING:
    import httpx2

# Los del cliente oficial (opentimestamps.calendar.DEFAULT_AGGREGATORS).
DEFAULT_CALENDARS: tuple[str, ...] = (
    "https://a.pool.opentimestamps.org",
    "https://b.pool.opentimestamps.org",
    "https://a.pool.eternitywall.com",
    "https://ots.btc.catallaxy.com",
)
# Calendarios que upgrade puede contactar (DEFAULT_CALENDAR_WHITELIST del cliente oficial).
CALENDAR_WHITELIST: tuple[str, ...] = (
    "https://*.calendar.opentimestamps.org",
    "https://*.calendar.eternitywall.com",
    "https://*.calendar.catallaxy.com",
)
# Forma de la URI de un calendario: https y un nombre DNS, sin puerto, usuario, ruta ni corchetes.
# La lista blanca del cliente compara el netloc de urllib.parse, que antes de Python 3.11.12
# (CVE-2025-0938) acepta corchetes en un nombre: «https://[::1].calendar…» pasaba según la versión.
_CALENDAR_URI_RE = re.compile(r"https://[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+/?")
MIN_CALENDARS = 2
NONCE_BYTES = 16
OTS_FILE = "manifest.json.ots"
RESPONSE_MAX_BYTES = 10_000  # el tope del cliente oficial
OTS_MAX_BYTES = 1_000_000
# Topes de upgrade por corrida (05 §9.26): una prueba ajena puede traer miles de pendientes.
REQUESTS_PER_CALENDAR = 4
REQUESTS_PER_RUN = 16
_ACCEPT = {"Accept": "application/vnd.opentimestamps.v1"}

StampStatus = Literal["ok", "error", "skip"]
UpgradeStatus = Literal["complete", "pending", "error", "skip"]


@dataclass(frozen=True, slots=True)
class OtsStamp:
    """Resultado de :func:`stamp`: con ``ok``, ``ots`` son los bytes de ``manifest.json.ots``."""

    status: StampStatus
    reason: str
    ots: bytes | None = None
    calendars: tuple[str, ...] = ()  # los que respondieron
    error: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def file(self) -> str | None:
        """``manifest.json.ots`` (campo ``file`` de la prueba en evidence-record/v1), o ``None``."""
        return OTS_FILE if self.status == "ok" else None


@dataclass(frozen=True, slots=True)
class BitcoinAttestation:
    """Atestación de un bloque: altura y raíz de Merkle, en el orden en que la muestran los
    exploradores y ``bitcoin-cli getblockheader`` (bytes invertidos)."""

    height: int
    merkle_root: str


@dataclass(frozen=True, slots=True)
class OtsInfo:
    """Lectura de una prueba (:func:`read`): ``ok``, ``invalid`` o ``skip`` (falta el extra)."""

    status: Literal["ok", "invalid", "skip"]
    digest: bytes | None = None
    pending: tuple[str, ...] = ()  # URI de los calendarios con atestación pendiente
    bitcoin: tuple[BitcoinAttestation, ...] = ()
    other: int = 0  # atestaciones de otro tipo (Litecoin, desconocidas)
    detail: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return bool(self.bitcoin)


@dataclass(frozen=True, slots=True)
class OtsUpgrade:
    """Resultado de :func:`upgrade`: ``ots`` es la prueba, nueva si ``changed``."""

    status: UpgradeStatus
    reason: str
    ots: bytes
    changed: bool = False
    detail: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass
class Budget:
    """Topes de las consultas a los calendarios en una corrida (05 §9.26).

    Cada par (calendario, mensaje) se pide una sola vez: su respuesta queda en ``answers``
    (los bytes, ``None`` si el calendario todavía no la tiene o el texto del error) y sirve
    a las demás pruebas que lo traigan, como la copia local de un mismo registro. Como
    mucho ``per_calendar`` solicitudes a cada calendario (por su host) y ``total`` entre
    todos. Quien verifica varias carpetas comparte uno solo.
    """

    per_calendar: int = REQUESTS_PER_CALENDAR
    total: int = REQUESTS_PER_RUN
    used: dict[str, int] = field(default_factory=dict)
    answers: dict[tuple[str, bytes], bytes | str | None] = field(default_factory=dict)

    @property
    def requests(self) -> int:
        """Solicitudes hechas en la corrida."""
        return sum(self.used.values())


class _CalendarError(Exception):
    """Un calendario no dio una respuesta útil (el texto va al detalle)."""


class _NotYet(Exception):
    """El calendario todavía no tiene el compromiso anclado (404)."""


class _OverBudget(Exception):
    """No quedan solicitudes para ese calendario o para la corrida (:class:`Budget`)."""


# -- stamp -------------------------------------------------------------------------------


def stamp(
    data: bytes,
    *,
    calendars: Sequence[str] = DEFAULT_CALENDARS,
    minimum: int = MIN_CALENDARS,
    transport: httpx2.BaseTransport | None = None,
    timeout: float = TIMEOUT_S,
    nonce: bytes | None = None,
) -> OtsStamp:
    """Prueba OTS de ``data`` (los bytes exactos del manifiesto) en ``calendars``.

    ``ok`` si responden al menos ``minimum``; ``nonce`` (16 bytes aleatorios si falta)
    y ``transport`` se inyectan en las pruebas.
    """
    try:
        lib = _lib()
    except ImportError:
        return OtsStamp(
            "skip", "missing_extra", error="missing_extra: falta el extra evidence", warnings=(TOOL_MISSING,)
        )
    salt = os.urandom(NONCE_BYTES) if nonce is None else nonce
    detached = lib.DetachedTimestampFile(lib.OpSHA256(), lib.Timestamp(hashlib.sha256(data).digest()))
    tip = detached.timestamp.ops.add(lib.OpAppend(salt)).ops.add(lib.OpSHA256())
    responded: list[str] = []
    problems: list[str] = []
    with http_client(transport=transport, timeout=timeout) as client:
        for url in calendars:
            try:
                tip.merge(_submit(client, lib, url, tip.msg))
            except _CalendarError as exc:
                problems.append(f"{_host(url)}: {exc}")
                continue
            responded.append(url)
    if len(responded) < minimum:
        detail = f"respondieron {len(responded)} de {len(calendars)} calendarios (mínimo {minimum})"
        if problems:
            detail += "; " + "; ".join(problems)
        return OtsStamp("error", "calendars", calendars=tuple(responded), error=clip(f"calendars: {detail}"))
    return OtsStamp("ok", "ok", ots=_serialize(lib, detached), calendars=tuple(responded))


def _submit(client: httpx2.Client, lib: Any, url: str, digest: bytes) -> Any:
    """``POST <url>/digest``: el calendario devuelve un árbol que compromete ``digest``."""
    headers = {**_ACCEPT, "Content-Type": "application/x-www-form-urlencoded"}
    body = _request(client, "POST", url.rstrip("/") + "/digest", digest, headers)
    timestamp = _deserialize_timestamp(lib, body, digest)
    if not _useful(lib, timestamp):
        raise _CalendarError("respuesta sin atestación utilizable")
    return timestamp


def _useful(lib: Any, timestamp: Any) -> bool:
    """El árbol trae una atestación de Bitcoin o una pendiente en un calendario de la lista."""
    whitelist = lib.UrlWhitelist(CALENDAR_WHITELIST)
    for _, attestation in timestamp.all_attestations():
        if isinstance(attestation, lib.BitcoinBlockHeaderAttestation):
            return True
        if isinstance(attestation, lib.PendingAttestation) and _allowed(whitelist, attestation.uri):
            return True
    return False


# -- upgrade -----------------------------------------------------------------------------


def upgrade(
    ots: bytes,
    *,
    transport: httpx2.BaseTransport | None = None,
    timeout: float = TIMEOUT_S,
    whitelist: Sequence[str] = CALENDAR_WHITELIST,
    budget: Budget | None = None,
) -> OtsUpgrade:
    """Completa una prueba pendiente con los calendarios de sus atestaciones (``ots upgrade``).

    ``complete`` si al terminar hay una atestación de Bitcoin, ``pending`` si no, y
    ``error`` si la prueba no se puede leer. Solo contacta URI de ``whitelist``, deja de
    pedir en cuanto hay una atestación de Bitcoin y respeta ``budget`` (uno nuevo si
    falta: cada par una vez, 4 solicitudes por calendario y 16 en total).
    """
    try:
        lib = _lib()
    except ImportError:
        return OtsUpgrade("skip", "missing_extra", ots, warnings=(TOOL_MISSING,))
    try:
        detached = _parse(lib, ots)
    except ValueError as exc:
        return OtsUpgrade("error", "invalid", ots, detail=clip(str(exc)))
    budget = Budget() if budget is None else budget
    allowed = lib.UrlWhitelist(whitelist)
    existing = {attestation for _, attestation in detached.timestamp.all_attestations()}
    bitcoin = any(isinstance(a, lib.BitcoinBlockHeaderAttestation) for a in existing)
    pending = [
        (node, attestation.uri)
        for node in _nodes(detached.timestamp)
        for attestation in sorted(node.attestations)
        if isinstance(attestation, lib.PendingAttestation)
    ]
    changed = False
    problems: list[str] = []
    offline = contacted = skipped = 0
    if pending and not bitcoin:
        with http_client(transport=transport, timeout=timeout) as client:
            for node, uri in pending:
                if bitcoin:
                    break  # ya hay una atestación de Bitcoin: no hace falta pedir más
                if not _allowed(allowed, uri):
                    problems.append(f"{_host(uri)}: fuera de la lista blanca")
                    continue
                try:
                    body = _ask(client, budget, uri, node.msg)
                except _OverBudget:
                    skipped += 1
                    continue
                except _CalendarError as exc:
                    contacted += 1
                    problems.append(f"{_host(uri)}: {exc}")
                    offline += str(exc) in ("offline", "timeout")
                    continue
                contacted += 1
                if body is None:  # el calendario todavía no lo tiene anclado
                    continue
                try:
                    upgraded = _deserialize_timestamp(lib, body, node.msg)
                except _CalendarError as exc:
                    problems.append(f"{_host(uri)}: {exc}")
                    continue
                news = {a for _, a in upgraded.all_attestations()} - existing
                if news:
                    node.merge(upgraded)
                    existing |= news
                    changed = True
                    bitcoin = bitcoin or any(isinstance(a, lib.BitcoinBlockHeaderAttestation) for a in news)
    if skipped:
        problems.append(f"{skipped} atestaciones sin consultar (tope de solicitudes)")
    warnings = ("W_NET_OFFLINE",) if contacted and offline == contacted else ()
    detail = clip("; ".join(problems)) if problems else None
    new_bytes = _serialize(lib, detached) if changed else ots
    if bitcoin:
        return OtsUpgrade("complete", "ok", new_bytes, changed, detail, warnings)
    return OtsUpgrade("pending", "pending", new_bytes, changed, detail, warnings)


def _allowed(allowed: Any, uri: str) -> bool:
    """``uri`` tiene la forma de un calendario (``_CALENDAR_URI_RE``) y está en la lista blanca.

    La forma se comprueba aquí y no en ``urllib.parse``, para que el resultado no dependa de la
    versión de Python; una URI que ni siquiera se puede analizar tampoco está en la lista.
    """
    if not isinstance(uri, str) or _CALENDAR_URI_RE.fullmatch(uri) is None:
        return False
    try:
        return uri in allowed
    except ValueError:  # p. ej., «Invalid IPv6 URL» de urllib.parse
        return False


def _ask(client: httpx2.Client, budget: Budget, uri: str, commitment: bytes) -> bytes | None:
    """``GET <uri>/timestamp/<hex>``: el árbol que sigue a ``commitment``, en bytes; ``None``
    si el calendario todavía no lo tiene (404).

    Cada par se pide una sola vez en la corrida (``budget.answers``). ``_OverBudget`` si ya
    no quedan solicitudes y ``_CalendarError`` si falló, también si el fallo era recordado.
    """
    key = (uri, bytes(commitment))
    if key not in budget.answers:
        host = _host(uri).lower()
        if budget.requests >= budget.total or budget.used.get(host, 0) >= budget.per_calendar:
            raise _OverBudget()
        budget.used[host] = budget.used.get(host, 0) + 1
        url = uri.rstrip("/") + "/timestamp/" + commitment.hex()
        try:
            budget.answers[key] = _request(client, "GET", url, None, dict(_ACCEPT))
        except _NotYet:
            budget.answers[key] = None
        except _CalendarError as exc:
            budget.answers[key] = str(exc)
    answer = budget.answers[key]
    if isinstance(answer, str):
        raise _CalendarError(answer)
    return answer


def _nodes(timestamp: Any) -> Iterator[Any]:
    yield timestamp
    for child in list(timestamp.ops.values()):
        yield from _nodes(child)


# -- lectura y verificación ------------------------------------------------------------


def read(ots: bytes) -> OtsInfo:
    """Huella sellada y atestaciones de una prueba ``.ots``, sin red."""
    try:
        lib = _lib()
    except ImportError:
        return OtsInfo("skip", detail="falta el extra evidence", warnings=(TOOL_MISSING,))
    try:
        detached = _parse(lib, ots)
    except ValueError as exc:
        return OtsInfo("invalid", detail=clip(str(exc)))
    if not isinstance(detached.file_hash_op, lib.OpSHA256):
        return OtsInfo("invalid", detail=f"la prueba no sella un SHA-256 ({detached.file_hash_op})")
    pending: set[str] = set()
    bitcoin: set[BitcoinAttestation] = set()
    other = 0
    for msg, attestation in detached.timestamp.all_attestations():
        if isinstance(attestation, lib.PendingAttestation):
            pending.add(attestation.uri)
        elif isinstance(attestation, lib.BitcoinBlockHeaderAttestation):
            bitcoin.add(BitcoinAttestation(attestation.height, msg[::-1].hex()))
        else:
            other += 1
    return OtsInfo(
        "ok",
        digest=detached.file_digest,
        pending=tuple(sorted(pending)),
        bitcoin=tuple(sorted(bitcoin, key=lambda b: (b.height, b.merkle_root))),
        other=other,
    )


def verify(ots: bytes, digest: bytes) -> Check:
    """Comprueba una prueba OTS contra ``digest`` (SHA-256 del manifiesto), sin red.

    ``fail`` si no se puede leer o sella otra huella; ``pending`` si solo hay atestaciones
    pendientes (``source`` = ``calendars``, motivo ``pending``) y también con la atestación
    de un bloque (``source`` = ``bitcoin``, motivo ``bitcoin_unverified``): v0.1 no coteja
    ese bloque con un nodo y cualquiera puede escribir una atestación falsa, así que nunca
    da ``pass`` ni ``weak`` (05-operaciones §9.26).
    """
    info = read(ots)
    if info.status == "skip":
        return skip_check("calendars")
    if info.status == "invalid":
        return Check("fail", "calendars", "invalid", detail=info.detail)
    if info.digest != digest:
        return Check("fail", "calendars", "digest_mismatch", detail="la prueba no sella el SHA-256 del manifiesto")
    if info.bitcoin:
        first = info.bitcoin[0]
        detail = (
            f"bloque {first.height} de Bitcoin, raíz de Merkle {first.merkle_root}; "
            "sin cotejar con un nodo (v0.1): cotéjalo a mano"
        )
        return Check("pending", "bitcoin", "bitcoin_unverified", detail=detail)
    return Check("pending", "calendars", "pending", detail=f"pendiente en {len(info.pending)} calendario(s)")


# -- utilidades --------------------------------------------------------------------------


def _request(
    client: httpx2.Client, method: str, url: str, body: bytes | None, headers: dict[str, str]
) -> bytes:
    import httpx2

    try:
        with client.stream(method, url, content=body, headers=headers) as response:
            if response.status_code == 404 and method == "GET":
                raise _NotYet()
            if response.status_code != 200:
                raise _CalendarError(f"HTTP {response.status_code}")
            return read_limited(response, RESPONSE_MAX_BYTES)
    except httpx2.HTTPError as exc:
        raise _CalendarError(classify_http_error(exc)) from exc
    except httpx2.InvalidURL as exc:  # una URI de la lista blanca que no es una URL (prueba ajena)
        raise _CalendarError("URL inválida") from exc
    except ValueError as exc:
        raise _CalendarError(str(exc)) from exc


def _deserialize_timestamp(lib: Any, body: bytes, msg: bytes) -> Any:
    ctx = lib.BytesDeserializationContext(body)
    try:
        timestamp = lib.Timestamp.deserialize(ctx, msg)
        ctx.assert_eof()
    except (lib.DeserializationError, ValueError, TypeError) as exc:
        raise _CalendarError(f"respuesta ilegible: {exc}") from exc
    return timestamp


def _parse(lib: Any, ots: bytes) -> Any:
    if len(ots) > OTS_MAX_BYTES:
        raise ValueError("prueba .ots demasiado grande")
    try:
        return lib.DetachedTimestampFile.deserialize(lib.BytesDeserializationContext(ots))
    except (lib.DeserializationError, ValueError, TypeError, KeyError) as exc:
        raise ValueError(f"prueba .ots ilegible: {exc}") from exc


def _serialize(lib: Any, detached: Any) -> bytes:
    ctx = lib.BytesSerializationContext()
    detached.serialize(ctx)
    return bytes(ctx.getbytes())


def _host(url: str) -> str:
    """El host de una URI con la forma de un calendario (``_CALENDAR_URI_RE``); de cualquier otra
    cadena, que puede venir de una prueba ajena, sus primeros 40 caracteres. No usa
    ``urllib.parse``, cuyo resultado cambia con la versión de Python (CVE-2025-0938)."""
    if _CALENDAR_URI_RE.fullmatch(url) is not None:
        return url[len("https://"):].rstrip("/")
    return url[:40]


def _lib() -> Any:
    """Las clases de ``opentimestamps``; ``ImportError`` sin el extra."""
    from types import SimpleNamespace

    import opentimestamps  # noqa: F401 - primero el paquete: sin él, un submódulo ya cargado no basta
    from opentimestamps.calendar import UrlWhitelist
    from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
    from opentimestamps.core.op import OpAppend, OpSHA256
    from opentimestamps.core.serialize import (
        BytesDeserializationContext,
        BytesSerializationContext,
        DeserializationError,
    )
    from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

    return SimpleNamespace(
        UrlWhitelist=UrlWhitelist,
        BitcoinBlockHeaderAttestation=BitcoinBlockHeaderAttestation,
        PendingAttestation=PendingAttestation,
        OpAppend=OpAppend,
        OpSHA256=OpSHA256,
        BytesDeserializationContext=BytesDeserializationContext,
        BytesSerializationContext=BytesSerializationContext,
        DeserializationError=DeserializationError,
        DetachedTimestampFile=DetachedTimestampFile,
        Timestamp=Timestamp,
    )
