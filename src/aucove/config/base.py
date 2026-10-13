# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Política de la rama base (06-permisos REQ-PERM-1..4, §2.1; 03 §2.5).

``read_base`` lee ``project.yaml``, ``roles.yaml`` y ``memory/decisions/`` de
``<remote>/<base>`` con ``git show`` y ``git ls-tree`` (``vcs.git.Git``). Si un
archivo de la punta no valida, usa la versión válida más reciente de ese archivo
en los últimos 100 commits de la historia de primer padre y lo informa en
``fallbacks`` y ``fallback_sha`` (el llamador agrega ``W_CONFIG_POLICY_FALLBACK``); sin
versión válida, ``E_CONFIG_POLICY_UNAVAILABLE`` y se deniega toda escritura (REQ-PERM-4).

El nombre de la rama base nunca sale del árbol de trabajo (07-hooks REQ-HOOK-41,
06-permisos §2.1): ``resolve_base_branch`` lo lee del ``project.yaml`` de la rama por
defecto de la forja (``refs/remotes/<remote>/HEAD``) o, si esa ref no existe, de
``capabilities.json``; sin ninguna de las dos, ``E_CONFIG_POLICY_UNAVAILABLE``. Después,
el ``project.yaml`` de ``<remote>/<base>`` tiene que declarar esa misma rama.

``read_policy_dir`` lee la política de un directorio (en CI, ``--policy`` es un
checkout de la base) y ``read_tree_base`` la de un árbol explícito (``--tree``) con
``git -C <tree> show origin/<base>:...``. ``to_policy`` arma ``domain.permissions.Policy``.

``config`` está por debajo de ``ops`` en las capas (01 §3.1), así que el ``OpError``
de ``ops.result`` se obtiene por nombre en tiempo de ejecución (:func:`_op_error`).
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Protocol, TypeVar

from .. import _meta
from ..domain.permissions import Policy
from ..i18n import Translator
from .project import ProjectConfig, parse_project
from .roles import Roles, parse_roles

PROJECT_FILE = "project.yaml"
ROLES_FILE = "roles.yaml"
DECISIONS_DIR = "memory/decisions/"
FALLBACK_WINDOW = 100  # commits de primer padre (REQ-PERM-4)
UNAVAILABLE = "E_CONFIG_POLICY_UNAVAILABLE"
CAPABILITIES_FILE = "capabilities.json"  # en <checkout principal>/<CACHE_DIR>/cache/ (01 §7.3)
_CACHE_SCHEMA = 1  # sobre común de las cachés (01 §7.3)
_MAX_CAPABILITIES_BYTES = 1 << 20

Source = Literal["live", "cache", "local"]

# Nombres admitidos antes de armar una ref (evitan opciones de git disfrazadas de rama).
# Se usan con ``fullmatch``: con ``match``, ``$`` aceptaría un salto de línea final.
_BRANCH_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*(/[A-Za-z0-9_][A-Za-z0-9._-]*)*")
_REMOTE_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")
_DECISION_RE = re.compile(r"memory/decisions/[^/]+\.md")
_SHA_RE = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")


class GitLike(Protocol):
    """Lo que usa este módulo de ``vcs.git.Git``."""

    def run(self, *args: str, check: bool = True, input: bytes | None = None) -> object: ...
    def show(self, rev: str, path: str) -> bytes | None: ...
    def rev_parse(self, rev: str) -> str | None: ...
    def ls_tree(self, rev: str, paths: Sequence[str] = ()) -> Mapping[str, tuple[str, str]]: ...
    def first_parent_revs(self, rev: str, path: str, max_count: int = 100) -> list[str]: ...


@dataclass(frozen=True)
class BaseSnapshot:
    """Política de la base: ``project.yaml``, ``roles.yaml`` y decisiones existentes."""

    ref: str
    sha: str | None
    project: ProjectConfig
    roles: Roles
    decisions: frozenset[str]  # memory/decisions/*.md en la base
    # No None -> el llamador agrega W_CONFIG_POLICY_FALLBACK. Si ambos archivos usaron
    # respaldo, es el sha de roles.yaml, el que decide la autorización (REQ-PERM-4).
    fallback_sha: str | None
    source: Source
    fallbacks: Mapping[str, str] = field(default_factory=dict)  # archivo -> sha de la versión usada

    def fallback_details(self) -> dict[str, object] | None:
        """``details`` de ``W_CONFIG_POLICY_FALLBACK`` con el sha de cada archivo; ``None`` sin respaldo.

        ``sha`` es ``fallback_sha`` (compatibilidad) y ``files`` da ``archivo -> sha`` de cada
        versión usada, para no atribuir a ``roles.yaml`` el sha de ``project.yaml``.
        """
        if self.fallback_sha is None:
            return None
        return {"sha": self.fallback_sha, "files": dict(sorted(self.fallbacks.items()))}


