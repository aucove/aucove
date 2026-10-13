# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Reglas ``assessment`` y ``direction-ack``: el coordinador en el CI (09-ci §4.1; 04-tareas §8.3 y §9).

- ``assessment`` (aviso, ``E_VALIDATION_ASSESSMENT``): un PR de tarea sin evaluación de traslape
  para su cabeza actual, es decir, sin un marcador ``assessment`` de fase ``submission`` con ese
  ``head_sha`` en un comentario del autor del PR, que es el dueño del reclamo (REQ-TASK-61 y
  REQ-TASK-63). Un PR que no sale de ``task/<n>`` no se evalúa.
- ``direction-ack`` (``E_VALIDATION_DIRECTION``): el PR tiene ``flag:direction-change`` sin un
  ``ack:direction`` válido, es decir, presente y agregado por el ``lead`` o el ``deputy`` de la base
  después del último ``labeled`` de ``flag:direction-change``, según los eventos del PR
  (REQ-TASK-70, REQ-TASK-71 y 06 REQ-PERM-17). Es aviso, o error con
  ``coordinator.require_direction_ack: true`` en ``project.yaml`` de la base.

Solo en modo ``pr``. Las dos leen la forja (los comentarios y los eventos del PR); si no pueden,
fallan cerradas (REQ-CI-8).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from ...domain.findings import Finding
from ...domain.text import same_login
from ...forge.base import ForgeError
from ..snapshot import parse_markers, task_number
from .base import RuleInput, RuleSpec, make_finding, unavailable

__all__ = ["ACK", "ASSESSMENT", "DIRECTION", "FLAG", "RULES"]

ASSESSMENT_CODE: Final[str] = "E_VALIDATION_ASSESSMENT"
DIRECTION_CODE: Final[str] = "E_VALIDATION_DIRECTION"
FLAG: Final[str] = "flag:direction-change"
ACK: Final[str] = "ack:direction"


def _assessment(inp: RuleInput) -> Sequence[Finding]:
    pr, forge = inp.pr, inp.forge
    if pr is None or forge is None:
        return [unavailable(ASSESSMENT, inp, inp.t("validate.reason.no_forge"))]
    task = task_number(pr.head_branch) if pr.same_repo else None
    if task is None:
        return []
    try:
        comments = forge.list_comments(pr.number)
    except ForgeError:
        return [unavailable(ASSESSMENT, inp, inp.t("validate.coordinator.forge"))]
    for c in comments:
        if not same_login(c.author, pr.author):  # solo el dueño del reclamo la registra (REQ-TASK-61)
            continue
        for kind, data in parse_markers(c.body):
            if kind == "assessment" and data.get("phase") == "submission" and data.get("head_sha") == pr.head_sha:
                return []
    return [make_finding(ASSESSMENT, inp, ASSESSMENT_CODE, severity="warning", path=None, task=task)]


def _direction(inp: RuleInput) -> Sequence[Finding]:
    pr, forge, policy = inp.pr, inp.forge, inp.policy
    if pr is None or forge is None or policy is None:
        return [unavailable(DIRECTION, inp, inp.t("validate.reason.no_forge"))]
    if FLAG not in pr.labels:
        return []
    try:
        events = forge.list_issue_events(pr.number)
    except ForgeError:
        return [unavailable(DIRECTION, inp, inp.t("validate.coordinator.forge"))]
    approvers = [x for x in (policy.roles.lead, policy.roles.deputy) if x]
    flagged = [e.created_at for e in events if e.label == FLAG and e.event == "labeled"]
    since = max(flagged) if flagged else None
    acks = [e for e in events if e.label == ACK]
    last = acks[-1] if acks else None  # orden cronológico (08 §4.2)
    if (ACK in pr.labels and last is not None and last.event == "labeled"
            and (since is None or last.created_at > since) and any(same_login(last.actor, a) for a in approvers)):
        return []
    required = policy.project.coordinator.get("require_direction_ack") is True
    return [make_finding(DIRECTION, inp, DIRECTION_CODE, severity="error" if required else "warning", path=None)]


ASSESSMENT = RuleSpec(id="assessment", modes=frozenset({"pr"}), run=_assessment,
                      codes={ASSESSMENT_CODE: frozenset({"task"})}, needs_forge=True)
DIRECTION = RuleSpec(id="direction-ack", modes=frozenset({"pr"}), run=_direction,
                     codes={DIRECTION_CODE: frozenset()}, needs_forge=True)

RULES: tuple[RuleSpec, ...] = (ASSESSMENT, DIRECTION)
