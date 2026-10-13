# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``render_template`` y ``normalize_text`` (01-arquitectura §10.5, REQ-ARCH-82..84)."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

from aucove.i18n import LANGS
from aucove.render.template import (
    RenderError,
    in_format,
    is_text,
    match_source,
    normalize_text,
    project_yaml_without_manuscript,
    render_template,
    template_sources,
)
from tests.conftest import REPO_ROOT

BINARY = b"\x89PNG\r\n\x1a\n\x00\x00{{var:name}}\xff\xfe\r\n"


def make_template(root: Path, files: dict[str, bytes]) -> Path:
    """Crea ``root/template`` con rutas ``base/...`` y ``l10n/<lang>/...``."""
    template = root / "template"
    for rel, data in files.items():
        target = template / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return template


@pytest.fixture
def template(tmp_path: Path) -> Path:
    return make_template(
        tmp_path,
        {
            "base/project.yaml": b"name: {{var:name}}\r\nlanguage: {{var:language}}\r\n",
            "base/.github/workflows/ci.yml": (
                b"sha: ${{ github.sha }}\nvar: ${{ vars.NAME }}\ndollar: ${{var:name}}\npin: {{var:gitleaks_sha256}}\n"
            ),
            "base/assets/logo.png": BINARY,
            "base/memory/notes/.gitkeep": b"",
            "l10n/es/README.md": b"\xef\xbb\xbf# {{var:title}}\n\nHola.\n\n\n",
            "l10n/en/README.md": b"# {{var:title}}\n\nHello.",
        },
    )


VARS = {"name": "demo", "language": "es", "title": "Título", "gitleaks_sha256": "ab" * 32}


def test_normalize_text() -> None:
    assert normalize_text(b"\xef\xbb\xbfa\r\nb\rc") == b"a\nb\nc\n"
    assert normalize_text(b"a\n\n\n") == b"a\n"
    assert normalize_text(b"a  \n") == b"a  \n"
    assert normalize_text(b"") == b""
    assert normalize_text(b"\n\r\n") == b""
    assert normalize_text(normalize_text(b"x\r\n\r\n")) == b"x\n"


def test_is_text_detects_utf8_without_nul() -> None:
    assert is_text("año\n".encode("utf-8"))
    assert not is_text(b"a\x00b")
    assert not is_text(b"\xff\xfe")
    assert not is_text(BINARY)


def test_l10n_of_the_language_is_applied_over_base(template: Path) -> None:
    es = render_template(template, "es", VARS)
    en = render_template(template, "en", VARS)
    assert list(es) == sorted(es)
    assert set(es) == {"project.yaml", ".github/workflows/ci.yml", "assets/logo.png", "memory/notes/.gitkeep", "README.md"}
    assert es["README.md"] == "# Título\n\nHola.\n".encode("utf-8")
    assert en["README.md"] == "# Título\n\nHello.\n".encode("utf-8")
    assert es["project.yaml"] == en["project.yaml"] == b"name: demo\nlanguage: es\n"


def test_binaries_and_actions_expressions_are_untouched(template: Path) -> None:
    out = render_template(template, "en", VARS)
    assert out["assets/logo.png"] == BINARY
    # Las expresiones válidas de Actions quedan intactas; un '$' delante no oculta
    # un marcador {{var:...}} (01 §10.2, REQ-ARCH-80).
    assert out[".github/workflows/ci.yml"] == (
        b"sha: ${{ github.sha }}\nvar: ${{ vars.NAME }}\ndollar: $demo\npin: " + b"ab" * 32 + b"\n"
    )
    assert out["memory/notes/.gitkeep"] == b""  # REQ-CONV-4


def test_text_is_normalized_to_lf(template: Path) -> None:
    for rel, data in render_template(template, "es", VARS).items():
        if rel == "assets/logo.png" or not data:
            continue
        assert b"\r" not in data, rel
        assert not data.startswith(b"\xef\xbb\xbf"), rel
        assert data.endswith(b"\n") and not data.endswith(b"\n\n"), rel


