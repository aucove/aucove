# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Manifiesto de la plantilla y archivos obligatorios (02 §2.2, 12 §1.2, D-35 j, D-36).

Sin la spec rigen los archivos versionados: la tabla de expansión se contrasta con una
copia de la columna «Ruta» y «Origen» de 02 §2.2 escrita aquí. Con ``AUCOVE_SPEC_DIR``,
esa copia se compara con la spec y ``gen_template_manifest --check`` confirma que el
manifiesto y ``required-paths.json`` versionados son los regenerados.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest

from aucove import _meta
from aucove.config import datafiles
from tests.conftest import FIXTURES, REPO_ROOT, load_tool

SPEC_ENV = _meta.ENV_PREFIX + "SPEC_DIR"
TABLE_FILE = REPO_ROOT / "tools" / "manifest_expand.toml"
MANIFEST = FIXTURES / "template-manifest.txt"
REQUIRED = REPO_ROOT / "src" / _meta.NAME / "_data" / "required-paths.json"

# Copia de 02-convencion §2.2: ítems de la columna Ruta de cada fila y letras de su Origen.
SPEC_ROWS: list[tuple[tuple[str, ...], frozenset[str]]] = [
    (("README.md",), frozenset("TI")),
    (("AGENTS.md",), frozenset("TI")),
    (("CLAUDE.md",), frozenset("TI")),
    (("LICENSE", "LICENSES/**", "REUSE.toml"), frozenset("TI")),
    (("project.yaml",), frozenset("TI")),
    (("roles.yaml",), frozenset("TI")),
    (("plan.md",), frozenset("TI")),
    ((".gitignore", ".gitattributes"), frozenset("T")),
    ((".latexmkrc",), frozenset("T")),
    ((".mcp.json",), frozenset("TI")),
    ((".claude/settings.json",), frozenset("TIN")),
    ((".claude/skills/**", ".agents/skills/**"), frozenset("TIN")),
    ((".gemini/settings.json", ".codex/config.toml"), frozenset("I")),
    ((".devcontainer/devcontainer.json",), frozenset("T")),
    ((".github/CODEOWNERS",), frozenset("IN")),
    ((".github/ISSUE_TEMPLATE/task.yml",), frozenset("IN")),
    ((".github/ISSUE_TEMPLATE/config.yml", ".github/pull_request_template.md"), frozenset("TI")),
    ((".github/workflows/**",), frozenset("T")),
    (("memory/constitution.md",), frozenset("TI")),
    (("memory/decisions/.gitkeep", "memory/notes/.gitkeep"), frozenset("T")),
    (("memory/decisions/<archivo>.md",), frozenset("P")),
    (("memory/notes/task-<n>.md",), frozenset("P")),
    (("bib/references.bib",), frozenset("TP")),
    (("data/README.md",), frozenset("TI")),
    (("tex/main.tex",), frozenset("TI")),
    (("tex/preamble.tex",), frozenset("TI")),
    (("tex/notation.tex",), frozenset("T")),
    # D-56: el esqueleto del editor.
    (("tex/outline/<id>.tex",), frozenset("TI")),
    (("tex/guide.md",), frozenset("TI")),
    (("tex/assets/**",), frozenset("P")),
    # D-43: la carpeta de cada parte está en la raíz; <id>.md es de la variante format: none.
    (("sections/<id>/<id>.tex",), frozenset("TI")),
    (("sections/<id>/<id>.md",), frozenset("I")),
    (("sections/<id>/pending.bib",), frozenset("TI")),
    (("sections/<id>/figures/**", "tables/**"), frozenset("TIP")),
    (("sections/<id>/analysis/**", "provenance.yaml"), frozenset("P")),
    (("sections/<id>/notes/**",), frozenset("P")),
]

