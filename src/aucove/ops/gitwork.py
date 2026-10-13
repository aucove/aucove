# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Pasos de git del ciclo de una tarea: traer, fusionar y empujar (04-tareas §5, §6 y §11.3).

Los usan ``save``, ``update``, ``switch`` y ``release`` (05 §9.13 a §9.16):

- ``guard_remote``: con la forja falsa (``<PREFIJO>FORGE_ADAPTER=fake``), git solo habla con
  un remoto local (08-forja REQ-FORGE-62).
- ``fetch_base``: trae la base a ``FETCH_HEAD`` (``--refmap=``: sin mover la ref de
  seguimiento) y comprueba que el oid anterior de ``origin/<base>`` sea ancestro del nuevo
  antes de moverla (10-seguridad REQ-SEC-74). Si la base se reescribió, falla con
  ``E_STATE_BASE_REWRITTEN`` y la ref queda donde estaba: repetir vuelve a fallar hasta que
  la persona clone de nuevo.
- ``fetch_task``: trae ``task/<n>``; distingue la ref que falta en el remoto (REQ-TASK-32)
  de la falta de red con ``git ls-remote --exit-code``.
- ``check_claim``: si el último commit de reclamo de ``origin/task/<n>`` no es el de la rama
  local, la tarea se volvió a reclamar (``E_CLAIM_NOT_HELD``, ``claim_replaced``).
- ``merge``: avance rápido o fusión con ``git merge --no-ff`` (REQ-TASK-47: nunca rebase).
  Clasifica cada conflicto según 04 §6.2 y aplica las resoluciones del núcleo:
  ``auto_rename`` (una decisión agregada en los dos lados se renombra con ``-2``, ``-3``…,
  03 REQ-SCHEMA-47) y ``auto_base`` (se toma la versión de la otra rama y el diff propio se
  guarda como parche; REQ-TASK-48). Si quedan conflictos ``agent``, la fusión queda en curso.
- ``push_task``: ``git push origin <rev>:refs/heads/task/<n>`` sin forzar (04 §11.3), con el
  resultado de ``--porcelain``; ``push_ref`` hace lo mismo con otra ref (la base, en ``init``).

Los mensajes de git se piden en inglés (``LC_ALL=C``) solo donde se interpretan.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .. import _meta
from ..domain.paths import classify
from ..domain.text import same_login
from ..localstore.store import atomic_write, ensure_root
from ..vcs.git import GitError, GitUnavailable
from .context import env_flag, testing
from .result import OpError
from .snapshot import claim_subject, task_branch

if TYPE_CHECKING:
    from ..vcs.git import Git
    from .context import OpContext

__all__ = [
    "BaseFetch",
    "Conflict",
    "MergeOutcome",
    "PushResult",
    "Scope",
    "check_claim",
    "conflict_markers",
    "counts",
    "fetch_base",
    "fetch_task",
    "guard_remote",
    "last_claim_commit",
    "merge",
    "push_ref",
    "push_task",
    "unmerged_paths",
    "worktree_id",
    "write_dropped",
]

FETCH_TIMEOUT_S = 20.0  # como una lectura de la forja (08 REQ-FORGE-23)
PUSH_TIMEOUT_S = 45.0
_C = {"LC_ALL": "C"}  # mensajes de git en inglés donde se interpretan
_CLAIM_LOG_MAX = 2000
_DECISION = re.compile(r"memory/decisions/(?P<stem>[0-9]{4}-[0-9]{2}-[0-9]{2}-[1-9][0-9]*-(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*))\.md")
_SLUG_MAX = 48  # 03 REQ-SCHEMA-45
_MARKER = re.compile(rb"^(?:<{7}|>{7}|\|{7})(?: |$)", re.MULTILINE)
_MARKER_SCAN_MAX = 5 * 1024 * 1024
_NETWORK = (
    "could not resolve host", "unable to access", "connection timed out", "failed to connect",
    "network is unreachable", "connection refused", "operation timed out", "timed out",
    "the remote end hung up", "could not connect", "ssl", "tls",
)
_AUTH = ("authentication failed", "permission denied", "invalid username or password",
         "could not read username", "403", "access denied", "not authorized")


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


# -- remoto y base -------------------------------------------------------------------------


def _is_local_url(url: str | None) -> bool:
    if not url:
        return False
    if url.lower().startswith("file://"):
        return True
    return "://" not in url and (os.path.isabs(url) or url.startswith((".", "/")))


