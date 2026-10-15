# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Archivos que ``init`` deja en el árbol de trabajo (05 §9.3; 01 §10.5 a §10.7; 02 REQ-CONV-2).

``plan_files`` calcula, sin escribir nada, qué archivos crea, cambia o borra ``init`` para
dejar el proyecto en el idioma, el formato y el modo elegidos. ``plan_generated`` es la parte
de ``init --regenerate`` (REQ-ARCH-87): solo los archivos ``generated``.

Archivos de la plantilla
    Un archivo sigue siendo el de la plantilla si su contenido normalizado (LF, sin BOM) es
    el de su fuente de ``template/`` en algún idioma y en alguno de los dos formatos, para
    algún valor de sus marcadores (``render.template.match_source``): así no hace falta saber
    con qué valores se publicó la plantilla. Solo esos se traducen, se vuelven a renderizar
    con los valores del proyecto o se borran; uno que cambió se deja y va a ``kept``
    (REQ-ARCH-85: «si difiere, DEBE dejarlo y advertirlo»). REQ-ARCH-85 compara con el
    render ``en``; aquí vale cualquier idioma, para que volver a ejecutar ``init`` no avise de
    los archivos que ya tradujo (REQ-PERM-25). Los que faltan se crean.

``project.yaml``
    Se reescriben líneas sueltas, sin perder los comentarios: ``core`` y ``core_pin`` con la
    versión instalada (REQ-ARCH-44), ``language``, ``format``, ``disclosure``, ``forge.repo``
    (el ``origin``) y, con ``--individual``, ``mode: individual`` (00-marco D-57). ``name`` y
    ``title`` solo cambian si siguen siendo los de la plantilla publicada: pasan a ser el
    nombre del repositorio. Con ``format: none`` se quitan ``build``, ``ci.pdf_retention_days``
    y ``ci.latexdiff`` (REQ-CONV-2). Pasar de ``none`` a ``latex`` es una tarea del responsable
    (REQ-CONV-88): ``E_VALIDATION_INPUT``. No escribe ``require_commercial`` (D-48).

``roles.yaml``
    Si sigue siendo el de ejemplo, ``init`` lo completa (05 §9.3: «se completan de forma
    interactiva»): en el idioma elegido, con quien ejecuta ``init`` como responsable y dueño de
    todas las partes, y sin las demás personas de ejemplo. Si no, queda tal cual y tiene que
    ser válido. Con ``mode: individual``, una sola persona activa (REQ-SCHEMA-94).

Partes (REQ-CONV-2)
    Cada parte de ``roles.yaml`` tiene ``figures/.gitkeep`` y ``tables/.gitkeep``; con LaTeX,
    ``<id>.tex`` y ``pending.bib``, su ``\\addbibresource`` en ``main.tex`` y su ``\\input`` en un
    archivo de enlace (``tex/outline/<id>.tex``, que se crea con el título de la parte si
    ninguno la incluye, y su ``\\input{outline/<id>}``); sin manuscrito, ``<id>.md``. Las partes
    de la plantilla traen su contenido; una parte nueva, el mínimo del catálogo
    (``init.file``). Las partes de la plantilla que ``roles.yaml`` no declara se borran, con su
    archivo de enlace y sus líneas de ``main.tex``, salvo que alguno de sus archivos cambiara.

Formato ``none`` (REQ-ARCH-100)
    Se borran ``tex/``, ``bib/``, ``.latexmkrc``, los ``pending.bib`` y los ``.tex`` de las
    partes que sigan siendo los de la plantilla (o los mínimos que creó ``init``).

Workflows
    Las líneas ``branches: [<rama>]`` marcadas con ``# = project.yaml: forge.base_branch``
    (09-ci §7) toman ``forge.base_branch``.

``.mcp.json`` (01-arquitectura §10.3)
    No sale de ``template/`` sino del generador ``render.mcp``: si falta, se crea; si ya está,
    es del responsable (clase ``lead``) y no se toca.

Generados
    ``.github/CODEOWNERS`` y ``.github/ISSUE_TEMPLATE/task.yml`` salen del ``roles.yaml``
    final con la misma función que ``sync-roles`` y la regla ``roles-sync``. Los directorios de
    skills generados (``<nombre>-generated: "true"``) que el render ya no produce se borran
    (REQ-ARCH-86 y REQ-ARCH-87); en esta versión el núcleo no genera skills.