# Hitos de 12-mvp §1.2 por ruta concreta; el resto de rutas de la plantilla van en H2.
# 09-ci §2: vencimiento (expire-claims) y main-broken en H3.
# Los del editor del artículo (09-ci §4.5; D-56) son de H3 y solo van con format: latex.
EDITOR_WORKFLOWS = {".github/workflows/editor-review.yml", ".github/workflows/editor-review-trigger.yml"}
MILESTONE_OVERRIDES = {
    ".github/workflows/main-broken.yml": "H3",
    ".github/workflows/expire-claims.yml": "H3",
    **{path: "H3" for path in EDITOR_WORKFLOWS},
    ".mcp.json": "H3",
    ".claude/settings.json": "H4",
    ".devcontainer/devcontainer.json": "H4",
}
H2_WORKFLOWS = {
    ".github/workflows/build.yml",
    ".github/workflows/compile.yml",
    ".github/workflows/seal.yml",  # D-36 y D-50 (c): obligatorio desde H2
    ".github/workflows/validate.yml",
}
PENDING_ITEMS_H4 = {".claude/skills/**", ".agents/skills/**"}  # 00-marco §5.8, sin rutas hasta H4
# D-36: lo que H3 todavía no exige (H4) y los textos de LICENSES/, que elige init (11 §7.3).
NOT_REQUIRED_IN_H3 = (".claude/", ".agents/", ".devcontainer/", "LICENSES/")
# 12-mvp §1.2: lo que llega en H3.
H3_PATHS = {".mcp.json", ".github/workflows/main-broken.yml", ".github/workflows/expire-claims.yml",
            *EDITOR_WORKFLOWS}
# D-43 y D-36 enmendada: ítems que solo existen y se exigen con un formato.
LATEX_ONLY_ITEMS = {
    ".latexmkrc", "bib/references.bib", "tex/main.tex", "tex/preamble.tex", "tex/notation.tex", "tex/assets/**",
    "tex/outline/<id>.tex", "tex/guide.md", "sections/<id>/<id>.tex", "sections/<id>/pending.bib",
}
NONE_ONLY_ITEMS = {"sections/<id>/<id>.md"}
# REQ-MVP-4 y REQ-ARCH-100: lo que cambia del manifiesto de la variante latex a la none.
DEFAULT_PARTS: tuple[str, ...] = (
    "introduction", "background", "theory", "methodology", "experiments", "results", "conclusions",
)  # 00-marco D-55
LATEX_ONLY_PATHS = {
    ".latexmkrc", "bib/references.bib", "tex/main.tex", "tex/preamble.tex", "tex/notation.tex", "tex/guide.md",
    *(f"tex/outline/{p}.tex" for p in DEFAULT_PARTS),  # D-56: en la plantilla, una sección por parte
    *(f"sections/{p}/{p}.tex" for p in DEFAULT_PARTS), *(f"sections/{p}/pending.bib" for p in DEFAULT_PARTS),
    *EDITOR_WORKFLOWS,  # 09-ci REQ-CI-57
}
NONE_ONLY_PATHS = {f"sections/{p}/{p}.md" for p in DEFAULT_PARTS}


@pytest.fixture(scope="module")
def gen():  # type: ignore[no-untyped-def]
    return load_tool("gen_template_manifest")


@pytest.fixture(scope="module")
def table() -> dict:
    return tomllib.loads(TABLE_FILE.read_text(encoding="utf-8"))


def spec_dir() -> Path | None:
    value = os.environ.get(SPEC_ENV)
    return Path(value) if value else None


def rows(gen):  # type: ignore[no-untyped-def]
    return [gen.Row(items, origin) for items, origin in SPEC_ROWS]


def test_snapshot_matches_the_spec_when_available(gen) -> None:  # type: ignore[no-untyped-def]
    spec = spec_dir()
    if spec is None:
        pytest.skip(f"{SPEC_ENV} no está definido")
    parsed = gen.parse_table((spec / "02-convencion.md").read_text(encoding="utf-8-sig"))
    assert [(row.items, row.origin) for row in parsed] == SPEC_ROWS


def test_every_path_item_appears_exactly_once(table: dict) -> None:
    specs = [item["spec"] for item in table["item"]]
    expected = [name for items, _origin in SPEC_ROWS for name in items]
    assert len(specs) == len(set(specs))
    assert sorted(specs) == sorted(expected)


def test_table_passes_the_generator_checks(gen, table: dict) -> None:  # type: ignore[no-untyped-def]
    gen.check_table(rows(gen), table)


