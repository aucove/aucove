# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Adaptador falso de la forja (08-forja §10.1).

En memoria, determinista y seguro entre hilos. Cumple ``ForgeAdapter`` y añade
utilidades de prueba que no forman parte del Protocol (``seed_*``, ``as_user``,
``admin_merge``, ``faults`` y ``calls``).

Modelo de peticiones y fallas
-----------------------------
Cada método registra en ``calls`` las peticiones HTTP que haría el adaptador
GitHub según la tabla de 08 §4.2: la principal y las secundarias (páginas,
``GET`` de confirmación). Tipos: ``L`` (lectura), ``E`` (escritura) y ``C``
(escritura que crea contenido: Issues y comentarios). ``C`` es un subconjunto
de ``E``: ``count("E")`` cuenta todas las escrituras (08 §7.5).

Una falla (``Fault``) se aplica a la petición principal de una llamada y
representa el resultado después de la política de reintentos de 08 §7.3 (el
falso no reintenta). ``timeout_before`` no tiene efecto; ``timeout_after`` y
``http_5xx_after`` (502 con el cambio ya hecho) sí; ``race`` muta el estado
justo antes del efecto. En una misma llamada se combinan a lo sumo una
``latency``, una ``race`` y una falla que responde con error; si coinciden dos
fallas del mismo tipo, la llamada lanza ``ValueError`` en vez de descartar una.
Tras la falla, el método aplica la recuperación que 08 §4.4 y §4.7 asignan al
adaptador: ``create_ref`` y ``update_ref`` confirman con un ``GET`` de la ref, y
``create_issue`` y ``create_comment`` buscan su marcador ``op`` entre lo creado
por la cuenta desde t0 (si no lo encuentran, relanzan la causa). Un
``RateLimited`` bloquea a la cuenta que lo recibió hasta ``reset_at`` del reloj
falso (REQ-FORGE-51: el proceso de esa cuenta; las demás vistas siguen).

Semánticas de GitHub reproducidas en H2 (REQ-FORGE-59): ``create_ref`` atómico
bajo un lock y sin refs en conflicto de directorio (``task`` frente a
``task/12``); ``update_ref`` solo con avance rápido; en el perfil ``enforced`` el
ruleset de la base rechaza el ``PATCH`` directo (sin bypass: solo
``admin_merge`` mueve la base) y la rama por defecto no se puede borrar; borrar
la rama cabeza cierra sus PR abiertos; un push a la cabeza de un PR mueve su
``head_sha`` y recalcula sus archivos (diff de tres puntos); la actividad de
refs registra ``branch_creation``, ``push`` y ``branch_deletion`` con el actor
de la vista; ``PATCH`` con ``assignees`` reemplaza el conjunto y lo descarta en
silencio si el actor no tiene push (y entonces REQ-FORGE-11 da
``ForgeValidationError``); los endpoints de Issues aceptan números de PR, salvo
``get_issue`` y ``list_issues`` (REQ-FORGE-9); revisiones en orden cronológico,
y las ``PENDING`` solo para su autor; ``Closes #n`` cierra el Issue solo al
integrar en la rama por defecto (``admin_merge``); los tokens fine-grained no
leen check runs (§4.6). Las cuentas que aparecen sin ``seed_user`` (por
``as_user`` o como autoras sembradas) tienen solo lectura; la del constructor y
la del CI tienen ``write``. Desde H3, ``create_pull_request`` (un PR abierto por cabeza y
ninguno sin commits de diferencia), ``update_pull_request`` (título, cuerpo y estado, nunca
``draft``) y ``request_reviewers`` (nunca al autor); desde 05 §9.24, ``submit_review``.
``mark_ready_for_review``, ``commit_files`` y ``merge_into`` lanzan ``ForgeUnsupported``.

``admin(token)`` da el ``ForgeAdmin`` del falso (08 §2.2; 00-marco D-62): la configuración del
repositorio que solo cambia ``init`` (ajustes, Actions, retención, colaboradores, organización
y rulesets), con el mismo registro de llamadas y las mismas fallas inyectables. Escribir y leer
la configuración exige administración; en un repo privado del perfil ``advisory`` (Free) no hay
rulesets. ``seed_admin`` y ``seed_admin_token`` preparan la configuración y los tokens.

Para ``ci seal`` (08 §10.1, D-45): un comentario admite hasta 65 536 caracteres,
contado con el marcador ``op`` que agrega el adaptador, y uno más largo es 422;
``seed_run(workflow, sha, event, conclusion)`` alimenta ``successful_runs``, que
filtra por rama y evento exactos; ``admin_merge`` (o la siembra) registra
``merged_by``, que, como en GitHub, solo devuelve ``get_pull_request``: las listas y
``pulls_for_commit`` lo dejan en ``None`` (08 §4.2, CT-SEAL-01), así que ``ci seal`` lo
pide aparte, como contra la forja real.

Solo se carga del JSON de ``<PREFIJO>FORGE_FAKE_STATE`` con ``<PREFIJO>TESTING=1``
(REQ-FORGE-62; lo decide ``ops.context.context_factory``).
"""

from __future__ import annotations

import base64
import copy
import fnmatch
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, NoReturn

from .. import _meta
from ..domain.text import same_login
from .base import (
    NULL_SHA,
    ORG_SETTINGS,
    REPO_SETTINGS,
    ActionsPolicy,
    AdminLevel,
    BaseRules,
    Capabilities,
    ChangedFile,
    CheckRun,
    Checks,
    CollaboratorPermission,
    Comment,
    CommitInfo,
    Comparison,
    FileChange,
    FileContent,
    ForgeAuthError,
    ForgeError,
    ForgeNotFound,
    ForgePermissionError,
    ForgeUnavailable,
    ForgeUnsupported,
    ForgeValidationError,
    HumanConfirmation,
    Identity,
    Issue,
    Label,
    LabelEvent,
    MergeConflict,
    NetworkTimeout,
    NetworkUnreachable,
    NoCommitsBetween,
    NoTokenError,
    PullRequest,
    PullRequestExists,
    RateLimited,
    Ref,
    RefActivity,
    RefAlreadyExists,
    RefConflict,
    RefNamespace,
    RepoRef,
    Review,
    RulesetInfo,
    WorkflowPermissions,
    WorkflowRun,
    rules_summary,
    token_sources,
)

__all__ = [
    "STATE_VERSION",
    "DEFAULT_START",
    "ADAPTER_METHODS",
    "UNSUPPORTED_METHODS",
    "FAULT_KINDS",
    "CI_LOGIN",
    "MAX_COMMENT_CHARS",
    "FakeClock",
    "Fault",
    "FaultSet",
    "Call",
    "CallLog",
    "FakeForge",
    "FakeAdmin",
]

STATE_VERSION = 1
DEFAULT_START = "2026-09-29T12:00:00Z"
PAGE_SIZE = 100  # per_page de 08 §4.2
MAX_PAGES = 10  # tope de REQ-FORGE-8
MAX_FILE_PAGES = 30  # list_pull_request_files: la excepción de REQ-FORGE-8 (los 3000 de GitHub)
CI_LOGIN = "github-actions[bot]"  # identidad del CI (REQ-FORGE-43)
MAX_COMMENT_CHARS = 65_536  # cuerpo de un comentario en GitHub, marcador op incluido (08 §10.1)
_PRIMARY_RESET = timedelta(hours=1)  # ventana del límite primario
_SECONDARY_RESET = timedelta(seconds=60)  # 08 §7.3: ahora + 60 s
_BIG_FILE = 1 << 20  # get_file repite con Accept raw si pesa más de 1 MB
_MAX_COMPARE_FILES = 300
_MAX_COMMITS = 250

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_OP_ID_RE = re.compile(r"[A-Za-z0-9._-]+")  # valor de 00-marco §5.14
_CLOSING_RE = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+#(\d+)\b")
_PERM_LEVEL = {"read": 0, "triage": 1, "write": 2, "maintain": 3, "admin": 4}
_ISSUE_STATE_REASONS = ("completed", "not_planned", "reopened", "duplicate")
_REVIEW_STATES = ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING")
_FAILED = frozenset({"failure", "timed_out", "cancelled", "action_required"})
_PASSED = frozenset({"success", "neutral", "skipped"})

CallKind = Literal["L", "E", "C"]
FaultKind = Literal[
    "timeout_before",
    "timeout_after",
    "http_401",
    "http_403",
    "http_404",
    "http_409",
    "http_422",
    "http_5xx",
    "http_5xx_after",
    "rate_limit_primary",
    "rate_limit_secondary",
    "unreachable",
    "race",
    "latency",
]
FAULT_KINDS: tuple[str, ...] = (
    "timeout_before",
    "timeout_after",
    "http_401",
    "http_403",
    "http_404",
    "http_409",
    "http_422",
    "http_5xx",
    "http_5xx_after",
    "rate_limit_primary",
    "rate_limit_secondary",
    "unreachable",
    "race",
    "latency",
)
# Fallas que actúan después del efecto: la forja aplicó el cambio y la respuesta se pierde.
_AFTER_EFFECT = frozenset({"timeout_after", "http_5xx_after"})

# Métodos del Protocol -> permiso de token fine-grained de 08 §4.2 (valor de la
# cabecera X-Accepted-GitHub-Permissions que va en el hint de un 403).
_PERMISSIONS: Mapping[str, str | None] = {
    "identity": None,
    "capabilities": "metadata=read",
    "server_time": None,  # la cabecera Date de cualquier respuesta: no es una petición
    "get_ref": "contents=read",
    "list_refs": "contents=read",
    "create_ref": "contents=write",
    "update_ref": "contents=write",
    "delete_ref": "contents=write",
    "list_ref_activity": "contents=read",
    "create_empty_commit": "contents=write",
    "commit_files": "contents=write",
    "merge_into": "contents=write",
    "get_commit": "contents=read",
    "get_file": "contents=read",
    "compare": "contents=read",
    "codeowners_errors": "contents=read",
    "get_issue": "issues=read",
    "list_issues": "issues=read",
    "create_issue": "issues=write",
    "update_issue": "issues=write",
    "add_labels": "issues=write",
    "remove_label": "issues=write",
    "ensure_labels": "issues=write",
    "list_issue_events": "issues=read",
    "set_assignees": "issues=write",
    "add_assignees": "issues=write",
    "remove_assignees": "issues=write",
    "list_comments": "issues=read",
    "create_comment": "issues=write",
    "update_comment": "issues=write",
    "get_pull_request": "pull_requests=read",
    "find_pull_request": "pull_requests=read",
    "list_pull_requests": "pull_requests=read",
    "list_pull_request_files": "pull_requests=read",
    "list_pull_request_commits": "pull_requests=read",
    "create_pull_request": "pull_requests=write",
    "update_pull_request": "pull_requests=write",
    "mark_ready_for_review": "pull_requests=write",
    "request_reviewers": "pull_requests=write",
    "list_reviews": "pull_requests=read",
    "submit_review": "pull_requests=write",
    "get_checks": "actions=read,statuses=read",
    "get_workflow_run": "actions=read",
    "successful_runs": "actions=read",
    "pulls_for_commit": "contents=read,pull_requests=read",
    "last_successful_run": "actions=read",
    "upsert_check_run": "checks=write",
    # Lecturas de la configuración (00-marco D-65; REQ-FORGE-73)
    "repo_settings": "metadata=read",
    "rulesets": "metadata=read",
    "list_labels": "issues=read",
    "collaborator_permission": "metadata=read",
    "outside_collaborators": "metadata=read",
    "actions_policy": "administration=read",
    "workflow_permissions": "administration=read",
    "org_settings": "organization_administration=read",
}
ADAPTER_METHODS: frozenset[str] = frozenset(_PERMISSIONS)
# Métodos de ForgeAdmin (08 §2.2; 00-marco D-62) -> permiso del hint de un 403.
_ADMIN_PERMISSIONS: Mapping[str, str | None] = {
    "identity": None,
    "repo_settings": "administration=read",
    "update_repo_settings": "administration=write",
    "actions_policy": "administration=read",
    "set_actions_policy": "administration=write",
    "workflow_permissions": "administration=read",
    "set_workflow_permissions": "administration=write",
    "retention_days": "administration=read",
    "set_retention_days": "administration=write",
    "collaborator_permission": "metadata=read",
    "add_collaborator": "administration=write",
    "outside_collaborators": "metadata=read",
    "user_id": None,
    "org_settings": "organization_administration=read",
    "ruleset": "administration=read",
    "upsert_ruleset": "administration=write",
    "branch_rules": "metadata=read",
}
ADMIN_METHODS: frozenset[str] = frozenset(_ADMIN_PERMISSIONS)
# Stubs: cumplen la firma y lanzan ForgeUnsupported (kind 'unsupported'). H3 implementó los
# demás con las operaciones que los usan; mark_ready_for_review queda reservado (08 §4.3) y
# ninguna operación de v0.1 lo invoca.
UNSUPPORTED_METHODS: frozenset[str] = frozenset({"mark_ready_for_review"})
CONFIRMATION_WINDOW = timedelta(seconds=120)  # REQ-FORGE-10, como el adaptador real
_REVIEW_OPERATIONS = {"APPROVE": "approve", "REQUEST_CHANGES": "request-changes"}  # 06 §6


# ---------------------------------------------------------------------------
# Fechas
# ---------------------------------------------------------------------------


def _aware(dt: datetime) -> datetime:
    if not isinstance(dt, datetime):
        raise TypeError("expected a datetime")
    if dt.utcoffset() is None:
        raise ValueError("naive datetime; use a timezone-aware one")
    return dt.astimezone(timezone.utc)


def _parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return _aware(value)
    if not isinstance(value, str):
        raise TypeError("expected an ISO 8601 string or a datetime")
    return _aware(datetime.fromisoformat(value))


def _parse_opt_dt(value: object) -> datetime | None:
    return None if value is None else _parse_dt(value)  # type: ignore[arg-type]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _opt_iso(dt: datetime | None) -> str | None:
    return None if dt is None else _iso(dt)


class FakeClock:
    """Reloj determinista del falso: solo avanza con ``advance`` o ``set``."""

    def __init__(self, start: str | datetime = DEFAULT_START) -> None:
        self._now = _parse_dt(start)
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, delta: timedelta | float = 1.0) -> datetime:
        """Avanza ``delta`` (segundos o ``timedelta``, no negativo) y devuelve la hora nueva."""
        step = delta if isinstance(delta, timedelta) else timedelta(seconds=float(delta))
        if step < timedelta(0):
            raise ValueError("the fake clock cannot go backwards")
        with self._lock:
            self._now += step
            return self._now

    def set(self, when: str | datetime) -> None:
        with self._lock:
            self._now = _parse_dt(when)

    def __repr__(self) -> str:
        return f"FakeClock({_iso(self.now())!r})"


# ---------------------------------------------------------------------------
# Fallas inyectables (REQ-FORGE-61, REQ-MVP-27)
# ---------------------------------------------------------------------------


@dataclass
class Fault:
    """Falla inyectable por método y por número de llamada.

    ``method`` es un método del Protocol o ``"*"`` (todos). ``nth`` cuenta las
    llamadas a ese método desde que se agregó la falla, empezando en 1; ``None``
    la aplica a todas. ``race`` recibe el estado interno (``s.issues[12]``,
    ``s.refs``…) justo antes del efecto. ``delay_s`` es la espera de ``latency``.
    ``timeout_after`` y ``http_5xx_after`` dejan hecho el efecto y luego fallan;
    las demás fallas que responden con error actúan antes del efecto.
    """

    method: str
    kind: FaultKind
    nth: int | None = None
    race: Callable[[object], None] | None = None
    delay_s: float = 0.01
    _seen: int = field(default=0, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.method != "*" and self.method not in ADAPTER_METHODS:
            raise ValueError(f"unknown adapter method: {self.method!r}")
        if self.kind not in FAULT_KINDS:
            raise ValueError(f"unknown fault kind: {self.kind!r}")
        if self.nth is not None and (isinstance(self.nth, bool) or not isinstance(self.nth, int) or self.nth < 1):
            raise ValueError("nth must be a positive int or None")
        if (self.kind == "race") != (self.race is not None):
            raise ValueError("race faults (and only them) need a race callable")
        if self.delay_s < 0:
            raise ValueError("delay_s must not be negative")


class FaultSet:
    """Conjunto de fallas compartido por todas las vistas de un mismo estado."""

    def __init__(self) -> None:
        self._faults: list[Fault] = []
        self._lock = threading.Lock()

    def add(self, fault: Fault) -> None:
        if not isinstance(fault, Fault):
            raise TypeError("expected a Fault")
        with self._lock:
            self._faults.append(fault)

    def clear(self) -> None:
        with self._lock:
            self._faults.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._faults)

    def __iter__(self) -> Iterator[Fault]:
        with self._lock:
            return iter(list(self._faults))

    def _match(self, method: str) -> list[Fault]:
        """Cuenta la llamada en cada falla del método y devuelve las que tocan, en orden de alta.

        Se combinan a lo sumo una ``latency``, una ``race`` y una falla que responde
        con error (antes o después del efecto). Dos del mismo tipo en la misma
        llamada lanzan ``ValueError``: aplicar una y consumir la otra en silencio
        escondería un error de la prueba (REQ-FORGE-61).
        """
        hits: list[Fault] = []
        with self._lock:
            for fault in self._faults:
                if fault.method not in (method, "*"):
                    continue
                fault._seen += 1
                if fault.nth is None or fault._seen == fault.nth:
                    hits.append(fault)
        by_slot: dict[str, Fault] = {}
        for fault in hits:
            slot = fault.kind if fault.kind in ("latency", "race") else "failure"
            if slot in by_slot:
                raise ValueError(
                    f"faults {by_slot[slot]!r} and {fault!r} both apply to the same call of {method!r}; "
                    "combine at most one latency, one race and one failure per call"
                )
            by_slot[slot] = fault
        return hits


# ---------------------------------------------------------------------------
# Registro de llamadas (REQ-FORGE-60, REQ-MVP-27)
# ---------------------------------------------------------------------------


@dataclass
class Call:
    """Una petición HTTP que haría el adaptador GitHub."""

    method: str  # método del Protocol
    http: str  # GET, POST, PATCH, PUT o DELETE
    kind: CallKind
    actor: str
    args: dict[str, Any]
    outcome: str = "pending"  # ok, <tipo de falla>, http_<estado> o el kind de la excepción


class CallLog:
    """Peticiones por método del Protocol, verbo HTTP y tipo (L, E, C)."""

    def __init__(self) -> None:
        self._entries: list[Call] = []
        self._lock = threading.Lock()

    def count(
        self,
        kind: Literal["L", "E", "C"] | None = None,
        method: str | None = None,
        *,
        http: str | None = None,
    ) -> int:
        """Número de peticiones. ``kind="E"`` incluye las ``C`` (C es un subconjunto de E)."""
        return sum(1 for c in self.entries() if _call_matches(c, kind, method, http))

    def writes(self) -> list[tuple[str, dict[str, Any]]]:
        """Todas las peticiones de escritura (E y C), en orden: ``(método, argumentos)``."""
        return [(c.method, dict(c.args)) for c in self.entries() if c.kind in ("E", "C")]

    def entries(self) -> list[Call]:
        with self._lock:
            return [replace(c, args=dict(c.args)) for c in self._entries]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def _record(self, method: str, http: str, kind: CallKind, actor: str, args: Mapping[str, Any]) -> Call:
        call = Call(method, http, kind, actor, {k: _jsonable(v) for k, v in args.items()})
        with self._lock:
            self._entries.append(call)
        return call

    def _load(self, entries: Iterable[Mapping[str, Any]]) -> None:
        with self._lock:
            self._entries = [
                Call(
                    str(e["method"]),
                    str(e["http"]),
                    e["kind"],
                    str(e["actor"]),
                    dict(e.get("args") or {}),
                    str(e.get("outcome", "ok")),
                )
                for e in entries
            ]


def _call_matches(c: Call, kind: str | None, method: str | None, http: str | None) -> bool:
    if kind is not None:
        if kind == "E":
            if c.kind not in ("E", "C"):
                return False
        elif c.kind != kind:
            return False
    if method is not None and c.method != method:
        return False
    return http is None or c.http == http.upper()


def _jsonable(value: object) -> Any:
    # Argumentos del registro en forma JSON (se guardan en el estado).
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, (bytes, bytearray)):
        return {"b64": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, Label):
        return {"name": value.name, "color": value.color, "description": value.description}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=repr) if isinstance(value, (set, frozenset)) else value
        return [_jsonable(v) for v in items]
    return repr(value)


# ---------------------------------------------------------------------------
# Estado interno (mutable; lo recibe Fault.race)
# ---------------------------------------------------------------------------


class _HttpError(Exception):
    """Respuesta HTTP de error simulada; cada método la clasifica (08 §4.2)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class _User:
    login: str
    id: int
    permission: str
    token_kind: str
    scopes: tuple[str, ...] | None = None  # X-OAuth-Scopes; None = los de omisión de token_kind
    member: bool = True  # colaboradora o miembro de la org (seed_user); False = cuenta implícita
    # Lo que mide GET /user/repos con un token fine-grained (08 §5.2); None = repo_restricted
    token_scope: str | None = None


