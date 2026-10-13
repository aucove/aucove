# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Reglas ``citations``, ``labels-refs``, ``latex-macros`` y ``convention`` LaTeX.

09-ci §4.1; 02-convencion §4, §5 y §12; D-33 (g). También cubre el
escáner ``aucove.domain.latex``, que no ejecuta nada y tiene tiempo acotado.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aucove.config.base import BaseSnapshot
from aucove.config.project import parse_project
from aucove.config.roles import parse_roles
from aucove.domain import latex as tex
from aucove.domain.findings import Finding, has_errors
from aucove.i18n import LANGS, Translator, load_catalog, placeholders, translator
from aucove.ops.rules import latex, latex_log
from aucove.ops.rules.base import UNAVAILABLE_CODE, RuleInput, RuleSpec, status_of
from aucove.vcs.tree import worktree
from tests.conftest import FIXTURES, REPO_ROOT, GitRepo, load_tool, roles_fixture

TREE = FIXTURES / "latex" / "tree"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
INTRO = "sections/introduction/introduction.tex"
METHODS = "sections/methods/methods.tex"
RESULTS = "sections/results/results.tex"
INTRO_BIB = "sections/introduction/pending.bib"
METHODS_BIB = "sections/methods/pending.bib"
OUTLINE_METHODS = "tex/outline/methods.tex"
OUTLINE_RESULTS = "tex/outline/results.tex"


class DictTree:
    """``TreeView`` en memoria: ruta -> bytes, con modos opcionales (120000, 160000...)."""

    def __init__(self, files: Mapping[str, bytes | str], modes: Mapping[str, str] | None = None) -> None:
        self.files = {p: (c.encode("utf-8") if isinstance(c, str) else c) for p, c in files.items()}
        self.modes = dict(modes or {})
        self.root: Path | None = None
        self.sha: str | None = None
        self.reads: list[str] = []

    def entries(self) -> Mapping[str, str]:
        out = {p: "100644" for p in self.files}
        out.update(self.modes)
        return out

    def read_bytes(self, path: str, limit: int = 5 * 2**20) -> bytes | None:
        self.reads.append(path)
        mode = self.entries().get(path)
        if mode is None or not mode.startswith("100") or path not in self.files:
            return None
        data = self.files[path]
        return data if len(data) <= limit else None

    def read_text(self, path: str, limit: int = 5 * 2**20) -> str | None:
        data = self.read_bytes(path, limit)
        try:
            return data.decode("utf-8") if data is not None else None
        except UnicodeDecodeError:
            return None


def fixture_files() -> dict[str, bytes | str]:
    files: dict[str, bytes | str] = {
        p.relative_to(TREE).as_posix(): p.read_bytes() for p in sorted(TREE.rglob("*")) if p.is_file()
    }
    files["roles.yaml"] = roles_fixture("basic")
    return files


def tree_with(changes: Mapping[str, str | bytes | None] | None = None, **kw: object) -> DictTree:
    files = fixture_files()
    for path, content in (changes or {}).items():
        if content is None:
            files.pop(path, None)
        else:
            files[path] = content
    return DictTree(files, **kw)  # type: ignore[arg-type]


def text_of(path: str) -> str:
    return (TREE / path).read_text(encoding="utf-8")


def run(
    rule: RuleSpec,
    tree: DictTree | None,
    *,
    t: Translator | None = None,
    policy: BaseSnapshot | None = None,
    mode: str = "tree",
) -> list[Finding]:
    inp = RuleInput(mode=mode, t=t or translator("en"), now=NOW, policy=policy, tree=tree)  # type: ignore[arg-type]
    return list(rule.run(inp))


def summary(findings: Sequence[Finding]) -> list[tuple[str, str, str | None, int | None]]:
    return [(f.code, f.severity, f.path, f.line) for f in findings]


def codes(findings: Sequence[Finding]) -> list[str]:
    return [f.code for f in findings]


ALL_RULES = latex.RULES


# -- registro y catálogo -------------------------------------------------------------------


def test_rules_registry() -> None:
    assert [r.id for r in latex.RULES] == ["citations", "labels-refs", "latex-macros", "convention"]
    for rule in latex.RULES:
        assert rule.modes == frozenset({"tree", "pr", "base"})
        assert not rule.needs_forge and rule.after == () and rule.tools == ()
    assert set(latex.CITATIONS.codes) == {"E_VALIDATION_CITATION"}
    assert set(latex.LABELS_REFS.codes) == {"E_VALIDATION_LABEL"}
    assert set(latex.LATEX_MACROS.codes) == {"E_VALIDATION_MACRO"}
    # D-33 (g): convention solo trae los conv.latex/main/outline/bib que no duplican las tres reglas.
    conv = set(latex.CONVENTION.codes)
    assert all(re.match(r"^conv\.(latex|main|outline|bib)\.", c) for c in conv)
    assert not conv & {"conv.latex.definition", "conv.latex.label", "conv.bib.undefined-key"}
    assert conv == {
        "conv.main.grammar",
        "conv.main.sections",
        "conv.outline.grammar",
        "conv.outline.parts",
        "conv.outline.missing",
        "conv.latex.structure",
        "conv.latex.forbidden-command",
        "conv.latex.hidden-block",
        "conv.latex.path-outside-section",
        "conv.latex.sentence-per-line",
        "conv.latex.shell-package",
        "conv.bib.key-syntax",
        "conv.bib.forbidden-field",
        "conv.bib.pending-consolidated",
        "conv.bib.pending-conflict",
    }


def test_convention_codes_are_disjoint_from_p16() -> None:
    try:
        from aucove.ops.rules import convention as convention_rules
    except ImportError:  # pragma: no cover - sin la regla convention
        pytest.skip("ops.rules.convention no está disponible")
    other = {code for rule in convention_rules.RULES for code in rule.codes}
    assert other and not other & set(latex.CONVENTION.codes)
    assert all(r.id == "convention" for r in convention_rules.RULES)


@pytest.mark.parametrize("lang", LANGS)
def test_catalog_has_every_finding_code(lang: str) -> None:
    catalog = load_catalog(lang)  # type: ignore[arg-type]
    for rule in (*latex.RULES, *latex_log.RULES):
        for code, params in rule.codes.items():
            message = catalog[f"finding.{code}.message"]
            hint = catalog.get(f"finding.{code}.hint")
            assert hint is not None, (lang, code)
            assert placeholders(message) | placeholders(hint) == params, (lang, code)
            assert not params & {"rule", "inp", "code", "severity", "path", "line", "pointer"}


DYNAMIC_KEYS = {
    "latex.citation.bib_syntax",
    "latex.citation.bib_missing_key",
    "latex.citation.bib_unterminated",
    "latex.reason.no_log",
    "latex.reason.log_unreadable",
}


def used_keys() -> set[str]:
    src = REPO_ROOT / "src" / "aucove" / "ops" / "rules"
    found: set[str] = set()
    for name in ("latex.py", "latex_log.py"):
        text = (src / name).read_text(encoding="utf-8")
        found |= set(re.findall(r"[\"'](latex\.[a-z_]+\.[a-z_]+)[\"']", text))
    return found | DYNAMIC_KEYS


@pytest.mark.parametrize("lang", LANGS)
def test_catalog_latex_keys_match_the_code(lang: str) -> None:
    catalog = load_catalog(lang)  # type: ignore[arg-type]
    defined = {k for k in catalog if k.startswith("latex.")}
    assert used_keys() == defined


# -- plantilla y fixture válido ------------------------------------------------------------


