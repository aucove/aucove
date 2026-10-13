# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``PullRequest`` dice si la cabeza es del mismo repositorio.

04-tareas §1.1 (``Pull.same_repo``) y REQ-TASK-5 piden ignorar y reportar
(``fork_pr_ignored``) los PR desde forks, y la invariante de 12-mvp §3.2 compara
``merged`` con ``any(p.merged and p.same_repo ...)``. La interfaz de la forja
(08-forja §2) no tenía ese dato, así que ningún adaptador podía decir que un PR con
cabeza ``task/<n>`` venía de un fork: la instantánea lo contaba como PR de la tarea y
``derive_state`` daba ``in_review`` o ``merged`` por un PR ajeno.

La prueba de punta a punta envuelve al falso con un adaptador que presenta como fork los
PR elegidos, con
``dataclasses.replace(pr, same_repo=False)``.
"""

from __future__ import annotations

import dataclasses
import json
import typing
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from aucove import _meta
from aucove.domain.taskbody import parse_task_body, render_task_body
from aucove.domain.tasks import Authz, derive_state
from aucove.forge.base import NULL_SHA, LabelEvent, PullRequest, RefActivity, RepoRef
from aucove.forge.fake import FakeClock, FakeForge
from aucove.ops import snapshot as snap

T0 = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
AUTHZ = Authz(
    lead="ana",
    deputy="bruno",
    ci_actors=frozenset({"github-actions[bot]"}),
    section_owner={"introduction": "ana", "methods": "carla", "results": "bruno"},
)
TASK = 17
OWN_PR = 50
FORK_PR = 60


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _positional_pull(**over: object) -> PullRequest:
    # Los 16 campos de 08 §2 en su orden, como los construye el falso (fake.pull_of).
    values: dict[str, object] = {
        "number": FORK_PR, "node_id": "PR_x", "title": "t", "body": "b", "state": "open",
        "draft": False, "merged": False, "merged_at": None, "head_branch": f"task/{TASK}",
        "head_sha": "b" * 40, "base_branch": "main", "author": "atacante", "labels": (),
        "requested_reviewers": (), "updated_at": at(0), "url": "https://example.invalid/pr/60",
    }
    values.update(over)
    return PullRequest(*values.values())


# -- la interfaz -----------------------------------------------------------------------


def test_pull_request_exposes_same_repo_as_a_bool() -> None:
    names = [f.name for f in dataclasses.fields(PullRequest)]
    assert "same_repo" in names
    assert typing.get_type_hints(PullRequest)["same_repo"] is bool


def test_a_fork_pull_request_can_be_represented() -> None:
    fork = dataclasses.replace(_positional_pull(), same_repo=False)
    assert fork.same_repo is False
    assert fork != _positional_pull()  # el dato entra en la igualdad
    assert dataclasses.replace(fork, same_repo=True) == _positional_pull()


def test_positional_construction_of_the_spec_fields_still_works() -> None:
    # Los adaptadores que aún no calculan el dato (el falso) siguen construyendo el PR
    # con los 16 campos de 08 §2; la cabeza se toma como del mismo repositorio.
    assert _positional_pull().same_repo is True


# -- de punta a punta: la instantánea y derive_state -------------------------------------


class _ForkView:
    """Delega en el falso y presenta como fork (``same_repo=False``) los PR de ``forks``."""

    def __init__(self, inner: FakeForge, forks: Iterable[int]) -> None:
        self._inner = inner
        self._forks = frozenset(forks)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _mark(self, pr: PullRequest) -> PullRequest:
        return dataclasses.replace(pr, same_repo=False) if pr.number in self._forks else pr

    def list_pull_requests(self, **kw: Any) -> list[PullRequest]:
        return [self._mark(p) for p in self._inner.list_pull_requests(**kw)]

    def get_pull_request(self, number: int) -> PullRequest | None:
        pr = self._inner.get_pull_request(number)
        return None if pr is None else self._mark(pr)


def _marker(kind: str, data: dict[str, Any]) -> str:
    return f"<!-- {_meta.NAME}:{kind} v1\n{json.dumps(data)}\n-->"


def _claimed_task(*, own_pr: bool, fork_merged: bool) -> FakeForge:
    """Tarea 17 aceptada por el lead y reclamada por carla, más un PR de ``atacante`` con cabeza ``task/17``."""
    forge = FakeForge(RepoRef("example-org", "demo"), viewer="ana", clock=FakeClock(at(600)))
    base = forge.seed_commit({"README.md": b"demo\n"}, [], "base", "ana", date=at(0))
    forge.seed_branch("main", base)
    body = render_task_body(
        {"summary": "Datos.", "sections": ["methods"], "plan_refs": [], "done": ["Compila."]}, "es"
    )
    spec_hash = str(parse_task_body(body)["spec_hash"])
    events = [
        LabelEvent(1, "labeled", "task", "carla", at(10)),
        LabelEvent(2, "labeled", "state:proposed", "carla", at(10)),
        LabelEvent(3, "unlabeled", "state:proposed", "ana", at(20)),
    ]
    forge.seed_issue(
        number=TASK, title="Tarea", body=body, labels=["task"], assignees=["carla"], author="carla",
        created_at=at(10), events=events,
    )
    claim = forge.seed_commit({"README.md": b"demo\n"}, [base], f"{_meta.CLAIM_SUBJECT}{TASK}", "carla", date=at(30))
    head = forge.seed_commit(
        {"README.md": b"demo\n", "sections/methods/methods.tex": b"x\n"}, [claim], "trabajo", "carla",
        date=at(40),
    )
    forge.seed_branch(f"task/{TASK}", head)
    forge.seed_ref_activity(
        f"task/{TASK}",
        [
            RefActivity("branch_creation", "carla", at(30), NULL_SHA, claim),
            RefActivity("push", "carla", at(40), claim, head),
        ],
    )
    forge.seed_comment(TASK, author="carla", created_at=at(31), body=_marker("claim", {
        "task": TASK, "login": "carla", "claim_sha": claim, "base_sha": base, "sections": ["methods"],
        "spec_hash": spec_hash, "claimed_at": "2026-09-28T09:30:00Z", "renewed_at": None,
    }))
    if own_pr:
        forge.seed_pull_request(
            files=[], number=OWN_PR, head_branch=f"task/{TASK}", head_sha=head, author="carla", created_at=at(50)
        )
    # El falso no siembra forks: el PR se siembra con la misma cabeza y _ForkView lo marca.
    forge.seed_pull_request(
        files=[], number=FORK_PR, head_branch=f"task/{TASK}", head_sha=head, author="atacante",
        created_at=at(55), merged=fork_merged,
    )
    return forge


def _derive(forge: FakeForge) -> tuple[snap.TaskEvidence, Any]:
    ev = snap.collect_task(_ForkView(forge, {FORK_PR}), TASK, AUTHZ, base_branch="main")  # type: ignore[arg-type]
    assert ev is not None
    return ev, derive_state(ev.snapshot)


@pytest.mark.parametrize("fork_merged", [False, True], ids=["fork-open", "fork-merged"])
def test_fork_pull_request_with_the_task_branch_is_ignored(fork_merged: bool) -> None:
    ev, derived = _derive(_claimed_task(own_pr=False, fork_merged=fork_merged))
    pulls = {p.number: p for p in ev.snapshot.pulls}
    assert pulls[FORK_PR].same_repo is False
    # Sin el dato, el PR del fork contaba como de la tarea: in_review (abierto) o merged.
    assert derived.state == "claimed"
    assert derived.pr is None
    assert "fork_pr_ignored" in derived.anomalies


def test_own_pull_request_still_counts_next_to_a_fork() -> None:
    ev, derived = _derive(_claimed_task(own_pr=True, fork_merged=True))
    pulls = {p.number: p for p in ev.snapshot.pulls}
    assert (pulls[OWN_PR].same_repo, pulls[FORK_PR].same_repo) == (True, False)
    # 12-mvp §3.2: merged sii hay un PR integrado del mismo repo; el del fork no cuenta.
    assert derived.state == "in_review"
    assert derived.pr == OWN_PR
    assert "fork_pr_ignored" in derived.anomalies
