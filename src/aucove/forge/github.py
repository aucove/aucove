# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Adaptador GitHub por REST (08-forja §3 a §8).

Transporte
----------
``http`` (``httpx2``: TLS con ``SSL_CERT_FILE`` o el almacén del sistema vía
``truststore``, y los proxies del entorno; REQ-ARCH-65) o ``gh`` (``gh api --include``,
obligatorio con el token ``proxy-injected`` de las sesiones web; REQ-FORGE-55). Toda
petición lleva ``Accept``, ``X-GitHub-Api-Version: 2026-03-10`` y ``User-Agent``; con
``http``, además, ``Authorization: Bearer`` (§4.1, REQ-FORGE-22). Timeouts de 5 s de
conexión y 20 s de lectura (REQ-FORGE-23).

Política (§7)
-------------
- Escrituras seriales en el proceso y hasta 4 lecturas en paralelo (REQ-FORGE-49).
- Reintentos solo de §7.3: 5xx, timeouts y conexión se reintentan si la petición es
  idempotente (0.5, 1 y 2 s con ±50 % de jitter; en CI, 5 veces). Un ``POST`` que no es
  idempotente no se repite: el método busca su efecto (§4.4 y §4.7, REQ-FORGE-35).
- Límites: ``retry-after``, ``x-ratelimit-remaining: 0`` y el límite secundario según la
  tabla de §7.3; un ``RateLimited`` bloquea el token en todo el proceso hasta
  ``reset_at`` (REQ-FORGE-51).
- ``GET`` 200 con ``ETag`` o ``Last-Modified`` se guarda y se revalida con
  ``If-None-Match`` o ``If-Modified-Since``; un 304 devuelve el cuerpo guardado
  (REQ-FORGE-52, ``cache.HttpCache``).
- Listas paginadas por ``Link: rel="next"``, con tope de 10 páginas de 100 (30 en
  ``list_pull_request_files``); pasarse del tope es ``ForgeValidationError``, nunca una
  lista truncada (REQ-FORGE-8).
- Ante un error: método, ruta, estado y ``X-GitHub-Request-Id`` en el log de depuración
  (``logging``, nunca la cabecera ``Authorization``; REQ-FORGE-24) y un evento ``net``
  con el host y el estado (01-arquitectura REQ-ARCH-90).
- ``<PREFIJO>OFFLINE=1`` falla sin abrir sockets (REQ-ARCH-63). En CLI y MCP las peticiones
  de una operación tienen 120 s en total (REQ-ARCH-62); en CI, no.

Sesión web (§8): con ``CLAUDE_CODE_REMOTE=true`` y ``write_mode="git"``,
``create_empty_commit``, ``create_ref`` y ``update_ref`` usan ``git`` sobre el clon local
(REQ-FORGE-67, REQ-FORGE-68), porque el proxy bloquea la API de datos de git.

``forge`` solo importa ``domain`` (01 §3.1): la caché en disco, el traductor de los
``hint``, el registro de eventos, ``gh`` y ``git`` llegan inyectados por ``create(ctx)``.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Literal, Protocol

from .. import _meta
from ..domain.text import same_login
from .auth import Token, ToolRunner, is_ci_token, make_token, resolve_token
from .base import (
    NULL_SHA,
    ORG_SETTINGS,
    REPO_SETTINGS,
    ActionsPolicy,
    AdminLevel,
    BaseNotDefault,
    BaseRules,
    Capabilities,
    ChangedFile,
    CheckRun,
    Checks,
    CollaboratorPermission,
    Comment,
    CommitInfo,
    Comparison,
    FileChange,
    FileContent,
    ForgeAuthError,
    ForgeError,
    ForgeNotFound,
    ForgePermissionError,
    ForgeUnavailable,
    ForgeUnsupported,
    ForgeValidationError,
    HumanConfirmation,
    Identity,
    Issue,
    Label,
    LabelEvent,
    MergeConflict,
    NetworkTimeout,
    NetworkTLSError,
    NetworkUnreachable,
    NoCommitsBetween,
    NoTokenError,
    PullRequest,
    PullRequestExists,
    RateLimited,
    Ref,
    RefActivity,
    RefAlreadyExists,
    RefConflict,
    RefNamespace,
    RepoRef,
    Review,
    RulesetInfo,
    WorkflowPermissions,
    WorkflowRun,
    rules_summary,
)
from .cache import CacheBackend, CachedCapabilities, ForgeCaches, HttpEntry, HttpKey, parse_time

if TYPE_CHECKING:
    import httpx2

__all__ = [
    "ACCEPT_JSON",
    "ACCEPT_RAW",
    "API_URL",
    "API_VERSION",
    "CI_LOGIN",
    "GitHubAdapter",
    "GitHubAdmin",
    "GitRunner",
    "create",
    "create_admin",
    "parse_repo",
]

_log = logging.getLogger(__name__)

API_URL = "https://api.github.com"
API_VERSION = "2026-03-10"  # REQ-FORGE-22
ACCEPT_JSON = "application/vnd.github+json"
ACCEPT_RAW = "application/vnd.github.raw+json"
USER_AGENT = f"{_meta.NAME}/{_meta.__version__}"
CONNECT_TIMEOUT_S = 5.0  # REQ-FORGE-23
READ_TIMEOUT_S = 20.0
GH_API_TIMEOUT_S = 30.0  # conexión, lectura y arranque de gh
OP_BUDGET_S = 120.0  # tiempo de red de una operación en CLI y MCP (REQ-ARCH-62)
PAGE_SIZE = 100
MAX_PAGES = 10  # REQ-FORGE-8
MAX_FILE_PAGES = 30  # list_pull_request_files: los 3000 archivos que da GitHub
MAX_COMMITS = 250  # compare y pulls/{n}/commits
MAX_COMPARE_FILES = 300
BIG_FILE = 1 << 20  # contents: más de 1 MB se repite con Accept raw (§4.2)
TEXT_BLOB_MAX = 1 << 20  # commit_files: texto hasta 1 MiB va en el árbol (§4.5)
CI_LOGIN = "github-actions[bot]"  # REQ-FORGE-43
CACHE_TTL = timedelta(hours=24)  # identidad y capacidades (REQ-FORGE-17, REQ-FORGE-45)
REVALIDATE_EVERY = timedelta(minutes=10)  # REQ-FORGE-20
CONFIRMATION_WINDOW = timedelta(seconds=120)  # REQ-FORGE-10
CLOCK_SKEW = timedelta(minutes=5)  # margen entre el reloj local y el de la forja (§4.7)
WRITE_SPACING_S = 1.0  # REQ-FORGE-50

_TRANSIENT = frozenset({500, 502, 503, 504})
_WAITS = (0.5, 1.0, 2.0)  # §7.3, interactivo
_WAITS_CI = (0.5, 1.0, 2.0, 4.0, 8.0)  # §7.3, CI: 5 intentos
_SECONDARY_WAITS_CI = (60.0, 120.0, 240.0)
_RETRY_AFTER_MAX_S = 10  # interactivo: se espera un retry-after de hasta 10 s, una vez
_PRIMARY_WAIT_MAX = timedelta(minutes=15)  # CI: se espera el reset si falta eso o menos
_SECONDARY_RESET = timedelta(seconds=60)

_FAILED = frozenset({"failure", "timed_out", "cancelled", "action_required"})
_PASSED = frozenset({"success", "neutral", "skipped"})
_STATE_REASONS = ("completed", "not_planned", "reopened", "duplicate")
_REVIEW_STATES = ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING")
_FILE_STATUSES = ("added", "modified", "removed", "renamed", "copied", "changed", "unchanged")
_ACTIVITY_TYPES = ("push", "force_push", "branch_creation", "branch_deletion", "pr_merge", "merge_queue_merge")
_REVIEW_OPERATIONS = {"APPROVE": "approve", "REQUEST_CHANGES": "request-changes"}  # 06 §6
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_OP_ID_RE = re.compile(r"[A-Za-z0-9._-]+")  # 00-marco §5.14
_LINK_RE = re.compile(r'<([^>]*)>\s*;\s*rel="([^"]*)"')
_GH_STATUS_RE = re.compile(rb"HTTP/[0-9.]+ ([0-9]{3})[^\r\n]*\r?\n")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

# Permiso de token fine-grained de cada método (08 §4.2): el hint de un 403 sin la
# cabecera X-Accepted-GitHub-Permissions.
_PERMISSIONS: Mapping[str, str] = {
    "capabilities": "metadata=read",
    "get_ref": "contents=read",
    "list_refs": "contents=read",
    "create_ref": "contents=write",
    "update_ref": "contents=write",
    "delete_ref": "contents=write",
    "list_ref_activity": "contents=read",
    "create_empty_commit": "contents=write",
    "commit_files": "contents=write",
    "merge_into": "contents=write",
    "get_commit": "contents=read",
    "get_file": "contents=read",
    "compare": "contents=read",
    "codeowners_errors": "contents=read",
    "get_issue": "issues=read",
    "list_issues": "issues=read",
    "create_issue": "issues=write",
    "update_issue": "issues=write",
    "add_labels": "issues=write",
    "remove_label": "issues=write",
    "ensure_labels": "issues=write",
    "list_issue_events": "issues=read",
    "set_assignees": "issues=write",
    "add_assignees": "issues=write",
    "remove_assignees": "issues=write",
    "list_comments": "issues=read",
    "create_comment": "issues=write",
    "update_comment": "issues=write",
    "get_pull_request": "pull_requests=read",
    "find_pull_request": "pull_requests=read",
    "list_pull_requests": "pull_requests=read",
    "list_pull_request_files": "pull_requests=read",
    "list_pull_request_commits": "pull_requests=read",
    "create_pull_request": "pull_requests=write",
    "update_pull_request": "pull_requests=write",
    "request_reviewers": "pull_requests=write",
    "list_reviews": "pull_requests=read",
    "submit_review": "pull_requests=write",
    "get_checks": "actions=read,statuses=read",
    "get_workflow_run": "actions=read",
    "successful_runs": "actions=read",
    "pulls_for_commit": "contents=read,pull_requests=read",
    "last_successful_run": "actions=read",
    "upsert_check_run": "checks=write",
    # ForgeAdmin (08 §2.2; 00-marco D-62)
    "repo_settings": "administration=read",
    "update_repo_settings": "administration=write",
    "actions_policy": "administration=read",
    "set_actions_policy": "administration=write",
    "workflow_permissions": "administration=read",
    "set_workflow_permissions": "administration=write",
    "retention_days": "administration=read",
    "set_retention_days": "administration=write",
    "collaborator_permission": "metadata=read",
    "add_collaborator": "administration=write",
    "outside_collaborators": "metadata=read",
    "org_settings": "organization_administration=read",
    "rulesets": "administration=read",
    "upsert_ruleset": "administration=write",
    "branch_rules": "metadata=read",
}

HintFn = Callable[..., "str | None"]
EventFn = Callable[..., None]


class GitRunner(Protocol):
    """Ejecuta ``git <args>`` en el clon local y devuelve ``(código, stdout, stderr)``.

    ``extra_env`` se suma al entorno del proceso. Cualquier falla al lanzar ``git`` o el
    timeout es una excepción: el adaptador la trata como un resultado ambiguo.
    """

    def __call__(
        self, args: Sequence[str], *, input: bytes | None = None, extra_env: Mapping[str, str] | None = None
    ) -> tuple[int, bytes, bytes]: ...


# ---------------------------------------------------------------------------
# Estado del proceso: bloqueos por límite y concurrencia (REQ-FORGE-49, REQ-FORGE-51)
# ---------------------------------------------------------------------------

_BLOCK_LOCK = threading.Lock()
_BLOCKED: dict[str, datetime] = {}  # huella del token -> reset_at
_WRITES = threading.Lock()
_READS = threading.BoundedSemaphore(4)


def _blocked_until(fp: str, now: datetime) -> datetime | None:
    with _BLOCK_LOCK:
        until = _BLOCKED.get(fp)
        if until is not None and until <= now:
            del _BLOCKED[fp]
            return None
        return until


def _block(fp: str, until: datetime) -> None:
    with _BLOCK_LOCK:
        current = _BLOCKED.get(fp)
        if current is None or until > current:
            _BLOCKED[fp] = until


# ---------------------------------------------------------------------------
# Utilidades puras
# ---------------------------------------------------------------------------


def _pct(text: str, safe: str = "") -> str:
    """Codificación por porcentaje de UTF-8 (sin ``urllib.parse``)."""
    out: list[str] = []
    for byte in text.encode("utf-8"):
        ch = chr(byte)
        out.append(ch if ch in _UNRESERVED or (byte < 0x80 and ch in safe) else f"%{byte:02X}")
    return "".join(out)


def _iso_z(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _query(params: Mapping[str, object]) -> str:
    parts: list[str] = []
    for name, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, datetime):
            text = _iso_z(value)
        elif isinstance(value, (list, tuple)):
            text = ",".join(_pct(str(v)) for v in value)
            parts.append(f"{_pct(name)}={text}")
            continue
        else:
            text = str(value)
        parts.append(f"{_pct(name)}={_pct(text)}")
    return "&".join(parts)


def _next_link(header: str | None) -> str | None:
    if not header:
        return None
    for url, rel in _LINK_RE.findall(header):
        if "next" in rel.split():
            return url
    return None


def _message(body: bytes) -> str:
    """``message`` del cuerpo JSON de un error de GitHub (sin el resto del cuerpo)."""
    try:
        data = json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, ValueError, RecursionError):
        return ""
    if isinstance(data, dict):
        msg = data.get("message")
        if isinstance(msg, str):
            return msg[:500]
    return ""


def _int_header(value: str | None) -> int | None:
    if value is None:
        return None
    text = value.strip()
    return int(text) if text.isdigit() and len(text) <= 12 else None


def _dt(value: object) -> datetime:
    moment = parse_time(value)
    if moment is None:
        raise ForgeValidationError(f"unexpected date in the forge response: {value!r}")
    return moment


def _opt_dt(value: object) -> datetime | None:
    return None if value is None else _dt(value)


