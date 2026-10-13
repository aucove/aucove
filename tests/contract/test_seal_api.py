# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Lo que ``ci seal`` pide a la forja (08-forja §2, §4.2 y §10.1; 00-marco D-34 y D-45).

CT-SEAL-01 (``pulls_for_commit`` con el PR integrado y sin ``merged_by``, que da
``get_pull_request``, y ``successful_runs`` con evento y rama exactos) corre con el falso y
queda para la forja real en H3. Las demás pruebas son del falso: el tope de 65 536
caracteres por comentario, ``seed_run`` y el estado JSON.
"""

from __future__ import annotations

import pytest

from aucove import _meta
from aucove.forge.base import ChangedFile, ForgeValidationError
from aucove.forge.fake import MAX_COMMENT_CHARS, FakeForge
from tests.contract.conftest import Sandbox

SEAL = "seal.yml"


def _merged_pull(sandbox: Sandbox) -> tuple[int, str]:
    """PR de ``task/7`` integrado en la web por la cuenta lead; devuelve el número y el sha integrado."""
    fake = sandbox.fake
    assert fake is not None
    head = sandbox.content_commit({"sections/methods/methods.tex": b"% methods v2\n"})
    fake.seed_branch("task/7", head)
    pr = sandbox.make_pull_request(head_branch="task/7", author=sandbox.researcher_a_login)
    fake.admin_merge(pr.number)
    main = sandbox.forge.get_ref(sandbox.base_branch)
    assert main is not None
    return pr.number, main.sha


def test_ct_seal_01_merged_by_comes_only_from_get_pull_request(sandbox: Sandbox) -> None:
    """CT-SEAL-01: ``pulls_for_commit`` da el PR integrado sin ``merged_by``; ``get_pull_request``,
    quién lo integró."""
    if sandbox.fake is None:
        pytest.skip("H3: un PR integrado en la web en el sandbox real")
    number, merged_sha = _merged_pull(sandbox)
    (pr,) = sandbox.forge.pulls_for_commit(merged_sha)
    assert pr.number == number and pr.merged and pr.merged_by is None  # resumido, como en GitHub
    detail = sandbox.forge.get_pull_request(number)
    assert detail is not None and detail.merged_by == sandbox.lead_login
    # GET pulls (lista) y GET pulls?head= tampoco traen merged_by (08 §4.2)
    listed = [p for p in sandbox.forge.list_pull_requests(state="all") if p.number == number]
    assert [p.merged_by for p in listed] == [None]
    found = sandbox.forge.find_pull_request("task/7")
    assert found is not None and found.number == number and found.merged_by is None


def test_ct_seal_01_successful_runs_skips_failures_and_pull_request_target(sandbox: Sandbox) -> None:
    """CT-SEAL-01: dos pushes con un sello fallido entre ellos; solo cuentan los push exitosos."""
    fake = sandbox.fake
    if fake is None:
        pytest.skip("H3: runs reales de seal.yml en el sandbox")
    base = sandbox.base_sha
    _number, first = _merged_pull(sandbox)
    second = sandbox.content_commit({"plan.md": b"# Plan\n"}, parent=first)
    ok_base = fake.seed_run(SEAL, base, "push", "success")
    sandbox.tick()
    fake.seed_run(SEAL, first, "push", "failure")  # el sello del primer push falló
    sandbox.tick()
    ok_second = fake.seed_run(SEAL, second, "push", "success")
    sandbox.tick()
    fake.seed_run(SEAL, second, "pull_request_target", "success")  # punta de la base en head_sha
    fake.seed_run(SEAL, first, "push", None)  # en curso: no cuenta como sellado
    fake.seed_run(SEAL, first, "push", "success", branch="task/7")
    fake.seed_run("main-broken.yml", first, "push", "success")
    runs = sandbox.forge.successful_runs(SEAL, sandbox.base_branch, event="push")
    assert [r.id for r in runs] == [ok_second.id, ok_base.id]  # más recientes primero
    assert all(r.event == "push" and r.conclusion == "success" and r.status == "completed" for r in runs)
    assert [r.head_sha for r in runs] == [second, base]
    assert [r.id for r in sandbox.forge.successful_runs(SEAL, sandbox.base_branch, event="push", limit=1)] == [ok_second.id]
    prt = sandbox.forge.successful_runs(SEAL, sandbox.base_branch, event="pull_request_target")
    assert [r.head_sha for r in prt] == [second]
    assert sandbox.forge.successful_runs(SEAL, "task/7", event="push")[0].head_sha == first
    assert sandbox.forge.successful_runs(".github/workflows/" + SEAL, sandbox.base_branch, event="push") == runs


# -- solo el falso -----------------------------------------------------------------------------


def test_fake_successful_runs_limit_and_pages(fake_sandbox: Sandbox) -> None:
    fake = fake_sandbox.fake
    assert fake is not None
    for _ in range(250):
        fake.seed_run(SEAL, fake_sandbox.base_sha, "push", "success")
    fake.calls.clear()
    assert len(fake.successful_runs(SEAL, "main", event="push", limit=250)) == 250
    assert fake.calls.count("L", "successful_runs") == 3  # páginas de 100 (REQ-FORGE-8)
    assert len(fake.successful_runs(SEAL, "main", event="push")) == 100
    for bad in (0, 1001, True):
        with pytest.raises(ForgeValidationError):
            fake.successful_runs(SEAL, "main", event="push", limit=bad)  # type: ignore[arg-type]


def test_fake_seed_run_defaults(fake_sandbox: Sandbox) -> None:
    fake = fake_sandbox.fake
    assert fake is not None
    run = fake.seed_run(SEAL, fake_sandbox.base_sha, "pull_request_target", "success")
    assert (run.head_branch, run.event, run.status, run.name) == ("main", "pull_request_target", "completed", "seal")
    pending = fake.seed_run(SEAL, fake_sandbox.base_sha, "push", None)
    assert pending.status == "in_progress" and pending.conclusion is None
    with pytest.raises(ValueError):
        fake.seed_run(SEAL, fake_sandbox.base_sha, "", "success")
    with pytest.raises(ValueError):
        fake.seed_run(SEAL, "not-a-sha", "push", "success")


def test_fake_comment_limit_counts_the_op_marker(fake_sandbox: Sandbox) -> None:
    """GitHub admite 65 536 caracteres por comentario; el marcador op del adaptador cuenta (08 §10.1)."""
    fake = fake_sandbox.fake
    assert fake is not None
    issue = fake.seed_issue(title="limit")
    op_id = "ct-limit"
    fits = "x" * (MAX_COMMENT_CHARS - len("\n\n") - len(_marker(op_id)))
    comment = fake.create_comment(issue.number, fits, op_id=op_id)
    assert len(comment.body) == MAX_COMMENT_CHARS
    with pytest.raises(ForgeValidationError) as info:
        fake.create_comment(issue.number, fits + "x", op_id="ct-limit-2")
    assert info.value.http_status == 422
    assert len(fake.list_comments(issue.number)) == 1
    with pytest.raises(ForgeValidationError):
        fake.update_comment(comment.id, "y" * (MAX_COMMENT_CHARS + 1))
    assert fake.update_comment(comment.id, "y" * MAX_COMMENT_CHARS).body == "y" * MAX_COMMENT_CHARS


def _marker(op_id: str) -> str:
    return f"<!-- {_meta.NAME}:op v1 id={op_id} -->"


def test_fake_merged_by_is_seeded_and_saved(fake_sandbox: Sandbox) -> None:
    fake = fake_sandbox.fake
    assert fake is not None
    pr = fake.seed_pull_request(
        files=[ChangedFile("a.tex", "modified")], head_branch="task/9", head_sha="9" * 40,
        merged=True, merged_by="Bruno", merge_commit_sha="8" * 40,
    )
    assert pr.merged_by is None  # la siembra responde como una lista
    assert fake.get_pull_request(pr.number).merged_by == "bruno"  # type: ignore[union-attr]  # grafía de la cuenta
    restored = FakeForge.from_state(fake.to_state())
    assert restored.get_pull_request(pr.number).merged_by == "bruno"  # type: ignore[union-attr]
    assert [(p.number, p.merged_by) for p in restored.pulls_for_commit("8" * 40)] == [(pr.number, None)]
    state = fake.to_state()
    del state["pulls"][0]["merged_by"]  # un estado anterior a D-45
    assert FakeForge.from_state(state).get_pull_request(pr.number).merged_by is None  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        fake.seed_pull_request(head_branch="task/10", head_sha="7" * 40, merged_by="ana")  # sin integrar
