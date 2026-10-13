# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Git por subprocess (01 REQ-ARCH-64, REQ-ARCH-97) y lecturas de ``vcs.git.Git``."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from aucove import _meta
from aucove.vcs import git as git_mod
from aucove.vcs.git import (
    Git,
    GitError,
    GitTimeout,
    GitUnavailable,
    ObjectUnavailable,
    child_env,
    is_offline,
    which_outside,
)
from tests.conftest import GitRepo

WINDOWS = os.name == "nt"
HAS_GIT = shutil.which("git") is not None
needs_git = pytest.mark.skipif(not HAS_GIT, reason="git no está disponible")


def _same(a: str | Path | None, b: str | Path) -> bool:
    return a is not None and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _make_exe(directory: Path, name: str, *, ext: str | None = None, executable: bool = True) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    suffix = ext if ext is not None else (".exe" if WINDOWS else "")
    path = directory / f"{name}{suffix}"
    path.write_bytes(b"#!/bin/sh\nexit 0\n")
    path.chmod(0o755 if executable else 0o644)
    return path


# -- which_outside (REQ-ARCH-97) ----------------------------------------------------


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return {
        "root": root,
        "inside": root / "tools" / "bin",
        "outside": tmp_path / "sys" / "bin",
        "other": tmp_path / "other" / "bin",
        "cwd": tmp_path / "work",
    }


def test_which_outside_finds_absolute_path_outside_repo(layout: dict[str, Path]) -> None:
    exe = _make_exe(layout["outside"], "tool")
    layout["cwd"].mkdir()
    found = which_outside("tool", layout["root"], path=str(layout["outside"]), cwd=layout["cwd"])
    assert found is not None and os.path.isabs(found)
    assert _same(found, exe)


def test_which_outside_skips_directories_inside_repo(layout: dict[str, Path]) -> None:
    _make_exe(layout["inside"], "tool")
    _make_exe(layout["root"], "tool")
    exe = _make_exe(layout["outside"], "tool")
    layout["cwd"].mkdir()
    search = os.pathsep.join([str(layout["root"]), str(layout["inside"]), str(layout["outside"])])
    assert _same(which_outside("tool", layout["root"], path=search, cwd=layout["cwd"]), exe)
    only_inside = os.pathsep.join([str(layout["root"]), str(layout["inside"])])
    assert which_outside("tool", layout["root"], path=only_inside, cwd=layout["cwd"]) is None