@pytest.fixture(scope="module")
def template() -> Mapping[str, Mapping[str, bytes]]:
    tool = load_tool("render_template")
    return {lang: tool.render_files(lang) for lang in LANGS}


@pytest.mark.parametrize("lang", LANGS)
def test_h2_template_passes_without_findings(template: Mapping[str, Mapping[str, bytes]], lang: str) -> None:
    tree = DictTree(template[lang])
    for rule in ALL_RULES:
        findings = run(rule, tree, t=translator(lang))  # type: ignore[arg-type]
        assert findings == [], (rule.id, summary(findings))


def test_fixture_tree_passes_without_findings() -> None:
    tree = tree_with()
    for rule in ALL_RULES:
        assert run(rule, tree) == [], rule.id


def test_rules_execute_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """REQ-CI-24: el árbol se lee como datos; ninguna regla lanza procesos."""

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("una regla intentó ejecutar algo")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr("os.system", boom)
    tree = tree_with({INTRO: text_of(INTRO) + "\\write18{rm -rf /}\n\\directlua{os.execute('x')}\n"})
    for rule in ALL_RULES:
        run(rule, tree)


def test_worktree_integration(git_repo: GitRepo) -> None:
    files = {k: v for k, v in fixture_files().items()}
    git_repo.write(files)
    tree = worktree(git_repo.path)
    for rule in ALL_RULES:
        assert run(rule, tree) == [], rule.id  # type: ignore[arg-type]
    (git_repo.path / "sections" / "methods" / "big.tex").write_bytes(b"x" * (5 * 2**20 + 1))
    findings = run(latex.CITATIONS, worktree(git_repo.path))  # type: ignore[arg-type]
    assert summary(findings) == [(UNAVAILABLE_CODE, "error", "sections/methods/big.tex", None)]


# -- límites: 5 MB, enlaces, tiempo acotado y árbol ausente --------------------------------


@pytest.mark.parametrize("rule", ALL_RULES, ids=lambda r: r.id)
def test_no_tree_is_unavailable(rule: RuleSpec) -> None:
    findings = run(rule, None)
    assert codes(findings) == [UNAVAILABLE_CODE]
    assert status_of(findings) == "fail"


@pytest.mark.parametrize("rule", ALL_RULES[:3], ids=lambda r: r.id)
def test_file_over_5_mb_fails_closed(rule: RuleSpec) -> None:
    big = "% " + "x" * (5 * 2**20) + "\n"
    findings = run(rule, tree_with({RESULTS: big}))
    assert (UNAVAILABLE_CODE, "error", RESULTS, None) in summary(findings)


def test_invalid_utf8_and_symlinked_bib_fail_closed() -> None:
    tree = tree_with({INTRO_BIB: b"\xff\xfe@misc{x}"})
    findings = run(latex.CITATIONS, tree)
    assert (UNAVAILABLE_CODE, "error", INTRO_BIB, None) in summary(findings)
    # Una entrada 120000 no se lee (REQ-CI-24).
    tree = tree_with(modes={METHODS_BIB: "120000"})
    findings = run(latex.CITATIONS, tree)
    assert (UNAVAILABLE_CODE, "error", METHODS_BIB, None) in summary(findings)
    assert METHODS_BIB not in tree.reads
    # Un .tex enlazado no se analiza.
    tree = tree_with(modes={RESULTS: "120000"})
    assert run(latex.LATEX_MACROS, tree) == []
    assert RESULTS not in tree.reads


@pytest.mark.parametrize("rule", ALL_RULES, ids=lambda r: r.id)
def test_deadline_gives_unavailable(rule: RuleSpec, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(latex, "RULE_TIME_BUDGET_S", -1.0)  # plazo ya vencido
    findings = run(rule, tree_with())
    assert codes(findings) == [UNAVAILABLE_CODE]
    assert "time" in findings[0].message or findings[0].message.startswith("finding.")


def test_pathological_input_is_fast() -> None:
    nasty = "\\cite{" * 100_000 + "{" * 100_000 + "\\verb" * 50_000 + "$" * 50_000 + "^^" * 50_000 + "\n"
    tree = tree_with({METHODS: nasty, INTRO_BIB: "@misc{k, t = {" + "{" * 200_000})
    start = time.monotonic()
    for rule in ALL_RULES:
        run(rule, tree)
    assert time.monotonic() - start < 30


def test_unlisted_and_local_tex_files_are_ignored() -> None:
    tree = tree_with(
        {
            "tex/build/main.tex": "\\def\\x{1}\n",
            "tex/build-diff/diff.tex": "\\newcommand{\\y}{2}\n",
            "notes/scratch.tex": "\\def\\z{3}\n",
        }
    )
    assert run(latex.LATEX_MACROS, tree) == []


# -- escáner (domain.latex) -----------------------------------------------------------------


def test_scan_blanks_comments_and_verbatim_preserving_layout() -> None:
    text = (
        "a % \\def\\x\n"
        "b \\% \\newcommand\n"
        "\\begin{verbatim}\n\\def\\y{1}\n\\end{verbatim}\n"
        "\\begin{ lstlisting }\n\\catcode\n\\end{lstlisting}\n"
        "\\verb|\\def| \\verb*+\\let+ \\lstinline[language=TeX]{\\edef} \\lstinline!\\gdef!\n"
        "\\verb \\gdef\n"
    )
    sc = tex.scan(text)
    assert len(sc.text) == len(text) and sc.text.count("\n") == text.count("\n")
    names = [c.name for c in sc.commands]
    assert names == ["newcommand", "begin", "end", "begin", "end", "verb", "verb", "lstinline", "lstinline", "verb", "gdef"]
    assert [c.line for c in sc.commands if c.name == "newcommand"] == [2]
    assert sc.line_of(0) == 1 and sc.line_of(len(text) - 1) == text.count("\n")


def test_scan_unclosed_verbatim_runs_to_the_end() -> None:
    sc = tex.scan("\\begin{Verbatim*}\n\\def\\x{1}\n")
    assert [c.name for c in sc.commands] == ["begin"]


def test_scan_full_names_and_catcode_regimes() -> None:
    sc = tex.scan("\\definition \\def \\@foo \\makeatletter\\@bar\\makeatother \\ExplSyntaxOn\\cs_new:Npn\\ExplSyntaxOff\\")
    names = [c.name for c in sc.commands]
    assert names == ["definition", "def", "makeatletter", "@bar", "makeatother", "ExplSyntaxOn", "cs_new:Npn", "ExplSyntaxOff"]


def test_scan_carets_outside_comments_and_verbatim() -> None:
    sc = tex.scan("a ^^41 ^^42\n% ^^43\n\\verb|^^44|\nb^^5c\n")
    assert sc.carets == (1, 4)
    assert tex.scan("^^").carets == (1,)


def test_read_args_forms() -> None:
    sc = tex.scan("\\cmd*[o1][o2]{g1}\n{g2}\n\n{g3}")
    a = sc.args(sc.commands[0], groups=3)
    assert (a.star, a.options, a.groups, a.braced, a.complete) == (True, ("o1", "o2"), ("g1", "g2"), True, True)
    # Una línea en blanco (\par) corta los argumentos.
    sc = tex.scan("\\cites(pre)(post)[a][b]{k1}[c]{k2} {k3}")
    assert sc.args(sc.commands[0], multi=True).groups == ("k1", "k2", "k3")
    sc = tex.scan("\\x[{a]b}]{c\\}d}")
    assert sc.args(sc.commands[0]).options == ("{a]b}",) and sc.args(sc.commands[0]).groups == ("c\\}d",)
    sc = tex.scan("\\x{never closed")
    a = sc.args(sc.commands[0])
    assert a.groups == () and not a.complete
    sc = tex.scan("\\x[never closed")
    assert not sc.args(sc.commands[0]).complete
    sc = tex.scan("\\cites(never")
    assert not sc.args(sc.commands[0], multi=True).complete
    sc = tex.scan("\\x{" + "a" * (tex.MAX_ARG_CHARS + 10) + "}")
    assert not sc.args(sc.commands[0]).complete
    sc = tex.scan("\\x a")
    assert sc.args(sc.commands[0]).braced is False


def test_environments_and_labels() -> None:
    sc = tex.scan("\\begin{figure}\\end{figure}\\begin")
    assert [e for e, _c in sc.environments()] == ["+figure", "-figure"]
    assert tex.parse_label("fig:methods:sample-sizes") == tex.Label("fig", "methods", "sample-sizes")
    assert tex.parse_label("sec:methods") == tex.Label("sec", "methods", None)
    for bad in ("fig:Methods", "figure:methods", "sec:", "sec:a--b", "fig:a:b:c", "sec:" + "a" * 300):
        assert tex.parse_label(bad) is None, bad
    assert tex.split_keys(" a, ,b ,") == ["a", "b"]
    assert tex.blank("ab\ncd") == "  \n  "


def test_prose_lines_skip_math_tables_and_masks_inline_math() -> None:
    text = (
        "Prose one.\n"
        "\\begin{equation}\nA. B\n\\end{equation}\n"
        "Inline $a. B$ and \\(c. D\\) end.\n"
        "Display \\[x. Y\n z. W\\] after.\n"
        "Double $$p. Q$$ done.\n"
        "\\begin{align}\nopen. Never\n"
    )
    lines = dict(tex.prose_lines(tex.scan(text)))
    assert lines[1] == "Prose one."
    assert not {2, 3, 4, 9, 10} & set(lines)
    assert tex.sentence_breaks(lines[5]) == []
    assert tex.sentence_breaks(lines[6]) == [] and tex.sentence_breaks(lines[7]) == []
    assert tex.sentence_breaks(lines[8]) == []


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("One. Two.", [3]),
        ("Uno. ¿Dos?", [3]),
        ("Uno! \\textbf{Dos}", [3]),
        ("Is it? Yes.", [5]),
        ("See e.g. Smith and et al. Jones, cf. Fig. Two.", []),
        ("Version 3.5 is out. no capital after.", []),
        ("Ends here.", []),
        ("Trailing. ", []),
        ("Ab. Cd", [2]),
        ("Dr. No. Who", []),
    ],
)
def test_sentence_breaks(line: str, expected: list[int]) -> None:
    assert tex.sentence_breaks(line) == expected


