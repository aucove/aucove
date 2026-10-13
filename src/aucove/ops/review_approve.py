# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove review approve <pr> [--body <texto>]`` (05 §9.24; 06-permisos §3.2 y §6).

Solo humana (D-09), sin MCP. El flujo es ``reviews.decide`` con el evento ``APPROVE``: lo
aprueba un aprobador que cuenta (06 §3.2), con el reto ``approve <pr>`` y sobre la cabeza que
se mostró. No integra: devuelve ``merge_url`` para integrar en la web (REQ-OPS-46).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .reviews import DECISION_STEPS, decide, render_decision

if TYPE_CHECKING:
    from ..i18n import Translator
    from .context import OpContext
    from .result import Result

__all__ = ["OP", "STEPS", "render_text", "run"]

OP = "review.approve"
STEPS = DECISION_STEPS


def run(ctx: OpContext, *, pr: object = None, body: object = None, body_file: object = None) -> Result:
    """``review approve <pr> [--body <texto> | --body-file <f>]``."""
    del body_file  # la CLI ya lo convirtió en body
    return decide(ctx, op=OP, event="APPROVE", pr=pr, body=body)


def render_text(result: Result, t: Translator, *, fmt: str = "text") -> str:
    del fmt
    return render_decision(result, t)
