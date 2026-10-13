# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Pasos locales de git de las operaciones de tarea (05 §9.12 a §9.16; 04-tareas §2.2 paso 8 y §5).

- ``dirty``: hay cambios sin guardar en archivos versionados (lo nuevo sin versionar no
  impide cambiar de rama).
- ``merge_in_progress``: hay una fusión a medias (``MERGE_HEAD``).
- ``fetch_task``: trae ``origin/task/<n>`` sin etiquetas, con el plazo de la forja.
- ``checkout_task``: deja la rama local ``task/<n>`` siguiendo a ``origin/task/<n>`` y cambia
  a ella si se puede. Con la forja falsa no trae de un remoto que no sea local
  (``remote_not_local``, 08 REQ-FORGE-62). En una sesión en la nube (``CLAUDE_CODE_REMOTE=true``) no cambia de
  rama: la sesión sigue en su rama, que pasa a seguir a ``task/<n>`` (06-permisos §2.1).

Un fallo de git en estos pasos no hace fallar la operación: se informa en ``reason`` y
quien llama agrega el aviso.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .gitwork import guard_remote
from .result import OpError
from .snapshot import task_branch

if TYPE_CHECKING:
    from ..vcs.git import Git
    from .context import OpContext

__all__ = ["Checkout", "checkout_task", "dirty", "fetch_task", "merge_in_progress"]

FETCH_TIMEOUT_S = 20.0  # como la lectura de la forja (08 REQ-FORGE-23)


def dirty(git: Git) -> bool:
    """``True`` si hay cambios sin guardar en archivos versionados (en el índice o no)."""
    proc = git.run("status", "--porcelain", "--untracked-files=no", check=False)
    return proc.returncode != 0 or bool(proc.stdout.strip())


def merge_in_progress(ctx: OpContext) -> bool:
    """``True`` si hay una fusión a medias en el worktree (``MERGE_HEAD``)."""
    project = ctx.project()
    assert project is not None
    return (project.git_dir / "MERGE_HEAD").exists()


def fetch_task(git: Git, number: int) -> bool:
    """``git fetch`` de ``task/<n>`` a ``origin/task/<n>``; ``False`` si no se pudo."""
    branch = task_branch(number)
    try:
        proc = git.run(
            "fetch", "--quiet", "--no-tags", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            check=False, read=False, timeout_s=FETCH_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 - sin red o git sin responder
        return False
    return proc.returncode == 0


@dataclass(frozen=True)
class Checkout:
    """Resultado del paso local: ``done`` y el motivo si no se cambió de rama."""

    done: bool
    reason: str | None
    local_branch: str | None = None  # en la nube, la rama de la sesión ligada a la tarea

    def to_dict(self) -> dict[str, object]:
        return {"done": self.done, "reason": self.reason}


def checkout_task(ctx: OpContext, git: Git, number: int, *, wanted: bool) -> Checkout:
    """Paso ``checkout`` de ``claim`` y ``switch`` (05 §9.12 paso 8)."""
    branch = task_branch(number)
    try:
        current = git.current_branch()
    except Exception:  # noqa: BLE001
        current = None
    if ctx.inv.env.get("CLAUDE_CODE_REMOTE") == "true":
        if current is None:
            return Checkout(False, "detached_head")
        if current != branch:
            proc = git.run("config", f"branch.{current}.merge", f"refs/heads/{branch}", check=False, read=False)
            if proc.returncode != 0:
                return Checkout(False, "git_error")
        return Checkout(True, None, local_branch=current)
    if not wanted:
        return Checkout(False, "checkout_false")
    if current == branch:
        return Checkout(True, None)
    if dirty(git):
        return Checkout(False, "dirty_worktree")
    try:
        guard_remote(ctx, git)  # con la forja falsa, solo un remoto local (REQ-FORGE-62)
    except OpError:
        return Checkout(False, "remote_not_local")
    if not fetch_task(git, number):
        return Checkout(False, "fetch_failed")
    exists = git.run("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0
    if exists:
        proc = git.run("switch", "--quiet", branch, check=False, read=False)
    else:
        proc = git.run("switch", "--quiet", "--track", "-c", branch, f"origin/{branch}", check=False, read=False)
    if proc.returncode != 0:
        return Checkout(False, "git_error")
    return Checkout(True, None)
