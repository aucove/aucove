# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Barrera de las operaciones solo humanas en ``registry.invoke``.

``require_human`` tiene dos fases (06-permisos §6, 05 §10). La fase 1 (contexto de agente y
TTY en stdin y stdout) corre antes de leer nada; la fase 2 (el reto tecleado) corre dentro de
la operación, tras las lecturas, y con ``--dry-run`` no corre (REQ-OPS-13, REQ-OPS-47).
Antes, ``invoke`` exigía ``human_confirmed`` también con ``--dry-run`` y, en la superficie
``cli``, nunca miraba la TTY: devolvía ``E_HUMAN_ONLY_NOT_CONFIRMED`` donde correspondía
``E_HUMAN_ONLY_NO_TTY``.
"""

from __future__ import annotations

import io
import sys
import types
from collections.abc import Iterator

import pytest

from aucove import _meta
from aucove.ops.context import Invocation, OpContext
from aucove.ops.registry import OpSpec, invoke
from aucove.ops.result import Result, ok

OP = "task.accept"
SPEC = OpSpec(id=OP, cli=("task", "accept"), mcp_tool=None, human_only=True, effects="forge",
              offline="online", module="ops._test_human_op")


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[bool]]:
    seen: list[bool] = []
    module = types.ModuleType(f"{_meta.NAME}.ops._test_human_op")

    def run(ctx: OpContext, **kwargs: object) -> Result:
        seen.append(ctx.inv.dry_run)
        return ok(OP, {"dry_run": True, "plan": []})

    module.run = run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    yield seen


def _inv(**kw: object) -> Invocation:
    base: dict[str, object] = {"root": None, "lang": "en", "json": True, "dry_run": False, "surface": "cli",
                               "env": {}, "cwd": "."}
    base.update(kw)
    return Invocation(**base)  # type: ignore[arg-type]


def _code(inv: Invocation) -> str | None:
    result = invoke(SPEC, OpContext(inv))
    return None if result.error is None else result.error.code


def test_dry_run_only_needs_phase_1(calls: list[bool]) -> None:
    assert _code(_inv(dry_run=True, tty=True)) is None
    assert calls == [True]


def test_cli_without_tty_is_no_tty(calls: list[bool]) -> None:
    assert _code(_inv(tty=False)) == "E_HUMAN_ONLY_NO_TTY"
    assert _code(_inv(tty=False, dry_run=True)) == "E_HUMAN_ONLY_NO_TTY"
    assert calls == []


def test_agent_env_precedes_tty(calls: list[bool]) -> None:
    for env in ({"CLAUDE_CODE_CHILD_SESSION": "1"}, {"CI": "true"}, {"GITHUB_ACTIONS": "true"}):
        assert _code(_inv(env=env, tty=True, dry_run=True)) == "E_HUMAN_ONLY_AGENT_ENV"
        assert _code(_inv(env=env, tty=False)) == "E_HUMAN_ONLY_AGENT_ENV"
    assert calls == []


def test_without_phase_2_there_are_no_effects(calls: list[bool]) -> None:
    # Con TTY y sin dry-run hace falta la fase 2; sin Invocation.confirm, falla cerrada.
    assert _code(_inv(tty=True)) == "E_HUMAN_ONLY_NOT_CONFIRMED"
    assert calls == []
    assert _code(_inv(human_confirmed=True, tty=True)) is None
    assert calls == [False]


def test_the_cli_phase_2_runs_inside_the_operation(calls: list[bool]) -> None:
    # Con Invocation.confirm (solo la CLI) la operación corre: ella pide el reto tras sus lecturas
    def confirm(op: str, target: str, summary: str) -> object:
        raise AssertionError("la operación de prueba no lo pide")

    assert _code(_inv(tty=True, confirm=confirm)) is None
    assert calls == [False]
    assert _code(_inv(tty=False, confirm=confirm)) == "E_HUMAN_ONLY_NO_TTY"  # la fase 1 va antes
    assert _code(_inv(surface="mcp", tty=True, confirm=confirm)) == "E_HUMAN_ONLY_NO_TTY"
    assert calls == [False]


def test_confirm_human_needs_the_cli_and_the_right_operation() -> None:
    from datetime import datetime, timezone

    from aucove.ops.result import OpError

    class Proof:
        def __init__(self, operation: str) -> None:
            self.operation, self.confirmed_at = operation, datetime.now(timezone.utc)

    def ctx(**kw: object) -> OpContext:
        return OpContext(_inv(tty=True, **kw))

    good = ctx(confirm=lambda op, target, summary: Proof(op))
    assert good.confirm_human("accept", "5", "plan").operation == "accept" and good.human_confirmed
    for bad in (ctx(), ctx(surface="mcp", confirm=lambda *a: Proof("accept")),
                ctx(dry_run=True, confirm=lambda *a: Proof("accept")), ctx(confirm=lambda *a: Proof("cancel"))):
        with pytest.raises(OpError) as exc:
            bad.confirm_human("accept", "5", "plan")
        assert exc.value.code == "E_HUMAN_ONLY_NOT_CONFIRMED" and not bad.human_confirmed


def test_mcp_never_passes(calls: list[bool]) -> None:
    assert _code(_inv(surface="mcp", tty=True, dry_run=True)) == "E_HUMAN_ONLY_NO_TTY"
    assert calls == []


class _Stream(io.StringIO):
    def __init__(self, tty: bool) -> None:
        super().__init__()
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.mark.parametrize(("stdin_tty", "stdout_tty", "expected"), [
    (True, True, None),
    (True, False, "E_HUMAN_ONLY_NO_TTY"),
    (False, True, "E_HUMAN_ONLY_NO_TTY"),
])
def test_tty_is_read_from_stdin_and_stdout(
    calls: list[bool], monkeypatch: pytest.MonkeyPatch, stdin_tty: bool, stdout_tty: bool, expected: str | None
) -> None:
    monkeypatch.setattr(sys, "stdin", _Stream(stdin_tty))
    monkeypatch.setattr(sys, "stdout", _Stream(stdout_tty))
    assert _code(_inv(dry_run=True)) == expected


def test_human_confirmed_only_counts_in_the_cli(calls: list[bool]) -> None:
    for surface in ("mcp", "hook", "ci"):
        assert _code(_inv(surface=surface, human_confirmed=True, tty=True)) == "E_HUMAN_ONLY_NO_TTY"
    assert calls == []
