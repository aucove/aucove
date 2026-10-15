# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Paquete de evidencia: ``index.txt`` y las carpetas de firma (``evidence.package``; 11-trazabilidad §8.8 y §8.10).

La verificación de las carpetas (firma, sellos e índice) y la cobertura de los registros se
prueban con el corpus en ``tests/unit/test_evidence_verify.py``; aquí, el formato del índice,
los nombres de las carpetas y su lectura y escritura.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from aucove import _meta
from aucove.evidence import package as pkg
from aucove.evidence import record
from aucove.evidence.package import PackageIndexError, SignatureFolder
from aucove.evidence.record import INDEX_SIG_FILE, Proof, RecordError, Revocation, Signature

H1, H2 = "a" * 64, "b" * 64


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# -- index.txt ------------------------------------------------------------------------------


def test_index_lines_as_sha256sum_writes_them() -> None:
    data = (f"{H1}  integration-abc/manifest.json\n{H2} *./paper.pdf\r\n{H1}  signature\n").encode()
    assert pkg.parse_index(data) == {"integration-abc/manifest.json": H1, "paper.pdf": H2, "signature": H1}
    assert pkg.parse_index(f"{H1}  a.txt".encode()) == {"a.txt": H1}  # sin salto final
    assert pkg.parse_index(f"{H1}  carpeta con espacios/ñandú.md\n".encode()) == {"carpeta con espacios/ñandú.md": H1}


@pytest.mark.parametrize(
    ("text", "line"),
    [
        (f"{H1} a.txt\n", 1),  # un solo espacio
        (f"{H1.upper()}  a.txt\n", 1),
        (f"{H1[:-1]}  a.txt\n", 1),
        (f"\\{H1}  a\\nb.txt\n", 1),  # escapada por sha256sum
        (f"{H1}  /abs.txt\n", 1),
        (f"{H1}  C:/x.txt\n", 1),
        (f"{H1}  a\\b.txt\n", 1),
        (f"{H1}  ../fuera.txt\n", 1),
        (f"{H1}  a/../b.txt\n", 1),
        (f"{H1}  a//b.txt\n", 1),
        (f"{H1}  a/./b.txt\n", 1),
        (f"{H1}  ./\n", 1),
        (f"{H1}  index.txt\n", 1),  # no se lista a sí mismo
        (f"{H1}  signature/index.txt.sig\n", 1),  # ni las carpetas de firma
        (f"{H1}  signature-2/signature.json\n", 1),
        (f"{H1}  a.txt\n{H2}  ./a.txt\n", 2),  # repetida
        (f"{H1}  a.txt\n\n", 2),  # línea vacía
        ("\n", 1),
    ],
)
def test_bad_index_lines_give_their_number(text: str, line: int) -> None:
    with pytest.raises(PackageIndexError) as info:
        pkg.parse_index(text.encode("utf-8"))
    assert (info.value.reason, info.value.line) == ("bad_index", line)


def test_an_index_that_is_not_utf8_empty_or_too_big(monkeypatch: pytest.MonkeyPatch) -> None:
    for data in (b"", b"\xff\xfe" + f"{H1}  a.txt\n".encode("utf-16-le"), b"x" * (pkg.MAX_INDEX_BYTES + 1)):
        with pytest.raises(PackageIndexError) as info:
            pkg.parse_index(data)
        assert info.value.line is None
    monkeypatch.setattr(pkg, "MAX_INDEX_LINES", 2)
    with pytest.raises(PackageIndexError):
        pkg.parse_index("".join(f"{H1}  f{i}.txt\n" for i in range(3)).encode())


def test_index_files_counts_same_different_and_missing(tmp_path: Path) -> None:
    (tmp_path / "rec").mkdir()
    (tmp_path / "rec" / "a.txt").write_bytes(b"uno")
    (tmp_path / "b.txt").write_bytes(b"dos")
    (tmp_path / "carpeta").mkdir()
    index = {"rec/a.txt": sha(b"uno"), "b.txt": sha(b"otro"), "falta.txt": H1, "carpeta": H2}
    assert pkg.index_files(tmp_path, index) == (1, 1, 2)  # una carpeta no es un archivo: falta


