# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Instantánea de tarea armada con la forja (04-tareas §1.1, §1.4, §1.5; ops/snapshot.py)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from aucove import _meta
from aucove.config.roles import parse_roles
from aucove.domain.permissions import Task
from aucove.domain.taskbody import parse_task_body, render_task_body
from aucove.domain.tasks import Authz
from aucove.forge.base import NULL_SHA, ForgeError, LabelEvent, RefActivity, RepoRef
from aucove.forge.fake import FakeClock, FakeForge, Fault
from aucove.ops import snapshot as snap
from tests.conftest import roles_fixture

T0 = datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc)
CI = "github-actions[bot]"
AUTHZ = Authz(
    lead="ana",
    deputy="bruno",
    ci_actors=frozenset({CI}),
    section_owner={"introduction": "ana", "methods": "carla", "results": "bruno"},
)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def marker(kind: str, data: dict[str, Any], *, attrs: str = "") -> str:
    return f"<!-- {_meta.NAME}:{kind} v1{attrs}\n{json.dumps(data)}\n-->"


def new_forge() -> FakeForge:
    return FakeForge(RepoRef("example-org", "demo"), viewer="ana", clock=FakeClock(at(600)))


def seed_task(
    forge: FakeForge,
    *,
    number: int = 12,
    claimant: str = "carla",
    sections: tuple[str, ...] = ("methods",),
    with_ref: bool = True,
    with_pr: bool = True,
    body: str | None = None,
    labels: tuple[str, ...] = ("task",),
) -> dict[str, Any]:
    """Tarea aceptada por el lead y reclamada por ``claimant``, con su PR abierto."""
    base = forge.seed_commit({"README.md": b"demo\n"}, [], "base", "ana", date=at(0))
    forge.seed_branch("main", base)
    if body is None:
        body = render_task_body(
            {"summary": "Datos.", "sections": list(sections), "plan_refs": [], "done": ["Compila."]}, "es"
        )
    spec_hash = str(parse_task_body(body)["spec_hash"])
    events = [
        LabelEvent(1, "labeled", "task", claimant, at(10)),
        LabelEvent(2, "labeled", "state:proposed", claimant, at(10)),
        LabelEvent(3, "unlabeled", "state:proposed", "ana", at(20)),
    ]
    forge.seed_issue(
        number=number, title="Tarea", body=body, labels=list(labels), assignees=[claimant], author=claimant,
        created_at=at(10), events=events,
    )
    out: dict[str, Any] = {"base": base, "spec_hash": spec_hash, "claim": None, "head": None}
    if with_ref:
        claim = forge.seed_commit({"README.md": b"demo\n"}, [base], f"{_meta.CLAIM_SUBJECT}{number}", claimant, date=at(30))
        head = forge.seed_commit(
            {"README.md": b"demo\n", "sections/methods/methods.tex": b"x\n"}, [claim], "trabajo", claimant, date=at(40)
        )
        forge.seed_branch(f"task/{number}", head)
        forge.seed_ref_activity(
            f"task/{number}",
            [
                RefActivity("branch_creation", claimant, at(30), NULL_SHA, claim),
                RefActivity("push", claimant, at(40), claim, head),
            ],
        )
        data = {"task": number, "login": claimant, "claim_sha": claim, "base_sha": base,
                "sections": list(sections), "spec_hash": spec_hash, "claimed_at": "2026-09-28T09:30:00Z",
                "renewed_at": None}
        forge.seed_comment(number, author=claimant, body=marker("claim", data), created_at=at(31))
        out.update(claim=claim, head=head)
        if with_pr:
            forge.seed_pull_request(
                files=[], number=50, head_branch=f"task/{number}", head_sha=head, author=claimant, created_at=at(50)
            )
    return out


# -- ramas, asunto y marcadores -------------------------------------------------------


@pytest.mark.parametrize(
    ("branch", "number"),
    [
        ("task/12", 12),
        ("task/1", 1),
        ("task/1234567890", 1234567890),
        ("task/0", None),
        ("task/012", None),
        ("task/12a", None),
        ("task/12/x", None),
        ("Task/12", None),
        ("task/12345678901", None),
        ("feature/12", None),
        ("", None),
        (None, None),
    ],
)
def test_task_number(branch: str | None, number: int | None) -> None:
    assert snap.task_number(branch) == number


