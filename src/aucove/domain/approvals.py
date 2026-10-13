# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Matriz de aprobación de 06-permisos §3.2: requisitos, aprobadores y evaluación.

Funciones puras (REQ-ARCH-18). Los logins se comparan sin distinguir mayúsculas
(``same_login``, 08 REQ-FORGE-46) y se devuelven con la grafía de la política.

- R1: una aprobación de ``lead`` o ``deputy`` distinto del autor.
- R2 (desde H3, 00-marco D-56): con ``format: latex``, la del editor del artículo si está
  declarado, no es el autor y el PR toca LaTeX (``touches_latex``). Sin editor declarado lo
  es el ``lead``, y R1 ya lo cubre.
- Con ``mode: individual`` (00-marco D-57) no hay requisitos.

Una aprobación cuenta solo sobre ``head_sha``; un ``CHANGES_REQUESTED`` vigente bloquea solo si
es de un aprobador que cuenta (06 §3.2).

``editor_verdict`` es la decisión del check ``aucove/editor-review`` (09-ci REQ-CI-58): R2
sola, con la última revisión del editor.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from .paths import is_editor_path
from .text import same_login

__all__ = [
    "Approval",
    "ApprovalPolicy",
    "EditorReason",
    "EditorVerdict",
    "ReviewRole",
    "ReviewState",
    "editor_verdict",
    "evaluate_approvals",
    "last_approval_at",
    "qualifying_approvers",
    "required_approvals",
    "review_role",
    "touches_latex",
]

ReviewRole = Literal["approver", "owner", "none"]
# Motivo de editor_verdict, en el orden en que se evalúa (09-ci REQ-CI-58).
EditorReason = Literal[
    "individual", "format_none", "no_editor", "author_is_editor", "no_latex",
    "changes_requested", "approved", "missing",
]
_PENDING_BIB = re.compile(r"sections/[a-z0-9]+(?:-[a-z0-9]+)*/pending\.bib")
_COUNTED = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class ApprovalPolicy:
    """Lo que la matriz necesita de la política de la base."""

    approvers: tuple[str, ...]  # lead y deputy, con la grafía de people
    editor: str | None  # editor declarado en roles.yaml; None: lo es el lead
    individual: bool  # mode: individual
    latex: bool  # format: latex


@dataclass(frozen=True)
class ReviewState:
    """Una revisión del PR, en orden cronológico."""

    author: str
    state: str  # APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED o PENDING
    commit_sha: str | None
    submitted_at: datetime | None


@dataclass(frozen=True)
class Approval:
    """Resultado de ``evaluate_approvals`` (06 §3.2)."""

    ok: bool
    missing: tuple[tuple[str, ...], ...]  # cada requisito sin cumplir, con quiénes lo cumplirían
    unsatisfiable: tuple[str, ...]  # "no_deputy": el autor es el lead y no hay suplente
    blocking: tuple[str, ...]  # aprobadores que cuentan con CHANGES_REQUESTED vigente

    @property
    def required_from(self) -> tuple[str, ...]:
        """Los logins de los requisitos que faltan, sin repetir."""
        return tuple(sorted({x for group in self.missing for x in group}, key=str.casefold))


def touches_latex(paths: Iterable[str]) -> bool:
    """El PR toca LaTeX: un ``.tex``, el ``pending.bib`` de una parte o un archivo del editor."""
    return any(p.lower().endswith(".tex") or _PENDING_BIB.fullmatch(p) or is_editor_path(p) for p in paths)


def _without(group: Iterable[str], login: str) -> frozenset[str]:
    return frozenset(x for x in group if not same_login(x, login))


def required_approvals(author: str, touches: bool, pol: ApprovalPolicy) -> list[frozenset[str]]:
    """Hace falta una aprobación vigente de cada conjunto: R1 y, si corresponde, R2."""
    if pol.individual:
        return []
    reqs = [_without(pol.approvers, author)]
    if pol.latex and pol.editor and not same_login(author, pol.editor) and touches:
        reqs.append(frozenset({pol.editor}))
    return reqs


def qualifying_approvers(author: str, touches: bool, pol: ApprovalPolicy) -> frozenset[str]:
    return frozenset().union(*required_approvals(author, touches, pol))


