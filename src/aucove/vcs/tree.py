# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Vista de un árbol que se valida, sin seguir enlaces (09 REQ-CI-24).

- ``git_tree(repo, rev)``: objetos de git (modo ``pr`` y ``base`` del CI). Los modos
  salen de ``git ls-tree -r -z``; nunca se lee el contenido de una entrada ``120000``
  (enlace) ni ``160000`` (gitlink).
- ``worktree(root)``: árbol de trabajo (modo ``tree``). Las entradas son los archivos
  rastreados más los no rastreados y no ignorados que existen en disco. La lectura hace
  ``lstat`` de cada prefijo, rechaza enlaces (en Windows, todo punto de reanálisis,
  porque ``O_NOFOLLOW`` no existe), exige que la ``realpath`` quede dentro de la raíz,
  abre con ``O_NOFOLLOW`` donde existe y comprueba con ``fstat`` que es el mismo archivo.

Las dos vistas leen solo entradas regulares (modo ``100...``) de ``entries()`` y como
máximo ``limit`` bytes (5 MiB por defecto); en cualquier otro caso devuelven ``None``.
Excepción: en un clon parcial sin red, ``git_tree`` lanza ``ObjectUnavailable`` al leer
un blob que no está en local, para que la regla falle cerrada en lugar de saltárselo.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from .. import _meta
from .git import Git, GitError, GitUnavailable, ObjectUnavailable, UnknownRevision
from .links import is_link

__all__ = [
    "MAX_FILE_BYTES",
    "SYMLINK_MODE",
    "GITLINK_MODE",
    "GitTreeView",
    "TreeView",
    "WorktreeView",
    "git_tree",
    "is_regular_mode",
    "worktree",
]

_WINDOWS = os.name == "nt"

MAX_FILE_BYTES = 5 * 2**20  # 09 REQ-CI-24: cada archivo se trata como texto de 5 MB como máximo
SYMLINK_MODE = "120000"
GITLINK_MODE = "160000"
_REGULAR = "100644"
_EXECUTABLE = "100755"
_CONTENT_CACHE_BYTES = 32 * 2**20  # contenido de blobs ya leídos (por sha, inmutable)


@runtime_checkable
class TreeView(Protocol):
    """Árbol validado: entradas con su modo git y lectura acotada sin seguir enlaces."""

    root: Path | None
    sha: str | None

    def entries(self) -> Mapping[str, str]:
        """``ruta -> modo git`` (``100644``, ``100755``, ``120000``, ``160000``)."""
        ...

    def read_bytes(self, path: str, limit: int = MAX_FILE_BYTES) -> bytes | None:
        """Contenido de una entrada regular de hasta ``limit`` bytes; si no, ``None``."""
        ...

    def read_text(self, path: str, limit: int = MAX_FILE_BYTES) -> str | None:
        """Como ``read_bytes`` y decodificado en UTF-8 estricto; ``None`` si no decodifica."""
        ...


def is_regular_mode(mode: str | None) -> bool:
    """``True`` para los modos de archivo regular de git (``100644``, ``100755``...)."""
    return mode is not None and mode.startswith("100")


def _decode_strict(data: bytes | None) -> str | None:
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


# -- objetos de git ----------------------------------------------------------------