def test_deadline() -> None:
    now = [0.0]
    dl = tex.Deadline(1.0, clock=lambda: now[0])
    dl.check()
    assert not dl.expired
    now[0] = 2.0
    assert dl.expired
    with pytest.raises(tex.ScanTimeout):
        dl.check()
    never = tex.Deadline(None)
    never.check()
    assert not never.expired
    ticking = tex.Deadline(1.0, clock=lambda: now[0])
    now[0] = 5.0
    for _ in range(2047):
        ticking.tick()  # solo consulta el reloj cada 2048 pasos
    with pytest.raises(tex.ScanTimeout):
        ticking.tick()
    with pytest.raises(tex.ScanTimeout):
        tex.scan("\\a " * 10_000, deadline=tex.Deadline(-1.0))


# -- citations ------------------------------------------------------------------------------


MAIN_TEXT = text_of("tex/main.tex")


def main_with(old: str, new: str) -> str:
    assert old in MAIN_TEXT
    return MAIN_TEXT.replace(old, new)


@pytest.mark.parametrize(
    "line",
    [
        "\\addbibresource{bib/references.bib}",
        "\\addbibresource[label=x]{../bib/references.bib}",
        "\\addbibresource{ ../bib/references.bib}",
        "\\addbibresource{../sections/Bad_Id/pending.bib}",
        "\\addbibresource{../sections/methods/other.bib}",
        # D-43: la forma anterior, relativa a tex/, y las que salen de la parte ya no valen.
        "\\addbibresource{sections/methods/pending.bib}",
        "\\addbibresource{../../sections/methods/pending.bib}",
        "\\addbibresource{../sections/methods/../results/pending.bib}",
        "\\addbibresource*{../bib/references.bib}",
        "\\bibliography{references}",
        "\\addglobalbib{x.bib}",
    ],
)
def test_only_the_two_exact_addbibresource_forms(line: str) -> None:
    tree = tree_with({"tex/main.tex": main_with("\\addbibresource{../bib/references.bib}", line)})
    findings = run(latex.CITATIONS, tree)
    assert summary(findings) == [("E_VALIDATION_CITATION", "error", "tex/main.tex", 5)]
    assert "accepted form" in findings[0].message


def test_addbibresource_outside_main() -> None:
    tree = tree_with({"tex/preamble.tex": text_of("tex/preamble.tex") + "\\addbibresource{../bib/references.bib}\n"})
    findings = run(latex.CITATIONS, tree)
    assert summary(findings) == [("E_VALIDATION_CITATION", "error", "tex/preamble.tex", 18)]
    # En una sección lo informa conv.latex.structure, no citations.
    tree = tree_with({RESULTS: text_of(RESULTS) + "\\addbibresource{x.bib}\n"})
    assert run(latex.CITATIONS, tree) == []
    assert ("conv.latex.structure", "error", RESULTS, 10) in summary(run(latex.CONVENTION, tree))


def test_cite_with_missing_key_is_an_error(t_es: Translator) -> None:
    tree = tree_with({RESULTS: text_of(RESULTS) + "As shown before \\parencite[12]{knuthTeXbook1984, nowhere2020}.\n"})
    findings = run(latex.CITATIONS, tree, t=t_es)
    assert summary(findings) == [("E_VALIDATION_CITATION", "error", RESULTS, 10)]
    assert "nowhere2020" in findings[0].message
    assert "sections/results/pending.bib" in findings[0].message
    assert findings[0].hint


def test_key_from_another_sections_pending_is_an_error() -> None:
    # doeSurvey2021 está en el pending.bib de introduction, no en el de results.
    tree = tree_with({RESULTS: text_of(RESULTS) + "\\textcite{doeSurvey2021}\n"})
    assert summary(run(latex.CITATIONS, tree)) == [("E_VALIDATION_CITATION", "error", RESULTS, 10)]
    # La misma clave en el pending.bib propio es válida.
    bib = "@article{doeSurvey2021, title = {A Survey of Everything}}\n"
    tree = tree_with({RESULTS: text_of(RESULTS) + "\\textcite{doeSurvey2021}\n", "sections/results/pending.bib": bib})
    assert run(latex.CITATIONS, tree) == []


@pytest.mark.parametrize(
    "cite",
    [
        "\\cites[see][1]{knuthTeXbook1984}[2]{missingA}",
        "\\volcite{3}{missingA}",
        "\\footcite{missingA}",
        "\\citeauthor*{missingA}",
        "\\nocite{missingA}",
        "\\Textcites(pre)(post){missingA}",
    ],
)
def test_cite_variants(cite: str) -> None:
    tree = tree_with({RESULTS: text_of(RESULTS) + cite + "\n"})
    findings = run(latex.CITATIONS, tree)
    assert summary(findings) == [("E_VALIDATION_CITATION", "error", RESULTS, 10)], cite
    assert "missingA" in findings[0].message


