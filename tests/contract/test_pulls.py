# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Pull requests de lectura y revisiones (08-forja §4.2, §4.3, §10.3; 12-mvp §3.3).

En H2 el falso lee PR (por número, por rama, archivos, commits, revisiones y
``pulls_for_commit``). Crear y modificar PR y escribir revisiones llegaron al falso en H3
(``submit_review`` con ``review approve``), así que CT-PR-04 corre también con él. Los archivos de un PR son
el diff real de su cabeza (``GET pulls/{n}/files``), así que las pruebas
compartidas los construyen con commits que tienen contenido.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from aucove.domain.taskbody import render_task_body
from aucove.domain.tasks import Authz, derive_state
from aucove.forge.base import (
    ChangedFile,
    ForgeNotFound,
    ForgePermissionError,
    ForgeUnsupported,
    ForgeValidationError,
    NoCommitsBetween,
    PullRequestExists,
)
from aucove.forge.fake import UNSUPPORTED_METHODS, FakeForge
from aucove.ops import snapshot as snap
from tests.contract.conftest import BASE_FILES, LEAD, RESEARCHER_A, RESEARCHER_B, Sandbox

METHODS = "sections/methods/methods.tex"

# Para las pruebas solo con el falso, que siembran la respuesta de GET pulls/{n}/files.
FILES = [
    ChangedFile("sections/methods/methods.tex", "modified"),
    ChangedFile("sections/methods/figures/f1.pdf", "added"),
    ChangedFile("notes/new.md", "renamed", "notes/old.md"),
]


@dataclass
class _Confirmation:
    operation: str
    confirmed_at: datetime


def _branch_with_commit(sandbox: Sandbox, suffix: str) -> tuple[str, str]:
    branch = sandbox.branch(suffix)
    sha = sandbox.claim_commit()
    sandbox.forge.create_ref(branch, sha)
    return branch, sha


def _base_content(sandbox: Sandbox, path: str) -> bytes:
    found = sandbox.forge.get_file(path, sandbox.base_sha)
    assert found is not None
    return found.content


def _branch_with_content(sandbox: Sandbox, suffix: str) -> tuple[str, str]:
    """Rama con el commit de reclamo y un push que modifica, agrega y renombra (08 §4.4)."""
    branch, claim = _branch_with_commit(sandbox, suffix)
    head = sandbox.content_commit(
        {
            "tex/main.tex": b"\\documentclass{book}\n",
            "sections/methods/figures/f1.pdf": b"%PDF-1.7\n",
            METHODS: None,
            "sections/methods/methods-v2.tex": _base_content(sandbox, METHODS),
        },
        parent=claim,
    )
    sandbox.forge.update_ref(branch, head)
    return branch, head


EXPECTED_FILES: dict[str, tuple[str, str | None]] = {
    "tex/main.tex": ("modified", None),
    "sections/methods/figures/f1.pdf": ("added", None),
    "sections/methods/methods-v2.tex": ("renamed", METHODS),
}


def _files(files: Sequence[ChangedFile]) -> dict[str, tuple[str, str | None]]:
    # GitHub no documenta el orden de GET pulls/{n}/files: se compara como conjunto.
    return {f.path: (f.status, f.previous_path) for f in files}


def test_find_pull_request_by_branch(sandbox: Sandbox) -> None:
    """Buscar el PR por la rama task/<n> (12 §3.3; 05 §6.1)."""
    branch, sha = _branch_with_commit(sandbox, "task/41")
    assert sandbox.forge.find_pull_request(branch) is None
    pr = sandbox.make_pull_request(head_branch=branch, title="Task 41")
    found = sandbox.forge.find_pull_request(branch)
    assert found == pr
    assert found is not None and found.head_branch == branch and found.head_sha == sha
    assert found.base_branch == sandbox.base_branch and found.state == "open" and found.merged is False
    assert sandbox.forge.find_pull_request(branch, state="open") == pr
    assert sandbox.forge.find_pull_request(branch, state="closed") is None
    assert sandbox.forge.find_pull_request(sandbox.branch("task/404")) is None
    assert sandbox.forge.delete_ref(branch) is True


