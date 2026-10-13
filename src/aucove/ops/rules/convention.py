# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Regla ``convention`` de ``validate``: los ``conv.*`` de 02-convencion §12 (D-33 g).

Cubre los ``conv.*`` que no tienen regla propia en 09-ci §4.1 (``tree``, ``format``,
``path``, ``section``, ``file``, ``memory``, ``gitignore``, ``gitattributes``, ``data``,
``provenance``, ``notebook``, ``convention``, ``agents``, ``claude``,
``constitution`` y ``latexmkrc``) y ``E_VALIDATION_LICENSE_UNKNOWN`` (11-trazabilidad
REQ-TRACE-64; el texto de una licencia solo se pide si su categoría ya tiene archivos que REUSE
cubre, 00-marco D-64). Los ``conv.latex.*``, ``conv.main.*`` y ``conv.bib.*`` van en otro
``RuleSpec`` con el mismo id (``ops.rules.latex``); el motor los une en un solo
``results[]``.

Todo se evalúa sobre ``RuleInput.tree`` sin ejecutar nada ni seguir enlaces
(09 REQ-CI-24): las entradas y sus modos salen de ``TreeView.entries()``, que en
``pr`` y ``base`` son los de ``git ls-tree``. Las partes declaradas (``sections`` de
``roles.yaml``; su carpeta es ``sections/<id>/``, D-43) salen de ``roles.yaml`` de head
si el árbol lo modifica y es válido y, si no, de la base (03-esquemas §2.5); el formato
(``project.yaml: format``, D-43) se resuelve igual (``tree_format``). Con ``format:
none``, la tabla de tipos de 02 §4.2 es la de su columna (REQ-CONV-89), ``conv.latexmkrc.*``
queda en ``skip`` y ``conv.format.unused`` avisa de lo que sobra del manuscrito
(REQ-CONV-88). ``conv.tree.missing`` lee la lista de obligatorios de
``required-paths.json``, la misma fuente filtrada por hito que el manifiesto
(D-36, temporal hasta H4), con su filtro por formato.

