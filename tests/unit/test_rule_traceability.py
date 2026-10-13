# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Regla ``traceability`` (09-ci §4.1; 11-trazabilidad §2.10 y §3.4; 03-esquemas §9.4).

El escenario es una rama ``task/12`` como la deja el núcleo: el commit vacío de ``claim``, uno de
``save``, la fusión de la base que hace ``update`` y el commit de la nota de sesión; y un cuerpo de
PR armado con ``render_body`` como lo arma ``submit``. Política: los roles ``basic`` (carla es
investigadora y dueña de ``methods``, sin correo ni ``forge_id``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

import pytest

from aucove.config.base import BaseSnapshot
from aucove.config.project import parse_project
from aucove.config.roles import parse_roles
from aucove.domain.findings import Finding
from aucove.domain.trailers import Entry, commit_message, trailer_block
from aucove.forge.base import PullRequest
from aucove.i18n import translator
from aucove.ops.prbody import PR_META_BEGIN, PR_META_END, render_body
from aucove.ops.rules import traceability
from aucove.ops.rules.base import Change, RuleInput
from aucove.vcs.tree import git_tree
from tests.conftest import FIXTURES, GitRepo, roles_fixture

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
PROJECT = (FIXTURES / "config" / "project" / "minimal.yaml").read_text(encoding="utf-8")
ENTRY = Entry("claude-code", "claude-opus-5-5", "env:CLAUDECODE", "hook:SessionStart")
SIGNER = ("Carla Soto", "123+carla@users.noreply.github.com")
NOTE = "memory/notes/task-12.md"
CHANGES = (Change("sections/methods/x.tex", "added"), Change(NOTE, "added"))


def snapshot(roles: str | None = None) -> BaseSnapshot:
    cfg, _ = parse_project(PROJECT.encode("utf-8"))
    parsed, _ = parse_roles((roles or roles_fixture("basic")).encode("utf-8"))
    assert cfg is not None and parsed is not None
    return BaseSnapshot(ref="origin/main", sha=None, project=cfg, roles=parsed, decisions=frozenset(),
                        fallback_sha=None, source="local")


def note(ai_use: Sequence[str] = ("drafting",), assisted: Sequence[str] = ("claude-code:claude-opus-5-5",)) -> str:
    lines = ["---", "task: 12", "created: 2026-10-01", "created_by: carla", "ai_use: [" + ", ".join(ai_use) + "]"]
    if assisted:
        lines.append("assisted_by: [" + ", ".join(f'"{a}"' for a in assisted) + "]")
    return "\n".join([*lines, "---", "", "Notas.", ""])


def save_message(text: str, *, ai_use: Sequence[str] = ("drafting",), sections: Sequence[str] = ("methods",)) -> str:
    return commit_message(text, trailer_block(task=12, sections=sections, ai_use=ai_use, assisted=[ENTRY]))


class Branch:
    """La rama de la tarea en un repositorio local: base, merge-base, cabeza y punta de main."""

    def __init__(self, repo: GitRepo, merge_base: str, head: str, main: str) -> None:
        self.repo, self.merge_base, self.head, self.main = repo, merge_base, head, main

    def commit(self, message: str, files: Mapping[str, str] | None = None) -> None:
        self.head = self.repo.commit(message, files=dict(files or {}), allow_empty=not files)


@pytest.fixture
def branch(git_repo: GitRepo) -> Branch:
    git_repo.commit("base", files={"project.yaml": PROJECT, "sections/methods/m.tex": "% m\n"})
    git_repo.git("checkout", "-q", "-b", "task/12")
    git_repo.commit("aucove: claim #12\n\nTask: #12\nAI-Use: none", allow_empty=True)
    git_repo.commit(save_message("Agrega x"), files={"sections/methods/x.tex": "% x\n"})
    git_repo.git("checkout", "-q", "main")
    main = git_repo.commit("otro PR", files={"sections/results/r.tex": "% r\n"})
    git_repo.git("checkout", "-q", "task/12")
    git_repo.merge_no_ff("main", "Integra main\n\nTask: #12")  # update: sin AI-Use, exento
    head = git_repo.commit(save_message("Nota de la sesión", sections=()), files={NOTE: note()})
    # con la base integrada, el merge-base que calcula validate es la punta de main
    return Branch(git_repo, git_repo.git("merge-base", "main", head), head, main)


def body(*, ai_use: Sequence[str] = ("drafting",), assisted: Sequence[Entry] = (ENTRY,),
         signer: tuple[str, str] = SIGNER, task: int = 12, sections: Sequence[str] = ("methods",),
         decisions: Sequence[str] = (), trailer_uses: Sequence[str] | None = None, trace: bool = True,
         meta: Mapping[str, Any] | None = None) -> str:
    """El cuerpo que escribe ``submit`` (03 §9.2), con ``trace`` y los trailers al final."""
    trace_obj = {
        "version": 1, "task": task, "sections": list(sections), "ai_use": list(ai_use),
        "ai_use_by_section": {s: list(ai_use) for s in sections},
        "assisted_by": [{"tool": e.tool, "model": e.model, "tool_source": e.tool_source, "model_source": e.model_source}
                        for e in assisted],
        "sign_off": {"login": "carla", "name": signer[0], "email": signer[1], "channel": "mcp",
                     "attestation": "attest-v1", "at": "2026-10-01T12:00:00Z"},
        "commits": {"base": "a" * 40, "head": "b" * 40, "count": 4, "undeclared": 0},
        "core_version": "0.1.0",
    }
    block: dict[str, Any] = {"schema": 1, "task": task, "sections": list(sections), "plan_refs": [],
                             "decisions": list(decisions)}
    if trace:
        block["trace"] = trace_obj
    block.update(meta or {})
    trailers = (trailer_block(task=task, sections=sections, ai_use=trailer_uses or ai_use, assisted=assisted)
                + f"Signed-off-by: {signer[0]} <{signer[1]}>\n")
    return render_body(lang=translator("es"), number=task, summary="Resumen.", replaces=None, parts={},
                       ai_use=ai_use, assisted=[e.value for e in assisted], ai_note=None, decisions=list(decisions),
                       hook_count=0, meta=block, trailers=trailers)


def pull(text: str, *, head: str, branch_name: str = "task/12") -> PullRequest:
    return PullRequest(20, "", "Tarea 12", text, "open", False, False, None, branch_name, head, "main", "carla", (), (),
                       NOW, "https://example.invalid/pull/20")


def run(b: Branch, text: str | None = None, *, changes: Sequence[Change] = CHANGES, tree: bool = True,
        branch_name: str = "task/12", policy: BaseSnapshot | None = None) -> list[Finding]:
    inp = RuleInput(
        mode="pr", t=translator("en"), now=NOW, policy=policy or snapshot(),
        tree=git_tree(b.repo.path, b.head) if tree else None, base_tree=git_tree(b.repo.path, b.merge_base),
        changes=tuple(changes), pr=pull(body() if text is None else text, head=b.head, branch_name=branch_name),
        head_ref=branch_name, head_sha=b.head, base_ref="main", base_sha=b.main,
    )
    return list(traceability.RULE.run(inp))


def codes(findings: Sequence[Finding]) -> list[tuple[str, str]]:
    return [(f.code, f.severity) for f in findings]


def test_what_the_core_writes_passes(branch: Branch) -> None:
    """El commit vacío de claim y la fusión de la base quedan exentos; lo demás cumple."""
    assert run(branch) == []


def test_a_commit_by_hand_needs_its_trailers(branch: Branch) -> None:
    branch.commit("a mano", {"sections/methods/y.tex": "% y\n"})
    found = run(branch)
    assert codes(found) == [("E_VALIDATION_TRAILER_MISSING", "error")] * 2
    assert all(branch.head[:12] in f.message for f in found)
    assert {"`AI-Use`", "`Task`"} == {f.message.split()[1] for f in found}


def test_a_merge_from_another_branch_is_not_exempt(branch: Branch) -> None:
    repo = branch.repo
    repo.git("checkout", "-q", "-b", "otra", branch.merge_base)
    repo.commit(save_message("ajeno"), files={"sections/methods/z.tex": "% z\n"})
    repo.git("checkout", "-q", "task/12")
    branch.head = repo.merge_no_ff("otra", "Integra otra")
    assert codes(run(branch)) == [("E_VALIDATION_TRAILER_MISSING", "error")] * 2


def test_a_malformed_commit_trailer(branch: Branch) -> None:
    branch.commit("Arreglo\n\nTask: #12\nAI-Use: editing, drafting", {"sections/methods/y.tex": "% y\n"})
    (f,) = run(branch)
    assert (f.code, f.severity) == ("E_VALIDATION_TRAILER_SYNTAX", "error") and "order of the table" in f.message


def test_without_a_checkout_it_is_an_error(branch: Branch) -> None:
    (f,) = [x for x in run(branch, tree=False) if x.code == "E_VALIDATION_TRACE"]
    assert f.severity == "error" and "no checkout" in f.message


def test_the_signoff_must_be_the_author_in_roles(branch: Branch) -> None:
    found = run(branch, body(signer=("Carla", "carla@example.org")))
    assert codes(found) == [("E_VALIDATION_SIGNOFF_MISMATCH", "error")]
    roles = roles_fixture("basic").replace('    name: "Carla Soto"\n', '    name: "Carla Soto"\n    forge_id: 777\n')
    assert codes(run(branch, policy=snapshot(roles))) == [("E_VALIDATION_SIGNOFF_MISMATCH", "error")]
    good = ("Carla Soto", "777+carla@users.noreply.github.com")
    assert run(branch, body(signer=good), policy=snapshot(roles)) == []
    with_email = roles_fixture("basic").replace('    name: "Carla Soto"\n',
                                                '    name: "Carla Soto"\n    email: "carla@uni.example"\n')
    assert run(branch, body(signer=("Carla Soto", "carla@uni.example")), policy=snapshot(with_email)) == []


def test_trailers_edited_after_submit(branch: Branch) -> None:
    """REQ-TRACE-21: los trailers del cuerpo y el objeto trace dicen lo mismo."""
    found = run(branch, body(trailer_uses=("drafting", "editing")))
    assert codes(found) == [("E_VALIDATION_TRACE_MISMATCH", "error")] and "`ai_use`" in found[0].message
    assert "body trailers" in found[0].message


def test_the_trace_must_match_the_session_note(branch: Branch) -> None:
    """REQ-TRACE-40: el bloque coincide con el front matter de la nota en la cabeza."""
    branch.commit(save_message("Nota editada", sections=()), {NOTE: note(ai_use=("editing",))})
    found = run(branch)
    assert codes(found) == [("E_VALIDATION_TRACE_MISMATCH", "error")]
    assert found[0].path == NOTE and "`ai_use`" in found[0].message
    branch.commit(save_message("Sin nota", sections=()), {NOTE: "sin front matter\n"})
    assert [f.code for f in run(branch)] == ["E_VALIDATION_TRACE"]


def test_the_block_is_required_and_unique(branch: Branch) -> None:
    text = body()
    start, end = text.index("<!-- "), text.index("\n\nTask:")
    without = text[:start] + text[end:]
    assert codes(run(branch, without)) == [("E_VALIDATION_PR_META_MISSING", "error")]
    block = text[text.index(PR_META_BEGIN):text.index(PR_META_END) + len(PR_META_END)]
    twice = text[:end] + "\n" + block + text[end:]
    assert codes(run(branch, twice)) == [("E_VALIDATION_PR_META", "error")]


def test_the_block_matches_the_pr(branch: Branch) -> None:
    """REQ-SCHEMA-77 a 79: task, decisions y sections."""
    added = Change("memory/decisions/2026-10-01-12-usar-panel.md", "added")
    found = run(branch, changes=(*CHANGES, added))
    assert codes(found) == [("E_VALIDATION_PR_META", "error")] and "decisions" in found[0].message
    found = run(branch, changes=(*CHANGES, Change("sections/results/r2.tex", "added")))
    assert codes(found) == [("E_VALIDATION_PR_META", "error")] and "results" in found[0].message
    found = run(branch, body(meta={"schema": 2}))
    assert [f.code for f in found] == ["E_VALIDATION_PR_META"]


def test_the_trace_object_is_required_and_valid(branch: Branch) -> None:
    found = run(branch, body(trace=False))
    assert codes(found) == [("E_VALIDATION_TRACE", "error")] and "no `trace` object" in found[0].message
    invalid = body(meta={"trace": {"version": 2}})
    assert {f.code for f in run(branch, invalid)} == {"E_VALIDATION_TRACE"}


def test_an_undeclared_submission(branch: Branch) -> None:
    found = run(branch, body(trailer_uses=("undeclared",)))
    assert ("E_VALIDATION_TRAILER_MISSING", "error") in codes(found)


def test_a_pr_not_from_a_task_branch_only_warns(branch: Branch) -> None:
    """11 §2.10: en un PR que no sale de task/<n>, lo del cuerpo es aviso."""
    text = body()
    without = text[:text.index("<!-- ")] + text[text.index("\n\nTask:"):]
    assert codes(run(branch, without, branch_name="patch-1")) == [("E_VALIDATION_PR_META_MISSING", "warning")]


def test_the_rule_runs_only_on_prs() -> None:
    assert traceability.RULE.modes == frozenset({"pr"}) and traceability.RULE.id == "traceability"
    assert traceability.RULE.codes == traceability.CODES
