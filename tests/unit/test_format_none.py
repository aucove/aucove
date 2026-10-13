# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``validate`` con ``format: none`` sobre el render ``none`` de la plantilla (00-marco D-43).

Con todas las reglas reales:

- REQ-CONV-3: el render ``none`` pasa ``validate`` sin errores. Las dos pruebas de la
  operación completa usan un repositorio git temporal, como ``test_validate_modes`` (modo
  ``tree`` y modo ``base``, el de ``main-broken.yml``), y por eso tardan unos segundos; las
  demás evalúan el motor sobre árboles en memoria.
- 00-marco D-33 y 09-ci §4.1: ``citations``, ``labels-refs``, ``latex-macros`` y
  ``latex-log`` dan ``skip``, y ``convention`` no informa ningún ``conv.bib.*``,
  ``conv.latex.*``, ``conv.main.*`` ni ``conv.latexmkrc.*``.
- REQ-CONV-88 y REQ-CONV-89: lo que sobra del manuscrito es un aviso; un ``.tex`` en una parte
  es un error de tipo.
- REQ-SCHEMA-90: el esquema rechaza ``build`` con ``format: none``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

import pytest

from aucove.config.project import parse_project
from aucove.i18n import LANGS, translator
from aucove.ops import validate
from aucove.ops.result import Result
from aucove.ops.rules.base import RuleInput
from tests.conftest import GitRepo, load_tool
from tests.unit.test_rule_convention import DictTree, snapshot
from tests.unit.test_validate_modes import by_check, run_validate

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
SECTION = "sections/introduction/"
LATEX_RULES = ("citations", "labels-refs", "latex-macros")
# Orden de results[]: el de primera aparición en los módulos de reglas.
TREE_RULES = ("schemas", "roles-sync", "generated-sync", "plan-ids", *LATEX_RULES, "convention", "instructions-lint",
              "ci-policy")

Outcomes = Sequence[validate.RuleOutcome]
Render = Mapping[str, Mapping[str, bytes]]  # idioma -> ruta -> bytes del render none


@pytest.fixture(scope="module")
def none_render() -> Render:
    tool = load_tool("render_template")
    return {lang: tool.render_files(lang, fmt="none") for lang in LANGS}


def evaluate(files: Mapping[str, bytes], base: Mapping[str, bytes], *, lang: str = "es",
             checks: Sequence[str] | None = None) -> Outcomes:
    """Todas las reglas reales del modo ``tree`` sobre ``files``; la base y la política son ``base``."""
    inp = RuleInput(mode="tree", t=translator(lang), now=NOW, policy=snapshot(base),  # type: ignore[arg-type]
                    tree=DictTree(files), base_tree=DictTree(base))  # type: ignore[arg-type]
    return validate.evaluate(validate.load_rules(), inp, checks=checks)


def statuses(outcomes: Outcomes) -> dict[str, str]:
    return {o.check: o.status for o in outcomes}


def found(outcomes: Outcomes, severity: str | None = None) -> list[tuple[str, str, str | None]]:
    return [(o.check, f.code, f.path) for o in outcomes for f in o.findings if severity in (None, f.severity)]


def seed(repo: GitRepo, files: Mapping[str, bytes]) -> str:
    """Commit raíz con ``files`` en ``main``, publicado en ``origin`` (la política de modo tree)."""
    sha = repo.commit("Initial commit from the template", files=dict(files))
    repo.push("HEAD:refs/heads/main")
    repo.fetch()
    return sha


def result_findings(result: Result) -> list[tuple[str, str]]:
    assert result.data is not None
    return [(r["check"], f["code"]) for r in result.data["results"] for f in r["findings"]]  # type: ignore[union-attr]


# -- la operación completa (REQ-CONV-3) --------------------------------------------------------


def test_none_render_passes_validate_in_tree_mode(git_repo: GitRepo, none_render: Render) -> None:
    seed(git_repo, none_render["es"])
    result = run_validate(git_repo.path, git_repo.env)
    assert result.ok, result.to_dict()
    results = by_check(result)
    assert tuple(results) == TREE_RULES
    assert {rule: results[rule]["status"] for rule in LATEX_RULES} == dict.fromkeys(LATEX_RULES, "skip")
    assert all(results[rule]["status"] == "pass" for rule in TREE_RULES if rule not in LATEX_RULES)
    assert result_findings(result) == []  # ni errores ni avisos: ningún conv.bib.*, conv.latex.* ni conv.main.*


