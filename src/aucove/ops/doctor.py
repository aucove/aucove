# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Operación ``doctor`` · ``check_environment`` (05-operaciones §9.2; 00-marco D-34 l).

Comprobaciones locales: ``python`` (con el lanzador de 01 REQ-ARCH-33, buscado solo en las
carpetas absolutas de ``PATH``, y un aviso si ``PATH`` tiene entradas que dependen del
directorio actual, S4), ``git``, ``project_root``, ``project_yaml``, ``roles_yaml``,
``convention``, ``remote``, ``deputy``, ``codeowners_sync``, ``generated_files``,
``mcp_config`` (``.mcp.json`` contra 10-seguridad SB04), ``cache`` y ``evidence_tools`` (si
está instalado el extra ``evidence``, con ``find_spec`` y sin importarlo; si falta, ``warn`` y
``W_CONFIG_TOOL_MISSING``, REQ-ARCH-99). ``evidence_signing`` (00-marco D-72) revisa la firma
del proyecto con la política de la base: sin firma, ``warn``; con ``ci`` o ``admin`` y sin una
llave vigente, ``fail``; con la forja, el environment ``<nombre>-evidence`` solo puede admitir
la rama base y, con ``ci``, tiene que existir en un repositorio que admita environments (06
REQ-PERM-61). El token de uso diario no lee los secretos.

**La forja.** Con un proyecto, la política de su base y red, ``doctor`` hace estas lecturas,
en orden y solo con ``GET`` (08-forja §7.5):

1. ``identity(refresh=True)``: la cuenta del token y su clasificación (08 §5.2; con un token
   fine-grained, ``GET /user/repos``). Check ``identity``: el tipo de token (10-seguridad
   REQ-SEC-29, con el enlace del token recomendado de REQ-SEC-27), su alcance
   (``W_AUTH_BROAD_TOKEN``, REQ-FORGE-39; ``W_AUTH_WORKFLOW_SCOPE``, REQ-FORGE-40) y los correos
   de los commits y de ``roles.yaml``, que tienen que ser la dirección noreply (00-marco D-44).
2. ``capabilities(refresh=True)`` (REQ-FORGE-18). Checks ``forge_reachable`` (el repositorio y
   el permiso; un 403 se explica con la aprobación de tokens de la org, 08 §5.4),
   ``membership`` (estar en ``roles.yaml`` y tener escritura, 06 REQ-PERM-28) y ``profile``
   (08 §3.3 y 06 §5: el perfil y sus avisos, la línea fija de ``advisory``, la mejora
   detectada y las reglas de la base contra 06 §4.7, con REQ-PERM-53 y REQ-PERM-60).
   ``data.profile`` es el detectado.
3. ``codeowners_errors(<base>)``: ``codeowners_sync`` falla también si la forja informa
   errores de ``CODEOWNERS`` (06 REQ-PERM-31).
4. La deriva de lo que aplica ``init`` (06 §4.11, REQ-PERM-39), con las lecturas de solo
   lectura del adaptador (00-marco D-65). Lo que el token no puede leer queda como
   desconocido, sin aviso:
   - ``merge-settings``: los ajustes del repositorio contra 06 §4.2; los de 11-trazabilidad
     §3.2 dan ``fail`` (REQ-TRACE-36), los demás ``warn``.
   - ``profile`` (``enforced``): los rulesets ``<nombre>-base`` (exacto y activo, REQ-PERM-32;
     sin bypass con suplente, REQ-PERM-21), ``<nombre>-task-branches`` (REQ-PERM-52) y, con push
     rulesets, ``<nombre>-workflows`` (REQ-PERM-53).
   - ``membership``: la escritura de cada persona activa (REQ-PERM-28) y, si consulta el
     ``lead``, los colaboradores externos (``W_AUTH_BROAD_TOKEN`` con ``outside_collaborator``,
     REQ-PERM-55).
   - ``forge_reachable``: las etiquetas de 06 §4.5, el token de los workflows (REQ-PERM-27),
     los ajustes de la organización (06 §4.10, REQ-PERM-37; 10-seguridad REQ-SEC-71) y la
     política de Actions (``W_FORGE_ACTIONS_RESTRICTED``, REQ-OPS-64; si no se puede leer, no
     avisa).
5. ``half_states``: el último run exitoso de ``expire-claims`` en la base
   (``last_successful_run``; con más de 2 días o sin ninguno, ``warn``: 09-ci REQ-CI-31) y los
   estados a medias del tablero de ``status`` (05 §6.3, REQ-OPS-27): su caché si tiene menos de
   10 min y, con ``--refresh`` o sin ella, la forja. ``data.half_states`` lleva cada anomalía de
   04-tareas §13 que cierra una operación, con la de ``HALF_STATES``: las de las tareas propias y,
   para el ``lead``, las que solo él puede cerrar o que repara ``expire-claims``.

Sin red (``<PREFIJO>OFFLINE=1`` o una falla de red), los checks de la forja quedan en ``skip``
y se agrega ``W_NET_OFFLINE``. Si no se pudo detectar el perfil, ``data.profile`` es
``advisory`` con ``W_FORGE_CAPABILITY_UNKNOWN`` (D-34 l; REQ-FORGE-14). ``claude_settings``
todavía queda en ``skip``.

La política es siempre la de la rama base (D-08; 01 REQ-ARCH-46): ``convention``,
``deputy``, ``profile``, ``role`` e ``is_deputy`` salen de ``OpContext.base()``. Si la base no
da una política válida (``E_CONFIG_POLICY_UNAVAILABLE``), ``convention`` y ``deputy`` dan
``fail`` con el motivo, ``role`` queda en ``null`` y nunca se recurre al árbol de trabajo.
La persona se resuelve con ``Roles.identify(login, id)`` (08-forja §6.3 y §6.4) sobre la
identidad que dio la forja o, sin consultarla, la de la caché: si ``people`` declara
``forge_id``, decide el id. Una cuenta que no es nadie de ``people`` no tiene rol; si el id
coincide y el login no, o si la forja informa el login anterior y ``roles.yaml`` todavía lo
tiene, se agrega ``W_AUTH_LOGIN_CHANGED`` (sin asociar el login nuevo, REQ-FORGE-48).
``data.identity.source`` es la fuente del token (08 §5.1) o ``cache``. ``codeowners_sync`` lee
el árbol con la misma vista y la misma función que la regla ``roles-sync``.

Funciona también fuera de un proyecto, con ``project_root: null``. Si algún check da
``fail``, termina con ``E_CONFIG_DOCTOR_FAILED`` y ``data`` completa.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypeVar
from urllib.parse import urlencode

from .. import _meta
from ..config.project import ProjectConfig, compat, parse_project
from ..config.roles import Person, Roles, parse_roles
from ..domain.checks import required_checks
from ..domain.findings import has_errors
from ..domain.text import same_login
from ..forge.base import BaseNotDefault, ForgeError, token_sources
from ..i18n import Translator
from ..render.mcp import MCP_JSON_PATH, mcp_problem
from ..vcs.git import GitError, GitUnavailable, child_env, which_outside
from ..vcs.links import is_link
from ..vcs.tree import worktree
from .board import is_network_error, load_board
from .context import env_flag
from .protections import (
    BASE_RULESET,
    EVIDENCE_ENVIRONMENT,
    LABELS,
    ORG_RECOMMENDED,
    TASK_RULESET,
    WORKFLOWS_RULESET,
    actions_restriction,
    base_rules_missing,
    base_ruleset,
    covers,
    desired_settings,
    environments_available,
    task_ruleset,
    template_actions,
)
from .registry import REGISTRY
from .result import MAX_TEXT, OpError, Result, WarningObj, clip, fail, make_error, make_warning, ok
from .rules.roles_sync import compare_files, is_skill_path, regenerate, tree_reader
from .snapshot import EXPIRE_WORKFLOW

if TYPE_CHECKING:
    from ..config.base import BaseSnapshot
    from ..forge.base import (
        ActionsPolicy,
        Capabilities,
        ForgeAdapter,
        Identity,
        Label,
        RulesetInfo,
        WorkflowPermissions,
        WorkflowRun,
    )
    from .board import Board
    from ..localstore.store import IdentityCache
    from ..vcs.git import Git
    from ..vcs.tree import TreeView
    from .context import OpContext, Project

OP = "doctor"
BUDGET: int = next(s.budget_chars or 6000 for s in REGISTRY if s.id == OP)
Status = Literal["ok", "warn", "fail", "skip"]