@dataclass
class _Commit:
    sha: str
    files: dict[str, bytes] | None  # None = contenido desconocido (sha sembrado sin árbol)
    parents: list[str]
    message: str
    author: str | None
    author_date: datetime
    committer_date: datetime


@dataclass
class _CommentRec:
    id: int
    author: str
    body: str
    created_at: datetime
    updated_at: datetime


@dataclass
class _IssueRec:
    number: int
    title: str
    body: str
    state: str
    state_reason: str | None
    labels: list[str]
    assignees: list[str]
    author: str
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    events: list[LabelEvent] = field(default_factory=list)
    comments: list[_CommentRec] = field(default_factory=list)
    # Eventos de issues/{n}/events que no son de etiquetas (closed, reopened, renamed,
    # assigned…): no se listan, pero GitHub los pagina (REQ-FORGE-8).
    other_events: int = 0


@dataclass
class _PullRec:
    number: int
    node_id: str
    title: str
    body: str
    state: str
    draft: bool
    merged: bool
    merged_at: datetime | None
    head_branch: str
    head_sha: str
    base_branch: str
    author: str
    labels: list[str]
    assignees: list[str]
    requested_reviewers: list[str]
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    merge_commit_sha: str | None = None
    files: list[ChangedFile] = field(default_factory=list)
    reviews: list[Review] = field(default_factory=list)
    comments: list[_CommentRec] = field(default_factory=list)
    events: list[LabelEvent] = field(default_factory=list)
    commits: list[str] | None = None  # None: se calculan del grafo mientras está abierto
    other_events: int = 0  # como en _IssueRec
    # False si la cabeza es una rama de un fork (PullRequest.same_repo; 04-tareas §1.1).
    # Esa rama vive en el fork: los push y borrados de la rama homónima de este repo no la tocan.
    same_repo: bool = True
    merged_by: str | None = None  # quien integró (admin_merge o la siembra); PullRequest.merged_by


@dataclass
class _CheckRec:
    id: int
    name: str
    status: str
    conclusion: str | None
    summary: str = ""  # output.summary (upsert_check_run, REQ-FORGE-70)


@dataclass
class _RunRec:
    id: int
    workflow_file: str
    name: str
    event: str
    status: str
    conclusion: str | None
    head_sha: str
    head_branch: str | None
    actor: str
    created_at: datetime


@dataclass
class _AdminState:
    """Configuración del repositorio que solo ve ``ForgeAdmin`` (08 §2.2; 00-marco D-62)."""

    settings: dict[str, object]
    actions: dict[str, dict[str, Any] | None]  # 'repo' | 'org' -> política (con 'selected'); None: no legible
    workflow: dict[str, dict[str, Any] | None]
    retention: int | None  # None: el endpoint no existe (REQ-PERM-57)
    org: dict[str, object] | None  # None: el dueño es una cuenta personal
    rulesets: dict[int, dict[str, Any]] = field(default_factory=dict)  # id -> definición (con name)
    outside: set[str] = field(default_factory=set)  # logins en minúsculas: colaboradores externos
    invitations: dict[str, str] = field(default_factory=dict)  # login en minúsculas -> permiso pendiente
    tokens: dict[str, str | None] = field(default_factory=dict)  # token -> login; None: token inválido
    # Sin rulesets, el perfil enforced declara la base ya protegida (como en las pruebas anteriores a
    # init); False simula un repo recién creado desde la plantilla, sin reglas (seed_admin(fresh=True)).
    protected_by_default: bool = True
    # ¿El visor ve el plan de la organización? Sin verlo, en un repo privado que sí puede exigir
    # reglas el sondeo no es concluyente (08 §3.2): rules_available None y perfil advisory.
    plan_visible: bool = True


def _default_admin(visibility: str) -> _AdminState:
    """Configuración de un repositorio recién creado, con los valores por omisión de GitHub."""
    return _AdminState(
        settings={
            "allow_squash_merge": True,
            "allow_merge_commit": True,
            "allow_rebase_merge": True,
            "squash_merge_commit_title": "COMMIT_OR_PR_TITLE",
            "squash_merge_commit_message": "COMMIT_MESSAGES",
            "allow_auto_merge": False,
            "allow_update_branch": False,
            "delete_branch_on_merge": False,
            "allow_forking": visibility == "public",
            "has_wiki": True,
        },
        actions={
            "repo": {"enabled": True, "allowed_actions": "all", "sha_pinning_required": False},
            "org": {"enabled_repositories": "all", "allowed_actions": "all", "sha_pinning_required": False},
        },
        workflow={
            "repo": {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False},
            "org": {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False},
        },
        retention=90,
        org={
            "default_repository_permission": "read",
            "members_can_fork_private_repositories": False,
            "members_can_change_repo_visibility": True,
            "members_can_delete_repositories": True,
            "members_can_create_public_repositories": True,
            "two_factor_requirement_enabled": False,
        },
    )


_COLLABORATOR_ROLE = {"pull": "read", "triage": "triage", "push": "write", "maintain": "maintain", "admin": "admin"}
# Ajustes de GET /repos que ve cualquiera; los de integración piden Contents W (08 §4.2; D-65).
_PUBLIC_SETTINGS = frozenset({"allow_forking", "has_wiki"})
_NO_RULESETS = "Upgrade to GitHub Pro or make this repository public to enable this feature."
_RETENTION_MAX = {"public": 90, "private": 400, "internal": 400}  # días (06 REQ-PERM-57)


def _ref_matches(ref: str, pattern: str, default_branch: str) -> bool:
    if pattern == "~ALL":
        return True
    if pattern == "~DEFAULT_BRANCH":
        return ref == f"refs/heads/{default_branch}"
    return fnmatch.fnmatchcase(ref, pattern)


def _ruleset_applies(rs: Mapping[str, Any], branch: str, default_branch: str) -> bool:
    if rs.get("target") != "branch" or rs.get("enforcement") != "active":
        return False
    names = ((rs.get("conditions") or {}).get("ref_name") or {})
    ref = f"refs/heads/{branch}"
    included = any(_ref_matches(ref, p, default_branch) for p in names.get("include") or ())
    excluded = any(_ref_matches(ref, p, default_branch) for p in names.get("exclude") or ())
    return included and not excluded


def _actions_policy(view: FakeForge, level: AdminLevel, *, admin: bool) -> ActionsPolicy | None:
    """``ActionsPolicy`` de ``level`` si quien pregunta administra; si no, ``None`` (REQ-PERM-56)."""
    s = view._s
    data = s.admin.actions.get(level) if level == "repo" or s.admin.org is not None else None
    if data is None or not admin:
        return None
    allowed = data.get("allowed_actions")
    enabled = data.get("enabled") if level == "repo" else data.get("enabled_repositories") != "none"
    selected = data.get("selected") if allowed == "selected" else None
    if selected is not None:
        view._log("actions_policy", "GET", "L", {"level": level, "selected": True})
    return ActionsPolicy(
        level,
        bool(enabled),
        allowed,
        data.get("sha_pinning_required"),
        None if selected is None else bool(selected.get("github_owned_allowed")),
        None if selected is None else bool(selected.get("verified_allowed")),
        None if selected is None else tuple(selected.get("patterns_allowed") or ()),
    )


def _workflow_permissions(s: _State, level: AdminLevel, *, admin: bool) -> WorkflowPermissions | None:
    data = s.admin.workflow.get(level) if level == "repo" or s.admin.org is not None else None
    if data is None or not admin:
        return None
    return WorkflowPermissions(data["default_workflow_permissions"], bool(data["can_approve_pull_request_reviews"]))


def _ruleset_info(ident: int, rs: Mapping[str, Any], *, bypass: bool) -> RulesetInfo:
    """``RulesetInfo`` de la definición guardada; ``bypass_actors`` solo si ``bypass`` (08 §4.2)."""
    payload = {
        k: copy.deepcopy(rs[k])
        for k in ("target", "enforcement", "conditions", "bypass_actors", "rules")
        if k in rs and (bypass or k != "bypass_actors")
    }
    return RulesetInfo(ident, str(rs.get("name", "")), str(rs.get("target", "")), str(rs.get("enforcement", "")), payload)


class _State:
    """Estado compartido por todas las vistas (``as_user``) de un FakeForge."""

    def __init__(
        self,
        repo: RepoRef,
        profile: str,
        default_branch: str,
        clock: FakeClock,
        *,
        visibility: str,
        write_mode: str = "git",
        graphql: bool = True,
        transport: str = "http",
    ) -> None:
        self.lock = threading.RLock()
        self.repo = repo
        self.profile = profile
        self.default_branch = default_branch
        self.clock = clock
        self.visibility = visibility
        self.write_mode = write_mode  # 08 §8.2: "api" en una sesión web
        self.graphql = graphql  # False: sesión web sin GraphQL general (08 §8.1)
        self.transport = transport
        self.users: dict[str, _User] = {}
        self.commits: dict[str, _Commit] = {}
        self.refs: dict[str, str] = {}
        self.ref_activity: dict[str, list[RefActivity]] = {}
        self.issues: dict[int, _IssueRec] = {}
        self.pulls: dict[int, _PullRec] = {}
        self.checks: dict[str, list[_CheckRec]] = {}
        self.codeowners_errors: dict[str, list[str]] = {}
        self.workflow_runs: list[_RunRec] = []
        self.labels: dict[str, Label] = {}
        self.admin = _default_admin(visibility)  # configuración que solo ve ForgeAdmin (08 §2.2)
        self.next_id: dict[str, int] = {"number": 1, "id": 1, "user": 1001}
        # Límite de la API por cuenta (login en minúsculas): en GitHub cada credencial
        # tiene el suyo, y cada vista representa otra cuenta (REQ-FORGE-51).
        self.blocked_until: dict[str, datetime] = {}
        self.faults = FaultSet()
        self.calls = CallLog()

    # --- utilidades -------------------------------------------------------

    def now(self) -> datetime:
        return self.clock.now()

    def new_id(self, counter: str = "id") -> int:
        with self.lock:
            value = self.next_id[counter]
            self.next_id[counter] = value + 1
            return value

    def user(self, login: str) -> _User | None:
        return self.users.get(login.strip().casefold()) if isinstance(login, str) else None

    def ensure_user(self, login: str, *, operator: bool = False) -> _User:
        """La cuenta ``login``; si no existe, la crea.

        Una cuenta implícita (que aparece por ``as_user`` o como autora sembrada)
        tiene solo lectura, como cualquier cuenta en un repo público, y no es
        colaboradora. La del constructor (``operator``) y la del CI tienen
        ``write``. ``seed_user`` fija el permiso de las demás.
        """
        if not isinstance(login, str) or not login.strip():
            raise ValueError("login must be a non-empty str")
        with self.lock:
            found = self.user(login)
            if found is None:
                ci = same_login(login, CI_LOGIN)
                token_kind = "app_installation" if ci else "fine_grained_pat"
                privileged = ci or operator
                found = _User(
                    login.strip(), self.new_id("user"), "write" if privileged else "read", token_kind,
                    member=privileged,
                )
                self.users[found.login.casefold()] = found
            return found

    def display(self, login: str) -> str:
        found = self.user(login)
        return found.login if found is not None else login

    def has_push(self, login: str) -> bool:
        found = self.user(login)
        return found is not None and _PERM_LEVEL[found.permission] >= _PERM_LEVEL["write"]

    def assignable(self, login: str, rec: _IssueRec | _PullRec) -> bool:
        # GitHub: quien tiene write, los miembros con lectura y quien participa en el Issue o PR.
        user = self.user(login)
        if user is None:
            return False
        if user.member or self.has_push(login) or same_login(rec.author, login):
            return True
        return any(same_login(c.author, login) for c in rec.comments)

    def check_blocked(self, login: str) -> None:
        key = login.casefold()
        with self.lock:
            until = self.blocked_until.get(key)
            if until is not None and self.now() < until:
                raise RateLimited("rate limited: the adapter is blocked until reset_at", reset_at=until)
            if until is not None:
                del self.blocked_until[key]

    def block(self, login: str, until: datetime) -> None:
        key = login.casefold()
        with self.lock:
            current = self.blocked_until.get(key)
            if current is None or until > current:
                self.blocked_until[key] = until

    def branch_rules(self, branch: str) -> list[dict[str, object]]:
        """Reglas de los rulesets activos que aplican a ``branch`` (``GET rules/branches/{b}``)."""
        out: list[dict[str, object]] = []
        for ident, rs in sorted(self.admin.rulesets.items()):
            if not _ruleset_applies(rs, branch, self.default_branch):
                continue
            for rule in rs.get("rules") or ():
                item: dict[str, object] = {"type": rule.get("type")}
                if "parameters" in rule:
                    item["parameters"] = copy.deepcopy(rule["parameters"])
                item.update({"ruleset_source_type": "Repository",
                             "ruleset_source": f"{self.repo.owner}/{self.repo.name}", "ruleset_id": ident})
                out.append(item)
        return out

    def url(self, *parts: object) -> str:
        return "/".join(["https://forge.invalid", self.repo.owner, self.repo.name, *map(str, parts)])

    # --- commits y refs -----------------------------------------------------

    def resolve(self, ref: str) -> str | None:
        if not isinstance(ref, str) or not ref:
            return None
        if _SHA_RE.fullmatch(ref) and ref in self.commits:
            return ref
        name = ref[5:] if ref.startswith("refs/") else ref
        for key in (name, f"heads/{name}", f"tags/{name}"):
            if key in self.refs:
                return self.refs[key]
        return None

    def register_sha(self, sha: str) -> None:
        if sha not in self.commits:
            now = self.now()
            self.commits[sha] = _Commit(sha, None, [], "", None, now, now)

    def ancestors(self, sha: str) -> list[str]:
        """Ancestros de ``sha`` (incluido) en orden de recorrido en anchura."""
        seen: dict[str, None] = {}
        queue = [sha]
        while queue:
            cur = queue.pop(0)
            if cur in seen:
                continue
            seen[cur] = None
            commit = self.commits.get(cur)
            if commit is not None:
                queue.extend(commit.parents)
        return list(seen)

    def is_ancestor(self, ancestor: str, sha: str) -> bool:
        return ancestor in self.ancestors(sha)

    def merge_base(self, a: str, b: str) -> str | None:
        in_a = set(self.ancestors(a))
        for cur in self.ancestors(b):
            if cur in in_a:
                return cur
        return None

    def topo(self, shas: set[str], head: str) -> list[str]:
        """``shas`` en orden topológico (padres antes que hijos) desde ``head``."""
        out: list[str] = []
        seen: set[str] = set()
        stack: list[tuple[str, bool]] = [(head, False)]
        while stack:
            cur, done = stack.pop()
            if done:
                out.append(cur)
                continue
            if cur in seen or cur not in shas:
                continue
            seen.add(cur)
            stack.append((cur, True))
            commit = self.commits.get(cur)
            for parent in reversed(commit.parents if commit else []):
                stack.append((parent, False))
        return out

    def ahead(self, base_sha: str, head_sha: str) -> list[str]:
        ahead = set(self.ancestors(head_sha)) - set(self.ancestors(base_sha))
        return self.topo(ahead, head_sha)

    def add_activity(
        self, key: str, activity_type: str, actor: str | None, before: str, after: str
    ) -> None:
        self.ref_activity.setdefault(key, []).append(
            RefActivity(activity_type, actor, self.now(), before, after)  # type: ignore[arg-type]
        )

    def commit_info(self, sha: str) -> CommitInfo | None:
        commit = self.commits.get(sha)
        if commit is None:
            return None
        return CommitInfo(sha, commit.message, commit.author, commit.author_date, commit.committer_date)

    # --- Issues y PR ------------------------------------------------------

    def item(self, number: int) -> _IssueRec | _PullRec | None:
        return self.issues.get(number) or self.pulls.get(number)

    def items(self) -> list[_IssueRec | _PullRec]:
        return [*self.issues.values(), *self.pulls.values()]

    def canonical_label(self, name: str) -> str:
        for existing in self.labels:
            if existing.casefold() == name.casefold():
                return existing
        self.labels[name] = Label(name)
        return name

    def issue_of(self, rec: _IssueRec) -> Issue:
        return Issue(
            rec.number,
            rec.title,
            rec.body,
            rec.state,  # type: ignore[arg-type]
            rec.state_reason,  # type: ignore[arg-type]
            tuple(rec.labels),
            tuple(rec.assignees),
            rec.author,
            rec.created_at,
            rec.updated_at,
            rec.closed_at,
            self.url("issues", rec.number),
        )

    def pull_of(self, rec: _PullRec, *, detail: bool = False) -> PullRequest:
        """El PR como lo da la API; ``merged_by`` solo con ``detail`` (``GET pulls/{n}``, 08 §4.2)."""
        return PullRequest(
            rec.number,
            rec.node_id,
            rec.title,
            rec.body,
            rec.state,  # type: ignore[arg-type]
            rec.draft,
            rec.merged,
            rec.merged_at,
            rec.head_branch,
            rec.head_sha,
            rec.base_branch,
            rec.author,
            tuple(rec.labels),
            tuple(rec.requested_reviewers),
            rec.updated_at,
            self.url("pull", rec.number),
            same_repo=rec.same_repo,
            merged_by=rec.merged_by if detail else None,
            created_at=rec.created_at,
            merge_commit_sha=rec.merge_commit_sha,
        )

    def comment_of(self, number: int, rec: _CommentRec) -> Comment:
        kind = "pull" if number in self.pulls else "issues"
        url = self.url(kind, f"{number}#issuecomment-{rec.id}")
        return Comment(rec.id, rec.author, rec.body, rec.created_at, rec.updated_at, url)

    def issue_view(self, rec: _IssueRec | _PullRec) -> Issue:
        """Respuesta de los endpoints de Issues; un PR también es un Issue para ellos (sin state_reason)."""
        if isinstance(rec, _IssueRec):
            return self.issue_of(rec)
        return Issue(
            rec.number,
            rec.title,
            rec.body,
            rec.state,  # type: ignore[arg-type]
            None,
            tuple(rec.labels),
            tuple(rec.assignees),
            rec.author,
            rec.created_at,
            rec.updated_at,
            rec.closed_at,
            self.url("pull", rec.number),
        )

    def pull_commits(self, rec: _PullRec) -> list[str]:
        if rec.commits is not None:
            return list(rec.commits)
        base_sha = self.refs.get(f"heads/{rec.base_branch}")
        if base_sha is None or rec.head_sha not in self.commits:
            return [rec.head_sha] if rec.head_sha in self.commits else []
        return self.ahead(base_sha, rec.head_sha)

    def pull_files(self, base_branch: str, head_sha: str) -> list[ChangedFile] | None:
        """``GET pulls/{n}/files``: diff de tres puntos de la base a la cabeza; None si falta un árbol."""
        base_sha = self.refs.get(f"heads/{base_branch}")
        head = self.commits.get(head_sha)
        if base_sha is None or head is None or head.files is None:
            return None
        merge_base = self.merge_base(base_sha, head_sha)
        mb = self.commits.get(merge_base) if merge_base is not None else None
        if mb is None or mb.files is None:
            return None
        return _diff_files(mb.files, head.files)

    def close_pull(self, rec: _PullRec, now: datetime) -> None:
        # Cierre sin integrar.
        rec.state = "closed"
        rec.closed_at = now
        rec.updated_at = now
        rec.other_events += 1  # closed

    def run_of(self, rec: _RunRec) -> WorkflowRun:
        return WorkflowRun(
            rec.id,
            rec.name,
            rec.event,
            rec.status,
            rec.conclusion,
            rec.head_sha,
            rec.head_branch,
            rec.actor,
            self.url("actions", "runs", rec.id),
            rec.created_at,
        )


# ---------------------------------------------------------------------------
# Utilidades puras
# ---------------------------------------------------------------------------


