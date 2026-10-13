# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Lectura de diagnostics.json y fixture compartido (09-ci REQ-CI-21)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

from aucove import _meta
from aucove.ops.diagnostics import (
    MAX_DIAGNOSTICS,
    MAX_MESSAGE,
    Diagnostic,
    clip_message,
    diagnostics_from_envelope,
    load_diagnostics,
)
from tests.conftest import FIXTURES, REPO_ROOT

SAMPLE = FIXTURES / "diagnostics" / "build-report.sample.json"


def _envelope(findings: list[dict[str, object]], *, mode: str = "build-log") -> dict[str, object]:
    return {
        "schema": _meta.RESULT_SCHEMA,
        "ok": True,
        "op": "validate",
        "data": {
            "valid": True,
            "mode": mode,
            "pr": None,
            "base": {"ref": "main", "sha": None},
            "head": {"ref": "HEAD", "sha": None},
            "results": [{"check": "latex-log", "status": "pass", "findings": findings}],
            "page": {"next_cursor": None, "returned": len(findings), "total": len(findings)},
        },
        "warnings": [],
        "error": None,
    }


def _warning(message: str = "w", line: int | None = None) -> dict[str, object]:
    return {"path": None, "line": line, "severity": "warning", "code": "E_VALIDATION_LATEX",
            "message": message, "pointer": None, "hint": None}


def test_sample_fixture_is_a_valid_envelope() -> None:
    schema = json.loads((REPO_ROOT / "schemas" / "result.schema.json").read_text(encoding="utf-8"))
    env = json.loads(SAMPLE.read_text(encoding="utf-8"))
    errors = [e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(env)]
    assert errors == []
    assert env["data"]["mode"] == "build-log"
    assert [w["code"] for w in env["warnings"]] == ["W_OUTPUT_TRUNCATED"]
    assert "omitted" in env["warnings"][0]["details"]
    raw = SAMPLE.read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")


def test_load_sample_fixture() -> None:
    diags = load_diagnostics(SAMPLE)
    assert diags == [
        Diagnostic(file="sections/methods/methods.tex", line=12, severity="error",
                   message="Undefined control sequence \\foo.", rule="latex-log"),
        Diagnostic(file="tex/main.tex", line=None, severity="warning",
                   message="Label(s) may have changed. Rerun to get cross-references right.", rule="latex-log"),
    ]


def test_missing_invalid_or_big_files(tmp_path: Path) -> None:
    assert load_diagnostics(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    assert load_diagnostics(bad) is None
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"schema": "x/v1"}), encoding="utf-8")
    assert load_diagnostics(other) is None
    tree = tmp_path / "tree.json"
    tree.write_text(json.dumps(_envelope([], mode="tree")), encoding="utf-8")
    assert load_diagnostics(tree) is None
    broken = tmp_path / "broken.json"
    env = _envelope([_warning()])
    env["data"]["results"][0]["findings"][0]["line"] = 0  # type: ignore[index]
    broken.write_text(json.dumps(env), encoding="utf-8")
    assert load_diagnostics(broken) is None
    big = tmp_path / "big.json"
    body = json.dumps(_envelope([_warning()])).encode("utf-8")
    big.write_bytes(body[:-1] + b" " * (5 * 2**20) + body[-1:])  # JSON válido de más de 5 MB
    assert big.stat().st_size > 5 * 2**20
    assert load_diagnostics(big) is None


def test_messages_are_one_clipped_line() -> None:
    assert clip_message("a\nb\r\n  c\td") == "a b c d"
    long = clip_message("x" * 1000)
    assert len(long) <= MAX_MESSAGE and long.endswith(" chars]")
    diags = diagnostics_from_envelope(_envelope([_warning("línea 1\nlínea 2 " + "y" * 400)]))
    assert "\n" not in diags[0].message and len(diags[0].message) <= MAX_MESSAGE


def test_at_most_200_diagnostics() -> None:
    diags = diagnostics_from_envelope(_envelope([_warning(line=i + 1) for i in range(250)]))
    assert len(diags) == MAX_DIAGNOSTICS
    assert diags[-1].line == MAX_DIAGNOSTICS


def test_rejects_non_build_log_envelopes() -> None:
    import pytest

    for env in (
        _envelope([], mode="pr"),
        {**_envelope([]), "op": "doctor"},
        {**_envelope([]), "schema": "otro/v1"},
        {**_envelope([]), "data": None},
    ):
        with pytest.raises(ValueError):
            diagnostics_from_envelope(env)
