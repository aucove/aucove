# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Contexto de operación: lo que recibe cada ``run(ctx, **kwargs)`` (05 §2).

Todo acceso pesado es perezoso: git, la política de la base, la caché y la forja
se importan dentro de las funciones (REQ-ARCH-15, REQ-ARCH-17). La fábrica de la
forja elige la implementación con import perezoso; ``ops`` nunca importa
``forge.github`` ni ``forge.fake`` en el nivel superior (import-linter lo exime
solo aquí y una prueba AST lo vigila).

Aquí viven también los candados locales de 05 §6.4 (``local_locks``), que
``registry.invoke`` toma antes de ``run``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .. import _meta
from ..i18n import LANGS, Translator, resolve_lang, translator
from .result import OpError

if TYPE_CHECKING:
    import subprocess

    from ..config.base import BaseSnapshot
    from ..forge.base import ForgeAdapter, ForgeAdmin, HumanConfirmation
    from ..localstore.store import Store
    from ..vcs.git import Git

Surface = Literal["cli", "mcp", "hook", "ci"]


def utcnow() -> datetime:
    """Hora actual con zona UTC (``domain`` recibe el tiempo como argumento)."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Invocation:
    """Datos de la invocación que no dependen de la operación."""

    root: str | None
    lang: str | None
    json: bool
    dry_run: bool
    surface: Surface
    human_confirmed: bool = False  # solo cli, tras las dos fases de 06 §6 (REQ-ARCH-23)
    # solo cli: la persona confirmó attest-v1 en la terminal (11-trazabilidad REQ-TRACE-22, canal tty)
    attest_tty: bool = False
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    cwd: str = field(default_factory=os.getcwd)
    now: Callable[[], datetime] = utcnow
    # stdin y stdout son una terminal (fase 1 de 06 §6); None: se miran sys.stdin y sys.stdout.
    tty: bool | None = None
    # MCP: se activa con notifications/cancelled; las operaciones de varios pasos lo miran en el
    # borde de cada paso y nunca cortan una escritura a la forja a medias (REQ-OPS-55).
    cancel: threading.Event | None = None
    # solo cli: la fase 2 de require_human (06-permisos §6). Recibe (operación, objetivo, resumen),
    # pide teclear el reto y devuelve la HumanConfirmation, o lanza E_HUMAN_ONLY_NOT_CONFIRMED.
    confirm: Callable[[str, str, str], HumanConfirmation] | None = None
    # solo cli, tras la fase 2 (init): muestra el texto y pide teclear una frase exacta (la de
    # 06-permisos REQ-PERM-40 o la del bypass de REQ-PERM-19); True si la persona la tecleó.
    phrase: Callable[[str, str], bool] | None = None
    # solo cli, tras la fase 2 (init): pide un secreto sin eco y lo devuelve; es el token de
    # administración, que vive solo en memoria (10-seguridad REQ-SEC-30).
    secret: Callable[[str], str] | None = None
    # solo cli (init): una pregunta con valor por omisión, antes de la fase 2 (las licencias de
    # 11-trazabilidad §7.3). Recibe (pregunta, valor por omisión) y devuelve la respuesta.
    ask: Callable[[str, str], str] | None = None


def stdio_is_tty() -> bool:
    """``True`` si stdin y stdout del proceso son una terminal (06-permisos §6, fase 1)."""
    try:
        return bool(
            sys.stdin is not None and sys.stdin.isatty() and sys.stdout is not None and sys.stdout.isatty()
        )
    except (AttributeError, OSError, ValueError):  # flujo cerrado o reemplazado
        return False


@dataclass(frozen=True)
class Project:
    """Raíz de un proyecto y sus directorios de git (admite worktrees)."""

    root: Path
    git_dir: Path
    common_dir: Path


def _git_dirs(dot_git: Path) -> tuple[Path, Path] | None:
    """``(git_dir, common_dir)`` de un ``.git`` directorio o archivo; ``None`` si no es válido."""
    if dot_git.is_dir():
        return dot_git, _common_dir(dot_git)
    if not dot_git.is_file():
        return None
    try:
        first = dot_git.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    prefix = "gitdir:"
    if not first.startswith(prefix):
        return None
    target = Path(first[len(prefix):].strip())
    if not target.is_absolute():
        target = dot_git.parent / target
    target = Path(os.path.normpath(target))
    if not target.is_dir():
        return None
    return target, _common_dir(target)


def _common_dir(git_dir: Path) -> Path:
    marker = git_dir / "commondir"
    try:
        rel = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return git_dir
    if not rel:
        return git_dir
    common = Path(rel)
    if not common.is_absolute():
        common = git_dir / common
    return Path(os.path.normpath(common))


def resolve_project(start: str) -> Project | None:
    """Sube desde ``start`` hasta un directorio con ``project.yaml`` y ``.git`` (REQ-OPS-4).

    ``.git`` puede ser un directorio o un archivo ``gitdir:`` (worktree). Devuelve
    ``None`` si llega a la raíz del sistema de archivos sin encontrarlo.
    """
    here = Path(os.path.abspath(start))
    for candidate in (here, *here.parents):
        if not (candidate / "project.yaml").is_file():
            continue
        dirs = _git_dirs(candidate / ".git")
        if dirs is None:
            continue
        return Project(root=candidate, git_dir=dirs[0], common_dir=dirs[1])
    return None


def env_flag(env: Mapping[str, str], name: str) -> str | None:
    """Variable ``<PREFIJO><name>`` (``'OFFLINE'`` -> ``<PREFIJO>OFFLINE``; PREFIJO = ``_meta.ENV_PREFIX``)."""
    return env.get(_meta.ENV_PREFIX + name)


def testing(env: Mapping[str, str]) -> bool:
    """``True`` solo si ``<PREFIJO>TESTING`` vale exactamente ``'1'``."""
    return env_flag(env, "TESTING") == "1"


# ``forge.repo`` de project.schema.json: ``owner/name``, sin ``.`` ni ``..`` como nombre.
_REPO = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*/[A-Za-z0-9._-]{1,100}")


def valid_repo(value: object) -> bool:
    """``True`` si ``value`` tiene la forma de ``forge.repo`` (``owner/name``; 03-esquemas)."""
    return (isinstance(value, str) and len(value) <= 140 and _REPO.fullmatch(value) is not None
            and value.rsplit("/", 1)[1] not in (".", ".."))


def github_repository(env: Mapping[str, str]) -> str | None:
    """``GITHUB_REPOSITORY`` si tiene la forma de ``forge.repo``; si no, ``None``."""
    value = env.get("GITHUB_REPOSITORY") or ""
    return value if valid_repo(value) else None


@dataclass(frozen=True)
class ForgeTarget:
    """Forja con la que trabaja el adaptador: ``forge.type`` y ``forge.repo`` (03-esquemas)."""

    type: str | None
    repo: str | None


class OpContext:
    """Contexto perezoso de una operación. ``close()`` lo llaman ``registry.invoke`` y ``cli/ci.py``."""

    def __init__(self, inv: Invocation, *, forge: ForgeAdapter | None = None) -> None:
        self.inv = inv
        self._forge: ForgeAdapter | None = forge
        self.injected_forge = forge is not None  # pruebas: un adaptador dado, no el de la fábrica
        self._target: ForgeTarget | None = None  # la de use_forge_target (init)
        self._admins: list[ForgeAdmin] = []  # los que abrió forge_admin (solo init)
        self._fake_state: Path | None = None  # estado del FakeForge que hay que guardar
        self._project: Project | None = None
        self._project_done = False
        self._t: Translator | None = None
        self._resolving_lang = False
        self._git: Git | None = None
        self._base: BaseSnapshot | None = None
        self._store: Store | None = None
        self._closed = False
        self.confirmation: HumanConfirmation | None = None  # la de la fase 2, si la hubo

    @property
    def human_confirmed(self) -> bool:
        """La persona pasó las dos fases de ``require_human`` (01 REQ-ARCH-23)."""
        return self.confirmation is not None

    def confirm_human(self, op: str, target: str, summary: str) -> HumanConfirmation:
        """Fase 2 de ``require_human`` (06-permisos §6; 05 REQ-OPS-47).

        La operación H la llama tras sus lecturas y antes del primer efecto: ``summary`` es el
        plan y el reto es ``<op> <target>``. Solo la CLI la ofrece (``Invocation.confirm``); sin
        ella, con ``--dry-run`` o si la prueba no es de ``op``, ``E_HUMAN_ONLY_NOT_CONFIRMED``.
        """
        confirm = self.inv.confirm
        if self.inv.surface != "cli" or confirm is None or self.inv.dry_run:
            raise OpError("E_HUMAN_ONLY_NOT_CONFIRMED")
        proof = confirm(op, target, summary)
        if getattr(proof, "operation", None) != op or not isinstance(getattr(proof, "confirmed_at", None), datetime):
            raise OpError("E_HUMAN_ONLY_NOT_CONFIRMED")
        self.confirmation = proof
        return proof

    def type_phrase(self, summary: str, phrase: str) -> bool:
        """Confirmación tecleada adicional de una operación H, después de la fase 2 (``init``).

        ``True`` si la persona teclea ``phrase``. Sin la fase 2 hecha, fuera de la CLI o con
        ``--dry-run``, ``E_HUMAN_ONLY_NOT_CONFIRMED``.
        """
        ask = self.inv.phrase
        if self.inv.surface != "cli" or ask is None or self.inv.dry_run or self.confirmation is None:
            raise OpError("E_HUMAN_ONLY_NOT_CONFIRMED")
        return bool(ask(summary, phrase))

    def ask_text(self, prompt: str, default: str) -> str:
        """Respuesta a una pregunta de una operación H (``init``), o ``default`` si queda vacía.

        Solo la CLI pregunta (``Invocation.ask``); fuera de ella, sin la función o con
        ``--dry-run`` se usa ``default`` sin preguntar.
        """
        ask = self.inv.ask
        if self.inv.surface != "cli" or ask is None or self.inv.dry_run:
            return default
        answer = " ".join(str(ask(prompt, default)).split())
        return answer or default

    def ask_secret(self, prompt: str) -> str:
        """Secreto que la persona teclea sin eco tras la fase 2 (10-seguridad REQ-SEC-30).

        No se guarda en ningún lado: la operación lo usa y lo descarta. Sin la fase 2, fuera de
        la CLI o con ``--dry-run``, ``E_HUMAN_ONLY_NOT_CONFIRMED``.
        """
        ask = self.inv.secret
        if self.inv.surface != "cli" or ask is None or self.inv.dry_run or self.confirmation is None:
            raise OpError("E_HUMAN_ONLY_NOT_CONFIRMED")
        return ask(prompt)

    # -- proyecto y lenguaje -------------------------------------------------

    def start_dir(self) -> str:
        """La carpeta de la invocación: ``--root``/``project_root`` o el directorio actual (REQ-OPS-4)."""
        root = self.inv.root
        if not root:
            return self.inv.cwd
        return root if os.path.isabs(root) else os.path.join(self.inv.cwd, root)

    def project(self, *, required: bool = True) -> Project | None:
        """Proyecto de ``--root``/``project_root`` o del directorio actual (REQ-OPS-4)."""
        if not self._project_done:
            self._project = resolve_project(self.start_dir())
            self._project_done = True
        if self._project is None and required:
            raise OpError("E_CONFIG_NOT_A_PROJECT")
        return self._project

    def use_project(self, project: Project | None) -> None:
        """Fija el proyecto de esta invocación; la usa ``init --repo`` (05 §9.3; 00-marco D-73).

        Con ``None``, ninguna carpeta de arriba cuenta como proyecto: la carpeta vacía puede estar
        dentro de otro, y nada de esta invocación debe leer ni escribir en él (su git, su caché, su
        registro de eventos ni su política). Cuando ``init`` crea el repositorio, fija el nuevo.
        """
        self._project = project
        self._project_done = True
        self._git = None
        self._store = None
        self._base = None

    def _base_language(self) -> str | None:
        if self.project(required=False) is None:
            return None
        lang = getattr(self.base().project, "language", None)
        return lang if lang in LANGS else None

    def t(self) -> Translator:
        """Traductor del idioma resuelto: ``--lang``, variable de entorno, idioma de la base, ``en``."""
        if self._t is None:
            project_lang: str | None = None
            decided = self.inv.lang in LANGS or env_flag(self.inv.env, "LANG") in LANGS
            if not decided and not self._resolving_lang:
                self._resolving_lang = True
                try:
                    project_lang = self._base_language()
                except Exception:  # sin base legible, el idioma cae a 'en'
                    project_lang = None
                finally:
                    self._resolving_lang = False
            lang = resolve_lang(cli=self.inv.lang, env=self.inv.env, project=project_lang)
            self._t = translator(lang)
        return self._t

    def cancelled(self) -> bool:
        """``True`` si el cliente MCP canceló la llamada (05 REQ-OPS-55).

        Una operación de varios pasos lo consulta entre un paso y el siguiente (§6.3); las
        de solo lectura terminan igual y el servidor descarta su resultado.
        """
        return self.inv.cancel is not None and self.inv.cancel.is_set()

    # -- accesos perezosos ----------------------------------------------------

    def git(self) -> Git:
        """``vcs.git.Git`` sobre la raíz del proyecto."""
        if self._git is None:
            from ..vcs.git import Git, GitUnavailable

            project = self.project()
            assert project is not None
            try:
                self._git = Git(project.root, env=self.inv.env)
            except GitUnavailable as exc:
                raise OpError("E_CONFIG_GIT_UNAVAILABLE") from exc
        return self._git

    def base(self) -> BaseSnapshot:
        """Política de la rama base (``config.base.read_base``), cacheada.

        El nombre de la rama base lo resuelve ``read_base`` desde ``origin/HEAD`` o, sin esa
        ref, desde ``capabilities.json``; nunca del árbol de trabajo (07-hooks REQ-HOOK-41,
        06-permisos §2.1). El ``project.yaml`` del árbol no decide la política (D-08): aquí no
        se lee, y dentro de ``validate`` sus errores son hallazgos (03-esquemas §2.4).
        """
        if self._base is None:
            from ..config.base import read_base

            self._base = read_base(self.git(), t=self._t)
        return self._base

    def use_policy(self, snapshot: BaseSnapshot) -> None:
        """Toma ``snapshot`` como la política de la base de esta invocación.

        En CI y en ``validate --mode pr`` la política llega por ``--policy`` (leída con
        ``config.base.read_policy_dir``) y el directorio actual no es el proyecto (09-ci §3.3):
        así ``base()`` y la fábrica de la forja no necesitan un checkout.
        """
        self._base = snapshot

    def use_forge_target(self, target: ForgeTarget) -> None:
        """Fija la forja de esta invocación antes de construir el adaptador.

        La usa ``init``, que toma el repositorio de ``origin``: en un repo recién creado desde la
        plantilla, ``project.yaml`` todavía nombra el de la plantilla (05 §9.3).
        """
        if self._forge is not None:
            raise RuntimeError("el adaptador de la forja ya existe")
        self._target = target

    def forge_target(self) -> ForgeTarget:
        """``forge.type`` y ``forge.repo`` de la política de la base (REQ-ARCH-15).

        Sin proyecto ni ``use_policy`` (superficie ``ci`` sin ``--root``, o ``validate --mode
        pr`` desde ``runner.temp``), se usa ``GITHUB_REPOSITORY``: el job corre en GitHub y
        v0.1 solo admite ``forge.type: github`` (03-esquemas, D-04). Si tampoco está, se
        mantiene ``E_CONFIG_NOT_A_PROJECT``.
        """
        if self._target is not None:
            return self._target
        try:
            cfg = self.base().project
        except OpError as exc:
            if exc.code != "E_CONFIG_NOT_A_PROJECT":
                raise
            repo = github_repository(self.inv.env)
            if repo is None:
                raise
            return ForgeTarget("github", repo)
        return ForgeTarget(getattr(cfg, "forge_type", None), getattr(cfg, "forge_repo", None))

    def store(self) -> Store:
        """Caché local (``_meta.CACHE_DIR``) del checkout principal."""
        if self._store is None:
            from ..localstore.store import Store

            project = self.project()
            assert project is not None
            self._store = Store(project.root)
        return self._store

    def forge(self) -> ForgeAdapter:
        """Adaptador de la forja elegido por ``context_factory``, cacheado."""
        if self._forge is None:
            self._forge = context_factory(self)
        return self._forge

    def forge_admin(self, token: str) -> ForgeAdmin:
        """``ForgeAdmin`` con el token de administración que tecleó la persona (08 §2.2; D-62).

        Solo lo usa ``init`` (REQ-FORGE-3). Con el adaptador falso (pruebas), su vista de
        administración sobre el mismo estado; con GitHub, un adaptador aparte, sin caché en
        disco (REQ-FORGE-71). ``close()`` lo cierra.
        """
        forge = self.forge()
        make = getattr(forge, "admin", None)  # el falso trae su propia vista
        if callable(make):
            admin: ForgeAdmin = make(token)
        else:
            from ..forge.github import create_admin

            admin = create_admin(self, token)
        self._admins.append(admin)
        return admin

    def run_tool(
        self,
        name: str,
        args: Sequence[str],
        *,
        timeout: float,
        input: bytes | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Ejecuta la herramienta ``name`` (p. ej. ``gh``) sin shell y con ``timeout``.

        El ejecutable se busca solo en las entradas absolutas del ``PATH``, fuera del
        directorio actual y del proyecto (``vcs.git.which_outside``; S4, 00-marco D-52), y
        nunca pide nada por la terminal. ``extra_env`` se suma al entorno (p. ej. el token
        en ``GH_TOKEN``, que así no va en los argumentos). ``FileNotFoundError`` si no está;
        ``subprocess.TimeoutExpired`` si vence el plazo.
        """
        from ..vcs.git import run_bounded, which_outside

        env = dict(self.inv.env)
        project = self.project(required=False)
        root = project.root if project is not None else Path(self.inv.cwd)
        exe = which_outside(name, root, path=env.get("PATH", ""), pathext=env.get("PATHEXT"), cwd=self.inv.cwd)
        if exe is None:
            raise FileNotFoundError(name)
        env.update({"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1", "GH_PAGER": ""})
        env.update(extra_env or {})
        if os.name == "nt":
            env["NoDefaultCurrentDirectoryInExePath"] = "1"
        return run_bounded([exe, *args], cwd=root, env=env, input=input, timeout=timeout)

    def emit(self, **fields: object) -> None:
        """Evento en ``.aucove/log/events.jsonl`` del proyecto (01 §11). Nunca falla."""
        try:
            project = self.project(required=False)
            if project is None:
                return
            from ..obs.events import emit

            emit(project.root, **fields)
        except Exception:  # noqa: BLE001 - registrar nunca cambia el resultado (REQ-ARCH-92)
            return

    def close(self) -> None:
        """Guarda el FakeForge cargado del estado JSON (escritura atómica) y cierra el
        cliente HTTP del adaptador real. Idempotente."""
        if self._closed:
            return
        self._closed = True
        for admin in self._admins:
            closer = getattr(admin, "close", None)
            if callable(closer):
                with contextlib.suppress(Exception):
                    closer()
        if self._fake_state is not None and self._forge is not None:
            self._forge.save(self._fake_state)  # type: ignore[attr-defined]
            return
        closer = getattr(self._forge, "close", None)
        if callable(closer):
            closer()


