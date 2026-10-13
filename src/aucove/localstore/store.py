# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Caché local en ``.aucove/`` (01 §7.3).

- ``.aucove/`` (``_meta.CACHE_DIR``) vive en el checkout principal, el padre de
  ``commondir``, y lo comparten todos los worktrees (REQ-ARCH-61).
- Toda entrada de ``cache/`` lleva el sobre común ``{cache_schema, core, created_at,
  key, data}``. Una lectura que no decodifica o no cumple el sobre, o cuya clave no es
  la pedida, se trata como ausencia (REQ-ARCH-58).
- Toda escritura es atómica: temporal en el mismo directorio y ``os.replace``; en
  Windows, ante ``PermissionError``, hasta 3 reintentos con 10 ms de espera y después
  se abandona sin error, porque la caché es descartable (REQ-ARCH-57). La primera
  escritura crea ``.aucove/.gitignore`` con ``*``.
- Nada de lo que se guarda aquí es secreto (REQ-ARCH-60): la identidad va con la huella
  del token, nunca con el token.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import _meta
from ..domain.tasks import STATES

__all__ = [
    "CACHE_SCHEMA",
    "GITIGNORE",
    "IdentityCache",
    "MAX_ENTRY_BYTES",
    "Store",
    "TaskCache",
    "atomic_write",
    "ensure_root",
    "main_checkout",
]

_WINDOWS = os.name == "nt"

CACHE_SCHEMA = 1
GITIGNORE = b"*\n"
MAX_ENTRY_BYTES = 16 * 2**20  # una entrada mayor se trata como ausente
REPLACE_RETRIES = 3
REPLACE_WAIT_S = 0.01
GITIGNORE_CHECKS = 10  # relecturas de .gitignore cuando otra invocación lo está creando
GITIGNORE_WAIT_S = 0.01
_ENVELOPE_KEYS = frozenset({"cache_schema", "core", "created_at", "key", "data"})


@dataclass(frozen=True)
class TaskCache:
    """``cache/tasks/<n>.json`` (05 §9.8): lo escriben claim, switch, submit, status y context."""

    number: int
    claimant: str | None
    state: str
    sections: tuple[str, ...]
    local_branch: str | None
    fetched_at: str


