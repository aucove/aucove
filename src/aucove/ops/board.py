# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Tablero de tareas que comparten ``status`` y ``context`` (05 §9.6 y §9.7; 08 §7.5).

**Lecturas en bloque** (08 §7.5): los Issues con ``task``, el Issue abierto con
``main-broken``, los PR, las refs ``task/*``, la ref y el CI de la base y, por cada PR
abierto de una tarea, sus revisiones y sus checks (sin ese detalle si hay más de 30).

**Estado.** Solo lo calcula ``derive_state`` (04-tareas REQ-TASK-3), siempre sobre una
instantánea completa de 04 §1.1, salvo en las tareas terminadas:

- Una tarea integrada o cancelada se deriva con lo leído en bloque: ``derive_state``
  llega a ``merged`` o ``cancelled`` antes de mirar eventos o marcadores (04 §1.5).
- Una tarea activa (``proposed`` a ``expired``) se deriva con ``collect_task`` y un
  ``Prefetch``, que solo agrega las lecturas propias de la tarea. El resultado queda en
  ``.aucove/cache/tasks/<n>.json`` con una **huella** de lo leído en bloque (el Issue con
  su ``updated_at``, sus etiquetas y asignados; la ref; los PR con su cabeza y su
  ``updated_at``; las revisiones; y la política de la base). Si la huella no cambió y la
  derivación tiene menos de 24 h, se reutiliza: un comentario nuevo, una etiqueta, un
  asignado, un push o una revisión cambian la huella.

**Cachés** (01 §7.3): ``forge-snapshot.json`` guarda el tablero completo con clave
``{"repo"}``; está fresco 10 min y se puede usar hasta 7 días sin red. ``tasks/<n>.json``
conserva el ``local_branch`` que escriben ``claim`` y ``switch``. Nada de esto guarda un
token (REQ-ARCH-60).

**Evidencia** (REQ-OPS-66): ``copy_evidence`` copia los registros ``ci:evidence`` de los
PR propios abiertos o integrados en los últimos 7 días. Nunca cambia el resultado: un
fallo solo queda en ``events.jsonl``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal

from ..domain.taskbody import analyze_task_body
from ..domain.tasks import STATES, Authz, Derived, blocked_ok, derive_state, effective_sections, resolve_owner
from ..domain.text import same_login
from ..forge.base import ForgeError, ForgeNotFound
from .snapshot import CI_ACTORS, TASK_LABEL, Prefetch, TaskEvidence, authz_from_roles, bulk_snapshot, collect_task
from .snapshot import task_branch, task_number

if TYPE_CHECKING:
    from ..config.project import ProjectConfig
    from ..config.roles import Roles
    from ..forge.base import Checks, ForgeAdapter, Issue, PullRequest, Ref
    from ..forge.base import Review as ForgeReview
    from ..localstore.store import Store
    from .context import OpContext

__all__ = [
    "FRESH_S",
    "NET_KINDS",
    "SNAPSHOT_FILE",
    "USABLE_S",
    "BaseView",
    "Board",
    "Fetched",
    "LoadMode",
    "Loaded",
    "PrView",
    "TaskView",
    "board_from_json",
    "board_to_json",
    "copy_evidence",
    "is_network_error",
    "load_board",
    "load_cached",
    "owner_and_sections",
    "write_task_cache",
    "refresh",
    "visible",
]

FRESH_S = 10 * 60  # forge-snapshot.json fresca (01 §7.3)
USABLE_S = 7 * 86400  # utilizable sin red (01 §7.3)
RECHECK_S = 86400  # una derivación con la misma huella se rehace al menos una vez al día
RECENT_CLOSED = timedelta(days=7)  # 05 §9.6 y REQ-OPS-66
PR_DETAIL_MAX = 30  # 08 §7.5: con más PR abiertos, sin detalle por PR
NET_KINDS = frozenset({"unreachable", "timeout"})  # «sin red» (05 §12.3)
# Comentarios ci:evidence que no se copian y quedan en events.jsonl (REQ-OPS-66).
_SKIP_EVENTS = {"invalid": "evidence-copy-invalid", "untrusted_author": "evidence-copy-untrusted"}
SNAPSHOT_FILE = "forge-snapshot.json"
BOARD_VERSION = 1
MAIN_BROKEN = "main-broken"
# Etiquetas de 00-marco §5.4 que muestra task_summary.flags ($defs/label).
FLAG_LABELS = frozenset(
    {"flag:overlap", "flag:direction-change", "flag:blocked", "ack:direction", "flag:bypass",
     "flag:instructions", "ack:overlap", "review:model"}
)
CiState = Literal["success", "failure", "pending", "unknown"]


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None


def is_network_error(exc: BaseException) -> bool:
    """``True`` si la falla de la forja es «sin red» (``E_NET_OFFLINE`` o ``E_NET_TIMEOUT``)."""
    return isinstance(exc, ForgeError) and exc.kind in NET_KINDS


# -- modelo ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrView:
    """PR de una tarea (``pr_brief`` y lo que necesitan los avisos de ``context``)."""

    number: int
    url: str
    draft: bool
    ci: CiState
    review: Literal["approved", "changes_requested", "pending", "none"]
    state: str  # open | closed
    merged: bool
    merged_at: str | None
    author: str
    requested_reviewers: tuple[str, ...]
    # Para review list (05 §9.22), también sin red: el título, las etiquetas, la cabeza y la
    # última actualización del PR. Una caché anterior no los trae: quedan vacíos.
    title: str = ""
    labels: tuple[str, ...] = ()
    head_sha: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class TaskView:
    """Una tarea del tablero. ``title`` es el texto crudo de la forja: se sanea al devolverlo."""

    number: int
    title: str
    author: str
    state: str
    owner: str | None
    sections: tuple[str, ...]  # efectivas con dueño (04 §3); si no, las del TaskSpec
    plan_refs: tuple[str, ...]
    branch: str | None
    pr: PrView | None
    flags: tuple[str, ...]
    anomalies: tuple[str, ...]
    updated_at: str
    closed_at: str | None
    last_activity_at: str | None
    blocked: bool  # flag:blocked válida: el reclamo no vence (REQ-TASK-34)

    def expires_at(self, expiry_days: int) -> str | None:
        """Vencimiento del reclamo (04 §4.2): solo ``claimed`` y ``changes_requested``."""
        if self.state not in ("claimed", "changes_requested") or self.blocked:
            return None
        last = _parse(self.last_activity_at)
        return _iso(last + timedelta(days=expiry_days)) if last is not None else None


@dataclass(frozen=True)
class BaseView:
    branch: str
    sha: str | None
    ci: CiState
    main_broken_issue: int | None
    pdf_url: str | None


@dataclass(frozen=True)
class Board:
    """Tablero completo: todas las tareas, también las terminadas hace tiempo."""

    repo: str
    login: str | None  # identidad con la que se leyó
    fetched_at: datetime
    base: BaseView
    tasks: tuple[TaskView, ...]
    profile: str | None  # enforced | advisory; None si no se pudo detectar
    default_branch: str | None

    def age_s(self, now: datetime) -> int:
        return max(0, int((now - self.fetched_at).total_seconds()))

    def task(self, number: int) -> TaskView | None:
        return next((t for t in self.tasks if t.number == number), None)


def visible(task: TaskView, now: datetime) -> bool:
    """05 §9.6: no se listan las integradas ni las canceladas cerradas hace más de 7 días."""
    if task.state not in ("merged", "cancelled"):
        return True
    closed = _parse(task.closed_at)
    return closed is None or now - closed <= RECENT_CLOSED


# -- serialización (forge-snapshot.json) -----------------------------------------------


def board_to_json(board: Board) -> dict[str, object]:
    def pr(p: PrView | None) -> dict[str, object] | None:
        if p is None:
            return None
        out = dict(p.__dict__)
        out["requested_reviewers"] = list(p.requested_reviewers)
        out["labels"] = list(p.labels)
        return out

    tasks = []
    for t in board.tasks:
        row: dict[str, Any] = dict(t.__dict__)
        for key in ("sections", "plan_refs", "flags", "anomalies"):
            row[key] = list(row[key])
        row["pr"] = pr(t.pr)
        tasks.append(row)
    return {
        "v": BOARD_VERSION,
        "repo": board.repo,
        "login": board.login,
        "fetched_at": _iso(board.fetched_at),
        "base": dict(board.base.__dict__),
        "tasks": tasks,
        "profile": board.profile,
        "default_branch": board.default_branch,
    }


class _Bad(ValueError):
    pass


def _str(obj: Mapping[str, Any], key: str, *, null: bool = False) -> Any:
    value = obj.get(key)
    if value is None and null:
        return None
    if not isinstance(value, str):
        raise _Bad(key)
    return value


def _int(obj: Mapping[str, Any], key: str, *, null: bool = False) -> Any:
    value = obj.get(key)
    if value is None and null:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _Bad(key)
    return value


def _strs(obj: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = obj.get(key)
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise _Bad(key)
    return tuple(value)


def _bool(obj: Mapping[str, Any], key: str) -> bool:
    value = obj.get(key)
    if not isinstance(value, bool):
        raise _Bad(key)
    return value


def _pr_from(raw: object) -> PrView | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise _Bad("pr")
    ci = _str(raw, "ci")
    review = _str(raw, "review")
    if ci not in ("success", "failure", "pending", "unknown") or review not in (
        "approved", "changes_requested", "pending", "none"
    ):
        raise _Bad("pr")
    return PrView(
        number=_int(raw, "number"),
        url=_str(raw, "url"),
        draft=_bool(raw, "draft"),
        ci=ci,
        review=review,
        state=_str(raw, "state"),
        merged=_bool(raw, "merged"),
        merged_at=_str(raw, "merged_at", null=True),
        author=_str(raw, "author"),
        requested_reviewers=_strs(raw, "requested_reviewers"),
        title=raw["title"] if isinstance(raw.get("title"), str) else "",
        labels=_strs(raw, "labels") if "labels" in raw else (),
        head_sha=_str(raw, "head_sha", null=True) if "head_sha" in raw else None,
        updated_at=_str(raw, "updated_at", null=True) if "updated_at" in raw else None,
    )


def board_from_json(data: Mapping[str, Any]) -> Board | None:
    """Tablero de ``forge-snapshot.json``; ``None`` si no tiene la forma esperada (REQ-ARCH-58)."""
    try:
        if data.get("v") != BOARD_VERSION:
            return None
        fetched = _parse(data.get("fetched_at"))
        base = data.get("base")
        raw_tasks = data.get("tasks")
        if fetched is None or not isinstance(base, dict) or not isinstance(raw_tasks, list):
            return None
        ci = _str(base, "ci")
        if ci not in ("success", "failure", "pending", "unknown"):
            return None
        base_view = BaseView(
            branch=_str(base, "branch"),
            sha=_str(base, "sha", null=True),
            ci=ci,
            main_broken_issue=_int(base, "main_broken_issue", null=True),
            pdf_url=_str(base, "pdf_url", null=True),
        )
        tasks = []
        for raw in raw_tasks:
            if not isinstance(raw, dict):
                return None
            state = _str(raw, "state")
            if state not in STATES:
                return None
            tasks.append(
                TaskView(
                    number=_int(raw, "number"),
                    title=_str(raw, "title"),
                    author=_str(raw, "author"),
                    state=state,
                    owner=_str(raw, "owner", null=True),
                    sections=_strs(raw, "sections"),
                    plan_refs=_strs(raw, "plan_refs"),
                    branch=_str(raw, "branch", null=True),
                    pr=_pr_from(raw.get("pr")),
                    flags=_strs(raw, "flags"),
                    anomalies=_strs(raw, "anomalies"),
                    updated_at=_str(raw, "updated_at"),
                    closed_at=_str(raw, "closed_at", null=True),
                    last_activity_at=_str(raw, "last_activity_at", null=True),
                    blocked=_bool(raw, "blocked"),
                )
            )
        profile = data.get("profile")
        if profile not in (None, "enforced", "advisory"):
            return None
        return Board(
            repo=_str(data, "repo"),
            login=_str(data, "login", null=True),
            fetched_at=fetched,
            base=base_view,
            tasks=tuple(sorted(tasks, key=lambda t: t.number)),
            profile=profile,
            default_branch=_str(data, "default_branch", null=True),
        )
    except _Bad:
        return None


def load_cached(store: Store, repo: str) -> Board | None:
    """``forge-snapshot.json`` del repositorio ``repo``; ``None`` si falta o no es válida."""
    data = store.read(SNAPSHOT_FILE, key={"repo": repo})
    if data is None:
        return None
    board = board_from_json(data)
    return board if board is not None and board.repo == repo else None


EXPIRING_WINDOW = timedelta(hours=24)  # W_CLAIM_EXPIRING (05 §12.2)


def expiring(tasks: Iterable[TaskView], login: str | None, now: datetime, expiry_days: int) -> list[tuple[int, str]]:
    """Las tareas de ``login`` cuyo reclamo vence en menos de 24 h, o ya venció sin la etiqueta.

    Solo corre el reloj de ``claimed`` y ``changes_requested`` sin ``flag:blocked`` válida
    (``TaskView.expires_at``; REQ-TASK-33 y REQ-TASK-34). Da ``(número, expires_at)``.
    """
    out: list[tuple[int, str]] = []
    for row in tasks:
        if not same_login(row.owner, login):
            continue
        expires = row.expires_at(expiry_days)
        when = _parse(expires)
        if expires is not None and when is not None and when - now < EXPIRING_WINDOW:
            out.append((row.number, expires))
    return out


def expiring_warnings(t: Any, tasks: Iterable[TaskView], login: str | None, now: datetime,
                      expiry_days: int) -> list[Any]:
    """``W_CLAIM_EXPIRING`` por cada reclamo propio de ``expiring`` (``context``, ``status`` y ``save``)."""
    from .result import make_warning

    return [make_warning("W_CLAIM_EXPIRING", t, details={"task": n, "expires_at": at})
            for n, at in expiring(tasks, login, now, expiry_days)]


# -- derivación ---------------------------------------------------------------------------


def _ci(checks: Checks | None) -> CiState:
    if checks is None:
        return "unknown"
    overall = checks.overall
    return overall if overall in ("success", "failure", "pending") else "unknown"  # type: ignore[return-value]


def _fingerprint(
    issue: Issue,
    ref: Ref | None,
    pulls: Sequence[PullRequest],
    reviews: Mapping[int, Sequence[ForgeReview]],
    authz: Authz,
    base_branch: str,
) -> str:
    doc = {
        "issue": [
            _iso(issue.updated_at), issue.state, issue.state_reason, sorted(issue.labels), sorted(issue.assignees),
            hashlib.sha256(issue.body.encode("utf-8", "surrogatepass")).hexdigest()[:16],
        ],
        "ref": ref.sha if ref is not None else None,
        "pulls": [
            [p.number, p.state, p.merged, p.draft, p.head_sha, p.base_branch, _iso(p.updated_at),
             bool(getattr(p, "same_repo", True))]
            for p in sorted(pulls, key=lambda p: p.number)
        ],
        "reviews": {
            str(n): [[r.id, r.state, _iso(r.submitted_at)] for r in reviews[n]]
            for n in sorted(p.number for p in pulls)
            if n in reviews
        },
        "authz": [authz.lead, authz.deputy, sorted(authz.section_owner.items()), sorted(authz.ci_actors)],
        "base": base_branch,
    }
    canon = json.dumps(doc, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("ascii")).hexdigest()[:32]


@dataclass(frozen=True)
class _Derivation:
    """Lo que se guarda de una derivación en ``tasks/<n>.json`` (además del sobre de H2)."""

    state: str
    owner: str | None
    owner_source: str | None
    sections: tuple[str, ...]
    anomalies: tuple[str, ...]
    approved: bool
    pr: int | None
    last_activity_at: str | None
    blocked: bool


def owner_and_sections(snapshot: Any, derived: Derived) -> tuple[str | None, tuple[str, ...]]:
    """Reclamante y secciones efectivas, como ``to_permission_task`` (04 §1.5, §3)."""
    owner = derived.owner
    if owner is None and derived.state == "cancelled":
        owner = resolve_owner(snapshot)[0]
    sections, _an = effective_sections(snapshot, owner)
    return owner, tuple(sections)


def _last_activity(evidence: TaskEvidence, derived: Derived) -> datetime | None:
    """``last_activity_at`` de 04 §4.2 con lo leído: reclamo, renovación, push y devolución."""
    snap = evidence.snapshot
    owner = derived.owner
    found: list[datetime] = []
    if evidence.claim_commit is not None:
        found.append(evidence.claim_commit.author_date)
    found.extend(m.updated_at for m in snap.issue_markers if m.kind == "claim" and same_login(m.author, owner))
    if snap.ref is not None and snap.ref.last_push_at is not None:
        found.append(snap.ref.last_push_at)
    if derived.state == "changes_requested" and derived.pr is not None:
        for pull in snap.pulls:
            if pull.number == derived.pr:
                found.extend(
                    r.submitted_at for r in pull.reviews if r.state == "CHANGES_REQUESTED" and r.submitted_at is not None
                )
    return max(found) if found else None


def _derive_full(evidence: TaskEvidence) -> _Derivation:
    snap = evidence.snapshot
    derived = derive_state(snap)
    owner, sections = owner_and_sections(snap, derived)
    return _Derivation(
        state=derived.state,
        owner=owner,
        owner_source=derived.owner_source,
        sections=sections,
        anomalies=derived.anomalies,
        approved=derived.approved,
        pr=derived.pr,
        last_activity_at=_iso(_last_activity(evidence, derived)),
        blocked=blocked_ok(snap, derived.owner),
    )


def _derive_terminal(snapshot: Any) -> _Derivation:
    derived = derive_state(snapshot)
    owner, sections = owner_and_sections(snapshot, derived)
    return _Derivation(
        state=derived.state,
        owner=owner,
        owner_source=derived.owner_source,
        sections=sections,
        anomalies=derived.anomalies,
        approved=derived.approved,
        pr=derived.pr,
        last_activity_at=None,
        blocked=False,
    )


def _cached_derivation(store: Store, number: int, repo: str, fingerprint: str, now: datetime) -> _Derivation | None:
    data = store.read(f"tasks/{number}.json", key={"repo": repo})
    view = data.get("view") if data is not None else None
    if not isinstance(view, dict) or view.get("fingerprint") != fingerprint:
        return None
    derived_at = _parse(view.get("derived_at"))
    if derived_at is None or not 0 <= (now - derived_at).total_seconds() <= RECHECK_S:
        return None
    try:
        state = _str(data, "state")  # type: ignore[arg-type]
        if state not in STATES:
            return None
        return _Derivation(
            state=state,
            owner=_str(data, "claimant", null=True),  # type: ignore[arg-type]
            owner_source=_str(view, "owner_source", null=True),
            sections=_strs(data, "sections"),  # type: ignore[arg-type]
            anomalies=_strs(view, "anomalies"),
            approved=_bool(view, "approved"),
            pr=_int(view, "pr", null=True),
            last_activity_at=_str(view, "last_activity_at", null=True),
            blocked=_bool(view, "blocked"),
        )
    except _Bad:
        return None


def _write_task_cache(
    store: Store, number: int, repo: str, d: _Derivation, *, fingerprint: str | None, now: datetime
) -> None:
    """``tasks/<n>.json``: los campos de H2 (``TaskCache``) y la derivación en ``view``.

    Conserva el ``local_branch`` que escribieron ``claim`` o ``switch`` (06 §2.1).
    """
    previous = store.read(f"tasks/{number}.json")
    local_branch = previous.get("local_branch") if previous is not None else None
    data: dict[str, object] = {
        "number": number,
        "claimant": d.owner,
        "state": d.state,
        "sections": list(d.sections),
        "local_branch": local_branch if isinstance(local_branch, str) and local_branch else None,
        "fetched_at": _iso(now),
    }
    if fingerprint is not None:
        data["view"] = {
            "fingerprint": fingerprint,
            "derived_at": _iso(now),
            "owner_source": d.owner_source,
            "anomalies": list(d.anomalies),
            "approved": d.approved,
            "pr": d.pr,
            "last_activity_at": d.last_activity_at,
            "blocked": d.blocked,
        }
    try:
        store.write(f"tasks/{number}.json", data, key={"repo": repo})
    except (TypeError, ValueError):  # la caché es descartable (REQ-ARCH-57)
        return


def write_task_cache(
    store: Store,
    number: int,
    repo: str,
    *,
    claimant: str | None,
    state: str,
    sections: Sequence[str],
    now: datetime,
    local_branch: str | None = None,
) -> None:
    """``tasks/<n>.json`` tras ``claim`` o ``switch`` (05 §9.12 paso 8 y §9.16 paso 5).

    Sin derivación guardada: la siguiente lectura de ``status`` o ``context`` la rehace. Con
    ``local_branch`` (sesión en la nube) la fija; si no, conserva la que hubiera.
    """
    previous = store.read(f"tasks/{number}.json")
    kept = previous.get("local_branch") if previous is not None else None
    branch = local_branch if local_branch is not None else (kept if isinstance(kept, str) and kept else None)
    data: dict[str, object] = {
        "number": number,
        "claimant": claimant,
        "state": state,
        "sections": list(sections),
        "local_branch": branch,
        "fetched_at": _iso(now),
    }
    try:
        store.write(f"tasks/{number}.json", data, key={"repo": repo})
    except (TypeError, ValueError):  # la caché es descartable (REQ-ARCH-57)
        return


def _review_of(d: _Derivation, pr: PullRequest | None) -> Literal["approved", "changes_requested", "pending", "none"]:
    if pr is None:
        return "none"
    if d.state == "changes_requested":
        return "changes_requested"
    if d.approved:
        return "approved"
    if pr.state == "open" and not pr.draft:
        return "pending"
    return "none"


def _pr_view(d: _Derivation, pulls: Sequence[PullRequest], checks: Mapping[int, CiState]) -> PrView | None:
    if d.pr is None:
        return None
    pr = next((p for p in pulls if p.number == d.pr), None)
    if pr is None:
        return None
    return PrView(
        number=pr.number,
        url=pr.url,
        draft=pr.draft,
        ci=checks.get(pr.number, "unknown"),
        review=_review_of(d, pr),
        state=pr.state,
        merged=pr.merged,
        merged_at=_iso(pr.merged_at),
        author=pr.author,
        requested_reviewers=tuple(pr.requested_reviewers),
        title=pr.title,
        labels=tuple(pr.labels),
        head_sha=pr.head_sha,
        updated_at=_iso(pr.updated_at),
    )


def _closed_at(issue: Issue, d: _Derivation, pulls: Sequence[PullRequest]) -> str | None:
    if d.state == "merged":
        merged = [p.merged_at for p in pulls if p.merged and p.merged_at is not None]
        return _iso(max(merged)) if merged else _iso(issue.closed_at)
    if d.state == "cancelled":
        return _iso(issue.closed_at)
    return None


# -- lectura en vivo ----------------------------------------------------------------------


@dataclass(frozen=True)
class Fetched:
    """Tablero leído en vivo, con lo leído en bloque: los PR (copia de evidencia) y, por
    tarea, el Issue, la ref, sus PR y las revisiones (``prefetch`` para ``claim``)."""

    board: Board
    pulls: tuple[PullRequest, ...]
    issues: Mapping[int, Issue] = field(default_factory=dict)
    refs: Mapping[int, Ref] = field(default_factory=dict)
    task_pulls: Mapping[int, tuple[PullRequest, ...]] = field(default_factory=dict)
    reviews: Mapping[int, Sequence[ForgeReview]] = field(default_factory=dict)

    def prefetch(self, number: int) -> Prefetch | None:
        """Lo leído en bloque de la tarea ``number`` (``None`` si no estaba)."""
        issue = self.issues.get(number)
        if issue is None:
            return None
        pulls = self.task_pulls.get(number, ())
        return Prefetch(issue=issue, ref=self.refs.get(number), pulls=pulls,
                        reviews={p.number: self.reviews[p.number] for p in pulls if p.number in self.reviews})


def refresh(
    forge: ForgeAdapter,
    *,
    cfg: ProjectConfig,
    roles: Roles,
    store: Store,
    now: datetime,
) -> Fetched:
    """Lee la forja, deriva cada tarea y escribe las cachés. Propaga los ``ForgeError``."""
    login = forge.identity().login
    try:
        caps = forge.capabilities()
    except ForgeError:
        caps = None
    # La clave de las cachés es forge.repo de la base, la misma con la que se leen sin red.
    repo = cfg.forge_repo or f"{forge.repo.owner}/{forge.repo.name}"
    base_branch = cfg.base_branch
    authz = authz_from_roles(roles)

    issues = forge.list_issues(labels=[TASK_LABEL], state="all")
    broken = forge.list_issues(labels=[MAIN_BROKEN], state="open")
    pulls = forge.list_pull_requests(state="all")
    refs = forge.list_refs("task/")
    try:
        base_ref = forge.get_ref(base_branch)
    except ForgeNotFound:
        base_ref = None
    base_ci = _ci(forge.get_checks(base_ref.sha)) if base_ref is not None else "unknown"

    by_task: dict[int, list[PullRequest]] = {}
    for pr in pulls:
        n = task_number(pr.head_branch)
        if n is not None:
            by_task.setdefault(n, []).append(pr)
    refs_by = {n: r for r in refs if (n := task_number(r.branch)) is not None}
    open_prs = sorted((p for ps in by_task.values() for p in ps if p.state == "open"), key=lambda p: p.number)
    reviews: dict[int, Sequence[ForgeReview]] = {}
    checks: dict[int, CiState] = {}
    if len(open_prs) <= PR_DETAIL_MAX:
        for pr in open_prs:
            reviews[pr.number] = forge.list_reviews(pr.number)
            checks[pr.number] = _ci(forge.get_checks(pr.head_sha))

    rows: list[TaskView] = []
    for issue in sorted(issues, key=lambda i: i.number):
        if not any(x.lower() == TASK_LABEL for x in issue.labels):
            continue
        n = issue.number
        tp = tuple(sorted(by_task.get(n, ()), key=lambda p: p.number))
        ref = refs_by.get(n)
        body = analyze_task_body(issue.body)
        terminal = issue.state == "closed" or any(p.merged and getattr(p, "same_repo", True) for p in tp)
        if terminal:
            d = _derive_terminal(bulk_snapshot(issue, body, ref, tp, authz=authz, base_branch=base_branch))
            if store.read(f"tasks/{n}.json") is not None:  # que los hooks dejen de ver la tarea activa
                _write_task_cache(store, n, repo, d, fingerprint=None, now=now)
        else:
            mine = {p.number: reviews[p.number] for p in tp if p.number in reviews}
            fingerprint = _fingerprint(issue, ref, tp, mine, authz, base_branch)
            cached = _cached_derivation(store, n, repo, fingerprint, now)
            if cached is not None:
                d = cached
            else:
                for p in tp:  # sin detalle en bloque (P > 30): las revisiones del PR abierto
                    if p.state == "open" and p.number not in mine:
                        mine[p.number] = forge.list_reviews(p.number)
                evidence = collect_task(
                    forge,
                    n,
                    authz,
                    base_branch=base_branch,
                    default_branch=caps.default_branch if caps is not None else None,
                    prefetch=Prefetch(issue=issue, ref=ref, pulls=tp, reviews=mine),
                )
                if evidence is None:
                    continue
                d = _derive_full(evidence)
                _write_task_cache(store, n, repo, d, fingerprint=fingerprint, now=now)
        rows.append(
            TaskView(
                number=n,
                title=issue.title,
                author=issue.author,
                state=d.state,
                owner=d.owner,
                sections=d.sections if d.owner is not None else body.sections,
                plan_refs=tuple(body.spec.get("plan_refs", ())) if body.spec is not None else (),  # type: ignore[arg-type]
                branch=task_branch(n) if ref is not None else None,
                pr=_pr_view(d, tp, checks),
                flags=tuple(sorted(x for x in issue.labels if x in FLAG_LABELS)),
                anomalies=d.anomalies,
                updated_at=_iso(max([issue.updated_at, *(p.updated_at for p in tp)])) or "",
                closed_at=_closed_at(issue, d, tp),
                last_activity_at=d.last_activity_at,
                blocked=d.blocked,
            )
        )
    board = Board(
        repo=repo,
        login=login,
        fetched_at=now,
        base=BaseView(
            branch=base_branch,
            sha=base_ref.sha if base_ref is not None else None,
            ci=base_ci,
            main_broken_issue=min((i.number for i in broken), default=None),
            pdf_url=None,
        ),
        tasks=tuple(rows),
        profile=caps.profile if caps is not None else None,
        default_branch=caps.default_branch if caps is not None else None,
    )
    try:
        store.write(SNAPSHOT_FILE, board_to_json(board), key={"repo": repo})
    except (TypeError, ValueError):
        pass
    return Fetched(
        board=board,
        pulls=tuple(pulls),
        issues={i.number: i for i in issues},
        refs=refs_by,
        task_pulls={n: tuple(sorted(ps, key=lambda p: p.number)) for n, ps in by_task.items()},
        reviews=reviews,
    )


# -- carga para las operaciones -----------------------------------------------------------

LoadMode = Literal["live", "auto", "refresh", "offline"]


@dataclass(frozen=True)
class Loaded:
    """Tablero con su origen; ``reason`` dice por qué se usó la caché en lugar de la forja."""

    board: Board
    source: Literal["live", "cache"]
    fetched: Fetched | None
    reason: str | None  # kind de la falla, código de la fábrica u "offline"; None si no hubo falla

    def offline_details(self, now: datetime) -> dict[str, object]:
        """``details`` de ``W_NET_OFFLINE``: el motivo y la antigüedad de la caché."""
        return {"reason": self.reason, "age_s": self.board.age_s(now)}


def load_board(
    ctx: OpContext,
    *,
    cfg: ProjectConfig,
    roles: Roles,
    store: Store,
    now: datetime,
    mode: LoadMode,
) -> Loaded | None:
    """Tablero según el modo; ``None`` si no hay forja ni caché utilizable.

    - ``live`` (``status``): lee la forja. Sin red (``E_NET_OFFLINE`` o ``E_NET_TIMEOUT``)
      usa la caché de hasta 7 días; cualquier otra falla se propaga.
    - ``auto`` (``context``): la caché si tiene menos de 10 min (REQ-OPS-43); si no, como
      ``refresh``.
    - ``refresh`` (``context --refresh``): lee la forja; ante cualquier falla de la forja o
      de su fábrica usa la caché (``context`` nunca falla por la forja, REQ-OPS-43).
    - ``offline`` (SessionStart, 07-hooks REQ-HOOK-17): solo la caché, sin red.
    """
    repo = cfg.forge_repo
    cached = load_cached(store, repo) if repo else None
    if cached is not None and cached.age_s(now) > USABLE_S:
        cached = None
    if mode == "offline":
        return Loaded(cached, "cache", None, "offline") if cached is not None else None
    if mode == "auto" and cached is not None and cached.age_s(now) < FRESH_S:
        return Loaded(cached, "cache", None, None)
    from .result import OpError

    reason: str
    try:
        fetched = refresh(ctx.forge(), cfg=cfg, roles=roles, store=store, now=now)
        return Loaded(fetched.board, "live", fetched, None)
    except ForgeError as exc:
        if mode == "live" and not is_network_error(exc):
            raise
        reason = exc.kind
    except OpError as exc:  # la fábrica de la forja: E_FORGE_CAPABILITY_MISSING, E_CONFIG_INVALID
        if mode == "live":
            raise
        reason = exc.code
    return Loaded(cached, "cache", None, reason) if cached is not None else None


# -- copia de evidencia (REQ-OPS-66) ----------------------------------------------------


def copy_evidence(
    ctx: OpContext,
    forge: ForgeAdapter,
    fetched: Fetched,
    *,
    now: datetime,
    ci_actors: Iterable[str] = CI_ACTORS,
    emit: Callable[..., None] | None = None,
) -> int:
    """Copia los ``ci:evidence`` de los PR propios a ``.aucove/evidence/``; devuelve cuántos.

    Nunca lanza (REQ-OPS-66): cualquier fallo, también de la forja, queda en
    ``events.jsonl`` y la copia sigue con el PR siguiente.
    """
    from ..evidence.local_copy import copy_comment
    from ..localstore.store import ensure_root

    log = emit or ctx.emit
    login = fetched.board.login
    if not login:
        return 0
    copied = 0
    try:
        store = ctx.store()
        if not ensure_root(store.root):
            return 0
        root = store.root / "evidence"
    except Exception:  # noqa: BLE001 - la copia nunca cambia el resultado
        log(kind="op", name="evidence-copy", result="error")
        return 0
    actors = tuple(ci_actors)
    for pr in sorted(fetched.pulls, key=lambda p: p.number):
        if not same_login(pr.author, login):
            continue
        recent = pr.merged and pr.merged_at is not None and now - pr.merged_at <= RECENT_CLOSED
        if pr.state != "open" and not recent:
            continue
        n = task_number(pr.head_branch)
        try:
            comments = forge.list_comments(pr.number)
        except Exception:  # noqa: BLE001
            log(kind="op", name="evidence-copy", result="error", task=n)
            continue
        for comment in comments:
            try:
                outcome = copy_comment(root, comment.body, comment.author, ci_actors=actors)
            except Exception:  # noqa: BLE001 - OSError al escribir, sobre todo
                log(kind="op", name="evidence-copy", result="error", task=n)
                continue
            if outcome.outcome in ("copied", "copied_truncated"):
                copied += 1
                name = "evidence-copy-truncated" if outcome.outcome == "copied_truncated" else "evidence-copy"
                log(kind="op", name=name, result="ok", task=n)
            elif outcome.outcome in _SKIP_EVENTS:
                log(kind="op", name=_SKIP_EVENTS[outcome.outcome], result="skip", task=n)
    return copied