def context_factory(ctx: OpContext) -> ForgeAdapter:
    """Elige el adaptador de la forja (REQ-ARCH-15, 08-forja REQ-FORGE-62).

    - ``<PREFIJO>TESTING=1`` y ``<PREFIJO>FORGE_ADAPTER=fake``: ``FakeForge`` cargado de
      ``<PREFIJO>FORGE_FAKE_STATE``; ``ctx.close()`` lo guarda al terminar.
    - Si no, ``forge.type`` de ``ctx.forge_target()`` (la base, la política de ``--policy`` o,
      sin proyecto, ``GITHUB_REPOSITORY``): con ``github``, ``forge.github.create(ctx)`` (H3),
      que obtiene el repositorio del mismo ``ctx.forge_target()``; si ese módulo no existe,
      o el tipo no se admite, ``E_FORGE_CAPABILITY_MISSING``.
    """
    env = ctx.inv.env
    if testing(env) and env_flag(env, "FORGE_ADAPTER") == "fake":
        from ..forge.fake import FakeForge

        state = env_flag(env, "FORGE_FAKE_STATE")
        if not state:
            raise OpError("E_FORGE_CAPABILITY_MISSING", details={"reason": "fake_state_missing"})
        path = Path(state)
        forge = FakeForge.load(path)
        ctx._fake_state = path
        return forge
    forge_type = ctx.forge_target().type
    if forge_type == "github":
        try:
            from ..forge.github import create
        except ImportError as exc:  # sin el adaptador REST (o reemplazado en pruebas)
            raise OpError("E_FORGE_CAPABILITY_MISSING", details={"forge": "github"}) from exc
        from ..forge.base import ForgeValidationError

        try:
            return create(ctx)  # type: ignore[no-any-return]
        except ForgeValidationError as exc:  # forge.repo ausente o inválido
            raise OpError("E_CONFIG_INVALID", details={"field": "forge.repo"}) from exc
    raise OpError("E_FORGE_CAPABILITY_MISSING", details={"forge": str(forge_type)})