def test_a_symlink_in_the_package_counts_as_missing(tmp_path: Path) -> None:
    (tmp_path / "real.txt").write_bytes(b"uno")
    try:
        os.symlink(tmp_path / "real.txt", tmp_path / "enlace.txt")
    except (OSError, NotImplementedError):
        pytest.skip("este sistema no permite crear enlaces simbólicos")
    assert pkg.index_files(tmp_path, {"enlace.txt": sha(b"uno")}) == (0, 0, 1)


def test_read_index(tmp_path: Path) -> None:
    assert pkg.read_index(tmp_path) == (None, "missing")
    (tmp_path / pkg.INDEX_FILE).write_bytes(f"{H1}  a.txt\n".encode())
    assert pkg.read_index(tmp_path) == (f"{H1}  a.txt\n".encode(), None)


# -- nombres de las carpetas de firma -------------------------------------------------------------


def test_signature_dirs_in_numeric_order_and_only_folders(tmp_path: Path) -> None:
    for name in ("signature-10", "signature", "signature-2", "signature-1", "signature-01", "signature-10000",
                 "signature-x"):
        (tmp_path / name).mkdir()
    (tmp_path / "signature-3").write_bytes(b"un archivo, no una carpeta")
    assert [p.name for p in pkg.signature_dirs(tmp_path)] == ["signature", "signature-2", "signature-10"]
    assert pkg.signature_dirs(tmp_path / "no-existe") == []


def test_next_signature_dirname_takes_the_lowest_free(tmp_path: Path) -> None:
    assert pkg.next_signature_dirname(tmp_path) == "signature"
    (tmp_path / "signature").mkdir()
    assert pkg.next_signature_dirname(tmp_path) == "signature-2"
    (tmp_path / "signature-2").mkdir()
    (tmp_path / "signature-4").mkdir()
    assert pkg.next_signature_dirname(tmp_path) == "signature-3"
    (tmp_path / "signature-3").write_bytes(b"un archivo con ese nombre tampoco se pisa")
    assert pkg.next_signature_dirname(tmp_path) == "signature-5"
    other = tmp_path / "otro"
    other.mkdir()
    (other / "signature").write_bytes(b"archivo")
    assert pkg.next_signature_dirname(other) == "signature-2"


# -- signature.json y la carpeta -------------------------------------------------------------------


def folder(**changes: object) -> SignatureFolder:
    signature = Signature(INDEX_SIG_FILE, "sha512", "SHA256:" + "A" * 43, "admin-2026", b"-----firma-----\n")
    proofs = (
        Proof.rfc3161("alpha", b"tsr", "2026-10-06T12:00:00Z", revocation=Revocation("ocsp", b"ocsp"),
                      chain_pem=b"cadena", root_pem=b"raiz", subject=INDEX_SIG_FILE),
        Proof.failed("rfc3161", "beta", "tiempo agotado", subject=INDEX_SIG_FILE),
        Proof.ots(b"ots", subject=INDEX_SIG_FILE),
    )
    values: dict[str, object] = {"signed_sha256": H1, "files": 3, "signature": signature, "proofs": proofs}
    values.update(changes)
    return SignatureFolder(**values)  # type: ignore[arg-type]


def test_the_document_has_no_bytes_and_parses_back() -> None:
    doc = folder().document()
    assert doc["schema"] == f"{_meta.SCHEMA_BASE}/evidence-signature/v1"
    assert (doc["signed"], doc["sealed"], doc["signed_sha256"], doc["files"]) == ("index.txt", INDEX_SIG_FILE, H1, 3)
    assert doc["signature"]["data"] is None and all(p["data"] is None for p in doc["proofs"])
    assert [p["file"] for p in doc["proofs"]] == ["tsa/alpha.tsr", None, "index.txt.sig.ots"]
    back = pkg.parse_signature_document(json.loads(json.dumps(doc)))
    assert back.signed_sha256 == H1 and back.files == 3
    assert back.signature.key_id == "admin-2026" and back.signature.data is None
    assert [(p.type, p.source, p.status, p.subject) for p in back.proofs] == [
        ("rfc3161", "alpha", "ok", INDEX_SIG_FILE), ("rfc3161", "beta", "error", INDEX_SIG_FILE),
        ("ots", "calendars", "ok", INDEX_SIG_FILE)]


