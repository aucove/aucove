# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Catálogo cerrado de errores, avisos y salidas (05 §5, §12; D-39)."""

from __future__ import annotations

import json
from functools import cache

import pytest

from aucove.domain import errors
from aucove.i18n import LANGS, load_catalog, placeholders
from tests.conftest import REPO_ROOT


@cache
def _defs() -> dict[str, object]:
    schema = json.loads((REPO_ROOT / "schemas" / "result.schema.json").read_text(encoding="utf-8"))
    return schema["$defs"]


def _enum(name: str) -> list[str]:
    return list(_defs()[name]["enum"])  # type: ignore[index]


def test_errors_equal_schema_enum() -> None:
    assert list(errors.ERRORS) == _enum("error_code")
    assert len(errors.ERRORS) == 81  # E_CONFIG_TOOL_MISSING y E_CONFIG_EVIDENCE_KEY (D-72)


def test_warnings_equal_schema_enum() -> None:
    assert errors.WARNINGS == frozenset(_enum("warning_code"))
    assert list(errors.WARNING_ORDER) == _enum("warning_code")
    assert len(errors.WARNINGS) == 30
    assert "W_FORGE_ACTIONS_RESTRICTED" in errors.WARNINGS  # reservado por D-39


def test_evidence_verify_codes() -> None:
    """05 §9.26 y §12: el error y los avisos de ``evidence verify`` (D-50)."""
    spec = errors.ERRORS["E_VALIDATION_EVIDENCE_FAILED"]
    assert (spec.area, spec.retryable, spec.params) == ("VALIDATION", False, frozenset())
    assert errors.exit_code(False, "E_VALIDATION_EVIDENCE_FAILED") == 3
    assert {"W_CONFIG_TOOL_MISSING", "W_VALIDATION_EVIDENCE_WEAK", "W_NET_OFFLINE"} <= errors.WARNINGS


def test_perm_codes_equal_schema_enum() -> None:
    assert errors.PERM_CODES == frozenset(_enum("perm_code"))
    assert errors.PERM_CODES <= set(errors.ERRORS)


def test_params_are_empty_in_h2() -> None:
    assert all(spec.params == frozenset() for spec in errors.ERRORS.values())
    assert set(errors.WARNING_PARAMS) == errors.WARNINGS
    assert all(p == frozenset() for p in errors.WARNING_PARAMS.values())


def test_spec_fields_consistent() -> None:
    for code, spec in errors.ERRORS.items():
        assert spec.code == code
        assert code.startswith("E_")
        if code.startswith("E_HUMAN_ONLY_"):
            assert spec.area == "HUMAN_ONLY"
        else:
            assert spec.area == code.split("_")[1]
        assert errors.area_of(code) == spec.area


def test_area_of_unknown_raises() -> None:
    with pytest.raises(KeyError):
        errors.area_of("E_NOPE_CODE")


def test_retryable_column() -> None:
    retryable = {c for c, s in errors.ERRORS.items() if s.retryable}
    assert retryable == {
        "E_FORGE_RATE_LIMITED",
        "E_FORGE_UNAVAILABLE",
        "E_FORGE_CONFLICT",
        "E_NET_OFFLINE",
        "E_NET_TIMEOUT",
        "E_STATE_BUSY",
    }


def test_exit_codes() -> None:
    assert dict(errors.EXIT_BY_AREA) == {
        "INTERNAL": 1, "VALIDATION": 3, "PERM": 4, "AUTH": 5, "STATE": 6,
        "CLAIM": 6, "FORGE": 7, "NET": 7, "CONFIG": 8, "HUMAN_ONLY": 9,
    }
    assert set(errors.EXIT_BY_AREA) == set(errors.AREAS)
    assert errors.exit_code(True, None) == 0
    assert errors.exit_code(True, "E_INTERNAL_UNEXPECTED") == 0
    assert errors.exit_code(False, None) == 1
    assert errors.exit_code(False, "E_UNKNOWN_THING") == 1
    assert errors.exit_code(False, "E_VALIDATION_INPUT") == 3
    assert errors.exit_code(False, "E_HUMAN_ONLY_NO_TTY") == 9
    assert errors.exit_code(False, "E_CONFIG_NOT_A_PROJECT") == 8


