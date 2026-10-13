# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Cuerpo del Issue de tarea: análisis y forma canónica (03-esquemas §8.2 y §8.3).

``parse_task_body`` es la transcripción de 03 §8.3: devuelve el ``TaskSpec`` como un
dict (``summary``, ``sections``, ``plan_refs``, ``done`` y ``spec_hash``) y lanza
``TaskBodyError`` (el ``Problem`` de la spec) con un campo repetido. El cuerpo es dato no
confiable (REQ-SCHEMA-74): solo se analiza con expresiones regulares acotadas por línea.

Diferencias con el código de 03 §8.3, todas aditivas:

- Antes de analizar se quita un BOM inicial y se pasa ``\\r\\n`` a ``\\n``
  (REQ-SCHEMA-1 y REQ-SCHEMA-72): GitHub guarda los cuerpos con CRLF.
- Los avisos ``W_VALIDATION_TASK_UNKNOWN_FIELD`` no se imprimen: se devuelven como
  ``BodyIssue`` (``analyze_task_body``) o se agregan a la lista ``warnings``.
- ``check_task_spec`` aplica REQ-SCHEMA-69..71 y ``render_task_body`` escribe el cuerpo
  con las etiquetas de un idioma (REQ-SCHEMA-73: ``parse(render(spec)) == spec``).

Módulo puro (solo biblioteca estándar): no localiza textos; los códigos y los datos del
problema los traduce el llamador.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

__all__ = [
    "BODY_CODE",
    "CANON",
    "EMPTY",
    "FIELDS",
    "LABELS",
    "MAX_DONE",
    "MAX_SUMMARY",
    "NO_SECTIONS_CODE",
    "UNKNOWN_FIELD_CODE",
    "UNKNOWN_PLAN_ID_CODE",
    "UNKNOWN_SECTION_CODE",
    "BodyIssue",
    "TaskBody",
    "TaskBodyError",
    "analyze_task_body",
    "check_task_spec",
    "compute_spec_hash",
    "normalize_body",
    "parse_task_body",
    "render_task_body",
]

# Etiqueta del formulario -> id del campo (tabla canónica de 03 §8.3, es y en).
CANON: Final[Mapping[str, str]] = MappingProxyType(
    {
        "Resumen": "summary",
        "Partes": "sections",
        "Referencias al plan": "plan_refs",
        "Criterio de terminado": "done",
        "Summary": "summary",
        "Parts": "sections",
        "Plan references": "plan_refs",
        "Definition of done": "done",
        # Etiquetas anteriores a D-43: los Issues ya creados las siguen usando.
        "Secciones": "sections",
        "Sections": "sections",
    }
)
FIELDS: Final[tuple[str, ...]] = ("summary", "sections", "plan_refs", "done")
EMPTY: Final[frozenset[str]] = frozenset({"_no response_", "none"})  # REQ-SCHEMA-68

# Etiquetas por idioma para escribir el cuerpo (REQ-SCHEMA-73); son las de CANON.
LABELS: Final[Mapping[str, Mapping[str, str]]] = MappingProxyType(
    {
        "es": MappingProxyType(
            {
                "summary": "Resumen",
                "sections": "Partes",
                "plan_refs": "Referencias al plan",
                "done": "Criterio de terminado",
            }
        ),
        "en": MappingProxyType(
            {
                "summary": "Summary",
                "sections": "Parts",
                "plan_refs": "Plan references",
                "done": "Definition of done",
            }
        ),
    }
)

MAX_SUMMARY: Final[int] = 2000  # REQ-SCHEMA-69
MAX_DONE: Final[int] = 20

BODY_CODE: Final[str] = "E_VALIDATION_TASK_BODY"
UNKNOWN_FIELD_CODE: Final[str] = "W_VALIDATION_TASK_UNKNOWN_FIELD"
NO_SECTIONS_CODE: Final[str] = "W_VALIDATION_TASK_NO_SECTIONS"
UNKNOWN_SECTION_CODE: Final[str] = "E_VALIDATION_UNKNOWN_SECTION"
UNKNOWN_PLAN_ID_CODE: Final[str] = "E_VALIDATION_UNKNOWN_PLAN_ID"