def guard_remote(ctx: OpContext, git: Git) -> None:
    """REQ-FORGE-62: con la forja falsa, ``E_CONFIG_INVALID`` si ``origin`` no es local."""
    env = ctx.inv.env
    if not (testing(env) and env_flag(env, "FORGE_ADAPTER") == "fake"):
        return
    if not _is_local_url(git.remote_url("origin")):
        raise OpError("E_CONFIG_INVALID", details={"field": "remote.origin.url", "reason": "fake_forge_remote"})


@dataclass(frozen=True)
class BaseFetch:
    """La base antes y después de traerla; ``offline`` si no se pudo hablar con ``origin``."""

    offline: bool
    before: str | None
    after: str | None


def fetch_base(git: Git, base: str) -> BaseFetch:
    """Trae ``origin/<base>`` con la comprobación de REQ-SEC-74.

    La base llega primero a una ref privada (``refs/<NAME>/fetch/<base>``, que se borra al
    terminar): ``FETCH_HEAD`` no sirve, porque ``fetch.writeFetchHEAD=false`` lo deja viejo.
    """
    tracking = f"refs/remotes/origin/{base}"
    before = git.rev_parse(tracking)
    if git.offline:
        return BaseFetch(True, before, before)
    temp = f"refs/{_meta.NAME}/fetch/{base}"
    try:
        proc = git.run("fetch", "--quiet", "--no-tags", "--refmap=", "origin", f"+refs/heads/{base}:{temp}",
                       check=False, read=False, timeout_s=FETCH_TIMEOUT_S)
        after = git.rev_parse(temp) if proc.returncode == 0 else None
    except (GitError, GitUnavailable):
        after = None
    finally:
        try:
            git.run("update-ref", "-d", temp, check=False, read=False)
        except (GitError, GitUnavailable):
            pass
    if after is None:
        return BaseFetch(True, before, before)
    if before is not None and after != before:
        try:
            kept = git.is_ancestor(before, after)
        except GitError:
            kept = False  # sin la historia para comprobarlo: falla cerrada
        if not kept:
            raise OpError("E_STATE_BASE_REWRITTEN", details={"ref": f"origin/{base}", "before": before, "after": after})
    if after != before:
        git.run("update-ref", tracking, after, read=False)
    return BaseFetch(False, before, after)


TaskFetch = Literal["ok", "missing", "offline"]


def fetch_task(git: Git, number: int) -> TaskFetch:
    """Trae ``origin/task/<n>``: ``missing`` si la ref no está en el remoto (REQ-TASK-32)."""
    if git.offline:
        return "offline"
    branch = task_branch(number)
    try:
        proc = git.run("fetch", "--quiet", "--no-tags", "--refmap=", "origin",
                       f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
                       check=False, read=False, timeout_s=FETCH_TIMEOUT_S)
        if proc.returncode == 0:
            return "ok"
        probe = git.run("ls-remote", "--exit-code", "origin", f"refs/heads/{branch}",
                        check=False, timeout_s=FETCH_TIMEOUT_S)
    except (GitError, GitUnavailable):
        return "offline"
    return "missing" if probe.returncode == 2 else "offline"


def last_claim_commit(git: Git, number: int, rev: str) -> str | None:
    """Último commit de reclamo de ``<n>`` en la historia de primer padre de ``rev``."""
    subject = claim_subject(number)
    proc = git.run("log", "--first-parent", f"--max-count={_CLAIM_LOG_MAX}", "--format=%H%x00%s", rev,
                   "--", check=False)
    if proc.returncode != 0:
        return None
    for line in _text(proc.stdout).splitlines():
        sha, _, subj = line.partition("\0")
        if subj.strip() == subject:
            return sha
    return None


def check_claim(git: Git, number: int) -> None:
    """``E_CLAIM_NOT_HELD`` (``claim_replaced``) si ``origin/task/<n>`` tiene otro reclamo.

    Sin commit de reclamo en la historia local de primer padre (la rama de una sesión en la
    nube, ligada por ``local_branch``) no hay con qué comparar y no se comprueba.
    """
    mine = last_claim_commit(git, number, "HEAD")
    if mine is None:
        return
    theirs = last_claim_commit(git, number, f"refs/remotes/origin/{task_branch(number)}")
    if theirs is not None and theirs != mine:
        raise OpError("E_CLAIM_NOT_HELD", details={"task": number, "reason": "claim_replaced"})