def test_cites_in_comments_verbatim_and_nocite_star_are_ignored() -> None:
    extra = "% \\cite{missingA}\n\\begin{verbatim}\\cite{missingB}\\end{verbatim}\n\\verb|\\cite{missingC}|\n\\nocite{*}\n"
    assert run(latex.CITATIONS, tree_with({RESULTS: text_of(RESULTS) + extra})) == []


def test_lead_files_may_cite_any_pending_key() -> None:
    # tex/appendix.tex cita doeSurvey2021 (pending de introduction): válido.
    assert run(latex.CITATIONS, tree_with()) == []
    tree = tree_with({"tex/appendix.tex": text_of("tex/appendix.tex") + "\\cite{nowhere}\n"})
    findings = run(latex.CITATIONS, tree)
    assert summary(findings) == [("E_VALIDATION_CITATION", "error", "tex/appendix.tex", 5)]
    assert "any `pending.bib`" in findings[0].message


def test_bib_syntax_errors_and_missing_references() -> None:
    tree = tree_with({INTRO_BIB: text_of(INTRO_BIB) + "\n@misc{,\n  note = {x}\n}\n"})
    findings = run(latex.CITATIONS, tree)
    assert summary(findings) == [("E_VALIDATION_CITATION", "error", INTRO_BIB, 11)]
    assert "without a key" in findings[0].message
    tree = tree_with({"bib/references.bib": None})
    findings = run(latex.CITATIONS, tree)
    # Sin references.bib, solo quedan las claves de los pending.bib: falla la introducción.
    assert {(f.code, f.path) for f in findings} == {("E_VALIDATION_CITATION", INTRO)}
    assert len(findings) == 3


# -- labels-refs ----------------------------------------------------------------------------


def test_duplicate_label_is_an_error_in_both_places() -> None:
    # sec:methods la define el archivo de enlace de su sección (02 §4.1.1).
    tree = tree_with({RESULTS: text_of(RESULTS) + "\\label{sec:methods}\n"})
    findings = run(latex.LABELS_REFS, tree)
    assert summary(findings) == [
        ("E_VALIDATION_LABEL", "error", RESULTS, 10),  # sec:methods es del espacio de otra sección
        ("E_VALIDATION_LABEL", "error", RESULTS, 10),
        ("E_VALIDATION_LABEL", "error", OUTLINE_METHODS, 4),
    ]
    assert "uses part 'methods'" in findings[0].message
    assert f"also defined in {OUTLINE_METHODS}, line 4" in findings[1].message
    assert f"also defined in {RESULTS}, line 10" in findings[2].message
    assert status_of(findings) == "fail"
    # Repetida dentro de la misma sección y con su propio prefijo: solo el duplicado.
    tree = tree_with({RESULTS: text_of(RESULTS) + "\\label{sec:results}\n"})
    assert [(f.path, f.line) for f in run(latex.LABELS_REFS, tree)] == [(RESULTS, 10), ("tex/outline/results.tex", 3)]


def test_ref_without_target_is_a_warning() -> None:
    extra = "See \\ref{fig:results:none}, \\cref{sec:methods,tab:x:y} and \\crefrange{eq:a}{eq:b}.\n\\hyperref[sec:gone]{here}\n"
    findings = run(latex.LABELS_REFS, tree_with({RESULTS: text_of(RESULTS) + extra}))
    assert {f.severity for f in findings} == {"warning"}
    assert [f.line for f in findings] == [10, 10, 10, 10, 11]
    assert not has_errors(findings) and status_of(findings) == "pass"
    assert "fig:results:none" in findings[0].message


@pytest.mark.parametrize(
    ("label", "needle"),
    [("Results", "is not of the form"), ("fig:methods:x", "uses part 'methods'"), ("sec:results:Bad", "is not of the form")],
)
def test_section_labels_follow_the_grammar(label: str, needle: str) -> None:
    findings = run(latex.LABELS_REFS, tree_with({RESULTS: text_of(RESULTS) + f"\\label{{{label}}}\n"}))
    assert summary(findings) == [("E_VALIDATION_LABEL", "error", RESULTS, 10)]
    assert needle in findings[0].message


def test_lead_files_do_not_define_labels() -> None:
    tree = tree_with({"tex/appendix.tex": text_of("tex/appendix.tex") + "\\label{sec:appendix}\n"})
    findings = run(latex.LABELS_REFS, tree)
    assert summary(findings) == [("E_VALIDATION_LABEL", "error", "tex/appendix.tex", 5)]
    assert "cannot define label 'sec:appendix'" in findings[0].message


def test_outline_labels_use_the_outline_id() -> None:
    """REQ-CONV-92: un archivo de enlace define etiquetas con el <id> de su sección del artículo."""
    own = "\\begin{figure}\\caption{Overview.}\\label{fig:methods:overview}\\end{figure}\n"
    assert run(latex.LABELS_REFS, tree_with({OUTLINE_METHODS: text_of(OUTLINE_METHODS) + own})) == []
    for label, needle in (("fig:results:x", "outline file of 'methods'"), ("Overview", "is not of the form")):
        tree = tree_with({OUTLINE_METHODS: text_of(OUTLINE_METHODS) + f"\\label{{{label}}}\n"})
        findings = run(latex.LABELS_REFS, tree)
        assert summary(findings) == [("E_VALIDATION_LABEL", "error", OUTLINE_METHODS, 10)], label
        assert needle in findings[0].message


# -- latex-macros ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "line"),
    [
        (RESULTS, "\\newcommand{\\x}{1}"),
        (RESULTS, "\\NewDocumentCommand{\\x}{m}{#1}"),
        (RESULTS, "\\makeatletter"),
        (RESULTS, "\\catcode`\\@=11"),
        ("tex/main.tex", "\\def\\x{1}"),
        ("tex/preamble.tex", "\\newcommand{\\x}{1}"),
        ("tex/appendix.tex", "\\renewenvironment{x}{}{}"),
    ],
)
def test_definitions_outside_notation_are_errors(path: str, line: str) -> None:
    body = text_of(path)
    if path == "tex/main.tex":
        body = body.replace("\\begin{document}\n", "\\begin{document}\n" + line + "\n")
    else:
        body += line + "\n"
    findings = run(latex.LATEX_MACROS, tree_with({path: body}))
    assert [(f.code, f.severity, f.path) for f in findings] == [("E_VALIDATION_MACRO", "error", path)]
    name = re.match(r"\\([A-Za-z]+)", line)
    assert name is not None and f"\\{name.group(1)}" in findings[0].message


def test_macro_exceptions_and_scanner_rules() -> None:
    # \makeatletter/\makeatother en preamble.tex (fixture), definiciones en notation.tex,
    # \definition (nombre más largo), comentarios y verbatim: nada que informar.
    extra = "\\definition{x}\n% \\def\\y{1}\n\\begin{verbatim}\n\\newcommand{\\z}{1}\n\\end{verbatim}\n"
    assert run(latex.LATEX_MACROS, tree_with({RESULTS: text_of(RESULTS) + extra})) == []
    assert latex.is_definition("ProvideDocumentEnvironment") and not latex.is_definition("define")