def test_task_branch_and_claim_subject_come_from_the_name_module() -> None:
    assert snap.task_branch(7) == "task/7"
    assert snap.task_number(snap.task_branch(7)) == 7
    assert snap.claim_subject(7) == _meta.CLAIM_SUBJECT + "7"


def test_parse_markers_reads_v1_markers_with_json_in_order() -> None:
    body = "Texto.\n" + marker("claim", {"login": "carla"}) + "\nmás\n" + marker("assessment", {"phase": "claim"})
    assert snap.parse_markers(body) == [("claim", {"login": "carla"}), ("assessment", {"phase": "claim"})]


def test_parse_markers_accepts_crlf_and_attributes() -> None:
    body = marker("expiry", {"run_id": 3}, attrs=" op=x-1").replace("\n", "\r\n")
    assert snap.parse_markers(body) == [("expiry", {"run_id": 3})]


@pytest.mark.parametrize(
    "body",
    [
        f"<!-- {_meta.NAME}:claim v2\n{{}}\n-->",  # otra versión
        f"<!-- {_meta.NAME}:op v1 id=abc -->",  # marcador de una línea (op)
        f"<!-- {_meta.NAME}:unknown v1\n{{}}\n-->",  # tipo desconocido
        f"<!-- {_meta.NAME}:claim v1\nno es json\n-->",
        f"<!-- {_meta.NAME}:claim v1\n[1, 2]\n-->",  # JSON que no es objeto
        "<!-- otro:claim v1\n{}\n-->",  # otro prefijo
        f"<!-- {_meta.NAME}:claim v1\n{{}}\nsin cierre",
    ],
)
def test_parse_markers_ignores_what_is_not_a_task_marker(body: str) -> None:
    assert snap.parse_markers(body) == []


def test_authz_from_roles_uses_the_base_roles() -> None:
    roles, findings = parse_roles(roles_fixture("basic"))
    assert roles is not None and not findings
    authz = snap.authz_from_roles(roles)
    assert (authz.lead, authz.deputy) == ("ana", "bruno")
    assert authz.ci_actors == snap.CI_ACTORS == frozenset({CI})
    assert authz.section_owner == {"introduction": "ana", "methods": "carla", "results": "bruno"}


# -- collect_task ---------------------------------------------------------------------


def test_collect_task_builds_the_snapshot_of_04_1_1() -> None:
    forge = new_forge()
    seeded = seed_task(forge)
    forge.seed_review(50, author="ana", state="COMMENTED", submitted_at=at(55))
    forge.seed_comment(50, author="carla", body=marker("submission", {"rounds": []}), created_at=at(51))
    ev = snap.collect_task(forge, 12, AUTHZ, base_branch="main")
    assert ev is not None
    s = ev.snapshot
    assert (s.number, s.issue_author, s.issue_state, s.state_reason) == (12, "carla", "open", None)
    assert s.labels == frozenset({"task"})
    assert [(e.label, e.action, e.actor, e.at) for e in s.label_events] == [
        ("task", "labeled", "carla", at(10)),
        ("state:proposed", "labeled", "carla", at(10)),
        ("state:proposed", "unlabeled", "ana", at(20)),
    ]
    assert s.assignees == ("carla",)
    assert s.sections == ("methods",)
    assert s.spec_hash == seeded["spec_hash"] == ev.body.spec_hash
    assert [(m.kind, m.author, m.ci_verified) for m in s.issue_markers] == [("claim", "carla", False)]
    assert s.ref is not None
    assert (s.ref.head_sha, s.ref.claim_commit_sha, s.ref.claim_commit_author) == (
        seeded["head"], seeded["claim"], "carla"
    )
    assert (s.ref.created_by, s.ref.last_push_at) == ("carla", at(40))
    (pull,) = s.pulls
    assert (pull.number, pull.state, pull.merged, pull.draft, pull.author, pull.base_ref) == (
        50, "open", False, False, "carla", "main"
    )
    assert pull.same_repo is True
    assert pull.created_at == at(50)  # PullRequest no trae created_at: se usa updated_at
    assert [(r.author, r.state, r.submitted_at) for r in pull.reviews] == [("ana", "COMMENTED", at(55))]
    assert [m.kind for m in pull.markers] == ["submission"]
    assert s.base_branch == "main" and s.authz == AUTHZ
    assert ev.activity is not None and [a.activity_type for a in ev.activity] == ["branch_creation", "push"]
    assert ev.issue.number == 12


