# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Reglas LaTeX y de bibliografía de ``validate`` (09-ci §4.1; 02-convencion §4, §5 y §12).

- ``citations`` (``E_VALIDATION_CITATION``): solo ``\\addbibresource{../bib/references.bib}``
  y ``\\addbibresource{../sections/<id>/pending.bib}`` en ``tex/main.tex``; cada clave de
  ``\\cite`` y variantes de una parte está en ``bib/references.bib`` o en el
  ``pending.bib`` de esa parte (REQ-CONV-45).
- ``labels-refs`` (``E_VALIDATION_LABEL``): etiquetas con la gramática de §4.4, solo en
  las partes (con el id de su carpeta) y en los archivos de enlace (con el de su
  sección del artículo), y sin repetir; un ``\\ref`` sin destino es un aviso (la
  autoridad es ``-Werror`` en build).
- ``latex-macros`` (``E_VALIDATION_MACRO``): los comandos de la fila «Definiciones y
  catcodes» de §4.6, y los demás definidores comunes de ``DEFINITION_COMMANDS``
  (REQ-CONV-20: «toda definición»), solo en ``tex/notation.tex``, salvo
  ``\\makeatletter`` y ``\\ExplSyntaxOn`` en ``preamble.tex`` y ``main.tex``.
- ``convention`` (D-33 g): los ``conv.latex.*``, ``conv.main.*``, ``conv.outline.*`` y
  ``conv.bib.*`` que no duplican las tres anteriores. Los demás ``conv.*`` son de
  ``ops.rules.convention``.

Las partes viven en ``sections/<id>/``, fuera de ``tex/`` (D-43): la compilación corre en
``tex/`` y las rutas de LaTeX hacia una parte empiezan con ``../sections/<id>/`` (02 §4.5).
``main.tex`` no incluye las partes: incluye los archivos de enlace ``tex/outline/<id>.tex``
del editor, uno por sección del artículo, y cada uno incluye sus partes (02 §4.1.1; D-56).
Con ``format: none`` (sin manuscrito) el motor de ``validate`` da ``skip`` en
``citations``, ``labels-refs`` y ``latex-macros``, y la parte de ``convention`` de este
módulo no informa nada (00-marco D-33; ``convention.tree_format``).