# -- candados locales (05 §6.4) ---------------------------------------------------------

LOCK_WAIT_S = 10.0  # espera máxima para obtener los candados (REQ-OPS-29)
LOCK_STALE_S = 15 * 60  # un candado con más de 15 min se considera vencido
LOCK_POLL_S = 0.05
LOCK_RELEASE_S = 2.0  # cuánto se reintenta soltar un candado que otro está leyendo (Windows)
_LOCK_KEY = re.compile(r"repo|task-[1-9][0-9]{0,9}")
_STILL_ACTIVE = 259  # GetExitCodeProcess de un proceso vivo (Windows)
_ERROR_INVALID_PARAMETER = 87  # OpenProcess con un pid que no existe (Windows)


def lock_order(keys: Iterable[str]) -> list[str]:
    """Claves sin repetir en el orden fijo de REQ-OPS-29: ``repo`` y luego ``task-<n>`` ascendente."""
    unique = set(keys)
    for key in unique:
        if not _LOCK_KEY.fullmatch(key):
            raise ValueError(f"clave de candado inválida: {key!r}")
    return sorted(unique, key=lambda k: -1 if k == "repo" else int(k[len("task-"):]))


def _pid_alive(pid: int) -> bool:
    """``True`` si el proceso ``pid`` de este host existe o no se puede saber (conservador)."""
    if pid == os.getpid():
        return True
    if os.name == "nt":  # os.kill(pid, 0) terminaría el proceso en Windows
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() != _ERROR_INVALID_PARAMETER  # sin acceso: existe
        try:
            status = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(status)):
                return True
            return status.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # PermissionError: existe, pero es de otro usuario
        return True
    return True


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        moment = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return moment.astimezone(timezone.utc)


