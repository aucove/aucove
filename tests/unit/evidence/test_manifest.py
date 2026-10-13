# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Manifiesto de evidencia (11-trazabilidad §8.3; 09-ci REQ-CI-51, REQ-CI-52; ``evidence.manifest``).

Repositorios temporales armados con ``git fast-import`` (identidad y fechas fijas): los
commits, y con sales inyectadas los manifiestos, son los mismos en cualquier máquina.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from aucove.evidence import canon
from aucove.evidence.manifest import (
    MANIFEST_SCHEMA,
    TEXT_ENCODINGS,
    BuiltManifest,
    ManifestError,
    build_integration,
    build_submission,
    check_manifest,
    first_parent_history,
    identity_value,
)
from aucove.evidence.openings import check_actor, check_entry, load_openings, manifest_entries, parse_openings
from aucove.vcs.git import Git, parse_commit
from tests.conftest import FIXTURES
from tests.unit.evidence.support import AUTHOR, COMMITTER, Gitlink, Stream, git_bytes, needs_git, new_repo, validate

pytestmark = needs_git

REPOSITORY = "org-x/aucove-ana"
REPOSITORY_ID = 987654321
CREATED = "2026-10-06T18:00:00Z"
GENERATOR = "core/0.1.0"
GOLDEN = FIXTURES / "evidence" / "manifest" / "integration_c1_c2.json"

PLAN = b"# Plan\n\nP-1 Medir.\n"
PLAN2 = PLAN + b"P-2 Comparar.\n"


def salts(seed: str = "s") -> Callable[[], bytes]:
    """Fuente de sales determinista y sin repeticiones."""
    counter = itertools.count()
    return lambda: hashlib.sha256(f"{seed}-{next(counter)}".encode()).digest()


@dataclass
class Project:
    git: Git
    repo: Any
    sha: dict[str, str]
    # manifiestos ya construidos con las sales deterministas de ``salts()``: son inmutables y
    # cada llamada a git cuesta (en Windows, decenas de ms), así que las pruebas los comparten
    built: dict[tuple[Any, ...], BuiltManifest] = field(default_factory=dict)


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Project:
    repo = new_repo(tmp_path_factory.mktemp("manifest"))
    s = Stream()
    m: dict[str, str] = {}
    m["c1"] = s.commit(
        "main",
        {
            b"plan.md": PLAN,
            b"roles.yaml": b"roles: []\n",
            b"memory/constitution.md": b"# C\n",
            b"a.txt": b"uno\n",
            b"data/x.bin": b"\x00\xff\r\n",
        },
    )
    m["c2"] = s.commit("main", {b"a.txt": b"uno\ndos\n", b"data/x.bin": None, b"b.txt": b"b\n"})
    m["c3"] = s.commit("main", {b"plan.md": PLAN2})
    m["c4"] = s.commit("main", {b"roles.yaml": b"roles: [ana]\n"})
    m["c5"] = s.commit("main", {b"memory/constitution.md": b"# C2\n"})
    m["c6"] = s.commit("main", {b"b.txt": b"b2\n"})
    m["p1"] = s.commit("pr", {b"sections/ana/n.md": b"nota\n"}, parent=m["c2"])
    m["pm"] = s.commit(
        "pr",
        {
            b"plan.md": PLAN2,
            b"roles.yaml": b"roles: [ana]\n",
            b"memory/constitution.md": b"# C2\n",
            b"b.txt": b"b2\n",
        },
        merge=[m["c6"]],
    )
    m["p2"] = s.commit("pr", {b"sections/ana/n.md": b"nota 2\n"})
    m["orphan"] = s.commit("orphan", {b"z.txt": b"z\n"})
    shas = s.run(repo)
    return Project(git=Git(repo.path, env=repo.env), repo=repo, sha={k: shas[v] for k, v in m.items()})


def _cached(project: Project, key: tuple[Any, ...], kwargs: dict[str, Any], build: Callable[[], BuiltManifest]) -> BuiltManifest:
    if "salt" in kwargs:  # sales propias: siempre se construye
        return build()
    if key not in project.built:
        project.built[key] = build()
    return project.built[key]


