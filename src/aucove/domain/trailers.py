# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Trailers de los commits del núcleo y detección de la herramienta (11-trazabilidad §2).

- ``AI_USES``: el vocabulario de ``AI-Use`` en el orden de la tabla de §2.3, y
  ``canonical_uses``, que valida una declaración (REQ-TRACE-5) y la deja en ese orden.
- ``detect_tool`` y ``resolve_assistance``: el pseudocódigo de §2.6. El modelo que registró
  la sesión (REQ-TRACE-13 y REQ-TRACE-14) lo lee ``ops`` y llega como ``SessionModel``.
- ``trailer_block``: el párrafo final con ``Task``, ``Section``, ``AI-Use`` y ``Assisted-by``,
  en el orden y la forma de REQ-TRACE-2 a REQ-TRACE-4. Nunca escribe ``Signed-off-by``
  (REQ-TRACE-11) ni ``Co-authored-by`` (REQ-TRACE-27).
- ``parse_trailers`` lee un bloque con tolerancia (§2.10) y ``validate_trailers`` lo valida en
  los contextos ``branch-commit``, ``pr-body`` y ``squash-commit`` (REQ-TRACE-32): la gramática,
  el orden y la cardinalidad de §2.1 y §2.2 y lo que cada contexto exige. El nombre y el correo
  de ``Signed-off-by`` y la igualdad con la nota de sesión los compara la regla
  ``traceability``, que tiene ``roles.yaml`` y el árbol.

Funciones puras, sin E/S.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from .. import _meta

__all__ = [
    "AI_USES",
    "ANTHROPIC_NOREPLY",
    "DECLARABLE_USES",
    "KNOWN_TOOLS",
    "MODEL_RE",
    "TOOL_MAX",
    "TOOL_RE",
    "TRAILER_KEYS",
    "TRAILER_MISSING",
    "TRAILER_SYNTAX",
    "UNKNOWN_MODEL",
    "Detection",
    "Entry",
    "SessionModel",
    "TrailerCheck",
    "TrailerContext",
    "TrailerProblem",
    "Trailers",
    "canonical_uses",
    "commit_message",
    "detect_tool",
    "parse_signoff",
    "parse_trailers",
    "resolve_assistance",
    "trailer_block",
    "valid_model",
    "valid_tool",
    "validate_trailers",
]

# §2.3, en el orden de la tabla: es el orden en que se escriben (REQ-TRACE-5).
AI_USES: tuple[str, ...] = (
    "none", "undeclared", "ideation", "literature", "drafting", "editing", "translation", "code",
    "analysis", "figures", "review",
)
# Lo que admite la entrada ``ai_use`` (§2.5): ``undeclared`` solo lo escribe el núcleo.
DECLARABLE_USES: tuple[str, ...] = tuple(u for u in AI_USES if u != "undeclared")
_SOLO = frozenset({"none", "undeclared"})
KNOWN_TOOLS: tuple[str, ...] = ("claude-code", "codex", "antigravity-cli", "gemini-cli", "opencode", "goose", "other")
TOOL_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
TOOL_MAX = 32
MODEL_RE = re.compile(r"[A-Za-z0-9._:@/+-]{1,128}")
UNKNOWN_MODEL = "unknown"
_TOOL_ENV = _meta.ENV_PREFIX + "AI_TOOL"
_MODEL_ENV = _meta.ENV_PREFIX + "AI_MODEL"


def valid_tool(value: object) -> bool:
    return isinstance(value, str) and len(value) <= TOOL_MAX and TOOL_RE.fullmatch(value) is not None


def valid_model(value: object) -> bool:
    return isinstance(value, str) and MODEL_RE.fullmatch(value) is not None


def canonical_uses(uses: Iterable[str]) -> tuple[str, ...]:
    """Usos sin repetir y en el orden de §2.3.

    ``ValueError`` con el motivo (``unknown``, ``duplicate``, ``empty`` o ``solo``) si hay un
    valor desconocido, uno repetido, ninguno, o ``none``/``undeclared`` acompañados
    (REQ-TRACE-5).
    """
    given = list(uses)
    if any(u not in AI_USES for u in given):
        raise ValueError("unknown")
    unique = set(given)
    if len(unique) != len(given):
        raise ValueError("duplicate")
    if not unique:
        raise ValueError("empty")
    if unique & _SOLO and len(unique) > 1:
        raise ValueError("solo")
    return tuple(u for u in AI_USES if u in unique)