Licencias (11-trazabilidad §7.3; 00-marco D-64)
    Con ``Choices.licenses``, ``project.yaml`` lleva el bloque ``licenses`` y, sobre el árbol ya
    planeado: ``LICENSES/`` queda con exactamente los textos que usan sus archivos según el
    ``REUSE.toml`` nuevo (los del paquete, el de ``LicenseRef-AllRightsReserved``, que se genera
    con el titular, o los que dio la persona; uno que ya está no se toca). Los textos que nadie
    usa se borran si son los del paquete o el generado; si no, se dejan (``license_unused``).
    ``LICENSE`` pasa a ser el resumen de las tres licencias y ``REUSE.toml`` se regenera
    (``domain.licenses.reuse_toml``), los dos en el idioma del proyecto y solo si siguen siendo
    los de la plantilla o los que generó ``init`` (lo dice una línea suya); si no, se dejan
    (``modified``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .. import _meta
from ..config.datafiles import data_path, license_text
from ..config.project import ProjectConfig, parse_project
from ..config.roles import Roles, mode_problems, parse_roles
from ..domain.compat import PIN_VERSION, core_range
from ..domain.findings import Finding
from ..domain.licenses import (
    ALL_RIGHTS_RESERVED,
    CATEGORIES,
    CODE_PATHS,
    DATA_PATHS,
    GENERATED_LICENSE,
    LicenseSet,
    categories_with_files,
    category,
    reuse_toml,
)
from ..domain.text import same_login
from ..i18n import LANGS, Lang, Translator
from ..render.mcp import MCP_JSON_PATH, mcp_json
from ..render.template import (
    FORMATS,
    RenderError,
    TemplateSource,
    in_format,
    is_text,
    match_source,
    normalize_text,
    project_yaml_without_manuscript,
    render_file,
    template_sources,
)
from .result import OpError
from .rules.roles_sync import Regeneration, regenerate

__all__ = [
    "Change",
    "Choices",
    "FilePlan",
    "Kept",
    "TEMPLATE_TITLE",
    "core_range",
    "plan_files",
    "plan_generated",
    "project_name",
    "with_base_branch",
]

Reader = Callable[[str], bytes | None]
Action = Literal["created", "updated", "deleted"]
# modified: cambió y no se traduce ni se borra; part_modified: una parte que roles.yaml no
# declara y que tiene cambios; unknown_value: falta el valor de un marcador para crearlo;
# license_unused: un texto de LICENSES/ que ningún archivo usa y que no es del paquete (D-64).
KeptReason = Literal["modified", "part_modified", "unknown_value", "license_unused"]

PROJECT = "project.yaml"
ROLES = "roles.yaml"
MAIN = "tex/main.tex"
OUTLINE_DIR = "tex/outline/"
WORKFLOWS_DIR = ".github/workflows/"
SKILL_ROOTS: tuple[str, ...] = (".claude/skills/", ".agents/skills/")
LICENSE = "LICENSE"
LICENSES_DIR = "LICENSES/"
REUSE = "REUSE.toml"
# title de tools/template_vars.toml: el de la plantilla publicada (lo comprueba una prueba)
TEMPLATE_TITLE = "Research project template"
MAX_FINDINGS = 20  # hallazgos citados en details de E_CONFIG_INVALID

_TOP_KEY = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)[ \t]*:(?P<rest>.*)")
_FORGE_REPO = re.compile(r"(?P<indent>[ \t]+)repo[ \t]*:(?P<rest>.*)")
_FORGE_BASE = re.compile(r"(?P<indent>[ \t]+)base_branch[ \t]*:(?P<rest>.*)")
# una rama que YAML lee como texto sin comillas: empieza con letra y no es un booleano ni null de YAML 1.1
_SAFE_BRANCH = re.compile(r"[A-Za-z][A-Za-z0-9_./-]*")
_YAML_WORDS = frozenset({"y", "yes", "n", "no", "true", "false", "on", "off", "null"})
_PERSON = re.compile(r"(?P<head>[ \t]*-[ \t]+login:[ \t]*)(?P<login>[^\s#]+)(?P<tail>.*)")
_OWNER = re.compile(r"(?P<head>[ \t]+owner:[ \t]*)(?P<login>[^\s#]+)(?P<tail>.*)")
_LEAD = re.compile(r"lead:[ \t]*(?P<login>[^\s#]+)(?P<tail>.*)")
_BIB_LINE = re.compile(r"[ \t]*\\addbibresource\{\.\./sections/(?P<part>[^/{}]+)/pending\.bib\}[ \t]*")
_OUTLINE_LINE = re.compile(r"[ \t]*\\input\{outline/(?P<id>[^/{}]+)\}[ \t]*")
_ANY_BIB = re.compile(r"[ \t]*\\addbibresource\{")
_INPUT_PART = re.compile(r"\\input\{\.\./sections/(?P<part>[^/{}]+)/(?P=part)\}")
_BRANCHES = re.compile(
    r"(?P<head>[ \t]*branches:[ \t]*\[[ \t]*)(?P<branch>[^\]\s]*)(?P<tail>[ \t]*\][ \t]*#[ \t]*=[ \t]*project\.yaml:"
    r"[ \t]*forge\.base_branch.*)"
)
_SAFE_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+")
_SLUG_SPLIT = re.compile(r"[^a-z0-9]+")
_LATEX_SPECIAL = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "#": r"\#",
    "$": r"\$",
    "%": r"\%",
    "&": r"\&",
    "_": r"\_",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


@dataclass(frozen=True, slots=True)
class Change:
    """Un archivo que ``init`` escribe o borra."""

    path: str
    action: Action
    content: bytes | None = None  # None al borrar


@dataclass(frozen=True, slots=True)
class Kept:
    """Un archivo (o la carpeta ``sections/<id>/`` de una parte) que ``init`` deja como está."""

    path: str
    reason: KeptReason


@dataclass(frozen=True, slots=True)
class Choices:
    """Lo que eligió el responsable y lo que ``init`` sabe del repositorio."""

    language: Lang
    format: str  # latex | none (00-marco D-43)
    disclosure: str  # private | milestones | open (D-44)
    individual: bool  # escribe mode: individual (D-57)
    repo: str  # owner/name del origin: forge.repo
    core_pin: str  # versión instalada que ejecuta init (REQ-ARCH-44)
    lead: str  # login de quien ejecuta init: completa el roles.yaml de ejemplo
    # perfil advisory: (login, fecha RFC 3339) de la aceptación (06 REQ-PERM-41); se escribe en
    # forge.advisory_acceptance si project.yaml todavía no la tiene
    acceptance: tuple[str, str] | None = None
    # licencias de 11 §7.3 (project.yaml: licenses); None: init no toca las licencias
    licenses: LicenseSet | None = None
    # textos que dio la persona para licencias que el paquete no trae (00-marco D-64), por identificador
    license_texts: Mapping[str, bytes] = field(default_factory=dict)
    year: int = 0  # año de los avisos de copyright (el de init); hace falta con licenses


@dataclass(frozen=True)
class FilePlan:
    """Lo que ``init`` hace en el árbol de trabajo, en orden de ruta."""

    changes: tuple[Change, ...]
    kept: tuple[Kept, ...]
    roles_completed: bool  # roles.yaml era el de ejemplo y se completó
    project: ProjectConfig  # project.yaml final
    roles: Roles  # roles.yaml final


# -- árbol con los cambios planeados ---------------------------------------------------


