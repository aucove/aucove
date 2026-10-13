# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Fallas inyectables y registro de llamadas del falso (08-forja REQ-FORGE-60/61; 12-mvp REQ-MVP-27).

Solo con el falso (REQ-FORGE-64). Cada escritura implementada en H2 se prueba
antes y después del efecto (REQ-MVP-27): ``timeout_before`` y ``timeout_after``,
5xx sin efecto (``http_5xx``) y con él (``http_5xx_after``), y con 401, 403, 404,
409, 422, límite primario y secundario y red caída. Toda escritura queda en
``calls``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest

from aucove.forge.base import (
    Comment,
    ForgeAuthError,
    ForgeError,
    ForgeNotFound,
    ForgePermissionError,
    ForgeUnavailable,
    ForgeUnsupported,
    ForgeValidationError,
    Label,
    NetworkTimeout,
    NetworkUnreachable,
    RateLimited,
    Ref,
    RefAlreadyExists,
    RefConflict,
)
from aucove.forge.fake import ADAPTER_METHODS, UNSUPPORTED_METHODS, Fault, FakeForge
from tests.contract.conftest import LEAD, RESEARCHER_A

Ctx = dict[str, Any]


def _state(fake: FakeForge) -> dict[str, Any]:
    return fake.to_state()


def _issue(fake: FakeForge, number: int) -> dict[str, Any]:
    return next(i for i in _state(fake)["issues"] if i["number"] == number)


def _base(fake: FakeForge) -> str:
    return str(_state(fake)["refs"]["heads/main"])


# --- Escrituras implementadas: preparación, llamada y efecto ------------------------


def _setup_new_ref(fake: FakeForge) -> Ctx:
    return {"sha": fake.create_empty_commit(_base(fake), "claim")}


def _setup_existing_ref(fake: FakeForge) -> Ctx:
    c1 = fake.create_empty_commit(_base(fake), "claim")
    fake.create_ref("task/1", c1)
    return {"old": c1, "sha": fake.create_empty_commit(c1, "work")}


def _setup_issue(**fields: object) -> Callable[[FakeForge], Ctx]:
    def setup(fake: FakeForge) -> Ctx:
        return {"n": fake.seed_issue(title="t", **fields).number}

    return setup


def _setup_comment(fake: FakeForge) -> Ctx:
    n = fake.seed_issue(title="t").number
    return {"n": n, "id": fake.seed_comment(n, author=LEAD, body="old").id}


def _setup_commits(fake: FakeForge) -> Ctx:
    return {"count": len(_state(fake)["commits"])}


@dataclass(frozen=True)
class WriteCase:
    name: str
    setup: Callable[[FakeForge], Ctx]
    act: Callable[[FakeForge, Ctx], object]
    effect: Callable[[FakeForge, Ctx], bool]