def test_get_and_list_pull_requests(sandbox: Sandbox) -> None:
    branch, _ = _branch_with_content(sandbox, "task/42")
    pr = sandbox.make_pull_request(head_branch=branch)
    assert sandbox.forge.get_pull_request(pr.number) == pr
    assert sandbox.forge.get_pull_request(999_999) is None
    listed = sandbox.forge.list_pull_requests(state="open", base=sandbox.base_branch)
    assert pr.number in [p.number for p in listed]
    assert pr.number not in [p.number for p in sandbox.forge.list_pull_requests(state="closed")]
    assert _files(sandbox.forge.list_pull_request_files(pr.number)) == EXPECTED_FILES
    assert sandbox.forge.delete_ref(branch) is True


def test_pull_request_files_follow_pushes(sandbox: Sandbox) -> None:
    """GET pulls/{n}/files es el diff de tres puntos de la cabeza actual: un push lo cambia (REQ-FORGE-59)."""
    branch, head = _branch_with_content(sandbox, "task/45")
    pr = sandbox.make_pull_request(head_branch=branch)
    assert _files(sandbox.forge.list_pull_request_files(pr.number)) == EXPECTED_FILES
    pushed = sandbox.content_commit(
        {"plan.md": b"# plan\n", "tex/main.tex": _base_content(sandbox, "tex/main.tex")}, parent=head
    )
    sandbox.forge.update_ref(branch, pushed)
    # GitHub tarda unos segundos en mover la cabeza del PR y su diff (00-marco D-68)
    moved = sandbox.eventually(lambda: sandbox.forge.get_pull_request(pr.number),
                               lambda p: p is not None and p.head_sha == pushed)
    assert moved is not None and moved.head_sha == pushed
    expected = {k: v for k, v in EXPECTED_FILES.items() if k != "tex/main.tex"}
    expected["plan.md"] = ("added", None)
    files = sandbox.eventually(lambda: _files(sandbox.forge.list_pull_request_files(pr.number)), lambda f: f == expected)
    assert files == expected
    assert sandbox.forge.delete_ref(branch) is True


def test_list_reviews_in_chronological_order(sandbox: Sandbox) -> None:
    """Listar revisiones (12 §3.3): orden cronológico (REQ-FORGE-59)."""
    sandbox.require_researcher_b()  # el autor y dos revisores distintos (00-marco D-67)
    branch, sha = _branch_with_commit(sandbox, "task/43")
    pr = sandbox.make_pull_request(head_branch=branch, author=sandbox.researcher_a_login)
    assert sandbox.forge.list_reviews(pr.number) == []
    now = sandbox.forge.identity().fetched_at
    later = sandbox.add_review(
        pr.number, author=sandbox.lead_login, state="APPROVED", submitted_at=now + timedelta(minutes=5)
    )
    earlier = sandbox.add_review(
        pr.number, author=sandbox.researcher_b_login, state="CHANGES_REQUESTED", submitted_at=now + timedelta(minutes=1)
    )
    reviews = sandbox.forge.list_reviews(pr.number)
    assert [r.id for r in reviews] == [earlier.id, later.id]
    assert reviews[1].state == "APPROVED" and reviews[1].commit_sha == sha
    assert reviews[0].author_id != reviews[1].author_id
    assert sandbox.forge.delete_ref(branch) is True


