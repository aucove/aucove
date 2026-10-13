# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Git por ``subprocess`` (01 §3.1 ``vcs``).

- El ejecutable se resuelve con ``which_outside``: una búsqueda en ``PATH`` que
  excluye el directorio actual y todo directorio del repositorio (REQ-ARCH-97).
  Solo se admiten entradas absolutas de ``PATH``; en Windows, solo ``.exe`` y
  ``.com`` (un ``.bat`` o ``.cmd`` pasaría por ``cmd.exe``).
- Todo subproceso corre con timeout, ``GIT_TERMINAL_PROMPT=0`` y, en Windows,
  ``NoDefaultCurrentDirectoryInExePath=1``; las lecturas, además, con
  ``GIT_OPTIONAL_LOCKS=0`` (REQ-ARCH-64). La entrada estándar es ``/dev/null``
  salvo que se pase ``input`` (el servidor MCP usa stdin para su protocolo).
- El timeout acota la llamada entera: al vencer se mata el árbol de procesos
  (en Windows, un Job Object; en POSIX, la sesión) y la espera a que se cierren
  las tuberías también tiene límite. Así no la alarga un descendiente que las
  herede: el git real tras el envoltorio ``Git\\cmd\\git.exe`` de Git for Windows,
  el ``sh`` de un alias ``!``, un remote helper...
- ``Git`` trabaja siempre sobre ``root``: se quitan del entorno las variables
  que apuntan a otro repositorio o índice (``GIT_DIR``, ``GIT_WORK_TREE``...).
- Sin red (``<PREFIJO>OFFLINE=1`` u ``offline=True``, p. ej. en hooks; REQ-ARCH-51
  y REQ-ARCH-63), git no abre ningún transporte: ``GIT_NO_LAZY_FETCH=1`` (git 2.44
  o posterior) y ``GIT_ALLOW_PROTOCOL`` vacío (ningún protocolo permitido, también
  con versiones anteriores).
- Clones parciales (``git clone --filter``): con red, git descarga del remoto
  promisor los objetos que falten, dentro del mismo timeout. Sin red, o si la
  descarga falla, ``show`` (y la lectura de ``vcs.tree.git_tree``) lanza
  ``ObjectUnavailable``, que no es lo mismo que «no existe»: quien lo recibe falla
  cerrado. ``ls_tree`` no lee blobs, así que nunca los descarga.
- Lecturas por id para la evidencia (09-ci REQ-CI-51; 11-trazabilidad §8.3):
  ``diff_tree_raw`` (rutas en bytes exactos), ``cat_file_check``, ``cat_file_batch`` y
  ``cat_file_objects`` (contenido crudo, sin ``--filters`` ni ``--textconv``: nunca se
  aplican los atributos del árbol), ``read_commit``, ``first_parent_chain`` e
  ``is_ancestor``.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
import types
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlsplit, urlunsplit

from .. import _meta

__all__ = [
    "BATCH_MAX_BYTES",
    "DEFAULT_TIMEOUT_S",
    "EMPTY_TREE",
    "CommitHeader",
    "Git",
    "GitError",
    "GitTimeout",
    "GitUnavailable",
    "MissingObject",
    "ObjectInfo",
    "ObjectUnavailable",
    "READ_COMMANDS",
    "RawChange",
    "UnknownRevision",
    "child_env",
    "is_offline",
    "is_oid",
    "parse_commit",
    "run_bounded",
    "which_outside",
]

_WINDOWS = os.name == "nt"

DEFAULT_TIMEOUT_S = 30.0

# Extensiones ejecutables admitidas en Windows, en el orden de PATHEXT.
_WINDOWS_EXTS: tuple[str, ...] = (".COM", ".EXE")

# Variables que fijan otro repositorio, índice u objetos: Git opera siempre sobre root.
_LOCATION_VARS = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
    }
)

# Variables que fija child_env (en mayúsculas, para compararlas sin distinguirlas).
_MANAGED_VARS = frozenset(
    {"GIT_TERMINAL_PROMPT", "GIT_OPTIONAL_LOCKS", "GCM_INTERACTIVE", "NODEFAULTCURRENTDIRECTORYINEXEPATH"}
)

# Sin red: git no descarga objetos ausentes de un clon parcial (GIT_NO_LAZY_FETCH,
# git >= 2.44) ni abre transporte alguno (GIT_ALLOW_PROTOCOL vacío no permite ningún
# protocolo; git lo respeta desde 2.12, así que cubre también la descarga perezosa
# de un git anterior a 2.44). REQ-ARCH-51 y REQ-ARCH-63.
_OFFLINE_VARS: Mapping[str, str] = {"GIT_NO_LAZY_FETCH": "1", "GIT_ALLOW_PROTOCOL": ""}

# Tras matar el árbol de procesos, espera máxima a que se cierren las tuberías.
_DRAIN_S = 2.0

# Subórdenes que solo leen: llevan GIT_OPTIONAL_LOCKS=0 (REQ-ARCH-64).
READ_COMMANDS = frozenset(
    {
        "cat-file",
        "check-attr",
        "check-ignore",
        "describe",
        "diff",
        "diff-tree",
        "for-each-ref",
        "log",
        "ls-files",
        "ls-remote",
        "ls-tree",
        "merge-base",
        "name-rev",
        "remote",
        "rev-list",
        "rev-parse",
        "show",
        "show-ref",
        "status",
        "symbolic-ref",
        "var",
        "version",
    }
)