Cada comprobación corre aislada: una excepción inesperada en una de ellas se
convierte en un hallazgo ``E_INTERNAL_UNEXPECTED`` (``unavailable``) sin el texto de
la excepción, y las demás siguen.
"""

from __future__ import annotations

import functools
import json
import re
import weakref
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Final
from urllib.parse import parse_qsl, urlsplit

from ... import _meta
from ...config import datafiles
from ...config.yamlload import line_for, load_with_lines, shown
from ...domain import licenses as dlicenses
from ...domain import paths as dpaths
from ...domain import plan as dplan
from ...domain.findings import Finding, Severity
from ...i18n import LANGS, Lang
from .base import RuleInput, RuleSpec, make_finding, unavailable

if TYPE_CHECKING:
    from ...config.roles import Roles
    from ...vcs.tree import TreeView

RULE_ID: Final[str] = "convention"

KIB: Final[int] = 1024
MIB: Final[int] = 1024 * 1024
LARGE_FILE: Final[int] = 1 * MIB  # REQ-CONV-61: aviso
MAX_FILE: Final[int] = 5 * MIB  # REQ-CONV-61: error (y tope de lectura de REQ-CI-24)
LARGE_SECTION: Final[int] = 20 * MIB  # REQ-CONV-62
NOTE_WARN: Final[int] = 32 * KIB  # REQ-CONV-69
AGENTS_MAX_BYTES: Final[int] = 8192  # REQ-CONV-73
AGENTS_MAX_LINES: Final[int] = 150
AGENTS_SECTION7_MAX: Final[int] = 2048  # REQ-CONV-75
CLAUDE_MAX_BYTES: Final[int] = 1024  # REQ-CONV-74
CONSTITUTION_MAX_BYTES: Final[int] = 8192  # REQ-CONV-64
CONSTITUTION_MAX_LINES: Final[int] = 150

SYMLINK_MODE: Final[str] = "120000"
GITLINK_MODE: Final[str] = "160000"

# Código de hallazgo -> (severidad, marcadores de finding.<code>.*).
_SPECS: Final[Mapping[str, tuple[Severity, frozenset[str]]]] = MappingProxyType(
    {
        "conv.tree.missing": ("error", frozenset({"file"})),
        "conv.format.unused": ("warning", frozenset({"unused"})),
        "conv.path.invalid-name": ("error", frozenset({"segment"})),
        "conv.path.case-collision": ("error", frozenset({"other"})),
        "conv.path.symlink": ("error", frozenset({"mode"})),
        "conv.path.local-committed": ("error", frozenset()),
        "conv.path.nested-instructions": ("error", frozenset()),
        "conv.path.foreign-instructions": ("warning", frozenset()),
        "conv.section.undeclared": ("error", frozenset({"section"})),
        "conv.section.missing": ("error", frozenset({"section", "files"})),
        "conv.section.disallowed-file": ("error", frozenset({"allowed"})),
        "conv.section.large": ("warning", frozenset({"section", "size"})),
        "conv.file.large": ("warning", frozenset({"size"})),
        "conv.file.too-large": ("error", frozenset()),
        "conv.file.encoding": ("error", frozenset({"problem"})),
        "conv.memory.unexpected-file": ("error", frozenset()),
        "conv.memory.note-large": ("warning", frozenset({"size"})),
        "conv.gitignore.required": ("error", frozenset({"entry"})),
        "conv.gitattributes.required": ("error", frozenset({"entry"})),
        "conv.gitattributes.lfs": ("error", frozenset()),
        "conv.data.unexpected-file": ("error", frozenset()),
        "conv.data.index": ("error", frozenset({"problem"})),
        "conv.data.credential-url": ("warning", frozenset({"param"})),
        "conv.provenance.missing": ("warning", frozenset()),
        "conv.provenance.invalid": ("error", frozenset({"problem"})),
        "conv.notebook.outputs": ("error", frozenset({"problem"})),
        "conv.convention.value": ("error", frozenset({"problem"})),
        "conv.agents.structure": ("error", frozenset({"problem"})),
        "conv.agents.import": ("error", frozenset({"target"})),
        "conv.agents.modified": ("warning", frozenset({"number"})),
        "conv.claude.content": ("error", frozenset({"problem"})),
        "conv.constitution.structure": ("error", frozenset({"problem"})),
        "conv.latexmkrc.noncanonical": ("warning", frozenset({"engine"})),
        "E_VALIDATION_LICENSE_UNKNOWN": ("error", frozenset({"category", "value", "problem"})),
    }
)
CODES: Final[Mapping[str, frozenset[str]]] = MappingProxyType({c: p for c, (_s, p) in _SPECS.items()})
SEVERITY: Final[Mapping[str, Severity]] = MappingProxyType({c: s for c, (s, _p) in _SPECS.items()})

# -- datos de la convención 0.1 --------------------------------------------------------

# 02 §9: líneas de los bloques [req] de .gitignore (REQ-CONV-81). La carpeta local sale de _meta.
# El bloque [req] secretos sigue a 10-seguridad REQ-SEC-35 (DEBE), en su orden: además de
# .env, .env.* y *.pem de 02 §9 exige *.key, *.p12, *.pfx, id_rsa* e id_ed25519*, que la
# plantilla ya trae (pendiente de enmienda en 02 §9 para que los dos capítulos coincidan).
GITIGNORE_REQUIRED: Final[tuple[str, ...]] = (
    "/" + _meta.CACHE_DIR + "/",
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
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
)
# 02 §9: todas las líneas de .gitattributes (REQ-CONV-82), sin el comentario.
GITATTRIBUTES_REQUIRED: Final[tuple[str, ...]] = (
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
# 02 §4.7: .latexmkrc canónico; la línea de $pdf_mode depende de build.engine (REQ-CONV-36).
_LATEXMKRC_LINES: Final[tuple[str, ...]] = (
    "# .latexmkrc (clase lead). Equivale a la compilación de referencia (02-convencion §4.7).",
    "@default_files = ('tex/main.tex');",
    "$do_cd = 1;",
    "$out_dir = 'build';",
    "$pdf_mode = 1;   # 1 = pdflatex, 5 = xelatex, 4 = lualatex: igual que project.yaml build.engine",
    "set_tex_cmds('%O -no-shell-escape -interaction=nonstopmode -halt-on-error -file-line-error %S');",
    "$ENV{'openout_any'} = 'p';",
    "$ENV{'shell_escape'} = 'f';",
)
PDF_MODE: Final[Mapping[str, int]] = MappingProxyType({"pdflatex": 1, "xelatex": 5, "lualatex": 4})
DEFAULT_ENGINE: Final[str] = "pdflatex"

# 02 §4: valores fijos de build.* en la convención 0.1 (REQ-CONV-85).
BUILD_FIXED: Final[Mapping[str, object]] = MappingProxyType(
    {"main": "tex/main.tex", "bibliography": "biber", "bib_files": ["bib/references.bib"]}
)

# REQ-CONV-38: extensiones de texto (en minúsculas; .R y .r cuentan igual).
TEXT_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".tex", ".bib", ".md", ".yaml", ".yml", ".json", ".csv", ".tsv", ".py", ".r", ".toml", ".ipynb"}
)
# REQ-CONV-39.
NAMED_TREES: Final[frozenset[str]] = frozenset({"sections", "tex", "memory", "bib", "data"})
# Carpeta de las partes en la raíz: sections/<id>/ (R6 de 02 §3.3; D-43).
SECTIONS_DIR: Final[str] = "sections"
SEGMENT: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._-]+")
# REQ-CONV-80 (sin distinguir mayúsculas, como R2).
FOREIGN_ROOT_FILES: Final[frozenset[str]] = frozenset(
    {"agents.override.md", "gemini.md", ".goosehints", ".cursorrules", ".windsurfrules", ".clinerules"}
)
FOREIGN_ROOT_DIRS: Final[frozenset[str]] = frozenset({".cursor"})

# project.yaml: format (D-43). Con none no hay manuscrito: sin tex/, bib/, .latexmkrc ni pending.bib.
FORMAT_LATEX: Final[str] = "latex"
FORMAT_NONE: Final[str] = "none"
FORMATS: Final[tuple[str, ...]] = (FORMAT_LATEX, FORMAT_NONE)
DEFAULT_FORMAT: Final[str] = FORMAT_LATEX  # sin project.yaml válido en el árbol ni en la base
PENDING_BIB: Final[str] = "pending.bib"

# 02 §4.2: archivos permitidos por ubicación dentro de sections/<id>/ (REQ-CONV-25), columna
# format: latex, con notes/ y los .md de la parte de D-43.
_SECTION_ROOT: Final[tuple[str, ...]] = ("*.tex", PENDING_BIB, "*.md", "provenance.yaml", ".gitkeep")
_SECTION_FIGURES: Final[tuple[str, ...]] = ("*.pdf", "*.png", "*.jpg", "*.jpeg", "*.tex", ".gitkeep")
_SECTION_TABLES: Final[tuple[str, ...]] = ("*.tex", "*.csv", ".gitkeep")
_SECTION_ANALYSIS: Final[tuple[str, ...]] = (
    "*.py", "*.R", "*.jl", "*.m", "*.do", "*.sh", "*.ipynb", "*.md", "*.txt", "*.toml", "*.yaml", "*.yml", ".gitkeep",
)
_SECTION_DERIVED: Final[tuple[str, ...]] = ("*.csv", "*.tsv", "*.json", ".gitkeep")
_SECTION_NOTES: Final[tuple[str, ...]] = ("*.md", "*.txt", ".gitkeep")
_SECTION_OTHER: Final[tuple[str, ...]] = ("*.tex", "*.md", ".gitkeep")
# Columna format: none de la misma tabla: ningún .tex (REQ-CONV-89) ni pending.bib; las tablas,
# en CSV o Markdown. analysis/, analysis/derived/ y notes/ son iguales en los dos formatos.
_NONE_ROOT: Final[tuple[str, ...]] = ("*.md", "provenance.yaml", ".gitkeep")
_NONE_FIGURES: Final[tuple[str, ...]] = ("*.pdf", "*.png", "*.jpg", "*.jpeg", ".gitkeep")
_NONE_TABLES: Final[tuple[str, ...]] = ("*.csv", "*.md", ".gitkeep")
_NONE_OTHER: Final[tuple[str, ...]] = ("*.md", ".gitkeep")
# REQ-CONV-88: lo que sobra con format: none (conv.format.unused). pending.bib, en la raíz de cada parte.
UNUSED_WITHOUT_MANUSCRIPT: Final[tuple[str, ...]] = ("tex/", "bib/", ".latexmkrc")
MAX_UNUSED_SHOWN: Final[int] = 5  # el hint tiene un tope de 500 caracteres (MAX_TEXT)

# 02 §6.1 y 10-seguridad §8.1.
DATA_COLUMNS: Final[tuple[str, ...]] = ("id", "description", "location", "access", "classification", "version")
DATA_LEVELS: Final[tuple[str, ...]] = ("public", "internal", "restricted", "local_only")
DATASET_ID: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
CREDENTIAL_PARAMS: Final[frozenset[str]] = frozenset(
    {"token", "key", "sig", "signature", "access_token", "x-amz-signature"}
)
_URL: Final[re.Pattern[str]] = re.compile(r"https?://[^\s<>()\[\]|`\"']+", re.IGNORECASE)

# 02 §6.4: provenance.yaml.
PROV_TOP_KEYS: Final[frozenset[str]] = frozenset({"version", "outputs"})
PROV_ENTRY_KEYS: Final[frozenset[str]] = frozenset({"path", "kind", "generator", "inputs", "credit"})
PROV_KINDS: Final[tuple[str, ...]] = ("figure", "table", "data")
_PROV_FIGURES: Final[tuple[str, ...]] = (".pdf", ".png", ".jpg", ".jpeg")

# 11-trazabilidad REQ-TRACE-64: identificador SPDX (sin el operador '+') o LicenseRef-*.
LICENSE_ID: Final[re.Pattern[str]] = dlicenses.LICENSE_ID
LICENSE_CATEGORIES: Final[tuple[str, ...]] = dlicenses.CATEGORIES

# Markdown: encabezados ATX, secciones numeradas, importaciones @ y definiciones de macros.
# Todas corren en tiempo lineal (09 REQ-CI-24). ``_HEADING`` se aplica con ``fullmatch`` a la
# línea sin espacios ni tabuladores finales: ``(.*)`` llega al final en el primer intento, sin
# retroceso (la forma anterior, ``(.*?)`` seguido de ``[ \t]*$``, era cuadrática con espacios
# dentro del título). El código en línea se quita con ``strip_code_spans`` y no con
# ``(`+).*?\1``, que era cúbica ante un run largo de acentos graves sin cerrar.
_HEADING: Final[re.Pattern[str]] = re.compile(r"(#{1,6})(?:[ \t]+(.*))?")
_NUMBERED: Final[re.Pattern[str]] = re.compile(r"^([1-9][0-9]*)\. \S")
_BACKTICKS: Final[re.Pattern[str]] = re.compile(r"`+")
_IMPORT: Final[re.Pattern[str]] = re.compile(r"(?<![\w@.`/\\-])@([A-Za-z0-9_~./\\-][^\s`]*)")
_IMPORT_TRAIL: Final[str] = ".,;:!?)]}>\"'"
_MACRO_DEF: Final[re.Pattern[str]] = re.compile(
    r"\\(newcommand|renewcommand|providecommand|DeclareRobustCommand|DeclareMathOperator"
    r"|NewDocumentCommand|RenewDocumentCommand|ProvideDocumentCommand|DeclareDocumentCommand"
    r"|newenvironment|renewenvironment|NewDocumentEnvironment|RenewDocumentEnvironment"
    r"|newtheorem|def|gdef|edef|xdef|let)(?![A-Za-z])"
)
AGENTS_IMPORT: Final[str] = "memory/constitution.md"  # REQ-CONV-76
CLAUDE_FIRST_LINE: Final[str] = "@AGENTS.md"  # REQ-CONV-74


# -- funciones puras ------------------------------------------------------------------


def canonical_latexmkrc(engine: str) -> bytes:
    """``.latexmkrc`` de 02 §4.7 con el ``$pdf_mode`` de ``engine`` (UTF-8, LF, salto final)."""
    mode = PDF_MODE.get(engine, PDF_MODE[DEFAULT_ENGINE])
    lines = [
        line.replace("$pdf_mode = 1;", f"$pdf_mode = {mode};", 1) if line.startswith("$pdf_mode") else line
        for line in _LATEXMKRC_LINES
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def normalize_text(text: str) -> str:
    """Sin BOM y con LF (la codificación la juzga ``conv.file.encoding``)."""
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def line_count(text: str) -> int:
    """Líneas de un texto con LF (la última cuenta aunque no tenga salto final)."""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def encoding_problem(data: bytes) -> tuple[str, int] | None:
    """``(problema, línea)`` de REQ-CONV-38: ``not_utf8``, ``bom`` o ``crlf``; ``None`` si cumple."""
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return "not_utf8", data.count(b"\n", 0, exc.start) + 1
    if data.startswith(b"\xef\xbb\xbf"):
        return "bom", 1
    cr = data.find(b"\r")
    if cr >= 0:
        return "crlf", data.count(b"\n", 0, cr) + 1
    return None


def is_text_path(path: str) -> bool:
    """``True`` si la extensión es de un archivo de texto de REQ-CONV-38."""
    name = path.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    return dot > 0 and name[dot:].lower() in TEXT_SUFFIXES


def matches(name: str, pattern: str) -> bool:
    """``*.ext`` o nombre exacto, distinguiendo mayúsculas (02 §3.2, REQ-CONV-8)."""
    if pattern.startswith("*"):
        return name.endswith(pattern[1:])
    return name == pattern


def section_patterns(parts: Sequence[str], fmt: str = FORMAT_LATEX) -> tuple[str, ...]:
    """Patrones permitidos para un archivo de ``sections/<id>/`` (``parts`` relativo a la carpeta).

    La tabla de 02 §4.2 tiene una columna por formato (D-43): con ``none`` no se admite ningún
    ``.tex`` (REQ-CONV-89) ni ``pending.bib``, y las tablas van en CSV o en Markdown.
    """
    none = fmt == FORMAT_NONE
    if len(parts) == 1:
        return _NONE_ROOT if none else _SECTION_ROOT
    top = parts[0]
    if top == "figures":
        return _NONE_FIGURES if none else _SECTION_FIGURES
    if top == "tables":
        return _NONE_TABLES if none else _SECTION_TABLES
    if top == "analysis":
        if len(parts) > 2 and parts[1] == "derived":
            return _SECTION_DERIVED
        return _SECTION_ANALYSIS
    if top == "notes":
        return _SECTION_NOTES
    return _NONE_OTHER if none else _SECTION_OTHER


def needs_provenance(rel: str) -> bool:
    """REQ-CONV-57: salidas que deberían estar en ``provenance.yaml`` (ruta relativa a la parte)."""
    parts = rel.split("/")
    name = parts[-1]
    if len(parts) < 2 or name == ".gitkeep":
        return False
    if parts[0] == "figures":
        return name.endswith(_PROV_FIGURES)
    if parts[0] == "tables":
        return name.endswith(".csv")
    return len(parts) > 2 and parts[0] == "analysis" and parts[1] == "derived"


def section_rel(value: object) -> str | None:
    """Ruta relativa a la carpeta de la parte, sin ``..`` ni absolutas; ``None`` si no lo es."""
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        return None
    if any(seg in ("", ".", "..") for seg in value.split("/")):
        return None
    return value


def strip_code_spans(line: str) -> str:
    """``line`` sin código en línea, en tiempo lineal (09 REQ-CI-24).

    Como en CommonMark, un run de acentos graves abre código en línea y lo cierra el
    siguiente run de la misma longitud; un run sin cierre es texto literal. Trabaja sobre
    una línea: el código en línea no cruza saltos de línea.
    """
    if "`" not in line:
        return line
    runs = [(m.start(), m.end()) for m in _BACKTICKS.finditer(line)]
    by_length: dict[int, list[int]] = {}  # longitud -> índices de los runs de esa longitud
    for i, (start, end) in enumerate(runs):
        by_length.setdefault(end - start, []).append(i)
    cursor: dict[int, int] = {}  # longitud -> primer índice de by_length[longitud] no descartado
    out: list[str] = []
    last = 0
    i = 0
    while i < len(runs):
        start, end = runs[i]
        same = by_length[end - start]
        k = cursor.get(end - start, 0)
        while k < len(same) and same[k] <= i:  # cada cursor solo avanza: costo total lineal
            k += 1
        cursor[end - start] = k
        if k == len(same):
            i += 1  # sin cierre: texto literal
            continue
        close = same[k]
        out.append(line[last:start])
        last = runs[close][1]
        i = close + 1
    out.append(line[last:])
    return "".join(out)


def outside_code(text: str) -> Iterator[tuple[int, str]]:
    """``(línea, texto)`` de las líneas fuera de bloques de código delimitados, sin código en línea."""
    fence: str | None = None
    for no, line in enumerate(text.split("\n"), 1):
        s = line.strip()
        if fence is not None:
            if s.startswith(fence):
                fence = None
            continue
        if s.startswith(("```", "~~~")):
            fence = s[:3]
            continue
        yield no, strip_code_spans(line)


def imports(text: str) -> list[tuple[int, str]]:
    """Importaciones ``@ruta`` de Claude Code fuera de código: ``(línea, destino)``."""
    out: list[tuple[int, str]] = []
    for no, line in outside_code(text):
        for m in _IMPORT.finditer(line):
            target = m.group(1).rstrip(_IMPORT_TRAIL)
            if target:
                out.append((no, target))
    return out


@dataclass(frozen=True, slots=True)
class Heading:
    level: int
    title: str
    line: int


def headings(text: str) -> list[Heading]:
    """Encabezados ATX fuera de bloques de código."""
    out: list[Heading] = []
    fence: str | None = None
    for no, line in enumerate(text.split("\n"), 1):
        s = line.strip()
        if fence is not None:
            if s.startswith(fence):
                fence = None
            continue
        if s.startswith(("```", "~~~")):
            fence = s[:3]
            continue
        m = _HEADING.fullmatch(line.rstrip(" \t"))
        if m:
            out.append(Heading(len(m.group(1)), m.group(2) or "", no))
    return out


def numbered_sections(text: str) -> tuple[dict[int, tuple[int, int]], list[Heading]]:
    """Secciones ``## <n>. <título>``: ``n -> (primera línea, última línea)`` y los ``##`` de más.

    Una sección va de su encabezado hasta la línea anterior al siguiente encabezado de
    nivel 1 o 2 (o hasta el final). Devuelve también los encabezados de nivel 2 que no
    siguen la numeración 1, 2, 3... en orden (``bad``).
    """
    lines = text.split("\n")
    total = len(lines) - 1 if text.endswith("\n") else len(lines)
    top = [h for h in headings(text) if h.level <= 2]
    sections: dict[int, tuple[int, int]] = {}
    bad: list[Heading] = []
    expected = 1
    for i, h in enumerate(top):
        if h.level != 2:
            continue
        end = top[i + 1].line - 1 if i + 1 < len(top) else total
        m = _NUMBERED.match(h.title)
        if m is None or int(m.group(1)) != expected:
            bad.append(h)
            continue
        sections[expected] = (h.line, end)
        expected += 1
    return sections, bad


def block(text: str, span: tuple[int, int]) -> str:
    """Líneas ``span`` (1-based, inclusivas), sin espacios finales ni líneas vacías al final."""
    lines = text.split("\n")[span[0] - 1 : span[1]]
    out = [line.rstrip() for line in lines]
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def split_cells(line: str) -> list[str]:
    """Celdas de una fila de tabla Markdown con ``|`` (admite ``\\|`` escapado)."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s)]