def _same(a: bytes | None, b: bytes | None) -> bool:
    """Iguales, o textos iguales tras normalizar (un checkout con CRLF no cuenta como cambio)."""
    if a is None or b is None:
        return a is b
    if a == b:
        return True
    return is_text(a) and is_text(b) and normalize_text(a) == normalize_text(b)


class _Tree:
    """El árbol de trabajo con los cambios planeados encima."""

    def __init__(self, read: Reader, entries: Collection[str]) -> None:
        self._read = read
        self._entries = frozenset(entries)
        self._original: dict[str, bytes | None] = {}
        self._writes: dict[str, bytes] = {}
        self._deletes: set[str] = set()

    def original(self, rel: str) -> bytes | None:
        """Contenido actual en disco (``None`` si no existe o no es un archivo regular)."""
        if rel not in self._original:
            self._original[rel] = self._read(rel) if rel in self._entries else None
        return self._original[rel]

    def get(self, rel: str) -> bytes | None:
        if rel in self._writes:
            return self._writes[rel]
        if rel in self._deletes:
            return None
        return self.original(rel)

    def exists(self, rel: str) -> bool:
        return rel in self._writes or (rel in self._entries and rel not in self._deletes)

    def put(self, rel: str, data: bytes) -> None:
        self._deletes.discard(rel)
        if rel in self._entries and _same(self.original(rel), data):
            self._writes.pop(rel, None)
        else:
            self._writes[rel] = data

    def delete(self, rel: str) -> None:
        self._writes.pop(rel, None)
        if rel in self._entries:
            self._deletes.add(rel)

    def paths(self) -> list[str]:
        return sorted((self._entries - self._deletes) | set(self._writes))

    def under(self, prefix: str) -> list[str]:
        return [p for p in self.paths() if p.startswith(prefix)]

    def changes(self) -> tuple[Change, ...]:
        out: list[Change] = []
        for rel in sorted(set(self._writes) | self._deletes):
            if rel in self._deletes:
                out.append(Change(rel, "deleted"))
            else:
                out.append(Change(rel, "updated" if rel in self._entries else "created", self._writes[rel]))
        return tuple(out)


# -- la plantilla ----------------------------------------------------------------------


class _Template:
    """Fuentes de ``template/`` en los dos idiomas y los dos formatos."""

    def __init__(self, root: Path) -> None:
        self.sources = {(lang, fmt): template_sources(root, lang, fmt) for lang in LANGS for fmt in FORMATS}
        self._bytes: dict[Path, bytes] = {}
        self.part_ids = frozenset(
            rel.split("/")[1]
            for files in self.sources.values()
            for rel in files
            if rel.startswith("sections/") and rel.count("/") >= 2
        )

    def data(self, source: TemplateSource) -> bytes:
        if source.path not in self._bytes:
            self._bytes[source.path] = source.read()
        return self._bytes[source.path]

    def candidates(self, rel: str) -> list[TemplateSource]:
        """Fuentes de ``rel`` en cualquier idioma y formato, sin repetir archivos."""
        seen: set[Path] = set()
        out: list[TemplateSource] = []
        for key in sorted(self.sources):
            found = self.sources[key].get(rel)
            if found is not None and found.path not in seen:
                seen.add(found.path)
                out.append(found)
        return out

    def match(self, rel: str, data: bytes | None) -> dict[str, str] | None:
        """Valores de los marcadores si ``data`` es el render de alguna fuente de ``rel``."""
        if data is None:
            return None
        for source in self.candidates(rel):
            found = match_source(self.data(source), data)
            if found is not None:
                return found
        return None

    def part_of(self, rel: str) -> str | None:
        """Parte de la plantilla a la que pertenece ``rel`` (su carpeta o su archivo de enlace)."""
        pieces = rel.split("/")
        if pieces[0] == "sections" and len(pieces) >= 3 and pieces[1] in self.part_ids:
            return pieces[1]
        if rel.startswith(OUTLINE_DIR) and rel.endswith(".tex") and rel.count("/") == 2:
            part = rel[len(OUTLINE_DIR) : -len(".tex")]
            return part if part in self.part_ids else None
        return None


# -- utilidades de texto -----------------------------------------------------------------


def _text(data: bytes | None) -> str | None:
    if data is None or not is_text(data):
        return None
    return normalize_text(data).decode("utf-8")


def _encode(text: str) -> bytes:
    return normalize_text(text.encode("utf-8"))


def _comment_of(rest: str) -> str:
    """Comentario final (con su espacio previo) del valor escalar ``rest``; ``""`` si no hay."""
    body = rest.lstrip(" \t")
    lead = rest[: len(rest) - len(body)]
    if body.startswith('"'):
        i = 1
        while i < len(body) and body[i] != '"':
            i += 2 if body[i] == "\\" else 1
        tail = body[i + 1 :]
    elif body.startswith("'"):
        i = 1
        while i < len(body):
            if body[i] == "'":
                if body[i + 1 : i + 2] != "'":
                    break
                i += 1
            i += 1
        tail = body[i + 1 :]
    else:
        found = re.search(r"[ \t]+#", body)
        tail = body[found.start() :] if found else ("" if not body.startswith("#") else lead + body)
    return tail if re.fullmatch(r"[ \t]+#.*", tail) else ""


def _top_index(lines: Sequence[str], key: str) -> int | None:
    for i, line in enumerate(lines):
        found = _TOP_KEY.fullmatch(line)
        if found is not None and found.group("key") == key:
            return i
    return None


def _block_end(lines: Sequence[str], start: int) -> int:
    """Fin (exclusivo) del bloque de primer nivel que abre ``lines[start]``, sin las líneas en blanco finales."""
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end][:1] in (" ", "\t")):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return end


def _set_top(lines: list[str], key: str, value: str, *, after: Sequence[str]) -> None:
    """Fija la clave de primer nivel ``key``; si falta, la inserta tras el bloque de la primera de ``after``."""
    index = _top_index(lines, key)
    if index is not None:
        found = _TOP_KEY.fullmatch(lines[index])
        assert found is not None
        lines[index] = f"{key}: {value}{_comment_of(found.group('rest'))}"
        return
    position = None
    for other in after:
        at = _top_index(lines, other)
        if at is not None:
            position = _block_end(lines, at)
            break
    if position is None:
        position = len(lines)
        while position > 0 and not lines[position - 1].strip():
            position -= 1
    lines.insert(position, f"{key}: {value}")