class GitUnavailable(Exception):
    """No hay ``git`` fuera del repositorio, o no se pudo lanzar."""


class GitError(subprocess.CalledProcessError):
    """``git`` terminó con error (``check=True``).

    Es un ``CalledProcessError``: ``returncode``, ``cmd``, ``stdout`` y ``stderr``
    están disponibles, pero ``str()`` no incluye la salida (puede traer URL con
    credenciales).
    """


class GitTimeout(GitError):
    """``git`` superó el tiempo máximo y se terminó."""

    def __init__(self, cmd: Sequence[str], timeout_s: float) -> None:
        super().__init__(-1, list(cmd))
        self.timeout_s = timeout_s

    def __str__(self) -> str:
        return f"git: timeout after {self.timeout_s:g} s"


class UnknownRevision(LookupError):
    """La revisión pedida no existe en el repositorio."""


class ObjectUnavailable(LookupError):
    """La ruta existe en la revisión, pero su contenido no está en local.

    Pasa en un clon parcial (``git clone --filter``) cuando git no puede descargar
    el objeto: sin red o porque la descarga falló. No es «no existe»: quien lo
    recibe falla cerrado en lugar de buscar otra versión (06 REQ-PERM-4).
    """

    def __init__(self, rev: str, path: str) -> None:
        super().__init__(rev, path)
        self.rev = rev
        self.path = path

    def __str__(self) -> str:
        return f"git: object not available locally: {self.rev}:{self.path}"


class MissingObject(LookupError):
    """Un objeto pedido por su id no está en el repositorio (o no en local, en un clon parcial)."""

    def __init__(self, oid: str) -> None:
        super().__init__(oid)
        self.oid = oid

    def __str__(self) -> str:
        return f"git: missing object {self.oid}"


# -- objetos y cambios por id (evidencia; 09-ci REQ-CI-51) ----------------------------

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
"""Árbol vacío de SHA-1: git lo conoce aunque no esté guardado (primer registro, base nula)."""

BATCH_MAX_BYTES = 32 * 2**20  # contenido por llamada a cat-file --batch; un objeto mayor va solo

_OID_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")  # SHA-1 o SHA-256, completo y en minúsculas


def is_oid(value: object) -> bool:
    """``True`` si ``value`` es un id de objeto completo (40 o 64 hex en minúsculas)."""
    return isinstance(value, str) and _OID_RE.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class RawChange:
    """Un cambio de ``git diff-tree -r -z --raw --no-renames`` (formato «raw»).

    ``path`` son los bytes exactos que da git, sin decodificar ni normalizar. En un
    borrado, ``new_mode`` es ``000000`` y ``new_oid`` son ceros; en un alta, lo mismo
    con ``old_*``. ``status`` es la letra de git (``A``, ``D``, ``M``, ``T``...).
    """

    old_mode: str
    new_mode: str
    old_oid: str
    new_oid: str
    status: str
    path: bytes

    @property
    def deleted(self) -> bool:
        return self.status == "D"


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    """Id, tipo y tamaño de un objeto (``git cat-file --batch-check``)."""

    oid: str
    type: str
    size: int


@dataclass(frozen=True, slots=True)
class CommitHeader:
    """Cabecera de un objeto commit, con las identidades en los bytes guardados.

    ``author`` y ``committer`` son ``Nombre <correo>`` sin la fecha ni la zona; no se
    aplica ``.mailmap``. ``encoding`` es la cabecera ``encoding`` del commit, si la hay.
    """

    tree: str
    parents: tuple[str, ...]
    author: bytes
    committer: bytes
    encoding: str | None


def _ident(value: bytes) -> bytes:
    """``Nombre <correo>`` de ``Nombre <correo> <segundos> <zona>``."""
    end = value.rfind(b">")
    if end >= 0:
        return value[: end + 1]
    parts = value.rsplit(b" ", 2)  # sin '>': se quitan la fecha y la zona si están
    return parts[0] if len(parts) == 3 else value


def parse_commit(raw: bytes) -> CommitHeader:
    """Cabecera de un objeto commit crudo (``git cat-file --batch``).

    Lee hasta la primera línea vacía y se queda con la primera aparición de cada
    campo; las continuaciones (líneas con espacio inicial, p. ej. de ``gpgsig``) se
    ignoran. ``ValueError`` si falta ``tree``, ``author`` o ``committer``.
    """
    head = raw.split(b"\n\n", 1)[0]
    fields: dict[bytes, bytes] = {}
    parents: list[str] = []
    for line in head.split(b"\n"):
        if not line or line.startswith(b" "):
            continue
        key, _, value = line.partition(b" ")
        if key == b"parent":
            parents.append(_decode(value))
        else:
            fields.setdefault(key, value)
    tree = _decode(fields.get(b"tree", b""))
    if not is_oid(tree) or b"author" not in fields or b"committer" not in fields:
        raise ValueError("objeto commit sin tree, author o committer")
    encoding = fields.get(b"encoding")
    return CommitHeader(
        tree=tree,
        parents=tuple(parents),
        author=_ident(fields[b"author"]),
        committer=_ident(fields[b"committer"]),
        encoding=_decode(encoding) if encoding is not None else None,
    )


def _check_oids(oids: Sequence[str]) -> list[str]:
    """Ids sin repetir, en su orden; ``ValueError`` si alguno no es un id completo."""
    unique = list(dict.fromkeys(oids))
    for oid in unique:
        if not is_oid(oid):
            raise ValueError(f"id de objeto inválido: {oid!r}")
    return unique


