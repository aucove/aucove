# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Interfaz de la forja: tipos, Protocol y errores (08-forja §2 y §2.1).

Transcripción de 08-forja §2 y §2.1, que ya incluyen ``pulls_for_commit`` y
``last_successful_run`` (00-marco D-34 h; 09-ci REQ-CI-33), ``successful_runs`` (D-34,
enmienda de D-45, que usa ``ci seal``), ``upsert_check_run`` (D-56, REQ-FORGE-70, desde
H3) y, al final de ``PullRequest`` y con valor por
defecto, ``same_repo`` (04-tareas §1.1 y REQ-TASK-5, PR desde forks, y la invariante de
12-mvp §3.2) y ``merged_by`` (D-45). Solo usa la biblioteca estándar: el cliente HTTP y
el almacén TLS los importa únicamente la implementación GitHub (01-arquitectura §3.1).

``ForgeAdapter`` no tiene métodos para fusionar PR, borrar Issues ni cambiar la
configuración del repositorio (REQ-FORGE-3). Esas escrituras están en ``ForgeAdmin``
(08 §2.2; 00-marco D-62), que solo construye ``init`` con el token de administración. Sí
lee esa configuración con el token de la persona, solo con GET, para ``doctor`` (00-marco
D-65; REQ-FORGE-73): las lecturas tienen las mismas firmas que en ``ForgeAdmin``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal, Protocol, runtime_checkable

__all__ = [
    "NULL_SHA",
    "ERROR_CLASSES",
    "RETRYABLE_BY_KIND",
    "Tristate",
    "ForgeKind",
    "TokenSource",
    "TokenKind",
    "RefNamespace",
    "token_sources",
    "RepoRef",
    "Identity",
    "Label",
    "Ref",
    "RefActivity",
    "CommitInfo",
    "Issue",
    "LabelEvent",
    "PullRequest",
    "Review",
    "Comment",
    "FileContent",
    "FileChange",
    "ChangedFile",
    "Comparison",
    "CheckRun",
    "Checks",
    "WorkflowRun",
    "BaseRules",
    "Capabilities",
    "HumanConfirmation",
    "ForgeAdapter",
    "REPO_SETTINGS",
    "ORG_SETTINGS",
    "AdminLevel",
    "CollaboratorPermission",
    "rules_summary",
    "ActionsPolicy",
    "WorkflowPermissions",
    "RulesetInfo",
    "EnvironmentInfo",
    "SecretScope",
    "ForgeAdmin",
    "ForgeError",
    "NoTokenError",
    "ForgeAuthError",
    "ForgePermissionError",
    "LoginChangedError",
    "ForgeNotFound",
    "RefAlreadyExists",
    "RefConflict",
    "MergeConflict",
    "PullRequestExists",
    "NoCommitsBetween",
    "ForgeValidationError",
    "BaseNotDefault",
    "ForgeUnsupported",
    "RateLimited",
    "ForgeUnavailable",
    "NetworkTimeout",
    "NetworkUnreachable",
    "NetworkTLSError",
]

# ---------------------------------------------------------------------------
# Alias de tipos (08 §2)
# ---------------------------------------------------------------------------

Tristate = bool | None  # None = no determinado
ForgeKind = Literal["github", "gitlab", "forgejo", "fake"]
# Nombre de la variable de entorno que dio el token, o "gh_auth_token" (§5.1).
# 08 §2 lo escribe como Literal con el nombre del núcleo en la primera variable;
# aquí es str para no escribir ese nombre fuera de _meta (01 REQ-ARCH-19). Los
# valores válidos, en su orden, los da token_sources(_meta.ENV_PREFIX).
TokenSource = str
TokenKind = Literal[
    "fine_grained_pat",
    "classic_pat",
    "oauth",
    "app_user",
    "app_installation",
    "proxy",
    "unknown",
]
# refs/heads/… (ramas) o refs/tags/… (p. ej. archive/task-<n>-<k>)
RefNamespace = Literal["heads", "tags"]

# sha de RefActivity.before/after al crear o borrar una ref ("000…0", 08 §2)
NULL_SHA = "0" * 40

_ENV_PREFIX_RE = re.compile(r"[A-Z][A-Z0-9_]*_")