def integration(project: Project, commit: str, base: str | None, **kwargs: Any) -> BuiltManifest:
    """``build_integration`` con los datos fijos de la prueba; sin ``salt``, con ``salts()`` y reutilizado."""
    key = ("integration", commit, base, tuple(sorted(kwargs.items())))
    return _cached(
        project,
        key,
        kwargs,
        lambda: build_integration(
            project.git,
            commit=project.sha[commit],
            base=project.sha[base] if base else None,
            repository=REPOSITORY,
            repository_id=REPOSITORY_ID,
            created_at=CREATED,
            generator=GENERATOR,
            **{"salt": salts(), **kwargs},
        ),
    )


def submission(project: Project, commit: str, base_sha: str, **kwargs: Any) -> BuiltManifest:
    """``build_submission`` con los datos fijos de la prueba (PR 12, tarea 7, autor ``ana-p``)."""
    key = ("submission", commit, base_sha, tuple(sorted(kwargs.items())))
    return _cached(
        project,
        key,
        kwargs,
        lambda: build_submission(
            project.git,
            commit=project.sha[commit],
            base_sha=project.sha[base_sha],
            pr=12,
            task=7,
            repository=REPOSITORY,
            repository_id=REPOSITORY_ID,
            created_at=CREATED,
            generator=GENERATOR,
            **{"salt": salts(), "pr_author": "ana-p", **kwargs},
        ),
    )


def assert_consistent(project: Project, built: BuiltManifest) -> None:
    """Esquemas, forma canónica, huella, orden por ``p`` y que cada apertura reproduce su compromiso."""
    manifest = json.loads(built.data)
    assert validate("evidence-manifest", manifest) == []
    assert validate("evidence-openings", built.openings.to_json()) == []
    assert canon.is_canonical(built.data)
    assert built.sha256 == hashlib.sha256(built.data).hexdigest() == built.openings.manifest_sha256
    assert manifest == dict(built.manifest)
    assert check_manifest(manifest) == ()
    ps = [e["p"] for e in manifest["entries"]]
    assert ps == sorted(ps)
    assert [o.p() for o in built.openings.entries] == ps  # mismas posiciones
    entries = manifest_entries(manifest)
    blobs = [o.blob for o in built.openings.entries if o.blob]
    contents = project.git.cat_file_objects(blobs)  # blobs pequeños: una sola llamada
    for opening in built.openings.entries:
        found = contents[opening.blob] if opening.blob else None
        data = found[1] if found is not None else None
        assert check_entry(opening, entries, data).content is True, opening.path
        assert opening.size == (len(data) if data is not None else None)
    for actor in built.openings.actors:
        assert check_actor(actor, manifest["actors"])
    salt_values = [o.salt for o in built.openings.entries] + [a.salt for a in built.openings.actors]
    assert len(set(salt_values)) == len(salt_values) and all(len(x) == 32 for x in salt_values)


def paths(built: BuiltManifest) -> dict[str, str | None]:
    return {o.path: o.blob for o in built.openings.entries}


# -- integration --------------------------------------------------------------------------


def test_first_record_covers_the_whole_tree_and_seals_the_plan(project: Project) -> None:
    built = integration(project, "c6", None)
    assert_consistent(project, built)
    m = built.manifest
    assert m["schema"] == MANIFEST_SCHEMA and m["kind"] == "integration" and m["base"] is None
    assert m["commits"] == [project.sha[f"c{i}"] for i in range(1, 7)]  # del más antiguo al más reciente
    assert m["commit"] == project.sha["c6"] and m["commits"][-1] == m["commit"]
    assert m["tree"] == project.repo.git("rev-parse", project.sha["c6"] + "^{tree}")
    assert m["pr"] is None and m["task"] is None and m["forge"] == "github"
    assert m["repository_id"] == REPOSITORY_ID and m["created_at"] == CREATED and m["generator"] == GENERATOR
    assert REPOSITORY.encode() not in built.data  # el nombre solo va en las aperturas
    assert set(paths(built)) == {"plan.md", "roles.yaml", "memory/constitution.md", "a.txt", "b.txt"}
    assert built.plan is True  # primer registro
    assert [a["r"] for a in m["actors"]] == ["author", "committer"]
    assert [a.value for a in built.openings.actors] == [AUTHOR.decode(), COMMITTER.decode()]