def _str(value: object, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _login(user: object) -> str:
    """Login de un objeto usuario; ``ghost`` para una cuenta borrada (``null``)."""
    if isinstance(user, dict) and isinstance(user.get("login"), str):
        return str(user["login"])
    return "ghost"


def _opt_login(user: object) -> str | None:
    if isinstance(user, dict) and isinstance(user.get("login"), str):
        return str(user["login"])
    return None


def _op_marker(op_id: str) -> str:
    # Gramática de 00-marco §5.14; el adaptador lo agrega al final del cuerpo (08 §4.7).
    return f"<!-- {_meta.NAME}:op v1 id={op_id} -->"


def _with_marker(body: str, op_id: str) -> str:
    marker = _op_marker(op_id)
    if marker in body:
        return body
    if not body:
        return marker
    return body + ("\n" if body.endswith("\n") else "\n\n") + marker


def _check_op_id(op_id: object) -> str:
    if not isinstance(op_id, str) or _OP_ID_RE.fullmatch(op_id) is None:
        raise ValueError(f"invalid op_id (expected [A-Za-z0-9._-]+): {op_id!r}")
    return op_id


def _casefold_set(values: Sequence[str]) -> set[str]:
    return {v.strip().casefold() for v in values}


def _overall(runs: Sequence[CheckRun], combined_failure: bool = False) -> Literal["success", "failure", "pending", "none"]:
    # 08 §4.6
    if combined_failure or any(r.conclusion in _FAILED for r in runs):
        return "failure"
    if not runs:
        return "none"
    if all(r.status == "completed" and r.conclusion in _PASSED for r in runs):
        return "success"
    return "pending"


def _valid_path(path: object) -> bool:
    if not isinstance(path, str) or not path or "\0" in path or "\\" in path or path.startswith("/"):
        return False
    return not any(part in ("", ".", "..") for part in path.split("/"))


def _push_outcome(rc: int, out: str, ref: str) -> Literal["accepted", "rejected", "unknown"]:
    """Resultado de ``git push --porcelain`` para ``ref`` (``<flag>\\t<de>:<a>\\t<resumen>``)."""
    for line in out.splitlines():
        if len(line) < 3 or line[1] != "\t":
            continue
        parts = line.split("\t")
        if len(parts) < 2 or parts[1].rsplit(":", 1)[-1] != ref:
            continue
        flag = line[0]
        if flag == "!":
            return "rejected"
        if flag in " *+=-":
            return "accepted"
    return "accepted" if rc == 0 else "unknown"


def parse_repo(value: object) -> RepoRef:
    """``owner/name`` de ``forge.repo`` como ``RepoRef``; ``ForgeValidationError`` si no lo es."""
    if not isinstance(value, str) or value.count("/") != 1 or len(value) > 140:
        raise ForgeValidationError(f"invalid forge.repo: {value!r}")
    owner, name = value.split("/")
    if not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", owner) or not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", name):
        raise ForgeValidationError(f"invalid forge.repo: {value!r}")
    if name in (".", ".."):
        raise ForgeValidationError(f"invalid forge.repo: {value!r}")
    return RepoRef(owner, name)


# ---------------------------------------------------------------------------
# Transporte
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Resp:
    status: int
    headers: Mapping[str, str]  # nombres en minúsculas
    body: bytes
    cached: bool = False  # 304: el cuerpo es el de la caché HTTP


class _HttpError(Exception):
    """Respuesta HTTP que el método interpreta (404 -> None, 422 -> §4.4, ...)."""

    def __init__(self, status: int, message: str, headers: Mapping[str, str]) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.headers = headers


class _Transport(Protocol):
    name: Literal["http", "gh"]
    host: str

    def send(self, method: str, target: str, headers: Mapping[str, str], body: bytes | None) -> _Resp: ...

    def close(self) -> None: ...


def _ssl_context(env: Mapping[str, str]) -> Any:
    """TLS con ``SSL_CERT_FILE`` si está definido; si no, el almacén del sistema (REQ-ARCH-65)."""
    import ssl

    cafile = env.get("SSL_CERT_FILE")
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    import truststore

    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


def _network_error(exc: BaseException, what: str) -> ForgeError:
    """Clase de una falla de transporte (01 REQ-ARCH-65; 08 §2.1)."""
    import ssl

    import httpx2

    seen: set[int] = set()
    cause: BaseException | None = exc
    while cause is not None and id(cause) not in seen:  # el error TLS viene encadenado
        seen.add(id(cause))
        if isinstance(cause, ssl.SSLError):
            return NetworkTLSError(f"{what}: TLS failure ({type(cause).__name__})")
        cause = cause.__cause__ or cause.__context__
    name = type(exc).__name__
    if isinstance(exc, (httpx2.ConnectTimeout, httpx2.ConnectError, httpx2.PoolTimeout, httpx2.ProxyError)):
        return NetworkUnreachable(f"{what}: cannot connect ({name})")
    if isinstance(exc, httpx2.LocalProtocolError):
        return ForgeValidationError(f"{what}: invalid request ({name})")
    # Timeout de lectura o escritura, o la conexión se cortó con la petición ya enviada:
    # resultado ambiguo (§4.4, §4.7).
    return NetworkTimeout(f"{what}: no response ({name})")


class _HttpTransport:
    name: Literal["http", "gh"] = "http"

    def __init__(self, base_url: str, env: Mapping[str, str], mock: httpx2.BaseTransport | None) -> None:
        import httpx2

        self._base = base_url
        self.host = base_url.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0].lower()
        timeout = httpx2.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
        if mock is not None:  # pruebas: sin TLS ni proxies del entorno
            self._client = httpx2.Client(transport=mock, timeout=timeout, trust_env=False, follow_redirects=True)
            return
        try:
            verify = _ssl_context(env)
        except (OSError, ValueError) as exc:  # SSL_CERT_FILE ilegible
            raise NetworkTLSError(f"cannot build the TLS context ({type(exc).__name__})") from exc
        self._client = httpx2.Client(verify=verify, timeout=timeout, trust_env=True, follow_redirects=True)

    def send(self, method: str, target: str, headers: Mapping[str, str], body: bytes | None) -> _Resp:
        import httpx2

        path = target.split("?", 1)[0]
        try:
            r = self._client.request(method, self._base + target, headers=dict(headers), content=body)
        except httpx2.HTTPError as exc:
            raise _network_error(exc, f"{method} {path}") from None
        except OSError as exc:
            raise _network_error(exc, f"{method} {path}") from None
        merged: dict[str, str] = {}
        for name, value in r.headers.multi_items():
            key = name.lower()
            merged[key] = f"{merged[key]}, {value}" if key in merged else value
        return _Resp(r.status_code, merged, r.content)

    def close(self) -> None:
        self._client.close()


def _parse_gh_output(raw: bytes) -> _Resp | None:
    """Estado, cabeceras y cuerpo de la salida de ``gh api --include``; ``None`` si no la hay."""
    m = _GH_STATUS_RE.match(raw)
    if m is None:
        return None
    rest = raw[m.end():]
    empty = re.match(rb"\r?\n", rest)  # sin cabeceras: el cuerpo empieza tras la línea vacía
    sep = None if empty else re.search(rb"\r?\n\r?\n", rest)
    if empty:
        head, body = b"", rest[empty.end():]
    else:
        head, body = (rest[: sep.start()], rest[sep.end():]) if sep else (rest, b"")
    headers: dict[str, str] = {}
    for line in head.splitlines():
        if b":" not in line:
            continue
        name, value = line.split(b":", 1)
        key = name.strip().decode("latin-1").lower()
        text = value.strip().decode("latin-1")
        headers[key] = f"{headers[key]}, {text}" if key in headers else text
    return _Resp(int(m.group(1)), headers, body)


# stderr de gh cuando no llegó a abrir la conexión: la petición no salió.
_GH_NOT_SENT = ("no such host", "connection refused", "network is unreachable", "dial tcp", "could not resolve")


class _GhTransport:
    """``gh api --include`` (REQ-FORGE-55).

    La credencial va en ``GH_TOKEN`` del entorno de ``gh``, nunca en sus argumentos: así ``gh``
    usa exactamente el token que el adaptador clasificó (con ``proxy-injected``, el proxy pone
    la credencial real).
    """

    name: Literal["http", "gh"] = "gh"
    host = "api.github.com"

    def __init__(self, run: ToolRunner | None, token: Token) -> None:
        self._run = run
        self._token = token

    def send(self, method: str, target: str, headers: Mapping[str, str], body: bytes | None) -> _Resp:
        path = target.split("?", 1)[0]
        if self._run is None:
            raise ForgeUnsupported(f"{method} {path}: the gh transport needs gh")
        args = ["api", "--include", "--method", method]
        for name, value in headers.items():
            if name.lower() == "authorization":
                continue
            args += ["-H", f"{name}: {value}"]
        if body is not None:
            args += ["--input", "-"]
        args.append(target.lstrip("/"))
        try:
            proc = self._run(args, timeout=GH_API_TIMEOUT_S, input=body, extra_env={"GH_TOKEN": self._token.value})
        except FileNotFoundError as exc:
            raise ForgeUnsupported(f"{method} {path}: gh is not installed") from exc
        except subprocess.TimeoutExpired as exc:
            raise NetworkTimeout(f"{method} {path}: gh did not answer") from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise NetworkUnreachable(f"{method} {path}: gh failed to start") from exc
        parsed = _parse_gh_output(proc.stdout)
        if parsed is None:
            # Sin respuesta HTTP. Si gh no llegó a conectar, la petición no salió; si no, una
            # escritura pudo aplicarse y el resultado es ambiguo (§4.4 y §4.7).
            err = proc.stderr.decode("utf-8", "replace").lower()
            if method == "GET" or any(mark in err for mark in _GH_NOT_SENT):
                raise NetworkUnreachable(f"{method} {path}: gh api failed (exit {proc.returncode})")
            raise NetworkTimeout(f"{method} {path}: gh api gave no response (exit {proc.returncode})")
        return parsed

    def close(self) -> None:
        return


# ---------------------------------------------------------------------------
# Adaptador
# ---------------------------------------------------------------------------