# -- convention: tex/main.tex ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "needle", "line"),
    [
        ("\\maketitle", "\\maketitle\nHello world.", "not part of the grammar", 11),
        # D-56: main.tex incluye los archivos de enlace, y estos, las partes.
        ("\\input{outline/results}", "\\input{../sections/results/results}", "includes a part", 14),
        ("\\input{outline/results}", "\\input{../sections/x/../results/results}", "includes a part", 14),
        ("\\input{outline/results}", "\\input{outline/Results}", "does not point to", 14),
        ("\\input{outline/results}", "\\input{outline/sub/results}", "does not point to", 14),
        ("\\input{outline/results}", "\\input{tex/outline/results}", "does not point to", 14),
        # D-43: la forma anterior (sections/ dentro de tex/) ya no es una parte.
        ("\\input{outline/results}", "\\input{sections/results/results}", "does not point to", 14),
        ("\\input{appendix}", "\\input{missing}", "does not point to", 16),
        ("\\input{notation}  % las definiciones viven aquí", "\\input{notation}\n\\input{preamble}", "more than once", 5),
        ("\\input{preamble}\n\\input{notation}", "\\input{notation}\n\\input{preamble}", "out of order", 4),
        ("\\documentclass[11pt,a4paper]{article}", "\\documentclass[11pt]{article}\n\\documentclass{book}", "more than once", 3),
        ("\\begin{document}", "\\begin{document}\n\\usepackage{x}", "not part of the grammar", 10),
    ],
)
def test_main_grammar(old: str, new: str, needle: str, line: int) -> None:
    findings = run(latex.CONVENTION, tree_with({"tex/main.tex": main_with(old, new)}))
    grammar = [f for f in findings if f.code == "conv.main.grammar"]
    assert [(f.severity, f.path, f.line) for f in grammar] == [("error", "tex/main.tex", line)], summary(findings)
    assert needle in grammar[0].message


def test_main_missing_lines() -> None:
    text = main_with("\\end{document}\n", "").replace("\\addbibresource{../bib/references.bib}\n", "")
    findings = run(latex.CONVENTION, tree_with({"tex/main.tex": text}))
    grammar = [f.message for f in findings if f.code == "conv.main.grammar"]
    assert len(grammar) == 2
    assert any("\\end{document}" in m and "is missing" in m for m in grammar)
    assert any("\\addbibresource{../bib/references.bib}" in m for m in grammar)


SECTION_CODES = ("conv.main.sections", "conv.outline.")


def sections_findings(main: str, **kw: object) -> list[Finding]:
    """Hallazgos de las partes en ``main.tex`` y ``tex/outline/``: error lo que sobra, aviso lo que falta."""
    return [f for f in run(latex.CONVENTION, tree_with({"tex/main.tex": main}), **kw) if f.code.startswith(SECTION_CODES)]  # type: ignore[arg-type]


def test_main_sections_must_match_roles() -> None:
    """REQ-CONV-18: un pending.bib por parte; lo que sobra es error y lo que falta, aviso (D-56)."""
    missing = sections_findings(main_with("\\addbibresource{../sections/results/pending.bib}\n", ""))
    assert [(f.code, f.severity, f.line) for f in missing] == [("conv.outline.missing", "warning", None)]
    assert "\\addbibresource{../sections/results/pending.bib}" in missing[0].message
    assert missing[0].hint is not None and "Add" in missing[0].hint
    assert status_of(missing) == "pass"
    bib = "\\addbibresource{../sections/results/pending.bib}"
    extra = sections_findings(main_with(bib, bib + "\n\\addbibresource{../sections/extra/pending.bib}"))
    assert [(f.code, f.line) for f in extra] == [("conv.main.sections", 9)] and "'extra'" in extra[0].message
    dup = sections_findings(main_with(bib, bib + "\n" + bib))
    assert [(f.code, f.line) for f in dup] == [("conv.main.sections", 9)] and "repeated" in dup[0].message


def test_main_outline_lines_match_the_outline_files() -> None:
    """REQ-CONV-18: un ``\\input{outline/<id>}`` por cada archivo de ``tex/outline/`` (02 §4.1.1)."""
    line = "\\input{outline/results}"
    unknown = sections_findings(main_with(line, line + "\n\\input{outline/appendix}"))
    assert [(f.code, f.severity, f.line) for f in unknown] == [("conv.main.sections", "error", 15)]
    assert "`tex/outline/appendix.tex`, which does not exist" in unknown[0].message
    dup = sections_findings(main_with(line, line + "\n" + line))
    assert [(f.code, f.line) for f in dup] == [("conv.main.sections", 15)] and "repeated" in dup[0].message
    missing = sections_findings(main_with(line + "\n", ""))
    assert [(f.code, f.severity, f.line) for f in missing] == [("conv.outline.missing", "warning", None)]
    assert "\\input{outline/results}" in missing[0].message and "tex/outline/results.tex" in missing[0].message


def test_main_references_come_first() -> None:
    before = main_with(
        "\\addbibresource{../bib/references.bib}\n\\addbibresource{../sections/introduction/pending.bib}",
        "\\addbibresource{../sections/introduction/pending.bib}\n\\addbibresource{../bib/references.bib}",
    )
    found = sections_findings(before)
    assert [f.line for f in found] == [5] and "comes before" in found[0].message
    # Desde D-56 el orden de las partes lo dan los archivos de enlace: el de los pending.bib es libre.
    swapped = main_with(
        "\\addbibresource{../sections/methods/pending.bib}\n\\addbibresource{../sections/results/pending.bib}",
        "\\addbibresource{../sections/results/pending.bib}\n\\addbibresource{../sections/methods/pending.bib}",
    )
    assert sections_findings(swapped) == []


def project_yaml(engine: str = "pdflatex") -> bytes:
    rendered = load_tool("render_template").render_files("en")["project.yaml"]
    assert b"engine: pdflatex" in rendered
    out = rendered.replace(b"engine: pdflatex", f"engine: {engine}".encode())
    cfg, _ = parse_project(out)
    assert cfg is not None and cfg.build.engine == engine
    return out


def snapshot(roles_text: str) -> BaseSnapshot:
    tool = load_tool("render_template")
    cfg, _ = parse_project(tool.render_files("en")["project.yaml"])
    roles, _ = parse_roles(roles_text)
    assert cfg is not None and roles is not None
    return BaseSnapshot(ref="origin/main", sha=None, project=cfg, roles=roles, decisions=frozenset(), fallback_sha=None, source="local")


def test_sections_come_from_tree_roles_or_policy() -> None:
    main = main_with("\\addbibresource{../sections/results/pending.bib}\n", "")
    # Sin roles.yaml en el árbol ni política: no se comparan las partes.
    tree = tree_with({"tex/main.tex": main, "roles.yaml": None})
    assert [f for f in run(latex.CONVENTION, tree) if f.code.startswith(SECTION_CODES)] == []
    # roles.yaml inválido en el árbol: rige la base (03 §2.5).
    no_results = roles_fixture("basic").split("  - id: results")[0]
    main = main.replace("\\input{outline/results}\n", "")
    tree = tree_with({"tex/main.tex": main, "roles.yaml": "version: [\n", OUTLINE_RESULTS: None})
    assert [f for f in run(latex.CONVENTION, tree, policy=snapshot(no_results)) if f.code.startswith(SECTION_CODES)] == []
    found = [f for f in run(latex.CONVENTION, tree, policy=snapshot(roles_fixture("basic"))) if f.code.startswith(SECTION_CODES)]
    assert [(f.code, f.path) for f in found] == [("conv.outline.missing", "tex/main.tex"), ("conv.outline.missing", "tex/outline")]
    assert all("results" in f.message for f in found)


# -- convention: tex/outline/ (archivos de enlace, D-56) --------------------------------------

OUTLINE_TAIL = "\\input{../sections/results/results}\n"


