# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Analizador de ``plan.md`` (03-esquemas §5; analizador de referencia de §5.6).

``parse_plan`` transcribe el analizador de referencia de 03 §5.6: mismas
expresiones (``LOOKS``, en una forma equivalente de tiempo lineal), mismo recorrido
línea a línea y la misma salida por elemento (``PlanItem.to_dict`` da el
diccionario de 03 §5.6). Los errores se devuelven
como ``PlanError``, una tupla con nombre que es igual a la tupla
``(código, línea, valor)`` del código de referencia.

Además, este módulo reúne las reglas puras sobre el plan que usa la regla
``plan-ids``: las referencias dentro del mismo archivo (REQ-SCHEMA-38) y las
reglas entre versiones base -> head (REQ-SCHEMA-40 a REQ-SCHEMA-43).

Solo biblioteca estándar: la capa ``domain`` no importa nada más (01 §3.1).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal, NamedTuple

__all__ = [
    "DEC_ID",
    "DEF",
    "KINDS",
    "LOOKS",
    "PLAN_ID",
    "REF",
    "PlanChange",
    "PlanError",
    "PlanItem",
    "compare_plans",
    "id_kind",
    "id_number",
    "index",
    "parse_plan",
    "unknown_refs",
]

Status = Literal["active", "retired"]

# Prefijos de tipo de elemento (00-marco §5.9), en el orden de la gramática de 03 §5.2.
KINDS: Final[tuple[str, ...]] = ("RQ", "HYP", "IN", "OUT", "METH")

# Expresiones de 03 §5.6, copiadas literalmente salvo LOOKS (ver abajo).
PLAN_ID: Final[str] = r"(?:RQ|HYP|IN|OUT|METH)-[1-9][0-9]{0,3}"
DEC_ID: Final[str] = r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[1-9][0-9]*-[a-z0-9]+(?:-[a-z0-9]+)*"
DEF: Final[re.Pattern[str]] = re.compile(
    rf"^- (?P<id>{PLAN_ID})(?P<ret> \(retired(?:: (?P<dec>{DEC_ID}))?\))?: (?P<text>\S.*)$"
)
# LOOKS de 03 §5.6 con cuantificadores posesivos (``*+``, ``++``). Con ``match`` acepta las
# mismas líneas: cada tramo de espacios o de marcas va seguido de un carácter que no puede
# absorber, así que solo su versión más larga lleva a una coincidencia. La de la referencia
# repartía un tramo de espacios entre dos ``\s*`` de todas las formas posibles antes de
# fallar, cuadrática en su longitud (09 REQ-CI-24: tiempo acotado en archivos de hasta 5 MB).
LOOKS: Final[re.Pattern[str]] = re.compile(
    r"^\s*+(?:[-*+]\s*+[*_~]*+\s*+(?i:RQ|HYP|IN|OUT|METH)\s*+-\s*+[0-9]+"
    r"|[#*_~]*+\s*+(?:RQ|HYP|IN|OUT|METH)-[0-9]++\s*+[:.)])"
)
REF: Final[re.Pattern[str]] = re.compile(rf"\b{PLAN_ID}\b")

# Códigos de problema de 03 §10 que produce este módulo.
SYNTAX_CODE: Final[str] = "E_VALIDATION_PLAN_SYNTAX"
DUPLICATE_CODE: Final[str] = "E_VALIDATION_DUPLICATE_ID"
REMOVED_CODE: Final[str] = "E_VALIDATION_PLAN_ID_REMOVED"
RETIRED_CHANGED_CODE: Final[str] = "E_VALIDATION_PLAN_RETIRED_CHANGED"
REUSED_CODE: Final[str] = "E_VALIDATION_PLAN_ID_REUSED"
TEXT_CHANGED_CODE: Final[str] = "W_VALIDATION_PLAN_TEXT_CHANGED"


@dataclass(frozen=True, slots=True)
class PlanItem:
    """Un elemento definido en ``plan.md`` (salida de 03 §5.6)."""

    id: str
    kind: str
    status: Status
    retired_by: str | None
    text: str
    detail: str
    refs: tuple[str, ...]
    line: int

    @property
    def number(self) -> int:
        return id_number(self.id)

    def to_dict(self) -> dict[str, object]:
        """Diccionario con las claves y el orden del ejemplo de 03 §5.6."""
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "retired_by": self.retired_by,
            "text": self.text,
            "detail": self.detail,
            "refs": list(self.refs),
            "line": self.line,
        }


class PlanError(NamedTuple):
    """Problema de sintaxis o duplicado: ``(código, línea, valor)`` como en 03 §5.6.

    ``value`` es la línea sin espacios en los extremos para ``E_VALIDATION_PLAN_SYNTAX``
    y el ID repetido para ``E_VALIDATION_DUPLICATE_ID``.
    """

    code: str
    line: int
    value: str


class PlanChange(NamedTuple):
    """Hallazgo de las reglas entre versiones (03 §5.4).

    ``line`` es la línea del elemento en head (``None`` si el ID ya no está) y
    ``max_number`` el máximo del prefijo en la base (solo en ``E_VALIDATION_PLAN_ID_REUSED``).
    """

    code: str
    id: str
    line: int | None
    max_number: int | None = None


@dataclass(slots=True)
class _Draft:
    """Elemento en construcción mientras se recorre el archivo (el detalle crece).

    Las líneas del detalle se unen al final: concatenarlas una a una copiaba el
    detalle acumulado en cada línea, cuadrático en un plan.md de hasta 5 MB
    (09 REQ-CI-24).
    """

    id: str
    kind: str
    line: int
    status: Status
    retired_by: str | None
    text: str
    detail: list[str] = field(default_factory=list)