def credential_params(text: str) -> list[str]:
    """Parámetros de credencial en las URL de ``text`` (REQ-CONV-55), en orden y sin repetir."""
    out: list[str] = []
    for m in _URL.finditer(text):
        try:
            parts = urlsplit(m.group(0))
        except ValueError:
            continue
        for query in (parts.query, parts.fragment):
            for key, _value in parse_qsl(query, keep_blank_values=True):
                if key.lower() in CREDENTIAL_PARAMS and key not in out:
                    out.append(key)
    return out


@dataclass(frozen=True)
class DataIndex:
    """Resultado de leer la tabla de ``data/README.md``."""

    ids: frozenset[str]
    problems: tuple[tuple[str, Mapping[str, str], int | None], ...]  # (clave, marcadores, línea)
    locations: tuple[tuple[str, int], ...]  # (celda location, línea)


def parse_data_index(text: str) -> DataIndex:
    """Tabla de 02 §6.1: columnas exactas, ``id`` único y válido, nivel de 10 §8.1 (REQ-CONV-53/54)."""
    problems: list[tuple[str, Mapping[str, str], int | None]] = []
    locations: list[tuple[str, int]] = []
    lines = text.split("\n")
    start: int | None = None
    for no, line in outside_code(text):
        if line.strip().startswith("|"):
            start = no - 1
            break
    if start is None:
        return DataIndex(frozenset(), (("no_table", {}, None),), ())
    if split_cells(lines[start]) != list(DATA_COLUMNS):
        problems.append(("header", {"columns": " | ".join(DATA_COLUMNS)}, start + 1))
        return DataIndex(frozenset(), tuple(problems), ())
    sep = split_cells(lines[start + 1]) if start + 1 < len(lines) else []
    if len(sep) != len(DATA_COLUMNS) or not all(re.fullmatch(r":?-+:?", c) for c in sep):
        problems.append(("separator", {}, start + 2))
        return DataIndex(frozenset(), tuple(problems), ())
    seen: dict[str, int] = {}
    for j in range(start + 2, len(lines)):
        if not lines[j].strip().startswith("|"):
            break
        no = j + 1
        cells = split_cells(lines[j])
        if len(cells) != len(DATA_COLUMNS):
            problems.append(("row", {"count": str(len(cells)), "expected": str(len(DATA_COLUMNS))}, no))
            continue
        rid, _description, location, _access, level, _version = cells
        if not DATASET_ID.fullmatch(rid):
            problems.append(("id", {"id": rid}, no))
        elif rid in seen:
            problems.append(("duplicate", {"id": rid, "first": str(seen[rid])}, no))
        else:
            seen[rid] = no
        if level not in DATA_LEVELS:
            problems.append(("level", {"value": level, "levels": ", ".join(DATA_LEVELS)}, no))
        locations.append((location, no))
    return DataIndex(frozenset(seen), tuple(problems), tuple(locations))