@pytest.mark.parametrize(
    "head",
    [
        "\\section{Results}\n\\label{sec:results}\n",
        "\\section*{Results}\n\\label{sec:results}\n",
        "\n% comentario\n\\section{Results with \\emph{emphasis}} % y otro\n\n\\label{sec:results}\n\n",
    ],
)
def test_outline_head_valid(head: str) -> None:
    tree = tree_with({OUTLINE_RESULTS: head + OUTLINE_TAIL})
    for rule in ALL_RULES:
        assert run(rule, tree) == [], rule.id


@pytest.mark.parametrize(
    ("head", "lines"),
    [
        ("\\section[Short]{Results}\n\\label{sec:results}\n", [1]),
        ("\\section{Results}\n\\label{sec:result}\n", [1]),
        ("\\section{Results}\\label{sec:results}\n", [1]),
        ("\\section{Results\n}\n\\label{sec:results}\n", [1]),
        ("\\subsection{Results}\n\\label{sec:results}\n", [1]),
        ("", [1]),
        # El \section que no está en la primera línea es, además, otro comando de sección.
        ("\\label{sec:results}\n\\section{Results}\n", [1, 2]),
        ("Results come first.\n\\section{Results}\n\\label{sec:results}\n", [1, 2]),
    ],
)
def test_outline_head_errors(head: str, lines: list[int]) -> None:
    """REQ-CONV-92: ``\\section{...}`` y, en la línea siguiente, ``\\label{sec:<id>}``."""
    findings = [f for f in run(latex.CONVENTION, tree_with({OUTLINE_RESULTS: head + OUTLINE_TAIL})) if f.code.startswith("conv.")]
    assert [(f.code, f.severity, f.path, f.line) for f in findings] == [
        ("conv.outline.grammar", "error", OUTLINE_RESULTS, n) for n in lines
    ], summary(findings)
    assert "must start with" in findings[0].message and "\\label{sec:results}" in findings[0].message


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        ("\\section{Another}", "the only sectioning command"),
        ("\\chapter{Another}", "the only sectioning command"),
        ("\\part*{Another}", "the only sectioning command"),
        ("\\input{../sections/results/other}", "is not an `\\input{../sections/<id>/<id>}` line"),
        ("\\input{../sections/methods/tables/descriptives}", "is not an"),
        ("\\input{appendix}", "is not an"),
        ("\\input ../sections/results/results", "is not an"),
        ("See \\input{../sections/methods/methods}", "is not an"),
        ("\\input{../sections/methods/methods} \\input{../sections/introduction/introduction}", "is not an"),
    ],
)
def test_outline_grammar_after_the_head(extra: str, needle: str) -> None:
    findings = run(latex.CONVENTION, tree_with({OUTLINE_RESULTS: text_of(OUTLINE_RESULTS) + extra + "\n"}))
    assert [(f.code, f.line) for f in findings] == [("conv.outline.grammar", 5)], summary(findings)
    assert needle in findings[0].message


def test_outline_parts_match_roles() -> None:
    """REQ-CONV-93: cada parte declarada se incluye una sola vez en todo ``tex/outline/``."""
    # Una sección del artículo con dos partes, y la entrada y la transición del editor (D-55).
    two = (
        "\\section{Methods and results}\n\\label{sec:methods}\n\n"
        "This section presents the model and its results.\n\n"
        "\\input{../sections/methods/methods}\n\n"
        "With the model fitted, the results follow.\n\n"
        "\\input{../sections/results/results}\n"
    )
    tree = tree_with({"tex/main.tex": main_with("\\input{outline/results}\n", ""), OUTLINE_METHODS: two, OUTLINE_RESULTS: None})
    for rule in ALL_RULES:
        assert run(rule, tree) == [], rule.id
    # Lo que sobra es error; lo que falta, aviso.
    twice = text_of(OUTLINE_METHODS) + "\\input{../sections/results/results}\n"
    dup = [f for f in run(latex.CONVENTION, tree_with({OUTLINE_METHODS: twice})) if f.code.startswith("conv.")]
    assert [(f.code, f.severity, f.path, f.line) for f in dup] == [("conv.outline.parts", "error", OUTLINE_RESULTS, 4)]
    assert f"already included in {OUTLINE_METHODS}, line 10" in dup[0].message
    other = text_of(OUTLINE_RESULTS) + "\\input{../sections/appendix/appendix}\n"
    undeclared = run(latex.CONVENTION, tree_with({OUTLINE_RESULTS: other}))
    assert [(f.code, f.line) for f in undeclared] == [("conv.outline.parts", 5)] and "'appendix'" in undeclared[0].message
    # Un archivo de enlace sin partes es válido; la parte que nadie incluye da el aviso.
    alone = text_of(OUTLINE_RESULTS).replace(OUTLINE_TAIL, "")
    missing = run(latex.CONVENTION, tree_with({OUTLINE_RESULTS: alone}))
    assert [(f.code, f.severity, f.path, f.line) for f in missing] == [("conv.outline.missing", "warning", "tex/outline", None)]
    assert "'results'" in missing[0].message
    assert missing[0].hint is not None and "\\input{../sections/results/results}" in missing[0].hint
    assert status_of(missing) == "pass"


@pytest.mark.parametrize(
    ("snippet", "code", "needle"),
    [
        ("\\immediate\\write18{ls}", "conv.latex.forbidden-command", "\\write18"),
        ("\\directlua{tex.print(1)}", "conv.latex.forbidden-command", "\\directlua"),
        ("A ^^5cdef sequence", "conv.latex.forbidden-command", "^^"),
        ("\\iffalse hidden \\fi", "conv.latex.hidden-block", "outline file"),
        ("\\usepackage{tikz}", "conv.latex.structure", "outline files"),
        ("\\includegraphics{../sections/results/figures/a.pdf}", "conv.latex.path-outside-section", "only includes parts"),
        ("\\lstinputlisting{x.py}", "conv.latex.path-outside-section", "only includes parts"),
        ("\\includesvg{x}", "conv.latex.path-outside-section", "only includes parts"),
        ("First sentence. Second sentence.", "conv.latex.sentence-per-line", "Second sentence"),
    ],
)
def test_outline_prose_follows_the_part_rules(snippet: str, code: str, needle: str) -> None:
    """REQ-CONV-92: a la prosa de un archivo de enlace le valen §4.3 a §4.6, como a una parte."""
    findings = run(latex.CONVENTION, tree_with({OUTLINE_RESULTS: text_of(OUTLINE_RESULTS) + snippet + "\n"}))
    assert {(f.code, f.path, f.line) for f in findings} == {(code, OUTLINE_RESULTS, 5)}, summary(findings)
    assert any(needle in f.message for f in findings), [f.message for f in findings]


def test_outline_citations_and_definitions() -> None:
    # Como los demás archivos del editor, cita cualquier clave de references.bib o de un pending.bib.
    ok = text_of(OUTLINE_RESULTS) + "As surveyed by \\textcite{doeSurvey2021}.\n"
    assert run(latex.CITATIONS, tree_with({OUTLINE_RESULTS: ok})) == []
    bad = text_of(OUTLINE_RESULTS) + "\\cite{nowhere}\n\\addbibresource{x.bib}\n\\newcommand{\\x}{1}\n"
    tree = tree_with({OUTLINE_RESULTS: bad})
    findings = run(latex.CITATIONS, tree)
    assert summary(findings) == [("E_VALIDATION_CITATION", "error", OUTLINE_RESULTS, 5)]
    assert "any `pending.bib`" in findings[0].message
    assert summary(run(latex.LATEX_MACROS, tree)) == [("E_VALIDATION_MACRO", "error", OUTLINE_RESULTS, 7)]


