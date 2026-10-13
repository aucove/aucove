# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``review list`` y ``review show`` (05 §9.22 y §9.23; 06-permisos §2.3 y §3.2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aucove import _meta
from aucove.config.schemas import validate_result
from aucove.forge.base import ChangedFile
from aucove.forge.fake import Fault
from aucove.i18n import translator
from aucove.ops.ci_comment import render_build
from aucove.ops.result import Result
from tests.board_world import CI, World, at, marker, new_world
from tests.conftest import roles_fixture

NOTE = "memory/notes/task-5.md"


def valid(result: Result) -> Result:
    envelope = result.to_dict()
    assert validate_result(envelope) == [], json.dumps(envelope, ensure_ascii=False, indent=1)
    return result


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = new_world(tmp_path)
    w.task(5, claimant="carla", pr=40, reviewers=("ana",))
    w.task(6, claimant="carla", pr=41, draft=True)
    w.task(7, claimant="ana", pr=42, sections=("introduction",))
    return w


# -- review list ---------------------------------------------------------------------------------


def _listed(result: Result) -> dict[int, str]:
    assert result.ok, result.error
    return {r["pr"]: r["role_in_review"] for r in result.data["reviews"]}  # type: ignore[index]


def test_list_shows_what_waits_for_each_person(world: World) -> None:
    lead = valid(world.run("review.list", login="ana"))
    assert _listed(lead) == {40: "approver"}  # ni el borrador ni su propio PR
    item = lead.data["reviews"][0]  # type: ignore[index]
    assert (item["task"], item["author"], item["sections"], item["requested_from_me"]) == (5, "carla", ["methods"], True)
    assert item["title"]["trust"] == "untrusted" and item["age_h"] >= 0
    assert _listed(valid(world.run("review.list", login="bruno"))) == {40: "approver", 42: "approver"}
    assert _listed(valid(world.run("review.list", login="carla"))) == {}  # solo tiene PR propios
    everything = valid(world.run("review.list", login="ana", all=True))
    assert _listed(everything) == {40: "approver", 41: "approver", 42: "none"}


def test_all_is_for_lead_and_deputy(world: World) -> None:
    result = world.run("review.list", login="carla", all=True)
    assert result.error is not None and result.error.code == "E_PERM_ROLE_REQUIRED"


def test_list_offline_uses_the_cache(world: World) -> None:
    assert world.run("review.list", login="ana").ok
    world.forge.faults.add(Fault("list_issues", "unreachable"))
    cached = valid(world.run("review.list", login="ana"))
    assert _listed(cached) == {40: "approver"} and [w.code for w in cached.warnings] == ["W_NET_OFFLINE"]


def test_list_pages_with_a_cursor(world: World) -> None:
    for n in range(20, 34):
        world.task(n, claimant="carla", pr=100 + n, reviewers=("ana",))
    first = valid(world.run("review.list", login="ana", max_chars=2000))
    page = first.data["page"]  # type: ignore[index]
    assert page["next_cursor"] and page["returned"] < page["total"] == 15
    second = valid(world.run("review.list", login="ana", max_chars=40000, cursor=page["next_cursor"]))
    assert len(second.data["reviews"]) == page["total"] - page["returned"]  # type: ignore[index]


# -- review show ---------------------------------------------------------------------------------


@pytest.fixture
def pr(tmp_path: Path) -> World:
    """Tarea 5 de carla (methods) con un PR que trae, además, un archivo de otra parte."""
    w = new_world(tmp_path)
    head = w.task(5, claimant="carla")["head"]
    w.forge.seed_pull_request(
        number=40, head_branch="task/5", head_sha=head, author="carla", title="Métodos listos",
        body="Resumen <!-- oculto --> del PR.", requested_reviewers=["ana"], created_at=at(-30),
        labels=["flag:overlap"],
        files=[ChangedFile("sections/methods/methods.tex", "modified", additions=3, deletions=1),
               ChangedFile(NOTE, "added", additions=12),
               ChangedFile("sections/results/r.tex", "added", additions=2),
               ChangedFile(".github/CODEOWNERS", "modified", additions=1, deletions=1)],
    )
    w.forge.seed_workflow_run(".github/workflows/build.yml", branch="task/5", sha=head, conclusion="success",
                              event="pull_request")
    build = render_build(translator("es"), outcome="success", pdf_url="https://example.invalid/pdf/40",
                         diff_url=None, run_url=None, diagnostics=[])
    w.forge.seed_comment(40, author=CI, body=build)
    assessment = {"task": 5, "phase": "submission", "head_sha": head, "verdict": "possible",
                  "effective_verdict": "possible", "overlaps": [{"target": {"task": 7}, "kind": "scope",
                                                                 "reason": "Las dos tocan el muestreo."}],
                  "direction": None, "assessed_by": "carla"}
    w.forge.seed_comment(40, author="carla", body=marker("assessment", assessment))
    w.forge.seed_comment(40, author="dario", body=marker("assessment", {**assessment, "verdict": "clear"}))
    return w