Nada del árbol se ejecuta: los ``.tex`` y ``.bib`` se leen como texto de hasta 5 MB
con ``TreeView`` y se analizan con ``domain.latex`` y ``domain.bibtex``, que tienen
tiempo acotado. Un archivo ilegible o un plazo vencido dan un hallazgo
``unavailable`` (la regla falla cerrada).
"""

from __future__ import annotations

import re
import weakref
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import islice

from ...domain import latex as tex
from ...domain.bibtex import BibFile, parse_bib
from ...domain.findings import Finding, Severity
from ...vcs.tree import MAX_FILE_BYTES, is_regular_mode
from .base import Mode, RuleInput, RuleSpec, make_finding, unavailable

RULE_TIME_BUDGET_S = 30.0  # plazo de cada regla (REQ-CI-24)
MAX_QUOTE = 120  # caracteres de texto del árbol citados en un mensaje
MAX_MAIN_LINE = 1000

MAIN = "tex/main.tex"
PREAMBLE = "tex/preamble.tex"
NOTATION = "tex/notation.tex"
REFERENCES = "bib/references.bib"
SECTIONS_DIR = "sections/"  # carpetas de las partes, en la raíz (R6)
SECTIONS_RESOURCE = "../" + SECTIONS_DIR  # las mismas, vistas desde tex/ (02 §4.5)
LOCAL_DIRS = ("tex/build/", "tex/build-diff/")
REFERENCES_RESOURCE = "../bib/references.bib"
OUTLINE_DIR = "tex/outline/"  # archivos de enlace del editor, uno por sección del artículo (02 §4.1.1; D-56)
OUTLINE_RESOURCE = "outline/"  # los mismos, vistos desde tex/ en main.tex
# REQ-CONV-94: los títulos de sección los pone el archivo de enlace, no la parte.
HEADING_COMMANDS: frozenset[str] = frozenset({"part", "chapter", "section"})

# -- tablas de 02 §4.6 -----------------------------------------------------------

EXEC_COMMANDS: frozenset[str] = frozenset(
    {"ShellEscape", "directlua", "luadirect", "luaexec", "latelua", "inputminted"}
)
EXEC_ENVS: frozenset[str] = frozenset({"luacode", "luacode*", "minted"})
LUA_COMMANDS: frozenset[str] = frozenset({"directlua", "luadirect", "luaexec", "latelua"})
LUA_ENVS: frozenset[str] = frozenset({"luacode", "luacode*"})
IO_COMMANDS: frozenset[str] = frozenset(
    {
        "openin",
        "openout",
        "read",
        "readline",
        "write",
        "immediate",
        "newread",
        "newwrite",
        "pdffiledump",
        "filedump",
        "pdfmdfivesum",
        "pdffilesize",
        "pdffilemoddate",
        "special",
        "pdfobj",
        "pdfximage",
        "pdfrefximage",
        "saveimageresource",
        "useimageresource",
        "XeTeXpdffile",
        "XeTeXpicfile",
        "InputIfFileExists",
        "IfFileExists",
    }
)
DEFINITION_COMMANDS: frozenset[str] = frozenset(
    {
        # Fila «Definiciones y catcodes» de 02 §4.6.
        "def",
        "edef",
        "gdef",
        "xdef",
        "let",
        "futurelet",
        "newcommand",
        "renewcommand",
        "providecommand",
        "DeclareRobustCommand",
        "newenvironment",
        "renewenvironment",
        "DeclareMathOperator",
        "newtheorem",
        "definecolor",
        "newcounter",
        "newlength",
        "catcode",
        "csname",
        "expandafter",
        "scantokens",
        "makeatletter",
        "ExplSyntaxOn",
        # Otras formas de definir comandos, contadores o longitudes (REQ-CONV-20: «toda definición»).
        "newrobustcmd",
        "renewrobustcmd",
        "providerobustcmd",
        "newcommandx",
        "renewcommandx",
        "providecommandx",
        "csdef",
        "csgdef",
        "csedef",
        "csxdef",
        "cslet",
        "csletcs",
        "letcs",
        "newif",
        "chardef",
        "mathchardef",
        "countdef",
        "dimendef",
        "skipdef",
        "toksdef",
        "newcount",
        "newdimen",
        "newskip",
        "newtoks",
        "newbox",
        "NewCommandCopy",
        "RenewCommandCopy",
        "DeclareCommandCopy",
        "NewEnvironmentCopy",
        "RenewEnvironmentCopy",
        "DeclareEnvironmentCopy",
        "DeclarePairedDelimiter",
        "DeclarePairedDelimiterX",
        "newcolumntype",
        # La tabla de §4.6 es mínima: REQ-CONV-20 abarca toda definición de comando,
        # entorno, operador, teorema, color, contador o longitud. Definidores comunes
        # de paquetes (xcolor, amsthm, thmtools, tcolorbox, enumitem, fontspec,
        # environ, newfloat, float, mdframed, xargs) y registros de TeX.
        "colorlet",
        "providecolor",
        "definecolorset",
        "definecolors",
        "definecolorseries",
        "newtheoremstyle",
        "declaretheorem",
        "declaretheoremstyle",
        "newtcbtheorem",
        "renewtcbtheorem",
        "newtcolorbox",
        "renewtcolorbox",
        "newtcbox",
        "renewtcbox",
        "newtcblisting",
        "renewtcblisting",
        "NewTColorBox",
        "RenewTColorBox",
        "ProvideTColorBox",
        "DeclareTColorBox",
        "NewTotalTColorBox",
        "RenewTotalTColorBox",
        "ProvideTotalTColorBox",
        "DeclareTotalTColorBox",
        "NewTCBListing",
        "RenewTCBListing",
        "ProvideTCBListing",
        "DeclareTCBListing",
        "newlist",
        "renewlist",
        "newfontfamily",
        "renewfontfamily",
        "setfontfamily",
        "newfontface",
        "renewfontface",
        "setfontface",
        "NewEnviron",
        "RenewEnviron",
        "newenvironmentx",
        "renewenvironmentx",
        "newfloat",
        "DeclareFloatingEnvironment",
        "newmdenv",
        "renewmdenv",
        "newmdtheoremenv",
        "newsavebox",
        "newmuskip",
        "muskipdef",
    }
)
DEFINITION_SUFFIXES: tuple[str, ...] = ("DocumentCommand", "DocumentEnvironment")
LEAD_DEFINITION_EXCEPTIONS: frozenset[str] = frozenset({"makeatletter", "makeatother", "ExplSyntaxOn", "ExplSyntaxOff"})
STRUCTURE_COMMANDS: frozenset[str] = frozenset(
    {
        "documentclass",
        "usepackage",
        "RequirePackage",
        "include",
        "includeonly",
        "import",
        "subimport",
        "subfile",
        "includestandalone",
        "addbibresource",
        "bibliography",
        "bibliographystyle",
        "printbibliography",
        "graphicspath",
        "hypersetup",
        # Las demás formas de cargar recursos bibliográficos (BIB_RESOURCE_COMMANDS):
        # citations las salta en las secciones porque las informa conv.latex.structure
        # (09-ci §4.1, fila citations: «Solo se aceptan exactamente...»).
        "addglobalbib",
        "addsectionbib",
        "nobibliography",
    }
)
# REQ-CONV-21: notation.tex no carga paquetes ni incluye archivos (la bibliografía la ve citations).
NOTATION_STRUCTURE: frozenset[str] = frozenset(
    {
        "documentclass",
        "usepackage",
        "RequirePackage",
        "input",
        "include",
        "includeonly",
        "import",
        "subimport",
        "subfile",
        "includestandalone",
        "InputIfFileExists",
    }
)
HIDDEN_COMMANDS: frozenset[str] = frozenset({"iffalse", "excludecomment", "includecomment"})
HIDDEN_ENVS: frozenset[str] = frozenset({"comment"})
SHELL_PACKAGES: frozenset[str] = frozenset(
    {"shellesc", "svg", "pythontex", "gnuplottex", "auto-pst-pdf", "minted", "pstool", "bashful"}
)

# REQ-CONV-31: comandos con ruta que una sección puede usar, dentro de su carpeta.
PATH_COMMANDS: frozenset[str] = frozenset(
    {
        "input",
        "includegraphics",
        "includepdf",
        "lstinputlisting",
        "verbatiminput",
        "VerbatimInput",
        "csvautotabular",
        "csvautobooktabular",
        "csvreader",
        "pgfplotstableread",
    }
)
# REQ-CONV-32: otros comandos que leen un archivo por ruta y no se admiten en una sección.
OTHER_READERS: frozenset[str] = frozenset(
    {
        "includesvg",
        "includeinkscape",
        "DTLloaddb",
        "DTLloadrawdb",
        "embedfile",
        "attachfile",
        "textattachfile",
        "LVerbatimInput",
        "BVerbatimInput",
        "verbatimtabinput",
        "listinginput",
    }
)
FIGURE_EXTENSIONS: frozenset[str] = frozenset({"", ".pdf", ".png", ".jpg", ".jpeg"})

BIB_RESOURCE_COMMANDS: frozenset[str] = frozenset(
    {"addbibresource", "addglobalbib", "addsectionbib", "bibliography", "nobibliography"}
)
# En una sección, citations deja estos comandos a conv.latex.structure: no deben quedar sin regla.
assert BIB_RESOURCE_COMMANDS <= STRUCTURE_COMMANDS
CITE_COMMANDS: frozenset[str] = frozenset(
    {
        "cite",
        "Cite",
        "parencite",
        "Parencite",
        "footcite",
        "footcitetext",
        "textcite",
        "Textcite",
        "smartcite",
        "Smartcite",
        "autocite",
        "Autocite",
        "supercite",
        "citeauthor",
        "Citeauthor",
        "citetitle",
        "Citetitle",
        "citeyear",
        "citedate",
        "citeurl",
        "citeurldate",
        "citefield",
        "citename",
        "citelist",
        "fullcite",
        "footfullcite",
        "nocite",
        "notecite",
        "Notecite",
        "pnotecite",
        "Pnotecite",
        "fnotecite",
        "citep",
        "citet",
        "citealp",
        "citealt",
        "Citep",
        "Citet",
        "Citealp",
        "Citealt",
        "citenum",
        "citeyearpar",
    }
)
MULTICITE_COMMANDS: frozenset[str] = frozenset(
    {
        "cites",
        "Cites",
        "parencites",
        "Parencites",
        "footcites",
        "footcitetexts",
        "smartcites",
        "Smartcites",
        "textcites",
        "Textcites",
        "supercites",
        "autocites",
        "Autocites",
    }
)
VOLCITE_COMMANDS: frozenset[str] = frozenset(
    {
        "volcite",
        "Volcite",
        "pvolcite",
        "Pvolcite",
        "fvolcite",
        "Fvolcite",
        "ftvolcite",
        "svolcite",
        "Svolcite",
        "tvolcite",
        "Tvolcite",
        "avolcite",
        "Avolcite",
    }
)
REF_COMMANDS: frozenset[str] = frozenset(
    {"ref", "eqref", "autoref", "Autoref", "pageref", "nameref", "Nameref", "vref", "Vref", "vpageref", "fref", "Fref"}
)
MULTIREF_COMMANDS: frozenset[str] = frozenset(
    {"cref", "Cref", "cpageref", "Cpageref", "labelcref", "labelcpageref", "namecref", "nameCref", "lcnamecref"}
)
RANGEREF_COMMANDS: frozenset[str] = frozenset({"crefrange", "Crefrange", "cpagerefrange", "Cpagerefrange"})

KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_:-]*$")  # REQ-CONV-49
MAX_KEY = 256
FORBIDDEN_FIELDS: Mapping[str, Severity] = {"file": "error", "abstract": "warning", "annotation": "warning", "keywords": "warning"}

# -- códigos y marcadores (finding.<code>.* en latex.toml) ----------------------

CITATION_CODES: Mapping[str, frozenset[str]] = {"E_VALIDATION_CITATION": frozenset({"detail"})}
LABEL_CODES: Mapping[str, frozenset[str]] = {"E_VALIDATION_LABEL": frozenset({"detail"})}
MACRO_CODES: Mapping[str, frozenset[str]] = {"E_VALIDATION_MACRO": frozenset({"command"})}
CONVENTION_CODES: Mapping[str, frozenset[str]] = {
    "conv.main.grammar": frozenset({"detail"}),
    "conv.main.sections": frozenset({"detail", "fix"}),
    "conv.outline.grammar": frozenset({"detail"}),
    "conv.outline.parts": frozenset({"detail", "fix"}),
    "conv.outline.missing": frozenset({"detail", "fix"}),
    "conv.latex.structure": frozenset({"command"}),
    "conv.latex.forbidden-command": frozenset({"command", "reason"}),
    "conv.latex.hidden-block": frozenset({"command"}),
    "conv.latex.path-outside-section": frozenset({"command", "target", "detail"}),
    "conv.latex.sentence-per-line": frozenset({"excerpt"}),
    "conv.latex.shell-package": frozenset({"package"}),
    "conv.bib.key-syntax": frozenset({"key"}),
    "conv.bib.forbidden-field": frozenset({"key", "field"}),
    "conv.bib.pending-consolidated": frozenset({"key"}),
    "conv.bib.pending-conflict": frozenset({"key", "other"}),
}
TREE_MODES: frozenset[Mode] = frozenset({"tree", "pr", "base"})


# -- lectura del árbol -----------------------------------------------------------


@dataclass(frozen=True)
class Doc:
    """Un ``.tex`` del árbol con su clase y su análisis."""

    path: str
    kind: str  # 'main' | 'preamble' | 'notation' | 'lead' | 'section' | 'outline'
    section: str | None  # la parte (section) o la sección del artículo (outline)
    scan: tex.Scan


# Clases con texto del artículo: las partes y los archivos de enlace del editor (02 §4.1.1).
TEXT_KINDS: frozenset[str] = frozenset({"section", "outline"})


@dataclass
class Corpus:
    """Los ``.tex`` del árbol ya analizados; se comparte entre las reglas de un mismo árbol."""

    docs: list[Doc] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    bibs: dict[str, BibFile | None] = field(default_factory=dict)  # None = ilegible


_CORPUS: weakref.WeakKeyDictionary[object, Corpus] = weakref.WeakKeyDictionary()


def doc_kind(path: str) -> tuple[str, str | None]:
    """Clase de un ``.tex`` de ``tex/`` o de ``sections/`` y su id: el de la parte o el del archivo de enlace."""
    if path == MAIN:
        return "main", None
    if path == PREAMBLE:
        return "preamble", None
    if path == NOTATION:
        return "notation", None
    parts = path.split("/")
    if len(parts) >= 3 and path.startswith(SECTIONS_DIR):
        return "section", parts[1]
    outline = outline_id(path)
    if outline is not None:
        return "outline", outline
    return "lead", None


def outline_id(path: str) -> str | None:
    """``<id>`` de un archivo de enlace ``tex/outline/<id>.tex`` con un id válido (02 §4.1.1); si no, ``None``."""
    if not path.startswith(OUTLINE_DIR) or not path.endswith(".tex"):
        return None
    name = path[len(OUTLINE_DIR) : -len(".tex")]
    return name if tex.SECTION_ID_PATTERN.fullmatch(name) else None


def _is_tex(path: str, mode: str) -> bool:
    return (
        path.startswith(("tex/", SECTIONS_DIR))
        and path.endswith(".tex")
        and is_regular_mode(mode)
        and not path.startswith(LOCAL_DIRS)
    )


def pending_path(section: str) -> str:
    return f"{SECTIONS_DIR}{section}/pending.bib"


def section_target(target: str) -> tuple[str, str] | None:
    """``(id, resto)`` de una ruta de LaTeX ``../sections/<id>/<resto>`` con un id válido; si no, ``None``."""
    if not target.startswith(SECTIONS_RESOURCE):
        return None
    section, sep, rest = target[len(SECTIONS_RESOURCE) :].partition("/")
    if not sep or tex.SECTION_ID_PATTERN.fullmatch(section) is None:
        return None
    return section, rest


def corpus(inp: RuleInput, dl: tex.Deadline) -> Corpus:
    """Análisis de los ``.tex`` de ``inp.tree``, cacheado por árbol."""
    tree = inp.tree
    assert tree is not None
    try:
        cached = _CORPUS.get(tree)
    except TypeError:
        cached = None
    if cached is not None:
        return cached
    out = Corpus()
    for path, mode in sorted(tree.entries().items()):
        if not _is_tex(path, mode):
            continue
        dl.check()
        text = tree.read_text(path, MAX_FILE_BYTES)
        if text is None:
            out.unreadable.append(path)
            continue
        kind, section = doc_kind(path)
        out.docs.append(Doc(path=path, kind=kind, section=section, scan=tex.scan(text, deadline=dl)))
    try:
        _CORPUS[tree] = out
    except TypeError:
        pass
    return out


def bib_file(inp: RuleInput, c: Corpus, path: str, dl: tex.Deadline) -> BibFile | None | bool:
    """``BibFile`` de ``path``; ``False`` si no existe; ``None`` si no se puede leer."""
    tree = inp.tree
    assert tree is not None
    mode = tree.entries().get(path)
    if mode is None:
        return False
    if path in c.bibs:
        return c.bibs[path]
    if not is_regular_mode(mode):
        c.bibs[path] = None
        return None
    text = tree.read_text(path, MAX_FILE_BYTES)
    parsed = None if text is None else parse_bib(text, deadline=dl)
    c.bibs[path] = parsed
    return parsed


def pending_files(inp: RuleInput) -> list[tuple[str, str]]:
    """``(parte, ruta)`` de cada ``sections/<id>/pending.bib`` del árbol."""
    tree = inp.tree
    assert tree is not None
    out: list[tuple[str, str]] = []
    for path in sorted(tree.entries()):
        parts = path.split("/")
        if len(parts) == 3 and path.startswith(SECTIONS_DIR) and parts[2] == "pending.bib":
            out.append((parts[1], path))
    return out


def quote(text: str, limit: int = MAX_QUOTE) -> str:
    """Texto del árbol para citar en un mensaje: una línea, sin acentos graves y recortado."""
    one = " ".join(text.replace("`", "'").split())
    if len(one) > limit:
        one = one[: limit - 1] + "…"
    return one


def command_text(sc: tex.Scan, cmd: tex.Command, a: tex.Args) -> str:
    return quote(sc.text[cmd.offset : a.end])


def _guard(rule_id: str, impl: Callable[[RuleInput, tex.Deadline], list[Finding]]) -> Callable[[RuleInput], list[Finding]]:
    def run(inp: RuleInput) -> list[Finding]:
        spec = _BY_ID[rule_id]
        if inp.tree is None:
            return [unavailable(spec, inp, inp.t("latex.reason.no_tree"))]
        dl = tex.Deadline(RULE_TIME_BUDGET_S)
        try:
            return impl(inp, dl)
        except tex.ScanTimeout:
            return [unavailable(spec, inp, inp.t("latex.reason.timeout"))]

    run.__name__ = f"run_{rule_id.replace('-', '_')}"
    return run


def _unreadable(spec: RuleSpec, inp: RuleInput, paths: Iterable[str]) -> list[Finding]:
    return [unavailable(spec, inp, inp.t("latex.reason.unreadable"), path=p) for p in paths]


# -- citations -------------------------------------------------------------------


def _accepted_resource(sc: tex.Scan, cmd: tex.Command, a: tex.Args) -> bool:
    if cmd.name != "addbibresource" or a.star or a.options or len(a.groups) != 1 or not a.complete:
        return False
    target = a.groups[0]
    if target == REFERENCES_RESOURCE:
        return True
    found = section_target(target)  # ../sections/<id>/pending.bib
    return found is not None and found[1] == "pending.bib"


def cite_keys(sc: tex.Scan, cmd: tex.Command, dl: tex.Deadline) -> list[str]:
    """Claves de una cita (``\\cite{a,b}``, ``\\cites{a}{b}``, ``\\volcite{3}{k}``)."""
    if cmd.name in MULTICITE_COMMANDS:
        groups = sc.args(cmd, multi=True, deadline=dl).groups
    elif cmd.name in VOLCITE_COMMANDS:
        groups = sc.args(cmd, groups=2, deadline=dl).groups[1:2]
    else:
        groups = sc.args(cmd, groups=1, deadline=dl).groups
    keys: list[str] = []
    for g in groups:
        keys.extend(k for k in tex.split_keys(g) if k != "*" and "#" not in k)
    return keys


def _run_citations(inp: RuleInput, dl: tex.Deadline) -> list[Finding]:
    rule = CITATIONS
    c = corpus(inp, dl)
    out = _unreadable(rule, inp, c.unreadable)

    def finding(path: str, line: int | None, msg: str, /, **kw: object) -> Finding:
        return make_finding(rule, inp, "E_VALIDATION_CITATION", severity="error", path=path, line=line, detail=inp.t(msg, **kw))

    def keys_of(path: str) -> frozenset[str] | None:
        bib = bib_file(inp, c, path, dl)
        if bib is False:
            return frozenset()
        if not isinstance(bib, BibFile):
            out.append(unavailable(rule, inp, inp.t("latex.reason.unreadable"), path=path))
            return None
        for err in bib.errors:
            out.append(finding(path, err.line, f"latex.citation.bib_{err.kind.replace('-', '_')}", file=path))
        return bib.keys()

    refs = keys_of(REFERENCES)
    pendings: dict[str, frozenset[str] | None] = {sec: keys_of(p) for sec, p in pending_files(inp)}
    all_keys: frozenset[str] | None = refs
    if refs is not None:
        for keys in pendings.values():
            all_keys = None if keys is None or all_keys is None else all_keys | keys
    for doc in c.docs:
        sc = doc.scan
        for cmd in sc.commands:
            dl.tick()
            if cmd.name in BIB_RESOURCE_COMMANDS:
                if doc.kind in TEXT_KINDS:
                    continue  # conv.latex.structure
                a = sc.args(cmd, groups=1, deadline=dl)
                text = command_text(sc, cmd, a)
                if doc.kind != "main":
                    out.append(finding(doc.path, cmd.line, "latex.citation.resource_place", text=text))
                elif not _accepted_resource(sc, cmd, a):
                    out.append(finding(doc.path, cmd.line, "latex.citation.resource", text=text))
                continue
            if cmd.name not in CITE_COMMANDS and cmd.name not in MULTICITE_COMMANDS and cmd.name not in VOLCITE_COMMANDS:
                continue
            if doc.kind == "section":
                assert doc.section is not None
                own = pendings.get(doc.section, frozenset())
                if refs is None or own is None:
                    continue  # ya hay un hallazgo unavailable
                known = refs | own
                pending = pending_path(doc.section)
                for key in cite_keys(sc, cmd, dl):
                    if key not in known:
                        out.append(finding(doc.path, cmd.line, "latex.citation.undefined", key=quote(key), pending=pending))
            else:
                if all_keys is None:
                    continue
                for key in cite_keys(sc, cmd, dl):
                    if key not in all_keys:
                        out.append(finding(doc.path, cmd.line, "latex.citation.undefined_lead", key=quote(key)))
    return out


# -- labels-refs -----------------------------------------------------------------


def _ref_targets(sc: tex.Scan, cmd: tex.Command, dl: tex.Deadline) -> list[str]:
    if cmd.name in REF_COMMANDS:
        groups = sc.args(cmd, groups=1, deadline=dl).groups
        return [g.strip() for g in groups if g.strip()]
    if cmd.name in MULTIREF_COMMANDS:
        groups = sc.args(cmd, groups=1, deadline=dl).groups
        return [k for g in groups for k in tex.split_keys(g)]
    if cmd.name in RANGEREF_COMMANDS:
        return [g.strip() for g in sc.args(cmd, groups=2, deadline=dl).groups if g.strip()]
    if cmd.name == "hyperref":
        opts = sc.args(cmd, groups=0, deadline=dl).options
        return [opts[0].strip()] if opts and opts[0].strip() else []
    return []


def _run_labels(inp: RuleInput, dl: tex.Deadline) -> list[Finding]:
    rule = LABELS_REFS
    c = corpus(inp, dl)
    out = _unreadable(rule, inp, c.unreadable)

    def finding(path: str, line: int | None, msg: str, /, *, severity: Severity = "error", **kw: object) -> Finding:
        return make_finding(rule, inp, "E_VALIDATION_LABEL", severity=severity, path=path, line=line, detail=inp.t(msg, **kw))

    defined: dict[str, list[tuple[str, int]]] = {}
    for doc in c.docs:
        sc = doc.scan
        for cmd in sc.commands:
            dl.tick()
            if cmd.name != "label":
                continue
            groups = sc.args(cmd, groups=1, options=False, deadline=dl).groups
            if not groups:
                continue
            label = groups[0].strip()
            if "#" in label:
                continue  # cuerpo de una definición
            defined.setdefault(label, []).append((doc.path, cmd.line))
            shown = quote(label)
            if doc.kind not in TEXT_KINDS:
                out.append(finding(doc.path, cmd.line, "latex.label.lead", label=shown))
                continue
            parsed = tex.parse_label(label)
            if parsed is None:
                out.append(finding(doc.path, cmd.line, "latex.label.malformed", label=shown))
            elif parsed.section != doc.section:
                # Un archivo de enlace usa el <id> de su sección del artículo (REQ-CONV-92).
                key = "latex.label.foreign" if doc.kind == "section" else "latex.label.foreign_outline"
                out.append(finding(doc.path, cmd.line, key, label=shown, other=parsed.section, section=doc.section))
    for label, places in defined.items():
        if len(places) < 2:
            continue
        for i, (path, line) in enumerate(places):
            other_path, other_line = places[1] if i == 0 else places[0]
            out.append(finding(path, line, "latex.label.duplicate", label=quote(label), other=other_path, number=other_line))
    for doc in c.docs:
        sc = doc.scan
        for cmd in sc.commands:
            dl.tick()
            for target in _ref_targets(sc, cmd, dl):
                if "#" in target or target in defined:
                    continue
                out.append(
                    finding(
                        doc.path,
                        cmd.line,
                        "latex.label.undefined_ref",
                        severity="warning",
                        command="\\" + cmd.name,
                        label=quote(target),
                    )
                )
    return out


# -- latex-macros ----------------------------------------------------------------


def is_definition(name: str) -> bool:
    return name in DEFINITION_COMMANDS or name.endswith(DEFINITION_SUFFIXES)


def _run_macros(inp: RuleInput, dl: tex.Deadline) -> list[Finding]:
    rule = LATEX_MACROS
    c = corpus(inp, dl)
    out = _unreadable(rule, inp, c.unreadable)
    for doc in c.docs:
        if doc.kind == "notation":
            continue
        allowed = LEAD_DEFINITION_EXCEPTIONS if doc.kind in ("main", "preamble") else frozenset()
        for cmd in doc.scan.commands:
            dl.tick()
            if is_definition(cmd.name) and cmd.name not in allowed:
                out.append(
                    make_finding(rule, inp, "E_VALIDATION_MACRO", severity="error", path=doc.path, line=cmd.line, command="\\" + cmd.name)
                )
    return out


# -- convention: tex/main.tex (conv.main.*) ---------------------------------------

_DOCCLASS = re.compile(r"\\documentclass(?:\[[^\[\]{}\\]*\])?\{[A-Za-z0-9._-]+\}")
_INPUT = re.compile(r"\\input\{([^{}\\]*)\}")
_BIB_LINE = re.compile(r"\\(?:addbibresource|addglobalbib|addsectionbib|bibliography|nobibliography)(?![A-Za-z@])")
_EXACT_BIB = re.compile(r"\\addbibresource\{([^{}\\]*)\}")
_PRINTBIB = re.compile(r"\\printbibliography(?:\[[^\[\]{}]*\])?")
BODY_COMMANDS: frozenset[str] = frozenset(
    "\\" + name
    for name in (
        "maketitle",
        "tableofcontents",
        "listoffigures",
        "listoftables",
        "appendix",
        "frontmatter",
        "mainmatter",
        "backmatter",
        "clearpage",
        "cleardoublepage",
        "newpage",
    )
)
# Rangos de la gramática de §4.1: el orden de los bloques es el de estos números.
R_CLASS, R_PREAMBLE, R_NOTATION, R_BIB, R_BEGIN, R_BODY, R_END = 1, 2, 3, 4, 5, 6, 7
_UNIQUE = {
    R_CLASS: "\\documentclass[...]{...}",
    R_PREAMBLE: "\\input{preamble}",
    R_NOTATION: "\\input{notation}",
    R_BEGIN: "\\begin{document}",
    R_END: "\\end{document}",
}


@dataclass(frozen=True)
class MainLine:
    number: int
    text: str
    rank: int
    kind: str  # 'refs', 'pending', 'bib', 'outline', 'lead-input', 'other'
    section: str | None = None  # la parte (pending) o la sección del artículo (outline)


def classify_main_line(text: str, entries: Mapping[str, str]) -> tuple[int, str, str | None] | str:
    """``(rango, tipo, sección)`` de una línea de ``main.tex``; si no es de la gramática, la clave del motivo."""
    if len(text) > MAX_MAIN_LINE:
        return "latex.main.unknown_line"
    if _DOCCLASS.fullmatch(text):
        return R_CLASS, "other", None
    if text == "\\begin{document}":
        return R_BEGIN, "other", None
    if text == "\\end{document}":
        return R_END, "other", None
    if text in BODY_COMMANDS or _PRINTBIB.fullmatch(text):
        return R_BODY, "other", None
    if _BIB_LINE.match(text):
        m = _EXACT_BIB.fullmatch(text)
        if m is not None and m.group(1) == REFERENCES_RESOURCE:
            return R_BIB, "refs", None
        found = section_target(m.group(1)) if m is not None else None
        if found is not None and found[1] == "pending.bib":
            return R_BIB, "pending", found[0]
        return R_BIB, "bib", None  # la forma exacta la exige citations
    m = _INPUT.fullmatch(text)
    if m is None:
        return "latex.main.unknown_line"
    target = m.group(1)
    if target == "preamble":
        return R_PREAMBLE, "other", None
    if target == "notation":
        return R_NOTATION, "other", None
    if target.startswith(OUTLINE_RESOURCE):  # outline/<id>: un archivo de enlace (02 §4.1.1)
        outline = target[len(OUTLINE_RESOURCE) :]
        if tex.SECTION_ID_PATTERN.fullmatch(outline):
            return R_BODY, "outline", outline
        return "latex.main.input_target"
    if section_target(target) is not None:  # las partes se incluyen desde tex/outline/ (D-56)
        return "latex.main.part_input"
    parts = target.split("/")
    if len(parts) == 1 and target:
        name = target if target.endswith(".tex") else target + ".tex"
        if name not in ("main.tex", "preamble.tex", "notation.tex") and is_regular_mode(entries.get("tex/" + name)):
            return R_BODY, "lead-input", None
    return "latex.main.input_target"


def _main_findings(
    inp: RuleInput, doc: Doc, sections: Sequence[str] | None, outlines: Sequence[str], dl: tex.Deadline
) -> list[Finding]:
    rule = CONVENTION
    assert inp.tree is not None
    entries = inp.tree.entries()
    out: list[Finding] = []

    def grammar(line: int | None, msg: str, /, **kw: object) -> None:
        out.append(make_finding(rule, inp, "conv.main.grammar", severity="error", path=MAIN, line=line, detail=inp.t(msg, **kw)))

    lines: list[MainLine] = []
    top = 0
    seen: dict[int, int] = {}
    for number, raw in enumerate(doc.scan.lines(), start=1):
        dl.tick()
        text = raw.strip()
        if not text:
            continue
        got = classify_main_line(text, entries)
        if isinstance(got, str):
            grammar(number, got, text=quote(text))
            continue
        rank, kind, section = got
        if rank in _UNIQUE and rank in seen:
            grammar(number, "latex.main.repeated", text=quote(text))
            continue
        if rank < top:
            grammar(number, "latex.main.order", text=quote(text))
            seen.setdefault(rank, number)  # está, aunque fuera de orden: no se informa como ausente
            continue
        top = max(top, rank)
        seen.setdefault(rank, number)
        lines.append(MainLine(number=number, text=text, rank=rank, kind=kind, section=section))
    for rank, shown in _UNIQUE.items():
        if rank not in seen:
            grammar(None, "latex.main.missing", text=shown)
    refs = [ln for ln in lines if ln.kind == "refs"]
    if not refs:
        grammar(None, "latex.main.missing", text=f"\\addbibresource{{{REFERENCES_RESOURCE}}}")
    for ln in refs[1:]:
        grammar(ln.number, "latex.main.repeated", text=quote(ln.text))
    out.extend(_main_sections(inp, lines, sections, outlines))
    return out


def _main_sections(
    inp: RuleInput, lines: Sequence[MainLine], sections: Sequence[str] | None, outlines: Sequence[str]
) -> list[Finding]:
    """REQ-CONV-18 y REQ-CONV-19: un ``pending.bib`` por parte y un ``outline/<id>`` por archivo de enlace.

    Una línea de más (de una parte no declarada, de un archivo de enlace que no existe, o repetida) es
    error; una que falta es el aviso ``conv.outline.missing``, para que el alta de una parte pueda
    hacerse en dos tareas, la del responsable y la del editor (02 §4.2.1; 00-marco D-56). Sin
    ``roles.yaml`` válido no se juzgan los ``pending.bib``.
    """
    rule = CONVENTION
    out: list[Finding] = []
    t = inp.t

    def add(line: int, detail: str, fix: str) -> None:
        out.append(make_finding(rule, inp, "conv.main.sections", severity="error", path=MAIN, line=line, detail=detail, fix=fix))

    def missing(detail: str, fix: str) -> None:
        out.append(make_finding(rule, inp, "conv.outline.missing", severity="warning", path=MAIN, line=None, detail=detail, fix=fix))

    pendings = [ln for ln in lines if ln.kind == "pending"]
    if sections is not None:
        seen: set[str] = set()
        for ln in pendings:
            assert ln.section is not None
            text = quote(ln.text)
            if ln.section not in sections:
                add(
                    ln.number,
                    t("latex.main.undeclared", text=text, section=ln.section),
                    t("latex.main.undeclared_fix", text=text, section=ln.section),
                )
            elif ln.section in seen:
                add(ln.number, t("latex.main.duplicate", text=text), t("latex.main.duplicate_fix", text=text))
            seen.add(ln.section)
        for sec in sections:
            if sec not in seen:
                missing(t("latex.main.missing_bib", section=sec), t("latex.main.missing_bib_fix", section=sec))
    included: set[str] = set()
    for ln in lines:
        if ln.kind != "outline":
            continue
        assert ln.section is not None
        text = quote(ln.text)
        if ln.section not in outlines:
            add(
                ln.number,
                t("latex.main.outline_unknown", text=text, outline=ln.section),
                t("latex.main.outline_unknown_fix", text=text, outline=ln.section),
            )
        elif ln.section in included:
            add(ln.number, t("latex.main.duplicate", text=text), t("latex.main.duplicate_fix", text=text))
        included.add(ln.section)
    for outline in outlines:
        if outline not in included:
            missing(t("latex.main.missing_outline", outline=outline), t("latex.main.missing_outline_fix", outline=outline))
    refs = [ln for ln in lines if ln.kind == "refs"]
    for ln in pendings:
        if refs and ln.number < refs[0].number:
            text = quote(ln.text)
            add(ln.number, t("latex.main.references_first", text=text), t("latex.main.references_first_fix", text=text))
    return out


# -- convention: tex/outline/<id>.tex (conv.outline.*) ------------------------------

_PART_INPUT = re.compile(r"\\input\{\.\./sections/([a-z0-9]+(?:-[a-z0-9]+)*)/([a-z0-9]+(?:-[a-z0-9]+)*)\}")


def _outline_head(sc: tex.Scan, meaningful: Sequence[tuple[int, str]], outline: str, dl: tex.Deadline) -> bool:
    """REQ-CONV-92: las dos primeras líneas con contenido son ``\\section{<título>}`` y ``\\label{sec:<id>}``.

    La primera tiene solo un ``\\section`` o ``\\section*``, sin título corto y con el título
    entre llaves en esa misma línea.
    """
    if len(meaningful) < 2 or meaningful[1][1] != f"\\label{{sec:{outline}}}":
        return False
    number, text = meaningful[0]
    cmd = next((c for c in sc.commands if c.line == number), None)
    if cmd is None or cmd.name != "section":
        return False
    a = sc.args(cmd, groups=1, deadline=dl)
    return not a.options and a.braced and a.complete and len(a.groups) == 1 and sc.text[cmd.offset : a.end].strip() == text


def _outline_findings(
    inp: RuleInput, docs: Sequence[Doc], sections: Sequence[str] | None, dl: tex.Deadline, *, report_missing: bool
) -> list[Finding]:
    """REQ-CONV-92 y REQ-CONV-93: la cabecera de cada archivo de enlace y cada parte incluida una sola vez."""
    rule = CONVENTION
    t = inp.t
    out: list[Finding] = []
    included: dict[str, tuple[str, int]] = {}

    def add(code: str, severity: Severity, path: str, line: int | None, **kw: str) -> None:
        out.append(make_finding(rule, inp, code, severity=severity, path=path, line=line, **kw))

    for doc in docs:
        sc = doc.scan
        outline = doc.section
        assert outline is not None
        lines = sc.lines()
        meaningful = list(islice(((n, raw.strip()) for n, raw in enumerate(lines, start=1) if raw.strip()), 2))
        if not _outline_head(sc, meaningful, outline, dl):
            first = meaningful[0][0] if meaningful else None
            add("conv.outline.grammar", "error", doc.path, first, detail=t("latex.outline.head", outline=outline))
        # La primera línea es la cabecera, válida o ya informada: su comando de sección no cuenta como otro.
        head = {n for n, _text in meaningful[:1]}
        for cmd in sc.commands:
            dl.tick()
            if cmd.name in HEADING_COMMANDS and cmd.line not in head:
                add("conv.outline.grammar", "error", doc.path, cmd.line, detail=t("latex.outline.heading", command="\\" + cmd.name))
        for number in sorted({cmd.line for cmd in sc.commands if cmd.name == "input"}):
            dl.tick()
            text = lines[number - 1].strip()
            m = _PART_INPUT.fullmatch(text)
            if m is None or m.group(1) != m.group(2):
                add("conv.outline.grammar", "error", doc.path, number, detail=t("latex.outline.input", text=quote(text)))
                continue
            part = m.group(1)
            if sections is not None and part not in sections:
                add(
                    "conv.outline.parts",
                    "error",
                    doc.path,
                    number,
                    detail=t("latex.outline.undeclared", part=part),
                    fix=t("latex.outline.undeclared_fix", part=part),
                )
            elif part in included:
                other, other_line = included[part]
                add(
                    "conv.outline.parts",
                    "error",
                    doc.path,
                    number,
                    detail=t("latex.outline.duplicate", part=part, other=other, number=other_line),
                    fix=t("latex.outline.duplicate_fix", part=part),
                )
            else:
                included[part] = (doc.path, number)
    for part in sections if sections is not None and report_missing else ():
        if part not in included:
            add(
                "conv.outline.missing",
                "warning",
                OUTLINE_DIR.rstrip("/"),
                None,
                detail=t("latex.outline.missing_part", part=part),
                fix=t("latex.outline.missing_part_fix", part=part),
            )
    return out


# -- convention: archivos .tex (conv.latex.*) --------------------------------------


def _unsafe(target: str) -> bool:
    """Ruta vacía o absoluta, o con ``~``, ``|`` o ``\\`` (TX03); los ``..`` los juzga ``_path_problem``."""
    if not target:
        return True
    if target.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", target[:2]):
        return True
    return "~" in target or "|" in target or "\\" in target


def _suffix(target: str) -> str:
    name = target.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    return "" if dot <= 0 else name[dot:]


def _path_problem(cmd: tex.Command, a: tex.Args, section: str) -> tuple[str, str] | None:
    """``(target, clave del motivo)`` si la inclusión incumple REQ-CONV-31..33 o TX03.

    El único ``..`` admitido es el del prefijo exacto ``../sections/<id-propio>/`` (la
    compilación corre en ``tex/``): uno detrás del prefijo, como ``../sections/<id>/../x``,
    saldría de la carpeta. Sin el prefijo propio (otra parte, ``../../x``, la forma
    anterior ``sections/<id>/``), el motivo es el prefijo que falta.
    """
    if cmd.name in OTHER_READERS:
        target = a.groups[0].strip() if a.groups else ""
        return target, "latex.path.reader"
    if not a.groups:
        return "", "latex.path.no_braces" if cmd.name == "input" else "latex.path.missing"
    target = a.groups[0].strip()
    if _unsafe(target):
        return target, "latex.path.unsafe"
    prefix = f"{SECTIONS_RESOURCE}{section}/"
    if not target.startswith(prefix):
        return target, "latex.path.prefix"
    if ".." in target[len(prefix) :].split("/"):
        return target, "latex.path.unsafe"
    if cmd.name == "input" and _suffix(target) not in ("", ".tex"):
        return target, "latex.path.not_tex"
    if cmd.name == "includegraphics" and (
        not target.startswith(prefix + "figures/") or _suffix(target) not in FIGURE_EXTENSIONS
    ):
        return target, "latex.path.figure"
    return None


def _tex_findings(inp: RuleInput, doc: Doc, engine: str, dl: tex.Deadline) -> list[Finding]:
    rule = CONVENTION
    sc = doc.scan
    out: list[Finding] = []
    t = inp.t

    def add(code: str, line: int | None, severity: Severity = "error", **kw: str) -> None:
        out.append(make_finding(rule, inp, code, severity=severity, path=doc.path, line=line, **kw))

    # Las partes y los archivos de enlace tienen texto del artículo: les valen §4.3 a §4.6 (REQ-CONV-92).
    text_doc = doc.kind in TEXT_KINDS
    lua_blocked = not text_doc and engine != "lualatex"
    for cmd in sc.commands:
        dl.tick()
        name = cmd.name
        shown = "\\" + name
        if name in ("begin", "end"):
            groups = sc.args(cmd, groups=1, options=False, deadline=dl).groups
            env = groups[0].strip() if groups else ""
            shown = f"\\{name}{{{quote(env)}}}"
            if text_doc and name == "begin" and env in EXEC_ENVS:
                add("conv.latex.forbidden-command", cmd.line, command=shown, reason=t("latex.forbidden.exec"))
            elif lua_blocked and name == "begin" and env in LUA_ENVS:
                add("conv.latex.forbidden-command", cmd.line, command=shown, reason=t("latex.forbidden.lua_engine"))
            if text_doc and name == "begin" and env in HIDDEN_ENVS:
                add("conv.latex.hidden-block", cmd.line, command=shown)
            if env == "document" and (text_doc or doc.kind == "notation"):
                add("conv.latex.structure", cmd.line, command=shown)
            continue
        if text_doc:
            if name == "write" and sc.text.startswith("18", cmd.end):
                add("conv.latex.forbidden-command", cmd.line, command="\\write18", reason=t("latex.forbidden.exec"))
            elif name in EXEC_COMMANDS:
                add("conv.latex.forbidden-command", cmd.line, command=shown, reason=t("latex.forbidden.exec"))
            elif name in IO_COMMANDS:
                add("conv.latex.forbidden-command", cmd.line, command=shown, reason=t("latex.forbidden.io"))
            # REQ-CONV-94: el título de la sección lo pone su archivo de enlace (en este, conv.outline.grammar).
            if name in STRUCTURE_COMMANDS or (doc.kind == "section" and name in HEADING_COMMANDS):
                add("conv.latex.structure", cmd.line, command=shown)
            if name in HIDDEN_COMMANDS:
                add("conv.latex.hidden-block", cmd.line, command=shown)
            if name == "input" and doc.kind == "outline":
                continue  # los \input de un archivo de enlace los juzga conv.outline.grammar
            if name in PATH_COMMANDS or name in OTHER_READERS:
                a = sc.args(cmd, groups=1, options=name != "input", deadline=dl)
                assert doc.section is not None
                if doc.kind == "outline":
                    problem: tuple[str, str] | None = (a.groups[0].strip() if a.groups else "", "latex.path.outline")
                else:
                    problem = _path_problem(cmd, a, doc.section)
                if problem is not None:
                    target, key = problem
                    if not a.groups and not target:
                        target = quote(sc.text[cmd.end : cmd.end + 40].split("\n", 1)[0].strip()) or "…"
                    add(
                        "conv.latex.path-outside-section",
                        cmd.line,
                        command=shown,
                        target=quote(target),
                        detail=t(key, section=doc.section),
                    )
        else:
            if lua_blocked and name in LUA_COMMANDS:
                add("conv.latex.forbidden-command", cmd.line, command=shown, reason=t("latex.forbidden.lua_engine"))
            if doc.kind == "notation" and name in NOTATION_STRUCTURE:
                add("conv.latex.structure", cmd.line, command=shown)
            if doc.kind == "preamble" and name in ("usepackage", "RequirePackage"):
                groups = sc.args(cmd, groups=1, deadline=dl).groups
                for package in tex.split_keys(groups[0]) if groups else []:
                    if package in SHELL_PACKAGES:
                        add("conv.latex.shell-package", cmd.line, package=quote(package))
    if text_doc:
        for line in sc.carets:
            add("conv.latex.forbidden-command", line, command="^^", reason=t("latex.forbidden.carets"))
    if text_doc or doc.kind == "lead":
        for number, text in tex.prose_lines(sc, deadline=dl):
            col = tex.first_sentence_break(text, deadline=dl)
            if col is not None:
                excerpt = quote(text[max(0, col - 30) : col + 30], 60)
                add("conv.latex.sentence-per-line", number, severity="warning", excerpt=excerpt)
    return out


# -- convention: memory/**/*.md (REQ-CONV-26) -------------------------------------


def markdown_prose(text: str) -> Iterable[tuple[int, str]]:
    """Líneas de prosa de un ``.md``: sin front matter, bloques de código, tablas ni código en línea."""
    lines = text.split("\n")
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                start = i + 1
                break
    fence: str | None = None
    for i in range(start, len(lines)):
        raw = lines[i]
        stripped = raw.strip()
        if fence is not None:
            if stripped.startswith(fence):
                fence = None
            continue
        if stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
            continue
        if not stripped or raw.startswith(("    ", "\t")) or stripped.startswith(("|", "<!--")):
            continue
        line = raw[: MAX_MAIN_LINE * 20]
        # El número de un título o de una lista («## 1. Propósito», «2. Paso») no cierra oración.
        m = _MD_MARKER.match(line)
        if m is not None:
            line = " " * m.end() + line[m.end() :]
        yield i + 1, _MD_CODE.sub(lambda c: " " * len(c.group(0)), line)


_MD_MARKER = re.compile(r"^ {0,3}(?:#{1,6} +|> ?)?(?:\d{1,9}[.)] +|[-*+] +)?")
_MD_CODE = re.compile(r"`[^`\n]{0,20000}`")


def _memory_findings(inp: RuleInput, dl: tex.Deadline) -> list[Finding]:
    tree = inp.tree
    assert tree is not None
    out: list[Finding] = []
    for path, mode in sorted(tree.entries().items()):
        if not (path.startswith("memory/") and path.endswith(".md") and is_regular_mode(mode)):
            continue
        dl.check()
        text = tree.read_text(path, MAX_FILE_BYTES)
        if text is None:
            continue  # conv.file.* lo informa
        for number, line in markdown_prose(text):
            dl.tick()
            col = tex.first_sentence_break(line, deadline=dl)
            if col is not None:
                out.append(
                    make_finding(
                        CONVENTION,
                        inp,
                        "conv.latex.sentence-per-line",
                        severity="warning",
                        path=path,
                        line=number,
                        excerpt=quote(line[max(0, col - 30) : col + 30], 60),
                    )
                )
    return out


# -- convention: bibliografía (conv.bib.*) ----------------------------------------


def _bib_findings(inp: RuleInput, c: Corpus, dl: tex.Deadline) -> list[Finding]:
    rule = CONVENTION
    out: list[Finding] = []
    files: list[tuple[str, BibFile]] = []
    for path in [REFERENCES] + [p for _sec, p in pending_files(inp)]:
        bib = bib_file(inp, c, path, dl)
        if isinstance(bib, BibFile):
            files.append((path, bib))
    refs_keys = next((b.keys() for p, b in files if p == REFERENCES), frozenset())
    seen: dict[str, list[tuple[str, str, int]]] = {}
    for path, bib in files:
        pending = path != REFERENCES
        for entry in bib.entries:
            dl.tick()
            key = entry.key
            if len(key) > MAX_KEY or KEY_PATTERN.match(key) is None:
                out.append(make_finding(rule, inp, "conv.bib.key-syntax", severity="error", path=path, line=entry.line, key=quote(key)))
            for name, severity in FORBIDDEN_FIELDS.items():
                if name in entry.fields:
                    out.append(
                        make_finding(
                            rule,
                            inp,
                            "conv.bib.forbidden-field",
                            severity=severity,
                            path=path,
                            line=entry.field_lines.get(name, entry.line),
                            key=quote(key),
                            field=name,
                        )
                    )
            if pending:
                if key in refs_keys:
                    out.append(
                        make_finding(rule, inp, "conv.bib.pending-consolidated", severity="warning", path=path, line=entry.line, key=quote(key))
                    )
                seen.setdefault(key, []).append((path, entry.canonical(), entry.line))
    for key, places in seen.items():
        if len({canon for _p, canon, _l in places}) < 2:
            continue
        for path, canon, line in places:
            other = next(p for p, cn, _l in places if cn != canon)
            out.append(
                make_finding(rule, inp, "conv.bib.pending-conflict", severity="warning", path=path, line=line, key=quote(key), other=other)
            )
    return out


# -- convention: configuración del árbol --------------------------------------------


def tree_sections(inp: RuleInput) -> tuple[str, ...] | None:
    """Secciones para la coherencia del árbol: ``roles.yaml`` del árbol si es válido; si no, la base (03 §2.5)."""
    from ...config.roles import parse_roles  # perezoso: carga yaml (REQ-ARCH-15)

    assert inp.tree is not None
    raw = inp.tree.read_bytes("roles.yaml")
    if raw is not None:
        roles, _findings = parse_roles(raw, t=inp.t)
        if roles is not None:
            return tuple(s.id for s in roles.sections)
    if inp.policy is not None:
        return tuple(s.id for s in inp.policy.roles.sections)
    return None


def tree_engine(inp: RuleInput) -> str:
    """``build.engine`` del ``project.yaml`` del árbol si es válido; si no, el de la base; si no, pdflatex."""
    from ...config.project import parse_project  # perezoso: carga yaml (REQ-ARCH-15)

    assert inp.tree is not None
    raw = inp.tree.read_bytes("project.yaml")
    if raw is not None:
        cfg, _findings = parse_project(raw, t=inp.t)
        if cfg is not None:
            return cfg.build.engine
    if inp.policy is not None:
        return inp.policy.project.build.engine
    return "pdflatex"


def _run_convention(inp: RuleInput, dl: tex.Deadline) -> list[Finding]:
    from .convention import FORMAT_NONE, tree_format

    if tree_format(inp) == FORMAT_NONE:
        # Sin manuscrito, conv.main.*, conv.latex.* y conv.bib.* quedan en skip (00-marco D-33, D-43);
        # REQ-CONV-26 no se verifica en Markdown (02 §2.3). Lo que sobra lo avisa conv.format.unused.
        return []
    c = corpus(inp, dl)
    out: list[Finding] = []
    engine = tree_engine(inp)
    sections = tree_sections(inp)
    outline_docs = [d for d in c.docs if d.kind == "outline"]
    unreadable = {o for p in c.unreadable if (o := outline_id(p)) is not None}
    main = next((d for d in c.docs if d.kind == "main"), None)
    if main is not None:
        # Un archivo de enlace ilegible existe igual: su \input en main.tex no apunta a un archivo que falta.
        outlines = sorted({d.section for d in outline_docs if d.section is not None} | unreadable)
        out.extend(_main_findings(inp, main, sections, outlines, dl))
    # Si uno no se puede leer, no se sabe qué partes incluye: no se avisa de las que faltan.
    out.extend(_outline_findings(inp, outline_docs, sections, dl, report_missing=not unreadable))
    for doc in c.docs:
        out.extend(_tex_findings(inp, doc, engine, dl))
    out.extend(_memory_findings(inp, dl))
    out.extend(_bib_findings(inp, c, dl))
    return out


# -- registro --------------------------------------------------------------------

CITATIONS = RuleSpec(id="citations", modes=TREE_MODES, run=_guard("citations", _run_citations), codes=CITATION_CODES)
LABELS_REFS = RuleSpec(id="labels-refs", modes=TREE_MODES, run=_guard("labels-refs", _run_labels), codes=LABEL_CODES)
LATEX_MACROS = RuleSpec(id="latex-macros", modes=TREE_MODES, run=_guard("latex-macros", _run_macros), codes=MACRO_CODES)
CONVENTION = RuleSpec(id="convention", modes=TREE_MODES, run=_guard("convention", _run_convention), codes=CONVENTION_CODES)
_BY_ID: dict[str, RuleSpec] = {r.id: r for r in (CITATIONS, LABELS_REFS, LATEX_MACROS, CONVENTION)}

RULES: tuple[RuleSpec, ...] = (CITATIONS, LABELS_REFS, LATEX_MACROS, CONVENTION)