@dataclass(frozen=True)
class Detection:
    """Herramienta detectada y de dónde salió (``env:<VARIABLE>``)."""

    tool: str
    source: str


@dataclass(frozen=True)
class SessionModel:
    """Modelo del registro de la sesión (``.aucove/state/sessions/<id>.json``)."""

    model: str | None
    event: str | None


@dataclass(frozen=True)
class Entry:
    """Una entrada ``Assisted-by`` con el origen de la herramienta y del modelo."""

    tool: str
    model: str
    tool_source: str
    model_source: str

    @property
    def value(self) -> str:
        """``<tool>:<model>``, el valor del trailer."""
        return f"{self.tool}:{self.model}"


def detect_tool(env: Mapping[str, str], via_mcp: bool) -> Detection | None:
    """§2.6: la variable de la herramienta, Gemini CLI o Claude Code (REQ-TRACE-15 y 16).

    Un valor de ``<PREFIJO>AI_TOOL`` que no cumple la gramática de ``tool`` (§2.2) se ignora.
    """
    declared = env.get(_TOOL_ENV)
    if declared and valid_tool(declared):
        return Detection(declared, f"env:{_TOOL_ENV}")
    if env.get("GEMINI_CLI") == "1":
        return Detection("gemini-cli", "env:GEMINI_CLI")
    if env.get("CLAUDECODE") == "1" and (via_mcp or env.get("CLAUDE_CODE_CHILD_SESSION") == "1"):
        return Detection("claude-code", "env:CLAUDECODE")
    return None  # CLI de la persona o terminal integrada del IDE


def _normalize(entries: Sequence[Entry]) -> list[Entry]:
    """Sin repetir ``tool:model``, sin ``tool:unknown`` si hay otro modelo de esa herramienta y
    ordenadas por herramienta y modelo (REQ-TRACE-4)."""
    by_value: dict[tuple[str, str], Entry] = {}
    for e in entries:
        by_value.setdefault((e.tool, e.model), e)
    known = {tool for tool, model in by_value if model != UNKNOWN_MODEL}
    kept = [e for (tool, model), e in by_value.items() if model != UNKNOWN_MODEL or tool not in known]
    return sorted(kept, key=lambda e: (e.tool, e.model))


def resolve_assistance(
    det: Detection | None,
    session: SessionModel | None,
    env: Mapping[str, str],
    declared_tool: str | None,
    declared_model: str | None,
) -> tuple[list[Entry], list[str]]:
    """Entradas ``Assisted-by`` y avisos (``W_VALIDATION_MODEL_MISMATCH``) de §2.6."""
    entries: list[Entry] = []
    warnings: list[str] = []
    if det is not None:
        env_model = env.get(_MODEL_ENV)
        if det.tool == "claude-code" and session is not None and session.model and valid_model(session.model):
            model, msrc = session.model, f"hook:{session.event or 'SessionStart'}"
        elif env_model and valid_model(env_model):
            model, msrc = env_model, f"env:{_MODEL_ENV}"
        else:
            model, msrc = UNKNOWN_MODEL, "none"
        entries.append(Entry(det.tool, model, det.source, msrc))
    if declared_tool or declared_model:
        tool = declared_tool or (det.tool if det is not None else "other")
        entry = Entry(tool, declared_model or UNKNOWN_MODEL, "declared", "declared")
        # REQ-TRACE-17: se conservan las dos y se avisa. Solo si la sesión registró un modelo: sin
        # registro (unknown) no hay de qué diferir, aunque el pseudocódigo de §2.6 no lo distinga.
        recorded = entries[0].model if entries else UNKNOWN_MODEL
        if det is not None and tool == det.tool and recorded != UNKNOWN_MODEL and entry.model not in (
            UNKNOWN_MODEL, recorded,
        ):
            warnings.append("W_VALIDATION_MODEL_MISMATCH")
        entries.append(entry)
    return _normalize(entries), warnings


