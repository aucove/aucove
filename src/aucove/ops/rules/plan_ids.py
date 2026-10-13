# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Regla ``plan-ids`` de ``validate`` (09-ci §4.1; 03-esquemas §5).

Sobre el ``plan.md`` del árbol validado (``RuleInput.tree``):

- sintaxis e IDs duplicados del analizador de 03 §5.6 (REQ-SCHEMA-36 y REQ-SCHEMA-37);
- IDs citados en el texto o el detalle que no existen en el mismo plan (REQ-SCHEMA-38);
  un elemento activo que cita uno retirado da el aviso ``W_VALIDATION_PLAN_REF_RETIRED``;
- la decisión de un retiro existe en ``memory/decisions/`` de la base o del propio árbol
  (REQ-SCHEMA-39).

Y, si hay ``base_tree`` (el merge-base en ``pr``, ``origin/<base>`` en ``tree`` y el
primer padre en ``base``), las reglas entre versiones de 03 §5.4 (REQ-SCHEMA-40 a 43).
Con ``base_tree`` ``None`` (commit raíz) no hay comparación entre versiones y esa parte
pasa (00-marco D-33).
"""

from __future__ import annotations

from collections.abc import Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING

from ...domain import plan as dplan
from ...domain.findings import Finding, Severity
from ..result import clip
from .base import RuleInput, RuleSpec, make_finding, unavailable

if TYPE_CHECKING:
    from ...vcs.tree import TreeView

RULE_ID = "plan-ids"
PLAN_FILE = "plan.md"
DECISIONS_PREFIX = "memory/decisions/"
MAX_QUOTED = 160  # caracteres de la línea citada en E_VALIDATION_PLAN_SYNTAX

# Código de hallazgo -> marcadores de finding.<code>.*.
# E_VALIDATION_DUPLICATE_ID es de config.toml y usa {id}; el resto vive en convention.toml.
CODES = MappingProxyType(
    {
        dplan.SYNTAX_CODE: frozenset({"text"}),
        dplan.DUPLICATE_CODE: frozenset({"id"}),
        "E_VALIDATION_UNKNOWN_PLAN_ID": frozenset({"id"}),
        "E_VALIDATION_UNKNOWN_DECISION": frozenset({"id"}),
        dplan.REMOVED_CODE: frozenset({"id"}),
        dplan.RETIRED_CHANGED_CODE: frozenset({"id"}),
        dplan.REUSED_CODE: frozenset({"id", "max"}),
        dplan.TEXT_CHANGED_CODE: frozenset({"id"}),
        "W_VALIDATION_PLAN_REF_RETIRED": frozenset({"id"}),
    }
)


def _severity(code: str) -> Severity:
    return "warning" if code.startswith("W_") else "error"


def _normalize(text: str) -> str:
    """Sin BOM y con LF: la codificación la juzga ``conv.file.encoding``, no esta regla."""
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


class _Unreadable(Exception):
    """``plan.md`` existe pero no se puede leer como texto UTF-8 de hasta 5 MiB."""


def _read_plan(tree: TreeView) -> str | None:
    """Texto de ``plan.md`` del árbol, ``None`` si no existe; ``_Unreadable`` si no se lee."""
    if PLAN_FILE not in tree.entries():
        return None
    text = tree.read_text(PLAN_FILE)
    if text is None:
        raise _Unreadable(PLAN_FILE)
    return _normalize(text)


def _known_decisions(inp: RuleInput) -> frozenset[str]:
    """IDs de decisión (nombre sin ``.md``) de la base y del propio árbol (REQ-SCHEMA-39)."""
    paths: set[str] = set()
    if inp.policy is not None:
        paths.update(inp.policy.decisions)
    for view in (inp.base_tree, inp.tree):
        if view is not None:
            paths.update(view.entries())
    out: set[str] = set()
    for p in paths:
        if p.startswith(DECISIONS_PREFIX) and p.endswith(".md"):
            name = p[len(DECISIONS_PREFIX) : -len(".md")]
            if name and "/" not in name:
                out.add(name)
    return frozenset(out)


def _run(inp: RuleInput) -> Sequence[Finding]:
    rule = RULE
    if inp.tree is None:
        return [unavailable(rule, inp, inp.t("convention.reason.no_tree"))]
    try:
        head_text = _read_plan(inp.tree)
    except _Unreadable:
        return [unavailable(rule, inp, inp.t("convention.reason.plan_unreadable"), path=PLAN_FILE)]
    out: list[Finding] = []

    def add(code: str, line: int | None, **params: str) -> None:
        out.append(make_finding(rule, inp, code, severity=_severity(code), path=PLAN_FILE, line=line, **params))

    head_items: list[dplan.PlanItem] = []
    if head_text is not None:
        head_items, errors = dplan.parse_plan(head_text)
        for err in errors:
            if err.code == dplan.DUPLICATE_CODE:
                add(err.code, err.line, id=err.value)
            else:
                add(err.code, err.line, text=clip(err.value, MAX_QUOTED))
        by_id = dplan.index(head_items)
        for item, ref in dplan.unknown_refs(head_items):
            add("E_VALIDATION_UNKNOWN_PLAN_ID", item.line, id=ref)
        for item in head_items:
            if item.status != "active":
                continue
            for ref in item.refs:
                target = by_id.get(ref)
                if target is not None and target.status == "retired":
                    add("W_VALIDATION_PLAN_REF_RETIRED", item.line, id=ref)
        decisions = _known_decisions(inp)
        for item in head_items:
            if item.retired_by is not None and item.retired_by not in decisions:
                add("E_VALIDATION_UNKNOWN_DECISION", item.line, id=item.retired_by)
    if inp.base_tree is not None:
        try:
            base_text = _read_plan(inp.base_tree)
        except _Unreadable:
            return [*out, unavailable(rule, inp, inp.t("convention.reason.base_plan_unreadable"), path=PLAN_FILE)]
        base_items = dplan.parse_plan(base_text)[0] if base_text is not None else []
        for change in dplan.compare_plans(base_items, head_items):
            if change.code == dplan.REUSED_CODE:
                add(change.code, change.line, id=change.id, max=str(change.max_number))
            else:
                add(change.code, change.line, id=change.id)
    return out


RULE = RuleSpec(
    id=RULE_ID,
    modes=frozenset({"tree", "pr", "base"}),
    run=_run,
    codes=CODES,
)

RULES: tuple[RuleSpec, ...] = (RULE,)