def _lock_is_stale(raw: bytes, mtime: float, now: datetime) -> bool:
    """Vencido: proceso muerto en este host o más de 15 min (REQ-OPS-29).

    Un contenido ilegible (por ejemplo, otro proceso lo está escribiendo) se juzga por la
    fecha de modificación del archivo.
    """
    try:
        info = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        info = None
    started = _parse_utc(info.get("started_at")) if isinstance(info, dict) else None
    if started is None:
        return time.time() - mtime > LOCK_STALE_S
    if (now - started).total_seconds() > LOCK_STALE_S:
        return True
    pid, host = info.get("pid"), info.get("host")  # type: ignore[union-attr]
    if isinstance(pid, int) and not isinstance(pid, bool) and 0 < pid < 2**32 and host == socket.gethostname():
        return not _pid_alive(pid)
    return False


def _busy(key: str, **details: object) -> OpError:
    return OpError("E_STATE_BUSY", details={"lock": key, **details})


def _clear_stale(path: Path, now: datetime) -> bool:
    """Borra el candado ``path`` si está vencido; ``True`` si ya no existe."""
    try:
        raw = path.read_bytes()
        mtime = os.stat(path).st_mtime
    except FileNotFoundError:
        return True  # lo acaban de soltar
    except OSError:
        return False
    if not _lock_is_stale(raw, mtime, now):
        return False
    try:
        if path.read_bytes() != raw:  # otro proceso lo reemplazó desde que se juzgó
            return False
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _take_lock(directory: Path, key: str, content: bytes, now: Callable[[], datetime], deadline: float) -> Path:
    """Crea ``<directory>/<key>.lock`` con ``O_CREAT|O_EXCL``; ``E_STATE_BUSY`` si vence el plazo."""
    path = directory / f"{key}.lock"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    while True:
        retry_now = False
        try:
            fd = os.open(path, flags, 0o644)
        except FileExistsError:
            retry_now = _clear_stale(path, now().astimezone(timezone.utc))
        except PermissionError:
            pass  # Windows: el candado anterior se está borrando
        except OSError as exc:
            raise _busy(key, reason="unavailable") from exc
        else:
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(content)
            except OSError as exc:
                with contextlib.suppress(OSError):
                    path.unlink()
                raise _busy(key, reason="unavailable") from exc
            return path
        if time.monotonic() >= deadline:
            raise _busy(key)
        if not retry_now:
            time.sleep(LOCK_POLL_S)


