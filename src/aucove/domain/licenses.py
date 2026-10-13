# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Licencias del proyecto (11-trazabilidad §7.2 y §7.3; 02-convencion S-8; 00-marco D-64).

Módulo puro (REQ-ARCH-18): las categorías de ``REUSE.toml``, qué licencias usa un árbol, el
``REUSE.toml`` que escribe ``init`` y las sugerencias de la tabla de 11 §7.3.

- ``category``: ``text`` (la regla general), ``code``, ``data`` o ``generated`` (los skills, que
  quedan bajo MIT-0), con la semántica de ``REUSE.toml``: gana la última tabla que coincide;
  ``*`` no cruza ``/`` y ``**`` sí. Las rutas son las de 02-convencion S-8.
- ``covered``: las rutas que REUSE no ignora por su nombre (carpetas ``LICENSES``, ``.git``,
  ``.hg``, ``.sl`` y ``.reuse``; ``LICENSE*``, ``COPYING*``, ``*.license``, ``REUSE.toml`` y los
  documentos SPDX). REUSE ignora también los archivos vacíos (los ``.gitkeep``): eso lo mira el
  llamador, que puede leer el árbol.
- ``in_use``: las licencias que usa un árbol. D-64: ``LICENSES/`` lleva exactamente esos textos,
  porque REUSE falla tanto con un texto que nadie usa como con el que falta para un archivo.
  Un archivo con su propia cabecera SPDX declara su licencia y aquí no se ve: su texto lo agrega
  quien trae ese archivo.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from .. import _meta

__all__ = [
    "ALL_RIGHTS_RESERVED",
    "CATEGORIES",
    "CODE_PATHS",
    "DATA_PATHS",
    "GENERATED_LICENSE",
    "GENERATED_PATHS",
    "LICENSE_ID",
    "OPTIONS",
    "PACKAGED",
    "TEMPLATE_HOLDER",
    "Category",
    "LicenseSet",
    "categories_with_files",
    "category",
    "covered",
    "in_use",
    "is_open",
    "reuse_toml",
    "suggested",
]

Category = Literal["text", "code", "data"]
CATEGORIES: Final[tuple[Category, ...]] = ("text", "code", "data")

# 11 REQ-TRACE-64: identificador SPDX (sin el operador '+') o LicenseRef-*.
LICENSE_ID: Final[re.Pattern[str]] = re.compile(r"LicenseRef-[A-Za-z0-9.-]+|[A-Za-z0-9][A-Za-z0-9.-]*")
ALL_RIGHTS_RESERVED: Final = "LicenseRef-AllRightsReserved"
GENERATED_LICENSE: Final = "MIT-0"
# Titular de lo que viene de la plantilla y de los skills generados (11 REQ-TRACE-61).
TEMPLATE_HOLDER: Final = f"2026 The {_meta.NAME} Authors"

# 02-convencion S-8. El orden importa: data va después de code (analysis/derived/ es data).
CODE_PATHS: Final[tuple[str, ...]] = (
    ".github/**", ".claude/**", ".agents/**", ".devcontainer/**", ".latexmkrc", ".gitignore",
    ".gitattributes", ".mcp.json", "tex/main.tex", "tex/preamble.tex", "tex/notation.tex",
    "sections/*/analysis/**",
)
DATA_PATHS: Final[tuple[str, ...]] = (
    "bib/**", "sections/*/pending.bib", "sections/*/tables/*.csv", "sections/*/analysis/derived/**",
)
GENERATED_PATHS: Final[tuple[str, ...]] = (".claude/skills/**", ".agents/skills/**")

# Tabla de 11 §7.3: la sugerida si el repositorio es público o privado, y las demás opciones.
_SUGGESTED: Final[Mapping[Category, tuple[str, str]]] = {
    "text": ("CC-BY-4.0", ALL_RIGHTS_RESERVED),
    "code": ("MIT", "MIT"),
    "data": ("CC0-1.0", ALL_RIGHTS_RESERVED),
}
OPTIONS: Final[Mapping[Category, tuple[str, ...]]] = {
    "text": ("CC-BY-4.0", ALL_RIGHTS_RESERVED, "CC-BY-SA-4.0", "CC0-1.0"),
    "code": ("MIT", "Apache-2.0", "BSD-3-Clause", "GPL-3.0-or-later", "MIT-0"),
    "data": ("CC0-1.0", ALL_RIGHTS_RESERVED, "CC-BY-4.0", "ODbL-1.0"),
}
# Los textos que trae el paquete (00-marco D-64): los de la tabla, salvo LicenseRef-AllRightsReserved,
# que init genera con el titular.
PACKAGED: Final[tuple[str, ...]] = tuple(sorted({x for opts in OPTIONS.values() for x in opts} - {ALL_RIGHTS_RESERVED}))

_IGNORED_DIRS: Final[frozenset[str]] = frozenset({"LICENSES", ".git", ".hg", ".sl", ".reuse"})
_IGNORED_FILE: Final[re.Pattern[str]] = re.compile(
    r"LICEN[CS]E([-.].*)?|COPYING([-.].*)?|\.git|\.hgtags|.*\.license|REUSE\.toml|.*\.spdx(\.(rdf|json|xml|ya?ml))?"
)


