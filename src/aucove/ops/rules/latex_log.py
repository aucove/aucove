# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Regla ``latex-log`` (modo ``build-log``) y ``diagnostics.json`` (09-ci REQ-CI-18, REQ-CI-21).

Lee el log del motor que deja latexmk (``tex/build/<main>.log``) como datos, sin
ejecutar nada, y produce hallazgos ``E_VALIDATION_LATEX``: errores de compilación
(con ``-file-line-error`` y en la forma ``! ...``), citas y referencias indefinidas
y etiquetas repetidas son ``error`` (las mismas condiciones que ``-Werror``); los
demás avisos de LaTeX, de paquetes y de clases son ``warning``.

``diagnostics_result`` arma el sobre ``<NAME>.result/v1`` de ``diagnostics.json``: ``data.mode =
'build-log'``, un solo ``results[]`` con ``check = 'latex-log'``, como máximo 200
hallazgos (primero los errores) con mensajes de una línea de hasta 300 caracteres
y, si sobran, ``W_OUTPUT_TRUNCATED`` con ``details.omitted``.
"""

from __future__ import annotations

import dataclasses
import os
import posixpath
import re
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ...domain.findings import PATH_PATTERN, Finding, Severity
from ...domain.latex import Deadline, ScanTimeout
from ...i18n import Translator
from ..diagnostics import MAX_DIAGNOSTICS, clip_message
from ..result import Result, WarningObj, dumps, fail, make_error, make_warning, ok
from .base import RuleInput, RuleSpec, make_finding, status_of, unavailable
from .latex import LOCAL_DIRS, RULE_TIME_BUDGET_S, quote

RULE_ID = "latex-log"
CODE = "E_VALIDATION_LATEX"
LATEX_LOG_CODES: Mapping[str, frozenset[str]] = {CODE: frozenset({"detail"})}
MAX_LOG_BYTES = 5 * 2**20  # REQ-CI-24
MAX_PRINT_LINE = 79  # max_print_line de texmf.cnf: TeX corta las líneas del log ahí
MAX_LINE_CHARS = 4096  # tope de una línea lógica (tras unir las cortadas)
MAX_TEXT_QUOTE = 250
CONTEXT_LINES = 12  # líneas tras un error en las que se busca «l.<n>»
CONTEXT_TAIL = 60  # caracteres finales de la línea «l.<n>» que se agregan al mensaje
TEX_DIR = "tex"

EntryKind = Literal[
    "error",
    "fatal",
    "undefined-citation",
    "undefined-reference",
    "multiply-defined",
    "undefined-summary",
    "multiply-summary",
    "warning",
]


@dataclass(frozen=True, slots=True)
class LogEntry:
    """Un diagnóstico leído del log."""

    kind: EntryKind
    file: str | None  # tal como aparece en el log
    line: int | None
    text: str
    key: str | None = None

    @property
    def severity(self) -> Severity:
        return "warning" if self.kind == "warning" else "error"


# Expresiones aplicadas a líneas de como máximo MAX_LINE_CHARS, sin cuantificadores anidados.
_FLE = re.compile(r":(\d{1,9}): ")
_CONTEXT_LINE = re.compile(r"^l\.(\d{1,9})(?:\s|$)")
_CITATION = re.compile(
    r"^(?:LaTeX|Package natbib) Warning: Citation [`']([^`']{1,256})' on page [^ ]{1,20} undefined(?: on input line (\d{1,9}))?"
)
_REFERENCE = re.compile(
    r"^LaTeX Warning: Reference [`']([^`']{1,256})' on page [^ ]{1,20} undefined(?: on input line (\d{1,9}))?"
)
_MULTIPLY = re.compile(r"^LaTeX Warning: Label [`']([^`']{1,256})' multiply defined")
_UNDEFINED_SUMMARY = (
    "LaTeX Warning: There were undefined references",
    "LaTeX Warning: There were undefined citations",
    "Package natbib Warning: There were undefined citations",
)
_MULTIPLY_SUMMARY = (
    "LaTeX Warning: There were multiply-defined labels",
    "Package natbib Warning: There were multiply defined citations",
)
_WARNING = re.compile(r"^(?:LaTeX|Package ([^ ]{1,64})|Class ([^ ]{1,64})) Warning: (.*)$")
_INPUT_LINE = re.compile(r"on input line (\d{1,9})\.?$")
_MESSAGE_START = re.compile(r"^(?:! |LaTeX Warning: |Package [^ ]{1,64} Warning: |Class [^ ]{1,64} Warning: |l\.\d)")
_FILE_EXT = re.compile(r"\.[A-Za-z0-9]{1,8}$")
_FATAL = ("Emergency stop", "==> Fatal error occurred", " ==> Fatal error occurred")


# -- lectura del log ---------------------------------------------------------------


def read_log(path: Path, *, max_bytes: int = MAX_LOG_BYTES) -> tuple[bytes | None, bool, str | None]:
    """``(datos, recortado, problema)`` del log, sin seguir enlaces (REQ-CI-24).

    Si supera ``max_bytes`` se leen sus últimos ``max_bytes`` bytes (con
    ``-halt-on-error`` los errores están al final). ``problema`` es ``'no_log'`` si
    no existe y ``'log_unreadable'`` si es un enlace, no es un archivo regular o no
    se puede leer.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None, False, "no_log"
    except OSError:
        return None, False, "log_unreadable"
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        return None, False, "log_unreadable"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None, False, "log_unreadable"
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode) or (fst.st_ino, fst.st_dev) != (st.st_ino, st.st_dev):
            return None, False, "log_unreadable"
        truncated = fst.st_size > max_bytes
        if truncated:
            os.lseek(fd, fst.st_size - max_bytes, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = max_bytes
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        return None, False, "log_unreadable"
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if truncated:
        cut = data.find(b"\n")
        data = data[cut + 1 :] if cut >= 0 else b""
    return data, truncated, None


def logical_lines(data: bytes, *, deadline: Deadline | None = None) -> list[str]:
    """Líneas del log con las cortadas a ``MAX_PRINT_LINE`` bytes o caracteres ya unidas."""
    dl = deadline or Deadline(None)
    out: list[str] = []
    buf: str | None = None
    for raw in data.split(b"\n"):
        dl.tick()
        raw = raw.rstrip(b"\r")
        text = raw.decode("utf-8", errors="replace")
        if buf is not None and not _MESSAGE_START.match(text) and len(buf) < MAX_LINE_CHARS:
            buf = (buf + text)[:MAX_LINE_CHARS]
        else:
            if buf is not None:
                out.append(buf)
            buf = text[:MAX_LINE_CHARS]
        if len(raw) != MAX_PRINT_LINE and len(text) != MAX_PRINT_LINE:
            out.append(buf)
            buf = None
    if buf is not None:
        out.append(buf)
    return out


def _looks_like_file(token: str) -> bool:
    if not token:
        return False
    if token.startswith(("./", "../", "/")) or re.match(r"^[A-Za-z]:[/\\]", token[:3]):
        return True
    return _FILE_EXT.search(token[-9:]) is not None and not token.endswith(".")


class _FileStack:
    """Pila de archivos abiertos, deducida de los paréntesis del log (heurística habitual)."""

    def __init__(self) -> None:
        self.stack: list[str | None] = []

    def current(self) -> str | None:
        for item in reversed(self.stack):
            if item is not None:
                return item
        return None

    def feed(self, line: str) -> None:
        i = 0
        n = len(line)
        while i < n:
            c = line[i]
            if c == "(":
                j = i + 1
                while j < n and line[j] not in " \t()[]{}<>\"":
                    j += 1
                token = line[i + 1 : j]
                self.stack.append(token if _looks_like_file(token) else None)
                i = j
                continue
            if c == ")" and self.stack:
                self.stack.pop()
            i += 1


def _context_line_number(lines: Sequence[str], start: int) -> tuple[int | None, int]:
    """Número de ``l.<n>`` en las líneas siguientes a un error y el índice de esa línea.

    La búsqueda se detiene en el siguiente mensaje de error, para no tomar su contexto.
    """
    for k in range(start, min(len(lines), start + CONTEXT_LINES)):
        line = lines[k]
        m = _CONTEXT_LINE.match(line)
        if m is not None:
            return int(m.group(1)), k
        if line.startswith("!") or _file_line_error(line) is not None:
            break
    return None, start - 1


def _with_context(text: str, lines: Sequence[str], k: int) -> str:
    """``text`` con el final de la línea de contexto ``l.<n> ...`` (donde TeX se detuvo)."""
    if k < 0 or k >= len(lines):
        return text
    m = _CONTEXT_LINE.match(lines[k])
    if m is None:
        return text
    tail = lines[k][m.end() :].strip()
    if not tail:
        return text
    if len(tail) > CONTEXT_TAIL:
        tail = "…" + tail[-(CONTEXT_TAIL - 1) :]
    return f"{text} {tail}"


def _file_line_error(line: str) -> tuple[str, int, str] | None:
    m = _FLE.search(line)
    if m is None:
        return None
    prefix = line[: m.start()]
    if not prefix or prefix[0] in " \t!(" or ": " in prefix or _FILE_EXT.search(prefix[-9:]) is None:
        return None
    return prefix, int(m.group(1)), line[m.end() :].strip()


def _warning_text(lines: Sequence[str], i: int, first: str, package: str | None) -> tuple[str, int]:
    """Texto de un aviso con sus líneas de continuación; devuelve también el último índice usado."""
    parts = [first]
    k = i + 1
    prefix = f"({package})" if package else None
    while k < len(lines) and len(" ".join(parts)) < MAX_LINE_CHARS:
        nxt = lines[k]
        if prefix is not None and nxt.startswith(prefix):
            parts.append(nxt[len(prefix) :].strip())
        elif package is None and nxt.startswith(" " * 15) and nxt.strip():
            parts.append(nxt.strip())
        else:
            break
        k += 1
    return " ".join(p for p in parts if p), k - 1


def parse_log(data: bytes, *, deadline: Deadline | None = None) -> list[LogEntry]:
    """Diagnósticos del log de TeX, en orden de aparición y sin repetidos."""
    dl = deadline or Deadline(None)
    lines = logical_lines(data, deadline=dl)
    files = _FileStack()
    entries: list[LogEntry] = []
    skip_until = -1  # índice hasta el que no se leen paréntesis (contexto de un error)
    i = 0
    while i < len(lines):
        dl.tick()
        line = lines[i]
        fle = _file_line_error(line)
        if fle is not None:
            name, number, text = fle
            # «<archivo>:<n>:  ==> Fatal error occurred...» cierra el log tras -halt-on-error.
            fle_kind: EntryKind = "fatal" if text.startswith(_FATAL) else "error"
            _n, last = _context_line_number(lines, i + 1)
            entries.append(LogEntry(fle_kind, name, number, _with_context(text, lines, last)))
            skip_until = max(skip_until, last + 1)
            i += 1
            continue
        if line.startswith("!"):
            text = line[1:].strip()
            ctx_line, last = _context_line_number(lines, i + 1)
            kind: EntryKind = "fatal" if text.startswith(_FATAL) else "error"
            entries.append(LogEntry(kind, files.current(), ctx_line, _with_context(text, lines, last)))
            skip_until = max(skip_until, last + 1)
            i += 1
            continue
        m = _CITATION.match(line)
        if m is not None:
            entries.append(LogEntry("undefined-citation", files.current(), _int(m.group(2)), line, key=m.group(1)))
        elif (m := _REFERENCE.match(line)) is not None:
            entries.append(LogEntry("undefined-reference", files.current(), _int(m.group(2)), line, key=m.group(1)))
        elif (m := _MULTIPLY.match(line)) is not None:
            entries.append(LogEntry("multiply-defined", None, None, line, key=m.group(1)))
        elif line.startswith(_UNDEFINED_SUMMARY):
            entries.append(LogEntry("undefined-summary", None, None, line))
        elif line.startswith(_MULTIPLY_SUMMARY):
            entries.append(LogEntry("multiply-summary", None, None, line))
        elif (m := _WARNING.match(line)) is not None:
            package = m.group(1) or m.group(2)
            text, last = _warning_text(lines, i, m.group(3).strip(), package)
            where = _INPUT_LINE.search(text[-40:])
            entries.append(LogEntry("warning", files.current(), _int(where.group(1)) if where else None, text))
            i = last + 1
            continue
        if i > skip_until:
            files.feed(line)
        i += 1
    return _dedupe(entries)


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    number = int(value)
    return number if number >= 1 else None


def _dedupe(entries: Sequence[LogEntry]) -> list[LogEntry]:
    kinds = {e.kind for e in entries}
    drop: set[str] = set()
    if "error" in kinds:
        drop.add("fatal")
    if kinds & {"undefined-citation", "undefined-reference"}:
        drop.add("undefined-summary")
    if "multiply-defined" in kinds:
        drop.add("multiply-summary")
    seen: set[tuple[object, ...]] = set()
    out: list[LogEntry] = []
    for e in entries:
        if e.kind in drop:
            continue
        ident = (e.kind, e.file, e.line, e.key if e.key is not None else e.text)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(e)
    return out


# -- hallazgos ---------------------------------------------------------------------


def repo_path(name: str | None, tex_dir: str | None = None) -> str | None:
    """Ruta del repositorio de un archivo del log (relativo a ``tex/``); ``None`` si no es del repo."""
    if not name:
        return None
    name = name.strip().strip('"')
    if not name or "\\" in name:
        return None
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name[:2]):
        if tex_dir is None:
            return None
        base = tex_dir.rstrip("/") + "/"
        if not name.startswith(base):
            return None
        name = name[len(base) :]
    rel = posixpath.normpath(f"{TEX_DIR}/{name}")
    if rel == ".." or rel.startswith("../") or rel.startswith(LOCAL_DIRS) or not PATH_PATTERN.match(rel):
        return None
    return rel