def _set_acceptance(lines: list[str], login: str, at: str) -> None:
    """``forge.advisory_acceptance`` (03 §3.1; 06 REQ-PERM-41), si el bloque ``forge`` no la tiene."""
    start = _top_index(lines, "forge")
    if start is None:
        raise OpError("E_CONFIG_INVALID", details={"path": PROJECT, "reason": "forge_missing"})
    end = _block_end(lines, start)
    if any(re.match(r"[ \t]+advisory_acceptance[ \t]*:", lines[i]) for i in range(start + 1, end)):
        return
    lines[end:end] = ["  advisory_acceptance:", f"    by: {login}", f"    at: {json.dumps(at)}"]


def _set_licenses(lines: list[str], lic: LicenseSet) -> None:
    """``licenses`` de 11 §7.3: reemplaza el bloque de primer nivel o lo agrega al final."""
    block = [
        "licenses:",
        f"  holder: {json.dumps(lic.holder, ensure_ascii=False)}",
        f"  text: {lic.text}",
        f"  code: {lic.code}",
        f"  data: {lic.data}",
    ]
    start = _top_index(lines, "licenses")
    if start is not None:
        lines[start:_block_end(lines, start)] = block
        return
    end = len(lines)
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    lines[end:end] = block


def _set_forge_repo(lines: list[str], repo: str) -> None:
    value = repo if _SAFE_REPO.fullmatch(repo) else json.dumps(repo)
    start = _top_index(lines, "forge")
    if start is None:
        raise OpError("E_CONFIG_INVALID", details={"path": PROJECT, "reason": "forge_missing"})
    end = _block_end(lines, start)
    for i in range(start + 1, end):
        found = _FORGE_REPO.fullmatch(lines[i])
        if found is not None:
            lines[i] = f"{found.group('indent')}repo: {value}{_comment_of(found.group('rest'))}"
            return
    lines.insert(start + 1, f"  repo: {value}")


def with_base_branch(data: bytes, branch: str) -> bytes:
    """``project.yaml`` con ``forge.base_branch: <branch>``, sin perder los comentarios.

    Con ``init --repo`` la base es la rama por defecto del repo vacío de la forja (00-marco D-73).
    """
    lines = (_text(data) or "").split("\n")
    start = _top_index(lines, "forge")
    if start is None:
        raise OpError("E_CONFIG_INVALID", details={"path": PROJECT, "reason": "forge_missing"})
    safe = _SAFE_BRANCH.fullmatch(branch) is not None and branch.lower() not in _YAML_WORDS
    value = branch if safe else json.dumps(branch)
    end = _block_end(lines, start)
    for i in range(start + 1, end):
        found = _FORGE_BASE.fullmatch(lines[i])
        if found is not None:
            lines[i] = f"{found.group('indent')}base_branch: {value}{_comment_of(found.group('rest'))}"
            return _encode("\n".join(lines))
    lines.insert(end, f"  base_branch: {value}")
    return _encode("\n".join(lines))


def _slug(name: str) -> str:
    """``name`` de ``project.yaml`` desde el nombre del repositorio (kebab-case ASCII, 64 como máximo)."""
    slug = "-".join(part for part in _SLUG_SPLIT.split(name.lower()) if part)[:64].strip("-")
    return slug or "project"


def project_name(cfg: ProjectConfig, repo: str) -> str:
    """El ``name`` que tendrá ``project.yaml`` tras ``init`` (el del repositorio si es el de la plantilla)."""
    return _slug(repo.split("/", 1)[-1]) if cfg.name == _meta.TEMPLATE_REPO else cfg.name


def _latex_escape(text: str) -> str:
    return "".join(_LATEX_SPECIAL.get(ch, ch) for ch in text)


def _invalid(path: str, reason: str, findings: Sequence[Finding] = ()) -> OpError:
    """``E_CONFIG_INVALID`` con los códigos de los hallazgos (como ``sync-roles``), sin su texto."""
    details: dict[str, object] = {"path": path, "reason": reason}
    errors = [f for f in findings if f.severity == "error"][:MAX_FINDINGS]
    if errors:
        details["code"] = errors[0].code
        details["findings"] = [{"code": f.code, "line": f.line, "pointer": f.pointer} for f in errors]
    return OpError("E_CONFIG_INVALID", details=details)


# -- project.yaml y roles.yaml -------------------------------------------------------------


def _project_text(current: str, cfg: ProjectConfig, choices: Choices) -> str:
    """``project.yaml`` con las claves que escribe ``init`` (sin tocar las demás ni los comentarios)."""
    found = PIN_VERSION.fullmatch(choices.core_pin)
    if found is None:  # core_pin solo admite X.Y.Z o X.Y.Z{a,b,rc}N (REQ-ARCH-45, D-66)
        raise OpError("E_CONFIG_INVALID", details={"path": PROJECT, "reason": "core_pin_version",
                                                   "version": choices.core_pin})
    repo_name = choices.repo.split("/", 1)[-1]
    lines = current.split("\n")
    _set_top(lines, "core", json.dumps(core_range(choices.core_pin)), after=("convention",))
    _set_top(lines, "core_pin", json.dumps(choices.core_pin), after=("core",))
    if cfg.name == _meta.TEMPLATE_REPO:
        _set_top(lines, "name", _slug(repo_name), after=("core_pin",))
    if cfg.title == TEMPLATE_TITLE:
        _set_top(lines, "title", json.dumps(repo_name, ensure_ascii=False), after=("name",))
    _set_top(lines, "language", choices.language, after=("title", "name"))
    _set_top(lines, "format", choices.format, after=("language",))
    _set_top(lines, "disclosure", choices.disclosure, after=("format",))
    if choices.individual:
        _set_top(lines, "mode", "individual", after=("disclosure", "format"))
    _set_forge_repo(lines, choices.repo)
    if choices.acceptance is not None:
        _set_acceptance(lines, *choices.acceptance)
    if choices.licenses is not None:
        _set_licenses(lines, choices.licenses)
    text = "\n".join(lines)
    if choices.format == "none":
        try:
            text = project_yaml_without_manuscript(text)
        except RenderError as exc:
            raise OpError("E_CONFIG_INVALID", details={"path": PROJECT, "reason": "format_line"}) from exc
    return text


