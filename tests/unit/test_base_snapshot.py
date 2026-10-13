# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Política de la rama base (06-permisos REQ-PERM-1..4, §2.1; 03 §2.5; 07-hooks REQ-HOOK-41).

Los repositorios son temporales (``git_repo``). Las historias largas se crean
con ``git fast-import`` en un solo proceso, directamente en ``refs/remotes/origin/main``.
``refs/remotes/origin/HEAD`` se fija a mano, como lo deja ``git clone``: git < 2.48 no lo
crea al hacer ``fetch`` y sin él (ni ``capabilities.json``) la base no se puede resolver.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from aucove.config import base as base_mod
from aucove.config.base import BaseSnapshot, read_base, read_policy_dir, read_tree_base, to_policy
from aucove.ops.result import OpError
from tests.conftest import FIXTURES, GIT_OPTIONS, GitRepo, roles_fixture

try:
    from aucove.vcs.git import Git
except ImportError:  # pragma: no cover - sin vcs.git
    Git = None  # type: ignore[assignment,misc]

PROJECT = (FIXTURES / "config" / "project" / "minimal.yaml").read_text(encoding="utf-8")
ROLES = roles_fixture("basic")
ROLES_V2 = roles_fixture("no-deputy")
BAD_ROLES = ROLES.replace("lead: ana", "lead: zoe")  # cumple el esquema y rompe REQ-SCHEMA-24
BROKEN_YAML = "version: 1\nlead: [\n"
BAD_PROJECT = PROJECT.replace('core_pin: "0.1.0"', "core_pin: 0.1.0")


