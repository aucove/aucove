# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Tipos y ayudas comunes de las reglas de ``validate`` (09-ci §4.1).

Cada módulo de ``RULE_MODULES`` exporta ``RULES: tuple[RuleSpec, ...]``. Varios
``RuleSpec`` pueden compartir ``id`` (``convention``): el motor los une en un
solo ``results[]``. ``RuleSpec.codes`` es el registro de marcadores de los
hallazgos que emite cada regla: ``make_finding`` rechaza un código no declarado
o marcadores distintos, para que el error de contrato aparezca en las pruebas de
la regla que lo comete.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ...domain.findings import Finding, Severity, has_errors
from ...i18n import Translator
from ..result import MAX_TEXT, clip

if TYPE_CHECKING:
    from ...config.base import BaseSnapshot
    from ...forge.base import ForgeAdapter, PullRequest
    from ...vcs.tree import TreeView

Mode = Literal["tree", "pr", "base", "build-log"]
Status = Literal["pass", "fail"]
MODES: tuple[Mode, ...] = ("tree", "pr", "base", "build-log")

# Código y marcadores del hallazgo de una regla que no pudo evaluarse (texto en validate.toml).
UNAVAILABLE_CODE = "E_INTERNAL_UNEXPECTED"
UNAVAILABLE_PARAMS: frozenset[str] = frozenset({"rule", "reason"})


@dataclass(frozen=True, slots=True)
class Change:
    """Un archivo cambiado entre la base y el contenido validado."""

    path: str
    status: Literal["added", "modified", "removed", "renamed"]
    previous_path: str | None = None
    file_mode: str | None = None


@dataclass(frozen=True)
class RuleInput:
    """Entradas de una regla; las arma el motor según el modo."""

    mode: Mode
    t: Translator
    now: datetime
    policy: BaseSnapshot | None = None  # autorización: siempre la base (REQ-CI-25)
    tree: TreeView | None = None  # contenido validado; tree.root = checkout
    base_tree: TreeView | None = None
    changes: tuple[Change, ...] = ()
    pr: PullRequest | None = None
    head_ref: str | None = None
    head_sha: str | None = None
    base_ref: str | None = None
    base_sha: str | None = None
    forge: ForgeAdapter | None = None
    log_path: Path | None = None
    prior: Mapping[str, Status] = field(default_factory=dict)
    tools: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuleSpec:
    """Declaración de una regla de ``validate``."""

    id: str
    modes: frozenset[Mode]
    run: Callable[[RuleInput], Sequence[Finding]]
    codes: Mapping[str, frozenset[str]]  # código de hallazgo -> marcadores de finding.<code>.*
    needs_forge: bool = False
    after: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()


def make_finding(
    rule: RuleSpec,
    inp: RuleInput,
    code: str,
    *,
    severity: Severity,
    path: str | None,
    line: int | None = None,
    pointer: str | None = None,
    **params: object,
) -> Finding:
    """Hallazgo localizado con ``finding.<code>.message`` y ``finding.<code>.hint``.

    ``ValueError`` si ``code`` no está en ``rule.codes`` o ``set(params)`` difiere de
    sus marcadores declarados.
    """
    if code not in rule.codes:
        raise ValueError(f"la regla {rule.id!r} no declara el código {code!r}")
    expected = rule.codes[code]
    if set(params) != set(expected):
        raise ValueError(f"marcadores de {code} en {rule.id!r} distintos de {sorted(expected)}: {sorted(params)}")
    message = inp.t(f"finding.{code}.message", **params)
    hint = inp.t.get(f"finding.{code}.hint", **params)
    return Finding(
        check=rule.id,
        code=code,
        severity=severity,
        path=path,
        line=line,
        message=message,
        hint=clip(hint, MAX_TEXT) if hint is not None else None,
        pointer=pointer,
    )


def unavailable(rule: RuleSpec, inp: RuleInput, reason: str, *, path: str | None = None) -> Finding:
    """Hallazgo de error ``E_INTERNAL_UNEXPECTED`` con ``{rule, reason}``; ``reason`` ya localizado."""
    params = {"rule": rule.id, "reason": reason}
    message = inp.t(f"finding.{UNAVAILABLE_CODE}.message", **params)
    hint = inp.t.get(f"finding.{UNAVAILABLE_CODE}.hint", **params)
    return Finding(
        check=rule.id,
        code=UNAVAILABLE_CODE,
        severity="error",
        path=path,
        line=None,
        message=message,
        hint=clip(hint, MAX_TEXT) if hint is not None else None,
        pointer=None,
    )


def status_of(findings: Sequence[Finding]) -> Status:
    """``fail`` si hay algún hallazgo de severidad ``error``; si no, ``pass``."""
    return "fail" if has_errors(findings) else "pass"


# Orden de results[] = orden de primera aparición en esta lista.
RULE_MODULES: tuple[str, ...] = (
    "ops.rules.scope",
    "ops.rules.schemas",
    "ops.rules.roles_sync",
    "ops.rules.decisions",
    "ops.rules.plan_ids",
    "ops.rules.latex",
    "ops.rules.instructions",
    "ops.rules.secrets",
    "ops.rules.traceability",
    "ops.rules.ci_policy",
    "ops.rules.coordinator",
    "ops.rules.convention",
    "ops.rules.latex_log",
)