def _complete_roles(text: str, example: Roles, lead: str) -> str:
    """El ``roles.yaml`` de ejemplo con ``lead`` como responsable, única persona y dueño de todo."""
    others = {p.login for p in example.people if not same_login(p.login, example.lead)}
    known = {example.lead, *others}
    lead_person = example.person(example.lead)
    assert lead_person is not None
    name_line = re.compile(
        r"(?P<head>[ \t]+name:[ \t]*)(?:" + re.escape(json.dumps(lead_person.name, ensure_ascii=False))
        + "|" + re.escape(lead_person.name) + r")(?P<tail>[ \t]+#.*)?"
    )
    out: list[str] = []
    dropping = False
    for line in text.split("\n"):
        person = _PERSON.fullmatch(line)
        if person is not None:
            dropping = person.group("login") in others
            if not dropping:
                line = f"{person.group('head')}{lead}{person.group('tail')}"
        elif dropping and line.startswith("    "):
            continue
        else:
            dropping = False
        if dropping:
            continue
        lead_line = _LEAD.fullmatch(line)
        owner = _OWNER.fullmatch(line)
        named = name_line.fullmatch(line)
        if lead_line is not None and lead_line.group("login") in known:
            line = f"lead: {lead}{lead_line.group('tail')}"
        elif owner is not None and owner.group("login") in known:
            line = f"{owner.group('head')}{lead}{owner.group('tail')}"
        elif named is not None:
            line = f"{named.group('head')}{json.dumps(lead, ensure_ascii=False)}{named.group('tail') or ''}"
        out.append(line)
    return "\n".join(out)


# -- el plan -------------------------------------------------------------------------------