def counts(git: Git, upstream: str, head: str = "HEAD") -> tuple[int, int]:
    """``(adelante, atrás)`` de ``head`` respecto de ``upstream``; ``(0, 0)`` si falta alguno."""
    proc = git.run("rev-list", "--left-right", "--count", f"{upstream}...{head}", check=False)
    parts = _text(proc.stdout).split()
    if proc.returncode != 0 or len(parts) != 2 or not all(p.isdigit() for p in parts):
        return 0, 0
    behind, ahead = int(parts[0]), int(parts[1])
    return ahead, behind


# -- push ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PushResult:
    """Resultado de ``push_task``; ``reason`` es el ``details.reason`` de ``E_FORGE_PUSH_REJECTED``."""

    outcome: Literal["accepted", "up_to_date", "non_ff", "rejected", "offline"]
    reason: str | None = None


def _rejection(text: str) -> PushResult:
    low = text.lower()
    if "non-fast-forward" in low or "fetch first" in low or "stale info" in low:
        return PushResult("non_ff")
    if "workflow" in low:
        return PushResult("rejected", "workflow_permission")  # 08-forja §5.4
    if "gh013" in low or "repository rule" in low:
        return PushResult("rejected", "ruleset")
    if "protected branch" in low:
        return PushResult("rejected", "protected_branch")
    if "hook declined" in low:
        return PushResult("rejected", "hook")
    return PushResult("rejected", "rejected")


def push_task(git: Git, number: int, rev: str = "HEAD") -> PushResult:
    """Empuja ``rev`` a ``task/<n>`` sin forzar (04 §11.3; nunca ``--force``, REQ-TASK-47)."""
    return push_ref(git, f"refs/heads/{task_branch(number)}", rev)


def push_ref(git: Git, ref: str, rev: str = "HEAD") -> PushResult:
    """Empuja ``rev`` a la ref ``ref`` de ``origin`` sin forzar.

    La usan ``push_task`` y ``init``, que sube a la base los archivos generados mientras la base
    no está protegida (05 §9.3). Un rechazo por no ser avance rápido es ``non_ff``.
    """
    if git.offline:
        return PushResult("offline")
    try:
        proc = git.run("push", "--porcelain", "origin", f"{rev}:{ref}", check=False, read=False,
                       timeout_s=PUSH_TIMEOUT_S, extra_env=_C)
    except (GitError, GitUnavailable):
        return PushResult("offline")
    err = _text(proc.stderr)
    for line in _text(proc.stdout).splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or len(parts[0]) != 1 or parts[1].rsplit(":", 1)[-1] != ref:
            continue
        flag = parts[0]
        if flag == "=":
            return PushResult("up_to_date")
        if flag in " *+":
            return PushResult("accepted")
        if flag == "!":
            return _rejection(" ".join(parts[2:]) + " " + err)
    if proc.returncode == 0:
        return PushResult("accepted")
    low = err.lower()
    if any(h in low for h in _AUTH):
        return PushResult("rejected", "permission")
    if any(h in low for h in _NETWORK):
        return PushResult("offline")
    return PushResult("rejected", "transport")


# -- fusión y conflictos (04 §6.2) --------------------------------------------------------------


Resolution = Literal["agent", "auto_rename", "auto_base"]


@dataclass(frozen=True)
class Scope:
    """Lo que decide quién resuelve un conflicto (04 §6.2; alcance de REQ-TASK-27)."""

    number: int
    login: str
    staff: bool  # el reclamante es lead o deputy
    sections: frozenset[str]  # partes efectivas de la tarea
    owners: Mapping[str, str]  # sección -> dueño, de roles.yaml de la base


@dataclass(frozen=True)
class Conflict:
    """Un archivo en conflicto con su clase (00-marco §5.2) y su resolución."""

    path: str
    cls: str
    resolution: Resolution
    renamed_to: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "class": self.cls, "resolution": self.resolution}


@dataclass(frozen=True)
class MergeOutcome:
    """``status`` y, con conflictos, su lista; ``patch`` es el diff propio que se descartó."""

    status: Literal["up_to_date", "fast_forward", "merged", "conflict"]
    conflicts: tuple[Conflict, ...] = ()
    patch: bytes | None = None
    dropped: tuple[str, ...] = ()
    commit: str | None = None

    @property
    def pending(self) -> tuple[Conflict, ...]:
        """Conflictos que resuelve el agente (``agent``)."""
        return tuple(c for c in self.conflicts if c.resolution == "agent")


