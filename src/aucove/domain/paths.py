# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Forma canónica y clasificación de rutas (02-convencion §3.1 y §3.5, D-32 d).

Módulo puro (REQ-ARCH-18): no toca el disco. La grafía real de REQ-CONV-9 llega como un
mapa (``apply_case_map``) y la detección de enlaces de REQ-CONV-10 como el argumento
``symlinked`` de ``classify``.

Además del algoritmo de 02 §3.1, ``canonical`` rechaza (``InvalidPath``) los nombres que en
Windows no designan el archivo que dicen: Win32 quita el punto o el espacio final de cada
segmento, ``x:flujo`` abre un flujo de ``x``, NTFS resuelve los nombres cortos 8.3
(``GIT~1`` es ``.git``) y CON, NUL, COM1... son dispositivos. Con ellos, ``.git./hooks/x`` o
``sections/m/CLAUDE~1/x`` escribirían en ``.git`` o en ``.claude`` aunque la clasificación
léxica dijera otra cosa. También rechaza los controles C0 y C1, los formatos (bidi, ancho cero;
HFS+ ignora varios al comparar nombres) y los separadores de línea (REQ-CONV-7). El rechazo es
igual en toda plataforma para que session, commit y CI den la misma respuesta y para que el
repositorio se pueda extraer en Windows (git para Windows rechaza esos nombres).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Literal

__all__ = [
    "GEN_EXACT",
    "INSTR_DIRS",
    "INSTR_FILES",
    "LOCAL_EXACT",
    "LOCAL_PREF",
    "SECTION_ID",
    "TASK_NOTE",
    "InvalidPath",
    "OutsideRepo",
    "PathClass",
    "PathInfo",
    "EDITOR_EXACT",
    "apply_case_map",
    "canonical",
    "classify",
    "is_editor_path",
    "is_git_path",
    "is_win32_alias",
]


class InvalidPath(ValueError):
    """Ruta vacía, con caracteres de control, que empieza por '~', que no nombra nada o con
    un segmento que Windows no lleva a un archivo del repositorio (alias, flujo o dispositivo).
    """


class OutsideRepo(ValueError):
    """Ruta que queda fuera de la raíz del repositorio."""


PathClass = Literal["lead", "section", "editor", "append", "task", "generated", "local"]

# La carpeta de estado local es '.' + NAME. Se deriva del nombre del paquete para no
# escribir el literal (REQ-ARCH-19) ni depender de otro módulo: el paquete se llama NAME.
_CACHE_DIR: Final[str] = "." + __name__.split(".", 1)[0]

SECTION_ID: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
TASK_NOTE: Final[re.Pattern[str]] = re.compile(r"memory/notes/task-([1-9][0-9]*)\.md")
# INSTR_FILES, INSTR_DIRS y GEN_EXACT van en minúsculas y se comparan en minúsculas (REQ-CONV-9)
INSTR_FILES: Final[frozenset[str]] = frozenset(
    {
        "agents.md",
        "agents.override.md",
        "claude.md",
        "claude.local.md",
        "gemini.md",
        ".goosehints",
        ".cursorrules",
        ".windsurfrules",
        ".clinerules",
        ".mcp.json",
    }
)
INSTR_DIRS: Final[frozenset[str]] = frozenset(
    {".claude", ".agents", ".codex", ".gemini", ".cursor", ".opencode"}
)
GEN_EXACT: Final[frozenset[str]] = frozenset(
    {".github/codeowners", ".github/issue_template/task.yml"}
)
LOCAL_EXACT: Final[frozenset[str]] = frozenset({".claude/settings.local.json", "CLAUDE.local.md"})
# R6a (02 §3.3; 00-marco D-56): los archivos del editor del artículo, además de tex/outline/*.tex
EDITOR_EXACT: Final[frozenset[str]] = frozenset(
    {"tex/main.tex", "tex/preamble.tex", "tex/notation.tex", "tex/guide.md", "bib/references.bib"}
)
LOCAL_PREF: Final[tuple[str, ...]] = (
    _CACHE_DIR + "/",
    ".claude/worktrees/",
    "tex/build/",
    "tex/build-diff/",
)

# R1 se compara sin distinguir mayúsculas, como R2 y R3: en Windows y macOS '.Aucove/x' es el
# mismo archivo que la ruta local, y las rutas local no están en el mapa de grafías (salen de
# git ls-files y no se versionan, REQ-CONV-14). Solo puede volver más restrictiva la clase.
_LOCAL_EXACT_LOW: Final[frozenset[str]] = frozenset(x.lower() for x in LOCAL_EXACT)
_LOCAL_PREF_LOW: Final[tuple[str, ...]] = tuple(x.lower() for x in LOCAL_PREF)

