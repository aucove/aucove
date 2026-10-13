# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Regla ``convention`` (02-convencion §12; 00-marco D-33 g y D-36).

Cada código ``conv.*`` de la regla y ``E_VALIDATION_LICENSE_UNKNOWN`` tienen un caso
que lo dispara y uno parecido que no. Los casos parten del render de la plantilla de
H2, que no da ningún hallazgo (REQ-CONV-3 a nivel de plantilla), y le aplican un
cambio. ``conv.tree.missing`` lee la lista de ``required-paths.json`` (D-36), y las
reglas de enlaces, mayúsculas y rutas locales se prueban también con los modos de
``git ls-tree`` sobre un repositorio temporal.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aucove import _meta
from aucove.config.base import BaseSnapshot
from aucove.config.project import parse_project
from aucove.config.roles import parse_roles
from aucove.domain.findings import Finding, has_errors
from aucove.i18n import Lang, Translator, load_catalog, placeholders, translator
from aucove.ops.rules import convention
from aucove.ops.rules.base import RuleInput, status_of
from aucove.render.template import project_yaml_without_manuscript
from aucove.vcs.git import Git
from aucove.vcs.tree import git_tree
from tests.conftest import FIXTURES, GitRepo, load_tool

FIX = FIXTURES / "convention"
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
RULE = convention.RULE
SECTION = "sections/introduction/"
KIB = 1024
MIB = 1024 * 1024
UNAVAILABLE = "E_INTERNAL_UNEXPECTED"

Change = bytes | str | None | Callable[[str], str]


class DictTree:
    """``TreeView`` en memoria: ruta -> bytes, con modos de ``git ls-tree`` opcionales."""

    def __init__(self, files: Mapping[str, bytes | str], modes: Mapping[str, str] | None = None) -> None:
        self.files = {p: (c.encode("utf-8") if isinstance(c, str) else c) for p, c in files.items()}
        self.modes = dict(modes or {})
        self.root: Path | None = None
        self.sha: str | None = None

    def entries(self) -> Mapping[str, str]:
        out = {p: "100644" for p in self.files}
        out.update(self.modes)
        return out

    def read_bytes(self, path: str, limit: int = 5 * MIB) -> bytes | None:
        mode = self.entries().get(path)
        if mode is None or not mode.startswith("100") or path not in self.files:
            return None
        data = self.files[path]
        return data if len(data) <= limit else None

    def read_text(self, path: str, limit: int = 5 * MIB) -> str | None:
        data = self.read_bytes(path, limit)
        try:
            return data.decode("utf-8") if data is not None else None
        except UnicodeDecodeError:
            return None


@pytest.fixture(scope="module")
def render() -> Mapping[str, Mapping[str, bytes]]:
    tool = load_tool("render_template")
    return {lang: tool.render_files(lang) for lang in ("es", "en")}


def snapshot(files: Mapping[str, bytes]) -> BaseSnapshot:
    cfg, _ = parse_project(files["project.yaml"])
    roles, _ = parse_roles(files["roles.yaml"])
    assert cfg is not None and roles is not None
    return BaseSnapshot(
        ref="origin/main", sha=None, project=cfg, roles=roles, decisions=frozenset(), fallback_sha=None, source="local"
    )


def apply(files: Mapping[str, bytes], changes: Mapping[str, Change]) -> dict[str, bytes]:
    """Copia de ``files`` con los cambios: bytes o texto reemplazan, ``None`` borra y una
    función recibe el texto actual y devuelve el nuevo."""
    out = dict(files)
    for path, change in changes.items():
        if change is None:
            out.pop(path, None)
        elif callable(change):
            out[path] = change(out[path].decode("utf-8")).encode("utf-8")
        else:
            out[path] = change.encode("utf-8") if isinstance(change, str) else change
    return out


def check_shape(findings: list[Finding]) -> None:
    """Todo hallazgo es de la regla y su texto salió del catálogo (sin claves crudas)."""
    for f in findings:
        assert f.check == "convention"
        if f.code == UNAVAILABLE:
            continue  # su texto vive en validate.toml
        assert f.severity == convention.SEVERITY[f.code]
        for text in (f.message, f.hint or ""):
            assert "finding." not in text and "convention." not in text, text
            assert "{" not in text, text


def evaluate(
    files: Mapping[str, bytes | str],
    *,
    lang: Lang = "es",
    modes: Mapping[str, str] | None = None,
    policy: BaseSnapshot | None = None,
    base: Mapping[str, bytes | str] | None = None,
    t: Translator | None = None,
) -> list[Finding]:
    inp = RuleInput(
        mode="pr",
        t=t or translator(lang),
        now=NOW,
        policy=policy,
        tree=DictTree(files, modes),
        base_tree=DictTree(base) if base is not None else None,
    )
    findings = list(RULE.run(inp))
    check_shape(findings)
    return findings


def run_on_render(
    render: Mapping[str, Mapping[str, bytes]],
    changes: Mapping[str, Change] | None = None,
    *,
    lang: Lang = "es",
    modes: Mapping[str, str] | None = None,
    t: Translator | None = None,
) -> list[Finding]:
    """La regla sobre el render con ``changes``; la base y la política son el render sin cambios."""
    original = render[lang]
    return evaluate(
        apply(original, changes or {}), lang=lang, modes=modes, policy=snapshot(original), base=original, t=t
    )


class Recorder:
    """Traductor que anota las claves pedidas, para saber qué texto de ``{problem}`` salió."""

    def __init__(self, lang: Lang = "es") -> None:
        self.inner = translator(lang)
        self.lang: Lang = lang
        self.keys: list[str] = []

    def __call__(self, key: str, /, **kw: object) -> str:
        self.keys.append(key)
        return self.inner(key, **kw)

    def get(self, key: str, /, **kw: object) -> str | None:
        self.keys.append(key)
        return self.inner.get(key, **kw)

    def plural(self, key: str, n: int, /, **kw: object) -> str:
        return self.inner.plural(key, n, **kw)

    def problems(self, prefix: str) -> list[str]:
        """Sufijos de las claves ``convention.<prefix>.*`` pedidas, en orden."""
        start = f"convention.{prefix}."
        return [k[len(start) :] for k in self.keys if k.startswith(start)]


def problems_of(
    render: Mapping[str, Mapping[str, bytes]], changes: Mapping[str, Change], prefix: str
) -> tuple[list[Finding], list[str]]:
    rec = Recorder()
    findings = run_on_render(render, changes, t=rec)  # type: ignore[arg-type]
    return findings, rec.problems(prefix)


def codes(findings: list[Finding]) -> list[str]:
    return [f.code for f in findings]


def of(findings: list[Finding], code: str) -> list[Finding]:
    return [f for f in findings if f.code == code]


def sub(old: str, new: str) -> Callable[[str], str]:
    def change(text: str) -> str:
        assert old in text, old
        return text.replace(old, new, 1)

    return change


def append(extra: str) -> Callable[[str], str]:
    return lambda text: text + extra


def fixture(*parts: str) -> str:
    return FIX.joinpath(*parts).read_text(encoding="utf-8")


# -- datos de los casos -------------------------------------------------------------------

# Una parte que la plantilla no trae (00-marco D-55 ya trae introduction a conclusions).
ROLES_WITH_RESULTS = append('  - id: appendix\n    title: "Appendix"\n    owner: example-researcher\n')
RESULTS_FOLDER: dict[str, Change] = {
    "sections/appendix/appendix.tex": "% Anexo.\n",
    "sections/appendix/pending.bib": "",
    "sections/appendix/figures/.gitkeep": "",
    "sections/appendix/tables/.gitkeep": "",
}
SIGNED_ROWS = (
    "# Datos\n\n"
    "| id | description | location | access | classification | version |\n"
    "|---|---|---|---|---|---|\n"
    "| survey | Encuesta | https://bucket.s3.amazonaws.com/s.csv?X-Amz-Signature=abc&x=1 | Privado | restricted | v1 |\n"
)


def prov_files(provenance: str | None) -> dict[str, Change]:
    """Las salidas, el generador y el índice de datos que cita el ejemplo de 02 §6.4."""
    files: dict[str, Change] = {
        SECTION + "figures/sample-sizes.pdf": b"%PDF-1.5\n",
        SECTION + "figures/flowchart.pdf": b"%PDF-1.5\n",
        SECTION + "figures/map.png": b"\x89PNG\r\n",
        SECTION + "analysis/sample_sizes.py": "print(1)\n",
        SECTION + "analysis/derived/counts.csv": "n\n1\n",
        "data/README.md": fixture("data", "README-rows.md"),
    }
    if provenance is not None:
        files[SECTION + "provenance.yaml"] = provenance
    return files


