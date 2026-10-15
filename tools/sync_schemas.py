# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Copia verificada de ``spec/schemas`` en ``schemas/`` y reescritura de ``SHA256SUMS``.

Uso::

    python tools/sync_schemas.py --spec-dir <spec> [--dest schemas] [--check]

Copia cada ``*.schema.json`` de ``<spec>/schemas`` (o de ``<spec>``, si ya es esa
carpeta) tras comprobar que es un objeto JSON válido, normalizado a UTF-8 sin BOM,
con LF y exactamente un salto de línea final (convención de archivos del repositorio). Borra
los ``*.schema.json`` de destino que ya no están en la spec y reescribe
``SHA256SUMS`` en el formato de ``sha256sum`` (``<hex>  <archivo>``, ordenado, con
LF). Con ``--check`` no escribe nada: sale con 1 si la copia o ``SHA256SUMS``
difieren. Solo usa la biblioteca estándar.

Para que un ``--spec-dir`` equivocado no vacíe ``schemas/``, la herramienta se niega
(sale con 1 sin tocar nada) si el origen no trae algún esquema conocido del núcleo
(``KNOWN_SCHEMAS``) que ya existe en el destino.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path

SUMS_NAME = "SHA256SUMS"
DEFAULT_DEST = Path(__file__).resolve().parent.parent / "schemas"
SCHEMA_GLOB = "*.schema.json"
# Esquemas que el núcleo carga de schemas/ (03-esquemas, con los de evidencia de REQ-SCHEMA-93);
# nunca se borran por omisión del origen.
KNOWN_SCHEMAS: tuple[str, ...] = tuple(
    f"{name}.schema.json"
    for name in (
        "decision", "evidence-manifest", "evidence-openings", "evidence-record", "evidence-signature",
        "pr-meta", "project", "result", "roles", "session-note",
    )
)


def normalize(data: bytes) -> bytes:
    """Sin BOM, CRLF y CR a LF, y exactamente un salto de línea final."""
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return data.rstrip(b"\n") + b"\n"


def schemas_dir(spec_dir: Path) -> Path:
    """Carpeta de esquemas de la spec: ``<spec>/schemas`` o la propia ``spec_dir``."""
    nested = spec_dir / "schemas"
    return nested if nested.is_dir() else spec_dir


def expected_files(spec_dir: Path) -> dict[str, bytes]:
    """Nombre -> contenido normalizado de cada esquema de la spec; ``ValueError`` si alguno no es JSON."""
    src = schemas_dir(spec_dir)
    if not src.is_dir():
        raise FileNotFoundError(f"no existe la carpeta de esquemas: {src}")
    out: dict[str, bytes] = {}
    for path in sorted(src.glob(SCHEMA_GLOB)):
        content = normalize(path.read_bytes())
        try:
            value = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"{path.name}: no es JSON UTF-8 válido") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{path.name}: un esquema debe ser un objeto JSON")
        out[path.name] = content
    if not out:
        raise FileNotFoundError(f"no hay esquemas {SCHEMA_GLOB} en {src}")
    return out


def _present(dest: Path) -> set[str]:
    return {p.name for p in dest.glob(SCHEMA_GLOB)} if dest.is_dir() else set()


def check_known(files: dict[str, bytes], dest: Path) -> None:
    """``ValueError`` si el origen no trae un esquema conocido que ya está en ``dest``.

    Protege ``schemas/`` de un ``--spec-dir`` que apunta a otra carpeta: sin esta
    comprobación, ``sync`` borraría los esquemas del núcleo.
    """
    missing = sorted(name for name in KNOWN_SCHEMAS if name in _present(dest) and name not in files)
    if missing:
        raise ValueError(
            f"el origen no trae {', '.join(missing)}, que existen en {dest}; "
            "¿--spec-dir apunta a la spec?"
        )


def sums_text(files: dict[str, bytes]) -> str:
    """Contenido de ``SHA256SUMS`` para ``files``."""
    return "".join(f"{hashlib.sha256(files[name]).hexdigest()}  {name}\n" for name in sorted(files))


def parse_sums(text: str) -> dict[str, str]:
    """Nombre -> hash de un ``SHA256SUMS``; ``ValueError`` ante una línea mal formada."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line:
            continue
        digest, sep, name = line.partition("  ")
        if not sep or len(digest) != 64 or not name:
            raise ValueError(f"línea inválida en {SUMS_NAME}: {line!r}")
        out[name] = digest
    return out


def diff(spec_dir: Path, dest: Path) -> list[str]:
    """Diferencias entre la spec y ``dest`` (vacío = sincronizado)."""
    files = expected_files(spec_dir)
    problems: list[str] = []
    present = _present(dest)
    for name in sorted(present - set(files)):
        problems.append(f"{name}: sobra en {dest.name}/")
    for name, content in files.items():
        target = dest / name
        if not target.is_file():
            problems.append(f"{name}: falta en {dest.name}/")
        elif target.read_bytes() != content:
            problems.append(f"{name}: difiere de la spec")
    sums = dest / SUMS_NAME
    if not sums.is_file() or sums.read_bytes() != sums_text(files).encode("ascii"):
        problems.append(f"{SUMS_NAME}: no coincide")
    return problems


def sync(spec_dir: Path, dest: Path) -> list[str]:
    """Copia los esquemas normalizados y reescribe ``SHA256SUMS``; devuelve los nombres copiados."""
    files = expected_files(spec_dir)
    check_known(files, dest)  # antes de escribir o borrar nada
    dest.mkdir(parents=True, exist_ok=True)
    for stale in dest.glob(SCHEMA_GLOB):
        if stale.name not in files:
            stale.unlink()
    for name, content in files.items():
        target = dest / name
        if not target.is_file() or target.read_bytes() != content:
            target.write_bytes(content)
    (dest / SUMS_NAME).write_bytes(sums_text(files).encode("ascii"))
    return sorted(files)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sincroniza schemas/ con spec/schemas.")
    parser.add_argument("--spec-dir", required=True, type=Path, help="carpeta de la spec (o su schemas/)")
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST, help="carpeta destino (por defecto schemas/)")
    parser.add_argument("--check", action="store_true", help="solo comprueba; sale con 1 si hay diferencias")
    args = parser.parse_args(argv)
    try:
        if args.check:
            problems = diff(args.spec_dir, args.dest)
            for problem in problems:
                print(problem, file=sys.stderr)
            return 1 if problems else 0
        for name in sync(args.spec_dir, args.dest):
            print(name)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
