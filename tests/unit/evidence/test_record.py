# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Registro de evidencia: carpeta ``<kind>-<commit>/`` y comentario ``ci:evidence``.

11-trazabilidad §8.5, 09-ci REQ-CI-54 y esquema ``evidence-record/v1``. Las pruebas TSA y
OTS son bytes opacos: aquí bastan bytes cualesquiera.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from aucove.evidence import canon
from aucove.evidence.openings import ActorOpening, EntryOpening, Openings
from aucove.evidence.record import (
    COMMENT_LIMIT,
    MANIFEST_FILE,
    OPEN_SCRIPT,
    OPENINGS_FILE,
    OTS_FILE,
    RECORD_FILE,
    RECORD_SCHEMA,
    VERIFY_FILE,
    Proof,
    Record,
    RecordError,
    Revocation,
    artifact_name,
    comment_json,
    find_record_dirs,
    load_comment_json,
    local_dirname,
    parse_record,
    read_record_dir,
    record_dirname,
    record_files,
    write_record_dir,
)
from tests.unit.evidence.support import validate

COMMIT = "c0ffee" + "0" * 34
MANIFEST = canon.dumps({"schema": "x", "entries": [], "commit": COMMIT})
SHA = hashlib.sha256(MANIFEST).hexdigest()


def openings(n: int = 2, path: str = "sección/año.txt") -> Openings:
    entries = tuple(
        EntryOpening(path=f"{path}.{i}", salt=hashlib.sha256(b"e%d" % i).digest(), blob="1" * 40, size=i)
        for i in range(n)
    )
    actors = (ActorOpening(role="author", value="Ana Pérez <ana@example.invalid>", salt=bytes(32)),)
    return Openings(manifest_sha256=SHA, repository="org/repo", entries=entries, actors=actors)


def proofs(revocation_size: int = 16) -> tuple[Proof, ...]:
    return (
        Proof.ots(b"\x00OpenTimestamps\x00proof"),
        Proof.rfc3161(
            "freetsa",
            b"TSR" * 100,
            "2026-10-06T18:00:01.25Z",
            revocation=Revocation("ocsp", b"\x30" * revocation_size),
            chain_pem=b"-----BEGIN CERTIFICATE-----\nAA==\n-----END CERTIFICATE-----\n",
            root_pem=b"root",
        ),
        Proof.failed("rfc3161", "sectigo", "timeout\n" + "x" * 400),
    )


def record(**kwargs: object) -> Record:
    values: dict[str, object] = {
        "kind": "integration",
        "commit": COMMIT,
        "pr": 12,
        "task": 7,
        "plan": False,
        "manifest_sha256": SHA,
        "run_id": 123456,
        "run_attempt": 2,
        "proofs": proofs(),
        "manifest": MANIFEST,
        "openings": openings(),
    }
    values.update(kwargs)
    return Record(**values)  # type: ignore[arg-type]


def test_names() -> None:
    assert record_dirname("submission", COMMIT) == f"submission-{COMMIT}"
    assert local_dirname("integration", COMMIT, 3) == f"integration-{COMMIT}-a3"
    assert artifact_name("integration", COMMIT, 3) == f"evidence-integration-{COMMIT}-a3"
    rec = record()
    assert rec.artifact == f"evidence-integration-{COMMIT}-a2" and rec.dirname == f"integration-{COMMIT}"
    bad_names = [("other", COMMIT, 1), ("integration", "HEAD", 1), ("integration", COMMIT, 0), ("integration", COMMIT, 1000)]
    for bad in bad_names:
        with pytest.raises(RecordError):
            artifact_name(*bad)  # type: ignore[arg-type]