def trailer_block(
    *,
    task: int | None,
    sections: Iterable[str],
    ai_use: Sequence[str],
    assisted: Iterable[Entry],
) -> str:
    """Párrafo de trailers de §2.1, con fin de línea LF y sin línea en blanco al final.

    Con ``AI-Use: none`` no lleva ``Assisted-by`` (REQ-TRACE-6). ``ValueError`` si ``ai_use``
    no es una declaración válida.
    """
    uses = canonical_uses(ai_use)
    lines: list[str] = []
    if task is not None:
        lines.append(f"Task: #{task}")
    lines.extend(f"Section: {s}" for s in sorted(set(sections)))  # orden ASCII (REQ-TRACE-4)
    lines.append("AI-Use: " + ", ".join(uses))
    if uses != ("none",):
        values = sorted({(e.tool, e.model) for e in assisted})
        lines.extend(f"Assisted-by: {tool}:{model}" for tool, model in values)
    return "\n".join(lines) + "\n"


def commit_message(text: str, block: str) -> str:
    """Mensaje de un commit del núcleo: el texto de la persona y el párrafo de trailers al final
    (REQ-TRACE-2), separados por una línea en blanco y con LF."""
    body = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return f"{body}\n\n{block}"


# -- lectura (§2.10) ---------------------------------------------------------------------------

_TRAILER_LINE = re.compile(r"([A-Za-z0-9][A-Za-z0-9-]*):[ \t]*(.*)")
_SOLO_KEYS = frozenset({"co-authored-by", "claude-session"})  # REQ-TRACE-30
_EMAIL = re.compile(r"<([^<>\s]+@[^<>\s]+)>\s*$")
_SECTION = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
ANTHROPIC_NOREPLY = "noreply@anthropic.com"  # REQ-TRACE-29


@dataclass(frozen=True)
class Trailers:
    """Trailers de un mensaje, ya validados; lo que no cumple la gramática se ignora."""

    task: int | None
    sections: tuple[str, ...]
    ai_use: tuple[str, ...] | None  # None: sin AI-Use válido (cuenta como undeclared, §2.5)
    assisted: tuple[tuple[str, str], ...]  # (tool, model)
    co_authors: tuple[str, ...]  # correos de Co-authored-by, en minúsculas
    signed_off: tuple[str, ...]


def _paragraphs(text: str) -> list[list[str]]:
    out: list[list[str]] = []
    current: list[str] = []
    for line in text.split("\n"):
        if line.strip():
            current.append(line.rstrip())
        elif current:
            out.append(current)
            current = []
    if current:
        out.append(current)
    return out


def _pairs(lines: Sequence[str]) -> list[tuple[str, str]] | None:
    """``(clave en minúsculas, valor)`` si todas las líneas son trailers; si no, ``None``."""
    pairs: list[tuple[str, str]] = []
    for line in lines:
        m = _TRAILER_LINE.fullmatch(line)
        if m is None:
            return None
        pairs.append((m.group(1).lower(), m.group(2).strip()))
    return pairs


def _block(message: str, *, subject: bool) -> list[str]:
    """Las líneas del bloque de trailers (REQ-TRACE-30); vacío si el último párrafo no lo es.

    Con ``subject`` (un mensaje de commit), el primer párrafo es el asunto y nunca es el bloque;
    el cuerpo de un PR no tiene asunto.
    """
    paragraphs = _paragraphs(message.replace("\r\n", "\n").replace("\r", "\n"))
    if len(paragraphs) < (2 if subject else 1):
        return []
    last = _pairs(paragraphs[-1])
    if last is None:
        return []
    if (last and all(k in _SOLO_KEYS for k, _ in last) and len(paragraphs) > (2 if subject else 1)
            and _pairs(paragraphs[-2]) is not None):
        return paragraphs[-2] + paragraphs[-1]
    return paragraphs[-1]


