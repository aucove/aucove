# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Sobre de resultado (05 §2-§3, REQ-OPS-5, REQ-OPS-6, REQ-OPS-9)."""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path

import jsonschema
import pytest

from aucove import _meta
from aucove.i18n import Translator, translator
from aucove.ops.result import (
    ErrorObj,
    OpError,
    Result,
    WarningObj,
    clip,
    dumps,
    fail,
    internal,
    make_error,
    make_warning,
    ok,
    read_envelope,
)
from tests.conftest import REPO_ROOT

SHA = "0123456789abcdef0123456789abcdef01234567"


@cache
def _validator() -> jsonschema.Draft202012Validator:
    schema = json.loads((REPO_ROOT / "schemas" / "result.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER)


def assert_valid(r: Result) -> None:
    errors = sorted(_validator().iter_errors(r.to_dict()), key=lambda e: list(e.path))
    assert errors == [], [f"{list(e.path)}: {e.message}" for e in errors]


@pytest.fixture
def t() -> Translator:
    return Translator(
        "es",
        catalogs={
            "es": {
                "error.E_VALIDATION_FAILED.message": "Fallaron {0} checks",  # marcador roto: queda literal
                "error.E_CLAIM_ALREADY_CLAIMED.message": "La tarea ya está reclamada.",
                "error.E_CLAIM_ALREADY_CLAIMED.hint": "Elige otra tarea con `status`.",
                "warning.W_NET_OFFLINE": "Sin red: se usó la caché.",
            },
            "en": {"error.E_INTERNAL_UNEXPECTED.message": "Internal error."},
        },
    )


def _validate_data(valid: bool) -> dict[str, object]:
    finding = {
        "path": "sections/methods/methods.tex", "line": 3, "severity": "error",
        "code": "E_VALIDATION_LATEX", "message": "x", "pointer": None, "hint": None,
    }
    return {
        "valid": valid,
        "mode": "tree",
        "pr": None,
        "base": {"ref": "origin/main", "sha": SHA},
        "head": {"ref": "HEAD", "sha": None},
        "results": [{"check": "latex-log", "status": "pass" if valid else "fail", "findings": [] if valid else [finding]}],
        "page": {"next_cursor": None, "returned": 1, "total": 1},
    }


def _check_path_data() -> dict[str, object]:
    return {
        "basis": {"base_ref": "origin/main", "base_sha": SHA, "identity": "carla", "task": 12, "source": "cache"},
        "allowed_all": False,
        "results": [
            {"path": "sections/methods/main.tex", "class": "section", "allowed": True, "reason_code": None,
             "section": "methods", "owner": "carla", "confirm": False, "reason": None},
            {"path": "plan.md", "class": "lead", "allowed": False, "reason_code": "E_PERM_LEAD_ONLY_PATH",
             "section": None, "owner": "ana", "confirm": False, "reason": "Solo el responsable."},
        ],
    }


def test_results_validate_against_schema(t: Translator) -> None:
    offline = make_warning("W_NET_OFFLINE", t)
    samples = [
        ok("validate", _validate_data(True)),
        ok("check-path", _check_path_data(), [offline]),
        fail("validate", make_error("E_VALIDATION_FAILED", t), data=_validate_data(False)),
        fail("claim", make_error("E_CLAIM_ALREADY_CLAIMED", t, details={"task": 12, "claimed_by": "ana"})),
        fail("save", make_error("E_NET_OFFLINE", t),
             data={"failed_step": "push", "steps": [{"id": "commit", "status": "done"}, {"id": "push", "status": "failed"}]}),
        ok("sync-roles", {"dry_run": True, "plan": [{"kind": "file_write", "target": ".github/CODEOWNERS", "summary": "1 archivo"}]}),
        ok("sync-roles", {"check_only": True, "changed": False, "files": [{"path": ".github/CODEOWNERS", "action": "unchanged"}],
                          "diff": None, "unknown_logins": []}),
        internal("doctor", RuntimeError("boom"), t),
    ]
    for r in samples:
        assert_valid(r)


# -- evidence.verify (05 §9.26; D-50) ------------------------------------------------


def _evidence_check(check: str, status: str, source: str | None = None, *, dated: bool = False,
                    reason: str | None = None, detail: str | None = None) -> dict[str, object]:
    return {
        "check": check,
        "source": source,
        "status": status,
        "time_utc": "2026-10-01T12:00:00Z" if dated else None,
        "time_local": "2026-10-01T06:00:00-06:00" if dated else None,
        "reason": reason,
        "detail": detail,
    }


def _evidence_data(verdict: str, confidence: str | None, checks: list[dict[str, object]]) -> dict[str, object]:
    record = {
        "kind": "integration",
        "commit": SHA,
        "manifest_sha256": "ab" * 32,
        "verdict": verdict,
        "confidence": confidence,
        "checks": checks,
    }
    return {"target": f"integration-{SHA}", "verdict": verdict, "confidence": confidence, "records": [record]}


@pytest.mark.parametrize("lang", ["es", "en"])
def test_evidence_verify_envelopes_validate_against_schema(lang: str) -> None:
    """Sobres de ejemplo de 05 §9.26: veredictos, avisos y errores de ``evidence verify``."""
    tr = translator(lang)  # type: ignore[arg-type]
    sound = [_evidence_check("manifest", "pass"), _evidence_check("openings", "pass"), _evidence_check("actors", "pass")]
    strong = _evidence_data("verified", "strong", [
        *sound,
        _evidence_check("tsa", "pass", "freetsa", dated=True),
        _evidence_check("tsa", "weak", "sectigo", dated=True, reason="no_revocation"),
        _evidence_check("ots", "pending", "calendars", reason="pending"),
        _evidence_check("git", "skip", reason="missing_commit", detail="falta el commit: trae refs/pull/*/head"),
    ])
    weak = _evidence_data("verified", "weak", [
        *sound,
        _evidence_check("tsa", "weak", "sectigo", dated=True, reason="no_revocation"),
        _evidence_check("ots", "pending", "bitcoin", reason="bitcoin_unverified"),
    ])
    no_extra = _evidence_data("pending", None, [
        *sound, _evidence_check("tsa", "skip", reason="missing_extra"), _evidence_check("ots", "skip", reason="missing_extra")])
    failed = _evidence_data("failed", None, [
        *sound, _evidence_check("tsa", "fail", "freetsa", reason="digest_mismatch", detail="huella distinta")])
    samples = [
        ok("evidence.verify", strong),
        ok("evidence.verify", weak, [make_warning("W_VALIDATION_EVIDENCE_WEAK", tr)]),
        ok("evidence.verify", no_extra, [make_warning("W_CONFIG_TOOL_MISSING", tr), make_warning("W_NET_OFFLINE", tr)]),
        fail("evidence.verify", make_error("E_VALIDATION_EVIDENCE_FAILED", tr), data=failed),
        fail("evidence.verify", make_error("E_VALIDATION_INPUT", tr, details={"errors": [{"param": "path"}]})),
    ]
    for r in samples:
        assert_valid(r)
    err = samples[3].error
    assert err is not None and (err.retryable, samples[3].exit_code()) == (False, 3)
    assert err.message != "error.E_VALIDATION_EVIDENCE_FAILED.message" and "`data.records[].checks[]`" in (err.hint or "")
    assert all(not w.message.startswith("warning.") for r in samples for w in r.warnings)


def _schema_errors(r: Result) -> list[str]:
    return [e.message for e in _validator().iter_errors(r.to_dict())]


def test_evidence_verify_data_is_closed(t: Translator) -> None:
    """``data_evidence_verify`` no admite campos de más (REQ-OPS-65: sin sales) ni valores fuera de su enum."""
    good = _evidence_data("verified", "strong", [_evidence_check("manifest", "pass"),
                                                  _evidence_check("tsa", "pass", "freetsa", dated=True)])
    assert _schema_errors(ok("evidence.verify", good)) == []
    salted = json.loads(json.dumps(good))
    salted["records"][0]["salts"] = ["00" * 32]
    assert _schema_errors(ok("evidence.verify", salted))
    opened = json.loads(json.dumps(good))
    opened["records"][0]["checks"][0]["salt"] = "00" * 32
    assert _schema_errors(ok("evidence.verify", opened))
    for field, value in (("verdict", "ok"), ("confidence", "high")):
        bad = dict(good, **{field: value})
        assert _schema_errors(ok("evidence.verify", bad)), field
    no_records = {k: v for k, v in good.items() if k != "records"}
    assert _schema_errors(fail("evidence.verify", make_error("E_VALIDATION_EVIDENCE_FAILED", t), data=no_records))


def test_full_data_errors_need_the_complete_data(t: Translator) -> None:
    """05 §12.1: ``E_CONFIG_DOCTOR_FAILED`` y ``E_VALIDATION_EVIDENCE_FAILED`` llevan la ``data`` completa."""
    for op, code in (("doctor", "E_CONFIG_DOCTOR_FAILED"), ("evidence.verify", "E_VALIDATION_EVIDENCE_FAILED")):
        err = make_error(code, t)
        with pytest.raises(ValueError):
            fail(op, err)
        with pytest.raises(ValueError):
            fail(op, err, data={"failed_step": "verify", "steps": []})
    assert fail("validate", make_error("E_VALIDATION_FAILED", t)).data is None  # o details.results


def test_to_dict_order_and_shape(t: Translator) -> None:
    r = fail("claim", make_error("E_CLAIM_ALREADY_CLAIMED", t), warnings=[make_warning("W_NET_OFFLINE", t, details={"a": 1})])
    d = r.to_dict()
    assert list(d) == ["schema", "ok", "op", "data", "warnings", "error"]
    assert d["schema"] == _meta.RESULT_SCHEMA
    assert d["error"] == {
        "code": "E_CLAIM_ALREADY_CLAIMED",
        "message": "La tarea ya está reclamada.",
        "retryable": False,
        "hint": "Elige otra tarea con `status`.",
    }
    assert d["warnings"] == [{"code": "W_NET_OFFLINE", "message": "Sin red: se usó la caché.", "details": {"a": 1}}]
    assert ok("doctor", {}).to_dict()["warnings"] == []


def test_message_and_hint_come_from_catalog(t: Translator) -> None:
    err = make_error("E_CLAIM_ALREADY_CLAIMED", t, details={"task": 3})
    assert err.message == "La tarea ya está reclamada."
    assert err.hint == "Elige otra tarea con `status`."
    assert err.details == {"task": 3}
    assert err.retryable is False
    missing = make_error("E_STATE_BUSY", t)  # sin texto: la clave; sin hint: None
    assert missing.message == "error.E_STATE_BUSY.message"
    assert missing.hint is None and missing.retryable is True
    assert make_error("E_VALIDATION_FAILED", t).message == "Fallaron {0} checks"


def test_dumps_is_one_compact_line(t: Translator) -> None:
    r = ok("validate", _validate_data(True), [make_warning("W_NET_OFFLINE", t)])
    raw = dumps(r)
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    assert not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")
    assert "caché" in text  # UTF-8, sin escapes \\u
    assert text[:-1] == json.dumps(r.to_dict(), ensure_ascii=False, separators=(",", ":"))
    assert json.loads(text) == r.to_dict()


def test_multiline_texts_stay_on_one_line() -> None:
    r = ok("doctor", {"note": "a\nb\r\nc"})
    assert dumps(r).count(b"\n") == 1


def test_exit_codes(t: Translator) -> None:
    assert ok("doctor", {}).exit_code() == 0
    assert fail("validate", make_error("E_VALIDATION_FAILED", t)).exit_code() == 3
    assert fail("claim", make_error("E_CLAIM_ALREADY_CLAIMED", t)).exit_code() == 6
    assert internal("doctor", ValueError(), t).exit_code() == 1


@pytest.mark.parametrize("factory", ["error", "warning", "operror"])
def test_unknown_code_raises(t: Translator, factory: str) -> None:
    with pytest.raises(ValueError):
        if factory == "error":
            make_error("E_NOT_IN_CATALOG", t)
        elif factory == "warning":
            make_warning("W_NOT_IN_CATALOG", t)
        else:
            OpError("E_NOT_IN_CATALOG")
    with pytest.raises(ValueError):
        make_error("W_NET_OFFLINE", t)  # un aviso no es un error
    with pytest.raises(ValueError):
        make_warning("E_VALIDATION_INPUT", t)


def test_markers_must_match_params(t: Translator) -> None:
    # En H2 ningún código tiene marcadores: cualquier kwarg de formato es un error de contrato.
    with pytest.raises(ValueError):
        make_error("E_CLAIM_ALREADY_CLAIMED", t, task=12)
    with pytest.raises(ValueError):
        make_warning("W_NET_OFFLINE", t, age=3)
    with pytest.raises(ValueError):
        OpError("E_CONFIG_INVALID", path="project.yaml")


def test_operror_attributes() -> None:
    w = WarningObj("W_NET_OFFLINE", "x")
    e = OpError("E_CONFIG_INVALID", details={"path": "roles.yaml"}, data={"a": 1}, warnings=[w])
    assert (e.code, e.details, e.data, e.warnings, e.fmt) == ("E_CONFIG_INVALID", {"path": "roles.yaml"}, {"a": 1}, (w,), {})
    assert str(e) == "E_CONFIG_INVALID"


def test_internal_hides_exception_text(t: Translator) -> None:
    r = internal("doctor", RuntimeError("token=ghp_secret /home/ana"), t)
    raw = dumps(r).decode("utf-8")
    assert "ghp_secret" not in raw and "/home/ana" not in raw and "RuntimeError" not in raw
    assert r.error is not None and r.error.code == "E_INTERNAL_UNEXPECTED"
    assert re.fullmatch(r"[0-9a-f]{12}", r.error.details["trace_id"])  # type: ignore[index]
    assert r.data is None and r.ok is False


def test_result_invariants() -> None:
    err = ErrorObj("E_INTERNAL_UNEXPECTED", "x", False, None)
    with pytest.raises(ValueError):
        Result(op="doctor", ok=True, data={}, error=err)
    with pytest.raises(ValueError):
        Result(op="doctor", ok=False, data=None)
    assert Result(op="doctor", ok=True, data={}, warnings=[]).warnings == ()  # type: ignore[arg-type]


def test_clip_marker() -> None:
    assert clip("abc", 10) == "abc"
    long = "x" * 1000
    clipped = clip(long, 500)
    assert len(clipped) <= 500
    m = re.search(r" \[\.\.\.truncated (\d+) chars\]$", clipped)
    assert m is not None
    assert len(clipped) - len(m.group(0)) + int(m.group(1)) == 1000
    long_msg = Translator("en", catalogs={"en": {"error.E_STATE_BUSY.message": "y" * 900}})
    assert len(make_error("E_STATE_BUSY", long_msg).message) <= 500


def test_read_envelope(tmp_path: Path, t: Translator) -> None:
    good = tmp_path / "good.json"
    good.write_bytes(dumps(ok("validate", _validate_data(True))))
    assert read_envelope(good) == ok("validate", _validate_data(True)).to_dict()
    bom = tmp_path / "bom.json"
    bom.write_bytes(b"\xef\xbb\xbf" + dumps(ok("doctor", {})))
    assert read_envelope(bom) is not None
    assert read_envelope(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_bytes(b"{not json")
    assert read_envelope(bad) is None
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"schema": "other/v1"}), encoding="utf-8")
    assert read_envelope(other) is None
    arr = tmp_path / "arr.json"
    arr.write_text("[1, 2]", encoding="utf-8")
    assert read_envelope(arr) is None
    big = tmp_path / "big.json"
    big.write_bytes(dumps(ok("doctor", {"pad": "z" * 2000})))
    assert read_envelope(big, max_bytes=1000) is None
    assert read_envelope(big) is not None
