# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Mundo de prueba de ``save``, ``update``, ``switch`` y ``release``.

Parte de ``board_world``: el FakeForge y el remoto ``origin`` (un repositorio bare en disco)
son independientes. ``claimed`` siembra el reclamo en los dos (en git, el commit de reclamo
empujado a ``task/<n>``) y escribe la caché de la tarea como la deja ``claim``. ``clone``
da otro clon del mismo ``origin``: la otra máquina de la persona, o la base que avanza.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from aucove import _meta
from aucove.localstore.store import Store
from aucove.ops.board import write_task_cache
from tests.board_world import NOW, World, new_world
from tests.conftest import GitRepo

REPO = "example-org/demo"


def claim_commit(repo: GitRepo, number: int) -> str:
    return repo.commit(f"{_meta.CLAIM_SUBJECT}{number}\n\nTask: #{number}\nAI-Use: none", allow_empty=True)


def add_claim(w: World, number: int, *, login: str = "carla", sections: Sequence[str] = ("methods",),
              state: str = "claimed", checkout: bool = True) -> str:
    """El reclamo en git (``task/<n>`` en ``origin``) y en la caché; devuelve el sha del reclamo."""
    repo = w.repo
    current = repo.git("rev-parse", "--abbrev-ref", "HEAD")
    repo.git("switch", "-q", "-c", f"task/{number}", "origin/main")
    sha = claim_commit(repo, number)
    repo.git("push", "-q", "origin", f"task/{number}")
    repo.git("fetch", "-q", "origin")
    if not checkout:
        repo.git("switch", "-q", current)
    write_task_cache(Store(repo.path), number, REPO, claimant=login, state=state, sections=sections, now=NOW)
    return sha


def claimed(tmp: Path, *, number: int = 11, login: str = "carla", sections: Sequence[str] = ("methods",),
            state: str = "claimed", extra_files: Mapping[str, str | bytes] | None = None) -> World:
    """Repositorio en ``task/<n>`` recién reclamada por ``login`` (forja falsa, git y caché)."""
    w = new_world(tmp, extra_files=extra_files)
    labels = ("state:expired",) if state == "expired" else ()
    w.task(number, claimant=login, sections=sections, labels=labels)
    add_claim(w, number, login=login, sections=sections, state=state)
    return w


def clone(w: World, tmp: Path, name: str, *, branch: str = "main") -> GitRepo:
    """Otro clon del mismo ``origin``, en ``branch``."""
    path = tmp / name
    w.repo._run(tmp, ["clone", "-q", "-b", branch, str(w.repo.origin), str(path)], True)
    return GitRepo(path, w.repo.origin, w.repo.env)


def message(repo: GitRepo, rev: str = "HEAD") -> str:
    return repo.git("log", "-1", "--format=%B", rev)


def remote_head(repo: GitRepo, branch: str) -> str | None:
    out = repo.git("ls-remote", "origin", f"refs/heads/{branch}")
    return out.split()[0] if out else None
