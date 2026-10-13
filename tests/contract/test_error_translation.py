# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Traducción de las excepciones de la forja a códigos y áreas (05-operaciones §12.3, REQ-OPS-63).

Prueba tabular: por cada ``kind`` de 08-forja §2.1, ``code_for_forge_kind``,
``area_of`` y ``retryable`` (columna R de 05 §12.1) coinciden con la
especificación. Con ``<PREFIJO>SPEC_DIR`` definido, la tabla transcrita se
compara además con la de la spec. Cierra con fallas reales del falso: los
errores de red caen en ``NET`` y los de autenticación en ``AUTH`` (12-mvp §3.3).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from aucove import _meta
from aucove.domain.errors import ERRORS, EXIT_BY_AREA, area_of, code_for_forge_kind, exit_code
from aucove.forge.base import ERROR_CLASSES, RETRYABLE_BY_KIND, BaseNotDefault, ForgeError
from aucove.forge.fake import Fault, FakeForge

# kind -> (código fuera de claim, área, retryable del código según 05 §12.1)
TABLE: dict[str, tuple[str | None, str | None, bool | None]] = {
    "no_token": ("E_AUTH_NOT_AUTHENTICATED", "AUTH", False),
    "auth": ("E_AUTH_TOKEN_INVALID", "AUTH", False),
    "permission": ("E_AUTH_INSUFFICIENT_SCOPE", "AUTH", False),
    "login_changed": ("E_AUTH_LOGIN_CHANGED", "AUTH", False),
    "not_found": ("E_FORGE_NOT_FOUND", "FORGE", False),
    "ref_exists": ("E_FORGE_CONFLICT", "FORGE", True),
    "ref_conflict": ("E_FORGE_CONFLICT", "FORGE", True),
    "merge_conflict": ("E_STATE_MERGE_CONFLICT", "STATE", False),
    "pr_exists": (None, None, None),  # no es un error: se reutiliza el PR existente
    "no_commits": ("E_FORGE_UNEXPECTED", "FORGE", False),
    "validation": ("E_FORGE_UNEXPECTED", "FORGE", False),
    "unsupported": ("E_FORGE_CAPABILITY_MISSING", "FORGE", False),
    "rate_limited": ("E_FORGE_RATE_LIMITED", "FORGE", True),
    "unavailable": ("E_FORGE_UNAVAILABLE", "FORGE", True),
    "timeout": ("E_NET_TIMEOUT", "NET", True),
    "unreachable": ("E_NET_OFFLINE", "NET", True),
    "tls": ("E_NET_TLS", "NET", False),
}
OPS = ("claim", "release", "submit", "validate", "doctor")


def test_table_covers_every_kind_of_08_2_1() -> None:
    assert set(TABLE) == set(RETRYABLE_BY_KIND) == {cls.kind for cls in ERROR_CLASSES}
    assert len(TABLE) == 17


@pytest.mark.parametrize("kind", sorted(TABLE))
@pytest.mark.parametrize("op", OPS)
def test_code_area_and_retryable(kind: str, op: str) -> None:
    code, area, retryable = TABLE[kind]
    if kind == "ref_exists" and op == "claim":
        code = area = retryable = None  # lo resuelve el paso create_ref de 05 §9.12
    got = code_for_forge_kind(kind, op)
    assert got == code
    if code is None:
        return
    assert area_of(code) == area
    assert ERRORS[code].retryable is retryable
    assert exit_code(False, code) == EXIT_BY_AREA[area]  # type: ignore[index]


def test_network_and_auth_areas() -> None:
    """05 §12.3 y 12 §3.3: timeout, unreachable y tls en NET; no_token, auth, permission y login_changed en AUTH."""
    net = {k for k, (code, area, _) in TABLE.items() if area == "NET"}
    auth = {k for k, (code, area, _) in TABLE.items() if area == "AUTH"}
    assert net == {"timeout", "unreachable", "tls"}
    assert auth == {"no_token", "auth", "permission", "login_changed"}
    for kind in net | auth:
        code = code_for_forge_kind(kind, "submit")
        assert code is not None
        # En NET y AUTH, el retryable de la operación es el mismo que el de la excepción (08 §2.1).
        assert ERRORS[code].retryable is RETRYABLE_BY_KIND[kind]