def test_proof_rules() -> None:
    failed = Proof.failed("rfc3161", "sectigo", "a\nb   c" + "x" * 500)
    error = failed.error or ""
    assert failed.file is None and len(error) == 300 and error.startswith("a b c") and error.endswith("…")
    assert Proof.ots(b"x").file == OTS_FILE and proofs()[1].file == "tsa/freetsa.tsr"
    bad = [
        {"type": "ots", "source": "freetsa", "status": "ok"},
        {"type": "ots", "source": "calendars", "status": "ok", "time_utc": "2026-10-06T18:00:00Z"},
        {"type": "rfc3161", "source": "../x", "status": "ok"},
        {"type": "rfc3161", "source": "Freetsa", "status": "ok"},
        {"type": "rfc3161", "source": "freetsa", "status": "error"},
        {"type": "rfc3161", "source": "freetsa", "status": "error", "error": "e", "data": b"x"},
        {"type": "rfc3161", "source": "freetsa", "status": "ok", "error": "e"},
        {"type": "rfc3161", "source": "freetsa", "status": "ok", "time_utc": "2026-10-06 18:00:00Z"},
        {"type": "sigstore", "source": "rekor", "status": "ok"},
    ]
    for kwargs in bad:
        with pytest.raises(RecordError):
            Proof(**kwargs)  # type: ignore[arg-type]


def test_record_rules() -> None:
    cases: list[dict[str, object]] = [
        {"proofs": ()},
        {"proofs": proofs() * 2},
        {"run_attempt": 0},
        {"pr": 0},
        {"plan": 1},
        {"manifest_sha256": "AB" * 32},
    ]
    for kwargs in cases:
        with pytest.raises(RecordError):
            record(**kwargs)


def test_record_and_comment_documents_validate() -> None:
    rec = record()
    stored = rec.document(embed=False)
    assert validate("evidence-record", stored) == []
    assert stored["manifest"] is None and stored["openings"] is None
    assert all(p["data"] is None for p in stored["proofs"])
    assert stored["proofs"][1]["revocation"] == {"type": "ocsp", "file": "tsa/freetsa.ocsp.der", "data": None}
    body = comment_json(rec)
    assert not body.truncated and body.dropped == ()
    full = json.loads(body.text)
    assert validate("evidence-record", full) == []
    assert full["manifest"].encode() == MANIFEST and full["openings"] == rec.openings.to_json()  # type: ignore[union-attr]
    assert base64.b64decode(full["proofs"][1]["data"]) == b"TSR" * 100
    assert validate("evidence-openings", full["openings"]) == []


def test_comment_roundtrip() -> None:
    rec = record()
    again = load_comment_json(comment_json(rec).text)
    # la cadena y la raíz de la TSA solo van en la carpeta
    expected = replace(
        rec, proofs=tuple(replace(p, chain_pem=None, root_pem=None) for p in rec.proofs)
    )
    assert again == expected


def test_comment_escapes_html_and_is_ascii() -> None:
    rec = record(openings=openings(1, path="a-->b <script>&"))
    text = comment_json(rec).text
    assert text.isascii()
    assert "-->" not in text and "<" not in text and ">" not in text and "&" not in text
    assert load_comment_json(text).openings.entries[0].path == "a-->b <script>&.0"  # type: ignore[union-attr]


def _comment_size(rec: Record) -> int:
    return len(comment_json(rec, limit=10**9).text)


def test_truncation_drops_openings_first() -> None:
    rec = record(openings=openings(400))
    assert _comment_size(rec) > COMMENT_LIMIT
    body = comment_json(rec)
    assert body.truncated and body.dropped == ("openings",)
    assert len(body.text) <= COMMENT_LIMIT
    doc = json.loads(body.text)
    assert doc["truncated"] is True and doc["openings"] is None and doc["manifest"] is not None
    assert doc["proofs"][1]["revocation"]["data"] is not None
    assert validate("evidence-record", doc) == []


