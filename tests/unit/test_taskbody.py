# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Cuerpo del Issue de tarea (03-esquemas §8.2 y §8.3; domain/taskbody.py)."""

from __future__ import annotations

import hashlib
import json

import pytest

from aucove.domain import taskbody as tb

# Cuerpo de 03 §8.2 y resultado de 03 §8.3 (sin number ni title, que completa el núcleo).
SPEC_BODY = """### Resumen

Redactar la subsección de datos de Métodos: fuentes, período y limpieza de las
dos olas de la encuesta origen-destino.

### Secciones

metodos

### Referencias al plan

RQ-1, METH-2

### Criterio de terminado

- La subsección 3.1 describe las dos olas de la encuesta.
- La subsección 3.2 explica la limpieza de la ola 2025.
- El PDF de CI compila sin referencias rotas.
"""
SPEC_RESULT = {
    "summary": "Redactar la subsección de datos de Métodos: fuentes, período y limpieza de las\n"
    "dos olas de la encuesta origen-destino.",
    "sections": ["metodos"],
    "plan_refs": ["RQ-1", "METH-2"],
    "done": [
        "La subsección 3.1 describe las dos olas de la encuesta.",
        "La subsección 3.2 explica la limpieza de la ola 2025.",
        "El PDF de CI compila sin referencias rotas.",
    ],
    "spec_hash": "sha256:c295fd3217cbf55ad3cf21ec63d6b615f55a1370f4ba9e3a007750926adb55a2",
}


def body(**fields: str) -> str:
    """Cuerpo con los campos dados, en el orden recibido (etiquetas en español)."""
    names = {"summary": "Resumen", "sections": "Secciones", "plan_refs": "Referencias al plan",
             "done": "Criterio de terminado"}
    return "".join(f"### {names.get(k, k)}\n\n{v}\n\n" for k, v in fields.items())


# -- parse_task_body: casos de 03 §8.2-8.3 --------------------------------------------


def test_spec_example_gives_the_spec_task_spec_and_hash() -> None:
    assert tb.parse_task_body(SPEC_BODY, {}) == SPEC_RESULT


def test_form_labels_are_optional_and_extend_the_canonical_table() -> None:
    assert tb.parse_task_body(SPEC_BODY) == SPEC_RESULT
    custom = SPEC_BODY.replace("### Secciones", "### Partes del documento")
    assert tb.parse_task_body(custom)["sections"] == []
    assert tb.parse_task_body(custom, {"Partes del documento": "sections"})["sections"] == ["metodos"]


def test_english_labels_give_the_same_spec() -> None:
    en = (
        SPEC_BODY.replace("### Resumen", "### Summary")
        .replace("### Secciones", "### Sections")
        .replace("### Referencias al plan", "### Plan references")
        .replace("### Criterio de terminado", "### Definition of done")
    )
    assert tb.parse_task_body(en) == SPEC_RESULT


@pytest.mark.parametrize(
    ("text", "field", "line"),
    [
        (body(summary="Uno.", sections="methods") + "### Resumen\n\nDos.\n", "summary", 9),
        ("### Summary\n\nUno.\n\n### Resumen\n\nDos.\n", "summary", 5),  # mismo campo, otro idioma
        (body(sections="methods", done="- a") + "### Sections\n\nresults\n", "sections", 9),
    ],
)
def test_repeated_field_is_e_validation_task_body(text: str, field: str, line: int) -> None:
    with pytest.raises(tb.TaskBodyError) as info:
        tb.parse_task_body(text)
    err = info.value
    assert err.code == tb.BODY_CODE == "E_VALIDATION_TASK_BODY"
    assert (err.issue.reason, err.issue.field, err.issue.line) == ("repeated_field", field, line)
    assert err.issue.severity == "error"
    assert "repeated_field" in str(err) and isinstance(err, ValueError)


def test_unknown_heading_is_content_of_the_current_field() -> None:
    text = body(summary="Primera línea.\n### Detalle\nSegunda línea.", sections="methods")
    assert tb.parse_task_body(text)["summary"] == "Primera línea.\n### Detalle\nSegunda línea."


def test_heading_needs_the_exact_form() -> None:
    # '#### Resumen' y '###Resumen' no son separadores; los espacios finales sí se toleran.
    text = "### Resumen  \n\nUno.\n#### Resumen\n###Resumen\n"
    assert tb.parse_task_body(text)["summary"] == "Uno.\n#### Resumen\n###Resumen"


def test_comment_lines_are_ignored() -> None:
    op = "<!-- demo:op v1 id=abc -->"
    text = op + "\n" + body(summary="Uno.\n  <!-- nota -->\nDos.", sections="methods")
    warnings: list[tb.BodyIssue] = []
    spec = tb.parse_task_body(text, warnings=warnings)
    assert spec["summary"] == "Uno.\nDos."
    assert warnings == []