PROV_EXAMPLE = fixture("provenance", "example-02.yaml")
LICENSES_BLOCK = "licenses:\n  text: CC-BY-4.0\n  code: MIT-0\n  data: CC0-1.0\n"
# format: none (D-43): project.yaml del render none y lo que la variante none no trae.
NONE_PROJECT: Callable[[str], str] = project_yaml_without_manuscript
# Las 7 partes de la plantilla (00-marco D-55).
TEMPLATE_PARTS: tuple[str, ...] = (
    "introduction", "background", "theory", "methodology", "experiments", "results", "conclusions",
)
LATEX_ONLY: dict[str, Change] = {
    ".latexmkrc": None,
    "bib/references.bib": None,
    "tex/main.tex": None,
    "tex/preamble.tex": None,
    "tex/notation.tex": None,
    "tex/guide.md": None,  # 00-marco D-56
    **{f"tex/outline/{p}.tex": None for p in TEMPLATE_PARTS},
    **{f"sections/{p}/pending.bib": None for p in TEMPLATE_PARTS},
    **{f"sections/{p}/{p}.tex": None for p in TEMPLATE_PARTS},
}
TO_NONE: dict[str, Change] = {
    "project.yaml": NONE_PROJECT, **LATEX_ONLY, **{f"sections/{p}/{p}.md": f"# {p}\n" for p in TEMPLATE_PARTS},
}
AGENTS_SECTION_2 = "## 2. Antes de empezar\n"
AGENTS_RULE = "- Regla propia: las tablas van en `tables/`.\n"

# (código, cambio que lo dispara, cambio parecido que no, ruta esperada, modos al disparar, modos sin disparar)
CASES: list[tuple[str, dict[str, Change], dict[str, Change], str, dict[str, str], dict[str, str]]] = [
    # Los textos de LICENSES/ no se exigen: los elige init (11-trazabilidad §7.3).
    ("conv.tree.missing", {"plan.md": None}, {"LICENSES/MIT-0.txt": None}, "plan.md", {}, {}),
    # REQ-CONV-88: con format: none, lo que queda del manuscrito es un aviso.
    ("conv.format.unused", {"project.yaml": NONE_PROJECT}, TO_NONE, "project.yaml", {}, {}),
    (
        "conv.tree.missing",
        {SECTION + "tables/.gitkeep": None},
        {SECTION + "tables/.gitkeep": None, SECTION + "tables/t1.tex": "% tabla\n"},
        SECTION + "tables",
        {},
        {},
    ),
    (
        "conv.path.invalid-name",
        {SECTION + "figures/mi figura.tex": "% x\n"},
        {SECTION + "figures/my_fig-1.tex": "% x\n", "notas sueltas.txt": "x\n"},
        SECTION + "figures/mi figura.tex",
        {},
        {},
    ),
    ("conv.path.case-collision", {"readme.md": "x\n"}, {"readme.txt": "x\n"}, "readme.md", {}, {}),
    (
        "conv.path.symlink",
        {},
        {"tools.sh": "echo\n"},
        SECTION + "link.tex",
        {SECTION + "link.tex": "120000"},
        {"tools.sh": "100755"},
    ),
    ("conv.path.symlink", {}, {}, "vendor/lib", {"vendor/lib": "160000"}, {}),
    (
        "conv.path.local-committed",
        {"tex/build/main.pdf": b"%PDF"},
        {"tex/build.tex": "% x\n", ".claude/settings.json": "{}\n"},
        "tex/build/main.pdf",
        {},
        {},
    ),
    ("conv.path.local-committed", {_meta.CACHE_DIR + "/cache/x.json": "{}\n"}, {}, _meta.CACHE_DIR + "/cache/x.json", {}, {}),
    ("conv.path.nested-instructions", {"docs/CLAUDE.md": "x\n"}, {"docs/claude-notes.md": "x\n"}, "docs/CLAUDE.md", {}, {}),
    ("conv.path.foreign-instructions", {"GEMINI.md": "x\n"}, {"gemini-notes.md": "x\n"}, "GEMINI.md", {}, {}),
    ("conv.path.foreign-instructions", {".cursor/rules.md": "x\n"}, {}, ".cursor", {}, {}),
    (
        "conv.section.undeclared",
        RESULTS_FOLDER,
        {**RESULTS_FOLDER, "roles.yaml": ROLES_WITH_RESULTS},
        "sections/appendix",
        {},
        {},
    ),
    (
        "conv.section.missing",
        {"roles.yaml": ROLES_WITH_RESULTS},
        {**RESULTS_FOLDER, "roles.yaml": ROLES_WITH_RESULTS},
        "sections/appendix",
        {},
        {},
    ),
    (
        "conv.section.disallowed-file",
        {SECTION + "figures/data.xlsx": b"PK"},
        {SECTION + "analysis/run.py": "print(1)\n", SECTION + "analysis/derived/out.json": "{}\n"},
        SECTION + "figures/data.xlsx",
        {},
        {},
    ),
    ("conv.file.large", {"docs/big.md": b"a" * (MIB + 1)}, {"docs/big.md": b"a" * MIB}, "docs/big.md", {}, {}),
    ("conv.file.too-large", {"docs/big.md": b"a" * (5 * MIB + 1)}, {"docs/big.md": b"a" * (5 * MIB)}, "docs/big.md", {}, {}),
    (
        "conv.file.encoding",
        {SECTION + "introduction.tex": lambda t: t.replace("\n", "\r\n")},
        {"docs/notes.txt": b"a\r\nb\r\n"},
        SECTION + "introduction.tex",
        {},
        {},
    ),
    (
        "conv.memory.unexpected-file",
        {"memory/scratch.txt": "x\n"},
        {"memory/notes/task-12.md": "x\n", "memory/decisions/2026-10-06-12-x.md": "x\n"},
        "memory/scratch.txt",
        {},
        {},
    ),
    (
        "conv.memory.note-large",
        {"memory/notes/task-3.md": "a" * (32 * KIB)},
        {"memory/notes/task-3.md": "a" * (32 * KIB - 1)},
        "memory/notes/task-3.md",
        {},
        {},
    ),
    ("conv.gitignore.required", {".gitignore": sub("/tex/build/\n", "")}, {".gitignore": append("*.tmp\n")}, ".gitignore", {}, {}),
    (
        "conv.gitattributes.required",
        {".gitattributes": sub("*.pdf   binary\n", "")},
        {".gitattributes": sub("*.pdf   binary", "*.pdf binary")},
        ".gitattributes",
        {},
        {},
    ),
    (
        "conv.gitattributes.lfs",
        {".gitattributes": append("*.psd filter=lfs diff=lfs merge=lfs -text\n")},
        {".gitattributes": append("# *.psd filter=lfs\n")},
        ".gitattributes",
        {},
        {},
    ),
    ("conv.data.unexpected-file", {"data/raw.csv": "a,b\n"}, {"database/raw.csv": "a,b\n"}, "data/raw.csv", {}, {}),
    (
        "conv.data.index",
        {"data/README.md": "# Datos\n\nSin tabla.\n"},
        {"data/README.md": fixture("data", "README-rows.md")},
        "data/README.md",
        {},
        {},
    ),
    (
        "conv.data.credential-url",
        {"data/README.md": SIGNED_ROWS},
        {"data/README.md": fixture("data", "README-rows.md")},
        "data/README.md",
        {},
        {},
    ),
    (
        "conv.provenance.missing",
        {SECTION + "figures/plot.png": b"\x89PNG"},
        {
            SECTION + "figures/plot.png": b"\x89PNG",
            SECTION + "provenance.yaml": "version: 1\noutputs:\n  - path: figures/plot.png\n    kind: figure\n    generator: manual\n",
        },
        SECTION + "figures/plot.png",
        {},
        {},
    ),
    (
        "conv.provenance.invalid",
        prov_files(PROV_EXAMPLE.replace('    credit: "OpenStreetMap contributors, ODbL"\n', "")),
        prov_files(PROV_EXAMPLE),
        SECTION + "provenance.yaml",
        {},
        {},
    ),
    (
        "conv.notebook.outputs",
        {SECTION + "analysis/nb.ipynb": fixture("notebooks", "dirty.ipynb")},
        {SECTION + "analysis/nb.ipynb": fixture("notebooks", "clean.ipynb")},
        SECTION + "analysis/nb.ipynb",
        {},
        {},
    ),
    (
        "conv.convention.value",
        {"project.yaml": sub('convention: "0.1"', "convention: 0.1")},
        {"project.yaml": sub('convention: "0.1"', "convention: '0.1'")},
        "project.yaml",
        {},
        {},
    ),
    (
        "conv.agents.structure",
        {"AGENTS.md": sub("## 7. ", "## 8. ")},
        {"AGENTS.md": append(AGENTS_RULE)},
        "AGENTS.md",
        {},
        {},
    ),
    (
        "conv.agents.import",
        {"AGENTS.md": append("- Lee también @docs/extra.md\n")},
        {"AGENTS.md": append("- Ejemplo sin efecto: `@docs/extra.md`, o escribe a ana@example.org.\n")},
        "AGENTS.md",
        {},
        {},
    ),
    (
        "conv.agents.modified",
        {"AGENTS.md": sub(AGENTS_SECTION_2, AGENTS_SECTION_2 + "Texto añadido.\n")},
        {"AGENTS.md": append(AGENTS_RULE)},
        "AGENTS.md",
        {},
        {},
    ),
    (
        "conv.claude.content",
        {"CLAUDE.md": sub("@AGENTS.md\n", "# Claude\n@AGENTS.md\n")},
        {"CLAUDE.md": append("- Ejemplo sin efecto: `@otro.md`.\n")},
        "CLAUDE.md",
        {},
        {},
    ),
    (
        "conv.constitution.structure",
        {"memory/constitution.md": append("\n- RQ-1: no va aquí\n")},
        {"memory/constitution.md": append("\nCita RQ-1 sin definirlo.\n")},
        "memory/constitution.md",
        {},
        {},
    ),
    (
        "conv.latexmkrc.noncanonical",
        {".latexmkrc": sub("$do_cd = 1;", "$do_cd = 0;")},
        {"project.yaml": sub("engine: pdflatex", "engine: xelatex"), ".latexmkrc": sub("$pdf_mode = 1;", "$pdf_mode = 5;")},
        ".latexmkrc",
        {},
        {},
    ),
    (
        "E_VALIDATION_LICENSE_UNKNOWN",
        {"project.yaml": append(LICENSES_BLOCK)},
        {"project.yaml": append(LICENSES_BLOCK), "LICENSES/CC-BY-4.0.txt": "Texto de la licencia.\n"},
        "project.yaml",
        {},
        {},
    ),
]
# conv.section.large necesita más de 20 MiB y tiene su propia prueba.
COVERED_ELSEWHERE = {"conv.section.large"}