def evaluate_approvals(author: str, head_sha: str, touches: bool, reviews: Sequence[ReviewState],
                       pol: ApprovalPolicy) -> Approval:
    """``evaluate_approvals`` de 06 §3.2 con la cabeza ``head_sha``."""
    latest: dict[str, ReviewState] = {}
    for r in sorted(reviews, key=lambda r: r.submitted_at or _EPOCH):
        if r.state in _COUNTED:
            latest[r.author.casefold()] = r
    approved = {k for k, r in latest.items()
                if r.state == "APPROVED" and r.commit_sha == head_sha and not same_login(r.author, author)}
    qualifying = {x.casefold() for x in qualifying_approvers(author, touches, pol)}
    blocking = sorted((r.author for k, r in latest.items() if r.state == "CHANGES_REQUESTED" and k in qualifying),
                      key=str.casefold)
    reqs = required_approvals(author, touches, pol)
    met = [any(x.casefold() in approved for x in group) for group in reqs]
    return Approval(
        ok=all(met) and not blocking,
        missing=tuple(tuple(sorted(group, key=str.casefold)) for group, ok in zip(reqs, met) if group and not ok),
        unsatisfiable=() if all(reqs) else ("no_deputy",),
        blocking=tuple(blocking),
    )


def last_approval_at(author: str, head_sha: str, touches: bool, reviews: Sequence[ReviewState],
                     pol: ApprovalPolicy) -> datetime | None:
    """La fecha de la última aprobación que cuenta para ``evaluate_approvals`` (12-mvp §5.3).

    Cuenta la última revisión vigente de cada aprobador que califica, si es ``APPROVED`` sobre
    ``head_sha``; ``None`` si no hay ninguna.
    """
    latest: dict[str, ReviewState] = {}
    for r in sorted(reviews, key=lambda r: r.submitted_at or _EPOCH):
        if r.state in _COUNTED:
            latest[r.author.casefold()] = r
    qualifying = {x.casefold() for x in qualifying_approvers(author, touches, pol)}
    times = [r.submitted_at for k, r in latest.items()
             if k in qualifying and r.state == "APPROVED" and r.commit_sha == head_sha and r.submitted_at is not None]
    return max(times, default=None)


def review_role(actor: str, author: str, touches: bool, sections: Iterable[str], owners: Mapping[str, str],
                pol: ApprovalPolicy) -> ReviewRole:
    """Rol del actor en el PR (05 §9.22): ``approver`` si su aprobación cuenta, ``owner`` si es
    dueño de una parte tocada (puede pedir cambios, 06 §2.3) y ``none`` en otro caso o si es el
    autor."""
    if same_login(actor, author):
        return "none"
    if any(same_login(actor, x) for x in qualifying_approvers(author, touches, pol)):
        return "approver"
    if any(same_login(actor, owners.get(s)) for s in sections):
        return "owner"
    return "none"


@dataclass(frozen=True)
class EditorVerdict:
    """Resultado de ``editor_verdict``: la conclusión del check y su motivo."""

    passed: bool
    reason: EditorReason
    editor: str | None  # el editor declarado, con la grafía de la política


def editor_verdict(author: str, head_sha: str, touches: bool, reviews: Sequence[ReviewState],
                   pol: ApprovalPolicy) -> EditorVerdict:
    """Decisión de ``aucove/editor-review`` (09-ci REQ-CI-58), con el primer motivo que aplica.

    Pasa con ``mode: individual``, sin ``format: latex``, sin editor declarado, si el autor es el
    editor o si el PR no toca LaTeX. Si no, decide la última revisión del editor que cuenta
    (``APPROVED``, ``CHANGES_REQUESTED`` o ``DISMISSED``, como R2): pasa solo si es
    ``APPROVED`` sobre ``head_sha``; ``CHANGES_REQUESTED`` la deja en ``failure``, y una
    aprobación de otra cabeza o descartada, también (``missing``).
    """
    editor = pol.editor
    if pol.individual:
        return EditorVerdict(True, "individual", editor)
    if not pol.latex:
        return EditorVerdict(True, "format_none", editor)
    if not editor:
        return EditorVerdict(True, "no_editor", None)
    if same_login(author, editor):
        return EditorVerdict(True, "author_is_editor", editor)
    if not touches:
        return EditorVerdict(True, "no_latex", editor)
    latest: ReviewState | None = None
    for r in sorted(reviews, key=lambda r: r.submitted_at or _EPOCH):
        if r.state in _COUNTED and same_login(r.author, editor):
            latest = r
    if latest is not None and latest.state == "CHANGES_REQUESTED":
        return EditorVerdict(False, "changes_requested", editor)
    if latest is not None and latest.state == "APPROVED" and latest.commit_sha == head_sha:
        return EditorVerdict(True, "approved", editor)
    return EditorVerdict(False, "missing", editor)