_DRIVE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z]:/")

# Categorías Unicode que se rechazan (REQ-CONV-7): controles C0, DEL y C1 (Cc), formatos como
# los bidi U+202A-202E y U+2066-2069 o los de ancho cero (Cf), suplentes sueltos (Cs) y
# separadores de línea y de párrafo (Zl, Zp).
_BAD_CATEGORIES: Final[frozenset[str]] = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
# Caracteres que Win32 no admite en un nombre: ':' además abre flujos alternativos
# ('x::$DATA', '.git::$INDEX_ALLOCATION') o rutas relativas a una unidad ('C:x').
_WIN32_BAD_CHARS: Final[frozenset[str]] = frozenset('<>:"|?*')
# Nombres de dispositivo de Windows; cuentan con extensión o espacios detrás ('aux.tex').
_WIN32_DEVICES: Final[frozenset[str]] = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"{dev}{n}" for dev in ("com", "lpt") for n in "0123456789\u00b9\u00b2\u00b3"}
)
# Nombre corto 8.3 de NTFS: base de hasta 8 caracteres que termina en '~' y dígitos y
# extensión opcional de hasta 3 ('GIT~1' es '.git', 'CLAUDE~1' es '.claude', 'ROLES~1.YAM'
# es 'roles.yaml').
_SHORT_NAME: Final[re.Pattern[str]] = re.compile(
    r"(?=[^.]{1,8}(?:\.|\Z))[^.]*~[0-9]+(?:\.[^.]{1,3})?"
)


@dataclass(frozen=True, slots=True)
class PathInfo:
    path: str
    cls: PathClass
    rule: str
    section: str | None = None
    task: int | None = None


def _same(x: str) -> str:
    return x


def _lower(x: str) -> str:
    # lower, no casefold: NTFS no iguala ß y SS
    return x.lower()


def is_win32_alias(seg: str) -> bool:
    """``True`` si Windows no lleva ``seg`` a un archivo con ese nombre exacto.

    Punto o espacio final (Win32 los quita: '.git.' es '.git'), carácter inválido o ':'
    (flujo alternativo), nombre de dispositivo ('aux.tex', 'NUL') o nombre corto 8.3
    ('GIT~1'). Así lo rechaza también git para Windows (verify_path, is_valid_win32_path).
    """
    if seg[-1:] in (".", " ") or any(c in _WIN32_BAD_CHARS for c in seg):
        return True
    if seg.split(".", 1)[0].rstrip(" ").lower() in _WIN32_DEVICES:
        return True
    return _SHORT_NAME.fullmatch(seg) is not None


def canonical(raw: str, root: str, ci_fs: bool) -> str:
    """Lleva ``raw`` a la ruta canónica relativa a ``root`` (02 §3.1).

    Los segmentos relativos (los de la raíz se comparan con ``root``) no pueden ser alias de
    Windows (``is_win32_alias``), en ninguna plataforma.
    """
    if not raw or any(unicodedata.category(c) in _BAD_CATEGORIES for c in raw):
        raise InvalidPath(raw)
    p = unicodedata.normalize("NFC", raw.replace("\\", "/"))
    r = unicodedata.normalize("NFC", root.replace("\\", "/")).rstrip("/")
    if p.startswith("/") or _DRIVE.match(p):  # absoluta (POSIX o Windows)
        ps = p.split("/")
        rs = r.split("/")
        k: Callable[[str], str] = _lower if ci_fs else _same
        if len(ps) < len(rs) or any(k(ps[i]) != k(rs[i]) for i in range(len(rs))):
            raise OutsideRepo(raw)
        p = "/".join(ps[len(rs) :])
    out: list[str] = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not out:
                raise OutsideRepo(raw)
            out.pop()
            continue
        if (seg == "~" and not out) or is_win32_alias(seg):
            raise InvalidPath(raw)
        out.append(seg)
    if not out:
        raise InvalidPath(raw)
    return "/".join(out)


