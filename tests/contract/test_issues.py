# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Issues, etiquetas, asignados y comentarios (08-forja §4.2, §4.7, §10.3; 12-mvp §3.3).

Casos mínimos de 12 §3.3: asignar y desasignar, etiquetas idempotentes, cerrar
como ``not_planned`` y comentario con marcador oculto que se actualiza en vez de
duplicarse. CT-ISS-01, CT-ISS-02 y CT-ISS-03 (recuperación por el marcador
``op``) pasan en H2. Las pruebas compartidas usan los logins de
``sandbox.accounts``, no los del falso.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Iterator

import pytest

from aucove import _meta
from aucove.domain.text import same_login
from aucove.forge.base import (
    Comment,
    ForgeAdapter,
    ForgeNotFound,
    ForgePermissionError,
    ForgeValidationError,
    Issue,
    Label,
    LabelEvent,
)
from aucove.forge.fake import Fault, FakeForge
from tests.contract.conftest import READ_ONLY, RESEARCHER_A, RESEARCHER_B, Accounts, LiveEnv, Sandbox, new_fake

_OPS = itertools.count(1)


def _op_id(sandbox: Sandbox) -> str:
    return f"ct-{sandbox.run_id}-{next(_OPS)}"


@pytest.fixture
def issue(sandbox: Sandbox) -> Iterator[Issue]:
    """Issue de prueba; al terminar se cierra como not_planned (08 §10.2)."""
    created = sandbox.forge.create_issue("ct issue", "body", op_id=_op_id(sandbox))
    yield created
    sandbox.forge.update_issue(created.number, state="closed", state_reason="not_planned")


def _labels(forge: ForgeAdapter, number: int) -> set[str]:
    found = forge.get_issue(number)
    assert found is not None
    return {n.casefold() for n in found.labels}


def _folded(logins: Iterable[str]) -> list[str]:
    return sorted(login.casefold() for login in logins)


def test_ct_iss_01_add_assignees_twice_lists_both(sandbox: Sandbox, issue: Issue) -> None:
    """CT-ISS-01: add_assignees dos veces con usuarios distintos no es un candado: quedan los dos."""
    a, b = sandbox.researcher_a_login, sandbox.second_assignee_login
    first = sandbox.forge.add_assignees(issue.number, [a])
    assert _folded(first.assignees) == _folded([a])
    second = sandbox.other.add_assignees(issue.number, [b])
    assert _folded(second.assignees) == _folded([a, b])


def test_ct_iss_02_list_issues_has_no_pull_requests(sandbox: Sandbox, issue: Issue) -> None:
    """CT-ISS-02: list_issues no devuelve PR (REQ-FORGE-9)."""
    head = sandbox.branch("iss-02")
    sandbox.forge.create_ref(head, sandbox.claim_commit())
    pr = sandbox.make_pull_request(head_branch=head)
    numbers = sandbox.eventually(lambda: {i.number for i in sandbox.forge.list_issues(state="all")},
                                 lambda found: issue.number in found)
    assert issue.number in numbers
    assert pr.number not in numbers
    with pytest.raises(ForgeValidationError):
        sandbox.forge.get_issue(pr.number)
    assert sandbox.forge.delete_ref(head) is True


def test_ct_iss_02_remove_absent_label_is_not_an_error(sandbox: Sandbox, issue: Issue) -> None:
    """CT-ISS-02: remove_label de una etiqueta ausente no falla y devuelve las actuales."""
    sandbox.forge.add_labels(issue.number, ["ct-keep"])
    current = sandbox.forge.remove_label(issue.number, "ct-absent")
    assert {n.casefold() for n in current} == {"ct-keep"}
    assert _labels(sandbox.forge, issue.number) == {"ct-keep"}


