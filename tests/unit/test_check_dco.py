# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""tools/check_dco.py: DCO de los PR al repo del framework (11-trazabilidad REQ-TRACE-66;
00-marco D-07 c).

Cada prueba crea un repositorio temporal con la identidad en variables de entorno:
nunca ``git config`` ni commits en este repositorio, y sin red.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest

from tests.conftest import GIT_IDENTITY, REPO_ROOT, GitRepo, load_tool

TOOL = REPO_ROOT / "tools" / "check_dco.py"
AUTHOR = f"{GIT_IDENTITY['GIT_AUTHOR_NAME']} <{GIT_IDENTITY['GIT_AUTHOR_EMAIL']}>"
SIGNOFF = f"Signed-off-by: {AUTHOR}"


@pytest.fixture(scope="module")
def dco() -> ModuleType:
    return load_tool("check_dco")


def message(subject: str, *trailers: str, body: str = "") -> str:
    parts = [subject]
    if body:
        parts.append(body)
    if trailers:
        parts.append("\n".join(trailers))
    return "\n\n".join(parts)


@pytest.fixture
def repo(git_repo: GitRepo) -> GitRepo:
    git_repo.commit("base", files={"README.md": "base\n"})
    return git_repo


def check(
    dco: ModuleType, repo: GitRepo, base: str, head: str = "HEAD", *, pr_author: str | None = None
) -> tuple[int, str]:
    return dco.run_check(base, head, pr_author=pr_author, git=dco.default_git(repo.path, env=repo.env))  # type: ignore[no-any-return]


def test_all_commits_signed_off_by_their_author_pass(dco: ModuleType, repo: GitRepo) -> None:
    base = repo.head()
    repo.commit(message("uno", SIGNOFF), allow_empty=True)
    repo.commit(message("dos", "Assisted-by: claude-code:claude-opus-5-5", SIGNOFF, body="Detalle."),
                allow_empty=True)
    code, text = check(dco, repo, base)
    assert code == 0, text
    assert "los 2 commits" in text


def test_a_commit_without_signoff_fails(dco: ModuleType, repo: GitRepo) -> None:
    base = repo.head()
    repo.commit(message("firmado", SIGNOFF), allow_empty=True)
    bad = repo.commit("sin firma", allow_empty=True)
    code, text = check(dco, repo, base)
    assert code == 1
    assert "1 de 2 commits" in text
    assert bad[:12] in text and "sin firma" in text
    assert "no lleva Signed-off-by" in text


@pytest.mark.parametrize(
    "trailer",
    [
        "Signed-off-by: Otra Persona <author@example.invalid>",  # nombre distinto
        f"Signed-off-by: {GIT_IDENTITY['GIT_AUTHOR_NAME']} <otra@example.invalid>",  # correo distinto
        f"Signed-off-by: {GIT_IDENTITY['GIT_AUTHOR_NAME']}",  # sin correo
        f"Signed-off-by: {GIT_IDENTITY['GIT_AUTHOR_NAME'].lower()} <author@example.invalid>",  # nombre exacto
    ],
)
def test_a_signoff_of_someone_else_fails(dco: ModuleType, repo: GitRepo, trailer: str) -> None:
    base = repo.head()
    sha = repo.commit(message("cambio", trailer), allow_empty=True)
    code, text = check(dco, repo, base)
    assert code == 1
    assert sha[:12] in text
    assert f"se esperaba: {SIGNOFF}" in text
    assert "lleva:" in text


def test_the_author_counts_not_the_committer(dco: ModuleType, repo: GitRepo) -> None:
    base = repo.head()
    # el committer es GIT_IDENTITY; el autor, Ana
    repo.commit(message("de Ana", SIGNOFF), allow_empty=True, author="Ana Pérez <ana@example.invalid>")
    code, text = check(dco, repo, base)
    assert code == 1 and "Ana Pérez <ana@example.invalid>" in text
    repo.commit(message("de Ana, firmado", "Signed-off-by: Ana Pérez <ana@example.invalid>"),
                allow_empty=True, author="Ana Pérez <ana@example.invalid>")
    code, text = check(dco, repo, repo.git("rev-parse", "HEAD^1"))
    assert code == 0, text


def test_email_case_does_not_matter(dco: ModuleType, repo: GitRepo) -> None:
    base = repo.head()
    upper = GIT_IDENTITY["GIT_AUTHOR_EMAIL"].upper()
    repo.commit(message("cambio", f"Signed-off-by: {GIT_IDENTITY['GIT_AUTHOR_NAME']} <{upper}>"), allow_empty=True)
    assert check(dco, repo, base)[0] == 0


def test_one_matching_signoff_among_several_is_enough(dco: ModuleType, repo: GitRepo) -> None:
    base = repo.head()
    repo.commit(message("cambio", "Signed-off-by: Otra Persona <otra@example.invalid>", SIGNOFF), allow_empty=True)
    assert check(dco, repo, base)[0] == 0


def test_signoff_outside_the_trailer_block_does_not_count(dco: ModuleType, repo: GitRepo) -> None:
    base = repo.head()
    repo.commit(message("cambio", body=f"{SIGNOFF}\n\nEste párrafo final no es de trailers."), allow_empty=True)
    code, text = check(dco, repo, base)
    assert code == 1 and "no lleva Signed-off-by" in text