def test_range_with_a_deleted_file(project: Project) -> None:
    built = integration(project, "c2", "c1")
    assert_consistent(project, built)
    assert built.manifest["base"] == project.sha["c1"] and built.manifest["commits"] == [project.sha["c2"]]
    assert set(paths(built)) == {"a.txt", "b.txt", "data/x.bin"}
    gone = next(o for o in built.openings.entries if o.path == "data/x.bin")
    assert gone.blob is None and gone.size is None
    by_p = {e["p"]: e["c"] for e in built.manifest["entries"]}
    assert by_p[gone.p()] is None  # c = null
    assert built.plan is False


@pytest.mark.parametrize(
    ("base", "commit", "plan"),
    [("c2", "c3", True), ("c3", "c4", True), ("c4", "c5", True), ("c5", "c6", False), ("c1", "c2", False)],
)
def test_plan_flag(project: Project, base: str, commit: str, plan: bool) -> None:
    assert integration(project, commit, base).plan is plan


def test_integration_actors_from_the_pr(project: Project) -> None:
    built = integration(project, "c3", "c2", pr=5, task=3, pr_author="ana-p", merged_by="lead-x")
    assert_consistent(project, built)
    assert built.manifest["pr"] == 5 and built.manifest["task"] == 3
    assert [a["r"] for a in built.manifest["actors"]] == ["pr_author", "merged_by", "author", "committer"]
    assert [a.value for a in built.openings.actors][:2] == ["ana-p", "lead-x"]


def test_commits_skip_merged_branches_and_base_must_be_an_ancestor(project: Project) -> None:
    with pytest.raises(ManifestError) as info:
        integration(project, "c6", "p1")
    assert info.value.reason == "base_not_ancestor"
    with pytest.raises(ManifestError) as info:
        integration(project, "c3", "c3")
    assert info.value.reason == "empty_range"


# -- submission ----------------------------------------------------------------------------


def test_submission_uses_the_merge_base(project: Project) -> None:
    built = submission(project, "p2", "c6")  # el PR ya fusionó c6: merge-base = c6
    assert_consistent(project, built)
    m = built.manifest
    assert m["kind"] == "submission" and m["base"] == project.sha["c6"]
    assert m["commits"] == [project.sha["p1"], project.sha["pm"], project.sha["p2"]]
    assert set(paths(built)) == {"sections/ana/n.md"}
    assert m["pr"] == 12 and m["task"] == 7
    assert [a["r"] for a in m["actors"]] == ["pr_author", "author", "committer"]
    assert built.plan is False


def test_submission_of_a_branch_behind_the_base(project: Project) -> None:
    built = submission(project, "p1", "c6")  # merge-base = c2
    assert built.manifest["base"] == project.sha["c2"]
    assert built.manifest["commits"] == [project.sha["p1"]]
    assert set(paths(built)) == {"sections/ana/n.md"}


def test_submission_without_common_history(project: Project) -> None:
    with pytest.raises(ManifestError) as info:
        submission(project, "orphan", "c6")
    assert info.value.reason == "no_merge_base"


# -- sales, determinismo y errores -----------------------------------------------------------


def test_deterministic_with_injected_salts_and_sorted_by_p(project: Project) -> None:
    one = integration(project, "c6", None)  # sales de salts()
    two = integration(project, "c6", None, salt=salts())  # otra construcción, mismas sales
    assert one is not two
    assert one.data == two.data and one.openings == two.openings and one.sha256 == two.sha256
    other = integration(project, "c6", None, salt=salts("y"))
    assert other.data != one.data
    # sin las sales, todo lo demás coincide
    strip = ("entries", "actors")
    assert {k: v for k, v in one.manifest.items() if k not in strip} == {
        k: v for k, v in other.manifest.items() if k not in strip
    }
    assert sorted(paths(one).items()) == sorted(paths(other).items())


def test_default_salts_are_random(project: Project) -> None:
    one = build_integration(
        project.git, commit=project.sha["c2"], base=project.sha["c1"], repository=REPOSITORY,
        repository_id=REPOSITORY_ID,
    )
    two = build_integration(
        project.git, commit=project.sha["c2"], base=project.sha["c1"], repository=REPOSITORY,
        repository_id=REPOSITORY_ID,
    )
    assert one.data != two.data
    assert not {o.salt for o in one.openings.entries} & {o.salt for o in two.openings.entries}
    assert canon.is_canonical(one.data) and one.manifest["generator"].split("/")[1][0].isdigit()


