# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Cargador YAML estricto y front matter (03-esquemas §2.1-2.2, REQ-SCHEMA-1..5)."""

from __future__ import annotations

import pytest

from aucove.config import yamlload
from aucove.config.yamlload import (
    FINDING_PARAMS,
    config_finding,
    line_for,
    load_with_lines,
    load_yaml,
    pointer_lines,
    repo_path,
    split_front_matter,
    strict_loader,
)
from aucove.i18n import Translator


def _codes(findings: list) -> list[str]:  # type: ignore[type-arg]
    return [f.code for f in findings]


# -- REQ-SCHEMA-3: sin conversiones implícitas de YAML 1.1 --------------------------


def test_dates_and_yaml11_booleans_stay_text() -> None:
    data, findings = load_yaml(
        "date: 2026-10-01\nwhen: 2026-10-01T14:05:00Z\na: yes\nb: no\nc: on\nd: off\ne: y\nf: Yes\n",
        file="project.yaml",
    )
    assert findings == []
    assert data == {
        "date": "2026-10-01",
        "when": "2026-10-01T14:05:00Z",
        "a": "yes",
        "b": "no",
        "c": "on",
        "d": "off",
        "e": "y",
        "f": "Yes",
    }


@pytest.mark.parametrize(("raw", "value"), [("true", True), ("True", True), ("TRUE", True), ("false", False),
                                            ("False", False), ("FALSE", False)])
def test_only_true_and_false_are_booleans(raw: str, value: bool) -> None:
    data, findings = load_yaml(f"x: {raw}\n", file="a.yaml")
    assert findings == [] and data == {"x": value}


def test_numbers_and_null_keep_safe_loader_semantics() -> None:
    data, _ = load_yaml("a: 0.1\nb: 10\nc: ~\nd: null\ne: '0.1'\nf: =\n", file="a.yaml")
    assert data == {"a": 0.1, "b": 10, "c": None, "d": None, "e": "0.1", "f": "="}


def test_explicit_timestamp_tag_stays_text() -> None:
    data, findings = load_yaml("a: !!timestamp 2026-10-01\n", file="a.yaml")
    assert findings == [] and data == {"a": "2026-10-01"}


# -- REQ-SCHEMA-2: alias, anclas, claves repetidas y claves no textuales ------------


@pytest.mark.parametrize(
    ("text", "line", "fragment"),
    [
        ("a: &x 1\nb: *x\n", 1, "&x"),  # el ancla ya se rechaza
        ("a: 1\nb: *x\n", 2, "*"),
        ("a: 1\nb: 2\na: 3\n", 3, '"a"'),
        ("a: 1\n1: 2\n", 2, "1"),
        ("a: 1\ntrue: 2\n", 2, "true"),
        ("a: 1\n? [1, 2]\n: 3\n", 2, "[1, 2]"),
        ("a: 1\nnull: 2\n", 2, "null"),
    ],
)
def test_rejects_aliases_anchors_duplicates_and_non_string_keys(text: str, line: int, fragment: str) -> None:
    data, findings = load_yaml(text, file="roles.yaml")
    assert data is None
    assert _codes(findings) == ["E_CONFIG_YAML"]
    assert findings[0].line == line
    assert findings[0].path == "roles.yaml" and findings[0].pointer is None
    assert fragment in findings[0].message


def test_nested_duplicate_key_is_rejected() -> None:
    _data, findings = load_yaml("a:\n  b: 1\n  b: 2\n", file="a.yaml")
    assert _codes(findings) == ["E_CONFIG_YAML"] and findings[0].line == 3


@pytest.mark.parametrize(
    "text",
    [
        "a: !!binary aGVsbG8=\n",
        "a: !!set {x, y}\n",
        "a: !!omap [x: 1]\n",
        "a: !!pairs [x: 1]\n",
        "a: !!python/object:os.system x\n",
        "a: !local x\n",
        "<<: {a: 1}\n",
    ],
)
def test_rejects_tags_without_json_type(text: str) -> None:
    data, findings = load_yaml(text, file="a.yaml")
    assert data is None and _codes(findings) == ["E_CONFIG_YAML"] and findings[0].line == 1


def test_syntax_error_stops_the_file_with_one_finding() -> None:
    data, findings = load_yaml("a: [1, 2\nb: {\nc: }\n", file="project.yaml")
    assert data is None and len(findings) == 1
    assert findings[0].code == "E_CONFIG_YAML" and findings[0].line is not None


