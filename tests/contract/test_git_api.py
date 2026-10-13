# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Commits, contenido, compare, checks y workflow runs (08-forja §4.2, §4.5, §4.6, §10.3).

CT-GIT-01 (``commit_files``) y CT-GIT-02 (``merge_into``) corren con el falso y con GitHub. CT-GIT-03
es una medición contra GitHub y corre solo en ``live``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

import pytest

from aucove.domain.text import same_login
from aucove.forge.base import (
    ChangedFile,
    FileChange,
    ForgeError,
    ForgeNotFound,
    ForgeValidationError,
    MergeConflict,
    Ref,
    RefConflict,
)
from aucove.forge.fake import FakeForge
from tests.contract.conftest import BASE_FILES, LEAD, Sandbox


def test_create_empty_commit_keeps_the_parent_tree(sandbox: Sandbox) -> None:
    sha = sandbox.forge.create_empty_commit(sandbox.base_sha, "ct: claim #1\n\nTask: #1")
    info = sandbox.forge.get_commit(sha)
    assert info.sha == sha
    assert info.message == "ct: claim #1\n\nTask: #1"
    assert same_login(info.author_login, sandbox.lead_login)
    assert info.author_date.utcoffset() is not None
    for path, content in BASE_FILES.items():
        at_parent = sandbox.forge.get_file(path, sandbox.base_sha)
        at_child = sandbox.forge.get_file(path, sha)
        assert at_parent is not None and at_child is not None
        assert at_child.content == at_parent.content == content
        assert at_child.blob_sha == hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()  # noqa: S324


def test_get_file_missing_is_none(sandbox: Sandbox) -> None:
    assert sandbox.forge.get_file("does/not/exist.tex", sandbox.base_branch) is None
    assert sandbox.forge.get_file("roles.yaml", sandbox.branch("no-such-branch")) is None
    found = sandbox.forge.get_file("roles.yaml", sandbox.base_branch)
    assert found is not None and found.path == "roles.yaml" and found.ref == sandbox.base_branch


def test_get_commit_unknown_is_not_found(sandbox: Sandbox) -> None:
    with pytest.raises(ForgeNotFound):
        sandbox.forge.get_commit("0123456789abcdef0123456789abcdef01234567")


def test_compare_statuses(sandbox: Sandbox) -> None:
    c1 = sandbox.claim_commit()
    c2 = sandbox.claim_commit()
    same = sandbox.forge.compare(sandbox.base_sha, sandbox.base_sha)
    assert same.status == "identical" and same.ahead_by == same.behind_by == 0

    ahead = sandbox.forge.compare(sandbox.base_sha, c1, with_commits=True)
    assert ahead.status == "ahead" and ahead.ahead_by == 1 and ahead.behind_by == 0
    assert ahead.merge_base_sha == sandbox.base_sha
    assert ahead.files == () and ahead.files_truncated is False
    assert [c.sha for c in ahead.commits] == [c1]

    behind = sandbox.forge.compare(c1, sandbox.base_sha)
    assert behind.status == "behind" and behind.behind_by == 1

    diverged = sandbox.forge.compare(c1, c2)
    assert diverged.status == "diverged" and diverged.ahead_by == 1 and diverged.behind_by == 1
    assert diverged.merge_base_sha == sandbox.base_sha
    assert diverged.commits == ()  # solo con with_commits=True


def test_compare_with_branch_names(sandbox: Sandbox) -> None:
    branch = sandbox.branch("cmp")
    c1 = sandbox.claim_commit()
    sandbox.forge.create_ref(branch, c1)
    assert sandbox.settled_ref(branch, Ref(branch, c1)) == Ref(branch, c1)  # compare lee la rama por su nombre
    result = sandbox.forge.compare(sandbox.base_branch, branch)
    assert result.base == sandbox.base_branch and result.head == branch
    assert result.ahead_by == 1
    assert sandbox.forge.delete_ref(branch) is True


def test_codeowners_errors_of_a_clean_ref(sandbox: Sandbox) -> None:
    assert sandbox.forge.codeowners_errors(sandbox.base_branch) == []


def test_ct_git_01_commit_files(sandbox: Sandbox) -> None:
    """CT-GIT-01: commit_files con base_tree conserva lo demás; borra; rechaza .github/workflows/."""
    branch = sandbox.branch("git-01")
    sandbox.forge.create_ref(branch, sandbox.base_sha)
    sha = sandbox.forge.commit_files(
        branch,
        expected_head=sandbox.base_sha,
        changes=[FileChange("notes/a.md", b"a\n"), FileChange("sections/methods/methods.tex", None)],
        message="ct: files",
    )
    assert sandbox.forge.get_file("roles.yaml", sha) is not None
    assert sandbox.forge.get_file("notes/a.md", sha) is not None
    assert sandbox.forge.get_file("sections/methods/methods.tex", sha) is None
    with pytest.raises(ForgeValidationError):
        sandbox.forge.commit_files(
            branch, expected_head=sha, changes=[FileChange(".github/workflows/x.yml", b"on: push\n")], message="x"
        )
    with pytest.raises(ForgeValidationError):  # borrar una ruta ausente: 422 de GitHub (00-marco D-68)
        sandbox.forge.commit_files(branch, expected_head=sha, changes=[FileChange("notes/absent.md", None)], message="x")
    assert sandbox.settled_ref(branch, Ref(branch, sha)) == Ref(branch, sha)