def test_salt_source_is_checked(project: Project) -> None:
    with pytest.raises(ManifestError) as info:
        integration(project, "c2", "c1", salt=lambda: b"x" * 31)
    assert info.value.reason == "bad_salt"
    with pytest.raises(ManifestError) as info:
        integration(project, "c2", "c1", salt=lambda: b"x" * 32)
    assert info.value.reason == "salt_reuse"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"commit": "HEAD"}, "bad_input"),
        ({"commit": "A" * 40}, "bad_input"),
        ({"base": "main"}, "bad_input"),
        ({"repository": "sin-barra"}, "bad_input"),
        ({"repository_id": 0}, "bad_input"),
        ({"repository_id": True}, "bad_input"),
        ({"pr": 0}, "bad_input"),
        ({"created_at": "2026-10-06T18:00:00.5Z"}, "bad_input"),
        ({"generator": "Core/1"}, "bad_input"),
        ({"commit": "0" * 40}, "unknown_commit"),
        ({"base": "0" * 40}, "unknown_base"),
    ],
)
def test_integration_input_errors(project: Project, kwargs: dict[str, Any], reason: str) -> None:
    args: dict[str, Any] = {
        "commit": project.sha["c2"],
        "base": project.sha["c1"],
        "repository": REPOSITORY,
        "repository_id": REPOSITORY_ID,
        "created_at": CREATED,
        "generator": GENERATOR,
    }
    args.update(kwargs)
    with pytest.raises(ManifestError) as info:
        build_integration(project.git, **args)
    assert info.value.reason == reason


def test_not_a_commit_and_missing_task(project: Project) -> None:
    blob = project.repo.git("rev-parse", project.sha["c1"] + ":a.txt")
    with pytest.raises(ManifestError) as info:
        build_integration(project.git, commit=blob, base=None, repository=REPOSITORY, repository_id=REPOSITORY_ID)
    assert info.value.reason == "not_a_commit"
    with pytest.raises(ManifestError) as info:
        build_submission(
            project.git, commit=project.sha["p1"], base_sha=project.sha["c6"], pr=1, task=None,  # type: ignore[arg-type]
            repository=REPOSITORY, repository_id=REPOSITORY_ID, pr_author=None,
        )
    assert info.value.reason == "bad_input"


def test_actor_too_long_is_skipped_and_reported(project: Project) -> None:
    built = integration(project, "c2", "c1", pr=1, pr_author="x" * 321, merged_by="lead-x")
    assert [a["r"] for a in built.manifest["actors"]] == ["merged_by", "author", "committer"]
    assert [(s.reason, s.role) for s in built.skipped] == [("actor_too_long", "pr_author")]


class RecordingGit(Git):
    """``Git`` que anota cada llamada (para comprobar REQ-CI-51)."""

    calls: list[tuple[str, ...]]

    def run(self, *args: str, **kwargs: Any) -> Any:
        self.__dict__.setdefault("calls", []).append(args)
        return super().run(*args, **kwargs)


def test_only_object_reads_are_used(project: Project) -> None:
    git = RecordingGit(project.repo.path, env=project.repo.env)
    build_integration(git, commit=project.sha["c6"], base=None, repository=REPOSITORY, repository_id=REPOSITORY_ID)
    build_submission(
        git, commit=project.sha["p2"], base_sha=project.sha["c6"], pr=1, task=1,
        repository=REPOSITORY, repository_id=REPOSITORY_ID, pr_author="ana-p",
    )
    subcommands = {args[0] for args in git.calls}
    assert subcommands <= {"diff-tree", "cat-file", "merge-base", "rev-list"}, subcommands
    flat = [arg for args in git.calls for arg in args]
    assert not {"--filters", "--textconv", "-p", "--path"} & set(flat)
    assert ("cat-file", "--batch", "--buffer") in git.calls


# -- rutas: NFD, no UTF-8, gitlinks --------------------------------------------------------


# Rutas en el tope de 11 §8.3, que se mide en bytes UTF-8 de git: 2048 «é» son 4096 bytes y
# entran; 2049 «é» son 2049 caracteres pero 4098 bytes, y quedan fuera, igual que 4097 ASCII.
PATH_FITS = ("é" * 2048).encode()
PATH_MULTIBYTE = ("é" * 2049).encode()
PATH_ASCII_LONG = b"d/" + b"x" * 4095


