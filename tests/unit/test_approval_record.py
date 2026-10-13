# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``derive_approval_record`` (11-trazabilidad §4.2 y §4.3; REQ-TRACE-45 y REQ-TRACE-47).

La función es pura: se prueba con datos sueltos y, como pide REQ-TRACE-45, con lo que devuelve el
adaptador falso (``reviews.approval_record`` lee el PR, sus archivos y sus revisiones).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aucove import _meta
from aucove.domain.approval_record import Fetched, RolesAtBase, derive_approval_record, record_schema
from aucove.domain.approvals import ApprovalPolicy
from aucove.forge.base import ChangedFile
from aucove.ops.reviews import approval_record
from tests.board_world import World, at, new_world

NOW = datetime(2026, 10, 2, 18, 20, tzinfo=timezone.utc)
HEAD = "a" * 40
POLICY = ApprovalPolicy(approvers=("ana", "bruno"), editor=None, individual=False, latex=True)
ROLES = RolesAtBase("5d1e0aa12345", {"ana": "lead", "bruno": "deputy", "carla": "researcher"}, POLICY, touches=True)
FETCHED = Fetched("github", "https://api.github.com/repos/org/estudio/pulls/57", NOW + timedelta(days=60))


@dataclass(frozen=True)
class Pr:
    number: int = 57
    title: str = "Métodos: diseño muestral"
    author: str = "carla"
    base_branch: str = "main"
    head_branch: str = "task/42"
    head_sha: str = HEAD
    merged_at: datetime | None = NOW
    merged_by: str | None = "ana"
    merge_commit_sha: str | None = "c" * 40
    labels: tuple[str, ...] = ("flag:overlap",)


@dataclass(frozen=True)
class Rv:
    id: int
    author: str
    state: str
    commit_sha: str | None = HEAD
    submitted_at: datetime | None = None


def derive(pr: Pr, reviews: list[Rv], **kw: object) -> dict[str, object]:
    args: dict[str, object] = {"roles": ROLES, "profile": "enforced", "merge_audit": "compliant", "fetched": FETCHED}
    args.update(kw)
    return derive_approval_record(pr, reviews, **args)  # type: ignore[arg-type]


def t(minutes: int) -> datetime:
    return NOW - timedelta(minutes=minutes)


def test_an_approved_merge() -> None:
    record = derive(Pr(), [Rv(1, "bruno", "COMMENTED", submitted_at=t(30)), Rv(2, "bruno", "APPROVED", submitted_at=t(25))])
    assert record["schema"] == record_schema() == f"https://{_meta.NAME}.invalid/schemas/approval-record/v1"
    assert (record["pr"], record["task"], record["method"], record["merge_commit_sha"]) == (57, 42, "squash", "c" * 40)
    assert (record["approval_status"], record["approved_by"], record["merge_audit"]) == ("approved", ["bruno"], "compliant")
    assert record["reviews"] == [{"login": "bruno", "role": "deputy", "state": "APPROVED",
                                  "submitted_at": "2026-10-02T17:55:00Z", "commit_id": HEAD, "on_head": True}]
    assert (record["merged_at"], record["roles_yaml_sha"], record["labels"]) == ("2026-10-02T18:20:00Z", "5d1e0aa12345",
                                                                                 ["flag:overlap"])
    source = record["source"]
    assert isinstance(source, dict) and source["fetched_at"] == "2026-12-01T18:20:00Z" and len(source["sha256"]) == 64


@pytest.mark.parametrize(("reviews", "status", "approved_by"), [
    ([], "missing", []),
    ([Rv(1, "bruno", "APPROVED", commit_sha="b" * 40, submitted_at=t(9))], "approved_stale", ["bruno"]),
    ([Rv(1, "bruno", "APPROVED", submitted_at=t(9)), Rv(2, "ana", "CHANGES_REQUESTED", submitted_at=t(8))],
     "changes_requested", ["bruno"]),
    ([Rv(1, "bruno", "APPROVED", submitted_at=t(9)), Rv(2, "bruno", "DISMISSED", submitted_at=t(8))], "missing", []),
    ([Rv(1, "carla", "APPROVED", submitted_at=t(9))], "missing", []),  # la autora no califica
    ([Rv(1, "", "APPROVED", submitted_at=t(9))], "missing", []),  # cuenta borrada: ghost
])
def test_the_status_counts_the_last_effective_review(reviews: list[Rv], status: str, approved_by: list[str]) -> None:
    record = derive(Pr(), reviews)
    assert (record["approval_status"], record["approved_by"]) == (status, approved_by)


def test_an_unmerged_pr_and_a_ghost() -> None:
    record = derive(Pr(merged_at=None, merged_by=None), [Rv(1, "", "APPROVED", submitted_at=t(9))], merge_audit=None)
    assert (record["approval_status"], record["merged_at"], record["merge_audit"]) == ("not_merged", None, None)
    assert record["reviews"] == [{"login": "ghost", "role": None, "state": "APPROVED", "submitted_at": "2026-10-02T18:11:00Z",
                                  "commit_id": HEAD, "on_head": True}]


def test_the_hash_does_not_depend_on_the_fetch() -> None:
    """§4.1: derivar otra vez da el mismo hash mientras los datos no cambien."""
    reviews = [Rv(2, "bruno", "APPROVED", submitted_at=t(25))]
    first, again = derive(Pr(), reviews), derive(Pr(), reviews, fetched=replace(FETCHED, at=NOW + timedelta(days=90)))
    assert first["source"]["sha256"] == again["source"]["sha256"]  # type: ignore[index]
    changed = derive(replace(Pr(), title="Otro título"), reviews)
    assert changed["source"]["sha256"] != first["source"]["sha256"]  # type: ignore[index]


def test_against_the_fake_adapter(tmp_path: Path) -> None:
    """REQ-TRACE-45: el registro con lo que dan GET pulls/{n}, sus archivos y sus revisiones."""
    w: World = new_world(tmp_path)
    f = w.forge
    head = f.seed_commit({"README.md": b"x\n", "sections/methods/x.tex": b"y\n"}, [w.base_fake], "trabajo", "carla")
    f.seed_pull_request(files=[ChangedFile("sections/methods/x.tex", "added")], number=20, head_branch="task/12",
                        head_sha=head, author="carla", state="closed", merged=True, merged_at=at(-10), merged_by="ana",
                        merge_commit_sha="d" * 40, created_at=at(-200), updated_at=at(-10))
    f.seed_review(20, author="bruno", state="APPROVED", commit_sha=head, submitted_at=at(-20))
    from aucove.ops.context import Invocation, OpContext

    inv = Invocation(root=str(w.repo.path), lang="es", json=True, dry_run=False, surface="mcp", env=dict(w.env),
                     cwd=str(w.repo.path))
    ctx = OpContext(inv, forge=f.as_user("ana"))
    try:
        snap = ctx.base()
        record = approval_record(ctx.forge(), snap, 20, merge_audit="compliant", fetched_at=NOW)
    finally:
        ctx.close()
    assert record is not None
    assert (record["merge_commit_sha"], record["merged_by"], record["task"]) == ("d" * 40, "ana", 12)
    assert (record["approval_status"], record["approved_by"]) == ("approved", ["bruno"])
    assert record["roles_yaml_sha"] == snap.roles.source_sha12 and record["profile"] == "enforced"
    assert record["source"]["forge"] == "fake"  # type: ignore[index]
    assert approval_record(f.as_user("ana"), snap, 999, merge_audit=None, fetched_at=NOW) is None