def _op_error_class() -> type[Exception]:
    """``ops.result.OpError`` sin importar ``ops`` desde ``config`` (capas de 01 §3.1)."""
    return importlib.import_module(f"{_meta.NAME}.ops.result").OpError  # type: ignore[no-any-return]


def _op_error(details: Mapping[str, object], code: str = UNAVAILABLE) -> Exception:
    """``OpError(code, details=...)``; ``registry.invoke`` lo convierte en ``fail``."""
    return _op_error_class()(code, details=dict(details))  # type: ignore[call-arg]


T = TypeVar("T")


def _parse_project_bytes(data: bytes, t: Translator | None) -> ProjectConfig | None:
    cfg, _findings = parse_project(data, file=PROJECT_FILE, t=t)
    return cfg


def _parse_roles_bytes(data: bytes, t: Translator | None) -> Roles | None:
    roles, _findings = parse_roles(data, file=ROLES_FILE, t=t)
    return roles


def _lines_of(output: object) -> list[str]:
    raw = getattr(output, "stdout", output)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _window(git: GitLike, tip: str) -> list[str]:
    """Los últimos ``FALLBACK_WINDOW`` commits de primer padre desde ``tip`` (la punta primero)."""
    out = git.run("rev-list", "--first-parent", f"--max-count={FALLBACK_WINDOW}", tip)
    return [sha for sha in _lines_of(out) if _SHA_RE.fullmatch(sha)]


def _candidates(git: GitLike, tip: str, path: str, *, include_tip: bool) -> Iterator[str]:
    """Commits de la ventana de primer padre donde puede haber otra versión de ``path``.

    Son los que tocaron ``path`` (``first_parent_revs``) dentro de la ventana, más el
    más antiguo de la ventana, cuya versión puede venir de antes de ella; del más
    nuevo al más antiguo. Un commit fuera de la ventana nunca es candidato.
    """
    window = _window(git, tip)
    inside = set(window)
    touching = {sha for sha in git.first_parent_revs(tip, path, max_count=FALLBACK_WINDOW) if sha in inside}
    if include_tip and window:
        touching.add(window[0])
    order = {sha: i for i, sha in enumerate(window)}
    picked = sorted(touching, key=order.__getitem__)
    if window and window[-1] not in picked:
        picked.append(window[-1])
    yield from (sha for sha in picked if include_tip or sha != tip)


def _read_versioned(
    git: GitLike,
    tip: str,
    path: str,
    parse: Callable[[bytes], T | None],
    *,
    current: bytes | None,
    include_tip: bool = False,
) -> tuple[T | None, str | None]:
    """Versión válida de ``path``: la de ``current`` o, si no valida, la de REQ-PERM-4.

    Devuelve ``(objeto, None)`` si ``current`` vale, ``(objeto, sha)`` si se usó el
    respaldo y ``(None, None)`` si no hay ninguna versión válida en la ventana.
    """
    tried: set[bytes] = set()
    if current is not None:
        parsed = parse(current)
        if parsed is not None:
            return parsed, None
        tried.add(hashlib.sha256(current).digest())
    for sha in _candidates(git, tip, path, include_tip=include_tip):
        data = git.show(sha, path)
        if data is None:
            continue
        digest = hashlib.sha256(data).digest()
        if digest in tried:  # misma versión que otra ya descartada
            continue
        tried.add(digest)
        parsed = parse(data)
        if parsed is not None:
            return parsed, sha
    return None, None


def _decisions_from_git(git: GitLike, sha: str) -> frozenset[str]:
    entries = git.ls_tree(sha, [DECISIONS_DIR])
    return frozenset(
        path for path, (mode, _blob) in entries.items() if _DECISION_RE.fullmatch(path) and not mode.startswith("04")
    )


def _valid_branch(name: object) -> bool:
    """Nombre de rama del esquema (``forge.base_branch``) que además no empieza por ``-``."""
    return (
        isinstance(name, str)
        and _BRANCH_RE.fullmatch(name) is not None
        and ".." not in name
        and not name.endswith((".lock", "."))
    )