# -- búsqueda del ejecutable (REQ-ARCH-97) -----------------------------------------


def _env_get(env: Mapping[str, str], name: str) -> str | None:
    """Variable de ``env``; en Windows, sin distinguir mayúsculas."""
    value = env.get(name)
    if value is not None or not _WINDOWS:
        return value
    upper = name.upper()
    for key, val in env.items():
        if key.upper() == upper:
            return val
    return None


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def _within(path: str, root: str) -> bool:
    """``True`` si ``path`` es ``root`` o está debajo (rutas ya normalizadas)."""
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:  # otra unidad en Windows
        return False


def _is_executable(path: str) -> bool:
    if not os.path.isfile(path):
        return False
    return _WINDOWS or os.access(path, os.X_OK)


def _candidate_names(exe: str, pathext: str | None) -> list[str]:
    if not _WINDOWS:
        return [exe]
    exts = [e.strip().upper() for e in (pathext or "").split(os.pathsep) if e.strip()]
    allowed = [e for e in exts if e in _WINDOWS_EXTS] or list(_WINDOWS_EXTS)
    if os.path.splitext(exe)[1].upper() in _WINDOWS_EXTS:
        return [exe]
    return [exe + ext.lower() for ext in allowed]


def which_outside(
    exe: str,
    root: Path,
    *,
    path: str | None = None,
    pathext: str | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> str | None:
    """Ruta absoluta de ``exe`` en ``PATH``, sin el directorio actual ni el repositorio.

    - Se ignoran las entradas vacías o relativas de ``PATH`` (resuelven contra el
      directorio actual), el propio directorio actual y todo directorio que esté
      dentro de ``root``; también un ejecutable cuya ruta real caiga en ``root``.
    - En Windows solo se prueban ``.exe`` y ``.com`` (en el orden de ``PATHEXT``).
    - ``exe`` con separadores de ruta se acepta solo si es absoluto y cumple lo mismo.
    - ``path``, ``pathext`` y ``cwd`` sustituyen a ``PATH``, ``PATHEXT`` y ``os.getcwd()``.

    Devuelve ``None`` si no lo encuentra.
    """
    if not exe or "\0" in exe:
        return None
    root_n = _norm(str(root))
    if cwd is None:
        try:
            cwd = os.getcwd()
        except OSError:  # directorio actual borrado
            cwd = None
    cwd_n = _norm(os.fspath(cwd)) if cwd is not None else None

    def excluded(directory: str) -> bool:
        d = _norm(directory)
        return d == cwd_n or _within(d, root_n)

    def acceptable(candidate: str) -> bool:
        if not _is_executable(candidate):
            return False
        real = os.path.realpath(candidate)  # un enlace no debe llevar al repositorio
        return not excluded(os.path.dirname(candidate)) and not excluded(os.path.dirname(real))

    seps = {os.sep, "/"} | ({os.altsep} if os.altsep else set())
    if any(s in exe for s in seps):
        if not os.path.isabs(exe):
            return None
        for name in _candidate_names(exe, pathext):
            if acceptable(name):
                return os.path.abspath(name)
        return None

    search = os.environ.get("PATH", "") if path is None else path
    if pathext is None and _WINDOWS:
        pathext = os.environ.get("PATHEXT")
    names = _candidate_names(exe, pathext)
    for raw in search.split(os.pathsep):
        entry = raw.strip().strip('"')
        if not entry or not os.path.isabs(entry) or excluded(entry):
            continue
        for name in names:
            candidate = os.path.join(entry, name)
            if acceptable(candidate):
                return os.path.abspath(candidate)
    return None


def is_offline(env: Mapping[str, str] | None = None) -> bool:
    """``True`` si ``<PREFIJO>OFFLINE`` vale exactamente ``'1'`` en ``env`` (REQ-ARCH-63)."""
    base = os.environ if env is None else env
    return _env_get(base, _meta.ENV_PREFIX + "OFFLINE") == "1"


def child_env(
    env: Mapping[str, str] | None = None,
    *,
    read: bool = False,
    offline: bool = False,
) -> dict[str, str]:
    """Entorno de un subproceso ``git`` (REQ-ARCH-64, REQ-ARCH-97).

    Sin red (``offline=True`` o ``<PREFIJO>OFFLINE=1`` en ``env``), git no abre
    ningún transporte ni descarga objetos de un clon parcial (REQ-ARCH-63).
    """
    base = dict(os.environ if env is None else env)
    no_network = offline or is_offline(base)
    # en Windows los nombres no distinguen mayúsculas: se quitan también las variantes
    drop = _LOCATION_VARS | _MANAGED_VARS | (frozenset(_OFFLINE_VARS) if no_network else frozenset())
    out = {k: v for k, v in base.items() if k.upper() not in drop}
    out["GIT_TERMINAL_PROMPT"] = "0"
    out["GCM_INTERACTIVE"] = "never"  # Git Credential Manager: sin diálogos
    if _WINDOWS:
        out["NoDefaultCurrentDirectoryInExePath"] = "1"
    if read:
        out["GIT_OPTIONAL_LOCKS"] = "0"
    if no_network:
        out.update(_OFFLINE_VARS)
    return out


# -- utilidades ----------------------------------------------------------------------


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "surrogateescape")


def _split_z(raw: bytes) -> list[str]:
    return [_decode(x) for x in raw.split(b"\0") if x]