def parse_trailers(message: str, *, subject: bool = True) -> Trailers:
    """Trailers del mensaje según REQ-TRACE-30 y REQ-TRACE-31.

    El bloque es el último párrafo si está formado solo por trailers; si ese párrafo solo trae
    ``Co-authored-by`` o ``Claude-Session`` y el anterior es de trailers, se unen. Las claves no
    distinguen mayúsculas, las desconocidas se ignoran y ``---`` no es un separador. Sin
    ``subject`` (el cuerpo de un PR), el primer párrafo también puede ser el bloque.
    """
    pairs = _pairs(_block(message, subject=subject)) or []
    task: int | None = None
    sections: list[str] = []
    uses: tuple[str, ...] | None = None
    assisted: list[tuple[str, str]] = []
    co_authors: list[str] = []
    signed: list[str] = []
    for key, value in pairs:
        if key == "task" and task is None and value.startswith("#") and value[1:].isdigit() and value[1:2] != "0":
            task = int(value[1:])
        elif key == "section" and _SECTION.fullmatch(value) and value not in sections:
            sections.append(value)
        elif key == "ai-use" and uses is None:
            try:
                uses = canonical_uses(u.strip() for u in value.split(","))
            except ValueError:
                uses = None
        elif key == "assisted-by":
            tool, sep, model = value.partition(":")  # el primer ':' separa (§2.2)
            if sep and valid_tool(tool) and valid_model(model) and (tool, model) not in assisted:
                assisted.append((tool, model))
        elif key == "co-authored-by":
            m = _EMAIL.search(value)
            if m:
                co_authors.append(m.group(1).lower())
        elif key == "signed-off-by":
            signed.append(value)
    return Trailers(task, tuple(sections), uses, tuple(assisted), tuple(co_authors), tuple(signed))


# -- validación (§2.10, REQ-TRACE-32) ----------------------------------------------------------

TrailerContext = Literal["branch-commit", "pr-body", "squash-commit"]
TRAILER_SYNTAX = "E_VALIDATION_TRAILER_SYNTAX"
TRAILER_MISSING = "E_VALIDATION_TRAILER_MISSING"
# Las claves del bloque con la grafía de la tabla de §2.1, en el orden de REQ-TRACE-2.
TRAILER_KEYS: tuple[str, ...] = ("Task", "Section", "AI-Use", "Assisted-by", "Signed-off-by")
_RANK = {k.lower(): i for i, k in enumerate(TRAILER_KEYS)}
_ONCE = frozenset({"task", "ai-use", "signed-off-by"})
_TASK_VALUE = re.compile(r"#[1-9][0-9]*")
_USES_VALUE = re.compile(r"[a-z]+(?:, [a-z]+)*")
# name: sin "<", ">" ni controles; email: ASCII visible sin "<" ni ">", con "@" (§2.2).
_SIGNOFF = re.compile(r"([^<>\x00-\x1f\x7f]+) <([\x21-\x3b\x3d\x3f-\x7e]+)>")


@dataclass(frozen=True)
class TrailerProblem:
    """Una falta de ``validate_trailers``: el código de hallazgo, la clave y el motivo."""

    code: str  # TRAILER_SYNTAX o TRAILER_MISSING
    key: str  # con la grafía de TRAILER_KEYS
    reason: str  # separator, key_order, repeated, grammar, unknown, duplicate, solo, use_order,
    # with_none, missing, other_task o undeclared


@dataclass(frozen=True)
class TrailerCheck:
    """Las faltas del bloque y lo que se leyó de él."""

    problems: tuple[TrailerProblem, ...]
    trailers: Trailers  # la lectura tolerante de parse_trailers
    signed_off: str | None  # el valor del único Signed-off-by con la gramática de §2.2


def parse_signoff(value: str) -> tuple[str, str] | None:
    """``(nombre, correo)`` de un valor de ``Signed-off-by`` con la gramática de §2.2."""
    m = _SIGNOFF.fullmatch(value)
    return (m.group(1), m.group(2)) if m is not None and "@" in m.group(2) else None