def _capabilities_path(git: GitLike) -> Path | None:
    """``capabilities.json`` del checkout principal, el padre de ``commondir`` (REQ-ARCH-61)."""
    out = git.run("rev-parse", "--path-format=absolute", "--git-common-dir", check=False)
    if getattr(out, "returncode", 0) != 0:
        return None
    lines = _lines_of(out)
    if len(lines) != 1:
        return None
    common = Path(lines[0])
    if common.name.lower() != ".git":  # repositorio bare o submódulo: no hay caché
        return None
    return common.parent / _meta.CACHE_DIR / "cache" / CAPABILITIES_FILE


def _capabilities_branch(path: Path | None) -> str | None:
    """``key.base_branch`` de ``capabilities.json`` (08-forja §3.4).

    Una entrada que no cumple el sobre común de 01 §7.3 (``cache_schema`` y ``core`` del
    proceso) cuenta como ausente (REQ-ARCH-58). El vencimiento de 24 h no se mira: sin red,
    es el último nombre de la base que confirmó la forja.
    """
    if path is None:
        return None
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_CAPABILITIES_BYTES:
            return None
        env = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError, RecursionError):  # UnicodeDecodeError y JSONDecodeError son ValueError
        return None
    if not isinstance(env, dict):
        return None
    schema = env.get("cache_schema")
    if type(schema) is not int or schema != _CACHE_SCHEMA or env.get("core") != _meta.__version__:
        return None
    if not isinstance(env.get("created_at"), str) or not isinstance(env.get("data"), dict):
        return None
    key = env.get("key")
    branch = key.get("base_branch") if isinstance(key, dict) else None
    return branch if _valid_branch(branch) else None


@dataclass(frozen=True)
class _Resolved:
    """Rama base resuelta y, si salió de ``<remote>/HEAD``, lo ya leído de esa punta."""

    branch: str
    head_sha: str | None = None
    project: ProjectConfig | None = None
    project_fb: str | None = None


def _resolve(git: GitLike, remote: str, t: Translator | None) -> _Resolved:
    head_ref = f"{remote}/HEAD"
    if not isinstance(remote, str) or not _REMOTE_RE.fullmatch(remote):
        raise _op_error({"ref": head_ref, "reason": "invalid_ref"})
    project: ProjectConfig | None = None
    project_fb: str | None = None
    branch: str | None = None
    try:
        head = git.rev_parse(f"refs/remotes/{head_ref}")
        if head is not None:
            project, project_fb = _read_versioned(
                git, head, PROJECT_FILE, lambda b: _parse_project_bytes(b, t), current=git.show(head, PROJECT_FILE)
            )
        else:
            branch = _capabilities_branch(_capabilities_path(git))
    except Exception as exc:
        if isinstance(exc, _op_error_class()):
            raise
        raise _op_error({"ref": head_ref, "reason": "git_failed"}) from exc
    if head is not None:
        if project is None:
            raise _op_error({"ref": head_ref, "path": PROJECT_FILE, "reason": "no_valid_version"})
        branch = project.base_branch
    if branch is None:  # sin <remote>/HEAD y sin capabilities.json: se falla cerrado
        raise _op_error({"ref": head_ref, "reason": "ref_missing"})
    if not _valid_branch(branch):
        raise _op_error({"ref": f"{remote}/{branch}", "reason": "invalid_ref"})
    return _Resolved(branch, head, project, project_fb)


def resolve_base_branch(git: GitLike, *, remote: str = "origin", t: Translator | None = None) -> str:
    """Nombre de la rama base, sin mirar el árbol de trabajo (07-hooks REQ-HOOK-41).

    1. ``forge.base_branch`` del ``project.yaml`` de ``refs/remotes/<remote>/HEAD``, la rama
       por defecto de la forja, con el respaldo de REQ-PERM-4 si su punta no valida.
    2. Si esa ref no existe, ``key.base_branch`` de ``capabilities.json`` (08-forja §3.4).
    3. Si no hay ninguna de las dos: ``E_CONFIG_POLICY_UNAVAILABLE``
       (``{"ref": "<remote>/HEAD", "reason": "ref_missing"}``).
    """
    return _resolve(git, remote, t).branch


