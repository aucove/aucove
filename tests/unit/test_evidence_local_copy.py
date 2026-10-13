# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Copia local de los registros ``ci:evidence`` (05 REQ-OPS-66; evidence/local_copy.py)."""

from __future__ import annotations

from pathlib import Path

from aucove.evidence.local_copy import copy_comment, evidence_json, parse_evidence_comment
from aucove.evidence.record import (
    MANIFEST_FILE,
    OPENINGS_FILE,
    OTS_FILE,
    RECORD_FILE,
    comment_json,
    read_record_dir,
)
from aucove.ops import ci_seal
from tests.unit.evidence.test_record import COMMIT, MANIFEST, openings, record

CI = ("github-actions[bot]",)


def body_of(rec: object, *, attempt: int | None = None) -> str:
    text = comment_json(rec).text  # type: ignore[arg-type]
    head = ci_seal.evidence_marker(rec.kind, rec.commit, attempt or rec.run_attempt)  # type: ignore[attr-defined]
    return f"{head}\n{text}\n-->\nSellado el 2026-10-08.\n"


def test_parse_marker_and_the_ci_seal_alias() -> None:
    rec = record()
    body = body_of(rec)
    parsed = parse_evidence_comment(body)
    assert parsed is not None and (parsed.kind, parsed.sha, parsed.attempt) == ("integration", COMMIT, 2)
    assert evidence_json(body) == parsed.json_text == ci_seal.evidence_json(body)
    assert parse_evidence_comment("hola") is None
    assert parse_evidence_comment(body.replace("-->", "--", 1)) is None
    assert parse_evidence_comment("\N{ZERO WIDTH NO-BREAK SPACE}" + body.replace("\n", "\r\n")) is not None


def test_copy_writes_the_record_folder_once(tmp_path: Path) -> None:
    rec = record()
    root = tmp_path / "evidence"
    first = copy_comment(root, body_of(rec), "github-actions[bot]", ci_actors=CI)
    assert first.outcome == "copied" and first.folder == f"integration-{COMMIT}-a2"
    folder = root / first.folder
    assert (folder / MANIFEST_FILE).read_bytes() == MANIFEST
    assert (folder / OPENINGS_FILE).is_file() and (folder / OTS_FILE).is_file()
    read = read_record_dir(folder)
    assert read.problems == () or not any(code != "missing" for _f, code in read.problems)
    before = (folder / RECORD_FILE).read_bytes()
    again = copy_comment(root, body_of(rec), "github-actions[bot]", ci_actors=CI)
    assert again.outcome == "exists" and (folder / RECORD_FILE).read_bytes() == before


def test_truncated_comment_copies_only_record_json(tmp_path: Path) -> None:
    rec = record(openings=openings(400))
    root = tmp_path / "evidence"
    out = copy_comment(root, body_of(rec), "github-actions[bot]", ci_actors=CI)
    assert out.outcome == "copied_truncated"
    files = sorted(p.relative_to(root / out.folder).as_posix() for p in (root / out.folder).rglob("*") if p.is_file())  # type: ignore[operator]
    assert files == [RECORD_FILE]


def test_untrusted_author_and_mismatched_marker_are_not_copied(tmp_path: Path) -> None:
    rec = record()
    root = tmp_path / "evidence"
    assert copy_comment(root, body_of(rec), "carla", ci_actors=CI).outcome == "untrusted_author"
    assert copy_comment(root, body_of(rec, attempt=3), "github-actions[bot]", ci_actors=CI).outcome == "invalid"
    broken = body_of(rec).replace('"kind"', '"knd"', 1)
    assert copy_comment(root, broken, "github-actions[bot]", ci_actors=CI).outcome == "invalid"
    assert copy_comment(root, "texto", "github-actions[bot]", ci_actors=CI).outcome == "not_evidence"
    assert not root.exists()
