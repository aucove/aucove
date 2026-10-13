# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""CT-ACT-01: actividad de refs (08-forja §4.2, §10.3; 04-tareas H-TASK-2; 09-ci §4.2).

Es la base de ``E_CLAIM_NOT_HELD`` en la regla ``scope``: los pushes a
``task/<n>`` se listan «paginado desde la creación» y cada ``actor`` se compara
con el reclamante.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aucove.domain.text import same_login
from aucove.forge.base import NULL_SHA, ForgeValidationError, RefActivity
from aucove.forge.fake import Fault, FakeForge
from tests.contract.conftest import LEAD, RESEARCHER_A, Sandbox


def test_ct_act_01_rest_writes_register_creation_and_push(sandbox: Sandbox) -> None:
    """CT-ACT-01: crear y mover una ref por REST registra branch_creation y push con el actor autenticado."""
    branch = sandbox.branch("act-01")
    c1 = sandbox.claim_commit()
    sandbox.forge.create_ref(branch, c1)
    c2 = sandbox.claim_commit(parent=c1, forge=sandbox.other)
    sandbox.other.update_ref(branch, c2)  # otra cuenta empuja

    activity = sandbox.eventually(lambda: sandbox.forge.list_ref_activity(branch), lambda a: len(a or []) >= 2)
    assert activity is not None
    kinds = [(a.activity_type, a.before, a.after) for a in activity]
    assert kinds == [("branch_creation", NULL_SHA, c1), ("push", c1, c2)]
    assert same_login(activity[0].actor, sandbox.lead_login)
    assert same_login(activity[1].actor, sandbox.researcher_a_login)
    assert activity[0].timestamp <= activity[1].timestamp
    assert sandbox.forge.delete_ref(branch) is True


def test_activity_of_unknown_branch_is_empty(sandbox: Sandbox) -> None:
    assert sandbox.forge.list_ref_activity(sandbox.branch("act-none")) == []


# --- Solo con el falso -------------------------------------------------------------


def _event(fake: FakeForge, kind: str, actor: str | None, before: str, after: str, minutes: int) -> RefActivity:
    when = fake.clock.now() + timedelta(minutes=minutes)
    return RefActivity(kind, actor, when, before, after)  # type: ignore[arg-type]


def test_fake_activity_pages_from_the_last_creation(fake: FakeForge) -> None:
    """Pagina desde la creación: la actividad de una encarnación anterior de la rama no aparece."""
    a, b = "a" * 40, "b" * 40
    old = [
        _event(fake, "branch_creation", "mallory", NULL_SHA, a, 1),
        _event(fake, "push", "mallory", a, b, 2),
        _event(fake, "branch_deletion", "mallory", b, NULL_SHA, 3),
    ]
    new = [_event(fake, "branch_creation", "carla", NULL_SHA, a, 10)]
    new += [_event(fake, "push", "carla", a, b, 11 + i) for i in range(150)]
    # Se siembran desordenados: el falso los lista por fecha.
    fake.seed_ref_activity("task/7", new[75:])
    fake.seed_ref_activity("task/7", old + new[:75])
    fake.calls.clear()

    listed = fake.list_ref_activity("task/7")
    assert listed == new
    assert all(ev.actor == "carla" for ev in listed)
    # 151 eventos: 2 páginas de 100 (REQ-FORGE-8).
    assert fake.calls.count("L", "list_ref_activity") == 2


def test_fake_activity_over_ten_pages_is_an_error(fake: FakeForge) -> None:
    a = "a" * 40
    events = [_event(fake, "branch_creation", "carla", NULL_SHA, a, 0)]
    events += [_event(fake, "push", "carla", a, a, 1 + i) for i in range(1000)]
    fake.seed_ref_activity("task/8", events)
    with pytest.raises(ForgeValidationError):
        fake.list_ref_activity("task/8")


def test_fake_activity_404_means_not_available(fake: FakeForge) -> None:
    fake.faults.add(Fault("list_ref_activity", "http_404"))
    assert fake.list_ref_activity(fake.default_branch) is None


def test_fake_as_user_actor_and_deletion(fake: FakeForge) -> None:
    base = fake.get_ref(fake.default_branch)
    assert base is not None
    bruno = fake.as_user(RESEARCHER_A)
    c1 = bruno.create_empty_commit(base.sha, "claim")
    bruno.create_ref("task/3", c1)
    fake.clock.advance(60)
    c2 = fake.create_empty_commit(c1, "work")
    fake.update_ref("task/3", c2)
    fake.clock.advance(60)
    assert fake.delete_ref("task/3") is True
    activity = fake.list_ref_activity("task/3")
    assert activity is not None
    assert [(a.activity_type, a.actor) for a in activity] == [
        ("branch_creation", RESEARCHER_A),
        ("push", LEAD),
        ("branch_deletion", LEAD),
    ]
    assert activity[2].before == c2 and activity[2].after == NULL_SHA
    assert activity[0].timestamp < activity[1].timestamp < activity[2].timestamp


def test_fake_seed_branch_registers_no_activity(fake: FakeForge) -> None:
    fake.seed_branch("task/5", "c" * 40)
    assert fake.list_ref_activity("task/5") == []
