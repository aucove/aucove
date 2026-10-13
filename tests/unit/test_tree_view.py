# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``TreeView`` sin seguir enlaces (09 REQ-CI-24), sonda de enlaces y mapa de grafías.

- ``git_tree`` lee de los objetos de git con los modos de ``ls-tree -z`` y nunca lee el
  contenido de una entrada ``120000``.
- ``worktree`` lee el árbol de trabajo con ``lstat`` de cada prefijo, rechaza enlaces (en
  Windows, puntos de reanálisis) y rutas cuya ``realpath`` sale de la raíz, y archivos
  de más de 5 MB.
"""

from __future__ import annotations

import os
import shutil
import stat
import types
import unicodedata
from pathlib import Path
from types import MappingProxyType

import pytest

from aucove import _meta
from aucove.vcs import links as links_mod
from aucove.vcs import tree as tree_mod
from aucove.vcs.git import Git, GitUnavailable, ObjectUnavailable, UnknownRevision
from aucove.vcs.links import case_map, is_link, link_probe
from aucove.vcs.tree import MAX_FILE_BYTES, TreeView, WorktreeView, git_tree, worktree
from tests.conftest import GitRepo

WINDOWS = os.name == "nt"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git no está disponible")


def _git(repo: GitRepo) -> Git:
    return Git(repo.path, env=repo.env)


def _dir_link(link: Path, target: Path) -> str | None:
    """Enlace a una carpeta: simbólico si se puede; en Windows, si no, una unión."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError):
        pass
    if WINDOWS:
        try:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))  # no requiere privilegios
            return "junction"
        except (OSError, ImportError, AttributeError):
            return None
    return None


def _file_link(link: Path, target: Path) -> bool:
    try:
        os.symlink(target, link)
        return True
    except (OSError, NotImplementedError):
        return False


def _add_index_entry(repo: GitRepo, tmp: Path, mode: str, path: str, content: bytes = b"a.txt") -> str:
    source = tmp / f"blob-{abs(hash(path))}"
    source.write_bytes(content)
    blob = repo.git("hash-object", "-w", "--", str(source))
    repo.git("update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}")
    return blob


class _NoGit:
    """Git que no está disponible: fuerza el recorrido del sistema de archivos."""

    def index_modes(self) -> dict[str, str]:
        raise GitUnavailable("git")

    def ls_files(self, *, others: bool = False) -> list[str]:
        raise GitUnavailable("git")


# -- git_tree -------------------------------------------------------------------------


@pytest.fixture
def modes_repo(git_repo: GitRepo, tmp_path: Path) -> tuple[GitRepo, dict[str, str]]:
    git_repo.write(
        {
            "a.txt": "hola\n",
            "bom.txt": b"\xef\xbb\xbfcon BOM\n",
            "latin1.txt": b"caf\xe9\n",
            "run.sh": "#!/bin/sh\n",
            "ten.bin": b"0123456789",
        }
    )
    os.chmod(git_repo.path / "run.sh", 0o755)
    git_repo.git("add", "--", "a.txt", "bom.txt", "latin1.txt", "run.sh", "ten.bin")
    git_repo.git("update-index", "--chmod=+x", "run.sh")
    blobs = {
        "link": _add_index_entry(git_repo, tmp_path, "120000", "link", b"a.txt"),
        "sub": "2" * 40,
    }
    git_repo.git("update-index", "--add", "--cacheinfo", f"160000,{blobs['sub']},sub")
    git_repo.git("commit", "-q", "--no-verify", "-m", "modes")
    return git_repo, blobs


@needs_git
def test_git_tree_entries_are_ls_tree_modes(modes_repo: tuple[GitRepo, dict[str, str]]) -> None:
    repo, _blobs = modes_repo
    view = git_tree(repo.path, "HEAD", git=_git(repo))
    assert isinstance(view, TreeView)
    assert view.sha == repo.head() and view.root == repo.path
    assert dict(view.entries()) == {
        "a.txt": "100644",
        "bom.txt": "100644",
        "latin1.txt": "100644",
        "link": "120000",
        "run.sh": "100755",
        "sub": "160000",
        "ten.bin": "100644",
    }
    with pytest.raises(TypeError):
        view.entries()["x"] = "100644"  # type: ignore[index]


