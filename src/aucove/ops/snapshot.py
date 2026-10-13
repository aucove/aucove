# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Instantánea de una tarea armada con el adaptador de la forja (04-tareas §1.1, §1.4, §1.5).

``collect_task`` hace las lecturas de 04 §1.1 y arma el ``TaskSnapshot`` de
``domain.tasks``; ``to_permission_task`` lo convierte, con ``derive_state`` y
``effective_sections``, en la ``Task`` que recibe ``check_path`` (06 §2.4). Solo lee:
ninguna función de este módulo escribe en la forja. Los errores de la forja
(``ForgeError``) se propagan: quien llama decide cómo fallar cerrado (en CI, la regla
``scope`` los convierte en un hallazgo de error, REQ-CI-8).

``status`` y ``context`` ya leyeron en bloque los Issues, las refs, los PR y las revisiones
de los PR abiertos (08 §7.5): se los pasan a ``collect_task`` en un ``Prefetch``, que así
solo lee lo propio de la tarea (eventos, comentarios, actividad y el commit de reclamo).
Con ``Prefetch`` tampoco se leen las revisiones ni los comentarios de los PR cerrados,
porque ``derive_state`` solo usa los del PR abierto que elige (04 §1.5).

Huecos de la interfaz de la forja (08 §2) que se cubren aquí:

- ``PullRequest.created_at`` puede faltar (``None``) en un adaptador que no lo lea:
  entonces ``created_at = updated_at``.
- ``WorkflowRun`` no trae ``path``. Un marcador ``expiry`` de CI se verifica con el
  ``path`` si el adaptador lo expone y, si no, con el nombre del workflow
  (``expire-claims``), además de la rama por defecto y el evento (04 §1.5).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

from .. import _meta
from ..domain.permissions import Task
from ..domain.taskbody import TaskBody, analyze_task_body
from ..domain.tasks import (
    Authz,
    Derived,
    LabelEvent,
    Marker,
    Pull,
    Review,
    TaskRef,
    TaskSnapshot,
    derive_state,
    effective_sections,
    resolve_owner,
)
from ..domain.text import same_login
from ..forge.base import ForgeNotFound, ForgeValidationError

if TYPE_CHECKING:
    from ..config.roles import Roles
    from ..forge.base import Comment, CommitInfo, ForgeAdapter, Issue, PullRequest, Ref, RefActivity
    from ..forge.base import Review as ForgeReview

__all__ = [
    "CI_ACTORS",
    "EXPIRE_EVENTS",
    "EXPIRE_WORKFLOW",
    "EXPIRE_WORKFLOW_NAME",
    "MARKER_KINDS",
    "PUSH_TYPES",
    "TASK_LABEL",
    "Prefetch",
    "TaskEvidence",
    "authz_from_roles",
    "build_task_snapshot",
    "bulk_snapshot",
    "claim_subject",
    "collect_task",
    "parse_markers",
    "read_task",
    "task_branch",
    "task_number",
    "to_permission_task",
]

# Identidad de CI de GitHub Actions (08 REQ-FORGE-43): Authz.ci_actors.
CI_ACTORS: Final[frozenset[str]] = frozenset({"github-actions[bot]"})
TASK_LABEL: Final[str] = "task"
MARKER_KINDS: Final[frozenset[str]] = frozenset(
    {"claim", "release", "submission", "assessment", "expiry", "escalation"}
)
PUSH_TYPES: Final[frozenset[str]] = frozenset({"push", "force_push"})  # 09 §4.2
EXPIRE_WORKFLOW: Final[str] = ".github/workflows/expire-claims.yml"
EXPIRE_WORKFLOW_NAME: Final[str] = "expire-claims"  # name: del workflow (09 §6)
EXPIRE_EVENTS: Final[frozenset[str]] = frozenset({"schedule", "workflow_dispatch"})

_TASK_BRANCH: Final[re.Pattern[str]] = re.compile(r"task/([1-9][0-9]{0,9})")
# Forma con JSON de 00-marco §5.14: primera línea sin "-->", el objeto JSON en la
# siguiente y "-->" en la tercera. El tipo de los marcadores de tarea no lleva ':'.
_MARKER: Final[re.Pattern[str]] = re.compile(
    r"<!-- "
    + re.escape(_meta.NAME)
    + r":(?P<kind>[a-z][a-z0-9-]*) v(?P<version>[0-9]+)(?: [a-z_]+=[A-Za-z0-9._-]+)*[ \t]*\n"
    + r"(?P<data>[^\n]*)\n"
    + r"[ \t]*-->"
)


def task_branch(number: int) -> str:
    """Rama de la tarea ``number`` (00-marco §5.3)."""
    return f"task/{number}"


