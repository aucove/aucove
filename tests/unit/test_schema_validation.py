# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Registro de esquemas y validación (03-esquemas §2.3, REQ-SCHEMA-6..9, REQ-ARCH-28)."""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

import jsonschema
import pytest

from aucove import _meta
from aucove.config import schemas
from aucove.config.schemas import (
    CONFIG_FINDING_CODES,
    SCHEMA_KINDS,
    format_checker,
    formats_used,
    registry,
    schema_id,
    validate,
    validate_result,
    validator,
)
from aucove.config.yamlload import load_with_lines
from aucove.i18n import LANGS, CATALOG_DIR, Translator, load_catalog, placeholders
from tests.conftest import FIXTURES, REPO_ROOT


def _validate_yaml(kind: str, text: str, t: Translator | None = None) -> list:  # type: ignore[type-arg]
    data, lines, findings, _norm = load_with_lines(text, file=f"{kind}.yaml")
    assert findings == []
    return validate(kind, data, file=f"{kind}.yaml", source_text=text, t=t)  # type: ignore[arg-type]


# -- registro local (REQ-ARCH-28, REQ-SCHEMA-7) --------------------------------------


def test_schema_ids_follow_meta_base() -> None:
    for kind in SCHEMA_KINDS:
        assert schema_id(kind) == f"{_meta.SCHEMA_BASE}/{kind}/v1"
        assert schemas.schema(kind)["$id"] == schema_id(kind)


def test_every_packaged_schema_is_valid_draft_2020_12() -> None:
    for kind in SCHEMA_KINDS:
        jsonschema.Draft202012Validator.check_schema(dict(schemas.schema(kind)))
    jsonschema.Draft202012Validator.check_schema(schemas._trace_schema())


def test_registry_holds_packaged_schemas_and_trace() -> None:
    reg = registry()
    for kind in SCHEMA_KINDS:
        assert reg.contents(schema_id(kind))["$id"] == schema_id(kind)
    assert reg.contents(schemas.TRACE_ID)["$id"] == schemas.TRACE_ID


