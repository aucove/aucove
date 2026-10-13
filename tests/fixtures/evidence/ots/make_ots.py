# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Generador determinista de las pruebas OpenTimestamps de las pruebas de EV-05.

Arma con la biblioteca ``opentimestamps`` (sin red) tres vectores sobre el SHA-256 de
``DATA``, con todos los bytes «aleatorios» derivados de ``SEED``:

- ``complete.ots``: la ruta de un sello real (nonce del cliente, árbol del calendario,
  transacción de Bitcoin y ramas de Merkle del bloque) hecha de ``append``, ``prepend``
  y ``sha256``, que termina en una ``BitcoinBlockHeaderAttestation`` a la altura
  ``HEIGHT``;
- ``pending.ots``: la misma prueba recortada donde el calendario la recibe, con una
  ``PendingAttestation`` a ``CALENDAR`` (de la lista blanca), como queda al sellar;
- ``calendar-reply.bin``: lo que ese calendario devuelve en
  ``GET <CALENDAR>/timestamp/<compromiso>`` cuando el bloque ya existe; ``upgrade``
  lo fusiona y la prueba pendiente pasa a completa.

``vectors.json`` guarda los datos de los vectores (huella, compromiso, altura y raíz de
Merkle en el orden de los exploradores) y el SHA-256 de cada archivo. Una prueba exige
que el generador reproduzca los archivos byte a byte. Para regenerarlos::

    python tests/fixtures/evidence/ots/make_ots.py
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
from opentimestamps.core.op import OpAppend, OpPrepend, OpSHA256
from opentimestamps.core.serialize import BytesDeserializationContext, BytesSerializationContext
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

HERE = Path(__file__).resolve().parent
DATA = b"evidence ots test vector"
SEED = b"evidence ots seed v1"
HEIGHT = 900_000
CALENDAR = "https://alice.btc.calendar.opentimestamps.org"
CALENDAR_DEPTH = 5  # append, sha256, prepend, append, sha256: ahí recibe el calendario
FILES = ("complete.ots", "pending.ots", "calendar-reply.bin")


def seeded(label: str, size: int) -> bytes:
    """``size`` bytes fijos para ``label``: SHA-256 de la semilla en cadena."""
    out = b""
    counter = 0
    while len(out) < size:
        out += hashlib.sha256(SEED + b"\x00" + label.encode("ascii") + counter.to_bytes(4, "big")).digest()
        counter += 1
    return out[:size]


def serialize(stamp: Timestamp | DetachedTimestampFile) -> bytes:
    ctx = BytesSerializationContext()
    stamp.serialize(ctx)
    return bytes(ctx.getbytes())


@dataclass(frozen=True)
class Vectors:
    digest: bytes
    complete: bytes
    pending: bytes
    reply: bytes
    commitment: bytes
    merkle_root: bytes  # el mensaje que atestigua el bloque (orden interno)

    def files(self) -> dict[str, bytes]:
        return {"complete.ots": self.complete, "pending.ots": self.pending, "calendar-reply.bin": self.reply}

    def summary(self) -> dict[str, Any]:
        """Contenido de ``vectors.json``."""
        return {
            "data": DATA.decode("ascii"),
            "digest_sha256": self.digest.hex(),
            "calendar": CALENDAR,
            "commitment": self.commitment.hex(),
            "bitcoin_block": HEIGHT,
            "merkle_root": self.merkle_root[::-1].hex(),
            "files": {name: hashlib.sha256(data).hexdigest() for name, data in self.files().items()},
        }


def _complete_tree(digest: bytes) -> tuple[DetachedTimestampFile, Timestamp, Timestamp]:
    """Prueba completa; devuelve también el nodo del calendario y el de la atestación."""
    detached = DetachedTimestampFile(OpSHA256(), Timestamp(digest))
    node = detached.timestamp
    # nonce del cliente (``ots stamp``): el calendario solo ve un valor opaco
    node = node.ops.add(OpAppend(seeded("nonce", 16))).ops.add(OpSHA256())
    # entrada del calendario: marca de tiempo y sal propias
    node = node.ops.add(OpPrepend(seeded("calendar-time", 4))).ops.add(OpAppend(seeded("calendar-salt", 8)))
    calendar_node = node.ops.add(OpSHA256())
    node = calendar_node
    # árbol de Merkle del calendario
    node = node.ops.add(OpPrepend(seeded("calendar-left", 32))).ops.add(OpSHA256())
    node = node.ops.add(OpAppend(seeded("calendar-right", 32))).ops.add(OpSHA256())
    # transacción de Bitcoin que lleva la raíz del calendario; su txid es SHA-256d
    node = node.ops.add(OpPrepend(seeded("tx-prefix", 42))).ops.add(OpAppend(seeded("tx-suffix", 13)))
    node = node.ops.add(OpSHA256()).ops.add(OpSHA256())
    # ramas de Merkle del bloque, también con SHA-256d
    for level, side in enumerate(("right", "left", "right")):
        sibling = seeded(f"block-{level}", 32)
        node = node.ops.add(OpAppend(sibling) if side == "right" else OpPrepend(sibling))
        node = node.ops.add(OpSHA256()).ops.add(OpSHA256())
    node.attestations.add(BitcoinBlockHeaderAttestation(HEIGHT))
    return detached, calendar_node, node


def build() -> Vectors:
    """Los tres vectores, siempre los mismos bytes."""
    digest = hashlib.sha256(DATA).digest()
    detached, calendar_node, attested = _complete_tree(digest)
    complete = serialize(detached)
    reply = serialize(calendar_node)
    pending, commitment = cut(complete, CALENDAR_DEPTH, CALENDAR)
    if commitment != calendar_node.msg:
        raise AssertionError("el corte no cae en el nodo del calendario")
    return Vectors(digest, complete, pending, reply, commitment, attested.msg)


def cut(proof: bytes, depth: int, uri: str) -> tuple[bytes, bytes]:
    """Recorta ``proof`` ``depth`` operaciones después de la huella y deja ahí una
    atestación pendiente en ``uri``; devuelve la prueba nueva y el compromiso del corte."""
    detached = DetachedTimestampFile.deserialize(BytesDeserializationContext(proof))
    node = detached.timestamp
    for _ in range(depth):
        [node] = node.ops.values()  # una sola rama
    node.ops.clear()
    node.attestations.clear()
    node.attestations.add(PendingAttestation(uri))
    return serialize(detached), node.msg


def render_summary(vectors: Vectors) -> str:
    return json.dumps(vectors.summary(), indent=2, sort_keys=True) + "\n"


def write(directory: Path = HERE) -> None:
    vectors = build()
    for name, data in vectors.files().items():
        (directory / name).write_bytes(data)
    (directory / "vectors.json").write_bytes(render_summary(vectors).encode("ascii"))


if __name__ == "__main__":
    write()
