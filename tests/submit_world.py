# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Mundo de prueba de ``submit``: el render de la plantilla como base, para que pase ``validate``.

``submit`` corre las reglas de ``validate`` en modo ``tree`` (05 §9.18 paso 5), así que la base
tiene que ser un proyecto válido: el render ``latex`` de ``tools/render_template`` (el que se
publica). ``patch_roles`` cambia ``roles.yaml`` y se regeneran ``CODEOWNERS`` y ``task.yml``,
como haría ``sync-roles``. El FakeForge y el ``origin`` de git son independientes, como en
``board_world``; ``claimed`` siembra el reclamo en los dos y en la caché.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import Path

from aucove.config.project import parse_project
from aucove.forge.base import RepoRef
from aucove.forge.fake import FakeClock, FakeForge
from aucove.ops.rules.roles_sync import regenerate
from tests.board_world import NOW, World, at, clean_env
from tests.conftest import GitRepo, isolated_git_env, load_tool
from tests.task_world import add_claim

LEAD = "example-lead"
RESEARCHER = "example-researcher"


def template_world(tmp: Path, *, lang: str = "es", patch_roles: Callable[[str], str] | None = None,
                   patch_project: Callable[[str], str] | None = None) -> World:
    """Repositorio con el render de la plantilla publicado en ``origin/main`` y un FakeForge."""
    path = tmp / "repo"
    list(load_tool("render_template").render_to(path, lang, fmt="latex"))
    if patch_roles is not None:
        change_roles(path, patch_roles)
    if patch_project is not None:
        project_file = path / "project.yaml"
        project_file.write_text(patch_project(project_file.read_text(encoding="utf-8")), encoding="utf-8",
                                newline="\n")
    env = isolated_git_env(tmp / "git-env")
    origin = tmp / "origin.git"
    repo = GitRepo(path, origin, env)
    repo._run(tmp, ["init", "-q", "--bare", "-b", "main", str(origin)], True)
    repo.git("init", "-q", "-b", "main")
    repo.git("remote", "add", "origin", str(origin))
    sha = repo.commit("Initial commit from the template")
    repo.push("main")
    repo.fetch()
    cfg, problems = parse_project((path / "project.yaml").read_bytes(), file="project.yaml")
    assert cfg is not None, problems
    owner, name = cfg.forge_repo.split("/", 1)
    forge = FakeForge(RepoRef(owner, name), viewer=LEAD, clock=FakeClock(NOW), default_branch=cfg.base_branch)
    for login in (LEAD, RESEARCHER):
        forge.seed_user(login, permission="write")
    base = forge.seed_commit({"README.md": b"x\n"}, [], "base", LEAD, date=at(-1000))
    forge.seed_branch(cfg.base_branch, base)
    return World(repo=repo, forge=forge, base_sha=sha, env=clean_env(env))


def change_roles(path: Path, patch_roles: Callable[[str], str]) -> None:
    """Cambia ``roles.yaml`` y regenera ``CODEOWNERS`` y ``task.yml``, como ``sync-roles``."""
    roles_file = path / "roles.yaml"
    roles_file.write_text(patch_roles(roles_file.read_text(encoding="utf-8")), encoding="utf-8")

    def read(rel: str) -> bytes | None:
        target = path / rel
        return target.read_bytes() if target.is_file() else None

    regen = regenerate(read)
    assert regen.expected is not None, regen.problem
    for rel, data in regen.expected.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_bytes(data)


def claimed(tmp: Path, *, number: int = 11, login: str = LEAD, sections: Sequence[str] = ("introduction",),
            patch_roles: Callable[[str], str] | None = None, state: str = "claimed",
            patch_project: Callable[[str], str] | None = None) -> World:
    """El render con ``task/<n>`` reclamada por ``login`` (forja falsa, git y caché)."""
    w = template_world(tmp, patch_roles=patch_roles, patch_project=patch_project)
    labels = ("state:expired",) if state == "expired" else ()
    w.task(number, claimant=login, author=login, sections=sections, accepter=LEAD, labels=labels)
    add_claim(w, number, login=login, sections=sections, state=state)
    return w


def only_the_lead(text: str) -> str:
    """El ``roles.yaml`` del render sin ``example-researcher``: el de un proyecto individual (D-57)."""
    out, n = re.subn(r"  - login: example-researcher\n(?:    .*\n)*", "", text)
    assert n == 1, "la plantilla no tiene a example-researcher"
    return out


def researcher_owns_results(text: str) -> str:
    """El ``roles.yaml`` del render con la parte ``results`` de ``example-researcher``."""
    head, sep, tail = text.partition("  - id: results\n")
    assert sep, "la plantilla no tiene la parte results"
    lines = tail.split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith("owner:"):
            lines[i] = line.split("owner:")[0] + f"owner: {RESEARCHER}"
            break
    return head + sep + "\n".join(lines)