def test_text_before_the_first_field_is_dropped_with_a_warning() -> None:
    text = "Hola.\n\nOtra línea.\n" + body(summary="Uno.", sections="methods")
    warnings: list[tb.BodyIssue] = []
    spec = tb.parse_task_body(text, warnings=warnings)
    assert spec["summary"] == "Uno."
    assert [(w.code, w.reason, w.line, w.severity) for w in warnings] == [
        ("W_VALIDATION_TASK_UNKNOWN_FIELD", "text_before_fields", 1, "warning"),
        ("W_VALIDATION_TASK_UNKNOWN_FIELD", "text_before_fields", 3, "warning"),
    ]
    assert tb.parse_task_body(text) == spec  # sin lista, el aviso no se informa


@pytest.mark.parametrize("empty", ["_No response_", "_no response_", "None", "NONE", "none", "", "   "])
def test_empty_values(empty: str) -> None:
    spec = tb.parse_task_body(body(summary=empty, sections=empty, plan_refs=empty, done=empty))
    assert (spec["summary"], spec["sections"], spec["plan_refs"], spec["done"]) == ("", [], [], [])


def test_missing_fields_are_empty() -> None:
    spec = tb.parse_task_body("")
    assert {k: spec[k] for k in tb.FIELDS} == {"summary": "", "sections": [], "plan_refs": [], "done": []}
    assert spec["spec_hash"] == tb.compute_spec_hash(spec)


def test_sections_are_split_by_commas_trimmed_and_deduplicated() -> None:
    spec = tb.parse_task_body(body(sections="methods,  results ,methods, , introduction"))
    assert spec["sections"] == ["methods", "results", "introduction"]


def test_plan_refs_are_upper_case_split_by_commas_or_spaces_and_deduplicated() -> None:
    spec = tb.parse_task_body(body(plan_refs="rq-1, METH-2\nrq-1  hyp-3,,"))
    assert spec["plan_refs"] == ["RQ-1", "METH-2", "HYP-3"]


def test_done_items_and_checkboxes() -> None:
    spec = tb.parse_task_body(body(done="- uno\n- [ ] dos\n- [x] tres\n  - [X] cuatro\ntexto suelto\n-cinco"))
    assert spec["done"] == ["uno", "dos", "tres", "cuatro"]


def test_done_without_list_is_one_condition() -> None:
    spec = tb.parse_task_body(body(done="Que compile\nsin errores."))
    assert spec["done"] == ["Que compile\nsin errores."]


def test_bom_and_crlf_do_not_change_the_spec_or_its_hash() -> None:
    crlf = "﻿" + SPEC_BODY.replace("\n", "\r\n")
    assert tb.parse_task_body(crlf) == SPEC_RESULT
    assert tb.normalize_body("﻿a\r\nb") == "a\nb"
    assert tb.normalize_body("a\nb") == "a\nb"