def test_no_ref_is_resolved_through_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un $ref que el registro no tiene falla; nunca se intenta descargar."""
    import urllib.request

    calls: list[object] = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: calls.append(a))
    remote = jsonschema.Draft202012Validator(
        {"$ref": f"{_meta.SCHEMA_BASE}/no-existe/v1"}, registry=registry(), format_checker=format_checker()
    )
    with pytest.raises(Exception) as info:
        remote.is_valid({})
    assert "Unresolvable" in type(info.value).__name__ or "Unresolvable" in repr(info.value)
    external = jsonschema.Draft202012Validator({"$ref": "https://example.org/schema.json"}, registry=registry())
    with pytest.raises(Exception):
        external.is_valid({})
    assert calls == []


def test_pr_meta_resolves_trace_without_network() -> None:
    trace = {
        "version": 1,
        "ai_use": ["drafting"],
        "assisted_by": [
            {"tool": "claude-code", "model": "claude-opus-5-5", "tool_source": "env:" + _meta.ENV_PREFIX + "AI_TOOL",
             "model_source": "declared"}
        ],
        "sign_off": {"login": "ana", "name": "Ana", "email": "a@example.invalid", "channel": "mcp",
                     "attestation": "attest-v1", "at": "2026-09-29T15:04:05Z"},
        "commits": {"base": "9f2c1e7", "head": "a1b3d44", "count": 7, "undeclared": 2},
        "core_version": "0.1.0",
    }
    meta = {"schema": 1, "task": 42, "sections": ["methods"], "plan_refs": ["RQ-1"], "decisions": [], "trace": trace}
    assert validate("pr-meta", meta, file="pr-body") == []
    bad = dict(meta, trace=dict(trace, sign_off=dict(trace["sign_off"], at="no-es-fecha")))
    found = validate("pr-meta", bad, file="pr-body")
    assert [f.pointer for f in found] == ["/trace/sign_off/at"]


def test_trace_schema_matches_the_spec_when_available() -> None:
    spec_dir = os.environ.get(_meta.ENV_PREFIX + "SPEC_DIR")
    if not spec_dir:
        pytest.skip("sin la especificación (" + _meta.ENV_PREFIX + "SPEC_DIR)")
    text = (Path(spec_dir) / "11-trazabilidad.md").read_text(encoding="utf-8")
    blocks = [json.loads(b) for b in re.findall(r"```json\n(.*?)```", text, re.S)]
    spec = next(b for b in blocks if b.get("$id") == schemas.TRACE_ID)
    assert schemas._trace_schema() == spec


# -- format (REQ-SCHEMA-6) ----------------------------------------------------------


def test_formats_used_today() -> None:
    assert formats_used() == frozenset({"date", "date-time", "email", "uri"})


INVALID_BY_FORMAT = {
    "date": ["2026-13-01", "2026-02-30", "01/10/2026"],
    "date-time": ["no-es-fecha", "2026-10-01", "2026-10-01T25:00:00Z"],
    "email": ["no-es-email"],
    "uri": ["no es una uri", "sin-esquema"],
}
VALID_BY_FORMAT = {
    "date": "2026-10-01",
    "date-time": "2026-10-01T14:05:00Z",
    "email": "ana@uni.example",
    "uri": "https://example.org/x",
}


def test_format_checker_covers_every_used_format() -> None:
    checker = format_checker()
    assert formats_used() <= set(checker.checkers)
    for fmt in formats_used():
        assert fmt in INVALID_BY_FORMAT, f"falta un caso inválido para {fmt}"
        check = jsonschema.Draft202012Validator({"format": fmt}, format_checker=checker)
        assert check.is_valid(VALID_BY_FORMAT[fmt])
        for value in INVALID_BY_FORMAT[fmt]:
            assert not check.is_valid(value), (fmt, value)


def test_no_es_fecha_fails_against_date_time() -> None:
    check = jsonschema.Draft202012Validator({"format": "date-time"}, format_checker=format_checker())
    assert not check.is_valid("no-es-fecha")


def test_packaged_validators_assert_format() -> None:
    found = _validate_yaml("roles", "version: 1\nlead: ana\npeople:\n  - login: ana\n    name: Ana\n    role: lead\n"
                                    "    account_type: team\n    email: ana@@x\nsections: []\n")
    assert [(f.pointer, f.code) for f in found] == [("/people/0/email", "E_VALIDATION_SCHEMA")]


def test_format_checker_fails_closed_without_a_checker(monkeypatch: pytest.MonkeyPatch) -> None:
    format_checker.cache_clear()
    monkeypatch.setattr(schemas, "formats_used", lambda: frozenset({"date", "formato-inexistente"}))
    try:
        with pytest.raises(RuntimeError):
            format_checker()
    finally:
        format_checker.cache_clear()


# -- todos los errores, con pointer y línea (REQ-SCHEMA-8, REQ-SCHEMA-9) ---------------


def test_all_errors_of_a_file_with_pointer_and_line() -> None:
    text = (FIXTURES / "config" / "project" / "many-errors.yaml").read_text(encoding="utf-8")
    found = _validate_yaml("project", text)
    got = {(f.pointer, f.line) for f in found}
    assert got == {
        ("/convention", 2),
        ("/name", 5),
        ("/language", 7),
        ("/forge/type", 10),
        ("/forge/host", 12),
        ("/build/timeout_minutes", 14),
        ("/build/engine", 15),
        ("/data_policy", 16),
    }
    assert all(f.code == "E_VALIDATION_SCHEMA" and f.severity == "error" and f.path == "project.yaml" for f in found)
    assert [f.line for f in found] == sorted(f.line for f in found)


def test_without_source_text_lines_are_none() -> None:
    found = validate("project", {"convention": 0.1}, file="project.yaml")
    assert found and all(f.line is None for f in found)
    assert {f.pointer for f in found} >= {"/convention", ""}


def test_required_gives_one_finding_per_missing_key(t_es: Translator) -> None:
    found = validate("project", {}, file="project.yaml", t=t_es)
    missing = sorted(re.search(r"'(\w+)'", f.message).group(1) for f in found)  # type: ignore[union-attr]
    assert missing == sorted(schemas.schema("project")["required"])
    assert all(f.pointer == "" and "raíz" in f.message for f in found)


def test_additional_properties_point_to_each_extra_key() -> None:
    found = validate("roles", {"version": 1, "lead": "ana", "people": [], "sections": [], "x": 1, "y~/": 2},
                     file="roles.yaml")
    pointers = {f.pointer for f in found}
    assert {"/x", "/y~0~1"} <= pointers


def test_exactly_one_lead_is_a_schema_rule(t_en: Translator) -> None:
    base = "version: 1\nlead: ana\npeople:\n{people}sections: []\n"
    person = "  - login: {login}\n    name: X\n    role: {role}\n    account_type: team\n"
    none = base.format(people=person.format(login="ana", role="researcher"))
    two = base.format(people=person.format(login="ana", role="lead") + person.format(login="bea", role="lead"))
    for text, word in ((none, "at least"), (two, "at most")):
        found = _validate_yaml("roles", text, t=t_en)
        assert [f.pointer for f in found] == ["/people"]
        assert word in found[0].message and "`role: \"lead\"`" in found[0].message


def test_front_matter_source_gives_file_lines() -> None:
    text = "---\nid: 2026-09-30-1-x\ntitle: Título\ndate: 2026-13-45\n---\n# Cuerpo\n"
    from aucove.config.yamlload import split_front_matter

    data, _body, problems = split_front_matter(text, file="memory/decisions/2026-09-30-1-x.md")
    assert problems == [] and data is not None
    found = validate("decision", data, file="memory/decisions/2026-09-30-1-x.md", source_text=text)
    by_pointer = {f.pointer: f.line for f in found}
    assert by_pointer["/date"] == 4
    assert by_pointer[""] == 2


@pytest.mark.parametrize(
    ("instance", "word"),
    [
        ({"x": "a"}, "type"),
        ({"x": 5}, "minimum"),
        ({"x": 50}, "maximum"),
        ({"x": 1.5}, "type"),
    ],
)
def test_details_are_specific(instance: dict, word: str, t_en: Translator) -> None:  # type: ignore[type-arg]
    schema = {"type": "object", "properties": {"x": {"type": "integer", "minimum": 10, "maximum": 20}}}
    v = jsonschema.Draft202012Validator(schema)
    err = next(iter(v.iter_errors(instance)))
    assert err.validator == word
    ((pointer, detail, quote),) = list(schemas._describe(err, t_en))
    assert pointer == "/x" and detail and not quote


def test_every_keyword_has_a_localized_detail(t_es: Translator) -> None:
    schema = {
        "type": "object",
        "properties": {
            "enum": {"enum": ["a", "b"]},
            "const": {"const": "latex"},
            "pattern": {"type": "string", "pattern": "^x$"},
            "minlen": {"type": "string", "minLength": 3},
            "maxlen": {"type": "string", "maxLength": 1},
            "xmin": {"exclusiveMinimum": 1},
            "xmax": {"exclusiveMaximum": 1},
            "minitems": {"minItems": 2},
            "maxitems": {"maxItems": 0},
            "minprops": {"minProperties": 1},
            "maxprops": {"maxProperties": 0},
            "unique": {"uniqueItems": True},
            "fmt": {"format": "date"},
            "not": {"not": {"const": "none"}},
            "any": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
            "one": {"oneOf": [{"type": "integer"}, {"minimum": 0}]},
            "contains": {"contains": {"type": "integer"}, "minContains": 2},
            "maxc": {"contains": {"type": "integer"}, "maxContains": 1},
            "other": {"dependentRequired": {"a": ["b"]}},
        },
    }
    instance = {
        "enum": "c", "const": "tex", "pattern": "y", "minlen": "ab", "maxlen": "ab", "xmin": 1, "xmax": 1,
        "minitems": [1], "maxitems": [1], "minprops": {}, "maxprops": {"a": 1}, "unique": [1, 1],
        "fmt": "2026-13-01", "not": "none", "any": "x", "one": 5, "contains": [1], "maxc": [1, 2],
        "other": {"a": 1},
    }
    v = jsonschema.Draft202012Validator(schema, format_checker=format_checker())
    details = {}
    for err in v.iter_errors(instance):
        for pointer, detail, _quote in schemas._describe(err, t_es):
            details[pointer] = detail
    assert set(details) == {"/" + k for k in instance}
    assert all(not d.startswith("config.") for d in details.values()), details
    assert "`dependentRequired`" in details["/other"]


# -- REQ-SCHEMA-89: "escríbelo entre comillas" ----------------------------------------


def test_number_where_text_is_expected_gets_the_quote_hint(t_es: Translator) -> None:
    found = _validate_yaml("project", "convention: 0.1\n", t=t_es)
    convention = next(f for f in found if f.pointer == "/convention")
    assert convention.hint is not None and "entre comillas" in convention.hint
    others = [f for f in found if f.pointer != "/convention"]
    assert others and all("entre comillas" not in (f.hint or "") for f in others)


def test_quote_hint_through_any_of_and_in_english(t_en: Translator) -> None:
    text = (FIXTURES / "config" / "project" / "minimal.yaml").read_text(encoding="utf-8")
    text += "data_policy:\n  human_subjects: true\n  ethics_ref: 114\n"
    found = _validate_yaml("project", text, t=t_en)
    ref = [f for f in found if f.pointer == "/data_policy/ethics_ref"]
    assert ref and any(f.hint and "quotes" in f.hint for f in ref)


def test_no_quote_hint_for_booleans_or_enum_mismatch(t_es: Translator) -> None:
    found = validate("project", {"language": 1, "title": True}, file="project.yaml", t=t_es)
    assert all("entre comillas" not in (f.hint or "") for f in found if f.pointer in ("/language", "/title"))


# -- validate_result (REQ-OPS-5) ----------------------------------------------------


def test_validate_result_accepts_the_shared_sample() -> None:
    sample = json.loads((FIXTURES / "diagnostics" / "build-report.sample.json").read_text(encoding="utf-8"))
    assert validate_result(sample) == []


def test_validate_result_lists_problems() -> None:
    problems = validate_result({"schema": "otra", "ok": "si"})
    assert problems and all(isinstance(p, str) and ": " in p for p in problems)
    assert any(p.startswith("/ok:") for p in problems)


def test_validator_is_cached() -> None:
    assert validator("roles") is validator("roles")


# -- CONFIG_FINDING_CODES y config.toml --------------------------------------


def test_every_config_finding_has_message_with_exact_markers() -> None:
    for lang in LANGS:
        catalog = load_catalog(lang)
        for code, params in CONFIG_FINDING_CODES.items():
            key = f"finding.{code}.message"
            assert key in catalog, (lang, key)
            assert placeholders(catalog[key]) == params, (lang, key)
            hint = catalog.get(f"finding.{code}.hint")
            if hint is not None:
                assert placeholders(hint) <= params, (lang, code)


def test_config_catalog_only_holds_its_keys() -> None:
    for lang in LANGS:
        data = tomllib.loads((CATALOG_DIR / lang / "config.toml").read_text(encoding="utf-8"))
        assert set(data) == {"config", "finding"}
        assert set(data["finding"]) == set(CONFIG_FINDING_CODES)
        for code, entry in data["finding"].items():
            assert set(entry) <= {"message", "hint"}, code


def test_config_catalog_texts_are_utf8_lf_without_bom() -> None:
    for lang in LANGS:
        raw = (REPO_ROOT / "src" / _meta.NAME / "i18n" / "catalogs" / lang / "config.toml").read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf") and b"\r" not in raw and raw.endswith(b"\n")


def test_every_config_text_key_used_in_code_exists() -> None:
    """Las claves config.* que citan los módulos existen en los dos catálogos."""
    src = REPO_ROOT / "src" / _meta.NAME / "config"
    used: set[str] = set()
    for path in src.glob("*.py"):
        used |= set(re.findall(r't\(\s*"(config\.[a-z_.]+)"', path.read_text(encoding="utf-8")))
    used |= {f"config.schema.{name}" for name in schemas._SIMPLE_LIMITS.values()}
    used |= {f"config.schema.{k}{s}" for k in ("min_contains", "max_contains") for s in ("", "_const")}
    used |= {f"config.type.{name}" for _py, name in schemas._TYPE_OF} | {"config.type.null"}
    assert len(used) > 30
    for lang in LANGS:
        missing = sorted(used - set(load_catalog(lang)))
        assert missing == [], (lang, missing)
