# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Analizador de ``plan.md`` (03-esquemas §5; ``aucove.domain.plan``).

- Acepta el ejemplo de 03 §5.1 y da la salida de 03 §5.6 para ``RQ-3``.
- Rechaza los IDs mal formados de REQ-SCHEMA-36 con ``E_VALIDATION_PLAN_SYNTAX``.
- Coincide con el analizador de referencia de 03 §5.6, transcrito aquí tal cual,
  en una tabla de casos y en entradas generadas con Hypothesis.
- Reglas entre versiones de 03 §5.4 (``compare_plans``).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aucove.domain import plan as dplan
from aucove.domain.plan import PlanChange, PlanError, compare_plans, parse_plan

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "convention" / "plan"

# -- analizador de referencia de 03 §5.6, copiado literalmente --------------------------

_PLAN_ID = r"(?:RQ|HYP|IN|OUT|METH)-[1-9][0-9]{0,3}"
_DEC_ID = r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[1-9][0-9]*-[a-z0-9]+(?:-[a-z0-9]+)*"
_DEF = re.compile(rf"^- (?P<id>{_PLAN_ID})(?P<ret> \(retired(?:: (?P<dec>{_DEC_ID}))?\))?: (?P<text>\S.*)$")
_LOOKS = re.compile(r"^\s*(?:[-*+]\s*[*_~]*\s*(?i:RQ|HYP|IN|OUT|METH)\s*-\s*[0-9]+"
                    r"|[#*_~]*\s*(?:RQ|HYP|IN|OUT|METH)-[0-9]+\s*[:.)])")
_REF = re.compile(rf"\b{_PLAN_ID}\b")


def reference_parse_plan(text):  # type: ignore[no-untyped-def]
    items, errors, fence, in_comment, cur = {}, [], None, False, None
    for no, line in enumerate(text.split("\n"), 1):
        s = line.strip()
        if in_comment:
            in_comment = "-->" not in line; continue  # noqa: E702
        if fence:
            fence = None if s.startswith(fence) else fence; continue  # noqa: E702
        if s.startswith(("```", "~~~")):
            fence, cur = s[:3], None; continue  # noqa: E702
        if s.startswith("<!--"):
            in_comment = "-->" not in s[4:]; continue  # noqa: E702
        if m := _DEF.match(line):
            i = m["id"]
            if i in items:
                errors.append(("E_VALIDATION_DUPLICATE_ID", no, i)); cur = None; continue  # noqa: E702
            t, retired = m["text"].strip(), m["ret"] is not None
            if retired and len(t) > 4 and t.startswith("~~") and t.endswith("~~"):
                t = t[2:-2].strip()
            cur = items[i] = {"id": i, "kind": i.split("-")[0], "line": no, "detail": "",
                              "status": "retired" if retired else "active",
                              "retired_by": m["dec"], "text": t}
            continue
        if _LOOKS.match(line):
            errors.append(("E_VALIDATION_PLAN_SYNTAX", no, s)); cur = None; continue  # noqa: E702
        if cur is not None and line.startswith("  ") and s:
            cur["detail"] += ("\n" if cur["detail"] else "") + s; continue  # noqa: E702
        cur = None
    for it in items.values():
        it["refs"] = sorted(set(_REF.findall(it["text"] + "\n" + it["detail"])) - {it["id"]})
    return list(items.values()), errors


def as_reference(text: str) -> tuple[list[dict[str, object]], list[tuple[str, int, str]]]:
    items, errors = parse_plan(text)
    return [it.to_dict() for it in items], [tuple(e) for e in errors]  # type: ignore[misc]


def _same_as_reference(text: str) -> None:
    ref_items, ref_errors = reference_parse_plan(text)
    items, errors = as_reference(text)
    assert [dict(sorted(d.items())) for d in items] == [dict(sorted(d.items())) for d in ref_items]
    assert errors == ref_errors


# -- ejemplo de 03 §5.1 --------------------------------------------------------------------


def example() -> str:
    return (FIXTURES / "example-03.md").read_text(encoding="utf-8")


