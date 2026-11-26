# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""El resumen como una parte con dueño, en el entorno ``abstract`` de la clase (issue #30).

Con la 0.1.0rc6 no había forma válida de usar el entorno ``abstract``: ``conv.outline.grammar``
exigía que todo archivo de enlace empezara con ``\\section`` y ``\\label{sec:<id>}``, y
``conv.main.grammar`` rechazaba el entorno en ``tex/main.tex``. Solo validaba una sección sin
número, con la tipografía de ``\\section``. Ahora un archivo de enlace puede ser un bloque, un solo
entorno de principio a fin (02-convencion REQ-CONV-92), y la plantilla trae la parte ``abstract``
con su bloque (00-marco D-55). Las pruebas usan el render ``es`` de la plantilla, con todas las
reglas reales del modo ``tree``.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from tests.conftest import load_tool
from tests.unit.test_format_none import evaluate, found

OUTLINE = "tex/outline/abstract.tex"
MAIN = "tex/main.tex"
PART_INPUT = "\\input{../sections/abstract/abstract}"


@pytest.fixture(scope="module")
def project() -> Mapping[str, bytes]:
    return load_tool("render_template").render_files("es", fmt="latex")


def with_files(files: Mapping[str, bytes], **changes: str | None) -> dict[str, bytes]:
    out = dict(files)
    for key, content in changes.items():
        path = {"outline": OUTLINE, "main": MAIN}[key]
        if content is None:
            out.pop(path, None)
        else:
            out[path] = content.encode("utf-8")
    return out


def test_the_template_brings_the_abstract_as_a_block(project: Mapping[str, bytes]) -> None:
    lines = [ln for ln in project[OUTLINE].decode("utf-8").split("\n") if ln.strip() and not ln.startswith("%")]
    assert lines == ["\\begin{abstract}", PART_INPUT, "\\end{abstract}"]
    assert "\\maketitle\n\\input{outline/abstract}\n" in project[MAIN].decode("utf-8")
    assert found(evaluate(project, project), "error") == []


def test_the_first_attempt_of_the_issue_now_validates(project: Mapping[str, bytes]) -> None:
    outline = f"% tex/outline/abstract.tex\n\\begin{{abstract}}\n{PART_INPUT}\n\\end{{abstract}}\n"
    files = with_files(project, outline=outline)
    assert found(evaluate(files, project)) == []


def test_the_abstract_can_go_before_maketitle(project: Mapping[str, bytes]) -> None:
    main = project[MAIN].decode("utf-8").replace(
        "\\maketitle\n\\input{outline/abstract}\n", "\\input{outline/abstract}\n\\maketitle\n"
    )
    files = with_files(project, main=main)
    assert found(evaluate(files, project)) == []


def test_the_workaround_of_the_issue_still_validates(project: Mapping[str, bytes]) -> None:
    outline = f"\\section*{{Resumen}}\n\\label{{sec:abstract}}\n\n{PART_INPUT}\n"
    files = with_files(project, outline=outline)
    assert found(evaluate(files, project), "error") == []


def test_the_environment_in_main_tex_points_to_the_block(project: Mapping[str, bytes]) -> None:
    """El segundo intento del issue sigue fuera de la gramática, con un mensaje que lleva al bloque."""
    main = project[MAIN].decode("utf-8").replace(
        "\\input{outline/abstract}\n", f"\\begin{{abstract}}\n{PART_INPUT}\n\\end{{abstract}}\n"
    )
    files = with_files(project, main=main, outline=None)
    outcomes = evaluate(files, project)
    grammar = [f for o in outcomes for f in o.findings if f.code == "conv.main.grammar"]
    assert len(grammar) == 3
    begin, part, end = sorted(grammar, key=lambda f: f.line or 0)
    for f in (begin, end):
        assert "abre o cierra un entorno" in f.message and "`tex/outline/`" in f.message
    assert "incluye una parte" in part.message
    assert ("convention", "conv.outline.missing", "tex/outline") in found(outcomes, "warning")