WRITES: tuple[WriteCase, ...] = (
    WriteCase(
        "create_ref",
        _setup_new_ref,
        lambda f, c: f.create_ref("task/1", c["sha"]),
        lambda f, c: _state(f)["refs"].get("heads/task/1") == c["sha"],
    ),
    WriteCase(
        "update_ref",
        _setup_existing_ref,
        lambda f, c: f.update_ref("task/1", c["sha"]),
        lambda f, c: _state(f)["refs"]["heads/task/1"] == c["sha"],
    ),
    WriteCase(
        "delete_ref",
        _setup_existing_ref,
        lambda f, c: f.delete_ref("task/1"),
        lambda f, c: "heads/task/1" not in _state(f)["refs"],
    ),
    WriteCase(
        "create_empty_commit",
        _setup_commits,
        lambda f, c: f.create_empty_commit(_base(f), "claim"),
        lambda f, c: len(_state(f)["commits"]) > c["count"],
    ),
    WriteCase(
        "create_issue",
        lambda f: {},
        lambda f, c: f.create_issue("new issue", "body", op_id="op-1"),
        lambda f, c: any(i["title"] == "new issue" for i in _state(f)["issues"]),
    ),
    WriteCase(
        "update_issue",
        _setup_issue(),
        lambda f, c: f.update_issue(c["n"], state="closed", state_reason="not_planned"),
        lambda f, c: _issue(f, c["n"])["state"] == "closed",
    ),
    WriteCase(
        "add_labels",
        _setup_issue(),
        lambda f, c: f.add_labels(c["n"], ["task"]),
        lambda f, c: _issue(f, c["n"])["labels"] == ["task"],
    ),
    WriteCase(
        "remove_label",
        _setup_issue(labels=["task"]),
        lambda f, c: f.remove_label(c["n"], "task"),
        lambda f, c: _issue(f, c["n"])["labels"] == [],
    ),
    WriteCase(
        "ensure_labels",
        lambda f: {},
        lambda f, c: f.ensure_labels([Label("task", "0e8a16")]),
        lambda f, c: "task" in _state(f)["labels"],
    ),
    WriteCase(
        "set_assignees",
        _setup_issue(),
        lambda f, c: f.set_assignees(c["n"], [RESEARCHER_A]),
        lambda f, c: _issue(f, c["n"])["assignees"] == [RESEARCHER_A],
    ),
    WriteCase(
        "add_assignees",
        _setup_issue(),
        lambda f, c: f.add_assignees(c["n"], [RESEARCHER_A]),
        lambda f, c: _issue(f, c["n"])["assignees"] == [RESEARCHER_A],
    ),
    WriteCase(
        "remove_assignees",
        _setup_issue(assignees=[RESEARCHER_A]),
        lambda f, c: f.remove_assignees(c["n"], [RESEARCHER_A]),
        lambda f, c: _issue(f, c["n"])["assignees"] == [],
    ),
    WriteCase(
        "create_comment",
        _setup_issue(),
        lambda f, c: f.create_comment(c["n"], "hello", op_id="c-1"),
        lambda f, c: len(_issue(f, c["n"])["comments"]) == 1,
    ),
    WriteCase(
        "update_comment",
        _setup_comment,
        lambda f, c: f.update_comment(c["id"], "new body"),
        lambda f, c: _issue(f, c["n"])["comments"][0]["body"] == "new body",
    ),
)
CASES = {case.name: case for case in WRITES}

# Resultado esperado: excepción (o None si devuelve) y efecto. Por defecto, sin efecto.
Outcome = tuple[type[BaseException] | None, bool]
# Recuperación de 08 §4.4 y §4.7 tras una respuesta ambigua con el efecto hecho: se
# encuentra el efecto (GET de la ref o marcador op) y es éxito.
_RECOVERED: dict[str, Outcome] = {
    "create_ref": (None, True),
    "update_ref": (None, True),
    "create_issue": (None, True),
    "create_comment": (None, True),
}
_HTTP_404: dict[str, Outcome] = {"delete_ref": (ForgeValidationError, False), "remove_label": (None, False)}
_HTTP_409: dict[str, Outcome] = {"update_ref": (ForgeUnavailable, False), "delete_ref": (ForgeUnavailable, False)}
_HTTP_422: dict[str, Outcome] = {"update_ref": (RefConflict, False)}

MATRIX: dict[str, tuple[type[BaseException] | None, bool, dict[str, Outcome]]] = {
    "timeout_before": (NetworkTimeout, False, {}),
    "timeout_after": (NetworkTimeout, True, _RECOVERED),
    "http_401": (ForgeAuthError, False, {}),
    "http_403": (ForgePermissionError, False, {}),
    "http_404": (ForgeNotFound, False, _HTTP_404),
    "http_409": (ForgeValidationError, False, _HTTP_409),
    "http_422": (ForgeValidationError, False, _HTTP_422),
    "http_5xx": (ForgeUnavailable, False, {}),
    # 502 con el cambio ya hecho (REQ-MVP-27: fallas después de cada escritura).
    "http_5xx_after": (ForgeUnavailable, True, _RECOVERED),
    "rate_limit_primary": (RateLimited, False, {}),
    "rate_limit_secondary": (RateLimited, False, {}),
    "unreachable": (NetworkUnreachable, False, {}),
}