def case_id(case: tuple[str, dict[str, Change], dict[str, Change], str, dict[str, str], dict[str, str]]) -> str:
    return f"{case[0]}:{case[3]}"


# -- registro y catálogo ----------------------------------------------------------------------


def test_rule_spec() -> None:
    assert convention.RULES == (RULE,)
    assert RULE.id == "convention"
    assert RULE.modes == frozenset({"tree", "pr", "base"})
    assert not RULE.needs_forge and RULE.after == () and RULE.tools == ()
    assert RULE.codes is convention.CODES


def test_codes_are_the_assigned_groups() -> None:
    groups = {c.split(".")[1] for c in RULE.codes if c.startswith("conv.")}
    assert groups == {
        "tree", "format", "path", "section", "file", "memory", "gitignore", "gitattributes", "data",
        "provenance", "notebook", "convention", "agents", "claude", "constitution", "latexmkrc",
    }
    assert {c for c in RULE.codes if not c.startswith("conv.")} == {"E_VALIDATION_LICENSE_UNKNOWN"}


def test_severities_follow_the_catalog_of_02_section_12() -> None:
    warnings = {c for c, s in convention.SEVERITY.items() if s == "warning"}
    assert warnings == {
        "conv.format.unused",
        "conv.path.foreign-instructions",
        "conv.section.large",
        "conv.file.large",
        "conv.memory.note-large",
        "conv.data.credential-url",
        "conv.provenance.missing",
        "conv.agents.modified",
        "conv.latexmkrc.noncanonical",
    }
    assert set(convention.SEVERITY) == set(RULE.codes)


def test_every_code_has_a_case_that_fires_and_one_that_does_not() -> None:
    covered = {case[0] for case in CASES} | COVERED_ELSEWHERE
    assert covered == set(RULE.codes)


@pytest.mark.parametrize("lang", ["es", "en"])
def test_every_code_has_localized_text_with_its_markers(lang: Lang) -> None:
    catalog = load_catalog(lang)
    for code, params in RULE.codes.items():
        message = catalog[f"finding.{code}.message"]
        hint = catalog.get(f"finding.{code}.hint", "")
        assert placeholders(message) | placeholders(hint) == params, code
        assert not params & {"rule", "inp", "code", "severity", "path", "line", "pointer"}, code


def test_markers_do_not_clash_with_other_rules() -> None:
    from aucove.ops.rules import plan_ids

    for code in set(RULE.codes) & set(plan_ids.RULE.codes):
        assert RULE.codes[code] == plan_ids.RULE.codes[code]


# -- criterios de aceptación -----------------------------------------------------------------


@pytest.mark.parametrize("lang", ["es", "en"])
@pytest.mark.parametrize("with_base", [True, False])
def test_template_render_has_no_findings(render: Mapping[str, Mapping[str, bytes]], lang: Lang, with_base: bool) -> None:
    files = render[lang]
    findings = evaluate(files, lang=lang, policy=snapshot(files), base=files if with_base else None)
    assert not has_errors(findings), [f.message for f in findings]
    assert findings == []


def test_template_render_without_policy_uses_its_own_roles(render: Mapping[str, Mapping[str, bytes]]) -> None:
    assert evaluate(render["en"], lang="en") == []


def test_tree_missing_counts_one_finding_when_plan_is_deleted(render: Mapping[str, Mapping[str, bytes]]) -> None:
    findings = run_on_render(render, {"plan.md": None})
    assert [(f.code, f.path, f.severity) for f in findings] == [("conv.tree.missing", "plan.md", "error")]
    assert "plan.md" in findings[0].message
    assert status_of(findings) == "fail"


def test_mcp_json_is_required_since_h3(render: Mapping[str, Mapping[str, bytes]]) -> None:
    """12-mvp §1.2: ``.mcp.json`` y los workflows de vencimiento y base rota son de H3."""
    required = convention.required_paths()
    assert required.milestone == "H3"
    wired = {".mcp.json", ".github/workflows/main-broken.yml", ".github/workflows/expire-claims.yml"}
    assert wired <= set(required.fixed) and wired <= set(render["es"])
    assert of(run_on_render(render), "conv.tree.missing") == []
    missing = of(run_on_render(render, {".mcp.json": None}), "conv.tree.missing")
    assert [f.path for f in missing] == [".mcp.json"]