def test_parts_do_not_declare_sections() -> None:
    """REQ-CONV-94: el título de la sección lo pone su archivo de enlace; ``\\subsection`` sí vale."""
    extra = "\\section{Results}\n\\chapter*{X}\n\\subsection{Details}\n\\paragraph{Note}\n"
    found = [(f.code, f.line) for f in run(latex.CONVENTION, tree_with({RESULTS: text_of(RESULTS) + extra}))]
    assert found == [("conv.latex.structure", 10), ("conv.latex.structure", 11)]


def test_unreadable_outline_is_not_reported_as_missing() -> None:
    tree = tree_with({OUTLINE_RESULTS: b"\xff\xfe\\section{Results}\n"})
    assert [f for f in run(latex.CONVENTION, tree) if f.code.startswith(SECTION_CODES)] == []
    # Las otras reglas fallan cerradas con ese archivo.
    assert (UNAVAILABLE_CODE, "error", OUTLINE_RESULTS, None) in summary(run(latex.LABELS_REFS, tree))


# -- convention: archivos .tex --------------------------------------------------------------


@pytest.mark.parametrize(
    ("snippet", "code", "needle"),
    [
        ("\\immediate\\write18{ls}", "conv.latex.forbidden-command", "\\write18"),
        ("\\ShellEscape{ls}", "conv.latex.forbidden-command", "external programs"),
        ("\\directlua{tex.print(1)}", "conv.latex.forbidden-command", "\\directlua"),
        ("\\begin{minted}{python}\nx\n\\end{minted}", "conv.latex.forbidden-command", "\\begin{minted}"),
        ("\\openin\\f=/etc/passwd", "conv.latex.forbidden-command", "reads or writes files"),
        ("\\IfFileExists{x}{}{}", "conv.latex.forbidden-command", "\\IfFileExists"),
        ("A ^^5cdef sequence", "conv.latex.forbidden-command", "^^"),
        ("\\iffalse hidden \\fi", "conv.latex.hidden-block", "\\iffalse"),
        ("\\begin{comment}\nhidden\n\\end{comment}", "conv.latex.hidden-block", "\\begin{comment}"),
        ("\\usepackage{tikz}", "conv.latex.structure", "\\usepackage"),
        ("\\begin{document}", "conv.latex.structure", "\\begin{document}"),
        ("\\graphicspath{{x}}", "conv.latex.structure", "\\graphicspath"),
    ],
)
def test_forbidden_constructs_in_sections(snippet: str, code: str, needle: str) -> None:
    findings = [f for f in run(latex.CONVENTION, tree_with({RESULTS: text_of(RESULTS) + snippet + "\n"})) if f.code == code]
    assert findings and all(f.path == RESULTS and f.line == 10 and f.severity == "error" for f in findings)
    assert any(needle in f.message for f in findings), [f.message for f in findings]


@pytest.mark.parametrize(
    ("snippet", "needle"),
    [
        ("\\input{../../main}", "must start with `../sections/results/`"),
        ("\\input{/etc/passwd}", "absolute"),
        ("\\input{../sections/methods/tables/descriptives}", "must start with `../sections/results/`"),
        ("\\input{../sections/results/data.csv}", "`.tex` file"),
        ("\\input ../sections/results/x", "in braces"),
        ("\\includegraphics{../sections/results/tables/a.pdf}", "../sections/results/figures/"),
        ("\\includegraphics[width=1cm]{../sections/results/figures/a.svg}", "../sections/results/figures/"),
        ("\\includesvg{../sections/results/figures/a}", "reads a file by path"),
        ("\\lstinputlisting{~/x.py}", "absolute"),
        ("\\includepdf", "is missing"),
    ],
)
def test_paths_outside_the_section(snippet: str, needle: str) -> None:
    findings = run(latex.CONVENTION, tree_with({RESULTS: text_of(RESULTS) + snippet + "\n"}))
    assert [(f.code, f.line) for f in findings] == [("conv.latex.path-outside-section", 10)], summary(findings)
    assert needle in findings[0].message, findings[0].message


def test_paths_inside_the_section_are_fine() -> None:
    extra = (
        "\\input{../sections/results/tables/summary}\n"
        "\\input{../sections/results/tables/summary.tex}\n"
        "\\includegraphics[width=\\linewidth]{../sections/results/figures/plot}\n"
        "\\includegraphics{../sections/results/figures/plot.PNG.png}\n"
        "\\csvautotabular{../sections/results/tables/data.csv}\n"
        "\\lstinputlisting{../sections/results/analysis/run.py}\n"
        "\\verbatiminput{../sections/results/notes/n1.md}\n"
    )
    assert run(latex.CONVENTION, tree_with({RESULTS: text_of(RESULTS) + extra})) == []


# TX03 (10-seguridad §7.2) y REQ-CONV-31: el único '..' admitido es el del prefijo exacto
# ../sections/<id-propio>/; la compilación corre en tex/ (02 §4.5).
TX03_COMMANDS = ("input", "includegraphics", "includepdf", "lstinputlisting", "verbatiminput", "csvautotabular")


@pytest.mark.parametrize("command", TX03_COMMANDS)
@pytest.mark.parametrize(
    ("target", "needle"),
    [
        ("../sections/methods/figures/a.pdf", "must start with `../sections/results/`"),  # parte ajena
        ("../../a.pdf", "must start with `../sections/results/`"),
        ("../bib/a.pdf", "must start with `../sections/results/`"),
        ("sections/results/figures/a.pdf", "must start with `../sections/results/`"),  # forma anterior a D-43
        ("../sections/results/../methods/figures/a.pdf", "other than the one of the prefix"),
        ("../sections/results/figures/../../../roles.yaml", "other than the one of the prefix"),
        ("../sections/results/..", "other than the one of the prefix"),
    ],
)
def test_tx03_parent_paths_only_through_the_own_prefix(command: str, target: str, needle: str) -> None:
    tree = tree_with({RESULTS: text_of(RESULTS) + f"\\{command}{{{target}}}\n"})
    findings = run(latex.CONVENTION, tree)
    assert [(f.code, f.line) for f in findings] == [("conv.latex.path-outside-section", 10)], summary(findings)
    assert needle in findings[0].message, findings[0].message


@pytest.mark.parametrize("command", TX03_COMMANDS)
def test_tx03_own_prefix_passes(command: str) -> None:
    target = "../sections/results/figures/a.pdf" if command in ("includegraphics", "includepdf") else (
        "../sections/results/tables/t" if command == "input" else "../sections/results/analysis/derived/d.csv"
    )
    assert run(latex.CONVENTION, tree_with({RESULTS: text_of(RESULTS) + f"\\{command}{{{target}}}\n"})) == []


def test_tx03_own_prefix_does_not_open_other_readers() -> None:
    # Con el prefijo propio siguen prohibidos los comandos de estructura y de E/S (02 §4.6).
    snippet = (
        "\\include{../sections/results/x}\n"
        "\\addbibresource{../sections/results/pending.bib}\n"
        "\\InputIfFileExists{../sections/results/x}{}{}\n"
    )
    found = [(f.code, f.line) for f in run(latex.CONVENTION, tree_with({RESULTS: text_of(RESULTS) + snippet}))]
    assert found == [
        ("conv.latex.structure", 10),
        ("conv.latex.structure", 11),
        ("conv.latex.forbidden-command", 12),
    ]