def test_truncation_then_manifest_then_revocation() -> None:
    big_manifest = canon.dumps({"entries": [{"c": None, "p": f"{i:064x}"} for i in range(600)]})
    sha = hashlib.sha256(big_manifest).hexdigest()
    rec = record(
        manifest=big_manifest,
        manifest_sha256=sha,
        openings=replace(openings(400), manifest_sha256=sha),
        proofs=proofs(revocation_size=50_000),
    )
    body = comment_json(rec)
    assert body.dropped == ("openings", "manifest", "revocation")
    assert len(body.text) <= COMMENT_LIMIT
    doc = json.loads(body.text)
    assert doc["openings"] is None and doc["manifest"] is None
    assert doc["proofs"][1]["revocation"] == {"type": "ocsp", "file": "tsa/freetsa.ocsp.der", "data": None}
    assert doc["proofs"][1]["data"] is not None  # la prueba misma sigue
    assert doc["manifest_sha256"] == sha and validate("evidence-record", doc) == []
    # el orden es fijo: con espacio para el manifiesto, solo se quitan las aperturas
    small = comment_json(rec, limit=_comment_size(replace(rec, openings=None)))
    assert small.dropped == ("openings",)


def test_truncation_last_resort_drops_proof_data() -> None:
    rec = record(proofs=(Proof.ots(b"\x01" * 60_000),))
    body = comment_json(rec)
    assert body.dropped == ("openings", "manifest", "proofs")  # no había OCSP ni CRL que quitar
    doc = json.loads(body.text)
    assert doc["proofs"][0]["data"] is None and doc["proofs"][0]["file"] == OTS_FILE
    assert len(body.text) <= COMMENT_LIMIT


def test_write_and_read_record_dir(tmp_path: Path) -> None:
    rec = record()
    extra = {VERIFY_FILE: "# Cómo verificar\n".encode(), OPEN_SCRIPT: b"print('ok')\n"}
    folder = write_record_dir(tmp_path / "out", rec, extra=extra)
    assert folder.name == f"integration-{COMMIT}"
    files = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())
    assert files == sorted(
        [
            RECORD_FILE, MANIFEST_FILE, OPENINGS_FILE, OTS_FILE, VERIFY_FILE, OPEN_SCRIPT,
            "tsa/freetsa.tsr", "tsa/freetsa.chain.pem", "tsa/freetsa.root.pem", "tsa/freetsa.ocsp.der",
        ]
    )
    assert (folder / MANIFEST_FILE).read_bytes() == MANIFEST  # bytes exactos, sin salto final
    assert (folder / OPENINGS_FILE).read_bytes() == rec.openings.dumps()  # type: ignore[union-attr]
    stored = json.loads((folder / RECORD_FILE).read_bytes())
    assert stored == rec.document(embed=False) and stored["schema"] == RECORD_SCHEMA
    assert validate("evidence-record", stored) == []
    for name in (RECORD_FILE, OPENINGS_FILE):
        raw = (folder / name).read_bytes()
        assert raw.endswith(b"\n") and b"\r" not in raw and not raw.startswith(b"\xef\xbb\xbf")
    read = read_record_dir(folder)
    assert read.problems == () and read.record == rec
    assert not list((tmp_path / "out").glob(".*"))  # sin carpetas temporales
    with pytest.raises(FileExistsError):
        write_record_dir(tmp_path / "out", rec)


def test_read_record_dir_reports_missing_and_invalid_files(tmp_path: Path) -> None:
    folder = write_record_dir(tmp_path, record())
    (folder / "tsa" / "freetsa.ocsp.der").unlink()
    (folder / MANIFEST_FILE).unlink()
    (folder / OPENINGS_FILE).write_bytes(b"{no es json")
    read = read_record_dir(folder)
    assert sorted(read.problems) == sorted(
        [(MANIFEST_FILE, "missing"), (OPENINGS_FILE, "invalid"), ("tsa/freetsa.ocsp.der", "missing")]
    )
    assert read.record.manifest is None and read.record.openings is None
    assert read.record.proofs[1].revocation == Revocation("ocsp", None)
    assert read.record.proofs[1].data == b"TSR" * 100
    (folder / RECORD_FILE).unlink()
    with pytest.raises(RecordError) as info:
        read_record_dir(folder)
    assert info.value.reason == "missing"


HOSTILE_JSON: tuple[bytes, ...] = (
    b'{"schema": 1, "run_id": ' + b"1" * 5000 + b"}",  # int() de más de 4300 cifras: ValueError
    b'{"kind": "integration", "n": ' + b"9" * 5000 + b"}",
    b"[" * 100_000 + b"]" * 100_000,  # anidamiento: RecursionError
    b'{"proofs": ' + b"[" * 5000 + b"]" * 5000 + b"}",
    b"\xff\xfe{}",  # no es UTF-8
)