def test_required_list_comes_from_required_paths_json(
    render: Mapping[str, Mapping[str, bytes]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = json.loads(convention.datafiles.required_paths_file().read_text(encoding="utf-8"))
    data["fixed"].append("docs/extra.md")
    data["per_section"].append("sections/{id}/notes.tex")
    target = tmp_path / "required-paths.json"
    target.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(convention.datafiles, "required_paths_file", lambda: target)
    findings = run_on_render(render)
    paths = [f.path for f in of(findings, "conv.tree.missing")]
    assert sorted(paths) == sorted(["docs/extra.md", *(f"sections/{p}/notes.tex" for p in TEMPLATE_PARTS)])


@pytest.mark.parametrize("case", CASES, ids=case_id)
def test_code_fires_and_near_miss_does_not(
    render: Mapping[str, Mapping[str, bytes]],
    case: tuple[str, dict[str, Change], dict[str, Change], str, dict[str, str], dict[str, str]],
) -> None:
    code, fire, quiet, path, fire_modes, quiet_modes = case
    fired = of(run_on_render(render, fire, modes=fire_modes), code)
    assert fired, f"{code} no se disparó"
    assert path in {f.path for f in fired}
    assert all(f.severity == convention.SEVERITY[code] for f in fired)
    assert of(run_on_render(render, quiet, modes=quiet_modes), code) == []


def test_section_large(render: Mapping[str, Mapping[str, bytes]]) -> None:
    blob = b"x" * (4 * MIB + MIB // 2)  # 4,5 MiB: aviso de archivo grande, pero no error
    five = {f"{SECTION}analysis/blob{i}.txt": blob for i in range(5)}
    findings = run_on_render(render, five)
    [large] = of(findings, "conv.section.large")
    assert large.path == "sections/introduction" and large.severity == "warning"
    assert "22,5 MiB" in large.message
    assert codes(findings).count("conv.file.large") == 5 and not has_errors(findings)
    four = {f"{SECTION}analysis/blob{i}.txt": blob for i in range(4)}
    assert of(run_on_render(render, four), "conv.section.large") == []
    findings_en = run_on_render(render, five, lang="en")
    assert "22.5 MiB" in of(findings_en, "conv.section.large")[0].message


# -- modos de git ls-tree -----------------------------------------------------------------------


def _blob(repo: GitRepo, tmp: Path, content: bytes) -> str:
    source = tmp / "blob.bin"
    source.write_bytes(content)
    return repo.git("hash-object", "-w", str(source))


def test_path_rules_use_git_ls_tree_modes(
    git_repo: GitRepo, tmp_path: Path, render: Mapping[str, Mapping[str, bytes]]
) -> None:
    files = render["es"]
    base = git_repo.commit("plantilla", files=dict(files))
    blob = _blob(git_repo, tmp_path, b"contenido\n")
    entries = [
        f"120000,{blob},{SECTION}link.tex",  # enlace simbólico
        f"160000,{base},vendor/lib",  # submódulo (gitlink)
        f"100644,{blob},tex/build/main.pdf",  # ruta local ignorada, forzada en el índice
        f"100644,{blob},{_meta.CACHE_DIR}/cache/x.json",
        f"100644,{blob},readme.md",  # choca con README.md salvo por mayúsculas
    ]
    for entry in entries:
        git_repo.git("-c", "core.ignorecase=false", "update-index", "--add", "--cacheinfo", entry)
    git_repo.git("commit", "-q", "--no-verify", "-m", "modos")
    head = git_repo.head()
    git = Git(git_repo.path, env=git_repo.env)
    tree = git_tree(git_repo.path, head, git=git)
    assert tree.entries()[SECTION + "link.tex"] == "120000"
    assert tree.entries()["vendor/lib"] == "160000"
    inp = RuleInput(
        mode="pr",
        t=translator("es"),
        now=NOW,
        policy=snapshot(files),
        tree=tree,
        base_tree=git_tree(git_repo.path, base, git=git),
    )
    findings = list(RULE.run(inp))
    check_shape(findings)
    got = sorted((f.code, f.path) for f in findings)
    assert got == [
        ("conv.path.case-collision", "readme.md"),
        ("conv.path.local-committed", _meta.CACHE_DIR + "/cache/x.json"),
        ("conv.path.local-committed", "tex/build/main.pdf"),
        ("conv.path.symlink", SECTION + "link.tex"),
        ("conv.path.symlink", "vendor/lib"),
    ]
    modes = {f.path: f.message for f in of(findings, "conv.path.symlink")}
    assert "120000" in modes[SECTION + "link.tex"] and "160000" in modes["vendor/lib"]
    [collision] = of(findings, "conv.path.case-collision")
    assert "README.md" in collision.message


def test_template_render_passes_from_git_objects(git_repo: GitRepo, render: Mapping[str, Mapping[str, bytes]]) -> None:
    files = render["en"]
    sha = git_repo.commit("plantilla", files=dict(files))
    git = Git(git_repo.path, env=git_repo.env)
    inp = RuleInput(
        mode="base",
        t=translator("en"),
        now=NOW,
        policy=snapshot(files),
        tree=git_tree(git_repo.path, sha, git=git),
        base_tree=None,
    )
    assert list(RULE.run(inp)) == []


# -- detalle de cada grupo -----------------------------------------------------------------------


def test_section_missing_skips_tree_missing_of_its_files(render: Mapping[str, Mapping[str, bytes]]) -> None:
    findings = run_on_render(render, {"roles.yaml": ROLES_WITH_RESULTS})
    assert codes(findings) == ["conv.section.missing"]


def test_old_section_folder_is_not_a_part(render: Mapping[str, Mapping[str, bytes]]) -> None:
    """D-43: la parte vive en sections/<id>/; una copia en tex/sections/<id>/ no la cuenta."""
    original = render["es"]
    moved = {("tex/" + p if p.startswith("sections/") else p): data for p, data in original.items()}
    assert any(p.startswith("tex/sections/introduction/") for p in moved)
    findings = evaluate(moved, policy=snapshot(original), base=original)
    parts = ("introduction", "background", "theory", "methodology", "experiments", "results", "conclusions")
    assert sorted((f.code, f.path) for f in findings) == sorted(("conv.section.missing", f"sections/{p}") for p in parts)
    intro = next(f for f in findings if f.path == "sections/introduction")
    assert "sections/introduction/" in intro.hint and "tex/sections" not in intro.hint


def test_part_notes_and_markdown_follow_the_latex_column(render: Mapping[str, Mapping[str, bytes]]) -> None:
    """02 §4.2 con format: latex: notes/ (md, txt) y .md en la raíz y en otras subcarpetas."""
    ok = {
        SECTION + "notes/n1.md": "# Nota\n",
        SECTION + "notes/raw.txt": "x\n",
        SECTION + "notes/.gitkeep": "",
        SECTION + "summary.md": "# Resumen\n",
        SECTION + "drafts/x.md": "x\n",
    }
    assert of(run_on_render(render, ok), "conv.section.disallowed-file") == []
    bad = run_on_render(render, {SECTION + "notes/n1.tex": "% x\n", SECTION + "notes/d.csv": "a\n"})
    assert sorted(f.path for f in of(bad, "conv.section.disallowed-file")) == [
        SECTION + "notes/d.csv",
        SECTION + "notes/n1.tex",
    ]
    names = run_on_render(render, {SECTION + "notes/mi nota.md": "x\n"})  # REQ-CONV-39 bajo sections/
    assert [f.path for f in of(names, "conv.path.invalid-name")] == [SECTION + "notes/mi nota.md"]


def test_section_rules_use_base_roles_when_head_roles_are_invalid(render: Mapping[str, Mapping[str, bytes]]) -> None:
    findings = run_on_render(render, {"roles.yaml": "version: [\n", **RESULTS_FOLDER})
    assert "conv.section.undeclared" in codes(findings)
    assert UNAVAILABLE not in codes(findings)


def test_without_any_valid_roles_section_checks_are_unavailable_once(render: Mapping[str, Mapping[str, bytes]]) -> None:
    files = apply(render["es"], {"roles.yaml": "version: [\n"})
    findings = evaluate(files, policy=None, base=None)
    assert codes(findings) == [UNAVAILABLE]
    assert findings[0].path == "roles.yaml" and findings[0].severity == "error"


def test_case_collision_reports_only_the_highest_folder() -> None:
    findings = evaluate({"Tex/a.tex": "a\n", "tex/a.tex": "a\n", "tex/b.tex": "b\n"})
    collisions = of(findings, "conv.path.case-collision")
    assert [(f.path, f.message) for f in collisions] == [("tex", collisions[0].message)]
    assert "Tex" in collisions[0].message


def test_foreign_instructions_once_per_root_entry() -> None:
    findings = evaluate({".cursor/a.md": "x\n", ".cursor/b.md": "x\n", ".windsurfrules": "x\n"})
    assert sorted(f.path for f in of(findings, "conv.path.foreign-instructions")) == [".cursor", ".windsurfrules"]


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        (b"\xef\xbb\xbf% x\n", "bom"),
        (b"% a\n% b\r\n", "crlf"),
        (b"% a\n\xe9\n", "not_utf8"),
    ],
)
def test_encoding_problems(data: bytes, problem: str) -> None:
    [finding] = of(evaluate({"tex/x.tex": data}), "conv.file.encoding")
    assert translator("es")(f"convention.encoding.{problem}") in finding.message
    assert finding.line == (1 if problem == "bom" else 2)


def test_encoding_counts_uppercase_r_and_ignores_unknown_suffixes() -> None:
    findings = evaluate({"analysis.R": b"x\r\n", "notes.txt": b"x\r\n", "Makefile": b"x\r\n"})
    assert [f.path for f in of(findings, "conv.file.encoding")] == ["analysis.R"]


def test_memory_layout() -> None:
    ok = {
        "memory/constitution.md": None,
        "memory/.gitkeep": "",
        "memory/decisions/.gitkeep": "",
        "memory/notes/.gitkeep": "",
        "memory/decisions/2026-10-06-12-x.md": "x\n",
        "memory/notes/task-7.md": "x\n",
    }
    bad = ["memory/decisions/sub/x.md", "memory/decisions/.hidden.md", "memory/notes/task-0.md", "memory/notes/otra.md", "memory/x.md"]
    files = {p: c for p, c in ok.items() if c is not None} | {p: "x\n" for p in bad}
    found = sorted(f.path for f in of(evaluate(files), "conv.memory.unexpected-file") if f.path)
    assert found == sorted(bad)


def test_gitignore_and_gitattributes_report_each_missing_line(render: Mapping[str, Mapping[str, bytes]]) -> None:
    findings = run_on_render(render, {".gitignore": "*.log\n", ".gitattributes": "* text=auto eol=lf\n"})
    assert len(of(findings, "conv.gitignore.required")) == len(convention.GITIGNORE_REQUIRED)
    assert len(of(findings, "conv.gitattributes.required")) == len(convention.GITATTRIBUTES_REQUIRED) - 1
    assert "`/" + _meta.CACHE_DIR + "/`" in of(findings, "conv.gitignore.required")[0].message


# 10-seguridad REQ-SEC-35: patrones de secretos que el bloque [req] de 02 §9 aún no lista.
SEC35_EXTRA: tuple[str, ...] = ("*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*")


def test_gitignore_secret_block_follows_req_sec_35() -> None:
    sec35 = (".env", ".env.*", "*.pem", *SEC35_EXTRA)
    assert convention.GITIGNORE_REQUIRED[-len(sec35) :] == sec35


@pytest.mark.parametrize("lang", ["es", "en"])
@pytest.mark.parametrize("entry", SEC35_EXTRA)
def test_gitignore_requires_each_req_sec_35_secret(
    render: Mapping[str, Mapping[str, bytes]], lang: Lang, entry: str
) -> None:
    findings = run_on_render(render, {".gitignore": sub("\n" + entry + "\n", "\n")}, lang=lang)
    [finding] = findings
    assert (finding.code, finding.path) == ("conv.gitignore.required", ".gitignore")
    assert finding.message == translator(lang)("finding.conv.gitignore.required.message", entry=entry)


def test_lfs_in_nested_gitattributes() -> None:
    [finding] = of(evaluate({"tex/.gitattributes": "*.tex text\n*.psd filter=lfs\n"}), "conv.gitattributes.lfs")
    assert finding.path == "tex/.gitattributes" and finding.line == 2


@pytest.mark.parametrize(
    ("table", "key"),
    [
        ("| id | description |\n|---|---|\n", "header"),
        ("| id | description | location | access | classification | version |\n| x |\n", "separator"),
        ("| id | description | location | access | classification | version |\n|---|---|---|---|---|---|\n| a | b |\n", "row"),
        (
            "| id | description | location | access | classification | version |\n|---|---|---|---|---|---|\n"
            "| Mal_ID | d | l | a | public | 1 |\n",
            "id",
        ),
        (
            "| id | description | location | access | classification | version |\n|---|---|---|---|---|---|\n"
            "| a | d | l | x | public | 1 |\n| a | d | l | x | public | 1 |\n",
            "duplicate",
        ),
        (
            "| id | description | location | access | classification | version |\n|---|---|---|---|---|---|\n"
            "| a | d | l | x | secreto | 1 |\n",
            "level",
        ),
    ],
)
def test_data_index_problems(table: str, key: str) -> None:
    index = convention.parse_data_index("# Datos\n\n" + table)
    assert [p[0] for p in index.problems] == [key]
    [finding] = of(evaluate({"data/README.md": "# Datos\n\n" + table}), "conv.data.index")
    assert finding.line is not None and finding.line >= 3


def test_data_index_ignores_tables_in_code_and_reads_escaped_pipes() -> None:
    text = (
        "```\n| no | es |\n```\n"
        "| id | description | location | access | classification | version |\n|:--|---|---|---|---|--:|\n"
        "| a-1 | uno \\| dos | https://x.org/?sig=1 | x | local_only | 1 |\n"
    )
    index = convention.parse_data_index(text)
    assert index.problems == () and index.ids == frozenset({"a-1"})
    assert convention.split_cells("| a \\| b | c |") == ["a | b", "c"]
    assert convention.credential_params(index.locations[0][0]) == ["sig"]


def test_credential_params_cover_the_list_of_req_conv_55() -> None:
    url = "https://h.org/f?token=1&KEY=2&signature=3&access_token=4&format=csv#X-Amz-Signature=5"
    assert convention.credential_params(url) == ["token", "KEY", "signature", "access_token", "X-Amz-Signature"]
    assert convention.credential_params("https://h.org/f?format=csv sin url") == []


@pytest.mark.parametrize(
    ("change", "key"),
    [
        (lambda t: "version: [\n", "yaml"),
        (lambda t: "- a\n", "not_mapping"),
        (lambda t: t + "extra: 1\n", "unknown_key"),
        (lambda t: t.replace("version: 1", "version: 2"), "version"),
        (lambda t: "version: 1\noutputs: {}\n", "outputs"),
        (lambda t: "version: 1\noutputs:\n  - figures/a.pdf\n", "entry"),
        (lambda t: t.replace("path: figures/flowchart.pdf", "path: ../x.pdf"), "entry_path"),
        (lambda t: t.replace("path: figures/flowchart.pdf", "path: figures/sample-sizes.pdf"), "duplicate"),
        (lambda t: t.replace("path: figures/flowchart.pdf", "path: figures/otra.pdf"), "path_missing"),
        (lambda t: t.replace("    kind: figure\n    generator: manual", "    kind: chart\n    generator: manual"), "kind"),
        (lambda t: t.replace("generator: manual", "generator: tables/x.py"), "generator"),
        (lambda t: t.replace("generator: manual", "generator: analysis/otro.py"), "generator_missing"),
        (lambda t: t.replace('credit: "OpenStreetMap contributors, ODbL"', 'credit: "  "'), "credit"),
        (lambda t: t.replace("    generator: manual\n", "    generator: manual\n    inputs: x\n"), "inputs"),
        (lambda t: t.replace("      - dataset: school-survey", "      - {dataset: school-survey, path: x}"), "input"),
        (lambda t: t.replace("dataset: school-survey", "dataset: otro"), "dataset"),
        (lambda t: t.replace("path: analysis/derived/counts.csv", "path: analysis/derived/otro.csv"), "input_missing"),
    ],
)
def test_provenance_problems(render: Mapping[str, Mapping[str, bytes]], change: Callable[[str], str], key: str) -> None:
    text = change(PROV_EXAMPLE)
    assert text != PROV_EXAMPLE
    findings, keys = problems_of(render, prov_files(text), "provenance")
    invalid = of(findings, "conv.provenance.invalid")
    assert keys == [key] and len(invalid) == 1, (keys, [f.message for f in invalid])
    assert invalid[0].path == SECTION + "provenance.yaml"


def test_provenance_example_lines_and_missing_outputs(render: Mapping[str, Mapping[str, bytes]]) -> None:
    findings = run_on_render(render, prov_files(PROV_EXAMPLE.replace("generator: manual", "generator: analysis/otro.py")))
    [invalid] = of(findings, "conv.provenance.invalid")
    assert invalid.line == 12  # la línea de generator en el ejemplo de 02 §6.4
    # counts.csv de analysis/derived/ no tiene entrada propia en el ejemplo: aviso, no error
    assert [f.path for f in of(findings, "conv.provenance.missing")] == [SECTION + "analysis/derived/counts.csv"]


def test_provenance_that_cannot_be_read(render: Mapping[str, Mapping[str, bytes]]) -> None:
    findings, keys = problems_of(render, {SECTION + "provenance.yaml": b"x" * (5 * MIB + 1)}, "provenance")
    assert keys == ["unreadable"]
    assert [f.path for f in of(findings, "conv.provenance.invalid")] == [SECTION + "provenance.yaml"]
    assert [f.path for f in of(findings, "conv.file.too-large")] == [SECTION + "provenance.yaml"]


def test_notebook_outside_analysis_and_invalid_json() -> None:
    clean = fixture("notebooks", "clean.ipynb")
    findings = evaluate(
        {
            "nb.ipynb": clean,
            "sections/introduction/analysis/x.ipynb": "{no es json",
            "tex/sections/introduction/analysis/y.ipynb": clean,  # D-43: fuera de la parte
        }
    )
    messages = {f.path: f.message for f in of(findings, "conv.notebook.outputs")}
    t = translator("es")
    assert t("convention.notebook.outside") in messages["nb.ipynb"]
    assert t("convention.notebook.invalid") in messages["sections/introduction/analysis/x.ipynb"]
    assert t("convention.notebook.outside") in messages["tex/sections/introduction/analysis/y.ipynb"]


def test_dirty_notebook_lists_every_problem() -> None:
    data = json.loads(fixture("notebooks", "dirty.ipynb"))
    assert convention.notebook_problems(data) == ["widgets", "attachments", "outputs", "execution_count"]
    assert convention.notebook_problems(json.loads(fixture("notebooks", "clean.ipynb"))) == []
    assert convention.notebook_problems({"cells": [1]}) == ["invalid"]
    assert convention.notebook_problems([]) == ["invalid"]


@pytest.mark.parametrize(
    ("change", "key"),
    [
        (sub('convention: "0.1"\n', ""), "missing"),
        (sub('convention: "0.1"', 'convention: "0.2"'), "value"),
        (sub("main: tex/main.tex", "main: tex/otro.tex"), "build_value"),
        (sub("bibliography: biber", "bibliography: bibtex"), "build_value"),
        (sub("    - bib/references.bib", "    - bib/otra.bib"), "build_value"),
        (sub("engine: pdflatex", "engine: context"), "build_engine"),
    ],
)
def test_convention_value_problems(render: Mapping[str, Mapping[str, bytes]], change: Callable[[str], str], key: str) -> None:
    findings, keys = problems_of(render, {"project.yaml": change}, "convention")
    [finding] = of(findings, "conv.convention.value")
    assert keys == [key]
    assert finding.path == "project.yaml" and finding.line is not None


def test_broken_project_yaml_is_left_to_other_rules(render: Mapping[str, Mapping[str, bytes]]) -> None:
    findings = run_on_render(render, {"project.yaml": "convention: [\n"})
    assert of(findings, "conv.convention.value") == []
    # sin project.yaml legible, .latexmkrc se compara con el motor de la base
    assert of(findings, "conv.latexmkrc.noncanonical") == []


@pytest.mark.parametrize(
    ("block", "extra", "problems"),
    [
        (LICENSES_BLOCK, {}, ["no_text"]),
        (LICENSES_BLOCK.replace("CC-BY-4.0", "MIT+"), {}, ["invalid"]),
        (LICENSES_BLOCK.replace("CC-BY-4.0", "LicenseRef-Propia"), {"LICENSES/LicenseRef-Propia.txt": "x\n"}, []),
        (LICENSES_BLOCK.replace("code: MIT-0", "code: 3"), {"LICENSES/CC-BY-4.0.txt": "x\n"}, ["invalid"]),
    ],
)
def test_license_identifiers(
    render: Mapping[str, Mapping[str, bytes]], block: str, extra: dict[str, Change], problems: list[str]
) -> None:
    all_findings, keys = problems_of(render, {"project.yaml": append(block), **extra}, "license")
    findings = of(all_findings, "E_VALIDATION_LICENSE_UNKNOWN")
    assert keys == problems and len(findings) == len(problems)
    for finding in findings:
        assert finding.path == "project.yaml" and finding.line is not None


def test_a_license_text_is_needed_only_with_files_of_its_category(render: Mapping[str, Mapping[str, bytes]]) -> None:
    """00-marco D-64: como REUSE, validate pide el texto de data solo si hay archivos de datos."""
    block = append(LICENSES_BLOCK.replace("CC-BY-4.0", "CC0-1.0").replace("data: CC0-1.0", "data: ODbL-1.0"))
    _findings, keys = problems_of(render, {"project.yaml": block}, "license")
    assert keys == ["no_text"]  # bib/references.bib y los pending.bib son datos
    data = {rel: None for rel in render["es"] if rel == "bib/references.bib" or rel.endswith("/pending.bib")}
    assert data
    findings, keys = problems_of(render, {"project.yaml": block, **data}, "license")
    assert keys == [] and of(findings, "E_VALIDATION_LICENSE_UNKNOWN") == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (append("- x\n" * 120), ["lines"]),
        (append("- " + "y" * 2100 + "\n"), ["section7"]),
        (append(("- " + "z" * 98 + "\n") * 60), ["size", "section7"]),
        (sub("# ", "Sin título "), ["title"]),
        (sub("## 7. ", "## 8. "), ["heading", "sections"]),
        (sub("## 3. ", "## 4. "), ["heading", "heading", "heading", "heading", "heading", "sections"]),
    ],
)
def test_agents_structure_problems(
    render: Mapping[str, Mapping[str, bytes]], change: Callable[[str], str], expected: list[str]
) -> None:
    findings, keys = problems_of(render, {"AGENTS.md": change}, "agents")
    assert keys == expected
    assert len(of(findings, "conv.agents.structure")) == len(expected)


