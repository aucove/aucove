# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Paquete de evidencia: el índice y la firma del índice (11-trazabilidad §8.8 y §8.10; D-72).

El paquete lo arma el responsable en cada hito, con ``gh`` y ``git`` (11 §8.8). En la raíz
lleva ``index.txt``, con una línea por archivo como la escribe ``sha256sum``::

    <SHA-256 en hexadecimal>  <ruta>

(dos espacios, o un espacio y ``*``). La ruta es relativa a la raíz del paquete, con o sin
``./``, usa ``/``, no tiene ``..`` y no se repite; el índice no se lista a sí mismo ni lista
las carpetas de firma.

``aucove evidence sign`` (05-operaciones §9.27) firma ``index.txt`` y sella la firma en una
carpeta ``signature/`` (o ``signature-<n>/``, con el menor ``n`` desde 2 que esté libre), que
se escribe de una vez desde una carpeta temporal hermana:

- ``signature.json`` (``evidence-signature/v1``): la huella del índice firmado, cuántos
  archivos lista, la firma y las pruebas, con los ``data`` en ``null``;
- ``index.txt.sig`` (la firma SSHSIG), ``index.txt.sig.ots`` y, por TSA, los archivos
  ``tsa/<id>.*`` de un registro;
- ``VERIFY.md``, que aporta quien llama.

Un paquete firmado no se modifica: lo nuevo va en el paquete del hito siguiente.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from .. import _meta
from .record import (
    INDEX_SIG_FILE,
    MAX_FILE_BYTES,
    MAX_PROOFS,
    Proof,
    RecordError,
    Signature,
    _need,
    _parse_proof,
    _proof_document,
    _read_file,
    _replace_dir,
    chain_file,
    parse_signature,
    revocation_file,
    root_file,
)

__all__ = [
    "INDEX_FILE",
    "MAX_INDEX_BYTES",
    "SIGNATURE_FILE",
    "SIGNATURE_SCHEMA",
    "PackageIndexError",
    "SignatureFolder",
    "SignatureRead",
    "index_files",
    "next_signature_dirname",
    "parse_index",
    "parse_signature_document",
    "read_index",
    "read_signature_dir",
    "signature_dirs",
    "signature_files",
    "write_signature_dir",
]

INDEX_FILE = "index.txt"
SIGNATURE_FILE = "signature.json"
SIGNATURE_SCHEMA = f"{_meta.SCHEMA_BASE}/evidence-signature/v1"
MAX_INDEX_BYTES = 16 * 2**20
MAX_INDEX_LINES = 200_000
SIGNATURE_DIR_RE = re.compile(r"signature(?:-([2-9]|[1-9][0-9]{1,3}))?")
_LINE_RE = re.compile(r"([0-9a-f]{64}) [ *](.+)")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")


class PackageIndexError(ValueError):
    """``index.txt`` que no tiene el formato de 11-trazabilidad §8.10 (``reason``: ``bad_index``)."""

    def __init__(self, reason: str = "bad_index", line: int | None = None) -> None:
        super().__init__(f"índice: {reason}" + (f" en la línea {line}" if line is not None else ""))
        self.reason = reason
        self.line = line


def _index_path(raw: str) -> str | None:
    """Ruta normalizada de una línea del índice, o ``None`` si no se admite."""
    path = raw[2:] if raw.startswith("./") else raw
    parts = path.split("/")
    if not path or "\\" in path or "\0" in path or path.startswith("/") or ":" in parts[0]:
        return None
    if any(p in ("", ".", "..") for p in parts):
        return None
    if path == INDEX_FILE or (len(parts) > 1 and SIGNATURE_DIR_RE.fullmatch(parts[0]) is not None):
        return None
    return path


def parse_index(data: bytes) -> dict[str, str]:
    """``ruta -> SHA-256`` de ``index.txt``; ``PackageIndexError`` si alguna línea no cumple el formato.

    Acepta fines de línea ``\\r\\n``. Una línea que ``sha256sum`` escapó (empieza con ``\\``,
    por un nombre con salto de línea o barra invertida) no se admite.
    """
    if len(data) > MAX_INDEX_BYTES:
        raise PackageIndexError()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise PackageIndexError() from None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or len(lines) > MAX_INDEX_LINES:
        raise PackageIndexError()
    out: dict[str, str] = {}
    for number, line in enumerate(lines, start=1):
        m = _LINE_RE.fullmatch(line.removesuffix("\r"))
        path = _index_path(m[2]) if m is not None else None
        if m is None or path is None or path in out:
            raise PackageIndexError(line=number)
        out[path] = m[1]
    return out