def read_base(
    git: GitLike,
    base_branch: str | None = None,
    *,
    remote: str = "origin",
    t: Translator | None = None,
) -> BaseSnapshot:
    """Política de ``<remote>/<base>`` con el respaldo de REQ-PERM-4.

    ``<base>`` lo da :func:`resolve_base_branch`, nunca el llamador. ``base_branch`` se
    acepta por compatibilidad con los llamadores que lo leían del ``project.yaml`` del
    árbol y se ignora: ese archivo no decide qué política se aplica (07-hooks REQ-HOOK-41,
    06-permisos REQ-PERM-1 y §2.1, D-08).

    ``OpError(E_CONFIG_POLICY_UNAVAILABLE)`` si no se puede resolver la base, si la ref no
    existe (nunca se hizo ``fetch``, REQ-PERM-3), si git falla, si algún archivo no tiene
    versión válida en la ventana, o si el ``project.yaml`` de ``<remote>/<base>`` declara
    otra rama base que la que declara la rama por defecto.
    """
    del base_branch  # no decide nada (REQ-HOOK-41)
    resolved = _resolve(git, remote, t)
    ref = f"{remote}/{resolved.branch}"
    try:
        sha = git.rev_parse(f"refs/remotes/{ref}")
        if sha is None:
            raise _op_error({"ref": ref, "reason": "ref_missing"})
        if sha == resolved.head_sha:  # la rama por defecto es la base: ya se leyó su project.yaml
            project, project_fb = resolved.project, resolved.project_fb
        else:
            project, project_fb = _read_versioned(
                git, sha, PROJECT_FILE, lambda b: _parse_project_bytes(b, t), current=git.show(sha, PROJECT_FILE)
            )
        roles, roles_fb = _read_versioned(
            git, sha, ROLES_FILE, lambda b: _parse_roles_bytes(b, t), current=git.show(sha, ROLES_FILE)
        )
        decisions = _decisions_from_git(git, sha)
    except Exception as exc:
        if isinstance(exc, _op_error_class()):
            raise
        raise _op_error({"ref": ref, "reason": "git_failed"}) from exc
    if project is None:
        raise _op_error({"ref": ref, "path": PROJECT_FILE, "reason": "no_valid_version"})
    if roles is None:
        raise _op_error({"ref": ref, "path": ROLES_FILE, "reason": "no_valid_version"})
    if project.base_branch != resolved.branch:
        raise _op_error({"ref": ref, "reason": "base_branch_mismatch", "declared": project.base_branch})
    return _snapshot(ref, sha, project, roles, decisions, project_fb, roles_fb, "live")


def read_policy_at(git: GitLike, sha: str, *, t: Translator | None = None) -> BaseSnapshot:
    """Política del commit ``sha`` (``git show <sha>:project.yaml`` y ``roles.yaml``).

    Con el respaldo de REQ-PERM-4 desde ese commit, como :func:`read_base` desde la punta de la
    base. La usa ``ci audit`` con ``--before`` (09-ci REQ-CI-46): la política con la que se
    integró cada commit del push. ``OpError(E_CONFIG_POLICY_UNAVAILABLE)`` si git falla o si
    algún archivo no tiene versión válida en la ventana.
    """
    try:
        project, project_fb = _read_versioned(
            git, sha, PROJECT_FILE, lambda b: _parse_project_bytes(b, t), current=git.show(sha, PROJECT_FILE)
        )
        roles, roles_fb = _read_versioned(
            git, sha, ROLES_FILE, lambda b: _parse_roles_bytes(b, t), current=git.show(sha, ROLES_FILE)
        )
        decisions = _decisions_from_git(git, sha)
    except Exception as exc:
        if isinstance(exc, _op_error_class()):
            raise
        raise _op_error({"ref": sha, "reason": "git_failed"}) from exc
    if project is None:
        raise _op_error({"ref": sha, "path": PROJECT_FILE, "reason": "no_valid_version"})
    if roles is None:
        raise _op_error({"ref": sha, "path": ROLES_FILE, "reason": "no_valid_version"})
    return _snapshot(sha, sha, project, roles, decisions, project_fb, roles_fb, "live")


def _snapshot(
    ref: str,
    sha: str | None,
    project: ProjectConfig,
    roles: Roles,
    decisions: frozenset[str],
    project_fb: str | None,
    roles_fb: str | None,
    source: Source,
) -> BaseSnapshot:
    fallbacks = {name: fb for name, fb in ((PROJECT_FILE, project_fb), (ROLES_FILE, roles_fb)) if fb is not None}
    return BaseSnapshot(
        ref=ref,
        sha=sha,
        project=project,
        roles=roles,
        decisions=decisions,
        fallback_sha=roles_fb if roles_fb is not None else project_fb,  # roles.yaml decide la autorización
        source=source,
        fallbacks=MappingProxyType(fallbacks),
    )


def _read_file(path: Path) -> bytes | None:
    try:
        if path.is_symlink() or not path.is_file():  # la política no sigue enlaces
            return None
        return path.read_bytes()
    except OSError:
        return None