def test_pull_request_commits_and_pulls_for_commit(sandbox: Sandbox) -> None:
    branch, c1 = _branch_with_commit(sandbox, "task/44")
    c2 = sandbox.claim_commit(parent=c1)
    sandbox.forge.update_ref(branch, c2)
    pr = sandbox.make_pull_request(head_branch=branch)
    assert pr.head_sha == c2
    assert [c.sha for c in sandbox.forge.list_pull_request_commits(pr.number)] == [c1, c2]
    assert [p.number for p in sandbox.forge.pulls_for_commit(c2)] == [pr.number]
    assert [p.number for p in sandbox.forge.pulls_for_commit(c1)] == [pr.number]
    # El commit base puede tener su propio PR integrado en el sandbox real; este no lo contiene.
    assert pr.number not in [p.number for p in sandbox.forge.pulls_for_commit(sandbox.base_sha)]
    assert sandbox.forge.delete_ref(branch) is True


def test_ct_pr_01_draft_is_kept(sandbox: Sandbox) -> None:
    """CT-PR-01 (solo ct-public): draft=true se conserva y update_pull_request no lo cambia."""
    branch, _ = _branch_with_commit(sandbox, "pr-01")
    pr = sandbox.forge.create_pull_request(head=branch, base=sandbox.base_branch, title="t", body="", draft=True)
    assert pr.draft is True
    assert sandbox.forge.update_pull_request(pr.number, title="t2").draft is True


def test_ct_pr_02_no_commits_and_duplicate_head(sandbox: Sandbox) -> None:
    """CT-PR-02: sin commits, NoCommitsBetween; dos veces con la misma cabeza, PullRequestExists."""
    empty = sandbox.branch("pr-02-empty")
    sandbox.forge.create_ref(empty, sandbox.base_sha)
    with pytest.raises(NoCommitsBetween):
        sandbox.forge.create_pull_request(head=empty, base=sandbox.base_branch, title="t", body="")
    branch, _ = _branch_with_commit(sandbox, "pr-02")
    sandbox.forge.create_pull_request(head=branch, base=sandbox.base_branch, title="t", body="")
    with pytest.raises(PullRequestExists):
        sandbox.forge.create_pull_request(head=branch, base=sandbox.base_branch, title="t", body="")


def test_ct_pr_04_author_cannot_approve_own_pull_request(sandbox: Sandbox) -> None:
    """CT-PR-04: el autor se aprueba a sí mismo -> rechazo (422) -> ForgeValidationError."""
    branch, sha = _branch_with_commit(sandbox, "pr-04")
    pr = sandbox.make_pull_request(head_branch=branch, author=sandbox.lead_login)
    # Confirmación válida (06-permisos §6: operación «approve», de los últimos 120 s), para que
    # el rechazo sea el de la forja y no el de REQ-FORGE-10.
    now = sandbox.fake.clock.now() if sandbox.fake is not None else datetime.now(timezone.utc)
    confirmation = _Confirmation("approve", now)
    with pytest.raises(ForgeValidationError):
        sandbox.forge.submit_review(pr.number, event="APPROVE", body="ok", commit_sha=sha, confirmation=confirmation)


# --- Solo con el falso -------------------------------------------------------------


def _stub_calls(fake: FakeForge) -> dict[str, object]:
    return {"mark_ready_for_review": lambda: fake.mark_ready_for_review(1)}


def test_fake_reserved_writes_are_unsupported_stubs(fake: FakeForge) -> None:
    """Pasar a listo, reservado (08 §4.3), lanza ForgeUnsupported sin peticiones (los rulesets, en ForgeAdmin)."""
    calls = _stub_calls(fake)
    assert set(calls) == UNSUPPORTED_METHODS
    before = fake.to_state()
    fake.calls.clear()
    for name, call in calls.items():
        with pytest.raises(ForgeUnsupported) as info:
            call()  # type: ignore[operator]
        assert info.value.kind == "unsupported" and name in info.value.message
    assert len(fake.calls) == 0  # sin peticiones
    after = fake.to_state()
    assert {k: v for k, v in after.items() if k != "calls"} == {k: v for k, v in before.items() if k != "calls"}