def notebook_problems(data: object) -> list[str]:
    """Problemas de REQ-CONV-59 en un notebook ya leído como JSON, en orden y sin repetir."""
    if not isinstance(data, dict) or not isinstance(data.get("cells"), list):
        return ["invalid"]
    out: list[str] = []

    def add(key: str) -> None:
        if key not in out:
            out.append(key)

    meta = data.get("metadata")
    if isinstance(meta, dict) and "widgets" in meta:
        add("widgets")
    for cell in data["cells"]:
        if not isinstance(cell, dict):
            add("invalid")
            continue
        if cell.get("cell_type") == "code":
            if cell.get("outputs") != []:
                add("outputs")
            if cell.get("execution_count") is not None:
                add("execution_count")
        if cell.get("attachments"):
            add("attachments")
    return out


@dataclass(frozen=True)
class RequiredPaths:
    """Rutas obligatorias de la convención (``required-paths.json``).

    ``formats`` es el filtro por formato de D-36 (enmendada por D-43): ruta -> formatos de
    ``project.yaml`` con los que se exige. Una ruta que no está en ``formats`` se exige
    siempre.
    """

    milestone: str
    fixed: tuple[str, ...]
    per_section: tuple[str, ...]
    per_section_dirs: tuple[str, ...]
    formats: Mapping[str, frozenset[str]] = field(default_factory=lambda: MappingProxyType({}))

    def required_with(self, path: str, fmt: str) -> bool:
        """``True`` si ``path`` (tal como figura en la lista) se exige con ``format: fmt``."""
        allowed = self.formats.get(path)
        return allowed is None or fmt in allowed

    def for_format(self, fmt: str) -> RequiredPaths:
        """Las listas que se exigen con ``format: fmt`` (D-36 enmendada)."""

        def keep(paths: tuple[str, ...]) -> tuple[str, ...]:
            return tuple(p for p in paths if self.required_with(p, fmt))

        return RequiredPaths(
            self.milestone, keep(self.fixed), keep(self.per_section), keep(self.per_section_dirs), self.formats
        )


def parse_required_paths(text: str) -> RequiredPaths:
    """Lee ``required-paths.json``; ``ValueError`` si no tiene la forma de ``RequiredPaths``."""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("required-paths.json no es un objeto")

    def strings(key: str) -> tuple[str, ...]:
        value = data.get(key)
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise ValueError(f"required-paths.json: {key} no es una lista de rutas")
        return tuple(value)

    milestone = data.get("milestone")
    if not isinstance(milestone, str):
        raise ValueError("required-paths.json: falta milestone")
    fixed, per_section, per_section_dirs = strings("fixed"), strings("per_section"), strings("per_section_dirs")
    listed = set(fixed) | set(per_section) | set(per_section_dirs)
    raw_formats = data.get("formats", {})
    if not isinstance(raw_formats, dict):
        raise ValueError("required-paths.json: formats no es un objeto")
    formats: dict[str, frozenset[str]] = {}
    for path, values in raw_formats.items():
        if path not in listed:
            raise ValueError(f"required-paths.json: formats cita {path!r}, que no está en las listas")
        if not isinstance(values, list) or not values or not all(v in FORMATS for v in values):
            raise ValueError(f"required-paths.json: formats de {path!r} no es una lista de {FORMATS}")
        formats[path] = frozenset(values)
    return RequiredPaths(milestone, fixed, per_section, per_section_dirs, MappingProxyType(formats))


def required_paths() -> RequiredPaths:
    """Lista de obligatorios empaquetada (D-36; ``FileNotFoundError`` si falta)."""
    return parse_required_paths(datafiles.required_paths_file().read_text(encoding="utf-8"))


