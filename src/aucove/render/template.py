# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Render determinista de la plantilla (01-arquitectura §10.5).

``render_template`` copia ``base/``, agrega ``l10n/<lang>/`` y los archivos de
``extra`` (los generados de ``render.generated`` y, desde H4, los skills) y
sustituye ``{{var:nombre}}`` en los archivos de texto. Es una función pura
sobre un directorio: no escribe nada ni importa ``config``.

Reglas (REQ-ARCH-82..84):

- Un archivo es de texto si sus bytes son UTF-8 válido y no contienen NUL; si
  no, es binario y se copia tal cual, sin sustitución ni normalización.
- El texto sale en UTF-8 sin BOM, con LF y exactamente un salto final
  (``normalize_text``). Un archivo vacío queda vacío (``.gitkeep``, REQ-CONV-4).
- Solo se sustituye ``{{var:nombre}}``, con la forma exacta de 01 §10.2 y
  aunque vaya detrás de ``$``. Las expresiones ``${{ ... }}`` válidas de
  GitHub Actions quedan intactas porque ninguna empieza por ``var:`` ni por
  otro tipo de marcador. Una variable sin valor, un marcador ``{{var:`` mal
  formado o un marcador de otro tipo (``tool``, ``cli``, ``human``,
  ``skill``, que solo admiten los cuerpos de skills) es ``RenderError``
  (REQ-ARCH-80).
- ``l10n/es`` y ``l10n/en`` tienen el mismo conjunto de rutas y no se cruzan
  con ``base/`` (REQ-ARCH-83); un cruce, un enlace simbólico o una ruta de
  ``extra`` que ya exista en la plantilla es ``RenderError``.
- El render filtra por ``format`` (REQ-ARCH-100, D-43). Con ``latex`` (la plantilla
  publicada) omite ``sections/*/*.md``. Con ``none`` omite ``tex/**``, ``bib/**``,
  ``.latexmkrc``, ``sections/*/pending.bib``, ``sections/*/*.tex`` y los dos workflows del
  editor del artículo (09-ci REQ-CI-57), y reescribe
  ``project.yaml`` con ``format: none`` y sin ``build``, ``ci.pdf_retention_days`` ni
  ``ci.latexdiff`` (02-convencion REQ-CONV-2). La reescritura es de texto, línea a
  línea, para conservar los comentarios.
- Variantes por formato: un archivo de ``formats/<fmt>/l10n/<lang>/`` reemplaza, en el
  render de ``fmt``, al de la misma ruta de ``l10n/<lang>/`` (así la constitución del
  render ``none`` no cita ``tex/``, ``bib/`` ni el PDF, 02-convencion REQ-CONV-91). Solo
  reemplaza: la ruta existe en ``l10n/`` y va en el render de ese formato, así que las
  rutas del render no cambian; ``es`` y ``en`` traen las mismas (REQ-ARCH-83).
  ``l10n/<lang>/`` sigue siendo exactamente la lista de 02-convencion §10.

``init`` usa además las piezas sueltas (05 §9.3; 01 §10.6): ``template_sources`` da la
fuente de cada ruta del render sin renderizarla, ``render_file`` renderiza un archivo y
``match_source`` dice si un archivo sigue siendo el de la plantilla, para algún valor de
sus marcadores, y con qué valores.

Aquí viven también ``RenderError``, ``normalize_text`` y las comprobaciones de
logins e ids que comparten ``codeowners`` y ``task_form``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from ..i18n import LANGS, Lang

__all__ = [
    "RenderError",
    "TemplateSource",
    "normalize_text",
    "is_text",
    "render_template",
    "render_file",
    "template_sources",
    "match_source",
    "in_format",
    "project_yaml_without_manuscript",
    "check_login",
    "check_section_id",
    "check_sha12",
    "VAR_MARKER",
    "FORMATS",
    "FORMATS_DIR",
    "LATEX_WORKFLOWS",
]

# project.yaml: format (00-marco D-43). La plantilla publicada es la variante latex (REQ-CONV-1).
FORMATS: Final[tuple[str, ...]] = ("latex", "none")
# Carpeta de template/ con las variantes de archivos localizables por formato (REQ-ARCH-100).
FORMATS_DIR: Final = "formats"
PROJECT_YAML: Final = "project.yaml"
# Claves de primer nivel y de ci que no existen sin manuscrito (REQ-ARCH-100, REQ-CONV-2).
_NO_MANUSCRIPT_TOP: Final[frozenset[str]] = frozenset({"build"})
_NO_MANUSCRIPT_CI: Final[frozenset[str]] = frozenset({"pdf_retention_days", "latexdiff"})
# Workflows que solo existen con format: latex (09-ci REQ-CI-57; 00-marco D-56).
LATEX_WORKFLOWS: Final[frozenset[str]] = frozenset(
    {".github/workflows/editor-review.yml", ".github/workflows/editor-review-trigger.yml"}
)
_TOP_KEY: Final[re.Pattern[str]] = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)[ \t]*:")
_FORMAT_LINE: Final[re.Pattern[str]] = re.compile(r"format:[ \t]*[A-Za-z]+([ \t]+#.*)?")

_BOM: Final = b"\xef\xbb\xbf"

# Marcador {{var:nombre}} con la forma exacta de 01 §10.2, sin excepciones por lo que
# lo preceda: ninguna expresión ${{ ... }} válida de Actions empieza por 'var:' ni por
# 'tool:', 'cli:', 'human:' o 'skill:' (el contexto real es 'vars.'), así que un '$'
# delante no protege nada y no debe ocultar un marcador (REQ-ARCH-80).
VAR_MARKER: Final[re.Pattern[str]] = re.compile(r"\{\{var:([A-Za-z0-9_ -]+)\}\}")
_VAR_START: Final[re.Pattern[str]] = re.compile(r"\{\{var:")
# Marcadores que solo admiten los cuerpos de skills (01 §10.2): en template/ son un error.
_OTHER_MARKER: Final[re.Pattern[str]] = re.compile(r"\{\{(tool|cli|human|skill):")

# Gramáticas de roles.schema.json ($defs/login y $defs/section.id).
_LOGIN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9]+(-[A-Za-z0-9]+)*(_[A-Za-z0-9]+)?")
_SECTION_ID: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
_SHA12: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{12}")
_LOGIN_MAX: Final = 39
_SECTION_ID_MAX: Final = 40


class RenderError(ValueError):
    """Entrada que no se puede renderizar de forma segura y determinista."""


def normalize_text(b: bytes) -> bytes:
    """Sin BOM, CRLF y CR sueltos a LF y exactamente un salto final (REQ-ARCH-82).

    Un contenido vacío, o hecho solo de saltos de línea, queda vacío: los
    ``.gitkeep`` deben seguir vacíos (REQ-CONV-4).
    """
    if b.startswith(_BOM):
        b = b[len(_BOM) :]
    b = b.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    b = b.rstrip(b"\n")
    return b + b"\n" if b else b""


def is_text(data: bytes) -> bool:
    """Detección de REQ-ARCH-84: UTF-8 estricto y sin bytes NUL."""
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def check_login(login: object, what: str) -> str:
    """``login`` con la gramática de ``$defs/login``; si no, ``RenderError``."""
    if not isinstance(login, str) or len(login) > _LOGIN_MAX or not _LOGIN.fullmatch(login):
        raise RenderError(f"{what}: login inválido {login!r}")
    return login


def check_section_id(section_id: object) -> str:
    """Id de sección con la gramática de ``$defs/section.id`` (sin ``none``); si no, ``RenderError``."""
    if (
        not isinstance(section_id, str)
        or len(section_id) > _SECTION_ID_MAX
        or not _SECTION_ID.fullmatch(section_id)
    ):
        raise RenderError(f"id de sección inválido: {section_id!r}")
    if section_id == "none":
        # GitHub muestra 'None' en un dropdown sin respuesta (03 §8.2): el id está prohibido.
        raise RenderError("el id de sección 'none' está prohibido")
    return section_id


def check_sha12(value: object) -> str:
    """12 hex en minúsculas (forma de ``domain.text.sha12_canonical``); si no, ``RenderError``."""
    if not isinstance(value, str) or not _SHA12.fullmatch(value):
        raise RenderError(f"source_sha12 inválido: {value!r}")
    return value


def _substitute(text: str, variables: Mapping[str, str], rel: str) -> str:
    other = _OTHER_MARKER.search(text)
    if other is not None:
        raise RenderError(f"{rel}: marcador {{{{{other.group(1)}:...}}}} no admitido en la plantilla")
    for start in _VAR_START.finditer(text):
        if VAR_MARKER.match(text, start.start()) is None:
            raise RenderError(f"{rel}: marcador {{{{var:...}}}} mal formado")

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        value = variables.get(name)
        if value is None:
            raise RenderError(f"{rel}: la variable {name!r} no tiene valor")
        if not isinstance(value, str):
            raise RenderError(f"{rel}: la variable {name!r} no es texto")
        return value

    return VAR_MARKER.sub(replace, text)


def render_file(data: bytes, variables: Mapping[str, str], rel: str) -> bytes:
    """Un archivo de la plantilla con ``{{var:...}}`` resuelto; ``rel`` solo nombra el origen en los errores."""
    if not is_text(data):
        return data  # binario (REQ-ARCH-84): se copia sin tocar
    text = normalize_text(data).decode("utf-8")
    return normalize_text(_substitute(text, variables, rel).encode("utf-8"))


def match_source(source: bytes, current: bytes) -> dict[str, str] | None:
    """Valores de los marcadores con los que ``source`` da ``current``; ``None`` si no lo da.

    Sirve para saber si un archivo sigue siendo el de la plantilla (01 REQ-ARCH-85) sin
    conocer los valores con que se publicó: cada ``{{var:nombre}}`` admite cualquier texto
    de una sola línea, y un nombre repetido debe valer lo mismo en todas sus apariciones.
    Los textos se comparan normalizados (``normalize_text``); un binario coincide solo si
    es idéntico, y entonces no da valores.
    """
    if not is_text(source) or not is_text(current):
        return {} if source == current else None
    src = normalize_text(source).decode("utf-8")
    cur = normalize_text(current).decode("utf-8")
    pieces = VAR_MARKER.split(src)  # literal, nombre, literal, nombre, ..., literal
    groups: dict[str, str] = {}
    pattern = [re.escape(pieces[0])]
    for i in range(1, len(pieces), 2):
        group = groups.get(pieces[i])
        if group is None:
            group = groups[pieces[i]] = f"v{len(groups)}"
            pattern.append(f"(?P<{group}>[^\\n]*?)")
        else:
            pattern.append(f"(?P={group})")
        pattern.append(re.escape(pieces[i + 1]))
    found = re.fullmatch("".join(pattern), cur)
    if found is None:
        return None
    return {name: found.group(group) for name, group in groups.items()}


def _tree(root: Path) -> dict[str, Path]:
    """Ruta POSIX relativa -> archivo, sin seguir enlaces; un enlace es ``RenderError``."""
    found: dict[str, Path] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(dirpath)
        for name in dirnames:
            if os.path.islink(current / name):
                raise RenderError(f"{(current / name).relative_to(root).as_posix()}: enlace en la plantilla")
        dirnames.sort()
        for name in sorted(filenames):
            path = current / name
            rel = path.relative_to(root).as_posix()
            if os.path.islink(path) or not path.is_file():
                raise RenderError(f"{rel}: no es un archivo regular")
            found[rel] = path
    return found


def _subdirs(path: Path, what: str) -> dict[str, Path]:
    """Nombre -> carpeta de las entradas de ``path``; un archivo o un enlace es ``RenderError``."""
    out: dict[str, Path] = {}
    for entry in sorted(path.iterdir()):
        if os.path.islink(entry) or not entry.is_dir():
            raise RenderError(f"{what}/{entry.name}: se esperaba una carpeta")
        out[entry.name] = entry
    return out


def _format_variants(src: Path, local: Mapping[str, Mapping[str, Path]]) -> dict[str, dict[str, dict[str, Path]]]:
    """``formato -> idioma -> ruta -> archivo`` de ``formats/<fmt>/l10n/<lang>/``, ya comprobado.

    Cada variante reemplaza al archivo de la misma ruta de ``l10n/<lang>/`` en el render de su
    formato: tiene que existir allí y estar en ese render (si no, nunca se usaría), y ``es`` y
    ``en`` traen las mismas rutas (REQ-ARCH-83). Cualquier otra cosa es ``RenderError``.
    """
    root = src / FORMATS_DIR
    if not root.exists() and not os.path.islink(root):
        return {}
    if os.path.islink(root) or not root.is_dir():
        raise RenderError(f"{FORMATS_DIR}: se esperaba una carpeta")
    out: dict[str, dict[str, dict[str, Path]]] = {}
    for fmt, fmt_dir in _subdirs(root, FORMATS_DIR).items():
        where = f"{FORMATS_DIR}/{fmt}"
        if fmt not in FORMATS:
            raise RenderError(f"{where}: formato no admitido")
        if set(_subdirs(fmt_dir, where)) != {"l10n"}:
            raise RenderError(f"{where}: solo puede contener l10n/")
        langs = _subdirs(fmt_dir / "l10n", f"{where}/l10n")
        if set(langs) != set(local):
            raise RenderError(f"{where}/l10n: debe traer los idiomas de l10n/ ({', '.join(sorted(local))})")
        trees = {code: _tree(path) for code, path in langs.items()}
        paths = {rel for files in trees.values() for rel in files}
        for code, files in trees.items():
            if set(files) != paths:
                raise RenderError(f"{where}/l10n: los idiomas no tienen las mismas rutas (REQ-ARCH-83)")
            for rel in files:
                if rel not in local[code]:
                    raise RenderError(f"{where}/l10n/{code}/{rel}: no reemplaza ningún archivo de l10n/{code}")
                if not in_format(rel, fmt):
                    raise RenderError(f"{where}/l10n/{code}/{rel}: no va en el render {fmt}")
        out[fmt] = trees
    return out


def in_format(rel: str, fmt: str) -> bool:
    """``True`` si la ruta ``rel`` de la plantilla va en el render de ``format: fmt`` (REQ-ARCH-100)."""
    parts = rel.split("/")
    part_file = len(parts) == 3 and parts[0] == "sections"  # sections/*/<archivo>, sin subcarpetas
    if fmt != "none":
        return not (part_file and parts[2].endswith(".md"))
    if len(parts) > 1 and parts[0] in ("tex", "bib"):
        return False
    if rel == ".latexmkrc" or rel in LATEX_WORKFLOWS:
        return False
    return not (part_file and (parts[2] == "pending.bib" or parts[2].endswith(".tex")))


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _block_end(lines: Sequence[str], start: int) -> int:
    """Fin (exclusivo) del bloque YAML que abre ``lines[start]``.

    Son de él las líneas en blanco y las más sangradas que la clave; las líneas en blanco
    del final quedan fuera, para que se conserven entre bloques.
    """
    indent = _indent(lines[start])
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or _indent(lines[end]) > indent):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return end


def _ci_without_manuscript(block: Sequence[str]) -> list[str]:
    """Bloque ``ci`` sin ``pdf_retention_days`` ni ``latexdiff``; vacío si no le queda ninguna clave."""
    head, children = block[0], list(block[1:])
    if head.split("#", 1)[0].strip() != "ci:":
        raise RenderError(f"{PROJECT_YAML}: ci debe ser un bloque, no un valor en la misma línea")
    kept: list[str] = []
    j = 0
    while j < len(children):
        stripped = children[j].strip()
        key = _TOP_KEY.match(stripped) if stripped and not stripped.startswith("#") else None
        end = _block_end(children, j) if key is not None else j + 1
        if key is None or key.group(1) not in _NO_MANUSCRIPT_CI:
            kept.extend(children[j:end])
        j = end
    if not any(line.strip() and not line.strip().startswith("#") for line in kept):
        return []
    return [head, *kept]


def project_yaml_without_manuscript(text: str) -> str:
    """``project.yaml`` de la variante ``none`` (REQ-ARCH-100, REQ-CONV-2), sin perder comentarios.

    Cambia la única línea ``format`` de primer nivel a ``format: none``, quita el bloque
    ``build`` y, dentro de ``ci``, ``pdf_retention_days`` y ``latexdiff``; si ``ci`` se queda
    sin claves, lo quita también. ``RenderError`` si no hay exactamente una línea ``format``
    de la forma ``format: <valor>``.
    """
    lines = text.split("\n")
    out: list[str] = []
    formats = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        key = _TOP_KEY.match(line)
        if key is None:  # comentario o línea en blanco de primer nivel
            out.append(line)
            i += 1
            continue
        end = _block_end(lines, i)
        name = key.group(1)
        if name == "format":
            found = _FORMAT_LINE.fullmatch(line)
            if found is None:
                raise RenderError(f"{PROJECT_YAML}: la línea format no tiene la forma 'format: <valor>'")
            formats += 1
            out.append("format: none" + (found.group(1) or ""))
            out.extend(lines[i + 1 : end])
        elif name == "ci":
            out.extend(_ci_without_manuscript(lines[i:end]))
        elif name not in _NO_MANUSCRIPT_TOP:
            out.extend(lines[i:end])
        i = end
    if formats != 1:
        raise RenderError(f"{PROJECT_YAML}: se esperaba una línea format de primer nivel y hay {formats}")
    return "\n".join(out)


def _check_rel(rel: object) -> str:
    if (
        not isinstance(rel, str)
        or not rel
        or rel.startswith("/")
        or "\\" in rel
        or ":" in rel
        or any(part in ("", ".", "..") for part in rel.split("/"))
    ):
        raise RenderError(f"ruta de extra inválida: {rel!r}")
    return rel


@dataclass(frozen=True, slots=True)
class TemplateSource:
    """Fuente de una ruta del render: el archivo de ``template/`` y de dónde viene."""

    origin: str  # 'base/<ruta>', 'l10n/<lang>/<ruta>' o 'formats/<fmt>/l10n/<lang>/<ruta>'
    path: Path
    localized: bool  # viene de l10n/ (o de su variante por formato): 02 §10

    def read(self) -> bytes:
        return self.path.read_bytes()


def template_sources(src: Path, lang: Lang, fmt: str = "latex") -> dict[str, TemplateSource]:
    """Ruta del render de ``format: fmt`` en ``lang`` -> su fuente, sin renderizar; rutas ordenadas.

    Hace las mismas comprobaciones que ``render_template`` (REQ-ARCH-83, REQ-ARCH-100 y
    REQ-ARCH-101). ``project.yaml`` es la fuente de ``base/``: la reescritura de la variante
    ``none`` la hace ``render_template`` (o quien la necesite) después de renderizar.
    """
    if lang not in LANGS:
        raise RenderError(f"idioma no admitido: {lang!r}")
    if fmt not in FORMATS:
        raise RenderError(f"formato no admitido: {fmt!r}")
    src = Path(src)
    base_dir = src / "base"
    l10n_dir = src / "l10n"
    if not base_dir.is_dir():
        raise RenderError(f"falta {base_dir.as_posix()}")
    if not (l10n_dir / lang).is_dir():
        raise RenderError(f"falta {(l10n_dir / lang).as_posix()}")
    base = _tree(base_dir)
    local = {code: _tree(l10n_dir / code) for code in LANGS if (l10n_dir / code).is_dir()}
    wanted = set(local[lang])
    for code, files in local.items():
        if set(files) != wanted:
            diff = sorted(set(files) ^ wanted)
            raise RenderError(f"l10n/{code} y l10n/{lang} no tienen las mismas rutas (REQ-ARCH-83): {diff[:5]}")
    crossed = sorted(wanted & set(base))
    if crossed:
        raise RenderError(f"l10n/{lang} repite rutas de base/ (REQ-ARCH-83): {crossed[:5]}")
    variants = _format_variants(src, local).get(fmt, {}).get(lang, {})

    out: dict[str, TemplateSource] = {}
    for rel, path in base.items():
        if in_format(rel, fmt):
            out[rel] = TemplateSource(f"base/{rel}", path, localized=False)
    for rel, path in local[lang].items():
        if not in_format(rel, fmt):
            continue
        variant = variants.get(rel)
        if variant is None:
            out[rel] = TemplateSource(f"l10n/{lang}/{rel}", path, localized=True)
        else:
            out[rel] = TemplateSource(f"{FORMATS_DIR}/{fmt}/l10n/{lang}/{rel}", variant, localized=True)
    return {rel: out[rel] for rel in sorted(out)}


def render_template(
    src: Path,
    lang: Lang,
    variables: Mapping[str, str],
    extra: Mapping[str, bytes] | None = None,
    *,
    fmt: str = "latex",
) -> dict[str, bytes]:
    """``base/`` + ``l10n/<lang>/`` + ``extra``, con ``{{var:...}}`` resuelto; rutas ordenadas.

    ``src`` es el directorio ``template/`` (con ``base/``, ``l10n/`` y, si hay variantes,
    ``formats/``). Los bytes de ``extra`` se agregan tal cual: son la salida ya normalizada
    de los generadores. ``fmt`` es el ``format`` del proyecto (REQ-ARCH-100): ``latex``, la
    plantilla publicada, o ``none``, la variante que usa ``init --format none``; las
    variantes de ``formats/<fmt>/l10n/<lang>/`` reemplazan a sus archivos de ``l10n/<lang>/``.
    """
    out = {rel: render_file(s.read(), variables, s.origin) for rel, s in template_sources(src, lang, fmt).items()}
    if fmt == "none" and PROJECT_YAML in out:
        try:
            text = out[PROJECT_YAML].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RenderError(f"{PROJECT_YAML} no es texto UTF-8") from exc
        out[PROJECT_YAML] = normalize_text(project_yaml_without_manuscript(text).encode("utf-8"))
    for rel, data in (extra or {}).items():
        rel = _check_rel(rel)
        if rel in out:
            raise RenderError(f"{rel}: extra repite una ruta de la plantilla")
        if not isinstance(data, bytes):
            raise RenderError(f"{rel}: el contenido de extra debe ser bytes")
        out[rel] = data
    # Dos rutas que solo difieren en mayúsculas se pisarían en Windows y macOS.
    folded: dict[str, str] = {}
    for rel in sorted(out):
        other_rel = folded.setdefault(rel.lower(), rel)
        if other_rel != rel:
            raise RenderError(f"{other_rel} y {rel} solo difieren en mayúsculas")
    return {rel: out[rel] for rel in sorted(out)}