def test_labels_are_idempotent(sandbox: Sandbox, issue: Issue) -> None:
    """Agregar y quitar etiquetas es idempotente en el estado (05 §6.1), no siempre en los eventos.

    Al volver a agregar una etiqueta presente, GitHub a veces registra otro ``labeled`` y a
    veces no; el falso lo registra siempre. Quitar una ausente no registra nada (00-marco D-68).
    """
    once = sandbox.forge.add_labels(issue.number, ["ct-task", "ct-claimed"])
    twice = sandbox.forge.add_labels(issue.number, ["ct-task", "ct-claimed"])
    assert sorted(n.casefold() for n in once) == sorted(n.casefold() for n in twice) == ["ct-claimed", "ct-task"]
    removed = sandbox.forge.remove_label(issue.number, "ct-claimed")
    again = sandbox.forge.remove_label(issue.number, "ct-claimed")
    assert [n.casefold() for n in removed] == [n.casefold() for n in again] == ["ct-task"]

    def claimed(events: list[LabelEvent]) -> list[tuple[str, str]]:
        return [(e.event, e.label.casefold()) for e in events if e.label.casefold() == "ct-claimed"]

    events = sandbox.eventually(lambda: sandbox.forge.list_issue_events(issue.number),
                                lambda e: claimed(e)[-1:] == [("unlabeled", "ct-claimed")])
    labeled, unlabeled = ("labeled", "ct-claimed"), ("unlabeled", "ct-claimed")
    assert claimed(events) in ([labeled, unlabeled], [labeled, labeled, unlabeled])
    assert all(same_login(e.actor, sandbox.lead_login) for e in events)
    assert [e.created_at for e in events] == sorted(e.created_at for e in events)


def test_assign_and_unassign(sandbox: Sandbox, issue: Issue) -> None:
    """set_assignees reemplaza el conjunto; con una lista vacía desasigna (08 §4.2)."""
    a, b = sandbox.researcher_a_login, sandbox.second_assignee_login
    assigned = sandbox.forge.set_assignees(issue.number, [a, b])
    assert _folded(assigned.assignees) == _folded([a, b])
    replaced = sandbox.forge.set_assignees(issue.number, [b.upper()])  # same_login (08 §6.3)
    assert _folded(replaced.assignees) == _folded([b])
    removed = sandbox.forge.remove_assignees(issue.number, [b])
    assert removed.assignees == ()
    assert sandbox.forge.set_assignees(issue.number, []).assignees == ()


def test_live_sandbox_with_two_accounts() -> None:
    """00-marco D-67: sin ``researcher-b``, la segunda persona es ``lead`` y la prueba de tres identidades se omite."""
    fake = new_fake()
    accounts = Accounts("lead-x", "researcher-x", "", "")
    live = LiveEnv("lab/ct-public", fake, fake, None, accounts, fake.default_branch, "s")
    two = Sandbox("github", fake, fake, fake.default_branch, "0" * 40, "r", None, accounts, live=live)
    assert not two.has_researcher_b and two.second_assignee_login == "lead-x"
    with pytest.raises(pytest.skip.Exception):
        two.require_researcher_b()
    full = Accounts("lead-x", "researcher-x", "researcher-y", "")
    three = Sandbox("github", fake, fake, fake.default_branch, "0" * 40, "r", None, full,
                    live=LiveEnv("lab/ct-public", fake, fake, fake, full, fake.default_branch, "s"))
    assert three.has_researcher_b and three.second_assignee_login == "researcher-y"
    three.require_researcher_b()  # no omite


def test_close_as_not_planned_and_reopen(sandbox: Sandbox, issue: Issue) -> None:
    closed = sandbox.forge.update_issue(issue.number, state="closed", state_reason="not_planned")
    assert closed.state == "closed" and closed.state_reason == "not_planned"
    assert closed.closed_at is not None
    fetched = sandbox.forge.get_issue(issue.number)
    assert fetched is not None and fetched.state_reason == "not_planned"
    assert issue.number not in {i.number for i in sandbox.forge.list_issues(state="open")}
    reopened = sandbox.forge.update_issue(issue.number, state="open")
    assert reopened.state == "open" and reopened.state_reason == "reopened" and reopened.closed_at is None


def _upsert_marker_comment(forge: ForgeAdapter, number: int, marker: str, body: str, op_id: str) -> Comment:
    # Patrón de 05 §6.1: comentario propio con el marcador, buscado por (marcador, autor).
    me = forge.identity().login
    mine = [c for c in forge.list_comments(number) if marker in c.body and same_login(c.author, me)]
    if mine:
        return forge.update_comment(mine[0].id, f"{marker}\n{body}")
    return forge.create_comment(number, f"{marker}\n{body}", op_id=op_id)