def test_accepts_the_example_of_03_5_1() -> None:
    items, errors = parse_plan(example())
    assert errors == []
    assert [it.id for it in items] == [
        "RQ-1", "RQ-2", "RQ-3", "HYP-1", "HYP-2", "IN-1", "IN-2", "OUT-1", "METH-1", "METH-2",
    ]
    assert "RQ-99" not in {it.id for it in items}  # dentro de un comentario HTML


def test_example_item_matches_the_output_of_03_5_6() -> None:
    by_id = dplan.index(parse_plan(example())[0])
    assert by_id["RQ-3"].to_dict() == {
        "id": "RQ-3",
        "kind": "RQ",
        "status": "retired",
        "retired_by": "2026-10-06-12-fusionar-rq3-en-rq1",
        "text": "¿Cambió el número de viajes por motivo trabajo?",
        "detail": "",
        "refs": [],
        "line": 17,
    }
    assert by_id["RQ-2"].detail == "Se responde con las dos olas de la encuesta origen-destino."
    assert by_id["HYP-1"].refs == ("RQ-2",)
    assert by_id["METH-2"].refs == ("IN-2",)
    assert by_id["HYP-2"].status == "retired" and by_id["HYP-2"].retired_by is None
    assert by_id["HYP-2"].text == "El efecto desaparece en 2024."
    assert by_id["METH-1"].kind == "METH" and by_id["METH-1"].number == 1


def test_example_has_no_unknown_references() -> None:
    assert dplan.unknown_refs(parse_plan(example())[0]) == []


def test_example_is_parsed_like_the_reference() -> None:
    _same_as_reference(example())


# -- IDs mal formados (REQ-SCHEMA-36) ---------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "- rq-2: minúsculas",
        "* HYP-1: otra viñeta",
        "- RQ-04: cero a la izquierda",
        "- HYP-2 texto sin dos puntos",
        "RQ-5: sin viñeta",
        "### HYP-3: como encabezado",
        "  - RQ-9: con sangría",
        "- RQ-10000: cinco cifras",
        "- RQ-0: número cero",
        "- METH -1: espacio antes del guion",
        "- IN-1 (retired: no-es-una-decision): decisión mal formada",
        "- OUT-1:sin espacio",
        "- RQ-1: ",
        "+ **RQ-7**: con negrita",
    ],
)
def test_rejects_malformed_ids(line: str) -> None:
    items, errors = parse_plan(f"# Plan\n\n{line}\n")
    assert items == []
    assert errors == [PlanError("E_VALIDATION_PLAN_SYNTAX", 3, line.strip())]
    assert errors == [("E_VALIDATION_PLAN_SYNTAX", 3, line.strip())]  # igual que la tupla de referencia
    _same_as_reference(f"# Plan\n\n{line}\n")


@pytest.mark.parametrize(
    "line",
    [
        "- RQ-1: una pregunta",
        "- HYP-9999: el número máximo",
        "- IN-2 (retired): ~~retirado~~",
        "- OUT-3 (retired: 2026-10-06-12-quitar-out3): ~~retirado con decisión~~",
        "- METH-1: método (RQ-1, HYP-9999)",
    ],
)
def test_accepts_well_formed_definitions(line: str) -> None:
    items, errors = parse_plan(line + "\n")
    assert errors == []
    assert len(items) == 1


def test_text_that_only_mentions_ids_is_not_a_definition() -> None:
    items, errors = parse_plan("Este párrafo cita RQ-1 y HYP-2 sin definirlos.\n- Una viñeta normal.\n")
    assert items == [] and errors == []


def test_duplicate_id_keeps_the_first_definition() -> None:
    text = "- RQ-1: primera\n- RQ-1: segunda\n  detalle que no se agrega\n- RQ-2: otra\n"
    items, errors = parse_plan(text)
    assert errors == [PlanError("E_VALIDATION_DUPLICATE_ID", 2, "RQ-1")]
    assert [(it.id, it.text, it.detail) for it in items] == [("RQ-1", "primera", ""), ("RQ-2", "otra", "")]
    _same_as_reference(text)


# -- código, comentarios y detalle (REQ-SCHEMA-35) ------------------------------------------