_COMMENT_LINE: Final[re.Pattern[str]] = re.compile(r"^\s*<!--.*-->\s*$")
_HEADING: Final[re.Pattern[str]] = re.compile(r"^### (.+?)\s*$")
_SECTION_SPLIT: Final[re.Pattern[str]] = re.compile(r",\s*")
_PLAN_SPLIT: Final[re.Pattern[str]] = re.compile(r"[,\s]+")
_DONE_ITEM: Final[re.Pattern[str]] = re.compile(r"^- (\[[ xX]\] )?")
_EMPTY_VALUE: Final[str] = "_No response_"

Reason = Literal[
    "repeated_field",
    "text_before_fields",
    "summary_empty",
    "summary_too_long",
    "done_empty",
    "done_too_many",
    "no_sections",
    "unknown_section",
    "unknown_plan_id",
]


@dataclass(frozen=True, slots=True)
class BodyIssue:
    """Un problema del cuerpo: código de 03 §10 y dato variable (sin texto localizado)."""

    code: str
    reason: Reason
    field: str | None = None  # id del campo (summary, sections, plan_refs, done)
    value: str | None = None  # sección o ID del plan desconocidos
    line: int | None = None  # línea del cuerpo (desde 1), si aplica

    @property
    def severity(self) -> Literal["error", "warning"]:
        return "warning" if self.code.startswith("W_") else "error"


class TaskBodyError(ValueError):
    """``Problem`` de 03 §8.3: el cuerpo no se puede analizar (``E_VALIDATION_TASK_BODY``)."""

    def __init__(self, issue: BodyIssue) -> None:
        super().__init__(f"{issue.code}: {issue.reason} ({issue.field})")
        self.issue = issue

    @property
    def code(self) -> str:
        return self.issue.code


@dataclass(frozen=True)
class TaskBody:
    """Resultado de ``analyze_task_body``: ``spec`` es ``None`` si el cuerpo no es analizable."""

    spec: Mapping[str, object] | None
    issues: tuple[BodyIssue, ...]

    @property
    def sections(self) -> tuple[str, ...]:
        if self.spec is None:
            return ()
        return tuple(self.spec["sections"])  # type: ignore[arg-type]

    @property
    def spec_hash(self) -> str:
        if self.spec is None:
            return ""
        return str(self.spec["spec_hash"])


def normalize_body(body: str) -> str:
    """Quita un BOM inicial y pasa ``\\r\\n`` a ``\\n`` (REQ-SCHEMA-1)."""
    if body.startswith("﻿"):
        body = body[1:]
    return body.replace("\r\n", "\n")


def compute_spec_hash(spec: Mapping[str, object]) -> str:
    """``sha256:`` del JSON canónico de ``summary``, ``sections``, ``plan_refs`` y ``done`` (REQ-SCHEMA-72)."""
    core = {k: spec[k] for k in FIELDS}
    canonical = json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_task_body(
    body: str,
    form_labels: Mapping[str, str] | None = None,
    *,
    warnings: list[BodyIssue] | None = None,
) -> dict[str, object]:
    """``TaskSpec`` del cuerpo de un Issue de tarea (03 §8.3).

    ``form_labels`` va de la etiqueta del ``task.yml`` de la base al id del campo y se
    suma a la tabla canónica. Un encabezado repetido lanza ``TaskBodyError``; el texto no
    vacío anterior al primer campo se descarta con ``W_VALIDATION_TASK_UNKNOWN_FIELD``
    (REQ-SCHEMA-67), que se agrega a ``warnings`` si se pasa.
    """
    labels: dict[str, str] = {**CANON, **(form_labels or {})}
    fields: dict[str, list[str]] = {}
    cur: str | None = None
    for number, line in enumerate(normalize_body(body).split("\n"), start=1):
        if _COMMENT_LINE.match(line):  # marcadores (p. ej. el de op de 05-operaciones)
            continue
        m = _HEADING.match(line)
        if m and m[1] in labels:  # un "###" que no es una etiqueta conocida es contenido
            cur = labels[m[1]]
            if cur in fields:
                raise TaskBodyError(BodyIssue(BODY_CODE, "repeated_field", field=cur, line=number))
            fields[cur] = []
            continue
        if cur:
            fields[cur].append(line)
        elif line.strip() and warnings is not None:
            warnings.append(BodyIssue(UNKNOWN_FIELD_CODE, "text_before_fields", line=number))
    raw = {k: "\n".join(v).strip() for k, v in fields.items()}
    raw = {k: "" if v.lower() in EMPTY else v for k, v in raw.items()}
    spec: dict[str, object] = {
        "summary": raw.get("summary", ""),
        "sections": list(
            dict.fromkeys(x.strip() for x in _SECTION_SPLIT.split(raw.get("sections", "")) if x.strip())
        ),
        "plan_refs": list(dict.fromkeys(x.upper() for x in _PLAN_SPLIT.split(raw.get("plan_refs", "")) if x)),
    }
    done = raw.get("done", "")
    items = [_DONE_ITEM.sub("", ln.strip()) for ln in done.split("\n") if ln.strip().startswith("- ")]
    spec["done"] = items or ([done] if done else [])
    spec["spec_hash"] = compute_spec_hash(spec)
    return spec