def test_which_outside_skips_current_directory(layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    _make_exe(layout["cwd"], "tool")
    exe = _make_exe(layout["outside"], "tool")
    search = os.pathsep.join([str(layout["cwd"]), str(layout["outside"])])
    assert _same(which_outside("tool", layout["root"], path=search, cwd=layout["cwd"]), exe)
    monkeypatch.chdir(layout["cwd"])  # sin cwd explícito, se usa os.getcwd()
    assert _same(which_outside("tool", layout["root"], path=search), exe)
    assert which_outside("tool", layout["root"], path=str(layout["cwd"])) is None


def test_which_outside_ignores_empty_and_relative_entries(
    layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_exe(layout["cwd"], "tool")
    _make_exe(layout["cwd"] / "bin", "tool")
    monkeypatch.chdir(layout["cwd"])
    search = os.pathsep.join(["", ".", "bin", os.path.join(".", "bin")])
    assert which_outside("tool", layout["root"], path=search) is None


def test_which_outside_rejects_executable_resolving_into_repo(layout: dict[str, Path]) -> None:
    target = _make_exe(layout["inside"], "tool")
    layout["outside"].mkdir(parents=True)
    layout["cwd"].mkdir()
    link = layout["outside"] / target.name
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("no se pueden crear enlaces simbólicos aquí")
    assert which_outside("tool", layout["root"], path=str(layout["outside"]), cwd=layout["cwd"]) is None


def test_which_outside_rejects_path_entry_linked_into_repo(layout: dict[str, Path]) -> None:
    """Una entrada de PATH que es un enlace (o una unión de Windows) al repositorio no cuenta."""
    _make_exe(layout["inside"], "tool")
    layout["cwd"].mkdir()
    linked = layout["outside"].parent / "linked-bin"
    linked.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(layout["inside"], linked, target_is_directory=True)
    except (OSError, NotImplementedError):
        if not WINDOWS:
            pytest.skip("no se pueden crear enlaces simbólicos aquí")
        import _winapi

        _winapi.CreateJunction(str(layout["inside"]), str(linked))
    assert (linked / ("tool.exe" if WINDOWS else "tool")).is_file()
    assert which_outside("tool", layout["root"], path=str(linked), cwd=layout["cwd"]) is None


def test_which_outside_explicit_paths(layout: dict[str, Path]) -> None:
    outside = _make_exe(layout["outside"], "tool")
    inside = _make_exe(layout["inside"], "tool")
    layout["cwd"].mkdir()
    assert _same(which_outside(str(outside), layout["root"], path="", cwd=layout["cwd"]), outside)
    assert which_outside(str(inside), layout["root"], path="", cwd=layout["cwd"]) is None
    assert which_outside(os.path.join("sys", "bin", outside.name), layout["root"], path="") is None
    assert which_outside("", layout["root"], path=str(layout["outside"])) is None
    assert which_outside("to\0ol", layout["root"], path=str(layout["outside"])) is None


def test_which_outside_missing_tool(layout: dict[str, Path]) -> None:
    layout["outside"].mkdir(parents=True)
    assert which_outside("gitleaks", layout["root"], path=str(layout["outside"])) is None


@pytest.mark.skipif(not WINDOWS, reason="PATHEXT y extensiones solo en Windows")
def test_which_outside_windows_only_exe_and_com(layout: dict[str, Path]) -> None:
    _make_exe(layout["outside"], "tool", ext=".bat")
    _make_exe(layout["outside"], "tool", ext=".cmd")
    layout["cwd"].mkdir()
    search = str(layout["outside"])
    assert which_outside("tool", layout["root"], path=search, pathext=".BAT;.CMD;.EXE", cwd=layout["cwd"]) is None
    assert which_outside("tool.bat", layout["root"], path=search, cwd=layout["cwd"]) is None
    exe = _make_exe(layout["outside"], "tool", ext=".exe")
    assert _same(which_outside("tool", layout["root"], path=search, pathext=".BAT;.EXE", cwd=layout["cwd"]), exe)
    assert _same(which_outside("tool.exe", layout["root"], path=search, cwd=layout["cwd"]), exe)


@pytest.mark.skipif(WINDOWS, reason="Windows no tiene bit de ejecución")
def test_which_outside_requires_exec_bit(layout: dict[str, Path]) -> None:
    _make_exe(layout["outside"], "tool", executable=False)
    layout["cwd"].mkdir()
    assert which_outside("tool", layout["root"], path=str(layout["outside"]), cwd=layout["cwd"]) is None


# -- entorno, timeout y ruta absoluta (REQ-ARCH-64, REQ-ARCH-97) ---------------------


class _Recorder:
    """Sustituto de ``git.run_bounded`` que registra las llamadas."""

    def __init__(self, *, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"", exc: BaseException | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.returncode, self.stdout, self.stderr, self.exc = returncode, stdout, stderr, exc

    def __call__(self, cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        self.calls.append((list(cmd), kwargs))
        if self.exc is not None:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


@pytest.fixture
def fake_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Callable[..., Git], _Recorder, Path]:
    bindir = tmp_path / "bin"
    exe = _make_exe(bindir, "git")
    root = tmp_path / "repo"
    root.mkdir()
    rec = _Recorder()
    monkeypatch.setattr(git_mod, "run_bounded", rec)

    def make(**env: str) -> Git:
        base = {"PATH": str(bindir), "GIT_DIR": str(tmp_path / "elsewhere"), "GIT_INDEX_FILE": "x", "KEEP": "1"}
        base.update(env)
        return Git(root, env=base, timeout_s=7.5)

    return make, rec, exe


def test_run_uses_absolute_git_path_and_safe_env(fake_git: tuple[Callable[..., Git], _Recorder, Path]) -> None:
    make, rec, exe = fake_git
    g = make()
    assert _same(g.exe, exe) and os.path.isabs(g.exe)
    g.run("rev-parse", "HEAD")
    cmd, kw = rec.calls[-1]
    assert _same(cmd[0], exe) and cmd[1:] == ["rev-parse", "HEAD"]
    env = kw["env"]
    assert isinstance(env, dict)
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    assert "GIT_DIR" not in env and "GIT_INDEX_FILE" not in env
    assert env["KEEP"] == "1"
    if WINDOWS:
        assert env["NoDefaultCurrentDirectoryInExePath"] == "1"
    assert kw["timeout"] == 7.5
    assert kw["input"] is None  # run_bounded: sin input, stdin es /dev/null
    assert kw["cwd"] == g.root
    assert kw.get("shell") in (None, False)


def test_writes_keep_optional_locks(fake_git: tuple[Callable[..., Git], _Recorder, Path]) -> None:
    make, rec, _exe = fake_git
    g = make(GIT_OPTIONAL_LOCKS="1")
    g.run("commit", "-q", "-m", "x")
    g.run("remote", "add", "x", "y", read=False)
    for _cmd, kw in rec.calls:
        env = kw["env"]
        assert isinstance(env, dict)
        assert "GIT_OPTIONAL_LOCKS" not in env
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert kw["timeout"] == 7.5
    g.run("hash-object", "--stdin", input=b"data", timeout_s=2)
    _cmd, kw = rec.calls[-1]
    assert kw["input"] == b"data" and kw["timeout"] == 2.0


def test_every_read_method_has_timeout_and_read_env(fake_git: tuple[Callable[..., Git], _Recorder, Path]) -> None:
    make, rec, _exe = fake_git
    g = make()
    g.show("HEAD", "roles.yaml")
    g.rev_parse("HEAD^1")
    g.ls_tree("HEAD")
    g.diff_name_status("HEAD", None)
    g.diff_name_status("HEAD~1", "HEAD")
    g.merge_base("a", "b")
    g.first_parent_revs("HEAD", "roles.yaml")
    g.current_branch()
    g.remote_url()
    g.ls_files()
    g.ls_files(others=True)
    g.index_modes()
    assert len(rec.calls) >= 12
    for cmd, kw in rec.calls:
        env = kw["env"]
        assert isinstance(env, dict)
        assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_OPTIONAL_LOCKS"] == "0", cmd
        assert kw["timeout"] == 7.5, cmd
        assert os.path.isabs(cmd[0])


def test_timeout_raises_git_timeout(fake_git: tuple[Callable[..., Git], _Recorder, Path]) -> None:
    make, rec, _exe = fake_git
    g = make()
    rec.exc = subprocess.TimeoutExpired(["git"], 7.5)
    with pytest.raises(GitTimeout) as info:
        g.run("status", check=False)
    assert isinstance(info.value, GitError) and isinstance(info.value, subprocess.CalledProcessError)
    with pytest.raises(GitTimeout):
        g.show("HEAD", "x")


def test_launch_failure_is_git_unavailable(fake_git: tuple[Callable[..., Git], _Recorder, Path]) -> None:
    make, rec, _exe = fake_git
    g = make()
    rec.exc = FileNotFoundError("git")
    with pytest.raises(GitUnavailable):
        g.run("status")


def test_check_raises_git_error_without_output_in_message(fake_git: tuple[Callable[..., Git], _Recorder, Path]) -> None:
    make, rec, _exe = fake_git
    g = make()
    rec.returncode, rec.stderr = 128, b"fatal: https://user:tok3n@example.invalid"
    with pytest.raises(GitError) as info:
        g.run("fetch", "origin")
    assert info.value.returncode == 128
    assert "tok3n" not in str(info.value)
    assert g.run("fetch", "origin", check=False).returncode == 128


def test_git_unavailable_without_git_outside_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    _make_exe(root / "bin", "git")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(GitUnavailable):
        Git(root, env={"PATH": str(empty)})
    with pytest.raises(GitUnavailable):
        Git(root, env={"PATH": str(root / "bin")})
    with pytest.raises(GitUnavailable):
        Git(root, env={"PATH": ""})
    # un entorno sin PATH busca con el PATH del proceso
    outside = _make_exe(tmp_path / "sys", "git")
    monkeypatch.setenv("PATH", str(outside.parent))
    assert _same(Git(root, env={}).exe, outside)
    monkeypatch.setenv("PATH", str(root / "bin"))
    with pytest.raises(GitUnavailable):
        Git(root, env={})


def test_child_env_contract() -> None:
    env = child_env({"GIT_WORK_TREE": "/x", "GIT_TERMINAL_PROMPT": "1", "A": "b"}, read=True)
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_OPTIONAL_LOCKS"] == "0"
    assert "GIT_WORK_TREE" not in env and env["A"] == "b"
    assert "GIT_OPTIONAL_LOCKS" not in child_env({"GIT_OPTIONAL_LOCKS": "0"}, read=False)
    variants = child_env({"git_terminal_prompt": "1", "Git_Dir": "/x", "B": "c"}, read=False)
    assert variants == {"B": "c", "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"} | (
        {"NoDefaultCurrentDirectoryInExePath": "1"} if WINDOWS else {}
    )
    if WINDOWS:
        assert env["NoDefaultCurrentDirectoryInExePath"] == "1"


def test_child_env_offline_blocks_every_transport() -> None:
    """Sin red, git no descarga objetos de un clon parcial ni abre transportes (REQ-ARCH-63)."""
    flag = _meta.ENV_PREFIX + "OFFLINE"
    user = {"GIT_ALLOW_PROTOCOL": "https", "A": "b"}
    online = child_env(user, read=True)
    assert online["GIT_ALLOW_PROTOCOL"] == "https" and "GIT_NO_LAZY_FETCH" not in online
    for env, kwargs in (({**user, flag: "1"}, {}), (user, {"offline": True})):
        for read in (True, False):
            out = child_env(env, read=read, **kwargs)
            assert out["GIT_NO_LAZY_FETCH"] == "1" and out["GIT_ALLOW_PROTOCOL"] == "", (env, kwargs, read)
            assert out["A"] == "b" and out["GIT_TERMINAL_PROMPT"] == "0"
    variants = child_env({flag: "1", "git_allow_protocol": "https", "Git_No_Lazy_Fetch": "0"})
    assert variants["GIT_ALLOW_PROTOCOL"] == "" and variants["GIT_NO_LAZY_FETCH"] == "1"
    assert "git_allow_protocol" not in variants and "Git_No_Lazy_Fetch" not in variants
    for value in ("0", "true", " 1", ""):
        assert "GIT_NO_LAZY_FETCH" not in child_env({flag: value}), value
        assert not is_offline({flag: value})
    assert is_offline({flag: "1"}) and not is_offline({})


def test_offline_env_reaches_every_call(fake_git: tuple[Callable[..., Git], _Recorder, Path]) -> None:
    make, rec, exe = fake_git
    g = make(**{_meta.ENV_PREFIX + "OFFLINE": "1"})
    assert g.offline
    g.run("fetch", "origin", check=False)
    g.show("HEAD", "x")
    g.ls_tree("HEAD")
    assert len(rec.calls) == 3
    for cmd, kw in rec.calls:
        env = kw["env"]
        assert isinstance(env, dict)
        assert env["GIT_NO_LAZY_FETCH"] == "1" and env["GIT_ALLOW_PROTOCOL"] == "", cmd
    rec.calls.clear()
    online = make()
    assert not online.offline
    online.show("HEAD", "x")
    env = rec.calls[-1][1]["env"]
    assert isinstance(env, dict) and "GIT_NO_LAZY_FETCH" not in env and "GIT_ALLOW_PROTOCOL" not in env
    forced = Git(online.root, env={"PATH": str(exe.parent)}, offline=True)  # hooks (REQ-ARCH-51)
    assert forced.offline
    forced.run("status")
    env = rec.calls[-1][1]["env"]
    assert isinstance(env, dict) and env["GIT_NO_LAZY_FETCH"] == "1"


def test_ls_tree_does_not_ask_for_blob_sizes(fake_git: tuple[Callable[..., Git], _Recorder, Path]) -> None:
    """``ls_tree`` no usa ``-l``: en un clon parcial no descarga cada blob para medirlo."""
    make, rec, _exe = fake_git
    g = make()
    a, b, c = "a" * 40, "b" * 40, "c" * 40
    rec.stdout = f"100644 blob {a}\tx.txt\x00".encode()
    assert g.ls_tree("HEAD") == {"x.txt": ("100644", a)}
    assert "-l" not in rec.calls[-1][0]
    records = [f"100644 blob {a}      12\tx.txt", f"160000 commit {b}       -\tsub", f"100644 blob {c}     BAD\ty.txt"]
    rec.stdout = "\x00".join(records).encode() + b"\x00"
    assert g.ls_tree_long("HEAD") == {"x.txt": ("100644", a, 12), "sub": ("160000", b, None), "y.txt": ("100644", c, None)}
    assert "-l" in rec.calls[-1][0]


# -- ejecución con el tiempo acotado (REQ-ARCH-64) ------------------------------------


def test_run_bounded_collects_output_input_and_exit_code(tmp_path: Path) -> None:
    run = git_mod.run_bounded
    echo = "import sys; d = sys.stdin.buffer.read(); sys.stdout.buffer.write(d[::-1]); sys.stderr.write('err'); sys.exit(3)"
    proc = run([sys.executable, "-c", echo], cwd=tmp_path, env=os.environ, input=b"abc", timeout=60)
    assert (proc.returncode, proc.stdout, proc.stderr) == (3, b"cba", b"err")
    # sin input, stdin es /dev/null: la lectura termina de inmediato
    count = "import sys; print(len(sys.stdin.buffer.read()))"
    proc = run([sys.executable, "-c", count], cwd=tmp_path, env=os.environ, input=None, timeout=60)
    assert proc.stdout.strip() == b"0" and proc.returncode == 0
    # entrada y salida grandes a la vez: ninguna tubería llena bloquea a la otra
    big = b"y" * (6 << 20)
    copy = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.stderr.buffer.write(b'e' * (1 << 20))"
    proc = run([sys.executable, "-c", copy], cwd=tmp_path, env=os.environ, input=big, timeout=60)
    assert proc.stdout == big and len(proc.stderr) == 1 << 20
    # el directorio de trabajo es cwd
    proc = run([sys.executable, "-c", "import os; print(os.getcwd())"], cwd=tmp_path, env=os.environ, input=None, timeout=60)
    assert _same(proc.stdout.decode().strip(), tmp_path)


def test_run_bounded_launch_failure_is_oserror(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        git_mod.run_bounded([str(tmp_path / "missing-git")], cwd=tmp_path, env=os.environ, input=None, timeout=5)


def test_run_bounded_timeout_stays_bounded_without_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin Job Object (no se pudo crear), el plazo sigue acotado: se abandonan las tuberías."""
    monkeypatch.setattr(git_mod, "_win_api", lambda: None)
    holder = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(8)'], stdout=sys.stdout, stderr=sys.stderr); time.sleep(8)"
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        git_mod.run_bounded([sys.executable, "-c", holder], cwd=tmp_path, env=os.environ, input=None, timeout=0.3)
    assert time.monotonic() - started < 0.3 + git_mod._DRAIN_S + 1.5  # el nieto, en cambio, duerme 8 s


def test_argument_validation(fake_git: tuple[Callable[..., Git], _Recorder, Path]) -> None:
    make, rec, _exe = fake_git
    g = make()
    with pytest.raises(ValueError):
        g.show("--output=/tmp/x", "a")
    with pytest.raises(ValueError):
        g.ls_tree("-r")
    with pytest.raises(ValueError):
        g.run("show", "a\0b")
    assert g.rev_parse("--all") is None
    assert g.rev_parse("") is None
    assert g.remote_url("--help") is None
    assert rec.calls == []


# -- lecturas con git real ------------------------------------------------------------


def _git(repo: GitRepo) -> Git:
    return Git(repo.path, env=repo.env)


def _commit_index(repo: GitRepo, message: str) -> str:
    """Commit del índice tal como está (sin ``add -A``)."""
    repo.git("commit", "-q", "--no-verify", "-m", message)
    return repo.head()


@needs_git
def test_rev_parse_root_commit_parent_is_none(git_repo: GitRepo) -> None:
    g = _git(git_repo)
    assert g.rev_parse("HEAD") is None  # repositorio sin commits
    c1 = git_repo.commit("c1", files={"a.txt": "a\n"})
    assert g.rev_parse("HEAD") == c1
    assert g.rev_parse("HEAD^1") is None
    assert g.rev_parse("HEAD~1") is None
    c2 = git_repo.commit("c2", files={"a.txt": "b\n"})
    assert g.rev_parse("HEAD^1") == c1
    assert g.rev_parse("main") == c2
    assert g.rev_parse("no-such-branch") is None


@needs_git
def test_show_returns_raw_bytes(git_repo: GitRepo) -> None:
    raw = b"\xef\xbb\xbfline1\r\nline2\n"
    git_repo.commit("c1", files={"roles.yaml": raw, "dir/x.txt": "x\n"})
    g = _git(git_repo)
    assert g.show("HEAD", "roles.yaml") == raw
    assert g.show("HEAD", "missing.yaml") is None
    assert g.show("nope", "roles.yaml") is None
    assert g.show("HEAD", "dir") is None


@needs_git
def test_ls_tree_returns_git_modes(git_repo: GitRepo, tmp_path: Path) -> None:
    git_repo.write({"a.txt": "a\n", "run.sh": "#!/bin/sh\n"})
    os.chmod(git_repo.path / "run.sh", 0o755)
    git_repo.git("add", "a.txt", "run.sh")
    git_repo.git("update-index", "--chmod=+x", "run.sh")
    target = tmp_path / "target.txt"
    target.write_bytes(b"a.txt")
    blob = git_repo.git("hash-object", "-w", "--", str(target))
    git_repo.git("update-index", "--add", "--cacheinfo", f"120000,{blob},link")
    gitlink = "1" * 40
    git_repo.git("update-index", "--add", "--cacheinfo", f"160000,{gitlink},sub")
    _commit_index(git_repo, "modes")
    tree = _git(git_repo).ls_tree("HEAD")
    assert {p: m for p, (m, _o) in tree.items()} == {
        "a.txt": "100644",
        "run.sh": "100755",
        "link": "120000",
        "sub": "160000",
    }
    assert tree["link"][1] == blob and tree["sub"][1] == gitlink
    assert set(_git(git_repo).ls_tree("HEAD", ["a.txt"])) == {"a.txt"}
    long = _git(git_repo).ls_tree_long("HEAD")
    assert long["a.txt"][2] == 2 and long["sub"][2] is None
    # el commit de un gitlink no está en este repositorio, pero no es un blob ausente
    assert _git(git_repo).show("HEAD", "sub") is None


@needs_git
def test_show_in_a_complete_repository_never_reports_unavailable(git_repo: GitRepo) -> None:
    c1 = git_repo.commit("c1", files={"roles.yaml": "v1\n", "dir/x.txt": "x\n"})
    g = _git(git_repo)
    for rev, path in ((c1, "absent.yaml"), (c1, "dir"), ("nope", "roles.yaml"), ("0" * 40, "roles.yaml"), (c1, "dir/")):
        assert g.show(rev, path) is None, (rev, path)
    assert g.show(c1, "dir/x.txt") == b"x\n"
    with pytest.raises(ObjectUnavailable) as info:
        raise ObjectUnavailable(c1, "roles.yaml")
    assert "roles.yaml" in str(info.value) and isinstance(info.value, LookupError)


@needs_git
def test_diff_name_status(git_repo: GitRepo) -> None:
    body = "".join(f"line {i}\n" for i in range(40))
    c1 = git_repo.commit("c1", files={"a.txt": "a\n", "b.txt": "b\n", "c.txt": body})
    git_repo.write({"a.txt": "a2\n", "e.txt": "e\n"})
    os.replace(git_repo.path / "c.txt", git_repo.path / "d.txt")
    c2 = git_repo.commit("c2", remove=["b.txt"])
    g = _git(git_repo)
    assert set(g.diff_name_status(c1, c2)) == {
        ("M", "a.txt", None),
        ("D", "b.txt", None),
        ("R100", "d.txt", "c.txt"),
        ("A", "e.txt", None),
    }
    git_repo.write({"a.txt": "a3\n", "untracked.txt": "u\n"})
    assert g.diff_name_status(c2, None) == [("M", "a.txt", None)]
    assert g.diff_name_status(c2, c2) == []


@needs_git
def test_merge_base(git_repo: GitRepo) -> None:
    c1 = git_repo.commit("c1", files={"a.txt": "a\n"})
    git_repo.branch("feature")
    git_repo.commit("c2", files={"a.txt": "b\n"})
    git_repo.checkout("feature")
    git_repo.commit("f1", files={"f.txt": "f\n"})
    g = _git(git_repo)
    assert g.merge_base("main", "feature") == c1
    git_repo.git("checkout", "-q", "--orphan", "other")
    git_repo.git("rm", "-r", "-q", "--cached", ".")
    git_repo.commit("o1", files={"o.txt": "o\n"})
    assert g.merge_base("main", "other") is None
    assert g.merge_base("main", "nope") is None


@needs_git
def test_first_parent_revs(git_repo: GitRepo) -> None:
    c1 = git_repo.commit("c1", files={"roles.yaml": "v1\n"})
    git_repo.commit("c2", files={"other.txt": "o\n"})
    c3 = git_repo.commit("c3", files={"roles.yaml": "v2\n"})
    git_repo.branch("feature")
    git_repo.checkout("feature")
    f1 = git_repo.commit("f1", files={"roles.yaml": "v3\n"})
    git_repo.checkout("main")
    git_repo.commit("c4", files={"other.txt": "o2\n"})
    merge = git_repo.merge_no_ff("feature")
    g = _git(git_repo)
    revs = g.first_parent_revs("HEAD", "roles.yaml")
    assert revs == [merge, c3, c1]
    assert f1 not in revs  # segundo padre: fuera de la historia de primer padre
    assert g.first_parent_revs("HEAD", "roles.yaml", max_count=3) == [merge, c3]
    assert g.first_parent_revs("HEAD", "roles.yaml", max_count=0) == []
    assert g.first_parent_revs("nope", "roles.yaml") == []
    assert g.first_parent_revs("HEAD", ":(glob)*.yaml") == []  # rutas literales, sin magia
    assert g.ls_tree("HEAD", [":(glob)*.yaml"]) == {}


@needs_git
def test_current_branch(git_repo: GitRepo) -> None:
    g = _git(git_repo)
    assert g.current_branch() == "main"  # rama sin commits
    git_repo.commit("c1", files={"a.txt": "a\n"})
    git_repo.branch("task/17")
    git_repo.checkout("task/17")
    assert g.current_branch() == "task/17"
    git_repo.git("checkout", "-q", "--detach")
    assert g.current_branch() is None


@needs_git
def test_remote_url_strips_credentials(git_repo: GitRepo) -> None:
    g = _git(git_repo)
    assert g.remote_url() == str(git_repo.origin)
    git_repo.git("remote", "add", "cred", "https://user:s3cret@example.invalid/o/r.git")
    git_repo.git("remote", "add", "tok", "https://ghp_x@example.invalid/o/r.git")
    git_repo.git("remote", "add", "ssh", "git@example.invalid:o/r.git")
    assert g.remote_url("cred") == "https://example.invalid/o/r.git"
    assert g.remote_url("tok") == "https://example.invalid/o/r.git"
    assert g.remote_url("ssh") == "git@example.invalid:o/r.git"
    assert g.remote_url("missing") is None


@needs_git
def test_ls_files_tracked_and_untracked(git_repo: GitRepo) -> None:
    git_repo.commit("c1", files={".gitignore": "*.log\n", "a.txt": "a\n", "tex/main.tex": "x\n"})
    git_repo.write({"new.txt": "n\n", "build.log": "l\n"})
    g = _git(git_repo)
    assert g.ls_files() == [".gitignore", "a.txt", "tex/main.tex"]
    assert g.ls_files(others=True) == ["new.txt"]
    assert g.index_modes() == {".gitignore": "100644", "a.txt": "100644", "tex/main.tex": "100644"}


@needs_git
def test_real_errors(git_repo: GitRepo) -> None:
    g = _git(git_repo)
    with pytest.raises(GitError):
        g.run("rev-parse", "--verify", "nope")
    assert g.run("rev-parse", "--verify", "nope", check=False).returncode != 0
    with pytest.raises(GitError):
        g.ls_tree("nope")