def test_to_permission_task_uses_derive_state_and_effective_sections() -> None:
    forge = new_forge()
    seed_task(forge, sections=("methods", "results"))
    ev = snap.collect_task(forge, 12, AUTHZ, base_branch="main")
    assert ev is not None
    task, derived = snap.to_permission_task(ev.snapshot)
    assert task == Task(12, "carla", "in_review", frozenset({"methods", "results"}))
    assert (derived.state, derived.owner, derived.owner_source, derived.pr) == ("in_review", "carla", "activity", 50)


def test_claimed_without_pr_and_build_task_snapshot() -> None:
    forge = new_forge()
    seed_task(forge, with_pr=False)
    s = snap.build_task_snapshot(forge, 12, AUTHZ, base_branch="main")
    assert s is not None and s.pulls == ()
    task, _ = snap.to_permission_task(s)
    assert task.state == "claimed"
    assert snap.build_task_snapshot(forge, 99, AUTHZ, base_branch="main") is None


def test_effective_sections_intersect_the_claim_and_the_current_spec() -> None:
    forge = new_forge()
    seeded = seed_task(forge, with_pr=False, sections=("methods",))
    # Después del reclamo, alguien edita el Issue y agrega results: no amplía el reclamo.
    body = render_task_body({"summary": "Datos.", "sections": ["methods", "results"], "plan_refs": [], "done": ["Ok."]}, "es")
    forge.as_user("ana").update_issue(12, body=body)
    ev = snap.collect_task(forge, 12, AUTHZ, base_branch="main")
    assert ev is not None and ev.snapshot.spec_hash != seeded["spec_hash"]
    task, derived = snap.to_permission_task(ev.snapshot)
    assert task.sections == frozenset({"methods"})
    assert "spec_changed_after_claim" in derived.anomalies


def test_cancelled_task_keeps_the_ref_owner_as_claimant() -> None:
    forge = new_forge()
    seed_task(forge, with_pr=False)
    forge.as_user("ana").update_issue(12, state="closed", state_reason="not_planned")
    s = snap.build_task_snapshot(forge, 12, AUTHZ, base_branch="main")
    assert s is not None
    task, derived = snap.to_permission_task(s)
    assert (derived.state, derived.owner) == ("cancelled", None)
    assert (task.state, task.claimant, task.sections) == ("cancelled", "carla", frozenset({"methods"}))


@pytest.mark.parametrize(
    "setup",
    ["missing", "no_task_label", "pull_request_number"],
)
def test_collect_task_returns_none_when_it_is_not_a_task_issue(setup: str) -> None:
    forge = new_forge()
    if setup == "no_task_label":
        seed_task(forge, labels=("bug",), with_ref=False)
    elif setup == "pull_request_number":
        seed_task(forge)
        assert snap.collect_task(forge, 50, AUTHZ, base_branch="main") is None
        return
    assert snap.collect_task(forge, 12, AUTHZ, base_branch="main") is None


def test_task_label_is_matched_without_case() -> None:
    forge = new_forge()
    seed_task(forge, labels=("Task",), with_ref=False)
    assert snap.collect_task(forge, 12, AUTHZ, base_branch="main") is not None


def test_without_ref_there_is_no_activity_read() -> None:
    forge = new_forge()
    seed_task(forge, with_ref=False)
    ev = snap.collect_task(forge, 12, AUTHZ, base_branch="main")
    assert ev is not None and ev.snapshot.ref is None and ev.activity is None
    assert forge.calls.count(method="list_ref_activity") == 0
    task, derived = snap.to_permission_task(ev.snapshot)
    assert (derived.state, task.claimant) == ("open", None)