@functools.cache
def reference_agents(lang: Lang) -> str | None:
    """``AGENTS.md`` de la plantilla empaquetada (``template/l10n/<lang>``), o ``None``."""
    try:
        path = datafiles.data_path("template") / "l10n" / lang / "AGENTS.md"
        return normalize_text(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


def _size_text(size: int, lang: str) -> str:
    text = f"{size / MIB:.1f} MiB" if size >= MIB else f"{size / KIB:.1f} KiB"
    return text.replace(".", ",") if lang == "es" else text


# -- formato del proyecto (D-43) ---------------------------------------------------------

# Árbol -> (base_tree, política, formato): el motor y las reglas lo piden varias veces por árbol.
_FORMAT_CACHE: weakref.WeakKeyDictionary[object, tuple[object, object, str]] = weakref.WeakKeyDictionary()


def _declared_format(data: bytes | None, inp: RuleInput) -> str | None:
    """``format`` de un ``project.yaml`` válido (03-esquemas §3); ``None`` si no lo es."""
    if data is None:
        return None
    from ...config.project import parse_project  # perezoso: carga el validador de esquemas

    cfg, _findings = parse_project(data, t=inp.t, check=RULE_ID)
    fmt = getattr(cfg, "format", None)
    return fmt if fmt in FORMATS else None


def _resolve_format(inp: RuleInput) -> str:
    policy = inp.policy
    base_fmt = getattr(getattr(policy, "project", None), "format", None) if policy is not None else None
    if base_fmt not in FORMATS:
        base_fmt = None
    tree = inp.tree
    if tree is not None:
        head = tree.read_bytes("project.yaml", MAX_FILE)
        modified = True
        if inp.base_tree is not None:
            modified = inp.base_tree.read_bytes("project.yaml", MAX_FILE) != head
        if modified or base_fmt is None:
            head_fmt = _declared_format(head, inp)
            if head_fmt is not None:
                return head_fmt
    return base_fmt or DEFAULT_FORMAT


def tree_format(inp: RuleInput) -> str:
    """``format`` con el que se juzga el árbol: ``latex`` o ``none`` (D-43). Nunca lanza.

    Como ``roles.yaml`` (03-esquemas §2.5): el del ``project.yaml`` del árbol si el árbol lo
    modifica y es válido; si no, el de la base; sin base, el del árbol si es válido; si no,
    ``latex``. Así, la tarea del responsable que cambia de formato se valida con el nuevo. Lo
    usan esta regla, la de LaTeX y el motor, que con ``none`` da ``skip`` en las reglas de
    LaTeX (00-marco D-33).
    """
    tree = inp.tree
    try:
        cached = _FORMAT_CACHE.get(tree) if tree is not None else None
    except TypeError:  # un árbol que no admite referencias débiles: sin caché
        cached = None
    if cached is not None and cached[0] is inp.base_tree and cached[1] is inp.policy:
        return cached[2]
    try:
        fmt = _resolve_format(inp)
    except Exception:  # un project.yaml ilegible no puede saltarse reglas: se juzga como LaTeX
        return DEFAULT_FORMAT
    if tree is not None:
        try:
            _FORMAT_CACHE[tree] = (inp.base_tree, inp.policy, fmt)
        except TypeError:
            pass
    return fmt


# -- estado de una ejecución --------------------------------------------------------------


class _Run:
    """Árbol, hallazgos y lecturas compartidas por las comprobaciones de una ejecución."""

    def __init__(self, inp: RuleInput, tree: TreeView) -> None:
        self.inp = inp
        self.t = inp.t
        self.tree = tree
        self.entries: dict[str, str] = dict(tree.entries())
        self.out: list[Finding] = []
        self._sizes: dict[str, int] = {}
        self._texts: dict[str, str | None] = {}
        self._project: tuple[object | None, Mapping[str, int]] | None = None
        self._roles_done = False
        self._roles: Roles | None = None
        self._declared: tuple[str, ...] | None = None
        self._sections_unavailable = False
        self._data_index: DataIndex | None = None
        self._folders: dict[str, list[str]] | None = None
        self._format: str | None = None

    # -- hallazgos ----------------------------------------------------------------

    def add(self, code: str, path: str | None, line: int | None = None, **params: str) -> None:
        self.out.append(
            make_finding(RULE, self.inp, code, severity=SEVERITY[code], path=path, line=line, **params)
        )

    def sections_unavailable(self) -> None:
        """Una sola vez: sin ``roles.yaml`` válido no se pueden evaluar las reglas por parte."""
        if not self._sections_unavailable:
            self._sections_unavailable = True
            self.out.append(unavailable(RULE, self.inp, self.t("convention.reason.no_roles"), path="roles.yaml"))

    # -- lecturas -----------------------------------------------------------------

    def is_regular(self, path: str) -> bool:
        mode = self.entries.get(path)
        return mode is not None and mode.startswith("100")

    def probe(self, path: str) -> tuple[int, bytes | None]:
        """``(tamaño, contenido)``; tamaño ``MAX_FILE + 1`` y ``None`` si supera 5 MiB o no se lee."""
        data = self.tree.read_bytes(path, LARGE_FILE)
        if data is None:
            data = self.tree.read_bytes(path, MAX_FILE)
        size = len(data) if data is not None else MAX_FILE + 1
        self._sizes[path] = size
        return size, data

    def size(self, path: str) -> int:
        cached = self._sizes.get(path)
        return cached if cached is not None else self.probe(path)[0]

    def data(self, path: str) -> bytes | None:
        if not self.is_regular(path):
            return None
        return self.tree.read_bytes(path, MAX_FILE)

    def text(self, path: str) -> str | None:
        """Texto normalizado (sin BOM, con LF; bytes inválidos reemplazados), o ``None``."""
        if path not in self._texts:
            data = self.data(path)
            self._texts[path] = normalize_text(data.decode("utf-8", errors="replace")) if data is not None else None
        return self._texts[path]

    def lang_str(self) -> str:
        return self.t.lang

    # -- configuración del árbol y de la base --------------------------------------

    def project(self) -> tuple[object | None, Mapping[str, int]]:
        """``project.yaml`` del árbol como datos YAML crudos y su mapa pointer -> línea."""
        if self._project is None:
            data = self.data("project.yaml")
            if data is None:
                self._project = (None, {})
            else:
                raw, lines, findings, _norm = load_with_lines(data, file="project.yaml", t=self.t, check=RULE_ID)
                self._project = (None, {}) if findings else (raw, lines)
        return self._project

    def engine(self) -> str:
        """``build.engine`` del ``project.yaml`` del árbol; si no se puede leer, el de la base."""
        raw, _lines = self.project()
        if isinstance(raw, dict):
            build = raw.get("build")
            if build is None or (isinstance(build, dict) and "engine" not in build):
                return DEFAULT_ENGINE  # valor por defecto de 03 §3.2
            value = build.get("engine") if isinstance(build, dict) else None
            if isinstance(value, str) and value in PDF_MODE:
                return value
        policy = self.inp.policy
        if policy is not None and policy.project.build.engine in PDF_MODE:
            return policy.project.build.engine
        return DEFAULT_ENGINE

    def format(self) -> str:
        """``format`` con el que se juzga el árbol (``tree_format``, D-43)."""
        if self._format is None:
            self._format = tree_format(self.inp)
        return self._format

    def language(self) -> Lang:
        """``language`` del ``project.yaml`` del árbol; si no, el de la base; si no, ``en``."""
        raw, _lines = self.project()
        if isinstance(raw, dict):
            value = raw.get("language")
            for lang in LANGS:
                if value == lang:
                    return lang
        policy = self.inp.policy
        if policy is not None and policy.project.language in LANGS:
            return policy.project.language
        return "en"

    def roles(self) -> Roles | None:
        """``roles.yaml`` de head si el árbol lo modifica y es válido; si no, el de la base (03 §2.5)."""
        if not self._roles_done:
            self._roles_done = True
            from ...config.roles import parse_roles

            head_bytes = self.data("roles.yaml")
            head: Roles | None = None
            if head_bytes is not None:
                head, _findings = parse_roles(head_bytes, file="roles.yaml", t=self.t, check=RULE_ID)
            base_tree = self.inp.base_tree
            modified = True
            if base_tree is not None:
                modified = base_tree.read_bytes("roles.yaml", MAX_FILE) != head_bytes
            policy = self.inp.policy
            if head is not None and modified:
                self._roles = head
            elif policy is not None:
                self._roles = policy.roles
            else:
                self._roles = head
            if self._roles is not None:
                self._declared = tuple(s.id for s in self._roles.sections)
        return self._roles

    def declared(self) -> tuple[str, ...] | None:
        self.roles()
        return self._declared

    def folders(self) -> dict[str, list[str]]:
        """Carpetas de ``sections/``: nombre -> rutas relativas a la carpeta (todas las entradas)."""
        if self._folders is None:
            out: dict[str, list[str]] = {}
            for path in self.entries:
                parts = path.split("/")
                if len(parts) >= 3 and parts[0] == SECTIONS_DIR:
                    out.setdefault(parts[1], []).append("/".join(parts[2:]))
            self._folders = out
        return self._folders

    def has_dir(self, directory: str) -> bool:
        prefix = directory.rstrip("/") + "/"
        return any(p.startswith(prefix) for p in self.entries)

    def data_index(self) -> DataIndex | None:
        if self._data_index is None:
            text = self.text("data/README.md")
            if text is None:
                return None
            self._data_index = parse_data_index(text)
        return self._data_index


# -- comprobaciones ----------------------------------------------------------------------


def _check_tree(run: _Run) -> None:
    """``conv.tree.missing`` (REQ-CONV-1, REQ-CONV-24; D-36, filtrada por formato desde D-43)."""
    req = required_paths().for_format(run.format())
    for path in req.fixed:
        if path not in run.entries:
            run.add("conv.tree.missing", path, file=path)
    declared = run.declared()
    if declared is None:
        run.sections_unavailable()
        return
    folders = run.folders()
    for sid in declared:
        if sid not in folders:
            continue  # la carpeta entera falta: lo informa conv.section.missing
        for template in req.per_section:
            path = template.replace("{id}", sid)
            if path not in run.entries:
                run.add("conv.tree.missing", path, file=path)
        for template in req.per_section_dirs:
            directory = template.replace("{id}", sid).rstrip("/")
            if not run.has_dir(directory):
                run.add("conv.tree.missing", directory, file=directory + "/")


def _case_collisions(run: _Run) -> None:
    """``conv.path.case-collision`` (REQ-CONV-16), también entre carpetas; solo la más alta."""
    spellings: dict[str, set[str]] = {}
    for path in run.entries:
        parts = path.split("/")
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            spellings.setdefault(prefix.lower(), set()).add(prefix)
    colliding: set[str] = set()
    for low in sorted(spellings, key=lambda k: (k.count("/"), k)):
        names = spellings[low]
        if len(names) < 2:
            continue
        parts = low.split("/")
        if any("/".join(parts[:i]) in colliding for i in range(1, len(parts))):
            continue
        colliding.add(low)
        first, *rest = sorted(names)
        for other in rest:
            run.add("conv.path.case-collision", other, other=first)


def _check_paths(run: _Run) -> None:
    """``conv.path.*``: nombres, enlaces y submódulos, rutas locales e instrucciones."""
    declared = frozenset(run.declared() or ())
    foreign_seen: set[str] = set()
    for path, mode in run.entries.items():
        parts = path.split("/")
        if parts[0] in NAMED_TREES:
            bad = next((seg for seg in parts[1:] if not SEGMENT.fullmatch(seg)), None)
            if bad is not None:
                run.add("conv.path.invalid-name", path, segment=bad)
        if mode in (SYMLINK_MODE, GITLINK_MODE):
            run.add("conv.path.symlink", path, mode=mode)
        info = dpaths.classify(path, declared, False)
        if info.cls == "local":
            run.add("conv.path.local-committed", path)
        elif info.rule == "R2":
            run.add("conv.path.nested-instructions", path)
        top = parts[0].lower()
        foreign = (len(parts) == 1 and top in FOREIGN_ROOT_FILES) or (len(parts) > 1 and top in FOREIGN_ROOT_DIRS)
        if foreign and parts[0] not in foreign_seen:
            foreign_seen.add(parts[0])
            run.add("conv.path.foreign-instructions", parts[0])
    _case_collisions(run)


def _check_sections(run: _Run) -> None:
    """``conv.section.*`` (REQ-CONV-12, 24, 25, 62 y 89), con la columna de 02 §4.2 del formato."""
    declared = run.declared()
    if declared is None:
        run.sections_unavailable()
        return
    fmt = run.format()
    folders = run.folders()
    for name in sorted(folders):
        if name not in declared:
            run.add("conv.section.undeclared", f"{SECTIONS_DIR}/{name}", section=name)
    for sid in declared:
        if sid not in folders:
            files = run.t(f"convention.section.files.{fmt}")
            run.add("conv.section.missing", f"{SECTIONS_DIR}/{sid}", section=sid, files=files)
            continue
        prefix = f"{SECTIONS_DIR}/{sid}/"
        total = 0
        for rel in sorted(folders[sid]):
            path = prefix + rel
            if not run.is_regular(path):
                continue  # enlaces y submódulos: conv.path.symlink
            total += run.size(path)
            if fmt == FORMAT_NONE and rel == PENDING_BIB:
                continue  # resto del manuscrito: lo avisa conv.format.unused (REQ-CONV-88)
            parts = rel.split("/")
            allowed = section_patterns(parts, fmt)
            if not any(matches(parts[-1], pattern) for pattern in allowed):
                run.add("conv.section.disallowed-file", path, allowed=", ".join(allowed))
        if total > LARGE_SECTION:
            run.add("conv.section.large", f"{SECTIONS_DIR}/{sid}", section=sid, size=_size_text(total, run.lang_str()))


def _check_files(run: _Run) -> None:
    """``conv.file.large``, ``conv.file.too-large`` y ``conv.file.encoding`` (REQ-CONV-38, 61)."""
    for path in run.entries:
        if not run.is_regular(path):
            continue
        size, data = run.probe(path)
        if size > MAX_FILE:
            run.add("conv.file.too-large", path)
        elif size > LARGE_FILE:
            run.add("conv.file.large", path, size=_size_text(size, run.lang_str()))
        if data is not None and is_text_path(path):
            problem = encoding_problem(data)
            if problem is not None:
                key, line = problem
                run.add("conv.file.encoding", path, line, problem=run.t(f"convention.encoding.{key}"))


def _is_memory_allowed(path: str) -> bool:
    rest = path[len("memory/") :]
    if rest in ("constitution.md", ".gitkeep", "decisions/.gitkeep", "notes/.gitkeep"):
        return True
    parts = rest.split("/")
    if len(parts) == 2 and parts[0] == "decisions" and parts[1].endswith(".md") and not parts[1].startswith("."):
        return True  # el nombre exacto lo juzga E_VALIDATION_DECISION_NAME (03 REQ-SCHEMA-45)
    return dpaths.TASK_NOTE.fullmatch(path) is not None


def _check_memory(run: _Run) -> None:
    """``conv.memory.unexpected-file`` (REQ-CONV-66, 70) y ``conv.memory.note-large`` (REQ-CONV-69)."""
    for path in run.entries:
        if not path.startswith("memory/"):
            continue
        if not _is_memory_allowed(path):
            run.add("conv.memory.unexpected-file", path)
        elif dpaths.TASK_NOTE.fullmatch(path) and run.is_regular(path):
            size = run.size(path)
            if size >= NOTE_WARN:
                run.add("conv.memory.note-large", path, size=_size_text(size, run.lang_str()))


def _attr_key(line: str) -> tuple[str, frozenset[str]] | None:
    tokens = line.split()
    if not tokens or tokens[0].startswith("#"):
        return None
    return tokens[0], frozenset(tokens[1:])


def _check_gitfiles(run: _Run) -> None:
    """``conv.gitignore.required``, ``conv.gitattributes.required`` y ``conv.gitattributes.lfs``."""
    if ".gitignore" in run.entries:
        present = {line.strip() for line in (run.text(".gitignore") or "").split("\n")}
        for entry in GITIGNORE_REQUIRED:
            if entry not in present:
                run.add("conv.gitignore.required", ".gitignore", entry=entry)
    for path in sorted(run.entries):
        if path.rsplit("/", 1)[-1] != ".gitattributes":
            continue
        text = run.text(path) or ""
        for no, line in enumerate(text.split("\n"), 1):
            key = _attr_key(line)
            if key is not None and "filter=lfs" in key[1]:
                run.add("conv.gitattributes.lfs", path, no)
        if path == ".gitattributes":
            have = {k for k in map(_attr_key, text.split("\n")) if k is not None}
            for entry in GITATTRIBUTES_REQUIRED:
                if _attr_key(entry) not in have:
                    run.add("conv.gitattributes.required", path, entry=" ".join(entry.split()))


def _check_data(run: _Run) -> None:
    """``conv.data.*`` (REQ-CONV-52 a 55)."""
    for path in run.entries:
        if path.startswith("data/") and path != "data/README.md":
            run.add("conv.data.unexpected-file", path)
    index = run.data_index()
    if index is None:
        return
    for key, params, line in index.problems:
        run.add("conv.data.index", "data/README.md", line, problem=run.t(f"convention.data.{key}", **params))
    for location, line in index.locations:
        for param in credential_params(location):
            run.add("conv.data.credential-url", "data/README.md", line, param=param)


class _Problems:
    """Acumula problemas de un YAML con su línea (pointer -> línea)."""

    def __init__(self, run: _Run, path: str, code: str, lines: Mapping[str, int]) -> None:
        self.run, self.path, self.code, self.lines = run, path, code, lines

    def __call__(self, pointer: str, what: str, /, **params: object) -> None:
        text = self.run.t(f"convention.provenance.{what}", **params)
        self.run.add(self.code, self.path, line_for(self.lines, pointer), problem=text)


def _provenance_entries(raw: object, report: _Problems, files: frozenset[str], datasets: frozenset[str]) -> set[str]:
    """Valida ``provenance.yaml`` (REQ-CONV-58); devuelve las salidas declaradas con ruta válida."""
    declared: set[str] = set()
    if not isinstance(raw, dict):
        report("", "not_mapping")
        return declared
    for key in raw:
        if key not in PROV_TOP_KEYS:
            report(f"/{key}", "unknown_key", key=shown(key))
    version = raw.get("version")
    if isinstance(version, bool) or version != 1:
        report("/version", "version")
    outputs = raw.get("outputs")
    if not isinstance(outputs, list):
        report("/outputs", "outputs")
        return declared
    for i, entry in enumerate(outputs):
        ptr = f"/outputs/{i}"
        if not isinstance(entry, dict):
            report(ptr, "entry")
            continue
        for key in entry:
            if key not in PROV_ENTRY_KEYS:
                report(f"{ptr}/{key}", "unknown_key", key=shown(key))
        rel = section_rel(entry.get("path"))
        if rel is None:
            report(f"{ptr}/path", "entry_path")
        elif rel in declared:
            report(f"{ptr}/path", "duplicate", file=rel)
        else:
            declared.add(rel)
            if rel not in files:
                report(f"{ptr}/path", "path_missing", file=rel)
        if entry.get("kind") not in PROV_KINDS:
            report(f"{ptr}/kind", "kind", kinds=", ".join(PROV_KINDS))
        generator = entry.get("generator")
        if generator not in ("manual", "external"):
            grel = section_rel(generator)
            if grel is None or not grel.startswith("analysis/") or grel.startswith("analysis/derived/"):
                report(f"{ptr}/generator", "generator")
            elif grel not in files:
                report(f"{ptr}/generator", "generator_missing", file=grel)
        credit = entry.get("credit")
        credit_ok = isinstance(credit, str) and bool(credit.strip())
        if (generator == "external" and not credit_ok) or ("credit" in entry and not credit_ok):
            report(f"{ptr}/credit" if "credit" in entry else ptr, "credit")
        if "inputs" not in entry:
            continue
        inputs = entry["inputs"]
        if not isinstance(inputs, list):
            report(f"{ptr}/inputs", "inputs")
            continue
        for j, item in enumerate(inputs):
            iptr = f"{ptr}/inputs/{j}"
            if not isinstance(item, dict) or len(item) != 1 or next(iter(item)) not in ("dataset", "path"):
                report(iptr, "input")
            elif "dataset" in item:
                dataset = item["dataset"]
                if not isinstance(dataset, str) or dataset not in datasets:
                    report(f"{iptr}/dataset", "dataset", dataset=dataset if isinstance(dataset, str) else shown(dataset))
            else:
                irel = section_rel(item["path"])
                if irel is None:
                    report(f"{iptr}/path", "input")
                elif irel not in files:
                    report(f"{iptr}/path", "input_missing", file=irel)
    return declared


def _check_provenance(run: _Run) -> None:
    """``conv.provenance.missing`` (REQ-CONV-57) y ``conv.provenance.invalid`` (REQ-CONV-58)."""
    declared = run.declared()
    if declared is None:
        run.sections_unavailable()
        return
    folders = run.folders()
    index = run.data_index()
    datasets = index.ids if index is not None else frozenset()
    for sid in declared:
        if sid not in folders:
            continue
        prefix = f"{SECTIONS_DIR}/{sid}/"
        files = frozenset(rel for rel in folders[sid] if run.is_regular(prefix + rel))
        outputs = sorted(rel for rel in files if needs_provenance(rel))
        prov_path = prefix + "provenance.yaml"
        recorded: set[str] = set()
        if prov_path in run.entries:
            data = run.data(prov_path)
            if data is None:
                run.add("conv.provenance.invalid", prov_path, problem=run.t("convention.provenance.unreadable"))
                continue
            raw, lines, findings, _norm = load_with_lines(data, file=prov_path, t=run.t, check=RULE_ID)
            if findings:
                run.add("conv.provenance.invalid", prov_path, findings[0].line, problem=run.t("convention.provenance.yaml"))
                continue
            report = _Problems(run, prov_path, "conv.provenance.invalid", lines)
            recorded = _provenance_entries(raw, report, files, datasets)
        for rel in outputs:
            if rel not in recorded:
                run.add("conv.provenance.missing", prefix + rel)


def _check_notebooks(run: _Run) -> None:
    """``conv.notebook.outputs`` (REQ-CONV-59)."""
    declared = run.declared()
    for path in sorted(run.entries):
        if not path.lower().endswith(".ipynb") or not run.is_regular(path):
            continue
        parts = path.split("/")
        inside = (
            len(parts) >= 4
            and parts[0] == SECTIONS_DIR
            and (declared is None or parts[1] in declared)
            and parts[2] == "analysis"
        )
        if not inside:
            run.add("conv.notebook.outputs", path, problem=run.t("convention.notebook.outside"))
        text = run.text(path)
        try:
            data: object = json.loads(text) if text is not None else None
        except ValueError:
            data = None
        for key in notebook_problems(data):
            run.add("conv.notebook.outputs", path, problem=run.t(f"convention.notebook.{key}"))


def _check_project(run: _Run) -> None:
    """``conv.convention.value`` (REQ-CONV-85, 02 §4) y ``E_VALIDATION_LICENSE_UNKNOWN`` (REQ-TRACE-64)."""
    raw, lines = run.project()
    if not isinstance(raw, dict):
        return  # sin project.yaml o con YAML roto: lo informan conv.tree.missing y schemas

    def problem(pointer: str, what: str, /, **params: object) -> None:
        run.add(
            "conv.convention.value",
            "project.yaml",
            line_for(lines, pointer),
            problem=run.t(f"convention.convention.{what}", **params),
        )

    supported = ", ".join(f'"{v}"' for v in _meta.SUPPORTED_CONVENTIONS)
    if "convention" not in raw:
        problem("", "missing", expected=supported)
    elif not isinstance(raw["convention"], str):
        problem("/convention", "not_string", value=shown(raw["convention"]), expected=supported)
    elif raw["convention"] not in _meta.SUPPORTED_CONVENTIONS:
        problem("/convention", "value", value=shown(raw["convention"]), expected=supported)
    build = raw.get("build")
    if isinstance(build, dict):
        for key, expected in BUILD_FIXED.items():
            if key in build and build[key] != expected:
                problem(f"/build/{key}", "build_value", key=f"build.{key}", expected=shown(expected))
        if "engine" in build and build["engine"] not in PDF_MODE:
            problem("/build/engine", "build_engine", engines=", ".join(PDF_MODE))
    licenses = raw.get("licenses")
    if not isinstance(licenses, dict):
        return
    present: frozenset[str] | None = None
    for category in LICENSE_CATEGORIES:
        if category not in licenses:
            continue  # la clave obligatoria la exige el esquema (regla schemas)
        value = licenses[category]
        line = line_for(lines, f"/licenses/{category}")
        shown_value = value if isinstance(value, str) else shown(value)
        if not isinstance(value, str) or not LICENSE_ID.fullmatch(value):
            reason = run.t("convention.license.invalid")
        elif f"LICENSES/{value}.txt" not in run.entries:
            if present is None:  # D-64: REUSE solo pide el texto si hay archivos de esa categoría
                present = dlicenses.categories_with_files(run.entries, lambda rel: run.is_regular(rel) and run.size(rel) > 0)
            if category not in present:
                continue
            reason = run.t("convention.license.no_text", file=f"LICENSES/{value}.txt")
        else:
            continue
        run.add("E_VALIDATION_LICENSE_UNKNOWN", "project.yaml", line, category=category, value=shown_value, problem=reason)


def _structure(
    run: _Run, path: str, code: str, text: str, *, max_bytes: int, max_lines: int, prefix: str
) -> dict[int, tuple[int, int]]:
    """Tamaño, líneas y secciones ``## 1.`` a ``## 7.`` en orden; devuelve las secciones halladas."""
    size = len(text.encode("utf-8"))
    if size > max_bytes:
        run.add(code, path, problem=run.t(f"convention.{prefix}.size", size=str(size), max=str(max_bytes)))
    count = line_count(text)
    if count > max_lines:
        run.add(code, path, problem=run.t(f"convention.{prefix}.lines", count=str(count), max=str(max_lines)))
    sections, bad = numbered_sections(text)
    for n in [n for n in sections if n > 7]:
        bad.append(Heading(2, "", sections.pop(n)[0]))
    for h in sorted(bad, key=lambda h: h.line):
        run.add(code, path, h.line, problem=run.t(f"convention.{prefix}.heading"))
    missing = [str(n) for n in range(1, 8) if n not in sections]
    if missing:
        run.add(code, path, problem=run.t(f"convention.{prefix}.sections", missing=", ".join(missing)))
    return sections


def _check_agents(run: _Run) -> None:
    """``conv.agents.structure``, ``conv.agents.import`` y ``conv.agents.modified`` (REQ-CONV-73, 75 a 77)."""
    path = "AGENTS.md"
    text = run.text(path)
    if text is None:
        return
    heads = headings(text)
    if not heads or heads[0].level != 1 or sum(1 for h in heads if h.level == 1) != 1:
        run.add("conv.agents.structure", path, heads[0].line if heads else None, problem=run.t("convention.agents.title"))
    sections = _structure(
        run, path, "conv.agents.structure", text, max_bytes=AGENTS_MAX_BYTES, max_lines=AGENTS_MAX_LINES, prefix="agents"
    )
    if 7 in sections:
        size = len(block(text, sections[7]).encode("utf-8"))
        if size > AGENTS_SECTION7_MAX:
            run.add(
                "conv.agents.structure",
                path,
                sections[7][0],
                problem=run.t("convention.agents.section7", size=str(size), max=str(AGENTS_SECTION7_MAX)),
            )
    for line, target in imports(text):
        if target != AGENTS_IMPORT:
            run.add("conv.agents.import", path, line, target=target)
    reference = reference_agents(run.language())
    if reference is None:
        return  # sin plantilla empaquetada no hay render con que comparar
    ref_sections, _bad = numbered_sections(reference)
    for n in range(1, 7):
        if n not in sections or n not in ref_sections:
            continue
        expected = block(reference, ref_sections[n])
        if "{{" in expected:
            continue  # marcador de render sin sustituir: no se puede comparar
        if block(text, sections[n]) != expected:
            run.add("conv.agents.modified", path, sections[n][0], number=str(n))


def _check_claude(run: _Run) -> None:
    """``conv.claude.content`` (REQ-CONV-74)."""
    path = "CLAUDE.md"
    text = run.text(path)
    if text is None:
        return
    first = text.split("\n", 1)[0]
    if first != CLAUDE_FIRST_LINE:
        run.add("conv.claude.content", path, 1, problem=run.t("convention.claude.first_line"))
    for line, target in imports(text):
        if line == 1 and first == CLAUDE_FIRST_LINE:
            continue
        run.add("conv.claude.content", path, line, problem=run.t("convention.claude.import", target=target))
    size = len(text.encode("utf-8"))
    if size > CLAUDE_MAX_BYTES:
        run.add("conv.claude.content", path, problem=run.t("convention.claude.size", size=str(size), max=str(CLAUDE_MAX_BYTES)))


def _word(value: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w-])@?{re.escape(value)}(?![\w-])", re.IGNORECASE)