def test_agents_imports_inside_fences_are_ignored(render: Mapping[str, Mapping[str, bytes]]) -> None:
    fenced = "```\n@docs/extra.md\n```\n~~~\n@x.md\n~~~\n"
    assert of(run_on_render(render, {"AGENTS.md": append(fenced)}), "conv.agents.import") == []
    assert convention.imports("@a.md, y @b/c.md.\n`@d.md`") == [(1, "a.md"), (1, "b/c.md")]


def test_agents_modified_compares_with_the_render_of_its_language(render: Mapping[str, Mapping[str, bytes]]) -> None:
    # el AGENTS.md en inglés en un proyecto en español difiere del render de la convención
    findings = run_on_render(render, {"AGENTS.md": render["en"]["AGENTS.md"]})
    assert [f.code for f in findings] == ["conv.agents.modified"] * 6
    assert [f.line for f in findings] == sorted(f.line for f in findings if f.line)


@pytest.mark.parametrize(
    ("change", "expected", "lines"),
    [
        (append("@docs/otro.md\n"), ["import"], [6]),
        (append("x" * 900 + "\n"), ["size"], [None]),
        (sub("@AGENTS.md\n", "\n"), ["first_line"], [1]),
        (sub("@AGENTS.md\n", "# Claude\n@AGENTS.md\n"), ["first_line", "import"], [1, 2]),
    ],
)
def test_claude_content_problems(
    render: Mapping[str, Mapping[str, bytes]],
    change: Callable[[str], str],
    expected: list[str],
    lines: list[int | None],
) -> None:
    findings, keys = problems_of(render, {"CLAUDE.md": change}, "claude")
    assert keys == expected
    assert [f.line for f in of(findings, "conv.claude.content")] == lines