def task_number(branch: str | None) -> int | None:
    """``n`` de una rama ``task/<n>``; ``None`` si la rama no tiene esa forma exacta."""
    if not isinstance(branch, str):
        return None
    m = _TASK_BRANCH.fullmatch(branch)
    return int(m[1]) if m else None


def claim_subject(number: int) -> str:
    """Asunto del commit de reclamo de la tarea (04 §2.2), del módulo del nombre (D-03)."""
    return f"{_meta.CLAIM_SUBJECT}{number}"


def authz_from_roles(roles: Roles, *, ci_actors: Iterable[str] = CI_ACTORS) -> Authz:
    """``Authz`` de la instantánea con el ``roles.yaml`` de la base (04 §1.1, D-08)."""
    return Authz(
        lead=roles.lead,
        deputy=roles.deputy,
        ci_actors=frozenset(ci_actors),
        section_owner=dict(roles.section_owner()),
    )


def parse_markers(body: str) -> list[tuple[str, dict[str, object]]]:
    """Marcadores de tarea v1 con JSON de un comentario (04 §1.4), en orden de aparición.

    Se ignoran los de otra versión o tipo, y los que no traen un objeto JSON.
    """
    text = body.replace("\r\n", "\n")
    out: list[tuple[str, dict[str, object]]] = []
    for m in _MARKER.finditer(text):
        if m["version"] != "1" or m["kind"] not in MARKER_KINDS:
            continue
        try:
            data = json.loads(m["data"])
        except ValueError:
            continue
        if isinstance(data, dict):
            out.append((m["kind"], data))
    return out


@dataclass(frozen=True)
class TaskEvidence:
    """Lo leído de la forja para una tarea, con la instantánea ya armada."""

    issue: Issue
    body: TaskBody
    snapshot: TaskSnapshot
    activity: tuple[RefActivity, ...] | None  # None: sin ref o la forja no da la actividad
    claim_commit: CommitInfo | None = None  # último commit de reclamo de compare (04 §1.1)
    comments: tuple[Comment, ...] = ()  # comentarios del Issue (claim busca aquí el suyo)
    # commits de compare(base...task/<n>), hasta 250 (release decide con ellos si archiva)
    branch_commits: tuple[CommitInfo, ...] | None = None


@dataclass(frozen=True)
class Prefetch:
    """Lecturas en bloque de ``status`` y ``context`` (08 §7.5) para una tarea."""

    issue: Issue
    ref: Ref | None  # de list_refs("task/")
    pulls: tuple[PullRequest, ...]  # de list_pull_requests(state="all"), cabeza task/<n>
    reviews: Mapping[int, Sequence[ForgeReview]]  # revisiones ya leídas, por número de PR


def collect_task(
    forge: ForgeAdapter,
    number: int,
    authz: Authz,
    *,
    base_branch: str,
    default_branch: str | None = None,
    form_labels: Mapping[str, str] | None = None,
    prefetch: Prefetch | None = None,
) -> TaskEvidence | None:
    """Lecturas de 04 §1.1 para la tarea ``number``; ``None`` si no es un Issue de tarea.

    ``default_branch`` es la rama de los workflows de CI (por defecto, ``base_branch``;
    REQ-CI-1). Con ``prefetch`` no se vuelven a leer el Issue, la ref ni los PR (ver el
    docstring del módulo). Propaga los ``ForgeError`` de la forja.
    """
    issue: Issue | None
    if prefetch is not None:
        issue = prefetch.issue
    else:
        try:
            issue = forge.get_issue(number)
        except ForgeValidationError:  # el número es de un PR (08 REQ-FORGE-9): no es un Issue de tarea
            return None
    if issue is None or not any(x.lower() == TASK_LABEL for x in issue.labels):
        return None  # no existe, es un PR o no lleva la etiqueta task (E_STATE_NOT_A_TASK)
    workflows_branch = default_branch or base_branch
    body = analyze_task_body(issue.body, form_labels)
    events = tuple(
        LabelEvent(label=e.label, action=e.event, actor=e.actor, at=e.created_at)
        for e in forge.list_issue_events(number)
    )
    verify = _expiry_verifier(forge, workflows_branch, authz)
    comments = tuple(forge.list_comments(number))
    issue_markers = _markers(comments, verify)
    branch = task_branch(number)
    ref = prefetch.ref if prefetch is not None else forge.get_ref(branch)
    activity: tuple[RefActivity, ...] | None = None
    task_ref: TaskRef | None = None
    claim: CommitInfo | None = None
    commits: tuple[CommitInfo, ...] | None = None
    if ref is not None:
        listed = forge.list_ref_activity(branch)
        activity = tuple(listed) if listed is not None else None
        claim, commits = _claim_commit(forge, base_branch, branch, number)
        task_ref = TaskRef(
            head_sha=ref.sha,
            claim_commit_sha=claim.sha if claim is not None else None,
            claim_commit_author=claim.author_login if claim is not None else None,
            created_by=_created_by(activity),
            last_push_at=_last_push_at(activity),
        )
    if prefetch is not None:
        pulls = tuple(
            _pull(forge, pr, verify, reviews=prefetch.reviews.get(pr.number), detail=pr.state == "open")
            for pr in prefetch.pulls
            if pr.head_branch == branch
        )
    else:
        pulls = tuple(
            _pull(forge, pr, verify)
            for pr in forge.list_pull_requests(state="all")
            if pr.head_branch == branch
        )
    snapshot = TaskSnapshot(
        number=number,
        issue_author=issue.author,
        issue_state=issue.state,
        state_reason=issue.state_reason,
        labels=frozenset(issue.labels),
        label_events=events,
        assignees=tuple(issue.assignees),
        sections=body.sections,
        spec_hash=body.spec_hash,
        issue_markers=issue_markers,
        ref=task_ref,
        pulls=pulls,
        base_branch=base_branch,
        authz=authz,
    )
    return TaskEvidence(
        issue=issue, body=body, snapshot=snapshot, activity=activity, claim_commit=claim, comments=comments,
        branch_commits=commits
    )