@pytest.fixture(scope="module")
def odd_paths(tmp_path_factory: pytest.TempPathFactory) -> Project:
    repo = new_repo(tmp_path_factory.mktemp("odd-paths"))
    s = Stream()
    c1 = s.commit(
        "main",
        {
            b"cafe\xcc\x81.txt": b"nfd\n",  # e + U+0301, tal como lo guardaría macOS
            b"caf\xc3\xa9.txt": b"nfc\n",  # U+00E9
            b"bad\xff.txt": b"latin-1\n",
            b"sub": Gitlink("1" * 40),
            b"ok.txt": b"ok\n",
            PATH_FITS: b"4096\n",
            PATH_MULTIBYTE: b"4098\n",
            PATH_ASCII_LONG: b"4097\n",
        },
    )
    shas = s.run(repo)
    return Project(git=Git(repo.path, env=repo.env), repo=repo, sha={"c1": shas[c1]})


def test_nfd_path_opening_reproduces_p_and_matches_the_repo(odd_paths: Project) -> None:
    built = integration(odd_paths, "c1", None)
    assert_consistent(odd_paths, built)
    nfd = "café.txt"
    assert unicodedata.normalize("NFC", nfd) != nfd
    by_path = {o.path: o for o in built.openings.entries}
    assert set(by_path) == {nfd, "café.txt", "ok.txt", PATH_FITS.decode()}  # NFD y NFC son entradas distintas
    opening = by_path[nfd]
    assert opening.path_bytes == b"cafe\xcc\x81.txt"
    assert opening.p() in manifest_entries(dict(built.manifest))
    assert opening.p() != by_path["café.txt"].p()
    # --repo: el blob de la apertura es el de esa ruta en el árbol del commit, y su contenido abre c
    tree = odd_paths.git.ls_tree(odd_paths.sha["c1"])
    assert tree[nfd][1] == opening.blob
    [(info, data)] = list(odd_paths.git.cat_file_batch([opening.blob]))  # type: ignore[list-item]
    assert data == b"nfd\n" and check_entry(opening, manifest_entries(dict(built.manifest)), data).content is True
    # tras escribir y leer openings.json, la ruta sigue en NFD y reproduce p
    again = {o.path: o for o in load_openings(built.openings.dumps()).entries}
    assert again[nfd].p() == opening.p()


def test_non_utf8_paths_and_gitlinks_are_skipped_and_reported(odd_paths: Project) -> None:
    built = integration(odd_paths, "c1", None)
    assert [(s.reason, s.path) for s in built.skipped] == [
        ("non_utf8", b"bad\xff.txt"), ("path_too_long", PATH_ASCII_LONG), ("gitlink", b"sub"),
        ("path_too_long", PATH_MULTIBYTE),
    ]
    assert len(built.manifest["entries"]) == 4


def test_path_limit_counts_the_utf8_bytes_of_git(odd_paths: Project) -> None:
    """C-3 (11 §8.3): más de 4096 bytes, tal como da la ruta git, queda fuera aunque sean 2049
    caracteres; hasta 4096 bytes entra, y su apertura vuelve a leerse y cumple el esquema."""
    built = integration(odd_paths, "c1", None)
    assert len(PATH_FITS) == 4096 and len(PATH_MULTIBYTE.decode()) == 2049 and len(PATH_ASCII_LONG) == 4097
    assert sorted(len(s.path or b"") for s in built.skipped if s.reason == "path_too_long") == [4097, 4098]
    fits = [o for o in built.openings.entries if o.path_bytes == PATH_FITS]
    assert len(fits) == 1 and fits[0].p() in manifest_entries(dict(built.manifest))
    assert load_openings(built.openings.dumps()) == built.openings
    assert validate("evidence-openings", built.openings.to_json()) == []


# -- core.autocrlf: las huellas salen de los blobs ----------------------------------------------


