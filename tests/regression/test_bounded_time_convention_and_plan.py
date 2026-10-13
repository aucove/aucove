# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Tiempo acotado en ``convention`` y en ``plan.md`` (09-ci REQ-CI-24).

``validate`` lee AGENTS.md, CLAUDE.md, memory/constitution.md, data/README.md y
plan.md hasta 5 MB y debe aplicar sus expresiones en tiempo acotado. Antes:

1. ``_CODE_SPAN`` (``(`+).*?\\1``) era cúbica ante un run largo de acentos graves
   sin cerrar: una línea de 16 000 tardaba unos 25 s y una de 300 000, horas.
2. ``_HEADING`` (``(.*?)`` seguido de ``[ \\t]*$``) era cuadrática con espacios
   dentro del título.
3. ``LOOKS`` de ``domain.plan`` (03 §5.6) era cuadrática con espacios al principio
   de la línea o después de la viñeta, y ``parse_plan`` copiaba el detalle acumulado
   en cada línea de continuación (cuadrático en el número de líneas). Las dos
   afectan a plan.md (``plan-ids``) y a memory/constitution.md (``convention``).

Los tamaños de estas pruebas tardaban de minutos a horas; ahora el costo es lineal.
Las pruebas de semántica fijan lo que se detecta: los encabezados y ``LOOKS`` dan lo
mismo que antes, y el código en línea sigue la regla de CommonMark (cierra el siguiente
run de la misma longitud; un run sin cierre es texto), que la expresión anterior
aproximaba cerrando a veces con un run más corto o más largo.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable, Mapping
from typing import TypeVar

import pytest

from aucove.domain import plan as dplan
from aucove.ops.rules import convention
from aucove.ops.rules.convention import Heading
from tests.conftest import load_tool
from tests.unit.test_rule_convention import UNAVAILABLE, append, codes, run_on_render
from tests.unit.test_rule_plan_ids import PLAN
from tests.unit.test_rule_plan_ids import run as run_plan_ids

LONG = 1_000_000  # una línea de ~1 MB (legal: el tope es 5 MB)
# El plazo de cada regla (REQ-CI-24). Distingue lo lineal de un retroceso catastrófico, que
# tarda minutos u horas; no mide velocidad. En local, con la cobertura de ramas de
# aucove.domain, el caso más lento tarda unos 2 s; en el CI, con 2 núcleos, unas 4 veces más.
BUDGET_S = 30.0

T = TypeVar("T")


def _timed(fn: Callable[[], T]) -> tuple[T, float]:
    start = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - start


@pytest.fixture(scope="module")
def render() -> Mapping[str, Mapping[str, bytes]]:
    tool = load_tool("render_template")
    return {lang: tool.render_files(lang) for lang in ("es", "en")}


# -- 1. código en línea ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "a " + "`" * LONG + " @x.md",  # un solo run sin cierre
        "a " + "".join("`" * k + "x" for k in range(1, 1400)),  # runs de longitudes distintas
        "a " + "`x" * (LONG // 2),  # runs de 1 que sí cierran
    ],
    ids=["unclosed-run", "distinct-runs", "many-spans"],
)
def test_code_spans_are_removed_in_linear_time(line: str) -> None:
    out, elapsed = _timed(lambda: (list(convention.outside_code(line)), convention.imports(line)))
    assert elapsed < BUDGET_S
    assert out is not None


def test_an_unclosed_run_of_backticks_is_literal_text() -> None:
    # CommonMark: un run sin otro de la misma longitud no abre código en línea.
    line = "a " + "`" * LONG + " @x.md"
    assert list(convention.outside_code(line)) == [(1, line)]
    assert convention.imports(line) == [(1, "x.md")]
    distinct = "a " + "".join("`" * k + "x" for k in range(1, 50))
    assert list(convention.outside_code(distinct)) == [(1, distinct)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@a.md, y @b/c.md.\n`@d.md`", [(1, "a.md"), (1, "b/c.md")]),
        ("``a ` @b.md`` @c.md", [(1, "c.md")]),  # el run de 1 de dentro no cierra el de 2
        ("`a`` @b.md` @c.md", [(1, "c.md")]),  # el run de 2 de dentro no cierra el de 1
        ("x ``` @a.md `` @b.md", [(1, "a.md"), (1, "b.md")]),  # sin cierre de 3 ni de 2
        ("`@a.md` @b.md ``@c.md`` `` @d.md", [(1, "b.md"), (1, "d.md")]),
    ],
)
def test_code_spans_close_with_a_run_of_the_same_length(text: str, expected: list[tuple[int, str]]) -> None:
    assert convention.imports(text) == expected