def test_ct_git_02_merge_into(sandbox: Sandbox) -> None:
    """CT-GIT-02: sin cambios -> None; una fusión limpia -> el commit nuevo; con conflicto -> MergeConflict."""
    branch = sandbox.branch("git-02")
    sandbox.forge.create_ref(branch, sandbox.base_sha)
    assert sandbox.forge.merge_into(branch, sandbox.base_branch, message="ct: merge") is None
    ours = sandbox.content_commit({"notes/c.md": b"ours\n"})
    sandbox.forge.update_ref(branch, ours)
    other = sandbox.content_commit({"notes/d.md": b"d\n"})
    merged = sandbox.forge.merge_into(branch, other, message="ct: merge")
    assert merged is not None
    assert sandbox.settled_ref(branch, Ref(branch, merged)) == Ref(branch, merged)
    assert sandbox.forge.get_file("notes/d.md", merged) is not None and sandbox.forge.get_file("notes/c.md", merged)
    theirs = sandbox.content_commit({"notes/c.md": b"theirs\n"})
    with pytest.raises(MergeConflict):
        sandbox.forge.merge_into(branch, theirs, message="ct: merge")


def test_ct_git_03_workflow_files_without_workflow_permission(
    live_sandbox: Sandbox, record_property: Callable[[str, object], None]
) -> None:
    """CT-GIT-03 (live, H3; 08 §5.4): token sin Workflows y una base que cambia ``.github/workflows/x.yml``.

    Es una medición: registra si GitHub acepta o rechaza cada vía que trae ese cambio a
    ``task/<n>``. Por el adaptador, ``POST merges`` (``merge_into``) y ``commit_files`` con
    ``extra_parents``; ``git push`` del merge y ``PUT pulls/{n}/update-branch`` van por
    ``vcs`` y por la API directa en la corrida de H3. Toda respuesta debe quedar
    clasificada como éxito o como ``ForgeError``.
    """
    sandbox = live_sandbox
    forge = sandbox.forge
    branch = sandbox.branch("git-03")
    forge.create_ref(branch, sandbox.base_sha)
    base_tip = sandbox.workflow_fixture()  # creado con permiso de Workflows: desde la web (00-marco D-68)
    attempts: dict[str, Callable[[], object]] = {
        "merge_into": lambda: forge.merge_into(branch, base_tip, message="ct: merge base"),
        "commit_files_extra_parents": lambda: forge.commit_files(
            branch, expected_head=sandbox.base_sha, changes=(), message="ct: merge base", extra_parents=[base_tip]
        ),
    }
    outcomes: dict[str, str] = {}
    for route, attempt in attempts.items():
        try:
            attempt()
            outcomes[route] = "accepted"
        except ForgeError as exc:
            outcomes[route] = f"{exc.kind}:{exc.http_status}"
    record_property("ct_git_03", outcomes)
    assert set(outcomes) == set(attempts)
    assert forge.delete_ref(branch) is True


# --- Solo con el falso -------------------------------------------------------------


def test_fake_compare_lists_file_changes(fake: FakeForge) -> None:
    base = fake.to_state()["refs"]["heads/main"]
    files = dict(BASE_FILES)
    files["tex/main.tex"] = b"changed\n"
    files["notes/moved.md"] = files.pop("sections/methods/methods.tex")
    files["data/new.csv"] = b"a,b\n"
    head = fake.seed_commit(files, parents=[base], message="work", author=LEAD)
    result = fake.compare(base, head)
    assert result.files == (
        ChangedFile("data/new.csv", "added"),
        ChangedFile("notes/moved.md", "renamed", "sections/methods/methods.tex"),
        ChangedFile("tex/main.tex", "modified"),
    )
    removed = fake.seed_commit({"roles.yaml": BASE_FILES["roles.yaml"]}, parents=[base])
    assert {f.path: f.status for f in fake.compare(base, removed).files} == {
        "tex/main.tex": "removed",
        "sections/methods/methods.tex": "removed",
    }


def test_fake_compare_truncates_at_300_files(fake: FakeForge) -> None:
    base = fake.to_state()["refs"]["heads/main"]
    files = {**BASE_FILES, **{f"data/f{i:03}.txt": str(i).encode() for i in range(301)}}
    head = fake.seed_commit(files, parents=[base])
    result = fake.compare(base, head)
    assert len(result.files) == 300 and result.files_truncated is True


def test_fake_compare_unrelated_histories(fake: FakeForge) -> None:
    orphan = fake.seed_commit({"x": b"y"})
    with pytest.raises(ForgeNotFound):
        fake.compare("main", orphan)
    with pytest.raises(ForgeNotFound):
        fake.compare("main", "no-such-branch")