@pytest.mark.parametrize(
    ("extra", "key"),
    [
        ("\n- RQ-1: no va aquí\n", "plan_id"),
        ("\nUsa \\newcommand{\\R}{x} para los reales.\n", "macro"),
        ("\nVer @memory/notes/task-1.md\n", "import"),
        ("\nLa sección introduction la escribe Example-Researcher.\n", "assignment"),
    ],
)
def test_constitution_rules(render: Mapping[str, Mapping[str, bytes]], extra: str, key: str) -> None:
    findings, keys = problems_of(render, {"memory/constitution.md": append(extra)}, "constitution")
    assert keys == [key]
    [finding] = of(findings, "conv.constitution.structure")
    assert finding.line is not None and finding.line > 40


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (sub("## 7. ", "## Siete. "), ["heading", "sections"]),
        (append("x\n" * 120), ["lines"]),
        (append(("x" * 199 + "\n") * 30), ["size"]),
        (append("\n## 8. Otra\n"), ["heading"]),
    ],
)
def test_constitution_size_lines_and_sections(
    render: Mapping[str, Mapping[str, bytes]], change: Callable[[str], str], expected: list[str]
) -> None:
    findings, keys = problems_of(render, {"memory/constitution.md": change}, "constitution")
    assert keys == expected
    assert len(of(findings, "conv.constitution.structure")) == len(expected)


def test_constitution_without_roles_still_checks_the_rest(render: Mapping[str, Mapping[str, bytes]]) -> None:
    files = apply(render["es"], {"roles.yaml": None, "memory/constitution.md": append("\n- RQ-1: no\n")})
    findings = evaluate(files)
    assert "conv.constitution.structure" in codes(findings)


@pytest.mark.parametrize(("engine", "mode"), [("pdflatex", 1), ("xelatex", 5), ("lualatex", 4)])
def test_canonical_latexmkrc(engine: str, mode: int) -> None:
    text = convention.canonical_latexmkrc(engine).decode("utf-8")
    assert f"$pdf_mode = {mode};   # 1 = pdflatex" in text and text.endswith("';\n")
    assert text.count("\n") == 8