def test_hostile_json_is_a_record_error_not_a_crash(tmp_path: Path) -> None:
    """``load_comment_json`` y ``read_record_dir`` convierten los ``ValueError`` (enteros
    de más de 4300 cifras) y los ``RecursionError`` en ``RecordError("invalid")``."""
    for i, data in enumerate(HOSTILE_JSON):
        for text in (data, data.decode("utf-8", "replace")):
            with pytest.raises(RecordError) as info:
                load_comment_json(text)
            assert info.value.reason == "invalid", data[:30]
        folder = tmp_path / f"integration-{COMMIT}-a{i + 1}"
        folder.mkdir()
        (folder / RECORD_FILE).write_bytes(data)
        with pytest.raises(RecordError) as info:
            read_record_dir(folder)
        assert info.value.reason == "invalid", data[:30]


def test_local_copy_of_a_truncated_comment(tmp_path: Path) -> None:
    rec = record(openings=openings(400))
    body = comment_json(rec)
    copy = load_comment_json(body.text)
    assert copy.truncated and copy.openings is None and copy.manifest == MANIFEST
    folder = write_record_dir(tmp_path / ".x" / "evidence", copy, name=copy.local_dirname)
    assert folder.name == f"integration-{COMMIT}-a2"
    assert not (folder / OPENINGS_FILE).exists() and (folder / MANIFEST_FILE).read_bytes() == MANIFEST
    read = read_record_dir(folder)
    assert read.record.truncated and (OPENINGS_FILE, "missing") in read.problems
    assert read.record.proofs[1].revocation.data == b"\x30" * 16  # type: ignore[union-attr]


def test_record_files_checks_consistency_and_extra_names() -> None:
    with pytest.raises(RecordError) as info:
        record_files(record(manifest=MANIFEST + b" "))
    assert info.value.reason == "inconsistent"
    with pytest.raises(RecordError) as info:
        record_files(record(openings=replace(openings(), manifest_sha256="0" * 64)))
    assert info.value.reason == "inconsistent"
    for name in ("../x", "/abs", "C:x", "a\\b", RECORD_FILE, MANIFEST_FILE, "tsa/x.tsr", "", "a/./b"):
        with pytest.raises(RecordError):
            record_files(record(), extra={name: b"x"})
    assert record_files(record(), extra={"docs/LEEME.md": b"x"})["docs/LEEME.md"] == b"x"


def test_parse_record_rejects_tampered_names() -> None:
    doc = json.loads(comment_json(record()).text)
    for pointer, mutate in (
        ("/proofs/1/file", lambda d: d["proofs"][1].update(file="../../etc/passwd")),
        ("/proofs/1/revocation/file", lambda d: d["proofs"][1]["revocation"].update(file="tsa/otra.ocsp.der")),
        ("/artifact", lambda d: d.update(artifact="evidence-x")),
        ("/schema", lambda d: d.update(schema="x")),
        ("/proofs/0/data", lambda d: d["proofs"][0].update(data="no es base64!")),
        ("/proofs/2/error", lambda d: d["proofs"][2].update(error=None)),
        ("/openings/entries/0/salt", lambda d: d["openings"]["entries"][0].update(salt="x")),
    ):
        broken = json.loads(json.dumps(doc))
        mutate(broken)
        with pytest.raises(RecordError) as info:
            parse_record(broken)
        assert info.value.pointer == pointer


def test_find_record_dirs(tmp_path: Path) -> None:
    rec = record()
    package = tmp_path / "paquete"
    a = write_record_dir(package / f"evidence-integration-{COMMIT}-a1", rec)
    b = write_record_dir(package / "local", replace(rec, kind="submission"), name="submission-x-a1")
    (package / "vacia" / "mas" / "hondo").mkdir(parents=True)
    assert find_record_dirs(package) == sorted([a, b])
    assert find_record_dirs(a) == [a]
    assert find_record_dirs(tmp_path, max_depth=1) == []
