# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Registro local de eventos en ``.aucove/log/events.jsonl`` (01 §11).

- Una línea JSON por evento, escrita con una sola llamada ``write`` en modo append.
- Los campos son una lista cerrada (REQ-ARCH-95): ``v``, ``ts`` y ``core`` los pone
  ``emit``; el resto (``FIELDS``) se valida uno a uno y lo que no cumple se descarta. Una
  clave desconocida nunca llega al registro. ``session`` que no sea ya un resumen de 12
  hex se resume con SHA-256, para que un ``session_id`` en claro no quede escrito.
- Rota a ``events.1.jsonl`` antes de pasar de 1 MiB y conserva hasta ``events.3.jsonl``
  (REQ-ARCH-91). ``<PREFIJO>LOG=off`` lo apaga; ``info`` y ``debug`` escriben lo mismo en
  H2, porque la lista cerrada no tiene campo para los tiempos por fase (REQ-ARCH-94).
- ``emit`` nunca falla: cualquier error se ignora (REQ-ARCH-92). No hay telemetría.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from .. import _meta
from ..localstore.store import Store, ensure_root

__all__ = [
    "AUTO_FIELDS",
    "FIELDS",
    "KINDS",
    "LOG_FILE",
    "LOG_KEEP",
    "LOG_MAX_BYTES",
    "PATH_CLASSES",
    "RESULTS",
    "emit",
    "event_line",
    "log_dir",
]

EVENT_VERSION = 1
LOG_FILE = "events.jsonl"
LOG_MAX_BYTES = 2**20  # 1 MiB (REQ-ARCH-91)
LOG_KEEP = 3  # events.1.jsonl .. events.3.jsonl

KINDS = frozenset({"op", "hook", "cache", "net", "compat", "audit", "mcp"})
RESULTS = frozenset({"ok", "error", "allow", "deny", "skip"})
PATH_CLASSES = frozenset({"lead", "section", "editor", "append", "task", "generated", "local", "outside"})

# Orden de las claves en la línea; AUTO_FIELDS los pone emit y no se aceptan del llamador.
AUTO_FIELDS: tuple[str, ...] = ("v", "ts", "core")
_CALLER_FIELDS: tuple[str, ...] = (
    "kind", "name", "result", "code", "tool", "path", "path_class", "task", "session", "net", "ms",
)
FIELDS: frozenset[str] = frozenset(AUTO_FIELDS + _CALLER_FIELDS)

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:-]{0,63}")
_TOOL = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}")
_CODE = re.compile(r"[EW]_[A-Z0-9]+(?:_[A-Z0-9]+)*")
_SESSION = re.compile(r"[0-9a-f]{12}")
_HOST = re.compile(r"(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}))*")
_DRIVE = re.compile(r"[A-Za-z]:")
_MAX_PATH = 1024


def _now() -> datetime:
    return datetime.now(timezone.utc)


_Check = Callable[[object], "object | None"]