def test_activity_not_given_by_the_forge_is_none() -> None:
    forge = new_forge()
    seed_task(forge)
    forge.faults.add(Fault(method="list_ref_activity", kind="http_404"))
    ev = snap.collect_task(forge, 12, AUTHZ, base_branch="main")
    assert ev is not None and ev.activity is None
    assert ev.snapshot.ref is not None
    assert (ev.snapshot.ref.created_by, ev.snapshot.ref.last_push_at) == (None, None)
    task, derived = snap.to_permission_task(ev.snapshot)
    assert (task.claimant, derived.owner_source) == ("carla", "claim_commit")


@pytest.mark.parametrize("method", ["get_issue", "list_ref_activity", "list_comments", "compare"])
def test_forge_errors_propagate(method: str) -> None:
    forge = new_forge()
    seed_task(forge)
    forge.faults.add(Fault(method=method, kind="http_5xx"))
    with pytest.raises(ForgeError):
        snap.collect_task(forge, 12, AUTHZ, base_branch="main")


def test_last_claim_commit_wins_and_its_absence_is_none() -> None:
    forge = new_forge()
    seeded = seed_task(forge, with_pr=False)
    again = forge.seed_commit({"README.md": b"demo\n"}, [seeded["head"]], f"{_meta.CLAIM_SUBJECT}12\n\ncuerpo", "bruno", date=at(45))
    forge.seed_branch("task/12", again)
    s = snap.build_task_snapshot(forge, 12, AUTHZ, base_branch="main")
    assert s is not None and s.ref is not None
    assert (s.ref.claim_commit_sha, s.ref.claim_commit_author) == (again, "bruno")
    evidence = snap.collect_task(forge, 12, AUTHZ, base_branch="main")
    assert evidence is not None and evidence.claim_commit is not None
    assert (evidence.claim_commit.sha, evidence.claim_commit.author_date) == (again, at(45))

    other = new_forge()
    base = other.seed_commit({"a": b"1"}, [], "base", "ana", date=at(0))
    other.seed_branch("main", base)
    plain = other.seed_commit({"a": b"2"}, [base], "sin reclamo", "carla", date=at(5))
    assert snap._claim_commit(other, "main", "x", 12) == (None, None)  # la rama no existe: 404
    other.seed_branch("task/12", plain)
    claim, commits = snap._claim_commit(other, "main", "task/12", 12)
    assert claim is None and [c.sha for c in commits or ()] == [plain]


def test_activity_helpers() -> None:
    acts = [
        RefActivity("branch_creation", "carla", at(30), NULL_SHA, "a" * 40),
        RefActivity("force_push", "carla", at(50), "a" * 40, "b" * 40),
        RefActivity("pr_merge", "ana", at(90), "b" * 40, "c" * 40),
        RefActivity("branch_deletion", "ana", at(91), "c" * 40, NULL_SHA),
        RefActivity("branch_creation", "bruno", at(92), NULL_SHA, "d" * 40),
    ]
    assert snap._created_by(acts) == "bruno"  # la última creación
    assert snap._last_push_at(acts) == at(92)
    assert snap._last_push_at(acts[:3]) == at(50)  # pr_merge no es un push
    assert snap._created_by(acts[1:4]) is None
    assert snap._last_push_at(acts[2:4]) is None
    assert snap._created_by(None) is None and snap._last_push_at([]) is None


def test_form_labels_extend_the_canonical_table() -> None:
    forge = new_forge()
    # «Partes» ya es canónica (03 §8.3, D-43): se usa una etiqueta que solo trae el task.yml.
    body = "### Bloques\n\nmethods\n\n### Resumen\n\nDatos.\n"
    seed_task(forge, body=body, with_pr=False)
    ev = snap.collect_task(forge, 12, AUTHZ, base_branch="main", form_labels={"Bloques": "sections"})
    assert ev is not None and ev.snapshot.sections == ("methods",)
    plain = snap.collect_task(forge, 12, AUTHZ, base_branch="main")
    assert plain is not None and plain.snapshot.sections == ()