def apply_case_map(rel: str, case_map: Mapping[str, str] | None) -> str:
    """Sustituye la grafía de los segmentos existentes por la real (REQ-CONV-9).

    ``case_map`` va de la ruta en minúsculas a su grafía real (sale de ``git ls-files``).
    Se usa el prefijo existente más largo: la ruta entera si está en el mapa y, si no, la
    carpeta más profunda que sea una clave o el prefijo de alguna clave. Los segmentos que no
    existen conservan la grafía recibida. Una entrada cuyo valor no difiere de su clave solo en
    mayúsculas se ignora, para que el mapa no pueda cambiar la ruta más allá de la grafía.
    """
    if not case_map:
        return rel
    low = rel.lower()
    exact = case_map.get(low)
    if exact is not None and exact.lower() == low:
        return exact
    segs = rel.split("/")
    lows = low.split("/")
    limit = len(lows) - 1  # solo prefijos propios: la ruta entera ya se buscó arriba
    best = 0
    best_real: list[str] = []
    for key, real in case_map.items():
        ks = key.lower().split("/")
        rs = real.split("/")
        if len(rs) != len(ks):
            continue
        top = min(len(ks), limit)
        n = 0
        while n < top and ks[n] == lows[n]:
            n += 1
        if n > best and all(rs[i].lower() == ks[i] for i in range(n)):
            best, best_real = n, rs[:n]
            if best == limit:
                break
    if not best:
        return rel
    return "/".join(best_real + segs[best:])


def _is_dotgit(seg: str) -> bool:
    """Segmento que git trata como '.git' (is_ntfs_dotgit): sin mayúsculas, sin puntos ni
    espacios finales, sin flujo ':...' y con el nombre corto 'git~1'."""
    base = seg.lower().split(":", 1)[0].rstrip(". ")
    return base in (".git", "git~1")


def is_git_path(rel: str) -> bool:
    """``True`` si algún segmento de ``rel`` es '.git' o un alias suyo (R0 y paso 1 de 06 §2.4).

    Como verify_path de git, mira todos los segmentos: un '.git' dentro de una carpeta del
    repositorio crea un repositorio anidado cuyo config (core.fsmonitor, core.hooksPath)
    ejecuta código cuando git corre en esa carpeta.
    """
    return any(_is_dotgit(x) for x in rel.split("/"))


def classify(rel: str, declared: frozenset[str], symlinked: bool) -> PathInfo:
    """Clase y regla de una ruta canónica según R0-R7, con R6a (02 §3.3 y §3.5)."""
    s = rel.split("/")
    low = [x.lower() for x in s]
    # R0: '.git' en cualquier segmento y sin distinguir mayúsculas, como git (verify_path).
    if symlinked or is_git_path(rel):
        return PathInfo(rel, "lead", "R0")
    lowrel = rel.lower()
    if lowrel in _LOCAL_EXACT_LOW or lowrel.startswith(_LOCAL_PREF_LOW):
        return PathInfo(rel, "local", "R1")
    if any(x in INSTR_DIRS for x in low[1:-1]) or (len(s) > 1 and low[-1] in INSTR_FILES):
        return PathInfo(rel, "lead", "R2")
    if lowrel in GEN_EXACT or (
        len(s) > 2 and s[0] in (".claude", ".agents") and s[1] == "skills"
    ):
        return PathInfo(rel, "generated", "R3")
    if (
        len(s) == 3
        and s[:2] == ["memory", "decisions"]
        and s[2].endswith(".md")
        and not s[2].startswith(".")
    ):
        return PathInfo(rel, "append", "R4")
    if m := TASK_NOTE.fullmatch(rel):
        return PathInfo(rel, "task", "R5", task=int(m[1]))
    # R6: la carpeta de la parte está en la raíz, sections/<id>/ (D-43); tex/sections/<id>/ es R7.
    if len(s) >= 3 and s[0] == "sections" and SECTION_ID.fullmatch(s[1]) and s[1] in declared:
        return PathInfo(rel, "section", "R6", section=s[1])
    if is_editor_path(rel):  # R6a, desde H3 (00-marco D-56)
        return PathInfo(rel, "editor", "R6a")
    return PathInfo(rel, "lead", "R7")


def is_editor_path(rel: str) -> bool:
    """Archivo del editor del artículo (02 §3.3, R6a): ``EDITOR_EXACT`` o ``tex/outline/*.tex``.

    Como R4, ``*`` es un solo segmento (02 §3.2) y la comparación distingue mayúsculas: en un
    sistema que no las distingue, la grafía real ya la puso ``apply_case_map`` (REQ-CONV-9).
    """
    if rel in EDITOR_EXACT:
        return True
    s = rel.split("/")
    return len(s) == 3 and s[:2] == ["tex", "outline"] and s[2].endswith(".tex")