def test_in_template_follows_origin_or_d35(table: dict) -> None:
    origin = {name: org for items, org in SPEC_ROWS for name in items}
    for item in table["item"]:
        spec = item["spec"]
        if item.get("amendment") == "D-35":
            assert spec in {".github/CODEOWNERS", ".github/ISSUE_TEMPLATE/task.yml"}
            assert "T" not in origin[spec] and item["in_template"] is True
        else:
            assert item["in_template"] is ("T" in origin[spec]), spec
        if not item["in_template"] and spec not in NONE_ONLY_ITEMS:
            assert item["paths"] == [] and item["required"] is False, spec


def test_every_expanded_path_matches_its_glob(gen, table: dict) -> None:  # type: ignore[no-untyped-def]
    sections = table["sections_example"]
    assert sections == list(DEFAULT_PARTS)  # REQ-CONV-1; 00-marco D-55
    seen: set[str] = set()
    for item in table["item"]:
        for entry in item["paths"]:
            for section in sections if "<id>" in entry["path"] else sections[:1]:
                pattern = gen.glob_regex(item.get("glob", item["spec"]).replace("<id>", section))
                concrete = entry["path"].replace("<id>", section)
                assert pattern.fullmatch(concrete), (item["spec"], entry["path"])
                assert concrete not in seen
                seen.add(concrete)


def test_milestones_follow_12_1_2(table: dict) -> None:
    by_path = {entry["path"]: entry["milestone"] for item in table["item"] for entry in item["paths"]}
    workflows = {p for p in by_path if p.startswith(".github/workflows/")}
    assert workflows == H2_WORKFLOWS | EDITOR_WORKFLOWS | {
        ".github/workflows/main-broken.yml", ".github/workflows/expire-claims.yml"}
    for path, milestone in by_path.items():
        assert milestone == MILESTONE_OVERRIDES.get(path, "H2"), path
    early = {entry["path"] for item in table["item"] for entry in item["paths"] if entry.get("early")}
    assert early == set()  # con la tabla en H3, ninguna ruta llega antes de su hito
    pending = {item["spec"]: item.get("milestone") for item in table["item"] if item["in_template"] and not item["paths"]}
    assert pending == {spec: "H4" for spec in PENDING_ITEMS_H4}


def test_licenses_follow_11_7_2(table: dict) -> None:
    by_spec = {item["spec"]: item for item in table["item"]}
    assert [e["path"] for e in by_spec["LICENSES/**"]["paths"]] == ["LICENSES/CC0-1.0.txt", "LICENSES/MIT-0.txt"]
    assert by_spec["LICENSES/**"]["required"] is False  # init elige las licencias (11 §7.3)
    assert by_spec["LICENSE"]["required"] is True
    assert by_spec["REUSE.toml"]["required"] is True


def test_versioned_files_are_the_build_of_the_table(gen, table: dict) -> None:  # type: ignore[no-untyped-def]
    manifest, required = gen.build(table, table["milestone"])
    assert MANIFEST.read_bytes() == gen.manifest_bytes(manifest)
    assert REQUIRED.read_bytes() == gen.required_bytes(required)


def test_manifest_format() -> None:
    data = MANIFEST.read_bytes()
    assert data.endswith(b"\n") and b"\r" not in data and not data.startswith(b"\xef\xbb\xbf")
    lines = data.decode("utf-8").splitlines()
    assert lines == sorted(set(lines))
    assert all(line and not line.startswith("/") and "\\" not in line for line in lines)
    assert ".github/CODEOWNERS" in lines and ".github/ISSUE_TEMPLATE/task.yml" in lines  # D-35 j