class GitTreeView:
    """Árbol de un commit leído de los objetos de git."""

    def __init__(self, git: Git, rev: str, *, root: Path | None = None) -> None:
        sha = git.rev_parse(f"{rev}^{{commit}}")
        if sha is None:
            raise UnknownRevision(rev)
        self._git = git
        self.root: Path | None = Path(root) if root is not None else git.root
        self.sha: str | None = sha
        listing = git.ls_tree_long(sha)
        self._objects: dict[str, tuple[str, int | None]] = {p: (o, s) for p, (_m, o, s) in listing.items()}
        self._entries: Mapping[str, str] = MappingProxyType({p: m for p, (m, _o, _s) in listing.items()})
        self._cache: dict[str, bytes] = {}
        self._cached_bytes = 0

    def entries(self) -> Mapping[str, str]:
        return self._entries

    def read_bytes(self, path: str, limit: int = MAX_FILE_BYTES) -> bytes | None:
        mode = self._entries.get(path)
        if not is_regular_mode(mode):
            return None  # no existe, enlace (120000) o gitlink (160000): no se lee
        obj, size = self._objects[path]
        if size is None:
            # blob regular sin tamaño: no está en local (clon parcial sin red). No es
            # «no legible»: la regla que lo pide falla cerrada.
            raise ObjectUnavailable(self.sha or obj, path)
        limit = max(0, int(limit))
        if size > limit:
            return None
        data = self._cache.get(obj)
        if data is None:
            try:
                proc = self._git.run("cat-file", "blob", obj)
            except GitError:
                return None
            data = proc.stdout
            if self._cached_bytes + len(data) <= _CONTENT_CACHE_BYTES:
                self._cache[obj] = data
                self._cached_bytes += len(data)
        return data if len(data) <= limit else None

    def read_text(self, path: str, limit: int = MAX_FILE_BYTES) -> str | None:
        return _decode_strict(self.read_bytes(path, limit))


def git_tree(repo: Path, rev: str, *, git: Git | None = None) -> TreeView:
    """Vista del commit ``rev`` de ``repo`` (``UnknownRevision`` si no existe)."""
    return GitTreeView(git if git is not None else Git(Path(repo)), rev, root=Path(repo))


# -- árbol de trabajo --------------------------------------------------------------


def _split_rel(path: object) -> list[str] | None:
    """Segmentos de una ruta relativa con ``/``; ``None`` si no es segura."""
    if not isinstance(path, str) or not path or "\0" in path or "\\" in path:
        return None
    if path.startswith("/"):
        return None
    segments = path.split("/")
    for seg in segments:
        if seg in ("", ".", "..") or seg.lower() == ".git":
            return None
        if _WINDOWS and (":" in seg or seg[-1] in ". "):
            return None  # flujos alternativos, unidades y nombres que NTFS recorta
    return segments


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


