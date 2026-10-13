# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Contenido de la plantilla y variables del render.

Autoridad: 12-mvp §1.2, REQ-MVP-4 y REQ-MVP-5; 02-convencion §2, §4-§10; 03-esquemas §3.1,
§4.1, §5 y §9.1; 11-trazabilidad §7.2 y §7.6; 01-arquitectura REQ-ARCH-40, REQ-ARCH-45 y
REQ-ARCH-83; 00-marco D-33 (o).

Las pruebas leen las fuentes de ``template/`` y hacen un render mínimo propio (solo
``{{var:...}}``), sin depender del render ni de los validadores de configuración. Los esquemas se
cargan de ``schemas/`` o de ``$AUCOVE_SPEC_DIR/schemas``. Si ``AUCOVE_SPEC_DIR`` está
definido, además se comparan los bloques literales con los de la especificación.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import unicodedata
from collections.abc import Iterator, Mapping
from functools import cache
from pathlib import Path, PurePosixPath

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker

from aucove import _meta

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = REPO_ROOT / "template"
BASE = TEMPLATE / "base"
L10N = TEMPLATE / "l10n"
FORMATS_ROOT = TEMPLATE / "formats"
LANGS: tuple[str, ...] = ("es", "en")
FMTS: tuple[str, ...] = ("latex", "none")  # project.yaml: format (00-marco D-43)
# Variantes por formato de archivos localizables (formats/<fmt>/l10n/<lang>/; REQ-ARCH-100):
# solo la constitución, que con none no cita tex/, bib/ ni el PDF (02-convencion REQ-CONV-91).
FORMAT_VARIANTS: dict[str, frozenset[str]] = {"none": frozenset({"memory/constitution.md"})}
VARS_FILE = REPO_ROOT / "tools" / "template_vars.toml"

# Claves de tools/template_vars.toml.
TEMPLATE_VAR_KEYS: frozenset[str] = frozenset(
    {
        "core",
        "core_pin",
        "language",
        "name",
        "title",
        "forge_repo",
        "framework_repo",
        "gitleaks_version",
        "gitleaks_sha256",
    }
)
# Marcadores provisionales que quedan mientras gitleaks no se verifique contra sus checksums.
GITLEAKS_MARKERS: dict[str, str] = {"gitleaks_version": "X.Y.Z", "gitleaks_sha256": "<64 hex>"}
# Marcador provisional de framework_repo mientras el repositorio del framework no exista: el
# punto hace que el dueño no sea un nombre de cuenta válido en GitHub, así que nadie puede
# registrarlo y la descarga del lock falla.
FRAMEWORK_REPO_MARKER = "framework-owner.invalid/aucove"
# Nombre de cuenta u organización de GitHub, y forma owner/repo.
GITHUB_LOGIN_RE = r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}"
OWNER_REPO_RE = GITHUB_LOGIN_RE + r"/[A-Za-z0-9._-]{1,100}"
# Dueños de ejemplo que existen o pueden registrarse (example-org existe en GitHub).
EXAMPLE_OWNERS: frozenset[str] = frozenset({"example-org", "example", "example-com", "your-org", "org", "owner"})

DEFAULT_PARTS: tuple[str, ...] = (
    "introduction", "background", "theory", "methodology", "experiments", "results", "conclusions",
)  # 00-marco D-55
PART_TITLES: dict[str, tuple[str, ...]] = {
    "es": ("Introducción", "Antecedentes y trabajo previo", "Marco teórico", "Propuesta y metodología",
           "Experimentos", "Análisis de resultados", "Conclusiones"),
    "en": ("Introduction", "Background and related work", "Theoretical framework", "Proposal and methodology",
           "Experiments", "Results analysis", "Conclusions"),
}
# Rutas de template/base/ (los workflows de .github/workflows/ tienen sus propias pruebas).
BASE_FILES: frozenset[str] = frozenset(
    {
        "project.yaml",
        ".gitignore",
        ".gitattributes",
        ".latexmkrc",
        "LICENSE",
        "LICENSES/MIT-0.txt",
        "LICENSES/CC0-1.0.txt",
        "REUSE.toml",
        "memory/decisions/.gitkeep",
        "memory/notes/.gitkeep",
        "tex/main.tex",
        "tex/notation.tex",
        "bib/references.bib",
        *(f"sections/{p}/{sub}/.gitkeep" for p in DEFAULT_PARTS for sub in ("figures", "tables")),
    }
)
# Rutas localizables: exactamente la lista de 02-convencion §10. El texto de la parte de ejemplo
# está en los dos formatos y el render elige uno según format (01-arquitectura REQ-ARCH-100).
L10N_FILES: frozenset[str] = frozenset(
    {
        "README.md",
        "AGENTS.md",
        "CLAUDE.md",
        "plan.md",
        "memory/constitution.md",
        "data/README.md",
        "tex/preamble.tex",
        "tex/guide.md",  # 00-marco D-56: guía del artículo de ejemplo
        *(f"tex/outline/{p}.tex" for p in DEFAULT_PARTS),  # D-56: una sección del artículo por parte
        "roles.yaml",  # 00-marco D-55: títulos de las partes y comentarios
        *(f"sections/{p}/{p}{ext}" for p in DEFAULT_PARTS for ext in (".tex", ".md")),
        *(f"sections/{p}/pending.bib" for p in DEFAULT_PARTS),
        ".github/ISSUE_TEMPLATE/config.yml",
        ".github/pull_request_template.md",
    }
)

VAR_RE = re.compile(r"\{\{var:([a-z][a-z0-9_]*)\}\}")
LOOSE_VAR_RE = re.compile(r"\{\{\s*var\s*:[^}]*\}\}")
BINARY_SUFFIXES: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".gz", ".zip"})

# Las etiquetas SPDX se arman por concatenación: este archivo no debe parecer anotado.
_SPDX = "SPDX" + "-"
SPDX_HEADER_RE = re.compile(
    re.escape(_SPDX)
    + r"(?:License-Identifier|FileCopyrightText|FileContributor|SnippetBegin|SnippetEnd"
    + r"|SnippetCopyrightText|SnippetLicenseConcluded)\s*:"
)

IMAGE_REQ_CI_17 = (
    "registry.gitlab.com/islandoftex/images/texlive:TL2026-2026-09-20-full"
    "@sha256:7334b00bf8e7a0996f7ddd65482363aaf7711d372e569f3ea78509619e3083ff"
)
CORE_PIN_LINE_RE = re.compile(r'^core_pin: "[0-9]+\.[0-9]+\.[0-9]+((a|b|rc)[0-9]+)?"( *#.*)?$')  # REQ-ARCH-45, D-66

# 02-convencion §4.7 con $pdf_mode = 1 (build.engine: pdflatex).
LATEXMKRC = (
    "# .latexmkrc (clase lead). Equivale a la compilación de referencia (02-convencion §4.7).\n"
    "@default_files = ('tex/main.tex');\n"
    "$do_cd = 1;\n"
    "$out_dir = 'build';\n"
    "$pdf_mode = 1;   # 1 = pdflatex, 5 = xelatex, 4 = lualatex: igual que project.yaml build.engine\n"
    "set_tex_cmds('%O -no-shell-escape -interaction=nonstopmode -halt-on-error -file-line-error %S');\n"
    "$ENV{'openout_any'} = 'p';\n"
    "$ENV{'shell_escape'} = 'f';\n"
)