def _check_constitution(run: _Run) -> None:
    """``conv.constitution.structure`` (REQ-CONV-64 y 65)."""
    path = "memory/constitution.md"
    text = run.text(path)
    if text is None:
        return
    code = "conv.constitution.structure"
    _structure(run, path, code, text, max_bytes=CONSTITUTION_MAX_BYTES, max_lines=CONSTITUTION_MAX_LINES, prefix="constitution")
    for line, target in imports(text):
        run.add(code, path, line, problem=run.t("convention.constitution.import", target=target))
    items, _errors = dplan.parse_plan(text)
    for item in items:
        run.add(code, path, item.line, problem=run.t("convention.constitution.plan_id", id=item.id))
    roles = run.roles()
    ids = [(sid, _word(sid)) for sid in (run.declared() or ())]
    logins = [(p.login, _word(p.login)) for p in roles.people] if roles is not None else []
    for line, content in outside_code(text):
        m = _MACRO_DEF.search(content)
        if m:
            run.add(code, path, line, problem=run.t("convention.constitution.macro", command="\\" + m.group(1)))
        section = next((sid for sid, rx in ids if rx.search(content)), None)
        login = next((name for name, rx in logins if rx.search(content)), None)
        if section is not None and login is not None:
            run.add(code, path, line, problem=run.t("convention.constitution.assignment", section=section, login=login))


