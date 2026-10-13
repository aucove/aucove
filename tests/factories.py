# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Factorías de T1: instantáneas de tarea para ``derive_state`` (12-mvp §3.2).

``snap()`` arma un ``TaskSnapshot`` coherente con pocos argumentos; ``snapshots()``
es la estrategia de Hypothesis para los invariantes; ``block_io()`` bloquea red y
archivos con monkeypatch (REQ-MVP-24).

Personas (las del fixture compartido ``basic``): ``ana`` es ``lead``, ``bruno`` es
``deputy``, ``carla`` es la dueña del reclamo y de ``methods`` y ``dario`` no
tiene rol. Los actores de ``label_events`` y ``reviews`` aceptan los alias
``lead``, ``deputy``, ``owner``, ``other`` y ``ci``.
"""

import builtins
import io
import os
import socket
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from types import FrameType
from typing import Any

import pytest
from hypothesis import strategies as st

from aucove.domain.tasks import (
    Authz,
    LabelEvent,
    Marker,
    Pull,
    Review,
    TaskRef,
    TaskSnapshot,
)

__all__ = [
    "ALIASES",
    "BASE",
    "CI_ACTOR",
    "CLAIM_SHA",
    "DEPUTY",
    "HEAD_SHA",
    "IOBlocked",
    "LEAD",
    "OLD_CLAIM_SHA",
    "OTHER",
    "OTHER_SPEC_HASH",
    "OWNER",
    "PR_NUMBER",
    "SECTIONS",
    "SECTION_OWNER",
    "SPEC_HASH",
    "T0",
    "TASK",
    "at",
    "block_io",
    "claim_marker",
    "expiry_marker",
    "io_guard",
    "label_event",
    "make_authz",
    "marker",
    "package_dirs",
    "pull",
    "release_marker",
    "review",
    "snap",
    "snapshots",
    "submission_marker",
    "task_ref",
    "who",
]

UTC = timezone.utc
T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)

LEAD = "ana"
DEPUTY = "bruno"
OWNER = "carla"
OTHER = "dario"
CI_ACTOR = "github-actions[bot]"
ALIASES: Mapping[str, str] = {
    "lead": LEAD, "deputy": DEPUTY, "owner": OWNER, "other": OTHER, "ci": CI_ACTOR,
}

BASE = "main"
TASK = 17
PR_NUMBER = 42
HEAD_SHA = "a" * 40
CLAIM_SHA = "c" * 40
OLD_CLAIM_SHA = "b" * 40
BASE_SHA = "d" * 40
SPEC_HASH = "sha256:" + "1" * 64
OTHER_SPEC_HASH = "sha256:" + "2" * 64
SECTIONS: tuple[str, ...] = ("methods",)
SECTION_OWNER: Mapping[str, str] = {"introduction": LEAD, "methods": OWNER, "results": DEPUTY}

# Línea de tiempo por defecto (minutos desde T0): eventos de etiqueta en 0, 1, 2...;
# reclamo en 10; PR y entrega en 30; revisiones en 40, 41...
CLAIM_AT = 10
PR_AT = 30
REVIEW_AT = 40


def at(minutes: float) -> datetime:
    """Instante fijo ``T0 + minutes``; nunca lee el reloj."""
    return T0 + timedelta(minutes=minutes)


def who(actor: str) -> str:
    """Resuelve los alias de persona (``lead``, ``owner``...)."""
    return ALIASES.get(actor, actor)


def make_authz(*, lead: str = LEAD, deputy: str | None = DEPUTY,
          ci_actors: Iterable[str] = (CI_ACTOR,),
          section_owner: Mapping[str, str] | None = None) -> Authz:
    return Authz(lead, deputy, frozenset(ci_actors),
                 dict(SECTION_OWNER if section_owner is None else section_owner))


def label_event(label: str, action: str, actor: str, when: datetime | None = None, *,
                index: int = 0) -> LabelEvent:
    return LabelEvent(label, action, who(actor), at(index) if when is None else when)


def marker(kind: str, author: str, *, when: datetime | None = None,
           updated: datetime | None = None, data: Mapping[str, object] | None = None,
           ci_verified: bool = False) -> Marker:
    created = at(CLAIM_AT) if when is None else when
    return Marker(kind, who(author), created, created if updated is None else updated,
                  dict(data or {}), ci_verified)


def claim_marker(author: str = OWNER, *, when: datetime | None = None,
                 claim_sha: str | None = CLAIM_SHA, sections: object = SECTIONS,
                 spec_hash: str = SPEC_HASH, login: object = None) -> Marker:
    """Marcador ``claim`` de 04 §1.4; ``login`` por defecto = autor."""
    author = who(author)
    data: dict[str, object] = {
        "task": TASK,
        "login": author if login is None else login,
        "claim_sha": claim_sha,
        "base_sha": BASE_SHA,
        "sections": list(sections) if isinstance(sections, tuple) else sections,
        "spec_hash": spec_hash,
        "claimed_at": (at(CLAIM_AT) if when is None else when).isoformat(),
        "renewed_at": None,
    }
    return marker("claim", author, when=when, data=data)


def release_marker(author: str = OWNER, *, when: datetime | None = None) -> Marker:
    return marker("release", author, when=at(CLAIM_AT + 5) if when is None else when,
                  data={"task": TASK})


def expiry_marker(author: str = CI_ACTOR, *, when: datetime | None = None,
                  ci_verified: bool = True) -> Marker:
    return marker("expiry", author, when=when, ci_verified=ci_verified,
                  data={"task": TASK, "run_id": 1, "head_sha": HEAD_SHA})


def submission_marker(author: str = OWNER, *, when: datetime | None = None,
                      updated: datetime | None = None, rounds: object = None) -> Marker:
    """Marcador ``submission``; ``rounds`` por defecto: una ronda en ``when``."""
    created = at(PR_AT) if when is None else when
    data: dict[str, object] = {"task": TASK}
    data["rounds"] = [{"submitted_at": created.isoformat()}] if rounds is None else rounds
    return marker("submission", author, when=created, updated=updated, data=data)


def task_ref(owner: str | None = OWNER, *, claim_commit_sha: str | None = CLAIM_SHA,
             claim_commit_author: str | None = "", created_by: str | None = "",
             last_push_at: datetime | None = None, head_sha: str = HEAD_SHA) -> TaskRef:
    """Ref ``task/<n>``; ``""`` en un autor significa «el dueño»."""
    owner = who(owner) if owner else None
    return TaskRef(
        head_sha,
        claim_commit_sha,
        owner if claim_commit_author == "" else claim_commit_author,
        owner if created_by == "" else created_by,
        at(CLAIM_AT) if last_push_at is None else last_push_at,
    )


def review(state: str, author: str = LEAD, when: datetime | None = None, *,
           index: int = 0) -> Review:
    return Review(who(author), state, at(REVIEW_AT + index) if when is None else when)


def _reviews(items: Iterable[object]) -> tuple[Review, ...]:
    out: list[Review] = []
    for i, r in enumerate(items):
        if isinstance(r, Review):
            out.append(r)
        elif isinstance(r, str):
            out.append(review(r, index=i))
        else:
            args = tuple(r)  # type: ignore[call-overload]
            out.append(review(args[1], args[0], args[2] if len(args) > 2 else None, index=i))
    return tuple(out)


_PULL_KINDS: Mapping[str, dict[str, object]] = {
    "ready": {"state": "open", "merged": False, "draft": False},
    "draft": {"state": "open", "merged": False, "draft": True},
    "merged": {"state": "closed", "merged": True, "draft": False},
    "closed": {"state": "closed", "merged": False, "draft": False},
    "fork": {"state": "open", "merged": False, "draft": False, "same_repo": False},
    "fork_merged": {"state": "closed", "merged": True, "draft": False, "same_repo": False},
    "wrong_base": {"state": "open", "merged": False, "draft": False, "base_ref": "develop"},
}


def pull(kind: str = "ready", *, number: int = PR_NUMBER, author: str = OWNER,
         base_ref: str = BASE, created_at: datetime | None = None,
         reviews: Iterable[object] = (), markers: Sequence[Marker] | None = None,
         via_core: bool = True) -> Pull:
    """PR de ``task/<n>``.

    ``kind``: ready, draft, merged, closed, fork, fork_merged o wrong_base.
    """
    spec: dict[str, Any] = {"same_repo": True, "base_ref": base_ref, **_PULL_KINDS[kind]}
    created = at(PR_AT) if created_at is None else created_at
    if markers is None:
        markers = (submission_marker(author, when=created),) if via_core else ()
    return Pull(number, spec["state"], spec["merged"], spec["draft"], who(author),
                spec["base_ref"], spec["same_repo"], created, _reviews(reviews), tuple(markers))


def _label_events(items: Iterable[object]) -> tuple[LabelEvent, ...]:
    out: list[LabelEvent] = []
    for i, e in enumerate(items):
        if isinstance(e, LabelEvent):
            out.append(e)
        else:
            args = tuple(e)  # type: ignore[call-overload]
            out.append(label_event(args[0], args[1], args[2],
                                   args[3] if len(args) > 3 else None, index=i))
    return tuple(out)


def snap(*, number: int = TASK, issue_author: str = OWNER, issue: str = "open",
         state_reason: str | None = None, labels: Iterable[str] = ("task",),
         label_events: Iterable[object] = (), assignees: Iterable[str] | None = None,
         sections: Iterable[str] = SECTIONS, spec_hash: str = SPEC_HASH,
         markers: Iterable[Marker] = (), ref: bool | TaskRef | None = False,
         owner: str = OWNER, claim: bool | None = None, pr: str | Pull | None = None,
         reviews: Iterable[object] = (), pulls: Iterable[Pull] = (), base_branch: str = BASE,
         authz: Authz | None = None, ci_expiry: bool | None = None) -> TaskSnapshot:
    """Instantánea coherente por defecto (ver el docstring del módulo).

    - ``label_events``: ``LabelEvent`` o tuplas ``(label, action, actor[, at])``;
      sin fecha, el evento i-ésimo ocurre en ``at(i)``.
    - ``ref=True``: ref con creador y autor del commit de reclamo = ``owner``; con
      ``claim`` en ``None`` agrega además su marcador ``claim`` válido.
    - ``assignees``: por defecto ``(owner,)`` si hay ref y ``()`` si no.
    - ``pr``: tipo de ``pull()`` (con ``reviews``) o un ``Pull``; ``pulls``: otros PR.
    - ``ci_expiry``: con ``None``, cada ``labeled`` de ``state:expired`` hecho por
      ``CI_ACTOR`` recibe su marcador ``expiry`` verificado a la misma hora.
    """
    owner = who(owner)
    events = _label_events(label_events)
    the_ref = task_ref(owner) if ref is True else (ref or None)
    issue_markers = list(markers)
    if claim or (claim is None and ref is True):
        issue_markers.append(claim_marker(owner))
    auto_ci = ci_expiry is None or ci_expiry
    if auto_ci:
        issue_markers += [expiry_marker(CI_ACTOR, when=e.at) for e in events
                          if e.label == "state:expired" and e.action == "labeled"
                          and e.actor == CI_ACTOR]
    issue_markers.sort(key=lambda m: m.created_at)
    all_pulls: list[Pull] = []
    if isinstance(pr, Pull):
        all_pulls.append(pr)
    elif pr is not None:
        all_pulls.append(pull(pr, author=owner, reviews=reviews))
    all_pulls += list(pulls)
    if assignees is None:
        assignees = (owner,) if the_ref is not None else ()
    return TaskSnapshot(
        number=number,
        issue_author=who(issue_author),
        issue_state=issue,
        state_reason=state_reason,
        labels=frozenset(labels),
        label_events=events,
        assignees=tuple(who(a) for a in assignees),
        sections=tuple(sections),
        spec_hash=spec_hash,
        issue_markers=tuple(issue_markers),
        ref=the_ref,
        pulls=tuple(all_pulls),
        base_branch=base_branch,
        authz=make_authz() if authz is None else authz,
    )


# --- Hypothesis --------------------------------------------------------------------

_ACTORS = (LEAD, DEPUTY, OWNER, OTHER, CI_ACTOR, "Carla", "ANA ")
_LABELS = ("task", "state:proposed", "state:expired", "flag:blocked", "flag:overlap")
_MARKER_KINDS = ("claim", "release", "expiry", "escalation", "submission", "assessment")
_REVIEW_STATES = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED", "COMMENTED", "PENDING")
_SHAS = (CLAIM_SHA, OLD_CLAIM_SHA, None)


def _times() -> st.SearchStrategy[datetime]:
    return st.integers(min_value=0, max_value=90).map(at)


def _round_values() -> st.SearchStrategy[object]:
    iso = _times().map(lambda t: t.isoformat())
    naive = _times().map(lambda t: t.replace(tzinfo=None).isoformat())
    return st.one_of(iso, naive, st.just("not-a-date"), st.none(), st.integers(0, 3))


def _marker_data() -> st.SearchStrategy[dict[str, object]]:
    return st.fixed_dictionaries({}, optional={
        "login": st.one_of(st.sampled_from(_ACTORS), st.integers(0, 3), st.none()),
        "claim_sha": st.sampled_from(_SHAS),
        "sections": st.one_of(
            st.lists(st.sampled_from(("introduction", "methods", "results", 7)), max_size=3),
            st.just("methods"),
        ),
        "spec_hash": st.sampled_from((SPEC_HASH, OTHER_SPEC_HASH)),
        "rounds": st.one_of(
            st.lists(st.one_of(st.fixed_dictionaries({"submitted_at": _round_values()}),
                               st.just("round")), max_size=2),
            st.just({"submitted_at": "x"}),
        ),
    })


@st.composite
def _markers(draw: st.DrawFn) -> Marker:
    created = draw(_times())
    return Marker(
        kind=draw(st.sampled_from(_MARKER_KINDS)),
        author=draw(st.sampled_from(_ACTORS)),
        created_at=created,
        updated_at=created + timedelta(minutes=draw(st.integers(0, 30))),
        data=draw(_marker_data()),
        ci_verified=draw(st.booleans()),
    )


def _reviews_st() -> st.SearchStrategy[Review]:
    return st.builds(Review, author=st.sampled_from(_ACTORS),
                     state=st.sampled_from(_REVIEW_STATES),
                     submitted_at=st.one_of(st.none(), _times()))


def _pulls_st() -> st.SearchStrategy[Pull]:
    return st.builds(
        Pull,
        number=st.integers(1, 5),
        state=st.sampled_from(("open", "closed")),
        merged=st.booleans(),
        draft=st.booleans(),
        author=st.sampled_from(_ACTORS),
        base_ref=st.sampled_from((BASE, "develop")),
        same_repo=st.booleans(),
        created_at=_times(),
        reviews=st.lists(_reviews_st(), max_size=4).map(tuple),
        markers=st.lists(_markers(), max_size=3).map(tuple),
    )


def _refs_st() -> st.SearchStrategy[TaskRef | None]:
    actor = st.one_of(st.none(), st.sampled_from(_ACTORS))
    return st.one_of(st.none(), st.builds(
        TaskRef,
        head_sha=st.just(HEAD_SHA),
        claim_commit_sha=st.sampled_from(_SHAS),
        claim_commit_author=actor,
        created_by=actor,
        last_push_at=st.one_of(st.none(), _times()),
    ))


def _authz_st() -> st.SearchStrategy[Authz]:
    return st.builds(
        make_authz,
        deputy=st.sampled_from((DEPUTY, None)),
        section_owner=st.sampled_from((dict(SECTION_OWNER), {"methods": "Carla"}, {})),
    )


def snapshots() -> st.SearchStrategy[TaskSnapshot]:
    """Estrategia de ``TaskSnapshot`` que recorre todas las ramas de 04 §1.5.

    Genera también datos de marcador mal formados (``login`` no cadena, ``sections``
    como cadena, ``rounds`` con fechas inválidas o sin zona) para probar que
    ``derive_state`` es total.
    """
    label_events = st.builds(
        LabelEvent,
        label=st.sampled_from(_LABELS),
        action=st.sampled_from(("labeled", "unlabeled")),
        actor=st.sampled_from(_ACTORS),
        at=_times(),
    )
    return st.builds(
        TaskSnapshot,
        number=st.just(TASK),
        issue_author=st.sampled_from(_ACTORS),
        issue_state=st.sampled_from(("open", "closed")),
        state_reason=st.sampled_from((None, "completed", "not_planned", "duplicate", "reopened")),
        labels=st.frozensets(st.sampled_from(_LABELS)),
        label_events=st.lists(label_events, max_size=6).map(tuple),
        assignees=st.lists(st.sampled_from(_ACTORS), max_size=3).map(tuple),
        sections=st.lists(st.sampled_from(("introduction", "methods", "results")),
                          max_size=3, unique=True).map(tuple),
        spec_hash=st.sampled_from((SPEC_HASH, OTHER_SPEC_HASH)),
        issue_markers=st.lists(_markers(), max_size=6).map(tuple),
        ref=_refs_st(),
        pulls=st.lists(_pulls_st(), max_size=3).map(tuple),
        base_branch=st.just(BASE),
        authz=_authz_st(),
    )


# --- Aislamiento de E/S (REQ-MVP-24) --------------------------------------------------

class IOBlocked(RuntimeError):
    """Una prueba de dominio intentó abrir un archivo o un socket."""


_IO_TARGETS: tuple[tuple[object, str], ...] = (
    (builtins, "open"), (io, "open"), (os, "open"),
    (socket, "socket"), (socket, "create_connection"),
    (socket, "getaddrinfo"), (socket, "socketpair"),
)


def package_dirs() -> tuple[str, ...]:
    """Directorios del paquete bajo prueba (``src/<nombre>``), normalizados."""
    pkg = sys.modules[Authz.__module__.split(".")[0]]
    return tuple(os.path.normcase(os.path.abspath(p)) for p in pkg.__path__)


def _called_from(prefixes: tuple[str, ...]) -> bool:
    frame: FrameType | None = sys._getframe(1)
    while frame is not None:
        name = os.path.normcase(os.path.abspath(frame.f_code.co_filename))
        if any(name.startswith(p + os.sep) for p in prefixes):
            return True
        frame = frame.f_back
    return False


def block_io(monkeypatch: pytest.MonkeyPatch, *, only_from: Sequence[str] | None = None) -> None:
    """Bloquea ``open`` y ``socket`` con monkeypatch mientras dure la prueba.

    Parchea ``builtins.open``, ``io.open``, ``os.open``, ``socket.socket``,
    ``socket.create_connection``, ``socket.getaddrinfo`` y ``socket.socketpair``.

    Con ``only_from=None`` toda llamada lanza ``IOBlocked``. Con una lista de
    directorios, lanza solo si algún marco de la pila es código de esos
    directorios, y si no delega en el original: es el modo de las pruebas de
    Hypothesis, que importa módulos de forma perezosa mientras genera datos (y el
    hook de reescritura de pytest lee sus ``.pyc`` con ``open``).
    """
    prefixes = None if only_from is None else tuple(
        os.path.normcase(os.path.abspath(p)) for p in only_from)

    def guard(original: Callable[..., Any]) -> Callable[..., Any]:
        def blocked(*args: object, **kwargs: object) -> Any:
            if prefixes is None or _called_from(prefixes):
                raise IOBlocked("I/O is blocked in domain unit tests")
            return original(*args, **kwargs)
        return blocked

    for module, name in _IO_TARGETS:
        monkeypatch.setattr(module, name, guard(getattr(module, name)))


def io_guard(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cuerpo del fixture autouse de T1: estricto, salvo en pruebas de Hypothesis."""
    if getattr(request.function, "is_hypothesis_test", False):
        block_io(monkeypatch, only_from=package_dirs())
    else:
        block_io(monkeypatch)