def test_lead_file_rules() -> None:
    # notation.tex no carga paquetes ni incluye archivos.
    tree = tree_with({"tex/notation.tex": text_of("tex/notation.tex") + "\\usepackage{x}\n\\input{other}\n"})
    found = [(f.code, f.line) for f in run(latex.CONVENTION, tree)]
    assert found == [("conv.latex.structure", 9), ("conv.latex.structure", 10)]
    # preamble.tex no carga paquetes de shell escape.
    tree = tree_with({"tex/preamble.tex": text_of("tex/preamble.tex") + "\\usepackage{graphicx,svg}\n\\usepackage[cache=false]{minted}\n"})
    findings = run(latex.CONVENTION, tree)
    assert [(f.code, f.line) for f in findings] == [("conv.latex.shell-package", 18), ("conv.latex.shell-package", 19)]
    assert "svg" in findings[0].message and "minted" in findings[1].message
    # Las secciones sí pueden usar \write como texto en verbatim; el lead puede \input en main.
    assert run(latex.CONVENTION, tree_with({RESULTS: text_of(RESULTS) + "\\verb|\\write18|\n"})) == []


def test_lua_only_in_lead_files_with_lualatex() -> None:
    lua = "\\directlua{tex.print('x')}\n\\begin{luacode}\nx = 1\n\\end{luacode}\n"
    tree = tree_with({"tex/preamble.tex": text_of("tex/preamble.tex") + lua})
    findings = run(latex.CONVENTION, tree)
    assert [(f.code, f.line) for f in findings] == [("conv.latex.forbidden-command", 18), ("conv.latex.forbidden-command", 19)]
    assert "lualatex" in findings[0].message
    tree = tree_with({"tex/preamble.tex": text_of("tex/preamble.tex") + lua, "project.yaml": project_yaml("lualatex")})
    assert run(latex.CONVENTION, tree) == []
    # La política de la base da el motor si el project.yaml del árbol no es válido.
    tree = tree_with({"tex/preamble.tex": text_of("tex/preamble.tex") + lua, "project.yaml": "x: [\n"})
    assert len(run(latex.CONVENTION, tree, policy=snapshot(roles_fixture("basic")))) == 2


def test_sentence_per_line_warnings() -> None:
    extra = "First sentence. Second sentence.\nThe value 3.5 is fine, e.g. here.\n$a. B$ is math.\n"
    findings = run(latex.CONVENTION, tree_with({RESULTS: text_of(RESULTS) + extra}))
    assert [(f.code, f.severity, f.line) for f in findings] == [("conv.latex.sentence-per-line", "warning", 10)]
    assert "Second sentence" in findings[0].message
    assert status_of(findings) == "pass"


def test_convention_part_is_skipped_without_manuscript() -> None:
    """00-marco D-33 y D-43: con format: none, conv.main.*, conv.latex.* y conv.bib.* quedan en skip."""
    none_project = load_tool("render_template").render_files("en", fmt="none")["project.yaml"]
    bad = {
        INTRO: text_of(INTRO) + "\\write18{rm}\nTwo sentences. In one line.\n",
        "tex/main.tex": text_of("tex/main.tex") + "\\usepackage{x}\n",
        "bib/references.bib": "@book{9bad, title = {x}, file = {y}}\n",
        "memory/notes/task-3.md": "Plain prose. With two sentences.\n",
    }
    with_latex = run(latex.CONVENTION, tree_with(bad))
    assert {"conv.latex.forbidden-command", "conv.main.grammar", "conv.bib.key-syntax"} <= set(codes(with_latex))
    assert "conv.latex.sentence-per-line" in codes(with_latex)
    without = run(latex.CONVENTION, tree_with({**bad, "project.yaml": none_project}))
    assert without == []  # REQ-CONV-26 tampoco se verifica en Markdown (02 §2.3)


def test_sentence_per_line_in_memory_markdown() -> None:
    note = (
        "---\ntitle: Front. Matter\n---\n"
        "## 1. Heading with a number\n"
        "1. A numbered item. Second sentence here.\n"
        "- A bullet. Another one.\n"
        "Use `a. B` in code.\n"
        "```\ncode. Block\n```\n"
        "| table. Cell |\n"
        "    indented. Code\n"
        "Plain prose. With two sentences.\n"
    )
    findings = run(latex.CONVENTION, tree_with({"memory/notes/2026-10-01-x.md": note}))
    assert [(f.path, f.line) for f in findings] == [
        ("memory/notes/2026-10-01-x.md", 5),
        ("memory/notes/2026-10-01-x.md", 6),
        ("memory/notes/2026-10-01-x.md", 13),
    ]
    assert {f.severity for f in findings} == {"warning"}


# -- convention: bibliografía ---------------------------------------------------------------


def test_bib_key_syntax_and_forbidden_fields() -> None:
    bib = (
        "@misc{1bad, note = {x}}\n"
        "@misc{withFile, file = {/home/me/paper.pdf}, abstract = {Long text.}}\n"
        "@misc{kw, keywords = {a, b}, annotation = {n}}\n"
    )
    findings = run(latex.CONVENTION, tree_with({"sections/results/pending.bib": bib}))
    assert [(f.code, f.severity, f.line) for f in findings] == [
        ("conv.bib.key-syntax", "error", 1),
        ("conv.bib.forbidden-field", "error", 2),
        ("conv.bib.forbidden-field", "warning", 2),
        ("conv.bib.forbidden-field", "warning", 3),
        ("conv.bib.forbidden-field", "warning", 3),
    ]
    assert "`file`" in findings[1].message
    refs = text_of("bib/references.bib") + "@misc{9lives, note = {z}}\n"
    assert [f.code for f in run(latex.CONVENTION, tree_with({"bib/references.bib": refs}))] == ["conv.bib.key-syntax"]


def test_pending_consolidated_and_conflict() -> None:
    consolidated = text_of(METHODS_BIB) + "@book{knuthTeXbook1984, title = {The {{TeX}}book}}\n"
    findings = run(latex.CONVENTION, tree_with({METHODS_BIB: consolidated}))
    assert [(f.code, f.severity, f.path, f.line) for f in findings] == [
        ("conv.bib.pending-consolidated", "warning", METHODS_BIB, 9)
    ]
    other = "@article{doeSurvey2021, title = {A Different Title}}\n"
    findings = run(latex.CONVENTION, tree_with({METHODS_BIB: other}))
    assert sorted((f.code, f.path) for f in findings) == [
        ("conv.bib.pending-conflict", INTRO_BIB),
        ("conv.bib.pending-conflict", METHODS_BIB),
    ]
    assert all(status_of([f]) == "pass" for f in findings)
    # El mismo contenido con otros espacios no es conflicto.
    same = "@article{doeSurvey2021,\n title = {A Survey of Everything}, author = {Doe, Jane},\n date = {2021}, journaltitle = {Journal of Tests}, volume = {4}}\n"
    assert run(latex.CONVENTION, tree_with({METHODS_BIB: same})) == []


def test_messages_are_localized(t_es: Translator, t_en: Translator) -> None:
    tree = tree_with({RESULTS: text_of(RESULTS) + "\\newcommand{\\x}{1}\n\\cite{nada}\n"})
    es = run(latex.LATEX_MACROS, tree, t=t_es)[0]
    en = run(latex.LATEX_MACROS, tree, t=t_en)[0]
    assert es.message != en.message and "\\newcommand" in es.message and "\\newcommand" in en.message
    assert "fuera de" in es.message and "outside" in en.message
    es_cite = run(latex.CITATIONS, tree, t=t_es)[0]
    assert "la clave 'nada'" in es_cite.message
