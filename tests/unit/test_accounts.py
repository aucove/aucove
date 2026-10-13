# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Matriz nivel de dato × tipo de cuenta (10-seguridad §8.3 y §8.4; REQ-SEC-64; D-48)."""

from __future__ import annotations

import itertools
import os
import re
from pathlib import Path

import pytest

from aucove import _meta
from aucove.config import schemas
from aucove.config.accounts import ACCOUNT_TYPES, ALLOWED, CONSUMER_ACCOUNTS, account_verdict

LEVELS = ("public", "internal", "restricted", "local_only")


def _pseudocode(account_type: str, level: str, require_commercial: bool) -> str:
    """El ``check_accounts`` de 10 §8.4, transcrito: ``error``, ``warning`` u ``ok``."""
    allowed = {
        "public": {"consumer_training_on", "consumer_training_off", "team", "enterprise", "api", "cloud_provider",
                   "local", "none", "unknown"},
        "internal": {"consumer_training_off", "team", "enterprise", "api", "cloud_provider", "local", "none"},
    }
    if level not in allowed:
        return "error"
    if account_type not in allowed[level] or (account_type.startswith("consumer") and require_commercial):
        return "error"
    if account_type == "consumer_training_off" and level == "internal":
        return "warning"
    return "ok"


SEVERITY = {"allowed": "ok", "consumer_terms": "warning", "not_allowed": "error", "commercial_required": "error"}


@pytest.mark.parametrize(
    ("account_type", "level", "require_commercial"),
    list(itertools.product(ACCOUNT_TYPES, LEVELS, (True, False))),
)
def test_verdict_follows_the_pseudocode(account_type: str, level: str, require_commercial: bool) -> None:
    verdict = account_verdict(account_type, level, require_commercial)
    assert SEVERITY[verdict] == _pseudocode(account_type, level, require_commercial)


def test_reasons_of_each_error() -> None:
    assert account_verdict("unknown", "internal", False) == "not_allowed"
    assert account_verdict("consumer_training_on", "internal", False) == "not_allowed"  # ✗ aunque sea false
    assert account_verdict("consumer_training_off", "internal", True) == "commercial_required"
    assert account_verdict("consumer_training_on", "public", True) == "commercial_required"
    assert account_verdict("consumer_training_off", "internal", False) == "consumer_terms"  # C1
    assert account_verdict("consumer_training_off", "public", False) == "allowed"
    assert account_verdict("unknown", "public", True) == "allowed"
    assert account_verdict("team", "restricted", False) == "not_allowed"  # no admitido en v0.1


def test_account_types_are_the_schema_enum() -> None:
    people = schemas.schema("roles")["$defs"]["person"]["properties"]
    assert tuple(people["account_type"]["enum"]) == ACCOUNT_TYPES
    assert CONSUMER_ACCOUNTS == {a for a in ACCOUNT_TYPES if a.startswith("consumer")}
    levels = schemas.schema("project")["$defs"]["data_policy"]["properties"]["level"]["enum"]
    assert set(ALLOWED) == set(levels)  # restricted y local_only, reservados (REQ-SEC-63)


def test_matrix_is_the_table_of_the_spec_when_available() -> None:
    """Celdas ✓ y C de 10 §8.3 para public e internal; las demás filas no se admiten en v0.1."""
    spec_dir = os.environ.get(_meta.ENV_PREFIX + "SPEC_DIR")
    if not spec_dir:
        pytest.skip("sin la especificación")
    text = (Path(spec_dir) / "10-seguridad.md").read_text(encoding="utf-8")
    start = text.index("### 8.3 ")
    rows = [line for line in text[start : text.index("\n### ", start + 1)].splitlines() if line.startswith("|")]
    header = [c.strip().strip("`") for c in rows[0].strip("|").split("|")][1:]
    assert tuple(header) == ACCOUNT_TYPES
    table: dict[str, dict[str, str]] = {}
    for line in rows[2:]:
        cells = [c.strip() for c in line.strip("|").split("|")]
        table[cells[0].strip("`")] = dict(zip(header, cells[1:], strict=True))
    assert set(table) == set(LEVELS)
    for level, cells in table.items():
        admitted = {a for a, mark in cells.items() if mark == "✓" or re.fullmatch(r"C[0-9]", mark)}
        if level in ALLOWED:
            assert admitted == ALLOWED[level], level
        else:
            assert not {a for a, mark in cells.items() if mark == "✓"}, level  # solo condicionales fuera del repo