def _spans_reference(line: str) -> str:
    """Versión directa (cuadrática) de la regla de CommonMark, para comparar."""
    runs = [(m.start(), m.end()) for m in re.finditer(r"`+", line)]
    out: list[str] = []
    last = i = 0
    while i < len(runs):
        start, end = runs[i]
        close = next((j for j in range(i + 1, len(runs)) if runs[j][1] - runs[j][0] == end - start), None)
        if close is None:
            i += 1
            continue
        out.append(line[last:start])
        last, i = runs[close][1], close + 1
    out.append(line[last:])
    return "".join(out)


def test_strip_code_spans_matches_the_direct_rule() -> None:
    rng = random.Random(16)
    for _ in range(20_000):
        line = "".join(rng.choice("``a @") for _ in range(rng.randint(0, 14)))
        assert convention.strip_code_spans(line) == _spans_reference(line), repr(line)


def test_outside_code_keeps_the_text_around_spans() -> None:
    assert list(convention.outside_code("a `b` c ``d`e`` f `g")) == [(1, "a  c  f `g")]
    assert list(convention.outside_code("```\n`x`\n```\n`y` z")) == [(4, " z")]


# -- 2. encabezados ---------------------------------------------------------------------------


def test_heading_with_long_inner_whitespace_is_linear() -> None:
    gap = " \t" * (LONG // 2)
    text = "# a" + gap + "b\n## 1. x  \n"
    heads, elapsed = _timed(lambda: convention.headings(text))
    assert elapsed < BUDGET_S
    assert heads == [Heading(1, "a" + gap + "b", 1), Heading(2, "1. x", 2)]


def test_heading_titles_keep_their_meaning() -> None:
    text = "#\n#   \n# título  \t\n##\tdos\t#\n#sin espacio\n####### siete\n  # sangría\n###### seis \n"
    assert [(h.level, h.title, h.line) for h in convention.headings(text)] == [
        (1, "", 1),
        (1, "", 2),
        (1, "título", 3),
        (2, "dos\t#", 4),
        (6, "seis", 8),
    ]


def test_headings_match_the_previous_expression_on_short_lines() -> None:
    # La expresión anterior da el resultado correcto; solo su tiempo era el problema.
    previous = re.compile(r"^(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
    rng = random.Random(16)
    for _ in range(20_000):
        line = "".join(rng.choice("## \tab\r") for _ in range(rng.randint(0, 10)))
        m = previous.match(line)
        expected = [(len(m.group(1)), m.group(2) or "")] if m else []
        assert [(h.level, h.title) for h in convention.headings(line)] == expected, repr(line)


# -- 3. plan.md: LOOKS y detalle --------------------------------------------------------------


# LOOKS de 03 §5.6, copiada literalmente: la referencia de equivalencia.
SPEC_LOOKS = re.compile(
    r"^\s*(?:[-*+]\s*[*_~]*\s*(?i:RQ|HYP|IN|OUT|METH)\s*-\s*[0-9]+"
    r"|[#*_~]*\s*(?:RQ|HYP|IN|OUT|METH)-[0-9]+\s*[:.)])"
)
SYNTAX = dplan.SYNTAX_CODE


@pytest.mark.parametrize(
    "line",
    [
        "- " + " " * LONG + "x",
        " " * LONG + "x",
        "-" + "\t" * LONG + "*x",
        "***" + " " * LONG + "x",
        " " * LONG + "#" + " " * LONG + "x",
    ],
    ids=["after-bullet", "leading", "tabs", "emphasis", "after-hash"],
)
def test_plan_looks_is_linear_on_long_whitespace(line: str) -> None:
    out, elapsed = _timed(lambda: dplan.parse_plan(line))
    assert elapsed < BUDGET_S
    assert out == ([], [])


def test_plan_looks_still_flags_malformed_ids_after_long_whitespace() -> None:
    pad = " " * LONG
    text = "-" + pad + "**rq" + pad + "-" + pad + "7\n" + pad + "~~RQ-3" + pad + ":\n"
    (items, errors), elapsed = _timed(lambda: dplan.parse_plan(text))
    assert elapsed < BUDGET_S
    assert items == [] and [(e.code, e.line) for e in errors] == [(SYNTAX, 1), (SYNTAX, 2)]


def test_plan_looks_matches_the_expression_of_03_5_6() -> None:
    # Muestra determinista de líneas cortas armadas con las piezas que distinguen a LOOKS.
    pieces = [
        ["", " ", "\t ", "\u3000"],
        ["", "-", "*", "+", "#", "##", "**", "~~", "_", "-*", "- **", "x"],
        ["", " ", " \t ", "\u00a0"],
        ["", "*", "**", "_~", "#"],
        ["", " "],
        ["RQ", "rq", "Hyp", "METH", "OUTx", "IN", "in", "X"],
        ["", " "],
        ["-", "", "\u2013", "--"],
        ["", " "],
        ["1", "0", "12", "", "x"],
        ["", " ", "\t"],
        ["", ":", ".", ")", " x", ";"],
    ]
    rng = random.Random(16)
    for _ in range(40_000):
        line = "".join(rng.choice(p) for p in pieces)
        assert (dplan.LOOKS.match(line) is not None) == (SPEC_LOOKS.match(line) is not None), repr(line)


def test_plan_detail_accumulates_in_linear_time() -> None:
    lines = 1_250_000  # ~5 MB de líneas de continuación
    text = "- RQ-1: a\n" + "  x\n" * lines + "- RQ-2: b (RQ-1)\n  y\n"
    (items, errors), elapsed = _timed(lambda: dplan.parse_plan(text))
    assert elapsed < BUDGET_S
    assert errors == []
    assert [it.id for it in items] == ["RQ-1", "RQ-2"]
    assert items[0].detail == "\n".join(["x"] * lines)
    assert items[1].detail == "y" and items[1].refs == ("RQ-1",)


# -- 4. reglas completas sobre archivos patológicos ------------------------------------------


def _prepend(extra: str) -> Callable[[str], str]:
    return lambda text: extra + text


def test_convention_on_pathological_markdown_finishes(render: Mapping[str, Mapping[str, bytes]]) -> None:
    ticks = "x " + "`" * LONG + "\n"
    gap = "# a" + " " * LONG + "b\n"
    looks = "- " + " " * LONG + "x\n"
    changes = {
        "AGENTS.md": append(ticks + gap),
        "CLAUDE.md": append(ticks + gap),
        "memory/constitution.md": append(ticks + gap + looks),
        "data/README.md": _prepend(ticks + gap),
    }
    findings, elapsed = _timed(lambda: run_on_render(render, changes))
    assert elapsed < 4 * BUDGET_S
    found = codes(findings)
    assert UNAVAILABLE not in found
    # Los archivos siguen juzgándose: el tamaño excedido se reporta como siempre.
    assert "conv.agents.structure" in found and "conv.claude.content" in found
    assert "conv.constitution.structure" in found


def test_plan_ids_on_a_pathological_plan_finishes() -> None:
    pad = " " * LONG
    # El detalle largo cuelga de HYP-1, el último elemento de PLAN. Total: ~4 MB (< 5 MB).
    head = PLAN + "  detalle\n" * 200_000 + "- " + pad + "x\n" + pad + "x\n"
    assert len(head.encode("utf-8")) < 5 * 2**20
    findings, elapsed = _timed(lambda: run_plan_ids(head, PLAN))
    assert elapsed < 2 * BUDGET_S
    assert [f.code for f in findings] == [dplan.TEXT_CHANGED_CODE]