def index_files(root: Path, index: Mapping[str, str]) -> tuple[int, int, int]:
    """``(coinciden, difieren, faltan)`` de los archivos que lista ``index`` dentro de ``root``.

    Un archivo que no está, que es un enlace o que no se puede leer cuenta como faltante: una
    copia parcial del paquete no es una alteración (11-trazabilidad REQ-TRACE-92).
    """
    base = Path(os.path.abspath(root))
    same = differ = missing = 0
    for rel, expected in index.items():
        target = base.joinpath(*rel.split("/"))
        try:
            if target.is_symlink() or not target.is_file():
                missing += 1
                continue
            digest = hashlib.sha256()
            with target.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
        except OSError:
            missing += 1
            continue
        if digest.hexdigest() == expected:
            same += 1
        else:
            differ += 1
    return same, differ, missing


def signature_dirs(package: Path) -> list[Path]:
    """Carpetas de firma del paquete (``signature/``, ``signature-2/``…), en orden."""
    found: list[tuple[int, Path]] = []
    try:
        entries = list(os.scandir(package))
    except OSError:
        return []
    for entry in entries:
        m = SIGNATURE_DIR_RE.fullmatch(entry.name)
        if m is not None and entry.is_dir(follow_symlinks=False):
            found.append((int(m[1] or 1), Path(entry.path)))
    return [path for _n, path in sorted(found)]


def next_signature_dirname(package: Path) -> str:
    """``signature`` o, si ya existe, ``signature-<n>`` con el menor ``n`` desde 2 libre."""
    taken = {p.name for p in signature_dirs(package)}
    if "signature" not in taken and not (package / "signature").exists():
        return "signature"
    n = 2
    while f"signature-{n}" in taken or (package / f"signature-{n}").exists():
        n += 1
    return f"signature-{n}"


@dataclass(frozen=True)
class SignatureFolder:
    """Firma del índice y sus pruebas (``evidence-signature/v1``), con los bytes que se tengan."""

    signed_sha256: str
    files: int
    signature: Signature
    proofs: tuple[Proof, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "proofs", tuple(self.proofs))
        _need(isinstance(self.signed_sha256, str) and _HEX64_RE.fullmatch(self.signed_sha256) is not None,
              "/signed_sha256")
        _need(isinstance(self.files, int) and not isinstance(self.files, bool) and self.files >= 0, "/files")
        _need(isinstance(self.signature, Signature) and self.signature.file == INDEX_SIG_FILE, "/signature")
        _need(1 <= len(self.proofs) <= MAX_PROOFS and all(isinstance(p, Proof) for p in self.proofs), "/proofs")
        _need(all(p.subject == INDEX_SIG_FILE for p in self.proofs), "/proofs")

    def document(self) -> dict[str, Any]:
        """``signature.json``: los ``data`` van en ``null``, porque van en archivos."""
        return {
            "schema": SIGNATURE_SCHEMA,
            "signed": INDEX_FILE,
            "signed_sha256": self.signed_sha256,
            "files": self.files,
            "signature": self.signature.document(embed=False),
            "sealed": INDEX_SIG_FILE,
            "proofs": [_proof_document(proof, embed=False) for proof in self.proofs],
        }


def parse_signature_document(obj: object) -> SignatureFolder:
    """``signature.json`` (``evidence-signature/v1``) sin los bytes; ``RecordError`` con el puntero."""
    _need(isinstance(obj, Mapping), "")
    assert isinstance(obj, Mapping)
    _need(obj.get("schema") == SIGNATURE_SCHEMA, "/schema")
    _need(obj.get("signed") == INDEX_FILE, "/signed")
    _need(obj.get("sealed") == INDEX_SIG_FILE, "/sealed")
    signature = parse_signature(obj.get("signature"), "/signature", INDEX_SIG_FILE)
    proofs_obj = obj.get("proofs")
    _need(isinstance(proofs_obj, list), "/proofs")
    assert isinstance(proofs_obj, list)
    proofs = tuple(_parse_proof(item, f"/proofs/{i}", INDEX_SIG_FILE) for i, item in enumerate(proofs_obj))
    return SignatureFolder(
        signed_sha256=obj.get("signed_sha256"),  # type: ignore[arg-type]
        files=obj.get("files"),  # type: ignore[arg-type]
        signature=signature,
        proofs=proofs,
    )