def _check_format(run: _Run) -> None:
    """``conv.format.unused`` (REQ-CONV-88): un aviso cuyo ``hint`` lista lo que sobra con ``format: none``.

    Sobran ``tex/``, ``bib/``, ``.latexmkrc`` y el ``pending.bib`` de cada parte. No estorban:
    cambiar de formato es una tarea del responsable. Apunta a la línea ``format`` de
    ``project.yaml``, que es donde se declara.
    """
    if run.format() != FORMAT_NONE:
        return
    unused = [
        item
        for item in UNUSED_WITHOUT_MANUSCRIPT
        if (run.has_dir(item) if item.endswith("/") else item in run.entries)
    ]
    for sid, rels in sorted(run.folders().items()):
        if PENDING_BIB in rels:
            unused.append(f"{SECTIONS_DIR}/{sid}/{PENDING_BIB}")
    if not unused:
        return
    shown = ", ".join(f"`{item}`" for item in unused[:MAX_UNUSED_SHOWN])
    if len(unused) > MAX_UNUSED_SHOWN:
        shown += ", …"
    raw, lines = run.project()
    line = line_for(lines, "/format") if isinstance(raw, dict) and "format" in raw else None
    run.add("conv.format.unused", "project.yaml", line, unused=shown)


def _check_latexmkrc(run: _Run) -> None:
    """``conv.latexmkrc.noncanonical`` (REQ-CONV-36); con ``format: none`` queda en ``skip`` (D-43)."""
    path = ".latexmkrc"
    if path not in run.entries or run.format() == FORMAT_NONE:
        return
    data = run.data(path)
    engine = run.engine()
    expected = canonical_latexmkrc(engine)
    if data == expected:
        return
    line: int | None = None
    if data is not None:
        got = data.split(b"\n")
        want = expected.split(b"\n")
        line = next((i + 1 for i, (a, b) in enumerate(zip(got, want, strict=False)) if a != b), min(len(got), len(want)))
    run.add("conv.latexmkrc.noncanonical", path, line, engine=engine)