def test_marker_comment_is_updated_not_duplicated(sandbox: Sandbox, issue: Issue) -> None:
    """Comentario con marcador oculto que se actualiza en vez de duplicarse (12 §3.3)."""
    marker = _meta.ci_marker("build")
    first = _upsert_marker_comment(sandbox.forge, issue.number, marker, "run 1", _op_id(sandbox))
    # El mismo marcador de otra cuenta no cuenta (REQ-OPS-18).
    sandbox.other.create_comment(issue.number, f"{marker}\nforged", op_id=_op_id(sandbox))
    second = _upsert_marker_comment(sandbox.forge, issue.number, marker, "run 2", _op_id(sandbox))
    assert second.id == first.id
    assert "run 2" in second.body and "run 1" not in second.body
    assert second.updated_at >= first.updated_at
    comments = sandbox.forge.list_comments(issue.number)
    own = [c for c in comments if marker in c.body and same_login(c.author, sandbox.lead_login)]
    assert [c.id for c in own] == [first.id]
    assert len([c for c in comments if marker in c.body]) == 2


def test_create_issue_adds_op_marker_without_dedup(sandbox: Sandbox) -> None:
    """El adaptador agrega el marcador op al final del cuerpo (08 §4.7) y no deduplica en H2."""
    op_id = _op_id(sandbox)
    marker = f"<!-- {_meta.NAME}:op v1 id={op_id} -->"
    one = sandbox.forge.create_issue("ct dup", "text", op_id=op_id)
    two = sandbox.forge.create_issue("ct dup", "text", op_id=op_id)
    try:
        assert one.number != two.number
        assert one.body.endswith(marker) and one.body.startswith("text")
        assert same_login(one.author, sandbox.lead_login) and one.state == "open" and one.state_reason is None
    finally:
        for created in (one, two):
            sandbox.forge.update_issue(created.number, state="closed", state_reason="not_planned")


def test_get_missing_issue_is_none(sandbox: Sandbox) -> None:
    assert sandbox.forge.get_issue(999_999) is None


def test_ct_iss_03_create_issue_recovers_by_op_marker(sandbox: Sandbox) -> None:
    """CT-ISS-03: con timeout ambiguo, create_issue se recupera por el marcador op sin duplicar."""
    if sandbox.fake is None:
        pytest.skip("H3: inyección de fallas solo con el falso")
    sandbox.fake.faults.add(Fault("create_issue", "timeout_after", nth=1))
    op_id = _op_id(sandbox)
    created = sandbox.forge.create_issue("ct recover", "body", op_id=op_id)
    matching = [i for i in sandbox.forge.list_issues(state="all") if f"id={op_id} " in i.body]
    assert [i.number for i in matching] == [created.number]
    sandbox.forge.update_issue(created.number, state="closed", state_reason="not_planned")


# --- Solo con el falso -------------------------------------------------------------


def test_fake_assignees_dropped_silently_without_push(fake: FakeForge) -> None:
    """REQ-FORGE-59/11: sin push, GitHub descarta el cambio y el adaptador lo detecta."""
    issue = fake.seed_issue(title="t", assignees=[RESEARCHER_A])
    reader = fake.as_user(READ_ONLY)
    with pytest.raises(ForgeValidationError):
        reader.set_assignees(issue.number, [READ_ONLY])
    assert fake.get_issue(issue.number).assignees == (RESEARCHER_A,)  # type: ignore[union-attr]
    with pytest.raises(ForgeValidationError):
        reader.add_labels(issue.number, ["task"])
    assert fake.get_issue(issue.number).labels == ()  # type: ignore[union-attr]


def test_fake_unknown_assignee_is_dropped(fake: FakeForge) -> None:
    issue = fake.seed_issue(title="t")
    with pytest.raises(ForgeValidationError):
        fake.set_assignees(issue.number, [RESEARCHER_A, "nobody-here"])
    assert fake.get_issue(issue.number).assignees == (RESEARCHER_A,)  # type: ignore[union-attr]