# Orden de data_doctor.checks[] (enum de result.schema.json y 05 §9.2).
CHECK_ORDER: tuple[str, ...] = (
    "python", "git", "project_root", "project_yaml", "roles_yaml", "convention", "remote",
    "identity", "membership", "forge_reachable", "profile", "deputy", "codeowners_sync",
    "merge-settings", "generated_files", "claude_settings", "mcp_config", "half_states", "cache",
    "evidence_tools", "evidence_signing",
)
# Queda en skip en esta versión: claude_settings llega con .claude/settings.json (H4).
SKIPPED: frozenset[str] = frozenset({"claude_settings"})
# 11-trazabilidad §3.2: el método de integración; si difiere, merge-settings falla (REQ-TRACE-36).
TRACE_SETTINGS: tuple[str, ...] = (
    "allow_squash_merge", "allow_merge_commit", "allow_rebase_merge", "squash_merge_commit_title",
    "squash_merge_commit_message",
)
MIN_PYTHON = (3, 11)
GIT_TIMEOUT_S = 10.0
PROFILE = "advisory"  # sin detección concluyente (D-34 l, REQ-FORGE-14)
WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})  # crear task/<n> (D-10; 06 REQ-PERM-28)
# REQ-SEC-29: el tipo de token se avisa salvo el fine-grained, el del CI y el del proxy de la
# sesión web (08 §8), que la persona no elige.
QUIET_TOKEN_KINDS = frozenset({"fine_grained_pat", "app_installation", "proxy"})
NOREPLY_SUFFIX = "@users.noreply.github.com"  # 00-marco D-44
# Enlace prellenado del token de uso diario (10-seguridad §5.2; 08 REQ-FORGE-41): Metadata R
# va implícito, y el repositorio se elige a mano porque GitHub no deja preseleccionarlo.
TOKEN_URL = "https://github.com/settings/personal-access-tokens/new"
TOKEN_NAME_MAX = 40
TOKEN_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ("contents", "write"), ("issues", "write"), ("pull_requests", "write"), ("actions", "read"), ("statuses", "read"),
)

_WIN_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD;.VBS;.JS;.WS;.MSC"  # el de shutil.which sin PATHEXT
_GIT_VERSION = re.compile(r"git version (\S{1,40})")
_GITHUB_PATH = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]{1,100}")
_REMOTE = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9+.-]*://(?:[^@/]*@)?|[^@/:]+@)(?P<host>[^/:]+)(?::\d+)?[/:](?P<path>.+?)(?:\.git)?/*$"
)
_IDENT_EMAIL = re.compile(r"<([^<>]*)>")
T = TypeVar("T")
# 05 §9.2 (REQ-OPS-27): anomalía de 04-tareas §13 -> quién la ve y qué operación la cierra. Repetir
# claim completa la asignación y el marcador; release sobre una tarea ya abierta, una liberación a
# medias; lo que repara expire-claims (REQ-TASK-15) se cierra en su próxima corrida. Las demás
# anomalías piden una persona o la web, y status ya las lista.
HALF_STATES: Mapping[str, tuple[Literal["owner", "lead"], str]] = {
    "claim_without_marker": ("owner", "claim"),
    "assignees_mismatch": ("owner", "claim"),
    "pr_closed_unmerged": ("owner", "submit"),
    "stale_expired_label": ("owner", "expire-claims"),
    "ref_without_owner": ("lead", "release"),
    "owner_sources_disagree": ("lead", "release"),
    "claim_ref_replaced": ("lead", "release"),
    "assignee_without_ref": ("lead", "expire-claims"),
    "issue_open_after_merge": ("lead", "expire-claims"),
}
MAX_HALF_STATES = 20  # en data.half_states; el mensaje dice cuántas más hay
EXPIRE_FILE = EXPIRE_WORKFLOW.rsplit("/", 1)[-1]
EXPIRE_STALE = timedelta(days=2)  # 09-ci REQ-CI-31


@dataclass(frozen=True)
class Check:
    """Un elemento de ``data_doctor.checks``."""

    id: str
    status: Status
    message: str
    fix: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "status": self.status,
            "message": clip(self.message, MAX_TEXT),
            "fix": clip(self.fix, MAX_TEXT) if self.fix is not None else None,
        }


def _env_get(env: Mapping[str, str], name: str) -> str | None:
    """Variable de entorno sin distinguir mayúsculas en el nombre (Windows)."""
    if name in env:
        return env[name]
    for key, value in env.items():
        if key.upper() == name:
            return value
    return None


def _absolute_entry(entry: str) -> bool:
    """``True`` si la carpeta de ``PATH`` no depende del directorio actual."""
    if not os.path.isabs(entry):
        return False
    # En Windows, '\\bin' (sin unidad) se resuelve contra la unidad del directorio actual.
    return os.name != "nt" or bool(os.path.splitdrive(entry)[0])


def find_launcher(name: str, *, path: str | None = None, pathext: str | None = None) -> str | None:
    """``shutil.which(name)`` de REQ-ARCH-33, pero solo en las carpetas absolutas de ``PATH``.

    ``shutil.which`` de Python 3.11 en Windows antepone siempre el directorio actual, y una
    entrada vacía o relativa de ``PATH`` también se resuelve contra él. Un lanzador que solo
    está ahí (por ejemplo, un ``aucove.exe`` commiteado en la raíz del proyecto) no está en
    el ``PATH`` con que los hooks invocan al núcleo (REQ-ARCH-31), así que no cuenta.

    En lo demás sigue a ``shutil.which``: en Windows prueba las extensiones de ``PATHEXT``
    (así ``doctor`` puede avisar de un ``.bat``) y en POSIX exige permiso de ejecución.
    ``path`` y ``pathext`` sustituyen a ``PATH`` y ``PATHEXT`` del proceso.
    """
    search = os.environ.get("PATH", os.defpath) if path is None else path
    if os.name == "nt":
        source = (os.environ.get("PATHEXT") if pathext is None else pathext) or _WIN_DEFAULT_PATHEXT
        exts = [e for e in source.split(os.pathsep) if e]
        names = [name] if any(name.lower().endswith(e.lower()) for e in exts) else [name + e for e in exts]
    else:
        names = [name]
    seen: set[str] = set()
    for raw in search.split(os.pathsep):
        entry = raw.strip().strip('"')
        if not entry or not _absolute_entry(entry):
            continue
        key = os.path.normcase(entry)
        if key in seen:
            continue
        seen.add(key)
        for candidate in (os.path.join(entry, n) for n in names):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def cwd_path_entries(path: str | None = None) -> list[str]:
    """Entradas de ``PATH`` que dependen del directorio actual (REQ-ARCH-33; S4, 00-marco D-52).

    Claude Code busca el ``command`` de un hook en forma exec solo en ``PATH``, sin el
    directorio actual, pero resuelve una entrada relativa (``.``, ``bin``; en Windows también
    ``C:bin`` y ``\\bin``) contra el directorio del hook, que está dentro del proyecto: un
    ``aucove`` que esté ahí se ejecutaría en lugar del instalado. Fuera de Windows una entrada
    vacía también es el directorio actual; en Windows Claude Code la ignora (S4).
    Devuelve cada entrada una vez, en el orden de ``PATH``.
    """
    search = os.environ.get("PATH", "") if path is None else path
    found: list[str] = []
    for raw in search.split(os.pathsep):
        entry = raw.strip().strip('"')
        if entry:
            if not _absolute_entry(entry):
                found.append(entry)
        elif os.name != "nt":
            found.append(raw)
    return list(dict.fromkeys(found))


def _shown_entries(entries: Sequence[str], limit: int = 5) -> str:
    """``.``, ``bin`` → «`.`, `bin`»; la entrada vacía se muestra como ``""``."""
    shown = ", ".join(f"`{e}`" if e.strip() else '`""`' for e in entries[:limit])
    return shown + (", …" if len(entries) > limit else "")


def github_repo(url: str) -> str | None:
    """``owner/name`` de una URL de ``origin`` en ``github.com`` (https, ssh o scp); si no, ``None``.

    La usa ``init``, que toma ``forge.repo`` de ``origin`` (05 §9.3).
    """
    m = _REMOTE.match(url.strip())
    if m is None or m.group("host").lower() != "github.com":
        return None
    path = m.group("path")
    return path if _GITHUB_PATH.fullmatch(path) else None


def matches_repo(url: str, repo: str) -> bool:
    """``True`` si ``url`` (https, ssh o scp) apunta a ``github.com/<repo>``."""
    m = _REMOTE.match(url.strip())
    if m is None:
        return False
    return m.group("host").lower() == "github.com" and m.group("path").lower() == repo.lower()


def token_link(repo: str) -> str:
    """Enlace prellenado del token de uso diario para ``owner/name`` (10-seguridad §5.2, REQ-SEC-27)."""
    owner, _, name = repo.partition("/")
    params = [
        ("name", f"{_meta.NAME}-{name}"[:TOKEN_NAME_MAX]),
        ("description", f"{_meta.NAME} ({name})"),
        ("target_name", owner),
        ("expires_in", "30"),
        *TOKEN_PERMISSIONS,
    ]
    return f"{TOKEN_URL}?{urlencode(params, safe='()')}"


