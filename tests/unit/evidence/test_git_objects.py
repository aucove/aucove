# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Lecturas de git por id para la evidencia (09-ci REQ-CI-51; ``vcs.git``).

``diff_tree_raw``, ``cat_file_check``, ``cat_file_batch``, ``read_commit``/``parse_commit``,
``first_parent_chain`` e ``is_ancestor``, sobre historias armadas con ``fast-import``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aucove.vcs.git import (
    EMPTY_TREE,
    Git,
    GitError,
    MissingObject,
    is_oid,
    parse_commit,
)
from tests.unit.evidence.support import AUTHOR, COMMITTER, Gitlink, Stream, git_bytes, needs_git, new_repo

pytestmark = needs_git


@pytest.fixture(scope="module")
def history(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    repo = new_repo(tmp_path_factory.mktemp("git-objects"))
    s = Stream()
    marks = {
        "c1": s.commit("main", {b"a.txt": b"uno\n", b"dir/b.bin": b"\x00\x01", b"link": b"x"}),
        "c2": s.commit("main", {b"a.txt": b"uno\ndos\n", b"dir/b.bin": None, b"cafe\xcc\x81.txt": b"nfd\n"}),
        "c3": s.commit("main", {b"sub": Gitlink("1" * 40), b"bad\xff.txt": b"latin\n"}),
    }
    marks["p1"] = s.commit("pr", {b"pr.txt": b"pr\n"}, parent=marks["c2"])
    marks["m"] = s.commit("pr", {b"sub": Gitlink("1" * 40), b"bad\xff.txt": b"latin\n"}, merge=[marks["c3"]])
    marks["p2"] = s.commit("pr", {b"pr.txt": b"pr2\n"})
    shas = s.run(repo)
    out: dict[str, object] = {name: shas[mark] for name, mark in marks.items()}
    out["git"] = Git(repo.path, env=repo.env)
    out["repo"] = repo
    return out


def test_is_oid() -> None:
    assert is_oid("a" * 40) and is_oid("0" * 64)
    assert not is_oid("A" * 40) and not is_oid("a" * 39) and not is_oid("HEAD") and not is_oid(None)


def test_diff_tree_raw_gives_exact_paths_and_full_ids(history: dict[str, object]) -> None:
    git: Git = history["git"]  # type: ignore[assignment]
    changes = git.diff_tree_raw(history["c1"], history["c2"])  # type: ignore[arg-type]
    by_path = {c.path: c for c in changes}
    assert list(by_path) == [b"a.txt", b"cafe\xcc\x81.txt", b"dir/b.bin"]  # orden de git, en bytes
    assert by_path[b"a.txt"].status == "M" and is_oid(by_path[b"a.txt"].new_oid)
    assert by_path[b"cafe\xcc\x81.txt"].status == "A" and by_path[b"cafe\xcc\x81.txt"].old_mode == "000000"
    gone = by_path[b"dir/b.bin"]
    assert gone.deleted and gone.new_mode == "000000" and gone.new_oid == "0" * 40
    later = {c.path: c for c in git.diff_tree_raw(history["c2"], history["c3"])}  # type: ignore[arg-type]
    assert later[b"sub"].new_mode == "160000" and later[b"bad\xff.txt"].status == "A"


def test_diff_tree_raw_from_the_empty_tree(history: dict[str, object]) -> None:
    git: Git = history["git"]  # type: ignore[assignment]
    changes = git.diff_tree_raw(EMPTY_TREE, history["c1"])  # type: ignore[arg-type]
    assert [c.path for c in changes] == [b"a.txt", b"dir/b.bin", b"link"]
    assert all(c.status == "A" for c in changes)


def test_cat_file_check_and_batch(history: dict[str, object]) -> None:
    git: Git = history["git"]  # type: ignore[assignment]
    changes = git.diff_tree_raw(EMPTY_TREE, history["c2"])  # type: ignore[arg-type]
    oids = [c.new_oid for c in changes]
    infos = git.cat_file_check(oids + oids[:1] + ["0" * 40])
    assert list(infos) == [*dict.fromkeys(oids), "0" * 40]  # sin repetir y en orden
    assert infos["0" * 40] is None
    assert all(i is not None and i.type == "blob" for k, i in infos.items() if k != "0" * 40)
    contents = {info.oid: data for info, data in git.cat_file_batch(oids)}
    by_path = {c.path: contents[c.new_oid] for c in changes}
    assert by_path[b"a.txt"] == b"uno\ndos\n" and by_path[b"cafe\xcc\x81.txt"] == b"nfd\n"
    # en tandas pequeñas da lo mismo (cada objeto en su propia llamada)
    small = {info.oid: data for info, data in git.cat_file_batch(oids, max_bytes=1)}
    assert small == contents


def test_cat_file_batch_fails_before_reading_when_an_object_is_missing(history: dict[str, object]) -> None:
    git: Git = history["git"]  # type: ignore[assignment]
    head = git.diff_tree_raw(EMPTY_TREE, history["c1"])[0].new_oid  # type: ignore[arg-type]
    with pytest.raises(MissingObject) as info:
        list(git.cat_file_batch([head, "f" * 40]))
    assert info.value.oid == "f" * 40
    with pytest.raises(ValueError):
        list(git.cat_file_batch(["HEAD"]))
    with pytest.raises(ValueError):
        git.cat_file_check(["--batch"])


def test_cat_file_objects_reads_small_objects_in_one_call(history: dict[str, object]) -> None:
    git: Git = history["git"]  # type: ignore[assignment]
    c1, c2 = history["c1"], history["c2"]
    found = git.cat_file_objects([c2, c1, c2, "e" * 40])  # type: ignore[list-item]
    assert list(found) == [c2, c1, "e" * 40] and found["e" * 40] is None
    info, raw = found[c2]  # type: ignore[index, misc]
    assert info.type == "commit" and parse_commit(raw).parents == (c1,)
    assert git.cat_file_objects([]) == {}


def test_read_commit(history: dict[str, object]) -> None:
    git: Git = history["git"]  # type: ignore[assignment]
    merge = git.read_commit(history["m"])  # type: ignore[arg-type]
    assert merge.parents == (history["p1"], history["c3"])
    assert merge.author == AUTHOR and merge.committer == COMMITTER and merge.encoding is None
    assert is_oid(merge.tree)
    blob = git.diff_tree_raw(EMPTY_TREE, history["c1"])[0].new_oid  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        git.read_commit(blob)
    with pytest.raises(MissingObject):
        git.read_commit("e" * 40)


def test_parse_commit_ignores_continuations_and_keeps_raw_identities() -> None:
    raw = (
        b"tree " + b"a" * 40 + b"\n"
        b"parent " + b"b" * 40 + b"\n"
        b"author Jos\xe9 <j@x.invalid> 1790000000 -0600\n"
        b"committer Bot <b@x.invalid> 1790000001 +0000\n"
        b"encoding ISO-8859-1\n"
        b"gpgsig -----BEGIN PGP SIGNATURE-----\n"
        b" author Falso <f@x.invalid> 1 +0000\n"
        b" -----END PGP SIGNATURE-----\n"
        b"\n"
        b"mensaje\n\nauthor No <n@x.invalid> 2 +0000\n"
    )
    header = parse_commit(raw)
    assert header.tree == "a" * 40 and header.parents == ("b" * 40,)
    assert header.author == b"Jos\xe9 <j@x.invalid>" and header.committer == b"Bot <b@x.invalid>"
    assert header.encoding == "ISO-8859-1"
    with pytest.raises(ValueError):
        parse_commit(b"tree " + b"a" * 40 + b"\n\nsin autor\n")


def test_first_parent_chain(history: dict[str, object]) -> None:
    git: Git = history["git"]  # type: ignore[assignment]
    c1, c2, c3, p1, m, p2 = (history[k] for k in ("c1", "c2", "c3", "p1", "m", "p2"))
    assert git.first_parent_chain(p2) == [p2, m, p1, c2, c1]  # type: ignore[arg-type]
    assert git.first_parent_chain(p2, max_count=2) == [p2, m]  # type: ignore[arg-type]
    # con exclude: se corta en lo alcanzable desde la base, también por el segundo padre
    assert git.first_parent_chain(p2, exclude=c3) == [p2, m, p1]  # type: ignore[arg-type]
    assert git.first_parent_chain(c3, exclude=c3) == []  # type: ignore[arg-type]
    assert git.first_parent_chain(c3, max_count=0) == []  # type: ignore[arg-type]
    with pytest.raises(GitError):
        git.first_parent_chain("d" * 40)


def test_is_ancestor(history: dict[str, object]) -> None:
    git: Git = history["git"]  # type: ignore[assignment]
    c1, c3, p1, m, p2 = (history[k] for k in ("c1", "c3", "p1", "m", "p2"))
    assert git.is_ancestor(c1, p2) and git.is_ancestor(c3, m) and git.is_ancestor(c3, c3)  # type: ignore[arg-type]
    assert not git.is_ancestor(p1, c3)  # type: ignore[arg-type]
    with pytest.raises(GitError):
        git.is_ancestor("d" * 40, c3)  # type: ignore[arg-type]


def test_reads_do_not_depend_on_the_checkout(history: dict[str, object], tmp_path: Path) -> None:
    """``diff_tree_raw`` y ``cat_file_batch`` leen objetos: un árbol de trabajo sucio no cambia nada."""
    repo = history["repo"]
    git: Git = history["git"]  # type: ignore[assignment]
    before = [(c.path, c.new_oid) for c in git.diff_tree_raw(EMPTY_TREE, history["c2"])]  # type: ignore[arg-type]
    git_bytes(repo, "read-tree", "--reset", "-u", history["c2"])  # type: ignore[arg-type]
    (repo.path / "a.txt").write_bytes(b"cambiado en disco\r\n")  # type: ignore[attr-defined]
    after = [(c.path, c.new_oid) for c in git.diff_tree_raw(EMPTY_TREE, history["c2"])]  # type: ignore[arg-type]
    assert before == after
    oid = dict(after)[b"a.txt"]
    assert [data for _info, data in git.cat_file_batch([oid])] == [b"uno\ndos\n"]
