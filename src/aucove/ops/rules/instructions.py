# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Regla ``instructions-lint``: IL01 a IL12 (10-seguridad §3.6 y §3.7; 09-ci §4.4; REQ-SEC-15 a 17).

Corre en los modos ``tree``, ``pr`` y ``base``, con la misma implementación en local y en el CI
(REQ-SEC-17), sobre los archivos que cambian (``inp.changes``, sin los borrados). Solo informa las
líneas nuevas: una línea que ya estaba, idéntica, en la versión anterior del archivo
(``base_tree``) ya pasó por revisión. Cada hallazgo cita la línea cruda con los caracteres
invisibles escritos como ``<U+XXXX>`` (REQ-CI-28).

- IL01 a IL04: el escáner de caracteres de §3.6.2, en todo archivo de texto. IL01 es error en los
  archivos de reglas y de memoria; IL02 e IL03, siempre.
- IL05: comentarios HTML en los ``.md`` de reglas, de memoria y de las partes, salvo las plantillas
  de Issue y de PR y los marcadores del núcleo (``<!-- <NAME>:``, 00-marco §5.14), que el núcleo
  escribe en las notas de sesión.
- IL06 e IL08: las frases de §3.6.1, sin distinguir mayúsculas y sobre el texto en NFKC: en los
  comentarios de los ``.tex`` (``%``, ``\\iffalse`` … ``\\fi`` y el entorno ``comment``) y en la
  memoria, los ``.md`` de las partes y los campos ``abstract``, ``note`` y ``annote`` de los ``.bib``.