def test_required_paths_of_h3(table: dict) -> None:
    required = json.loads(datafiles.required_paths_file().read_text(encoding="utf-8"))
    assert list(required) == ["milestone", "fixed", "per_section", "per_section_dirs", "formats"]
    assert required["milestone"] == table["milestone"] == "H3"
    assert required["per_section"] == ["sections/{id}/{id}.tex", "sections/{id}/{id}.md", "sections/{id}/pending.bib"]
    assert required["per_section_dirs"] == ["sections/{id}/figures", "sections/{id}/tables"]
    # D-36 enmendada: con none no se exigen tex/, bib/, .latexmkrc ni pending.bib, y sí <id>.md.
    assert required["formats"] == {
        ".latexmkrc": ["latex"],
        "bib/references.bib": ["latex"],
        "sections/{id}/pending.bib": ["latex"],
        "sections/{id}/{id}.md": ["none"],
        "sections/{id}/{id}.tex": ["latex"],
        "tex/guide.md": ["latex"],  # REQ-CONV-95
        "tex/main.tex": ["latex"],
        "tex/notation.tex": ["latex"],
        "tex/preamble.tex": ["latex"],
        **{path: ["latex"] for path in EDITOR_WORKFLOWS},  # 09-ci REQ-CI-57
    }
    # D-43: ninguna ruta obligatoria vive en la carpeta anterior de las partes.
    assert not any("tex/sections" in p for p in required["fixed"] + required["per_section"])
    for path in required["fixed"]:
        assert not path.startswith(NOT_REQUIRED_IN_H3), path  # D-36
    manifest = set(MANIFEST.read_text(encoding="utf-8").splitlines())
    assert set(required["fixed"]) <= manifest
    assert H2_WORKFLOWS | H3_PATHS <= set(required["fixed"])
    assert {"AGENTS.md", "project.yaml", "roles.yaml", ".github/CODEOWNERS"} <= set(required["fixed"])
    raw = REQUIRED.read_bytes()
    assert raw.endswith(b"\n") and b"\r" not in raw


def test_the_manifest_leaves_out_later_milestones(gen, table: dict) -> None:  # type: ignore[no-untyped-def]
    """REQ-MVP-4: el manifiesto de cada hito filtra por la columna «Hito» de 12-mvp §1.2."""
    h2, required_h2 = gen.build(table, "H2")
    h3, _required = gen.build(table, "H3")
    assert set(h3) - set(h2) == H3_PATHS and ".mcp.json" not in required_h2["fixed"]
    for path in h3:
        assert not path.startswith((".claude/", ".agents/", ".devcontainer/")), path  # H4
    with pytest.raises(gen.ManifestError, match="skills"):
        gen.build(table, "H4")  # H4 debe expandir los skills antes de generar


def test_check_table_detects_inconsistencies(gen, table: dict) -> None:  # type: ignore[no-untyped-def]
    def broken(mutate) -> dict:  # type: ignore[no-untyped-def]
        copy = json.loads(json.dumps(table))
        mutate(copy)
        return copy

    cases = [
        lambda t: t["item"].pop(0),  # falta un ítem
        lambda t: t["item"].append(dict(t["item"][0])),  # ítem repetido
        lambda t: t["item"][0].update(in_template=False),  # contradice el origen
        lambda t: t["item"][0]["paths"].append({"path": "OTHER.md", "milestone": "H2"}),  # fuera del glob
        lambda t: t["item"][0]["paths"][0].update(milestone="X2"),  # hito inválido
        lambda t: t["item"][0].update(amendment="D-35"),  # D-35 con origen T
    ]
    for mutate in cases:
        with pytest.raises(gen.ManifestError):
            gen.check_table(rows(gen), broken(mutate))


def test_format_filter_follows_d43(gen, table: dict) -> None:  # type: ignore[no-untyped-def]
    """D-43 y D-36 enmendada: qué ítems existen solo con un formato."""
    by_formats = {item["spec"]: gen.item_formats(item) for item in table["item"]}
    assert {spec for spec, formats in by_formats.items() if formats == ("latex",)} == LATEX_ONLY_ITEMS
    assert {spec for spec, formats in by_formats.items() if formats == ("none",)} == NONE_ONLY_ITEMS
    assert all(formats in (("latex",), ("none",), ("latex", "none")) for formats in by_formats.values())
    md = next(item for item in table["item"] if item["spec"] == "sections/<id>/<id>.md")
    assert md["in_template"] is False and md["required"] is True  # origen I: la produce el render none


def test_none_manifest_is_the_latex_one_without_the_manuscript(gen, table: dict) -> None:  # type: ignore[no-untyped-def]
    """REQ-MVP-4: la variante none excluye tex/**, bib/**, .latexmkrc y pending.bib e incluye <id>.md."""
    latex, required = gen.build(table, table["milestone"])
    none, required_none = gen.build(table, table["milestone"], "none")
    assert required_none == required  # required-paths no depende del render
    assert set(latex) - set(none) == LATEX_ONLY_PATHS
    assert set(none) - set(latex) == NONE_ONLY_PATHS
    assert none == sorted(none)
    with pytest.raises(gen.ManifestError):
        gen.build(table, table["milestone"], "markdown")