class _SubprocessGit:
    """Subconjunto de ``vcs.git.Git`` para cuando ese módulo no está disponible."""

    def __init__(self, root: Path, *, env: Mapping[str, str] | None = None) -> None:
        self.root = Path(root)
        self.env = dict(env or {})

    def run(self, *args: str, check: bool = True, input: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(["git", *args], cwd=self.root, env=self.env, input=input, capture_output=True, check=check)

    def show(self, rev: str, path: str) -> bytes | None:
        proc = self.run("cat-file", "blob", f"{rev}:{path}", check=False)
        return proc.stdout if proc.returncode == 0 else None

    def rev_parse(self, rev: str) -> str | None:
        proc = self.run("rev-parse", "--verify", "--quiet", rev, check=False)
        return proc.stdout.decode().strip() or None if proc.returncode == 0 else None

    def ls_tree(self, rev: str, paths: tuple[str, ...] | list[str] = ()) -> dict[str, tuple[str, str]]:
        proc = self.run("ls-tree", "-r", "-z", "--full-tree", rev, "--", *paths)
        out = {}
        for record in proc.stdout.split(b"\0"):
            if record:
                meta, _, name = record.partition(b"\t")
                mode, _type, obj = meta.decode().split()
                out[name.decode()] = (mode, obj)
        return out

    def first_parent_revs(self, rev: str, path: str, max_count: int = 100) -> list[str]:
        proc = self.run("rev-list", "--first-parent", f"--max-count={max_count}", rev, "--", path)
        return proc.stdout.decode().split()


def _git(repo: GitRepo) -> object:
    if Git is not None:
        return Git(repo.path, env=repo.env)
    return _SubprocessGit(repo.path, env=repo.env)  # pragma: no cover


def _set_head(repo: GitRepo, branch: str = "main") -> None:
    """``refs/remotes/origin/HEAD`` -> ``refs/remotes/origin/<branch>``, sin consultar el remoto."""
    repo.git("symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}")


def _publish(repo: GitRepo, files: Mapping[str, str], message: str = "commit") -> str:
    sha = repo.commit(message, files=files)
    repo.push("HEAD:main")
    repo.fetch()
    _set_head(repo)
    return sha


def _unavailable(info: pytest.ExceptionInfo[OpError]) -> dict:  # type: ignore[type-arg]
    assert info.value.code == "E_CONFIG_POLICY_UNAVAILABLE"
    assert info.value.details is not None
    return info.value.details


# -- fast-import ---------------------------------------------------------------------


class Stream:
    """Historia sintética para ``git fast-import`` (marcas :1, :2, ...)."""

    def __init__(self) -> None:
        self.parts: list[bytes] = []
        self.mark = 0

    def commit(
        self,
        files: Mapping[str, str],
        *,
        ref: str = "refs/remotes/origin/main",
        parent: int | None = None,
        merge: int | None = None,
    ) -> int:
        self.mark += 1
        message = f"c{self.mark}".encode()
        head = (
            f"commit {ref}\nmark :{self.mark}\n"
            f"committer Test <test@example.invalid> {1790000000 + 60 * self.mark} +0000\n"
            f"data {len(message)}\n"
        ).encode()
        body = head + message + b"\n"
        if parent is not None:
            body += f"from :{parent}\n".encode()
        if merge is not None:
            body += f"merge :{merge}\n".encode()
        for path, content in files.items():
            data = content.encode("utf-8")
            body += f"M 100644 inline {path}\ndata {len(data)}\n".encode() + data + b"\n"
        self.parts.append(body)
        return self.mark

    def run(self, repo: GitRepo, tmp: Path) -> dict[int, str]:
        marks = tmp / "marks.txt"
        subprocess.run(
            ["git", *GIT_OPTIONS, "fast-import", "--quiet", f"--export-marks={marks}"],
            cwd=repo.path, env=dict(repo.env), input=b"".join(self.parts), capture_output=True, check=True,
        )
        out = {}
        for line in marks.read_text(encoding="ascii").splitlines():
            mark, sha = line.split()
            out[int(mark[1:])] = sha
        _set_head(repo)
        return out


# -- lectura en vivo -------------------------------------------------------------------


def test_read_base_live(git_repo: GitRepo) -> None:
    sha = _publish(
        git_repo,
        {
            "project.yaml": PROJECT,
            "roles.yaml": ROLES,
            "memory/decisions/2026-09-01-1-alcance.md": "---\nid: x\n---\n",
            "memory/decisions/2026-09-02-1-datos.md": "---\nid: y\n---\n",
            "memory/decisions/.gitkeep": "",
            "memory/decisions/sub/otro.md": "x\n",
            "memory/notes/task-1.md": "x\n",
        },
    )
    snap = read_base(_git(git_repo), "main")  # type: ignore[arg-type]
    assert isinstance(snap, BaseSnapshot)
    assert (snap.ref, snap.sha, snap.source, snap.fallback_sha) == ("origin/main", sha, "live", None)
    assert dict(snap.fallbacks) == {}
    assert snap.project.base_branch == "main" and snap.project.language == "es"
    assert snap.roles.lead == "ana" and snap.roles.deputy == "bruno"
    assert snap.decisions == frozenset(
        {"memory/decisions/2026-09-01-1-alcance.md", "memory/decisions/2026-09-02-1-datos.md"}
    )


def test_working_tree_and_local_commits_do_not_count(git_repo: GitRepo) -> None:
    """REQ-PERM-1 y REQ-PERM-2: solo cuenta la punta de origin/<base>."""
    sha = _publish(git_repo, {"project.yaml": PROJECT, "roles.yaml": ROLES})
    git_repo.commit("local", files={"roles.yaml": ROLES_V2})
    git_repo.write({"roles.yaml": BROKEN_YAML})
    snap = read_base(_git(git_repo), "main")  # type: ignore[arg-type]
    assert snap.sha == sha and snap.roles.deputy == "bruno" and snap.fallback_sha is None


def test_without_fetch_the_policy_is_unavailable(git_repo: GitRepo) -> None:
    """REQ-PERM-3: sin origin/HEAD ni capabilities.json no hay base (REQ-HOOK-41)."""
    git_repo.commit("local", files={"project.yaml": PROJECT, "roles.yaml": ROLES})
    with pytest.raises(OpError) as info:
        read_base(_git(git_repo))  # type: ignore[arg-type]
    details = _unavailable(info)
    assert details == {"ref": "origin/HEAD", "reason": "ref_missing"}


def test_other_remote_name(git_repo: GitRepo) -> None:
    _publish(git_repo, {"project.yaml": PROJECT, "roles.yaml": ROLES})
    with pytest.raises(OpError) as info:
        read_base(_git(git_repo), remote="upstream")  # type: ignore[arg-type]
    assert _unavailable(info)["ref"] == "upstream/HEAD"


@pytest.mark.parametrize("remote", ["bad remote", "-o", "", "origin\n"])
def test_remote_is_checked_before_calling_git(remote: str) -> None:
    class Boom:
        def __getattr__(self, name: str) -> object:
            raise AssertionError("no debe llamar a git")

    with pytest.raises(OpError) as info:
        read_base(Boom(), "main", remote=remote)  # type: ignore[arg-type]
    assert _unavailable(info)["reason"] == "invalid_ref"


@pytest.mark.parametrize("declared", ["-x", "a..b", "x.lock", "x."])
def test_declared_base_name_is_checked_before_building_the_ref(declared: str) -> None:
    """El nombre que declara origin/HEAD no llega a git si no es una rama admitida."""
    project = PROJECT.replace("  repo: example-org/demo\n", f"  repo: example-org/demo\n  base_branch: \"{declared}\"\n")
    calls: list[str] = []

    class HeadOnly:
        def rev_parse(self, rev: str) -> str | None:
            calls.append(rev)
            return "a" * 40 if rev == "refs/remotes/origin/HEAD" else None

        def show(self, rev: str, path: str) -> bytes | None:
            return project.encode("utf-8") if path == "project.yaml" else None

        def run(self, *args: str, check: bool = True, input: bytes | None = None) -> bytes:
            return b""  # rev-list sin historia: no hay respaldo

        def first_parent_revs(self, rev: str, path: str, max_count: int = 100) -> list[str]:
            return []

    with pytest.raises(OpError) as info:
        read_base(HeadOnly())  # type: ignore[arg-type]
    details = _unavailable(info)
    if details.get("reason") == "invalid_ref":  # el esquema lo admite: lo frena _valid_branch
        assert details["ref"] == f"origin/{declared}"
    else:  # el esquema ya lo rechaza: sin versión válida de project.yaml
        assert details == {"ref": "origin/HEAD", "path": "project.yaml", "reason": "no_valid_version"}
    assert calls == ["refs/remotes/origin/HEAD"]


def test_git_failures_are_unavailable() -> None:
    class Failing:
        def rev_parse(self, rev: str) -> str:
            raise RuntimeError("git se cayó")

    with pytest.raises(OpError) as info:
        read_base(Failing(), "main")  # type: ignore[arg-type]
    assert _unavailable(info)["reason"] == "git_failed"
    assert isinstance(info.value.__cause__, RuntimeError)


TRUNK = PROJECT.replace("  repo: example-org/demo\n", "  repo: example-org/demo\n  base_branch: trunk\n")


def test_base_branch_is_confirmed_by_the_base(git_repo: GitRepo) -> None:
    """La rama por defecto (main) declara trunk; el project.yaml de origin/trunk debe decir lo mismo."""
    _publish(git_repo, {"project.yaml": TRUNK, "roles.yaml": ROLES})
    git_repo.commit("trunk", files={"project.yaml": PROJECT})
    git_repo.push("HEAD:trunk")
    git_repo.fetch()
    with pytest.raises(OpError) as info:
        read_base(_git(git_repo))  # type: ignore[arg-type]
    assert _unavailable(info) == {"ref": "origin/trunk", "reason": "base_branch_mismatch", "declared": "main"}


def test_declared_base_without_its_remote_ref(git_repo: GitRepo) -> None:
    _publish(git_repo, {"project.yaml": TRUNK, "roles.yaml": ROLES})
    with pytest.raises(OpError) as info:
        read_base(_git(git_repo))  # type: ignore[arg-type]
    assert _unavailable(info) == {"ref": "origin/trunk", "reason": "ref_missing"}


def test_other_base_branch(git_repo: GitRepo) -> None:
    sha = git_repo.commit("c", files={"project.yaml": TRUNK, "roles.yaml": ROLES})
    git_repo.push("HEAD:trunk")
    git_repo.fetch()
    _set_head(git_repo, "trunk")
    snap = read_base(_git(git_repo))  # type: ignore[arg-type]
    assert snap.ref == "origin/trunk" and snap.sha == sha


def test_default_branch_other_than_the_base(git_repo: GitRepo) -> None:
    """origin/HEAD apunta a main, que declara trunk como base: la política es la de origin/trunk."""
    _publish(git_repo, {"project.yaml": TRUNK, "roles.yaml": ROLES_V2})
    sha = git_repo.commit("trunk", files={"roles.yaml": ROLES})
    git_repo.push("HEAD:trunk")
    git_repo.fetch()
    snap = read_base(_git(git_repo), "main")  # type: ignore[arg-type]
    assert snap.ref == "origin/trunk" and snap.sha == sha and snap.roles.deputy == "bruno"


# -- respaldo de REQ-PERM-4 --------------------------------------------------------------


def test_invalid_roles_at_the_tip_fall_back_to_the_last_valid_version(git_repo: GitRepo) -> None:
    first = _publish(git_repo, {"project.yaml": PROJECT, "roles.yaml": ROLES_V2})
    good = _publish(git_repo, {"roles.yaml": ROLES})
    _publish(git_repo, {"notes.txt": "x\n"})
    _publish(git_repo, {"roles.yaml": BAD_ROLES})
    tip = _publish(git_repo, {"roles.yaml": BROKEN_YAML})
    snap = read_base(_git(git_repo), "main")  # type: ignore[arg-type]
    assert snap.sha == tip and snap.fallback_sha == good != first
    assert dict(snap.fallbacks) == {"roles.yaml": good}
    assert snap.roles.deputy == "bruno"  # la versión de 'good', no la de 'first'


def test_invalid_project_at_the_tip_falls_back(git_repo: GitRepo) -> None:
    good = _publish(git_repo, {"project.yaml": PROJECT, "roles.yaml": ROLES})
    _publish(git_repo, {"project.yaml": BAD_PROJECT})
    snap = read_base(_git(git_repo), "main")  # type: ignore[arg-type]
    assert snap.fallback_sha == good and dict(snap.fallbacks) == {"project.yaml": good}
    assert snap.project.core_pin == "0.1.0"


def test_both_files_fall_back_independently(git_repo: GitRepo) -> None:
    first = _publish(git_repo, {"project.yaml": PROJECT, "roles.yaml": ROLES})
    second = _publish(git_repo, {"roles.yaml": ROLES_V2})
    _publish(git_repo, {"project.yaml": BAD_PROJECT, "roles.yaml": BAD_ROLES})
    snap = read_base(_git(git_repo), "main")  # type: ignore[arg-type]
    assert dict(snap.fallbacks) == {"project.yaml": first, "roles.yaml": second}
    assert snap.fallback_sha == second  # el de roles.yaml, que decide la autorización
    assert snap.fallback_details() == {"sha": second, "files": {"project.yaml": first, "roles.yaml": second}}


def test_missing_file_at_the_tip_falls_back(git_repo: GitRepo) -> None:
    good = _publish(git_repo, {"project.yaml": PROJECT, "roles.yaml": ROLES})
    git_repo.commit("rm", remove=["roles.yaml"])
    git_repo.push("HEAD:main")
    git_repo.fetch()
    snap = read_base(_git(git_repo), "main")  # type: ignore[arg-type]
    assert snap.fallback_sha == good


def test_without_a_valid_version_the_policy_is_unavailable(git_repo: GitRepo) -> None:
    _publish(git_repo, {"project.yaml": PROJECT, "roles.yaml": BAD_ROLES})
    _publish(git_repo, {"roles.yaml": BROKEN_YAML})
    with pytest.raises(OpError) as info:
        read_base(_git(git_repo), "main")  # type: ignore[arg-type]
    assert _unavailable(info) == {"ref": "origin/main", "path": "roles.yaml", "reason": "no_valid_version"}


def test_without_a_valid_project_the_policy_is_unavailable(git_repo: GitRepo) -> None:
    _publish(git_repo, {"project.yaml": BAD_PROJECT, "roles.yaml": ROLES})
    with pytest.raises(OpError) as info:
        read_base(_git(git_repo), "main")  # type: ignore[arg-type]
    assert _unavailable(info)["path"] == "project.yaml"


@pytest.mark.parametrize(("after", "ok"), [(99, True), (100, False)])
def test_fallback_window_is_100_first_parent_commits(git_repo: GitRepo, tmp_path: Path, after: int, ok: bool) -> None:
    """La versión válida cuenta solo si está en los últimos 100 commits de primer padre."""
    stream = Stream()
    first = stream.commit({"project.yaml": PROJECT, "roles.yaml": ROLES})
    stream.commit({"roles.yaml": BAD_ROLES})
    for i in range(after - 1):
        stream.commit({"notes.txt": f"{i}\n"})
    shas = stream.run(git_repo, tmp_path)
    assert len(shas) == after + 1
    git = _git(git_repo)
    if ok:
        snap = read_base(git, "main")  # type: ignore[arg-type]
        assert snap.fallback_sha == shas[first] and snap.sha == shas[stream.mark]
    else:
        with pytest.raises(OpError) as info:
            read_base(git, "main")  # type: ignore[arg-type]
        assert _unavailable(info)["path"] == "roles.yaml"


def test_a_valid_version_older_than_the_window_counts_if_it_is_present_in_it(git_repo: GitRepo, tmp_path: Path) -> None:
    stream = Stream()
    stream.commit({"project.yaml": PROJECT, "roles.yaml": ROLES})
    for i in range(150):
        stream.commit({"notes.txt": f"{i}\n"})
    stream.commit({"roles.yaml": BROKEN_YAML})
    shas = stream.run(git_repo, tmp_path)
    snap = read_base(_git(git_repo), "main")  # type: ignore[arg-type]
    oldest_in_window = shas[stream.mark - 99]
    assert snap.fallback_sha == oldest_in_window and snap.roles.deputy == "bruno"


def test_fallback_follows_only_the_first_parent(git_repo: GitRepo, tmp_path: Path) -> None:
    """Una versión válida que solo existe en el segundo padre de un merge no cuenta."""
    stream = Stream()
    a = stream.commit({"project.yaml": PROJECT, "roles.yaml": ROLES})
    side = stream.commit({"roles.yaml": ROLES_V2}, ref="refs/heads/side", parent=a)
    b = stream.commit({"roles.yaml": BAD_ROLES}, parent=a)
    stream.commit({"roles.yaml": BROKEN_YAML}, parent=b, merge=side)
    shas = stream.run(git_repo, tmp_path)
    snap = read_base(_git(git_repo), "main")  # type: ignore[arg-type]
    assert snap.fallback_sha == shas[a]
    assert snap.roles.deputy == "bruno"  # la de 'a'; la de 'side' no tiene deputy


# -- directorio de política (CI) y árbol explícito -----------------------------------------


def _write(root: Path, files: Mapping[str, str]) -> None:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))