def _tex_dir_of(log_path: Path) -> str | None:
    try:
        resolved = Path(os.path.abspath(log_path))
    except (OSError, ValueError):
        return None
    if resolved.parent.name != "build":
        return None
    return resolved.parent.parent.as_posix()


def _detail(t: Translator, e: LogEntry) -> str:
    key = quote(e.key) if e.key is not None else ""
    text = quote(e.text, MAX_TEXT_QUOTE)
    return {
        "error": lambda: t("latex.log.error", text=text),
        "fatal": lambda: t("latex.log.fatal"),
        "undefined-citation": lambda: t("latex.log.undefined_citation", key=key),
        "undefined-reference": lambda: t("latex.log.undefined_reference", key=key),
        "multiply-defined": lambda: t("latex.log.multiply_defined", key=key),
        "undefined-summary": lambda: t("latex.log.undefined_summary"),
        "multiply-summary": lambda: t("latex.log.multiply_summary"),
        "warning": lambda: t("latex.log.warning", text=text),
    }[e.kind]()


def _finding(inp: RuleInput, *, severity: Severity, path: str | None, line: int | None, detail: str) -> Finding:
    f = make_finding(LATEX_LOG, inp, CODE, severity=severity, path=path, line=line, detail=detail)
    return dataclasses.replace(f, message=clip_message(f.message))