def build_task_snapshot(
    forge: ForgeAdapter,
    number: int,
    authz: Authz,
    *,
    base_branch: str,
    default_branch: str | None = None,
    form_labels: Mapping[str, str] | None = None,
) -> TaskSnapshot | None:
    """``build_task_snapshot(adapter, n, authz)`` de 04 §1.5; ``None`` si no es una tarea."""
    evidence = collect_task(
        forge, number, authz, base_branch=base_branch, default_branch=default_branch, form_labels=form_labels
    )
    return evidence.snapshot if evidence is not None else None


def bulk_snapshot(
    issue: Issue,
    body: TaskBody,
    ref: Ref | None,
    pulls: Sequence[PullRequest],
    *,
    authz: Authz,
    base_branch: str,
) -> TaskSnapshot:
    """Instantánea sin historia (sin eventos, marcadores ni detalle de los PR).

    Solo sirve para una tarea terminada, integrada o con el Issue cerrado: ``derive_state``
    llega a ``merged`` o ``cancelled`` antes de mirar eventos o marcadores (04 §1.5), así
    que el estado es el mismo que con la instantánea completa. Lo usan ``status`` y
    ``context``, que ya leyeron todo eso en bloque (08 §7.5).
    """
    task_ref = TaskRef(ref.sha, None, None, None, None) if ref is not None else None
    return TaskSnapshot(
        number=issue.number,
        issue_author=issue.author,
        issue_state=issue.state,
        state_reason=issue.state_reason,
        labels=frozenset(issue.labels),
        label_events=(),
        assignees=tuple(issue.assignees),
        sections=body.sections,
        spec_hash=body.spec_hash,
        issue_markers=(),
        ref=task_ref,
        pulls=tuple(_as_pull(pr, (), ()) for pr in pulls),
        base_branch=base_branch,
        authz=authz,
    )


def to_permission_task(snap: TaskSnapshot) -> tuple[Task, Derived]:
    """``Task`` de ``check_path``: reclamante, estado y secciones efectivas (04 §1.5, §3).

    ``derive_state`` no da dueño a una tarea cancelada; aquí el reclamante es entonces el
    dueño de la ref (``resolve_owner``), para que ``check_path`` responda por el estado
    (``E_STATE_INVALID_TRANSITION``) y no por un reclamo ajeno.
    """
    derived = derive_state(snap)
    owner = derived.owner
    if owner is None and derived.state == "cancelled":
        owner = resolve_owner(snap)[0]
    sections, _anomalies = effective_sections(snap, owner)
    task = Task(
        number=snap.number,
        claimant=owner,
        state=derived.state,
        sections=frozenset(sections),
    )
    return task, derived


# -- auxiliares -------------------------------------------------------------------