def test_unknown_kind_is_unexpected() -> None:
    assert code_for_forge_kind("brand_new_kind", "submit") == "E_FORGE_UNEXPECTED"


def test_base_not_default_keeps_validation_kind() -> None:
    """BaseNotDefault se traduce por su tipo (E_CONFIG_BASE_NOT_DEFAULT), no por su kind (08 §2.1)."""
    exc = BaseNotDefault("base", default_branch="main")
    assert exc.kind == "validation"
    assert code_for_forge_kind(exc.kind, "doctor") == "E_FORGE_UNEXPECTED"
    assert "E_CONFIG_BASE_NOT_DEFAULT" in ERRORS


# --- Fallas reales del falso ---------------------------------------------------------


@pytest.mark.parametrize(
    ("fault", "area"),
    [
        ("timeout_before", "NET"),
        ("timeout_after", "NET"),
        ("unreachable", "NET"),
        ("http_401", "AUTH"),
        ("http_403", "AUTH"),
        ("http_404", "FORGE"),
        ("http_422", "FORGE"),
        ("http_5xx", "FORGE"),
        ("http_5xx_after", "FORGE"),
        ("rate_limit_primary", "FORGE"),
        ("rate_limit_secondary", "FORGE"),
    ],
)
def test_fake_failures_translate_to_their_area(fake: FakeForge, fault: str, area: str) -> None:
    number = fake.seed_issue(title="t").number
    fake.faults.add(Fault("set_assignees", fault, nth=1))  # type: ignore[arg-type]
    with pytest.raises(ForgeError) as info:
        fake.set_assignees(number, ["bruno"])
    exc = info.value
    code = code_for_forge_kind(exc.kind, "claim")
    assert code is not None
    assert area_of(code) == area
    assert ERRORS[code].retryable is TABLE[exc.kind][2]


# --- Contra la especificación (opcional) -----------------------------------------------


def _spec_dir() -> Path | None:
    value = os.environ.get(_meta.ENV_PREFIX + "SPEC_DIR")
    if not value:
        return None
    path = Path(value)
    return path if (path / "05-operaciones.md").is_file() else None


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n### ", start + len(heading))
    end2 = text.find("\n## ", start + len(heading))
    ends = [e for e in (end, end2) if e != -1]
    return text[start : min(ends) if ends else len(text)]


@pytest.mark.skipif(_spec_dir() is None, reason="sin la spec (definir <PREFIJO>SPEC_DIR)")
def test_table_matches_the_spec() -> None:
    spec = _spec_dir()
    assert spec is not None
    text = (spec / "05-operaciones.md").read_text(encoding="utf-8")
    rows = [ln for ln in _section(text, "### 12.3").splitlines() if ln.startswith("| `")]
    found: dict[str, str | None] = {}
    for row in rows:
        cells = [c.strip() for c in row.strip("|").split("|")]
        codes = re.findall(r"E_[A-Z_]+", cells[1])
        for kind in re.findall(r"`([a-z_]+)`", cells[0]):
            if kind != "kind":  # fila de encabezado
                found[kind] = codes[-1] if codes else None
    assert found == {k: v[0] for k, v in TABLE.items()}
    # Columna R de 05 §12.1 para cada código de la tabla.
    errors = _section(text, "### 12.1")
    for code, _area, retryable in TABLE.values():
        if code is None:
            continue
        row = next(ln for ln in errors.splitlines() if ln.startswith(f"| `{code}` |"))
        r_cell = [c.strip() for c in row.strip("|").split("|")][2]
        assert (r_cell == "sí") is retryable, code