def unmerged_paths(git: Git) -> dict[str, frozenset[int]]:
    """Rutas sin fusionar con sus etapas (``git ls-files -u``: 1 base, 2 propia, 3 ajena)."""
    proc = git.run("ls-files", "-u", "-z", check=False)
    out: dict[str, set[int]] = {}
    for record in proc.stdout.split(b"\0"):
        meta, _, name = record.partition(b"\t")
        fields = meta.split()
        if len(fields) != 3 or not name or not fields[2].isdigit():
            continue
        out.setdefault(_text(name), set()).add(int(fields[2]))
    return {path: frozenset(stages) for path, stages in out.items()}


def _resolution(path: str, stages: frozenset[int], scope: Scope) -> tuple[str, Resolution]:
    """Clase y resolución de un archivo en conflicto (tabla de 04 §6.2)."""
    info = classify(path, frozenset(scope.owners), symlinked=False)
    cls = info.cls
    if cls == "append":  # memory/decisions/: agregado en los dos lados (sin etapa 1)
        return cls, ("auto_rename" if stages == frozenset({2, 3}) else "auto_base")
    if cls == "task":
        return cls, ("agent" if info.task == scope.number else "auto_base")
    if cls == "section":
        owner = scope.owners.get(info.section or "")
        mine = info.section in scope.sections and owner is not None and same_login(owner, scope.login)
        return cls, ("agent" if mine else "auto_base")
    if cls == "lead":
        return cls, ("agent" if scope.staff else "auto_base")
    return cls, "auto_base"  # generated, local, outside: fuera del alcance


def _free_decision(git: Git, root: Path, path: str, revs: Sequence[str]) -> str | None:
    """Nombre libre para una decisión propia: el slug con ``-2``, ``-3``… (03 REQ-SCHEMA-47)."""
    m = _DECISION.fullmatch(path)
    if m is None:
        return None
    stem, slug = m["stem"], m["slug"]
    prefix = stem[: len(stem) - len(slug)]
    for k in range(2, 1000):
        suffix = f"-{k}"
        base = slug[: _SLUG_MAX - len(suffix)].rstrip("-")
        candidate = f"memory/decisions/{prefix}{base}{suffix}.md"
        taken = os.path.lexists(root.joinpath(*candidate.split("/"))) or any(
            git.run("cat-file", "-e", f"{rev}:{candidate}", check=False).returncode == 0 for rev in revs
        )
        if not taken:
            return candidate
    return None


def _renamed_record(content: bytes, old: str, new: str) -> bytes:
    """El registro con ``id`` del front matter igual al nombre nuevo (03 REQ-SCHEMA-46)."""
    old_id = old.rsplit("/", 1)[-1].removesuffix(".md")
    new_id = new.rsplit("/", 1)[-1].removesuffix(".md")
    text = content.decode("utf-8", "surrogateescape")
    if not text.startswith("---"):
        return content
    end = text.find("\n---", 3)
    if end < 0:
        return content
    head = re.sub(rf"(?m)^id:[ \t]*['\"]?{re.escape(old_id)}['\"]?[ \t]*$", f"id: {new_id}", text[:end], count=1)
    return (head + text[end:]).encode("utf-8", "surrogateescape")


def _theirs(git: Git, src: str, path: str) -> None:
    """Deja ``path`` como está en ``src`` (o lo borra si ahí no existe), resuelto en el índice."""
    if git.run("cat-file", "-e", f"{src}:{path}", check=False).returncode == 0:
        git.run("--literal-pathspecs", "checkout", src, "--", path, read=False)
    else:
        git.run("--literal-pathspecs", "rm", "-q", "-f", "--ignore-unmatch", "--", path, read=False)


def _merge_failed(proc_err: bytes) -> OpError:
    low = _text(proc_err).lower()
    if "would be overwritten" in low or "untracked working tree" in low:
        return OpError("E_STATE_DIRTY_WORKTREE", details={"reason": "would_be_overwritten"})
    return OpError("E_CONFIG_GIT_UNAVAILABLE", details={"reason": "merge_failed"})