- IL07: texto oculto en los ``.tex``. IL09: tramos de 200 caracteres o más del alfabeto base64,
  salvo en ``bib/**`` y en las figuras. IL10 (solo en ``pr``): el PR toca a la vez archivos de reglas
  y archivos de clase ``section``. IL11: instrucciones sobre la atribución en reglas y memoria.
  IL12: el token OAuth de Claude Code en ``.github/workflows/**`` (error).

Los archivos de reglas son los de §3.7 (``is_rules_file``); un hallazgo por regla y línea.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Iterator, Sequence
from typing import Final

from ... import _meta
from ...domain.findings import Finding, Severity
from ...domain.paths import INSTR_DIRS, INSTR_FILES, classify
from .base import Change, RuleInput, RuleSpec, make_finding

__all__ = ["CODES", "PHRASES", "RANGES", "RULE", "RULES", "is_rules_file", "raw_line", "scan_chars"]

RULE_ID: Final[str] = "instructions-lint"
LINE_MAX: Final[int] = 160  # caracteres de la línea citada
# §3.6.2: rangos del escáner de caracteres.
RANGES: Final[dict[str, tuple[tuple[int, int], ...]]] = {
    "IL01": ((0x00AD, 0x00AD), (0x034F, 0x034F), (0x180E, 0x180E), (0x200B, 0x200F), (0x2060, 0x2064),
             (0xFEFF, 0xFEFF)),
    "IL02": ((0x061C, 0x061C), (0x202A, 0x202E), (0x2066, 0x2069)),
    "IL03": ((0xE0000, 0xE007F),),
    "IL04": ((0xE0100, 0xE01EF),),
}
# §3.6.1, en es y en.
PHRASES: Final[re.Pattern[str]] = re.compile("|".join((
    r"ignore (all |any )?(previous|prior|above) (instructions|prompts)",
    r"ignora (todas )?las instrucciones",
    r"you are (now )?(an?|the) ",
    r"(ahora )?eres (un|una|el|la) ",
    r"system prompt",
    r"instrucciones para (el|la) (agente|ia|modelo|asistente)",
    r"instructions for the (agent|ai|model|assistant)",
    r"as an ai",
    r"como (una )?ia",
    r"(assistant|asistente)\s*:",
    r"do not (tell|mention|reveal)",
    r"no (le )?(digas|menciones|reveles)",
    r"(approve|aprueba) (this|este) (pr|pull request)",
)), re.IGNORECASE)
_HIDDEN: Final[re.Pattern[str]] = re.compile(
    r"\\textcolor\{white\}|\\color\{white\}|\\color\[gray\]\{1\}|\\phantom\{|\\scalebox\{0|\\resizebox\{0")
_FONTSIZE: Final[re.Pattern[str]] = re.compile(r"\\fontsize\{\s*([0-9]*\.?[0-9]+)\s*(pt)?\s*\}")
_BASE64: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9+/=]{200,}")
_ATTRIBUTION: Final[re.Pattern[str]] = re.compile(
    r"co-authored-by|\battribution\b|\bno (agregues|incluyas)\b.*\btrailer|\bdo not add\b.*\bco-?author",
    re.IGNORECASE)
_OAUTH: Final[re.Pattern[str]] = re.compile(r"CLAUDE_CODE_OAUTH_TOKEN|claude_code_oauth_token")
_HTML_COMMENT: Final[str] = "<!--"
_CORE_MARKER: Final[str] = f"<!-- {_meta.NAME}:"  # 00-marco §5.14
_TEX_COMMENT: Final[re.Pattern[str]] = re.compile(r"(?<!\\)%(.*)$")
_BIB_FIELD: Final[re.Pattern[str]] = re.compile(r"\b(abstract|note|annote)\s*=\s*", re.IGNORECASE)
_IL05_EXCLUDED: Final[tuple[str, ...]] = (".github/issue_template/", ".github/pull_request_template.md")
_FIGURE_EXT: Final[tuple[str, ...]] = (".svg", ".eps", ".ps", ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".tif",
                                       ".tiff", ".webp")
_RULES_EXACT: Final[frozenset[str]] = frozenset({
    "agents.md", "claude.md", "memory/constitution.md", "plan.md", "roles.yaml", "project.yaml", ".mcp.json",
    ".latexmkrc", "tex/preamble.tex",
})
_RULES_PREFIX: Final[tuple[str, ...]] = (".claude/", ".agents/skills/", ".github/", ".devcontainer/")
_LOCAL: Final[frozenset[str]] = frozenset({".claude/settings.local.json", "claude.local.md"})

_CHAR: Final[frozenset[str]] = frozenset({"codepoint", "name", "text"})
_TEXT: Final[frozenset[str]] = frozenset({"text"})
# Código de hallazgo -> marcadores de finding.<code>.* (validate.toml).
CODES: Final[dict[str, frozenset[str]]] = {
    "IL01": _CHAR, "IL02": _CHAR, "IL03": _CHAR, "IL04": _CHAR, "IL05": _TEXT, "IL06": _TEXT, "IL07": _TEXT,
    "IL08": _TEXT, "IL09": _TEXT, "IL10": frozenset({"rules", "section"}), "IL11": _TEXT, "IL12": _TEXT,
}


def is_rules_file(rel: str) -> bool:
    """Archivo de reglas de 10 §3.7, sin las rutas de clase ``local``; sin distinguir mayúsculas."""
    low = rel.lower()
    if low in _LOCAL or low.startswith(".claude/worktrees/"):
        return False
    if low in _RULES_EXACT or low.startswith(_RULES_PREFIX):
        return True
    segments = low.split("/")
    return segments[-1] in INSTR_FILES or any(seg in INSTR_DIRS for seg in segments[:-1])


def _flagged(cp: int) -> str | None:
    return next((rule for rule, spans in RANGES.items() if any(a <= cp <= b for a, b in spans)), None)


def raw_line(line: str, limit: int = LINE_MAX) -> str:
    """La línea cruda en una sola línea, con lo invisible como ``<U+XXXX>`` (REQ-CI-28)."""
    out = []
    for ch in line:
        cp = ord(ch)
        if ch == "\t":
            out.append(" ")
        elif (_flagged(cp) or 0xFE00 <= cp <= 0xFE0F
              or unicodedata.category(ch) in ("Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp")):
            out.append(f"<U+{cp:04X}>")
        else:
            out.append(ch)
    text = "".join(out)
    return text if len(text) <= limit else text[:limit] + "…"


def scan_chars(lines: Sequence[str]) -> Iterator[tuple[str, int, int]]:
    """§3.6.2: ``(regla, línea, punto de código)`` del primer carácter de cada regla en cada línea."""
    for lineno, line in enumerate(lines, 1):
        seen: set[str] = set()
        run_fe = 0
        for col, ch in enumerate(line):
            cp = ord(ch)
            if cp == 0xFEFF and lineno == 1 and col == 0:
                continue  # BOM al inicio
            run_fe = run_fe + 1 if 0xFE00 <= cp <= 0xFE0F else 0
            rule = _flagged(cp) or ("IL04" if run_fe == 2 else None)
            if rule is not None and rule not in seen:
                seen.add(rule)
                yield rule, lineno, cp


def _tex_comments(lines: Sequence[str]) -> Iterator[tuple[int, str]]:
    """El texto de los comentarios de un ``.tex``: tras ``%``, en ``\\iffalse`` … ``\\fi`` y en ``comment``."""
    inside: str | None = None  # el cierre que se espera
    for lineno, line in enumerate(lines, 1):
        if inside is not None:
            yield lineno, line
            if inside in line:
                inside = None
            continue
        for opening, closing in ((r"\begin{comment}", r"\end{comment}"), (r"\iffalse", r"\fi")):
            if opening in line:
                rest = line.split(opening, 1)[1]
                yield lineno, rest
                inside = None if closing in rest else closing
                break
        else:
            m = _TEX_COMMENT.search(line)
            if m:
                yield lineno, m.group(1)


def _bib_fields(lines: Sequence[str]) -> Iterator[tuple[int, str]]:
    """El texto de los campos ``abstract``, ``note`` y ``annote`` de un ``.bib``, línea por línea."""
    depth = 0
    for lineno, line in enumerate(lines, 1):
        if depth > 0:
            yield lineno, line
            depth = max(0, depth + line.count("{") - line.count("}"))
            continue
        m = _BIB_FIELD.search(line)
        if m:
            rest = line[m.end():]
            yield lineno, rest
            if rest.startswith("{"):
                depth = max(0, rest.count("{") - rest.count("}"))


def _klass(rel: str) -> str:
    if is_rules_file(rel):
        return "rules"
    return "memory" if rel.startswith("memory/") else "parts" if rel.startswith("sections/") else "other"


def _file_findings(path: str, lines: Sequence[str]) -> Iterator[tuple[str, int, Severity, dict[str, object]]]:
    """``(código, línea, severidad, marcadores sin text)`` de un archivo de texto."""
    low = path.lower()
    klass = _klass(path)
    for rule, lineno, cp in scan_chars(lines):
        strict = rule in ("IL02", "IL03") or (rule == "IL01" and klass in ("rules", "memory"))
        yield rule, lineno, "error" if strict else "warning", {
            "codepoint": f"U+{cp:04X}", "name": unicodedata.name(chr(cp), "?")}
    if low.endswith(".md") and klass in ("rules", "memory", "parts") and not low.startswith(_IL05_EXCLUDED):
        for lineno, line in enumerate(lines, 1):
            at = line.find(_HTML_COMMENT)
            while at >= 0:
                if not line.startswith(_CORE_MARKER, at):
                    yield "IL05", lineno, "warning", {}
                    break
                at = line.find(_HTML_COMMENT, at + 1)
    if low.endswith(".tex"):
        for lineno, text in _tex_comments(lines):
            if PHRASES.search(unicodedata.normalize("NFKC", text)):
                yield "IL06", lineno, "warning", {}
        for lineno, line in enumerate(lines, 1):
            size = _FONTSIZE.search(line)
            if _HIDDEN.search(line) or (size is not None and float(size.group(1)) < 2):
                yield "IL07", lineno, "warning", {}
    phrase_lines: Iterable[tuple[int, str]] = ()
    if klass == "memory" or (klass == "parts" and low.endswith(".md")):
        phrase_lines = enumerate(lines, 1)
    elif low.endswith(".bib"):
        phrase_lines = _bib_fields(lines)
    for lineno, text in phrase_lines:
        if PHRASES.search(unicodedata.normalize("NFKC", text)):
            yield "IL08", lineno, "warning", {}
    if not low.startswith("bib/") and "/figures/" not in f"/{low}" and not low.endswith(_FIGURE_EXT):
        for lineno, line in enumerate(lines, 1):
            if _BASE64.search(line):
                yield "IL09", lineno, "warning", {}
    if klass in ("rules", "memory"):
        for lineno, line in enumerate(lines, 1):
            if _ATTRIBUTION.search(unicodedata.normalize("NFKC", line)):
                yield "IL11", lineno, "warning", {}
    if low.startswith(".github/workflows/"):
        for lineno, line in enumerate(lines, 1):
            if _OAUTH.search(line):
                yield "IL12", lineno, "error", {}


def _old_lines(inp: RuleInput, change: Change) -> frozenset[str]:
    """Las líneas de la versión anterior del archivo, para informar solo las nuevas."""
    if inp.base_tree is None or change.status == "added":
        return frozenset()
    text = inp.base_tree.read_text(change.previous_path or change.path)
    return frozenset(text.replace("\r\n", "\n").split("\n")) if text is not None else frozenset()


def _run(inp: RuleInput) -> Sequence[Finding]:
    out: list[Finding] = []
    tree = inp.tree
    if tree is not None:
        for change in inp.changes:
            if change.status == "removed":
                continue
            text = tree.read_text(change.path)
            if text is None:  # binario, ilegible o sin contenido: no es un archivo de texto
                continue
            lines = text.replace("\r\n", "\n").split("\n")
            old = _old_lines(inp, change)
            seen: set[tuple[str, int]] = set()
            for code, lineno, severity, params in _file_findings(change.path, lines):
                line = lines[lineno - 1]
                if (code, lineno) in seen or line in old:
                    continue
                seen.add((code, lineno))
                out.append(make_finding(RULE, inp, code, severity=severity, path=change.path, line=lineno,
                                        text=raw_line(line), **params))
    if inp.mode == "pr" and inp.policy is not None:  # IL10: el diff del PR
        declared = frozenset(s.id for s in inp.policy.roles.sections)
        rules = sorted(c.path for c in inp.changes if is_rules_file(c.path))
        parts = sorted(c.path for c in inp.changes if classify(c.path, declared, symlinked=False).cls == "section")
        if rules and parts:
            out.append(make_finding(RULE, inp, "IL10", severity="warning", path=None, rules=rules[0], section=parts[0]))
    return sorted(out, key=lambda f: (f.path or "", f.line or 0, f.code))


RULE = RuleSpec(id=RULE_ID, modes=frozenset({"tree", "pr", "base"}), run=_run, codes=CODES)

RULES: tuple[RuleSpec, ...] = (RULE,)
