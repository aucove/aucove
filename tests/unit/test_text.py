# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""T1: texto puro del dominio (01 §9.5, 03 REQ-SCHEMA-13 y §4.5, 08 §6.3, D-35 m)."""

import hashlib
import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from aucove.domain.text import ascii_slug, orcid_ok, same_login, sha12_canonical, slugify
from tests.factories import io_guard

SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")  # patrón slug de 03 §2.6


@pytest.fixture(autouse=True)
def _no_io(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    io_guard(request, monkeypatch)  # REQ-MVP-24


# --- ascii_slug (01 §9.5, REQ-ARCH-76) -------------------------------------------------

@pytest.mark.parametrize(("raw", "expected"), [
    # Tabla de 01 §9.5
    ("Revisión de métodos", "revision-de-metodos"),
    ("Año 2026: ¿qué cambió?", "ano-2026-que-cambio"),
    ("Straße — Œuvre", "strasse-oeuvre"),
    # Mapeos explícitos, en minúsculas y en mayúsculas
    ("ß æ œ ø đ ł", "ss-ae-oe-o-d-l"),
    ("ẞ Æ Œ Ø Đ Ł", "ss-ae-oe-o-d-l"),
    ("Łódź Øre", "lodz-ore"),
    # NFKD: ligaduras, números en círculo e İ con punto
    ("ﬁn ① İstanbul", "fin-1-istanbul"),
    ("  --Hola--  ", "hola"),
    ("a__b..c", "a-b-c"),
    # Vacío o sin nada representable
    ("", "x"),
    ("¿¡—!?", "x"),
    ("日本語", "x"),
])
def test_ascii_slug_table(raw: str, expected: str) -> None:
    assert ascii_slug(raw) == expected


def test_ascii_slug_cuts_without_trailing_hyphen() -> None:
    assert ascii_slug("abc def", max_len=4) == "abc"
    assert ascii_slug("abc def", max_len=5) == "abc-d"
    assert ascii_slug("a" * 60) == "a" * 48
    assert ascii_slug("abc", max_len=1) == "a"


def test_ascii_slug_rejects_non_positive_max_len() -> None:
    with pytest.raises(ValueError):
        ascii_slug("abc", max_len=0)


@settings(database=None, deadline=None)
@given(st.text(), st.integers(min_value=1, max_value=80))
def test_ascii_slug_shape(raw: str, max_len: int) -> None:
    out = ascii_slug(raw, max_len)
    assert SLUG.match(out)
    assert len(out) <= max_len
    assert ascii_slug(out, max_len) == out  # idempotente


# --- slugify (03 REQ-SCHEMA-13) ------------------------------------------------------

def test_slugify_spec_example() -> None:
    expected = "fusionar-rq-3-en-rq-1-ano-base-2019"
    assert slugify("Fusionar RQ-3 en RQ-1: ¿año base 2019?") == expected


@pytest.mark.parametrize(("raw", "max_len", "expected"), [
    ("", 48, "decision"),
    ("¿?", 48, "decision"),
    ("Núcleo y puertas", 48, "nucleo-y-puertas"),
    ("abc def", 4, "abc"),
    ("a" * 60, 48, "a" * 48),
])
def test_slugify_cases(raw: str, max_len: int, expected: str) -> None:
    assert slugify(raw, max_len) == expected


@settings(database=None, deadline=None)
@given(st.text(), st.integers(min_value=1, max_value=80))
def test_slugify_shape(raw: str, max_len: int) -> None:
    out = slugify(raw, max_len)
    assert SLUG.match(out)
    assert out == "decision" or len(out) <= max_len


# --- same_login (08 §6.3, REQ-FORGE-46) ------------------------------------------------

@pytest.mark.parametrize(("a", "b", "expected"), [
    ("carla", "carla", True),
    ("Carla", "carla", True),
    (" CARLA ", "carla", True),
    ("carla", "carl", False),
    ("carla", None, False),
    (None, "carla", False),
    (None, None, False),
    (7, "7", False),
])
def test_same_login(a: object, b: object, expected: bool) -> None:
    assert same_login(a, b) is expected  # type: ignore[arg-type]
    assert same_login(b, a) is expected  # type: ignore[arg-type]


# --- orcid_ok (03 §4.5, REQ-SCHEMA-29) --------------------------------------------------

@pytest.mark.parametrize(("orcid", "expected"), [
    ("0000-0002-1825-0097", True),     # ejemplo válido de 03 §4.1
    ("0000-0002-1825-0098", False),    # dígito de control cambiado
    ("0000-0002-1694-233X", True),     # control 10 -> X
    ("0000-0002-0000-009X", True),
    ("0000-0002-0000-0090", False),
    ("0000-0002-1694-233x", False),    # x minúscula no vale
    ("0000000218250097", True),        # sin guiones: mismo cálculo que la spec
    ("0000-0002-1825-009", False),     # corto
    ("0000-0002-1825-00977", False),   # largo
    ("0000-0002-18X5-0097", False),    # X fuera del final
    ("０000-0002-1825-0097", False),   # dígito no ASCII
    ("", False),
    ("1", False),
])
def test_orcid_ok(orcid: str, expected: bool) -> None:
    assert orcid_ok(orcid) is expected


def test_orcid_ok_non_string_is_false() -> None:
    assert orcid_ok(None) is False  # type: ignore[arg-type]


# --- sha12_canonical (D-35 m) --------------------------------------------------------

ROLES_LF = b"version: 1\nlead: ana\npeople:\n  - login: ana\n"


def test_sha12_canonical_is_sha256_prefix_of_lf_bytes() -> None:
    expected = hashlib.sha256(ROLES_LF).hexdigest()[:12]
    assert sha12_canonical(ROLES_LF) == expected
    assert re.fullmatch(r"[0-9a-f]{12}", expected)


@pytest.mark.parametrize("variant", [
    b"\xef\xbb\xbf" + ROLES_LF,                                   # con BOM
    ROLES_LF.replace(b"\n", b"\r\n"),                             # CRLF
    b"\xef\xbb\xbf" + ROLES_LF.replace(b"\n", b"\r\n"),           # BOM y CRLF
    ROLES_LF.replace(b"\n", b"\r"),                               # CR sueltos
    ROLES_LF.replace(b"\n", b"\r\n", 2),                          # mezcla
])
def test_sha12_canonical_ignores_bom_and_line_endings(variant: bytes) -> None:
    assert sha12_canonical(variant) == sha12_canonical(ROLES_LF)


def test_sha12_canonical_distinguishes_content() -> None:
    assert sha12_canonical(ROLES_LF) != sha12_canonical(ROLES_LF + b"\n")
    assert sha12_canonical(ROLES_LF) != sha12_canonical(ROLES_LF.replace(b"ana", b"Ana"))
    # Solo se quita un BOM inicial; uno en medio es contenido.
    assert sha12_canonical(b"a\xef\xbb\xbfb") != sha12_canonical(b"ab")


def test_sha12_canonical_empty() -> None:
    assert sha12_canonical(b"") == hashlib.sha256(b"").hexdigest()[:12]
    assert sha12_canonical(b"\xef\xbb\xbf") == sha12_canonical(b"")