@dataclass(frozen=True)
class IdentityCache:
    """``cache/identity.json`` (08 §6.2), ya comprobada contra la huella del token y el vencimiento."""

    login: str
    id: int | None
    expires_at: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(moment: datetime) -> datetime:
    """``moment`` en UTC; una fecha sin zona se toma como UTC."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _iso_z(moment: datetime) -> str:
    return _aware(moment).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: object) -> datetime | None:
    """Fecha ISO 8601 con zona (``Z`` o desfase); ``None`` si no lo es."""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# -- ubicación ---------------------------------------------------------------------


def _read_first_line(path: Path) -> str | None:
    try:
        with open(path, "rb") as fh:
            raw = fh.read(4096)
    except OSError:
        return None
    lines = raw.decode("utf-8", "replace").splitlines()
    return lines[0].strip() if lines else None


def _git_dir_of(dot_git: Path) -> Path | None:
    if dot_git.is_dir():
        return dot_git
    first = _read_first_line(dot_git)
    if not first or not first.startswith("gitdir:"):
        return None
    target = Path(first[len("gitdir:"):].strip())
    if not target.is_absolute():
        target = dot_git.parent / target
    target = Path(os.path.normpath(target))
    return target if target.is_dir() else None


def main_checkout(project_root: Path) -> Path:
    """Checkout principal de ``project_root``: el padre de ``commondir`` (REQ-ARCH-61).

    Si ``commondir`` no es una carpeta ``.git`` con checkout (repositorio bare, submódulo)
    o no se puede leer, se usa ``project_root``.
    """
    root = Path(os.path.abspath(os.fspath(project_root)))
    git_dir = _git_dir_of(root / ".git")
    if git_dir is None:
        return root
    common = git_dir
    rel = _read_first_line(git_dir / "commondir")
    if rel:
        candidate = Path(rel)
        if not candidate.is_absolute():
            candidate = git_dir / candidate
        common = Path(os.path.normpath(candidate))
    if common.name.lower() != ".git" or not common.parent.is_dir():
        return root
    return common.parent


# -- escritura atómica ---------------------------------------------------------------


def atomic_write(path: Path, payload: bytes) -> bool:
    """Escribe ``payload`` en ``path`` de forma atómica (REQ-ARCH-57).

    El temporal va en el mismo directorio. Si ``os.replace`` falla con
    ``PermissionError``, en Windows se reintenta hasta 3 veces con 10 ms de espera; si
    sigue fallando se abandona y devuelve ``False``. Otros ``OSError`` se propagan.
    """
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=os.fspath(path.parent))
    replaced = False
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        attempts = 1 + (REPLACE_RETRIES if _WINDOWS else 0)
        for attempt in range(attempts):
            try:
                os.replace(tmp, path)
            except PermissionError:
                if attempt + 1 < attempts:
                    time.sleep(REPLACE_WAIT_S)
                continue
            replaced = True
            return True
        return False
    finally:
        if not replaced:
            try:
                os.unlink(tmp)
            except OSError:
                pass  # el temporal queda para la limpieza de doctor


def ensure_root(root: Path) -> bool:
    """Crea ``root`` (``.aucove``) y su ``.gitignore`` con ``*``; ``False`` si no se pudo.

    No crea carpetas por encima de ``root``: si falta el checkout, no escribe nada.

    Varias invocaciones pueden crear el ``.gitignore`` a la vez. En Windows, abrirlo
    mientras otra lo reemplaza da ``PermissionError``, y el ``os.replace`` propio puede
    rendirse tras sus reintentos (REQ-ARCH-57) aunque la otra ya lo haya dejado bien. Por
    eso, ante un ``PermissionError`` o un reemplazo abandonado, se vuelve a leer hasta
    ``GITIGNORE_CHECKS`` veces con ``GITIGNORE_WAIT_S`` de espera: es ``True`` si el
    archivo contiene ``*`` y ``False`` solo si al final falta, difiere o sigue ocupado.
    Se escribe como mucho una vez por llamada. Si el archivo ya es correcto, basta una
    lectura (ruta caliente).
    """
    try:
        if not root.parent.is_dir():
            return False
        root.mkdir(exist_ok=True)
    except OSError:
        return False
    ignore = root / ".gitignore"
    wrote = False
    for check in range(1 + GITIGNORE_CHECKS):
        if check:
            time.sleep(GITIGNORE_WAIT_S)
        try:
            current: bytes | None = ignore.read_bytes()
        except FileNotFoundError:
            current = None
        except PermissionError:
            continue  # otra invocación lo está reemplazando
        except OSError:
            return False
        if current == GITIGNORE:
            return True
        if wrote:
            continue  # quizá otra invocación aún no termina su reemplazo
        wrote = True
        try:
            if atomic_write(ignore, GITIGNORE):
                return True
        except PermissionError:
            continue  # p. ej., el temporal no se pudo crear mientras otra escribía
        except OSError:
            return False
    return False


# -- Store ----------------------------------------------------------------------------


def _segments(rel: object) -> list[str]:
    if not isinstance(rel, str) or not rel or "\0" in rel or "\\" in rel or ":" in rel:
        raise ValueError(f"entrada de caché inválida: {rel!r}")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"entrada de caché inválida: {rel!r}")
    return parts


def _json_normal(value: Mapping[str, object]) -> object:
    """Forma JSON de ``value`` (tuplas -> listas), para comparar claves leídas."""
    return json.loads(json.dumps(dict(value), ensure_ascii=False, allow_nan=False))


class Store:
    """Caché ``.aucove/cache/`` del checkout principal de ``project_root``.

    ``rel`` es relativa a ``cache/`` y usa ``/`` (``tasks/12.json``, ``identity.json``).
    """

    def __init__(self, project_root: Path, *, now: Callable[[], datetime] | None = None) -> None:
        self.project_root = Path(os.path.abspath(os.fspath(project_root)))
        self.checkout = main_checkout(self.project_root)
        self.root = self.checkout / _meta.CACHE_DIR
        self.cache_dir = self.root / "cache"
        self._now = now or _utcnow

    def ensure(self) -> bool:
        """Crea ``.aucove/`` con su ``.gitignore``; ``False`` si no se pudo."""
        return ensure_root(self.root)

    def path(self, rel: str) -> Path:
        """Ruta de la entrada ``rel`` (``ValueError`` si ``rel`` no es segura)."""
        return self.cache_dir.joinpath(*_segments(rel))

    # -- lectura -----------------------------------------------------------------

    def read_envelope(self, rel: str, *, key: Mapping[str, object] | None = None) -> dict[str, Any] | None:
        """Sobre completo de ``rel`` si es válido y, con ``key``, si su clave es esa."""
        try:
            path = self.path(rel)
        except ValueError:
            return None
        try:
            st = os.stat(path)
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_ENTRY_BYTES:
                return None
            with open(path, "rb") as fh:
                raw = fh.read(MAX_ENTRY_BYTES + 1)
            if len(raw) > MAX_ENTRY_BYTES:
                return None
            env = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, RecursionError):
            return None  # UnicodeDecodeError y JSONDecodeError son ValueError
        if not isinstance(env, dict) or not _ENVELOPE_KEYS <= env.keys():
            return None
        if not _is_int(env["cache_schema"]) or env["cache_schema"] != CACHE_SCHEMA:
            return None
        if env["core"] != _meta.__version__ or _parse_time(env["created_at"]) is None:
            return None
        if not isinstance(env["key"], dict) or not isinstance(env["data"], dict):
            return None
        if key is not None:
            try:
                expected = _json_normal(key)
            except (TypeError, ValueError):
                return None
            if env["key"] != expected:
                return None
        return env

    def read(self, rel: str, *, key: Mapping[str, object] | None = None) -> dict[str, Any] | None:
        """``data`` de la entrada ``rel``; ``None`` si falta o no es válida (REQ-ARCH-58)."""
        env = self.read_envelope(rel, key=key)
        return env["data"] if env is not None else None

    # -- escritura ---------------------------------------------------------------

    def write(self, rel: str, data: Mapping[str, object], *, key: Mapping[str, object]) -> None:
        """Escribe ``rel`` con el sobre común, de forma atómica.

        ``ValueError``/``TypeError`` si ``rel`` no es segura o ``data``/``key`` no son JSON;
        un fallo del sistema de archivos se ignora (la caché es descartable).
        """
        path = self.path(rel)
        envelope = {
            "cache_schema": CACHE_SCHEMA,
            "core": _meta.__version__,
            "created_at": _iso_z(self._now()),
            "key": dict(key),
            "data": dict(data),
        }
        payload = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        try:
            if not self.ensure():
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, payload.encode("utf-8") + b"\n")
        except OSError:
            return

    def remove(self, rel: str) -> None:
        """Borra la entrada ``rel`` si existe; un fallo se ignora (la caché es descartable)."""
        try:
            self.path(rel).unlink(missing_ok=True)
        except (OSError, ValueError):
            return

    # -- entradas de H2 ------------------------------------------------------------

    def task(self, n: int) -> TaskCache | None:
        """``tasks/<n>.json`` como ``TaskCache``; ``None`` si falta o su forma no es válida."""
        if not _is_int(n) or n < 1:
            return None
        data = self.read(f"tasks/{n}.json")
        if data is None:
            return None
        number = data.get("number")
        claimant = data.get("claimant")
        state = data.get("state")
        sections = data.get("sections")
        local_branch = data.get("local_branch")
        fetched_at = data.get("fetched_at")
        if not _is_int(number) or number != n:
            return None
        if claimant is not None and (not isinstance(claimant, str) or not claimant):
            return None
        if not isinstance(state, str) or state not in STATES:
            return None
        if not isinstance(sections, list) or not all(isinstance(s, str) and s for s in sections):
            return None
        if local_branch is not None and (not isinstance(local_branch, str) or not local_branch):
            return None
        if not isinstance(fetched_at, str) or _parse_time(fetched_at) is None:
            return None
        return TaskCache(n, claimant, state, tuple(sections), local_branch, fetched_at)

    def write_task(self, task: TaskCache, *, key: Mapping[str, object] | None = None) -> None:
        """Escribe ``tasks/<n>.json`` (lo usan las pruebas y, en H3, claim y switch)."""
        data = {
            "number": task.number,
            "claimant": task.claimant,
            "state": task.state,
            "sections": list(task.sections),
            "local_branch": task.local_branch,
            "fetched_at": task.fetched_at,
        }
        self.write(f"tasks/{task.number}.json", data, key=dict(key or {}))

    def identity(self, *, token_fp: str | None) -> IdentityCache | None:
        """Identidad de ``identity.json`` si es de ``token_fp`` y no venció (08 REQ-FORGE-45).

        Con ``token_fp=None`` no se compara la huella (la ruta de hooks no tiene el token);
        el vencimiento se comprueba siempre.
        """
        env = self.read_envelope("identity.json")
        if env is None:
            return None
        data, key = env["data"], env["key"]
        if token_fp is not None:
            stored = key.get("token_fp", data.get("token_fp"))
            if stored != token_fp or data.get("token_fp", stored) != token_fp:
                return None
        login = data.get("login")
        ident = data.get("id")
        expires_at = data.get("expires_at")
        if not isinstance(login, str) or not login:
            return None
        if ident is not None and not _is_int(ident):
            return None
        expires = _parse_time(expires_at)
        if expires is None or expires <= _aware(self._now()):
            return None
        return IdentityCache(login, ident, expires_at)

    def write_identity(
        self,
        ident: IdentityCache,
        *,
        token_fp: str | None,
        forge: str = "github",
        extra: Mapping[str, object] | None = None,
    ) -> None:
        """Escribe ``identity.json`` con clave ``(forge, token_fp)`` (08 §6.2); nunca el token."""
        data: dict[str, object] = dict(extra or {})
        data.update(
            {"forge": forge, "token_fp": token_fp, "login": ident.login, "id": ident.id, "expires_at": ident.expires_at}
        )
        self.write("identity.json", data, key={"forge": forge, "token_fp": token_fp})
