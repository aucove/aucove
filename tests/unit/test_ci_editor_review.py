# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``ci editor-review`` contra FakeForge (09-ci §4.5, REQ-CI-57 a REQ-CI-60; 00-marco D-56).

Llaman al ``CiCommand`` directamente, como ``test_ci_base_report``; la decisión pura la prueba
``test_approvals`` (``editor_verdict``). La política es el ``roles.yaml`` de 03 §4, que declara
a ``carla-soto`` como editora del artículo.
"""

from __future__ import annotations

import argparse
import importlib
import io
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from aucove import _meta
from aucove.domain.checks import EDITOR_REVIEW
from aucove.forge.base import ChangedFile, RepoRef
from aucove.forge.fake import CI_LOGIN, FakeForge, Fault
from aucove.ops import ci_editor_review as er
from aucove.ops.context import Invocation, OpContext
from aucove.ops.registry import CI_MODULES, CiCommand
from aucove.ops.rules.scope import MAX_PR_FILES
from tests.conftest import FIXTURES

COMMAND = er.COMMANDS[0]
PROJECT = (FIXTURES / "config" / "project" / "minimal.yaml").read_text(encoding="utf-8")
ROLES = (FIXTURES / "config" / "roles" / "example-03.yaml").read_text(encoding="utf-8")
REPO = RepoRef("example-org", "demo")
LEAD, DEPUTY, EDITOR = "ana-perez", "Bruno-Diaz", "carla-soto"
PART = "sections/metodos/metodos.tex"


@dataclass
class World:
    forge: FakeForge  # vista como github-actions[bot]
    policy: Path
    base: str
    pr: int
    head: str


def write_policy(root: Path, *, project: str = PROJECT, roles: str = ROLES) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "project.yaml").write_text(project, encoding="utf-8", newline="\n")
    (root / "roles.yaml").write_text(roles, encoding="utf-8", newline="\n")
    return root


def make_world(tmp_path: Path, *, files: Sequence[ChangedFile] = (ChangedFile(PART, "modified"),),
               author: str = DEPUTY, project: str = PROJECT, roles: str = ROLES) -> World:
    fake = FakeForge(REPO, viewer=CI_LOGIN)
    for login in (LEAD, DEPUTY, EDITOR):
        fake.seed_user(login, permission="write")
    base = fake.seed_commit({PART: b"v0"}, message="init")
    fake.seed_branch("main", base)
    head = fake.seed_commit({PART: b"v1"}, parents=[base], message="work", author=author)
    fake.seed_branch("task/3", head)
    pr = fake.seed_pull_request(files=list(files), head_branch="task/3", head_sha=head, author=author)
    return World(fake, write_policy(tmp_path / "policy", project=project, roles=roles), base, pr.number, head)


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="editor-review")
    COMMAND.add_arguments(parser)
    return parser.parse_args(argv)


def run(w: World, *, summary: Path | None = None, pr: int | None = None,
        env: dict[str, str] | None = None) -> tuple[int, dict[str, str]]:
    argv = ["--pr", str(pr or w.pr), "--policy", str(w.policy)]
    if summary is not None:
        argv += ["--summary", str(summary)]
    inv = Invocation(root=None, lang=None, json=False, dry_run=False, surface="ci", env=dict(env or {}),
                     cwd=str(Path.cwd()))
    ctx = OpContext(inv, forge=w.forge)
    out = io.StringIO()
    try:
        code = COMMAND.main(parse(argv), ctx, out)
    finally:
        ctx.close()
    return code, dict(line.split("=", 1) for line in out.getvalue().splitlines())


def check(w: World, sha: str | None = None) -> tuple[str | None, str | None]:
    """``(conclusión, resumen)`` del único check run ``aucove/editor-review`` de la cabeza."""
    runs = [r for r in w.forge.get_checks(sha or w.head).runs if r.name == EDITOR_REVIEW]
    assert len(runs) <= 1, runs  # REQ-CI-59: uno solo por cabeza
    if not runs:
        return None, None
    return runs[0].conclusion, w.forge.check_summary(sha or w.head, EDITOR_REVIEW)


# -- registro del subcomando y argumentos ------------------------------------------------------


def test_command_is_exported_for_ci_modules() -> None:
    assert "ops.ci_editor_review" in CI_MODULES
    module = importlib.import_module(".ops.ci_editor_review", _meta.NAME)
    assert module is er and isinstance(COMMAND, CiCommand) and COMMAND.name == "editor-review"
    assert EDITOR_REVIEW == f"{_meta.NAME}/editor-review"


def test_help_is_localized() -> None:
    parser = argparse.ArgumentParser(prog="editor-review")
    COMMAND.add_arguments(parser)
    text = parser.format_help()
    assert "--pr" in text and "--policy" in text and "--summary" in text


@pytest.mark.parametrize("value", ["0", "-1", "07", "x", "", "2147483648", "１"])
def test_pr_must_be_a_positive_number(value: str) -> None:
    with pytest.raises(SystemExit):
        parse(["--pr", value, "--policy", "p"])


# -- la decisión (REQ-CI-58) y la publicación (REQ-CI-59) -----------------------------------------


def test_missing_then_approved_then_new_push(tmp_path: Path) -> None:
    """E13: falla hasta que la editora aprueba la cabeza, y vuelve a fallar tras un push nuevo."""
    w = make_world(tmp_path)
    summary = tmp_path / "summary.md"
    assert run(w, summary=summary) == (0, {"conclusion": "failure", "reason": "missing"})
    conclusion, text = check(w)
    assert conclusion == "failure" and f"@{EDITOR}" in (text or "") and w.head[:12] in (text or "")
    assert f"{_meta.NAME} review approve {w.pr}" in (text or "")
    assert summary.read_text(encoding="utf-8") == text
    w.forge.seed_review(w.pr, author=EDITOR, state="APPROVED", commit_sha=w.head)
    assert run(w) == (0, {"conclusion": "success", "reason": "approved"})
    assert check(w)[0] == "success"
    # Un push nuevo: la aprobación era de la cabeza anterior.
    head2 = w.forge.seed_commit({PART: b"v2"}, parents=[w.head], message="more", author=DEPUTY)
    w.forge.update_ref("task/3", head2)
    assert w.forge.get_pull_request(w.pr).head_sha == head2  # type: ignore[union-attr]
    assert run(w) == (0, {"conclusion": "failure", "reason": "missing"})
    assert check(w, head2)[0] == "failure" and check(w)[0] == "success"
    assert w.forge.calls.count(kind="E", method="upsert_check_run") == 3


def test_changes_requested_fails(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.forge.seed_review(w.pr, author=EDITOR, state="APPROVED", commit_sha=w.head)
    w.forge.seed_review(w.pr, author=EDITOR, state="CHANGES_REQUESTED", commit_sha=w.head)
    assert run(w) == (0, {"conclusion": "failure", "reason": "changes_requested"})
    assert "pidió cambios" in (check(w)[1] or "")


def test_other_approvals_do_not_count(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.forge.seed_review(w.pr, author=LEAD, state="APPROVED", commit_sha=w.head)
    assert run(w) == (0, {"conclusion": "failure", "reason": "missing"})


@pytest.mark.parametrize(("files", "author", "reason"), [
    ((ChangedFile("sections/metodos/figures/f1.pdf", "added"),), DEPUTY, "no_latex"),
    ((ChangedFile("plan.md", "modified"),), DEPUTY, "no_latex"),
    ((ChangedFile(PART, "modified"),), "Carla-Soto", "author_is_editor"),
])
def test_passes_without_r2(tmp_path: Path, files: Sequence[ChangedFile], author: str, reason: str) -> None:
    w = make_world(tmp_path, files=files, author=author)
    assert run(w) == (0, {"conclusion": "success", "reason": reason})
    assert check(w)[0] == "success"


@pytest.mark.parametrize("files", [
    (ChangedFile("tex/outline/metodos.tex", "modified"),),
    (ChangedFile("bib/references.bib", "modified"),),
    (ChangedFile("tex/guide.md", "modified"),),
    (ChangedFile("sections/metodos/pending.bib", "modified"),),
    (ChangedFile("old/notation.txt", "renamed", "tex/notation.tex"),),  # el origen cuenta
])
def test_latex_and_editor_files_need_the_editor(tmp_path: Path, files: Sequence[ChangedFile]) -> None:
    w = make_world(tmp_path, files=files)
    assert run(w) == (0, {"conclusion": "failure", "reason": "missing"})


def test_too_many_files_need_the_editor(tmp_path: Path) -> None:
    """Con el tope de la API no se sabe si falta un .tex: se pide la aprobación (REQ-PERM-15)."""
    many = tuple(ChangedFile(f"data/f{i}.csv", "added") for i in range(MAX_PR_FILES))
    w = make_world(tmp_path, files=many)
    assert run(w) == (0, {"conclusion": "failure", "reason": "missing"})
    w.forge.seed_review(w.pr, author=EDITOR, state="APPROVED", commit_sha=w.head)
    assert run(w) == (0, {"conclusion": "success", "reason": "approved"})


def test_without_editor_individual_or_format_none(tmp_path: Path) -> None:
    no_editor = ROLES.replace("editor: carla-soto ", "# editor: carla-soto ")
    assert run(make_world(tmp_path / "a", roles=no_editor)) == (0, {"conclusion": "success", "reason": "no_editor"})
    individual = PROJECT + "mode: individual\n"
    solo = "\n".join(line for line in ROLES.splitlines() if not line.startswith(("deputy:", "editor:")))
    solo = solo.split("  - login: Bruno-Diaz")[0] + "sections:\n  - id: introduccion\n    title: \"Introducción\"\n    owner: ana-perez\n"
    w = make_world(tmp_path / "b", project=individual, roles=solo)
    assert run(w) == (0, {"conclusion": "success", "reason": "individual"})
    none = PROJECT.replace("format: latex", "format: none")
    w = make_world(tmp_path / "c", project=none)
    assert run(w) == (0, {"conclusion": "success", "reason": "format_none"})


# -- falla cerrada (REQ-CI-8) ---------------------------------------------------------------------


def test_policy_unavailable_publishes_failure(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    (w.policy / "roles.yaml").write_text("people: [", encoding="utf-8")
    code, out = run(w)
    assert (code, out) == (1, {"conclusion": "failure", "reason": "policy_unavailable"})
    assert "E_CONFIG_POLICY_UNAVAILABLE" in (check(w)[1] or "")


@pytest.mark.parametrize("method", ["list_pull_request_files", "list_reviews"])
def test_api_errors_publish_failure(tmp_path: Path, method: str) -> None:
    w = make_world(tmp_path)
    w.forge.seed_review(w.pr, author=EDITOR, state="APPROVED", commit_sha=w.head)
    w.forge.faults.add(Fault(method, "http_5xx"))
    assert run(w) == (1, {"conclusion": "failure", "reason": "forge_error"})
    assert check(w)[0] == "failure"


def test_without_the_pr_nothing_is_published(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    assert run(w, pr=999) == (1, {"conclusion": "none", "reason": "pr_unavailable"})
    w.forge.faults.add(Fault("get_pull_request", "http_5xx"))
    assert run(w) == (1, {"conclusion": "none", "reason": "pr_unavailable"})
    assert check(w) == (None, None)


def test_publish_error_exits_1(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.forge.faults.add(Fault("upsert_check_run", "http_5xx"))
    assert run(w) == (1, {"conclusion": "failure", "reason": "missing"})


def test_closed_pr_is_not_evaluated(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.forge.update_pull_request(w.pr, state="closed")
    assert run(w) == (0, {"conclusion": "none", "reason": "not_open"})
    assert check(w) == (None, None)


def test_messages_follow_the_language_variable(tmp_path: Path) -> None:
    """El texto del check es un mensaje: ``<PREFIJO>LANG`` antes que ``language`` (REQ-ARCH-69)."""
    w = make_world(tmp_path)
    run(w, env={f"{_meta.ENV_PREFIX}LANG": "en"})
    assert "the approval of @carla-soto" in (check(w)[1] or "")