@pytest.mark.parametrize(
    "change",
    [
        {"signed_sha256": "ABC"},
        {"files": -1},
        {"files": True},
        {"proofs": ()},
        {"proofs": (Proof.ots(b"ots"),)},  # de manifest.json, no de la firma del índice
        {"signature": Signature(record.SIG_FILE, "sha512", "SHA256:" + "A" * 43, "admin-2026")},
    ],
)
def test_a_folder_with_bad_fields_is_rejected(change: dict[str, object]) -> None:
    with pytest.raises(RecordError):
        folder(**change)


@pytest.mark.parametrize(
    ("key", "value"),
    [("schema", "https://example.invalid/otro/v1"), ("signed", "otro.txt"), ("sealed", "manifest.json.sig"),
     ("proofs", {}), ("signature", None)],
)
def test_a_document_with_other_fields_is_rejected(key: str, value: object) -> None:
    doc = folder().document()
    doc[key] = value
    with pytest.raises(RecordError):
        pkg.parse_signature_document(doc)


def test_write_and_read_a_signature_folder(tmp_path: Path) -> None:
    written = pkg.write_signature_dir(tmp_path, folder(), name="signature", extra={"VERIFY.md": b"# Verificar\n"})
    assert written == tmp_path / "signature"
    files = sorted(p.relative_to(written).as_posix() for p in written.rglob("*") if p.is_file())
    assert files == ["VERIFY.md", "index.txt.sig", "index.txt.sig.ots", "signature.json", "tsa/alpha.chain.pem",
                     "tsa/alpha.ocsp.der", "tsa/alpha.root.pem", "tsa/alpha.tsr"]
    assert [p.name for p in tmp_path.iterdir()] == ["signature"]  # sin la carpeta temporal
    read = pkg.read_signature_dir(written)
    assert read.problems == ()
    assert read.folder.signature.data == b"-----firma-----\n"
    alpha = read.folder.proofs[0]
    assert (alpha.data, alpha.chain_pem, alpha.root_pem) == (b"tsr", b"cadena", b"raiz")
    assert alpha.revocation is not None and alpha.revocation.data == b"ocsp"
    assert read.folder.proofs[2].data == b"ots"
    # una carpeta de firma nunca se reescribe
    with pytest.raises(FileExistsError):
        pkg.write_signature_dir(tmp_path, folder(), name="signature")
    assert pkg.write_signature_dir(tmp_path, folder(), name=pkg.next_signature_dirname(tmp_path)).name == "signature-2"


def test_write_rejects_a_bad_name_or_extra_and_leaves_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("firma", "signature-1", "../signature"):
        with pytest.raises(RecordError):
            pkg.write_signature_dir(tmp_path, folder(), name=name)
    for extra in ({"sub/VERIFY.md": b"x"}, {"signature.json": b"x"}, {"VERIFY.md": "texto"}):
        with pytest.raises(RecordError):
            pkg.write_signature_dir(tmp_path, folder(), name="signature", extra=extra)  # type: ignore[arg-type]

    def boom(_tmp: Path, _final: Path) -> None:
        raise OSError("disco lleno")

    monkeypatch.setattr(pkg, "_replace_dir", boom)
    with pytest.raises(OSError):
        pkg.write_signature_dir(tmp_path, folder(), name="signature")
    assert list(tmp_path.iterdir()) == []


def test_reading_a_signature_folder_with_missing_parts(tmp_path: Path) -> None:
    written = pkg.write_signature_dir(tmp_path, folder(), name="signature")
    (written / INDEX_SIG_FILE).unlink()
    (written / "tsa" / "alpha.ocsp.der").unlink()
    (written / "tsa" / "alpha.chain.pem").unlink()  # opcional: el catálogo también la da
    read = pkg.read_signature_dir(written)
    assert read.problems == ((INDEX_SIG_FILE, "missing"), ("tsa/alpha.ocsp.der", "missing"))
    assert read.folder.signature.data is None and read.folder.proofs[0].chain_pem is None
    (written / pkg.SIGNATURE_FILE).write_bytes(b"{no es json")
    with pytest.raises(RecordError) as info:
        pkg.read_signature_dir(written)
    assert info.value.reason == "invalid"
    (written / pkg.SIGNATURE_FILE).unlink()
    with pytest.raises(RecordError) as info:
        pkg.read_signature_dir(written)
    assert info.value.reason == "missing"