def test_show_summarizes_the_pr_for_the_lead(pr: World) -> None:
    result = valid(pr.run("review.show", login="ana", pr=40))
    assert result.ok, result.error
    data = result.data
    assert data["task"] == 5 and data["pr"]["files_url"].endswith("/pull/40/files")  # type: ignore[index]
    assert data["pr"]["body"]["trust"] == "untrusted" and "oculto" not in data["pr"]["body"]["text"]  # type: ignore[index]
    files = {f["path"]: f for f in data["files"]}  # type: ignore[index]
    assert files["sections/methods/methods.tex"] == {"path": "sections/methods/methods.tex", "class": "section",
                                                     "owner": "carla", "additions": 3, "deletions": 1,
                                                     "allowed_for_task": True}
    assert files[NOTE]["class"] == "task" and files[NOTE]["allowed_for_task"] is True
    assert files[".github/CODEOWNERS"]["allowed_for_task"] is None  # generated: lo valida el CI
    assert files["sections/results/r.tex"]["allowed_for_task"] is False
    assert data["scope_violations"] == [{"path": "sections/results/r.tex", "reason_code": "E_PERM_OUTSIDE_TASK"}]  # type: ignore[index]
    assert data["ci"]["state"] == "success" and data["pdf_url"] == "https://example.invalid/pdf/40"  # type: ignore[index]
    assert [(a["phase"], a["overlap_verdict"]) for a in data["assessments"]] == [("submission", "possible")]  # type: ignore[index]
    assert data["flags"] == ["flag:overlap"]  # type: ignore[index]
    assert data["approval"] == {"profile": "enforced", "required_from": ["ana", "bruno"], "satisfied": False}  # type: ignore[index]
    actions = data["human_actions"]  # type: ignore[index]
    assert [a["action"] for a in actions] == ["approve", "request_changes"]
    assert actions[0]["web_url"].endswith("/files") and actions[0]["cli"] == f"{_meta.NAME} review approve 40"


def test_an_approval_on_the_head_satisfies_r1(pr: World) -> None:
    head = pr.forge.get_pull_request(40).head_sha  # type: ignore[union-attr]
    pr.forge.seed_review(40, author="bruno", state="APPROVED", commit_sha=head, submitted_at=at(1))
    data = valid(pr.run("review.show", login="ana", pr=40)).data
    assert data["approval"]["satisfied"] is True and data["reviews"][-1]["login"] == "bruno"  # type: ignore[index]


def test_who_may_see_the_review(pr: World) -> None:
    owner_view = valid(pr.run("review.show", login="bruno", pr=40))  # suplente
    assert owner_view.ok
    outsider = pr.run("review.show", login="dario", pr=40)
    assert outsider.error is not None and outsider.error.code == "E_PERM_NOT_REVIEWER"
    author = pr.run("review.show", login="carla", pr=40)
    assert author.error is not None and author.error.code == "E_PERM_NOT_REVIEWER"
    missing = pr.run("review.show", login="ana", pr=99)
    assert missing.error is not None and missing.error.code == "E_FORGE_NOT_FOUND"


def test_failing_ci_is_a_warning(pr: World) -> None:
    head = pr.forge.get_pull_request(40).head_sha  # type: ignore[union-attr]
    pr.forge.seed_workflow_run(".github/workflows/build.yml", branch="task/5", sha=head, conclusion="failure",
                               event="pull_request")
    result = valid(pr.run("review.show", login="ana", pr=40))
    assert "W_VALIDATION_CI_FAILING" in [w.code for w in result.warnings]


def test_the_owner_of_a_touched_part_may_only_request_changes(tmp_path: Path) -> None:
    roles = roles_fixture("basic").replace('title: "Introduction"\n    owner: ana', 'title: "Introduction"\n    owner: dario')
    w = new_world(tmp_path, extra_files={"roles.yaml": roles})
    head = w.task(5, claimant="carla")["head"]
    w.forge.seed_pull_request(number=40, head_branch="task/5", head_sha=head, author="carla",
                              files=[ChangedFile("sections/introduction/i.tex", "modified")])
    owner = valid(w.run("review.show", login="dario", pr=40))  # 06 §2.3: dueño de una parte tocada
    assert [a["action"] for a in owner.data["human_actions"]] == ["request_changes"]  # type: ignore[index]
    listed = valid(w.run("review.list", login="dario"))
    assert listed.data["reviews"] == []  # el tablero solo conoce las partes de la tarea (methods)


def test_show_pages_its_files(pr: World) -> None:
    head = pr.forge.get_pull_request(40).head_sha  # type: ignore[union-attr]
    files = [ChangedFile(f"sections/methods/f{i:03}.tex", "added", additions=1) for i in range(200)]
    pr.forge.seed_pull_request(number=60, head_branch="task/5", head_sha=head, author="carla", files=files)
    first = valid(pr.run("review.show", login="ana", pr=60))
    page = first.data["page"]  # type: ignore[index]
    assert page["total"] == 200 and page["next_cursor"] and page["returned"] < 200
    second = valid(pr.run("review.show", login="ana", pr=60, cursor=page["next_cursor"], max_chars=40000))
    assert second.data["files"][0]["path"] == f"sections/methods/f{page['returned']:03}.tex"  # type: ignore[index]


def test_a_pr_without_task_has_no_scope_verdict(pr: World) -> None:
    base = pr.forge.get_ref("main")
    assert base is not None
    pr.forge.seed_branch("fix/typo", base.sha)
    pr.forge.seed_pull_request(number=70, head_branch="fix/typo", author="carla",
                               files=[ChangedFile("sections/methods/methods.tex", "modified")])
    data = valid(pr.run("review.show", login="ana", pr=70)).data
    assert data["task"] is None and data["scope_violations"] == []  # type: ignore[index]
    assert data["files"][0]["allowed_for_task"] is None  # type: ignore[index]