def test_missing_variable_is_a_render_error(template: Path) -> None:
    variables = dict(VARS)
    del variables["title"]
    with pytest.raises(RenderError, match="title"):
        render_template(template, "es", variables)


@pytest.mark.parametrize(
    "text",
    [
        b"x {{var:bad.name}}\n",
        b"x {{var:}}\n",
        b"x {{tool:check_path}}\n",
        b"{{skill:start}}\n",
        b"x ${{tool:check_path}}\n",
        b"x ${{var:bad.name}}\n",
    ],
)
def test_malformed_or_foreign_markers_are_render_errors(tmp_path: Path, text: bytes) -> None:
    template = make_template(tmp_path, {"base/a.md": text, "l10n/es/b.md": b"b\n", "l10n/en/b.md": b"b\n"})
    with pytest.raises(RenderError):
        render_template(template, "es", VARS)


def test_values_are_not_rescanned(tmp_path: Path) -> None:
    template = make_template(tmp_path, {"base/a.md": b"{{var:name}}\n", "l10n/es/b.md": b"b\n", "l10n/en/b.md": b"b\n"})
    assert render_template(template, "es", {"name": "{{var:other}}"})["a.md"] == b"{{var:other}}\n"


def test_l10n_crossing_base_is_rejected(tmp_path: Path) -> None:
    template = make_template(tmp_path, {"base/README.md": b"a\n", "l10n/es/README.md": b"b\n", "l10n/en/README.md": b"c\n"})
    with pytest.raises(RenderError, match="REQ-ARCH-83"):
        render_template(template, "es", VARS)


def test_l10n_languages_must_have_the_same_paths(tmp_path: Path) -> None:
    template = make_template(tmp_path, {"base/a.md": b"a\n", "l10n/es/b.md": b"b\n", "l10n/en/c.md": b"c\n"})
    with pytest.raises(RenderError, match="REQ-ARCH-83"):
        render_template(template, "en", VARS)


def test_missing_language_or_base_is_rejected(tmp_path: Path) -> None:
    only_es = make_template(tmp_path / "one", {"base/a.md": b"a\n", "l10n/es/b.md": b"b\n"})
    with pytest.raises(RenderError):
        render_template(only_es, "en", VARS)
    no_base = make_template(tmp_path / "two", {"l10n/es/b.md": b"b\n", "l10n/en/b.md": b"b\n"})
    with pytest.raises(RenderError):
        render_template(no_base, "es", VARS)
    with pytest.raises(RenderError):
        render_template(only_es, "fr", VARS)  # type: ignore[arg-type]


def test_extra_is_added_verbatim(template: Path) -> None:
    extra = {".github/CODEOWNERS": b"# x\n*   @ana\n"}
    out = render_template(template, "es", VARS, extra=extra)
    assert out[".github/CODEOWNERS"] == extra[".github/CODEOWNERS"]
    assert list(out) == sorted(out)


@pytest.mark.parametrize(
    "extra",
    [
        {"project.yaml": b"x\n"},
        {"PROJECT.yaml": b"x\n"},
        {"../escape": b"x\n"},
        {"/abs": b"x\n"},
        {"a\\b": b"x\n"},
        {"c:/x": b"x\n"},
        {"a//b": b"x\n"},
        {"ok.txt": "not bytes"},
    ],
)
def test_bad_extra_is_rejected(template: Path, extra: dict) -> None:
    with pytest.raises(RenderError):
        render_template(template, "es", VARS, extra=extra)


def test_symlinks_are_rejected(template: Path) -> None:
    link = template / "base" / "link.md"
    try:
        os.symlink(template / "base" / "project.yaml", link)
    except (OSError, NotImplementedError):
        pytest.skip("no se pueden crear enlaces simbólicos en este sistema")
    with pytest.raises(RenderError):
        render_template(template, "es", VARS)