def packaged_copies(env: Mapping[str, str]) -> list[tuple[str, str, str]]:
    """Copias del núcleo que instaló una app empaquetada de Windows (MSIX) en su almacenamiento privado.

    Lo que se instala desde dentro de una app así, como Claude Desktop (su agente o su panel Terminal),
    queda en ``%LOCALAPPDATA%\\Packages\\<app>\\LocalCache\\Roaming\\uv\\tools\\<nombre>``, y esa app usa esa
    copia en lugar de la que se instala desde una terminal normal, aunque sea de otra versión. Devuelve
    ``(app, versión, ruta)``, con la versión del ``.dist-info`` (``?`` si no se lee). Fuera de Windows no
    hay ``LOCALAPPDATA`` y no devuelve nada.
    """
    local = _env_get(env, "LOCALAPPDATA")
    if not local:
        return []
    out: list[tuple[str, str, str]] = []
    try:
        for tools in sorted(Path(local, "Packages").glob(f"*/LocalCache/Roaming/uv/tools/{_meta.NAME}")):
            if not tools.is_dir():
                continue
            infos = sorted((tools / "Lib" / "site-packages").glob(f"{_meta.NAME}-*.dist-info"))
            version = infos[0].name[len(_meta.NAME) + 1:-len(".dist-info")] if infos else "?"
            out.append((tools.parents[4].name, version, str(tools)))
    except OSError:  # sin permiso para recorrer Packages: no hay nada que avisar
        return out
    return out


def is_noreply(email: str | None) -> bool:
    """``True`` si ``email`` es una dirección noreply de GitHub (00-marco D-44)."""
    if not isinstance(email, str):
        return False
    value = email.strip().lower()
    return value.endswith(NOREPLY_SUFFIX) and len(value) > len(NOREPLY_SUFFIX)


def noreply_email(login: str, uid: int | None) -> str:
    """La dirección noreply de la cuenta: ``<id>+<login>@users.noreply.github.com``."""
    return f"{uid}+{login}{NOREPLY_SUFFIX}" if uid else f"{login}{NOREPLY_SUFFIX}"