def test_spec_hash_is_the_canonical_json_of_the_four_fields() -> None:
    spec = {"done": ["ñandú"], "plan_refs": [], "sections": ["methods"], "summary": "Año", "extra": 1}
    canonical = json.dumps(
        {"done": ["ñandú"], "plan_refs": [], "sections": ["methods"], "summary": "Año"},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    assert "ñandú" in canonical  # caracteres no ASCII sin escapar (REQ-SCHEMA-72)
    expected = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert tb.compute_spec_hash(spec) == expected
    reordered = {"summary": "Año", "sections": ["methods"], "plan_refs": [], "done": ["ñandú"]}
    assert tb.compute_spec_hash(reordered) == expected


def test_spec_hash_changes_with_any_field() -> None:
    base = tb.parse_task_body(SPEC_BODY)["spec_hash"]
    assert tb.parse_task_body(SPEC_BODY.replace("metodos", "methods"))["spec_hash"] != base
    assert tb.parse_task_body(SPEC_BODY.replace("RQ-1, ", ""))["spec_hash"] != base


# -- analyze_task_body ----------------------------------------------------------------


def test_analyze_ok_keeps_warnings() -> None:
    res = tb.analyze_task_body("suelto\n" + SPEC_BODY)
    assert res.spec is not None and dict(res.spec) == SPEC_RESULT
    assert res.sections == ("metodos",)
    assert res.spec_hash == SPEC_RESULT["spec_hash"]
    assert [i.code for i in res.issues] == ["W_VALIDATION_TASK_UNKNOWN_FIELD"]


def test_analyze_error_has_no_spec() -> None:
    res = tb.analyze_task_body("suelto\n" + SPEC_BODY + "### Resumen\n\notra vez\n")
    assert res.spec is None
    assert (res.sections, res.spec_hash) == ((), "")
    assert [(i.code, i.reason) for i in res.issues] == [
        ("W_VALIDATION_TASK_UNKNOWN_FIELD", "text_before_fields"),
        ("E_VALIDATION_TASK_BODY", "repeated_field"),
    ]


def test_analyze_spec_is_read_only() -> None:
    res = tb.analyze_task_body(SPEC_BODY)
    assert res.spec is not None
    with pytest.raises(TypeError):
        res.spec["summary"] = "x"  # type: ignore[index]


# -- check_task_spec (REQ-SCHEMA-69..71) ----------------------------------------------


def _reasons(issues: list[tb.BodyIssue]) -> list[tuple[str, str, str | None]]:
    return [(i.code, i.reason, i.value) for i in issues]


def test_valid_spec_has_no_issues() -> None:
    spec = tb.parse_task_body(SPEC_BODY)
    assert tb.check_task_spec(spec, sections=["metodos"], plan_ids=["RQ-1", "meth-2"]) == []
    assert tb.check_task_spec(spec) == []  # sin la base, no se comprueban ids


def test_summary_and_done_limits() -> None:
    ok = tb.parse_task_body(SPEC_BODY)
    assert _reasons(tb.check_task_spec({**ok, "summary": ""})) == [(tb.BODY_CODE, "summary_empty", None)]
    assert tb.check_task_spec({**ok, "summary": "x" * tb.MAX_SUMMARY}) == []
    assert _reasons(tb.check_task_spec({**ok, "summary": "x" * (tb.MAX_SUMMARY + 1)})) == [
        (tb.BODY_CODE, "summary_too_long", None)
    ]
    assert _reasons(tb.check_task_spec({**ok, "done": []})) == [(tb.BODY_CODE, "done_empty", None)]
    assert tb.check_task_spec({**ok, "done": ["x"] * tb.MAX_DONE}) == []
    assert _reasons(tb.check_task_spec({**ok, "done": ["x"] * (tb.MAX_DONE + 1)})) == [
        (tb.BODY_CODE, "done_too_many", None)
    ]


def test_sections_and_plan_ids_against_the_base() -> None:
    ok = tb.parse_task_body(SPEC_BODY)
    assert _reasons(tb.check_task_spec({**ok, "sections": []}, sections=["metodos"])) == [
        ("W_VALIDATION_TASK_NO_SECTIONS", "no_sections", None)
    ]
    issues = tb.check_task_spec({**ok, "sections": ["metodos", "otra"]}, sections=["metodos"], plan_ids=["RQ-1"])
    assert _reasons(issues) == [
        ("E_VALIDATION_UNKNOWN_SECTION", "unknown_section", "otra"),
        ("E_VALIDATION_UNKNOWN_PLAN_ID", "unknown_plan_id", "METH-2"),
    ]
    assert [i.severity for i in issues] == ["error", "error"]


def test_check_tolerates_malformed_values() -> None:
    bad = {"summary": "Resumen", "sections": "methods", "plan_refs": None, "done": ["ok", 3]}
    assert _reasons(tb.check_task_spec(bad, sections=["methods"], plan_ids=[])) == [
        ("W_VALIDATION_TASK_NO_SECTIONS", "no_sections", None)
    ]


# -- render_task_body (REQ-SCHEMA-73) -------------------------------------------------


@pytest.mark.parametrize("lang", ["es", "en"])
def test_render_then_parse_is_the_identity(lang: str) -> None:
    spec = {k: SPEC_RESULT[k] for k in tb.FIELDS}
    rendered = tb.render_task_body(spec, lang)
    assert tb.parse_task_body(rendered) == SPEC_RESULT
    assert rendered.endswith("\n") and "\r" not in rendered
    assert all(f"### {label}\n" in rendered for label in tb.LABELS[lang].values())


def test_render_writes_no_response_for_empty_fields() -> None:
    rendered = tb.render_task_body({"summary": "", "sections": [], "plan_refs": [], "done": []})
    assert rendered.count("_No response_") == 4
    spec = tb.parse_task_body(rendered)
    assert {k: spec[k] for k in tb.FIELDS} == {"summary": "", "sections": [], "plan_refs": [], "done": []}


def test_labels_match_the_canonical_table() -> None:
    for lang, labels in tb.LABELS.items():
        assert {tb.CANON[label] for label in labels.values()} == set(tb.FIELDS), lang
        assert all(tb.CANON[label] == field for field, label in labels.items())
    assert tb.EMPTY == frozenset({"_no response_", "none"})
