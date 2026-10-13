# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Reglas ``assessment`` y ``direction-ack`` (09-ci §4.1; 04-tareas §8.3 y §9; 06 REQ-PERM-17).

Un PR de ``task/12`` de carla contra FakeForge; los roles ``basic`` dan ana responsable y bruno
suplente. Las etiquetas se ponen con la API del falso, que registra cada evento con el reloj.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from aucove import _meta
from aucove.config.base import BaseSnapshot
from aucove.config.project import parse_project
from aucove.config.roles import parse_roles
from aucove.domain.findings import Finding
from aucove.forge.base import RepoRef
from aucove.forge.fake import CI_LOGIN, Fault, FakeClock, FakeForge
from aucove.i18n import translator
from aucove.ops.rules import coordinator
from aucove.ops.rules.base import RuleInput, RuleSpec
from tests.conftest import FIXTURES, roles_fixture

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
PROJECT = (FIXTURES / "config" / "project" / "minimal.yaml").read_text(encoding="utf-8")


def snapshot(*, require: bool = False) -> BaseSnapshot:
    text = PROJECT + ("coordinator:\n  require_direction_ack: true\n" if require else "")
    cfg, problems = parse_project(text.encode("utf-8"))
    roles, _ = parse_roles(roles_fixture("basic").encode("utf-8"))
    assert cfg is not None and roles is not None, problems
    return BaseSnapshot(ref="origin/main", sha=None, project=cfg, roles=roles, decisions=frozenset(),
                        fallback_sha=None, source="local")


class World:
    def __init__(self, head_branch: str = "task/12") -> None:
        self.clock = FakeClock(NOW)
        self.forge = FakeForge(RepoRef("example-org", "demo"), viewer=CI_LOGIN, clock=self.clock)
        for login in ("ana", "bruno", "carla"):
            self.forge.seed_user(login, permission="write")
        base = self.forge.seed_commit({"README.md": b"x\n"}, [], "base", "ana")
        self.forge.seed_branch("main", base)
        self.head = self.forge.seed_commit({"README.md": b"y\n"}, [base], "trabajo", "carla")
        self.forge.seed_pull_request(number=20, head_branch=head_branch, head_sha=self.head, author="carla")

    def label(self, login: str, label: str, *, remove: bool = False) -> None:
        self.clock.advance(60)
        forge = self.forge.as_user(login)
        forge.remove_label(20, label) if remove else forge.add_labels(20, [label])

    def assess(self, author: str = "carla", *, head: str | None = None, phase: str = "submission") -> None:
        data = {"task": 12, "phase": phase, "head_sha": head or self.head, "verdict": "none"}
        body = f"<!-- {_meta.NAME}:assessment v1\n{json.dumps(data)}\n-->\n**Entrega 1 de #12.**"
        self.forge.seed_comment(20, author=author, body=body)

    def run(self, rule: RuleSpec, *, require: bool = False) -> list[Finding]:
        pr = self.forge.get_pull_request(20)
        inp = RuleInput(mode="pr", t=translator("en"), now=NOW, policy=snapshot(require=require), pr=pr,
                        forge=self.forge, head_ref=pr.head_branch if pr else None)
        return list(rule.run(inp))


def codes(findings: list[Finding]) -> list[tuple[str, str]]:
    return [(f.code, f.severity) for f in findings]


# -- assessment ---------------------------------------------------------------------------------


def test_a_pr_without_an_assessment_warns() -> None:
    w = World()
    (f,) = w.run(coordinator.ASSESSMENT)
    assert (f.code, f.severity, f.check) == ("E_VALIDATION_ASSESSMENT", "warning", "assessment")
    assert "record-assessment 12 --phase submission" in (f.hint or "")


def test_the_assessment_of_the_current_head_by_the_claimant_passes() -> None:
    w = World()
    w.assess()
    assert w.run(coordinator.ASSESSMENT) == []


@pytest.mark.parametrize("kwargs", [
    {"head": "b" * 40},  # una cabeza anterior
    {"author": "bruno"},  # no es el dueño del reclamo (REQ-TASK-61)
    {"phase": "claim"},  # la evaluación del reclamo no es la de la entrega
])
def test_other_assessments_do_not_count(kwargs: dict[str, str]) -> None:
    w = World()
    w.assess(**kwargs)
    assert codes(w.run(coordinator.ASSESSMENT)) == [("E_VALIDATION_ASSESSMENT", "warning")]


def test_a_pr_not_from_a_task_branch_is_not_assessed() -> None:
    assert World(head_branch="patch-1").run(coordinator.ASSESSMENT) == []


def test_without_comments_it_fails_closed() -> None:
    w = World()
    w.forge.faults.add(Fault("list_comments", "unreachable"))
    assert codes(w.run(coordinator.ASSESSMENT)) == [("E_INTERNAL_UNEXPECTED", "error")]


# -- direction-ack ------------------------------------------------------------------------------


def test_without_the_flag_there_is_nothing_to_acknowledge() -> None:
    assert World().run(coordinator.DIRECTION) == []


def test_the_flag_without_an_ack_warns_or_blocks() -> None:
    w = World()
    w.label("carla", coordinator.FLAG)
    assert codes(w.run(coordinator.DIRECTION)) == [("E_VALIDATION_DIRECTION", "warning")]
    assert codes(w.run(coordinator.DIRECTION, require=True)) == [("E_VALIDATION_DIRECTION", "error")]


@pytest.mark.parametrize("approver", ["ana", "bruno"])
def test_an_ack_by_the_lead_or_the_deputy_after_the_flag_passes(approver: str) -> None:
    w = World()
    w.label("carla", coordinator.FLAG)
    w.label(approver, coordinator.ACK)
    assert w.run(coordinator.DIRECTION, require=True) == []


def test_an_ack_that_does_not_count() -> None:
    """REQ-TASK-70: de otra persona, anterior a la última flag, o quitado."""
    w = World()
    w.label("carla", coordinator.FLAG)
    w.label("carla", coordinator.ACK)  # no es lead ni deputy
    assert codes(w.run(coordinator.DIRECTION, require=True)) == [("E_VALIDATION_DIRECTION", "error")]
    w = World()
    w.label("ana", coordinator.ACK)
    w.label("carla", coordinator.FLAG)  # la flag llega después del ack
    assert codes(w.run(coordinator.DIRECTION)) == [("E_VALIDATION_DIRECTION", "warning")]
    w = World()
    w.label("carla", coordinator.FLAG)
    w.label("ana", coordinator.ACK)
    w.label("ana", coordinator.ACK, remove=True)
    assert codes(w.run(coordinator.DIRECTION)) == [("E_VALIDATION_DIRECTION", "warning")]


def test_without_events_it_fails_closed() -> None:
    w = World()
    w.label("carla", coordinator.FLAG)
    w.forge.faults.add(Fault("list_issue_events", "unreachable"))
    assert codes(w.run(coordinator.DIRECTION)) == [("E_INTERNAL_UNEXPECTED", "error")]


def test_both_rules_run_only_on_prs_and_need_the_forge() -> None:
    for rule in coordinator.RULES:
        assert rule.modes == frozenset({"pr"}) and rule.needs_forge
    assert [r.id for r in coordinator.RULES] == ["assessment", "direction-ack"]