def test_exit_code_never_2() -> None:
    codes = [*errors.ERRORS, None, "E_UNKNOWN_THING", "", "W_NET_OFFLINE"]
    for ok in (True, False):
        for code in codes:
            assert errors.exit_code(ok, code) != 2


# 05 §12.3: las 16 filas (17 kinds; no_commits y validation comparten fila).
FORGE_TABLE = {
    "no_token": "E_AUTH_NOT_AUTHENTICATED",
    "auth": "E_AUTH_TOKEN_INVALID",
    "permission": "E_AUTH_INSUFFICIENT_SCOPE",
    "login_changed": "E_AUTH_LOGIN_CHANGED",
    "not_found": "E_FORGE_NOT_FOUND",
    "ref_conflict": "E_FORGE_CONFLICT",
    "merge_conflict": "E_STATE_MERGE_CONFLICT",
    "no_commits": "E_FORGE_UNEXPECTED",
    "validation": "E_FORGE_UNEXPECTED",
    "unsupported": "E_FORGE_CAPABILITY_MISSING",
    "rate_limited": "E_FORGE_RATE_LIMITED",
    "unavailable": "E_FORGE_UNAVAILABLE",
    "timeout": "E_NET_TIMEOUT",
    "unreachable": "E_NET_OFFLINE",
    "tls": "E_NET_TLS",
}


@pytest.mark.parametrize("kind", sorted(FORGE_TABLE))
def test_code_for_forge_kind(kind: str) -> None:
    for op in ("claim", "save", "submit", "task.propose"):
        assert errors.code_for_forge_kind(kind, op) == FORGE_TABLE[kind]


def test_forge_kind_special_rows() -> None:
    assert errors.code_for_forge_kind("pr_exists", "submit") is None
    assert errors.code_for_forge_kind("ref_exists", "claim") is None
    for op in ("release", "save", "task.cancel"):
        assert errors.code_for_forge_kind("ref_exists", op) == "E_FORGE_CONFLICT"
    assert errors.code_for_forge_kind("brand_new_kind", "claim") == "E_FORGE_UNEXPECTED"


def test_forge_table_covers_all_rows() -> None:
    assert set(errors.FORGE_KIND_CODES) == set(FORGE_TABLE) | {"pr_exists", "ref_exists"}
    assert len(errors.FORGE_KIND_CODES) == 17
    for code in errors.FORGE_KIND_CODES.values():
        assert code is None or code in errors.ERRORS


def test_core_catalog_markers_match_params() -> None:
    """Las claves error.* y warning.* existentes usan exactamente los marcadores de ``params``."""
    for lang in LANGS:
        catalog = load_catalog(lang)
        for key, text in catalog.items():
            if key.startswith("error."):
                _prefix, code, suffix = key.split(".")
                assert code in errors.ERRORS, key
                assert suffix in ("message", "hint"), key
                assert placeholders(text) == errors.ERRORS[code].params, key
            elif key.startswith("warning."):
                code = key.split(".", 1)[1]
                assert code in errors.WARNINGS, key
                assert placeholders(text) == errors.WARNING_PARAMS[code], key


def test_core_catalog_complete_when_present() -> None:
    core = REPO_ROOT / "src" / "aucove" / "i18n" / "catalogs"
    if not all((core / lang / "core.toml").is_file() for lang in LANGS):
        pytest.skip("core.toml aún no existe en los dos idiomas")
    for lang in LANGS:
        catalog = load_catalog(lang)
        missing = [c for c in errors.ERRORS if f"error.{c}.message" not in catalog]
        missing += [w for w in errors.WARNING_ORDER if f"warning.{w}" not in catalog]
        assert missing == [], f"{lang}: {missing}"