@needs_git
def test_git_tree_never_reads_symlink_content(
    modes_repo: tuple[GitRepo, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, blobs = modes_repo
    g = _git(repo)
    view = git_tree(repo.path, "HEAD", git=g)
    calls: list[tuple[str, ...]] = []
    real_run = g.run

    def spy(*args: str, **kwargs: object):  # type: ignore[no-untyped-def]
        calls.append(args)
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(g, "run", spy)
    assert view.read_bytes("link") is None
    assert view.read_text("link") is None
    assert view.read_bytes("sub") is None
    assert view.read_bytes("missing") is None
    assert calls == []  # ni cat-file del enlace ni del gitlink
    assert view.read_bytes("a.txt") == b"hola\n"
    assert all(blobs["link"] not in a for a in calls)
    assert [a[:2] for a in calls] == [("cat-file", "blob")]


@needs_git
def test_git_tree_limits_and_text(modes_repo: tuple[GitRepo, dict[str, str]], monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _blobs = modes_repo
    g = _git(repo)
    view = git_tree(repo.path, "HEAD", git=g)
    reads: list[tuple[str, ...]] = []
    real_run = g.run
    monkeypatch.setattr(g, "run", lambda *a, **k: reads.append(a) or real_run(*a, **k))
    assert view.read_bytes("ten.bin", limit=9) is None
    assert reads == []  # el tamaño de ls-tree -l basta para rechazarlo
    assert view.read_bytes("ten.bin", limit=10) == b"0123456789"
    assert view.read_bytes("run.sh") == b"#!/bin/sh\n"
    assert view.read_text("bom.txt") == "﻿con BOM\n"
    assert view.read_text("latin1.txt") is None  # UTF-8 estricto
    assert view.read_text("a.txt") == "hola\n"


@needs_git
def test_git_tree_default_limit_is_5_mib(git_repo: GitRepo) -> None:
    git_repo.commit("big", files={"ok.bin": b"x" * MAX_FILE_BYTES, "big.bin": b"x" * (MAX_FILE_BYTES + 1)})
    view = git_tree(git_repo.path, "HEAD", git=_git(git_repo))
    assert MAX_FILE_BYTES == 5 * 2**20
    assert view.read_bytes("big.bin") is None
    data = view.read_bytes("ok.bin")
    assert data is not None and len(data) == MAX_FILE_BYTES


@needs_git
def test_git_tree_unknown_revision(git_repo: GitRepo) -> None:
    git_repo.commit("c1", files={"a.txt": "a\n"})
    with pytest.raises(UnknownRevision):
        git_tree(git_repo.path, "nope", git=_git(git_repo))
    with pytest.raises(UnknownRevision):
        git_tree(git_repo.path, "HEAD^1", git=_git(git_repo))


@needs_git
def test_git_tree_other_revision(git_repo: GitRepo) -> None:
    c1 = git_repo.commit("c1", files={"roles.yaml": "v1\n"})
    git_repo.commit("c2", files={"roles.yaml": "v2\n", "b.txt": "b\n"})
    view = git_tree(git_repo.path, c1, git=_git(git_repo))
    assert view.sha == c1
    assert dict(view.entries()) == {"roles.yaml": "100644"}
    assert view.read_text("roles.yaml") == "v1\n"


class _ListingOnlyGit:
    """``Git`` mínimo: un listado con un blob sin tamaño (``BAD``) y un gitlink."""

    root = Path(".")

    def rev_parse(self, rev: str) -> str:
        return "c" * 40

    def ls_tree_long(self, rev: str, paths: tuple[str, ...] = ()) -> dict[str, tuple[str, str, int | None]]:
        return {"gone.txt": ("100644", "a" * 40, None), "sub": ("160000", "b" * 40, None)}

    def run(self, *args: str, **kwargs: object) -> object:
        raise AssertionError("no debe leer ningún objeto")


def test_git_tree_blob_not_in_local_is_unavailable() -> None:
    """Clon parcial sin red: ``ls-tree -l`` da ``BAD`` y la lectura falla cerrada (no ``None``)."""
    view = tree_mod.GitTreeView(_ListingOnlyGit(), "HEAD")  # type: ignore[arg-type]
    with pytest.raises(ObjectUnavailable) as info:
        view.read_bytes("gone.txt")
    assert (info.value.rev, info.value.path) == ("c" * 40, "gone.txt")
    with pytest.raises(ObjectUnavailable):
        view.read_text("gone.txt", limit=1)
    assert view.read_bytes("sub") is None  # gitlink: nunca se lee
    assert view.read_bytes("absent.txt") is None


# -- worktree: entradas -----------------------------------------------------------------


@needs_git
def test_worktree_entries_tracked_and_untracked(git_repo: GitRepo, tmp_path: Path) -> None:
    git_repo.commit("c1", files={".gitignore": "*.log\n", "a.txt": "a\n", "gone.txt": "g\n", "tex/main.tex": "m\n"})
    (git_repo.path / "gone.txt").unlink()
    git_repo.write({"new.txt": "n\n", "build.log": "l\n", "sections/x.tex": "x\n"})
    view = worktree(git_repo.path, git=_git(git_repo))
    assert isinstance(view, TreeView) and view.sha is None
    assert dict(view.entries()) == {
        ".gitignore": "100644",
        "a.txt": "100644",
        "new.txt": "100644",
        "tex/main.tex": "100644",
        "sections/x.tex": "100644",
    }
    assert view.read_text("new.txt") == "n\n"
    assert view.read_bytes("build.log") is None  # ignorado: no es una entrada
    assert view.read_bytes("gone.txt") is None


@needs_git
def test_worktree_index_symlink_and_gitlink_modes(git_repo: GitRepo, tmp_path: Path) -> None:
    git_repo.commit("c1", files={"a.txt": "a\n"})
    # core.symlinks=false: el enlace del índice queda en disco como archivo regular
    git_repo.write({"link": "a.txt"})
    _add_index_entry(git_repo, tmp_path, "120000", "link")
    (git_repo.path / "sub").mkdir()
    git_repo.git("update-index", "--add", "--cacheinfo", f"160000,{'3' * 40},sub")
    git_repo.git("update-index", "--add", "--cacheinfo", f"160000,{'4' * 40},nosub")
    view = worktree(git_repo.path, git=_git(git_repo))
    entries = dict(view.entries())
    assert entries["link"] == "120000"
    assert entries["sub"] == "160000"
    assert "nosub" not in entries  # no existe en disco
    assert view.read_bytes("link") is None
    assert view.read_bytes("sub") is None


@needs_git
@pytest.mark.skipif(WINDOWS, reason="Windows no tiene bit de ejecución")
def test_worktree_exec_bit(git_repo: GitRepo) -> None:
    git_repo.write({"run.sh": "#!/bin/sh\n"})
    os.chmod(git_repo.path / "run.sh", 0o755)
    view = worktree(git_repo.path, git=_git(git_repo))
    assert view.entries()["run.sh"] == "100755"


@needs_git
def test_worktree_directory_link_hides_its_content(git_repo: GitRepo, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"secreto\n")
    git_repo.commit("c1", files={"a.txt": "a\n"})
    if _dir_link(git_repo.path / "jn", outside) is None:
        pytest.skip("no se pueden crear enlaces ni uniones aquí")
    view = worktree(git_repo.path, git=_git(git_repo))
    entries = dict(view.entries())
    assert entries.get("jn") == "120000"
    assert not any(p.startswith("jn/") for p in entries)
    assert view.read_bytes("jn/secret.txt") is None
    assert view.read_bytes("jn") is None


def test_worktree_walk_without_git(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_bytes(b"x")
    (root / _meta.CACHE_DIR).mkdir()
    (root / _meta.CACHE_DIR / "x.json").write_bytes(b"{}")
    (root / "sections").mkdir(parents=True)
    (root / "sections" / "a.tex").write_bytes(b"a\n")
    (root / "roles.yaml").write_bytes(b"r\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "s.txt").write_bytes(b"s\n")
    linked = _dir_link(root / "jn", outside)
    view = WorktreeView(root, git=_NoGit())  # type: ignore[arg-type]
    expected = {"roles.yaml": "100644", "sections/a.tex": "100644"}
    if linked:
        expected["jn"] = "120000"
    assert dict(view.entries()) == expected
    assert view.read_text("sections/a.tex") == "a\n"
    assert view.read_bytes(".git/config") is None


# -- worktree: lectura segura (REQ-CI-24) ----------------------------------------------


def _primed(root: Path, entries: dict[str, str]) -> WorktreeView:
    view = WorktreeView(root, git=_NoGit())  # type: ignore[arg-type]
    view._entries = MappingProxyType(entries)
    return view


def test_worktree_rejects_files_over_5_mb(tmp_path: Path) -> None:
    (tmp_path / "ok.bin").write_bytes(b"x" * MAX_FILE_BYTES)
    (tmp_path / "big.bin").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    view = _primed(tmp_path, {"ok.bin": "100644", "big.bin": "100644"})
    assert view.read_bytes("big.bin") is None
    assert view.read_text("big.bin") is None
    data = view.read_bytes("ok.bin")
    assert data is not None and len(data) == MAX_FILE_BYTES
    assert view.read_bytes("ok.bin", limit=10) is None


def test_worktree_rejects_unsafe_paths(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"a\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_bytes(b"c\n")
    bad = ["../a.txt", "/a.txt", "./a.txt", "x//a.txt", "x\\a.txt", ".git/config", ".GIT/config", "a\0b", ""]
    view = _primed(tmp_path, {p: "100644" for p in bad} | {"a.txt": "100644"})
    for path in bad:
        assert view.read_bytes(path) is None, path
    assert view.read_bytes("a.txt") == b"a\n"
    assert view.read_bytes("a.txt", limit=0) is None
    (tmp_path / "empty.txt").write_bytes(b"")
    assert _primed(tmp_path, {"empty.txt": "100644"}).read_bytes("empty.txt", limit=0) == b""


def test_worktree_rejects_directories_and_missing(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    view = _primed(tmp_path, {"d": "100644", "missing.txt": "100644", "d/x.txt": "100644"})
    assert view.read_bytes("d") is None
    assert view.read_bytes("missing.txt") is None
    assert view.read_bytes("d/x.txt") is None


def test_worktree_rejects_link_prefix_and_final_link(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"secreto\n")
    if _dir_link(root / "jn", outside) is None:
        pytest.skip("no se pueden crear enlaces ni uniones aquí")
    entries = {"jn/secret.txt": "100644"}
    if _file_link(root / "ln.txt", outside / "secret.txt"):
        entries["ln.txt"] = "100644"
    view = _primed(root, entries)
    for path in entries:
        assert view.read_bytes(path) is None, path


def test_worktree_rejects_realpath_outside_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Aunque la detección de enlaces fallara, la realpath fuera de la raíz se rechaza."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "ok.txt").write_bytes(b"ok\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"secreto\n")
    if _dir_link(root / "jn", outside) is None:
        pytest.skip("no se pueden crear enlaces ni uniones aquí")
    monkeypatch.setattr(tree_mod, "is_link", lambda st: False)
    view = _primed(root, {"jn/secret.txt": "100644", "ok.txt": "100644"})
    assert view.read_bytes("jn/secret.txt") is None
    assert view.read_bytes("ok.txt") == b"ok\n"


def test_worktree_open_flags_and_identity_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a.txt").write_bytes(b"a\n")
    (tmp_path / "b.txt").write_bytes(b"b\n")
    view = _primed(tmp_path, {"a.txt": "100644"})
    seen: list[int] = []
    real_open = os.open

    def spy_open(path: str, flags: int, *args: object) -> int:
        seen.append(flags)
        return real_open(path, flags, *args)  # type: ignore[arg-type]

    monkeypatch.setattr(tree_mod.os, "open", spy_open)
    assert view.read_bytes("a.txt") == b"a\n"
    if hasattr(os, "O_NOFOLLOW"):
        assert seen[-1] & os.O_NOFOLLOW
    else:
        assert WINDOWS  # en Windows no existe: rige el lstat de cada prefijo
    monkeypatch.undo()
    # si el archivo cambia entre lstat y open (otro inodo), no se lee
    other = os.stat(tmp_path / "b.txt")
    if other.st_ino and os.stat(tmp_path / "a.txt").st_ino:
        monkeypatch.setattr(tree_mod.os, "fstat", lambda fd: other)
        assert view.read_bytes("a.txt") is None


def test_split_rel_rules() -> None:
    assert tree_mod._split_rel("sections/m/a.tex") == ["sections", "m", "a.tex"]
    for bad in ["", "/x", "a/../b", "a/./b", "a//b", ".git", "a/.Git/b", "a\\b", None]:
        assert tree_mod._split_rel(bad) is None, bad
    if WINDOWS:
        for bad in ["CLAUDE.md.", "a /b", "a.tex:stream", "C:x"]:
            assert tree_mod._split_rel(bad) is None, bad


# -- is_link, link_probe y case_map --------------------------------------------------


def test_is_link_counts_reparse_points() -> None:
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    assert is_link(types.SimpleNamespace(st_mode=stat.S_IFLNK | 0o777))  # type: ignore[arg-type]
    assert is_link(types.SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=reparse))  # type: ignore[arg-type]
    assert is_link(types.SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=reparse))  # type: ignore[arg-type]
    assert not is_link(types.SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x20))  # type: ignore[arg-type]
    assert not is_link(types.SimpleNamespace(st_mode=stat.S_IFDIR))  # type: ignore[arg-type]


def test_link_probe(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "sections" / "methods").mkdir(parents=True)
    (root / "sections" / "methods" / "m.tex").write_bytes(b"m\n")
    probe = link_probe(root)
    assert probe("sections/methods/m.tex") is False
    assert probe("sections/methods/new.tex") is False  # no existe
    assert probe("nope/deeper/x.tex") is False
    assert probe("sections/methods/m.tex/x") is False  # un archivo como prefijo
    assert probe("") is False
    for bad in ["a/../b", "a//b", "./a", "a\0b"]:
        assert probe(bad) is True, bad  # falla cerrada
    outside = tmp_path / "outside"
    outside.mkdir()
    kind = _dir_link(root / "sections" / "results", outside)
    if kind is None:
        pytest.skip("no se pueden crear enlaces ni uniones aquí")
    assert probe("sections/results") is True
    assert probe("sections/results/r.tex") is True
    assert probe("sections/results/deep/r.tex") is True


def test_link_probe_counts_windows_reparse_points(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a").mkdir()
    real_lstat = os.lstat
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

    def fake_lstat(path: str) -> object:
        st = real_lstat(path)
        if os.path.basename(path) == "a":
            return types.SimpleNamespace(st_mode=st.st_mode, st_file_attributes=reparse)
        return st

    monkeypatch.setattr(links_mod.os, "lstat", fake_lstat)
    assert link_probe(tmp_path)("a/x.tex") is True


def test_link_probe_fails_closed_on_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(path: str) -> object:
        raise PermissionError(path)

    monkeypatch.setattr(links_mod.os, "lstat", denied)
    assert link_probe(tmp_path)("a/x.tex") is True


@needs_git
def test_case_map_lower_keys_and_nfc_values(git_repo: GitRepo, tmp_path: Path) -> None:
    nfd = unicodedata.normalize("NFD", "sections/Métodos/Café.tex")
    git_repo.commit("c1", files={"TEX/Main.tex": "m\n", "roles.yaml": "r\n"})
    _add_index_entry(git_repo, tmp_path, "100644", nfd, b"x\n")
    _add_index_entry(git_repo, tmp_path, "100644", "Dup.tex", b"1\n")
    _add_index_entry(git_repo, tmp_path, "100644", "dup.tex", b"2\n")
    git_repo.git("commit", "-q", "--no-verify", "-m", "c2")
    git_repo.write({"Untracked.txt": "u\n"})
    cmap = case_map(_git(git_repo))
    nfc = unicodedata.normalize("NFC", "sections/Métodos/Café.tex")
    assert cmap["tex/main.tex"] == "TEX/Main.tex"
    assert cmap["roles.yaml"] == "roles.yaml"
    assert cmap[nfc.lower()] == nfc
    assert cmap["dup.tex"] == "Dup.tex"  # gana la primera en el orden de git
    assert "untracked.txt" not in cmap
    assert all(k == k.lower() and v.lower() == k for k, v in cmap.items())