def signature_files(folder: SignatureFolder, *, extra: Mapping[str, bytes] | None = None) -> dict[str, bytes]:
    """Archivos de la carpeta de firma: ruta relativa (con ``/``) -> bytes."""
    files: dict[str, bytes] = {
        SIGNATURE_FILE: (json.dumps(folder.document(), ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    }
    if folder.signature.data is not None:
        files[INDEX_SIG_FILE] = folder.signature.data
    for proof in folder.proofs:
        if proof.file is not None and proof.data is not None:
            files[proof.file] = proof.data
        if proof.chain_pem is not None:
            files[chain_file(proof.source)] = proof.chain_pem
        if proof.root_pem is not None:
            files[root_file(proof.source)] = proof.root_pem
        if proof.revocation is not None and proof.revocation.data is not None:
            files[revocation_file(proof.source, proof.revocation.type)] = proof.revocation.data
    for name, data in (extra or {}).items():
        pure = PurePosixPath(name)
        _need(isinstance(data, bytes) and len(pure.parts) == 1 and name not in files, "/extra", name)
        files[name] = data
    return files


def write_signature_dir(package: Path, folder: SignatureFolder, *, name: str,
                        extra: Mapping[str, bytes] | None = None) -> Path:
    """Escribe ``<paquete>/<name>/`` de una vez (carpeta temporal hermana y ``os.replace``).

    ``FileExistsError`` si ya existe: una carpeta de firma nunca se reescribe.
    """
    _need(SIGNATURE_DIR_RE.fullmatch(name) is not None, "/name", name)
    files = signature_files(folder, extra=extra)
    base = Path(package)
    final = base / name
    if final.exists() or final.is_symlink():
        raise FileExistsError(str(final))
    tmp = base / f".{name}.tmp-{secrets.token_hex(4)}"
    try:
        tmp.mkdir()
        for rel, data in files.items():
            target = tmp.joinpath(*rel.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        _replace_dir(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return final


@dataclass(frozen=True)
class SignatureRead:
    """Carpeta de firma leída: el documento con los bytes que había y los problemas (archivo, código)."""

    path: Path
    folder: SignatureFolder
    problems: tuple[tuple[str, str], ...]


def read_signature_dir(path: Path, *, max_file_bytes: int = MAX_FILE_BYTES) -> SignatureRead:
    """Lee una carpeta de firma; ``RecordError`` si ``signature.json`` falta o no se entiende."""
    folder = Path(path)
    raw, problem = _read_file(folder, SIGNATURE_FILE, max_file_bytes)
    if raw is None:
        raise RecordError("missing" if problem == "missing" else "invalid", "/" + SIGNATURE_FILE, problem or "")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise RecordError("invalid", "", str(exc)[:120]) from None
    doc = parse_signature_document(obj)
    problems: list[tuple[str, str]] = []

    def take(rel: str, *, required: bool = True) -> bytes | None:
        data, issue = _read_file(folder, rel, max_file_bytes)
        if data is None and required:
            problems.append((rel, issue or "missing"))
        return data

    signature = replace(doc.signature, data=take(INDEX_SIG_FILE))
    proofs: list[Proof] = []
    for proof in doc.proofs:
        if proof.status != "ok":
            proofs.append(proof)
            continue
        assert proof.file is not None
        updated = replace(proof, data=take(proof.file))
        if proof.type == "rfc3161":
            revocation = proof.revocation
            if revocation is not None:
                revocation = replace(revocation, data=take(revocation_file(proof.source, revocation.type)))
            updated = replace(
                updated,
                revocation=revocation,
                chain_pem=take(chain_file(proof.source), required=False),  # candidatas: el catálogo también las da
                root_pem=take(root_file(proof.source), required=False),
            )
        proofs.append(updated)
    return SignatureRead(folder, replace(doc, signature=signature, proofs=tuple(proofs)), tuple(problems))


def read_index(package: Path) -> tuple[bytes | None, str | None]:
    """Bytes de ``<paquete>/index.txt`` y, si no se pudo leer, el problema (``missing``,
    ``too_large`` o ``unreadable``), como en una carpeta de registro."""
    return _read_file(Path(package), INDEX_FILE, MAX_INDEX_BYTES)