def test_pull_uses_created_at_and_same_repo_when_the_adapter_gives_them() -> None:
    forge = new_forge()
    seed_task(forge)
    pr = forge.get_pull_request(50)
    assert pr is not None
    # PullRequest ya puede traer same_repo (y quizá created_at): el valor del caso reemplaza al suyo.
    fields = {k: getattr(pr, k) for k in pr.__slots__}
    fields.update(created_at=at(1), same_repo=False)
    rich = SimpleNamespace(**fields)
    pull = snap._pull(forge, rich, lambda _a, _d: False)  # type: ignore[arg-type]
    assert (pull.created_at, pull.same_repo) == (at(1), False)


# -- marcadores expiry de CI (04 §1.5) ------------------------------------------------


def _expiry_setup(**run: Any) -> tuple[FakeForge, int]:
    forge = new_forge()
    seeded = seed_task(forge, with_pr=False)
    options = {"branch": "main", "event": "schedule", "name": None, **run}
    wf = forge.seed_workflow_run(
        "expire-claims.yml", branch=options["branch"], sha=seeded["base"], conclusion="success",
        event=options["event"], name=options["name"], actor=CI,
    )
    return forge, wf.id


def _ci_verified(forge: FakeForge, data: dict[str, Any], *, author: str = CI, default_branch: str | None = None) -> bool:
    forge.seed_comment(12, author=author, body=marker("expiry", data), created_at=at(70))
    ev = snap.collect_task(forge, 12, AUTHZ, base_branch="main", default_branch=default_branch)
    assert ev is not None
    (m,) = [m for m in ev.snapshot.issue_markers if m.kind == "expiry"]
    return m.ci_verified


def test_expiry_marker_of_the_expire_claims_run_is_verified() -> None:
    forge, run_id = _expiry_setup()
    assert _ci_verified(forge, {"run_id": run_id, "head_sha": "x"}) is True


@pytest.mark.parametrize(
    ("run", "data"),
    [
        ({"branch": "task/12"}, None),  # run en otra rama
        ({"event": "push"}, None),  # evento que no es schedule ni workflow_dispatch
        ({"name": "otro"}, None),  # otro workflow
        ({}, {"run_id": "7"}),
        ({}, {"run_id": True}),
        ({}, {"run_id": 0}),
        ({}, {}),
        ({}, {"run_id": 999999}),  # run inexistente (404)
    ],
)
def test_expiry_marker_not_verified(run: dict[str, Any], data: dict[str, Any] | None) -> None:
    forge, run_id = _expiry_setup(**run)
    assert _ci_verified(forge, data if data is not None else {"run_id": run_id}) is False


def test_expiry_marker_of_a_person_is_not_ci_verified() -> None:
    forge, run_id = _expiry_setup()
    assert _ci_verified(forge, {"run_id": run_id}, author="ana") is False
    assert forge.calls.count(method="get_workflow_run") == 0


def test_expiry_runs_are_checked_on_the_default_branch() -> None:
    forge, run_id = _expiry_setup(branch="trunk")
    assert _ci_verified(forge, {"run_id": run_id}, default_branch="trunk") is True


def test_expiry_verifier_memoizes_by_run_id_and_prefers_the_path() -> None:
    calls: list[int] = []

    class _Runs:
        def get_workflow_run(self, run_id: int) -> SimpleNamespace:
            calls.append(run_id)
            return SimpleNamespace(path=snap.EXPIRE_WORKFLOW, name="otro nombre", head_branch="main", event="workflow_dispatch")

    verify = snap._expiry_verifier(_Runs(), "main", AUTHZ)  # type: ignore[arg-type]
    assert verify(CI, {"run_id": 5}) is True
    assert verify(CI, {"run_id": 5}) is True
    assert calls == [5]

    class _OtherPath(_Runs):
        def get_workflow_run(self, run_id: int) -> SimpleNamespace:
            return SimpleNamespace(path=".github/workflows/build.yml", name=snap.EXPIRE_WORKFLOW_NAME,
                                   head_branch="main", event="schedule")

    assert snap._expiry_verifier(_OtherPath(), "main", AUTHZ)(CI, {"run_id": 6}) is False  # type: ignore[arg-type]
