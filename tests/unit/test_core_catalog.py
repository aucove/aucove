# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Pruebas del catálogo núcleo de errores y avisos.

Solo usan la biblioteca estándar (tomllib) y no importan el paquete, para no
depender de él. Los códigos esperados salen de `$defs/error_code` y
`$defs/warning_code` de `schemas/result.schema.json` del repo o, si aún no
existe, de `$AUCOVE_SPEC_DIR/schemas`; sin ninguno, esas pruebas se omiten.
"""

from __future__ import annotations

import json
import os
import re
import string
import tomllib
from collections.abc import Mapping
from functools import cache
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_DIR = REPO_ROOT / "src" / "aucove" / "i18n" / "catalogs"
LANGS: tuple[str, ...] = ("es", "en")
SPEC_ENV = "AUCOVE_SPEC_DIR"

MAX_TEXT = 500  # maxLength de message y hint en result.schema.json
ERROR_KEY = re.compile(r"^error\.(E_[A-Z0-9]+(?:_[A-Z0-9]+)+)\.(message|hint)$")
WARNING_KEY = re.compile(r"^warning\.(W_[A-Z0-9]+(?:_[A-Z0-9]+)+)$")
CODE_SPAN = re.compile(r"`([^`]*)`")
# Letras y signos que delatan un texto sin traducir en en/core.toml.
SPANISH_ONLY = frozenset("áéíóúüñÁÉÍÓÚÜÑ¿¡…")

# Sugerencias de 05 §12.1 que el catálogo es reescribe a propósito. La prueba
# exige que sigan distintas, para que la lista no quede obsoleta.
HINT_REWORDED: Mapping[str, str] = {
    "E_CONFIG_CORE_MISMATCH": (
        "la sugerencia remite al comando del propio hint; sin marcadores "
        "se da el comando genérico con <core_pin>"
    ),
    "E_AUTH_NOT_AUTHENTICATED": "se quita la referencia al capítulo 08-forja",
    "E_FORGE_PUSH_REJECTED": (
        "la remisión a 08-forja se sustituye por la sugerencia de 08-forja §5.4"
    ),
    "E_STATE_MERGE_CONFLICT": (
        "<motivo> dentro del código quedaría sin traducir en en (REQ-ARCH-73)"
    ),
}


# --- Lectura -----------------------------------------------------------------


def _catalog_path(lang: str) -> Path:
    return CATALOG_DIR / lang / "core.toml"


def _flatten(table: Mapping[str, object], prefix: str = "") -> dict[str, object]:
    # Claves planas unidas por '.', como las fusiona el cargador (D-32 f).
    flat: dict[str, object] = {}
    for key, value in table.items():
        full = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, full + "."))
        else:
            flat[full] = value
    return flat


@cache
def _raw(lang: str) -> bytes:
    return _catalog_path(lang).read_bytes()


@cache
def _catalog(lang: str) -> dict[str, object]:
    return _flatten(tomllib.loads(_raw(lang).decode("utf-8")))


def _texts(lang: str) -> dict[str, str]:
    cat = _catalog(lang)
    return {k: v for k, v in cat.items() if isinstance(v, str)}


def _codes(lang: str) -> tuple[set[str], set[str]]:
    errors: set[str] = set()
    warnings: set[str] = set()
    for key in _catalog(lang):
        if m := ERROR_KEY.match(key):
            errors.add(m.group(1))
        elif m := WARNING_KEY.match(key):
            warnings.add(m.group(1))
    return errors, warnings


def _spec_dir() -> Path | None:
    value = os.environ.get(SPEC_ENV)
    return Path(value) if value else None


def _schema_file() -> Path | None:
    local = REPO_ROOT / "schemas" / "result.schema.json"
    if local.is_file():
        return local
    spec = _spec_dir()
    if spec is not None and (spec / "schemas" / "result.schema.json").is_file():
        return spec / "schemas" / "result.schema.json"
    return None


@pytest.fixture(scope="module")
def schema_codes() -> tuple[list[str], list[str]]:
    path = _schema_file()
    if path is None:
        pytest.skip(f"no hay schemas/result.schema.json ni ${SPEC_ENV}/schemas")
    defs = json.loads(path.read_text(encoding="utf-8"))["$defs"]
    return list(defs["error_code"]["enum"]), list(defs["warning_code"]["enum"])


def _spec_rows(section: str, prefix: str) -> dict[str, list[str]]:
    # Filas de la tabla de 05-operaciones §12.x: código -> celdas.
    spec = _spec_dir()
    doc = spec / "05-operaciones.md" if spec is not None else None
    if doc is None or not doc.is_file():
        pytest.skip(f"sin ${SPEC_ENV}/05-operaciones.md")
    text = doc.read_text(encoding="utf-8")
    start = text.index(f"### {section} ")
    end = text.index("\n### ", start + 1)
    rows: dict[str, list[str]] = {}
    for line in text[start:end].splitlines():
        if not line.startswith(f"| `{prefix}"):
            continue
        raw_cells = re.split(r"(?<!\\)\|", line)[1:-1]
        cells = [c.strip().replace("\\|", "|") for c in raw_cells]
        rows[cells[0].strip("`")] = cells
    return rows


# --- Formato de archivo ------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_file_encoding(lang: str) -> None:
    data = _raw(lang)
    assert not data.startswith(b"\xef\xbb\xbf"), "UTF-8 sin BOM"
    assert b"\r" not in data, "solo LF"
    assert data.endswith(b"\n") and not data.endswith(b"\n\n"), "un único salto final"
    text = data.decode("utf-8")  # UTF-8 estricto
    for n, line in enumerate(text.split("\n"), 1):
        assert line == line.rstrip(), f"{lang}/core.toml:{n}: espacio al final"
        assert "\t" not in line, f"{lang}/core.toml:{n}: tabulador"


@pytest.mark.parametrize("lang", LANGS)
def test_values_are_strings(lang: str) -> None:
    cat = _catalog(lang)
    bad = {k: type(v).__name__ for k, v in cat.items() if not isinstance(v, str)}
    assert not bad


# --- Claves ------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_only_core_prefixes(lang: str) -> None:
    # D-32 (f): core.toml solo tiene error.* y warning.*
    bad = [
        k for k in _catalog(lang) if not ERROR_KEY.match(k) and not WARNING_KEY.match(k)
    ]
    assert not bad, f"claves fuera de error.<CODE>.message|hint y warning.<CODE>: {bad}"


@pytest.mark.parametrize("lang", LANGS)
def test_each_error_has_message_and_hint(lang: str) -> None:
    keys = set(_catalog(lang))
    errors, _ = _codes(lang)
    missing = sorted(
        f"error.{code}.{part}"
        for code in errors
        for part in ("message", "hint")
        if f"error.{code}.{part}" not in keys
    )
    assert not missing


def test_same_keys_in_es_and_en() -> None:
    es, en = set(_catalog("es")), set(_catalog("en"))
    assert sorted(es - en) == [], "claves solo en es"
    assert sorted(en - es) == [], "claves solo en en"


@pytest.mark.parametrize("lang", LANGS)
def test_codes_match_schema(
    lang: str, schema_codes: tuple[list[str], list[str]]
) -> None:
    error_enum, warning_enum = schema_codes
    errors, warnings = _codes(lang)
    assert sorted(set(error_enum) - errors) == [], "errores sin texto"
    assert sorted(errors - set(error_enum)) == [], "errores fuera del esquema"
    assert sorted(set(warning_enum) - warnings) == [], "avisos sin texto"
    assert sorted(warnings - set(warning_enum)) == [], "avisos fuera del esquema"
    # Exactamente message y hint por error y un texto por aviso: no sobra nada.
    assert len(_catalog(lang)) == 2 * len(set(error_enum)) + len(set(warning_enum))


def test_reserved_warning_present(schema_codes: tuple[list[str], list[str]]) -> None:
    # D-39: código reservado en H2 aunque nadie lo emita todavía.
    _, warning_enum = schema_codes
    assert "W_FORGE_ACTIONS_RESTRICTED" in warning_enum
    for lang in LANGS:
        assert "warning.W_FORGE_ACTIONS_RESTRICTED" in _catalog(lang)


# --- Textos ------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_no_placeholders(lang: str) -> None:
    # Params vacíos en H2; tampoco llaves literales, que str.format rompería.
    for key, text in _texts(lang).items():
        assert "{" not in text and "}" not in text, f"{lang}:{key}"
        fields = [f for _, f, _, _ in string.Formatter().parse(text) if f is not None]
        assert fields == [], f"{lang}:{key}: marcadores {fields}"


@pytest.mark.parametrize("lang", LANGS)
def test_text_shape(lang: str) -> None:
    for key, text in _texts(lang).items():
        assert text, f"{lang}:{key} vacío"
        assert text == text.strip(), f"{lang}:{key}: espacios en los extremos"
        assert "\n" not in text and "\r" not in text, f"{lang}:{key}: varias líneas"
        assert len(text) <= MAX_TEXT, f"{lang}:{key}: {len(text)} > {MAX_TEXT}"
        assert text.count("`") % 2 == 0, f"{lang}:{key}: acento grave sin cerrar"


@pytest.mark.parametrize("lang", LANGS)
def test_hints_within_limit(lang: str) -> None:
    hints = {k: v for k, v in _texts(lang).items() if k.endswith(".hint")}
    assert hints, "sin hints"
    long = {k: len(v) for k, v in hints.items() if len(v) > MAX_TEXT}
    assert not long


def test_code_spans_identical() -> None:
    # REQ-ARCH-73: misma lista de fragmentos entre acentos graves, en el mismo orden.
    es, en = _texts("es"), _texts("en")
    diff = {
        key: (CODE_SPAN.findall(es[key]), CODE_SPAN.findall(en[key]))
        for key in es.keys() & en.keys()
        if CODE_SPAN.findall(es[key]) != CODE_SPAN.findall(en[key])
    }
    assert not diff


def test_en_is_translated() -> None:
    es, en = _texts("es"), _texts("en")
    same = sorted(k for k in es.keys() & en.keys() if es[k] == en[k])
    assert not same, f"textos iguales en es y en: {same}"
    for key, text in en.items():
        prose = CODE_SPAN.sub("", text)
        leftovers = sorted(set(prose) & SPANISH_ONLY)
        assert not leftovers, f"en:{key}: {leftovers}"


# --- Correspondencia con 05-operaciones §12 -----------------------------------


def test_es_hints_come_from_spec() -> None:
    rows = _spec_rows("12.1", "E_")
    es = _texts("es")
    for code, cells in rows.items():
        spec_hint = cells[3]
        ours = es[f"error.{code}.hint"]
        if code in HINT_REWORDED:
            assert ours != spec_hint, f"{code}: ya es la de la spec (HINT_REWORDED)"
        else:
            assert ours == spec_hint, f"{code}: no es la Sugerencia de 05 §12.1"
    assert set(HINT_REWORDED) <= set(rows)


def test_spec_tables_match_catalog() -> None:
    errors, warnings = _codes("es")
    assert set(_spec_rows("12.1", "E_")) == errors
    assert set(_spec_rows("12.2", "W_")) == warnings