def test_fake_seed_commit_with_explicit_sha(fake: FakeForge) -> None:
    """Las pruebas por escenarios reproducen los sha de un repo git real."""
    real = "1234567890abcdef1234567890abcdef12345678"
    assert fake.seed_commit({"a": b"b"}, message="real", sha=real) == real
    assert fake.get_commit(real).message == "real"
    with pytest.raises(ValueError):
        fake.seed_commit({"a": b"b"}, sha="not-a-sha")


def test_fake_big_file_needs_raw_request(fake: FakeForge) -> None:
    big = fake.seed_commit({"data/big.bin": b"x" * ((1 << 20) + 1)})
    fake.calls.clear()
    found = fake.get_file("data/big.bin", big)
    assert found is not None and len(found.content) == (1 << 20) + 1
    assert fake.calls.count("L", "get_file") == 2


def test_fake_codeowners_errors(fake: FakeForge) -> None:
    fake.set_codeowners_errors("main", ["Unknown owner on line 3: @ghost"])
    assert fake.codeowners_errors("main") == ["Unknown owner on line 3: @ghost"]
    sha = fake.to_state()["refs"]["heads/main"]
    assert fake.codeowners_errors(sha) == ["Unknown owner on line 3: @ghost"]  # misma ref resuelta
    assert fake.codeowners_errors("other") == []


def test_fake_checks_depend_on_token_kind(fake: FakeForge) -> None:
    """08 §4.6: un token fine-grained no lee check runs y cae a los runs de Actions."""
    sha = fake.to_state()["refs"]["heads/main"]
    fake.set_check(sha, name="build-pdf", status="completed", conclusion="success")
    fake.set_check(sha, name="validate", status="in_progress", conclusion=None)
    fake.seed_workflow_run("build.yml", branch="main", sha=sha, conclusion="failure")

    fine = fake.get_checks(sha)  # el visor usa un token fine-grained
    assert [(r.name, r.source) for r in fine.runs] == [("build", "workflow_run")]
    assert fine.overall == "failure"
    assert fake.calls.count("L", "get_checks") == 2  # runs de Actions + status combinado

    fake.seed_user("classic", token_kind="classic_pat")
    classic = fake.as_user("classic").get_checks(sha)
    assert [(r.name, r.source) for r in classic.runs] == [("build-pdf", "check_run"), ("validate", "check_run")]
    assert classic.overall == "pending"
    fake.set_check(sha, name="validate", status="completed", conclusion="skipped")
    assert fake.as_user("classic").get_checks(sha).overall == "success"
    fake.set_check(sha, name="validate", status="completed", conclusion="timed_out")
    assert fake.as_user("classic").get_checks(sha).overall == "failure"
    assert fake.get_checks("f" * 40).overall == "none"


def test_fake_workflow_runs(fake: FakeForge) -> None:
    sha = fake.to_state()["refs"]["heads/main"]
    assert fake.last_successful_run("build.yml", "main") is None
    ok1 = fake.seed_workflow_run(".github/workflows/build.yml", branch="main", sha=sha, conclusion="success")
    fake.clock.advance(60)
    ok2 = fake.seed_workflow_run("build.yml", branch="main", sha=sha, conclusion="success")
    fake.clock.advance(60)
    fake.seed_workflow_run("build.yml", branch="main", sha=sha, conclusion="failure")
    fake.seed_workflow_run("build.yml", branch="task/1", sha=sha, conclusion="success")
    fake.seed_workflow_run("validate.yml", branch="main", sha=sha, conclusion=None)
    last = fake.last_successful_run("build.yml", "main")
    assert last == ok2 and last.id != ok1.id
    assert fake.get_workflow_run(ok1.id) == ok1
    assert ok1.status == "completed" and ok1.event == "push" and ok1.head_branch == "main"
    with pytest.raises(ForgeNotFound):
        fake.get_workflow_run(123456)


def test_fake_commit_files_moves_only_by_fast_forward(fake: FakeForge) -> None:
    """08 §4.5: si la rama ya no apunta a expected_head, RefConflict con la punta; extra_parents da una fusión."""
    base = fake.to_state()["refs"]["heads/main"]
    fake.create_ref("ct/ff", base)
    first = fake.commit_files("ct/ff", expected_head=base, changes=[FileChange("notes/a.md", b"a\n")], message="a")
    with pytest.raises(RefConflict) as info:
        fake.commit_files("ct/ff", expected_head=base, changes=[FileChange("notes/b.md", b"b\n")], message="b")
    assert info.value.current == first
    other = fake.create_empty_commit(base, "otra")
    merged = fake.commit_files("ct/ff", expected_head=first, changes=(), message="fusión", extra_parents=[other])
    assert fake.get_ref("ct/ff").sha == merged  # type: ignore[union-attr]
    assert fake.get_commit(merged).sha == merged and fake.get_file("notes/a.md", merged) is not None
    with pytest.raises(ForgeValidationError):
        fake.commit_files("ct/ff", expected_head=merged, changes=[FileChange("../x", b"x")], message="x")