def test_multiple_documents_are_a_syntax_error() -> None:
    _data, findings = load_yaml("a: 1\n---\nb: 2\n", file="a.yaml")
    assert _codes(findings) == ["E_CONFIG_YAML"]


def test_control_character_is_reported_with_its_line() -> None:
    _data, findings = load_yaml("a: 1\nb: \x07\n", file="a.yaml")
    assert _codes(findings) == ["E_CONFIG_YAML"] and findings[0].line == 2


def test_syntax_code_can_be_changed() -> None:
    _data, findings = load_yaml("a: [\n", file="x.md", syntax_code="E_VALIDATION_FRONT_MATTER")
    assert _codes(findings) == ["E_VALIDATION_FRONT_MATTER"]


def test_too_deep_documents_do_not_crash() -> None:
    text = "a: " + "[" * 5000 + "]" * 5000 + "\n"
    data, findings = load_yaml(text, file="a.yaml")
    assert data is None and _codes(findings) == ["E_CONFIG_YAML"]


def test_empty_document_is_none_without_findings() -> None:
    assert load_yaml("", file="a.yaml") == (None, [])
    assert load_yaml("# solo un comentario\n", file="a.yaml") == (None, [])


def test_loader_is_a_safe_loader_subclass() -> None:
    import yaml

    loader = strict_loader()
    assert issubclass(loader, yaml.SafeLoader)
    assert strict_loader() is loader  # se construye una vez
    assert not issubclass(loader, getattr(yaml, "CSafeLoader", ()))  # type: ignore[arg-type]


# -- REQ-SCHEMA-1: BOM y CRLF --------------------------------------------------------


def test_bom_and_crlf_are_normalized() -> None:
    data, lines, findings, norm = load_with_lines(b"\xef\xbb\xbfa: 1\r\nb:\r\n  c: 2\r\n", file="a.yaml")
    assert findings == [] and data == {"a": 1, "b": {"c": 2}}
    assert norm == "a: 1\nb:\n  c: 2\n"
    assert lines["/b/c"] == 3


def test_bom_in_text_and_lone_cr() -> None:
    data, findings = load_yaml("\ufeffa: 1\rb: 2\r", file="a.yaml")
    assert findings == [] and data == {"a": 1, "b": 2}


def test_non_utf8_bytes_give_a_finding() -> None:
    data, findings = load_yaml(b"a: \xff\n", file="roles.yaml")
    assert data is None and _codes(findings) == ["E_CONFIG_YAML"] and findings[0].line is None


# -- mapa de líneas -----------------------------------------------------------------


def test_pointer_lines_cover_maps_and_sequences() -> None:
    text = "# comentario\na: 1\nlist:\n  - x: 1\n    y: [1, 2]\n  - 2\n'k/~': 3\n"
    lines = pointer_lines(text)
    assert lines[""] == 2
    assert lines["/a"] == 2 and lines["/list"] == 3
    assert lines["/list/0"] == 4 and lines["/list/0/y"] == 5 and lines["/list/0/y/1"] == 5
    assert lines["/list/1"] == 6
    assert lines["/k~1~0"] == 7  # RFC 6901


def test_pointer_lines_of_a_front_matter_file() -> None:
    text = "---\nid: x\nauthors:\n  - ana\n---\n# Cuerpo: [no es YAML\n"
    assert pointer_lines(text) == {"": 2, "/id": 2, "/authors": 3, "/authors/0": 4}


@pytest.mark.parametrize("text", [None, b"\xff", "a: [\n", "---\na: [\n---\nbody\n", "---\na: 1\nb: [\n"])
def test_pointer_lines_are_empty_when_unreadable(text: str | bytes | None) -> None:
    assert pointer_lines(text) == {}


def test_pointer_lines_of_a_yaml_with_document_start() -> None:
    assert pointer_lines("---\na: 1\n") == {"": 2, "/a": 2}


def test_line_for_walks_up_to_the_nearest_ancestor() -> None:
    lines = {"": 1, "/forge": 4}
    assert line_for(lines, "/forge/host") == 4
    assert line_for(lines, "/otra/cosa") == 1
    assert line_for({"/a": 2}, "/b") is None
    assert line_for(lines, None) is None and line_for({}, "/a") is None


# -- REQ-SCHEMA-5: front matter -----------------------------------------------------