class _Planner:
    """Estado del cálculo de ``plan_files``."""

    def __init__(self, tree: _Tree, tpl: _Template, choices: Choices, t: Translator) -> None:
        self.tree = tree
        self.tpl = tpl
        self.choices = choices
        self.t = t
        self.target = tpl.sources[(choices.language, choices.format)]
        self.kept: dict[str, KeptReason] = {}
        self.variables: dict[str, str] = {}
        self.deleted_parts: set[str] = set()
        self.created_outlines: list[str] = []
        self.generated: frozenset[str] = frozenset()  # CODEOWNERS y task.yml, que la plantilla trae

    def keep(self, rel: str, reason: KeptReason) -> None:
        self.kept.setdefault(rel, reason)

    def render(self, rel: str) -> bytes | None:
        """Render de ``rel`` en el idioma y el formato elegidos; ``None`` si falta un valor."""
        source = self.target[rel]
        try:
            return render_file(self.tpl.data(source), self.variables, source.origin)
        except RenderError:
            self.keep(rel, "unknown_value")
            return None

    def captured(self) -> dict[str, str]:
        """Valores de los marcadores de los archivos que siguen siendo los de la plantilla."""
        out: dict[str, str] = {}
        for rel in self.tree.paths():
            if rel == PROJECT or not self.tpl.candidates(rel):
                continue
            found = self.tpl.match(rel, self.tree.original(rel))
            for key, value in (found or {}).items():
                out.setdefault(key, value)
        return out

    # -- formato none

    def generic_part(self, rel: str) -> bool:
        """``rel`` es el ``<id>.tex`` mínimo que crea ``init`` para una parte nueva (en algún idioma)."""
        pieces = rel.split("/")
        if len(pieces) != 3 or pieces[0] != "sections" or pieces[2] != f"{pieces[1]}.tex":
            return False
        current = self.tree.original(rel)
        return any(_same(current, _part_tex(pieces[1], _translator(lang))) for lang in LANGS)

    def drop_manuscript(self) -> None:
        """REQ-CONV-2 con ``format: none``: lo que solo usa LaTeX, si no cambió."""
        for rel in self.tree.paths():
            if in_format(rel, "none") or rel in (PROJECT, ROLES):
                continue
            if self.tpl.match(rel, self.tree.original(rel)) is not None or self.generic_part(rel):
                self.tree.delete(rel)
            else:
                self.keep(rel, "modified")

    # -- archivos de la plantilla

    def mcp(self) -> None:
        """``.mcp.json``: se crea si falta (ver el docstring del módulo)."""
        if not self.tree.exists(MCP_JSON_PATH):
            self.tree.put(MCP_JSON_PATH, mcp_json())

    def restore_missing(self) -> None:
        """Crea los archivos del render elegido que faltan (salvo las partes, que van aparte)."""
        for rel in self.target:
            if rel in (PROJECT, ROLES) or self.tpl.part_of(rel) is not None or self.tree.exists(rel):
                continue
            data = self.render(rel)
            if data is not None:
                self.tree.put(rel, data)

    def localize(self, declared: Collection[str]) -> None:
        """REQ-ARCH-85: los localizables que siguen siendo los de la plantilla, en el idioma elegido."""
        for rel, source in self.target.items():
            if not source.localized or rel == ROLES or not self.tree.exists(rel):
                continue
            part = self.tpl.part_of(rel)
            if part is not None and part not in declared:
                continue  # parte que no se declara: la decide parts()
            if self.tpl.match(rel, self.tree.original(rel)) is None:
                self.keep(rel, "modified")
                continue
            data = self.render(rel)
            if data is not None:
                self.tree.put(rel, data)

    # -- partes

    def drop_undeclared(self, declared: Collection[str]) -> None:
        """Partes de la plantilla que ``roles.yaml`` no declara: se borran si nada cambió."""
        for part in sorted(self.tpl.part_ids - set(declared)):
            group = self.tree.under(f"sections/{part}/")
            outline = f"{OUTLINE_DIR}{part}.tex"
            if self.tree.exists(outline):
                group.append(outline)
            if not group:
                continue
            if all(self.tpl.match(rel, self.tree.original(rel)) is not None for rel in group):
                for rel in group:
                    self.tree.delete(rel)
                self.deleted_parts.add(part)
            else:
                self.keep(f"sections/{part}/", "part_modified")

    def part_files(self, sections: Sequence[tuple[str, str]]) -> None:
        """Los archivos obligatorios de cada parte declarada que faltan (REQ-CONV-2, REQ-CONV-24)."""
        bib_source = next(
            (self.target[rel] for rel in self.target if rel.startswith("sections/") and rel.endswith("/pending.bib")),
            None,
        )
        for part, title in sections:
            root = f"sections/{part}/"
            wanted = [f"{root}figures/.gitkeep", f"{root}tables/.gitkeep"]
            wanted += [f"{root}{part}.tex", f"{root}pending.bib"] if self.choices.format == "latex" else [f"{root}{part}.md"]
            for rel in wanted:
                if self.tree.exists(rel):
                    continue
                data: bytes | None
                if rel in self.target:
                    data = self.render(rel)
                elif rel.endswith("/.gitkeep"):
                    data = b""
                elif rel.endswith(".tex"):
                    data = _part_tex(part, self.t)
                elif rel.endswith("/pending.bib"):
                    data = b"" if bib_source is None else render_file(self.tpl.data(bib_source), {}, bib_source.origin)
                else:
                    data = _encode(f"# {title}\n")
                if data is not None:
                    self.tree.put(rel, data)

    def outlines(self, sections: Sequence[tuple[str, str]]) -> None:
        """Cada parte declarada, incluida en un archivo de enlace (REQ-CONV-2, REQ-CONV-93)."""
        included: set[str] = set()
        for rel in self.tree.under(OUTLINE_DIR):
            included |= {m.group("part") for m in _INPUT_PART.finditer(_text(self.tree.get(rel)) or "")}
        for part, title in sections:
            if part in included:
                continue
            rel = f"{OUTLINE_DIR}{part}.tex"
            current = _text(self.tree.get(rel))
            data: bytes | None
            if current is None and not self.tree.exists(rel):
                data = self.render(rel) if rel in self.target else _outline_tex(part, title, self.t)
                if data is not None:
                    self.created_outlines.append(part)
            elif current is not None:
                data = _encode(current.rstrip("\n") + f"\n\n\\input{{../sections/{part}/{part}}}\n")
            else:
                self.keep(rel, "modified")  # existe pero no es texto
                data = None
            if data is not None:
                self.tree.put(rel, data)

    def main_tex(self, sections: Sequence[tuple[str, str]]) -> None:
        """``main.tex``: las líneas de las partes borradas fuera, las de las nuevas dentro (REQ-CONV-18)."""
        text = _text(self.tree.get(MAIN))
        if text is None:
            if self.tree.exists(MAIN):
                self.keep(MAIN, "modified")
            return
        lines = [
            line for line in text.split("\n")
            if not ((m := _BIB_LINE.fullmatch(line)) and m.group("part") in self.deleted_parts)
            and not ((m := _OUTLINE_LINE.fullmatch(line)) and m.group("id") in self.deleted_parts)
        ]
        bibs = {m.group("part") for line in lines if (m := _BIB_LINE.fullmatch(line))}
        outlines = {m.group("id") for line in lines if (m := _OUTLINE_LINE.fullmatch(line))}
        new_bibs = [f"\\addbibresource{{../sections/{part}/pending.bib}}" for part, _ in sections if part not in bibs]
        new_outlines = [f"\\input{{outline/{part}}}" for part in self.created_outlines if part not in outlines]
        if new_bibs:
            at = max((i for i, line in enumerate(lines) if _ANY_BIB.match(line)), default=None)
            if at is None:
                at = max((i for i, line in enumerate(lines) if line.strip().startswith("\\input{notation}")), default=None)
            position = len(lines) if at is None else at + 1
            lines[position:position] = new_bibs
        if new_outlines:
            at = max((i for i, line in enumerate(lines) if _OUTLINE_LINE.fullmatch(line)), default=None)
            if at is None:
                at = next(
                    (i - 1 for i, line in enumerate(lines)
                     if line.strip().startswith(("\\printbibliography", "\\end{document}"))),
                    None,
                )
            position = len(lines) if at is None else at + 1
            lines[position:position] = new_outlines
        self.tree.put(MAIN, _encode("\n".join(lines)))

    # -- workflows y generados

    # -- licencias (11-trazabilidad §7.3; 00-marco D-64)

    def licenses(self, title: str) -> None:
        """``LICENSES/``, ``LICENSE`` y ``REUSE.toml`` sobre el árbol ya planeado (ver el docstring)."""
        lic = self.choices.licenses
        if lic is None:
            return
        tree = self.tree
        present = categories_with_files(tree.paths(), lambda rel: bool(tree.get(rel)))
        used: dict[str, str] = {}  # identificador -> la primera categoría que lo usa
        for cat in CATEGORIES:
            if cat in present:
                used.setdefault(lic.of(cat), cat)
        if "generated" in present:
            used.setdefault(GENERATED_LICENSE, "generated")
        for license_id, cat in sorted(used.items()):
            rel = f"{LICENSES_DIR}{license_id}.txt"
            if tree.exists(rel):
                continue  # el texto que ya está se respeta
            data = self.license_text(license_id)
            if data is None:  # D-64: init lo pidió antes; sin él no se escribe nada
                raise OpError("E_VALIDATION_INPUT", details={"errors": [
                    {"param": f"licenses.{cat}", "reason": "text_missing", "license": license_id}]})
            tree.put(rel, data)
        for rel in tree.under(LICENSES_DIR):
            license_id = rel[len(LICENSES_DIR):-len(".txt")] if rel.endswith(".txt") else ""
            if not license_id or "/" in license_id or license_id in used:
                continue
            if self.managed_text(license_id, tree.get(rel)):
                tree.delete(rel)
            else:
                self.keep(rel, "license_unused")
        self.managed(LICENSE, self.license_summary(lic, present, title), self.is_summary)
        template_code = [rel for rel in tree.paths()
                         if (rel in self.target or rel in self.generated or rel == MCP_JSON_PATH)
                         and category(rel) == "code"]
        comments = {key: self.t(f"init.reuse.{key}", name=_meta.NAME)
                    for key in ("head", "text", "code", "template", "data", "generated")}
        toml = reuse_toml(lic, year=self.choices.year, template_code=template_code, comments=comments)
        self.managed(REUSE, _encode(toml), self.is_reuse)

    def license_text(self, license_id: str) -> bytes | None:
        """El texto de ``license_id``: el del paquete, el generado o el que dio la persona."""
        lic = self.choices.licenses
        assert lic is not None
        if license_id == ALL_RIGHTS_RESERVED:
            return _encode(self.t("init.license.file.all_rights_reserved", year=self.choices.year, holder=lic.holder))
        packaged = license_text(license_id)
        if packaged is not None:
            return packaged
        given = self.choices.license_texts.get(license_id)
        return _encode(normalize_text(given).decode("utf-8")) if given is not None else None

    def managed_text(self, license_id: str, data: bytes | None) -> bool:
        """Un texto de ``LICENSES/`` que puso ``init`` (o la plantilla) y se puede borrar sin perder nada."""
        if data is None:
            return True
        packaged = license_text(license_id)
        if packaged is not None:
            return _same(data, packaged)
        if license_id != ALL_RIGHTS_RESERVED:
            return False
        text = _text(data) or ""
        return any(_filled(_translator(lang)("init.license.file.all_rights_reserved", year="\0", holder="\0"), text)
                   for lang in LANGS)

    def managed(self, rel: str, desired: bytes, generated: Callable[[str], bool]) -> None:
        """Escribe ``rel`` si falta, si sigue siendo el de la plantilla o si lo generó ``init``."""
        current = self.tree.original(rel)
        if current is None or self.tpl.match(rel, current) is not None or generated(_text(current) or ""):
            self.tree.put(rel, desired)
        else:
            self.keep(rel, "modified")

    def is_summary(self, text: str) -> bool:
        lines = set(text.split("\n"))
        return any(_translator(lang)("init.license.file.generated", name=_meta.NAME) in lines for lang in LANGS)

    def is_reuse(self, text: str) -> bool:
        first = text.split("\n", 1)[0]
        return any(first == "# " + _translator(lang)("init.reuse.head", name=_meta.NAME) for lang in LANGS)

    def license_summary(self, lic: LicenseSet, present: Collection[str], title: str) -> bytes:
        """``LICENSE``: el resumen en texto plano de las tres licencias y sus rutas (REQ-TRACE-61)."""
        t = self.t
        paths = {"text": t("init.license.paths.text"), "code": ", ".join(CODE_PATHS), "data": ", ".join(DATA_PATHS)}
        lines = [t("init.license.file.head", title=title), "", t("init.license.file.generated", name=_meta.NAME), ""]
        for cat in CATEGORIES:
            key = "line" if cat in present else "unused"
            license_id = lic.of(cat)
            lines.append(t(f"init.license.file.{key}", label=t(f"init.license.label.{cat}"), paths=paths[cat],
                           id=license_id, file=f"{LICENSES_DIR}{license_id}.txt"))
        lines += ["", t("init.license.file.holder", holder=lic.holder), t("init.license.file.template", name=_meta.NAME)]
        return _encode("\n".join(lines))

    def workflow_branches(self, base: str) -> None:
        """``branches: [<rama>]`` de los workflows marcados (09-ci §7): ``forge.base_branch``."""
        for rel in self.tree.under(WORKFLOWS_DIR):
            text = _text(self.tree.get(rel))
            if text is None:
                continue
            lines = text.split("\n")
            changed = False
            for i, line in enumerate(lines):
                found = _BRANCHES.fullmatch(line)
                if found is not None and found.group("branch") != base:
                    lines[i] = f"{found.group('head')}{base}{found.group('tail')}"
                    changed = True
            if changed:
                self.tree.put(rel, _encode("\n".join(lines)))