CHECKS: Final[tuple[tuple[str, Callable[[_Run], None]], ...]] = (
    ("tree", _check_tree),
    ("format", _check_format),
    ("path", _check_paths),
    ("section", _check_sections),
    ("file", _check_files),
    ("memory", _check_memory),
    ("gitfiles", _check_gitfiles),
    ("data", _check_data),
    ("provenance", _check_provenance),
    ("notebook", _check_notebooks),
    ("project", _check_project),
    ("agents", _check_agents),
    ("claude", _check_claude),
    ("constitution", _check_constitution),
    ("latexmkrc", _check_latexmkrc),
)


def _run(inp: RuleInput) -> Sequence[Finding]:
    if inp.tree is None:
        return [unavailable(RULE, inp, inp.t("convention.reason.no_tree"))]
    run = _Run(inp, inp.tree)
    for name, check in CHECKS:
        try:
            check(run)
        except Exception:  # la regla falla cerrada en esta comprobación y sigue con las demás
            run.out.append(unavailable(RULE, inp, inp.t("convention.reason.check_failed", check=name)))
    return run.out


RULE: Final[RuleSpec] = RuleSpec(
    id=RULE_ID,
    modes=frozenset({"tree", "pr", "base"}),
    run=_run,
    codes=CODES,
)

RULES: tuple[RuleSpec, ...] = (RULE,)

__all__ = [
    "CHECKS",
    "CODES",
    "GITATTRIBUTES_REQUIRED",
    "GITIGNORE_REQUIRED",
    "RULE",
    "RULES",
    "SEVERITY",
    "FORMATS",
    "FORMAT_NONE",
    "canonical_latexmkrc",
    "parse_data_index",
    "parse_required_paths",
    "required_paths",
    "section_patterns",
    "tree_format",
]