def _valid_rev(rev: object) -> bool:
    return (
        isinstance(rev, str)
        and bool(rev)
        and not rev.startswith("-")
        and not any(c in rev for c in "\0\n\r")
    )


def _check_rev(rev: str) -> str:
    if not _valid_rev(rev):
        raise ValueError(f"revisión inválida: {rev!r}")
    return rev


def _check_path(path: str) -> str:
    if not isinstance(path, str) or not path or "\0" in path:
        raise ValueError(f"ruta inválida: {path!r}")
    return path


def _subcommand(args: Sequence[str]) -> str | None:
    """Primera palabra de ``args`` que no es una opción global de git."""
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in ("-c", "-C"):
            skip = True
            continue
        if arg.startswith("-"):
            continue
        return arg
    return None


def _strip_credentials(url: str) -> str:
    """Quita usuario y contraseña de una URL http(s); el resto queda igual."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if parts.scheme.lower() not in ("http", "https") or "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


# -- ejecución con el tiempo acotado (REQ-ARCH-64) -------------------------------------

_CREATE_SUSPENDED = 0x00000004
# PROCESS_TERMINATE | PROCESS_SET_QUOTA | PROCESS_SUSPEND_RESUME
_PROCESS_JOB_ACCESS = 0x0001 | 0x0100 | 0x0800

_win_api_cache: types.SimpleNamespace | None = None
_win_api_failed = False


def _win_api() -> types.SimpleNamespace | None:
    """Funciones de Windows para los Job Objects; ``None`` si no se pueden cargar."""
    global _win_api_cache, _win_api_failed
    if _win_api_cache is not None or _win_api_failed or not _WINDOWS:
        return _win_api_cache
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")
        k32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        k32.TerminateJobObject.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle.restype = wintypes.BOOL
        # NtResumeProcess reanuda todos los hilos de un proceso creado suspendido
        # (Popen no conserva el handle del hilo principal); lo usan psutil y Process Explorer.
        ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
        ntdll.NtResumeProcess.restype = ctypes.c_long
    except (ImportError, OSError, AttributeError):
        _win_api_failed = True
        return None
    _win_api_cache = types.SimpleNamespace(k32=k32, ntdll=ntdll)
    return _win_api_cache


class _ProcessTree:
    """Un proceso y todos sus descendientes, para matarlos juntos (REQ-ARCH-64).

    En Windows es un Job Object: el proceso nace suspendido, entra en el job y se
    reanuda, así que todo lo que lance queda dentro (el git real tras el envoltorio
    ``Git\\cmd\\git.exe``, el ``sh`` de un alias, los remote helpers...). En POSIX
    es una sesión nueva (``start_new_session``), que además deja a git sin terminal
    de control: nunca puede pedir credenciales por ella. Si el job no se puede crear
    o asignar, al vencer el plazo se mata solo el proceso; la espera sigue acotada.
    """

    def __init__(self) -> None:
        self._job: int | None = None
        api = _win_api()
        if api is not None:
            self._job = api.k32.CreateJobObjectW(None, None) or None

    def popen_kwargs(self) -> dict[str, Any]:
        if not _WINDOWS:
            return {"start_new_session": True}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if self._job is not None:
            flags |= _CREATE_SUSPENDED
        return {"creationflags": flags}

    def attach(self, proc: subprocess.Popen[bytes]) -> None:
        """Mete en el job el proceso recién creado (suspendido) y lo reanuda."""
        api = _win_api()
        if self._job is None or api is None:
            return
        handle = api.k32.OpenProcess(_PROCESS_JOB_ACCESS, False, proc.pid)
        if not handle:
            raise OSError("git: OpenProcess failed")
        try:
            if not api.k32.AssignProcessToJobObject(self._job, handle):
                self.close()  # sin job: al vencer el plazo se mata solo el proceso
            if api.ntdll.NtResumeProcess(handle) != 0:
                raise OSError("git: NtResumeProcess failed")
        finally:
            api.k32.CloseHandle(handle)

    def kill(self, proc: subprocess.Popen[bytes]) -> None:
        """Mata el proceso y sus descendientes; nunca lanza."""
        api = _win_api()
        if self._job is not None and api is not None:
            api.k32.TerminateJobObject(self._job, 1)
        elif not _WINDOWS:
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # la sesión nueva tiene pgid == pid
            except OSError:
                pass
        try:
            proc.kill()
        except OSError:
            pass

    def close(self) -> None:
        """Libera el job sin matar nada (no lleva KILL_ON_JOB_CLOSE)."""
        api = _win_api()
        if self._job is not None and api is not None:
            api.k32.CloseHandle(self._job)
        self._job = None


def _reader(stream: IO[bytes], sink: list[bytes]) -> threading.Thread:
    def target() -> None:
        try:
            sink.append(stream.read())
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    thread = threading.Thread(target=target, name="git-read", daemon=True)
    thread.start()
    return thread


def _writer(stream: IO[bytes], data: bytes) -> threading.Thread:
    def target() -> None:
        try:
            stream.write(data)
        except (OSError, ValueError):  # git terminó sin leerlo todo
            pass
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    thread = threading.Thread(target=target, name="git-write", daemon=True)
    thread.start()
    return thread


def _join(threads: Sequence[threading.Thread], deadline: float) -> bool:
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    return not any(thread.is_alive() for thread in threads)


def _wait(proc: subprocess.Popen[bytes], deadline: float) -> bool:
    try:
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return False
    return True


def run_bounded(
    cmd: Sequence[str],
    *,
    cwd: str | os.PathLike[str],
    env: Mapping[str, str],
    input: bytes | None = None,
    timeout: float,
) -> subprocess.CompletedProcess[bytes]:
    """Ejecuta ``cmd`` (ruta absoluta, sin shell) y espera como mucho ``timeout`` segundos.

    Sin ``input``, la entrada estándar es ``/dev/null``; con ``input``, se escribe en un
    hilo aparte, para que un proceso que no la lee tampoco alargue la espera. Al vencer
    el plazo se mata el árbol de procesos y se lanza ``subprocess.TimeoutExpired``; la
    espera a que se cierren las tuberías también está acotada (``_DRAIN_S``): si algo
    las sigue reteniendo, se abandonan. ``subprocess.run`` no sirve: en Windows, tras
    matar el proceso llama a ``communicate()`` sin timeout y espera a los nietos que
    heredaron las tuberías. Un fallo al arrancar es ``OSError``. Sirve también para
    otras herramientas que lanzan git (p. ej. gitleaks).
    """
    args = list(cmd)
    tree = _ProcessTree()
    try:
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL if input is None else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **tree.popen_kwargs(),
        )
        try:
            tree.attach(proc)
        except BaseException:
            tree.kill(proc)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
            _wait(proc, time.monotonic() + _DRAIN_S)
            raise
        deadline = time.monotonic() + timeout  # como subprocess.run: desde que el proceso corre
        assert proc.stdout is not None and proc.stderr is not None
        out: list[bytes] = []
        err: list[bytes] = []
        threads = [_reader(proc.stdout, out), _reader(proc.stderr, err)]
        if input is not None:
            assert proc.stdin is not None
            threads.append(_writer(proc.stdin, input))
        try:
            finished = _join(threads, deadline) and _wait(proc, deadline)
        except BaseException:  # p. ej. KeyboardInterrupt: no se deja el árbol vivo
            tree.kill(proc)
            raise
        if not finished:
            tree.kill(proc)
            drain = time.monotonic() + _DRAIN_S
            _join(threads, drain)
            _wait(proc, drain)
            raise subprocess.TimeoutExpired(args, timeout)
        return subprocess.CompletedProcess(args, proc.returncode, b"".join(out), b"".join(err))
    finally:
        tree.close()


# -- Git ---------------------------------------------------------------------------------


class Git:
    """Repositorio local en ``root``, con ``git`` resuelto fuera de él."""

    def __init__(
        self,
        root: Path,
        *,
        env: Mapping[str, str] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        offline: bool = False,
    ) -> None:
        """``offline=True`` (hooks, REQ-ARCH-51) o ``<PREFIJO>OFFLINE=1`` en ``env``: sin red."""
        self.root = Path(root)
        base = dict(os.environ if env is None else env)
        # un entorno parcial (p. ej. de pruebas) sin PATH busca con el PATH del proceso
        search = _env_get(base, "PATH")
        pathext = _env_get(base, "PATHEXT")
        exe = which_outside(
            "git",
            self.root,
            path=os.environ.get("PATH", "") if search is None else search,
            pathext=os.environ.get("PATHEXT") if pathext is None else pathext,
        )
        if exe is None:
            raise GitUnavailable("git")
        self.exe: str = exe
        self.timeout_s = float(timeout_s)
        self.offline: bool = bool(offline) or is_offline(base)
        self._env_write = child_env(base, read=False, offline=self.offline)
        self._env_read = child_env(base, read=True, offline=self.offline)
        self._env_probe = child_env(base, read=True, offline=True)  # diagnósticos: nunca usan la red

    def run(
        self,
        *args: str,
        check: bool = True,
        input: bytes | None = None,
        read: bool | None = None,
        timeout_s: float | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Ejecuta ``git <args>`` en ``root`` y devuelve la salida en bytes.

        ``read=None`` decide por la suborden (``READ_COMMANDS``); quien escribe con
        una de ellas pasa ``read=False``. Con ``check=True``, un código distinto de 0
        lanza ``GitError``. El timeout lanza siempre ``GitTimeout`` (con el árbol de
        procesos ya muerto y sin esperar más de ``_DRAIN_S`` extra) y un fallo al
        lanzar el proceso, ``GitUnavailable``. ``extra_env`` agrega variables al
        entorno, como la identidad del commit de reclamo (08-forja REQ-FORGE-67); no
        puede tocar las que fija ``child_env`` ni las de ubicación del repositorio.
        """
        if any("\0" in a for a in args):
            raise ValueError("argumento con NUL")
        is_read = _subcommand(args) in READ_COMMANDS if read is None else read
        env = self._env_read if is_read else self._env_write
        if extra_env:
            fixed = _LOCATION_VARS | _MANAGED_VARS | frozenset(_OFFLINE_VARS)
            if any(name.upper() in fixed for name in extra_env):
                raise ValueError("extra_env no puede cambiar las variables que fija child_env")
            env = {**env, **extra_env}
        return self._exec(args, env=env, check=check, input=input, timeout_s=timeout_s)

    def _exec(
        self,
        args: Sequence[str],
        *,
        env: Mapping[str, str],
        check: bool = False,
        input: bytes | None = None,
        timeout_s: float | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        timeout = self.timeout_s if timeout_s is None else float(timeout_s)
        cmd = [self.exe, *args]
        try:
            # ejecutable absoluto y sin shell; sin input, stdin es /dev/null
            proc = run_bounded(cmd, cwd=self.root, env=env, input=input, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise GitTimeout(cmd, timeout) from exc
        except OSError as exc:
            raise GitUnavailable("git") from exc
        if check and proc.returncode != 0:
            raise GitError(proc.returncode, cmd, proc.stdout, proc.stderr)
        return proc

    # -- lecturas -----------------------------------------------------------------

    def show(self, rev: str, path: str) -> bytes | None:
        """Bytes del blob ``<rev>:<path>`` sin filtros; ``None`` si no existe o no es un blob.

        ``ObjectUnavailable`` si la ruta existe en ``rev`` pero su contenido no está en
        local y git no lo descargó (clon parcial sin red): no es «no existe».
        """
        _check_rev(rev)
        _check_path(path)
        proc = self.run("cat-file", "blob", f"{rev}:{path}", check=False)
        if proc.returncode == 0:
            return proc.stdout
        if self._missing_locally(rev, path):
            raise ObjectUnavailable(rev, path)
        return None

    def _missing_locally(self, rev: str, path: str) -> bool:
        """``True`` si ``<rev>:<path>`` no se pudo leer porque faltan objetos en local.

        Pasa en un clon parcial (``--filter=blob:none``, ``--filter=tree:0``...). Estas
        comprobaciones nunca descargan nada, ni con red: corren con el entorno sin red y
        ``rev-list --missing=print`` no busca en el remoto promisor.
        """
        env = self._env_probe
        found = self._exec(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], env=env)
        commit = _decode(found.stdout).strip() if found.returncode == 0 else ""
        if not commit:
            return False  # la revisión no existe: la ruta tampoco
        listing = self._exec(["--literal-pathspecs", "ls-tree", "-z", "--full-tree", commit, "--", path], env=env)
        if listing.returncode != 0:
            return True  # el commit existe, pero su árbol no se pudo leer
        for record in listing.stdout.split(b"\0"):
            meta, _, name = record.partition(b"\t")
            fields = meta.split()
            if len(fields) < 3 or _decode(name) != path:
                continue
            if fields[1] != b"blob":
                return False  # directorio o gitlink: no es un blob
            probe = self._exec(["rev-list", "--objects", "--missing=print", "--no-walk", _decode(fields[2])], env=env)
            return probe.returncode != 0 or any(line.startswith(b"?") for line in probe.stdout.splitlines())
        return False  # la ruta no existe en el commit

    def rev_parse(self, rev: str) -> str | None:
        """Sha del objeto ``rev``; ``None`` si no existe (p. ej. ``HEAD^1`` en un commit raíz).

        No pela etiquetas: para el commit, ``rev + '^{commit}'``.
        """
        if not _valid_rev(rev):
            return None
        proc = self.run("rev-parse", "--verify", "--quiet", rev, check=False)
        if proc.returncode != 0:
            return None
        out = _decode(proc.stdout).strip()
        return out or None

    def ls_tree(self, rev: str, paths: Sequence[str] = ()) -> dict[str, tuple[str, str]]:
        """``ruta -> (modo, objeto)`` de ``git ls-tree -r -z --full-tree`` (modos tal cual).

        Sin ``-l``: no lee los blobs, así que en un clon parcial no los descarga.
        """
        return {p: (m, o) for p, (m, o, _s) in self._ls_tree(rev, paths, sizes=False).items()}

    def ls_tree_long(self, rev: str, paths: Sequence[str] = ()) -> dict[str, tuple[str, str, int | None]]:
        """Como ``ls_tree`` pero con el tamaño del blob.

        El tamaño es ``None`` en los gitlinks y en un blob que no está en local (clon
        parcial sin red: git escribe ``BAD``).
        """
        return self._ls_tree(rev, paths, sizes=True)

    def _ls_tree(self, rev: str, paths: Sequence[str], *, sizes: bool) -> dict[str, tuple[str, str, int | None]]:
        _check_rev(rev)
        for p in paths:
            _check_path(p)
        long = ["-l"] if sizes else []
        proc = self.run("--literal-pathspecs", "ls-tree", "-r", "-z", *long, "--full-tree", rev, "--", *paths)
        out: dict[str, tuple[str, str, int | None]] = {}
        for record in proc.stdout.split(b"\0"):
            if not record:
                continue
            meta, _, name = record.partition(b"\t")
            fields = meta.split()
            if len(fields) < (4 if sizes else 3) or not name:
                continue
            mode, _type, obj = (_decode(f) for f in fields[:3])
            size = _decode(fields[3]) if sizes else ""
            out[_decode(name)] = (mode, obj, int(size) if size.isdigit() else None)
        return out

    def diff_name_status(self, a: str, b: str | None) -> list[tuple[str, str, str | None]]:
        """``(status, path, previous_path)`` de ``git diff --name-status -M -z``.

        ``status`` es la letra de git tal cual (``A``, ``M``, ``D``, ``T``, ``R<n>``...).
        Con ``b=None`` compara ``a`` con el árbol de trabajo (solo archivos rastreados).
        """
        _check_rev(a)
        revs = [a] if b is None else [a, _check_rev(b)]
        proc = self.run(
            "diff", "--name-status", "-M", "-z", "--no-color", "--no-ext-diff", "--no-textconv",
            *revs, "--",
        )
        tokens = [_decode(t) for t in proc.stdout.split(b"\0")]
        out: list[tuple[str, str, str | None]] = []
        i = 0
        while i < len(tokens):
            status = tokens[i]
            if not status:
                i += 1
                continue
            if status[0] in "RC" and i + 2 < len(tokens):
                out.append((status, tokens[i + 2], tokens[i + 1]))
                i += 3
            elif i + 1 < len(tokens):
                out.append((status, tokens[i + 1], None))
                i += 2
            else:
                break
        return out

    def merge_base(self, a: str, b: str) -> str | None:
        """Mejor ancestro común; ``None`` si no hay (historias sin relación) o no existen."""
        _check_rev(a)
        _check_rev(b)
        proc = self.run("merge-base", a, b, check=False)
        if proc.returncode != 0:
            return None
        return _decode(proc.stdout).strip() or None

    def first_parent_revs(self, rev: str, path: str, max_count: int = 100) -> list[str]:
        """Commits de la historia de primer padre de ``rev`` que modifican ``path``.

        Solo mira los ``max_count`` commits más recientes de esa historia (incluido
        ``rev``), del más nuevo al más viejo (respaldo de 06 REQ-PERM-4).
        """
        _check_rev(rev)
        _check_path(path)
        if max_count < 1:
            return []
        limit = f"--max-count={int(max_count)}"
        window = self.run("rev-list", "--first-parent", limit, rev, check=False)
        if window.returncode != 0:
            return []
        inside = set(_decode(window.stdout).split())
        touching = self.run("--literal-pathspecs", "rev-list", "--first-parent", limit, rev, "--", path, check=False)
        if touching.returncode != 0:
            return []
        return [c for c in _decode(touching.stdout).split() if c in inside]

    def current_branch(self) -> str | None:
        """Rama de ``HEAD`` (sin ``refs/heads/``); ``None`` con ``HEAD`` separado."""
        proc = self.run("symbolic-ref", "-q", "HEAD", check=False)
        if proc.returncode != 0:
            return None
        ref = _decode(proc.stdout).strip()
        prefix = "refs/heads/"
        return ref[len(prefix):] if ref.startswith(prefix) and len(ref) > len(prefix) else None

    def remote_url(self, name: str = "origin") -> str | None:
        """URL del remoto ``name`` sin credenciales http(s); ``None`` si no existe."""
        if not _valid_rev(name):
            return None
        proc = self.run("remote", "get-url", name, check=False)
        if proc.returncode != 0:
            return None
        url = _decode(proc.stdout).strip()
        return _strip_credentials(url) if url else None

    def ls_files(self, *, others: bool = False) -> list[str]:
        """Archivos rastreados; con ``others=True``, solo los no rastreados y no ignorados.

        Sin duplicados (un conflicto lista la ruta una vez) y en el orden de git.
        """
        args = ["ls-files", "-z"]
        args += ["--others", "--exclude-standard"] if others else ["--cached"]
        proc = self.run(*args)
        return list(dict.fromkeys(_split_z(proc.stdout)))

    def index_modes(self) -> dict[str, str]:
        """``ruta -> modo`` del índice (``git ls-files -s -z``); en conflicto, la etapa menor."""
        proc = self.run("ls-files", "-s", "-z")
        out: dict[str, str] = {}
        for record in proc.stdout.split(b"\0"):
            if not record:
                continue
            meta, _, name = record.partition(b"\t")
            fields = meta.split()
            if not fields or not name:
                continue
            out.setdefault(_decode(name), _decode(fields[0]))
        return out

    # -- objetos por id (evidencia; 09-ci REQ-CI-51) ------------------------------

    def diff_tree_raw(self, old: str, new: str) -> list[RawChange]:
        """Cambios entre dos árboles o commits (``git diff-tree -r -z --raw --no-renames``).

        Sin ``-t`` (solo blobs y gitlinks), con los ids completos y en el orden de git
        (por ruta en bytes). No lee el contenido, no aplica atributos ni filtros y no
        detecta renombres: un renombre es un borrado más un alta. ``old`` puede ser
        ``EMPTY_TREE``. ``ValueError`` si la salida no tiene el formato esperado.
        """
        _check_rev(old)
        _check_rev(new)
        proc = self.run("diff-tree", "-r", "-z", "--raw", "--no-renames", "--no-abbrev", old, new)
        fields = proc.stdout.split(b"\0")
        out: list[RawChange] = []
        i = 0
        while i < len(fields):
            meta = fields[i]
            if not meta:
                i += 1
                continue
            parts = meta[1:].split(b" ") if meta.startswith(b":") else []
            if len(parts) != 5 or i + 1 >= len(fields) or not fields[i + 1]:
                raise ValueError(f"salida inesperada de git diff-tree: {meta[:80]!r}")
            old_mode, new_mode, old_oid, new_oid, status = (_decode(p) for p in parts)
            out.append(RawChange(old_mode, new_mode, old_oid, new_oid, status[:1], fields[i + 1]))
            i += 2
        return out

    def cat_file_check(self, oids: Sequence[str]) -> dict[str, ObjectInfo | None]:
        """Tipo y tamaño de cada objeto (``git cat-file --batch-check``); ``None`` si no está.

        Solo ids completos (``is_oid``); las claves van sin repetir y en el orden dado.
        """
        unique = _check_oids(oids)
        if not unique:
            return {}
        data = "".join(oid + "\n" for oid in unique).encode("ascii")
        lines = self.run("cat-file", "--batch-check", "--buffer", input=data).stdout.split(b"\n")
        out: dict[str, ObjectInfo | None] = {}
        for oid, line in zip(unique, lines):
            parts = line.split(b" ")
            if len(parts) == 3 and _decode(parts[0]) == oid and parts[2].isdigit():
                out[oid] = ObjectInfo(oid, _decode(parts[1]), int(parts[2]))
            elif len(parts) == 2 and _decode(parts[0]) == oid and parts[1] == b"missing":
                out[oid] = None
            else:
                raise ValueError(f"salida inesperada de git cat-file: {line[:80]!r}")
        if len(out) != len(unique):
            raise ValueError("git cat-file --batch-check no respondió por todos los objetos")
        return out

    def cat_file_batch(
        self, oids: Sequence[str], *, max_bytes: int = BATCH_MAX_BYTES
    ) -> Iterator[tuple[ObjectInfo, bytes]]:
        """Contenido crudo de cada objeto con ``git cat-file --batch``.

        Sin ``--filters`` ni ``--textconv``: son los bytes del blob, nunca los del
        disco ni los de un filtro del árbol (REQ-CI-51, REQ-TRACE-77). Da ``(info,
        bytes)`` sin repetir y en el orden dado. Pide los objetos en tandas de hasta
        ``max_bytes`` (un objeto mayor va solo), así que la memoria queda acotada por
        la tanda. ``MissingObject`` si alguno no está.
        """
        unique = _check_oids(oids)
        found: list[ObjectInfo] = []
        for oid, info in self.cat_file_check(unique).items():
            if info is None:  # antes de leer nada: nunca se entrega una lista a medias
                raise MissingObject(oid)
            found.append(info)
        batch: list[ObjectInfo] = []
        total = 0
        for info in found:
            if batch and total + info.size > max_bytes:
                yield from self._cat_batch(batch)
                batch, total = [], 0
            batch.append(info)
            total += info.size
        if batch:
            yield from self._cat_batch(batch)

    def _batch(self, oids: Sequence[str]) -> Iterator[tuple[str, ObjectInfo | None, bytes]]:
        """Una llamada a ``git cat-file --batch``: ``(oid, info o None si no está, bytes)``."""
        data = "".join(oid + "\n" for oid in oids).encode("ascii")
        raw = self.run("cat-file", "--batch", "--buffer", input=data).stdout
        pos = 0
        for oid in oids:
            nl = raw.find(b"\n", pos)
            header = raw[pos:nl].split(b" ") if nl >= 0 else []
            if len(header) == 2 and _decode(header[0]) == oid and header[1] == b"missing":
                yield oid, None, b""
                pos = nl + 1
                continue
            if len(header) != 3 or _decode(header[0]) != oid or not header[2].isdigit():
                raise ValueError(f"salida inesperada de git cat-file --batch para {oid}")
            start = nl + 1
            end = start + int(header[2])
            if raw[end : end + 1] != b"\n":
                raise ValueError(f"contenido incompleto de git cat-file --batch para {oid}")
            yield oid, ObjectInfo(oid, _decode(header[1]), end - start), raw[start:end]
            pos = end + 1

    def _cat_batch(self, infos: Sequence[ObjectInfo]) -> Iterator[tuple[ObjectInfo, bytes]]:
        for oid, info, data in self._batch([info.oid for info in infos]):
            if info is None:
                raise MissingObject(oid)
            yield info, data

    def cat_file_objects(self, oids: Sequence[str]) -> dict[str, tuple[ObjectInfo, bytes] | None]:
        """Objetos pequeños (commits, etiquetas) en una sola llamada a ``git cat-file --batch``.

        ``None`` si el objeto no está. Sin tandas: para blobs, ``cat_file_batch``.
        """
        unique = _check_oids(oids)
        if not unique:
            return {}
        return {oid: (info, data) if info is not None else None for oid, info, data in self._batch(unique)}

    def read_commit(self, oid: str) -> CommitHeader:
        """Cabecera del commit ``oid`` (id completo), leída con ``cat-file --batch``.

        ``MissingObject`` si no está y ``ValueError`` si el objeto no es un commit.
        """
        found = self.cat_file_objects([oid])[oid]
        if found is None:
            raise MissingObject(oid)
        info, raw = found
        if info.type != "commit":
            raise ValueError(f"{oid} no es un commit ({info.type})")
        return parse_commit(raw)

    def first_parent_chain(self, rev: str, *, max_count: int = 100, exclude: str | None = None) -> list[str]:
        """Commits de la historia de primer padre de ``rev`` (incluido), del más nuevo al más viejo.

        Como mucho ``max_count``. Con ``exclude``, sin los alcanzables desde él
        (``git rev-list --first-parent --max-count=<n> <rev> --not <exclude>``): si
        ``exclude`` es un ancestro de primer padre, la lista se corta justo antes.
        Una revisión que no existe es ``GitError``.
        """
        _check_rev(rev)
        if max_count < 1:
            return []
        args = ["rev-list", "--first-parent", f"--max-count={int(max_count)}", rev]
        if exclude is not None:
            args += ["--not", _check_rev(exclude)]
        return _decode(self.run(*args).stdout).split()

    def is_ancestor(self, ancestor: str, rev: str) -> bool:
        """``True`` si ``ancestor`` es ancestro de ``rev`` o el mismo (``git merge-base --is-ancestor``).

        Una revisión que no existe es ``GitError``.
        """
        args = ("merge-base", "--is-ancestor", _check_rev(ancestor), _check_rev(rev))
        proc = self.run(*args, check=False)
        if proc.returncode not in (0, 1):
            raise GitError(proc.returncode, [self.exe, *args], proc.stdout, proc.stderr)
        return proc.returncode == 0