def test_read_policy_dir(tmp_path: Path) -> None:
    _write(tmp_path, {"project.yaml": PROJECT, "roles.yaml": ROLES, "memory/decisions/2026-09-01-1-a.md": "x\n",
                      "memory/decisions/.gitkeep": "", "memory/decisions/sub/b.md": "x\n"})
    snap = read_policy_dir(tmp_path)
    assert (snap.sha, snap.source, snap.fallback_sha, snap.ref) == (None, "local", None, tmp_path.as_posix())
    assert snap.decisions == frozenset({"memory/decisions/2026-09-01-1-a.md"})
    assert snap.roles.lead == "ana"


def test_read_policy_dir_without_decisions(tmp_path: Path) -> None:
    _write(tmp_path, {"project.yaml": PROJECT, "roles.yaml": ROLES})
    assert read_policy_dir(str(tmp_path)).decisions == frozenset()


@pytest.mark.parametrize(
    ("files", "path"),
    [
        ({"project.yaml": PROJECT}, "roles.yaml"),
        ({"roles.yaml": ROLES}, "project.yaml"),
        ({"project.yaml": PROJECT, "roles.yaml": BAD_ROLES}, "roles.yaml"),
        ({"project.yaml": BAD_PROJECT, "roles.yaml": ROLES}, "project.yaml"),
    ],
)
def test_read_policy_dir_without_valid_files(tmp_path: Path, files: Mapping[str, str], path: str) -> None:
    _write(tmp_path, files)
    with pytest.raises(OpError) as info:
        read_policy_dir(tmp_path)
    assert _unavailable(info)["path"] == path