def _blob_sha(content: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()  # noqa: S324 (id de git)


def _tree_path_ok(path: object) -> bool:
    """Ruta de un archivo del árbol: relativa, con ``/``, sin segmentos vacíos, ``.`` ni ``..``."""
    if not isinstance(path, str) or not path or "\0" in path or "\\" in path or path.startswith("/"):
        return False
    return all(seg not in ("", ".", "..") for seg in path.split("/"))


def _three_way(base: Mapping[str, bytes], ours: Mapping[str, bytes],
               theirs: Mapping[str, bytes]) -> dict[str, bytes] | None:
    """Fusión de tres vías por archivo; ``None`` si los dos lados cambian distinto el mismo archivo."""
    out: dict[str, bytes] = {}
    for path in sorted(set(base) | set(ours) | set(theirs)):
        b, o, t = base.get(path), ours.get(path), theirs.get(path)
        if o == t or t == b:
            value = o
        elif o == b:
            value = t
        else:
            return None
        if value is not None:
            out[path] = value
    return out


def _commit_sha(
    files: Mapping[str, bytes], parents: Sequence[str], message: str, author: str | None, date: datetime
) -> str:
    payload = {
        "tree": {path: _blob_sha(files[path]) for path in sorted(files)},
        "parents": list(parents),
        "message": message,
        "author": author,
        "date": _iso(date),
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha1(b"commit " + raw).hexdigest()  # noqa: S324 (id de contenido)


def _check_sha(sha: object) -> str:
    if not isinstance(sha, str) or _SHA_RE.fullmatch(sha) is None:
        raise ValueError(f"expected a 40-char lowercase hex sha, got {sha!r}")
    return sha


def _valid_ref_name(name: str) -> bool:
    # Subconjunto de git check-ref-format.
    if not isinstance(name, str) or not name or name == "@":
        return False
    if name.startswith("/") or name.endswith(("/", ".", ".lock")):
        return False
    if "//" in name or ".." in name or "@{" in name:
        return False
    if any(c in " ~^:?*[\\" or ord(c) < 32 or ord(c) == 127 for c in name):
        return False
    return not any(part.startswith(".") for part in name.split("/"))


def _op_marker(op_id: str) -> str:
    # Gramática de 00-marco §5.14; lo agrega el adaptador al final del cuerpo (08 §4.7).
    return f"<!-- {_meta.NAME}:op v1 id={op_id} -->"


def _with_marker(body: str, op_id: str) -> str:
    marker = _op_marker(op_id)
    if marker in body:
        return body
    if not body:
        return marker
    return body + ("\n" if body.endswith("\n") else "\n\n") + marker


def _check_op_id(op_id: object) -> str:
    if not isinstance(op_id, str) or _OP_ID_RE.fullmatch(op_id) is None:
        raise ValueError(f"invalid op_id (expected [A-Za-z0-9._-]+): {op_id!r}")
    return op_id


def _casefold_set(values: Iterable[str]) -> set[str]:
    return {v.strip().casefold() for v in values}


def _dedupe_logins(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if not any(same_login(value, seen) for seen in out):
            out.append(value)
    return out


def _diff_files(old: Mapping[str, bytes], new: Mapping[str, bytes]) -> list[ChangedFile]:
    added = sorted(p for p in new if p not in old)
    removed = sorted(p for p in old if p not in new)
    modified = sorted(p for p in new if p in old and new[p] != old[p])
    out: list[ChangedFile] = []
    # Renombres exactos: mismo contenido en una ruta quitada y otra agregada.
    renamed_from: dict[str, str] = {}
    free = list(removed)
    for path in added:
        for src in free:
            if old[src] == new[path]:
                renamed_from[path] = src
                free.remove(src)
                break
    for path in sorted(set(added) | set(modified) | set(free)):
        if path in renamed_from:
            out.append(ChangedFile(path, "renamed", renamed_from[path]))
        elif path in modified:
            out.append(ChangedFile(path, "modified"))
        elif path in added:
            out.append(ChangedFile(path, "added"))
        else:
            out.append(ChangedFile(path, "removed"))
    return out


# X-OAuth-Scopes por omisión (08 §5.2): un classic con repo; gh auth login pide repo, read:org y gist.
_DEFAULT_SCOPES: Mapping[str, tuple[str, ...]] = {
    "classic_pat": ("repo",),
    "oauth": ("gist", "read:org", "repo"),
}


def _token_scope(user: _User) -> tuple[str, tuple[str, ...] | None]:
    """``(token_scope, oauth_scopes)`` de 08 §5.2 según el tipo de token de la cuenta."""
    kind = user.token_kind
    if kind in _DEFAULT_SCOPES:  # ghp_ y gho_: la única respuesta con X-OAuth-Scopes
        scopes = user.scopes if user.scopes is not None else _DEFAULT_SCOPES[kind]
        return ("broad" if "repo" in scopes else "unknown"), tuple(scopes)
    if kind == "fine_grained_pat":
        return user.token_scope or "repo_restricted", None
    if kind == "app_user":
        return "repo_restricted", None
    if kind == "app_installation":
        return "ci", None
    return "unknown", None  # proxy-injected y otros


def _overall(runs: Sequence[CheckRun]) -> Literal["success", "failure", "pending", "none"]:
    # 08 §4.6
    if not runs:
        return "none"
    if any(r.conclusion in _FAILED for r in runs):
        return "failure"
    if all(r.status == "completed" and r.conclusion in _PASSED for r in runs):
        return "success"
    return "pending"


# ---------------------------------------------------------------------------
# Petición simulada
# ---------------------------------------------------------------------------


class _Request:
    """Contexto de la petición principal de un método: fallas, registro y lock."""

    def __init__(
        self,
        forge: FakeForge,
        method: str,
        http: str,
        kind: CallKind,
        args: Mapping[str, Any],
        pre: Sequence[tuple[str, CallKind, Mapping[str, Any]]] = (),
    ) -> None:
        self._forge = forge
        self._method = method
        self._http = http
        self._kind = kind
        self._args = args
        self._pre = pre
        self._failure: Fault | None = None
        self._call: Call | None = None

    def __enter__(self) -> _Request:
        s = self._forge._s
        actor = self._forge.viewer
        s.check_blocked(actor)
        faults = s.faults._match(self._method)
        self._failure = next((f for f in faults if f.kind not in ("latency", "race")), None)
        for http, kind, args in self._pre:
            s.calls._record(self._method, http, kind, actor, args).outcome = "ok"
        self._call = s.calls._record(self._method, self._http, self._kind, actor, self._args)
        for fault in faults:
            if fault.kind == "latency":
                time.sleep(fault.delay_s)  # fuera del lock: simula la red
        s.lock.acquire()
        current: Fault | None = None
        try:
            for fault in faults:
                if fault.kind == "race":
                    current = fault
                    assert fault.race is not None
                    fault.race(s)
            if self._failure is not None and self._failure.kind not in _AFTER_EFFECT:
                current = self._failure
                self._apply(self._failure)
        except BaseException:
            s.lock.release()
            self._call.outcome = current.kind if current is not None else "error"
            raise
        return self

    def _apply(self, fault: Fault) -> None:
        # Fallas que responden con error antes del efecto.
        s = self._forge._s
        kind = fault.kind
        if kind == "timeout_before":
            raise NetworkTimeout("read timeout (injected; the request did not reach the forge)")
        if kind == "unreachable":
            raise NetworkUnreachable("connection refused (injected)")
        if kind == "rate_limit_primary":
            reset = s.now() + _PRIMARY_RESET
            s.block(self._forge.viewer, reset)
            raise RateLimited("API rate limit exceeded (injected)", reset_at=reset, http_status=403)
        if kind == "rate_limit_secondary":
            reset = s.now() + _SECONDARY_RESET
            s.block(self._forge.viewer, reset)
            raise RateLimited("You have exceeded a secondary rate limit (injected)", reset_at=reset, http_status=403)
        statuses = {
            "http_401": (401, "Bad credentials"),
            "http_403": (403, "Resource not accessible by personal access token"),
            "http_404": (404, "Not Found"),
            "http_409": (409, "Conflict"),
            "http_422": (422, "Validation Failed"),
            "http_5xx": (502, "Bad Gateway"),
        }
        if kind in statuses:
            raise _HttpError(*statuses[kind])
        raise AssertionError(f"not a before-effect fault: {kind!r}")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._forge._s.lock.release()
        assert self._call is not None
        if exc is None:
            failure = self._failure
            if failure is not None and failure.kind == "timeout_after":
                self._call.outcome = "timeout_after"
                raise NetworkTimeout("read timeout (injected; the forge applied the effect)")
            if failure is not None and failure.kind == "http_5xx_after":
                self._call.outcome = "http_5xx_after"
                raise _HttpError(502, "Bad Gateway (injected; the forge applied the effect)")
            self._call.outcome = "ok"
        else:
            self._call.outcome = _outcome_of(exc)


def _outcome_of(exc: BaseException) -> str:
    if isinstance(exc, _HttpError):
        return f"http_{exc.status}"
    if isinstance(exc, ForgeError):
        return exc.kind
    return type(exc).__name__


# ---------------------------------------------------------------------------
# FakeForge
# ---------------------------------------------------------------------------


class FakeForge:
    """Forja en memoria que cumple ``ForgeAdapter`` (08 §10.1)."""

    repo: RepoRef
    faults: FaultSet
    calls: CallLog
    viewer: str

    def __init__(
        self,
        repo: RepoRef,
        viewer: str,
        profile: Literal["enforced", "advisory"] = "enforced",
        clock: FakeClock | None = None,
        default_branch: str = "main",
        *,
        write_mode: Literal["git", "api"] = "git",
        graphql: bool = True,
        transport: Literal["http", "gh"] = "http",
        visibility: Literal["public", "private", "internal"] | None = None,
    ) -> None:
        """``viewer`` es la cuenta que opera el repo (``write``).

        ``write_mode``, ``graphql`` y ``transport`` simulan la superficie (08 §8: en una
        sesión web el adaptador escribe por git, ``write_mode="git"`` según D-41, con
        ``transport="gh"`` y sin GraphQL general; ``"api"`` es el modo forzado por
        ``<PREFIJO>FORGE_WRITE_MODE`` fuera de la web); ``visibility`` vale por omisión ``public`` en ``enforced`` y
        ``private`` en ``advisory`` (``private`` con ``enforced`` es un repo
        privado de una org Team, 00-marco D-24).
        """
        if not isinstance(repo, RepoRef):
            raise TypeError("repo must be a RepoRef")
        if profile not in ("enforced", "advisory"):
            raise ValueError(f"unknown profile: {profile!r}")
        if not _valid_ref_name(default_branch):
            raise ValueError(f"invalid default branch: {default_branch!r}")
        if clock is not None and not isinstance(clock, FakeClock):
            raise TypeError("clock must be a FakeClock")
        if write_mode not in ("git", "api"):
            raise ValueError(f"unknown write_mode: {write_mode!r}")
        if not isinstance(graphql, bool):
            raise TypeError("graphql must be a bool")
        if transport not in ("http", "gh"):
            raise ValueError(f"unknown transport: {transport!r}")
        if visibility not in (None, "public", "private", "internal"):
            raise ValueError(f"unknown visibility: {visibility!r}")
        state = _State(
            repo,
            profile,
            default_branch,
            clock or FakeClock(),
            visibility=visibility or ("public" if profile == "enforced" else "private"),
            write_mode=write_mode,
            graphql=graphql,
            transport=transport,
        )
        state.ensure_user(viewer, operator=True)
        self._bind(state, viewer)

    def _bind(self, state: _State, viewer: str) -> None:
        self._s = state
        self.repo = state.repo
        self.faults = state.faults
        self.calls = state.calls
        self.viewer = state.ensure_user(viewer).login
        self._identity: Identity | None = None
        self._caps: Capabilities | None = None

    def __repr__(self) -> str:
        return f"FakeForge({self.repo.owner}/{self.repo.name}, viewer={self.viewer!r})"

    # --- vistas y propiedades de prueba ------------------------------------

    @property
    def clock(self) -> FakeClock:
        return self._s.clock

    @property
    def profile(self) -> str:
        return self._s.profile

    @property
    def default_branch(self) -> str:
        return self._s.default_branch

    @property
    def graphql(self) -> bool:
        """¿Hay GraphQL general? En una sesión web, no (08 §8.1); lo usa ``mark_ready_for_review`` (H3)."""
        return self._s.graphql

    def as_user(self, login: str) -> FakeForge:
        """Otra vista sobre el mismo estado, con ``login`` como cuenta autenticada.

        Si la cuenta no existe, se crea con solo lectura: ``seed_user`` le da más permiso.
        """
        view = FakeForge.__new__(FakeForge)
        view._bind(self._s, login)
        return view

    # --- utilidades internas ----------------------------------------------

    def _request(
        self,
        method: str,
        http: str,
        kind: CallKind,
        args: Mapping[str, Any],
        pre: Sequence[tuple[str, CallKind, Mapping[str, Any]]] = (),
    ) -> _Request:
        return _Request(self, method, http, kind, args, pre)

    def _log(self, method: str, http: str, kind: CallKind, args: Mapping[str, Any]) -> None:
        self._s.calls._record(method, http, kind, self.viewer, args).outcome = "ok"

    def _pages(self, method: str, n: int, args: Mapping[str, Any], *, max_pages: int = MAX_PAGES) -> None:
        # REQ-FORGE-8: una petición por página de 100 y tope de 10 páginas (30 para los archivos de un PR).
        pages = max(1, math.ceil(n / PAGE_SIZE))
        for page in range(2, min(pages, max_pages) + 1):
            self._log(method, "GET", "L", {**args, "page": page})
        if pages > max_pages:
            raise ForgeValidationError(
                f"{method}: more than {max_pages * PAGE_SIZE} items; refusing a truncated list"
            )

    def _classify(self, method: str, e: _HttpError) -> ForgeError:
        if e.status == 401:
            return ForgeAuthError(e.message, http_status=401)
        if e.status == 403:
            perm = _PERMISSIONS.get(method) or _ADMIN_PERMISSIONS.get(method)
            hint = f"X-Accepted-GitHub-Permissions: {perm}" if perm else None
            return ForgePermissionError(e.message, http_status=403, hint=hint)
        if e.status in (404, 410):
            return ForgeNotFound(e.message, http_status=e.status)
        if e.status >= 500:
            return ForgeUnavailable(e.message, http_status=e.status)
        return ForgeValidationError(e.message, http_status=e.status)

    def _require_push(self) -> None:
        if not self._s.has_push(self.viewer):
            raise _HttpError(403, "Resource not accessible by personal access token")

    def _unsupported(self, method: str) -> NoReturn:
        raise ForgeUnsupported(f"{method}: not implemented by the fake forge until H3")

    def _item(self, number: int) -> _IssueRec | _PullRec:
        rec = self._s.item(number)
        if rec is None:
            raise _HttpError(404, "Not Found")
        return rec

    def _issue_only(self, number: int) -> _IssueRec:
        rec = self._item(number)
        if isinstance(rec, _PullRec):
            raise ForgeValidationError(f"#{number} is a pull request, not an issue (REQ-FORGE-9)")
        return rec

    def _confirm_ref(self, method: str, branch: str, namespace: str) -> Ref | None:
        # GET de confirmación (08 §4.4); no le aplican fallas.
        self._log(method, "GET", "L", {"branch": branch, "namespace": namespace})
        with self._s.lock:
            sha = self._s.refs.get(f"{namespace}/{branch}")
        return None if sha is None else Ref(branch, sha)

    # --- identidad y capacidades ------------------------------------------

    def identity(self, *, refresh: bool = False) -> Identity:
        if self._identity is not None and not refresh:
            return self._identity
        s = self._s
        if same_login(self.viewer, CI_LOGIN):
            # En CI no hay llamada (REQ-FORGE-43).
            ident = Identity(self.viewer, None, "ci", "GITHUB_TOKEN", "app_installation", "ci", None, None, s.now())
        else:
            try:
                with self._request("identity", "GET", "L", {}):
                    user = s.ensure_user(self.viewer)
                    kind: Literal["user", "bot", "ci"] = "bot" if user.login.endswith("[bot]") else "user"
                    scope, oauth_scopes = _token_scope(user)
                    ident = Identity(
                        user.login,
                        user.id,
                        kind,
                        token_sources(_meta.ENV_PREFIX)[0],
                        user.token_kind,  # type: ignore[arg-type]
                        scope,  # type: ignore[arg-type]
                        oauth_scopes,
                        None,
                        s.now(),
                    )
            except _HttpError as e:
                raise self._classify("identity", e) from e
        self._identity = ident
        return ident

    def server_time(self) -> datetime | None:
        """La hora del reloj del falso: la de la cabecera ``Date`` de GitHub (05 §9.19)."""
        return self._s.now()

    def capabilities(self, *, refresh: bool = False) -> Capabilities:
        if self._caps is not None and not refresh:
            return self._caps
        s = self._s
        try:
            with self._request("capabilities", "GET", "L", {}):
                user = s.ensure_user(self.viewer)
                enforced = s.profile == "enforced"
                # 08 §3.2: sin ver el plan, el sondeo de un repo privado con reglas no es concluyente
                unknown = enforced and s.visibility == "private" and not s.admin.plan_visible
                applied = s.branch_rules(s.default_branch) if enforced else []
                rules: BaseRules | None = None
                if applied:
                    rules = rules_summary(applied)
                elif enforced and s.admin.protected_by_default:
                    rules = BaseRules("ruleset", 1, True, True, True, (), True)
                elif enforced:  # repo recién creado: la forja puede exigir reglas, pero no hay ninguna
                    rules = BaseRules("none", None, None, None, None, (), None)
                caps = Capabilities(
                    forge="fake",
                    repo=s.repo,
                    visibility=s.visibility,  # type: ignore[arg-type]
                    owner_type="Organization",
                    viewer_permission=user.permission,  # type: ignore[arg-type]
                    default_branch=s.default_branch,
                    rules_available=None if unknown else enforced,
                    code_owners_enforceable=None if unknown else enforced,
                    base_rules=rules,
                    profile="advisory" if unknown else s.profile,  # type: ignore[arg-type]
                    push_rulesets=False,
                    checks_api=user.token_kind != "fine_grained_pat",
                    transport=s.transport,  # type: ignore[arg-type]
                    write_mode=s.write_mode,  # type: ignore[arg-type]
                    detected_at=s.now(),
                    evidence=(f"fake: profile={s.profile} visibility={s.visibility}",),
                )
        except _HttpError as e:
            raise self._classify("capabilities", e) from e
        self._caps = caps
        return caps

    # --- refs -------------------------------------------------------------

    def get_ref(self, branch: str, *, namespace: RefNamespace = "heads") -> Ref | None:
        _check_namespace(namespace)
        try:
            with self._request("get_ref", "GET", "L", {"branch": branch, "namespace": namespace}):
                sha = self._s.refs.get(f"{namespace}/{branch}")
                if sha is None:
                    raise _HttpError(404, "Not Found")
                return Ref(branch, sha)
        except _HttpError as e:
            if e.status == 404:
                return None
            raise self._classify("get_ref", e) from e

    def list_refs(self, prefix: str, *, namespace: RefNamespace = "heads") -> list[Ref]:
        _check_namespace(namespace)
        args = {"prefix": prefix, "namespace": namespace}
        try:
            with self._request("list_refs", "GET", "L", args):
                start = f"{namespace}/{prefix}"
                cut = len(namespace) + 1
                refs = sorted(
                    (Ref(key[cut:], sha) for key, sha in self._s.refs.items() if key.startswith(start)),
                    key=lambda r: r.branch,
                )
                self._pages("list_refs", len(refs), args)
                return refs
        except _HttpError as e:
            raise self._classify("list_refs", e) from e

    def create_ref(self, branch: str, sha: str, *, namespace: RefNamespace = "heads") -> Ref:
        _check_namespace(namespace)
        s = self._s
        key = f"{namespace}/{branch}"
        try:
            with self._request("create_ref", "POST", "E", {"branch": branch, "sha": sha, "namespace": namespace}):
                self._require_push()
                if not _valid_ref_name(branch):
                    raise _HttpError(422, "Reference name is invalid")
                if not isinstance(sha, str) or sha not in s.commits:
                    raise _HttpError(422, "Object does not exist")
                if key in s.refs:
                    raise _HttpError(422, "Reference already exists")
                if any(other.startswith(key + "/") or key.startswith(other + "/") for other in s.refs):
                    # git no admite refs/heads/task junto a refs/heads/task/12 (directorio y archivo).
                    raise _HttpError(422, "Reference cannot be created: it conflicts with an existing ref")
                s.refs[key] = sha
                s.add_activity(key, "branch_creation", self.viewer, NULL_SHA, sha)
                return Ref(branch, sha)
        except _HttpError as e:
            if e.status in (409, 422):
                existing = self._confirm_ref("create_ref", branch, namespace)
                if existing is not None and existing.sha == sha:
                    return existing  # ya existe con el sha pedido: éxito (REQ-FORGE-28)
                if existing is not None:
                    raise RefAlreadyExists(e.message, existing=existing, http_status=e.status) from e
                raise ForgeValidationError(e.message, http_status=e.status) from e
            err = self._classify("create_ref", e)
            if isinstance(err, ForgeUnavailable):
                return self._create_ref_recover(branch, sha, namespace, err)
            raise err from e
        except NetworkTimeout as e:
            return self._create_ref_recover(branch, sha, namespace, e)

    def _create_ref_recover(self, branch: str, sha: str, namespace: str, cause: ForgeError) -> Ref:
        # Respuesta ambigua: GET de confirmación, sin reintentar el POST (REQ-FORGE-29).
        existing = self._confirm_ref("create_ref", branch, namespace)
        if existing is not None and existing.sha == sha:
            return existing
        if existing is not None:
            raise RefAlreadyExists(str(cause), existing=existing) from cause
        raise cause

    def update_ref(self, branch: str, sha: str) -> Ref:
        s = self._s
        key = f"heads/{branch}"
        try:
            with self._request("update_ref", "PATCH", "E", {"branch": branch, "sha": sha, "force": False}):
                self._require_push()
                current = s.refs.get(key)
                if current is None:
                    raise _HttpError(422, "Reference does not exist")
                if not isinstance(sha, str) or sha not in s.commits:
                    raise _HttpError(422, "Object does not exist")
                if current != sha:
                    if s.profile == "enforced" and branch == s.default_branch:
                        # Ruleset de la base (capabilities().base_rules), sin actores con bypass:
                        # solo se integra por PR (admin_merge en las pruebas).
                        raise _HttpError(
                            422, "Repository rule violations found: changes must be made through a pull request"
                        )
                    if not s.is_ancestor(current, sha):
                        raise _HttpError(422, "Update is not a fast forward")
                    s.refs[key] = sha
                    s.add_activity(key, "push", self.viewer, current, sha)
                    self._sync_pull_heads(branch, sha)
                return Ref(branch, sha)
        except _HttpError as e:
            if e.status == 422:
                existing = self._confirm_ref("update_ref", branch, "heads")
                if existing is not None and existing.sha == sha:
                    return existing  # idempotente con el mismo sha (08 §4.7)
                raise RefConflict(
                    e.message, current=existing.sha if existing else None, http_status=422
                ) from e
            if e.status == 409:
                raise ForgeUnavailable(e.message, http_status=409) from e
            err = self._classify("update_ref", e)
            if isinstance(err, ForgeUnavailable):
                return self._update_ref_recover(branch, sha, err)
            raise err from e
        except NetworkTimeout as e:
            return self._update_ref_recover(branch, sha, e)

    def _update_ref_recover(self, branch: str, sha: str, cause: ForgeError) -> Ref:
        existing = self._confirm_ref("update_ref", branch, "heads")
        if existing is not None and existing.sha == sha:
            return existing
        raise cause

    def _sync_pull_heads(self, branch: str, sha: str) -> None:
        # Un push a la cabeza de un PR abierto mueve su head_sha, y GET pulls/{n}/files pasa
        # a ser el diff de tres puntos de la cabeza nueva. Si el falso no conoce los árboles
        # (shas sembrados sin contenido), se conserva la lista sembrada.
        s = self._s
        for rec in s.pulls.values():
            if rec.state == "open" and rec.same_repo and rec.head_branch == branch:
                rec.head_sha = sha
                rec.updated_at = s.now()
                files = s.pull_files(rec.base_branch, sha)
                if files is not None:
                    rec.files = files

    def delete_ref(self, branch: str, *, namespace: RefNamespace = "heads") -> bool:
        _check_namespace(namespace)
        s = self._s
        key = f"{namespace}/{branch}"
        try:
            with self._request("delete_ref", "DELETE", "E", {"branch": branch, "namespace": namespace}):
                self._require_push()
                current = s.refs.get(key)
                if current is None:
                    raise _HttpError(422, "Reference does not exist")
                if namespace == "heads" and branch == s.default_branch:
                    raise _HttpError(422, "Cannot delete the default branch")
                del s.refs[key]
                s.add_activity(key, "branch_deletion", self.viewer, current, NULL_SHA)
                if namespace == "heads":
                    # GitHub cierra los PR abiertos cuya rama cabeza (del mismo repo) desaparece.
                    now = s.now()
                    for rec in s.pulls.values():
                        if rec.state == "open" and rec.same_repo and rec.head_branch == branch:
                            s.close_pull(rec, now)
                            rec.other_events += 1  # head_ref_deleted
                return True
        except _HttpError as e:
            if e.status in (404, 422):
                if self._confirm_ref("delete_ref", branch, namespace) is None:
                    return False
                raise ForgeValidationError(e.message, http_status=e.status) from e
            if e.status == 409:
                raise ForgeUnavailable(e.message, http_status=409) from e
            raise self._classify("delete_ref", e) from e

    def list_ref_activity(self, branch: str) -> list[RefActivity] | None:
        """Actividad de ``refs/heads/<branch>`` desde su última creación, en orden cronológico.

        La API se pagina de la más reciente hacia atrás hasta llegar al
        ``branch_creation`` (09-ci §4.2: «paginado desde la creación»).
        """
        args = {"branch": branch}
        try:
            with self._request("list_ref_activity", "GET", "L", args):
                events = sorted(self._s.ref_activity.get(f"heads/{branch}", []), key=lambda a: a.timestamp)
                start = 0
                for i, event in enumerate(events):
                    if event.activity_type == "branch_creation":
                        start = i
                since = events[start:]
                self._pages("list_ref_activity", len(since), args)
                return since
        except _HttpError as e:
            if e.status == 404:
                return None
            raise self._classify("list_ref_activity", e) from e

    # --- commits y contenido ----------------------------------------------

    def create_empty_commit(self, parent: str, message: str) -> str:
        s = self._s
        pre: tuple[tuple[str, CallKind, Mapping[str, Any]], ...] = (("GET", "L", {"sha": parent}),)  # árbol del padre
        try:
            with self._request("create_empty_commit", "POST", "E", {"parent": parent, "message": message}, pre):
                self._require_push()
                base = s.commits.get(parent) if isinstance(parent, str) else None
                if base is None or base.files is None:
                    raise _HttpError(404, "Not Found")
                if not isinstance(message, str):
                    raise _HttpError(422, "Invalid request")
                now = s.now()
                sha = _commit_sha(base.files, [parent], message, self.viewer, now)
                if sha not in s.commits:
                    s.commits[sha] = _Commit(sha, dict(base.files), [parent], message, self.viewer, now, now)
                return sha
        except _HttpError as e:
            raise self._classify("create_empty_commit", e) from e

    def commit_files(
        self,
        branch: str,
        *,
        expected_head: str,
        changes: Sequence[FileChange],
        message: str,
        extra_parents: Sequence[str] = (),
    ) -> str:
        """Commit por la API de datos de git (08 §4.5), sobre el grafo del falso.

        El árbol de ``expected_head`` con ``changes`` (``None`` borra; borrar una ruta ausente es
        ``ForgeValidationError``, como el 422 de GitHub), padres ``[expected_head,
        *extra_parents]`` y la rama en avance rápido: si ya no apunta a ``expected_head``,
        ``RefConflict`` con la punta actual. Como el adaptador, rechaza una ruta inválida o de
        ``.github/workflows/`` antes de llamar (REQ-FORGE-32). El falso no guarda el bit de ejecución.
        """
        for change in changes:
            if not isinstance(change, FileChange) or not _tree_path_ok(change.path):
                raise ForgeValidationError(f"commit_files: invalid path {getattr(change, 'path', change)!r}")
            if change.path.casefold().startswith(".github/workflows/"):  # REQ-FORGE-32
                raise ForgeValidationError(f"commit_files: {change.path} is a workflow (class lead)")
        s = self._s
        key = f"heads/{branch}"
        args = {"branch": branch, "expected_head": expected_head, "changes": [c.path for c in changes],
                "extra_parents": list(extra_parents)}
        try:
            with self._request("commit_files", "POST", "E", args):
                self._require_push()
                base = s.commits.get(expected_head) if isinstance(expected_head, str) else None
                if base is None or base.files is None or any(x not in s.commits for x in extra_parents):
                    raise _HttpError(404, "Not Found")
                files = dict(base.files)
                for change in changes:
                    if change.content is None:
                        if change.path not in files:  # GitHub: 422 de POST git/trees (00-marco D-68)
                            raise ForgeValidationError("commit_files: HTTP 422: GitRPC::BadObjectState", http_status=422)
                        del files[change.path]
                    else:
                        files[change.path] = bytes(change.content)
                parents = [expected_head, *extra_parents]
                now = s.now()
                sha = _commit_sha(files, parents, message, self.viewer, now)
                if sha not in s.commits:
                    s.commits[sha] = _Commit(sha, files, parents, message, self.viewer, now, now)
                current = s.refs.get(key)
                if current != expected_head:
                    raise _HttpError(422, "Update is not a fast forward")
                if s.profile == "enforced" and branch == s.default_branch:
                    raise _HttpError(422, "Repository rule violations found: changes must be made through a pull request")
                s.refs[key] = sha
                s.add_activity(key, "push", self.viewer, current, sha)
                self._sync_pull_heads(branch, sha)
                return sha
        except _HttpError as e:
            if e.status == 422:
                existing = self._confirm_ref("commit_files", branch, "heads")
                raise RefConflict(e.message, current=existing.sha if existing else None, http_status=422) from e
            raise self._classify("commit_files", e) from e

    def merge_into(self, branch: str, source: str, *, message: str) -> str | None:
        """``POST merges`` (08 §4.5) sobre el grafo del falso.

        ``None`` si ``source`` ya está en la rama (204); ``MergeConflict`` si los dos lados cambian
        distinto un mismo archivo desde el merge-base (409); si no, un commit de fusión de tres vías
        con padres ``[punta, source]`` y la rama movida a él. ``ForgeNotFound`` sin la rama o sin
        ``source``.
        """
        s = self._s
        key = f"heads/{branch}"
        try:
            with self._request("merge_into", "POST", "E", {"branch": branch, "source": source, "message": message}):
                self._require_push()
                head = s.refs.get(key)
                theirs = s.resolve(source)
                if head is None or theirs is None:
                    raise _HttpError(404, "Not Found")
                if s.is_ancestor(theirs, head):
                    return None
                base_sha = s.merge_base(head, theirs)
                ours_files, theirs_files = s.commits[head].files, s.commits[theirs].files
                base_files = s.commits[base_sha].files if base_sha is not None else {}
                if ours_files is None or theirs_files is None or base_files is None:
                    raise ForgeUnsupported("merge_into: the fake does not know these trees")
                merged = _three_way(base_files, ours_files, theirs_files)
                if merged is None:
                    raise _HttpError(409, "Merge conflict")
                if s.profile == "enforced" and branch == s.default_branch:
                    raise _HttpError(409, "Repository rule violations found: changes must be made through a pull request")
                now = s.now()
                sha = _commit_sha(merged, [head, theirs], message, self.viewer, now)
                s.commits[sha] = _Commit(sha, merged, [head, theirs], message, self.viewer, now, now)
                s.refs[key] = sha
                s.add_activity(key, "push", self.viewer, head, sha)
                self._sync_pull_heads(branch, sha)
                return sha
        except _HttpError as e:
            if e.status == 409:
                raise MergeConflict(e.message, http_status=409) from e
            raise self._classify("merge_into", e) from e

    def get_commit(self, sha: str) -> CommitInfo:
        try:
            with self._request("get_commit", "GET", "L", {"sha": sha}):
                resolved = self._s.resolve(sha)
                info = self._s.commit_info(resolved) if resolved else None
                if info is None:
                    raise _HttpError(404, "No commit found for SHA")
                return info
        except _HttpError as e:
            if e.status in (404, 422):
                raise ForgeNotFound(e.message, http_status=e.status) from e
            raise self._classify("get_commit", e) from e

    def get_file(self, path: str, ref: str) -> FileContent | None:
        s = self._s
        args = {"path": path, "ref": ref}
        try:
            with self._request("get_file", "GET", "L", args):
                resolved = s.resolve(ref)
                commit = s.commits.get(resolved) if resolved else None
                if commit is None or commit.files is None or path not in commit.files:
                    raise _HttpError(404, "Not Found")
                content = commit.files[path]
                if len(content) > _BIG_FILE:
                    self._log("get_file", "GET", "L", {**args, "accept": "raw"})
                return FileContent(path, ref, _blob_sha(content), content)
        except _HttpError as e:
            if e.status == 404:
                return None
            raise self._classify("get_file", e) from e

    def compare(self, base: str, head: str, *, with_commits: bool = False) -> Comparison:
        s = self._s
        try:
            with self._request("compare", "GET", "L", {"base": base, "head": head, "with_commits": with_commits}):
                base_sha = s.resolve(base)
                head_sha = s.resolve(head)
                if base_sha is None or head_sha is None:
                    raise _HttpError(404, "Not Found")
                merge_base = s.merge_base(base_sha, head_sha)
                if merge_base is None:
                    raise _HttpError(404, "No common ancestor between base and head")
                mb_files = s.commits[merge_base].files if merge_base in s.commits else None
                head_files = s.commits[head_sha].files if head_sha in s.commits else None
                if mb_files is None or head_files is None:
                    raise _HttpError(404, "Not Found")
                ahead = s.ahead(base_sha, head_sha)
                behind = set(s.ancestors(base_sha)) - set(s.ancestors(head_sha))
                if base_sha == head_sha:
                    status: Literal["identical", "ahead", "behind", "diverged"] = "identical"
                elif not behind:
                    status = "ahead"
                elif not ahead:
                    status = "behind"
                else:
                    status = "diverged"
                files = _diff_files(mb_files, head_files)
                commits: tuple[CommitInfo, ...] = ()
                if with_commits:
                    infos = (s.commit_info(sha) for sha in ahead[:_MAX_COMMITS])
                    commits = tuple(i for i in infos if i is not None)
                return Comparison(
                    base,
                    head,
                    status,
                    len(ahead),
                    len(behind),
                    merge_base,
                    tuple(files[:_MAX_COMPARE_FILES]),
                    len(files) > _MAX_COMPARE_FILES,
                    commits,
                )
        except _HttpError as e:
            raise self._classify("compare", e) from e

    def codeowners_errors(self, ref: str) -> list[str]:
        s = self._s
        try:
            with self._request("codeowners_errors", "GET", "L", {"ref": ref}):
                if ref in s.codeowners_errors:
                    return list(s.codeowners_errors[ref])
                resolved = s.resolve(ref)
                for key, errors in sorted(s.codeowners_errors.items()):
                    if resolved is not None and s.resolve(key) == resolved:
                        return list(errors)
                return []
        except _HttpError as e:
            if e.status == 404:
                return []
            raise self._classify("codeowners_errors", e) from e

    # --- Issues -----------------------------------------------------------

    def get_issue(self, number: int) -> Issue | None:
        try:
            with self._request("get_issue", "GET", "L", {"number": number}):
                return self._s.issue_of(self._issue_only(number))
        except _HttpError as e:
            if e.status in (404, 410):
                return None
            raise self._classify("get_issue", e) from e

    def list_issues(
        self,
        *,
        labels: Sequence[str] = (),
        state: str = "all",
        since: datetime | None = None,
    ) -> list[Issue]:
        args = {"labels": list(labels), "state": state, "since": since}
        try:
            with self._request("list_issues", "GET", "L", args):
                if state not in ("open", "closed", "all"):
                    raise _HttpError(422, "Validation Failed")
                wanted = _casefold_set(labels)
                limit = _aware(since) if since is not None else None

                def selected(rec: _IssueRec | _PullRec) -> bool:
                    return (
                        (state == "all" or rec.state == state)
                        and wanted <= _casefold_set(rec.labels)
                        and (limit is None or rec.updated_at >= limit)
                    )

                recs = [rec for rec in self._s.issues.values() if selected(rec)]
                recs.sort(key=lambda r: (r.created_at, r.number), reverse=True)
                # GitHub pagina Issues y PR juntos; el adaptador quita los PR de cada página
                # (REQ-FORGE-9), así que las páginas y el tope de REQ-FORGE-8 cuentan los dos.
                pulls = sum(1 for rec in self._s.pulls.values() if selected(rec))
                self._pages("list_issues", len(recs) + pulls, args)
                return [self._s.issue_of(r) for r in recs]
        except _HttpError as e:
            raise self._classify("list_issues", e) from e

    def create_issue(self, title: str, body: str, *, labels: Sequence[str] = (), op_id: str) -> Issue:
        """Crea el Issue con el marcador ``op`` al final del cuerpo (08 §4.7).

        No deduplica cuando la respuesta llega bien; tras una falla ambigua (timeout o
        5xx) busca su marcador entre los Issues propios creados desde t0 (CT-ISS-03).
        """
        _check_op_id(op_id)
        s = self._s
        t0 = s.now()
        requested = list(labels)
        args = {"title": title, "body": body, "labels": requested, "op_id": op_id}
        try:
            with self._request("create_issue", "POST", "C", args):
                if not isinstance(title, str) or not title.strip() or not isinstance(body, str):
                    raise _HttpError(422, "Validation Failed")
                applied = [s.canonical_label(n) for n in requested] if s.has_push(self.viewer) else []
                applied = list(dict.fromkeys(applied))
                now = s.now()
                rec = _IssueRec(
                    s.new_id("number"), title, _with_marker(body, op_id), "open", None,
                    applied, [], self.viewer, now, now, None,
                )
                rec.events = [LabelEvent(s.new_id(), "labeled", name, self.viewer, now) for name in applied]
                s.issues[rec.number] = rec
                issue = s.issue_of(rec)
        except _HttpError as e:
            err = self._classify("create_issue", e)
            if not isinstance(err, ForgeUnavailable):
                raise err from e
            issue = self._recover_issue(op_id, t0, err)
        except NetworkTimeout as e:
            issue = self._recover_issue(op_id, t0, e)
        if not _casefold_set(requested) <= _casefold_set(issue.labels):
            raise ForgeValidationError("labels were not applied (REQ-FORGE-11)", http_status=201)
        return issue

    def _recover_issue(self, op_id: str, t0: datetime, cause: ForgeError) -> Issue:
        # GET issues?creator={login}&since={t0}: un Issue propio con el marcador (REQ-FORGE-35).
        # Si no aparece, el POST no tuvo efecto y se relanza la causa, que es reintentable.
        marker = _op_marker(op_id)
        self._log("create_issue", "GET", "L", {"creator": self.viewer, "since": t0, "state": "all"})
        with self._s.lock:
            for rec in sorted(self._s.issues.values(), key=lambda r: (r.created_at, r.number)):
                if same_login(rec.author, self.viewer) and rec.created_at >= t0 and marker in rec.body:
                    return self._s.issue_of(rec)
        raise cause

    def update_issue(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
        state_reason: str | None = None,
    ) -> Issue:
        s = self._s
        args = {"number": number, "title": title, "body": body, "state": state, "state_reason": state_reason}
        try:
            with self._request("update_issue", "PATCH", "E", args):
                rec = self._item(number)  # PATCH issues/{n} también acepta un PR
                user = s.user(self.viewer)
                level = _PERM_LEVEL[user.permission] if user else 0
                if not same_login(rec.author, self.viewer) and level < _PERM_LEVEL["triage"]:
                    raise _HttpError(403, "Must have triage access to edit this issue")
                if state not in (None, "open", "closed") or state_reason not in (None, *_ISSUE_STATE_REASONS):
                    raise _HttpError(422, "Validation Failed")
                if state == "closed" and state_reason == "reopened":
                    raise _HttpError(422, "Validation Failed")
                if state == "open" and state_reason not in (None, "reopened"):
                    raise _HttpError(422, "Validation Failed")
                if isinstance(rec, _PullRec):
                    self._patch_pull_as_issue(rec, title, body, state)
                    return s.issue_view(rec)
                if state is None and state_reason is not None and (rec.state != "closed" or state_reason == "reopened"):
                    raise _HttpError(422, "Validation Failed")
                now = s.now()
                if title is not None:
                    if title != rec.title:
                        rec.other_events += 1  # renamed
                    rec.title = title
                if body is not None:
                    rec.body = body
                if state == "closed":
                    if rec.state == "open":
                        rec.state = "closed"
                        rec.closed_at = now
                        rec.state_reason = state_reason or "completed"
                        rec.other_events += 1  # closed
                    elif state_reason is not None:
                        rec.state_reason = state_reason
                elif state == "open":
                    if rec.state == "closed":
                        rec.state = "open"
                        rec.closed_at = None
                        rec.state_reason = "reopened"
                        rec.other_events += 1  # reopened
                elif state_reason is not None:
                    rec.state_reason = state_reason
                rec.updated_at = now
                return s.issue_of(rec)
        except _HttpError as e:
            raise self._classify("update_issue", e) from e

    def _patch_pull_as_issue(self, rec: _PullRec, title: str | None, body: str | None, state: str | None) -> None:
        # PATCH issues/{n} sobre un PR: título, cuerpo y estado; un PR no tiene state_reason.
        s = self._s
        now = s.now()
        if state == "open" and rec.state == "closed":
            # La rama cabeza de un fork vive en el fork, que el falso no modela: se da por existente.
            if rec.merged or (rec.same_repo and f"heads/{rec.head_branch}" not in s.refs):
                raise _HttpError(422, "Validation Failed")  # integrado, o sin rama cabeza
        if title is not None:
            if title != rec.title:
                rec.other_events += 1  # renamed
            rec.title = title
        if body is not None:
            rec.body = body
        if state == "closed" and rec.state == "open":
            s.close_pull(rec, now)
        elif state == "open" and rec.state == "closed":
            rec.state = "open"
            rec.closed_at = None
            rec.other_events += 1  # reopened
        rec.updated_at = now

    def add_labels(self, number: int, labels: Sequence[str]) -> tuple[str, ...]:
        s = self._s
        requested = list(labels)
        try:
            with self._request("add_labels", "POST", "E", {"number": number, "labels": requested}):
                rec = self._item(number)
                if s.has_push(self.viewer):  # sin push, GitHub las descarta en silencio
                    now = s.now()
                    seen: set[str] = set()
                    for name in requested:
                        canon = s.canonical_label(name)
                        if canon.casefold() in seen:
                            continue
                        seen.add(canon.casefold())
                        if canon.casefold() not in _casefold_set(rec.labels):
                            rec.labels.append(canon)
                        # GitHub a veces registra otro `labeled` aunque la etiqueta ya estuviera; el falso,
                        # siempre: es el caso más exigente para las reglas del último `labeled` (00-marco D-68)
                        rec.events.append(LabelEvent(s.new_id(), "labeled", canon, self.viewer, now))
                    if seen:
                        rec.updated_at = now
                current = tuple(rec.labels)
        except _HttpError as e:
            raise self._classify("add_labels", e) from e
        if not _casefold_set(requested) <= _casefold_set(current):
            raise ForgeValidationError("labels were not applied (REQ-FORGE-11)", http_status=200)
        return current

    def remove_label(self, number: int, label: str) -> tuple[str, ...]:
        s = self._s
        try:
            with self._request("remove_label", "DELETE", "E", {"number": number, "label": label}):
                rec = self._item(number)
                present = [n for n in rec.labels if n.casefold() == label.casefold()]
                if not present:
                    raise _HttpError(404, "Label does not exist")
                if s.has_push(self.viewer):
                    rec.labels = [n for n in rec.labels if n not in present]
                    now = s.now()
                    rec.events.append(LabelEvent(s.new_id(), "unlabeled", present[0], self.viewer, now))
                    rec.updated_at = now
                current = tuple(rec.labels)
        except _HttpError as e:
            if e.status != 404:
                raise self._classify("remove_label", e) from e
            # DELETE 404: idempotente; se leen las etiquetas actuales (08 §4.2).
            self._log("remove_label", "GET", "L", {"number": number})
            with s.lock:
                found = s.item(number)
                if found is None:
                    raise ForgeNotFound("Not Found", http_status=404) from e
                return tuple(found.labels)
        if label.casefold() in _casefold_set(current):
            raise ForgeValidationError("label was not removed (REQ-FORGE-11)", http_status=200)
        return current

    def ensure_labels(self, labels: Sequence[Label]) -> dict[str, Literal["created", "updated", "unchanged"]]:
        wanted = list(labels)
        if not all(isinstance(lab, Label) for lab in wanted):
            raise TypeError("ensure_labels expects Label objects")
        actions: dict[str, Literal["created", "updated", "unchanged"]] = {}
        if not wanted:
            return actions
        s = self._s
        try:
            with self._request("ensure_labels", "GET", "L", {"name": wanted[0].name}):
                for i, lab in enumerate(wanted):
                    if i:
                        self._log("ensure_labels", "GET", "L", {"name": lab.name})
                    existing = next((v for k, v in s.labels.items() if k.casefold() == lab.name.casefold()), None)
                    if existing is None:
                        self._require_push()
                        self._log("ensure_labels", "POST", "E", {"label": lab})
                        s.labels[lab.name] = lab
                        actions[lab.name] = "created"
                    elif existing != lab:
                        self._require_push()
                        self._log("ensure_labels", "PATCH", "E", {"label": lab})
                        del s.labels[existing.name]
                        s.labels[lab.name] = lab
                        for item in s.items():
                            item.labels = [lab.name if n == existing.name else n for n in item.labels]
                        actions[lab.name] = "updated"
                    else:
                        actions[lab.name] = "unchanged"
        except _HttpError as e:
            if e.status == 422:
                # 422 al crear: releer (08 §4.2).
                for lab in wanted:
                    self._log("ensure_labels", "GET", "L", {"name": lab.name})
                with s.lock:
                    names = _casefold_set(s.labels)
                if all(lab.name.casefold() in names for lab in wanted):
                    return {**{lab.name: "unchanged" for lab in wanted}, **actions}  # type: ignore[dict-item]
            raise self._classify("ensure_labels", e) from e
        return actions

    def list_issue_events(self, number: int) -> list[LabelEvent]:
        args = {"number": number}
        try:
            with self._request("list_issue_events", "GET", "L", args):
                rec = self._item(number)
                events = sorted(rec.events, key=lambda ev: (ev.created_at, ev.id))
                # GitHub pagina todos los tipos de evento; el adaptador filtra labeled/unlabeled.
                self._pages("list_issue_events", len(events) + rec.other_events, args)
                return events
        except _HttpError as e:
            if e.status in (404, 410):
                raise ForgeNotFound(e.message, http_status=e.status) from e
            raise self._classify("list_issue_events", e) from e

    def _assign(self, method: str, http: str, number: int, logins: Sequence[str], op: str) -> Issue:
        s = self._s
        requested = list(logins)
        try:
            with self._request(method, http, "E", {"number": number, "logins": requested}):
                rec = self._item(number)  # los endpoints de Issues también aceptan un PR
                if s.has_push(self.viewer):  # sin push, el cambio se descarta en silencio
                    # Solo se pueden asignar las cuentas asignables (State.assignable); las demás se descartan.
                    valid = [s.display(u) for u in requested if s.assignable(u, rec)]
                    if op == "set":
                        new = _dedupe_logins(valid)
                    elif op == "add":
                        new = _dedupe_logins([*rec.assignees, *valid])
                    else:
                        new = [a for a in rec.assignees if not any(same_login(a, u) for u in requested)]
                    if new != rec.assignees:
                        added = [a for a in new if not any(same_login(a, b) for b in rec.assignees)]
                        removed = [b for b in rec.assignees if not any(same_login(a, b) for a in new)]
                        rec.other_events += len(added) + len(removed)  # assigned / unassigned
                        rec.assignees = new
                        rec.updated_at = s.now()
                issue = s.issue_view(rec)
        except _HttpError as e:
            raise self._classify(method, e) from e
        got = _casefold_set(issue.assignees)
        want = _casefold_set(requested)
        ok = got == want if op == "set" else (want <= got if op == "add" else not (want & got))
        if not ok:
            raise ForgeValidationError("assignees were not applied (REQ-FORGE-11)", http_status=200)
        return issue

    def set_assignees(self, number: int, logins: Sequence[str]) -> Issue:
        return self._assign("set_assignees", "PATCH", number, logins, "set")

    def add_assignees(self, number: int, logins: Sequence[str]) -> Issue:
        return self._assign("add_assignees", "POST", number, logins, "add")

    def remove_assignees(self, number: int, logins: Sequence[str]) -> Issue:
        return self._assign("remove_assignees", "DELETE", number, logins, "remove")

    # --- comentarios --------------------------------------------------------

    def list_comments(self, number: int, *, since: datetime | None = None) -> list[Comment]:
        args = {"number": number, "since": since}
        try:
            with self._request("list_comments", "GET", "L", args):
                return self._comments(number, since, args)
        except _HttpError as e:
            raise self._classify("list_comments", e) from e

    def _comments(self, number: int, since: datetime | None, args: Mapping[str, Any]) -> list[Comment]:
        rec = self._item(number)
        limit = _aware(since) if since is not None else None
        items = [c for c in rec.comments if limit is None or c.updated_at >= limit]
        items.sort(key=lambda c: (c.created_at, c.id))
        self._pages("list_comments", len(items), args)
        return [self._s.comment_of(number, c) for c in items]

    def create_comment(self, number: int, body: str, *, op_id: str) -> Comment:
        """Crea un comentario con el marcador ``op`` al final; tras una falla ambigua lo busca (08 §4.7)."""
        _check_op_id(op_id)
        s = self._s
        t0 = s.now()
        try:
            with self._request("create_comment", "POST", "C", {"number": number, "body": body, "op_id": op_id}):
                rec = self._item(number)
                if not isinstance(body, str) or not body.strip():
                    raise _HttpError(422, "Validation Failed")
                marked = _with_marker(body, op_id)
                if len(marked) > MAX_COMMENT_CHARS:
                    raise _HttpError(
                        422, f"Validation Failed: body is too long (maximum is {MAX_COMMENT_CHARS} characters)"
                    )
                now = s.now()
                comment = _CommentRec(s.new_id(), self.viewer, marked, now, now)
                rec.comments.append(comment)
                rec.updated_at = now
                return s.comment_of(number, comment)
        except _HttpError as e:
            err = self._classify("create_comment", e)
            if isinstance(err, ForgeUnavailable):
                return self._recover_comment(number, op_id, t0, err)
            raise err from e
        except NetworkTimeout as e:
            return self._recover_comment(number, op_id, t0, e)

    def _recover_comment(self, number: int, op_id: str, t0: datetime, cause: ForgeError) -> Comment:
        # GET issues/{n}/comments?since=t0: un comentario propio con el marcador (REQ-FORGE-35).
        marker = _op_marker(op_id)
        args = {"number": number, "since": t0}
        self._log("create_comment", "GET", "L", args)
        with self._s.lock:
            rec = self._s.item(number)
            if rec is not None:
                for c in sorted(rec.comments, key=lambda c: (c.created_at, c.id)):
                    if same_login(c.author, self.viewer) and c.created_at >= t0 and marker in c.body:
                        return self._s.comment_of(number, c)
        raise cause

    def update_comment(self, comment_id: int, body: str) -> Comment:
        s = self._s
        try:
            with self._request("update_comment", "PATCH", "E", {"comment_id": comment_id, "body": body}):
                for rec in s.items():
                    for i, c in enumerate(rec.comments):
                        if c.id != comment_id:
                            continue
                        if not same_login(c.author, self.viewer) and not s.has_push(self.viewer):
                            raise _HttpError(403, "Must have push access to edit this comment")
                        if not isinstance(body, str) or not body.strip():
                            raise _HttpError(422, "Validation Failed")
                        if len(body) > MAX_COMMENT_CHARS:
                            raise _HttpError(
                                422, f"Validation Failed: body is too long (maximum is {MAX_COMMENT_CHARS} characters)"
                            )
                        updated = replace(c, body=body, updated_at=s.now())
                        rec.comments[i] = updated
                        return s.comment_of(rec.number, updated)
                raise _HttpError(404, "Not Found")
        except _HttpError as e:
            raise self._classify("update_comment", e) from e

    # --- pull requests y revisiones -----------------------------------------

    def get_pull_request(self, number: int) -> PullRequest | None:
        try:
            with self._request("get_pull_request", "GET", "L", {"number": number}):
                rec = self._s.pulls.get(number)
                if rec is None:
                    raise _HttpError(404, "Not Found")
                return self._s.pull_of(rec, detail=True)  # GET pulls/{n} trae merged_by
        except _HttpError as e:
            if e.status == 404:
                return None
            raise self._classify("get_pull_request", e) from e

    def find_pull_request(self, head_branch: str, *, state: str = "all") -> PullRequest | None:
        """PR más reciente con cabeza ``<owner>:<head_branch>`` (GET pulls?head=…&state=…).

        El filtro ``{owner}:{rama}`` deja fuera los PR desde forks (``same_repo=False``)
        aunque su rama se llame igual; ``list_pull_requests`` sí los lista.
        """
        s = self._s
        try:
            with self._request("find_pull_request", "GET", "L", {"head_branch": head_branch, "state": state}):
                if state not in ("open", "closed", "all"):
                    raise _HttpError(422, "Validation Failed")
                recs = [
                    r
                    for r in s.pulls.values()
                    if r.same_repo and r.head_branch == head_branch and (state == "all" or r.state == state)
                ]
                if not recs:
                    return None
                return s.pull_of(max(recs, key=lambda r: (r.created_at, r.number)))
        except _HttpError as e:
            if e.status == 404:
                return None
            raise self._classify("find_pull_request", e) from e

    def list_pull_requests(self, *, state: str = "open", base: str | None = None) -> list[PullRequest]:
        s = self._s
        args = {"state": state, "base": base}
        try:
            with self._request("list_pull_requests", "GET", "L", args):
                if state not in ("open", "closed", "all"):
                    raise _HttpError(422, "Validation Failed")
                recs = [
                    r
                    for r in s.pulls.values()
                    if (state == "all" or r.state == state) and (base is None or r.base_branch == base)
                ]
                recs.sort(key=lambda r: (r.created_at, r.number), reverse=True)
                self._pages("list_pull_requests", len(recs), args)
                return [s.pull_of(r) for r in recs]
        except _HttpError as e:
            raise self._classify("list_pull_requests", e) from e

    def _pull(self, number: int) -> _PullRec:
        rec = self._s.pulls.get(number)
        if rec is None:
            raise _HttpError(404, "Not Found")
        return rec

    def list_pull_request_files(self, number: int) -> list[ChangedFile]:
        args = {"number": number}
        try:
            with self._request("list_pull_request_files", "GET", "L", args):
                files = list(self._pull(number).files)
                self._pages("list_pull_request_files", len(files), args, max_pages=MAX_FILE_PAGES)
                return files
        except _HttpError as e:
            raise self._classify("list_pull_request_files", e) from e

    def list_pull_request_commits(self, number: int) -> list[CommitInfo]:
        s = self._s
        try:
            with self._request("list_pull_request_commits", "GET", "L", {"number": number}):
                shas = s.pull_commits(self._pull(number))[:_MAX_COMMITS]
                self._pages("list_pull_request_commits", len(shas), {"number": number})
                infos = (s.commit_info(sha) for sha in shas)
                return [i for i in infos if i is not None]
        except _HttpError as e:
            raise self._classify("list_pull_request_commits", e) from e

    def create_pull_request(
        self, *, head: str, base: str, title: str, body: str, draft: bool = False
    ) -> PullRequest:
        """``POST pulls`` (08 §4.2), con las reglas de GitHub de REQ-FORGE-59: un PR abierto por
        cabeza, ninguno sin commits de diferencia y el ``draft`` pedido. Ante el 422 sondea como el
        adaptador real (REQ-FORGE-36): ``PullRequestExists``, ``NoCommitsBetween`` o
        ``ForgeValidationError``."""
        s = self._s
        args = {"head": head, "base": base, "title": title, "draft": bool(draft)}
        try:
            with self._request("create_pull_request", "POST", "C", args):
                self._require_push()
                if not isinstance(title, str) or not title.strip() or not isinstance(body, str):
                    raise _HttpError(422, "Validation Failed")
                head_sha = s.refs.get(f"heads/{head}")
                base_sha = s.refs.get(f"heads/{base}")
                if head_sha is None or base_sha is None:
                    raise _HttpError(422, "Validation Failed: head or base is invalid")
                if any(r.state == "open" and r.same_repo and r.head_branch == head for r in s.pulls.values()):
                    raise _HttpError(422, f"A pull request already exists for {s.repo.owner}:{head}.")
                if not s.ahead(base_sha, head_sha):
                    raise _HttpError(422, f"No commits between {base} and {head}")
                number = s.new_id("number")
                while s.item(number) is not None:
                    number = s.new_id("number")
                now = s.now()
                rec = _PullRec(
                    number=number, node_id=f"PR_fake{number}", title=title, body=body, state="open",
                    draft=bool(draft), merged=False, merged_at=None, head_branch=head, head_sha=head_sha,
                    base_branch=base, author=s.ensure_user(self.viewer).login, labels=[], assignees=[],
                    requested_reviewers=[], created_at=now, updated_at=now, closed_at=None,
                    files=s.pull_files(base, head_sha) or [],
                )
                s.pulls[number] = rec
                return s.pull_of(rec)
        except _HttpError as e:
            if e.status != 422:
                raise self._classify("create_pull_request", e) from e
            try:  # REQ-FORGE-36, con las mismas sondas que GitHubAdapter
                existing = self.find_pull_request(head, state="open")
            except ForgeError:
                existing = None
            if existing is not None:
                raise PullRequestExists(e.message, existing=existing, http_status=422) from e
            try:
                ahead: int | None = self.compare(base, head).ahead_by
            except ForgeError:
                ahead = None
            if ahead == 0:
                raise NoCommitsBetween(e.message, http_status=422) from e
            raise ForgeValidationError(f"create_pull_request: HTTP 422: {e.message}", http_status=422) from e

    def update_pull_request(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
    ) -> PullRequest:
        """``PATCH pulls/{n}`` (08 §4.2): título, cuerpo y estado; nunca ``draft`` (REQ-FORGE-59)."""
        s = self._s
        args = {"number": number, "title": title, "body": body, "state": state}
        try:
            with self._request("update_pull_request", "PATCH", "E", args):
                rec = self._pull(number)
                if not same_login(rec.author, self.viewer) and not s.has_push(self.viewer):
                    raise _HttpError(403, "Must have push access to update this pull request")
                if state not in (None, "open", "closed"):
                    raise _HttpError(422, "Validation Failed")
                self._patch_pull_as_issue(rec, title, body, state)
                return s.pull_of(rec)
        except _HttpError as e:
            raise self._classify("update_pull_request", e) from e

    def mark_ready_for_review(self, number: int) -> PullRequest:
        self._unsupported("mark_ready_for_review")

    def request_reviewers(self, number: int, logins: Sequence[str]) -> PullRequest:
        """``POST pulls/{n}/requested_reviewers`` (08 §4.2): agrega al conjunto. Como GitHub, 422 si
        se pide al autor del PR o a quien no es colaborador del repositorio."""
        s = self._s
        wanted = [str(x) for x in logins]
        try:
            with self._request("request_reviewers", "POST", "E", {"number": number, "reviewers": wanted}):
                rec = self._pull(number)
                if not same_login(rec.author, self.viewer) and not s.has_push(self.viewer):
                    raise _HttpError(403, "Must have push access to request reviewers")
                for login in wanted:
                    if same_login(login, rec.author):
                        raise _HttpError(422, "Review cannot be requested from pull request author.")
                    if s.user(login) is None:
                        raise _HttpError(422, "Reviews may only be requested from collaborators.")
                for login in wanted:
                    if not any(same_login(login, r) for r in rec.requested_reviewers):
                        rec.requested_reviewers.append(s.display(login))
                rec.updated_at = s.now()
                return s.pull_of(rec)
        except _HttpError as e:
            raise self._classify("request_reviewers", e) from e

    def list_reviews(self, number: int) -> list[Review]:
        """Revisiones en orden cronológico; una ``PENDING`` solo la ve su autor (sin fecha, al final)."""
        args = {"number": number}
        try:
            with self._request("list_reviews", "GET", "L", args):
                far = datetime.max.replace(tzinfo=timezone.utc)
                visible = [
                    r for r in self._pull(number).reviews if r.state != "PENDING" or same_login(r.author, self.viewer)
                ]
                reviews = sorted(visible, key=lambda r: (r.submitted_at or far, r.id))
                self._pages("list_reviews", len(reviews), args)
                return reviews
        except _HttpError as e:
            raise self._classify("list_reviews", e) from e

    def submit_review(
        self,
        number: int,
        *,
        event: Literal["APPROVE", "REQUEST_CHANGES"],
        body: str,
        commit_sha: str,
        confirmation: HumanConfirmation,
    ) -> Review:
        """``POST pulls/{n}/reviews`` (08 §4.2) con la confirmación humana de REQ-FORGE-10: de la
        operación de 06 §6 que corresponde al evento y de los últimos 120 s del reloj del falso.
        Como GitHub, 422 si el autor revisa su propio PR, si el PR está cerrado o si
        ``REQUEST_CHANGES`` llega sin cuerpo."""
        expected = _REVIEW_OPERATIONS.get(event)
        if expected is None:
            raise ForgeValidationError(f"submit_review: invalid event {event!r}")
        s = self._s
        operation = getattr(confirmation, "operation", None)
        confirmed_at = getattr(confirmation, "confirmed_at", None)
        now = s.now()
        if (
            operation != expected
            or not isinstance(confirmed_at, datetime)
            or confirmed_at.tzinfo is None
            or not now - CONFIRMATION_WINDOW <= confirmed_at <= now + timedelta(seconds=5)
        ):
            raise ForgeValidationError("submit_review needs a recent human confirmation (REQ-FORGE-10)")
        args = {"number": number, "event": event, "commit_sha": commit_sha}
        try:
            with self._request("submit_review", "POST", "C", args):
                rec = self._pull(number)
                if rec.state != "open":
                    raise _HttpError(422, "Can not review a closed pull request")
                if same_login(rec.author, self.viewer):
                    raise _HttpError(422, "Can not approve your own pull request")
                if event == "REQUEST_CHANGES" and not body.strip():
                    raise _HttpError(422, "Review body is required for REQUEST_CHANGES")
                user = s.ensure_user(self.viewer)
                state: Literal["APPROVED", "CHANGES_REQUESTED"] = "APPROVED" if event == "APPROVE" else "CHANGES_REQUESTED"
                review = Review(s.new_id(), user.login, user.id, state, commit_sha, s.now())
                rec.reviews.append(review)
                rec.updated_at = s.now()
                return review
        except _HttpError as e:
            raise self._classify("submit_review", e) from e

    # --- checks y workflows ---------------------------------------------------

    def get_checks(self, sha: str) -> Checks:
        """08 §4.6: check runs si el token puede leerlos; si es fine-grained, runs de Actions."""
        s = self._s
        user = s.user(self.viewer)
        fine_grained = user is not None and user.token_kind == "fine_grained_pat"
        http_args = {"sha": sha, "source": "workflow_run" if fine_grained else "check_run"}
        try:
            with self._request("get_checks", "GET", "L", http_args):
                if fine_grained:
                    latest: dict[str, _RunRec] = {}
                    for run in sorted(s.workflow_runs, key=lambda r: (r.created_at, r.id)):
                        if run.head_sha == sha:
                            latest[run.name] = run
                    runs = [
                        CheckRun(r.name, r.status, r.conclusion, "workflow_run", s.url("actions", "runs", r.id))
                        for r in sorted(latest.values(), key=lambda r: r.name)
                    ]
                else:
                    runs = [
                        CheckRun(c.name, c.status, c.conclusion, "check_run", s.url("runs", c.id))
                        for c in sorted(s.checks.get(sha, []), key=lambda c: c.name)
                    ]
                self._log("get_checks", "GET", "L", {"sha": sha, "source": "status"})
                return Checks(sha, _overall(runs), tuple(runs))
        except _HttpError as e:
            raise self._classify("get_checks", e) from e

    def get_workflow_run(self, run_id: int) -> WorkflowRun:
        s = self._s
        try:
            with self._request("get_workflow_run", "GET", "L", {"run_id": run_id}):
                for run in s.workflow_runs:
                    if run.id == run_id:
                        return s.run_of(run)
                raise _HttpError(404, "Not Found")
        except _HttpError as e:
            raise self._classify("get_workflow_run", e) from e

    def successful_runs(
        self, workflow_file: str, branch: str, *, event: str, limit: int = 100
    ) -> list[WorkflowRun]:
        """Runs exitosos de ``workflow_file`` con ``branch`` y ``event`` exactos, del más reciente al más viejo.

        ``GET actions/workflows/{f}/runs?branch=…&event=…&status=success&per_page=…``
        (REQ-FORGE-69): solo ``completed`` con ``success``. Un run de ``pull_request_target``
        lleva la punta de la base en ``head_sha`` y no cuenta para ``event='push'``. ``limit``
        va de 1 a 1000 y se pide en páginas de 100 (REQ-FORGE-8).
        """
        s = self._s
        wanted = workflow_file.rsplit("/", 1)[-1]
        args = {"workflow_file": workflow_file, "branch": branch, "event": event, "limit": limit}
        try:
            with self._request("successful_runs", "GET", "L", args):
                if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= PAGE_SIZE * MAX_PAGES:
                    raise _HttpError(422, "Validation Failed")
                runs = [
                    r
                    for r in s.workflow_runs
                    if r.workflow_file == wanted
                    and r.head_branch == branch
                    and r.event == event
                    and r.status == "completed"
                    and r.conclusion == "success"
                ]
                runs.sort(key=lambda r: (r.created_at, r.id), reverse=True)
                found = runs[:limit]
                self._pages("successful_runs", len(found), args)
                return [s.run_of(r) for r in found]
        except _HttpError as e:
            raise self._classify("successful_runs", e) from e

    def pulls_for_commit(self, sha: str) -> list[PullRequest]:
        """PR asociados al commit (``GET commits/{sha}/pulls``), resumidos como en GitHub.

        Traen ``merged`` pero no ``merged_by`` (``None``): ese dato solo sale de
        ``get_pull_request`` (08 §4.2, CT-SEAL-01), que es donde lo pide ``ci seal``.
        """
        s = self._s
        try:
            with self._request("pulls_for_commit", "GET", "L", {"sha": sha}):
                recs = [
                    r
                    for r in s.pulls.values()
                    if sha in (r.head_sha, r.merge_commit_sha) or sha in s.pull_commits(r)
                ]
                return [s.pull_of(r) for r in sorted(recs, key=lambda r: r.number)]
        except _HttpError as e:
            raise self._classify("pulls_for_commit", e) from e

    def last_successful_run(self, workflow_file: str, branch: str) -> WorkflowRun | None:
        s = self._s
        wanted = workflow_file.rsplit("/", 1)[-1]
        try:
            with self._request("last_successful_run", "GET", "L", {"workflow_file": workflow_file, "branch": branch}):
                runs = [
                    r
                    for r in s.workflow_runs
                    if r.workflow_file == wanted
                    and r.head_branch == branch
                    and r.status == "completed"
                    and r.conclusion == "success"
                ]
                if not runs:
                    return None
                return s.run_of(max(runs, key=lambda r: (r.created_at, r.id)))
        except _HttpError as e:
            raise self._classify("last_successful_run", e) from e

    def upsert_check_run(self, sha: str, name: str, *, conclusion: str, summary: str) -> None:
        """Un solo check run ``name`` en ``sha``: lo actualiza o lo crea (REQ-FORGE-70).

        ``GET commits/{sha}/check-runs?check_name=…`` y luego ``PATCH`` o ``POST``. Como en
        GitHub, un token fine-grained no puede usar la API de Checks (08 §4.6): 403.
        """
        s = self._s
        try:
            with self._request("upsert_check_run", "GET", "L", {"sha": sha, "name": name}):
                user = s.user(self.viewer)
                if user is not None and user.token_kind == "fine_grained_pat":
                    raise _HttpError(403, "Resource not accessible by personal access token")
                self._require_push()
                existing = [c for c in s.checks.get(sha, []) if c.name == name]
                self._log(
                    "upsert_check_run", "PATCH" if existing else "POST", "E", {"sha": sha, "name": name}
                )
                ident = existing[-1].id if existing else s.new_id()
                runs = [c for c in s.checks.get(sha, []) if c.name != name]
                runs.append(_CheckRec(ident, name, "completed", conclusion, summary))
                s.checks[sha] = runs
        except _HttpError as e:
            raise self._classify("upsert_check_run", e) from e

    def check_summary(self, sha: str, name: str) -> str | None:
        """Resumen del check run ``name`` de ``sha`` (solo pruebas)."""
        with self._s.lock:
            found = [c for c in self._s.checks.get(sha, []) if c.name == name]
        return found[-1].summary if found else None

    # --- lecturas de la configuración (00-marco D-65; REQ-FORGE-73) ----------------------

    def _administers(self) -> bool:
        """¿El token lee lo reservado a la administración? Solo si la cuenta administra el repo y el
        token no es fine-grained: el de uso diario no lleva Administration (08 REQ-FORGE-41), y
        ``gh auth login`` o un token clásico heredan el permiso de la cuenta."""
        user = self._s.user(self.viewer)
        return user is not None and user.permission == "admin" and user.token_kind in ("classic_pat", "oauth")

    def repo_settings(self) -> dict[str, object]:
        """Como ``GET /repos``: los ajustes de integración solo con escritura (Contents W)."""
        try:
            with self._request("repo_settings", "GET", "L", {}):
                writer = self._s.has_push(self.viewer)
                return {k: v for k, v in self._s.admin.settings.items()
                        if k in REPO_SETTINGS and (writer or k in _PUBLIC_SETTINGS)}
        except _HttpError as e:
            raise self._classify("repo_settings", e) from e

    def rulesets(self) -> list[RulesetInfo]:
        """Los rulesets del repositorio; ``bypass_actors``, solo a quien puede editarlos
        (``_administers``). En un repo privado del perfil ``advisory`` (Free) no hay rulesets:
        ``ForgeUnsupported``."""
        s = self._s
        try:
            with self._request("rulesets", "GET", "L", {}):
                if s.profile == "advisory" and s.visibility == "private":
                    raise _HttpError(403, _NO_RULESETS)
                admin = self._administers()
                out: list[RulesetInfo] = []
                for ident, rs in sorted(s.admin.rulesets.items()):
                    self._log("rulesets", "GET", "L", {"id": ident})
                    out.append(_ruleset_info(ident, rs, bypass=admin))
                return out
        except _HttpError as e:
            if e.status == 404 or "upgrade" in e.message.lower():
                raise ForgeUnsupported(f"rulesets: HTTP {e.status}: {e.message}", http_status=e.status) from e
            raise self._classify("rulesets", e) from e

    def list_labels(self) -> list[Label]:
        try:
            with self._request("list_labels", "GET", "L", {}):
                labels = sorted(self._s.labels.values(), key=lambda lab: lab.name.casefold())
                self._pages("list_labels", len(labels), {})
                return labels
        except _HttpError as e:
            raise self._classify("list_labels", e) from e

    def collaborator_permission(self, login: str) -> str | None:
        try:
            with self._request("collaborator_permission", "GET", "L", {"login": login}):
                user = self._s.user(login)
                return user.permission if user is not None and user.member else None
        except _HttpError as e:
            raise self._classify("collaborator_permission", e) from e

    def outside_collaborators(self) -> list[str]:
        """Como ``GET collaborators?affiliation=outside``: hace falta escritura en el repo."""
        try:
            with self._request("outside_collaborators", "GET", "L", {}):
                self._require_push()
                return sorted(u.login for key, u in self._s.users.items() if key in self._s.admin.outside and u.member)
        except _HttpError as e:
            raise self._classify("outside_collaborators", e) from e

    def actions_policy(self, level: AdminLevel = "repo") -> ActionsPolicy | None:
        try:
            with self._request("actions_policy", "GET", "L", {"level": level}):
                return _actions_policy(self, level, admin=self._administers())
        except _HttpError as e:
            raise self._classify("actions_policy", e) from e

    def workflow_permissions(self, level: AdminLevel = "repo") -> WorkflowPermissions | None:
        try:
            with self._request("workflow_permissions", "GET", "L", {"level": level}):
                return _workflow_permissions(self._s, level, admin=self._administers())
        except _HttpError as e:
            raise self._classify("workflow_permissions", e) from e

    def org_settings(self) -> dict[str, object] | None:
        """Como ``GET /orgs/{org}``: los ajustes de 06 §4.10 solo los ve un owner (aquí, ``_administers``)."""
        try:
            with self._request("org_settings", "GET", "L", {}):
                org = self._s.admin.org
                if org is None:
                    return None
                return {k: v for k, v in org.items() if k in ORG_SETTINGS} if self._administers() else {}
        except _HttpError as e:
            raise self._classify("org_settings", e) from e

    def admin(self, token: str) -> FakeAdmin:
        """``ForgeAdmin`` sobre el mismo estado (08 §2.2; 00-marco D-62).

        El token se busca entre los de ``seed_admin_token``; uno que no está ahí es de la
        cuenta de esta vista. Uno sembrado con ``None`` es inválido: cada llamada da 401.
        """
        if not isinstance(token, str) or not token.strip():
            raise NoTokenError("administration token is empty")
        with self._s.lock:
            tokens = self._s.admin.tokens
            login = tokens[token.strip()] if token.strip() in tokens else self.viewer
        return FakeAdmin(self, login)

    def seed_admin_token(self, token: str, login: str | None) -> None:
        """El token de administración ``token`` es de ``login`` (``None``: inválido)."""
        with self._s.lock:
            self._s.admin.tokens[token] = login

    def seed_admin(self, **fields: object) -> None:
        """Cambia la configuración que ve ``ForgeAdmin``.

        Claves: ``settings`` (se mezcla), ``actions_repo``, ``actions_org``, ``workflow_repo``,
        ``workflow_org`` (``None``: no se puede leer), ``retention`` (``None``: sin endpoint),
        ``org`` (``None``: cuenta personal), ``outside`` (logins de colaboradores externos),
        ``fresh`` (un repo recién creado: sin rulesets, la base no está protegida) y
        ``plan_visible`` (``False``: el visor no ve el plan de la org, 08 §3.2).
        """
        _check_fields(fields, {"settings", "actions_repo", "actions_org", "workflow_repo", "workflow_org",
                               "retention", "org", "outside", "fresh", "plan_visible"})
        with self._s.lock:
            admin = self._s.admin
            if "settings" in fields:
                admin.settings.update(dict(fields["settings"]))  # type: ignore[call-overload]
            for key in ("actions_repo", "actions_org"):
                if key in fields:
                    admin.actions[key.split("_")[1]] = copy.deepcopy(fields[key])  # type: ignore[assignment]
            for key in ("workflow_repo", "workflow_org"):
                if key in fields:
                    admin.workflow[key.split("_")[1]] = copy.deepcopy(fields[key])  # type: ignore[assignment]
            if "retention" in fields:
                admin.retention = fields["retention"]  # type: ignore[assignment]
            if "org" in fields:
                admin.org = copy.deepcopy(fields["org"])  # type: ignore[assignment]
            if "outside" in fields:
                admin.outside = {str(x).casefold() for x in _seq(fields["outside"])}
            if "fresh" in fields:  # repo recién creado: sin rulesets, la base no está protegida
                admin.protected_by_default = not bool(fields["fresh"])
            if "plan_visible" in fields:
                admin.plan_visible = bool(fields["plan_visible"])
            self._caps = None

    def admin_state(self) -> dict[str, Any]:
        """Copia de la configuración (solo pruebas)."""
        with self._s.lock:
            return _admin_to_json(self._s.admin)

    # --- siembra (no pasa por calls ni por faults) ------------------------------

    def seed_user(
        self,
        login: str,
        *,
        permission: Literal["admin", "maintain", "write", "triage", "read"] = "write",
        token_kind: str | None = None,
        id: int | None = None,  # noqa: A002 (nombre del campo de la forja)
        scopes: Sequence[str] | None = None,
        token_scope: Literal["repo_restricted", "owner_wide", "unknown"] | None = None,
    ) -> None:
        """Registra o ajusta una cuenta colaboradora: permiso sobre el repo, tipo de token, id y alcances.

        ``scopes`` es la cabecera ``X-OAuth-Scopes`` de un token ``classic_pat`` u
        ``oauth`` (08 §5.2); sin ella valen ``repo`` (classic) o los de ``gh auth
        login`` (oauth). Cambiar ``token_kind`` sin ``scopes`` vuelve a esos valores.
        ``token_scope`` es lo que mide ``GET /user/repos`` con un token fine-grained.
        """
        if permission not in _PERM_LEVEL:
            raise ValueError(f"unknown permission: {permission!r}")
        if scopes is not None and (isinstance(scopes, str) or not all(isinstance(x, str) for x in scopes)):
            raise TypeError("scopes must be a sequence of str")
        with self._s.lock:
            user = self._s.ensure_user(login)
            user.permission = permission
            user.member = True
            if token_kind is not None:
                user.token_kind = token_kind
                user.scopes = None
            if scopes is not None:
                user.scopes = tuple(scopes)
            if token_scope is not None:
                user.token_scope = token_scope
            if id is not None:
                user.id = id
            if self._s.user(self.viewer) is user:
                self._identity = None
                self._caps = None

    def seed_commit(
        self,
        files: Mapping[str, bytes],
        parents: Sequence[str] = (),
        message: str = "seed",
        author: str | None = None,
        *,
        sha: str | None = None,
        date: str | datetime | None = None,
    ) -> str:
        """Crea un commit con ese árbol completo; ``sha`` permite reproducir el de un repo real."""
        tree: dict[str, bytes] = {}
        for path, content in files.items():
            if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
                raise ValueError(f"invalid path: {path!r}")
            if isinstance(content, str):
                content = content.encode("utf-8")
            if not isinstance(content, (bytes, bytearray)):
                raise TypeError(f"content of {path!r} must be bytes")
            tree[path] = bytes(content)
        s = self._s
        with s.lock:
            parent_list = [_check_sha(p) for p in parents]
            for parent in parent_list:
                s.register_sha(parent)
            when = _parse_dt(date) if date is not None else s.now()
            new = _check_sha(sha) if sha is not None else _commit_sha(tree, parent_list, message, author, when)
            s.commits[new] = _Commit(new, tree, parent_list, message, author, when, when)
            return new

    def tree_of(self, ref: str) -> dict[str, bytes] | None:
        """Copia del árbol completo de un commit, rama o etiqueta; ``None`` si el falso no lo conoce.

        Utilidad de prueba (no pasa por ``calls`` ni por ``faults``): con ``seed_commit``
        permite sembrar un commit que cambia unas rutas sobre otro.
        """
        s = self._s
        with s.lock:
            resolved = s.resolve(ref)
            commit = s.commits.get(resolved) if resolved else None
            return None if commit is None or commit.files is None else dict(commit.files)

    def seed_branch(self, branch: str, sha: str) -> None:
        """Apunta ``refs/heads/<branch>`` a ``sha`` sin registrar actividad (usa ``seed_ref_activity``)."""
        if not _valid_ref_name(branch):
            raise ValueError(f"invalid branch name: {branch!r}")
        _check_sha(sha)
        with self._s.lock:
            self._s.register_sha(sha)
            self._s.refs[f"heads/{branch}"] = sha

    def seed_tag(self, tag: str, sha: str) -> None:
        if not _valid_ref_name(tag):
            raise ValueError(f"invalid tag name: {tag!r}")
        _check_sha(sha)
        with self._s.lock:
            self._s.register_sha(sha)
            self._s.refs[f"tags/{tag}"] = sha

    def seed_ref_activity(self, branch: str, events: Sequence[RefActivity]) -> None:
        """Agrega eventos de actividad a ``refs/heads/<branch>`` (se listan por fecha)."""
        items = list(events)
        for ev in items:
            if not isinstance(ev, RefActivity):
                raise TypeError("events must be RefActivity objects")
            _aware(ev.timestamp)
        with self._s.lock:
            self._s.ref_activity.setdefault(f"heads/{branch}", []).extend(items)

    def seed_issue(self, **fields: object) -> Issue:
        """Crea un Issue directamente. Campos: los de ``Issue`` salvo ``url``, más ``events``."""
        allowed = {
            "number", "title", "body", "state", "state_reason", "labels", "assignees", "author",
            "created_at", "updated_at", "closed_at", "events",
        }
        _check_fields(fields, allowed)
        s = self._s
        with s.lock:
            number = self._seed_number(fields.get("number"))
            now = s.now()
            created = _parse_dt(fields["created_at"]) if fields.get("created_at") else now  # type: ignore[arg-type]
            author = s.ensure_user(str(fields.get("author") or self.viewer)).login
            labels = [s.canonical_label(str(n)) for n in _seq(fields.get("labels"))]
            state = str(fields.get("state") or "open")
            if state not in ("open", "closed"):
                raise ValueError(f"invalid issue state: {state!r}")
            reason = fields.get("state_reason")
            if reason is not None and reason not in _ISSUE_STATE_REASONS:
                raise ValueError(f"invalid state_reason: {reason!r}")
            closed_at = _parse_opt_dt(fields.get("closed_at"))
            if state == "closed" and closed_at is None:
                closed_at = created
            rec = _IssueRec(
                number,
                str(fields.get("title") or f"Issue {number}"),
                str(fields.get("body") or ""),
                state,
                str(reason) if reason is not None else ("completed" if state == "closed" else None),
                labels,
                [s.display(str(a)) for a in _seq(fields.get("assignees"))],
                author,
                created,
                _parse_dt(fields["updated_at"]) if fields.get("updated_at") else created,  # type: ignore[arg-type]
                closed_at,
            )
            if "events" in fields:
                rec.events = [_label_event(ev) for ev in _seq(fields["events"])]
            else:
                rec.events = [LabelEvent(s.new_id(), "labeled", n, author, created) for n in labels]
            s.issues[number] = rec
            return s.issue_of(rec)

    def seed_comment(
        self, number: int, *, author: str, body: str, created_at: str | datetime | None = None
    ) -> Comment:
        """Agrega un comentario a un Issue o PR, sin marcador ``op``."""
        s = self._s
        with s.lock:
            rec = s.item(number)
            if rec is None:
                raise ValueError(f"no issue or pull request #{number}")
            when = _parse_dt(created_at) if created_at is not None else s.now()
            comment = _CommentRec(s.new_id(), s.ensure_user(author).login, body, when, when)
            rec.comments.append(comment)
            return s.comment_of(number, comment)

    def seed_pull_request(self, *, files: Sequence[ChangedFile] | None = None, **fields: object) -> PullRequest:
        """Crea un PR directamente. ``head_branch`` es obligatorio; ``head_sha`` sale de la rama si falta.

        ``files`` es la respuesta sembrada de ``GET pulls/{n}/files`` para esa cabeza;
        sin ella se calcula del grafo (diff de tres puntos) si el falso conoce los
        árboles, y si no queda vacía. Un push posterior a la rama la recalcula.

        Campos: los de ``PullRequest`` salvo ``url``, más ``assignees``, ``created_at``,
        ``closed_at``, ``merge_commit_sha``, ``commits`` (shas o ``CommitInfo``) y
        ``reviews`` (``Review``). ``same_repo=False`` (por omisión ``True``) siembra un
        PR desde un fork: su ``head_branch`` es una rama del fork, así que ``head_sha``
        es obligatorio y no se toma de la rama homónima de este repositorio.
        ``merged_by`` solo vale con ``merged=True``.
        """
        allowed = {
            "number", "node_id", "title", "body", "state", "draft", "merged", "merged_at", "head_branch",
            "head_sha", "base_branch", "author", "labels", "requested_reviewers", "updated_at", "assignees",
            "created_at", "closed_at", "merge_commit_sha", "commits", "reviews", "same_repo", "merged_by",
        }
        _check_fields(fields, allowed)
        same_repo = fields.get("same_repo", True)
        if not isinstance(same_repo, bool):
            raise TypeError("same_repo must be a bool")
        changed = None if files is None else list(files)
        if changed is not None and not all(isinstance(f, ChangedFile) for f in changed):
            raise TypeError("files must be ChangedFile objects")
        head_branch = fields.get("head_branch")
        if not isinstance(head_branch, str) or not head_branch:
            raise ValueError("seed_pull_request needs head_branch")
        s = self._s
        with s.lock:
            head_sha = fields.get("head_sha")
            if not head_sha and not same_repo:
                raise ValueError("a pull request from a fork needs head_sha")
            head_sha = head_sha or s.refs.get(f"heads/{head_branch}")
            if head_sha is None:
                raise ValueError(f"no head_sha given and branch {head_branch!r} does not exist")
            _check_sha(head_sha)
            s.register_sha(str(head_sha))
            number = self._seed_number(fields.get("number"))
            now = s.now()
            created = _parse_dt(fields["created_at"]) if fields.get("created_at") else now  # type: ignore[arg-type]
            merged = bool(fields.get("merged", False))
            state = str(fields.get("state") or ("closed" if merged else "open"))
            if state not in ("open", "closed") or (merged and state != "closed"):
                raise ValueError(f"invalid pull request state: {state!r}")
            merged_at = _parse_opt_dt(fields.get("merged_at"))
            if merged and merged_at is None:
                merged_at = created
            merged_by = fields.get("merged_by")
            if merged_by is not None and (not merged or not isinstance(merged_by, str) or not merged_by.strip()):
                raise ValueError("merged_by needs merged=True and a login")
            closed_at = _parse_opt_dt(fields.get("closed_at"))
            if state == "closed" and closed_at is None:
                closed_at = merged_at or created
            commits: list[str] | None = None
            if fields.get("commits") is not None:
                commits = []
                for item in _seq(fields["commits"]):
                    if isinstance(item, CommitInfo):
                        _check_sha(item.sha)
                        if item.sha not in s.commits or s.commits[item.sha].files is None:
                            s.commits[item.sha] = _Commit(
                                item.sha, None, [], item.message, item.author_login,
                                _aware(item.author_date), _aware(item.committer_date),
                            )
                        commits.append(item.sha)
                    else:
                        s.register_sha(_check_sha(item))
                        commits.append(str(item))
            author = s.ensure_user(str(fields.get("author") or self.viewer)).login
            labels = [s.canonical_label(str(n)) for n in _seq(fields.get("labels"))]
            base_branch = str(fields.get("base_branch") or s.default_branch)
            if changed is None:
                changed = s.pull_files(base_branch, str(head_sha)) or []
            rec = _PullRec(
                number=number,
                node_id=str(fields.get("node_id") or f"PR_fake{number}"),
                title=str(fields.get("title") or f"Pull request {number}"),
                body=str(fields.get("body") or ""),
                state=state,
                draft=bool(fields.get("draft", False)),
                merged=merged,
                merged_at=merged_at,
                head_branch=head_branch,
                head_sha=str(head_sha),
                base_branch=base_branch,
                author=author,
                labels=labels,
                assignees=[s.display(str(a)) for a in _seq(fields.get("assignees"))],
                requested_reviewers=[s.display(str(a)) for a in _seq(fields.get("requested_reviewers"))],
                created_at=created,
                updated_at=_parse_dt(fields["updated_at"]) if fields.get("updated_at") else created,  # type: ignore[arg-type]
                closed_at=closed_at,
                merge_commit_sha=_check_sha(fields["merge_commit_sha"]) if fields.get("merge_commit_sha") else None,
                files=changed,
                commits=commits,
                same_repo=same_repo,
                merged_by=s.ensure_user(merged_by).login if isinstance(merged_by, str) else None,
            )
            rec.events = [LabelEvent(s.new_id(), "labeled", n, author, created) for n in labels]
            for review in _seq(fields.get("reviews")):
                if not isinstance(review, Review):
                    raise TypeError("reviews must be Review objects")
                rec.reviews.append(review)
            s.pulls[number] = rec
            return s.pull_of(rec)

    def seed_review(
        self,
        pr_number: int,
        *,
        author: str,
        state: Literal["APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING"],
        commit_sha: str | None = None,
        submitted_at: str | datetime | None = None,
    ) -> Review:
        """Agrega una revisión a un PR (la escritura de revisiones por API llega en H3)."""
        if state not in _REVIEW_STATES:
            raise ValueError(f"invalid review state: {state!r}")
        s = self._s
        with s.lock:
            rec = s.pulls.get(pr_number)
            if rec is None:
                raise ValueError(f"no pull request #{pr_number}")
            user = s.ensure_user(author)
            when = None if state == "PENDING" else (_parse_dt(submitted_at) if submitted_at else s.now())
            review = Review(s.new_id(), user.login, user.id, state, commit_sha or rec.head_sha, when)
            rec.reviews.append(review)
            return review

    def seed_workflow_run(
        self,
        workflow_file: str,
        *,
        branch: str,
        sha: str,
        conclusion: str | None,
        name: str | None = None,
        event: str | None = None,
        actor: str | None = None,
    ) -> WorkflowRun:
        """Registra un run de Actions; ``conclusion=None`` lo deja ``in_progress``."""
        _check_sha(sha)
        file_name = workflow_file.rsplit("/", 1)[-1]
        s = self._s
        with s.lock:
            run = _RunRec(
                id=s.new_id(),
                workflow_file=file_name,
                name=name or file_name.rsplit(".", 1)[0],
                event=event or ("push" if branch == s.default_branch else "pull_request"),
                status="completed" if conclusion is not None else "in_progress",
                conclusion=conclusion,
                head_sha=sha,
                head_branch=branch,
                actor=s.ensure_user(actor or self.viewer).login,
                created_at=s.now(),
            )
            s.workflow_runs.append(run)
            return s.run_of(run)

    def seed_run(
        self,
        workflow_file: str,
        sha: str,
        event: str,
        conclusion: str | None,
        *,
        branch: str | None = None,
        name: str | None = None,
        actor: str | None = None,
    ) -> WorkflowRun:
        """Run de ``workflow_file`` sobre ``sha`` con su evento (08 §10.1): alimenta ``successful_runs``.

        La rama es por omisión la rama por defecto, también en ``pull_request_target``: ese
        run corre en la base y lleva su punta en ``head_sha`` (REQ-FORGE-69).
        ``conclusion=None`` lo deja ``in_progress``.
        """
        if not isinstance(event, str) or not event:
            raise ValueError("seed_run needs an event")
        return self.seed_workflow_run(
            workflow_file,
            branch=branch or self._s.default_branch,
            sha=sha,
            conclusion=conclusion,
            name=name,
            event=event,
            actor=actor,
        )

    def set_check(self, sha: str, *, name: str, status: str, conclusion: str | None) -> None:
        """Fija el check run más reciente ``name`` del commit ``sha`` (filter=latest)."""
        _check_sha(sha)
        s = self._s
        with s.lock:
            runs = [c for c in s.checks.get(sha, []) if c.name != name]
            runs.append(_CheckRec(s.new_id(), name, status, conclusion))
            s.checks[sha] = runs

    def set_codeowners_errors(self, ref: str, errors: Sequence[str]) -> None:
        with self._s.lock:
            self._s.codeowners_errors[ref] = [str(e) for e in errors]

    def admin_merge(self, pr_number: int) -> None:
        """Integra un PR como lo haría una persona en la web (solo en pruebas; el núcleo no fusiona).

        Crea un commit de fusión en la rama base, registra ``pr_merge`` y, si la
        base es la rama por defecto, cierra los Issues de ``Closes #n``.
        """
        s = self._s
        with s.lock:
            rec = s.pulls.get(pr_number)
            if rec is None or rec.state != "open":
                raise ValueError(f"pull request #{pr_number} is not open")
            key = f"heads/{rec.base_branch}"
            base_sha = s.refs.get(key)
            head = s.commits.get(rec.head_sha)
            if base_sha is None or head is None or head.files is None:
                raise ValueError("admin_merge needs a known base branch and head commit")
            merge_base = s.merge_base(base_sha, rec.head_sha)
            base_files = s.commits[base_sha].files
            mb_files = s.commits[merge_base].files if merge_base else None
            if base_files is None or mb_files is None:
                raise ValueError("admin_merge needs the contents of the base and the merge base")
            if rec.commits is None:
                rec.commits = s.pull_commits(rec)
            merged = dict(base_files)
            for change in _diff_files(mb_files, head.files):
                if change.previous_path:
                    merged.pop(change.previous_path, None)
                if change.status == "removed":
                    merged.pop(change.path, None)
                else:
                    merged[change.path] = head.files[change.path]
            now = s.now()
            message = f"Merge pull request #{pr_number} from {s.repo.owner}/{rec.head_branch}"
            parents = [base_sha, rec.head_sha]
            sha = _commit_sha(merged, parents, message, self.viewer, now)
            s.commits[sha] = _Commit(sha, merged, parents, message, self.viewer, now, now)
            s.refs[key] = sha
            s.add_activity(key, "pr_merge", self.viewer, base_sha, sha)
            rec.merged = True
            rec.merged_at = now
            rec.merged_by = self.viewer
            rec.state = "closed"
            rec.closed_at = now
            rec.updated_at = now
            rec.merge_commit_sha = sha
            rec.other_events += 2  # merged y closed
            if rec.base_branch == s.default_branch:
                for match in _CLOSING_RE.finditer(rec.body):
                    issue = s.issues.get(int(match.group(1)))
                    if issue is not None and issue.state == "open":
                        issue.state = "closed"
                        issue.state_reason = "completed"
                        issue.closed_at = now
                        issue.updated_at = now
                        issue.other_events += 1  # closed

    def _seed_number(self, value: object) -> int:
        s = self._s
        if value is None:
            number = s.new_id("number")
            while s.item(number) is not None:
                number = s.new_id("number")
            return number
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"invalid number: {value!r}")
        if s.item(value) is not None:
            raise ValueError(f"#{value} already exists")
        if value >= s.next_id["number"]:
            s.next_id["number"] = value + 1
        return value

    # --- estado JSON -------------------------------------------------------------

    def to_state(self) -> dict[str, Any]:
        """Estado completo en forma JSON (``fake_state: 1``). Las fallas ``race`` no se guardan."""
        s = self._s
        with s.lock:
            return {
                "fake_state": STATE_VERSION,
                "repo": {"owner": s.repo.owner, "name": s.repo.name},
                "viewer": self.viewer,
                "profile": s.profile,
                "visibility": s.visibility,
                "write_mode": s.write_mode,
                "graphql": s.graphql,
                "transport": s.transport,
                "default_branch": s.default_branch,
                "clock": {"now": _iso(s.now())},
                "users": {
                    u.login: {
                        "id": u.id,
                        "permission": u.permission,
                        "token_kind": u.token_kind,
                        "scopes": None if u.scopes is None else list(u.scopes),
                        "member": u.member,
                        "token_scope": u.token_scope,
                    }
                    for u in sorted(s.users.values(), key=lambda u: u.login.casefold())
                },
                "commits": {sha: _commit_to_json(c) for sha, c in sorted(s.commits.items())},
                "refs": dict(sorted(s.refs.items())),
                "ref_activity": {
                    key: [_activity_to_json(a) for a in events] for key, events in sorted(s.ref_activity.items())
                },
                "issues": [_issue_to_json(r) for r in sorted(s.issues.values(), key=lambda r: r.number)],
                "pulls": [_pull_to_json(r) for r in sorted(s.pulls.values(), key=lambda r: r.number)],
                "checks": {
                    sha: [
                        {"id": c.id, "name": c.name, "status": c.status, "conclusion": c.conclusion, "summary": c.summary}
                        for c in runs
                    ]
                    for sha, runs in sorted(s.checks.items())
                },
                "codeowners_errors": {k: list(v) for k, v in sorted(s.codeowners_errors.items())},
                "workflow_runs": [_run_to_json(r) for r in s.workflow_runs],
                "labels": {
                    name: {"color": lab.color, "description": lab.description}
                    for name, lab in sorted(s.labels.items())
                },
                "admin": _admin_to_json(s.admin),
                "next_id": dict(sorted(s.next_id.items())),
                "blocked_until": {login: _iso(until) for login, until in sorted(s.blocked_until.items())},
                "faults": [
                    {"method": f.method, "kind": f.kind, "nth": f.nth, "delay_s": f.delay_s, "seen": f._seen}
                    for f in s.faults
                    if f.race is None
                ],
                "calls": [
                    {
                        "method": c.method,
                        "http": c.http,
                        "kind": c.kind,
                        "actor": c.actor,
                        "args": c.args,
                        "outcome": c.outcome,
                    }
                    for c in s.calls.entries()
                ],
            }

    @classmethod
    def from_state(cls, state: Mapping[str, object]) -> FakeForge:
        """Reconstruye un FakeForge de ``to_state()``; solo exige ``fake_state``, ``repo`` y ``viewer``."""
        if not isinstance(state, Mapping) or state.get("fake_state") != STATE_VERSION:
            raise ValueError(f"unsupported fake state (expected fake_state: {STATE_VERSION})")
        st: Mapping[str, Any] = state
        repo = st.get("repo")
        if not isinstance(repo, Mapping):
            raise ValueError("fake state without repo")
        clock = FakeClock((st.get("clock") or {}).get("now", DEFAULT_START))
        fake = cls(
            RepoRef(str(repo["owner"]), str(repo["name"])),
            str(st["viewer"]),
            profile=st.get("profile", "enforced"),
            clock=clock,
            default_branch=str(st.get("default_branch", "main")),
            write_mode=st.get("write_mode", "git"),
            graphql=bool(st.get("graphql", True)),
            transport=st.get("transport", "http"),
            visibility=st.get("visibility"),
        )
        s = fake._s
        with s.lock:
            s.users.clear()
            s.next_id = {"number": 1, "id": 1, "user": 1001}
            s.next_id.update({str(k): int(v) for k, v in (st.get("next_id") or {}).items()})
            for login, data in (st.get("users") or {}).items():
                scopes = data.get("scopes")
                s.users[login.casefold()] = _User(
                    login,
                    int(data["id"]),
                    data["permission"],
                    data["token_kind"],
                    None if scopes is None else tuple(str(x) for x in scopes),
                    bool(data.get("member", True)),
                    data.get("token_scope"),
                )
            for sha, data in (st.get("commits") or {}).items():
                s.commits[sha] = _commit_from_json(sha, data)
            s.refs = {str(k): str(v) for k, v in (st.get("refs") or {}).items()}
            s.ref_activity = {
                str(k): [_activity_from_json(a) for a in v] for k, v in (st.get("ref_activity") or {}).items()
            }
            for data in st.get("issues") or []:
                rec = _issue_from_json(data)
                s.issues[rec.number] = rec
            for data in st.get("pulls") or []:
                prec = _pull_from_json(data)
                s.pulls[prec.number] = prec
            s.checks = {
                str(sha): [
                    _CheckRec(int(c["id"]), c["name"], c["status"], c.get("conclusion"), str(c.get("summary", "")))
                    for c in runs
                ]
                for sha, runs in (st.get("checks") or {}).items()
            }
            s.codeowners_errors = {
                str(k): [str(e) for e in v] for k, v in (st.get("codeowners_errors") or {}).items()
            }
            s.workflow_runs = [_run_from_json(r) for r in st.get("workflow_runs") or []]
            s.admin = _admin_from_json(st.get("admin"), s.visibility)
            s.labels = {
                str(name): Label(str(name), data.get("color", "ededed"), data.get("description", ""))
                for name, data in (st.get("labels") or {}).items()
            }
            blocked = st.get("blocked_until")
            if isinstance(blocked, str):  # forma antigua: un solo bloqueo, el de la cuenta del estado
                blocked = {str(st["viewer"]): blocked}
            s.blocked_until = {str(k).casefold(): _parse_dt(v) for k, v in (blocked or {}).items()}
            for data in st.get("faults") or []:
                fault = Fault(data["method"], data["kind"], data.get("nth"), None, float(data.get("delay_s", 0.01)))
                fault._seen = int(data.get("seen", 0))
                s.faults.add(fault)
            s.calls._load(st.get("calls") or [])
            for sha in s.refs.values():
                s.register_sha(sha)
            _bump_counters(s)
            fake.viewer = s.ensure_user(str(st["viewer"]), operator=True).login
        return fake

    @classmethod
    def load(cls, path: Path) -> FakeForge:
        return cls.from_state(json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        """Escribe el estado de forma atómica (temporal en la misma carpeta y ``os.replace``)."""
        target = Path(path)
        data = (json.dumps(self.to_state(), ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            for attempt in range(20):
                try:
                    os.replace(tmp, target)
                    break
                except PermissionError:
                    # En Windows, otro proceso puede tener el destino abierto un instante.
                    if attempt == 19:
                        raise
                    time.sleep(0.05)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# ---------------------------------------------------------------------------
# FakeAdmin (08 §2.2; 00-marco D-62)
# ---------------------------------------------------------------------------


class FakeAdmin:
    """``ForgeAdmin`` del falso (08 §2.2; 00-marco D-62): la configuración de ``_AdminState``.

    Cada método pasa por el mismo registro de llamadas y las mismas fallas inyectables que
    ``FakeForge`` (con su nombre de método), y exige lo que GitHub: administración del repo
    para las escrituras y para leer la configuración del repo y de la org. En un repo
    privado del perfil ``advisory`` (Free) no hay rulesets (403 «Upgrade»).
    """

    def __init__(self, forge: FakeForge, login: str | None) -> None:
        self.repo = forge.repo
        self._view = forge.as_user(login) if login is not None else forge
        self._valid = login is not None
        self._s = forge._s

    def __repr__(self) -> str:
        return f"FakeAdmin({self.repo.owner}/{self.repo.name}, viewer={self._view.viewer!r})"

    def close(self) -> None:
        return

    # -- utilidades

    def _request(self, method: str, http: str, kind: CallKind, args: Mapping[str, Any]) -> _Request:
        return self._view._request(method, http, kind, args)

    def _auth(self) -> None:
        if not self._valid:
            raise _HttpError(401, "Bad credentials")

    def _is_admin(self) -> bool:
        user = self._s.user(self._view.viewer)
        return user is not None and user.permission == "admin"

    def _require_admin(self) -> None:
        self._auth()
        if not self._is_admin():
            raise _HttpError(403, "Resource not accessible by personal access token")

    def _no_rulesets(self) -> bool:
        return self._s.profile == "advisory" and self._s.visibility == "private"

    def _fail(self, method: str, e: _HttpError) -> ForgeError:
        text = e.message.lower()
        if method in ("ruleset", "upsert_ruleset") and (e.status == 404 or "upgrade" in text or "not supported" in text):
            return ForgeUnsupported(f"{method}: HTTP {e.status}: {e.message}", http_status=e.status)
        return self._view._classify(method, e)

    # -- ForgeAdmin

    def identity(self) -> Identity:
        if not self._valid:
            raise ForgeAuthError("Bad credentials", http_status=401)
        return self._view.identity(refresh=True)

    def repo_settings(self) -> dict[str, object]:
        try:
            with self._request("repo_settings", "GET", "L", {}):
                self._require_admin()
                return {k: v for k, v in self._s.admin.settings.items() if k in REPO_SETTINGS}
        except _HttpError as e:
            raise self._fail("repo_settings", e) from e

    def update_repo_settings(self, changes: Mapping[str, object]) -> dict[str, object]:
        unknown = sorted(set(changes) - set(REPO_SETTINGS))
        if unknown:  # REQ-FORGE-72: sin llamar a la forja
            raise ForgeValidationError(f"update_repo_settings: keys outside REPO_SETTINGS: {', '.join(unknown)}")
        if not changes:
            return self.repo_settings()
        try:
            with self._request("update_repo_settings", "PATCH", "E", {"changes": dict(changes)}):
                self._require_admin()
                self._s.admin.settings.update(changes)
                return {k: v for k, v in self._s.admin.settings.items() if k in REPO_SETTINGS}
        except _HttpError as e:
            raise self._fail("update_repo_settings", e) from e

    def actions_policy(self, level: AdminLevel = "repo") -> ActionsPolicy | None:
        try:
            with self._request("actions_policy", "GET", "L", {"level": level}):
                self._auth()
                return _actions_policy(self._view, level, admin=self._is_admin())
        except _HttpError as e:
            raise self._fail("actions_policy", e) from e

    def set_actions_policy(self, *, enabled: bool, allowed_actions: str, sha_pinning_required: bool) -> None:
        body = {"enabled": enabled, "allowed_actions": allowed_actions, "sha_pinning_required": sha_pinning_required}
        try:
            with self._request("set_actions_policy", "PUT", "E", body):
                self._require_admin()
                if allowed_actions not in ("all", "local_only", "selected"):
                    raise _HttpError(422, "Invalid allowed_actions")
                current = dict(self._s.admin.actions.get("repo") or {})
                current.update(body)
                self._s.admin.actions["repo"] = current
        except _HttpError as e:
            raise self._fail("set_actions_policy", e) from e

    def workflow_permissions(self, level: AdminLevel = "repo") -> WorkflowPermissions | None:
        try:
            with self._request("workflow_permissions", "GET", "L", {"level": level}):
                self._auth()
                return _workflow_permissions(self._s, level, admin=self._is_admin())
        except _HttpError as e:
            raise self._fail("workflow_permissions", e) from e

    def set_workflow_permissions(self, *, default: Literal["read", "write"], can_approve: bool) -> None:
        body = {"default_workflow_permissions": default, "can_approve_pull_request_reviews": can_approve}
        try:
            with self._request("set_workflow_permissions", "PUT", "E", body):
                self._require_admin()
                if default not in ("read", "write"):
                    raise _HttpError(422, "Invalid default_workflow_permissions")
                self._s.admin.workflow["repo"] = body
        except _HttpError as e:
            raise self._fail("set_workflow_permissions", e) from e

    def retention_days(self) -> int | None:
        try:
            with self._request("retention_days", "GET", "L", {}):
                self._require_admin()
                return self._s.admin.retention
        except _HttpError as e:
            raise self._fail("retention_days", e) from e

    def set_retention_days(self, days: int) -> None:
        try:
            with self._request("set_retention_days", "PUT", "E", {"days": days}):
                self._require_admin()
                if self._s.admin.retention is None:
                    raise ForgeUnsupported("set_retention_days: the endpoint does not exist", http_status=404)
                if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= _RETENTION_MAX[self._s.visibility]:
                    raise _HttpError(422, "days exceeds the maximum allowed")
                self._s.admin.retention = days
        except _HttpError as e:
            raise self._fail("set_retention_days", e) from e

    def collaborator_permission(self, login: str) -> str | None:
        try:
            with self._request("collaborator_permission", "GET", "L", {"login": login}):
                self._auth()
                user = self._s.user(login)
                if user is None or not user.member:
                    return None
                return user.permission
        except _HttpError as e:
            raise self._fail("collaborator_permission", e) from e

    def add_collaborator(self, login: str, permission: CollaboratorPermission) -> Literal["added", "invited"]:
        try:
            with self._request("add_collaborator", "PUT", "E", {"login": login, "permission": permission}):
                self._require_admin()
                role = _COLLABORATOR_ROLE.get(permission)
                if role is None:
                    raise _HttpError(422, "Invalid permission")
                user = self._s.user(login)
                if user is None or not user.member:  # 201: invitación pendiente
                    self._s.admin.invitations[login.casefold()] = role
                    return "invited"
                user.permission = role  # 204: miembro o colaboradora, con acceso directo
                return "added"
        except _HttpError as e:
            raise self._fail("add_collaborator", e) from e

    def outside_collaborators(self) -> list[str]:
        try:
            with self._request("outside_collaborators", "GET", "L", {}):
                self._auth()
                return sorted(u.login for key, u in self._s.users.items() if key in self._s.admin.outside and u.member)
        except _HttpError as e:
            raise self._fail("outside_collaborators", e) from e

    def user_id(self, login: str) -> int:
        try:
            with self._request("user_id", "GET", "L", {"login": login}):
                self._auth()
                user = self._s.user(login)
                if user is None:
                    raise _HttpError(404, "Not Found")
                return user.id
        except _HttpError as e:
            raise self._fail("user_id", e) from e

    def org_settings(self) -> dict[str, object] | None:
        try:
            with self._request("org_settings", "GET", "L", {}):
                self._auth()
                org = self._s.admin.org
                if org is None:
                    return None
                return {k: v for k, v in org.items() if k in ORG_SETTINGS} if self._is_admin() else {}
        except _HttpError as e:
            raise self._fail("org_settings", e) from e

    def _ruleset_id(self, name: str) -> int | None:
        return next((i for i, rs in sorted(self._s.admin.rulesets.items()) if rs.get("name") == name), None)

    def ruleset(self, name: str) -> RulesetInfo | None:
        try:
            with self._request("ruleset", "GET", "L", {"name": name}):
                self._require_admin()
                if self._no_rulesets():
                    raise _HttpError(403, _NO_RULESETS)
                found = self._ruleset_id(name)
                if found is None:
                    return None
                self._view._log("ruleset", "GET", "L", {"id": found})
                return _ruleset_info(found, self._s.admin.rulesets[found], bypass=True)
        except _HttpError as e:
            raise self._fail("ruleset", e) from e

    def upsert_ruleset(self, name: str, payload: Mapping[str, object]) -> int:
        body = {**copy.deepcopy(dict(payload)), "name": name}
        try:
            with self._request("upsert_ruleset", "GET", "L", {"name": name}):
                self._require_admin()
                if self._no_rulesets():
                    raise _HttpError(403, _NO_RULESETS)
                if body.get("target") == "push" and self._s.visibility == "public":
                    raise _HttpError(422, "Push rulesets are not supported for public repositories")
                found = self._ruleset_id(name)
                if found is None:
                    self._view._log("upsert_ruleset", "POST", "E", {"name": name})
                    found = self._s.new_id()
                else:
                    self._view._log("upsert_ruleset", "PUT", "E", {"name": name, "id": found})
                self._s.admin.rulesets[found] = body
                return found
        except _HttpError as e:
            raise self._fail("upsert_ruleset", e) from e

    def branch_rules(self, branch: str) -> list[dict[str, object]]:
        try:
            with self._request("branch_rules", "GET", "L", {"branch": branch}):
                self._auth()
                return self._s.branch_rules(branch)
        except _HttpError as e:
            raise self._fail("branch_rules", e) from e


# ---------------------------------------------------------------------------
# Serialización del estado
# ---------------------------------------------------------------------------


def _admin_to_json(a: _AdminState) -> dict[str, Any]:
    return {
        "settings": dict(sorted(a.settings.items())),
        "actions": copy.deepcopy(a.actions),
        "workflow": copy.deepcopy(a.workflow),
        "retention": a.retention,
        "org": copy.deepcopy(a.org),
        "rulesets": {str(k): copy.deepcopy(v) for k, v in sorted(a.rulesets.items())},
        "outside": sorted(a.outside),
        "invitations": dict(sorted(a.invitations.items())),
        "tokens": dict(sorted(a.tokens.items())),
        "protected_by_default": a.protected_by_default,
        "plan_visible": a.plan_visible,
    }


def _admin_from_json(data: object, visibility: str) -> _AdminState:
    """Configuración guardada; lo que falte toma los valores por omisión (estados anteriores a 6b)."""
    admin = _default_admin(visibility)
    if not isinstance(data, Mapping):
        return admin
    admin.settings.update(data.get("settings") or {})
    for key in ("actions", "workflow"):
        if isinstance(data.get(key), Mapping):
            getattr(admin, key).update(copy.deepcopy(dict(data[key])))
    if "retention" in data:
        admin.retention = data["retention"]
    if "org" in data:
        admin.org = copy.deepcopy(data["org"])
    admin.rulesets = {int(k): copy.deepcopy(v) for k, v in (data.get("rulesets") or {}).items()}
    admin.outside = {str(x).casefold() for x in data.get("outside") or ()}
    admin.invitations = {str(k): str(v) for k, v in (data.get("invitations") or {}).items()}
    admin.tokens = {str(k): (None if v is None else str(v)) for k, v in (data.get("tokens") or {}).items()}
    admin.protected_by_default = bool(data.get("protected_by_default", True))
    admin.plan_visible = bool(data.get("plan_visible", True))
    return admin


def _bump_counters(s: _State) -> None:
    # Un estado escrito a mano puede omitir next_id: los contadores nunca repiten un id.
    numbers = [*s.issues, *s.pulls]
    ids = [ev.id for rec in s.items() for ev in rec.events]
    ids += [c.id for rec in s.items() for c in rec.comments]
    ids += [r.id for rec in s.pulls.values() for r in rec.reviews]
    ids += [c.id for runs in s.checks.values() for c in runs]
    ids += [r.id for r in s.workflow_runs]
    users = [u.id for u in s.users.values()]
    for counter, used in (("number", numbers), ("id", ids), ("user", users)):
        if used:
            s.next_id[counter] = max(s.next_id.get(counter, 1), max(used) + 1)


def _check_namespace(namespace: str) -> None:
    if namespace not in ("heads", "tags"):
        raise ValueError(f"unknown ref namespace: {namespace!r}")


def _check_fields(fields: Mapping[str, object], allowed: set[str]) -> None:
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise TypeError(f"unknown fields: {', '.join(unknown)}")


def _seq(value: object) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise TypeError(f"expected a sequence, got {value!r}")
    return list(value)


def _label_event(ev: object) -> LabelEvent:
    if isinstance(ev, LabelEvent):
        return ev
    if isinstance(ev, Mapping):
        return _event_from_json(ev)
    raise TypeError("events must be LabelEvent objects")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _commit_to_json(c: _Commit) -> dict[str, Any]:
    return {
        "files": None if c.files is None else {p: _b64(c.files[p]) for p in sorted(c.files)},
        "parents": list(c.parents),
        "message": c.message,
        "author": c.author,
        "author_date": _iso(c.author_date),
        "committer_date": _iso(c.committer_date),
    }


def _commit_from_json(sha: str, d: Mapping[str, Any]) -> _Commit:
    files = d.get("files")
    return _Commit(
        sha,
        None if files is None else {p: base64.b64decode(v) for p, v in files.items()},
        list(d.get("parents") or []),
        str(d.get("message", "")),
        d.get("author"),
        _parse_dt(d["author_date"]),
        _parse_dt(d.get("committer_date", d["author_date"])),
    )


def _activity_to_json(a: RefActivity) -> dict[str, Any]:
    return {
        "activity_type": a.activity_type,
        "actor": a.actor,
        "timestamp": _iso(a.timestamp),
        "before": a.before,
        "after": a.after,
    }


def _activity_from_json(d: Mapping[str, Any]) -> RefActivity:
    return RefActivity(d["activity_type"], d.get("actor"), _parse_dt(d["timestamp"]), d["before"], d["after"])


def _event_to_json(e: LabelEvent) -> dict[str, Any]:
    return {"id": e.id, "event": e.event, "label": e.label, "actor": e.actor, "created_at": _iso(e.created_at)}


def _event_from_json(d: Mapping[str, Any]) -> LabelEvent:
    return LabelEvent(int(d["id"]), d["event"], d["label"], d["actor"], _parse_dt(d["created_at"]))


def _comment_to_json(c: _CommentRec) -> dict[str, Any]:
    return {
        "id": c.id,
        "author": c.author,
        "body": c.body,
        "created_at": _iso(c.created_at),
        "updated_at": _iso(c.updated_at),
    }


def _comment_from_json(d: Mapping[str, Any]) -> _CommentRec:
    created = _parse_dt(d["created_at"])
    return _CommentRec(int(d["id"]), d["author"], d["body"], created, _parse_dt(d.get("updated_at") or d["created_at"]))


def _issue_to_json(r: _IssueRec) -> dict[str, Any]:
    return {
        "number": r.number,
        "title": r.title,
        "body": r.body,
        "state": r.state,
        "state_reason": r.state_reason,
        "labels": list(r.labels),
        "assignees": list(r.assignees),
        "author": r.author,
        "created_at": _iso(r.created_at),
        "updated_at": _iso(r.updated_at),
        "closed_at": _opt_iso(r.closed_at),
        "events": [_event_to_json(e) for e in r.events],
        "comments": [_comment_to_json(c) for c in r.comments],
        "other_events": r.other_events,
    }


def _issue_from_json(d: Mapping[str, Any]) -> _IssueRec:
    return _IssueRec(
        int(d["number"]),
        d["title"],
        d.get("body", ""),
        d.get("state", "open"),
        d.get("state_reason"),
        list(d.get("labels") or []),
        list(d.get("assignees") or []),
        d["author"],
        _parse_dt(d["created_at"]),
        _parse_dt(d.get("updated_at") or d["created_at"]),
        _parse_opt_dt(d.get("closed_at")),
        [_event_from_json(e) for e in d.get("events") or []],
        [_comment_from_json(c) for c in d.get("comments") or []],
        int(d.get("other_events", 0)),
    )


def _review_to_json(r: Review) -> dict[str, Any]:
    return {
        "id": r.id,
        "author": r.author,
        "author_id": r.author_id,
        "state": r.state,
        "commit_sha": r.commit_sha,
        "submitted_at": _opt_iso(r.submitted_at),
    }


def _review_from_json(d: Mapping[str, Any]) -> Review:
    return Review(
        int(d["id"]), d["author"], int(d["author_id"]), d["state"], d.get("commit_sha"),
        _parse_opt_dt(d.get("submitted_at")),
    )


def _pull_to_json(r: _PullRec) -> dict[str, Any]:
    return {
        "number": r.number,
        "node_id": r.node_id,
        "title": r.title,
        "body": r.body,
        "state": r.state,
        "draft": r.draft,
        "merged": r.merged,
        "merged_at": _opt_iso(r.merged_at),
        "head_branch": r.head_branch,
        "head_sha": r.head_sha,
        "base_branch": r.base_branch,
        "author": r.author,
        "labels": list(r.labels),
        "assignees": list(r.assignees),
        "requested_reviewers": list(r.requested_reviewers),
        "created_at": _iso(r.created_at),
        "updated_at": _iso(r.updated_at),
        "closed_at": _opt_iso(r.closed_at),
        "merge_commit_sha": r.merge_commit_sha,
        "files": [{"path": f.path, "status": f.status, "previous_path": f.previous_path, "additions": f.additions,
                   "deletions": f.deletions} for f in r.files],
        "reviews": [_review_to_json(v) for v in r.reviews],
        "comments": [_comment_to_json(c) for c in r.comments],
        "events": [_event_to_json(e) for e in r.events],
        "commits": None if r.commits is None else list(r.commits),
        "other_events": r.other_events,
        "same_repo": r.same_repo,
        "merged_by": r.merged_by,
    }


def _pull_from_json(d: Mapping[str, Any]) -> _PullRec:
    created = _parse_dt(d["created_at"])
    return _PullRec(
        number=int(d["number"]),
        node_id=d.get("node_id") or f"PR_fake{d['number']}",
        title=d.get("title", ""),
        body=d.get("body", ""),
        state=d.get("state", "open"),
        draft=bool(d.get("draft", False)),
        merged=bool(d.get("merged", False)),
        merged_at=_parse_opt_dt(d.get("merged_at")),
        head_branch=d["head_branch"],
        head_sha=d["head_sha"],
        base_branch=d.get("base_branch", "main"),
        author=d["author"],
        labels=list(d.get("labels") or []),
        assignees=list(d.get("assignees") or []),
        requested_reviewers=list(d.get("requested_reviewers") or []),
        created_at=created,
        updated_at=_parse_dt(d.get("updated_at") or d["created_at"]),
        closed_at=_parse_opt_dt(d.get("closed_at")),
        merge_commit_sha=d.get("merge_commit_sha"),
        files=[ChangedFile(f["path"], f["status"], f.get("previous_path"), int(f.get("additions") or 0),
                           int(f.get("deletions") or 0)) for f in d.get("files") or []],
        reviews=[_review_from_json(v) for v in d.get("reviews") or []],
        comments=[_comment_from_json(c) for c in d.get("comments") or []],
        events=[_event_from_json(e) for e in d.get("events") or []],
        commits=None if d.get("commits") is None else list(d["commits"]),
        other_events=int(d.get("other_events", 0)),
        same_repo=bool(d.get("same_repo", True)),  # un estado anterior no lo trae
        merged_by=d.get("merged_by"),  # tampoco este (D-45)
    )


def _run_to_json(r: _RunRec) -> dict[str, Any]:
    return {
        "id": r.id,
        "workflow_file": r.workflow_file,
        "name": r.name,
        "event": r.event,
        "status": r.status,
        "conclusion": r.conclusion,
        "head_sha": r.head_sha,
        "head_branch": r.head_branch,
        "actor": r.actor,
        "created_at": _iso(r.created_at),
    }


def _run_from_json(d: Mapping[str, Any]) -> _RunRec:
    return _RunRec(
        int(d["id"]),
        d["workflow_file"],
        d["name"],
        d.get("event", "push"),
        d.get("status", "completed"),
        d.get("conclusion"),
        d["head_sha"],
        d.get("head_branch"),
        d["actor"],
        _parse_dt(d["created_at"]),
    )