def test_entry_formats_and_early(gen, table: dict) -> None:  # type: ignore[no-untyped-def]
    """Una ruta puede tener sus formatos (un subconjunto de los del ítem) y llegar antes de su hito."""
    item = next(i for i in table["item"] if i["spec"] == ".github/workflows/**")
    entry = next(e for e in item["paths"] if e["path"] == ".github/workflows/editor-review.yml")
    assert gen.entry_formats(item, entry) == ("latex",) and gen.in_render(item, "none", entry) is False
    assert gen.delivered(entry, 3) and not gen.delivered(entry, 2) and gen.delivered({**entry, "early": True}, 2)
    copy = json.loads(json.dumps(table))
    target = next(i for i in copy["item"] if i["spec"] == "tex/main.tex")
    target["paths"][0]["formats"] = ["none"]  # no es un subconjunto de los del ítem
    with pytest.raises(gen.ManifestError):
        gen.build(copy, "H2")
    target["paths"][0].pop("formats")
    target["paths"][0]["early"] = "yes"
    with pytest.raises(gen.ManifestError):
        gen.build(copy, "H2")


def test_check_table_detects_format_inconsistencies(gen, table: dict) -> None:  # type: ignore[no-untyped-def]
    def broken(spec: str, **changes: object) -> dict:
        copy = json.loads(json.dumps(table))
        next(item for item in copy["item"] if item["spec"] == spec).update(changes)
        return copy

    cases = [
        broken("README.md", formats=["none"]),  # la plantilla publicada es la variante latex
        broken("tex/assets/**", formats=["latex"], required=True),  # no viene en la plantilla
        broken("sections/<id>/<id>.md", paths=[]),  # obligatorio con none, sin rutas
        broken("sections/<id>/<id>.md", formats=["latex", "none"]),  # vale para latex sin venir en ella
        broken("README.md", formats=["quarto"]),
        broken("README.md", formats=["latex", "latex"]),
        broken("README.md", formats=[]),
    ]
    for case in cases:
        with pytest.raises(gen.ManifestError):
            gen.check_table(rows(gen), case)


def test_parse_table_reads_a_small_table(gen) -> None:  # type: ignore[no-untyped-def]
    md = (
        "## 2\n\n### 2.2 Tabla por archivo\n\n"
        "| Ruta | Clase | Origen |\n|---|---|---|\n"
        "| `a.md`, `b/**` | `lead` | T (`x`), I |\n"
        "| `c|d.md` | `lead` | P |\n\n"
        "- nota\n\n### 2.3 Otra\n\n| Ruta | Origen |\n|---|---|\n| `z` | T |\n"
    )
    parsed = gen.parse_table(md)
    assert [(r.items, r.origin) for r in parsed] == [(("a.md", "b/**"), frozenset("TI")), (("c|d.md",), frozenset("P"))]
    with pytest.raises(gen.ManifestError):
        gen.parse_table("sin tabla\n")


def test_generator_check_against_the_spec(gen, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    spec = spec_dir()
    if spec is None:
        pytest.skip(f"{SPEC_ENV} no está definido")
    assert gen.main(["--spec-dir", str(spec), "--check"]) == 0
    # Y detecta un archivo desactualizado.
    stale = tmp_path / "manifest.txt"
    stale.write_bytes(MANIFEST.read_bytes() + b"extra\n")
    assert gen.main(["--spec-dir", str(spec), "--check", "--manifest", str(stale)]) == 1
    # Regenerar en otra ruta da los mismos bytes.
    out_manifest, out_required = tmp_path / "m.txt", tmp_path / "r.json"
    assert gen.main(["--spec-dir", str(spec), "--manifest", str(out_manifest), "--required", str(out_required)]) == 0
    assert out_manifest.read_bytes() == MANIFEST.read_bytes()
    assert out_required.read_bytes() == REQUIRED.read_bytes()


def test_generator_without_spec_fails_closed(gen, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv(SPEC_ENV, raising=False)
    assert gen.main(["--check"]) == 1
    assert gen.main(["--spec-dir", str(tmp_path), "--check"]) == 1  # sin 02-convencion.md
