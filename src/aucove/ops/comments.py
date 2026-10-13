# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Comentarios con estado del núcleo: el de reclamo y el de entrega (04-tareas §1.4, REQ-TASK-13).

Cada uno es un solo comentario propio por destino, que el núcleo edita en su sitio: en el
Issue, el de reclamo (marcadores ``claim`` y ``assessment`` de fase ``claim``); en el PR, el de
entrega (``submission`` y ``assessment`` de fase ``submission``). ``rebuild`` reemplaza el
marcador de un tipo y rehace el texto visible desde los marcadores, así el comentario siempre
dice lo mismo que sus datos (ejemplo de 04 §8.3).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from ..domain.markers import render_marker
from ..domain.text import same_login
from .snapshot import parse_markers

if TYPE_CHECKING:
    from ..forge.base import Comment
    from ..i18n import Translator

__all__ = ["REASON_SHOWN", "assessment_text", "own_comment", "rebuild", "target_label"]

REASON_SHOWN = 300  # el motivo completo queda en el marcador
_ORDER = ("claim", "submission", "assessment")


def own_comment(comments: Sequence[Comment], me: str, kind: str) -> Comment | None:
    """El último comentario de ``me`` con un marcador ``kind`` (REQ-TASK-13)."""
    found = [c for c in comments if same_login(c.author, me) and any(k == kind for k, _ in parse_markers(c.body))]
    return found[-1] if found else None


def target_label(target: Mapping[str, object]) -> str:
    """Un objeto citado, para el texto visible: ``#15``, ```methods``` o la ruta de la decisión."""
    if "task" in target:
        return f"#{target['task']}"
    if "pr" in target:
        return f"#{target['pr']}"
    if "section" in target:
        return f"`{target['section']}`"
    return f"`{target.get('decision')}`"


def _clip(text: object) -> str:
    raw = " ".join(str(text).split())
    return raw if len(raw) <= REASON_SHOWN else raw[: REASON_SHOWN - 1] + "…"


def assessment_text(data: Mapping[str, object], lang: Translator) -> str:
    """La evaluación en palabras (consultiva; decide el responsable, 04 §8.3)."""
    lines: list[str] = []
    effective = data.get("effective_verdict")
    if effective in ("possible", "clear"):
        lines.append(lang(f"tasks.assessment.overlap_{effective}"))
        for item in data.get("overlaps") or []:  # type: ignore[union-attr]
            if isinstance(item, Mapping) and isinstance(item.get("target"), Mapping):
                lines.append(lang("tasks.assessment.overlap_item", target=target_label(item["target"]),
                                  reason=_clip(item.get("reason", ""))))
    direction = data.get("direction")
    valid = direction.get("affected_ids") if isinstance(direction, Mapping) else None
    if isinstance(valid, list) and valid:  # solo los IDs vigentes del plan (REQ-TASK-69)
        lines.append(lang("tasks.assessment.direction", ids=", ".join(f"`{i}`" for i in valid)))
    if not lines:
        lines.append(lang("tasks.assessment.none"))
    lines.append(lang("tasks.assessment.footer", login=data.get("assessed_by", "")))
    return "\n".join(lines)


def _visible(markers: Mapping[str, Mapping[str, object]], lang: Translator, number: int) -> str:
    parts: list[str] = []
    claim = markers.get("claim")
    if claim is not None:
        sections = claim.get("sections")
        shown = ", ".join(str(s) for s in sections) if isinstance(sections, list) and sections else "-"
        parts.append(lang("tasks.claim.comment", task=number, login=claim.get("login", ""), sections=shown))
    submission = markers.get("submission")
    if submission is not None:
        rounds = submission.get("rounds")
        last = rounds[-1] if isinstance(rounds, list) and rounds and isinstance(rounds[-1], Mapping) else {}
        parts.append(lang("tasks.submit.comment", round=last.get("round", 1), task=number))
        parts.append(lang("tasks.submit.seal"))
    assessment = markers.get("assessment")
    if assessment is not None:
        parts.append(assessment_text(assessment, lang))
    return "\n\n".join(parts)


def rebuild(body: str | None, kind: str, data: Mapping[str, object], lang: Translator, number: int) -> str:
    """El comentario con el marcador ``kind`` nuevo y el texto visible rehecho."""
    markers: dict[str, Mapping[str, object]] = {}
    for found_kind, found_data in parse_markers(body or ""):
        if found_kind in _ORDER:
            markers.setdefault(found_kind, found_data)
    markers[kind] = dict(data)
    rendered = [render_marker(k, markers[k]) for k in _ORDER if k in markers]
    return "\n".join(rendered) + "\n" + _visible(markers, lang, number)