class _Doctor:
    """Estado de una corrida: lecturas compartidas entre checks y avisos acumulados."""

    def __init__(self, ctx: OpContext, *, refresh: bool = False) -> None:
        self.ctx = ctx
        self.refresh = refresh
        self.t: Translator = ctx.t()
        self.env = ctx.inv.env
        self.project: Project | None = ctx.project(required=False)
        self.warnings: list[WarningObj] = []
        self.cfg: ProjectConfig | None = None
        self.project_bytes: bytes | None = None
        self.project_errors = 0
        self.roles: Roles | None = None
        self.roles_bytes: bytes | None = None
        self.roles_errors = 0
        self.roles_warnings = 0
        self.snap: BaseSnapshot | None = None
        self.policy_error: OpError | None = None  # por qué no hay política de la base
        self.tree: TreeView | None = None
        self.identity_cache: IdentityCache | None = None
        self.read: Callable[[str], bytes | None] | None = None
        self.exists: Callable[[str], bool] | None = None
        # La forja (08 §7.5). forge_skip dice por qué no se consultó; *_error, qué falló.
        self.forge_skip: str | None = None
        self.forge_detail: dict[str, object] = {}
        self.net_failed = False
        self.ident: Identity | None = None
        self.ident_error: ForgeError | None = None
        self.caps: Capabilities | None = None
        self.caps_error: ForgeError | None = None
        self.codeowners: list[str] | None = None
        # La deriva (D-65): None = no se pudo leer.
        self.settings: dict[str, object] | None = None
        self.rulesets: list[RulesetInfo] | None = None
        self.labels: list[Label] | None = None
        self.permissions: dict[str, str | None] | None = None  # login -> role_name (None: no colabora)
        self.outside: list[str] | None = None
        self.actions: dict[str, ActionsPolicy | None] = {}
        self.workflow: dict[str, WorkflowPermissions | None] = {}
        self.org: dict[str, object] | None = None
        # half_states: None = no se pudo leer
        self.half: list[dict[str, object]] | None = None
        self.half_total = 0
        self.expire: tuple[WorkflowRun | None] | None = None
        # La persona de roles.yaml (de la base) de la identidad: forja o caché.
        self.shown_identity: dict[str, object] = {"login": None, "source": None}
        self.person: Person | None = None
        self.renamed_from: str | None = None  # login anterior que roles.yaml todavía tiene (08 §6.4)
        self.forge: ForgeAdapter | None = None
        if self.project is not None:
            self._load(self.project)
            self._consult()
            self._resolve()  # antes de la deriva: la revisión de los externos es del lead
            if self.forge is not None and self.caps is not None:
                self._drift(self.forge)
                self._tasks(self.forge)

    # -- lecturas ------------------------------------------------------------------

    def _git(self) -> Git | None:
        try:
            return self.ctx.git()
        except OpError:
            return None

    def _load(self, project: Project) -> None:
        # misma vista y misma lectura que la regla roles-sync en modo tree
        self.tree = worktree(project.root, git=self._git())
        self.read, self.exists = tree_reader(self.tree)
        self.project_bytes = self.read("project.yaml")
        if self.project_bytes is not None:
            cfg, findings = parse_project(self.project_bytes, file="project.yaml", t=self.t)
            self.cfg = cfg
            self.project_errors = sum(1 for f in findings if f.severity == "error")
        self.roles_bytes = self.read("roles.yaml")
        if self.roles_bytes is not None:
            roles, findings = parse_roles(self.roles_bytes, file="roles.yaml", t=self.t)
            self.roles = roles
            self.roles_errors = sum(1 for f in findings if f.severity == "error")
            self.roles_warnings = 0 if has_errors(findings) else len(findings)
        try:
            self.snap = self.ctx.base()
        except OpError as exc:  # D-08: sin la política de la base no se recurre al árbol
            self.snap = None
            self.policy_error = exc
        if self.snap is not None and self.snap.fallback_sha is not None:  # REQ-PERM-4: sha de cada archivo
            self.warnings.append(
                make_warning("W_CONFIG_POLICY_FALLBACK", self.t, details=self.snap.fallback_details())
            )
        self.identity_cache = self.ctx.store().identity(token_fp=None)

    def _consult(self) -> None:
        """Las lecturas de la forja, en el orden de 08 §7.5 (solo ``GET``)."""
        if env_flag(self.env, "OFFLINE") == "1":
            self.forge_skip = "offline"
            return
        if self.snap is None:
            self.forge_skip = "policy"
            return
        try:
            forge = self.ctx.forge()
        except OpError as exc:  # forja sin adaptador en esta versión, o forge.repo inválido
            self.forge_skip, self.forge_detail = "unsupported", {"code": exc.code}
            return
        try:
            self.ident = forge.identity(refresh=True)
        except ForgeError as exc:
            if not self._skipped_by(exc):
                self.ident_error = exc
            return
        try:
            self.caps = forge.capabilities(refresh=True)  # REQ-FORGE-18
        except ForgeError as exc:
            if not self._skipped_by(exc):
                self.caps_error = exc
            return
        try:
            self.codeowners = forge.codeowners_errors(self.snap.project.base_branch)
        except ForgeError as exc:  # sin esa lectura, codeowners_sync solo compara en local
            self.net_failed = self.net_failed or is_network_error(exc)
        self.forge = forge

    def _read(self, fn: Callable[[], T]) -> T | None:
        """Una lectura de la deriva: lo que no se puede leer queda como desconocido (D-65)."""
        if self.net_failed:  # sin red, las demás tampoco responderían
            return None
        try:
            return fn()
        except ForgeError as exc:
            self.net_failed = is_network_error(exc)
            return None

    def _drift(self, forge: ForgeAdapter) -> None:
        """Lo que aplica ``init`` y revisa ``doctor`` (06 §4.11, REQ-PERM-39): solo ``GET``."""
        caps, roles = self.caps, self.policy_roles
        assert caps is not None and roles is not None
        org_owned = caps.owner_type == "Organization"
        self.settings = self._read(forge.repo_settings)
        if caps.profile == "enforced":
            self.rulesets = self._read(forge.rulesets)
        self.labels = self._read(forge.list_labels)
        permissions: dict[str, str | None] = {}
        for person in roles.people:
            if not person.active:
                continue
            role = self._read(lambda login=person.login: (forge.collaborator_permission(login),))
            if role is None:  # una sola que no se pueda leer deja el conjunto en desconocido
                permissions = {}
                break
            permissions[person.login] = role[0]
        self.permissions = permissions or None
        if self.person is not None and self.person.role == "lead":
            self.outside = self._read(forge.outside_collaborators)  # REQ-PERM-55: lo revisa el lead
        for level in ("repo", "org") if org_owned else ("repo",):
            self.actions[level] = self._read(lambda level=level: forge.actions_policy(level))  # type: ignore[misc]
            self.workflow[level] = self._read(lambda level=level: forge.workflow_permissions(level))  # type: ignore[misc]
        if org_owned:
            self.org = self._read(forge.org_settings)

    def _tasks(self, forge: ForgeAdapter) -> None:
        """El último run de ``expire-claims`` (REQ-CI-31) y el tablero de ``status`` (REQ-OPS-27)."""
        snap = self.snap
        assert snap is not None
        base = snap.project.base_branch
        self.expire = self._read(lambda: (forge.last_successful_run(EXPIRE_FILE, base),))
        if self.net_failed:
            return
        try:  # como context: la caché de 10 min o la forja; con --refresh, siempre la forja
            loaded = load_board(self.ctx, cfg=snap.project, roles=snap.roles, store=self.ctx.store(),
                                now=self.ctx.inv.now(), mode="refresh" if self.refresh else "auto")
        except (OpError, ForgeError, OSError):
            loaded = None
        if loaded is not None:
            found = self._half_states(loaded.board)
            self.half, self.half_total = found[:MAX_HALF_STATES], len(found)

    def _half_states(self, board: Board) -> list[dict[str, object]]:
        """Las anomalías que cierra una operación: las de las tareas propias y, para el ``lead``,
        las que solo él puede cerrar (04-tareas REQ-TASK-16)."""
        login = self.ident.login if self.ident is not None else None
        lead = self.person is not None and self.person.role == "lead"
        out: list[dict[str, object]] = []
        for task in sorted(board.tasks, key=lambda x: x.number):
            mine = login is not None and same_login(task.owner, login)
            for kind in task.anomalies:
                who, op = HALF_STATES.get(kind, (None, None))
                if (who == "owner" and mine) or (who == "lead" and lead):
                    out.append({"task": task.number, "kind": kind, "suggested_op": op})
        return out

    def _skipped_by(self, exc: ForgeError) -> bool:
        """Sin red o con la API limitada, los checks de la forja quedan en ``skip``."""
        if is_network_error(exc):
            self.forge_skip, self.net_failed = "network", True
            return True
        if exc.kind == "rate_limited":
            self.forge_skip, self.forge_detail = "rate_limited", {"name": _meta.NAME}
            return True
        return False

    def _resolve(self) -> None:
        """La persona de ``roles.yaml`` de la base para la identidad de la forja o de la caché."""
        if self.ident is not None:
            login: object = self.ident.login
            uid, source = self.ident.id, self.ident.token_source
            previous = self.ident.previous_login
        else:
            cached = self.identity_cache
            login = cached.login if cached is not None else None
            uid, source, previous = (cached.id if cached is not None else None), "cache", None
        if not _login_ok(login):
            return
        assert isinstance(login, str)
        self.shown_identity = {"login": login, "source": source}
        roles = self.policy_roles
        if roles is None:
            return
        found = roles.identify(login, uid)
        self.person = found.person
        if found.login_changed and found.person is not None:
            self.warnings.append(
                make_warning("W_AUTH_LOGIN_CHANGED", self.t, details={"login": login, "roles_login": found.person.login})
            )
        elif found.person is None and previous and roles.person(previous) is not None:
            self.renamed_from = previous  # REQ-FORGE-48: no se asocia el login nuevo
            self.warnings.append(make_warning("W_AUTH_LOGIN_CHANGED", self.t, details={"login": login, "roles_login": previous}))

    @property
    def policy_roles(self) -> Roles | None:
        """``roles.yaml`` de la base (D-08); ``None`` si la base no da una política válida."""
        return self.snap.roles if self.snap is not None else None

    @property
    def policy_cfg(self) -> ProjectConfig | None:
        """``project.yaml`` de la base (REQ-ARCH-46); ``None`` si la base no da una política válida."""
        return self.snap.project if self.snap is not None else None

    def _repo(self) -> str:
        """``owner/name`` del repositorio que se consulta."""
        cfg = self.policy_cfg
        if cfg is not None and cfg.forge_repo:
            return str(cfg.forge_repo)
        caps = self.caps
        return f"{caps.repo.owner}/{caps.repo.name}" if caps is not None else ""

    def _tree_project_invalid(self) -> bool:
        """``True`` si la base falló con ``E_CONFIG_INVALID`` y el ``project.yaml`` del árbol no vale.

        ``OpContext.base()`` ya no lee el árbol (D-08; 07-hooks REQ-HOOK-41) y ``read_base``
        solo lanza ``E_CONFIG_POLICY_UNAVAILABLE`` o ``E_CONFIG_GIT_UNAVAILABLE``, así que
        hoy no ocurre: con la base válida, ``convention`` y ``deputy`` salen de ella aunque el
        árbol tenga un ``project.yaml`` inválido. Se conserva como defensa; si ocurriera, el
        problema ya lo informa el check ``project_yaml``.
        """
        exc = self.policy_error
        return self.cfg is None and exc is not None and exc.code == "E_CONFIG_INVALID"

    def _policy_reason(self) -> str:
        """Motivo de ``policy_error`` para los mensajes: ``<ref>: <reason>`` o el código."""
        exc = self.policy_error
        if exc is None:
            return ""
        details = exc.details if isinstance(exc.details, Mapping) else {}
        reason = str(details.get("reason") or exc.code)
        ref = details.get("ref")
        return f"{ref}: {reason}" if isinstance(ref, str) and ref else reason

    def _skip_no_project(self, check_id: str) -> Check:
        return Check(check_id, "skip", self.t("doctor.common.skip_no_project"))

    def _forge_skip(self, check_id: str, *, needs_repo: bool = False) -> Check | None:
        """``skip`` de un check de la forja que no se pudo hacer; ``None`` si se puede hacer."""
        if self.project is None:
            return self._skip_no_project(check_id)
        if self.forge_skip is not None:
            return Check(check_id, "skip", self.t(f"doctor.forge.skip_{self.forge_skip}", **self.forge_detail))
        if check_id != "identity" and self.ident is None:
            return Check(check_id, "skip", self.t("doctor.forge.skip_identity"))
        if needs_repo and self.caps is None:
            return Check(check_id, "skip", self.t("doctor.forge.skip_repo"))
        return None

    def _git_email(self) -> str | None:
        """Correo con que git firmaría un commit (``git var GIT_AUTHOR_IDENT``); ``None`` si no hay."""
        git = self._git()
        if git is None:
            return None
        try:
            proc = git.run("var", "GIT_AUTHOR_IDENT", check=False, read=True)
        except (GitError, GitUnavailable, OSError, ValueError):
            return None
        if proc.returncode != 0:
            return None
        m = _IDENT_EMAIL.search(proc.stdout.decode("utf-8", "replace"))
        return m.group(1).strip() if m is not None and m.group(1).strip() else None

    # -- checks ----------------------------------------------------------------------

    def python(self) -> Check:
        t = self.t
        v = sys.version_info
        version = f"{v.major}.{v.minor}.{v.micro}"
        if (v.major, v.minor) < MIN_PYTHON:
            return Check("python", "fail", t("doctor.python.fail", version=version), t("doctor.python.fix_python"))
        pin = self.cfg.core_pin if self.cfg is not None else _meta.__version__
        fix = t("doctor.python.fix_launcher", name=_meta.NAME, pin=pin)
        path = _env_get(self.env, "PATH")
        launcher = find_launcher(  # REQ-ARCH-33, sin el directorio actual
            _meta.NAME, path=path, pathext=_env_get(self.env, "PATHEXT")
        )
        if launcher is None:
            check = Check("python", "warn", t("doctor.python.warn_launcher", version=version, name=_meta.NAME), fix)
        elif os.name == "nt" and not launcher.lower().endswith(".exe"):
            check = Check(
                "python", "warn", t("doctor.python.warn_not_exe", version=version, name=_meta.NAME, launcher=launcher), fix
            )
        else:
            check = Check("python", "ok", t("doctor.python.ok", version=version, name=_meta.NAME, launcher=launcher))
        copies = packaged_copies(self.env)
        if copies:  # 05 §9.2: la copia privada de una app empaquetada de Windows (MSIX)
            app, copy_version, where = copies[0]
            note = t("doctor.python.warn_packaged", name=_meta.NAME, version=copy_version, app=app)
            note_fix = t("doctor.python.fix_packaged", name=_meta.NAME, path=where)
            check = Check("python", "warn", f"{check.message} {note}",
                          note_fix if check.fix is None else f"{check.fix} {note_fix}")
        relative = cwd_path_entries(path)  # S4: Claude Code las resuelve contra el directorio del hook
        if not relative:
            return check
        relative_fix = t("doctor.python.fix_relative_path")
        return Check(
            "python",
            "warn",
            f"{check.message} {t('doctor.python.warn_relative_path', entries=_shown_entries(relative), name=_meta.NAME)}",
            relative_fix if check.fix is None else f"{check.fix} {relative_fix}",
        )

    def git(self) -> Check:
        t = self.t
        start = self.project.root if self.project is not None else Path(self.ctx.inv.cwd)
        exe = which_outside(
            "git",
            start,
            path=_env_get(self.env, "PATH"),
            pathext=_env_get(self.env, "PATHEXT"),
            cwd=self.ctx.inv.cwd,
        )
        if exe is None:
            return Check("git", "fail", t("doctor.git.fail"), t("doctor.git.fix"))
        try:
            proc = subprocess.run(
                [exe, "--version"],
                capture_output=True,
                env=child_env(self.env, read=True),
                timeout=GIT_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return Check("git", "fail", t("doctor.git.fail_run"), t("doctor.git.fix"))
        m = _GIT_VERSION.search(proc.stdout.decode("utf-8", "replace"))
        if proc.returncode != 0 or m is None:
            return Check("git", "fail", t("doctor.git.fail_run"), t("doctor.git.fix"))
        return Check("git", "ok", t("doctor.git.ok", version=m.group(1), path=exe))

    def project_root(self) -> Check:
        t = self.t
        if self.project is None:
            return Check("project_root", "warn", t("doctor.project_root.warn"), t("doctor.project_root.fix", name=_meta.NAME))
        return Check("project_root", "ok", t("doctor.project_root.ok", path=str(self.project.root)))

    def project_yaml(self) -> Check:
        t = self.t
        if self.project is None:
            return self._skip_no_project("project_yaml")
        fix = t("doctor.common.fix_validate", name=_meta.NAME)
        if self.project_bytes is None:
            return Check("project_yaml", "fail", t("doctor.project_yaml.fail_unreadable"), fix)
        if self.cfg is None:
            return Check("project_yaml", "fail", t("doctor.project_yaml.fail", n=self.project_errors), fix)
        return Check("project_yaml", "ok", t("doctor.project_yaml.ok"))

    def roles_yaml(self) -> Check:
        t = self.t
        if self.project is None:
            return self._skip_no_project("roles_yaml")
        fix = t("doctor.common.fix_validate", name=_meta.NAME)
        if self.roles_bytes is None:
            return Check("roles_yaml", "fail", t("doctor.roles_yaml.fail_missing"), fix)
        if self.roles is None:
            return Check("roles_yaml", "fail", t("doctor.roles_yaml.fail", n=self.roles_errors), fix)
        counts = {"people": len(self.roles.people), "sections": len(self.roles.sections)}
        if self.roles_warnings:
            return Check("roles_yaml", "warn", t("doctor.roles_yaml.warn", n=self.roles_warnings, **counts), fix)
        return Check("roles_yaml", "ok", t("doctor.roles_yaml.ok", **counts))

    def convention(self) -> Check:
        t = self.t
        if self.project is None:
            return self._skip_no_project("convention")
        cfg = self.policy_cfg
        if cfg is None:
            if self._tree_project_invalid():
                return Check("convention", "skip", t("doctor.convention.skip"))
            return Check(
                "convention",
                "fail",
                t("doctor.convention.fail_policy", reason=self._policy_reason()),
                t("doctor.common.fix_policy"),
            )
        result = compat(cfg)
        params = {"convention": cfg.convention, "installed": _meta.__version__, "pin": cfg.core_pin}
        if result.level == "error":
            fix = t.get(f"error.{result.code}.hint") if result.code else None
            return Check("convention", "fail", t("doctor.convention.fail", **params), fix)
        if result.level == "warning":
            return Check("convention", "warn", t("doctor.convention.warn", **params), t("doctor.python.fix_launcher", name=_meta.NAME, pin=cfg.core_pin))
        return Check("convention", "ok", t("doctor.convention.ok", **params))

    def remote(self) -> Check:
        t = self.t
        if self.project is None:
            return self._skip_no_project("remote")
        cfg = self.cfg
        if cfg is None:
            return Check("remote", "skip", t("doctor.remote.skip"))
        try:
            git = self.ctx.git()
        except OpError:
            return Check("remote", "skip", t("doctor.remote.skip_git"))
        url = git.remote_url("origin")
        if url is None:
            return Check("remote", "fail", t("doctor.remote.fail"), t("doctor.remote.fix_missing"))
        ref = f"origin/{cfg.base_branch}"
        if cfg.forge_type == "github" and not matches_repo(url, cfg.forge_repo):
            return Check(
                "remote", "warn", t("doctor.remote.warn_mismatch", url=url, repo=cfg.forge_repo), t("doctor.remote.fix_mismatch")
            )
        if git.rev_parse(f"refs/remotes/{ref}") is None:
            return Check("remote", "warn", t("doctor.remote.warn_ref", ref=ref), t("doctor.remote.fix_ref"))
        return Check("remote", "ok", t("doctor.remote.ok", repo=cfg.forge_repo, ref=ref))

    def identity(self) -> Check:
        """La cuenta y el token (08 §5.2; 10-seguridad REQ-SEC-27 y REQ-SEC-29) y el correo (D-44)."""
        skipped = self._forge_skip("identity")
        if skipped is not None:
            return skipped
        t = self.t
        link = token_link(self._repo())
        exc = self.ident_error
        if exc is not None:
            if exc.kind == "no_token":
                sources = ", ".join(f"`{s}`" for s in token_sources(_meta.ENV_PREFIX)[:-1])
                return Check("identity", "fail", t("doctor.identity.fail_no_token", sources=sources),
                             t("doctor.identity.fix_token", url=link))
            if exc.kind == "auth":
                return Check("identity", "fail", t("doctor.identity.fail_auth"), t("doctor.identity.fix_token", url=link))
            return Check("identity", "fail", t("doctor.identity.fail_other", kind=exc.kind), exc.hint)
        ident = self.ident
        assert ident is not None
        kind = t(f"doctor.identity.kind.{ident.token_kind}")
        message = t("doctor.identity.ok", login=ident.login, kind=kind, source=ident.token_source,
                    scope=t(f"doctor.identity.scope.{ident.token_scope}"))
        if ident.kind == "ci":
            return Check("identity", "ok", message)
        problems: list[str] = []
        fixes: list[str] = []
        if ident.token_kind not in QUIET_TOKEN_KINDS:  # REQ-SEC-29
            problems.append(t("doctor.identity.warn_kind"))
            fixes.append(t("doctor.identity.fix_token", url=link))
        elif ident.token_scope == "owner_wide":
            problems.append(t("doctor.identity.warn_owner_wide"))
            fixes.append(t("doctor.identity.fix_token", url=link))
        if ident.token_scope == "broad":  # REQ-FORGE-39
            self.warnings.append(
                make_warning("W_AUTH_BROAD_TOKEN", t, details={"reason": "token_scope", "token_kind": ident.token_kind})
            )
        if ident.oauth_scopes and "workflow" in ident.oauth_scopes:  # REQ-FORGE-40
            self.warnings.append(make_warning("W_AUTH_WORKFLOW_SCOPE", t))
            problems.append(t("doctor.identity.warn_workflow"))
        noreply = noreply_email(ident.login, ident.id)
        email = self._git_email()
        if not is_noreply(email):  # D-44: la historia sellada no se reescribe
            problems.append(t("doctor.identity.warn_git_email") if email else t("doctor.identity.warn_git_email_missing"))
            fixes.append(t("doctor.identity.fix_git_email", email=noreply))
        if self.person is not None and self.person.email and not is_noreply(self.person.email):
            problems.append(t("doctor.identity.warn_roles_email"))  # va en el Signed-off-by (REQ-TRACE-24)
            fixes.append(t("doctor.identity.fix_roles_email", email=noreply))
        if not problems:
            return Check("identity", "ok", message)
        return Check("identity", "warn", " ".join([message, *problems]), " ".join(dict.fromkeys(fixes)))

    def membership(self) -> Check:
        """Estar en ``roles.yaml`` de la base y tener escritura en el repositorio (06 REQ-PERM-28)."""
        skipped = self._forge_skip("membership", needs_repo=True)
        if skipped is not None:
            return skipped
        t = self.t
        ident, caps = self.ident, self.caps
        assert ident is not None and caps is not None
        if ident.kind == "ci":
            return Check("membership", "ok", t("doctor.membership.ok_ci"))
        own = self._own_membership()
        problems: list[str] = []
        fixes = [own.fix] if own.fix else []
        roles = self.policy_roles
        assert roles is not None
        if self.permissions is not None:  # REQ-PERM-28: los code owners necesitan escritura explícita
            lacking = sorted(login for login, role in self.permissions.items()
                             if role not in WRITE_PERMISSIONS and not same_login(login, ident.login))
            if lacking:
                problems.append(t("doctor.membership.warn_collaborators", logins=", ".join(f"@{x}" for x in lacking)))
                fixes.append(t("doctor.profile.fix_init", name=_meta.NAME))
        if self.outside:  # REQ-PERM-55: colaboradoras externas, que solo pueden usar un token amplio
            logins = sorted(p.login for p in roles.people if any(same_login(p.login, o) for o in self.outside))
            if logins:
                self.warnings.append(make_warning("W_AUTH_BROAD_TOKEN", t,
                                                  details={"reason": "outside_collaborator", "logins": logins}))
                problems.append(t("doctor.membership.warn_outside", logins=", ".join(f"@{x}" for x in logins)))
                fixes.append(t("doctor.membership.fix_outside", name=_meta.NAME))
        if not problems:
            return own
        status: Status = "fail" if own.status == "fail" else "warn"
        return Check("membership", status, " ".join([own.message, *problems]), " ".join(dict.fromkeys(fixes)))

    def _own_membership(self) -> Check:
        """La persona que consulta: en ``roles.yaml`` de la base, activa y con escritura."""
        t = self.t
        ident, caps = self.ident, self.caps
        assert ident is not None and caps is not None
        permission = caps.viewer_permission
        person = self.person
        if person is None:
            if self.renamed_from is not None:  # 08 §6.4
                return Check("membership", "warn", t("doctor.membership.warn_renamed", previous=self.renamed_from),
                             t("doctor.membership.fix_renamed", name=_meta.NAME))
            return Check("membership", "warn", t("doctor.membership.warn_not_member", login=ident.login),
                         t("doctor.membership.fix_not_member", name=_meta.NAME))
        if not person.active:
            return Check("membership", "warn", t("doctor.membership.warn_inactive", login=person.login),
                         t("doctor.membership.fix_not_member", name=_meta.NAME))
        if permission not in WRITE_PERMISSIONS:
            return Check("membership", "fail", t("doctor.membership.fail_permission", role=person.role, permission=permission),
                         t("doctor.membership.fix_permission", name=_meta.NAME))
        return Check("membership", "ok", t("doctor.membership.ok", role=person.role, permission=permission))

    def forge_reachable(self) -> Check:
        """El repositorio en la forja (08 §3.1); un 403 de la org se explica (08 §5.4)."""
        skipped = self._forge_skip("forge_reachable")
        if skipped is not None:
            return skipped
        t = self.t
        repo = self._repo()
        exc = self.caps_error
        if isinstance(exc, BaseNotDefault):  # REQ-PERM-24
            base = self.policy_cfg.base_branch if self.policy_cfg is not None else ""
            return Check("forge_reachable", "fail", t("doctor.forge_reachable.fail_base", base=base, default=exc.default_branch),
                         t("doctor.forge_reachable.fix_base", base=base))
        if exc is not None:
            if exc.kind == "not_found":
                return Check("forge_reachable", "fail", t("doctor.forge_reachable.fail_not_found", repo=repo),
                             t("doctor.forge_reachable.fix_not_found", url=token_link(repo)))
            if exc.kind == "permission":
                return Check("forge_reachable", "fail", t("doctor.forge_reachable.fail_permission", repo=repo),
                             t("doctor.forge_reachable.fix_permission"))
            if exc.kind == "auth":
                return Check("forge_reachable", "fail", t("doctor.identity.fail_auth"),
                             t("doctor.identity.fix_token", url=token_link(repo)))
            return Check("forge_reachable", "fail", t("doctor.forge_reachable.fail_other", repo=repo, kind=exc.kind), exc.hint)
        caps = self.caps
        assert caps is not None
        message = t("doctor.forge_reachable.ok", repo=repo, visibility=t(f"doctor.forge_reachable.visibility.{caps.visibility}"),
                    branch=caps.default_branch, permission=caps.viewer_permission)
        problems: list[str] = []
        fixes: list[str] = []
        if self.labels is not None:  # 06 §4.5
            present = {lab.name.casefold() for lab in self.labels}
            missing = [name for name, _color in LABELS if name.casefold() not in present]
            if missing:
                problems.append(t("doctor.forge_reachable.warn_labels", labels=", ".join(missing)))
                fixes.append(t("doctor.profile.fix_init", name=_meta.NAME))
        for level in ("repo", "org"):  # REQ-PERM-27: el token de los workflows lee y no aprueba PR
            perms = self.workflow.get(level)
            if perms is not None and (perms.default_workflow_permissions != "read" or perms.can_approve_pull_request_reviews):
                problems.append(t(f"doctor.forge_reachable.warn_workflow_{level}"))
                fixes.append(t("doctor.profile.fix_init", name=_meta.NAME) if level == "repo"
                             else t("doctor.forge_reachable.fix_owner"))
        if self.org:  # 06 §4.10 (REQ-PERM-37); 10-seguridad §9.2 (REQ-SEC-71)
            differing = [f"{k}={_shown(self.org[k])}" for k, v in ORG_RECOMMENDED.items() if k in self.org and self.org[k] != v]
            if differing:
                problems.append(t("doctor.forge_reachable.warn_org", settings=", ".join(differing)))
                fixes.append(t("doctor.forge_reachable.fix_owner"))
        restriction = actions_restriction(self.actions.get("org"), self.actions.get("repo"), self._workflow_uses(),
                                          org_owned=caps.owner_type == "Organization")
        if restriction is not None and restriction["level"] is not None:  # REQ-OPS-64: desconocida, no avisa
            self.warnings.append(make_warning("W_FORGE_ACTIONS_RESTRICTED", t, details=restriction))
            missing_uses = ", ".join(str(u) for u in restriction["missing"])  # type: ignore[union-attr]
            problems.append(t("doctor.forge_reachable.warn_actions", level=restriction["level"], missing=missing_uses))
            fixes.append(t("doctor.forge_reachable.fix_owner"))
        if not problems:
            return Check("forge_reachable", "ok", message)
        return Check("forge_reachable", "warn", " ".join([message, *problems]), " ".join(dict.fromkeys(fixes)))

    def _workflow_uses(self) -> list[str]:
        """Las acciones que usan los workflows del árbol (06 §4.3: con su SHA, del render)."""
        if self.tree is None or self.read is None:
            return []
        try:
            entries = self.tree.entries()
        except (OpError, OSError, ValueError, LookupError, subprocess.SubprocessError):
            return []
        files = {rel: data for rel in entries if rel.startswith(".github/workflows/") and (data := self.read(rel)) is not None}
        return template_actions(files)

    def profile(self) -> Check:
        """El perfil de 08 §3.3 y lo que dice de él 06 §5 y §4.7 (REQ-PERM-53, REQ-PERM-60)."""
        skipped = self._forge_skip("profile", needs_repo=True)
        if skipped is not None:
            return skipped
        t = self.t
        caps, cfg = self.caps, self.policy_cfg
        assert caps is not None and cfg is not None
        fix = t("doctor.profile.fix_init", name=_meta.NAME)
        if caps.profile == "advisory":
            self.warnings.append(make_warning("W_FORGE_PROFILE_ADVISORY", t))
            line = t("doctor.profile.advisory_line")  # 06 §5, compensación 4: la línea fija
            if caps.rules_available is None:  # REQ-FORGE-14
                self.warnings.append(make_warning("W_FORGE_CAPABILITY_UNKNOWN", t, details={"reason": "inconclusive"}))
                return Check("profile", "warn", f"{t('doctor.profile.warn_unknown')} {line}",
                             t("doctor.profile.fix_unknown", name=_meta.NAME))
            if cfg.advisory_acceptance is None:  # REQ-PERM-40 y REQ-PERM-41: init no se ejecutó
                return Check("profile", "warn", f"{t('doctor.profile.warn_not_accepted')} {line}", fix)
            return Check("profile", "ok", line)
        problems: list[str] = []
        if not caps.push_rulesets:  # REQ-PERM-53
            self.warnings.append(make_warning("W_FORGE_CHECKS_SPOOFABLE", t))
        if cfg.advisory_acceptance is not None:  # 06 §5, compensación 5: la mejora detectada
            problems.append(t("doctor.profile.warn_upgrade"))
        checks = required_checks(cfg.format)
        rules = caps.base_rules
        missing: list[str] = []
        if rules is None or rules.source == "none":  # 08 §3.3
            self.warnings.append(make_warning("W_FORGE_RULES_NOT_APPLIED", t))
            problems.append(t("doctor.profile.warn_no_rules", base=caps.default_branch))
        else:
            missing = base_rules_missing(rules, checks=checks, individual=cfg.mode == "individual")
            if missing:  # REQ-PERM-39; con mode: group sin aprobaciones, REQ-PERM-60
                self.warnings.append(make_warning("W_FORGE_RULES_NOT_APPLIED", t, details={"missing": missing}))
                problems.append(t("doctor.profile.warn_missing", missing=", ".join(missing)))
            if rules.source == "classic":
                problems.append(t("doctor.profile.warn_classic", ruleset=BASE_RULESET))
        if self.rulesets is not None:
            problems += self._ruleset_problems(caps, cfg, checks, effective_missing=bool(missing))
        if problems:
            return Check("profile", "warn", " ".join([t("doctor.profile.enforced"), *problems]), fix)
        return Check("profile", "ok", t("doctor.profile.ok_enforced", ruleset=BASE_RULESET, checks=", ".join(checks)))

    def _ruleset_problems(self, caps: Capabilities, cfg: ProjectConfig, checks: Sequence[str], *,
                          effective_missing: bool) -> list[str]:
        """Los rulesets de 06 §4.7 y §4.7b contra los que aplica ``init`` (REQ-PERM-32, 21, 52 y 53)."""
        t = self.t
        assert self.rulesets is not None
        by_name = {r.name: r for r in self.rulesets}
        individual = cfg.mode == "individual"
        out: list[str] = []
        base = by_name.get(BASE_RULESET)
        if base is None or base.enforcement != "active":
            if not any(w.code == "W_FORGE_RULES_NOT_APPLIED" for w in self.warnings):
                self.warnings.append(make_warning("W_FORGE_RULES_NOT_APPLIED", t, details={"ruleset": BASE_RULESET}))
            out.append(t("doctor.profile.warn_ruleset_missing", ruleset=BASE_RULESET) if base is None else
                       t("doctor.profile.warn_ruleset_inactive", ruleset=BASE_RULESET, enforcement=base.enforcement))
        else:
            expected = _without_bypass(base_ruleset(caps.default_branch, checks=checks, individual=individual, bypass_id=None))
            if not effective_missing and not covers(base.payload, expected):  # REQ-PERM-32: exactamente §4.7
                out.append(t("doctor.profile.warn_ruleset_differs", ruleset=BASE_RULESET))
            actors = base.payload.get("bypass_actors")  # GitHub solo los muestra a quien puede editarlo
            if isinstance(actors, list) and actors and not self._bypass_ok(actors, individual):
                out.append(t("doctor.profile.warn_bypass", ruleset=BASE_RULESET))
        task = by_name.get(TASK_RULESET)  # REQ-PERM-52
        if task is None or not covers(task.payload, _without_bypass(task_ruleset())):
            out.append(t("doctor.profile.warn_task_ruleset", ruleset=TASK_RULESET))
        if caps.push_rulesets:  # REQ-PERM-53
            workflows = by_name.get(WORKFLOWS_RULESET)
            if workflows is None or workflows.enforcement != "active":
                out.append(t("doctor.profile.warn_workflows_ruleset", ruleset=WORKFLOWS_RULESET))
        return out

    def _bypass_ok(self, actors: Sequence[object], individual: bool) -> bool:
        """Sin suplente, solo el ``lead`` con ``pull_request`` (REQ-PERM-19, 20); si no, ninguno (REQ-PERM-21, 59)."""
        roles = self.policy_roles
        if individual or roles is None or roles.deputy:
            return False
        return len(actors) == 1 and all(
            isinstance(a, Mapping) and a.get("actor_type") == "User" and a.get("bypass_mode") == "pull_request"
            for a in actors
        )

    def merge_settings(self) -> Check:
        """Los ajustes del repositorio contra 06 §4.2; los de 11-trazabilidad §3.2 fallan (REQ-TRACE-36)."""
        skipped = self._forge_skip("merge-settings", needs_repo=True)
        if skipped is not None:
            return skipped
        t = self.t
        caps, settings = self.caps, self.settings
        assert caps is not None
        if settings is None or any(k not in settings for k in TRACE_SETTINGS):  # piden Contents W
            return Check("merge-settings", "skip", t("doctor.merge-settings.skip_unreadable"))
        desired = desired_settings(caps.visibility)
        trace = [k for k in TRACE_SETTINGS if settings[k] != desired[k]]
        other = [k for k in desired if k not in TRACE_SETTINGS and k in settings and settings[k] != desired[k]]
        fix = t("doctor.profile.fix_init", name=_meta.NAME)
        parts = []
        if trace:
            parts.append(t("doctor.merge-settings.fail", settings=_pairs(settings, trace)))
        if other:
            parts.append(t("doctor.merge-settings.warn", settings=_pairs(settings, other)))
        if not parts:
            return Check("merge-settings", "ok", t("doctor.merge-settings.ok"))
        return Check("merge-settings", "fail" if trace else "warn", " ".join(parts), fix)

    def half_states(self) -> Check:
        """Estados a medias (REQ-OPS-27) y el último run de ``expire-claims`` (09-ci REQ-CI-31)."""
        skipped = self._forge_skip("half_states", needs_repo=True)
        if skipped is not None:
            return skipped
        t = self.t
        if self.half is None and self.expire is None:
            return Check("half_states", "skip", t("doctor.half_states.skip_board"))
        base = self.policy_cfg.base_branch if self.policy_cfg is not None else ""
        parts: list[str] = []
        fixes: list[str] = []
        if self.half:
            items = ", ".join(f"#{h['task']} {h['kind']} → {h['suggested_op']}" for h in self.half)
            more = self.half_total - len(self.half)
            parts.append(t("doctor.half_states.warn", items=items + (f", … (+{more})" if more else "")))
            fixes.append(t("doctor.half_states.fix", name=_meta.NAME))
        if self.expire is not None:
            run = self.expire[0]
            now = self._server_now()
            if run is None:
                parts.append(t("doctor.half_states.warn_expire_never", base=base))
                fixes.append(t("doctor.half_states.fix_expire"))
            elif run.created_at is not None and now - run.created_at > EXPIRE_STALE:
                parts.append(t("doctor.half_states.warn_expire_stale", base=base, days=(now - run.created_at).days))
                fixes.append(t("doctor.half_states.fix_expire"))
        if parts:
            return Check("half_states", "warn", " ".join(parts), " ".join(fixes))
        message = t("doctor.half_states.ok") if self.half is not None else t("doctor.half_states.skip_board")
        if self.expire is not None and self.expire[0] is not None:
            message = f"{message} {t('doctor.half_states.ok_expire', base=base)}"
        return Check("half_states", "ok", message)

    def _server_now(self) -> datetime:
        """La hora de la forja (cabecera ``Date``, como ``expire-claims``) o, sin ella, la local."""
        moment = self.forge.server_time() if self.forge is not None else None
        return moment if moment is not None else self.ctx.inv.now()

    def skipped(self, check_id: str) -> Check:
        return Check(check_id, "skip", self.t(f"doctor.{check_id}.skip"))

    def deputy(self) -> Check:
        t = self.t
        if self.project is None:
            return self._skip_no_project("deputy")
        roles = self.policy_roles
        if roles is None:
            if self._tree_project_invalid():
                return Check("deputy", "skip", t("doctor.deputy.skip_project"))
            return Check(
                "deputy",
                "fail",
                t("doctor.deputy.fail_policy", reason=self._policy_reason()),
                t("doctor.common.fix_policy"),
            )
        cfg = self.policy_cfg
        if cfg is not None and cfg.mode == "individual":  # 00-marco D-57: sin suplente ni alerta
            return Check("deputy", "ok", t("doctor.deputy.ok_individual"))
        if roles.deputy:
            return Check("deputy", "ok", t("doctor.deputy.ok", deputy=roles.deputy))
        self.warnings.append(make_warning("W_PERM_DEPUTY_MISSING", t))
        return Check("deputy", "warn", t("doctor.deputy.warn"), t("doctor.deputy.fix", name=_meta.NAME))

    def codeowners_sync(self) -> Check:
        t = self.t
        if self.project is None or self.read is None:
            return self._skip_no_project("codeowners_sync")
        regen = regenerate(self.read, t=t)
        if regen.expected is None:
            return Check("codeowners_sync", "skip", t("doctor.codeowners_sync.skip"))
        states = compare_files(self.read, regen.expected, exists=self.exists, normalize_eol=True)
        pending = [s.path for s in states if s.status != "same"]
        if pending:
            return Check(
                "codeowners_sync",
                "fail",
                t("doctor.codeowners_sync.fail", paths=", ".join(pending)),
                t("doctor.codeowners_sync.fix", name=_meta.NAME),
            )
        if self.codeowners:  # 06 REQ-PERM-31: errores que informa la forja en la base
            errors = "; ".join(self.codeowners[:3]) + ("; …" if len(self.codeowners) > 3 else "")
            base = self.policy_cfg.base_branch if self.policy_cfg is not None else ""
            return Check("codeowners_sync", "fail", t("doctor.codeowners_sync.fail_forge", base=base, errors=errors),
                         t("doctor.codeowners_sync.fix_forge", name=_meta.NAME))
        return Check("codeowners_sync", "ok", t("doctor.codeowners_sync.ok"))

    def generated_files(self) -> Check:
        t = self.t
        if self.project is None or self.tree is None:
            return self._skip_no_project("generated_files")
        try:
            entries = self.tree.entries()
        except (OpError, OSError, ValueError, LookupError, subprocess.SubprocessError):
            return Check("generated_files", "skip", t("doctor.generated_files.skip"))
        skills = [p for p in entries if is_skill_path(p)]
        if skills:
            return Check("generated_files", "fail", t("doctor.generated_files.fail", n=len(skills)), t("doctor.generated_files.fix"))
        return Check("generated_files", "ok", t("doctor.generated_files.ok"))

    def mcp_config(self) -> Check:
        """``.mcp.json`` del árbol contra 10-seguridad SB04 (01-arquitectura §10.3)."""
        t = self.t
        if self.project is None or self.read is None:
            return self._skip_no_project("mcp_config")
        raw = self.read(MCP_JSON_PATH)
        if raw is None:  # sin él, Claude Code no conecta el servidor del proyecto
            return Check("mcp_config", "warn", t("doctor.mcp_config.warn_missing", name=_meta.NAME),
                         t("doctor.mcp_config.fix_missing", name=_meta.NAME))
        problem = mcp_problem(raw)
        if problem is not None:
            return Check("mcp_config", "fail", t(f"doctor.mcp_config.fail_{problem}", name=_meta.NAME),
                         t("doctor.mcp_config.fix", name=_meta.NAME))
        return Check("mcp_config", "ok", t("doctor.mcp_config.ok", name=_meta.NAME))

    def cache(self) -> Check:
        t = self.t
        if self.project is None:
            return self._skip_no_project("cache")
        root = self.ctx.store().root
        shown = str(root)
        try:
            st = os.lstat(root)
        except FileNotFoundError:
            return Check("cache", "ok", t("doctor.cache.ok_absent"))
        except OSError:
            return Check("cache", "fail", t("doctor.cache.fail", path=shown), t("doctor.cache.fix_fail", path=shown))
        if is_link(st) or not stat.S_ISDIR(st.st_mode):
            return Check("cache", "fail", t("doctor.cache.fail", path=shown), t("doctor.cache.fix_fail", path=shown))
        pending = _count_files(root / "work")
        if pending:  # REQ-ARCH-59: work/ guarda trabajo recuperable
            return Check("cache", "warn", t("doctor.cache.warn_work", n=pending), t("doctor.cache.fix_work"))
        if not _gitignore_ok(root / ".gitignore"):
            return Check("cache", "warn", t("doctor.cache.warn_gitignore"), t("doctor.cache.fix_gitignore"))
        return Check("cache", "ok", t("doctor.cache.ok", path=shown))

    def evidence_tools(self) -> Check:
        """Extra ``evidence`` para verificar las pruebas TSA y OTS (05 §9.2; REQ-ARCH-99).

        Funciona también fuera de un proyecto. Solo busca los módulos (``find_spec``): no
        los importa, así que ``doctor`` no carga el extra.
        """
        from ..evidence.verify import missing_extra  # perezoso: solo este check usa la capa

        t = self.t
        missing = missing_extra()
        if not missing:
            return Check("evidence_tools", "ok", t("doctor.evidence_tools.ok"))
        self.warnings.append(make_warning("W_CONFIG_TOOL_MISSING", t))
        pin = self.cfg.core_pin if self.cfg is not None else _meta.__version__
        return Check(
            "evidence_tools",
            "warn",
            t("doctor.evidence_tools.warn", missing=", ".join(missing)),
            t("doctor.evidence_tools.fix", name=_meta.NAME, pin=pin),
        )

    def evidence_signing(self) -> Check:
        """La firma del proyecto (05 §9.2; 00-marco D-72): el modo, una llave vigente y el environment.

        Con la política de la base (D-08). El environment se lee con el token de uso diario
        (Actions R); sus secretos no: si falta la llave, lo dicen ``init`` y el resumen del job.
        """
        from ..domain.signing import current_keys

        check_id, t = "evidence_signing", self.t
        if self.project is None:
            return self._skip_no_project(check_id)
        cfg = self.policy_cfg
        if cfg is None:
            return Check(check_id, "skip", t("doctor.evidence_signing.skip_policy", reason=self._policy_reason()))
        signing = cfg.evidence_signing
        guide = t("doctor.evidence_signing.fix_guide")
        if signing.mode == "none":
            return Check(check_id, "warn", t("doctor.evidence_signing.warn_none"), guide)
        keys = current_keys(signing.keys, self.ctx.inv.now())
        if not keys:
            return Check(check_id, "fail", t("doctor.evidence_signing.fail_no_key", mode=signing.mode), guide)
        summary = t("doctor.evidence_signing.ok", mode=signing.mode, keys=", ".join(k.id for k in keys))
        forge, caps = self.forge, self.caps
        if forge is None or caps is None:
            return Check(check_id, "ok", summary + " " + t("doctor.evidence_signing.forge_unchecked"))
        if environments_available(caps) is False:
            if signing.mode == "ci":
                return Check(check_id, "fail", t("doctor.evidence_signing.fail_no_environments"),
                             t("doctor.evidence_signing.fix_no_environments"))
            return Check(check_id, "ok", summary)
        name, base = EVIDENCE_ENVIRONMENT, cfg.base_branch
        fix_init = t("doctor.evidence_signing.fix_init", name=_meta.NAME)
        found = self._read(lambda: (forge.environment(name),))
        if found is None:  # lo que no se puede leer queda como desconocido (D-65)
            return Check(check_id, "ok", summary + " " + t("doctor.evidence_signing.environment_unreadable",
                                                           environment=name))
        env = found[0]
        if env is None:
            if signing.mode == "ci":
                return Check(check_id, "fail", t("doctor.evidence_signing.fail_environment_missing", environment=name),
                             fix_init)
            return Check(check_id, "ok", summary)
        if not env.only_branch(base):
            return Check(check_id, "fail", t("doctor.evidence_signing.fail_environment_rules", environment=name,
                                              base=base), fix_init)
        return Check(check_id, "ok", summary + " " + t("doctor.evidence_signing.environment_ok", environment=name,
                                                       base=base))

    # -- corrida -------------------------------------------------------------------

    def checks(self) -> list[Check]:
        out: list[Check] = []
        for check_id in CHECK_ORDER:
            if check_id in SKIPPED:
                out.append(self.skipped(check_id))
            else:
                out.append(getattr(self, check_id.replace("-", "_"))())
        return out

    def identity_data(self) -> tuple[dict[str, object], str | None, bool]:
        """``(identity, role, is_deputy)``: la identidad de la forja o de la caché y su persona."""
        person = self.person
        if person is None:
            return dict(self.shown_identity), None, False
        roles = self.policy_roles
        role = person.role if person.active else None
        deputy = roles.deputy if roles is not None else None
        is_deputy = bool(role and deputy and person.login.lower() == deputy.lower())
        return dict(self.shown_identity), role, is_deputy


def _count_files(folder: Path, limit: int = 1000) -> int:
    """Entradas bajo ``folder`` sin seguir enlaces (hasta ``limit``); 0 si no existe."""
    count = 0
    stack = [folder]
    while stack and count < limit:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    else:
                        count += 1
        except OSError:
            continue
    return count


def _gitignore_ok(path: Path) -> bool:
    """``True`` si ``.gitignore`` de la caché ignora todo (una línea ``*``)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return any(line.strip() == "*" for line in text.splitlines())


def _shown(value: object) -> str:
    return str(value).lower() if isinstance(value, bool) else str(value)


def _pairs(settings: Mapping[str, object], keys: Sequence[str]) -> str:
    """``allow_merge_commit=true, …``: los valores actuales de ``keys``."""
    return ", ".join(f"{k}={_shown(settings[k])}" for k in keys)


def _without_bypass(ruleset: Mapping[str, object]) -> dict[str, object]:
    """El ruleset sin ``bypass_actors``, que GitHub no muestra a quien no puede editarlo."""
    return {k: v for k, v in ruleset.items() if k != "bypass_actors"}


def _size(result: Result) -> int:
    return len(json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":")))


def _login_ok(login: object) -> bool:
    return isinstance(login, str) and re.match(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?$", login) is not None


def run(ctx: OpContext, *, refresh: bool = False) -> Result:
    """``doctor [--refresh]``: diagnóstico del entorno, del proyecto y de la forja."""
    if not isinstance(refresh, bool):
        raise OpError("E_VALIDATION_INPUT", details={"errors": [{"param": "refresh", "reason": "type"}]})
    # La identidad y las capacidades se leen siempre de nuevo (REQ-FORGE-18; la clasificación
    # del token de 08 §5.2 la mide doctor); --refresh fuerza además el tablero de half_states.
    doc = _Doctor(ctx, refresh=refresh)
    t = doc.t
    checks = doc.checks()
    identity, role, is_deputy = doc.identity_data()
    cfg = doc.policy_cfg
    warnings = list(doc.warnings)
    if doc.caps is None:  # D-34 (l): sin detección, advisory
        warnings.append(make_warning("W_FORGE_CAPABILITY_UNKNOWN", t, details={"reason": "forge_not_consulted"}))
    if doc.forge_skip == "offline" or doc.net_failed:
        warnings.append(make_warning("W_NET_OFFLINE", t))
    failed = [c.id for c in checks if c.status == "fail"]
    error = make_error("E_CONFIG_DOCTOR_FAILED", t, details={"failed": failed}) if failed else None

    def build(items: Sequence[Check], extra: Sequence[WarningObj]) -> Result:
        data = {
            "core_version": _meta.__version__,
            "project_root": str(doc.project.root) if doc.project is not None else None,
            "convention": {
                "project": cfg.convention if cfg is not None else None,
                "supported": list(_meta.SUPPORTED_CONVENTIONS),
            },
            "identity": identity,
            "role": role,
            "is_deputy": is_deputy,
            "profile": doc.caps.profile if doc.caps is not None else PROFILE,
            "checks": [c.to_dict() for c in items],
            "half_states": list(doc.half or []),
        }
        all_warnings = [*warnings, *extra]
        if error is not None:
            return fail(OP, error, data=data, warnings=all_warnings)
        return ok(OP, data, all_warnings)

    result = build(checks, ())
    if _size(result) <= BUDGET:
        return result
    # 05 §7.1: se recortan primero los checks con ok, desde el final.
    kept = list(checks)
    truncated = [make_warning("W_OUTPUT_TRUNCATED", t, details={"budget": BUDGET, "trimmed": ["checks"]})]
    for i in range(len(kept) - 1, -1, -1):
        if kept[i].status == "ok":
            del kept[i]
            result = build(kept, truncated)
            if _size(result) <= BUDGET:
                break
    return result


def render_text(result: Result, t: Translator, *, fmt: str = "text") -> str:
    """Vista legible de ``data_doctor``: una línea por check y su solución."""
    del fmt
    data = result.data or {}
    root = data.get("project_root")
    lines = [
        t(
            "doctor.render.header",
            version=data.get("core_version", ""),
            root=root if root else t("doctor.render.no_project"),
            profile=data.get("profile", ""),
        )
    ]
    for item in data.get("checks", []):
        status = str(item.get("status"))
        lines.append(t("doctor.render.line", status=t(f"doctor.render.status.{status}"), id=item.get("id"), message=item.get("message")))
        fix = item.get("fix")
        if fix:
            lines.append("  " + t("doctor.render.fix", fix=fix))
    return "\n".join(lines)
