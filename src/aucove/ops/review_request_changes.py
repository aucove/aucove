# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove review request-changes <pr> (--body <texto> | --body-file <f>)`` (05 §9.24).

Solo humana (D-09), sin MCP. El flujo es ``reviews.decide`` con el evento ``REQUEST_CHANGES``:
lo pide un aprobador que cuenta o el dueño de una parte tocada (06 §2.3), con el reto
``request-changes <pr>``. El cuerpo es obligatorio; si falta, la CLI lo pide en la terminal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .reviews import DECISION_STEPS, decide, render_decision

if TYPE_CHECKING:
    from ..i18n import Translator
    from .context import OpContext
    from .result import Result

__all__ = ["OP", "STEPS", "render_text", "run"]

OP = "review.request-changes"
STEPS = DECISION_STEPS


def run(ctx: OpContext, *, pr: object = None, body: object = None, body_file: object = None) -> Result:
    """``review request-changes <pr> (--body <texto> | --body-file <f>)``."""
    del body_file  # la CLI ya lo convirtió en body
    return decide(ctx, op=OP, event="REQUEST_CHANGES", pr=pr, body=body)


def render_text(result: Result, t: Translator, *, fmt: str = "text") -> str:
    del fmt
    return render_decision(result, t)
