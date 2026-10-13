# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""El falso modela los PR desde forks (04-tareas §1.1, REQ-TASK-5).

``PullRequest.same_repo`` (08 §2, base.py) dice si la cabeza del PR es una rama de este
repositorio. El falso no lo sembraba (``seed_pull_request`` rechazaba el campo),
``pull_of`` lo dejaba siempre en ``True`` y ``find_pull_request`` devolvía PR de forks,
aunque ``GET pulls?head={o}:{b}`` los deja fuera. La prueba principal está en
``tests/contract/test_pulls.py`` (``test_fake_fork_pull_requests``); aquí van las
consecuencias en el resto del falso: la rama ``task/<n>`` de un fork vive en el fork,
así que un push o un borrado de la rama homónima de este repositorio no toca ese PR.
"""

from __future__ import annotations

import json

import pytest

from aucove.forge.base import ForgeValidationError
from aucove.forge.fake import FakeForge
from tests.contract.conftest import BASE_FILES, RESEARCHER_B, new_fake

BRANCH = "task/17"
METHODS = "sections/methods/methods.tex"


def _main(fake: FakeForge) -> str:
    ref = fake.get_ref(fake.default_branch)
    assert ref is not None
    return ref.sha


def _commit(fake: FakeForge, parent: str, text: bytes) -> str:
    return fake.seed_commit({**BASE_FILES, METHODS: text}, parents=[parent], message=text.decode().strip())


def _own_and_fork(fake: FakeForge) -> tuple[int, int, str, str]:
    base = _main(fake)
    own_head = _commit(fake, base, b"own\n")
    fake.seed_branch(BRANCH, own_head)
    own = fake.seed_pull_request(head_branch=BRANCH, author=RESEARCHER_B)
    fork_head = _commit(fake, base, b"fork\n")
    fork = fake.seed_pull_request(head_branch=BRANCH, head_sha=fork_head, author="atacante", same_repo=False)
    return own.number, fork.number, own_head, fork_head


def test_same_repo_defaults_to_true_and_is_stored() -> None:
    fake = new_fake()
    fake.seed_branch(BRANCH, _commit(fake, _main(fake), b"own\n"))
    pr = fake.seed_pull_request(head_branch=BRANCH)
    assert pr.same_repo is True
    saved = next(p for p in fake.to_state()["pulls"] if p["number"] == pr.number)
    assert saved["same_repo"] is True


def test_only_a_fork_pull_request_is_not_found_by_branch() -> None:
    fake = new_fake()
    fork_head = _commit(fake, _main(fake), b"fork\n")
    fork = fake.seed_pull_request(head_branch=BRANCH, head_sha=fork_head, same_repo=False)
    assert fake.find_pull_request(BRANCH) is None
    assert [p.number for p in fake.list_pull_requests()] == [fork.number]


def test_fork_pull_request_needs_its_own_head_sha() -> None:
    # La rama task/17 de este repositorio no es la cabeza del fork: sin head_sha no hay de dónde sacarla.
    fake = new_fake()
    fake.seed_branch(BRANCH, _commit(fake, _main(fake), b"own\n"))
    with pytest.raises(ValueError, match="fork"):
        fake.seed_pull_request(head_branch=BRANCH, same_repo=False)


def test_same_repo_must_be_a_bool() -> None:
    fake = new_fake()
    fake.seed_branch(BRANCH, _commit(fake, _main(fake), b"own\n"))
    with pytest.raises(TypeError, match="same_repo must be a bool"):
        fake.seed_pull_request(head_branch=BRANCH, same_repo="no")


def test_push_to_the_homonymous_branch_does_not_move_the_fork_head() -> None:
    fake = new_fake()
    own, fork, own_head, fork_head = _own_and_fork(fake)
    pushed = _commit(fake, own_head, b"own 2\n")
    fake.update_ref(BRANCH, pushed)
    moved = fake.get_pull_request(own)
    kept = fake.get_pull_request(fork)
    assert moved is not None and moved.head_sha == pushed
    assert kept is not None and kept.head_sha == fork_head


def test_deleting_the_homonymous_branch_does_not_close_the_fork_pull_request() -> None:
    fake = new_fake()
    own, fork, _, _ = _own_and_fork(fake)
    assert fake.delete_ref(BRANCH) is True
    closed = fake.get_pull_request(own)
    still_open = fake.get_pull_request(fork)
    assert closed is not None and closed.state == "closed"
    assert still_open is not None and still_open.state == "open"


def test_reopening_a_fork_pull_request_does_not_look_at_this_repo_branches() -> None:
    # La rama cabeza de un fork vive en el fork (el falso no lo modela): reabrir no depende de
    # que exista una rama homónima aquí. Un PR propio sin rama cabeza sigue sin poder reabrirse.
    fake = new_fake()
    own, fork, _, _ = _own_and_fork(fake)
    fake.update_issue(fork, state="closed")
    assert fake.delete_ref(BRANCH) is True
    reopened = fake.update_issue(fork, state="open")
    assert reopened.state == "open"
    pr = fake.get_pull_request(fork)
    assert pr is not None and pr.state == "open" and pr.same_repo is False
    with pytest.raises(ForgeValidationError):
        fake.update_issue(own, state="open")


def test_state_without_same_repo_restores_as_same_repo() -> None:
    # Un estado guardado antes de este cambio no trae same_repo: el PR es del mismo repositorio.
    fake = new_fake()
    own, fork, _, _ = _own_and_fork(fake)
    state = json.loads(json.dumps(fake.to_state()))
    for pull in state["pulls"]:
        del pull["same_repo"]
    restored = FakeForge.from_state(state)
    assert {p.number: p.same_repo for p in restored.list_pull_requests()} == {own: True, fork: True}
