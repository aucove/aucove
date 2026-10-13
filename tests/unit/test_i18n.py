# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Catálogos por área y traductor (01 §9; REQ-ARCH-69..74; D-32 e, f)."""

from __future__ import annotations

from pathlib import Path

import pytest

from aucove import _meta
from aucove.i18n import (
    CATALOG_AREAS,
    LANGS,
    CatalogError,
    Translator,
    catalog_keys,
    check_parity,
    code_spans,
    load_catalog,
    placeholders,
    resolve_lang,
    translator,
)


def _write(root: Path, lang: str, area: str, text: str) -> None:
    path = root / lang / f"{area}.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


CORE_ES = """
[error.E_VALIDATION_INPUT]
message = "Argumentos inválidos para `validate`."
hint = "Corrige los argumentos."

[warning]
W_NET_OFFLINE = "Sin red."
"""

CORE_EN = """
[error.E_VALIDATION_INPUT]
message = "Invalid arguments for `validate`."
hint = "Fix the arguments."

[warning]
W_NET_OFFLINE = "Offline."
"""

VALIDATE_ES = """
[finding.E_INTERNAL_UNEXPECTED]
message = "La regla {rule} no pudo evaluarse: {reason}"

[validate.count]
one = "{n} hallazgo"
other = "{n} hallazgos"
"""

VALIDATE_EN = """
[finding.E_INTERNAL_UNEXPECTED]
message = "Rule {rule} could not be evaluated: {reason}"

[validate.count]
one = "{n} finding"
other = "{n} findings"
"""


@pytest.fixture
def catalogs(tmp_path: Path) -> Path:
    root = tmp_path / "catalogs"
    _write(root, "es", "core", CORE_ES)
    _write(root, "en", "core", CORE_EN)
    _write(root, "es", "validate", VALIDATE_ES)
    _write(root, "en", "validate", VALIDATE_EN)
    return root


def test_areas_contract() -> None:
    assert LANGS == ("es", "en")
    assert CATALOG_AREAS["core"] == ("error.", "warning.")
    assert set(CATALOG_AREAS) == {
        "core", "config", "render", "cli", "doctor", "roles", "validate",
        "convention", "latex", "scope", "ci", "base_report", "evidence", "forge", "status", "context", "mcp", "tasks",
        "review", "init", "metrics",
    }


def test_load_merges_areas_with_flat_keys(catalogs: Path) -> None:
    es = load_catalog("es", catalog_dir=catalogs)
    assert es["error.E_VALIDATION_INPUT.message"].startswith("Argumentos")
    assert es["warning.W_NET_OFFLINE"] == "Sin red."
    assert es["validate.count.one"] == "{n} hallazgo"
    assert "finding.E_INTERNAL_UNEXPECTED.message" in es


def test_missing_dir_or_area_has_no_keys(tmp_path: Path) -> None:
    assert dict(load_catalog("es", catalog_dir=tmp_path / "nope")) == {}
    _write(tmp_path / "c", "en", "cli", 'cli.title = "x"\n')
    assert dict(load_catalog("es", catalog_dir=tmp_path / "c")) == {}
    assert dict(load_catalog("en", catalog_dir=tmp_path / "c")) == {"cli.title": "x"}


def test_duplicate_key_between_areas_fails(catalogs: Path) -> None:
    _write(catalogs, "es", "config", '[finding.E_INTERNAL_UNEXPECTED]\nmessage = "otra"\n')
    with pytest.raises(CatalogError):
        load_catalog("es", catalog_dir=catalogs)


@pytest.mark.parametrize(
    ("area", "text"),
    [
        ("core", 'cli.title = "x"\n'),  # core solo admite error.* y warning.*
        ("cli", 'finding.E_X_Y.message = "x"\n'),  # cli no es dueño de finding.*
        ("validate", 'doctor.git.ok = "x"\n'),
        ("unknown_area", 'unknown_area.x = "x"\n'),  # área fuera de CATALOG_AREAS
    ],
)
def test_prefix_outside_area_fails(tmp_path: Path, area: str, text: str) -> None:
    _write(tmp_path, "es", area, text)
    with pytest.raises(CatalogError):
        load_catalog("es", catalog_dir=tmp_path)


@pytest.mark.parametrize(
    "text",
    [
        'cli.x = 3\n',  # no es texto
        'cli.x = "{a.b}"\n',  # atributo (REQ-ARCH-74)
        'cli.x = "{a[0]}"\n',  # índice
        'cli.x = "{a:>5}"\n',  # especificador de formato
        'cli.x = "{a!r}"\n',  # conversión
        'cli.x = "{0}"\n',  # posicional
        'cli.x = "{}"\n',
        'cli.x = "{sin cerrar"\n',
        'cli.x = "no es toml\n',
    ],
)
def test_invalid_catalog_fails(tmp_path: Path, text: str) -> None:
    _write(tmp_path, "en", "cli", text)
    with pytest.raises(CatalogError):
        load_catalog("en", catalog_dir=tmp_path)