def test_none_render_passes_validate_in_base_mode(git_repo: GitRepo, none_render: Render) -> None:
    """El job validate de main-broken.yml (09-ci §7.1) sobre el commit raíz de un proyecto sin manuscrito."""
    seed(git_repo, none_render["en"])
    result = run_validate(git_repo.path, git_repo.env, mode="base", policy=".", tree=".")
    assert result.ok, result.to_dict()
    assert {rule: by_check(result)[rule]["status"] for rule in LATEX_RULES} == dict.fromkeys(LATEX_RULES, "skip")
    assert result_findings(result) == []


# -- el motor con las reglas reales ------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_none_render_has_no_findings_in_either_language(none_render: Render, lang: str) -> None:
    files = none_render[lang]
    out = evaluate(files, files, lang=lang)
    assert tuple(statuses(out)) == TREE_RULES
    assert {rule: statuses(out)[rule] for rule in LATEX_RULES} == dict.fromkeys(LATEX_RULES, "skip")
    assert found(out) == []


def test_latex_log_is_skipped_when_requested(none_render: Render) -> None:
    files = none_render["es"]
    out = evaluate(files, files, checks=["latex-log", "citations", "convention"])
    assert [(o.check, o.status) for o in out] == [("citations", "skip"), ("convention", "pass"), ("latex-log", "skip")]


def test_leftovers_of_the_manuscript_only_warn(none_render: Render) -> None:
    """REQ-CONV-88: tex/, bib/ y pending.bib sobran, pero no estorban; conv.bib.* queda en skip."""
    base = none_render["es"]
    files = {
        **base,
        "tex/main.tex": b"\\documentclass{article}\n\\usepackage{x}\n",
        "bib/references.bib": b"@book{9bad, title = {x}, file = {y}}\n",  # clave inválida y campo file
        SECTION + "pending.bib": b"% pendiente\n",
    }
    out = evaluate(files, base)
    assert found(out) == [("convention", "conv.format.unused", "project.yaml")]
    assert {statuses(out)[rule] for rule in LATEX_RULES} == {"skip"}


def test_a_tex_file_in_a_part_is_a_type_error(none_render: Render) -> None:
    """REQ-CONV-89: con format: none, un .tex en una parte es un tipo no permitido."""
    base = none_render["en"]
    out = evaluate({**base, SECTION + "introduction.tex": b"\\section{Introduction}\n"}, base, lang="en")
    assert found(out, "error") == [("convention", "conv.section.disallowed-file", SECTION + "introduction.tex")]
    assert statuses(out)["convention"] == "fail"
    assert {statuses(out)[rule] for rule in LATEX_RULES} == {"skip"}  # sin manuscrito igual


def test_schema_rejects_build_with_format_none(none_render: Render) -> None:
    """REQ-SCHEMA-90: con format: none, project.yaml no admite el bloque build."""
    text = none_render["en"]["project.yaml"].decode("utf-8")
    cfg, problems = parse_project(text)
    assert cfg is not None and cfg.format == "none" and problems == []
    with_build = text + "build:\n  engine: pdflatex\n"
    cfg, problems = parse_project(with_build)
    assert cfg is None
    assert [p.code for p in problems] == ["E_VALIDATION_SCHEMA"]
    latex = with_build.replace("format: none", "format: latex")
    assert parse_project(latex)[0] is not None  # con latex, el mismo bloque es válido


def test_schemas_rule_reports_build_with_format_none(none_render: Render) -> None:
    base = none_render["es"]
    out = evaluate({**base, "project.yaml": base["project.yaml"] + b"build:\n  engine: pdflatex\n"}, base)
    assert ("schemas", "E_VALIDATION_SCHEMA", "project.yaml") in found(out, "error")
    # Con el project.yaml del árbol inválido, el formato es el de la base: sigue sin manuscrito.
    assert {statuses(out)[rule] for rule in LATEX_RULES} == {"skip"}