def _filled(template: str, text: str) -> bool:
    """``text`` es ``template`` con cada ``\0`` reemplazado por una línea de texto no vacía."""
    pattern = "".join("[^\n]+" if piece == "\0" else re.escape(piece) for piece in re.split("(\0)", template))
    return re.fullmatch(pattern, text) is not None


def _translator(lang: Lang) -> Translator:
    from ..i18n import translator

    return translator(lang)


def _part_tex(part: str, t: Translator) -> bytes:
    """``<id>.tex`` mínimo de una parte que no viene en la plantilla."""
    return _encode(t("init.file.part_tex", id=part))


def _outline_tex(part: str, title: str, t: Translator) -> bytes:
    """Archivo de enlace mínimo de una parte (REQ-CONV-92): título, etiqueta y su ``\\input``."""
    header = t("init.file.outline", title=title, id=part).rstrip("\n")
    body = f"\\section{{{_latex_escape(title)}}}\n\\label{{sec:{part}}}\n\n\\input{{../sections/{part}/{part}}}\n"
    return _encode(f"{header}\n{body}")


def _generated(tree: _Tree, t: Translator) -> Regeneration:
    """CODEOWNERS y task.yml desde ``roles.yaml`` y ``project.yaml`` del árbol planeado."""
    regen = regenerate(tree.get, t=t)
    if regen.expected is None:
        raise _invalid(regen.problem_path or ROLES, regen.problem or "render_failed", regen.findings)
    for rel, data in regen.expected.items():
        tree.put(rel, data)
    return regen


