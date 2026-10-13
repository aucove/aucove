# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``ci audit`` contra FakeForge y un git local (09-ci §7.1, REQ-CI-46; 06-permisos §3.5).

La base local de ``tests.board_world`` (roles ``basic``: ana responsable, bruno suplente, carla
dueña de ``methods``) recibe commits como los squash de PR integrados; el falso los asocia con su
PR por ``merge_commit_sha``. Se llama al ``CiCommand`` directamente, como en
``test_ci_editor_review``, con la vista del CI (``github-actions[bot]``).
"""

from __future__ import annotations

import argparse
import io
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

import pytest

from aucove import _meta
from aucove.domain.checks import required_checks
from aucove.forge.base import NULL_SHA, ChangedFile
from aucove.forge.fake import CI_LOGIN, FakeForge
from aucove.ops import ci_audit as audit
from aucove.ops.context import Invocation, OpContext
from aucove.ops.registry import CI_MODULES
from tests.board_world import World, at, new_world
from tests.conftest import roles_fixture

COMMAND = audit.COMMANDS[0]
OP_MARKER = f"<!-- {_meta.NAME}:op v1 id=audit-"


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="audit")
    COMMAND.add_arguments(parser)
    return parser.parse_args(argv)


def run(w: World, before: str, after: str, *, forced: bool = False, summary: Path | None = None) -> tuple[int, list[str]]:
    """Las líneas ``merge_audit=`` de stdout; las anotaciones de la traza las da ``run_all``."""
    code, lines = run_all(w, before, after, forced=forced, summary=summary)
    return code, [line for line in lines if line.startswith("merge_audit=")]


def run_all(w: World, before: str, after: str, *, forced: bool = False,
            summary: Path | None = None) -> tuple[int, list[str]]:
    argv = ["--before", before, "--after", after, "--forced", "true" if forced else "false"]
    if summary is not None:
        argv += ["--summary", str(summary)]
    inv = Invocation(root=str(w.repo.path), lang=None, json=False, dry_run=False, surface="ci", env=dict(w.env),
                     cwd=str(w.repo.path))
    ctx = OpContext(inv, forge=w.forge.as_user(CI_LOGIN))
    out = io.StringIO()
    try:
        code = COMMAND.main(parse(argv), ctx, out)
    finally:
        ctx.close()
    return code, out.getvalue().splitlines()


def merged_pr(w: World, number: int, *, task: int, author: str, sha: str, files: Sequence[str],
              reviews: Sequence[tuple[str, str, datetime]] = (), merged_at: datetime | None = None,
              checks: str | None = "success") -> str:
    """Un PR de ``task/<task>`` integrado en la base local como ``sha``; devuelve su cabeza."""
    f = w.forge
    head = f.seed_commit({"README.md": b"x\n", **{p: b"y\n" for p in files}}, [w.base_fake], "trabajo", author)
    when = merged_at or at(-10)
    f.seed_pull_request(files=[ChangedFile(p, "added") for p in files], number=number, head_branch=f"task/{task}",
                        head_sha=head, author=author, state="closed", merged=True, merged_at=when,
                        merge_commit_sha=sha, created_at=at(-200), updated_at=when)
    for reviewer, state, submitted in reviews:
        f.seed_review(number, author=reviewer, state=state, commit_sha=head, submitted_at=submitted)  # type: ignore[arg-type]
    if checks is not None:
        for name in required_checks("latex"):
            f.set_check(head, name=name, status="completed", conclusion=checks)
    return head


def squash(w: World, files: Mapping[str, str], message: str = "squash") -> str:
    """El commit que deja un squash en la base local (o un push directo)."""
    sha = w.repo.commit(message, files=dict(files))
    return sha


def comments_with_marker(forge: FakeForge, number: int) -> list[str]:
    return [c.body for c in forge.list_comments(number) if OP_MARKER in c.body]


@pytest.fixture
def w(tmp_path: Path) -> World:
    return new_world(tmp_path)


def test_it_is_registered() -> None:
    assert "ops.ci_audit" in CI_MODULES and COMMAND.name == "audit"


def test_an_approved_pr_in_scope_is_compliant(w: World) -> None:
    w.task(12, claimant="carla", sections=("methods",))
    sha = squash(w, {"sections/methods/x.tex": "% x\n"})
    merged_pr(w, 20, task=12, author="carla", sha=sha, files=["sections/methods/x.tex"],
              reviews=[("bruno", "APPROVED", at(-20))])
    code, lines = run(w, w.base_sha, sha)
    assert code == 0 and lines == [f"merge_audit=compliant sha={sha} pr=20"]
    assert "flag:bypass" not in (w.forge.get_pull_request(20).labels)  # type: ignore[union-attr]
    assert comments_with_marker(w.forge, 20) == []


def test_without_approval_the_pr_is_marked_once(w: World) -> None:
    """REQ-PERM-23: flag:bypass en el PR, el marcador y la mención; un run repetido no duplica."""
    w.task(12, claimant="carla", sections=("methods",))
    sha = squash(w, {"sections/methods/x.tex": "% x\n"})
    merged_pr(w, 20, task=12, author="carla", sha=sha, files=["sections/methods/x.tex"])
    code, lines = run(w, w.base_sha, sha)
    assert code == 0 and lines == [f"merge_audit=bypass sha={sha} pr=20"]
    pr = w.forge.get_pull_request(20)
    assert pr is not None and "flag:bypass" in pr.labels
    (body,) = comments_with_marker(w.forge, 20)
    assert _meta.ci_marker("audit") in body and "@ana" in body and "@bruno" in body
    assert run(w, w.base_sha, sha)[0] == 0
    assert len(comments_with_marker(w.forge, 20)) == 1


def test_an_approval_after_the_merge_does_not_count(w: World) -> None:
    w.task(12, claimant="carla", sections=("methods",))
    sha = squash(w, {"sections/methods/x.tex": "% x\n"})
    merged_pr(w, 20, task=12, author="carla", sha=sha, files=["sections/methods/x.tex"], merged_at=at(-30),
              reviews=[("bruno", "APPROVED", at(-20))])
    assert run(w, w.base_sha, sha)[1] == [f"merge_audit=bypass sha={sha} pr=20"]


def test_a_file_outside_the_task_is_a_bypass(w: World) -> None:
    """REQ-CI-46: el alcance se recalcula; results es de bruno y no está en la tarea."""
    w.task(12, claimant="carla", sections=("methods",))
    sha = squash(w, {"sections/methods/x.tex": "% x\n", "sections/results/y.tex": "% y\n"})
    merged_pr(w, 20, task=12, author="carla", sha=sha, files=["sections/methods/x.tex", "sections/results/y.tex"],
              reviews=[("bruno", "APPROVED", at(-20))])
    code, lines = run(w, w.base_sha, sha)
    assert lines == [f"merge_audit=bypass sha={sha} pr=20"]
    (body,) = comments_with_marker(w.forge, 20)
    assert "E_PERM_" in body


def test_a_pr_not_from_a_task_branch_is_a_bypass(w: World) -> None:
    sha = squash(w, {"sections/methods/x.tex": "% x\n"})
    head = w.forge.seed_commit({"README.md": b"x\n"}, [w.base_fake], "web", "carla")
    w.forge.seed_pull_request(files=[ChangedFile("sections/methods/x.tex", "added")], number=21, head_branch="patch-1",
                              head_sha=head, author="carla", state="closed", merged=True, merged_at=at(-10),
                              merge_commit_sha=sha)
    w.forge.seed_review(21, author="bruno", state="APPROVED", commit_sha=head, submitted_at=at(-20))
    assert run(w, w.base_sha, sha)[1] == [f"merge_audit=bypass sha={sha} pr=21"]


def test_the_lead_without_a_deputy_is_bypass_no_deputy(tmp_path: Path) -> None:
    """06 §3.4: nadie podía aprobar el PR del lead."""
    w = new_world(tmp_path, extra_files={"roles.yaml": roles_fixture("no-deputy")})
    w.task(14, claimant="ana", sections=("introduction",))
    sha = squash(w, {"sections/introduction/x.tex": "% x\n"})
    merged_pr(w, 22, task=14, author="ana", sha=sha, files=["sections/introduction/x.tex"])
    code, lines = run(w, w.base_sha, sha)
    assert code == 0 and lines == [f"merge_audit=bypass_no_deputy sha={sha} pr=22"]
    (body,) = comments_with_marker(w.forge, 22)
    assert "@ana" in body and "@bruno" not in body


def test_a_commit_without_a_pr_opens_one_issue(w: World) -> None:
    sha = squash(w, {"notes.md": "a mano\n"})
    code, lines = run(w, w.base_sha, sha)
    assert code == 0 and lines == [f"merge_audit=direct_push sha={sha} pr=-"]
    issues = [i for i in w.forge.list_issues(labels=["flag:bypass"], state="all") if OP_MARKER in i.body]
    assert len(issues) == 1 and _meta.ci_marker("audit") in issues[0].body and "@ana" in issues[0].body
    assert run(w, w.base_sha, sha)[0] == 0
    assert len([i for i in w.forge.list_issues(labels=["flag:bypass"], state="all") if OP_MARKER in i.body]) == 1


def test_several_commits_are_audited_in_order(w: World) -> None:
    first = squash(w, {"a.md": "a\n"})
    second = squash(w, {"b.md": "b\n"})
    assert run(w, w.base_sha, second)[1] == [f"merge_audit=direct_push sha={first} pr=-",
                                             f"merge_audit=direct_push sha={second} pr=-"]


def test_a_forced_push_is_recorded_once(w: World) -> None:
    sha = squash(w, {"a.md": "a\n"})
    code, lines = run(w, w.base_sha, sha, forced=True)
    assert code == 0 and lines == [f"merge_audit=force_push sha={sha} pr=-"]
    (issue,) = [i for i in w.forge.list_issues(labels=["flag:bypass"], state="all") if OP_MARKER in i.body]
    assert sha[:12] in issue.title


def test_a_forced_push_without_before_in_the_checkout(w: World) -> None:
    """Después de un push forzado ninguna rama alcanza before: la política sale de after."""
    sha = squash(w, {"a.md": "a\n"})
    gone = "f" * 40  # el before que el checkout ya no tiene
    code, lines = run(w, gone, sha, forced=True)
    assert code == 0 and lines == [f"merge_audit=force_push sha={sha} pr=-"]
    (issue,) = [i for i in w.forge.list_issues(labels=["flag:bypass"], state="all") if OP_MARKER in i.body]
    assert "@ana" in issue.body and "@bruno" in issue.body
    assert run(w, gone, sha)[0] == 1  # sin forzar, before tiene que estar


def test_a_new_base_has_nothing_to_audit(w: World) -> None:
    code, lines = run(w, NULL_SHA, w.base_sha)
    assert code == 0 and lines == []
    assert len(w.forge.calls) == 0


def test_without_a_valid_policy_it_fails(w: World) -> None:
    """Un commit sin historia y con un roles.yaml inválido: sin versión válida (REQ-PERM-4)."""
    broken = w.repo.commit("rota", files={"roles.yaml": "version: 1\npeople: 3\n"})
    orphan = w.repo.git("commit-tree", "-m", "sin historia", w.repo.git("rev-parse", f"{broken}^{{tree}}"))
    code, lines = run(w, orphan, broken)
    assert code == 1 and lines == []


def test_individual_mode_needs_green_checks(tmp_path: Path) -> None:
    """REQ-PERM-59: sin aprobaciones; compliant con el alcance en regla y los checks en verde."""
    project = (Path(__file__).parents[1] / "fixtures" / "config" / "project" / "minimal.yaml").read_text(encoding="utf-8")
    roles = ("version: 1\nlead: ana\npeople:\n  - login: ana\n    name: \"Ana Pérez\"\n    role: lead\n"
             "    account_type: team\nsections:\n  - id: methods\n    title: \"Methods\"\n    owner: ana\n")
    w = new_world(tmp_path, extra_files={"project.yaml": project + "mode: individual\n", "roles.yaml": roles})
    w.task(15, claimant="ana", sections=("methods",), author="ana")
    green = squash(w, {"sections/methods/x.tex": "% x\n"})
    merged_pr(w, 23, task=15, author="ana", sha=green, files=["sections/methods/x.tex"])
    assert run(w, w.base_sha, green)[1] == [f"merge_audit=compliant sha={green} pr=23"]
    w.task(16, claimant="ana", sections=("methods",), author="ana")
    red = squash(w, {"sections/methods/y.tex": "% y\n"})
    merged_pr(w, 24, task=16, author="ana", sha=red, files=["sections/methods/y.tex"], checks="failure")
    assert run(w, green, red)[1] == [f"merge_audit=bypass sha={red} pr=24"]
    (body,) = comments_with_marker(w.forge, 24)
    assert f"{_meta.NAME}/build" in body


def test_the_summary_lists_each_commit(w: World, tmp_path: Path) -> None:
    sha = squash(w, {"a.md": "a\n"})
    summary = tmp_path / "summary.md"
    run(w, w.base_sha, sha, summary=summary)
    text = summary.read_text(encoding="utf-8")
    assert text.startswith(f"### {_meta.NAME}/base-audit\n") and f"`{sha[:12]}`: `direct_push`" in text


@pytest.mark.parametrize("argv", [
    ["--before", "x", "--after", "a" * 40, "--forced", "false"],
    ["--before", "a" * 40, "--after", "a" * 40, "--forced", "maybe"],
    ["--before", "a" * 40, "--forced", "false"],
])
def test_usage_errors(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        parse(argv)


# -- el commit de squash (REQ-TRACE-41) -------------------------------------------------------------


def test_a_squash_with_the_submit_trace_has_no_findings(w: World, tmp_path: Path) -> None:
    """El mensaje es el título con (#20) y el cuerpo que escribe submit; la nota está en el árbol."""
    from tests.unit.test_rule_traceability import NOTE, body, note

    w.task(12, claimant="carla", sections=("methods",))
    sha = squash(w, {"sections/methods/x.tex": "% x\n", NOTE: note()}, message=f"Tarea 12 (#20)\n\n{body()}")
    merged_pr(w, 20, task=12, author="carla", sha=sha, files=["sections/methods/x.tex", NOTE],
              reviews=[("bruno", "APPROVED", at(-20))])
    summary = tmp_path / "summary.md"
    code, lines = run_all(w, w.base_sha, sha, summary=summary)
    assert code == 0 and lines == [f"merge_audit=compliant sha={sha} pr=20"]
    assert "traza del squash en regla" in summary.read_text(encoding="utf-8")


def test_a_squash_without_its_trace_is_reported_not_blocked(w: World, tmp_path: Path) -> None:
    """11 §2.10: lo que falta es error y la falta del bloque pr-meta, aviso hasta S-10."""
    w.task(12, claimant="carla", sections=("methods",))
    sha = squash(w, {"sections/methods/x.tex": "% x\n"})
    merged_pr(w, 20, task=12, author="carla", sha=sha, files=["sections/methods/x.tex"],
              reviews=[("bruno", "APPROVED", at(-20))])
    summary = tmp_path / "summary.md"
    code, lines = run_all(w, w.base_sha, sha, summary=summary)
    assert code == 0 and lines[0] == f"merge_audit=compliant sha={sha} pr=20"
    annotations = lines[1:]
    assert annotations and all(line.startswith(("::error ", "::warning ")) for line in annotations)
    assert any("E_VALIDATION_TRAILER_MISSING" in line and line.startswith("::error ") for line in annotations)
    assert any("E_VALIDATION_PR_META_MISSING" in line and line.startswith("::warning ") for line in annotations)
    assert sha[:12] in annotations[0]
    assert "traza del squash: " in summary.read_text(encoding="utf-8")
    assert w.forge.get_pull_request(20).labels == ()  # type: ignore[union-attr]  # informar no es marcar