def token_sources(env_prefix: str) -> tuple[TokenSource, ...]:
    """Fuentes del token en el orden de REQ-FORGE-37 (08 §5.1).

    La primera es la variable propia, derivada del prefijo de entorno del
    núcleo (``_meta.ENV_PREFIX``); después, las de ``gh`` y, al final,
    ``gh auth token``.
    """
    if not isinstance(env_prefix, str) or _ENV_PREFIX_RE.fullmatch(env_prefix) is None:
        raise ValueError(f"invalid environment prefix: {env_prefix!r}")
    return (env_prefix + "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "gh_auth_token")


# ---------------------------------------------------------------------------
# Datos (08 §2). Todos son inmutables y con __slots__.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RepoRef:
    owner: str
    name: str


@dataclass(frozen=True, slots=True)
class Identity:
    login: str  # tal como lo devuelve la forja; "github-actions[bot]" en CI (REQ-FORGE-43)
    id: int | None  # id durable de la cuenta; None en CI
    kind: Literal["user", "bot", "ci"]
    token_source: TokenSource
    token_kind: TokenKind
    token_scope: Literal["repo_restricted", "owner_wide", "broad", "unknown", "ci"]
    oauth_scopes: tuple[str, ...] | None  # cabecera X-OAuth-Scopes (solo ghp_ y gho_)
    previous_login: str | None  # login anterior con el mismo id (§6.4)
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class Label:
    name: str
    color: str = "ededed"
    description: str = ""


@dataclass(frozen=True, slots=True)
class Ref:
    branch: str  # nombre sin "refs/<namespace>/": "task/12", "archive/task-12-1"
    sha: str


@dataclass(frozen=True, slots=True)
class RefActivity:
    activity_type: Literal[
        "push",
        "force_push",
        "branch_creation",
        "branch_deletion",
        "pr_merge",
        "merge_queue_merge",
    ]
    actor: str | None  # login de quien movió la ref
    timestamp: datetime
    before: str  # sha anterior (NULL_SHA al crear)
    after: str  # sha nuevo (NULL_SHA al borrar)


@dataclass(frozen=True, slots=True)
class CommitInfo:
    sha: str
    message: str
    author_login: str | None  # None si el correo del autor no está ligado a una cuenta
    author_date: datetime
    committer_date: datetime


@dataclass(frozen=True, slots=True)
class Issue:
    number: int
    title: str
    body: str
    state: Literal["open", "closed"]
    state_reason: Literal["completed", "not_planned", "reopened", "duplicate"] | None
    labels: tuple[str, ...]
    assignees: tuple[str, ...]
    author: str
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    url: str


@dataclass(frozen=True, slots=True)
class LabelEvent:
    id: int
    event: Literal["labeled", "unlabeled"]
    label: str
    actor: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PullRequest:
    number: int
    node_id: str  # node_id solo para §4.3
    title: str
    body: str
    state: Literal["open", "closed"]
    draft: bool
    merged: bool
    merged_at: datetime | None
    head_branch: str
    head_sha: str
    base_branch: str
    author: str
    labels: tuple[str, ...]
    requested_reviewers: tuple[str, ...]
    updated_at: datetime
    url: str
    # ¿La cabeza está en este mismo repositorio? En GitHub, head.repo.full_name igual
    # al del repositorio; False si viene de un fork (o el fork ya no existe y head.repo
    # es null). 04-tareas §1.1 (Pull.same_repo) y REQ-TASK-5 lo necesitan para ignorar
    # y reportar los PR desde forks (fork_pr_ignored). Como en 08 §2, va al final y con
    # valor por defecto (igual que Comparison.commits): la construcción posicional con
    # los demás campos sigue valiendo. Un adaptador que lea PR de la forja DEBE fijarlo.
    same_repo: bool = True
    # Login de quien integró el PR (08 §2, D-45; lo usa ci seal). Solo lo trae GET
    # pulls/{n} (get_pull_request): las listas de GitHub, pulls_for_commit incluido, lo
    # dejan en None (08 §4.2, CT-SEAL-01). Como en 08 §2, va al final y con valor por
    # defecto, igual que same_repo.
    merged_by: str | None = None
    # Creación del PR (08 §2): Pull.created_at de 04-tareas §1.1 (derive_state elige con ella
    # el PR de la tarea) y las columnas de metrics (12-mvp §5.3). Al final y con valor por
    # defecto, como same_repo y merged_by; un adaptador que lea PR de la forja DEBE fijarlo.
    created_at: datetime | None = None
    # Commit de la integración (el de squash en la base), como lo da GitHub en merge_commit_sha; en
    # un PR abierto es el commit de prueba de la fusión. Lo usa derive_approval_record (11-trazabilidad
    # §4.3). Al final y con valor por defecto, como created_at.
    merge_commit_sha: str | None = None


@dataclass(frozen=True, slots=True)
class Review:
    id: int
    author: str
    author_id: int
    state: Literal["APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING"]
    commit_sha: str | None  # para detectar aprobaciones obsoletas
    submitted_at: datetime | None


@dataclass(frozen=True, slots=True)
class Comment:
    id: int
    author: str
    body: str
    created_at: datetime
    updated_at: datetime
    url: str


@dataclass(frozen=True, slots=True)
class FileContent:
    path: str
    ref: str
    blob_sha: str
    content: bytes


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    content: bytes | None  # None = borrar el archivo
    executable: bool = False


@dataclass(frozen=True, slots=True)
class ChangedFile:
    path: str
    status: Literal["added", "modified", "removed", "renamed", "copied", "changed", "unchanged"]
    previous_path: str | None = None
    # Líneas agregadas y quitadas de GET pulls/{n}/files (las muestra review show, 05 §9.23);
    # van al final con 0 por omisión, como en 08 §2
    additions: int = 0
    deletions: int = 0


@dataclass(frozen=True, slots=True)
class Comparison:
    base: str
    head: str
    status: Literal["identical", "ahead", "behind", "diverged"]
    ahead_by: int
    behind_by: int
    merge_base_sha: str
    files: tuple[ChangedFile, ...]
    files_truncated: bool  # GitHub lista a lo sumo 300 archivos
    commits: tuple[CommitInfo, ...] = ()  # solo con with_commits=True; hasta 250


@dataclass(frozen=True, slots=True)
class CheckRun:
    name: str
    status: str  # queued, in_progress, completed, ...
    conclusion: str | None  # success, failure, neutral, cancelled, ...
    source: Literal["check_run", "workflow_run", "status"]
    url: str


@dataclass(frozen=True, slots=True)
class Checks:
    sha: str
    overall: Literal["success", "failure", "pending", "none"]
    runs: tuple[CheckRun, ...]


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    id: int
    name: str
    event: str
    status: str
    conclusion: str | None
    head_sha: str
    head_branch: str | None
    actor: str
    url: str
    # Cuándo se creó el run (created_at de GitHub). Lo usa doctor para REQ-CI-31; como en 08 §2,
    # va al final y con valor por defecto, como PullRequest.same_repo
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BaseRules:
    source: Literal["ruleset", "classic", "none"]
    required_approvals: int | None
    require_code_owner_review: bool | None
    require_last_push_approval: bool | None
    dismiss_stale_reviews_on_push: bool | None
    required_checks: tuple[str, ...] | None  # None = no se pudo leer (§3.1)
    blocks_force_push: bool | None


@dataclass(frozen=True, slots=True)
class Capabilities:
    forge: ForgeKind
    repo: RepoRef
    visibility: Literal["public", "private", "internal"]
    owner_type: Literal["User", "Organization"]
    viewer_permission: Literal["admin", "maintain", "write", "triage", "read"]
    default_branch: str
    rules_available: Tristate  # ¿se pueden exigir reglas o protección?
    code_owners_enforceable: Tristate
    base_rules: BaseRules | None  # reglas activas en la rama base
    profile: Literal["enforced", "advisory"]
    push_rulesets: bool  # ¿admite push rulesets (restricción por ruta)? §3.1
    checks_api: Tristate
    transport: Literal["http", "gh"]  # §8
    write_mode: Literal["git", "api"]  # §8
    detected_at: datetime
    evidence: tuple[str, ...]  # p. ej. "GET /repos/o/r/rulesets -> 200"


class HumanConfirmation(Protocol):
    """Prueba de confirmación TTY. Solo la construye la capa CLI (05-operaciones REQ-OPS-47)."""

    operation: str
    confirmed_at: datetime


# ---------------------------------------------------------------------------
# Protocol del adaptador (08 §2 y 00-marco D-34 h)
# ---------------------------------------------------------------------------


@runtime_checkable
class ForgeAdapter(Protocol):
    repo: RepoRef

    # Identidad y capacidades
    def identity(self, *, refresh: bool = False) -> Identity: ...
    def capabilities(self, *, refresh: bool = False) -> Capabilities: ...
    # Hora del servidor (05 §9.19): la cabecera Date de la última respuesta; None si aún no hubo
    def server_time(self) -> datetime | None: ...

    # Refs, commits y contenido
    def get_ref(self, branch: str, *, namespace: RefNamespace = "heads") -> Ref | None: ...
    def list_refs(self, prefix: str, *, namespace: RefNamespace = "heads") -> list[Ref]: ...  # p. ej. "task/"
    def create_ref(self, branch: str, sha: str, *, namespace: RefNamespace = "heads") -> Ref: ...  # §4.4
    def update_ref(self, branch: str, sha: str) -> Ref: ...  # solo avance rápido
    def delete_ref(self, branch: str, *, namespace: RefNamespace = "heads") -> bool: ...  # False si no existía
    def list_ref_activity(self, branch: str) -> list[RefActivity] | None: ...  # None si la forja no la da
    def create_empty_commit(self, parent: str, message: str) -> str: ...  # árbol del padre
    def commit_files(
        self,
        branch: str,
        *,
        expected_head: str,
        changes: Sequence[FileChange],
        message: str,
        extra_parents: Sequence[str] = (),
    ) -> str: ...  # §4.5
    def merge_into(self, branch: str, source: str, *, message: str) -> str | None: ...
    def get_commit(self, sha: str) -> CommitInfo: ...
    def get_file(self, path: str, ref: str) -> FileContent | None: ...
    def compare(self, base: str, head: str, *, with_commits: bool = False) -> Comparison: ...
    def codeowners_errors(self, ref: str) -> list[str]: ...

    # Issues, etiquetas, asignaciones y comentarios
    def get_issue(self, number: int) -> Issue | None: ...
    def list_issues(
        self,
        *,
        labels: Sequence[str] = (),
        state: str = "all",
        since: datetime | None = None,
    ) -> list[Issue]: ...
    def create_issue(
        self, title: str, body: str, *, labels: Sequence[str] = (), op_id: str
    ) -> Issue: ...  # §4.7
    def update_issue(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
        state_reason: str | None = None,
    ) -> Issue: ...
    def add_labels(self, number: int, labels: Sequence[str]) -> tuple[str, ...]: ...
    def remove_label(self, number: int, label: str) -> tuple[str, ...]: ...
    # Una acción por etiqueta pedida: created, updated o unchanged (REQ-PERM-25 de init)
    def ensure_labels(self, labels: Sequence[Label]) -> dict[str, Literal["created", "updated", "unchanged"]]: ...
    def list_issue_events(self, number: int) -> list[LabelEvent]: ...  # solo labeled/unlabeled
    def set_assignees(self, number: int, logins: Sequence[str]) -> Issue: ...  # reemplaza el conjunto
    def add_assignees(self, number: int, logins: Sequence[str]) -> Issue: ...  # solo para CT-ISS-01
    def remove_assignees(self, number: int, logins: Sequence[str]) -> Issue: ...
    def list_comments(self, number: int, *, since: datetime | None = None) -> list[Comment]: ...
    def create_comment(self, number: int, body: str, *, op_id: str) -> Comment: ...
    def update_comment(self, comment_id: int, body: str) -> Comment: ...

    # Pull requests y revisiones
    def get_pull_request(self, number: int) -> PullRequest | None: ...
    # GET pulls?head={o}:{b} (§4.2): solo PR cuya cabeza es la rama head_branch de este
    # repositorio (same_repo=True); el filtro {o}:{b} deja fuera los PR desde forks
    def find_pull_request(self, head_branch: str, *, state: str = "all") -> PullRequest | None: ...
    def list_pull_requests(self, *, state: str = "open", base: str | None = None) -> list[PullRequest]: ...
    def list_pull_request_files(self, number: int) -> list[ChangedFile]: ...
    def list_pull_request_commits(self, number: int) -> list[CommitInfo]: ...  # hasta 250
    def create_pull_request(
        self, *, head: str, base: str, title: str, body: str, draft: bool = False
    ) -> PullRequest: ...
    def update_pull_request(
        self,
        number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
    ) -> PullRequest: ...
    def mark_ready_for_review(self, number: int) -> PullRequest: ...  # reservado (§4.3)
    def request_reviewers(self, number: int, logins: Sequence[str]) -> PullRequest: ...
    def list_reviews(self, number: int) -> list[Review]: ...  # orden cronológico
    def submit_review(
        self,
        number: int,
        *,
        event: Literal["APPROVE", "REQUEST_CHANGES"],
        body: str,
        commit_sha: str,
        confirmation: HumanConfirmation,
    ) -> Review: ...
    def get_checks(self, sha: str) -> Checks: ...  # §4.6
    def get_workflow_run(self, run_id: int) -> WorkflowRun: ...
    # GET actions/workflows/{f}/runs?branch=…&event=…&status=success&per_page=…: runs
    # exitosos con la rama y el evento exactos, más recientes primero (REQ-FORGE-69). ci seal
    # pide event='push': un run de pull_request_target lleva la punta de la base en head_sha.
    def successful_runs(
        self, workflow_file: str, branch: str, *, event: str, limit: int = 100
    ) -> list[WorkflowRun]: ...

    # CI de la rama base: PR sospechosos de REQ-CI-33 y PR integrado de ci seal (00-marco D-34 h)
    # GET /repos/{o}/{r}/commits/{sha}/pulls; lista vacía si el commit no tiene PR. Son PR
    # resumidos: merged sale de merged_at y merged_by queda en None (08 §4.2)
    def pulls_for_commit(self, sha: str) -> list[PullRequest]: ...
    # GET /repos/{o}/{r}/actions/workflows/{f}/runs?branch=<branch>&status=success&per_page=1;
    # el run exitoso más reciente, o None si no hay ninguno
    def last_successful_run(self, workflow_file: str, branch: str) -> WorkflowRun | None: ...
    # Desde H3 (00-marco D-56): un solo check run con ese nombre en sha, que se actualiza o se
    # crea (REQ-FORGE-70). Solo lo usa ci editor-review, con el GITHUB_TOKEN de Actions
    def upsert_check_run(self, sha: str, name: str, *, conclusion: str, summary: str) -> None: ...

    # Lecturas de la configuración con el token de la persona (00-marco D-65; REQ-FORGE-73): solo
    # GET, para doctor. Mismas firmas y mismo trato de los permisos que en ForgeAdmin; las
    # escrituras siguen solo allí (REQ-FORGE-3)
    def repo_settings(self) -> dict[str, object]: ...  # las claves de REPO_SETTINGS que muestra la forja
    def rulesets(self) -> list[RulesetInfo]: ...  # sin bypass_actors si la forja no los muestra
    def list_labels(self) -> list[Label]: ...
    def collaborator_permission(self, login: str) -> str | None: ...  # role_name; None si no es colaboradora
    def outside_collaborators(self) -> list[str]: ...
    def actions_policy(self, level: AdminLevel = "repo") -> ActionsPolicy | None: ...  # None: no se puede leer
    def workflow_permissions(self, level: AdminLevel = "repo") -> WorkflowPermissions | None: ...
    def org_settings(self) -> dict[str, object] | None: ...  # None: cuenta personal o no se puede leer
    # GET environments/{name} y sus reglas de despliegue (00-marco D-72): None si no existe
    def environment(self, name: str) -> EnvironmentInfo | None: ...


# ---------------------------------------------------------------------------
# Interfaz de administración (08 §2.2; 00-marco D-62): solo init
# ---------------------------------------------------------------------------

# Ajustes del repositorio que init lee y escribe (06-permisos §4.2; 11-trazabilidad §3.2).
# update_repo_settings rechaza cualquier otra clave sin llamar a la forja (REQ-FORGE-72).
REPO_SETTINGS: tuple[str, ...] = (
    "allow_squash_merge",
    "allow_merge_commit",
    "allow_rebase_merge",
    "squash_merge_commit_title",
    "squash_merge_commit_message",
    "allow_auto_merge",
    "allow_update_branch",
    "delete_branch_on_merge",
    "allow_forking",
    "has_wiki",
)
# Ajustes de la organización que init informa y no cambia (06-permisos §4.10). GitHub
# solo los muestra a los owners: los que faltan en la respuesta no se pudieron leer.
ORG_SETTINGS: tuple[str, ...] = (
    "default_repository_permission",
    "members_can_fork_private_repositories",
    "members_can_change_repo_visibility",
    "members_can_delete_repositories",
    "members_can_create_public_repositories",
    "two_factor_requirement_enabled",
)
AdminLevel = Literal["repo", "org"]
CollaboratorPermission = Literal["pull", "triage", "push", "maintain", "admin"]
# Secretos de Actions cuyos nombres lee init (06-permisos REQ-PERM-61): del repositorio, de la
# organización visibles para el repositorio o de un environment.
SecretScope = Literal["repo", "org", "environment"]


def rules_summary(rules: Sequence[object]) -> BaseRules:
    """``BaseRules`` de las reglas activas de una rama (``GET rules/branches/{b}``, 08 §3.1).

    La usan la detección de capacidades (``GitHubAdapter``) y el falso, para resumir las
    mismas reglas de la misma forma.
    """
    approvals = 0
    code_owner = last_push = dismiss = force = False
    checks: list[str] = []
    for rule in rules:
        if not isinstance(rule, Mapping):
            continue
        params = rule.get("parameters") if isinstance(rule.get("parameters"), Mapping) else {}
        assert isinstance(params, Mapping)
        kind = rule.get("type")
        if kind == "pull_request":
            count = params.get("required_approving_review_count")
            if isinstance(count, int) and not isinstance(count, bool):
                approvals = max(approvals, count)
            code_owner = code_owner or bool(params.get("require_code_owner_review"))
            last_push = last_push or bool(params.get("require_last_push_approval"))
            dismiss = dismiss or bool(params.get("dismiss_stale_reviews_on_push"))
        elif kind == "required_status_checks":
            for check in params.get("required_status_checks") or ():
                context = check.get("context") if isinstance(check, Mapping) else None
                if isinstance(context, str) and context not in checks:
                    checks.append(context)
        elif kind == "non_fast_forward":
            force = True
    return BaseRules("ruleset", approvals, code_owner, last_push, dismiss, tuple(checks), force)


@dataclass(frozen=True, slots=True)
class ActionsPolicy:
    """Política de Actions de un nivel (06-permisos §4.3 y REQ-PERM-56)."""

    level: AdminLevel
    enabled: bool  # repo: enabled; org: enabled_repositories distinto de "none"
    allowed_actions: Literal["all", "local_only", "selected"] | None
    sha_pinning_required: bool | None
    # Solo con allowed_actions: selected (GET .../actions/permissions/selected-actions)
    github_owned_allowed: bool | None = None
    verified_allowed: bool | None = None
    patterns_allowed: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class WorkflowPermissions:
    """Token de los workflows (06-permisos §4.3, REQ-PERM-27)."""

    default_workflow_permissions: Literal["read", "write"]
    can_approve_pull_request_reviews: bool


@dataclass(frozen=True, slots=True)
class RulesetInfo:
    """Un ruleset del repositorio con su definición completa (``GET rulesets/{id}``)."""

    id: int
    name: str
    target: str  # branch | tag | push
    enforcement: str  # active | evaluate | disabled
    payload: Mapping[str, Any]  # conditions, bypass_actors y rules, tal como los da la forja


@dataclass(frozen=True, slots=True)
class EnvironmentInfo:
    """Un environment del repositorio y sus reglas de despliegue (06-permisos REQ-PERM-61; 00-marco D-72).

    ``policy``: ``all`` (sin ``deployment_branch_policy``: cualquier rama), ``protected``
    (las ramas protegidas) o ``custom`` (las reglas propias de ``rules``, pares ``(tipo,
    nombre)`` con ``tipo`` en ``branch`` o ``tag``).
    """

    name: str
    policy: Literal["all", "protected", "custom"]
    rules: tuple[tuple[str, str], ...] = ()

    def only_branch(self, branch: str) -> bool:
        """La única regla es la rama ``branch`` (REQ-PERM-61)."""
        return self.policy == "custom" and self.rules == (("branch", branch),)


@runtime_checkable
class ForgeAdmin(Protocol):
    """Lecturas y escrituras de la configuración del repositorio (08 §2.2; 00-marco D-62).

    Solo la construye ``init``, con el token de administración que la persona teclea en la
    terminal (10-seguridad REQ-SEC-30) y sin cachés en disco (REQ-FORGE-71). El adaptador
    normal no puede cambiar la configuración (REQ-FORGE-3). Las lecturas que pueden no
    estar permitidas devuelven ``None`` en vez de fallar, para que ``init`` informe el
    resultado como desconocido (06-permisos REQ-PERM-56).
    """

    repo: RepoRef

    # Cuenta del token de administración: GET /user
    def identity(self) -> Identity: ...
    # Ajustes del repositorio (06 §4.2): solo las claves de REPO_SETTINGS que trae la forja
    def repo_settings(self) -> dict[str, object]: ...
    def update_repo_settings(self, changes: Mapping[str, object]) -> dict[str, object]: ...
    # Actions (06 §4.3): None = no se puede leer con este token
    def actions_policy(self, level: AdminLevel = "repo") -> ActionsPolicy | None: ...
    def set_actions_policy(self, *, enabled: bool, allowed_actions: str, sha_pinning_required: bool) -> None: ...
    def workflow_permissions(self, level: AdminLevel = "repo") -> WorkflowPermissions | None: ...
    def set_workflow_permissions(self, *, default: Literal["read", "write"], can_approve: bool) -> None: ...
    # Retención de artefactos y registros (REQ-PERM-57): None = el endpoint no existe
    def retention_days(self) -> int | None: ...
    def set_retention_days(self, days: int) -> None: ...
    # Colaboradores (06 §4.4): role_name, o None si no es colaboradora
    def collaborator_permission(self, login: str) -> str | None: ...
    def add_collaborator(self, login: str, permission: CollaboratorPermission) -> Literal["added", "invited"]: ...
    def outside_collaborators(self) -> list[str]: ...
    def user_id(self, login: str) -> int: ...  # GET /users/{login}
    # Organización (06 §4.10), solo lectura: None si el dueño no es una org o no se puede leer
    def org_settings(self) -> dict[str, object] | None: ...
    # Rulesets (06 §4.7 y §4.11)
    def ruleset(self, name: str) -> RulesetInfo | None: ...
    def upsert_ruleset(self, name: str, payload: Mapping[str, object]) -> int: ...
    def branch_rules(self, branch: str) -> list[dict[str, object]]: ...  # reglas activas en la rama
    # Environment de la firma del proyecto (06 §4.3b; REQ-FORGE-74): put_environment lo deja con una
    # sola regla, la rama; secret_names da solo nombres, nunca valores (None: no se pueden leer)
    def environment(self, name: str) -> EnvironmentInfo | None: ...
    def put_environment(self, name: str, *, branch: str) -> EnvironmentInfo: ...
    def secret_names(self, scope: SecretScope, environment: str | None = None) -> tuple[str, ...] | None: ...


# ---------------------------------------------------------------------------
# Errores (08 §2.1)
# ---------------------------------------------------------------------------


def _restore_error(cls: type[ForgeError], message: str) -> ForgeError:
    # Reconstrucción para pickle y copy: el estado llega después por __setstate__.
    return cls.__new__(cls, message)


class ForgeError(Exception):
    """Falla clasificada del adaptador (08-forja §2.1).

    ``kind`` clasifica la falla dentro del adaptador y no es un código del
    sobre: la traducción a ``E_*`` está en 05-operaciones §12.3. ``hint`` va ya
    en el idioma del repositorio de trabajo. Nunca lleva el valor de un token
    (REQ-FORGE-6).
    """

    kind: str
    retryable: bool
    message: str
    http_status: int | None
    hint: str | None

    def __init__(
        self,
        kind: str,
        message: str = "",
        retryable: bool | None = None,
        http_status: int | None = None,
        hint: str | None = None,
    ) -> None:
        if kind not in RETRYABLE_BY_KIND:
            raise ValueError(f"unknown forge error kind: {kind!r}")
        expected = RETRYABLE_BY_KIND[kind]
        if retryable is None:
            retryable = expected
        elif retryable is not expected:
            raise ValueError(f"kind {kind!r} has retryable={expected}, got {retryable!r}")
        if not isinstance(message, str):
            raise TypeError("message must be a str")
        if http_status is not None and (
            isinstance(http_status, bool)
            or not isinstance(http_status, int)
            or not 100 <= http_status <= 599
        ):
            raise ValueError(f"invalid HTTP status: {http_status!r}")
        if hint is not None and not isinstance(hint, str):
            raise TypeError("hint must be a str or None")
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.message = message
        self.http_status = http_status
        self.hint = hint

    def __str__(self) -> str:
        return self.message or self.kind

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(kind={self.kind!r}, message={self.message!r}, "
            f"http_status={self.http_status!r})"
        )

    def __reduce__(self) -> tuple[Any, ...]:
        return (_restore_error, (type(self), self.message), dict(self.__dict__))


class _ClassifiedError(ForgeError):
    # Base de las excepciones de la tabla: kind y retryable los fija la clase.
    kind: str
    retryable: bool

    def __init__(
        self,
        message: str = "",
        *,
        http_status: int | None = None,
        hint: str | None = None,
    ) -> None:
        cls = type(self)
        super().__init__(cls.kind, message, cls.retryable, http_status, hint)


class NoTokenError(_ClassifiedError):
    """Ninguna fuente de §5.1 da token."""

    kind = "no_token"
    retryable = False


class ForgeAuthError(_ClassifiedError):
    """401."""

    kind = "auth"
    retryable = False


class ForgePermissionError(_ClassifiedError):
    """403 sin señal de límite; ``hint`` incluye X-Accepted-GitHub-Permissions."""

    kind = "permission"
    retryable = False


class LoginChangedError(_ClassifiedError):
    """El visor cambió de login y roles.yaml tiene el anterior (§6.4)."""

    kind = "login_changed"
    retryable = False


class ForgeNotFound(_ClassifiedError):
    """404 donde el método no devuelve None."""

    kind = "not_found"
    retryable = False


class RefAlreadyExists(_ClassifiedError):
    """La ref existe con otro sha después del intento (§4.4, REQ-FORGE-28)."""

    kind = "ref_exists"
    retryable = False
    existing: Ref

    def __init__(
        self,
        message: str = "",
        *,
        existing: Ref,
        http_status: int | None = None,
        hint: str | None = None,
    ) -> None:
        if not isinstance(existing, Ref):
            raise TypeError("existing must be a Ref")
        super().__init__(message, http_status=http_status, hint=hint)
        self.existing = existing


class RefConflict(_ClassifiedError):
    """Actualización que no es avance rápido; ``current`` es el sha actual si se conoce."""

    kind = "ref_conflict"
    retryable = False
    current: str | None

    def __init__(
        self,
        message: str = "",
        *,
        current: str | None = None,
        http_status: int | None = None,
        hint: str | None = None,
    ) -> None:
        if current is not None and not isinstance(current, str):
            raise TypeError("current must be a str or None")
        super().__init__(message, http_status=http_status, hint=hint)
        self.current = current


class MergeConflict(_ClassifiedError):
    """409 en merge_into."""

    kind = "merge_conflict"
    retryable = False


class PullRequestExists(_ClassifiedError):
    """Ya hay un PR abierto para esa cabeza (REQ-FORGE-36)."""

    kind = "pr_exists"
    retryable = False
    existing: PullRequest

    def __init__(
        self,
        message: str = "",
        *,
        existing: PullRequest,
        http_status: int | None = None,
        hint: str | None = None,
    ) -> None:
        if not isinstance(existing, PullRequest):
            raise TypeError("existing must be a PullRequest")
        super().__init__(message, http_status=http_status, hint=hint)
        self.existing = existing


class NoCommitsBetween(_ClassifiedError):
    """PR sin commits entre base y cabeza."""

    kind = "no_commits"
    retryable = False


class ForgeValidationError(_ClassifiedError):
    """Otros 422 o las verificaciones de REQ-FORGE-8..11."""

    kind = "validation"
    retryable = False


class BaseNotDefault(ForgeValidationError):
    """``forge.base_branch`` no es la rama por defecto (§3.2).

    Mantiene ``kind = "validation"``; el núcleo la traduce a
    E_CONFIG_BASE_NOT_DEFAULT por su tipo, no por su kind.
    """

    default_branch: str

    def __init__(
        self,
        message: str = "",
        *,
        default_branch: str,
        http_status: int | None = None,
        hint: str | None = None,
    ) -> None:
        if not isinstance(default_branch, str) or not default_branch:
            raise ValueError("default_branch must be a non-empty str")
        super().__init__(message, http_status=http_status, hint=hint)
        self.default_branch = default_branch


class ForgeUnsupported(_ClassifiedError):
    """Capacidad ausente (GraphQL bloqueado, plan)."""

    kind = "unsupported"
    retryable = False


class RateLimited(_ClassifiedError):
    """Límite primario o secundario (§7.3); ``reset_at`` es aware."""

    kind = "rate_limited"
    retryable = True
    reset_at: datetime

    def __init__(
        self,
        message: str = "",
        *,
        reset_at: datetime,
        http_status: int | None = None,
        hint: str | None = None,
    ) -> None:
        if not isinstance(reset_at, datetime) or reset_at.utcoffset() is None:
            raise ValueError("reset_at must be a timezone-aware datetime")
        super().__init__(message, http_status=http_status, hint=hint)
        self.reset_at = reset_at


class ForgeUnavailable(_ClassifiedError):
    """5xx tras agotar los reintentos; 409 en update_ref y delete_ref (§4.2)."""

    kind = "unavailable"
    retryable = True


class NetworkTimeout(_ClassifiedError):
    """Timeout de conexión o de lectura."""

    kind = "timeout"
    retryable = True


class NetworkUnreachable(_ClassifiedError):
    """DNS o conexión rechazada."""

    kind = "unreachable"
    retryable = True


class NetworkTLSError(_ClassifiedError):
    """Falla de TLS (certificado o negociación)."""

    kind = "tls"
    retryable = False


# Filas de la tabla de 08 §2.1, en su orden (BaseNotDefault es la nota al pie).
ERROR_CLASSES: tuple[type[ForgeError], ...] = (
    NoTokenError,
    ForgeAuthError,
    ForgePermissionError,
    LoginChangedError,
    ForgeNotFound,
    RefAlreadyExists,
    RefConflict,
    MergeConflict,
    PullRequestExists,
    NoCommitsBetween,
    ForgeValidationError,
    ForgeUnsupported,
    RateLimited,
    ForgeUnavailable,
    NetworkTimeout,
    NetworkUnreachable,
    NetworkTLSError,
)

# kind -> retryable; conjunto cerrado de kinds que traduce 05-operaciones §12.3
RETRYABLE_BY_KIND: Mapping[str, bool] = MappingProxyType(
    {cls.kind: cls.retryable for cls in ERROR_CLASSES}
)