# Patrones de secretos de 10-seguridad REQ-SEC-35 que no trae el bloque de 02-convencion §9.
# 02 §9 fija las líneas mínimas, así que la plantilla los añade al bloque [req] secretos,
# después de `*.pem`, y cumple los dos capítulos.
GITIGNORE_SEC35_EXTRA: tuple[str, ...] = ("*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*")
# Líneas de los bloques [req] de 02-convencion §9 (REQ-CONV-81) más las de REQ-SEC-35.
GITIGNORE_REQUIRED: tuple[str, ...] = (
    "/.aucove/",
    "/.claude/settings.local.json",
    "/.claude/worktrees/",
    "/CLAUDE.local.md",
    "/tex/build/",
    "/tex/build-diff/",
    "/data/*",
    "!/data/README.md",
    ".env",
    ".env.*",
    "*.pem",
    *GITIGNORE_SEC35_EXTRA,
)
# Todas las líneas de .gitattributes de 02-convencion §9 (REQ-CONV-82).
GITATTRIBUTES_LINES: tuple[str, ...] = (
    "# .gitattributes (clase lead). Todas las líneas son obligatorias (conv.gitattributes.required).",
    "* text=auto eol=lf",
    "*.tex   text diff=tex",
    "*.bib   text diff=bibtex",
    "*.md    text diff=markdown",
    "*.py    text diff=python",
    "*.ipynb text eol=lf filter=nbstripout diff=ipynb",
    "*.pdf   binary",
    "*.png   binary",
    "*.jpg   binary",
    "*.jpeg  binary",
)

_HOLDER = _SPDX + 'FileCopyrightText = "2026 The aucove Authors"\n'
_LIC = _SPDX + 'License-Identifier = "{}"\n'
# 11-trazabilidad §7.2 (REQ-TRACE-60), byte a byte.
REUSE_TOML = (
    "version = 1\n"
    "\n"
    "[[annotations]]   # texto de ejemplo, notas y `.md` de las partes, y configuración: CC0-1.0 (regla general)\n"
    'path = ["**", "sections/*/notes/**", "sections/*/*.md"]\n' + _HOLDER + _LIC.format("CC0-1.0") + "\n"
    "[[annotations]]   # código: MIT-0 (en REUSE.toml gana la última tabla que coincide)\n"
    'path = [".github/**", ".claude/**", ".agents/**", ".devcontainer/**", ".latexmkrc",\n'
    '        ".gitignore", ".gitattributes", ".mcp.json", "tex/main.tex", "tex/preamble.tex",\n'
    '        "tex/notation.tex", "sections/*/analysis/**"]\n' + _HOLDER + _LIC.format("MIT-0") + "\n"
    "[[annotations]]   # datos derivados del análisis: CC0-1.0\n"
    'path = ["sections/*/analysis/derived/**"]\n' + _HOLDER + _LIC.format("CC0-1.0")
)

# Aviso de no afiliación de 11-trazabilidad §7.6 (REQ-TRACE-71).
NOTICE: dict[str, str] = {
    "es": (
        "*aucove* es un proyecto independiente de código abierto. No está afiliado a Anthropic, "
        "OpenAI, Google, GitHub ni a otros proveedores mencionados, ni cuenta con su respaldo o "
        "patrocinio. Claude, Claude Code, Codex, Gemini, GitHub y las demás marcas pertenecen a sus "
        "titulares y se mencionan solo para indicar compatibilidad."
    ),
    "en": (
        "*aucove* is an independent open source project. It is not affiliated with, endorsed by, "
        "or sponsored by Anthropic, OpenAI, Google, GitHub, or any other provider mentioned. Claude, "
        "Claude Code, Codex, Gemini, GitHub, and other marks belong to their respective owners and "
        "are mentioned only to indicate compatibility."
    ),
}