def test_fake_race_before_set_assignees(fake: FakeForge) -> None:
    """08 §10.1: la falla race muta el estado justo antes del efecto; PATCH reemplaza el conjunto."""
    issue = fake.seed_issue(title="t")
    n = issue.number
    fake.faults.add(Fault("set_assignees", "race", race=lambda s: s.issues[n].assignees.append(RESEARCHER_A)))  # type: ignore[attr-defined]
    result = fake.set_assignees(n, [RESEARCHER_B])
    assert result.assignees == (RESEARCHER_B,)


def test_fake_create_issue_labels_need_push(fake: FakeForge) -> None:
    reader = fake.as_user(READ_ONLY)
    with pytest.raises(ForgeValidationError):
        reader.create_issue("proposal", "body", labels=["task"], op_id="task-propose-1")
    created = [i for i in fake.list_issues() if i.title == "proposal"]
    assert len(created) == 1 and created[0].labels == () and created[0].author == READ_ONLY


def test_fake_list_issues_filters(fake: FakeForge) -> None:
    a = fake.seed_issue(title="a", labels=["task", "claimed"])
    fake.clock.advance(60)
    b = fake.seed_issue(title="b", labels=["Task"])
    c = fake.seed_issue(title="c", state="closed", state_reason="not_planned")
    assert [i.number for i in fake.list_issues(labels=["task"])] == [b.number, a.number]
    assert [i.number for i in fake.list_issues(labels=["task", "claimed"])] == [a.number]
    assert [i.number for i in fake.list_issues(state="closed")] == [c.number]
    assert [i.number for i in fake.list_issues(since=fake.clock.now())] == [c.number, b.number]
    with pytest.raises(ForgeValidationError):
        fake.list_issues(state="bogus")


def test_fake_ensure_labels_creates_and_updates(fake: FakeForge) -> None:
    first = fake.ensure_labels([Label("task", "0e8a16", "Tarea"), Label("claimed")])
    assert first == {"task": "created", "claimed": "created"}
    writes = fake.calls.count("E", "ensure_labels")
    again = fake.ensure_labels([Label("task", "0e8a16", "Tarea"), Label("claimed")])
    assert again == {"task": "unchanged", "claimed": "unchanged"}
    assert fake.calls.count("E", "ensure_labels") == writes == 2  # la segunda vez, solo lecturas
    assert fake.ensure_labels([Label("task", "ffffff", "Task")]) == {"task": "updated"}
    assert fake.ensure_labels([]) == {}
    assert fake.to_state()["labels"]["task"] == {"color": "ffffff", "description": "Task"}
    with pytest.raises(ForgePermissionError):
        fake.as_user(READ_ONLY).ensure_labels([Label("new-label")])


def test_fake_comment_permissions_and_since(fake: FakeForge) -> None:
    issue = fake.seed_issue(title="t")
    c1 = fake.create_comment(issue.number, "hello", op_id="c-1")
    fake.clock.advance(60)
    cut = fake.clock.now()
    c2 = fake.as_user(READ_ONLY).create_comment(issue.number, "from reader", op_id="c-2")
    assert [c.id for c in fake.list_comments(issue.number, since=cut)] == [c2.id]
    with pytest.raises(ForgePermissionError):
        fake.as_user(READ_ONLY).update_comment(c1.id, "hijack")
    assert fake.update_comment(c2.id, "moderated").body == "moderated"  # el lead tiene push
    with pytest.raises(ForgeNotFound):
        fake.update_comment(987654, "x")
    with pytest.raises(ForgeNotFound):
        fake.create_comment(424242, "x", op_id="c-3")


def test_fake_seed_issue_events_and_comments(fake: FakeForge) -> None:
    issue = fake.seed_issue(title="task", labels=["task"], author=RESEARCHER_A)
    events = fake.list_issue_events(issue.number)
    assert [(e.event, e.label, e.actor) for e in events] == [("labeled", "task", RESEARCHER_A)]
    comment = fake.seed_comment(issue.number, author=RESEARCHER_B, body="claim marker")
    assert fake.list_comments(issue.number) == [comment]
    with pytest.raises(ForgeNotFound):
        fake.list_issue_events(31337)
    with pytest.raises(TypeError):
        fake.seed_issue(titel="typo")


def test_fake_op_id_grammar(fake: FakeForge) -> None:
    with pytest.raises(ValueError):
        fake.create_issue("t", "b", op_id="bad id!")
