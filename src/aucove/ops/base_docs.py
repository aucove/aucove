# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Documentos de la rama base que resume ``context`` (05 REQ-OPS-67; 07 §4.1).

Lee de la punta de la base (``BaseSnapshot.sha``, nunca del árbol de trabajo: D-08), con
un solo ``git ls-tree`` y un lote de ``git cat-file --batch``:

- ``memory/decisions/*.md``: las decisiones vigentes más recientes (sin las que otra
  reemplaza con ``supersedes``), por fecha descendente y, a igual fecha, por ruta
  (REQ-OPS-35). Se leen las 20 más nuevas por nombre: una decisión solo la puede
  reemplazar otra posterior, así que el estado de esas 20 es exacto.
- ``plan.md``: el objetivo (el párrafo bajo ``## Objetivo`` u ``## Objective``) y las
  preguntas de investigación activas (``RQ-*``, 03 §5).
- Con ``format: latex``: ``tex/guide.md`` y la sección del artículo de cada parte, que
  sale de los ``\\input{../sections/<parte>/...}`` de ``tex/outline/<id>.tex`` (D-56).
- ``memory/notes/task-<n>.md``: las notas de sesión más recientes (por número de tarea).

Ningún fallo de git ni un archivo mal formado hace fallar la operación: lo que no se
puede leer queda vacío.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..domain.plan import PlanItem, parse_plan

if TYPE_CHECKING:
    from ..config.base import BaseSnapshot
    from ..vcs.git import Git

__all__ = [
    "BaseDocs",
    "DecisionBrief",
    "SectionDoc",
    "guide_excerpt",
    "read_base_docs",
    "read_sections",
]

DECISIONS_DIR = "memory/decisions/"
NOTES_DIR = "memory/notes/"
OUTLINE_DIR = "tex/outline/"
PLAN_FILE = "plan.md"
GUIDE_FILE = "tex/guide.md"
DECISION_WINDOW = 20  # decisiones más nuevas que se leen
MAX_BLOB = 256 * 1024  # un archivo mayor no se resume
MAX_NOTES = 3
_DECISION = re.compile(r"memory/decisions/([^/]+)\.md")
_NOTE = re.compile(r"memory/notes/task-([1-9][0-9]*)\.md")
_OUTLINE = re.compile(r"tex/outline/([a-z0-9]+(?:-[a-z0-9]+)*)\.tex")
_INPUT_PART = re.compile(r"\\input\{\.\./sections/([a-z0-9]+(?:-[a-z0-9]+)*)/")
_OBJECTIVE = re.compile(r"^##\s+(?:Objetivo|Objective)\s*$", re.IGNORECASE)
_HEADING = re.compile(r"^#{1,6}\s")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_REGULAR = ("100644", "100755")
_LABEL = re.compile(r"\\label\{([^}\n]{1,120})\}")
_MACRO = re.compile(r"\\(?:re)?newcommand\*?\s*\{?\s*(\\[A-Za-z@]+)")
MAX_ITEMS = 30  # labels y macros por sección (05 §7.2)
EXCERPT_MAX = 600  # 04-tareas REQ-TASK-58


@dataclass(frozen=True)
class DecisionBrief:
    """Campos de una decisión que puede llevar el contexto (10-seguridad REQ-SEC-8)."""

    path: str
    title: str
    date: str
    status: str
    summary: str
    plan_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class BaseDocs:
    decisions: tuple[DecisionBrief, ...] = ()
    objective: str | None = None
    questions: tuple[PlanItem, ...] = ()
    guide: str | None = None
    outline_of: Mapping[str, str] = field(default_factory=dict)  # parte -> tex/outline/<id>.tex
    notes: tuple[str, ...] = ()
    plan_text: str | None = None  # plan.md de la base, para la preparación pendiente (00-marco D-70)


def _text(data: bytes) -> str | None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text.lstrip("\N{ZERO WIDTH NO-BREAK SPACE}").replace("\r\n", "\n").replace("\r", "\n")


def _front_matter(text: str) -> Mapping[str, object] | None:
    from ..config.yamlload import split_front_matter

    fm, _body, findings = split_front_matter(text, file="-")
    return fm if not findings and isinstance(fm, dict) else None


def _decisions(files: Mapping[str, str], wanted: int) -> tuple[DecisionBrief, ...]:
    """Las ``wanted`` decisiones vigentes más recientes de ``files`` (ruta -> texto)."""
    records: list[tuple[DecisionBrief, tuple[str, ...]]] = []
    for path, text in files.items():
        fm = _front_matter(text)
        if fm is None:
            continue
        title, date, status, summary = (fm.get(k) for k in ("title", "date", "status", "summary"))
        date = date.isoformat() if hasattr(date, "isoformat") else date
        if not all(isinstance(v, str) and v.strip() for v in (title, date, status, summary)):
            continue
        assert isinstance(date, str)
        if not _DATE.fullmatch(date):
            continue
        raw = fm.get("supersedes") or []
        supersedes = tuple(x for x in raw if isinstance(x, str)) if isinstance(raw, list) else ()
        refs = fm.get("plan_refs") or []
        plan_refs = tuple(x for x in refs if isinstance(x, str)) if isinstance(refs, list) else ()
        records.append(
            (DecisionBrief(path, str(title), date, str(status), str(summary), plan_refs), supersedes)
        )
    replaced = {f"{DECISIONS_DIR}{x}.md" for _d, sup in records for x in sup}
    alive = [d for d, _sup in records if d.path not in replaced]
    alive.sort(key=lambda d: d.path)
    alive.sort(key=lambda d: d.date, reverse=True)  # fecha descendente; a igual fecha, ruta
    return tuple(alive[:wanted])


def _objective(text: str) -> str | None:
    lines = _COMMENT.sub("", text).split("\n")
    out: list[str] = []
    inside = False
    for line in lines:
        if _OBJECTIVE.match(line.strip()):
            inside = True
            continue
        if inside and _HEADING.match(line.strip()):
            break
        if inside:
            out.append(line.strip())
    paragraph = " ".join(x for x in out if x).strip()
    return paragraph or None


def guide_excerpt(guide: str, parts: Sequence[str]) -> str | None:
    """Bloques ``###`` de la guía que nombran alguna de ``parts`` entre acentos graves.

    Así se toma la sección del artículo de cada parte de la persona (02 §4.1.1): el
    bloque dice qué cubre y qué partes incluye. ``None`` si no hay ninguno.
    """
    wanted = {f"`{p}`" for p in parts}
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in guide.split("\n"):
        if line.startswith("### "):
            current = [line]
            blocks.append(current)
        elif line.startswith(("# ", "## ")):
            current = None
        elif current is not None:
            current.append(line)
    chosen = ["\n".join(b).strip() for b in blocks if any(w in "\n".join(b) for w in wanted)]
    return "\n\n".join(chosen) if chosen else None


def read_base_docs(git: Git, snap: BaseSnapshot, *, decisions: int, latex: bool) -> BaseDocs:
    """Documentos de la base para el resumen; vacío si la base no está en local."""
    sha = snap.sha
    if not sha:
        return BaseDocs()
    try:
        tree = git.ls_tree_long(sha, ["memory/decisions", "memory/notes", PLAN_FILE, "tex"])
    except Exception:  # noqa: BLE001 - sin base legible, el resumen sale sin estos bloques
        return BaseDocs()

    def readable(path: str) -> str | None:
        entry = tree.get(path)
        if entry is None or entry[0] not in _REGULAR or entry[2] is None or entry[2] > MAX_BLOB:
            return None
        return entry[1]

    decision_paths = sorted((p for p in tree if _DECISION.fullmatch(p)), reverse=True)[:DECISION_WINDOW]
    wanted_paths = [*decision_paths, PLAN_FILE]
    if latex:
        wanted_paths.append(GUIDE_FILE)
        wanted_paths.extend(sorted(p for p in tree if _OUTLINE.fullmatch(p)))
    oids = {p: oid for p in wanted_paths if (oid := readable(p)) is not None}
    blobs: dict[str, bytes] = {}
    try:
        by_oid = {info.oid: data for info, data in git.cat_file_batch(sorted(set(oids.values())))}
        blobs = {p: by_oid[o] for p, o in oids.items() if o in by_oid}
    except Exception:  # noqa: BLE001
        blobs = {}
    texts = {p: t for p, raw in blobs.items() if (t := _text(raw)) is not None}

    plan_text = texts.get(PLAN_FILE)
    objective: str | None = None
    questions: tuple[PlanItem, ...] = ()
    if plan_text is not None:
        objective = _objective(plan_text)
        items, _errors = parse_plan(plan_text)
        questions = tuple(i for i in items if i.kind == "RQ" and i.status == "active")
    outline_of: dict[str, str] = {}
    if latex:
        for path in sorted(p for p in texts if _OUTLINE.fullmatch(p)):
            for part in _INPUT_PART.findall(texts[path]):
                outline_of.setdefault(part, path)
    notes = sorted(
        (p for p in tree if _NOTE.fullmatch(p)),
        key=lambda p: int(_NOTE.fullmatch(p)[1]),  # type: ignore[index]
        reverse=True,
    )[:MAX_NOTES]
    return BaseDocs(
        decisions=_decisions({p: texts[p] for p in decision_paths if p in texts}, decisions),
        objective=objective,
        questions=questions,
        guide=texts.get(GUIDE_FILE) if latex else None,
        outline_of=outline_of,
        notes=tuple(notes),
        plan_text=plan_text,
    )


@dataclass(frozen=True)
class SectionDoc:
    """Lo que el paquete de contexto toma de una parte de la base (04-tareas REQ-TASK-58)."""

    id: str
    path: str  # sections/<id>/
    labels: tuple[str, ...]
    macros: tuple[str, ...]
    excerpt_source: str | None  # el archivo principal de la parte, si existe
    excerpt: str  # hasta 600 caracteres; el llamador lo marca como reviewed


def _main_file(section: str, files: Sequence[str]) -> str | None:
    """Archivo principal de la parte: ``<id>.tex`` o ``<id>.md``; si no, el primero."""
    for ext in (".tex", ".md"):
        candidate = f"sections/{section}/{section}{ext}"
        if candidate in files:
            return candidate
    texts = [f for f in files if f.endswith((".tex", ".md"))]
    return texts[0] if texts else None


def read_sections(git: Git, snap: BaseSnapshot, ids: Sequence[str]) -> dict[str, SectionDoc]:
    """``\\label``, ``\\newcommand`` y un extracto de cada parte de ``ids`` en la base.

    Extracción determinista por expresión regular sobre los ``.tex`` de la carpeta de la
    parte (REQ-TASK-58). Una parte sin archivos en la base da listas vacías y extracto vacío.
    """
    sha = snap.sha
    wanted = sorted(set(ids))
    if not sha or not wanted:
        return {}
    try:
        tree = git.ls_tree_long(sha, [f"sections/{i}" for i in wanted])
    except Exception:  # noqa: BLE001 - sin base legible no hay extractos
        tree = {}
    per: dict[str, list[str]] = {i: [] for i in wanted}
    for path in sorted(tree):
        parts = path.split("/")
        if len(parts) >= 3 and parts[1] in per and tree[path][0] in _REGULAR:
            per[parts[1]].append(path)
    oids = {}
    for files in per.values():
        for path in files:
            entry = tree[path]
            if path.endswith((".tex", ".md")) and entry[2] is not None and entry[2] <= MAX_BLOB:
                oids[path] = entry[1]
    blobs: dict[str, bytes] = {}
    if oids:
        try:
            by_oid = {info.oid: data for info, data in git.cat_file_batch(sorted(set(oids.values())))}
            blobs = {p: by_oid[o] for p, o in oids.items() if o in by_oid}
        except Exception:  # noqa: BLE001
            blobs = {}
    out: dict[str, SectionDoc] = {}
    for section in wanted:
        files = per[section]
        labels: list[str] = []
        macros: list[str] = []
        for path in files:
            if not path.endswith(".tex") or path not in blobs:
                continue
            text = _text(blobs[path]) or ""
            labels.extend(x for x in _LABEL.findall(text) if x not in labels)
            macros.extend(x for x in _MACRO.findall(text) if x not in macros)
        main = _main_file(section, files)
        excerpt = (_text(blobs[main]) or "")[:EXCERPT_MAX] if main is not None and main in blobs else ""
        out[section] = SectionDoc(
            id=section,
            path=f"sections/{section}/",
            labels=tuple(x[:120] for x in labels[:MAX_ITEMS]),
            macros=tuple(x[:120] for x in macros[:MAX_ITEMS]),
            excerpt_source=main,
            excerpt=excerpt,
        )
    return out