def _release_lock(path: Path, content: bytes) -> None:
    """Borra el candado ``path`` si sigue siendo el propio (``content``).

    En Windows, ``unlink`` falla con ``PermissionError`` mientras otro proceso tiene el
    archivo abierto, por ejemplo quien espera y lo lee en ``_clear_stale``. Ese lector lo
    suelta enseguida, así que se reintenta hasta ``LOCK_RELEASE_S``: si no, el candado
    quedaría con el pid de un proceso vivo y bloquearía las operaciones siguientes.
    """
    deadline = time.monotonic() + LOCK_RELEASE_S
    while True:
        try:
            if path.read_bytes() != content:
                return  # ya no es el propio: no se toca
            path.unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                return
            time.sleep(0.01)  # el lector suelta el archivo en microsegundos
        except OSError:
            return


@contextlib.contextmanager
def local_locks(ctx: OpContext, keys: Iterable[str], op: str) -> Iterator[None]:
    """Toma los candados ``keys`` de 05 §6.4 mientras dura el bloque (REQ-OPS-28, REQ-OPS-29).

    Cada candado es ``<caché>/locks/<clave>.lock`` en el checkout principal, compartido por
    los worktrees, la CLI y el servidor MCP; guarda ``{pid, host, op, started_at}``. Al
    salir se borra solo si sigue siendo el propio. Sin claves no toca el disco.
    """
    order = lock_order(keys)
    if not order:
        yield
        return
    from ..localstore.store import Store, ensure_root

    project = ctx.project()
    assert project is not None
    root = Store(project.root).root
    directory = root / "locks"
    try:
        if not ensure_root(root):
            raise OSError("caché no disponible")
        directory.mkdir(exist_ok=True)
    except OSError as exc:
        raise _busy(order[0], reason="unavailable") from exc
    deadline = time.monotonic() + LOCK_WAIT_S
    held: list[tuple[Path, bytes]] = []
    try:
        for key in order:
            started = ctx.inv.now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            record = {"pid": os.getpid(), "host": socket.gethostname(), "op": op, "started_at": started}
            content = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
            held.append((_take_lock(directory, key, content, ctx.inv.now, deadline), content))
        yield
    finally:
        for path, content in reversed(held):
            _release_lock(path, content)