def test_fake_submit_review_like_github(fake: FakeForge) -> None:
    """REQ-FORGE-10 y los 422 de GitHub: confirmación de la operación de los últimos 120 s, sin
    revisar el PR propio, con cuerpo para pedir cambios y solo en un PR abierto."""
    base = fake.to_state()["refs"]["heads/main"]
    head = fake.create_empty_commit(base, "c1")
    fake.create_ref("task/9", head)
    fake.seed_user("ana", permission="write")
    pr = fake.seed_pull_request(files=FILES, head_branch="task/9", author="carla")
    lead = fake.as_user("ana")
    now = fake.clock.now()
    with pytest.raises(ForgeValidationError):  # confirmación de otra operación
        lead.submit_review(pr.number, event="APPROVE", body="", commit_sha=head,
                           confirmation=_Confirmation("request-changes", now))
    with pytest.raises(ForgeValidationError):  # vencida
        lead.submit_review(pr.number, event="APPROVE", body="", commit_sha=head,
                           confirmation=_Confirmation("approve", now - timedelta(seconds=121)))
    with pytest.raises(ForgeValidationError):  # pedir cambios sin cuerpo
        lead.submit_review(pr.number, event="REQUEST_CHANGES", body=" ", commit_sha=head,
                           confirmation=_Confirmation("request-changes", now))
    fake.calls.clear()
    review = lead.submit_review(pr.number, event="APPROVE", body="", commit_sha=head,
                                confirmation=_Confirmation("approve", now))
    assert (review.author, review.state, review.commit_sha, review.submitted_at) == ("ana", "APPROVED", head, now)
    assert fake.calls.count("C", "submit_review") == 1
    assert [r.state for r in fake.list_reviews(pr.number)] == ["APPROVED"]
    fake.update_pull_request(pr.number, state="closed")
    with pytest.raises(ForgeValidationError):
        lead.submit_review(pr.number, event="APPROVE", body="", commit_sha=head,
                           confirmation=_Confirmation("approve", now))


def test_fake_update_pull_request_keeps_draft(fake: FakeForge) -> None:
    """PATCH pulls/{n} cambia título, cuerpo y estado, nunca draft (REQ-FORGE-59); lo usa release."""
    base = fake.to_state()["refs"]["heads/main"]
    fake.create_ref("task/9", fake.create_empty_commit(base, "c1"))
    pr = fake.seed_pull_request(files=FILES, head_branch="task/9", draft=True)
    closed = fake.update_pull_request(pr.number, state="closed", title="Nuevo")
    assert (closed.state, closed.title, closed.draft) == ("closed", "Nuevo", True)
    with pytest.raises(ForgeValidationError):
        fake.update_pull_request(pr.number, state="merged")
    with pytest.raises(ForgePermissionError):
        fake.as_user("zoe").update_pull_request(pr.number, state="open")  # sin push ni autoría


def test_fake_request_reviewers_like_github(fake: FakeForge) -> None:
    """POST requested_reviewers agrega al conjunto; 422 al autor o a quien no es colaborador (lo usa submit)."""
    base = fake.to_state()["refs"]["heads/main"]
    fake.create_ref("task/9", fake.create_empty_commit(base, "c1"))
    pr = fake.seed_pull_request(files=FILES, head_branch="task/9", author=RESEARCHER_A)
    fake.seed_user(LEAD, permission="write")
    asked = fake.request_reviewers(pr.number, [LEAD])
    assert asked.requested_reviewers == (LEAD,)
    assert fake.request_reviewers(pr.number, [LEAD]).requested_reviewers == (LEAD,)  # idempotente
    with pytest.raises(ForgeValidationError):
        fake.request_reviewers(pr.number, [RESEARCHER_A])  # el autor
    with pytest.raises(ForgeValidationError):
        fake.request_reviewers(pr.number, ["nadie-conocido"])