def test_parity_passes_with_same_keys_markers_and_code(catalogs: Path) -> None:
    assert check_parity(catalog_dir=catalogs) == []


@pytest.mark.parametrize(
    ("es_text", "en_text"),
    [
        ('cli.a = "uno"\ncli.b = "dos"\n', 'cli.a = "one"\n'),  # claves distintas (REQ-ARCH-72)
        ('cli.a = "hola {name}"\n', 'cli.a = "hello {login}"\n'),  # marcadores distintos
        ('cli.a = "usa `status`"\n', 'cli.a = "use `estado`"\n'),  # código traducido (REQ-ARCH-73)
        ('cli.a = "`a` y `b`"\n', 'cli.a = "`b` and `a`"\n'),  # orden de fragmentos
    ],
)
def test_parity_fails_when_catalogs_differ(tmp_path: Path, es_text: str, en_text: str) -> None:
    _write(tmp_path, "es", "cli", es_text)
    _write(tmp_path, "en", "cli", en_text)
    assert check_parity(catalog_dir=tmp_path) != []


def test_parity_reports_load_errors(tmp_path: Path) -> None:
    _write(tmp_path, "es", "cli", 'cli.a = "{a.b}"\n')
    assert check_parity(catalog_dir=tmp_path) != []


def test_placeholders_and_code_spans() -> None:
    assert placeholders("a {x} {y} {{z}} {x}") == frozenset({"x", "y"})
    assert placeholders("sin marcadores") == frozenset()
    assert placeholders("{roto") == frozenset()
    assert code_spans("usa `a` y `b c`") == ("a", "b c")


def test_translator_fallback_chain(catalogs: Path) -> None:
    only_en = catalogs.parent / "only_en"
    _write(only_en, "en", "cli", 'cli.a = "English"\n')
    _write(only_en, "es", "cli", 'cli.b = "Solo es"\n')
    t = Translator("es", catalog_dir=only_en)
    assert t("cli.b") == "Solo es"
    assert t("cli.a") == "English"  # es -> en
    assert t("cli.missing") == "cli.missing"  # -> la clave
    assert t.get("cli.missing") is None
    assert t.get("cli.a") == "English"


def test_translator_never_raises(catalogs: Path) -> None:
    t = Translator("en", catalog_dir=catalogs)
    text = t("finding.E_INTERNAL_UNEXPECTED.message", rule="scope")
    assert text == "Rule scope could not be evaluated: {reason}"  # marcador sin valor: literal
    assert t("finding.E_INTERNAL_UNEXPECTED.message", rule="a", reason="b", extra=1) == (
        "Rule a could not be evaluated: b"
    )  # sobrantes ignorados
    broken = Translator("en", catalogs={"en": {"k": "{a.b} {0} {", "j": "{x!r}"}})
    assert broken("k", a=1) == "{a.b} {0} {"
    assert isinstance(broken("j", x=1), str)
    assert broken("missing.key") == "missing.key"
    bad_dir = catalogs.parent / "bad"
    _write(bad_dir, "en", "cli", 'cli.x = "{a.b}"\n')
    assert Translator("en", catalog_dir=bad_dir)("cli.x") == "cli.x"


def test_plural(catalogs: Path) -> None:
    t = Translator("es", catalog_dir=catalogs)
    assert t.plural("validate.count", 1) == "1 hallazgo"
    assert t.plural("validate.count", 0) == "0 hallazgos"
    assert t.plural("validate.count", 3) == "3 hallazgos"


def test_resolve_lang_order() -> None:
    env_var = _meta.ENV_PREFIX + "LANG"
    assert resolve_lang(cli="es", env={env_var: "en"}, project="en") == "es"
    assert resolve_lang(cli=None, env={env_var: "es"}, project="en") == "es"
    assert resolve_lang(cli=None, env={}, project="es") == "es"
    assert resolve_lang(cli=None, env={}, project=None) == "en"
    # Valores inválidos se saltan; el locale del sistema nunca cuenta (D-32 e).
    assert resolve_lang(cli="fr", env={env_var: "de", "LANG": "es_MX.UTF-8", "LC_ALL": "es_ES"}, project=None) == "en"
    assert resolve_lang(cli="", env={env_var: "es_MX"}, project="es") == "es"


def test_translator_is_cached_per_process() -> None:
    assert translator("es") is translator("es")
    assert translator("en").lang == "en"


def test_packaged_catalogs_are_valid() -> None:
    """REQ-ARCH-72..74 sobre los catálogos del paquete (cada área la escribe su dueño)."""
    for lang in LANGS:
        load_catalog(lang)  # CatalogError si hay claves repetidas o fuera de su área
        assert catalog_keys(lang) == frozenset(load_catalog(lang))
    assert check_parity() == []