def _expected(fault: str, method: str) -> Outcome:
    exc, effect, overrides = MATRIX[fault]
    return overrides.get(method, (exc, effect))


@pytest.mark.parametrize("fault_kind", list(MATRIX))
@pytest.mark.parametrize("case", WRITES, ids=[c.name for c in WRITES])
def test_fault_around_each_write(fake: FakeForge, case: WriteCase, fault_kind: str) -> None:
    ctx = case.setup(fake)
    fake.calls.clear()
    fake.faults.add(Fault(case.name, fault_kind, nth=1))  # type: ignore[arg-type]
    exc_type, effect = _expected(fault_kind, case.name)
    if exc_type is None:
        case.act(fake, ctx)
    else:
        with pytest.raises(exc_type) as info:
            case.act(fake, ctx)
        if isinstance(info.value, ForgeError) and fault_kind.startswith("http_") and exc_type is not ForgeUnsupported:
            assert info.value.http_status is not None
        if fault_kind == "http_403":
            assert info.value.hint and "X-Accepted-GitHub-Permissions" in info.value.hint  # type: ignore[attr-defined]
    assert case.effect(fake, ctx) is effect
    # La petición quedó registrada (REQ-MVP-27), con su resultado.
    entries = [c for c in fake.calls.entries() if c.method == case.name]
    primary = [c for c in entries if c.outcome == fault_kind]
    assert len(primary) == 1
    if case.name != "ensure_labels":  # su petición principal es el GET de la etiqueta
        assert primary[0].kind in ("E", "C")
        assert case.name in [m for m, _ in fake.calls.writes()]


@pytest.mark.parametrize("case", WRITES, ids=[c.name for c in WRITES])
def test_each_write_without_faults_is_logged(fake: FakeForge, case: WriteCase) -> None:
    ctx = case.setup(fake)
    fake.calls.clear()
    case.act(fake, ctx)
    assert case.effect(fake, ctx) is True
    writes = fake.calls.writes()
    assert writes and writes[0][0] == case.name
    assert all(c.outcome == "ok" for c in fake.calls.entries())
    assert all(c.actor == LEAD for c in fake.calls.entries())


def test_timeout_after_recovery_does_not_duplicate(fake: FakeForge) -> None:
    """create_comment tras timeout_after encuentra su marcador op y no crea otro (REQ-FORGE-35)."""
    n = fake.seed_issue(title="t").number
    fake.faults.add(Fault("create_comment", "timeout_after", nth=1))
    comment = fake.create_comment(n, "hello", op_id="c-9")
    assert isinstance(comment, Comment)
    assert [c.id for c in fake.list_comments(n)] == [comment.id]
    assert fake.calls.count("C", "create_comment") == 1
    assert fake.calls.count("L", "create_comment") == 1  # la búsqueda del marcador


def test_create_ref_recovers_as_already_exists_after_ambiguous_race(fake: FakeForge) -> None:
    """Si otra cuenta crea la ref justo antes del efecto (race), create_ref da RefAlreadyExists (08 §4.4)."""
    base = _base(fake)
    mine = fake.create_empty_commit(base, "mine")
    theirs = fake.as_user(RESEARCHER_A).create_empty_commit(base, "theirs")

    def rival(state: object) -> None:
        fake.as_user(RESEARCHER_A).create_ref("task/2", theirs)

    fake.faults.add(Fault("create_ref", "race", nth=1, race=rival))
    with pytest.raises(RefAlreadyExists) as info:
        fake.create_ref("task/2", mine)
    assert info.value.existing == Ref("task/2", theirs)