def test_autocrlf_checkout_gives_the_same_manifest(tmp_path: Path) -> None:
    source = new_repo(tmp_path, "source")
    s = Stream()
    mark = s.commit(
        "main", {b"a.txt": b"uno\ndos\n", b"data.bin": b"\x00\x01\n\x02", b"crlf.txt": b"ya venia\r\ncon crlf\r\n"}
    )
    head = s.run(source)[mark]
    manifests = []
    for flag in ("true", "false"):
        clone = tmp_path / f"clone-{flag}"
        git_bytes(source, "-c", f"core.autocrlf={flag}", "clone", "-q", str(source.path), str(clone), cwd=tmp_path)
        on_disk = (clone / "a.txt").read_bytes()
        assert on_disk == (b"uno\r\ndos\r\n" if flag == "true" else b"uno\ndos\n")
        env = dict(source.env, GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.autocrlf", GIT_CONFIG_VALUE_0=flag)
        built = build_integration(
            Git(clone, env=env), commit=head, base=None, repository=REPOSITORY, repository_id=REPOSITORY_ID,
            created_at=CREATED, generator=GENERATOR, salt=salts("crlf"),
        )
        manifests.append(built)
    crlf, lf = manifests
    assert crlf.data == lf.data and crlf.openings == lf.openings
    sizes = {o.path: o.size for o in lf.openings.entries}
    assert sizes == {"a.txt": 8, "crlf.txt": 20, "data.bin": 4}  # tamaños del blob, no del disco


# -- límites ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def wide(tmp_path_factory: pytest.TempPathFactory) -> Project:
    repo = new_repo(tmp_path_factory.mktemp("wide"))
    s = Stream()
    blob = s.blob(b"mismo contenido\n")
    c1 = s.commit("main", {f"d{i // 1000}/f{i:05d}.txt".encode(): blob for i in range(10_000)})
    c2 = s.commit("main", {b"extra.txt": blob})
    shas = s.run(repo)
    return Project(git=Git(repo.path, env=repo.env), repo=repo, sha={"c1": shas[c1], "c2": shas[c2]})


def test_more_than_10000_entries_is_an_error(wide: Project) -> None:
    with pytest.raises(ManifestError) as info:
        integration(wide, "c2", None)
    assert info.value.reason == "too_many_entries" and info.value.detail == "10001"


def test_exactly_10000_entries_is_fine(wide: Project) -> None:
    built = integration(wide, "c1", None)
    assert len(built.manifest["entries"]) == 10_000
    assert len({o.blob for o in built.openings.entries}) == 1  # un blob, 10 000 compromisos distintos
    assert len({e["c"] for e in built.manifest["entries"]}) == 10_000
    assert validate("evidence-manifest", json.loads(built.data)) == []


@pytest.fixture(scope="module")
def long_history(tmp_path_factory: pytest.TempPathFactory) -> Project:
    repo = new_repo(tmp_path_factory.mktemp("long"))
    s = Stream()
    marks = [s.commit("main", {b"n.txt": f"{i}\n".encode()}, message=f"c{i}") for i in range(105)]
    shas = s.run(repo)
    return Project(git=Git(repo.path, env=repo.env), repo=repo, sha={f"c{i}": shas[m] for i, m in enumerate(marks)})


def test_commits_are_capped_at_the_100_most_recent(long_history: Project) -> None:
    sha = long_history.sha
    built = integration(long_history, "c104", None)
    assert built.manifest["commits"] == [sha[f"c{i}"] for i in range(5, 105)]
    assert validate("evidence-manifest", json.loads(built.data)) == []
    history = first_parent_history(long_history.git, sha["c104"])
    assert history == [sha[f"c{i}"] for i in range(103, 3, -1)]  # los 100 anteriores, sin el commit
    ranged = integration(long_history, "c104", "c4")  # la base más lejana que REQ-CI-52 admite
    assert ranged.manifest["commits"] == [sha[f"c{i}"] for i in range(5, 105)]
    assert first_parent_history(long_history.git, sha["c104"], limit=3) == [sha["c103"], sha["c102"], sha["c101"]]


# -- identidades -------------------------------------------------------------------------------


def test_identity_value_honours_the_commit_encoding(tmp_path: Path) -> None:
    assert identity_value(b"Jos\xc3\xa9 <j@x>") == "José <j@x>"
    assert identity_value(b"Jos\xe9 <j@x>", "ISO-8859-1") == "José <j@x>"
    assert identity_value(b"Jos\xe9 <j@x>") is None
    assert identity_value(b"Jos\xe9 <j@x>", "no-such-codec") is None
    repo = new_repo(tmp_path)
    s = Stream()
    declared = s.commit("main", {b"a.txt": b"a\n"}, author=b"Jos\xe9 <j@x.invalid>", encoding="ISO-8859-1")
    undeclared = s.commit("other", {b"a.txt": b"a\n"}, author=b"Jos\xe9 <j@x.invalid>")
    shas = s.run(repo)
    git = Git(repo.path, env=repo.env)
    built = build_integration(git, commit=shas[declared], base=None, repository=REPOSITORY, repository_id=REPOSITORY_ID)
    assert built.openings.actors[0].value == "José <j@x.invalid>"
    built = build_integration(git, commit=shas[undeclared], base=None, repository=REPOSITORY, repository_id=REPOSITORY_ID)
    assert [a.role for a in built.openings.actors] == ["committer"]
    assert [(s.reason, s.role) for s in built.skipped] == [("actor_not_utf8", "author")]


# Inputs de r6: con cada códec de Python, ninguno puede hacer lanzar a identity_value.
HOSTILE_IDENTITIES: tuple[bytes, ...] = (
    b"\xff\xfe\x00", rb"\N{", rb"\x4", b"xn--" + b"\xff" * 3, b"a" * 70_000, b"-" * 10, b"\x80" * 5,
    bytes(range(256)), b"." * 300, b"a..b", b"xn--a", b"A" * 64 + b".b", b"\x00", b"+2D8-", b"",
)


def test_identity_value_never_raises_whatever_the_declared_codec() -> None:
    """Con ``idna``, ``punycode`` o ``undefined``, ``identity_value`` no lanza ``UnicodeError`` (un ``ValueError``)."""
    import encodings
    import pkgutil

    names = sorted({m.name for m in pkgutil.iter_modules(encodings.__path__)} | set(TEXT_ENCODINGS))
    names += ["\udcff", "utf-8\x00", "x" * 10_000, "rot13", "base64", "hex", "zip"]
    for name in names:
        for raw in HOSTILE_IDENTITIES:
            value = identity_value(raw, name)
            assert value is None or isinstance(value, str), (name, raw[:10])


@pytest.mark.parametrize(
    "codec",
    ["mbcs", "oem", "undefined", "idna", "punycode", "rot13", "base64", "unicode_escape", "raw_unicode_escape",
     "palmos", "charmap", "no-such-codec", "\udcff\udcfe", "utf-8\x00", ""],
)
def test_codecs_outside_the_portable_list_fall_back_to_utf8(codec: str) -> None:
    """Un códec que no está en la lista portable (solo de Windows, que no es de texto o
    especial) se trata como desconocido: UTF-8 estricto, igual en Linux, macOS y Windows."""
    assert identity_value("José <j@x>".encode(), codec) == "José <j@x>"
    assert identity_value(b"Jos\xe9 <j@x>", codec) is None


def test_the_portable_list_is_made_of_text_codecs() -> None:
    import codecs

    assert len(TEXT_ENCODINGS) == 97  # la tabla «Standard Encodings» de Python
    for name in TEXT_ENCODINGS:
        info = codecs.lookup(name)
        assert info.name == name and info._is_text_encoding, name  # type: ignore[attr-defined]
    for alias, expected in (("latin1", "José <j@x>"), ("ISO-8859-1", "José <j@x>"), ("windows-1252", "José <j@x>"),
                            ("cp65001", None), ("UTF8", None)):
        assert identity_value(b"Jos\xe9 <j@x>", alias) == expected, alias


def _raw_commit(encoding: bytes, author: bytes = b"Jos\xe9 <j@x.invalid>") -> bytes:
    """Objeto commit crudo, como lo da ``git cat-file --batch``, con la cabecera ``encoding`` dada."""
    when = b" 1790000000 +0000"
    return (
        b"tree " + b"4b825dc642cb6eb9a060e54bf8d69288fbee4904\n"
        + b"author " + author + when + b"\ncommitter Ana P\xc3\xa9rez <ana@x.invalid>" + when
        + b"\nencoding " + encoding + b"\n\nmensaje\n"
    )


@pytest.mark.parametrize("encoding", [b"undefined", b"idna", b"punycode", b"mbcs", b"oem", b"rot13", b"\xff\xfe",
                                      b"utf-8\x00x", b"x" * 300])
def test_raw_commits_with_hostile_encodings_give_no_actor_and_no_error(encoding: bytes) -> None:
    """La cabecera ``encoding`` de un objeto commit crudo (de cualquier repo) nunca hace
    lanzar a ``identity_value``; la identidad que no es UTF-8 queda fuera y la que sí, entra."""
    header = parse_commit(_raw_commit(encoding))
    assert identity_value(header.author, header.encoding) is None
    assert identity_value(header.committer, header.encoding) == "Ana Pérez <ana@x.invalid>"


def test_a_raw_commit_with_a_portable_encoding_is_decoded_with_it() -> None:
    header = parse_commit(_raw_commit(b"latin1"))
    assert identity_value(header.author, header.encoding) == "José <j@x.invalid>"


def test_encoding_undefined_still_builds_the_manifest(tmp_path: Path) -> None:
    """Un commit con ``encoding undefined`` no tumba ``build_integration``: el
    manifiesto se arma y el actor que no se puede decodificar queda en ``skipped``."""
    repo = new_repo(tmp_path)
    s = Stream()
    mark = s.commit("main", {b"a.txt": b"a\n"}, author=b"Jos\xe9 <j@x.invalid>", encoding="undefined")
    shas = s.run(repo)
    built = build_integration(
        Git(repo.path, env=repo.env), commit=shas[mark], base=None, repository=REPOSITORY, repository_id=REPOSITORY_ID
    )
    assert [a.role for a in built.openings.actors] == ["committer"]
    assert [(s.reason, s.role) for s in built.skipped] == [("actor_not_utf8", "author")]


# -- comprobaciones del núcleo y golden ------------------------------------------------------


def test_check_manifest_reports_what_the_schema_cannot(project: Project) -> None:
    manifest = json.loads(integration(project, "c6", None).data)
    assert check_manifest(manifest) == ()
    unsorted = dict(manifest, entries=list(reversed(manifest["entries"])))
    assert check_manifest(unsorted) == ("entries_unsorted",)
    duplicated = dict(manifest, entries=manifest["entries"] + manifest["entries"][-1:])
    assert check_manifest(duplicated) == ("entries_duplicate",)
    actors = dict(manifest, actors=manifest["actors"] + manifest["actors"][:1])
    assert check_manifest(actors) == ("actors_duplicate",)
    last = dict(manifest, commits=list(reversed(manifest["commits"])))
    assert check_manifest(last) == ("commits_last",)


# Contenido de los archivos vivos del commit golden (va también en el fixture, para abrir c sin git).
GOLDEN_CONTENTS: dict[str, bytes] = {"a.txt": b"uno\ndos\n", "café.txt": b"nfd\n"}


def golden_build(tmp: Path) -> BuiltManifest:
    """Manifiesto del fixture golden: historia propia, fija, con un borrado y una ruta NFD."""
    repo = new_repo(tmp, "golden")
    s = Stream()
    c1 = s.commit("main", {b"plan.md": PLAN, b"a.txt": b"uno\n", b"data/x.bin": b"\x00\xff\r\n"})
    c2 = s.commit("main", {b"data/x.bin": None, **{k.encode(): v for k, v in GOLDEN_CONTENTS.items()}})
    shas = s.run(repo)
    return build_integration(
        Git(repo.path, env=repo.env),
        commit=shas[c2],
        base=shas[c1],
        repository=REPOSITORY,
        repository_id=REPOSITORY_ID,
        pr=4,
        task=2,
        pr_author="ana-p",
        merged_by="lead-x",
        created_at=CREATED,
        generator=GENERATOR,
        salt=salts("golden"),
    )


def test_golden_manifest(tmp_path: Path) -> None:
    """Formato estable: los bytes de un manifiesto conocido no cambian sin querer.

    El fixture sirve también como muestra con aperturas para otras pruebas (abrir
    compromisos sin el núcleo, verificación).
    """
    built = golden_build(tmp_path)
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert built.data == golden["manifest"].encode("utf-8")
    assert built.sha256 == golden["manifest_sha256"] == hashlib.sha256(built.data).hexdigest()
    assert built.openings.to_json() == golden["openings"]
    assert golden["contents"] == {k: v.decode("utf-8") for k, v in GOLDEN_CONTENTS.items()}
    # el fixture se abre solo, sin git: cada apertura reproduce su p, su c y sus actores
    manifest = canon.loads(golden["manifest"].encode("utf-8"))
    entries = manifest_entries(manifest)
    opened = parse_openings(golden["openings"])
    for opening in opened.entries:
        content = golden["contents"].get(opening.path)
        data = content.encode("utf-8") if content is not None else None
        assert check_entry(opening, entries, data).content is True, opening.path
    assert all(check_actor(a, manifest["actors"]) for a in opened.actors)