class WorktreeView:
    """Árbol de trabajo bajo ``root`` leído sin seguir enlaces."""

    def __init__(self, root: Path, *, git: Git | None = None) -> None:
        self.root: Path | None = Path(os.path.abspath(os.fspath(root)))
        self.sha: str | None = None
        self._base = os.fspath(self.root)
        self._real_root = os.path.normcase(os.path.realpath(self._base))
        self._git = git
        self._entries: Mapping[str, str] | None = None

    # -- entradas ---------------------------------------------------------------

    def entries(self) -> Mapping[str, str]:
        if self._entries is None:
            self._entries = MappingProxyType(self._compute_entries())
        return self._entries

    def _compute_entries(self) -> dict[str, str]:
        listed: dict[str, str | None] | None = None
        try:
            git = self._git if self._git is not None else Git(Path(self._base))
            index = git.index_modes()
            listed = dict(index)
            for path in git.ls_files(others=True):
                listed.setdefault(path, None)
        except (GitUnavailable, GitError):
            listed = None
        if listed is None:
            return self._walk()
        out: dict[str, str] = {}
        dir_cache: dict[str, bool] = {}  # prefijo -> es enlace
        for path, index_mode in listed.items():
            if path.endswith("/"):
                continue  # repositorio anidado sin registrar
            segments = path.split("/")
            link_prefix = self._link_prefix(segments, dir_cache)
            if link_prefix is not None:
                out[link_prefix] = SYMLINK_MODE  # git no pasa de un enlace
                continue
            mode = self._disk_mode(path, index_mode)
            if mode is not None:
                out[path] = mode
        return dict(sorted(out.items()))

    def _link_prefix(self, segments: list[str], cache: dict[str, bool]) -> str | None:
        """Primer prefijo propio de la ruta que es un enlace; ``None`` si no hay."""
        for i in range(1, len(segments)):
            prefix = "/".join(segments[:i])
            hit = cache.get(prefix)
            if hit is None:
                try:
                    hit = is_link(os.lstat(os.path.join(self._base, *segments[:i])))
                except OSError:
                    hit = False
                cache[prefix] = hit
            if hit:
                return prefix
        return None

    def _disk_mode(self, path: str, index_mode: str | None) -> str | None:
        try:
            st = os.lstat(os.path.join(self._base, *path.split("/")))
        except (OSError, ValueError):
            return None  # borrada del disco o nombre inválido en esta plataforma
        if is_link(st) or index_mode == SYMLINK_MODE:
            return SYMLINK_MODE  # core.symlinks=false deja el enlace como archivo regular
        if stat.S_ISDIR(st.st_mode):
            return GITLINK_MODE if index_mode == GITLINK_MODE else None
        if not stat.S_ISREG(st.st_mode):
            return None
        if _WINDOWS:  # sin bit de ejecución: manda el índice (core.filemode=false)
            return index_mode if is_regular_mode(index_mode) else _REGULAR
        return _EXECUTABLE if st.st_mode & stat.S_IXUSR else _REGULAR

    def _walk(self) -> dict[str, str]:
        """Recorrido sin git (respaldo): sin seguir enlaces y sin ``.git`` ni la caché local."""
        out: dict[str, str] = {}
        skip_top = {".git", _meta.CACHE_DIR}

        def visit(directory: str, prefix: str) -> None:
            try:
                with os.scandir(directory) as it:
                    items = sorted(it, key=lambda e: e.name)
            except OSError:
                return
            for entry in items:
                if entry.name.lower() == ".git" or (not prefix and entry.name in skip_top):
                    continue
                rel = f"{prefix}{entry.name}"
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if is_link(st):
                    out[rel] = SYMLINK_MODE
                elif stat.S_ISDIR(st.st_mode):
                    visit(entry.path, rel + "/")
                elif stat.S_ISREG(st.st_mode):
                    out[rel] = _EXECUTABLE if (not _WINDOWS and st.st_mode & stat.S_IXUSR) else _REGULAR

        visit(self._base, "")
        return dict(sorted(out.items()))

    # -- lectura ------------------------------------------------------------------

    def read_bytes(self, path: str, limit: int = MAX_FILE_BYTES) -> bytes | None:
        if not is_regular_mode(self.entries().get(path)):
            return None
        segments = _split_rel(path)
        if segments is None:
            return None
        return self._safe_read(segments, max(0, int(limit)))

    def read_text(self, path: str, limit: int = MAX_FILE_BYTES) -> str | None:
        return _decode_strict(self.read_bytes(path, limit))

    def _safe_read(self, segments: list[str], limit: int) -> bytes | None:
        current = self._base
        try:
            for seg in segments[:-1]:
                current = os.path.join(current, seg)
                st = os.lstat(current)
                if is_link(st) or not stat.S_ISDIR(st.st_mode):
                    return None
            full = os.path.join(current, segments[-1])
            before = os.lstat(full)
            if is_link(before) or not stat.S_ISREG(before.st_mode) or before.st_size > limit:
                return None
            if not _within(os.path.normcase(os.path.realpath(full)), self._real_root):
                return None
            flags = (
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_NOCTTY", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            fd = os.open(full, flags)
        except (OSError, ValueError):
            return None
        try:
            after = os.fstat(fd)
            if not stat.S_ISREG(after.st_mode):
                return None
            if before.st_ino and after.st_ino and (before.st_ino, before.st_dev) != (after.st_ino, after.st_dev):
                return None  # cambió entre lstat y open
            chunks: list[bytes] = []
            total = 0
            while total <= limit:
                chunk = os.read(fd, min(1 << 20, limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if total > limit:
                return None
            return b"".join(chunks)
        except OSError:
            return None
        finally:
            os.close(fd)


def worktree(root: Path, *, git: Git | None = None) -> TreeView:
    """Vista del árbol de trabajo bajo ``root`` (modo ``tree``)."""
    return WorktreeView(root, git=git)