def test_ignores_fenced_code_and_html_comments() -> None:
    text = "\n".join(
        [
            "```markdown",
            "- RQ-50: dentro de código",
            "- rq-51: tampoco es error",
            "```",
            "~~~",
            "- HYP-50: dentro de otro bloque",
            "~~~",
            "<!-- - RQ-60: comentario de una línea -->",
            "<!--",
            "- RQ-61: comentario de varias líneas",
            "-->",
            "- RQ-1: la única definición",
        ]
    )
    items, errors = parse_plan(text)
    assert [it.id for it in items] == ["RQ-1"]
    assert errors == []
    _same_as_reference(text)


def test_detail_lines_and_their_references() -> None:
    text = "- RQ-1: pregunta\n  detalle con HYP-1\n    más detalle\n\n  no es detalle tras línea en blanco\n- HYP-1: hipótesis (RQ-1, RQ-1)\n"
    items, errors = parse_plan(text)
    by_id = dplan.index(items)
    assert by_id["RQ-1"].detail == "detalle con HYP-1\nmás detalle"
    assert by_id["RQ-1"].refs == ("HYP-1",)
    assert by_id["HYP-1"].refs == ("RQ-1",)  # sin repetir y sin el propio ID
    assert errors == []
    _same_as_reference(text)


def test_retired_text_keeps_strikethrough_when_too_short() -> None:
    items, _errors = parse_plan("- RQ-1 (retired): ~~~~\n- RQ-2 (retired): ~~a~~\n- RQ-3: ~~activo~~\n")
    by_id = dplan.index(items)
    assert by_id["RQ-1"].text == "~~~~"  # len 4: no se quita (referencia)
    assert by_id["RQ-2"].text == "a"
    assert by_id["RQ-3"].text == "~~activo~~"  # solo se quita en los retirados


def test_unknown_references() -> None:
    items, _errors = parse_plan("- RQ-1: pregunta\n- HYP-1: responde RQ-1 y RQ-7\n  y METH-2\n")
    assert [(it.id, ref) for it, ref in dplan.unknown_refs(items)] == [("HYP-1", "METH-2"), ("HYP-1", "RQ-7")]


def test_id_helpers() -> None:
    assert dplan.id_kind("METH-12") == "METH"
    assert dplan.id_number("METH-12") == 12
    with pytest.raises(ValueError):
        dplan.id_number("RQ-012")
    with pytest.raises(ValueError):
        dplan.id_number("XX-1")


# -- tabla de casos frente a la referencia ---------------------------------------------------

CASES = [
    "",
    "\n\n",
    "- RQ-1: a\n  b\n  c\n- RQ-2: d\ntexto\n  sangría sin elemento\n",
    "```\n- RQ-1: abierto sin cerrar\n",
    "<!-- abierto\n- RQ-1: nunca cierra\n",
    "- RQ-1: a\n```\n  no es detalle\n```\n  tampoco\n",
    "- RQ-1: a\n<!-- c -->\n  no es detalle tras el comentario\n",
    "- RQ-1 (retired: 2026-01-01-1-x): ~~t~~\n- RQ-1 (retired): ~~t~~\n",
    "   - RQ-3: sangría de tres\n\t- RQ-4: tabulador\n",
    "- RQ-1: a\r\n- RQ-2: con CR\r\n",
    "* rq - 5 : suelto\n_RQ-6_: subrayado\n~RQ-7) tachado\n",
    "- RQ-1: cita RQ-12 y RQ-1234 y RQ-12345 y xRQ-2\n",
]


@pytest.mark.parametrize("text", CASES)
def test_matches_the_reference_on_edge_cases(text: str) -> None:
    _same_as_reference(text)


try:
    from hypothesis import given, settings
    from hypothesis import strategies as st
except ImportError:  # pragma: no cover - hypothesis es dependencia de desarrollo
    given = None


if given is not None:
    _FRAGMENTS = st.sampled_from(
        [
            "- RQ-1: a", "- RQ-1: b", "- HYP-2 (retired): ~~x~~", "- IN-3 (retired: 2026-10-06-12-a-b): t",
            "  detalle RQ-1", "  ", "", "```", "~~~", "<!--", "-->", "<!-- x -->", "- rq-1: m",
            "RQ-9: s", "### METH-4: h", "texto METH-4", "* OUT-1: o", "- OUT-2: (RQ-9)", "\t- RQ-5: t",
        ]
    )

    @settings(max_examples=300, database=None, deadline=None)
    @given(st.lists(_FRAGMENTS, max_size=25))
    def test_matches_the_reference_on_generated_plans(lines: list[str]) -> None:
        _same_as_reference("\n".join(lines))


