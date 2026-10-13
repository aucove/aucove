# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Registro ``approval-record/v1`` del libro de aprobaciones (11-trazabilidad §4.2 y §4.3).

``derive_approval_record`` es la función pura de §4.3 (REQ-TRACE-45). Con el PR, sus revisiones y
``roles.yaml`` del primer padre del commit de squash (REQ-TRACE-48), arma el registro:

- cuenta la última revisión ``APPROVED``, ``CHANGES_REQUESTED`` o ``DISMISSED`` de cada persona
  (``ghost`` si la cuenta se borró) y es aprobación si es ``APPROVED`` y la persona califica en
  la matriz de 06 §3.2 (REQ-TRACE-47);
- ``approval_status``: ``not_merged``, ``changes_requested`` (si alguna última revisión pide
  cambios), ``approved`` (una aprobación sobre la cabeza), ``approved_stale`` o ``missing``;
- ``merge_audit`` es la clasificación de 06 §3.5 que da ``ci audit``, si se conoce.

``source.sha256`` es el SHA-256 del registro sin ``source``, en JSON canónico (claves ordenadas,
sin espacios, UTF-8): derivarlo otra vez desde la API da el mismo hash mientras los datos no
cambien (§4.1). El registro no se escribe en ninguna parte en v0.1: la exportación
``report approvals`` está reservada (REQ-TRACE-46).

Funciones puras, sin E/S. El PR y las revisiones se leen por sus atributos (``PullFacts`` y
``ReviewFacts``), que cumplen ``PullRequest`` y ``Review`` de la forja.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from .. import _meta
from .approvals import ApprovalPolicy, qualifying_approvers

__all__ = [
    "APPROVAL_STATUSES",
    "EFFECTIVE",
    "GHOST",
    "Fetched",
    "PullFacts",
    "ReviewFacts",
    "RolesAtBase",
    "derive_approval_record",
    "record_schema",
]

EFFECTIVE = frozenset({"APPROVED", "CHANGES_REQUESTED", "DISMISSED"})
APPROVAL_STATUSES = ("approved", "approved_stale", "changes_requested", "missing", "not_merged")
GHOST = "ghost"  # cuenta borrada (§4.3)
_TASK_BRANCH = re.compile(r"task/([1-9][0-9]{0,9})")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class PullFacts(Protocol):
    """Lo que el registro lee del PR (``GET pulls/{n}``)."""

    number: int
    title: str
    author: str
    base_branch: str
    head_branch: str
    head_sha: str
    merged_at: datetime | None
    merged_by: str | None
    merge_commit_sha: str | None
    labels: tuple[str, ...]


class ReviewFacts(Protocol):
    """Una revisión del PR (``GET pulls/{n}/reviews``)."""

    id: int
    author: str
    state: str
    commit_sha: str | None
    submitted_at: datetime | None


@dataclass(frozen=True)
class RolesAtBase:
    """``roles.yaml`` del primer padre del commit de squash (REQ-TRACE-48) y lo que se deriva de él."""

    sha: str | None  # roles_yaml_sha: el sha12 canónico del contenido
    roles: Mapping[str, str]  # login en minúsculas -> lead, deputy o researcher
    policy: ApprovalPolicy  # la matriz de 06 §3.2 con esa política
    touches: bool  # el PR toca LaTeX (R2, el editor del artículo)


@dataclass(frozen=True)
class Fetched:
    """De dónde y cuándo salieron los datos: el bloque ``source`` del registro."""

    forge: str
    api: str  # URL de GET /repos/{owner}/{repo}/pulls/{n}
    at: datetime


def record_schema() -> str:
    """El identificador del esquema del registro (11 §9)."""
    return f"https://{_meta.NAME}.invalid/schemas/approval-record/v1"


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _login(review: ReviewFacts) -> str:
    return review.author or GHOST


def derive_approval_record(pr: PullFacts, reviews: Sequence[ReviewFacts], *, roles: RolesAtBase, profile: str,
                           merge_audit: str | None, fetched: Fetched) -> dict[str, object]:
    """``derive_approval_record`` de 11 §4.3: el registro ``approval-record/v1`` del PR."""
    last: dict[str, ReviewFacts] = {}
    for r in sorted(reviews, key=lambda r: (r.submitted_at or _EPOCH, r.id)):
        if r.state in EFFECTIVE:
            last[_login(r).casefold()] = r
    allowed = {x.casefold() for x in qualifying_approvers(pr.author, roles.touches, roles.policy)}
    ok = [r for login, r in last.items() if r.state == "APPROVED" and login in allowed]
    status: str
    if pr.merged_at is None:
        status = "not_merged"
    elif any(r.state == "CHANGES_REQUESTED" for r in last.values()):
        status = "changes_requested"
    elif any(r.commit_sha == pr.head_sha for r in ok):
        status = "approved"
    elif ok:
        status = "approved_stale"
    else:
        status = "missing"
    task = _TASK_BRANCH.fullmatch(pr.head_branch or "")
    record: dict[str, object] = {
        "schema": record_schema(),
        "pr": pr.number,
        "task": int(task.group(1)) if task else None,
        "title": pr.title,
        "author": pr.author,
        "base_branch": pr.base_branch,
        "head_sha": pr.head_sha,
        "merge_commit_sha": pr.merge_commit_sha,
        "merged_by": pr.merged_by,
        "merged_at": _iso(pr.merged_at),
        "method": "squash",  # la única estrategia admitida (REQ-TRACE-33)
        "profile": profile,
        "reviews": [
            {"login": _login(r), "role": roles.roles.get(_login(r).casefold()), "state": r.state,
             "submitted_at": _iso(r.submitted_at), "commit_id": r.commit_sha, "on_head": r.commit_sha == pr.head_sha}
            for r in sorted(last.values(), key=lambda r: (r.submitted_at or _EPOCH, r.id))
        ],
        "approved_by": sorted((_login(r) for r in ok), key=str.casefold),
        "approval_status": status,
        "merge_audit": merge_audit,
        "labels": list(pr.labels),
        "roles_yaml_sha": roles.sha,
    }
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    record["source"] = {"forge": fetched.forge, "api": fetched.api, "fetched_at": _iso(fetched.at),
                        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}
    return record