def _int_in(value: object, low: int, high: int | None = None) -> bool:
    """``True`` si ``value`` es un entero (no booleano) entre ``low`` y ``high``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value >= low and (high is None or value <= high)


def _enum(allowed: frozenset[str]) -> _Check:
    def check(value: object) -> object | None:
        return value if isinstance(value, str) and value in allowed else None

    return check


def _pattern(regex: re.Pattern[str]) -> _Check:
    def check(value: object) -> object | None:
        return value if isinstance(value, str) and regex.fullmatch(value) else None

    return check


def _path(value: object) -> object | None:
    """Ruta relativa a la raíz con ``/``; nunca absoluta (revela el usuario del sistema)."""
    if not isinstance(value, str) or not value or len(value) > _MAX_PATH:
        return None
    if any(ord(c) < 0x20 or c == "\x7f" for c in value) or "\\" in value:
        return None
    if value.startswith(("/", "~")) or _DRIVE.match(value):
        return None
    if any(seg in ("", ".", "..") for seg in value.split("/")):
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:  # sustitutos sueltos de un nombre que no es UTF-8
        return None
    return value


def _task(value: object) -> object | None:
    return value if _int_in(value, 1) else None


def _session(value: object) -> object | None:
    """Primeros 12 hex de SHA-256 del ``session_id``; se resume aquí si llega en claro."""
    if not isinstance(value, str) or not value:
        return None
    if _SESSION.fullmatch(value):
        return value
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def _net(value: object) -> object | None:
    """``{"host": ..., "status": ...}``: solo host y código HTTP, nunca URL ni cabeceras."""
    host: object
    status: object = None
    if isinstance(value, str):
        host = value
    elif isinstance(value, Mapping):
        if not set(value) <= {"host", "status"}:
            return None
        host, status = value.get("host"), value.get("status")
    elif isinstance(value, tuple) and len(value) == 2:
        host, status = value
    else:
        return None
    if not isinstance(host, str) or not _HOST.fullmatch(host):
        return None
    if status is not None and not _int_in(status, 100, 599):
        return None
    return {"host": host.lower(), "status": status}


def _ms(value: object) -> object | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return int(round(value))


_VALIDATORS: dict[str, _Check] = {
    "kind": _enum(KINDS),
    "name": _pattern(_NAME),
    "result": _enum(RESULTS),
    "code": _pattern(_CODE),
    "tool": _pattern(_TOOL),
    "path": _path,
    "path_class": _enum(PATH_CLASSES),
    "task": _task,
    "session": _session,
    "net": _net,
    "ms": _ms,
}


def event_line(**fields: object) -> bytes | None:
    """Línea JSON (con ``\\n``) de un evento; ``None`` si ``kind`` falta o no es válido.

    Solo pasan los campos de ``FIELDS`` que cumplen su forma; el resto se descarta.
    """
    record: dict[str, object] = {
        "v": EVENT_VERSION,
        "ts": _now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        "core": _meta.__version__,
    }
    for name in _CALLER_FIELDS:
        if name not in fields:
            continue
        value = _VALIDATORS[name](fields[name])
        if value is not None:
            record[name] = value
    if "kind" not in record:
        return None
    text = json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    try:
        return text.encode("utf-8") + b"\n"
    except UnicodeEncodeError:
        return None


def log_dir(store_root: Path) -> Path:
    """Carpeta ``log/`` de la caché: ``store_root`` es ``.aucove`` o la raíz del proyecto."""
    root = Path(store_root)
    base = root if root.name == _meta.CACHE_DIR else Store(root).root
    return base / "log"


def _level() -> str:
    value = os.environ.get(_meta.ENV_PREFIX + "LOG", "info").strip().lower()
    return value if value in ("off", "info", "debug") else "info"


def _rotate(directory: Path) -> None:
    """``events.jsonl`` -> ``events.1.jsonl`` -> ... -> ``events.3.jsonl`` (se borra el más viejo)."""
    for i in range(LOG_KEEP, 0, -1):
        src = directory / (LOG_FILE if i == 1 else f"events.{i - 1}.jsonl")
        try:
            os.replace(src, directory / f"events.{i}.jsonl")
        except FileNotFoundError:
            continue
        except OSError:
            return  # otro proceso lo tiene abierto (Windows): se rota en el próximo evento


def _append(directory: Path, line: bytes) -> None:
    target = directory / LOG_FILE
    try:
        size = os.stat(target).st_size
    except FileNotFoundError:
        size = 0
    if size and size + len(line) > LOG_MAX_BYTES:
        _rotate(directory)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(target, flags, 0o600)
    try:
        os.write(fd, line)  # una sola llamada (01 §11)
    finally:
        os.close(fd)


def emit(store_root: Path | None, **fields: object) -> None:
    """Registra un evento en ``<store_root>/log/events.jsonl``. Nunca lanza (REQ-ARCH-92).

    ``store_root`` es la carpeta ``.aucove`` (``Store.root``) o la raíz del proyecto;
    con ``None`` no hace nada. Los campos admitidos son los de ``FIELDS``.
    """
    try:
        if store_root is None or _level() == "off":
            return
        line = event_line(**fields)
        if line is None:
            return
        directory = log_dir(store_root)
        if not ensure_root(directory.parent):
            return
        directory.mkdir(exist_ok=True)
        _append(directory, line)
    except Exception:  # noqa: BLE001 - un fallo al registrar no cambia el resultado
        return
