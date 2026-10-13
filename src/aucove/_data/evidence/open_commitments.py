# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Abre los compromisos de un registro de evidencia sin el núcleo (11-trazabilidad §8.3 y §8.6).

Uso: python open_commitments.py manifest.json openings.json [carpeta]. Solo biblioteca estándar de
Python 3.8 o posterior; cada línea dice ok, different, missing o not-in-manifest y sale con 0 o 1."""
import hashlib
import json
import sys
from pathlib import Path


def commitment(domain: bytes, salt: str, *parts: bytes) -> str:
    """SHA-256(dominio ‖ 0x00 ‖ sal ‖ partes), en hexadecimal."""
    return hashlib.sha256(domain + b"\0" + bytes.fromhex(salt) + b"".join(parts)).hexdigest()


def main(argv: list) -> int:
    manifest, opened = json.loads(raw := Path(argv[1]).read_bytes()), json.loads(Path(argv[2]).read_bytes())
    entries = {e["p"]: e["c"] for e in manifest["entries"]}
    rows = [("ok" if hashlib.sha256(raw).hexdigest() == opened["manifest_sha256"] else "different", argv[1])]
    for e in opened["entries"]:
        p, path = commitment(b"evidence.path.v1", e["salt"], e["path"].encode("utf-8")), Path(e["path"])
        unsafe = path.anchor or ".." in path.parts  # absoluta o con ..: git nunca la da y no se lee
        state = "different" if unsafe else "ok" if p in entries else "not-in-manifest"
        if p in entries and len(argv) > 3 and not unsafe:  # c es null si el archivo se borró
            f = Path(argv[3], path)
            c = commitment(b"evidence.blob.v1", e["salt"], f.read_bytes()) if f.is_file() else None
            state = "ok" if c == entries[p] else ("missing" if c is None else "different")
        rows.append((state, e["path"]))
    for a in opened["actors"]:
        v = commitment(b"evidence.actor.v1", a["salt"], a["r"].encode(), b"\0", a["value"].encode("utf-8"))
        rows.append(("ok" if {"r": a["r"], "a": v} in manifest["actors"] else "not-in-manifest", "actor:" + a["r"]))
    sys.stdout.reconfigure(errors="backslashreplace")  # rutas no ASCII en Windows; lo no imprimible va escapado
    print("\n".join("%-16s%s" % (s, "".join(x if x.isprintable() else ascii(x)[1:-1] for x in n)) for s, n in rows))
    return 0 if all(state == "ok" for state, _ in rows) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