def test_front_matter_split() -> None:
    data, body, findings = split_front_matter("---\nid: x\ntags: [a]\n---\n# Título\n\nTexto.\n", file="memory/decisions/x.md")
    assert findings == [] and data == {"id": "x", "tags": ["a"]}
    assert body == "# Título\n\nTexto.\n"


def test_front_matter_with_bom_crlf_and_bytes() -> None:
    data, body, findings = split_front_matter(b"\xef\xbb\xbf---\r\nid: x\r\n---\r\nbody\r\n", file="a.md")
    assert findings == [] and data == {"id": "x"} and body == "body\n"


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("# sin front matter\n", 1),
        ("--- \nid: x\n---\n", 1),  # la primera línea debe ser exactamente ---
        ("\n---\nid: x\n---\n", 1),
        ("---\nid: x\n", 1),  # sin cierre
        ("---\n---\nbody\n", 1),  # vacío: no es un mapa
        ("---\n- a\n- b\n---\n", 1),  # una lista no es un mapa
        ("---\nid: x\nid: y\n---\n", 3),  # clave repetida: línea del archivo, no del bloque
        ("---\nid: [\n---\n", 3),
    ],
)
def test_front_matter_problems(text: str, line: int) -> None:
    data, _body, findings = split_front_matter(text, file="memory/notes/task-1.md")
    assert data is None
    assert _codes(findings) == ["E_VALIDATION_FRONT_MATTER"]
    assert findings[0].line == line and findings[0].path == "memory/notes/task-1.md"


def test_front_matter_non_utf8() -> None:
    data, body, findings = split_front_matter(b"---\nid: \xff\n---\n", file="a.md")
    assert data is None and body == "" and _codes(findings) == ["E_VALIDATION_FRONT_MATTER"]


def test_front_matter_closing_line_must_be_exact() -> None:
    data, body, findings = split_front_matter("---\nid: x\n--- \n---\nbody\n", file="a.md")
    assert data is None and _codes(findings) == ["E_VALIDATION_FRONT_MATTER"]  # '--- ' cae dentro del YAML


# -- hallazgos y localización --------------------------------------------------------


def test_messages_are_localized(t_es: Translator, t_en: Translator) -> None:
    _d, es = load_yaml("a: 1\na: 2\n", file="a.yaml", t=t_es)
    _d, en = load_yaml("a: 1\na: 2\n", file="a.yaml", t=t_en)
    assert "repetida" in es[0].message and "repeated" in en[0].message
    assert es[0].hint and en[0].hint and es[0].hint != en[0].hint


def test_without_translator_messages_are_english() -> None:
    _d, findings = load_yaml("a: 1\na: 2\n", file="a.yaml")
    assert findings[0].message.startswith("Invalid YAML")


def test_check_is_passed_through() -> None:
    _d, findings = load_yaml("a: [\n", file="a.yaml", check="convention")
    assert findings[0].check == "convention"


def test_config_finding_validates_code_and_markers(t_en: Translator) -> None:
    with pytest.raises(ValueError):
        config_finding("E_PERM_PATH_DENIED", t=t_en, file="a.yaml")
    with pytest.raises(ValueError):
        config_finding("E_CONFIG_YAML", t=t_en, file="a.yaml", other="x")
    f = config_finding("E_CONFIG_YAML", t=t_en, file="a.yaml", detail="x", hint="h" * 600)
    assert f.hint is not None and len(f.hint) == 500


def test_marker_names_do_not_clash_with_make_finding() -> None:
    """Firma de ``make_finding(rule, inp, code, *, severity, path, line, pointer, **params)``."""
    reserved = {"rule", "inp", "code", "severity", "path", "line", "pointer"}
    assert all(not (params & reserved) for params in FINDING_PARAMS.values())


@pytest.mark.parametrize(
    ("file", "expected"),
    [("project.yaml", "project.yaml"), ("./roles.yaml", "roles.yaml"), ("memory\\notes\\a.md", "memory/notes/a.md"),
     ("/abs/path.yaml", None), ("", None), (None, None)],
)
def test_repo_path(file: str | None, expected: str | None) -> None:
    assert repo_path(file) == expected


def test_shown_clips_long_values() -> None:
    assert yamlload.shown("x" * 200).endswith("...") and len(yamlload.shown("x" * 200)) == yamlload.MAX_SHOWN
    assert yamlload.shown({"b": 1, "a": 2}) == '{"a": 2, "b": 1}'
    assert yamlload.shown(object()).startswith('"<object')