def id_kind(plan_id: str) -> str:
    """Prefijo de tipo de un ID (``RQ-3`` -> ``RQ``)."""
    return plan_id.split("-", 1)[0]


def id_number(plan_id: str) -> int:
    """Número de un ID (``RQ-3`` -> ``3``); ``ValueError`` si no es un ID del plan."""
    if not re.fullmatch(PLAN_ID, plan_id):
        raise ValueError(f"no es un ID del plan: {plan_id!r}")
    return int(plan_id.rsplit("-", 1)[1])


def parse_plan(text: str) -> tuple[list[PlanItem], list[PlanError]]:
    """Elementos y problemas de ``plan.md``; transcripción de ``parse_plan`` de 03 §5.6.

    Recibe el texto ya decodificado. Como en la referencia, las líneas se separan
    solo por ``\\n``: quien llama normaliza el BOM y los CRLF si hace falta.
    """
    items: dict[str, _Draft] = {}
    errors: list[PlanError] = []
    fence: str | None = None
    in_comment = False
    cur: _Draft | None = None
    for no, line in enumerate(text.split("\n"), 1):
        s = line.strip()
        if in_comment:
            in_comment = "-->" not in line
            continue
        if fence:
            fence = None if s.startswith(fence) else fence
            continue
        if s.startswith(("```", "~~~")):
            fence, cur = s[:3], None
            continue
        if s.startswith("<!--"):
            in_comment = "-->" not in s[4:]
            continue
        if m := DEF.match(line):
            i = m["id"]
            if i in items:
                errors.append(PlanError(DUPLICATE_CODE, no, i))
                cur = None
                continue
            t, retired = m["text"].strip(), m["ret"] is not None
            if retired and len(t) > 4 and t.startswith("~~") and t.endswith("~~"):
                t = t[2:-2].strip()
            cur = items[i] = _Draft(
                id=i,
                kind=i.split("-")[0],
                line=no,
                status="retired" if retired else "active",
                retired_by=m["dec"],
                text=t,
            )
            continue
        if LOOKS.match(line):
            errors.append(PlanError(SYNTAX_CODE, no, s))
            cur = None
            continue
        if cur is not None and line.startswith("  ") and s:
            cur.detail.append(s)
            continue
        cur = None
    out: list[PlanItem] = []
    for it in items.values():
        detail = "\n".join(it.detail)
        refs = sorted(set(REF.findall(it.text + "\n" + detail)) - {it.id})
        out.append(
            PlanItem(
                id=it.id,
                kind=it.kind,
                status=it.status,
                retired_by=it.retired_by,
                text=it.text,
                detail=detail,
                refs=tuple(refs),
                line=it.line,
            )
        )
    return out, errors


def index(items: Iterable[PlanItem]) -> dict[str, PlanItem]:
    """``id -> PlanItem`` conservando el orden del archivo."""
    return {it.id: it for it in items}


def unknown_refs(items: Sequence[PlanItem]) -> list[tuple[PlanItem, str]]:
    """Pares ``(elemento, ID citado)`` cuyo ID no está definido en el mismo plan (REQ-SCHEMA-38)."""
    known = {it.id for it in items}
    return [(it, ref) for it in items for ref in it.refs if ref not in known]


def _max_by_kind(items: Iterable[PlanItem]) -> Mapping[str, int]:
    out: dict[str, int] = {}
    for it in items:
        out[it.kind] = max(out.get(it.kind, 0), it.number)
    return out


def compare_plans(base: Sequence[PlanItem], head: Sequence[PlanItem]) -> list[PlanChange]:
    """Reglas entre versiones de 03 §5.4, de la base a head.

    - REQ-SCHEMA-40: un ID de la base que falta en head -> ``E_VALIDATION_PLAN_ID_REMOVED``.
    - REQ-SCHEMA-41: un retirado de la base sigue retirado, con el mismo texto y detalle;
      la decisión solo puede agregarse si no tenía -> ``E_VALIDATION_PLAN_RETIRED_CHANGED``.
    - REQ-SCHEMA-42: un ID nuevo tiene un número mayor que el máximo de su prefijo en la
      base, contando los retirados -> ``E_VALIDATION_PLAN_ID_REUSED``.
    - REQ-SCHEMA-43: un activo de la base cuyo texto o detalle cambió ->
      ``W_VALIDATION_PLAN_TEXT_CHANGED`` (también si en head se retira con otro texto).

    El orden de salida es el de la base y luego el de los IDs nuevos en head.
    """
    b = index(base)
    h = index(head)
    out: list[PlanChange] = []
    for plan_id, old in b.items():
        new = h.get(plan_id)
        if new is None:
            out.append(PlanChange(REMOVED_CODE, plan_id, None))
            continue
        same_content = old.text == new.text and old.detail == new.detail
        if old.status == "retired":
            decision_ok = new.retired_by == old.retired_by or old.retired_by is None
            if new.status != "retired" or not same_content or not decision_ok:
                out.append(PlanChange(RETIRED_CHANGED_CODE, plan_id, new.line))
        elif not same_content:
            out.append(PlanChange(TEXT_CHANGED_CODE, plan_id, new.line))
    maxima = _max_by_kind(b.values())
    for plan_id, new in h.items():
        if plan_id in b:
            continue
        top = maxima.get(new.kind, 0)
        if new.number <= top:
            out.append(PlanChange(REUSED_CODE, plan_id, new.line, top))
    return out