def test_fake_push_moves_open_pull_request_head(fake: FakeForge) -> None:
    base = fake.to_state()["refs"]["heads/main"]
    c1 = fake.create_empty_commit(base, "c1")
    fake.create_ref("task/9", c1)
    pr = fake.seed_pull_request(files=FILES, head_branch="task/9")
    c2 = fake.create_empty_commit(c1, "c2")
    fake.update_ref("task/9", c2)
    moved = fake.get_pull_request(pr.number)
    assert moved is not None and moved.head_sha == c2


def test_fake_admin_merge_closes_issue_only_on_default_branch(fake: FakeForge) -> None:
    """REQ-FORGE-59: «Closes #n» cierra el Issue solo al integrar en la rama por defecto."""
    base = fake.to_state()["refs"]["heads/main"]
    task = fake.seed_issue(title="task", labels=["task"])
    other = fake.seed_issue(title="other")
    head = fake.seed_commit({"tex/main.tex": b"new\n", "roles.yaml": b"version: 1\nlead: ana\n"}, parents=[base])
    fake.seed_branch("task/1", head)
    fake.seed_branch("release", base)
    side = fake.seed_pull_request(files=FILES, head_branch="task/1", base_branch="release", body=f"Fixes #{other.number}")
    fake.admin_merge(side.number)
    assert fake.get_issue(other.number).state == "open"  # type: ignore[union-attr]

    pr = fake.seed_pull_request(files=FILES, head_branch="task/1", body=f"Work.\n\nCloses #{task.number}")
    fake.admin_merge(pr.number)
    merged = fake.get_pull_request(pr.number)
    assert merged is not None and merged.merged and merged.state == "closed" and merged.merged_at is not None
    closed = fake.get_issue(task.number)
    assert closed is not None and closed.state == "closed" and closed.state_reason == "completed"
    main = fake.get_ref("main")
    assert main is not None and main.sha != base
    file = fake.get_file("tex/main.tex", "main")
    assert file is not None and file.content == b"new\n"
    assert [p.number for p in fake.pulls_for_commit(main.sha)] == [pr.number]
    activity = fake.list_ref_activity("main")
    assert activity is not None and activity[-1].activity_type == "pr_merge"
    # Los commits del PR integrado se conservan.
    assert [c.sha for c in fake.list_pull_request_commits(pr.number)] == [head]
    with pytest.raises(ValueError):
        fake.admin_merge(pr.number)


def test_fake_pull_request_files_go_up_to_3000(fake: FakeForge) -> None:
    """REQ-FORGE-8: los archivos de un PR admiten 30 páginas (los 3000 de GitHub); más, ForgeValidationError."""
    base = fake.to_state()["refs"]["heads/main"]
    fake.seed_branch("task/8", fake.create_empty_commit(base, "c1"))
    full = fake.seed_pull_request(files=[ChangedFile(f"data/{i}.csv", "added") for i in range(3000)],
                                  head_branch="task/8")
    assert len(fake.list_pull_request_files(full.number)) == 3000
    over = fake.seed_pull_request(files=[ChangedFile(f"data/{i}.csv", "added") for i in range(3001)],
                                  head_branch="task/8", state="closed")
    with pytest.raises(ForgeValidationError):
        fake.list_pull_request_files(over.number)


def test_fake_pull_request_errors(fake: FakeForge) -> None:
    issue = fake.seed_issue(title="not a pr")
    assert fake.get_pull_request(issue.number) is None
    with pytest.raises(ForgeNotFound):
        fake.list_reviews(issue.number)
    with pytest.raises(ForgeNotFound):
        fake.list_pull_request_files(777)
    with pytest.raises(ForgeValidationError):
        fake.list_pull_requests(state="bogus")
    with pytest.raises(ValueError):
        fake.seed_pull_request(files=[], head_branch="no-such-branch")