def analyze_task_body(body: str, form_labels: Mapping[str, str] | None = None) -> TaskBody:
    """``parse_task_body`` sin excepciones: el problema o los avisos van en ``issues``."""
    warnings: list[BodyIssue] = []
    try:
        spec = parse_task_body(body, form_labels, warnings=warnings)
    except TaskBodyError as exc:
        return TaskBody(None, (*warnings, exc.issue))
    return TaskBody(MappingProxyType(spec), tuple(warnings))


def check_task_spec(
    spec: Mapping[str, object],
    *,
    sections: Iterable[str] | None = None,
    plan_ids: Iterable[str] | None = None,
) -> list[BodyIssue]:
    """Reglas de contenido del ``TaskSpec`` (REQ-SCHEMA-69, 70 y 71).

    ``sections`` son los ``sections[].id`` del ``roles.yaml`` de la base y ``plan_ids``
    los IDs vigentes de ``plan.md``; si alguno es ``None``, esa comprobación se omite.
    """
    out: list[BodyIssue] = []
    summary = str(spec.get("summary", ""))
    if not summary:
        out.append(BodyIssue(BODY_CODE, "summary_empty", field="summary"))
    elif len(summary) > MAX_SUMMARY:
        out.append(BodyIssue(BODY_CODE, "summary_too_long", field="summary"))
    done = _strings(spec.get("done"))
    if not done:
        out.append(BodyIssue(BODY_CODE, "done_empty", field="done"))
    elif len(done) > MAX_DONE:
        out.append(BodyIssue(BODY_CODE, "done_too_many", field="done"))
    declared = _strings(spec.get("sections"))
    if not declared:
        out.append(BodyIssue(NO_SECTIONS_CODE, "no_sections", field="sections"))
    elif sections is not None:
        known = frozenset(sections)
        out.extend(
            BodyIssue(UNKNOWN_SECTION_CODE, "unknown_section", field="sections", value=x)
            for x in declared
            if x not in known
        )
    if plan_ids is not None:
        known_ids = frozenset(x.upper() for x in plan_ids)
        out.extend(
            BodyIssue(UNKNOWN_PLAN_ID_CODE, "unknown_plan_id", field="plan_refs", value=x)
            for x in _strings(spec.get("plan_refs"))
            if x not in known_ids
        )
    return out


def render_task_body(spec: Mapping[str, object], lang: str = "es") -> str:
    """Cuerpo del Issue con el formato de 03 §8.2 y las etiquetas de ``lang`` (REQ-SCHEMA-73).

    Un campo vacío se escribe ``_No response_``, como GitHub.
    """
    labels = LABELS[lang]
    summary = str(spec.get("summary", "")) or _EMPTY_VALUE
    sections = ", ".join(_strings(spec.get("sections"))) or _EMPTY_VALUE
    plan_refs = ", ".join(_strings(spec.get("plan_refs"))) or _EMPTY_VALUE
    done_items = _strings(spec.get("done"))
    done = "\n".join(f"- {x}" for x in done_items) if done_items else _EMPTY_VALUE
    parts = [
        (labels["summary"], summary),
        (labels["sections"], sections),
        (labels["plan_refs"], plan_refs),
        (labels["done"], done),
    ]
    return "\n\n".join(f"### {label}\n\n{value}" for label, value in parts) + "\n"


def _strings(value: object) -> list[str]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        return []
    return [x for x in value if isinstance(x, str)]