def _decisions_from_dir(root: Path) -> frozenset[str]:
    folder = root / DECISIONS_DIR
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return frozenset()
    return frozenset(
        f"{DECISIONS_DIR}{e.name}" for e in entries if e.name.endswith(".md") and not e.is_dir(follow_symlinks=False)
    )


def _dir_git(root: Path) -> GitLike | None:
    """``vcs.git.Git`` sobre un checkout, o ``None`` si no es un repositorio o git no está."""
    if not (root / ".git").exists():
        return None
    try:
        from ..vcs.git import Git

        return Git(root)
    except Exception:  # sin vcs.git o sin git: no hay respaldo, se falla cerrado más abajo
        return None


def read_policy_dir(path: Path | str, *, t: Translator | None = None) -> BaseSnapshot:
    """Política de un directorio con ``project.yaml`` y ``roles.yaml`` (en CI, ``--policy``).

    Si el directorio es un checkout con historia, un archivo que no valida usa el
    respaldo de REQ-PERM-4 desde ``HEAD``; si no, ``E_CONFIG_POLICY_UNAVAILABLE``.
    """
    root = Path(path)
    ref = root.as_posix()
    current_project = _read_file(root / PROJECT_FILE)
    current_roles = _read_file(root / ROLES_FILE)
    project = _parse_project_bytes(current_project, t) if current_project is not None else None
    roles = _parse_roles_bytes(current_roles, t) if current_roles is not None else None
    git = _dir_git(root)
    sha: str | None = None
    project_fb = roles_fb = None
    if git is not None:
        try:
            sha = git.rev_parse("HEAD")
            if sha is not None and project is None:
                project, project_fb = _read_versioned(
                    git, sha, PROJECT_FILE, lambda b: _parse_project_bytes(b, t),
                    current=current_project, include_tip=True,
                )
            if sha is not None and roles is None:
                roles, roles_fb = _read_versioned(
                    git, sha, ROLES_FILE, lambda b: _parse_roles_bytes(b, t),
                    current=current_roles, include_tip=True,
                )
        except Exception:  # un fallo de git deja sin respaldo lo que falte: se falla cerrado abajo
            pass
    if project is None:
        raise _op_error({"ref": ref, "path": PROJECT_FILE, "reason": "no_valid_version"})
    if roles is None:
        raise _op_error({"ref": ref, "path": ROLES_FILE, "reason": "no_valid_version"})
    return _snapshot(ref, sha, project, roles, _decisions_from_dir(root), project_fb, roles_fb, "local")


def read_tree_base(
    tree: Path | str,
    *,
    remote: str = "origin",
    env: Mapping[str, str] | None = None,
    t: Translator | None = None,
) -> BaseSnapshot:
    """Política para ``validate --mode tree --tree <dir>``: ``git -C <tree> show <remote>/<base>``.

    La rama base la da :func:`resolve_base_branch` en el repositorio de ``<tree>``; el
    ``project.yaml`` de ``<tree>`` es solo un dato que valida la regla ``schemas`` (sus
    errores son hallazgos, 03-esquemas §2.4; REQ-HOOK-41). Sin git: ``E_CONFIG_GIT_UNAVAILABLE``.
    """
    root = Path(tree)
    try:
        from ..vcs.git import Git

        git = Git(root, env=env)
    except Exception as exc:
        raise _op_error({"root": root.as_posix()}, code="E_CONFIG_GIT_UNAVAILABLE") from exc
    return read_base(git, remote=remote, t=t)


def to_policy(
    snap: BaseSnapshot,
    *,
    root: str,
    ci_fs: bool,
    case_map: Mapping[str, str] | None = None,
    link_probe: Callable[[str], bool] | None = None,
) -> Policy:
    """``domain.permissions.Policy`` de la base (06 §2.1).

    Solo las personas activas tienen permisos (``active: false`` es una baja).
    ``display`` guarda la grafía de ``people[].login``; ``case_map`` solo se pasa con un
    sistema de archivos que no distingue mayúsculas (``ci_fs``). El editor del artículo solo
    cuenta con ``format: latex`` (03 REQ-SCHEMA-95).
    """
    roles = snap.roles
    return Policy(
        people=frozenset(p.login for p in roles.people if p.active),
        lead=roles.lead,
        deputy=roles.deputy,
        sections=roles.section_owner(),
        base_decisions=snap.decisions,
        root=root,
        ci_fs=ci_fs,
        display={p.login.lower(): p.login for p in roles.people},
        case_map=case_map if ci_fs else None,
        link_probe=link_probe,
        editor=roles.editor if snap.project.format == "latex" else None,
    )
