# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Nota de sesión de la tarea (03-esquemas §7; paso ``write_note`` de 05 §9.18).

``memory/notes/task-<n>.md`` la escribe solo el núcleo, con lo que redactó la sesión (el objeto
``note`` de ``submit``):

- si no existe, la crea con el front matter ``task``, ``created`` y ``created_by``;
- agrega un bloque al final, que empieza con el marcador de una línea
  ``<!-- <NAME>:session {...} -->`` (REQ-SCHEMA-57 y REQ-SCHEMA-61) y sigue con el encabezado
  localizado y las subsecciones «Hecho», «Pendiente», «Decisiones» y «Preguntas abiertas»;
- nunca toca los bloques anteriores (REQ-SCHEMA-59);
- del front matter reescribe solo ``ai_use`` y ``assisted_by``, con la traza de toda la tarea
  (REQ-TRACE-21, REQ-SCHEMA-86).

Repetir la entrega con la misma ``note`` no agrega un segundo bloque igual.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import _meta
from ..config.yamlload import split_front_matter
from ..i18n import Translator
from .result import OpError

__all__ = ["NOTE_TEXT_MAX", "Block", "Note", "note_path", "read_note", "render_block", "write"]

NOTE_TEXT_MAX = 4000  # 05 §9.18: los textos de note suman como máximo esto
BLOCK_MAX = 4000  # REQ-SCHEMA-60
NOTE_MAX_BYTES = 64 * 1024  # REQ-SCHEMA-60
_MARKER_PREFIX = f"<!-- {_meta.NAME}:session"
_MARKER = re.compile(r"^<!-- " + re.escape(_meta.NAME) + r":session (\{.*\}) -->$")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}")  # session_block.model


def note_path(number: int) -> str:
    """Ruta de la nota de la tarea (03 §7.1)."""
    return f"memory/notes/task-{number}.md"


@dataclass(frozen=True)
class Block:
    meta: dict[str, object]
    text: str  # del marcador hasta antes del siguiente, sin líneas vacías al final

    @property
    def body(self) -> str:
        """El bloque sin la línea del marcador: lo que redactó la sesión."""
        return self.text.split("\n", 1)[1].strip() if "\n" in self.text else ""


@dataclass(frozen=True)
class Note:
    front: dict[str, object] | None
    blocks: tuple[Block, ...]
    body: str


def _blocks(body: str) -> list[Block]:
    out: list[Block] = []
    current: list[str] | None = None
    meta: dict[str, object] = {}
    for line in body.split("\n"):
        if line.startswith(_MARKER_PREFIX):
            if current is not None:
                out.append(Block(meta, "\n".join(current).rstrip()))
            m = _MARKER.match(line)
            try:
                parsed = json.loads(m.group(1)) if m else None
            except ValueError:
                parsed = None
            meta = parsed if isinstance(parsed, dict) else {}
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        out.append(Block(meta, "\n".join(current).rstrip()))
    return out


def parse_note(text: str, path: str) -> Note:
    """Front matter y bloques de una nota; ``front`` es ``None`` si no es válido."""
    front, body, findings = split_front_matter(text, file=path)
    if findings or not isinstance(front, dict):
        return Note(None, tuple(_blocks(text)), text)
    return Note(front, tuple(_blocks(body)), body)


def read_note(root: Path, number: int) -> Note | None:
    """La nota del árbol de trabajo; ``None`` si no existe."""
    target = root.joinpath(*note_path(number).split("/"))
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OpError("E_VALIDATION_NOTE_MISSING", details={"reason": "unreadable"}) from exc
    try:
        text = raw.decode("utf-8-sig").replace("\r\n", "\n")
    except UnicodeDecodeError as exc:
        raise OpError("E_VALIDATION_NOTE_MISSING", details={"reason": "unreadable"}) from exc
    return parse_note(text, note_path(number))


def _safe(text: str) -> str:
    """Texto de la sesión sin un marcador de bloque que rompa la nota."""
    return text.replace("\r\n", "\n").strip().replace(_MARKER_PREFIX, "&lt;!-- " + _MARKER_PREFIX[5:])


def render_block(
    *,
    n: int,
    date: str,
    login: str,
    door: str,
    model: str | None,
    note: Mapping[str, object],
    lang: Translator,
) -> str:
    """Un bloque de sesión (03 §7.2), sin línea vacía al final."""
    safe_model = model if isinstance(model, str) and _MODEL.fullmatch(model) and "--" not in model else None
    meta = {"n": n, "date": date, "login": login, "door": door, "model": safe_model}
    raw = json.dumps(meta, ensure_ascii=False).replace("--", "\\u002d\\u002d")  # REQ-SCHEMA-61
    parts = [f"{_MARKER_PREFIX} {raw} -->", lang("tasks.note.heading", n=n, date=date, login=login), ""]
    parts += [f"### {lang('tasks.note.done')}", "", _safe(str(note["done"]))]
    nxt = note.get("next")
    if isinstance(nxt, str) and nxt.strip():
        parts += ["", f"### {lang('tasks.note.next')}", "", _safe(nxt)]
    decisions = note.get("decisions")
    if isinstance(decisions, list) and decisions:
        parts += ["", f"### {lang('tasks.note.decisions')}", ""] + [f"- {d}" for d in decisions]
    questions = note.get("questions")
    if isinstance(questions, str) and questions.strip():
        parts += ["", f"### {lang('tasks.note.questions')}", "", _safe(questions)]
    return "\n".join(parts)


def _front(task: int, created: str, created_by: str, ai_use: Sequence[str], assisted: Sequence[str]) -> str:
    lines = ["---", f"task: {task}", f"created: {created}", f"created_by: {created_by}"]
    if ai_use:
        lines.append("ai_use: [" + ", ".join(ai_use) + "]")
    if assisted:
        lines.append("assisted_by: [" + ", ".join(json.dumps(a) for a in assisted) + "]")
    return "\n".join(lines + ["---"]) + "\n"


def write(
    root: Path,
    number: int,
    *,
    created: str,
    created_by: str,
    block: str | None,
    ai_use: Sequence[str],
    assisted: Sequence[str],
) -> bool:
    """Escribe la nota: agrega ``block`` (si no es ``None``) y reescribe ``ai_use`` y
    ``assisted_by``. ``True`` si el archivo cambió."""
    target = root.joinpath(*note_path(number).split("/"))
    current = read_note(root, number)
    if current is not None and current.front is None:
        raise OpError("E_VALIDATION_NOTE_MISSING", details={"reason": "invalid_front_matter"})
    front = current.front if current is not None else {}
    assert front is not None
    head = _front(
        number,
        str(front.get("created") or created),
        str(front.get("created_by") or created_by),
        ai_use,
        assisted,
    )
    body = current.body.strip("\n") if current is not None else ""
    if block is not None:
        if len(block) > BLOCK_MAX:
            raise OpError("E_VALIDATION_INPUT", details={"errors": [{"param": "note", "reason": "maxLength"}]})
        body = f"{body}\n\n{block}" if body else block
    text = f"{head}\n{body}\n" if body else head
    old = target.read_text(encoding="utf-8") if target.exists() else None
    if old == text:
        return False
    if len(text.encode("utf-8")) > NOTE_MAX_BYTES:
        raise OpError("E_VALIDATION_NOTE_MISSING", details={"reason": "too_large"})
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode("utf-8"))
    return True