def validate_trailers(message: str, context: TrailerContext, *, task: int | None = None,
                      subject: bool = True) -> TrailerCheck:
    """REQ-TRACE-32: las faltas del bloque de trailers de ``message`` en ``context``.

    En todos los contextos: ``AI-Use`` obligatorio, ``Task: #<task>`` si se da ``task`` (la rama
    es ``task/<n>``) y la gramática de §2.2, con las claves conocidas en el orden de REQ-TRACE-2,
    sin repetir ``Task``, ``AI-Use`` ni ``Signed-off-by``, los usos de REQ-TRACE-5 y sin
    ``Assisted-by`` con ``AI-Use: none`` (REQ-TRACE-6). En ``pr-body`` y ``squash-commit``,
    además, REQ-TRACE-7 y ``Signed-off-by``; lo que falta es ``TRAILER_MISSING``. Las claves
    se comparan sin distinguir mayúsculas y las desconocidas se ignoran (REQ-TRACE-31).
    """
    problems: list[TrailerProblem] = []
    seen: dict[str, int] = {}
    highest = -1
    task_value: int | None = None
    uses: tuple[str, ...] | None = None
    assisted = 0
    signed: list[str] = []
    for line in _block(message, subject=subject):
        m = _TRAILER_LINE.fullmatch(line)
        if m is None:
            continue
        key = m.group(1).lower()
        if key not in _RANK:
            continue
        name = TRAILER_KEYS[_RANK[key]]

        def bad(reason: str, name: str = name) -> None:
            problems.append(TrailerProblem(TRAILER_SYNTAX, name, reason))

        rest = line[len(m.group(1)) + 1:]
        if rest.startswith(" ") and rest[1:2] not in (" ", "\t") and rest.strip():
            value = rest[1:]
        else:
            bad("separator")  # REQ-TRACE-3: ": " y el valor
            value = rest.strip()
        if _RANK[key] < highest:
            bad("key_order")
        highest = max(highest, _RANK[key])
        seen[key] = seen.get(key, 0) + 1
        if key in _ONCE and seen[key] == 2:
            bad("repeated")
        if key == "task":
            if not _TASK_VALUE.fullmatch(value):
                bad("grammar")
            elif task_value is None:
                task_value = int(value[1:])
        elif key == "section":
            if not _SECTION.fullmatch(value):
                bad("grammar")
        elif key == "ai-use":
            if not _USES_VALUE.fullmatch(value):
                bad("grammar")
                continue
            given = value.split(", ")
            try:
                canon = canonical_uses(given)
            except ValueError as exc:  # unknown, duplicate o solo (REQ-TRACE-5)
                bad(str(exc))
                continue
            if tuple(given) != canon:
                bad("use_order")
            elif uses is None:
                uses = canon
        elif key == "assisted-by":
            tool, sep, model = value.partition(":")  # el primer ':' separa (§2.2)
            if sep and valid_tool(tool) and valid_model(model):
                assisted += 1
            else:
                bad("grammar")
        elif parse_signoff(value) is None:
            bad("grammar")
        else:
            signed.append(value)
    if not seen.get("ai-use"):
        problems.append(TrailerProblem(TRAILER_MISSING, "AI-Use", "missing"))
    if task is not None:
        if not seen.get("task"):
            problems.append(TrailerProblem(TRAILER_MISSING, "Task", "missing"))
        elif task_value is not None and task_value != task:
            problems.append(TrailerProblem(TRAILER_MISSING, "Task", "other_task"))
    if uses == ("none",) and assisted:
        problems.append(TrailerProblem(TRAILER_SYNTAX, "Assisted-by", "with_none"))  # REQ-TRACE-6
    if context != "branch-commit":
        if uses == ("undeclared",):
            problems.append(TrailerProblem(TRAILER_MISSING, "AI-Use", "undeclared"))  # REQ-TRACE-7
        elif uses is not None and uses != ("none",) and not assisted:
            problems.append(TrailerProblem(TRAILER_MISSING, "Assisted-by", "missing"))
        if not seen.get("signed-off-by"):
            problems.append(TrailerProblem(TRAILER_MISSING, "Signed-off-by", "missing"))
    return TrailerCheck(tuple(problems), parse_trailers(message, subject=subject),
                        signed[0] if len(signed) == 1 else None)