def test_latexmkrc_engine_mismatch_and_line(render: Mapping[str, Mapping[str, bytes]]) -> None:
    [finding] = of(
        run_on_render(render, {"project.yaml": sub("engine: pdflatex", "engine: lualatex")}), "conv.latexmkrc.noncanonical"
    )
    assert finding.line == 5 and "lualatex" in finding.message
    assert of(run_on_render(render, {".latexmkrc": None}), "conv.latexmkrc.noncanonical") == []
    crlf = of(run_on_render(render, {".latexmkrc": lambda t: t.replace("\n", "\r\n")}), "conv.latexmkrc.noncanonical")
    assert [f.line for f in crlf] == [1]


def test_engine_default_when_build_has_no_engine(render: Mapping[str, Mapping[str, bytes]]) -> None:
    findings = run_on_render(render, {"project.yaml": sub("  engine: pdflatex            # pdflatex | xelatex | lualatex\n", "")})
    assert findings == []


# -- format: none (D-43; 02 §2.3, REQ-CONV-88 y REQ-CONV-89) ------------------------------------------


@pytest.fixture(scope="module")
def none_render() -> Mapping[str, Mapping[str, bytes]]:
    tool = load_tool("render_template")
    return {lang: tool.render_files(lang, fmt="none") for lang in ("es", "en")}


def run_on_none(
    none_render: Mapping[str, Mapping[str, bytes]], changes: Mapping[str, Change] | None = None, *, lang: Lang = "es"
) -> list[Finding]:
    """La regla sobre el render none con ``changes``; la base y la política son ese render."""
    original = none_render[lang]
    return evaluate(apply(original, changes or {}), lang=lang, policy=snapshot(original), base=original)


@pytest.mark.parametrize("lang", ["es", "en"])
@pytest.mark.parametrize("with_base", [True, False])
def test_none_render_has_no_findings(
    none_render: Mapping[str, Mapping[str, bytes]], lang: Lang, with_base: bool
) -> None:
    """REQ-CONV-3 con format: none: sin tex/, bib/, .latexmkrc ni pending.bib no falta nada."""
    files = none_render[lang]
    assert not any(p.startswith(("tex/", "bib/")) or p == ".latexmkrc" or p.endswith(".tex") for p in files)
    assert evaluate(files, lang=lang, policy=snapshot(files), base=files if with_base else None) == []


def test_format_unused_lists_what_is_left_over(none_render: Mapping[str, Mapping[str, bytes]]) -> None:
    leftovers = {
        "tex/main.tex": "% x\n",
        "bib/references.bib": "% x\n",
        ".latexmkrc": "# x\n",
        SECTION + "pending.bib": "% x\n",
    }
    findings = run_on_none(none_render, leftovers)
    [unused] = of(findings, "conv.format.unused")
    assert (unused.severity, unused.path) == ("warning", "project.yaml")
    lines = none_render["es"]["project.yaml"].decode("utf-8").split("\n")
    assert unused.line == lines.index("format: none") + 1  # la línea que declara el formato
    assert unused.hint is not None
    for item in ("`tex/`", "`bib/`", "`.latexmkrc`", f"`{SECTION}pending.bib`"):
        assert item in unused.hint, item
    # Lo que sobra no estorba (REQ-CONV-88): ni errores ni conv.latexmkrc.* (queda en skip).
    assert not has_errors(findings), [(f.code, f.path) for f in findings]
    assert codes(findings) == ["conv.format.unused"]


def test_format_unused_caps_the_list(none_render: Mapping[str, Mapping[str, bytes]]) -> None:
    many = {f"sections/s{i}/pending.bib": "% x\n" for i in range(9)}
    [unused] = of(run_on_none(none_render, {"tex/main.tex": "% x\n", **many}), "conv.format.unused")
    assert unused.hint is not None and "`tex/`" in unused.hint
    assert unused.hint.count("/pending.bib`") == convention.MAX_UNUSED_SHOWN - 1
    assert "…" in unused.hint and len(unused.hint) <= 500


@pytest.mark.parametrize(
    "path",
    [SECTION + "introduction.tex", SECTION + "figures/plot.tex", SECTION + "tables/t1.tex", SECTION + "drafts/d.tex"],
)
def test_tex_in_a_part_is_a_type_error_with_none(none_render: Mapping[str, Mapping[str, bytes]], path: str) -> None:
    """REQ-CONV-89: con format: none, un .tex en una parte es un tipo no permitido (REQ-CONV-25)."""
    [finding] = of(run_on_none(none_render, {path: "% x\n"}), "conv.section.disallowed-file")
    assert (finding.path, finding.severity) == (path, "error")
    assert "*.tex" not in finding.message


def test_type_table_follows_the_format(
    render: Mapping[str, Mapping[str, bytes]], none_render: Mapping[str, Mapping[str, bytes]]
) -> None:
    """02 §4.2: las tablas en Markdown valen con none y no con latex; analysis/ y notes/ valen igual."""
    extra = {SECTION + "tables/t1.md": "| a |\n|---|\n| 1 |\n", SECTION + "notes/n1.md": "# Nota\n"}
    assert of(run_on_none(none_render, extra), "conv.section.disallowed-file") == []
    latex = of(run_on_render(render, extra), "conv.section.disallowed-file")
    assert [f.path for f in latex] == [SECTION + "tables/t1.md"]


def test_none_requires_the_markdown_text_and_not_latex(none_render: Mapping[str, Mapping[str, bytes]]) -> None:
    """D-36 enmendada: con none se exigen <id>.md, figures/ y tables/; nada de tex/, bib/ ni pending.bib."""
    findings = run_on_none(none_render, {SECTION + "introduction.md": None})
    assert [(f.code, f.path) for f in findings] == [("conv.tree.missing", SECTION + "introduction.md")]
    folders = run_on_none(none_render, {SECTION + "tables/.gitkeep": None})
    assert [(f.code, f.path) for f in folders] == [("conv.tree.missing", SECTION + "tables")]


def test_latex_still_requires_its_files(render: Mapping[str, Mapping[str, bytes]]) -> None:
    findings = run_on_render(render, {SECTION + "pending.bib": None, "tex/notation.tex": None})
    assert sorted(f.path for f in of(findings, "conv.tree.missing")) == [SECTION + "pending.bib", "tex/notation.tex"]
    assert of(run_on_render(render, {SECTION + "introduction.md": "# x\n"}), "conv.tree.missing") == []


@pytest.mark.parametrize(
    ("fmt", "present", "absent"), [("latex", "`pending.bib`", "`.md`"), ("none", "`.md`", "`pending.bib`")]
)
def test_section_missing_hint_follows_the_format(
    render: Mapping[str, Mapping[str, bytes]],
    none_render: Mapping[str, Mapping[str, bytes]],
    fmt: str,
    present: str,
    absent: str,
) -> None:
    changes: dict[str, Change] = {"roles.yaml": ROLES_WITH_RESULTS}
    findings = run_on_render(render, changes) if fmt == "latex" else run_on_none(none_render, changes)
    [missing] = of(findings, "conv.section.missing")
    assert missing.hint is not None and "`sections/appendix/`" in missing.hint
    assert present in missing.hint and absent not in missing.hint


def test_latexmkrc_rule_is_skipped_with_none(none_render: Mapping[str, Mapping[str, bytes]]) -> None:
    findings = run_on_none(none_render, {".latexmkrc": "$do_cd = 0;\n"})
    assert codes(findings) == ["conv.format.unused"]


def test_switching_to_none_is_judged_with_the_new_format(render: Mapping[str, Mapping[str, bytes]]) -> None:
    """03 §2.5, como roles.yaml: un project.yaml del árbol que cambia y es válido manda."""
    switched = run_on_render(render, TO_NONE)
    assert switched == [], [(f.code, f.path) for f in switched]
    # Sin quitar lo de LaTeX: el aviso y el .tex de la parte, que ya no es un tipo permitido.
    half = run_on_render(render, {"project.yaml": NONE_PROJECT})
    assert sorted(set(codes(half))) == ["conv.format.unused", "conv.section.disallowed-file", "conv.tree.missing"]


def test_an_invalid_none_project_falls_back_to_the_base_format(render: Mapping[str, Mapping[str, bytes]]) -> None:
    """format: none con bloque build no es válido (REQ-SCHEMA-90): se juzga con el formato de la base."""
    findings = run_on_render(render, {"project.yaml": sub("format: latex", "format: none")})
    assert of(findings, "conv.format.unused") == [] and not has_errors(findings)


