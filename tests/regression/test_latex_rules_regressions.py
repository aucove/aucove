# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Reglas LaTeX (09-ci §4.1; 02-convencion §4.6).

1. ``\\addglobalbib``, ``\\addsectionbib`` y ``\\nobibliography`` en una sección no
   daban ningún hallazgo: ``citations`` los dejaba a ``conv.latex.structure``, pero
   esa regla solo conocía ``\\addbibresource`` y ``\\bibliography`` (REQ-CONV-34).
2. ``sentence_breaks`` era cuadrática por línea (copiaba el prefijo de la línea en
   cada candidato) y no consultaba el ``Deadline``: una línea de prosa de cientos de
   KB superaba el plazo sin ``ScanTimeout`` (REQ-CI-24).
3. ``latex-macros`` no veía definidores de color, teorema, entorno o registro como
   ``\\colorlet``, ``\\declaretheorem``, ``\\newtcolorbox`` o ``\\newsavebox``
   (REQ-CONV-20: «toda definición»).
"""

from __future__ import annotations

import time

import pytest

from aucove.domain import latex as tex
from aucove.ops.rules import latex
from aucove.ops.rules.base import UNAVAILABLE_CODE
from tests.unit.test_rule_latex import METHODS, RESULTS, run, summary, text_of, tree_with

# -- 1. recursos bibliográficos en una sección -------------------------------------------


def _append(path: str, lines: list[str]) -> tuple[str, int]:
    """Texto de ``path`` con ``lines`` al final y el número de la primera línea agregada."""
    body = text_of(path)
    assert body.endswith("\n")
    return body + "".join(line + "\n" for line in lines), body.count("\n") + 1


BIB_LINES = [
    "\\addglobalbib[location=remote]{https://evil.example/x.bib}",
    "\\addsectionbib{../sections/methods/other.bib}",
    "\\nobibliography{x}",
    "\\addbibresource{x.bib}",
]


def test_every_bib_resource_command_in_a_section_is_a_structure_error() -> None:
    body, first = _append(METHODS, BIB_LINES)
    tree = tree_with({METHODS: body})
    structure = [f for f in run(latex.CONVENTION, tree) if f.code == "conv.latex.structure"]
    assert [(f.severity, f.path, f.line) for f in structure] == [
        ("error", METHODS, first + i) for i in range(len(BIB_LINES))
    ]
    for finding, name in zip(structure, ("addglobalbib", "addsectionbib", "nobibliography", "addbibresource")):
        assert f"\\{name}" in finding.message
    # citations no lo duplica: en una sección lo informa conv.latex.structure.
    assert run(latex.CITATIONS, tree) == []


@pytest.mark.parametrize("line", BIB_LINES[:3])
def test_each_bib_resource_variant_alone(line: str) -> None:
    body, first = _append(RESULTS, [line])
    assert ("conv.latex.structure", "error", RESULTS, first) in summary(run(latex.CONVENTION, tree_with({RESULTS: body})))


def test_section_skip_in_citations_is_covered_by_structure() -> None:
    # citations salta en las secciones todo comando de BIB_RESOURCE_COMMANDS porque
    # conv.latex.structure lo informa; las dos listas no deben desalinearse.
    assert latex.BIB_RESOURCE_COMMANDS <= latex.STRUCTURE_COMMANDS


# -- 2. sentence_breaks lineal y con plazo ------------------------------------------------


class CountingClock:
    """Reloj falso que avanza un segundo en cada consulta."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


def test_sentence_breaks_consults_the_deadline_inside_a_line() -> None:
    # Cada «e.g. B» es un candidato que la abreviatura descarta: no hay ruptura.
    line = "e.g. B " * 50_000
    assert tex.sentence_breaks(line) == []
    clock = CountingClock()
    dl = tex.Deadline(3.0, clock=clock)
    with pytest.raises(tex.ScanTimeout):
        tex.sentence_breaks(line, deadline=dl)
    with pytest.raises(tex.ScanTimeout):
        tex.first_sentence_break(line, deadline=tex.Deadline(3.0, clock=CountingClock()))