def read_task(
    forge: ForgeAdapter,
    number: int,
    authz: Authz,
    *,
    base_branch: str,
    default_branch: str | None = None,
) -> TaskEvidence | None:
    """``collect_task`` de una sola tarea con lecturas acotadas (``release`` y ``switch``).

    En lugar de listar todos los PR del repositorio (paginado), lee el Issue, la ref y el PR
    más reciente con cabeza ``task/<n>`` (``find_pull_request``): una tarea tiene un solo PR
    (D-12). ``None`` si el número no es un Issue de tarea.
    """
    try:
        issue = forge.get_issue(number)
    except ForgeValidationError:  # el número es de un PR (08 REQ-FORGE-9)
        return None
    if issue is None:
        return None
    branch = task_branch(number)
    pr = forge.find_pull_request(branch, state="all")
    prefetch = Prefetch(issue=issue, ref=forge.get_ref(branch), pulls=(pr,) if pr is not None else (), reviews={})
    return collect_task(forge, number, authz, base_branch=base_branch, default_branch=default_branch,
                        prefetch=prefetch)


def _expiry_verifier(forge: ForgeAdapter, default_branch: str, authz: Authz) -> Callable[[str, Mapping[str, object]], bool]:
    """``ci_verified`` de un marcador ``expiry`` de CI (04 §1.5); memoriza por ``run_id``."""
    cache: dict[int, bool] = {}

    def verify(author: str, data: Mapping[str, object]) -> bool:
        if not any(same_login(author, x) for x in authz.ci_actors):
            return False  # solo los marcadores de CI necesitan el run
        run_id = data.get("run_id")
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1:
            return False
        if run_id not in cache:
            try:
                run = forge.get_workflow_run(run_id)
            except ForgeNotFound:
                cache[run_id] = False
            else:
                path = getattr(run, "path", None)
                where = path == EXPIRE_WORKFLOW if path is not None else run.name == EXPIRE_WORKFLOW_NAME
                cache[run_id] = bool(
                    where and run.head_branch == default_branch and run.event in EXPIRE_EVENTS
                )
        return cache[run_id]

    return verify


def _markers(
    comments: Sequence[Comment],
    verify: Callable[[str, Mapping[str, object]], bool],
) -> tuple[Marker, ...]:
    out: list[Marker] = []
    for c in comments:
        for kind, data in parse_markers(c.body):
            out.append(
                Marker(
                    kind=kind,
                    author=c.author,  # la identidad sale del autor del comentario (REQ-TASK-10)
                    created_at=c.created_at,
                    updated_at=c.updated_at,
                    data=data,
                    ci_verified=kind == "expiry" and verify(c.author, data),
                )
            )
    return tuple(out)


def _claim_commit(
    forge: ForgeAdapter, base: str, branch: str, number: int
) -> tuple[CommitInfo | None, tuple[CommitInfo, ...] | None]:
    """Último commit de ``compare(base...task/<n>)`` con el asunto de reclamo (04 §1.1), y los
    commits de esa comparación."""
    try:
        comparison = forge.compare(base, branch, with_commits=True)
    except ForgeNotFound:
        return None, None
    subject = claim_subject(number)
    found = [c for c in comparison.commits if c.message.split("\n", 1)[0].strip() == subject]
    return (found[-1] if found else None), tuple(comparison.commits)


def _created_by(activity: Sequence[RefActivity] | None) -> str | None:
    if not activity:
        return None
    creations = [a for a in activity if a.activity_type == "branch_creation"]
    return creations[-1].actor if creations else None


def _last_push_at(activity: Sequence[RefActivity] | None) -> datetime | None:
    if not activity:
        return None
    moves = [a.timestamp for a in activity if a.activity_type in PUSH_TYPES or a.activity_type == "branch_creation"]
    return max(moves) if moves else None


def _pull(
    forge: ForgeAdapter,
    pr: PullRequest,
    verify: Callable[[str, Mapping[str, object]], bool],
    *,
    reviews: Sequence[ForgeReview] | None = None,
    detail: bool = True,
) -> Pull:
    """``Pull`` de la instantánea. Sin ``detail`` (PR cerrado con ``Prefetch``) no lee nada."""
    pull_reviews: tuple[Review, ...] = ()
    markers: tuple[Marker, ...] = ()
    if detail:
        listed = reviews if reviews is not None else forge.list_reviews(pr.number)
        pull_reviews = tuple(Review(author=r.author, state=r.state, submitted_at=r.submitted_at) for r in listed)
        markers = _markers(forge.list_comments(pr.number), verify)
    return _as_pull(pr, pull_reviews, markers)


def _as_pull(pr: PullRequest, reviews: tuple[Review, ...], markers: tuple[Marker, ...]) -> Pull:
    created: datetime = pr.created_at or pr.updated_at
    return Pull(
        number=pr.number,
        state=pr.state,
        merged=pr.merged,
        draft=pr.draft,
        author=pr.author,
        base_ref=pr.base_branch,
        same_repo=pr.same_repo,
        created_at=created,
        reviews=reviews,
        markers=markers,
    )