class GitHubAdapter:
    """Implementación GitHub de ``ForgeAdapter`` (08 §4).

    Sin ``token``, el token se resuelve en la primera petición (§5.1): construir el
    adaptador nunca falla por credenciales. ``cache`` es el almacén ``.aucove/cache``;
    sin él (CI, o fuera de un proyecto), las cachés viven solo en memoria.
    """

    def __init__(
        self,
        repo: RepoRef,
        *,
        env: Mapping[str, str] | None = None,
        token: Token | str | None = None,
        base_branch: str | None = None,
        ci: bool | None = None,
        cache: CacheBackend | None = None,
        http_transport: httpx2.BaseTransport | None = None,
        run_gh: ToolRunner | None = None,
        run_git: GitRunner | None = None,
        hint: HintFn | None = None,
        on_event: EventFn | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        jitter: Callable[[], float] | None = None,
        budget_s: float | None = None,
    ) -> None:
        if not isinstance(repo, RepoRef):
            raise TypeError("repo must be a RepoRef")
        self.repo = repo
        self._env: dict[str, str] = dict(env or {})
        self._explicit = make_token(token, "explicit") if isinstance(token, str) else token
        self._token_value: Token | None = None
        self._token_lock = threading.Lock()
        self._base_branch = base_branch
        self._ci = (self._env.get("GITHUB_ACTIONS") == "true") if ci is None else bool(ci)
        self._caches = ForgeCaches(cache)
        self._mock = http_transport
        self._run_gh = run_gh
        self._run_git = run_git
        self._hint_fn = hint
        self._on_event = on_event
        self._now_fn = now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep or time.sleep
        self._jitter = jitter or random.random
        self._deadline = (time.monotonic() + budget_s) if budget_s else None
        self._web = self._env.get("CLAUDE_CODE_REMOTE") == "true"
        api = self._env.get("GITHUB_API_URL", "").strip().rstrip("/")
        use_env_api = self._env.get("GITHUB_ACTIONS") == "true" and api.startswith("https://") and " " not in api
        self._api_base = api if use_env_api else API_URL
        self._repo_path = f"{_pct(repo.owner)}/{_pct(repo.name)}"
        self._transport: _Transport | None = None
        self._transport_lock = threading.Lock()
        self._identity: Identity | None = None
        self._caps: Capabilities | None = None
        self._caps_from_cache = False
        self._redetected = False
        self._checks_api: bool | None = None
        self.notices: list[str] = []  # códigos W_* que la operación puede informar
        self.last_status: int | None = None  # CT-ETAG-01 (http_probe)
        self.last_rate_remaining: int | None = None
        self._server_date: datetime | None = None  # cabecera Date de la última respuesta
        self._repo_json: dict[str, Any] | None = None  # GET /repos de este proceso (lecturas de D-65)

    def __repr__(self) -> str:
        return f"GitHubAdapter({self.repo.owner}/{self.repo.name})"

    # -- configuración --------------------------------------------------------------------

    def _now(self) -> datetime:
        moment = self._now_fn()
        return moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)

    def _flag(self, name: str) -> str | None:
        return self._env.get(_meta.ENV_PREFIX + name)

    def _hint(self, key: str, **params: object) -> str | None:
        if self._hint_fn is None:
            return None
        try:
            return self._hint_fn(key, **params)
        except Exception:  # noqa: BLE001 - un hint nunca cambia el error
            return None

    def _notice(self, code: str) -> None:
        if code not in self.notices:
            self.notices.append(code)

    def _token(self) -> Token:
        if self._explicit is not None:
            return self._explicit
        with self._token_lock:
            if self._token_value is None:
                self._token_value = resolve_token(self._env, self._run_gh, hint=self._hint("no_token"))
            return self._token_value

    @property
    def write_mode(self) -> Literal["git", "api"]:
        """``git`` salvo que ``<PREFIJO>FORGE_WRITE_MODE`` lo fuerce (REQ-FORGE-56)."""
        forced = self._flag("FORGE_WRITE_MODE")
        if forced in ("git", "api") and (not self._web or self._flag("TESTING") == "1"):
            return forced  # type: ignore[return-value]
        return "git"

    def _transport_kind(self, token: Token) -> Literal["http", "gh"]:
        forced = self._flag("FORGE_TRANSPORT")
        if token.kind == "proxy":  # REQ-FORGE-55
            if forced == "http":
                self._notice("W_FORGE_CAPABILITY_UNKNOWN")
            return "gh"
        return forced if forced in ("http", "gh") else "http"  # type: ignore[return-value]

    def _get_transport(self, token: Token) -> _Transport:
        with self._transport_lock:
            if self._transport is None:
                if self._transport_kind(token) == "gh":
                    self._transport = _GhTransport(self._run_gh, token)
                else:
                    self._transport = _HttpTransport(self._api_base, self._env, self._mock)
            return self._transport

    def close(self) -> None:
        """Cierra el cliente HTTP. Idempotente."""
        with self._transport_lock:
            transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()

    # -- petición ---------------------------------------------------------------------------

    def _target(self, path: str, params: Mapping[str, object] | None) -> str:
        if path.startswith("/"):
            base = path
        else:
            base = f"/repos/{self._repo_path}" + (f"/{path}" if path else "")
        query = _query(params) if params else ""
        return base + (f"?{query}" if query else "")

    def _relative(self, url: str) -> str:
        if not url.startswith(self._api_base + "/"):
            raise ForgeValidationError("pagination link outside the forge API")
        return url[len(self._api_base):]

    def _check_budget(self, what: str) -> None:
        if self._deadline is not None and time.monotonic() > self._deadline:
            raise NetworkTimeout(f"{what}: the network time of the operation ran out")

    def _pause(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self._deadline is not None and time.monotonic() + seconds > self._deadline:
            raise NetworkTimeout("the network time of the operation ran out")
        self._sleep(seconds)

    def _event(self, label: str, host: str, status: int | None, started: float) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(
                kind="net",
                name=f"forge.{label}",
                result="error",
                net={"host": host, "status": status},
                ms=(time.monotonic() - started) * 1000,
            )
        except Exception:  # noqa: BLE001 - registrar nunca cambia el resultado (REQ-ARCH-92)
            return

    @staticmethod
    def _debug(method: str, target: str, status: int | None, headers: Mapping[str, str] | None) -> None:
        # REQ-FORGE-24: método, ruta, estado y X-GitHub-Request-Id; nunca cabeceras de autorización.
        request_id = (headers or {}).get("x-github-request-id")
        _log.debug("forge %s %s -> %s (request id %s)", method, target.split("?", 1)[0], status, request_id)

    def _invalidate(self) -> None:
        """Un 401 invalida la identidad y las capacidades (REQ-FORGE-19, REQ-FORGE-45)."""
        self._identity = None
        self._caps = None
        self._caches.drop_identity()
        self._caches.drop_capabilities()

    def _call(
        self,
        label: str,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        body: object = None,
        accept: str = ACCEPT_JSON,
        idempotent: bool | None = None,
        target: str | None = None,
    ) -> _Resp:
        """Una petición con la política de §7; 2xx (o 304 de la caché) o una excepción.

        Lanza ``_HttpError`` para los estados que interpreta el método (3xx, 4xx salvo 401
        y límites), ``ForgeAuthError`` (401), ``RateLimited``, ``ForgeUnavailable`` (5xx
        tras los reintentos) o un error de red.
        """
        if self._flag("OFFLINE") == "1":  # REQ-ARCH-63: sin abrir sockets
            raise NetworkUnreachable(f"{method} {path}: offline")
        if target is None:
            target = self._target(path, params)
        if idempotent is None:
            idempotent = method != "POST"
        payload = None if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        token = self._token()
        transport = self._get_transport(token)
        key: HttpKey | None = (self._api_base + target, accept, API_VERSION, token.fp) if method == "GET" else None
        cached = self._caches.http.get(key) if key is not None else None
        headers = {"Accept": accept, "X-GitHub-Api-Version": API_VERSION, "User-Agent": USER_AGENT}
        if transport.name == "http":
            headers["Authorization"] = f"Bearer {token.value}"
        if payload is not None:
            headers["Content-Type"] = "application/json"
        if cached is not None:
            if cached.etag:
                headers["If-None-Match"] = cached.etag
            elif cached.last_modified:
                headers["If-Modified-Since"] = cached.last_modified
        waits = _WAITS_CI if self._ci else _WAITS
        transient = rate = 0
        while True:
            now = self._now()
            until = _blocked_until(token.fp, now)
            if until is not None:  # REQ-FORGE-51
                raise RateLimited(f"{method} {path}: rate limited until {_iso_z(until)}", reset_at=until)
            self._check_budget(f"{method} {path}")
            started = time.monotonic()
            try:
                with _READS if method == "GET" else _WRITES:
                    resp = transport.send(method, target, headers, payload)
            except (NetworkTimeout, NetworkUnreachable) as exc:
                if idempotent and transient < len(waits):
                    self._pause(waits[transient] * (0.5 + self._jitter()))
                    transient += 1
                    continue
                self._event(label, transport.host, None, started)
                self._debug(method, target, None, None)
                raise exc from None
            except NetworkTLSError:
                self._event(label, transport.host, None, started)
                raise
            status = resp.status
            self.last_status = status
            self._record_date(resp.headers.get("date"))
            self.last_rate_remaining = _int_header(resp.headers.get("x-ratelimit-remaining"))
            if status == 304 and cached is not None and key is not None:
                self._caches.http.touch(key)
                merged = dict(resp.headers)
                for name, value in (("link", cached.link), ("content-type", cached.content_type), ("etag", cached.etag)):
                    if value is not None:
                        merged[name] = value
                    else:
                        merged.pop(name, None)
                if "x-oauth-scopes" not in merged and cached.oauth_scopes is not None:
                    merged["x-oauth-scopes"] = cached.oauth_scopes  # un 304 puede no traerla (§6.1)
                return _Resp(200, merged, cached.body, cached=True)
            if status in _TRANSIENT:
                if idempotent and transient < len(waits):
                    self._pause(waits[transient] * (0.5 + self._jitter()))
                    transient += 1
                    continue
                self._event(label, transport.host, status, started)
                self._debug(method, target, status, resp.headers)
                raise ForgeUnavailable(f"{method} {path}: HTTP {status}", http_status=status)
            if status in (403, 429):
                decision = self._rate_limit(resp, rate, f"{method} {path}")
                if decision is not None:
                    wait_s, err = decision
                    if err is None:
                        self._pause(wait_s)
                        rate += 1
                        continue
                    _block(token.fp, err.reset_at)
                    self._event(label, transport.host, status, started)
                    self._debug(method, target, status, resp.headers)
                    raise err
            if status == 401:
                self._invalidate()
                self._event(label, transport.host, status, started)
                self._debug(method, target, status, resp.headers)
                raise ForgeAuthError(f"{method} {path}: {_message(resp.body) or 'bad credentials'}", http_status=401)
            if 200 <= status < 300:
                if key is not None and status == 200:
                    etag = resp.headers.get("etag")
                    last_modified = resp.headers.get("last-modified")
                    if etag or last_modified:
                        entry = HttpEntry(
                            etag,
                            last_modified,
                            resp.headers.get("link"),
                            resp.headers.get("content-type"),
                            resp.body,
                            resp.headers.get("x-oauth-scopes"),
                        )
                        self._caches.http.put(key, entry)
                return resp
            self._debug(method, target, status, resp.headers)
            raise _HttpError(status, _message(resp.body), resp.headers)

    def _rate_limit(self, resp: _Resp, attempt: int, what: str) -> tuple[float, RateLimited | None] | None:
        """Decisión de §7.3 ante un 403 o 429: ``(espera, None)``, ``(0, RateLimited)`` o ``None``."""
        h = resp.headers
        status = resp.status
        now = self._now()
        message = _message(resp.body)
        retry_after = _int_header(h.get("retry-after"))
        hint = None
        if retry_after is not None:
            if (self._ci and attempt < 3) or (not self._ci and retry_after <= _RETRY_AFTER_MAX_S and attempt < 1):
                return float(retry_after), None
            reset_at = now + timedelta(seconds=retry_after)
            hint = self._hint("rate_limited", reset_at=_iso_z(reset_at))
            return 0.0, RateLimited(f"{what}: {message or 'rate limited'}", reset_at=reset_at, http_status=status, hint=hint)
        if h.get("x-ratelimit-remaining") == "0":  # límite primario
            reset = _int_header(h.get("x-ratelimit-reset"))
            reset_at = datetime.fromtimestamp(reset, timezone.utc) if reset is not None else now + timedelta(hours=1)
            if self._ci and attempt < 1 and reset_at - now <= _PRIMARY_WAIT_MAX:
                return max(0.0, (reset_at - now).total_seconds()) + 1.0, None
            hint = self._hint("rate_limited", reset_at=_iso_z(reset_at))
            return 0.0, RateLimited(f"{what}: {message or 'rate limited'}", reset_at=reset_at, http_status=status, hint=hint)
        if "secondary rate limit" in message.lower() or status == 429:
            if self._ci and attempt < len(_SECONDARY_WAITS_CI):
                return _SECONDARY_WAITS_CI[attempt], None
            reset_at = now + _SECONDARY_RESET
            hint = self._hint("rate_limited", reset_at=_iso_z(reset_at))
            return 0.0, RateLimited(f"{what}: {message or 'rate limited'}", reset_at=reset_at, http_status=status, hint=hint)
        return None

    def _json(self, resp: _Resp, what: str) -> Any:
        try:
            return json.loads(resp.body.decode("utf-8")) if resp.body else None
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise ForgeValidationError(f"{what}: the forge returned invalid JSON") from None

    def _obj(self, resp: _Resp, what: str) -> dict[str, Any]:
        data = self._json(resp, what)
        if not isinstance(data, dict):
            raise ForgeValidationError(f"{what}: unexpected response from the forge")
        return data

    def _paged(
        self,
        label: str,
        path: str,
        params: Mapping[str, object] | None = None,
        *,
        key: str | None = None,
        max_pages: int = MAX_PAGES,
        per_page: int = PAGE_SIZE,
    ) -> Iterator[list[Any]]:
        """Páginas de una lista, siguiendo ``Link: rel="next"`` (REQ-FORGE-8)."""
        query = {**(params or {}), "per_page": per_page}
        target: str | None = None
        for _ in range(max_pages):
            resp = self._call(label, "GET", path, params=query, target=target)
            data = self._json(resp, label)
            items = data.get(key) if key is not None and isinstance(data, dict) else data
            if not isinstance(items, list):
                raise ForgeValidationError(f"{label}: unexpected response from the forge")
            yield items
            nxt = _next_link(resp.headers.get("link"))
            if nxt is None:
                return
            target = self._relative(nxt)
        raise ForgeValidationError(f"{label}: more than {max_pages * per_page} items; refusing a truncated list")

    def _all(self, label: str, path: str, params: Mapping[str, object] | None = None, **kw: Any) -> list[Any]:
        out: list[Any] = []
        for page in self._paged(label, path, params, **kw):
            out.extend(page)
        return out

    def _classify(self, label: str, e: _HttpError) -> ForgeError:
        if e.status == 403:
            # 08 §2.1: el hint incluye X-Accepted-GitHub-Permissions (o el permiso de §4.2).
            perms = e.headers.get("x-accepted-github-permissions") or _PERMISSIONS.get(label)
            header = f"X-Accepted-GitHub-Permissions: {perms}" if perms else None
            parts = [self._hint("permission", permissions=header) or header] if header else [self._hint("permission_unknown")]
            if self._token().kind == "fine_grained_pat":
                parts.append(self._hint("token_approval"))
            self._redetect_once()
            hint = " ".join(p for p in parts if p) or None
            return ForgePermissionError(f"{label}: {e.message or 'forbidden'}", http_status=403, hint=hint)
        if e.status in (404, 410):
            self._redetect_once()
            return ForgeNotFound(f"{label}: {e.message or 'not found'}", http_status=e.status)
        if e.status >= 500:
            return ForgeUnavailable(f"{label}: HTTP {e.status}", http_status=e.status)
        return ForgeValidationError(f"{label}: HTTP {e.status}: {e.message}", http_status=e.status)

    def _redetect_once(self) -> None:
        """Un 403 o 404 con las capacidades de la caché: una sola redetección (REQ-FORGE-19)."""
        if not self._caps_from_cache or self._redetected:
            return
        self._redetected = True
        try:
            self.capabilities(refresh=True)
        except ForgeError:
            return

    # -- mapeos -------------------------------------------------------------------------------

    def _same_repo(self, head: object) -> bool:
        repo = head.get("repo") if isinstance(head, dict) else None
        full = repo.get("full_name") if isinstance(repo, dict) else None
        return isinstance(full, str) and full.casefold() == f"{self.repo.owner}/{self.repo.name}".casefold()

    def _issue(self, d: Mapping[str, Any]) -> Issue:
        reason = d.get("state_reason")
        state = d.get("state")
        return Issue(
            int(d["number"]),
            _str(d.get("title")),
            _str(d.get("body")),
            state if state in ("open", "closed") else "open",  # type: ignore[arg-type]
            reason if reason in _STATE_REASONS and "pull_request" not in d else None,  # type: ignore[arg-type]
            tuple(_str(lab.get("name")) for lab in d.get("labels") or () if isinstance(lab, dict)),
            tuple(_login(a) for a in d.get("assignees") or ()),
            _login(d.get("user")),
            _dt(d.get("created_at")),
            _dt(d.get("updated_at")),
            _opt_dt(d.get("closed_at")),
            _str(d.get("html_url")),
        )

    def _pull(self, d: Mapping[str, Any], *, detail: bool = False) -> PullRequest:
        head = d.get("head") if isinstance(d.get("head"), dict) else {}
        base = d.get("base") if isinstance(d.get("base"), dict) else {}
        merged_at = _opt_dt(d.get("merged_at"))
        merged = d.get("merged")
        state = d.get("state")
        return PullRequest(
            int(d["number"]),
            _str(d.get("node_id")),
            _str(d.get("title")),
            _str(d.get("body")),
            state if state in ("open", "closed") else "open",  # type: ignore[arg-type]
            bool(d.get("draft")),
            merged if isinstance(merged, bool) else merged_at is not None,
            merged_at,
            _str(head.get("ref")),
            _str(head.get("sha")),
            _str(base.get("ref")),
            _login(d.get("user")),
            tuple(_str(lab.get("name")) for lab in d.get("labels") or () if isinstance(lab, dict)),
            tuple(_login(u) for u in d.get("requested_reviewers") or ()),
            _dt(d.get("updated_at")),
            _str(d.get("html_url")),
            same_repo=self._same_repo(head),
            merged_by=_opt_login(d.get("merged_by")) if detail else None,
            created_at=_opt_dt(d.get("created_at")),
            merge_commit_sha=_str(d.get("merge_commit_sha")) or None,
        )

    @staticmethod
    def _comment(d: Mapping[str, Any]) -> Comment:
        return Comment(
            int(d["id"]),
            _login(d.get("user")),
            _str(d.get("body")),
            _dt(d.get("created_at")),
            _dt(d.get("updated_at")),
            _str(d.get("html_url")),
        )

    @staticmethod
    def _commit_info(d: Mapping[str, Any]) -> CommitInfo:
        commit = d.get("commit") if isinstance(d.get("commit"), dict) else {}
        author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
        committer = commit.get("committer") if isinstance(commit.get("committer"), dict) else {}
        return CommitInfo(
            _str(d.get("sha")),
            _str(commit.get("message")),
            _opt_login(d.get("author")),
            _dt(author.get("date")),
            _dt(committer.get("date")),
        )

    @staticmethod
    def _changed(d: Mapping[str, Any]) -> ChangedFile:
        status = d.get("status")
        previous = d.get("previous_filename")
        added, removed = d.get("additions"), d.get("deletions")
        return ChangedFile(
            _str(d.get("filename")),
            status if status in _FILE_STATUSES else "changed",  # type: ignore[arg-type]
            previous if isinstance(previous, str) else None,
            added if type(added) is int and added >= 0 else 0,
            removed if type(removed) is int and removed >= 0 else 0,
        )

    @staticmethod
    def _run(d: Mapping[str, Any]) -> WorkflowRun:
        branch = d.get("head_branch")
        conclusion = d.get("conclusion")
        return WorkflowRun(
            int(d["id"]),
            _str(d.get("name")),
            _str(d.get("event")),
            _str(d.get("status")),
            conclusion if isinstance(conclusion, str) else None,
            _str(d.get("head_sha")),
            branch if isinstance(branch, str) else None,
            _login(d.get("actor")),
            _str(d.get("html_url")),
            parse_time(d.get("created_at")),
        )

    @staticmethod
    def _review(d: Mapping[str, Any]) -> Review:
        user = d.get("user") if isinstance(d.get("user"), dict) else {}
        state = d.get("state")
        commit = d.get("commit_id")
        uid = user.get("id")
        return Review(
            int(d["id"]),
            _login(user),
            uid if isinstance(uid, int) and not isinstance(uid, bool) else 0,
            state if state in _REVIEW_STATES else "COMMENTED",  # type: ignore[arg-type]
            commit if isinstance(commit, str) else None,
            _opt_dt(d.get("submitted_at")),
        )

    def _ref_of(self, d: Mapping[str, Any], namespace: str) -> Ref:
        name = _str(d.get("ref"))
        prefix = f"refs/{namespace}/"
        obj = d.get("object") if isinstance(d.get("object"), dict) else {}
        return Ref(name[len(prefix):] if name.startswith(prefix) else name, _str(obj.get("sha")))

    def _record_date(self, value: str | None) -> None:
        if not value:
            return
        try:
            moment = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return
        self._server_date = moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)

    def server_time(self) -> datetime | None:
        """Hora del servidor según la cabecera ``Date`` de la última respuesta (05 §9.19); ``None``
        si todavía no hubo ninguna."""
        return self._server_date

    # -- identidad (§6) ---------------------------------------------------------------------

    def identity(self, *, refresh: bool = False) -> Identity:
        if self._identity is not None and not refresh:
            return self._identity
        token = self._token()
        now = self._now()
        if is_ci_token(self._env, token):  # REQ-FORGE-43: sin llamada
            ident = Identity(CI_LOGIN, None, "ci", token.source, "app_installation", "ci", None, None, now)
            self._identity = ident
            return ident
        if not refresh:
            cached = self._caches.load_identity(token.fp, now)
            if cached is not None:
                self._identity = cached
                return cached
        previous = self._caches.load_identity(token.fp, now, allow_expired=True)
        try:
            resp = self._call("identity", "GET", "/user")
        except _HttpError as e:
            raise self._classify("identity", e) from e
        data = self._obj(resp, "identity")
        login = data.get("login")
        uid = data.get("id")
        if not isinstance(login, str) or not login or not isinstance(uid, int) or isinstance(uid, bool):
            raise ForgeValidationError("identity: unexpected response from the forge")
        kind: Literal["user", "bot", "ci"] = "bot" if data.get("type") == "Bot" else "user"
        scopes: tuple[str, ...] | None = None
        scope: Literal["repo_restricted", "owner_wide", "broad", "unknown", "ci"] = "unknown"
        if token.kind in ("classic_pat", "oauth"):
            raw = resp.headers.get("x-oauth-scopes", "")
            scopes = tuple(sorted({s.strip() for s in raw.split(",") if s.strip()}))
            scope = "broad" if "repo" in scopes else "unknown"
        elif token.kind == "fine_grained_pat":
            # 08 §5.2: lo mide doctor (refresh=True) con GET /user/repos; si no, vale lo último medido.
            known = previous.token_scope if previous is not None and previous.token_kind == token.kind else "unknown"
            scope = self._fine_grained_scope() if refresh else known  # type: ignore[assignment]
        elif token.kind == "app_user":
            scope = "repo_restricted"
        elif token.kind == "app_installation":
            scope = "ci"
        prior: str | None = None
        if previous is not None and previous.id == uid:  # §6.4: mismo id, otro login
            prior = previous.login if not same_login(previous.login, login) else previous.previous_login
        ident = Identity(login, uid, kind, token.source, token.kind, scope, scopes, prior, now)
        self._identity = ident
        self._caches.save_identity(ident, token.fp, now + CACHE_TTL)
        return ident

    def _fine_grained_scope(self) -> Literal["repo_restricted", "owner_wide", "unknown"]:
        """Token fine-grained: un solo repo visible -> ``repo_restricted`` (08 §5.2)."""
        try:
            resp = self._call("identity", "GET", "/user/repos", params={"per_page": 2})
        except (_HttpError, ForgeError):
            return "unknown"
        data = self._json(resp, "identity")
        if not isinstance(data, list):
            return "unknown"
        return "repo_restricted" if len(data) <= 1 else "owner_wide"

    # -- capacidades (§3) -----------------------------------------------------------------

    def _caps_key(self, login: str, token: Token, default_branch: str) -> dict[str, object]:
        return {
            "forge": "github",
            "repo": f"{self.repo.owner}/{self.repo.name}",
            "base_branch": self._base_branch or default_branch,
            "token_fp": token.fp,
            "login": login,
        }

    def capabilities(self, *, refresh: bool = False) -> Capabilities:
        if self._caps is not None and not refresh:
            return self._caps
        token = self._token()
        login = self.identity().login
        now = self._now()
        previous = self._caps.profile if self._caps is not None else None
        if self._base_branch is not None:
            key = self._caps_key(login, token, self._base_branch)
            cached = self._caches.load_capabilities(key, self.repo)
            if cached is not None and cached.expires_at > now:
                previous = previous or cached.caps.profile
                caps = None if refresh else self._revalidate(cached, key, now)
                if caps is not None:
                    self._caps = caps
                    self._caps_from_cache = True
                    return caps
        caps = self._detect()
        self._caps = caps
        self._caps_from_cache = False
        if previous == "enforced" and caps.profile == "advisory":  # REQ-FORGE-20: el perfil cambió
            self._notice("W_FORGE_PROFILE_ADVISORY")
        self._caches.save_capabilities(self._caps_key(login, token, caps.default_branch), caps, now + CACHE_TTL, now)
        return caps

    def _revalidate(self, cached: CachedCapabilities, key: Mapping[str, object], now: datetime) -> Capabilities | None:
        """REQ-FORGE-20: cada 10 min, ``GET /repos/{o}/{r}`` condicional; ``None`` si hay que redetectar."""
        caps = cached.caps
        if now - cached.revalidated_at < REVALIDATE_EVERY:
            return caps
        try:
            resp = self._call("capabilities", "GET", "")
        except ForgeAuthError:
            raise  # REQ-FORGE-19: un 401 invalida identidad y capacidades
        except _HttpError:
            return None  # 403 o 404: se redetecta, y la detección lanza el error clasificado
        except ForgeError:
            return caps  # red, 5xx o límite: sigue valiendo la caché (24 h)
        if not resp.cached:
            data = self._json(resp, "capabilities")
            if not isinstance(data, dict):
                return None
            vis, owner, perm, default = self._repo_fields(data)
            if (vis, perm, default) != (caps.visibility, caps.viewer_permission, caps.default_branch):
                return None
        self._caches.save_capabilities(key, caps, cached.expires_at, now)
        return caps

    @staticmethod
    def _repo_fields(data: Mapping[str, Any]) -> tuple[str, str, str, str]:
        vis = data.get("visibility")
        if vis not in ("public", "private", "internal"):
            vis = "private" if data.get("private") else "public"
        owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
        owner_type = "Organization" if owner.get("type") == "Organization" else "User"
        perms = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
        if perms.get("admin"):
            perm = "admin"
        elif perms.get("maintain"):
            perm = "maintain"
        elif perms.get("push"):
            perm = "write"
        elif perms.get("triage"):
            perm = "triage"
        else:
            perm = "read"
        default = data.get("default_branch")
        if not isinstance(default, str) or not default:
            raise ForgeValidationError("capabilities: the repository has no default branch")
        return vis, owner_type, perm, default  # type: ignore[return-value]

    def _detect(self) -> Capabilities:
        """Algoritmo de 08 §3.2; solo hace ``GET`` (REQ-FORGE-13)."""
        token = self._token()
        o, r = self.repo.owner, self.repo.name
        try:
            resp = self._call("capabilities", "GET", "")
        except _HttpError as e:
            raise self._classify("capabilities", e) from e
        data = self._obj(resp, "capabilities")
        self._repo_json = data  # repo_settings la reusa sin otra petición (D-65)
        vis, owner_type, perm, default_branch = self._repo_fields(data)
        if self._base_branch is not None and default_branch != self._base_branch:
            hint = self._hint("base_not_default", default_branch=default_branch)
            raise BaseNotDefault(
                f"base branch {self._base_branch!r} is not the default branch", default_branch=default_branch, hint=hint
            )
        evidence = [f"GET /repos/{o}/{r} -> 200 visibility={vis}"]
        rules: bool | None
        if vis in ("public", "internal"):
            rules = True
        else:
            plan = self._visible_plan(data, owner_type, evidence)
            if plan is not None:
                rules = plan != "free"
                evidence.append(f"plan={plan}")
            else:
                try:
                    self._call("capabilities", "GET", "rulesets", params={"per_page": 1})
                    evidence.append("GET rulesets -> 200")
                    rules = None  # un 200 no prueba que se exija (§3.1)
                except _HttpError as e:
                    evidence.append(f"GET rulesets -> {e.status}")
                    rules = False if e.status == 403 and "upgrade" in e.message.lower() else None
        base = self._base_rules(self._base_branch or default_branch, evidence) if rules is not False else None
        if rules is None:
            self._notice("W_FORGE_CAPABILITY_UNKNOWN")
        elif rules and base is not None and base.source == "none":
            self._notice("W_FORGE_RULES_NOT_APPLIED")
        if self._checks_api is None:
            self._checks_api = token.kind != "fine_grained_pat"
        return Capabilities(
            forge="github",
            repo=self.repo,
            visibility=vis,  # type: ignore[arg-type]
            owner_type=owner_type,  # type: ignore[arg-type]
            viewer_permission=perm,  # type: ignore[arg-type]
            default_branch=default_branch,
            rules_available=rules,
            code_owners_enforceable=rules,
            base_rules=base,
            profile="enforced" if rules is True else "advisory",
            push_rulesets=vis != "public" and rules is True,
            checks_api=self._checks_api,
            transport=self._get_transport(token).name,
            write_mode=self.write_mode,
            detected_at=self._now(),
            evidence=tuple(evidence),
        )

    def _visible_plan(self, repo: Mapping[str, Any], owner_type: str, evidence: list[str]) -> str | None:
        """``plan.name`` si el visor puede leerlo (§3.1); ``None`` si no es visible."""
        owner = repo.get("owner") if isinstance(repo.get("owner"), dict) else {}
        login = owner.get("login")
        try:
            if owner_type == "Organization" and isinstance(login, str):
                resp = self._call("capabilities", "GET", f"/orgs/{_pct(login)}")
            elif isinstance(login, str) and same_login(login, self.identity().login):
                resp = self._call("capabilities", "GET", "/user")
            else:
                return None
        except _HttpError as e:
            evidence.append(f"GET plan -> {e.status}")
            return None
        data = self._json(resp, "capabilities")
        plan = data.get("plan") if isinstance(data, dict) else None
        name = plan.get("name") if isinstance(plan, dict) else None
        return name.lower() if isinstance(name, str) and name else None

    def _base_rules(self, branch: str, evidence: list[str]) -> BaseRules | None:
        """Reglas activas en la base: rulesets o protección clásica (§3.1)."""
        try:
            resp = self._call("capabilities", "GET", f"rules/branches/{_pct(branch, safe='/')}")
            items = self._json(resp, "capabilities")
        except _HttpError as e:
            evidence.append(f"GET rules/branches/{branch} -> {e.status}")
            items = None
        if isinstance(items, list) and items:
            evidence.append(f"GET rules/branches/{branch} -> 200 ({len(items)} reglas)")
            return rules_summary(items)
        try:
            resp = self._call("capabilities", "GET", f"branches/{_pct(branch, safe='/')}")
        except _HttpError as e:
            evidence.append(f"GET branches/{branch} -> {e.status}")
            return BaseRules("none", None, None, None, None, (), None) if e.status == 404 else None
        data = self._json(resp, "capabilities")
        if not isinstance(data, dict):
            return None
        if not data.get("protected"):
            evidence.append(f"GET branches/{branch} -> 200 protected=false")
            return BaseRules("none", None, None, None, None, (), None)
        evidence.append(f"GET branches/{branch} -> 200 protected=true")
        protection = data.get("protection") if isinstance(data.get("protection"), dict) else {}
        status_checks = protection.get("required_status_checks")
        contexts = status_checks.get("contexts") if isinstance(status_checks, dict) else None
        checks = tuple(c for c in contexts if isinstance(c, str)) if isinstance(contexts, list) else None
        return BaseRules("classic", None, None, None, None, checks, None)

    # -- refs ---------------------------------------------------------------------------------

    @staticmethod
    def _check_namespace(namespace: str) -> None:
        if namespace not in ("heads", "tags"):
            raise ValueError(f"unknown ref namespace: {namespace!r}")

    def get_ref(self, branch: str, *, namespace: RefNamespace = "heads") -> Ref | None:
        self._check_namespace(namespace)
        try:
            resp = self._call("get_ref", "GET", f"git/ref/{namespace}/{_pct(branch, safe='/')}")
        except _HttpError as e:
            if e.status == 404:
                return None
            raise self._classify("get_ref", e) from e
        data = self._obj(resp, "get_ref")
        ref = self._ref_of(data, namespace)
        if ref.branch != branch:  # GitHub devolvió otra ref
            return None
        return ref

    def list_refs(self, prefix: str, *, namespace: RefNamespace = "heads") -> list[Ref]:
        self._check_namespace(namespace)
        try:
            items = self._all("list_refs", f"git/matching-refs/{namespace}/{_pct(prefix, safe='/')}")
        except _HttpError as e:
            raise self._classify("list_refs", e) from e
        refs = [self._ref_of(d, namespace) for d in items if isinstance(d, dict)]
        return sorted((r for r in refs if r.branch.startswith(prefix)), key=lambda r: r.branch)

    def create_ref(self, branch: str, sha: str, *, namespace: RefNamespace = "heads") -> Ref:
        self._check_namespace(namespace)
        if self._web and self.write_mode == "git":
            return self._create_ref_by_push(branch, sha, namespace)
        try:
            resp = self._call(
                "create_ref", "POST", "git/refs", body={"ref": f"refs/{namespace}/{branch}", "sha": sha}, idempotent=False
            )
            return self._ref_of(self._obj(resp, "create_ref"), namespace)
        except _HttpError as e:
            if e.status not in (409, 422):
                raise self._classify("create_ref", e) from e
            existing = self.get_ref(branch, namespace=namespace)  # GET de confirmación (§4.4)
            if existing is not None and existing.sha == sha:
                return existing  # ya existe con el sha pedido: éxito (REQ-FORGE-28)
            if existing is not None:
                raise RefAlreadyExists(e.message, existing=existing, http_status=e.status) from e
            raise ForgeValidationError(f"create_ref: HTTP {e.status}: {e.message}", http_status=e.status) from e
        except (NetworkTimeout, ForgeUnavailable) as cause:  # respuesta ambigua (REQ-FORGE-29)
            existing = self.get_ref(branch, namespace=namespace)
            if existing is not None and existing.sha == sha:
                return existing
            if existing is not None:
                raise RefAlreadyExists(str(cause), existing=existing) from cause
            raise

    def _git(
        self, what: str, args: Sequence[str], *, input: bytes | None = None, extra_env: Mapping[str, str] | None = None
    ) -> tuple[int, str, str]:
        if self._run_git is None:
            raise ForgeUnsupported(f"{what}: the git write mode needs the local clone")
        if self._flag("OFFLINE") == "1":
            raise NetworkUnreachable(f"{what}: offline")
        rc, out, err = self._run_git(args, input=input, extra_env=extra_env)
        return rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    def _create_ref_by_push(self, branch: str, sha: str, namespace: str) -> Ref:
        """Reclamo por push con lease en la web (REQ-FORGE-67; 00-marco D-41 y D-42)."""
        ref = f"refs/{namespace}/{branch}"
        cause: Exception | None = None
        try:
            rc, out, _err = self._git(
                "create_ref", ["push", "--porcelain", f"--force-with-lease={ref}:", "origin", f"{sha}:{ref}"]
            )
            outcome = _push_outcome(rc, out, ref)
        except ForgeUnsupported:
            raise
        except Exception as exc:  # noqa: BLE001 - git falló o no respondió: resultado ambiguo
            outcome, cause = "unknown", exc
        existing = self.get_ref(branch, namespace=namespace)  # GET de confirmación (REQ-FORGE-28)
        if existing is not None and existing.sha == sha:
            return existing
        if existing is not None:
            raise RefAlreadyExists(f"create_ref: push {outcome}", existing=existing)
        if outcome == "rejected":
            raise ForgeValidationError("create_ref: the push was rejected and the ref does not exist")
        if outcome == "accepted":
            raise ForgeUnavailable("create_ref: the push was accepted but the ref is not visible yet")
        raise NetworkTimeout(f"create_ref: ambiguous push ({type(cause).__name__})") from cause

    def update_ref(self, branch: str, sha: str) -> Ref:
        if self._web and self.write_mode == "git":
            return self._update_ref_by_push(branch, sha)
        try:
            resp = self._call(
                "update_ref", "PATCH", f"git/refs/heads/{_pct(branch, safe='/')}", body={"sha": sha, "force": False}
            )
            return self._ref_of(self._obj(resp, "update_ref"), "heads")
        except _HttpError as e:
            if e.status == 422:  # no es avance rápido, o la regla de la base lo impide
                existing = self.get_ref(branch)
                if existing is not None and existing.sha == sha:
                    return existing  # idempotente con el mismo sha (§4.7)
                raise RefConflict(e.message, current=existing.sha if existing else None, http_status=422) from e
            if e.status == 409:
                raise ForgeUnavailable(e.message, http_status=409) from e
            raise self._classify("update_ref", e) from e
        except (NetworkTimeout, ForgeUnavailable):
            existing = self.get_ref(branch)
            if existing is not None and existing.sha == sha:
                return existing
            raise

    def _update_ref_by_push(self, branch: str, sha: str) -> Ref:
        """``git push`` sin ``--force``: solo avance rápido (REQ-FORGE-68)."""
        ref = f"refs/heads/{branch}"
        try:
            rc, out, _err = self._git("update_ref", ["push", "--porcelain", "origin", f"{sha}:{ref}"])
            outcome = _push_outcome(rc, out, ref)
        except ForgeUnsupported:
            raise
        except Exception:  # noqa: BLE001 - resultado ambiguo
            outcome = "unknown"
        if outcome == "accepted":
            return Ref(branch, sha)
        existing = self.get_ref(branch)
        if existing is not None and existing.sha == sha:
            return existing
        if outcome == "rejected":
            raise RefConflict("update_ref: the push is not a fast forward", current=existing.sha if existing else None)
        raise NetworkTimeout("update_ref: ambiguous push")

    def delete_ref(self, branch: str, *, namespace: RefNamespace = "heads") -> bool:
        self._check_namespace(namespace)
        try:
            # Sin reintento automático: tras un reintento, el 422 del segundo intento no diría si
            # la ref existía (False solo si no existía, CT-REF-04).
            self._call("delete_ref", "DELETE", f"git/refs/{namespace}/{_pct(branch, safe='/')}", idempotent=False)
            return True
        except _HttpError as e:
            if e.status in (404, 422):
                if self.get_ref(branch, namespace=namespace) is None:
                    return False
                raise ForgeValidationError(f"delete_ref: HTTP {e.status}: {e.message}", http_status=e.status) from e
            if e.status == 409:
                raise ForgeUnavailable(e.message, http_status=409) from e
            raise self._classify("delete_ref", e) from e
        except (NetworkTimeout, ForgeUnavailable):
            if self.get_ref(branch, namespace=namespace) is None:
                return True  # la respuesta se perdió, pero el borrado se aplicó
            raise

    def list_ref_activity(self, branch: str) -> list[RefActivity] | None:
        """Actividad de ``refs/heads/<branch>`` desde su última creación, en orden cronológico.

        La API la da de la más reciente a la más vieja: se pagina hasta encontrar el
        ``branch_creation`` (09-ci §4.2).
        """
        found: list[tuple[datetime, int, RefActivity]] = []
        try:
            for page in self._paged("list_ref_activity", "activity", {"ref": f"refs/heads/{branch}"}):
                created = False
                for d in page:
                    if not isinstance(d, dict) or d.get("activity_type") not in _ACTIVITY_TYPES:
                        continue
                    when = _dt(d.get("timestamp"))
                    act = RefActivity(
                        d["activity_type"],
                        _opt_login(d.get("actor")),
                        when,
                        _str(d.get("before"), NULL_SHA),
                        _str(d.get("after"), NULL_SHA),
                    )
                    ident = d.get("id")
                    found.append((when, ident if isinstance(ident, int) else 0, act))
                    if act.activity_type == "branch_creation":
                        created = True
                        break
                if created:
                    break
        except _HttpError as e:
            if e.status == 404:
                return None
            raise self._classify("list_ref_activity", e) from e
        found.sort(key=lambda item: (item[0], item[1]))
        return [act for _, _, act in found]

    # -- commits y contenido ------------------------------------------------------------------

    def create_empty_commit(self, parent: str, message: str) -> str:
        if self._web and self.write_mode == "git":
            return self._commit_tree(parent, message)
        try:
            resp = self._call("create_empty_commit", "GET", f"git/commits/{_pct(parent)}")
            tree = self._obj(resp, "create_empty_commit").get("tree")
            tree_sha = tree.get("sha") if isinstance(tree, dict) else None
            if not isinstance(tree_sha, str):
                raise ForgeValidationError("create_empty_commit: the parent has no tree")
            # Un commit se identifica por su contenido: repetirlo no tiene efecto (§4.5).
            resp = self._call(
                "create_empty_commit",
                "POST",
                "git/commits",
                body={"message": message, "tree": tree_sha, "parents": [parent]},
                idempotent=True,
            )
        except _HttpError as e:
            raise self._classify("create_empty_commit", e) from e
        sha = self._obj(resp, "create_empty_commit").get("sha")
        if not isinstance(sha, str) or _SHA_RE.fullmatch(sha) is None:
            raise ForgeValidationError("create_empty_commit: unexpected response from the forge")
        return sha

    def _commit_tree(self, parent: str, message: str) -> str:
        """Commit de reclamo en el clon local, a nombre de la cuenta (REQ-FORGE-67)."""
        ident = self.identity()
        noreply = f"{ident.id}+{ident.login}" if ident.id is not None else ident.login
        email = f"{noreply}@users.noreply.github.com"
        env = {
            "GIT_AUTHOR_NAME": ident.login,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": ident.login,
            "GIT_COMMITTER_EMAIL": email,
        }
        if _SHA_RE.fullmatch(parent) is None:
            raise ForgeValidationError("create_empty_commit: the parent must be a full sha")
        try:
            rc, out, _err = self._git(
                "create_empty_commit",
                ["commit-tree", f"{parent}^{{tree}}", "-p", parent, "-F", "-"],
                input=message.encode("utf-8"),
                extra_env=env,
            )
        except ForgeError:
            raise
        except Exception as exc:  # noqa: BLE001 - git no respondió
            raise NetworkTimeout(f"create_empty_commit: git failed ({type(exc).__name__})") from exc
        sha = out.strip()
        if rc != 0 or _SHA_RE.fullmatch(sha) is None:
            raise ForgeNotFound("create_empty_commit: the parent is not in the local clone")
        return sha

    def commit_files(
        self,
        branch: str,
        *,
        expected_head: str,
        changes: Sequence[FileChange],
        message: str,
        extra_parents: Sequence[str] = (),
    ) -> str:
        """Commit por la API de datos de git (§4.5)."""
        entries: list[dict[str, object]] = []
        for change in changes:
            if not isinstance(change, FileChange) or not _valid_path(change.path):
                raise ForgeValidationError(f"commit_files: invalid path {getattr(change, 'path', change)!r}")
            if change.path.casefold().startswith(".github/workflows/"):  # REQ-FORGE-32
                raise ForgeValidationError(f"commit_files: {change.path} is a workflow (class lead)")
        try:
            resp = self._call("commit_files", "GET", f"git/commits/{_pct(expected_head)}")
            tree = self._obj(resp, "commit_files").get("tree")
            base_tree = tree.get("sha") if isinstance(tree, dict) else None
            if not isinstance(base_tree, str):
                raise ForgeValidationError("commit_files: the head has no tree")
            for change in changes:
                mode = "100755" if change.executable else "100644"
                entry: dict[str, object] = {"path": change.path, "mode": mode, "type": "blob"}
                if change.content is None:
                    entry["sha"] = None
                else:
                    text = None
                    if len(change.content) <= TEXT_BLOB_MAX and b"\0" not in change.content:
                        try:
                            text = change.content.decode("utf-8")
                        except UnicodeDecodeError:
                            text = None
                    if text is not None:
                        entry["content"] = text
                    else:
                        blob = self._call(
                            "commit_files",
                            "POST",
                            "git/blobs",
                            body={"content": base64.b64encode(change.content).decode("ascii"), "encoding": "base64"},
                            idempotent=True,
                        )
                        entry["sha"] = self._obj(blob, "commit_files").get("sha")
                entries.append(entry)
            new_tree = self._call(
                "commit_files", "POST", "git/trees", body={"base_tree": base_tree, "tree": entries}, idempotent=True
            )
            tree_sha = self._obj(new_tree, "commit_files").get("sha")
            commit = self._call(
                "commit_files",
                "POST",
                "git/commits",
                body={"message": message, "tree": tree_sha, "parents": [expected_head, *extra_parents]},
                idempotent=True,
            )
            new_sha = self._obj(commit, "commit_files").get("sha")
        except _HttpError as e:
            raise self._classify("commit_files", e) from e
        if not isinstance(new_sha, str) or _SHA_RE.fullmatch(new_sha) is None:
            raise ForgeValidationError("commit_files: unexpected response from the forge")
        try:
            self._call(
                "commit_files", "PATCH", f"git/refs/heads/{_pct(branch, safe='/')}", body={"sha": new_sha, "force": False}
            )
        except _HttpError as e:
            if e.status == 409:
                raise ForgeUnavailable(e.message, http_status=409) from e
            if e.status != 422:
                raise self._classify("commit_files", e) from e
            current = self.get_ref(branch)
            if current is not None and current.sha == new_sha:
                return new_sha  # hubo un reintento (§4.5, paso 5)
            raise RefConflict(e.message, current=current.sha if current else None, http_status=422) from e
        except (NetworkTimeout, ForgeUnavailable):
            current = self.get_ref(branch)
            if current is not None and current.sha == new_sha:
                return new_sha
            raise
        return new_sha

    def merge_into(self, branch: str, source: str, *, message: str) -> str | None:
        try:
            resp = self._call(
                "merge_into",
                "POST",
                "merges",
                body={"base": branch, "head": source, "commit_message": message},
                idempotent=False,
            )
        except _HttpError as e:
            if e.status == 409:
                raise MergeConflict(e.message, http_status=409) from e
            raise self._classify("merge_into", e) from e
        if resp.status == 204:
            return None  # ya integrado
        sha = self._obj(resp, "merge_into").get("sha")
        if not isinstance(sha, str):
            raise ForgeValidationError("merge_into: unexpected response from the forge")
        return sha

    def get_commit(self, sha: str) -> CommitInfo:
        try:
            resp = self._call("get_commit", "GET", f"commits/{_pct(sha)}")
        except _HttpError as e:
            if e.status in (404, 422):
                raise ForgeNotFound(e.message, http_status=e.status) from e
            raise self._classify("get_commit", e) from e
        return self._commit_info(self._obj(resp, "get_commit"))

    def get_file(self, path: str, ref: str) -> FileContent | None:
        target = f"contents/{_pct(path, safe='/')}"
        try:
            resp = self._call("get_file", "GET", target, params={"ref": ref})
            data = self._json(resp, "get_file")
            if not isinstance(data, dict) or data.get("type") != "file":
                return None  # carpeta, enlace o submódulo
            blob_sha = _str(data.get("sha"))
            size = data.get("size")
            raw = data.get("content")
            if data.get("encoding") == "base64" and isinstance(raw, str) and raw and not (isinstance(size, int) and size > BIG_FILE):
                content = base64.b64decode(raw)
            elif isinstance(size, int) and size == 0:
                content = b""
            else:  # más de 1 MB: el contenido viene vacío; se pide en crudo (§4.2)
                content = self._call("get_file", "GET", target, params={"ref": ref}, accept=ACCEPT_RAW).body
        except _HttpError as e:
            if e.status == 404:
                return None
            raise self._classify("get_file", e) from e
        except ValueError as exc:  # base64 inválido
            raise ForgeValidationError("get_file: invalid content from the forge") from exc
        return FileContent(path, ref, blob_sha, content)

    def compare(self, base: str, head: str, *, with_commits: bool = False) -> Comparison:
        target = f"compare/{_pct(base, safe='/')}...{_pct(head, safe='/')}"
        try:
            resp = self._call("compare", "GET", target, params=None if with_commits else {"per_page": 1})
        except _HttpError as e:
            raise self._classify("compare", e) from e
        d = self._obj(resp, "compare")
        status = d.get("status")
        merge_base = d.get("merge_base_commit") if isinstance(d.get("merge_base_commit"), dict) else {}
        files = [self._changed(f) for f in d.get("files") or () if isinstance(f, dict)]
        commits: tuple[CommitInfo, ...] = ()
        if with_commits:
            commits = tuple(self._commit_info(c) for c in (d.get("commits") or ())[:MAX_COMMITS] if isinstance(c, dict))
        ahead, behind = d.get("ahead_by"), d.get("behind_by")
        return Comparison(
            base,
            head,
            status if status in ("identical", "ahead", "behind", "diverged") else "diverged",  # type: ignore[arg-type]
            ahead if isinstance(ahead, int) else 0,
            behind if isinstance(behind, int) else 0,
            _str(merge_base.get("sha")),
            tuple(files[:MAX_COMPARE_FILES]),
            len(files) >= MAX_COMPARE_FILES,  # GitHub corta en 300 sin avisar
            commits,
        )

    def codeowners_errors(self, ref: str) -> list[str]:
        try:
            resp = self._call("codeowners_errors", "GET", "codeowners/errors", params={"ref": ref})
        except _HttpError as e:
            if e.status == 404:
                return []
            raise self._classify("codeowners_errors", e) from e
        data = self._obj(resp, "codeowners_errors")
        out: list[str] = []
        for err in data.get("errors") or ():
            if not isinstance(err, dict):
                continue
            msg = err.get("message")
            out.append(msg if isinstance(msg, str) and msg else f"{err.get('kind')} at line {err.get('line')}")
        return out

    # -- Issues -------------------------------------------------------------------------------

    def get_issue(self, number: int) -> Issue | None:
        try:
            resp = self._call("get_issue", "GET", f"issues/{int(number)}")
        except _HttpError as e:
            if e.status in (404, 410):
                return None
            raise self._classify("get_issue", e) from e
        data = self._obj(resp, "get_issue")
        if "pull_request" in data:
            raise ForgeValidationError(f"#{number} is a pull request, not an issue (REQ-FORGE-9)")
        return self._issue(data)

    def list_issues(
        self,
        *,
        labels: Sequence[str] = (),
        state: str = "all",
        since: datetime | None = None,
    ) -> list[Issue]:
        if state not in ("open", "closed", "all"):
            raise ForgeValidationError(f"list_issues: invalid state {state!r}")
        params: dict[str, object] = {"state": state, "since": since}
        if labels:
            params["labels"] = list(labels)
        try:
            items = self._all("list_issues", "issues", params)
        except _HttpError as e:
            raise self._classify("list_issues", e) from e
        # GitHub pagina Issues y PR juntos; los PR se quitan aquí (REQ-FORGE-9).
        rows = [d for d in items if isinstance(d, dict) and "pull_request" not in d]
        issues = [(_dt(d.get("created_at")), self._issue(d)) for d in rows]
        issues.sort(key=lambda item: (item[0], item[1].number), reverse=True)
        return [issue for _, issue in issues]

    def _recover_issue(self, op_id: str, t0: datetime, cause: ForgeError) -> Issue:
        # GET issues?creator={login}&since={t0}: un Issue propio con el marcador (REQ-FORGE-35).
        marker = _op_marker(op_id)
        login = self.identity().login
        since = t0 - CLOCK_SKEW
        try:
            items = self._all("create_issue", "issues", {"creator": login, "since": since, "state": "all"})
        except (_HttpError, ForgeError):
            raise cause from None
        for d in sorted((x for x in items if isinstance(x, dict)), key=lambda x: _str(x.get("created_at"))):
            if "pull_request" in d or marker not in _str(d.get("body")):
                continue
            if same_login(_login(d.get("user")), login) and _dt(d.get("created_at")) >= since:
                return self._issue(d)
        raise cause

    def create_issue(self, title: str, body: str, *, labels: Sequence[str] = (), op_id: str) -> Issue:
        _check_op_id(op_id)
        requested = list(labels)
        payload: dict[str, object] = {"title": title, "body": _with_marker(body, op_id)}
        if requested:
            payload["labels"] = requested
        t0 = self._now()
        try:
            resp = self._call("create_issue", "POST", "issues", body=payload, idempotent=False)
            issue = self._issue(self._obj(resp, "create_issue"))
        except _HttpError as e:
            raise self._classify("create_issue", e) from e
        except (NetworkTimeout, ForgeUnavailable) as cause:
            issue = self._recover_issue(op_id, t0, cause)
        if not _casefold_set(requested) <= _casefold_set(issue.labels):
            raise ForgeValidationError("labels were not applied (REQ-FORGE-11)", http_status=201)
        return issue

    def update_issue(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
        state_reason: str | None = None,
    ) -> Issue:
        if state not in (None, "open", "closed") or state_reason not in (None, *_STATE_REASONS):
            raise ForgeValidationError("update_issue: invalid state or state_reason")
        payload = {k: v for k, v in (("title", title), ("body", body), ("state", state), ("state_reason", state_reason)) if v is not None}
        try:
            resp = self._call("update_issue", "PATCH", f"issues/{int(number)}", body=payload)
        except _HttpError as e:
            raise self._classify("update_issue", e) from e
        return self._issue(self._obj(resp, "update_issue"))

    def _labels_of(self, resp: _Resp, what: str) -> tuple[str, ...]:
        data = self._json(resp, what)
        if not isinstance(data, list):
            raise ForgeValidationError(f"{what}: unexpected response from the forge")
        return tuple(_str(d.get("name")) for d in data if isinstance(d, dict))

    def add_labels(self, number: int, labels: Sequence[str]) -> tuple[str, ...]:
        requested = list(labels)
        try:
            resp = self._call("add_labels", "POST", f"issues/{int(number)}/labels", body={"labels": requested}, idempotent=True)
        except _HttpError as e:
            raise self._classify("add_labels", e) from e
        current = self._labels_of(resp, "add_labels")
        if not _casefold_set(requested) <= _casefold_set(current):  # sin push, GitHub las descarta
            raise ForgeValidationError("labels were not applied (REQ-FORGE-11)", http_status=200)
        return current

    def remove_label(self, number: int, label: str) -> tuple[str, ...]:
        try:
            resp = self._call("remove_label", "DELETE", f"issues/{int(number)}/labels/{_pct(label)}")
            current = self._labels_of(resp, "remove_label")
        except _HttpError as e:
            if e.status != 404:
                raise self._classify("remove_label", e) from e
            # DELETE 404: idempotente; se leen las etiquetas actuales (08 §4.2).
            try:
                resp = self._call("remove_label", "GET", f"issues/{int(number)}")
            except _HttpError as e2:
                if e2.status in (404, 410):
                    raise ForgeNotFound(e2.message, http_status=e2.status) from e
                raise self._classify("remove_label", e2) from e
            data = self._obj(resp, "remove_label")
            current = tuple(_str(lab.get("name")) for lab in data.get("labels") or () if isinstance(lab, dict))
        if label.strip().casefold() in _casefold_set(current):
            raise ForgeValidationError("label was not removed (REQ-FORGE-11)", http_status=200)
        return current

    def ensure_labels(self, labels: Sequence[Label]) -> dict[str, Literal["created", "updated", "unchanged"]]:
        wanted = list(labels)
        if not all(isinstance(lab, Label) for lab in wanted):
            raise TypeError("ensure_labels expects Label objects")
        actions: dict[str, Literal["created", "updated", "unchanged"]] = {}
        last_write: float | None = None

        def spaced() -> None:  # REQ-FORGE-50: al menos 1 s entre escrituras de un lote
            if last_write is not None:
                self._pause(max(0.0, WRITE_SPACING_S - (time.monotonic() - last_write)))

        try:
            for lab in wanted:
                color = lab.color.lstrip("#").lower()
                try:
                    resp = self._call("ensure_labels", "GET", f"labels/{_pct(lab.name)}")
                except _HttpError as e:
                    if e.status != 404:
                        raise
                    spaced()
                    try:
                        self._call(
                            "ensure_labels",
                            "POST",
                            "labels",
                            body={"name": lab.name, "color": color, "description": lab.description},
                            idempotent=False,
                        )
                    except _HttpError as created:
                        # 422 al crear: releer esa etiqueta (08 §4.2); quizá otro la acaba de crear.
                        if created.status != 422 or not self._label_exists(lab):
                            raise
                        actions[lab.name] = "unchanged"
                    else:
                        actions[lab.name] = "created"
                    last_write = time.monotonic()
                    continue
                current = self._obj(resp, "ensure_labels")
                same = (
                    _str(current.get("name")) == lab.name
                    and _str(current.get("color")).lower() == color
                    and _str(current.get("description")) == lab.description
                )
                if not same:
                    spaced()
                    self._call(
                        "ensure_labels",
                        "PATCH",
                        f"labels/{_pct(_str(current.get('name'), lab.name))}",
                        body={"new_name": lab.name, "color": color, "description": lab.description},
                    )
                    last_write = time.monotonic()
                actions[lab.name] = "updated" if not same else "unchanged"
        except _HttpError as e:
            raise self._classify("ensure_labels", e) from e
        return actions

    def _label_exists(self, lab: Label) -> bool:
        try:
            self._call("ensure_labels", "GET", f"labels/{_pct(lab.name)}")
        except _HttpError:
            return False
        return True

    def list_issue_events(self, number: int) -> list[LabelEvent]:
        try:
            items = self._all("list_issue_events", f"issues/{int(number)}/events")
        except _HttpError as e:
            if e.status in (404, 410):
                raise ForgeNotFound(e.message, http_status=e.status) from e
            raise self._classify("list_issue_events", e) from e
        events: list[LabelEvent] = []
        for d in items:
            if not isinstance(d, dict) or d.get("event") not in ("labeled", "unlabeled"):
                continue
            label = d.get("label") if isinstance(d.get("label"), dict) else {}
            events.append(
                LabelEvent(int(d["id"]), d["event"], _str(label.get("name")), _login(d.get("actor")), _dt(d.get("created_at")))
            )
        events.sort(key=lambda ev: (ev.created_at, ev.id))
        return events

    def _assign(self, label: str, method: str, number: int, logins: Sequence[str], op: str) -> Issue:
        requested = list(logins)
        path = f"issues/{int(number)}" if op == "set" else f"issues/{int(number)}/assignees"
        try:
            resp = self._call(label, method, path, body={"assignees": requested}, idempotent=True)
        except _HttpError as e:
            raise self._classify(label, e) from e
        issue = self._issue(self._obj(resp, label))
        got = _casefold_set(issue.assignees)
        want = _casefold_set(requested)
        ok = got == want if op == "set" else (want <= got if op == "add" else not (want & got))
        if not ok:  # sin push o sin acceso, GitHub descarta el cambio en silencio
            raise ForgeValidationError("assignees were not applied (REQ-FORGE-11)", http_status=200)
        return issue

    def set_assignees(self, number: int, logins: Sequence[str]) -> Issue:
        return self._assign("set_assignees", "PATCH", number, logins, "set")

    def add_assignees(self, number: int, logins: Sequence[str]) -> Issue:
        return self._assign("add_assignees", "POST", number, logins, "add")

    def remove_assignees(self, number: int, logins: Sequence[str]) -> Issue:
        return self._assign("remove_assignees", "DELETE", number, logins, "remove")

    # -- comentarios -----------------------------------------------------------------------

    def list_comments(self, number: int, *, since: datetime | None = None) -> list[Comment]:
        try:
            items = self._all("list_comments", f"issues/{int(number)}/comments", {"since": since})
        except _HttpError as e:
            raise self._classify("list_comments", e) from e
        comments = [self._comment(d) for d in items if isinstance(d, dict)]
        comments.sort(key=lambda c: (c.created_at, c.id))
        return comments

    def create_comment(self, number: int, body: str, *, op_id: str) -> Comment:
        _check_op_id(op_id)
        t0 = self._now()
        try:
            resp = self._call(
                "create_comment",
                "POST",
                f"issues/{int(number)}/comments",
                body={"body": _with_marker(body, op_id)},
                idempotent=False,
            )
        except _HttpError as e:
            raise self._classify("create_comment", e) from e
        except (NetworkTimeout, ForgeUnavailable) as cause:
            return self._recover_comment(number, op_id, t0, cause)
        return self._comment(self._obj(resp, "create_comment"))

    def _recover_comment(self, number: int, op_id: str, t0: datetime, cause: ForgeError) -> Comment:
        # GET issues/{n}/comments?since=t0: un comentario propio con el marcador (REQ-FORGE-35).
        marker = _op_marker(op_id)
        login = self.identity().login
        since = t0 - CLOCK_SKEW
        try:
            comments = self.list_comments(number, since=since)
        except ForgeError:
            raise cause from None
        for c in comments:
            if same_login(c.author, login) and c.created_at >= since and marker in c.body:
                return c
        raise cause

    def update_comment(self, comment_id: int, body: str) -> Comment:
        try:
            resp = self._call("update_comment", "PATCH", f"issues/comments/{int(comment_id)}", body={"body": body})
        except _HttpError as e:
            raise self._classify("update_comment", e) from e
        return self._comment(self._obj(resp, "update_comment"))

    # -- pull requests y revisiones -----------------------------------------------------------

    def get_pull_request(self, number: int) -> PullRequest | None:
        try:
            resp = self._call("get_pull_request", "GET", f"pulls/{int(number)}")
        except _HttpError as e:
            if e.status == 404:
                return None
            raise self._classify("get_pull_request", e) from e
        return self._pull(self._obj(resp, "get_pull_request"), detail=True)  # trae merged_by

    def find_pull_request(self, head_branch: str, *, state: str = "all") -> PullRequest | None:
        """PR más reciente con cabeza ``<owner>:<head_branch>``; deja fuera los de forks."""
        if state not in ("open", "closed", "all"):
            raise ForgeValidationError(f"find_pull_request: invalid state {state!r}")
        try:
            items = self._all(
                "find_pull_request", "pulls", {"head": f"{self.repo.owner}:{head_branch}", "state": state}
            )
        except _HttpError as e:
            if e.status == 404:
                return None
            raise self._classify("find_pull_request", e) from e
        rows = [
            d
            for d in items
            if isinstance(d, dict)
            and self._same_repo(d.get("head"))
            and isinstance(d.get("head"), dict)
            and d["head"].get("ref") == head_branch
        ]
        if not rows:
            return None
        newest = max(rows, key=lambda d: (_dt(d.get("created_at")), int(d["number"])))
        return self._pull(newest)

    def list_pull_requests(self, *, state: str = "open", base: str | None = None) -> list[PullRequest]:
        if state not in ("open", "closed", "all"):
            raise ForgeValidationError(f"list_pull_requests: invalid state {state!r}")
        try:
            items = self._all("list_pull_requests", "pulls", {"state": state, "base": base})
        except _HttpError as e:
            raise self._classify("list_pull_requests", e) from e
        rows = sorted(
            (d for d in items if isinstance(d, dict)),
            key=lambda d: (_dt(d.get("created_at")), int(d["number"])),
            reverse=True,
        )
        return [self._pull(d) for d in rows]

    def list_pull_request_files(self, number: int) -> list[ChangedFile]:
        try:
            items = self._all("list_pull_request_files", f"pulls/{int(number)}/files", max_pages=MAX_FILE_PAGES)
        except _HttpError as e:
            raise self._classify("list_pull_request_files", e) from e
        return [self._changed(d) for d in items if isinstance(d, dict)]

    def list_pull_request_commits(self, number: int) -> list[CommitInfo]:
        try:
            items = self._all("list_pull_request_commits", f"pulls/{int(number)}/commits")
        except _HttpError as e:
            raise self._classify("list_pull_request_commits", e) from e
        return [self._commit_info(d) for d in items[:MAX_COMMITS] if isinstance(d, dict)]

    def create_pull_request(
        self, *, head: str, base: str, title: str, body: str, draft: bool = False
    ) -> PullRequest:
        payload = {"title": title, "head": head, "base": base, "body": body, "draft": bool(draft)}
        try:
            resp = self._call("create_pull_request", "POST", "pulls", body=payload, idempotent=False)
        except _HttpError as e:
            if e.status != 422:
                raise self._classify("create_pull_request", e) from e
            try:  # REQ-FORGE-36; si una sonda falla, queda ForgeValidationError
                existing = self.find_pull_request(head, state="open")
            except ForgeError:
                existing = None
            if existing is not None:
                raise PullRequestExists(e.message, existing=existing, http_status=422) from e
            try:
                ahead: int | None = self.compare(base, head).ahead_by
            except ForgeError:
                ahead = None
            if ahead == 0:
                raise NoCommitsBetween(e.message, http_status=422) from e
            raise ForgeValidationError(f"create_pull_request: HTTP 422: {e.message}", http_status=422) from e
        except (NetworkTimeout, ForgeUnavailable):  # §4.7: find_pull_request(head)
            existing = self.find_pull_request(head, state="open")
            if existing is not None:
                return existing
            raise
        return self._pull(self._obj(resp, "create_pull_request"), detail=True)

    def update_pull_request(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
    ) -> PullRequest:
        if state not in (None, "open", "closed"):
            raise ForgeValidationError("update_pull_request: invalid state")
        payload = {k: v for k, v in (("title", title), ("body", body), ("state", state)) if v is not None}
        try:
            resp = self._call("update_pull_request", "PATCH", f"pulls/{int(number)}", body=payload)
        except _HttpError as e:
            raise self._classify("update_pull_request", e) from e
        return self._pull(self._obj(resp, "update_pull_request"), detail=True)

    def mark_ready_for_review(self, number: int) -> PullRequest:
        # Reservado (§4.3, REQ-FORGE-26 y REQ-FORGE-27): v0.1 no usa GraphQL.
        url = f"https://github.com/{self.repo.owner}/{self.repo.name}/pull/{int(number)}"
        raise ForgeUnsupported("mark_ready_for_review is reserved in v0.1", hint=self._hint("mark_ready", url=url))

    def request_reviewers(self, number: int, logins: Sequence[str]) -> PullRequest:
        try:
            resp = self._call(
                "request_reviewers",
                "POST",
                f"pulls/{int(number)}/requested_reviewers",
                body={"reviewers": list(logins)},
                idempotent=True,
            )
        except _HttpError as e:
            raise self._classify("request_reviewers", e) from e
        return self._pull(self._obj(resp, "request_reviewers"), detail=True)

    def list_reviews(self, number: int) -> list[Review]:
        try:
            items = self._all("list_reviews", f"pulls/{int(number)}/reviews")
        except _HttpError as e:
            raise self._classify("list_reviews", e) from e
        far = datetime.max.replace(tzinfo=timezone.utc)
        reviews = [self._review(d) for d in items if isinstance(d, dict)]
        return sorted(reviews, key=lambda r: (r.submitted_at or far, r.id))

    def submit_review(
        self,
        number: int,
        *,
        event: Literal["APPROVE", "REQUEST_CHANGES"],
        body: str,
        commit_sha: str,
        confirmation: HumanConfirmation,
    ) -> Review:
        expected = _REVIEW_OPERATIONS.get(event)
        if expected is None:
            raise ForgeValidationError(f"submit_review: invalid event {event!r}")
        operation = getattr(confirmation, "operation", None)
        confirmed_at = getattr(confirmation, "confirmed_at", None)
        now = self._now()
        if (  # REQ-FORGE-10: confirmación humana de esta revisión, de los últimos 120 s
            operation != expected
            or not isinstance(confirmed_at, datetime)
            or confirmed_at.tzinfo is None
            or not now - CONFIRMATION_WINDOW <= confirmed_at <= now + timedelta(seconds=5)
        ):
            raise ForgeValidationError("submit_review needs a recent human confirmation (REQ-FORGE-10)")
        t0 = now
        try:
            resp = self._call(
                "submit_review",
                "POST",
                f"pulls/{int(number)}/reviews",
                body={"commit_id": commit_sha, "body": body, "event": event},
                idempotent=False,
            )
        except _HttpError as e:
            raise self._classify("submit_review", e) from e
        except (NetworkTimeout, ForgeUnavailable) as cause:  # §4.7: list_reviews
            login = self.identity().login
            state = "APPROVED" if event == "APPROVE" else "CHANGES_REQUESTED"
            try:
                reviews = self.list_reviews(number)
            except ForgeError:
                raise cause from None
            for review in reversed(reviews):
                if (
                    same_login(review.author, login)
                    and review.state == state
                    and review.commit_sha == commit_sha
                    and review.submitted_at is not None
                    and review.submitted_at >= t0 - CLOCK_SKEW
                ):
                    return review
            raise
        return self._review(self._obj(resp, "submit_review"))

    # -- checks y workflows ---------------------------------------------------------------------

    def get_checks(self, sha: str) -> Checks:
        """08 §4.6: check runs si el token puede leerlos; si no, runs de Actions; y el estado combinado."""
        runs: list[CheckRun] = []
        try:
            use_checks = self._checks_api is not False and self._token().kind != "fine_grained_pat"
            if use_checks:
                try:
                    items = self._all("get_checks", f"commits/{_pct(sha)}/check-runs", {"filter": "latest"}, key="check_runs")
                    for d in sorted((x for x in items if isinstance(x, dict)), key=lambda x: (_str(x.get("name")), x.get("id") or 0)):
                        if isinstance(d, dict):
                            conclusion = d.get("conclusion")
                            runs.append(
                                CheckRun(
                                    _str(d.get("name")),
                                    _str(d.get("status")),
                                    conclusion if isinstance(conclusion, str) else None,
                                    "check_run",
                                    _str(d.get("html_url")),
                                )
                            )
                except _HttpError as e:
                    if e.status != 403:
                        raise
                    self._checks_api = False  # hasta el primer 403 (§3.1)
                    use_checks = False
            if not use_checks:
                latest: dict[str, tuple[datetime, int, Mapping[str, Any]]] = {}
                for d in self._all("get_checks", "actions/runs", {"head_sha": sha}, key="workflow_runs"):
                    if not isinstance(d, dict):
                        continue
                    created = _dt(d.get("created_at"))
                    name = _str(d.get("name"))
                    current = latest.get(name)
                    if current is None or (created, int(d["id"])) > current[:2]:
                        latest[name] = (created, int(d["id"]), d)
                for name in sorted(latest):
                    d = latest[name][2]
                    conclusion = d.get("conclusion")
                    runs.append(
                        CheckRun(
                            name,
                            _str(d.get("status")),
                            conclusion if isinstance(conclusion, str) else None,
                            "workflow_run",
                            _str(d.get("html_url")),
                        )
                    )
            state, statuses = self._combined_status(sha)
        except _HttpError as e:
            raise self._classify("get_checks", e) from e
        for s in sorted(statuses, key=lambda s: _str(s.get("context"))):
            state = s.get("state")
            done = state in ("success", "failure", "error")
            runs.append(
                CheckRun(
                    _str(s.get("context")),
                    "completed" if done else "pending",
                    ("failure" if state == "error" else state) if done else None,  # type: ignore[arg-type]
                    "status",
                    _str(s.get("target_url")),
                )
            )
        overall = _overall(runs, bool(statuses) and state == "failure")
        if overall == "success" and statuses and state == "pending":
            overall = "pending"  # el estado combinado cuenta todos los contextos
        return Checks(sha, overall, tuple(runs))

    def _combined_status(self, sha: str) -> tuple[str, list[Mapping[str, Any]]]:
        """``GET commits/{sha}/status``: estado combinado y todos los contextos, paginados (REQ-FORGE-8)."""
        query: dict[str, object] = {"per_page": PAGE_SIZE}
        target: str | None = None
        state = ""
        statuses: list[Mapping[str, Any]] = []
        for page in range(MAX_PAGES):
            resp = self._call("get_checks", "GET", f"commits/{_pct(sha)}/status", params=query, target=target)
            data = self._obj(resp, "get_checks")
            if page == 0:
                state = _str(data.get("state"))
            statuses.extend(s for s in data.get("statuses") or () if isinstance(s, dict))
            nxt = _next_link(resp.headers.get("link"))
            if nxt is None:
                return state, statuses
            target = self._relative(nxt)
        raise ForgeValidationError(f"get_checks: more than {MAX_PAGES * PAGE_SIZE} statuses; refusing a truncated list")

    def get_workflow_run(self, run_id: int) -> WorkflowRun:
        try:
            resp = self._call("get_workflow_run", "GET", f"actions/runs/{int(run_id)}")
        except _HttpError as e:
            raise self._classify("get_workflow_run", e) from e
        return self._run(self._obj(resp, "get_workflow_run"))

    @staticmethod
    def _workflow(workflow_file: str) -> str:
        name = workflow_file.rsplit("/", 1)[-1]
        if not name:
            raise ForgeValidationError(f"invalid workflow file: {workflow_file!r}")
        return _pct(name)

    def successful_runs(
        self, workflow_file: str, branch: str, *, event: str, limit: int = 100
    ) -> list[WorkflowRun]:
        """Runs exitosos con ``branch`` y ``event`` exactos, del más reciente al más viejo (REQ-FORGE-69)."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= PAGE_SIZE * MAX_PAGES:
            raise ForgeValidationError(f"successful_runs: invalid limit {limit!r}")
        path = f"actions/workflows/{self._workflow(workflow_file)}/runs"
        params = {"branch": branch, "event": event, "status": "success"}
        found: list[Mapping[str, Any]] = []
        try:
            for page in self._paged("successful_runs", path, params, key="workflow_runs", per_page=min(limit, PAGE_SIZE)):
                found.extend(d for d in page if isinstance(d, dict))
                if len(found) >= limit:
                    break
        except _HttpError as e:
            raise self._classify("successful_runs", e) from e
        rows = [
            d
            for d in found
            if d.get("head_branch") == branch
            and d.get("event") == event
            and d.get("status") == "completed"
            and d.get("conclusion") == "success"
        ]
        rows.sort(key=lambda d: (_dt(d.get("created_at")), int(d["id"])), reverse=True)
        return [self._run(d) for d in rows[:limit]]

    def pulls_for_commit(self, sha: str) -> list[PullRequest]:
        """PR asociados al commit, resumidos: ``merged_by`` queda en ``None`` (08 §4.2)."""
        try:
            items = self._all("pulls_for_commit", f"commits/{_pct(sha)}/pulls")
        except _HttpError as e:
            raise self._classify("pulls_for_commit", e) from e
        pulls = [self._pull(d) for d in items if isinstance(d, dict)]
        return sorted(pulls, key=lambda p: p.number)

    def last_successful_run(self, workflow_file: str, branch: str) -> WorkflowRun | None:
        path = f"actions/workflows/{self._workflow(workflow_file)}/runs"
        try:
            resp = self._call(
                "last_successful_run", "GET", path, params={"branch": branch, "status": "success", "per_page": 1}
            )
        except _HttpError as e:
            raise self._classify("last_successful_run", e) from e
        data = self._obj(resp, "last_successful_run")
        rows = [
            d
            for d in data.get("workflow_runs") or ()
            if isinstance(d, dict) and d.get("head_branch") == branch and d.get("conclusion") == "success"
        ]
        return self._run(rows[0]) if rows else None

    # -- escrituras de configuración y del CI ----------------------------------------------------

    def _unsupported_plan(self, label: str, e: _HttpError) -> ForgeError:
        text = e.message.lower()
        if e.status == 404 or (e.status in (403, 422) and ("upgrade" in text or "not available" in text)):
            return ForgeUnsupported(f"{label}: HTTP {e.status}: {e.message}", http_status=e.status)
        return self._classify(label, e)

    def upsert_check_run(self, sha: str, name: str, *, conclusion: str, summary: str) -> None:
        """Un solo check run ``name`` por cabeza: lo actualiza o lo crea (REQ-FORGE-70)."""
        output = {"title": name, "summary": summary}
        completed = _iso_z(self._now())
        path = f"commits/{_pct(sha)}/check-runs"
        try:
            found = self._all("upsert_check_run", path, {"check_name": name, "filter": "latest"}, key="check_runs")
            ids = sorted(int(d["id"]) for d in found if isinstance(d, dict) and d.get("name") == name)
            update = {"status": "completed", "conclusion": conclusion, "completed_at": completed, "output": output}
            if ids:
                self._call("upsert_check_run", "PATCH", f"check-runs/{ids[-1]}", body=update)
                return
            create = {"name": name, "head_sha": sha, **update}
            try:
                self._call("upsert_check_run", "POST", "check-runs", body=create, idempotent=False)
            except (NetworkTimeout, ForgeUnavailable):
                again = self._all("upsert_check_run", path, {"check_name": name, "filter": "latest"}, key="check_runs")
                if not any(isinstance(d, dict) and d.get("name") == name for d in again):
                    raise
        except _HttpError as e:
            raise self._classify("upsert_check_run", e) from e


    # -- lecturas de la configuración (00-marco D-65; REQ-FORGE-73; solo GET) --------------------

    def _repo_data(self, label: str) -> dict[str, Any]:
        """``GET /repos/{o}/{r}``: la respuesta de la detección de este proceso o una nueva."""
        if self._repo_json is None:
            try:
                self._repo_json = self._obj(self._call(label, "GET", ""), label)
            except _HttpError as e:
                raise self._classify(label, e) from e
        return self._repo_json

    def _org(self) -> str | None:
        """Login de la organización dueña del repositorio; ``None`` si es una cuenta personal."""
        owner = self._repo_data("org_settings").get("owner")
        if isinstance(owner, dict) and owner.get("type") == "Organization" and isinstance(owner.get("login"), str):
            return str(owner["login"])
        return None

    def _get_or_none(self, label: str, path: str) -> Any:
        """GET que puede no estar permitido: ``None`` ante 403 o 404 (REQ-PERM-56)."""
        try:
            return self._json(self._call(label, "GET", path), label)
        except _HttpError as e:
            if e.status in (403, 404):
                return None
            raise self._classify(label, e) from e

    def repo_settings(self) -> dict[str, object]:
        """Las claves de ``REPO_SETTINGS`` que trae ``GET /repos`` (los ajustes de integración piden Contents W)."""
        data = self._repo_data("repo_settings")
        return {key: data[key] for key in REPO_SETTINGS if key in data}

    def _ruleset_ids(self) -> list[tuple[int, str]]:
        """``(id, nombre)`` de los rulesets del repositorio, sin los de la organización."""
        out: list[tuple[int, str]] = []
        for d in self._all("rulesets", "rulesets", {"includes_parents": False}):
            ident = d.get("id") if isinstance(d, dict) else None
            if isinstance(ident, int) and not isinstance(ident, bool):
                out.append((ident, str(d.get("name") or "")))
        return out

    def _ruleset(self, ident: int, name: str) -> RulesetInfo:
        data = self._obj(self._call("rulesets", "GET", f"rulesets/{ident}"), "rulesets")
        payload = {k: data[k] for k in ("target", "enforcement", "conditions", "bypass_actors", "rules") if k in data}
        return RulesetInfo(ident, str(data.get("name") or name), str(data.get("target", "")),
                           str(data.get("enforcement", "")), payload)

    def rulesets(self) -> list[RulesetInfo]:
        """Los rulesets del repositorio con su definición; GitHub muestra ``bypass_actors`` solo a
        quien puede editar el ruleset. Sin rulesets en el plan, ``ForgeUnsupported``."""
        try:
            return [self._ruleset(ident, name) for ident, name in self._ruleset_ids()]
        except _HttpError as e:
            raise self._unsupported_plan("rulesets", e) from e

    def list_labels(self) -> list[Label]:
        try:
            items = self._all("list_labels", "labels")
        except _HttpError as e:
            raise self._classify("list_labels", e) from e
        return [
            Label(str(d["name"]), str(d.get("color") or "ededed"), str(d.get("description") or ""))
            for d in items
            if isinstance(d, dict) and isinstance(d.get("name"), str)
        ]

    def collaborator_permission(self, login: str) -> str | None:
        try:
            data = self._json(self._call("collaborator_permission", "GET", f"collaborators/{_pct(login)}/permission"),
                              "collaborator_permission")
        except _HttpError as e:
            if e.status == 404:
                return None
            raise self._classify("collaborator_permission", e) from e
        if not isinstance(data, dict) or data.get("permission") in (None, "none"):
            return None
        role = data.get("role_name")
        return role if isinstance(role, str) and role else str(data["permission"])

    def outside_collaborators(self) -> list[str]:
        try:
            users = self._all("outside_collaborators", "collaborators", {"affiliation": "outside"})
        except _HttpError as e:
            raise self._classify("outside_collaborators", e) from e
        return [login for u in users if (login := _opt_login(u))]

    def actions_policy(self, level: AdminLevel = "repo") -> ActionsPolicy | None:
        if level == "org":
            org = self._org()
            if org is None:
                return None
            root = f"/orgs/{_pct(org)}/actions/permissions"
        else:
            root = "actions/permissions"
        data = self._get_or_none("actions_policy", root)
        if not isinstance(data, dict):
            return None
        allowed = data.get("allowed_actions")
        allowed = allowed if allowed in ("all", "local_only", "selected") else None
        pinning = data.get("sha_pinning_required")
        enabled = data.get("enabled") if level == "repo" else data.get("enabled_repositories") != "none"
        policy = ActionsPolicy(level, bool(enabled), allowed, pinning if isinstance(pinning, bool) else None)
        if allowed != "selected":
            return policy
        selected = self._get_or_none("actions_policy", f"{root}/selected-actions")
        if not isinstance(selected, dict):
            return policy
        patterns = selected.get("patterns_allowed")
        return ActionsPolicy(
            level,
            policy.enabled,
            allowed,
            policy.sha_pinning_required,
            bool(selected.get("github_owned_allowed")),
            bool(selected.get("verified_allowed")),
            tuple(p for p in patterns if isinstance(p, str)) if isinstance(patterns, list) else (),
        )

    def workflow_permissions(self, level: AdminLevel = "repo") -> WorkflowPermissions | None:
        if level == "org":
            org = self._org()
            if org is None:
                return None
            path = f"/orgs/{_pct(org)}/actions/permissions/workflow"
        else:
            path = "actions/permissions/workflow"
        data = self._get_or_none("workflow_permissions", path)
        if not isinstance(data, dict) or data.get("default_workflow_permissions") not in ("read", "write"):
            return None
        return WorkflowPermissions(data["default_workflow_permissions"], bool(data.get("can_approve_pull_request_reviews")))

    def org_settings(self) -> dict[str, object] | None:
        org = self._org()
        if org is None:
            return None
        data = self._get_or_none("org_settings", f"/orgs/{_pct(org)}")
        if not isinstance(data, dict):
            return None
        return {key: data[key] for key in ORG_SETTINGS if key in data}


# ---------------------------------------------------------------------------
# Fábrica (ops.context.context_factory, REQ-ARCH-15)
# ---------------------------------------------------------------------------


def _base_branch_of(ctx: Any) -> str | None:
    try:
        branch = ctx.base().project.base_branch
    except Exception:  # noqa: BLE001 - sin política de la base no se comprueba la rama por defecto
        return None
    return branch if isinstance(branch, str) and branch else None


def _store_of(ctx: Any) -> CacheBackend | None:
    try:
        if ctx.project(required=False) is None:
            return None
        return ctx.store()  # type: ignore[no-any-return]
    except Exception:  # noqa: BLE001 - sin caché en disco, en memoria
        return None


def _hint_of(ctx: Any) -> HintFn | None:
    try:
        t = ctx.t()
    except Exception:  # noqa: BLE001 - sin traductor no hay hint
        return None

    def hint(key: str, **params: object) -> str | None:
        return t.get(f"forge.hint.{key}", **params)  # type: ignore[no-any-return]

    return hint


def _git_of(ctx: Any) -> GitRunner:
    def run(
        args: Sequence[str], *, input: bytes | None = None, extra_env: Mapping[str, str] | None = None
    ) -> tuple[int, bytes, bytes]:
        proc = ctx.git().run(*args, check=False, input=input, read=False, extra_env=extra_env)
        return proc.returncode, proc.stdout, proc.stderr

    return run


def create(ctx: Any) -> GitHubAdapter:
    """Adaptador GitHub de la operación (``ops.context.context_factory``).

    El repositorio sale de ``ctx.forge_target()``; ``ForgeValidationError`` si no es un
    ``owner/name`` válido. En CI (superficie ``ci`` o ``GITHUB_ACTIONS=true``) no hay
    caché en disco (REQ-FORGE-18, REQ-FORGE-53) ni tope de tiempo por operación.
    """
    inv = ctx.inv
    env = dict(inv.env)
    repo = parse_repo(ctx.forge_target().repo)
    ci = getattr(inv, "surface", None) == "ci" or env.get("GITHUB_ACTIONS") == "true"
    run_tool = getattr(ctx, "run_tool", None)
    run_gh: ToolRunner | None = None
    if callable(run_tool):

        def run_gh(
            args: Sequence[str],
            *,
            timeout: float,
            input: bytes | None = None,
            extra_env: Mapping[str, str] | None = None,
        ) -> subprocess.CompletedProcess[bytes]:
            return run_tool("gh", args, timeout=timeout, input=input, extra_env=extra_env)  # type: ignore[no-any-return]

    emit = getattr(ctx, "emit", None)
    return GitHubAdapter(
        repo,
        env=env,
        base_branch=_base_branch_of(ctx),
        ci=ci,
        cache=None if ci else _store_of(ctx),
        run_gh=run_gh,
        run_git=_git_of(ctx),
        hint=_hint_of(ctx),
        on_event=emit if callable(emit) else None,
        now=getattr(inv, "now", None),
        budget_s=None if ci else OP_BUDGET_S,
    )


# ---------------------------------------------------------------------------
# Administración (08 §2.2; 00-marco D-62): solo init
# ---------------------------------------------------------------------------


class GitHubAdmin:
    """``ForgeAdmin`` de GitHub (08 §2.2; 00-marco D-62).

    Usa la política HTTP de ``GitHubAdapter`` (§4.1 y §7) con el token de administración
    explícito: nunca lo resuelve de las fuentes de §5.1 y no guarda nada en disco, ni
    respuestas, ni identidad, ni capacidades (REQ-FORGE-71). Solo lo construye ``init``.
    """

    def __init__(self, repo: RepoRef, token: str, **options: Any) -> None:
        if not isinstance(token, str) or not token.strip():
            raise NoTokenError("administration token is empty")
        self.repo = repo
        self._gh = GitHubAdapter(repo, token=token.strip(), cache=None, ci=False, **options)

    def __repr__(self) -> str:
        return f"GitHubAdmin({self.repo.owner}/{self.repo.name})"

    def close(self) -> None:
        self._gh.close()

    # -- utilidades ---------------------------------------------------------------------------

    def _get(self, label: str, path: str, params: Mapping[str, object] | None = None) -> Any:
        try:
            return self._gh._json(self._gh._call(label, "GET", path, params=params), label)
        except _HttpError as e:
            raise self._gh._classify(label, e) from e

    def _write(self, label: str, method: str, path: str, body: object) -> _Resp:
        try:
            return self._gh._call(label, method, path, body=body)
        except _HttpError as e:
            raise self._gh._classify(label, e) from e

    # -- ForgeAdmin: las lecturas son las del adaptador (D-65), con el token de administración ----

    def identity(self) -> Identity:
        """La cuenta del token de administración (``GET /user``, una vez por instancia)."""
        return self._gh.identity()

    def repo_settings(self) -> dict[str, object]:
        return self._gh.repo_settings()

    def update_repo_settings(self, changes: Mapping[str, object]) -> dict[str, object]:
        unknown = sorted(set(changes) - set(REPO_SETTINGS))
        if unknown:  # REQ-FORGE-72: sin llamar a la forja
            raise ForgeValidationError(f"update_repo_settings: keys outside REPO_SETTINGS: {', '.join(unknown)}")
        if not changes:
            return self.repo_settings()
        resp = self._write("update_repo_settings", "PATCH", "", dict(changes))
        data = self._gh._obj(resp, "update_repo_settings")
        self._gh._repo_json = data
        current = {key: data[key] for key in REPO_SETTINGS if key in data}
        ignored = sorted(k for k, v in changes.items() if k in current and current[k] != v)
        if ignored:  # REQ-FORGE-11: el efecto tiene que verse en la respuesta
            raise ForgeValidationError(f"update_repo_settings: the forge did not apply {', '.join(ignored)}")
        return current

    def actions_policy(self, level: AdminLevel = "repo") -> ActionsPolicy | None:
        return self._gh.actions_policy(level)

    def set_actions_policy(self, *, enabled: bool, allowed_actions: str, sha_pinning_required: bool) -> None:
        body = {"enabled": enabled, "allowed_actions": allowed_actions, "sha_pinning_required": sha_pinning_required}
        self._write("set_actions_policy", "PUT", "actions/permissions", body)

    def workflow_permissions(self, level: AdminLevel = "repo") -> WorkflowPermissions | None:
        return self._gh.workflow_permissions(level)

    def set_workflow_permissions(self, *, default: Literal["read", "write"], can_approve: bool) -> None:
        body = {"default_workflow_permissions": default, "can_approve_pull_request_reviews": can_approve}
        self._write("set_workflow_permissions", "PUT", "actions/permissions/workflow", body)

    def retention_days(self) -> int | None:
        try:
            data = self._gh._json(
                self._gh._call("retention_days", "GET", "actions/permissions/artifact-and-log-retention"), "retention_days"
            )
        except _HttpError as e:
            if e.status == 404:
                return None  # REQ-PERM-57: el endpoint no existe (por confirmar en S5)
            raise self._gh._classify("retention_days", e) from e
        days = data.get("days") if isinstance(data, dict) else None
        return days if isinstance(days, int) and not isinstance(days, bool) else None

    def set_retention_days(self, days: int) -> None:
        try:
            self._gh._call("set_retention_days", "PUT", "actions/permissions/artifact-and-log-retention", body={"days": days})
        except _HttpError as e:
            if e.status == 404:
                raise ForgeUnsupported("set_retention_days: the endpoint does not exist", http_status=404) from e
            raise self._gh._classify("set_retention_days", e) from e

    def collaborator_permission(self, login: str) -> str | None:
        return self._gh.collaborator_permission(login)

    def add_collaborator(self, login: str, permission: CollaboratorPermission) -> Literal["added", "invited"]:
        resp = self._write("add_collaborator", "PUT", f"collaborators/{_pct(login)}", {"permission": permission})
        return "invited" if resp.status == 201 else "added"

    def outside_collaborators(self) -> list[str]:
        return self._gh.outside_collaborators()

    def user_id(self, login: str) -> int:
        data = self._get("user_id", f"/users/{_pct(login)}")
        uid = data.get("id") if isinstance(data, dict) else None
        if not isinstance(uid, int) or isinstance(uid, bool):
            raise ForgeValidationError("user_id: unexpected response from the forge")
        return uid

    def org_settings(self) -> dict[str, object] | None:
        return self._gh.org_settings()

    def _ruleset_id(self, name: str) -> int | None:
        return next((ident for ident, found in self._gh._ruleset_ids() if found == name), None)

    def ruleset(self, name: str) -> RulesetInfo | None:
        try:
            found = self._ruleset_id(name)
            return None if found is None else self._gh._ruleset(found, name)
        except _HttpError as e:
            raise self._gh._unsupported_plan("rulesets", e) from e

    def upsert_ruleset(self, name: str, payload: Mapping[str, object]) -> int:
        """Crea o actualiza el ruleset ``name`` del repositorio (06 §4.7)."""
        body = {**dict(payload), "name": name}
        try:
            existing = self._ruleset_id(name)
            if existing is not None:
                self._gh._call("upsert_ruleset", "PUT", f"rulesets/{existing}", body=body)
                return existing
            try:
                resp = self._gh._call("upsert_ruleset", "POST", "rulesets", body=body, idempotent=False)
            except (NetworkTimeout, ForgeUnavailable):
                found = self._ruleset_id(name)  # REQ-FORGE-35: buscar el efecto antes de repetir
                if found is None:
                    raise
                return found
        except _HttpError as e:
            raise self._gh._unsupported_plan("upsert_ruleset", e) from e
        ident = self._gh._obj(resp, "upsert_ruleset").get("id")
        if not isinstance(ident, int) or isinstance(ident, bool):
            raise ForgeValidationError("upsert_ruleset: unexpected response from the forge")
        return ident

    def branch_rules(self, branch: str) -> list[dict[str, object]]:
        try:
            items = self._gh._all("branch_rules", f"rules/branches/{_pct(branch, safe='/')}")
        except _HttpError as e:
            raise self._gh._classify("branch_rules", e) from e
        return [dict(item) for item in items if isinstance(item, dict)]


def create_admin(ctx: Any, token: str) -> GitHubAdmin:
    """``ForgeAdmin`` de la operación (``ops.context.OpContext.forge_admin``; 00-marco D-62).

    Mismo repositorio que ``create``, con el token de administración que tecleó la persona
    y sin caché en disco (REQ-FORGE-71). Sin tope de tiempo de red: ``init`` está exceptuada
    del plazo de las operaciones interactivas (05 REQ-OPS-22).
    """
    inv = ctx.inv
    emit = getattr(ctx, "emit", None)
    return GitHubAdmin(
        parse_repo(ctx.forge_target().repo),
        token,
        env=dict(inv.env),
        hint=_hint_of(ctx),
        on_event=emit if callable(emit) else None,
        now=getattr(inv, "now", None),
    )