def test_merge_commits_are_checked_too(dco: ModuleType, repo: GitRepo) -> None:
    base = repo.head()
    repo.branch("tema")
    repo.checkout("tema")
    repo.commit(message("en tema", SIGNOFF), files={"a.txt": "a\n"})
    repo.checkout("main")
    repo.commit(message("en main", SIGNOFF), files={"b.txt": "b\n"})
    merge = repo.merge_no_ff("tema", message="fusión sin firma")
    code, text = check(dco, repo, base)
    assert code == 1 and merge[:12] in text and "1 de 3 commits" in text
    repo.git("reset", "-q", "--hard", "HEAD^1")
    repo.merge_no_ff("tema", message=message("fusión", SIGNOFF))
    code, text = check(dco, repo, base)
    assert code == 0, text


def test_only_the_range_base_to_head_is_checked(dco: ModuleType, repo: GitRepo) -> None:
    repo.commit("viejo sin firma", allow_empty=True)  # ya está en la base
    base = repo.head()
    repo.commit(message("nuevo", SIGNOFF), allow_empty=True)
    assert check(dco, repo, base)[0] == 0
    code, text = check(dco, repo, base, base)
    assert code == 0 and "los 0 commits" in text


@pytest.mark.parametrize("author", ["dependabot[bot]", "Dependabot[bot]", " dependabot[bot] "])
def test_a_dependabot_pull_request_is_exempt(dco: ModuleType, repo: GitRepo, author: str) -> None:
    base = repo.head()
    repo.commit("Bump actions/checkout", allow_empty=True,
                author="dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>")
    code, text = check(dco, repo, base, pr_author=author)
    assert code == 0
    assert "exento" in text and "D-07 (c)" in text


@pytest.mark.parametrize("author", ["dependabot", "github-actions[bot]", "renovate[bot]", "ana", ""])
def test_other_authors_are_not_exempt(dco: ModuleType, repo: GitRepo, author: str) -> None:
    base = repo.head()
    # el correo del commit se puede falsificar: no exime nada
    repo.commit("Bump x", allow_empty=True,
                author="dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>")
    code, text = check(dco, repo, base, pr_author=author)
    assert code == 1 and "no lleva Signed-off-by" in text


def test_exemption_does_not_need_git(dco: ModuleType) -> None:
    def no_git(_: Sequence[str]) -> str:
        raise AssertionError("un PR exento no lee la historia")

    assert dco.run_check("a", "b", pr_author="dependabot[bot]", git=no_git)[0] == 0


@pytest.mark.parametrize("rev", ["no-existe", "-n1", "HEAD\nHEAD", ""])
def test_an_unreadable_range_fails_closed(dco: ModuleType, repo: GitRepo, rev: str) -> None:
    code, text = check(dco, repo, rev)
    assert code == 1 and "falla cerrado" in text


def test_untrusted_text_cannot_start_a_workflow_command(dco: ModuleType, repo: GitRepo) -> None:
    base = repo.head()
    repo.commit("::error::falso\x1b[31m rojo", allow_empty=True,
                author="::add-mask::x <::warning::@example.invalid>")
    code, text = check(dco, repo, base)
    assert code == 1
    for line in text.splitlines():
        assert not line.startswith("::"), line
    assert "\x1b" not in text


def test_parse_ident_and_signoff_matches(dco: ModuleType) -> None:
    assert dco.parse_ident("Ana Pérez <ana@example.invalid>") == ("Ana Pérez", "ana@example.invalid")
    assert dco.parse_ident("  Ana   <ana@x>  ") == ("Ana", "ana@x")
    assert dco.parse_ident("Ana") is None
    assert dco.signoff_matches("Ana <ANA@X>", "Ana", "ana@x")
    assert not dco.signoff_matches("Ana <>", "Ana", "")
    assert not dco.signoff_matches("Ana B <ana@x>", "Ana", "ana@x")


def test_main_writes_failures_to_stderr(dco: ModuleType, repo: GitRepo, capsys: pytest.CaptureFixture[str]) -> None:
    base = repo.head()
    repo.commit("sin firma", allow_empty=True)
    git = dco.default_git(repo.path, env=repo.env)
    assert dco.main(["--base", base, "--head", "HEAD", "--repo", str(repo.path)], git=git) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "no cumplen REQ-TRACE-66" in captured.err
    assert dco.main(["--base", base], git=git) == 1  # falta --head: error de uso, falla cerrada


def test_script_runs_as_a_subprocess(repo: GitRepo) -> None:
    base = repo.head()
    repo.commit(message("cambio con acento: módulo ✓", SIGNOFF), allow_empty=True)
    proc = subprocess.run(
        [sys.executable, str(TOOL), "--base", base, "--head", "HEAD"],
        cwd=repo.path, env=dict(repo.env), capture_output=True, timeout=120, check=False,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert "los 1 commits" in proc.stdout.decode("utf-8")
    repo.commit("sin firma ✗", allow_empty=True)
    proc = subprocess.run(
        [sys.executable, str(TOOL), "--base", base, "--head", "HEAD", "--repo", str(repo.path)],
        cwd=Path(repo.path).parent, env=dict(repo.env), capture_output=True, timeout=120, check=False,
    )
    assert proc.returncode == 1
    assert "sin firma ✗" in proc.stderr.decode("utf-8")
