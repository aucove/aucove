# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Regla ``decisions-immutable`` (09-ci §4.1; 02-convencion REQ-CONV-67; 03 §6.1).

En ``memory/decisions/`` un cambio solo puede agregar archivos. Un archivo
``modified``, ``removed`` o ``renamed`` (también un renombre que entra o sale de la
carpeta) es ``E_VALIDATION_DECISION_IMMUTABLE`` sin importar el autor. En modo ``pr``
también falla un archivo ``added`` cuya ruta ya existe en la política de la base,
porque reemplazaría un registro integrado (el diff contra el merge-base no lo ve).

En un commit raíz del modo ``base`` no hay cambios y la regla pasa (D-33).
"""

from __future__ import annotations

from collections.abc import Sequence

from ...domain.findings import Finding
from .base import Change, RuleInput, RuleSpec, make_finding

RULE_ID = "decisions-immutable"
CODE = "E_VALIDATION_DECISION_IMMUTABLE"
DECISIONS_PREFIX = "memory/decisions/"


def in_decisions(path: str | None) -> bool:
    """``True`` si ``path`` está bajo ``memory/decisions/`` (sin distinguir mayúsculas)."""
    return bool(path) and str(path).lower().startswith(DECISIONS_PREFIX)


def _finding(inp: RuleInput, path: str, change: str) -> Finding:
    return make_finding(RULE, inp, CODE, severity="error", path=path, change=change)


def _run(inp: RuleInput) -> Sequence[Finding]:
    existing = {p.lower() for p in inp.policy.decisions} if inp.policy is not None and inp.mode == "pr" else set()
    out: list[Finding] = []
    for ch in sorted(inp.changes, key=lambda c: (c.path, c.previous_path or "")):
        if not (in_decisions(ch.path) or in_decisions(ch.previous_path)):
            continue
        if ch.status == "added":
            if ch.path.lower() in existing:
                out.append(_finding(inp, ch.path, inp.t("validate.decisions.overwrite")))
            continue
        path = ch.path if in_decisions(ch.path) else str(ch.previous_path)
        out.append(_finding(inp, path, inp.t(f"validate.decisions.status.{_status(ch)}")))
    return out


def _status(ch: Change) -> str:
    return ch.status if ch.status in ("modified", "removed", "renamed") else "modified"


RULE = RuleSpec(
    id=RULE_ID,
    modes=frozenset({"pr", "base"}),
    run=_run,
    codes={CODE: frozenset({"change"})},
)

RULES: tuple[RuleSpec, ...] = (RULE,)