def test_create_ref_409_loser_gets_the_winner(fake: FakeForge) -> None:
    """D-40 y 04-tareas §2.3: la perdedora de la carrera puede recibir 409; se trata como el 422 (08 §4.4)."""
    base = _base(fake)
    winner = fake.create_empty_commit(base, "claim ana")
    loser = fake.as_user(RESEARCHER_A).create_empty_commit(base, "claim bruno")
    fake.create_ref("task/1", winner)
    fake.calls.clear()
    fake.faults.add(Fault("create_ref", "http_409", nth=1))
    with pytest.raises(RefAlreadyExists) as info:
        fake.as_user(RESEARCHER_A).create_ref("task/1", loser)
    assert info.value.existing == Ref("task/1", winner)
    assert info.value.http_status == 409
    assert fake.calls.count("L", "create_ref", http="GET") == 1  # GET de confirmación, sin reintentar el POST
    assert fake.calls.count("E", "create_ref") == 1


def test_create_ref_409_with_my_own_sha_is_success(fake: FakeForge) -> None:
    """REQ-FORGE-28: si tras el 409 la ref existe con el sha pedido, es éxito (un intento anterior la creó)."""
    mine = fake.create_empty_commit(_base(fake), "claim")
    fake.create_ref("task/1", mine)
    fake.faults.add(Fault("create_ref", "http_409", nth=1))
    assert fake.create_ref("task/1", mine) == Ref("task/1", mine)


def test_nth_counts_calls_since_the_fault_was_added(fake: FakeForge) -> None:
    fake.get_ref("main")
    fake.faults.add(Fault("get_ref", "timeout_before", nth=2))
    assert fake.get_ref("main") is not None
    with pytest.raises(NetworkTimeout):
        fake.get_ref("main")
    assert fake.get_ref("main") is not None
    assert len(fake.faults) == 1
    fake.faults.clear()
    assert len(fake.faults) == 0


def test_wildcard_fault_hits_every_method(fake: FakeForge) -> None:
    fake.faults.add(Fault("*", "unreachable"))
    with pytest.raises(NetworkUnreachable):
        fake.get_issue(1)
    with pytest.raises(NetworkUnreachable):
        fake.create_issue("t", "b", op_id="x")
    assert _state(fake)["issues"] == []


def test_faults_on_reads(fake: FakeForge) -> None:
    fake.faults.add(Fault("get_ref", "http_5xx", nth=1))
    with pytest.raises(ForgeUnavailable):
        fake.get_ref("main")
    fake.faults.add(Fault("get_issue", "http_404", nth=1))
    issue = fake.seed_issue(title="t")
    assert fake.get_issue(issue.number) is None  # 404 -> None (08 §4.2)
    assert fake.get_issue(issue.number) is not None
    fake.faults.add(Fault("codeowners_errors", "http_404", nth=1))
    assert fake.codeowners_errors("main") == []
    fake.faults.add(Fault("list_issues", "timeout_after", nth=1))
    with pytest.raises(NetworkTimeout):
        fake.list_issues()


@pytest.mark.parametrize(
    ("kind", "window"),
    [("rate_limit_secondary", timedelta(seconds=60)), ("rate_limit_primary", timedelta(hours=1))],
)
def test_rate_limit_blocks_the_account_until_reset(fake: FakeForge, kind: str, window: timedelta) -> None:
    """REQ-FORGE-51: un RateLimited bloquea el adaptador de esa cuenta hasta reset_at.

    En GitHub el límite es por credencial (§7.1): otra cuenta, otra vista del falso, sigue.
    """
    now = fake.clock.now()
    fake.faults.add(Fault("get_ref", kind, nth=1))  # type: ignore[arg-type]
    with pytest.raises(RateLimited) as info:
        fake.get_ref("main")
    assert info.value.reset_at == now + window
    assert info.value.retryable is True and info.value.http_status == 403
    recorded = len(fake.calls)
    with pytest.raises(RateLimited):
        fake.get_issue(1)
    with pytest.raises(RateLimited):
        fake.as_user(LEAD.upper()).create_issue("t", "b", op_id="blocked")  # la misma cuenta
    assert len(fake.calls) == recorded  # bloqueado: no hay peticiones
    assert fake.as_user(RESEARCHER_A).get_ref("main") is not None
    fake.clock.advance(window)
    assert fake.get_ref("main") is not None