PLAN_IDS: frozenset[str] = frozenset({"RQ-1", "HYP-1", "IN-1", "OUT-1", "METH-1"})
SKILL_NAMES: tuple[str, ...] = (
    "empezar", "estado", "entregar", "pendientes", "decision", "direccion",
    "start", "project-status", "submit", "reviews", "direction",
)


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def files_under(root: Path) -> frozenset[str]:
    """Rutas POSIX relativas de todos los archivos bajo ``root`` (sin seguir enlaces)."""
    if not root.is_dir():
        return frozenset()
    return frozenset(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def all_template_files() -> Iterator[Path]:
    yield from sorted(p for p in TEMPLATE.rglob("*") if p.is_file())


def read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def src(lang: str | None, rel: str) -> str:
    """Texto de una fuente: de ``l10n/<lang>/`` si ``lang``, si no de ``base/``."""
    return read((L10N / lang if lang else BASE) / rel)


def localized(lang: str, rel: str, fmt: str) -> str:
    """Fuente de un archivo localizable en el render de ``fmt``: su variante de ``formats/`` o la de ``l10n/``."""
    variant = FORMATS_ROOT / fmt / "l10n" / lang / rel
    return read(variant) if variant.is_file() else src(lang, rel)


@cache
def template_vars() -> Mapping[str, object]:
    return tomllib.loads(VARS_FILE.read_text(encoding="utf-8"))


def render_values(lang: str) -> dict[str, str]:
    """Valores de ``tools/template_vars.toml`` con ``language`` fijado a ``lang`` (como ``render_template.py --lang``)."""
    values = {key: str(value) for key, value in template_vars().items()}
    values["language"] = lang
    return values


def render(text: str, lang: str) -> str:
    values = render_values(lang)
    return VAR_RE.sub(lambda m: values[m.group(1)], text)


def schema_dirs() -> list[Path]:
    """Directorios de esquemas disponibles: ``schemas/`` del repo y el de la especificación."""
    found: list[Path] = []
    local = REPO_ROOT / "schemas"
    if (local / "project.schema.json").is_file():
        found.append(local)
    spec = os.environ.get("AUCOVE_SPEC_DIR")
    if spec and (Path(spec) / "schemas" / "project.schema.json").is_file():
        found.append(Path(spec) / "schemas")
    return found


def validate_against(kind: str, instance: object) -> list[str]:
    dirs = schema_dirs()
    if not dirs:
        pytest.skip("no hay esquemas: falta schemas/ y AUCOVE_SPEC_DIR no está definido")
    errors: list[str] = []
    for directory in dirs:
        schema = json.loads((directory / f"{kind}.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        for err in validator.iter_errors(instance):
            errors.append(f"{directory.name}: /{'/'.join(map(str, err.absolute_path))}: {err.message}")
    return errors


def spec_file(name: str) -> str | None:
    spec = os.environ.get("AUCOVE_SPEC_DIR")
    if not spec:
        return None
    path = Path(spec) / name
    return path.read_text(encoding="utf-8") if path.is_file() else None


def spec_block(chapter: str, anchor: str, fence: str) -> str:
    """Primer bloque delimitado por ``fence`` después de la línea que empieza con ``anchor``."""
    text = spec_file(chapter)
    if text is None:
        pytest.skip("AUCOVE_SPEC_DIR no está definido")
    lines = text.split("\n")
    close = re.match(r"^(`+|~+)", fence).group(1)  # type: ignore[union-attr]
    start = next(i for i, line in enumerate(lines) if line.startswith(anchor))
    begin = next(i for i in range(start, len(lines)) if lines[i].strip() == fence)
    end = next(i for i in range(begin + 1, len(lines)) if lines[i].strip() == close)
    return "\n".join(lines[begin + 1 : end]) + "\n"


def numbered_sections(text: str) -> list[tuple[int, str, list[str]]]:
    """Secciones ``## <n>. <título>`` con sus líneas."""
    out: list[tuple[int, str, list[str]]] = []
    for line in text.split("\n"):
        m = re.match(r"^## (\d+)\. (\S.*)$", line)
        if m:
            out.append((int(m.group(1)), m.group(2), []))
        elif line.startswith("## "):
            raise AssertionError(f"encabezado de nivel 2 sin numerar: {line!r}")
        elif out:
            out[-1][2].append(line)
    return out


def bullets(lines: list[str]) -> int:
    return sum(1 for line in lines if re.match(r"^(?:- |\d+\. )", line))


def strip_tex_comments(text: str) -> str:
    return "\n".join(re.sub(r"(?<!\\)%.*$", "", line) for line in text.split("\n"))


# 03-esquemas §5.6, analizador de referencia de plan.md.
PLAN_ID = r"(?:RQ|HYP|IN|OUT|METH)-[1-9][0-9]{0,3}"
DEC_ID = r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[1-9][0-9]*-[a-z0-9]+(?:-[a-z0-9]+)*"
PLAN_DEF = re.compile(rf"^- (?P<id>{PLAN_ID})(?P<ret> \(retired(?:: (?P<dec>{DEC_ID}))?\))?: (?P<text>\S.*)$")
PLAN_LOOKS = re.compile(
    r"^\s*(?:[-*+]\s*[*_~]*\s*(?i:RQ|HYP|IN|OUT|METH)\s*-\s*[0-9]+"
    r"|[#*_~]*\s*(?:RQ|HYP|IN|OUT|METH)-[0-9]+\s*[:.)])"
)
PLAN_REF = re.compile(rf"\b{PLAN_ID}\b")


def parse_plan(text: str) -> tuple[list[dict[str, object]], list[tuple[str, int, str]]]:
    items: dict[str, dict[str, object]] = {}
    errors: list[tuple[str, int, str]] = []
    fence: str | None = None
    in_comment = False
    cur: dict[str, object] | None = None
    for no, line in enumerate(text.split("\n"), 1):
        s = line.strip()
        if in_comment:
            in_comment = "-->" not in line
            continue
        if fence:
            fence = None if s.startswith(fence) else fence
            continue
        if s.startswith(("```", "~~~")):
            fence, cur = s[:3], None
            continue
        if s.startswith("<!--"):
            in_comment = "-->" not in s[4:]
            continue
        if m := PLAN_DEF.match(line):
            ident = m["id"]
            if ident in items:
                errors.append(("E_VALIDATION_DUPLICATE_ID", no, ident))
                cur = None
                continue
            retired = m["ret"] is not None
            cur = items[ident] = {"id": ident, "status": "retired" if retired else "active",
                                  "text": m["text"].strip(), "detail": ""}
            continue
        if PLAN_LOOKS.match(line):
            errors.append(("E_VALIDATION_PLAN_SYNTAX", no, s))
            cur = None
            continue
        if cur is not None and line.startswith("  ") and s:
            cur["detail"] = f"{cur['detail']}\n{s}" if cur["detail"] else s
            continue
        cur = None
    for item in items.values():
        item["refs"] = sorted(set(PLAN_REF.findall(f"{item['text']}\n{item['detail']}")) - {item["id"]})
    return list(items.values()), errors


# ---------------------------------------------------------------------------
# Árbol
# ---------------------------------------------------------------------------


def test_base_contains_package_files() -> None:
    missing = BASE_FILES - files_under(BASE)
    assert not missing, f"faltan en template/base: {sorted(missing)}"


@pytest.mark.parametrize("lang", LANGS)
def test_l10n_paths_are_exactly_the_localizable_list(lang: str) -> None:
    assert files_under(L10N / lang) == L10N_FILES


def test_l10n_same_paths_and_disjoint_from_base() -> None:
    """REQ-ARCH-83: mismo conjunto en es y en, sin intersección con base."""
    es, en = files_under(L10N / "es"), files_under(L10N / "en")
    assert es == en
    assert not es & files_under(BASE)
    assert sorted(p.name for p in L10N.iterdir()) == sorted(LANGS)


def test_format_variants_only_replace_localizable_files() -> None:
    """``formats/<fmt>/l10n/<lang>/`` (REQ-ARCH-100): las rutas de 02 §10 que cambian con el formato.

    Reemplazan a la de ``l10n/`` con la misma ruta, así que el manifiesto no cambia; los dos
    idiomas traen las mismas (REQ-ARCH-83).
    """
    assert sorted(p.name for p in FORMATS_ROOT.iterdir()) == sorted(FORMAT_VARIANTS)
    for fmt, expected in FORMAT_VARIANTS.items():
        assert fmt in FMTS
        assert sorted(p.name for p in (FORMATS_ROOT / fmt).iterdir()) == ["l10n"]
        assert sorted(p.name for p in (FORMATS_ROOT / fmt / "l10n").iterdir()) == sorted(LANGS)
        for lang in LANGS:
            assert files_under(FORMATS_ROOT / fmt / "l10n" / lang) == expected
        assert expected <= L10N_FILES


def test_no_h3_h4_paths_in_template() -> None:
    """REQ-MVP-4: en H2 faltan a propósito .mcp.json, .claude/, .agents/ y .devcontainer/."""
    offending = []
    for path in all_template_files():
        parts = PurePosixPath(path.relative_to(TEMPLATE).as_posix()).parts
        if {".claude", ".agents", ".devcontainer"} & set(parts) or ".mcp.json" in parts:
            offending.append(path.relative_to(TEMPLATE).as_posix())
    for directory in TEMPLATE.rglob("*"):
        if directory.is_dir() and directory.name in {".claude", ".agents", ".devcontainer"}:
            offending.append(directory.relative_to(TEMPLATE).as_posix() + "/")
    assert not offending


def test_no_local_paths_versioned() -> None:
    """REQ-MVP-5: la plantilla no versiona .claude/settings.local.json ni .aucove/."""
    for rel in files_under(BASE) | files_under(L10N / "es") | files_under(L10N / "en"):
        parts = PurePosixPath(rel).parts
        assert ".aucove" not in parts and rel != ".claude/settings.local.json", rel
        assert not rel.startswith(("tex/build/", "tex/build-diff/")), rel
        assert not (rel.startswith("data/") and rel != "data/README.md"), rel


@pytest.mark.parametrize("path", list(all_template_files()), ids=lambda p: p.relative_to(TEMPLATE).as_posix())
def test_text_file_encoding_and_no_spdx_headers(path: Path) -> None:
    """UTF-8 sin BOM, LF y salto final (REQ-CONV-38, REQ-ARCH-82); sin cabeceras SPDX."""
    data = path.read_bytes()
    if path.suffix.lower() in BINARY_SUFFIXES:
        return
    text = data.decode("utf-8")  # UnicodeDecodeError = falla
    assert not data.startswith(b"\xef\xbb\xbf"), "BOM"
    assert b"\r" not in data, "CR o CRLF"
    if path.name == ".gitkeep":
        assert data == b"", ".gitkeep debe estar vacío (REQ-CONV-4)"
        return
    assert data, "archivo vacío"
    assert text.endswith("\n") and not text.endswith("\n\n"), "debe terminar con un solo salto de línea"
    assert not SPDX_HEADER_RE.search(text), "la plantilla no lleva cabeceras SPDX"
    assert not path.name.endswith(".license")


def test_path_segments_are_ascii() -> None:
    """REQ-CONV-39 bajo sections/, tex/, memory/, bib/ y data/."""
    for rel in files_under(BASE) | files_under(L10N / "en"):
        if rel.split("/", 1)[0] in {"sections", "tex", "memory", "bib", "data"}:
            for segment in rel.split("/"):
                assert re.fullmatch(r"[A-Za-z0-9._-]+", segment), rel


def test_single_example_section_and_empty_memory_dirs() -> None:
    """REQ-CONV-1, REQ-CONV-4, REQ-CONV-5: las 7 partes de D-55 y memoria sin registros."""
    for root in (BASE, L10N / "es", L10N / "en"):
        sections = root / "sections"
        if sections.is_dir():
            assert sorted(p.name for p in sections.iterdir()) == sorted(DEFAULT_PARTS)
        assert not (root / "tex" / "sections").exists()  # D-43: las partes ya no viven en tex/
    merged = files_under(BASE) | files_under(L10N / "en")
    assert sorted(p for p in merged if p.startswith("memory/decisions/")) == ["memory/decisions/.gitkeep"]
    assert sorted(p for p in merged if p.startswith("memory/notes/")) == ["memory/notes/.gitkeep"]
    assert sorted(p for p in merged if p.startswith("memory/")) == [
        "memory/constitution.md", "memory/decisions/.gitkeep", "memory/notes/.gitkeep",
    ]
    section = sorted(p for p in merged if p.startswith("sections/"))
    assert section == sorted(  # <id>.md es la variante format: none (REQ-ARCH-100)
        f"sections/{p}/{name}"
        for p in DEFAULT_PARTS
        for name in ("figures/.gitkeep", f"{p}.md", f"{p}.tex", "pending.bib", "tables/.gitkeep")
    )


# ---------------------------------------------------------------------------
# Variables del render
# ---------------------------------------------------------------------------


def test_template_vars_has_exactly_c28_keys() -> None:
    data = template_vars()
    assert set(data) == TEMPLATE_VAR_KEYS
    assert all(isinstance(value, str) and value for value in data.values())
    assert data["language"] == "en"
    # El render por defecto instala la versión de este árbol (01 §2.3; 00-marco D-66).
    assert data["core_pin"] == _meta.__version__
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", str(data["name"]))
    assert re.fullmatch(OWNER_REPO_RE, str(data["forge_repo"]))


def test_framework_repo_is_real_or_unregistrable_marker() -> None:
    """09-ci REQ-CI-12 y 10-seguridad REQ-SEC-45: el lock de las restricciones sale de aquí.

    O es el repositorio real del framework, o el marcador provisional, cuyo dueño GitHub no
    admite. Nunca un dueño de ejemplo que exista o pueda registrarse.
    """
    value = str(template_vars()["framework_repo"])
    owner = value.partition("/")[0]
    assert owner.lower() not in EXAMPLE_OWNERS, value
    assert value == FRAMEWORK_REPO_MARKER or re.fullmatch(OWNER_REPO_RE, value), value
    if value == FRAMEWORK_REPO_MARKER:
        assert not re.fullmatch(GITHUB_LOGIN_RE, owner)
        assert re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", value)  # sin metacaracteres


def test_template_vars_core_pin_satisfies_core() -> None:
    """REQ-ARCH-44: core = '>=X.Y.Z[aN|bN|rcN],<X.(Y+1)' con mayor 0, y core_pin lo satisface."""
    specifiers = pytest.importorskip("packaging.specifiers")
    data = template_vars()
    found = re.fullmatch(r"([0-9]+)\.([0-9]+)\.[0-9]+(?:(?:a|b|rc)[0-9]+)?", str(data["core_pin"]))
    assert found is not None, data["core_pin"]
    major, minor = int(found.group(1)), int(found.group(2))
    assert major == 0 and data["core"] == f">={data['core_pin']},<0.{minor + 1}"
    # Una preliminar solo satisface un rango que la nombra (PEP 440): por eso core empieza en ella.
    assert specifiers.SpecifierSet(str(data["core"]), prereleases=True).contains(str(data["core_pin"]))


def test_gitleaks_values_have_valid_shape_or_c28_markers() -> None:
    data = template_vars()
    version, sha = str(data["gitleaks_version"]), str(data["gitleaks_sha256"])
    real = bool(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)) and bool(re.fullmatch(r"[0-9a-f]{64}", sha))
    markers = {"gitleaks_version": version, "gitleaks_sha256": sha} == GITLEAKS_MARKERS
    assert real or markers, "gitleaks: o versión y sha256 verificados, o los dos marcadores provisionales"


def test_gitleaks_values_are_pinned() -> None:
    """La comparación con el archivo de checksums oficial es manual (sin red en las pruebas).

    Mientras queden los marcadores provisionales, la prueba se omite, para que el pendiente quede a la vista.
    """
    data = template_vars()
    if {k: str(data[k]) for k in GITLEAKS_MARKERS} == GITLEAKS_MARKERS:
        pytest.skip("gitleaks sin verificar: quedan los marcadores provisionales (sin permiso de descarga; "
                    "el CI del framework lo bloquea)")
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", str(data["gitleaks_version"]))
    assert re.fullmatch(r"[0-9a-f]{64}", str(data["gitleaks_sha256"]))


def test_only_c28_variables_in_template() -> None:
    used: set[str] = set()
    for path in all_template_files():
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        text = read(path)
        loose = set(LOOSE_VAR_RE.findall(text))
        strict = {m.group(0) for m in VAR_RE.finditer(text)}
        assert loose == strict, f"{path}: marcador {{{{var:...}}}} mal formado"
        used |= set(VAR_RE.findall(text))
    assert used <= TEMPLATE_VAR_KEYS, sorted(used - TEMPLATE_VAR_KEYS)
    # Las de project.yaml (y el README) salen de aquí.
    assert {"core", "core_pin", "language", "name", "title", "forge_repo"} <= used


@pytest.mark.parametrize("lang", LANGS)
def test_render_leaves_no_markers(lang: str) -> None:
    for root in (BASE, L10N / lang, *(FORMATS_ROOT / fmt / "l10n" / lang for fmt in FORMAT_VARIANTS)):
        for rel in files_under(root):
            out = render(read(root / rel), lang)
            assert "{{var:" not in out, rel


# ---------------------------------------------------------------------------
# project.yaml y roles.yaml
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_project_yaml_valid_after_render(lang: str) -> None:
    text = render(src(None, "project.yaml"), lang)
    data = yaml.safe_load(text)
    assert validate_against("project", data) == []
    assert data["language"] == lang
    assert data["convention"] == "0.1" and 'convention: "0.1"' in text.split("\n")  # REQ-CONV-85
    values = render_values(lang)
    for key in ("core", "core_pin", "name", "title"):
        assert data[key] == values[key]
    assert data["forge"] == {"type": "github", "repo": values["forge_repo"], "base_branch": "main"}
    pin_lines = [line for line in text.split("\n") if line.startswith("core_pin:")]
    assert len(pin_lines) == 1 and CORE_PIN_LINE_RE.match(pin_lines[0])


def test_project_yaml_uses_vars_for_c28_keys() -> None:
    text = src(None, "project.yaml")
    for key, line in (
        ("core", 'core: "{{var:core}}"'),
        ("core_pin", 'core_pin: "{{var:core_pin}}"'),
        ("name", "name: {{var:name}}"),
        ("title", 'title: "{{var:title}}"'),
        ("language", "language: {{var:language}}"),
        ("forge_repo", "  repo: {{var:forge_repo}}"),
    ):
        assert line in text.split("\n"), key


def test_project_yaml_build_is_declarative() -> None:
    """build.* por enumeración (REQ-CI-16), imagen de REQ-CI-17 y tope de 1 a 12 (D-33 o)."""
    build = yaml.safe_load(render(src(None, "project.yaml"), "en"))["build"]
    assert build == {
        "engine": "pdflatex",
        "main": "tex/main.tex",
        "image": IMAGE_REQ_CI_17,
        "bibliography": "biber",
        "bib_files": ["bib/references.bib"],
        "shell_escape": "disabled",
        "compile_on_submit": "if_available",
        "timeout_minutes": 10,
    }
    assert 1 <= build["timeout_minutes"] <= 12
    # REQ-SCHEMA-20: el principal y los .bib existen en la plantilla.
    merged = files_under(BASE) | files_under(L10N / "en")
    assert build["main"] in merged and set(build["bib_files"]) <= merged


@pytest.mark.parametrize("lang", LANGS)
def test_roles_yaml_valid_and_minimal(lang: str) -> None:
    text = src(lang, "roles.yaml")
    data = yaml.safe_load(text)
    assert validate_against("roles", data) == []
    people = data["people"]
    assert [p["role"] for p in people] == ["lead", "researcher"]
    assert all(p["account_type"] == "team" for p in people)  # EV-03: la matriz de cuentas rechaza unknown
    assert all(p.get("active", True) for p in people)
    assert people[0]["login"] == data["lead"]  # REQ-SCHEMA-24
    logins = [p["login"].lower() for p in people]
    assert len(set(logins)) == len(logins)  # REQ-SCHEMA-26
    # 00-marco D-55: las 7 partes del esquema por defecto, con su título en cada idioma.
    assert data["sections"] == [
        {"id": p, "title": t, "owner": data["lead"]} for p, t in zip(DEFAULT_PARTS, PART_TITLES[lang], strict=True)
    ]
    assert "deputy" not in data and "editor" not in data


@pytest.mark.parametrize("lang", LANGS)
def test_roles_yaml_editor_is_commented_and_valid_when_enabled(lang: str) -> None:
    """00-marco D-56: la línea del editor viene comentada y, al activarla, el esquema la acepta."""
    text = src(lang, "roles.yaml")
    commented = [line for line in text.split("\n") if re.match(r"^# editor: ", line)]
    assert len(commented) == 1
    enabled = yaml.safe_load(text.replace(commented[0], commented[0][2:], 1))
    assert validate_against("roles", enabled) == []
    assert enabled["editor"] in {p["login"] for p in enabled["people"]}


@pytest.mark.parametrize("lang", LANGS)
def test_roles_yaml_deputy_is_commented_and_valid_when_enabled(lang: str) -> None:
    text = src(lang, "roles.yaml")
    commented = [line for line in text.split("\n") if re.match(r"^# deputy: ", line)]
    assert len(commented) == 1
    enabled = yaml.safe_load(text.replace(commented[0], commented[0][2:], 1))
    assert validate_against("roles", enabled) == []
    researchers = {p["login"] for p in enabled["people"] if p["role"] == "researcher"}
    assert enabled["deputy"] in researchers and enabled["deputy"] != enabled["lead"]  # REQ-SCHEMA-25


# ---------------------------------------------------------------------------
# plan.md, constitución, AGENTS.md y CLAUDE.md
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_plan_has_exactly_the_five_ids(lang: str) -> None:
    items, errors = parse_plan(src(lang, "plan.md"))
    assert errors == []
    assert {str(i["id"]) for i in items} == PLAN_IDS and len(items) == len(PLAN_IDS)
    assert all(i["status"] == "active" for i in items)
    for item in items:
        assert set(item["refs"]) <= PLAN_IDS  # type: ignore[arg-type]  # REQ-SCHEMA-38


def test_plan_same_structure_in_both_languages() -> None:
    es, en = (parse_plan(src(lang, "plan.md"))[0] for lang in LANGS)
    assert [(i["id"], i["refs"]) for i in es] == [(i["id"], i["refs"]) for i in en]


@pytest.mark.parametrize("lang", LANGS)
def test_agents_md_structure(lang: str) -> None:
    """REQ-CONV-73, REQ-CONV-75, REQ-CONV-76 y REQ-ARCH-40."""
    text = src(lang, "AGENTS.md")
    assert len(text.encode("utf-8")) <= 8192 and text.count("\n") <= 150
    assert text.startswith("# ") and text.count("\n# ") == 0
    sections = numbered_sections(text)
    assert [n for n, _, _ in sections] == [1, 2, 3, 4, 5, 6, 7]
    assert len("\n".join(sections[6][2]).encode("utf-8")) <= 2048
    imports = re.findall(r"(?<![\w`])@[\w./-]+", text)
    assert imports == ["@memory/constitution.md"]
    before_start = "\n".join(sections[1][2])
    assert "aucove --version" in before_start and "aucove doctor" in before_start
    assert "{{var:" not in text


@pytest.mark.parametrize("lang", LANGS)
def test_claude_md(lang: str) -> None:
    """REQ-CONV-74 y REQ-HOOK-16; sin .claude/ ni skills en H2."""
    text = src(lang, "CLAUDE.md")
    assert text.startswith("@AGENTS.md\n")
    assert re.findall(r"(?<![\w`])@[\w./-]+", text) == ["@AGENTS.md"]
    assert len(text.encode("utf-8")) <= 1024
    assert "aucove doctor" in text


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("name", ["AGENTS.md", "CLAUDE.md"])
def test_agent_files_do_not_link_to_claude_dir_or_skills(lang: str, name: str) -> None:
    text = src(lang, name)
    assert not re.search(r"\]\([^)]*\.claude", text)
    assert not re.search(r"@\.?claude/", text)
    assert "skill" not in text.lower()
    names = "|".join(re.escape(n) for n in SKILL_NAMES)
    assert not re.search(rf"(?<![\w./-])/(?:{names})\b(?!/)", text)


@pytest.mark.parametrize("fmt", FMTS)
@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("name", ["AGENTS.md", "CLAUDE.md", "memory/constitution.md"])
def test_no_html_comments_or_invisible_characters(lang: str, name: str, fmt: str) -> None:
    """REQ-CONV-78, también en las variantes por formato."""
    text = localized(lang, name, fmt)
    assert "<!--" not in text
    bad = [f"U+{ord(ch):04X}" for ch in text if ch != "\n" and unicodedata.category(ch) in {"Cf", "Cc", "Co"}]
    assert not bad


@pytest.mark.parametrize("fmt", FMTS)
@pytest.mark.parametrize("name", ["AGENTS.md", "memory/constitution.md"])
def test_es_and_en_have_same_sections_and_bullets(name: str, fmt: str) -> None:
    """REQ-CONV-84, en el render de cada formato (la constitución tiene variante con none)."""
    es, en = (numbered_sections(localized(lang, name, fmt)) for lang in LANGS)
    assert [(n, bullets(lines)) for n, _, lines in es] == [(n, bullets(lines)) for n, _, lines in en]


def test_agents_md_es_matches_spec() -> None:
    """02-convencion §8.3 byte a byte; la convención va escrita, porque no es una variable del render."""
    expected = spec_block("02-convencion.md", "### 8.3", "```markdown")
    assert expected.count("{{var:convention}}") == 1
    assert src("es", "AGENTS.md") == expected.replace("{{var:convention}}", "0.1")


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("name", ["AGENTS.md", "README.md"])
def test_part_paths_are_format_neutral(lang: str, name: str) -> None:
    """D-43: la carpeta es sections/<section-id>/ y, solo con LaTeX, las rutas empiezan con ../sections/."""
    text = src(lang, name)
    assert "tex/sections" not in text
    assert "`sections/<section-id>/`" in text and "`../sections/<section-id>/`" in text
    assert "`format: latex`" in text
    part = {"es": "parte", "en": "part"}[lang]
    assert re.search(rf"\b{part}\b", text)


def test_agents_md_forbids_reading_openings() -> None:
    """02-convencion §8.3, sección 5: la viñeta de las aperturas está en las dos variantes."""
    for lang in LANGS:
        never = "\n".join(numbered_sections(src(lang, "AGENTS.md"))[4][2])
        assert "`openings.json`" in never and "`.aucove/evidence/`" in never, lang


def test_no_template_file_uses_the_old_section_path() -> None:
    """D-43: ningún archivo de la plantilla nombra tex/sections/ ni la forma de LaTeX anterior."""
    for path in all_template_files():
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        text = read(path)
        rel = path.relative_to(TEMPLATE).as_posix()
        assert "tex/sections" not in text, rel
        assert not re.search(r"\\(?:input|addbibresource|includegraphics)(?:\[[^\]]*\])?\{sections/", text), rel


@pytest.mark.parametrize("fmt", FMTS)
@pytest.mark.parametrize("lang", LANGS)
def test_constitution_structure(lang: str, fmt: str) -> None:
    """REQ-CONV-64, REQ-CONV-65 y REQ-SEC-68, y §7.1 según el formato (REQ-CONV-91)."""
    text = localized(lang, "memory/constitution.md", fmt)
    assert len(text.encode("utf-8")) <= 8192 and text.count("\n") <= 150
    sections = numbered_sections(text)
    assert [n for n, _, _ in sections] == [1, 2, 3, 4, 5, 6, 7]
    assert not re.search(r"(?<![\w`])@[\w./-]+", text)
    assert not any(PLAN_DEF.match(line) or PLAN_LOOKS.match(line) for line in text.split("\n"))
    assert "\\newcommand" not in text and "owner:" not in text
    integrity = "\n".join(sections[1][2])
    assert "`restricted`" in integrity and "`local_only`" in integrity
    truth = "\n".join(sections[6][2])
    for target in ("plan.md", "roles.yaml", "AGENTS.md", "decisions/"):
        assert target in truth, target
    part = {"es": "parte", "en": "part"}[lang]
    assert re.search(rf"\b{part}\b", truth)  # la carpeta de cada persona es su parte
    if fmt == "latex":
        assert "[tex/notation.tex](../tex/notation.tex)" in truth
        return
    # Con none, ni tex/, ni bib/, ni el PDF; la notación misma va en la sección 4 (02 §7.1).
    assert "tex/" not in text and "bib/" not in text and "pdf" not in text.lower() and ".tex" not in text
    assert {"es": "la sección 4", "en": "section 4"}[lang] in truth
    notation = "\n".join(sections[3][2])
    assert {"es": "en esta sección", "en": "in this section"}[lang] in notation


@pytest.mark.parametrize("lang", LANGS)
def test_none_constitution_keeps_the_headings_of_the_latex_one(lang: str) -> None:
    """La variante none cambia el contenido, no las siete secciones de §7.1."""
    latex, none = (numbered_sections(localized(lang, "memory/constitution.md", fmt)) for fmt in FMTS)
    assert [(n, title) for n, title, _ in latex] == [(n, title) for n, title, _ in none]
    assert localized(lang, "memory/constitution.md", "none").split("\n", 1)[0] == src(
        lang, "memory/constitution.md").split("\n", 1)[0]


def one_sentence_per_line_violations(lines: list[str]) -> list[str]:
    """Aproximación de conv.latex.sentence-per-line (REQ-CONV-27)."""
    abbrev = re.compile(r"\b(?:e\.g|i\.e|et al|etc|vs|cf|Fig|Eq|Sec|Tab|p|pp|No|Dr|Sr|Sra|aprox|núm|pág)\.$")
    out = []
    for line in lines:
        # Quita el encabezado markdown, la viñeta y la numeración ("## 1. ", "- ", "2. ").
        body = re.sub(r"^\s*(?:#+\s+)?(?:- |\d+\.\s+)?", "", line)
        for m in re.finditer(r"[.!?] +(?=[A-ZÁÉÍÓÚÑ¿¡\\])", body):
            if not abbrev.search(body[: m.start() + 1]):
                out.append(line)
    return out


@pytest.mark.parametrize("lang", LANGS)
def test_sentence_per_line_in_section_and_memory(lang: str) -> None:
    tex = strip_tex_comments(src(lang, "sections/introduction/introduction.tex")).split("\n")
    md = [line for fmt in FMTS for line in localized(lang, "memory/constitution.md", fmt).split("\n")]
    part = src(lang, "sections/introduction/introduction.md").split("\n")
    assert one_sentence_per_line_violations(tex + md + part) == []


@pytest.mark.parametrize("lang", LANGS)
def test_markdown_part_mirrors_the_latex_one(lang: str) -> None:
    """D-43: el texto de la parte de ejemplo con format: none, en Markdown y sin nada de LaTeX.

    Lo leen los agentes de otras personas (IL05 e IL08, 10-seguridad §3.6): sin comentarios
    HTML, sin caracteres invisibles y sin importaciones ``@``.
    """
    md = src(lang, "sections/introduction/introduction.md")
    title = {"es": "# Introducción", "en": "# Introduction"}[lang]
    assert md.split("\n", 1)[0] == title and md.count("\n# ") == 0
    assert "\\" not in md and "%" not in md and "<!--" not in md  # nada de LaTeX ni comentarios ocultos
    assert not re.search(r"(?<![\w`])@[\w./-]+", md)
    assert not [ch for ch in md if ch != "\n" and unicodedata.category(ch) in {"Cf", "Cc", "Co"}]
    assert "`figures/`" in md and "`tables/`" in md
    part = {"es": "parte", "en": "part"}[lang]
    assert re.search(rf"\b{part}\b", md) and "section" not in md.lower().replace("sections/", "")
    # Las dos variantes dicen lo mismo: misma estructura de párrafos.
    tex_body = [line for line in strip_tex_comments(src(lang, "sections/introduction/introduction.tex")).split("\n")
                if line.strip() and not line.startswith("\\")]
    assert tex_body[0].rstrip(".") in md  # la primera oración es la misma


# ---------------------------------------------------------------------------
# LaTeX y bibliografía
# ---------------------------------------------------------------------------


def test_main_tex_only_includes_files() -> None:
    """REQ-CONV-17, REQ-CONV-18 y REQ-CONV-19: las partes las incluyen los archivos de enlace (D-56)."""
    lines = [
        line for line in src(None, "tex/main.tex").split("\n") if line.strip() and not line.lstrip().startswith("%")
    ]
    assert lines == [
        r"\documentclass[11pt,a4paper]{article}",
        r"\input{preamble}",
        r"\input{notation}",
        r"\addbibresource{../bib/references.bib}",
        *(rf"\addbibresource{{../sections/{p}/pending.bib}}" for p in DEFAULT_PARTS),
        r"\begin{document}",
        r"\maketitle",
        *(rf"\input{{outline/{p}}}" for p in DEFAULT_PARTS),
        r"\printbibliography",
        r"\end{document}",
    ]


FORBIDDEN_IN_SECTION: tuple[str, ...] = (
    "write18", "ShellEscape", "directlua", "luadirect", "luaexec", "latelua", "inputminted",
    "openin", "openout", "read", "readline", "write", "immediate", "newread", "newwrite",
    "pdffiledump", "filedump", "pdfmdfivesum", "pdffilesize", "pdffilemoddate", "special", "pdfobj",
    "pdfximage", "pdfrefximage", "saveimageresource", "useimageresource", "XeTeXpdffile",
    "XeTeXpicfile", "InputIfFileExists", "IfFileExists", "def", "edef", "gdef", "xdef", "let",
    "futurelet", "newcommand", "renewcommand", "providecommand", "DeclareRobustCommand",
    "NewDocumentCommand", "RenewDocumentCommand", "ProvideDocumentCommand", "DeclareDocumentCommand",
    "NewDocumentEnvironment", "newenvironment", "renewenvironment", "DeclareMathOperator",
    "newtheorem", "definecolor", "newcounter", "newlength", "catcode", "csname", "expandafter",
    "scantokens", "makeatletter", "ExplSyntaxOn", "documentclass", "usepackage", "RequirePackage",
    "include", "includeonly", "import", "subimport", "subfile", "includestandalone", "addbibresource",
    "bibliography", "bibliographystyle", "printbibliography", "graphicspath", "hypersetup", "iffalse",
    "excludecomment", "includecomment",
    "part", "chapter", "section",  # REQ-CONV-94: el título lo pone el archivo de enlace
)


@pytest.mark.parametrize("lang", LANGS)
def test_section_file_follows_latex_rules(lang: str) -> None:
    """REQ-CONV-29, REQ-CONV-31, REQ-CONV-34 y REQ-CONV-45 sobre la parte de ejemplo."""
    body = strip_tex_comments(src(lang, "sections/introduction/introduction.tex"))
    commands = set(re.findall(r"\\([A-Za-z@]+)", body))
    assert not commands & set(FORBIDDEN_IN_SECTION)
    assert "^^" not in body and r"\begin{document}" not in body
    assert not re.search(r"\\begin\{(?:comment|luacode|minted)\}", body)
    labels = re.findall(r"\\label\{([^}]*)\}", body)
    label_re = r"(sec|fig|tab|eq|lst|alg|thm|itm|fn):introduction(?::[a-z0-9]+(?:-[a-z0-9]+)*)?"
    assert all(re.fullmatch(label_re, label) for label in labels)
    prefix = "../sections/introduction/"  # TX03: el único '..' es el del prefijo propio
    for arg in re.findall(r"\\(?:input|includegraphics)(?:\[[^\]]*\])?\{([^}]*)\}", body):
        assert arg.startswith(prefix) and ".." not in arg[len(prefix) :].split("/")
    keys = {k.strip() for group in re.findall(r"\\[a-zA-Z]*cite[a-z]*\*?(?:\[[^\]]*\])*\{([^}]*)\}", body)
            for k in group.split(",")}
    assert keys and keys <= bib_keys(src(None, "bib/references.bib")) | bib_keys(
        src(lang, "sections/introduction/pending.bib")
    )


@pytest.mark.parametrize("lang", LANGS)
def test_outline_files_follow_their_grammar(lang: str) -> None:
    """REQ-CONV-92 y REQ-CONV-93: una sección del artículo por parte, con su título y su etiqueta (D-56)."""
    for part, title in zip(DEFAULT_PARTS, PART_TITLES[lang], strict=True):
        body = strip_tex_comments(src(lang, f"tex/outline/{part}.tex"))
        lines = [line for line in body.split("\n") if line.strip()]
        assert lines == [rf"\section{{{title}}}", rf"\label{{sec:{part}}}", rf"\input{{../sections/{part}/{part}}}"]


@pytest.mark.parametrize("lang", LANGS)
def test_article_guide(lang: str) -> None:
    """REQ-CONV-95: la guía de ejemplo nombra cada sección con su archivo de enlace, en 16 KiB o menos."""
    text = src(lang, "tex/guide.md")
    assert len(text.encode("utf-8")) <= 16 * 1024 and "<!--" not in text
    assert text.startswith("# ") and "`tex/notation.tex`" in text
    for part, title in zip(DEFAULT_PARTS, PART_TITLES[lang], strict=True):
        assert f"### {title} (`tex/outline/{part}.tex`)" in text, part
    # Una oración por línea (REQ-CONV-26), fuera del código en línea.
    for line in text.split("\n"):
        prose = re.sub(r"`[^`]*`", "x", line)
        assert not re.search(r"[.!?] +[A-ZÁÉÍÓÚÑ¿¡]", prose), line


def bib_keys(text: str) -> set[str]:
    return set(re.findall(r"^@\w+\{([^,\s]+),", text, re.M))


def test_references_bib_has_two_example_entries() -> None:
    """02-convencion §5: 2 entradas de ejemplo, claves válidas y ordenadas, sin campos prohibidos."""
    text = src(None, "bib/references.bib")
    keys = re.findall(r"^@\w+\{([^,\s]+),", text, re.M)
    assert len(keys) == 2 and keys == sorted(keys)
    assert all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_:-]*", k) for k in keys)  # REQ-CONV-49
    fields = {f.lower() for f in re.findall(r"^\s+([A-Za-z]+)\s*=", text, re.M)}
    assert not fields & {"file", "abstract", "annotation", "keywords"}  # REQ-CONV-41


@pytest.mark.parametrize("lang", LANGS)
def test_pending_bib_has_only_comments(lang: str) -> None:
    text = src(lang, "sections/introduction/pending.bib")
    assert "@" not in text
    assert all(line.startswith("%") for line in text.split("\n") if line.strip())


@pytest.mark.parametrize("lang", LANGS)
def test_preamble_loads_biblatex_biber_and_babel(lang: str) -> None:
    """02-convencion §4.1 y REQ-CONV-20/23: sin definiciones ni paquetes de shell escape."""
    body = strip_tex_comments(src(lang, "tex/preamble.tex"))
    assert r"\usepackage[backend=biber,style=authoryear]{biblatex}" in body
    babel = {"es": "spanish", "en": "english"}[lang]
    assert rf"\usepackage[{babel}]{{babel}}" in body
    definitions = {
        "def", "edef", "gdef", "xdef", "let", "newcommand", "renewcommand", "providecommand",
        "DeclareMathOperator", "newtheorem", "newenvironment", "definecolor", "newcounter", "newlength",
        "catcode", "csname", "expandafter", "NewDocumentCommand", "input", "include", "label",
    }
    assert not set(re.findall(r"\\([A-Za-z@]+)", body)) & definitions
    packages = {p.strip() for group in re.findall(r"\\usepackage(?:\[[^\]]*\])?\{([^}]*)\}", body)
                for p in group.split(",")}
    assert not packages & {"shellesc", "svg", "pythontex", "gnuplottex", "auto-pst-pdf", "minted"}


def test_notation_has_no_packages_or_inputs() -> None:
    """REQ-CONV-21."""
    body = strip_tex_comments(src(None, "tex/notation.tex"))
    commands = set(re.findall(r"\\([A-Za-z@]+)", body))
    assert not commands & {"usepackage", "RequirePackage", "input", "include", "label"}
    assert commands & {"newcommand", "DeclareMathOperator"}


# ---------------------------------------------------------------------------
# .gitignore, .gitattributes y .latexmkrc
# ---------------------------------------------------------------------------


def test_gitignore_required_lines() -> None:
    """REQ-CONV-81, REQ-MVP-5 y REQ-SEC-35."""
    lines = src(None, ".gitignore").split("\n")
    for line in GITIGNORE_REQUIRED:
        assert line in lines, line
    assert "/.aucove/" in lines and "/.claude/settings.local.json" in lines
    start = lines.index("# [req] secretos (defensa en profundidad)")
    assert lines[start + 1 : start + 9] == [".env", ".env.*", "*.pem", *GITIGNORE_SEC35_EXTRA]
    assert lines[start + 9].startswith("# ")


def test_gitattributes_lines() -> None:
    """REQ-CONV-82 y REQ-CONV-63."""
    text = src(None, ".gitattributes")
    assert tuple(text.rstrip("\n").split("\n")) == GITATTRIBUTES_LINES
    assert "filter=lfs" not in text


def test_latexmkrc_is_canonical() -> None:
    """REQ-CONV-36 con build.engine: pdflatex."""
    assert src(None, ".latexmkrc") == LATEXMKRC


def gitignore_expected_from_spec() -> str:
    """Bloque de 02-convencion §9 con los patrones de REQ-SEC-35 que le falten tras ``*.pem``.

    La spec se contradice: 02 §9 trae `.env`, `.env.*` y `*.pem`, y 10-seguridad REQ-SEC-35
    (DEBE) exige además los de ``GITIGNORE_SEC35_EXTRA``. Si 02 §9 se enmienda, no se duplican.
    """
    lines = spec_block("02-convencion.md", "## 9.", "```gitignore").split("\n")
    sec35 = spec_file("10-seguridad.md") or ""
    req = next(line for line in sec35.split("\n") if line.startswith("- **REQ-SEC-35.**"))
    for pattern in GITIGNORE_SEC35_EXTRA:
        assert f"`{pattern}`" in req, pattern
    missing = [p for p in GITIGNORE_SEC35_EXTRA if p not in lines]
    at = lines.index("*.pem") + 1
    return "\n".join(lines[:at] + missing + lines[at:])


def test_git_files_match_spec_blocks() -> None:
    assert src(None, ".gitignore") == gitignore_expected_from_spec()
    assert src(None, ".gitattributes") == spec_block("02-convencion.md", "## 9.", "```gitattributes")
    assert src(None, ".latexmkrc") == spec_block("02-convencion.md", "### 4.7", "```perl")


# ---------------------------------------------------------------------------
# Licencias y aviso
# ---------------------------------------------------------------------------


def test_reuse_toml_is_exactly_the_one_of_11_7_2() -> None:
    text = src(None, "REUSE.toml")
    assert text == REUSE_TOML
    data = tomllib.loads(text)
    assert data["version"] == 1
    assert [a[_SPDX + "License-Identifier"] for a in data["annotations"]] == ["CC0-1.0", "MIT-0", "CC0-1.0"]


def test_reuse_toml_matches_spec_block() -> None:
    assert src(None, "REUSE.toml") == spec_block("11-trazabilidad.md", "- **REQ-TRACE-60:**", "```toml")


def test_license_files() -> None:
    """REQ-TRACE-60: LICENSE con MIT-0 y LICENSES/ con los dos textos que usa REUSE.toml."""
    assert sorted(files_under(BASE / "LICENSES")) == ["CC0-1.0.txt", "MIT-0.txt"]
    mit0 = src(None, "LICENSES/MIT-0.txt")
    assert mit0.startswith("MIT No Attribution\n\nCopyright <year> <copyright holders>\n\n")
    assert "Permission is hereby granted, free of charge" in mit0
    assert src(None, "LICENSE") == mit0.replace(
        "Copyright <year> <copyright holders>", "Copyright 2026 The aucove Authors", 1
    )
    cc0 = src(None, "LICENSES/CC0-1.0.txt")
    assert cc0.startswith("Creative Commons Legal Code\n\nCC0 1.0 Universal\n")
    assert "Statement of Purpose" in cc0 and "4. Limitations and Disclaimers." in cc0
    assert cc0.rstrip().endswith("this CC0 or use of the Work.")
    used = {a[_SPDX + "License-Identifier"] for a in tomllib.loads(src(None, "REUSE.toml"))["annotations"]}
    assert {f"{u}.txt" for u in used} == files_under(BASE / "LICENSES")


@pytest.mark.parametrize("lang", LANGS)
def test_readme_has_non_affiliation_notice(lang: str) -> None:
    """REQ-TRACE-71."""
    text = src(lang, "README.md")
    assert NOTICE[lang] in text
    assert not re.search(r"!\[[^\]]*\]\(", text), "sin logos ni imágenes de terceros"


def test_notice_matches_spec() -> None:
    text = spec_file("11-trazabilidad.md")
    if text is None:
        pytest.skip("AUCOVE_SPEC_DIR no está definido")
    for lang, notice in NOTICE.items():
        assert f"> **{lang}:** {notice}" in text


# ---------------------------------------------------------------------------
# .github, data/README.md
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang", LANGS)
def test_pull_request_template(lang: str) -> None:
    """03-esquemas §9.1: bloque pr-meta vacío (task: 0) y ninguna línea que empiece con ---."""
    text = src(lang, ".github/pull_request_template.md")
    begin, end = "<!-- aucove:pr-meta-begin v1 -->", "<!-- aucove:pr-meta-end v1 -->"
    assert text.count(begin) == 1 and text.count(end) == 1
    m = re.search(re.escape(begin) + r"[ \t]*\n```ya?ml[ \t]*\n(.*?)\n```[ \t]*\n" + re.escape(end), text, re.S)
    assert m
    assert yaml.safe_load(m.group(1)) == {"schema": 1, "task": 0, "sections": [], "plan_refs": [], "decisions": []}
    assert not any(line.startswith("---") for line in text.split("\n"))
    assert "Closes #<n>" in text
    assert len(re.findall(r"^## ", text, re.M)) == 6


def test_pull_request_template_es_matches_spec() -> None:
    expected = spec_block("03-esquemas.md", "### 9.1", "````markdown")
    assert src("es", ".github/pull_request_template.md") == expected


@pytest.mark.parametrize("lang", LANGS)
def test_issue_template_config(lang: str) -> None:
    """REQ-SCHEMA-66."""
    data = yaml.safe_load(render(src(lang, ".github/ISSUE_TEMPLATE/config.yml"), lang))
    assert data["blank_issues_enabled"] is False
    for link in data.get("contact_links", []):
        assert set(link) == {"name", "url", "about"}
        assert link["url"].startswith(f"https://github.com/{render_values(lang)['forge_repo']}")


@pytest.mark.parametrize("lang", LANGS)
def test_data_readme_table(lang: str) -> None:
    """REQ-CONV-53: tabla con exactamente las columnas fijas."""
    lines = src(lang, "data/README.md").split("\n")
    header = "| id | description | location | access | classification | version |"
    assert lines.count(header) == 1
    assert lines[lines.index(header) + 1] == "|---|---|---|---|---|---|"
    assert sum(1 for line in lines if line.startswith("|")) == 2


# ---------------------------------------------------------------------------
# Render completo y reuse lint (opcional)
# ---------------------------------------------------------------------------


def render_tree(lang: str, out: Path) -> list[str]:
    """Render mínimo: base + l10n/<lang>, con {{var:...}} sustituido (sin los archivos generados)."""
    written: list[str] = []
    for root in (BASE, L10N / lang):
        for rel in sorted(files_under(root)):
            target = out / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(render(read(root / rel), lang).encode("utf-8"))
            written.append(rel)
    return sorted(written)


def find_reuse() -> str | None:
    env = os.environ.get("AUCOVE_REUSE")
    if env and Path(env).is_file():
        return env
    exe = "reuse.exe" if sys.platform == "win32" else "reuse"
    scripts = "Scripts" if sys.platform == "win32" else "bin"
    local = REPO_ROOT / ".venv-tools" / scripts / exe
    if local.is_file():
        return str(local)
    return shutil.which("reuse")


@pytest.mark.parametrize("lang", LANGS)
def test_rendered_template_passes_reuse_lint(lang: str, tmp_path: Path) -> None:
    """REQ-TRACE-58: reuse lint sobre el render (sin red; se omite si no hay reuse)."""
    reuse = find_reuse()
    if reuse is None:
        pytest.skip("reuse no está instalado (.venv-tools, PATH o AUCOVE_REUSE)")
    out = tmp_path / "render"
    render_tree(lang, out)
    proc = subprocess.run(
        [reuse, "--root", str(out), "lint"], capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