def test_links_are_rejected_without_following_them(template: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Igual que la anterior, pero sin crear el enlace (en Windows hace falta un permiso)."""
    fake = template / "base" / "assets" / "logo.png"
    real_islink = os.path.islink
    monkeypatch.setattr(os.path, "islink", lambda p: Path(p) == fake or real_islink(p))
    with pytest.raises(RenderError, match="logo.png"):
        render_template(template, "es", VARS)


# -- render por formato (REQ-ARCH-100; 00-marco D-43) ------------------------------------------

PROJECT = (
    "# comentario\n"
    "name: {{var:name}}\n"
    "format: latex\n"
    "build:\n"
    "  engine: pdflatex            # pdflatex | xelatex | lualatex\n"
    "  bib_files:\n"
    "    - bib/references.bib\n"
    "\n"
    "limits:\n"
    "  max_active_claims: 3\n"
    "ci:\n"
    "  pdf_retention_days: 14\n"
    "  latexdiff: false\n"
)
FORMAT_FILES = {
    "base/project.yaml": PROJECT.encode("utf-8"),
    "base/.latexmkrc": b"$do_cd = 1;\n",
    "base/tex/main.tex": b"\\documentclass{article}\n",
    "base/bib/references.bib": b"@book{a, title={x}}\n",
    "base/sections/intro/figures/.gitkeep": b"",
    "base/sections/intro/figures/plot.tex": b"% tikz\n",
    "base/texts.md": b"no es tex/\n",
    "l10n/es/sections/intro/intro.tex": b"\\section{Intro}\n",
    "l10n/es/sections/intro/intro.md": b"# Intro\n",
    "l10n/es/sections/intro/pending.bib": b"% pendiente\n",
    "l10n/en/sections/intro/intro.tex": b"\\section{Intro}\n",
    "l10n/en/sections/intro/intro.md": b"# Intro\n",
    "l10n/en/sections/intro/pending.bib": b"% pending\n",
}


def test_latex_render_leaves_out_the_markdown_text_of_the_part(tmp_path: Path) -> None:
    out = render_template(make_template(tmp_path, FORMAT_FILES), "es", VARS)
    assert "sections/intro/intro.md" not in out
    assert {"sections/intro/intro.tex", "sections/intro/pending.bib", "tex/main.tex", "bib/references.bib",
            ".latexmkrc", "sections/intro/figures/plot.tex"} <= set(out)
    assert out["project.yaml"] == PROJECT.replace("{{var:name}}", "demo").encode("utf-8")  # sin tocar
    assert out == render_template(make_template(tmp_path / "again", FORMAT_FILES), "es", VARS, fmt="latex")


def test_none_render_leaves_out_the_manuscript(tmp_path: Path) -> None:
    out = render_template(make_template(tmp_path, FORMAT_FILES), "es", VARS, fmt="none")
    assert set(out) == {
        "project.yaml",
        "sections/intro/figures/.gitkeep",
        "sections/intro/figures/plot.tex",  # solo sections/*/*.tex: una figura TikZ la juzga validate
        "sections/intro/intro.md",
        "texts.md",
    }
    assert out["project.yaml"] == (
        b"# comentario\nname: demo\nformat: none\n\nlimits:\n  max_active_claims: 3\n"
    )


def test_none_project_yaml_keeps_comments_and_other_ci_keys() -> None:
    text = (
        "# cabecera\nformat: latex   # latex | none\nbuild:\n  engine: x\n# sobre limits\nlimits:\n  a: 1\n"
        "ci:\n  pdf_retention_days: 14\n  # comentario de ci\n  model_review:\n    enabled: false\n  latexdiff: true\n"
    )
    assert project_yaml_without_manuscript(text) == (
        "# cabecera\nformat: none   # latex | none\n# sobre limits\nlimits:\n  a: 1\n"
        "ci:\n  # comentario de ci\n  model_review:\n    enabled: false\n"
    )
    assert project_yaml_without_manuscript("format: none\nci:\n  latexdiff: false\n") == "format: none\n"


@pytest.mark.parametrize(
    "text",
    ["name: x\n", "format: latex\nformat: none\n", "format: [latex]\n", "format: latex\nci: {latexdiff: true}\n"],
)
def test_none_project_yaml_needs_one_plain_format_line(text: str) -> None:
    with pytest.raises(RenderError):
        project_yaml_without_manuscript(text)


def test_unknown_format_is_a_render_error(template: Path) -> None:
    with pytest.raises(RenderError, match="formato"):
        render_template(template, "es", VARS, fmt="markdown")


@pytest.mark.parametrize(
    ("rel", "latex", "none"),
    [
        ("tex/main.tex", True, False),
        ("bib/references.bib", True, False),
        (".latexmkrc", True, False),
        ("sections/intro/intro.tex", True, False),
        ("sections/intro/pending.bib", True, False),
        ("sections/intro/intro.md", False, True),
        ("sections/intro/notes/n1.md", True, True),
        ("sections/intro/tables/t1.tex", True, True),
        ("tex", True, True),
        ("texts/a.tex", True, True),
        ("README.md", True, True),
    ],
)
def test_in_format(rel: str, latex: bool, none: bool) -> None:
    assert (in_format(rel, "latex"), in_format(rel, "none")) == (latex, none)


# -- variantes por formato (formats/<fmt>/l10n/<lang>/; 02-convencion REQ-CONV-91) ------------------

VARIANT_FILES = {
    **FORMAT_FILES,
    "l10n/es/memory/c.md": b"# C\nVer tex/notation.tex.\n",
    "l10n/en/memory/c.md": b"# C\nSee tex/notation.tex.\n",
    "formats/none/l10n/es/memory/c.md": b"# C\r\nNotaci\xc3\xb3n de {{var:name}}: secci\xc3\xb3n 4.\r\n",
    "formats/none/l10n/en/memory/c.md": b"# C\nNotation of {{var:name}}: section 4.\n",
}


def test_a_format_variant_replaces_its_file_only_in_that_format(tmp_path: Path) -> None:
    template = make_template(tmp_path, VARIANT_FILES)
    latex = render_template(template, "es", VARS)
    none = render_template(template, "es", VARS, fmt="none")
    assert latex["memory/c.md"] == b"# C\nVer tex/notation.tex.\n"
    assert none["memory/c.md"] == "# C\nNotación de demo: sección 4.\n".encode("utf-8")  # con variables y LF
    assert render_template(template, "en", VARS, fmt="none")["memory/c.md"] == b"# C\nNotation of demo: section 4.\n"
    # Solo cambia el contenido: las rutas son las del formato, igual que sin la variante.
    plain = {k: v for k, v in VARIANT_FILES.items() if not k.startswith("formats/")}
    assert list(none) == list(render_template(make_template(tmp_path / "plain", plain), "es", VARS, fmt="none"))


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"formats/none/l10n/es/memory/d.md": b"x\n", "formats/none/l10n/en/memory/d.md": b"x\n"},
         "no reemplaza"),  # no existe en l10n/: el render tendría otra ruta
        ({"formats/none/l10n/es/README.md": b"x\n", "formats/none/l10n/en/memory/c.md": b"x\n"},
         "mismas rutas"),  # cada idioma reemplaza otro archivo
        ({"formats/none/l10n/es/README.md": b"x\n"}, "idiomas"),  # falta un idioma
        ({"formats/none/l10n/es/sections/intro/intro.tex": b"x\n", "formats/none/l10n/en/sections/intro/intro.tex": b"x\n"},
         "no va en el render none"),
        ({"formats/quarto/l10n/es/memory/c.md": b"x\n", "formats/quarto/l10n/en/memory/c.md": b"x\n"},
         "formato no admitido"),
        ({"formats/none/base/memory/c.md": b"x\n"}, "solo puede contener l10n/"),
        ({"formats/none.md": b"x\n"}, "se esperaba una carpeta"),
        ({"formats/none/l10n/fr/memory/c.md": b"x\n"}, "idiomas"),
    ],
)
def test_bad_format_variants_are_render_errors(tmp_path: Path, extra: dict[str, bytes], message: str) -> None:
    files = {**FORMAT_FILES, "l10n/es/README.md": b"a\n", "l10n/en/README.md": b"a\n",
             "l10n/es/memory/c.md": b"c\n", "l10n/en/memory/c.md": b"c\n", **extra}
    template = make_template(tmp_path, files)
    for fmt in ("latex", "none"):  # la carpeta se comprueba entera, con cualquier formato
        with pytest.raises(RenderError, match=message):
            render_template(template, "es", VARS, fmt=fmt)


@pytest.mark.parametrize("lang", LANGS)
def test_real_template_renders_without_markers(lang: str) -> None:
    variables = tomllib.loads((REPO_ROOT / "tools" / "template_vars.toml").read_text(encoding="utf-8"))
    variables["language"] = lang
    out = render_template(REPO_ROOT / "template", lang, variables)  # type: ignore[arg-type]
    assert "roles.yaml" in out and "README.md" in out
    for rel, data in out.items():
        if is_text(data):
            assert b"{{var:" not in data, rel
    assert f"language: {lang}\n".encode() in out["project.yaml"]
    assert out == render_template(REPO_ROOT / "template", lang, variables)  # type: ignore[arg-type]


# -- piezas de init: fuentes y coincidencia (01 §10.6) -------------------------------------


def test_template_sources_name_each_origin(tmp_path: Path) -> None:
    template = make_template(tmp_path, VARIANT_FILES)
    sources = template_sources(template, "es", "none")
    assert sources["memory/c.md"].origin == "formats/none/l10n/es/memory/c.md" and sources["memory/c.md"].localized
    assert sources["project.yaml"].origin == "base/project.yaml" and not sources["project.yaml"].localized
    assert list(sources) == sorted(sources)
    rendered = render_template(template, "es", VARS, fmt="none")
    assert set(sources) == set(rendered)


def test_match_source_recovers_the_values() -> None:
    source = b"# {{var:title}}\r\nPin {{var:core_pin}}; again {{var:title}}.\n"
    assert match_source(source, b"# Mi estudio\nPin 0.1.3; again Mi estudio.\n") == {
        "title": "Mi estudio", "core_pin": "0.1.3"}
    assert match_source(source, b"# A\nPin 1; again B.\n") is None  # un nombre repetido vale lo mismo
    assert match_source(source, b"# A\nPin 1; again A.\nMore.\n") is None
    assert match_source(source, b"# A\nB\nPin 1; again A\nB.\n") is None  # una sola línea por valor
    assert match_source(b"plain\n", b"\xef\xbb\xbfplain\r\n") == {}  # texto normalizado
    assert match_source(BINARY, BINARY) == {} and match_source(BINARY, BINARY + b"x") is None
    assert match_source(b"a.b {{var:x}}\n", b"aXb y\n") is None  # los literales no son expresiones


@pytest.mark.parametrize("lang", LANGS)
def test_the_real_render_matches_its_sources(lang: str) -> None:
    variables = tomllib.loads((REPO_ROOT / "tools" / "template_vars.toml").read_text(encoding="utf-8"))
    variables["language"] = lang
    out = render_template(REPO_ROOT / "template", lang, variables)  # type: ignore[arg-type]
    for rel, source in template_sources(REPO_ROOT / "template", lang).items():
        found = match_source(source.read(), out[rel])
        assert found is not None, rel
        assert all(variables[name] == value for name, value in found.items()), rel