def order_findings(findings: Sequence[Finding]) -> list[Finding]:
    """Primero los errores y después los avisos, conservando el orden del log."""
    return sorted(findings, key=lambda f: f.severity != "error")


def _run(inp: RuleInput) -> list[Finding]:
    t = inp.t
    if inp.log_path is None:
        return [unavailable(LATEX_LOG, inp, t("latex.reason.no_log"))]
    data, truncated, problem = read_log(inp.log_path)
    if data is None:
        return [unavailable(LATEX_LOG, inp, t(f"latex.reason.{problem or 'log_unreadable'}"))]
    dl = Deadline(RULE_TIME_BUDGET_S)
    try:
        entries = parse_log(data, deadline=dl)
    except ScanTimeout:
        return [unavailable(LATEX_LOG, inp, t("latex.reason.timeout"))]
    tex_dir = _tex_dir_of(inp.log_path)
    findings = [
        _finding(inp, severity=e.severity, path=repo_path(e.file, tex_dir), line=e.line, detail=_detail(t, e))
        for e in entries
    ]
    if truncated:
        findings.append(_finding(inp, severity="warning", path=None, line=None, detail=t("latex.log.truncated")))
    return order_findings(findings)


LATEX_LOG = RuleSpec(id=RULE_ID, modes=frozenset({"build-log"}), run=_run, codes=LATEX_LOG_CODES)
RULES: tuple[RuleSpec, ...] = (LATEX_LOG,)


