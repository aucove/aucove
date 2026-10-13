# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``review approve`` y ``review request-changes`` (05 §9.24): solo humanas, con el reto tras
las lecturas, sobre la cabeza que se mostró y sin integrar nunca (REQ-OPS-46)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aucove.cli.main import HumanConfirmation
from aucove.config.schemas import validate_result
from aucove.forge.base import ChangedFile
from aucove.ops.result import Result
from tests.board_world import NOW, Typed, World, human, new_world
from tests.conftest import roles_fixture


def valid(result: Result) -> Result:
    envelope = result.to_dict()
    assert validate_result(envelope) == [], json.dumps(envelope, ensure_ascii=False, indent=1)
    return result


def _pr(w: World, *, number: int = 40, task: int = 5, files: tuple[str, ...] = ("sections/methods/methods.tex",),
        draft: bool = False) -> str:
    head = w.task(task, claimant="carla")["head"]
    assert head is not None
    w.forge.seed_pull_request(number=number, head_branch=f"task/{task}", head_sha=head, author="carla", draft=draft,
                              title="Métodos listos", files=[ChangedFile(f, "modified") for f in files])
    return head


@pytest.fixture
def world(tmp_path: Path) -> World:
    return new_world(tmp_path)


def test_the_lead_approves_on_the_shown_head(world: World) -> None:
    head = _pr(world)
    typed = Typed()
    world.forge.calls.clear()
    result = valid(human(world, "review.approve", typed, pr=40, body="Bien."))
    calls = world.forge.calls.entries()
    assert result.ok, result.error
    data = result.data
    assert data["created"] is True and data["merge_url"].endswith("/pull/40")  # type: ignore[index]
    assert data["review"]["state"] == "APPROVED" and data["review"]["commit_id"] == head  # type: ignore[index]
    # 08 §7.5: el PR, sus archivos, los checks (2: los runs y el estado combinado) y las
    # revisiones; el PR otra vez tras el reto
    assert [c.method for c in calls if c.kind == "L" and c.method != "identity"] == [
        "get_pull_request", "list_pull_request_files", "get_checks", "get_checks", "list_reviews", "get_pull_request"]
    assert [c.method for c in calls if c.kind in ("E", "C")] == ["submit_review"]
    op, target, summary = typed.seen[0]
    assert (op, target) == ("approve", "40") and head[:12] in summary and "Métodos listos" in summary
    assert world.forge.get_pull_request(40).state == "open"  # type: ignore[union-attr]  # no integra
    again = valid(human(world, "review.approve", typed, pr=40))
    assert again.ok and again.data["created"] is False and len(typed.seen) == 1  # type: ignore[index]


def test_request_changes_needs_a_body(world: World) -> None:
    _pr(world)
    missing = human(world, "review.request-changes", login="bruno", pr=40)
    assert missing.error is not None and missing.error.details == {"errors": [{"param": "body", "reason": "required"}]}
    typed = Typed()
    result = valid(human(world, "review.request-changes", typed, login="bruno", pr=40, body="Falta la tabla 2."))
    assert result.ok and result.data["review"]["state"] == "CHANGES_REQUESTED"  # type: ignore[index]
    assert typed.seen[0][:2] == ("request-changes", "40")


def test_who_may_decide(world: World) -> None:
    _pr(world)
    own = human(world, "review.approve", login="carla", pr=40)
    assert own.error is not None and own.error.code == "E_PERM_SELF_REVIEW"
    outsider = human(world, "review.approve", login="dario", pr=40)
    assert outsider.error is not None and outsider.error.code == "E_PERM_NOT_REVIEWER"
    assert world.forge.calls.writes() == []


def test_the_owner_of_a_touched_part_only_requests_changes(tmp_path: Path) -> None:
    roles = roles_fixture("basic").replace('title: "Introduction"\n    owner: ana', 'title: "Introduction"\n    owner: dario')
    w = new_world(tmp_path, extra_files={"roles.yaml": roles})
    _pr(w, files=("sections/introduction/i.tex",))
    approve = human(w, "review.approve", login="dario", pr=40)
    assert approve.error is not None and approve.error.code == "E_PERM_NOT_REVIEWER"
    assert valid(human(w, "review.request-changes", login="dario", pr=40, body="Revisa la entrada.")).ok


def test_the_editor_approves_latex_prs(tmp_path: Path) -> None:
    w = new_world(tmp_path, extra_files={"roles.yaml": roles_fixture("basic") + "editor: dario\n"})
    _pr(w)  # toca methods.tex: R2 (00-marco D-56)
    assert valid(human(w, "review.approve", login="dario", pr=40)).ok
    _pr(w, number=41, task=6, files=("sections/methods/data.csv",))
    other = human(w, "review.approve", login="dario", pr=41)
    assert other.error is not None and other.error.code == "E_PERM_NOT_REVIEWER"  # no toca LaTeX


def test_a_new_push_during_the_challenge_is_detected(world: World) -> None:
    head = _pr(world)

    def push_then_confirm(op: str, target: str, summary: str) -> HumanConfirmation:
        newer = world.forge.create_empty_commit(head, "más")
        world.forge.update_ref("task/5", newer)
        return HumanConfirmation(op, NOW)

    result = human(world, "review.approve", push_then_confirm, pr=40)  # type: ignore[arg-type]
    assert result.error is not None and result.error.code == "E_STATE_PR_HEAD_CHANGED"
    assert result.data["failed_step"] == "recheck_head"  # type: ignore[index]
    assert not world.forge.list_reviews(40)


@pytest.mark.parametrize(("change", "code"), [
    ({"draft": True}, "E_STATE_PR_DRAFT"),
    ({"number": 41}, "E_FORGE_NOT_FOUND"),
])
def test_prs_that_cannot_be_reviewed(world: World, change: dict[str, object], code: str) -> None:
    _pr(world, draft=bool(change.get("draft")))
    result = human(world, "review.approve", pr=int(change.get("number", 40)))  # type: ignore[call-overload]
    assert result.error is not None and result.error.code == code


def test_a_closed_pr_is_not_open(world: World) -> None:
    _pr(world)
    world.forge.update_pull_request(40, state="closed")
    result = human(world, "review.approve", pr=40)
    assert result.error is not None and result.error.code == "E_STATE_PR_NOT_OPEN"


def test_dry_run_and_the_barrier(world: World) -> None:
    _pr(world)
    typed = Typed()
    planned = valid(human(world, "review.approve", typed, pr=40, dry_run=True))
    assert planned.ok and planned.data["plan"][0]["target"] == "pr#40" and typed.seen == []  # type: ignore[index]
    mcp = world.run("review.approve", login="ana", pr=40)
    assert mcp.error is not None and mcp.error.code == "E_HUMAN_ONLY_NO_TTY"
    wrong = human(world, "review.approve", Typed("approve 41"), pr=40)
    assert wrong.error is not None and wrong.error.code == "E_HUMAN_ONLY_NOT_CONFIRMED"
    assert world.forge.calls.writes() == []