@dataclass(frozen=True)
class LicenseSet:
    """``project.yaml: licenses`` (11 §7.3): el titular y una licencia por categoría."""

    holder: str
    text: str
    code: str
    data: str

    def of(self, cat: Category) -> str:
        return {"text": self.text, "code": self.code, "data": self.data}[cat]


def suggested(cat: Category, visibility: str) -> str:
    """La licencia sugerida de la tabla de 11 §7.3; ``internal`` cuenta como privado."""
    public, private = _SUGGESTED[cat]
    return public if visibility == "public" else private


def is_open(license_id: str) -> bool:
    """Una licencia de la lista SPDX es abierta; un ``LicenseRef-*`` no (11 REQ-TRACE-63)."""
    return not license_id.startswith("LicenseRef-")


def _glob(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


_CODE: Final = tuple(_glob(p) for p in CODE_PATHS)
_DATA: Final = tuple(_glob(p) for p in DATA_PATHS)
_GENERATED: Final = tuple(_glob(p) for p in GENERATED_PATHS)


def category(rel: str) -> Category | Literal["generated"]:
    """Categoría de una ruta del proyecto en el ``REUSE.toml`` de ``init`` (gana la última)."""
    if any(p.fullmatch(rel) for p in _GENERATED):
        return "generated"
    if any(p.fullmatch(rel) for p in _DATA):
        return "data"
    if any(p.fullmatch(rel) for p in _CODE):
        return "code"
    return "text"


def covered(rel: str) -> bool:
    """``False`` si REUSE ignora la ruta por su nombre (ver el docstring del módulo)."""
    parts = rel.split("/")
    if any(part in _IGNORED_DIRS for part in parts[:-1]):
        return False
    return _IGNORED_FILE.fullmatch(parts[-1]) is None


def categories_with_files(paths: Iterable[str], nonempty: Callable[[str], bool]) -> frozenset[str]:
    """Las categorías (``text``, ``code``, ``data`` y ``generated``) que tienen algún archivo que
    REUSE cubre. ``nonempty(ruta)`` dice si el archivo tiene contenido (REUSE ignora los vacíos);
    se llama a lo sumo hasta encontrar un archivo de cada categoría."""
    found: set[str] = set()
    for rel in sorted(paths):
        if len(found) == 4:
            break
        cat = category(rel)
        if cat not in found and covered(rel) and nonempty(rel):
            found.add(cat)
    return frozenset(found)


def in_use(paths: Iterable[str], licenses: LicenseSet, nonempty: Callable[[str], bool]) -> dict[str, list[str]]:
    """Licencias que usan los archivos ``paths`` (rutas del árbol), con sus categorías."""
    present = categories_with_files(paths, nonempty)
    found: dict[str, list[str]] = {}
    for cat in CATEGORIES:
        if cat in present:
            found.setdefault(licenses.of(cat), []).append(cat)
    if "generated" in present:
        found.setdefault(GENERATED_LICENSE, []).append("generated")
    return found


def _toml_list(items: Sequence[str]) -> str:
    return "[" + ", ".join(json.dumps(x, ensure_ascii=False) for x in items) + "]"


def _table(comment: str, paths: Sequence[str], holders: Sequence[str], license_id: str) -> list[str]:
    holder = json.dumps(holders[0], ensure_ascii=False) if len(holders) == 1 else _toml_list(holders)
    return [
        "",
        f"[[annotations]]   # {comment}",
        f"path = {_toml_list(paths)}",
        f"SPDX-FileCopyrightText = {holder}",
        f"SPDX-License-Identifier = {json.dumps(license_id)}",
    ]


def reuse_toml(licenses: LicenseSet, *, year: int, template_code: Sequence[str], comments: Mapping[str, str]) -> str:
    """El ``REUSE.toml`` de ``init`` (11 REQ-TRACE-61): ``text`` como regla general, luego ``code``
    (con el segundo titular en ``template_code``, las rutas de código que vienen de la
    plantilla), ``data`` y, al final, los skills generados bajo MIT-0.

    ``comments`` trae, en el idioma del proyecto, ``head`` (la primera línea, que marca el archivo
    como generado) y el comentario de cada tabla: ``text``, ``code``, ``template``, ``data`` y
    ``generated``.
    """
    owner = f"{year} {licenses.holder}"
    lines = [f"# {comments['head']}", "version = 1"]
    lines += _table(comments["text"], ["**"], [owner], licenses.text)
    lines += _table(comments["code"], CODE_PATHS, [owner], licenses.code)
    if template_code:
        lines += _table(comments["template"], sorted(template_code), [owner, TEMPLATE_HOLDER], licenses.code)
    lines += _table(comments["data"], DATA_PATHS, [owner], licenses.data)
    lines += _table(comments["generated"], GENERATED_PATHS, [TEMPLATE_HOLDER], GENERATED_LICENSE)
    return "\n".join(lines) + "\n"