def merge(git: Git, source: str, *, message: str, scope: Scope, root: Path) -> MergeOutcome:
    """Integra ``source`` en ``HEAD`` (04 §6.2 pasos 3 y 4; §5 paso 4).

    ``up_to_date`` si ya está; ``fast_forward`` si ``HEAD`` no tiene nada propio; si no, una
    fusión con ``message`` (que lleva el trailer ``Task``, 11 §2.5). Con conflictos aplica
    ``auto_rename`` y ``auto_base`` y, si no queda ninguno ``agent``, hace el commit de fusión.
    """
    src = git.rev_parse(f"{source}^{{commit}}")
    head = git.rev_parse("HEAD")
    if src is None or head is None:
        raise OpError("E_CONFIG_GIT_UNAVAILABLE", details={"reason": "unknown_revision", "rev": source})
    if git.is_ancestor(src, head):
        return MergeOutcome("up_to_date")
    if git.is_ancestor(head, src):
        proc = git.run("merge", "--ff-only", "--quiet", src, check=False, read=False, extra_env=_C)
        if proc.returncode != 0:
            raise _merge_failed(proc.stderr)
        return MergeOutcome("fast_forward", commit=src)
    base = git.merge_base(head, src)
    proc = git.run("merge", "--no-ff", "--no-edit", "--quiet", "-m", message, src,
                   check=False, read=False, extra_env=_C)
    if proc.returncode == 0:
        return MergeOutcome("merged", commit=git.rev_parse("HEAD"))
    if git.rev_parse("MERGE_HEAD") is None:
        raise _merge_failed(proc.stderr)
    conflicts: list[Conflict] = []
    patch = b""
    dropped: list[str] = []
    for path, stages in sorted(unmerged_paths(git).items()):
        cls, resolution = _resolution(path, stages, scope)
        renamed: str | None = None
        if resolution == "auto_rename":
            renamed = _free_decision(git, root, path, (head, src))
            ours = git.show(head, path)
            if renamed is None or ours is None:
                resolution = "auto_base"
            else:
                target = root.joinpath(*renamed.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(_renamed_record(ours, path, renamed))
                git.run("--literal-pathspecs", "add", "--", renamed, read=False)
                _theirs(git, src, path)
        if resolution == "auto_base":
            old = base or "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # árbol vacío
            diff = git.run("--literal-pathspecs", "diff", "--binary", "--no-color", "--no-ext-diff",
                           "--no-textconv", old, head, "--", path, check=False)
            patch += diff.stdout
            dropped.append(path)
            _theirs(git, src, path)
        conflicts.append(Conflict(path, cls, resolution, renamed))
    outcome = MergeOutcome("conflict", tuple(conflicts), patch or None, tuple(dropped))
    if outcome.pending:
        return outcome  # la fusión queda en curso: el agente resuelve y sigue con update --continue
    git.run("commit", "--quiet", "--no-edit", "--cleanup=strip", read=False)
    return MergeOutcome("merged", tuple(conflicts), patch or None, tuple(dropped), git.rev_parse("HEAD"))


def conflict_markers(git: Git, root: Path, paths: Sequence[str]) -> list[str]:
    """Rutas de ``paths`` que aún tienen marcas de conflicto (``<<<<<<<``, ``|||||||``, ``>>>>>>>``)."""
    found: list[str] = []
    for path in paths:
        target = root.joinpath(*path.split("/"))
        try:
            if not target.is_file() or target.is_symlink() or target.stat().st_size > _MARKER_SCAN_MAX:
                continue
            if _MARKER.search(target.read_bytes()):
                found.append(path)
        except OSError:
            continue
    return found


# -- trabajo descartado -----------------------------------------------------------------------


def worktree_id(root: Path) -> str:
    """``<id>`` del worktree: 12 hex del SHA-256 de su ruta real (01-arquitectura §7.3)."""
    real = os.path.realpath(os.fspath(root))
    return hashlib.sha256(real.encode("utf-8", "surrogateescape")).hexdigest()[:12]


def write_dropped(cache_root: Path, worktree: Path, number: int, now: datetime, patch: bytes) -> str | None:
    """Guarda ``patch`` en ``work/dropped/<id>/<YYYYMMDDTHHMMSSZ>-task-<n>.patch`` (04 §6.2).

    Devuelve la ruta relativa al checkout principal, o ``None`` si no se pudo escribir.
    """
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rel = ("work", "dropped", worktree_id(worktree), f"{stamp}-task-{number}.patch")
    target = cache_root.joinpath(*rel)
    try:
        if not ensure_root(cache_root):
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        if not atomic_write(target, patch):
            return None
    except OSError:
        return None
    return "/".join((_meta.CACHE_DIR, *rel))