def _stale_skills(tree: _Tree, rendered: Collection[str] = ()) -> None:
    """Borra los directorios de skills generados que el render ya no produce (REQ-ARCH-86 y 87)."""
    flag = re.compile(r"[ \t]+" + re.escape(_meta.NAME) + r"-generated:[ \t]*[\"']true[\"'][ \t]*")
    for root in SKILL_ROOTS:
        for rel in tree.under(root):
            pieces = rel[len(root) :].split("/")
            if len(pieces) != 2 or pieces[1] != "SKILL.md" or rel in rendered:
                continue
            text = _text(tree.original(rel)) or ""
            lines = text.split("\n")
            if not lines or lines[0] != "---":
                continue
            end = next((i for i in range(1, len(lines)) if lines[i] == "---"), None)
            if end is None or not any(flag.fullmatch(line) for line in lines[1:end]):
                continue
            for other in tree.under(f"{root}{pieces[0]}/"):
                tree.delete(other)


def _sections(roles: Roles) -> list[tuple[str, str]]:
    return [(s.id, s.title) for s in roles.sections]


def plan_files(
    read: Reader,
    entries: Collection[str],
    choices: Choices,
    *,
    t: Translator,
    template: Path | None = None,
) -> FilePlan:
    """Lo que ``init`` escribe y borra en el árbol de trabajo; no escribe nada.

    ``read(ruta)`` da los bytes de un archivo regular del árbol o ``None``; ``entries`` son
    sus rutas (las de git: índice y sin rastrear). ``t`` es el traductor del idioma elegido,
    para los archivos mínimos de las partes nuevas. Errores (``OpError``):
    ``E_CONFIG_INVALID`` si ``project.yaml`` o ``roles.yaml`` no son válidos (con los códigos
    de sus hallazgos) o si ``roles.yaml`` no admite ``mode: individual`` (REQ-SCHEMA-94), y
    ``E_VALIDATION_INPUT`` al pasar de ``format: none`` a ``latex`` (REQ-CONV-88).
    """
    tree = _Tree(read, entries)
    tpl = _Template(template if template is not None else data_path("template"))
    if choices.format not in FORMATS:
        raise OpError("E_VALIDATION_INPUT", details={"errors": [{"param": "format", "reason": "enum"}]})
    current = tree.original(PROJECT)
    if current is None:
        raise OpError("E_CONFIG_NOT_A_PROJECT")
    cfg, findings = parse_project(current, file=PROJECT, t=t)
    if cfg is None:
        raise _invalid(PROJECT, "project_invalid", findings)
    if cfg.format == "none" and choices.format == "latex":
        raise OpError("E_VALIDATION_INPUT", details={"errors": [{"param": "format", "reason": "format_change"}]})

    planner = _Planner(tree, tpl, choices, t)
    project_text = _project_text(_text(current) or "", cfg, choices)
    final_cfg, findings = parse_project(project_text, file=PROJECT, t=t)
    if final_cfg is None:
        raise _invalid(PROJECT, "project_invalid", findings)
    planner.variables = {
        **planner.captured(),
        "core": final_cfg.core,
        "core_pin": final_cfg.core_pin,
        "name": final_cfg.name,
        "title": final_cfg.title,
        "language": choices.language,
        "forge_repo": final_cfg.forge_repo,
    }

    roles_now = tree.original(ROLES)
    completed = tpl.match(ROLES, roles_now) is not None
    if completed:
        example_bytes = tpl.data(planner.target[ROLES])
        example, _ = parse_roles(example_bytes, file=ROLES, t=t)
        if example is None:
            raise _invalid(ROLES, "template_roles_invalid")
        roles_bytes = _encode(_complete_roles(normalize_text(example_bytes).decode("utf-8"), example, choices.lead))
    elif roles_now is None:
        raise _invalid(ROLES, "roles_missing")
    else:
        roles_bytes = roles_now
    roles, findings = parse_roles(roles_bytes, file=ROLES, t=t)
    if roles is None:
        raise _invalid(ROLES, "roles_invalid", findings)
    problems = mode_problems(roles, final_cfg.mode)
    if problems:
        details: dict[str, object] = {
            "path": ROLES,
            "reason": "roles_mode",
            "code": "E_VALIDATION_ROLES_MODE",
            "findings": [{"code": "E_VALIDATION_ROLES_MODE", "line": None, "pointer": p} for p, _ in problems],
        }
        raise OpError("E_CONFIG_INVALID", details=details)

    tree.put(PROJECT, _encode(project_text))
    tree.put(ROLES, roles_bytes)
    sections = _sections(roles)
    declared = {part for part, _ in sections}
    if choices.format == "none":
        planner.drop_manuscript()
    planner.localize(declared)  # antes de restore_missing: solo lo que ya existía
    planner.restore_missing()
    planner.mcp()
    planner.drop_undeclared(declared)
    planner.part_files(sections)
    if choices.format == "latex":
        planner.outlines(sections)
        planner.main_tex(sections)
    planner.workflow_branches(final_cfg.base_branch)
    planner.generated = frozenset(_generated(tree, t).expected or ())
    _stale_skills(tree)
    planner.licenses(final_cfg.title)  # al final: depende de todos los archivos del árbol (D-64)
    kept = tuple(Kept(rel, reason) for rel, reason in sorted(planner.kept.items()))
    return FilePlan(tree.changes(), kept, completed, final_cfg, roles)


def plan_generated(read: Reader, entries: Collection[str], *, t: Translator) -> tuple[Change, ...]:
    """``init --regenerate`` (REQ-ARCH-87): solo los archivos ``generated``, desde el árbol de trabajo.

    CODEOWNERS y task.yml salen de ``roles.yaml`` y ``language`` de ``project.yaml`` del árbol,
    como en ``sync-roles``; los skills generados que el render ya no produce se borran.
    ``E_CONFIG_INVALID`` si alguna de las dos entradas falta o no es válida.
    """
    tree = _Tree(read, entries)
    _generated(tree, t)
    _stale_skills(tree)
    return tree.changes()