def test_an_unchanged_project_follows_the_base_format(
    render: Mapping[str, Mapping[str, bytes]], none_render: Mapping[str, Mapping[str, bytes]]
) -> None:
    """La base pasó a none y la rama aún no: el árbol se juzga con el formato de la base."""
    files = render["es"]
    findings = evaluate(files, policy=snapshot(none_render["es"]), base=files)
    assert of(findings, "conv.format.unused") != []
    assert (SECTION + "introduction.md") in {f.path for f in of(findings, "conv.tree.missing")}
    assert (SECTION + "introduction.tex") in {f.path for f in of(findings, "conv.section.disallowed-file")}


def test_tree_format_resolution(
    render: Mapping[str, Mapping[str, bytes]], none_render: Mapping[str, Mapping[str, bytes]]
) -> None:
    latex, none = render["es"], none_render["es"]
    Files = Mapping[str, bytes] | None

    def fmt(tree: Files, base: Files, policy: Files) -> str:
        inp = RuleInput(
            mode="pr", t=translator("es"), now=NOW,
            policy=snapshot(policy) if policy is not None else None,
            tree=DictTree(tree) if tree is not None else None,
            base_tree=DictTree(base) if base is not None else None,
        )
        return convention.tree_format(inp)

    assert fmt(none, latex, latex) == "none"  # el árbol lo cambia y es válido
    assert fmt(latex, latex, none) == "none"  # el árbol no lo cambia: manda la base
    assert fmt(none, None, None) == "none"  # sin base ni política, el del árbol
    assert fmt(None, None, none) == "none"  # PR desde un fork: solo la política
    assert fmt({"README.md": b"x\n"}, None, None) == "latex"  # sin project.yaml válido
    broken = apply(none, {"project.yaml": "format: [\n"})
    assert fmt(broken, latex, latex) == "latex"  # roto: el de la base


def test_required_paths_filtered_by_format() -> None:
    required = convention.required_paths()
    latex, none = required.for_format("latex"), required.for_format("none")
    assert "sections/{id}/{id}.md" not in latex.per_section
    assert {"sections/{id}/{id}.tex", "sections/{id}/pending.bib"} <= set(latex.per_section)
    assert none.per_section == ("sections/{id}/{id}.md",)
    assert none.per_section_dirs == latex.per_section_dirs == ("sections/{id}/figures", "sections/{id}/tables")
    assert not any(p.startswith(("tex/", "bib/")) or p == ".latexmkrc" for p in none.fixed)
    assert set(latex.fixed) - set(none.fixed) == {
        ".latexmkrc", "bib/references.bib", "tex/guide.md", "tex/main.tex", "tex/notation.tex", "tex/preamble.tex",
        ".github/workflows/editor-review.yml", ".github/workflows/editor-review-trigger.yml",  # 09-ci REQ-CI-57
    }


# -- funciones puras --------------------------------------------------------------------------------


def test_needs_provenance_and_section_patterns() -> None:
    assert convention.needs_provenance("figures/a.pdf")
    assert convention.needs_provenance("tables/a.csv")
    assert convention.needs_provenance("analysis/derived/x/y.json")
    assert not convention.needs_provenance("figures/a.tex")
    assert not convention.needs_provenance("figures/.gitkeep")
    assert not convention.needs_provenance("a.pdf")
    assert not convention.needs_provenance("analysis/run.py")
    assert "*.R" in convention.section_patterns(["analysis", "x.R"])
    assert convention.section_patterns(["analysis", "derived", "x.csv"]) == ("*.csv", "*.tsv", "*.json", ".gitkeep")
    assert convention.section_patterns(["otra", "x.tex"]) == ("*.tex", "*.md", ".gitkeep")
    assert convention.section_patterns(["notes", "x.md"]) == ("*.md", "*.txt", ".gitkeep")
    assert "*.md" in convention.section_patterns(["x.md"]) and "pending.bib" in convention.section_patterns(["x"])
    assert not convention.matches("x.r", "*.R")  # distingue mayúsculas (REQ-CONV-8)
    # Columna format: none de 02 §4.2: ningún .tex ni pending.bib; las tablas, en CSV o Markdown.
    assert convention.section_patterns(["x.md"], "none") == ("*.md", "provenance.yaml", ".gitkeep")
    figures = ("*.pdf", "*.png", "*.jpg", "*.jpeg", ".gitkeep")
    assert convention.section_patterns(["figures", "a.pdf"], "none") == figures
    assert convention.section_patterns(["tables", "t.md"], "none") == ("*.csv", "*.md", ".gitkeep")
    assert convention.section_patterns(["otra", "x.md"], "none") == ("*.md", ".gitkeep")
    for parts in (["analysis", "x.R"], ["analysis", "derived", "x.csv"], ["notes", "x.md"]):
        assert convention.section_patterns(parts, "none") == convention.section_patterns(parts)


def test_section_rel() -> None:
    assert convention.section_rel("figures/a.pdf") == "figures/a.pdf"
    for bad in (None, "", "/a", "a\\b", "a/../b", "./a", "a//b", 3):
        assert convention.section_rel(bad) is None


def test_numbered_sections_and_headings_ignore_code() -> None:
    text = "# T\n\n## 1. Uno\na\n```\n## 2. falso\n```\n## 3. Tres\nb\n## Libre\n"
    sections, bad = convention.numbered_sections(text)
    assert sections == {1: (3, 7)}
    assert [h.line for h in bad] == [8, 10]
    assert convention.block(text, (3, 4)) == "## 1. Uno\na"
    assert convention.line_count("") == 0 and convention.line_count("a") == 1 and convention.line_count("a\n") == 1


def test_parse_required_paths_rejects_bad_shapes() -> None:
    good = {"milestone": "H2", "fixed": ["a"], "per_section": [], "per_section_dirs": []}
    assert convention.parse_required_paths(json.dumps(good)).fixed == ("a",)
    for bad in ([], {**good, "milestone": 2}, {**good, "fixed": "a"}, {**good, "per_section": [""]}):
        with pytest.raises(ValueError):
            convention.parse_required_paths(json.dumps(bad))


def test_parse_required_paths_reads_the_format_filter() -> None:
    """D-36 enmendada: ``formats`` es ruta -> formatos que la exigen; sin entrada, todos."""
    good = {"milestone": "H2", "fixed": ["a", "b"], "per_section": ["s/{id}"], "per_section_dirs": [],
            "formats": {"b": ["latex"], "s/{id}": ["none"]}}
    parsed = convention.parse_required_paths(json.dumps(good))
    assert parsed.for_format("latex").fixed == ("a", "b") and parsed.for_format("latex").per_section == ()
    assert parsed.for_format("none").fixed == ("a",) and parsed.for_format("none").per_section == ("s/{id}",)
    for formats in ([], {"c": ["latex"]}, {"b": []}, {"b": ["markdown"]}, {"b": "latex"}):
        with pytest.raises(ValueError):
            convention.parse_required_paths(json.dumps({**good, "formats": formats}))


def test_reference_agents_without_template(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    convention.reference_agents.cache_clear()
    monkeypatch.setattr(convention.datafiles, "data_path", lambda kind: tmp_path)
    try:
        assert convention.reference_agents("es") is None
    finally:
        convention.reference_agents.cache_clear()


# -- falla cerrada y localización ------------------------------------------------------------------


def test_without_tree_the_rule_is_unavailable() -> None:
    [finding] = RULE.run(RuleInput(mode="pr", t=translator("es"), now=NOW, tree=None))
    assert finding.code == UNAVAILABLE and finding.severity == "error" and finding.check == "convention"


def test_a_failing_check_does_not_stop_the_others(
    render: Mapping[str, Mapping[str, bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(run: object) -> None:
        raise RuntimeError("secreto que no debe salir")

    checks = tuple((name, boom if name == "tree" else check) for name, check in convention.CHECKS)
    monkeypatch.setattr(convention, "CHECKS", checks)
    findings = run_on_render(render, {"GEMINI.md": "x\n"})
    assert codes(findings) == [UNAVAILABLE, "conv.path.foreign-instructions"]
    assert all("secreto" not in (f.message + (f.hint or "")) for f in findings)


@pytest.mark.parametrize("lang", ["es", "en"])
def test_messages_are_localized(render: Mapping[str, Mapping[str, bytes]], lang: Lang) -> None:
    [finding] = run_on_render(render, {"plan.md": None}, lang=lang)
    catalog = load_catalog(lang)
    assert finding.message == catalog["finding.conv.tree.missing.message"].format(file="plan.md")
    assert finding.hint == catalog["finding.conv.tree.missing.hint"]