def test_read_policy_dir_ignores_symlinks(tmp_path: Path) -> None:
    _write(tmp_path, {"project.yaml": PROJECT, "real-roles.yaml": ROLES})
    try:
        (tmp_path / "roles.yaml").symlink_to(tmp_path / "real-roles.yaml")
    except (OSError, NotImplementedError):
        pytest.skip("sin permiso para crear enlaces simbólicos")
    with pytest.raises(OpError):
        read_policy_dir(tmp_path)


def test_read_policy_dir_falls_back_in_a_checkout(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    good = git_repo.commit("a", files={"project.yaml": PROJECT, "roles.yaml": ROLES})
    tip = git_repo.commit("b", files={"roles.yaml": BAD_ROLES})
    for key, value in git_repo.env.items():
        monkeypatch.setenv(key, value)
    snap = read_policy_dir(git_repo.path)
    assert (snap.sha, snap.fallback_sha, snap.source) == (tip, good, "local")
    assert dict(snap.fallbacks) == {"roles.yaml": good}


def test_read_policy_dir_uses_head_when_the_disk_copy_is_modified(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    tip = git_repo.commit("a", files={"project.yaml": PROJECT, "roles.yaml": ROLES})
    git_repo.write({"roles.yaml": BROKEN_YAML})
    for key, value in git_repo.env.items():
        monkeypatch.setenv(key, value)
    snap = read_policy_dir(git_repo.path)
    assert snap.fallback_sha == tip


def test_read_policy_dir_without_git_has_no_fallback(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    git_repo.commit("a", files={"project.yaml": PROJECT, "roles.yaml": ROLES})
    git_repo.commit("b", files={"roles.yaml": BAD_ROLES})
    monkeypatch.setattr(base_mod, "_dir_git", lambda root: None)
    with pytest.raises(OpError):
        read_policy_dir(git_repo.path)


def test_read_policy_dir_survives_git_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, {"project.yaml": PROJECT, "roles.yaml": BAD_ROLES})

    class Failing:
        def rev_parse(self, rev: str) -> str:
            raise RuntimeError("x")

    monkeypatch.setattr(base_mod, "_dir_git", lambda root: Failing())
    with pytest.raises(OpError) as info:
        read_policy_dir(tmp_path)
    assert _unavailable(info)["path"] == "roles.yaml"


def test_read_tree_base(git_repo: GitRepo) -> None:
    sha = _publish(git_repo, {"project.yaml": PROJECT, "roles.yaml": ROLES})
    snap = read_tree_base(git_repo.path, env=git_repo.env)
    assert snap.sha == sha and snap.source == "live"


def test_read_tree_base_does_not_need_a_valid_tree_project(git_repo: GitRepo) -> None:
    """El project.yaml del árbol es un dato de la regla schemas, no la fuente de la base (REQ-HOOK-41)."""
    sha = _publish(git_repo, {"project.yaml": PROJECT, "roles.yaml": ROLES})
    git_repo.write({"project.yaml": BAD_PROJECT})
    snap = read_tree_base(git_repo.path, env=git_repo.env)
    assert snap.sha == sha and snap.project.core_pin == "0.1.0"


def test_read_tree_base_without_git(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    git_repo.write({"project.yaml": PROJECT})
    import aucove.vcs.git as vcs_git

    def unavailable(*args: object, **kwargs: object) -> object:
        raise vcs_git.GitUnavailable("git")

    monkeypatch.setattr(vcs_git, "Git", unavailable)
    with pytest.raises(OpError) as info:
        read_tree_base(git_repo.path)
    assert info.value.code == "E_CONFIG_GIT_UNAVAILABLE"


# -- Policy ---------------------------------------------------------------------------


def _snapshot(tmp_path: Path, roles: str) -> BaseSnapshot:
    _write(tmp_path, {"project.yaml": PROJECT, "roles.yaml": roles, "memory/decisions/2026-09-01-1-a.md": "x\n"})
    return read_policy_dir(tmp_path)


def test_to_policy(tmp_path: Path) -> None:
    roles = roles_fixture("mixed-case") + ""
    roles = roles.replace("  - login: dario\n", "  - login: dario\n    active: false\n")
    snap = _snapshot(tmp_path, roles)
    pol = to_policy(snap, root="/repo", ci_fs=False, case_map={"a": "A"}, link_probe=None)
    assert pol.people == frozenset({"ana", "bruno", "carla"})  # dario tiene active: false
    assert (pol.lead, pol.deputy) == ("ana", "bruno")
    assert dict(pol.sections) == {"introduction": "ana", "methods": "carla", "results": "bruno"}
    assert pol.shown("carla") == "Carla" and pol.shown("ana") == "Ana" and pol.shown("dario") == "dario"
    assert pol.base_decisions == frozenset({"memory/decisions/2026-09-01-1-a.md"})
    assert pol.case_map is None and pol.link_probe is None and pol.root == "/repo" and pol.ci_fs is False
    assert pol.approvers == frozenset({"ana", "bruno"})


def test_to_policy_passes_case_map_only_on_case_insensitive_filesystems(tmp_path: Path) -> None:
    snap = _snapshot(tmp_path, ROLES)

    def probe(rel: str) -> bool:
        return False

    pol = to_policy(snap, root="C:/repo", ci_fs=True, case_map={"tex/a.tex": "tex/A.tex"}, link_probe=probe)
    assert pol.case_map == {"tex/a.tex": "tex/A.tex"} and pol.link_probe is probe


def test_to_policy_without_deputy(tmp_path: Path) -> None:
    pol = to_policy(_snapshot(tmp_path, ROLES_V2), root="/r", ci_fs=False)
    assert pol.deputy is None and pol.approvers == frozenset({"ana"})


# -- integración con OpContext -----------------------------------------------------------


def test_op_context_reads_the_base(git_repo: GitRepo) -> None:
    from aucove.ops.context import Invocation, OpContext

    sha = _publish(git_repo, {"project.yaml": PROJECT, "roles.yaml": ROLES})
    inv = Invocation(root=str(git_repo.path), lang=None, json=True, dry_run=False, surface="cli", env=dict(git_repo.env))
    ctx = OpContext(inv)
    try:
        snap = ctx.base()
        assert snap.sha == sha and ctx.base() is snap
        assert ctx.t().lang == "es"  # language de la base
    finally:
        ctx.close()