# -- diagnostics.json ---------------------------------------------------------


def _git_point(value: Mapping[str, object] | None) -> dict[str, object]:
    if value is None:
        return {"ref": "HEAD", "sha": None}
    return {"ref": value.get("ref") or "HEAD", "sha": value.get("sha")}


def diagnostics_result(
    findings: Sequence[Finding],
    t: Translator,
    *,
    base: Mapping[str, object] | None = None,
    head: Mapping[str, object] | None = None,
    pr: int | None = None,
    warnings: Sequence[WarningObj] = (),
) -> Result:
    """Sobre de ``validate --mode build-log``: 200 hallazgos como máximo y ``W_OUTPUT_TRUNCATED``."""
    ordered = order_findings(findings)
    kept = ordered[:MAX_DIAGNOSTICS]
    omitted = len(ordered) - len(kept)
    items: list[dict[str, object]] = []
    for f in kept:
        item = f.to_dict()
        item["message"] = clip_message(f.message)
        item["pointer"] = None
        items.append(item)
    status = status_of(findings)
    data: dict[str, object] = {
        "valid": status == "pass",
        "mode": "build-log",
        "pr": pr,
        "base": _git_point(base),
        "head": _git_point(head),
        "results": [{"check": RULE_ID, "status": status, "findings": items}],
        "page": {"next_cursor": None, "returned": len(items), "total": len(ordered)},
    }
    out_warnings = list(warnings)
    if omitted:
        out_warnings.append(make_warning("W_OUTPUT_TRUNCATED", t, details={"omitted": omitted}))
    if status == "fail":
        return fail("validate", make_error("E_VALIDATION_FAILED", t), data=data, warnings=out_warnings)
    return ok("validate", data, out_warnings)


def write_diagnostics(path: Path, result: Result) -> None:
    """Escribe el sobre en ``path`` de forma atómica (UTF-8 sin BOM, una línea y ``\\n``)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".diagnostics-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(dumps(result))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