def test_latency_delays_but_succeeds(fake: FakeForge) -> None:
    fake.faults.add(Fault("get_ref", "latency", delay_s=0.05))
    start = time.monotonic()
    assert fake.get_ref("main") is not None
    assert time.monotonic() - start >= 0.04


def test_fault_validation() -> None:
    with pytest.raises(ValueError):
        Fault("no_such_method", "http_401")
    with pytest.raises(ValueError):
        Fault("get_ref", "http_418")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Fault("get_ref", "race")
    with pytest.raises(ValueError):
        Fault("get_ref", "http_401", race=lambda s: None)
    with pytest.raises(ValueError):
        Fault("get_ref", "http_401", nth=0)
    assert UNSUPPORTED_METHODS < ADAPTER_METHODS


def test_faults_combine_or_fail_loudly(fake: FakeForge) -> None:
    """Una latency, una race y una falla se combinan; dos fallas en la misma llamada son un error de la prueba."""
    seen: list[object] = []
    fake.faults.add(Fault("get_ref", "latency", nth=1, delay_s=0))
    fake.faults.add(Fault("get_ref", "race", nth=1, race=seen.append))
    fake.faults.add(Fault("get_ref", "http_5xx", nth=1))
    with pytest.raises(ForgeUnavailable):
        fake.get_ref("main")
    assert len(seen) == 1
    fake.faults.clear()
    fake.faults.add(Fault("*", "unreachable", nth=1))
    fake.faults.add(Fault("get_ref", "timeout_after", nth=1))
    with pytest.raises(ValueError, match="same call"):
        fake.get_ref("main")
    assert fake.get_ref("main") is not None  # las dos ya se consumieron


def test_call_log_counts_by_kind_method_and_http(fake: FakeForge) -> None:
    """REQ-FORGE-60: peticiones por método HTTP y por tipo; C es un subconjunto de E."""
    n = fake.seed_issue(title="t").number
    fake.calls.clear()
    fake.get_issue(n)
    fake.add_labels(n, ["task"])
    fake.create_comment(n, "hi", op_id="c-1")
    fake.list_comments(n)
    assert fake.calls.count() == 4
    assert fake.calls.count("L") == 2
    assert fake.calls.count("E") == 2
    assert fake.calls.count("C") == 1
    assert fake.calls.count(http="POST") == 2
    assert fake.calls.count(http="get") == 2
    assert fake.calls.count("E", "add_labels") == 1
    assert fake.calls.writes() == [
        ("add_labels", {"number": n, "labels": ["task"]}),
        ("create_comment", {"number": n, "body": "hi", "op_id": "c-1"}),
    ]


def test_budget_of_a_claim_like_sequence(fake: FakeForge) -> None:
    """El contador permite verificar presupuestos como el de claim (08 §7.5: E <= 5, C <= 2)."""
    n = fake.seed_issue(title="task", labels=["task"]).number
    fake.calls.clear()
    base = _base(fake)
    sha = fake.create_empty_commit(base, "claim #1")
    fake.create_ref("task/1", sha)
    fake.set_assignees(n, [LEAD])
    fake.add_labels(n, ["claimed"])
    fake.create_comment(n, "claim marker", op_id="claim-1")
    assert fake.calls.count(kind="E") <= 5
    assert fake.calls.count(kind="C") <= 2
    assert fake.calls.count(kind="L") == 1  # el GET del árbol del padre


def test_pagination_counts_one_request_per_page(fake: FakeForge) -> None:
    for i in range(250):
        fake.seed_issue(title=f"i{i}")
    fake.calls.clear()
    assert len(fake.list_issues()) == 250
    assert fake.calls.count("L", "list_issues") == 3