def test_sentence_breaks_is_linear_on_a_long_line() -> None:
    # 3,5 MB en una sola línea (legal: el límite es 5 MB). Antes tardaba minutos. El tope es el
    # plazo de cada regla (REQ-CI-24), no una medida de velocidad: en el CI, con 2 núcleos y la
    # cobertura de ramas de aucove.domain, el caso tarda unas 4 veces lo que en local.
    line = "e.g. B " * 500_000
    start = time.perf_counter()
    assert tex.first_sentence_break(line, deadline=tex.Deadline(latex.RULE_TIME_BUDGET_S)) is None
    assert tex.sentence_breaks(line, deadline=tex.Deadline(latex.RULE_TIME_BUDGET_S)) == []
    assert time.perf_counter() - start < latex.RULE_TIME_BUDGET_S


def test_first_sentence_break_matches_the_full_list() -> None:
    for line in ("One. Two. Three.", "Uno. ¿Dos? ¡Tres!", "See e.g. Smith. Then", "Ends here.", "", "a.  \tB"):
        breaks = tex.sentence_breaks(line)
        assert tex.first_sentence_break(line) == (breaks[0] if breaks else None), line
    assert tex.sentence_breaks("a.  \tB c! D") == [1, 8]
    # Abreviatura al principio de la línea y precedida de letra (no es abreviatura).
    assert tex.sentence_breaks("p. Smith") == [] and tex.sentence_breaks("Xp. Smith") == [2]


def test_convention_on_a_huge_prose_line_finishes_and_still_warns() -> None:
    # Una sección con una línea de prosa de ~3,5 MB y una ruptura real al final.
    # El tope es el plazo de la regla (REQ-CI-24): si se pasara, la regla cortaría y no
    # avisaría; en el CI lento, con cobertura, tarda unos 12 s.
    huge = "e.g. B " * 500_000 + "End. Next"
    body, first = _append(RESULTS, [huge])
    start = time.perf_counter()
    findings = run(latex.CONVENTION, tree_with({RESULTS: body}))
    assert time.perf_counter() - start < latex.RULE_TIME_BUDGET_S
    warned = [f for f in findings if f.code == "conv.latex.sentence-per-line" and f.line == first]
    assert [(f.severity, f.path) for f in warned] == [("warning", RESULTS)]
    assert "End. Next" in warned[0].message  # la ruptura real, al final de la línea
    assert not [f for f in findings if f.code == UNAVAILABLE_CODE]


# -- 3. más definidores en latex-macros ---------------------------------------------------


NEW_DEFINITIONS = [
    "\\colorlet{peligro}{red}",
    "\\providecolor{aviso}{rgb}{1,0,0}",
    "\\definecolorset{rgb}{}{}{rojo,1,0,0}",
    "\\declaretheorem{lema}",
    "\\declaretheoremstyle{simple}",
    "\\newtheoremstyle{nota}{}{}{}{}{}{}{ }{}",
    "\\newtcolorbox{caja}{}",
    "\\renewtcolorbox{caja}{}",
    "\\NewTColorBox{caja}{}{}",
    "\\newtcbtheorem{teo}{Teorema}{}{th}",
    "\\newsavebox{\\caja}",
    "\\newfontfamily\\griego{Font}",
    "\\newfontface\\titulo{Font}",
    "\\newlist{pasos}{enumerate}{3}",
    "\\renewlist{pasos}{enumerate}{3}",
    "\\NewEnviron{cuadro}{\\BODY}",
    "\\DeclareFloatingEnvironment{grafo}",
    "\\newmuskip\\espacio",
]


@pytest.mark.parametrize("line", NEW_DEFINITIONS)
def test_more_definitions_in_a_section_are_macro_errors(line: str) -> None:
    body, first = _append(RESULTS, [line])
    findings = run(latex.LATEX_MACROS, tree_with({RESULTS: body}))
    assert summary(findings) == [("E_VALIDATION_MACRO", "error", RESULTS, first)], line
    name = line[1:].split("{", 1)[0].split("\\", 1)[0]
    assert f"\\{name}" in findings[0].message


@pytest.mark.parametrize("path", ["tex/preamble.tex", "tex/appendix.tex"])
def test_more_definitions_in_lead_files_are_macro_errors(path: str) -> None:
    body, first = _append(path, ["\\colorlet{peligro}{red}", "\\declaretheorem{lema}", "\\newsavebox{\\caja}"])
    findings = run(latex.LATEX_MACROS, tree_with({path: body}))
    assert summary(findings) == [("E_VALIDATION_MACRO", "error", path, first + i) for i in range(3)]


def test_more_definitions_are_allowed_in_notation() -> None:
    body, _first = _append("tex/notation.tex", NEW_DEFINITIONS)
    assert run(latex.LATEX_MACROS, tree_with({"tex/notation.tex": body})) == []