# -- reglas entre versiones (03 §5.4) --------------------------------------------------------


def _items(text: str) -> list[dplan.PlanItem]:
    items, errors = parse_plan(text)
    assert errors == []
    return items


BASE = "- RQ-1: a\n- RQ-2 (retired): ~~b~~\n- RQ-3 (retired: 2026-10-06-12-x): ~~c~~\n- HYP-1: h\n"


def test_same_plan_has_no_changes() -> None:
    assert compare_plans(_items(BASE), _items(BASE)) == []


def test_removed_id() -> None:
    head = "- RQ-2 (retired): ~~b~~\n- RQ-3 (retired: 2026-10-06-12-x): ~~c~~\n- HYP-1: h\n"
    assert compare_plans(_items(BASE), _items(head)) == [PlanChange("E_VALIDATION_PLAN_ID_REMOVED", "RQ-1", None)]


def test_retired_id_reactivated_or_changed() -> None:
    reactivated = BASE.replace("- RQ-2 (retired): ~~b~~", "- RQ-2: b")
    changed = BASE.replace("~~b~~", "~~otro~~")
    new_decision = BASE.replace("- RQ-3 (retired: 2026-10-06-12-x)", "- RQ-3 (retired: 2026-10-06-12-y)")
    lost_decision = BASE.replace("- RQ-3 (retired: 2026-10-06-12-x)", "- RQ-3 (retired)")
    assert compare_plans(_items(BASE), _items(reactivated)) == [
        PlanChange("E_VALIDATION_PLAN_RETIRED_CHANGED", "RQ-2", 2)
    ]
    assert compare_plans(_items(BASE), _items(changed)) == [PlanChange("E_VALIDATION_PLAN_RETIRED_CHANGED", "RQ-2", 2)]
    assert [c.id for c in compare_plans(_items(BASE), _items(new_decision))] == ["RQ-3"]
    assert [c.id for c in compare_plans(_items(BASE), _items(lost_decision))] == ["RQ-3"]


def test_decision_may_be_added_to_a_retirement() -> None:
    head = BASE.replace("- RQ-2 (retired)", "- RQ-2 (retired: 2026-10-07-3-quitar-rq2)")
    assert compare_plans(_items(BASE), _items(head)) == []


def test_new_ids_must_exceed_the_base_maximum() -> None:
    head = BASE + "- RQ-4: nueva\n- HYP-1000: otra\n- METH-1: primer método\n"
    assert compare_plans(_items(BASE), _items(head)) == []
    gap = "- RQ-1: a\n- RQ-3: c\n"
    reused = _items("- RQ-1: a\n- RQ-2: reusa el hueco\n- RQ-3: c\n")
    assert compare_plans(_items(gap), reused) == [PlanChange("E_VALIDATION_PLAN_ID_REUSED", "RQ-2", 2, 3)]


def test_active_text_or_detail_change_is_a_warning() -> None:
    text_changed = BASE.replace("- RQ-1: a", "- RQ-1: a cambiada")
    detail_added = BASE.replace("- RQ-1: a\n", "- RQ-1: a\n  con detalle\n")
    retired_with_other_text = BASE.replace("- RQ-1: a", "- RQ-1 (retired): ~~otra~~")
    retired_same_text = BASE.replace("- RQ-1: a", "- RQ-1 (retired): ~~a~~")
    expected = [PlanChange("W_VALIDATION_PLAN_TEXT_CHANGED", "RQ-1", 1)]
    assert compare_plans(_items(BASE), _items(text_changed)) == expected
    assert compare_plans(_items(BASE), _items(detail_added)) == expected
    assert compare_plans(_items(BASE), _items(retired_with_other_text)) == expected
    assert compare_plans(_items(BASE), _items(retired_same_text)) == []


def test_empty_base_accepts_any_head() -> None:
    assert compare_plans([], _items(BASE)) == []