def test_fake_fork_pull_requests(fake: FakeForge) -> None:
    """PR desde forks (04-tareas §1.1 y REQ-TASK-5; 08 §2 y §4.2).

    ``GET pulls?head={o}:{b}`` (``find_pull_request``) solo ve PR cuya cabeza es la
    rama de este repositorio; ``GET pulls`` (``list_pull_requests``) los lista todos,
    con ``same_repo``. La instantánea de la tarea registra ``fork_pr_ignored`` con el
    falso tal cual, sin envoltorios, y el dato sobrevive a guardar y restaurar.
    """
    base = fake.to_state()["refs"]["heads/main"]
    body = render_task_body(
        {"summary": "Datos.", "sections": ["methods"], "plan_refs": [], "done": ["Compila."]}, "es"
    )
    task = fake.seed_issue(title="Tarea", body=body, labels=["task"], author=RESEARCHER_B)
    branch = f"task/{task.number}"
    own_head = fake.seed_commit({**BASE_FILES, METHODS: b"own\n"}, parents=[base], message="own")
    fake.seed_branch(branch, own_head)
    own = fake.seed_pull_request(head_branch=branch, author=RESEARCHER_B)
    fake.clock.advance(60)  # el PR del fork es el más reciente con esa rama cabeza
    fork_head = fake.seed_commit({**BASE_FILES, METHODS: b"fork\n"}, parents=[base], message="fork")
    fork = fake.seed_pull_request(head_branch=branch, head_sha=fork_head, author="atacante", same_repo=False)
    assert (own.same_repo, fork.same_repo) == (True, False)
    assert fork.head_branch == own.head_branch == branch

    # find_pull_request: el filtro {o}:{b} deja fuera el PR del fork.
    assert fake.find_pull_request(branch) == own
    assert fake.find_pull_request(branch, state="open") == own
    assert fake.find_pull_request(branch, state="closed") is None
    # list_pull_requests y get_pull_request los ven todos, con su same_repo.
    listed = {p.number: p.same_repo for p in fake.list_pull_requests(state="all")}
    assert listed == {own.number: True, fork.number: False}
    assert fake.get_pull_request(fork.number) == fork

    # La instantánea ve el fork y derive_state lo ignora y lo reporta.
    authz = Authz(LEAD, RESEARCHER_A, frozenset({"github-actions[bot]"}), {"methods": RESEARCHER_B})
    evidence = snap.collect_task(fake, task.number, authz, base_branch=fake.default_branch)
    assert evidence is not None
    assert {p.number: p.same_repo for p in evidence.snapshot.pulls} == {own.number: True, fork.number: False}
    derived = derive_state(evidence.snapshot)
    assert "fork_pr_ignored" in derived.anomalies
    assert derived.pr == own.number

    # El estado JSON guarda same_repo y la restauración lo conserva.
    state = json.loads(json.dumps(fake.to_state()))
    saved = {p["number"]: p["same_repo"] for p in state["pulls"]}
    assert saved == {own.number: True, fork.number: False}
    restored = FakeForge.from_state(state)
    assert restored.get_pull_request(fork.number) == fork
    assert restored.find_pull_request(branch) == own
    assert restored.to_state()["pulls"] == state["pulls"]


def test_fake_pr_labels_and_comments_share_issue_endpoints(fake: FakeForge) -> None:
    """Etiquetas y comentarios de un PR usan los endpoints de Issues (Q-FORGE-11)."""
    base = fake.to_state()["refs"]["heads/main"]
    fake.seed_branch("task/2", fake.create_empty_commit(base, "c"))
    pr = fake.seed_pull_request(files=FILES, head_branch="task/2")
    assert fake.add_labels(pr.number, ["needs-review"]) == ("needs-review",)
    comment = fake.create_comment(pr.number, "hi", op_id="pr-c-1")
    assert "/pull/" in comment.url
    assert fake.list_comments(pr.number) == [comment]
    assert fake.get_pull_request(pr.number).labels == ("needs-review",)  # type: ignore[union-attr]
