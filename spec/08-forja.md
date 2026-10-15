# 08 — Adaptador de forja

- **Estado:** v0.1 · **Código:** `FORGE`
- **Autoridad (00-marco §6):** interfaz del adaptador, detección de capacidades, implementación GitHub (REST), identidad y autenticación, límites de la API, restricciones de las sesiones en la nube, diferencias con GitLab y Forgejo, y el adaptador falso.
- **No es autoridad sobre:** la semántica del reclamo ni los estados (04-tareas), las operaciones, el sobre y los errores de operación (05-operaciones), la matriz de aprobación y las protecciones que aplica `init` (06-permisos), los hooks (07-hooks), los workflows (09-ci) ni el modelo de amenazas (10-seguridad).
- Los hechos externos se verificaron en la documentación oficial al preparar la v0.1 (§14). Lo que no se pudo verificar va marcado **[supuesto]** o **[no verificado]** y tiene una prueba de contrato o una pregunta abierta asociada.

## 1. Principios

- **REQ-FORGE-1:** Toda interacción del núcleo con la forja DEBE pasar por un objeto que cumpla `ForgeAdapter` (§2) o, solo en `init`, `ForgeAdmin` (§2.2). Ningún otro módulo DEBE construir URLs de la API ni invocar `gh`.
- **REQ-FORGE-2:** El adaptador NO DEBE contener lógica del proyecto. No decide permisos, estados de tarea ni roles: traduce llamadas, normaliza respuestas y clasifica errores.
- **REQ-FORGE-3:** `ForgeAdapter` NO DEBE tener métodos para fusionar un PR, borrar Issues ni cambiar la configuración del repositorio. Las escrituras de configuración (ajustes del repositorio, permisos de Actions, colaboradores y rulesets) están en `ForgeAdmin` (§2.2), que solo construye `init` (D-09; 00-marco D-62). Desde 00-marco D-65, `ForgeAdapter` sí lee esa configuración con el token de la persona, solo con GET (§2, REQ-FORGE-73), para `doctor`.
- **REQ-FORGE-4:** La implementación GitHub DEBE usar solo la API REST, con dos excepciones: `mark_ready_for_review` (§4.3), que queda reservado y ninguna operación de v0.1 invoca; y, en una sesión web, `create_empty_commit`, `create_ref` y `update_ref`, que usan `git` sobre el clon local porque el proxy bloquea la API de datos de git (REQ-FORGE-67 y REQ-FORGE-68; 00-marco D-41).
- **REQ-FORGE-5:** `aucove hook <evento>` NO DEBE hacer llamadas de red a través del adaptador. Solo PUEDE leer las cachés de §3.4 y §6.2.
- **REQ-FORGE-6:** El adaptador NO DEBE registrar, persistir ni devolver el valor de un token. Para las claves de caché DEBE usar la huella `sha256(token).hexdigest()[:12]`.
- **REQ-FORGE-7:** El adaptador NO DEBE obtener la identidad de `git config`. La identidad sale siempre de la forja (§6).

Ubicación en el paquete: `aucove/forge/base.py` (tipos, errores, Protocol), `github.py`, `fake.py`, `auth.py` y `cache.py`.

## 2. Interfaz

```python
# aucove/forge/base.py
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Mapping, Protocol, Sequence, runtime_checkable

Tristate = bool | None                      # None = no determinado
ForgeKind = Literal["github", "gitlab", "forgejo", "fake"]
TokenSource = Literal["AUCOVE_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "gh_auth_token"]
TokenKind = Literal["fine_grained_pat", "classic_pat", "oauth", "app_user",
                    "app_installation", "proxy", "unknown"]
RefNamespace = Literal["heads", "tags"]     # refs/heads/… (ramas) o refs/tags/… (p. ej. archive/task-<n>-<k>)
frozen = dataclass(frozen=True, slots=True)

@frozen
class RepoRef:
    owner: str; name: str

@frozen
class Identity:
    login: str                              # tal como lo devuelve la forja; "github-actions[bot]" en CI (REQ-FORGE-43)
    id: int | None                          # id durable de la cuenta; None en CI
    kind: Literal["user", "bot", "ci"]
    token_source: TokenSource; token_kind: TokenKind
    token_scope: Literal["repo_restricted", "owner_wide", "broad", "unknown", "ci"]
    oauth_scopes: tuple[str, ...] | None    # cabecera X-OAuth-Scopes (solo ghp_ y gho_)
    previous_login: str | None              # login anterior con el mismo id (§6.4)
    fetched_at: datetime

@frozen
class Label:
    name: str; color: str = "ededed"; description: str = ""

@frozen
class Ref:
    branch: str                             # nombre sin "refs/<namespace>/": "task/12", "archive/task-12-1"
    sha: str

@frozen
class RefActivity:
    activity_type: Literal["push", "force_push", "branch_creation", "branch_deletion",
                           "pr_merge", "merge_queue_merge"]
    actor: str | None                       # login de quien movió la ref
    timestamp: datetime
    before: str; after: str                 # sha anterior y nuevo ("000…0" al crear o borrar)

@frozen
class CommitInfo:
    sha: str; message: str
    author_login: str | None                # None si el correo del autor no está ligado a una cuenta
    author_date: datetime; committer_date: datetime

@frozen
class Issue:
    number: int; title: str; body: str
    state: Literal["open", "closed"]
    state_reason: Literal["completed", "not_planned", "reopened", "duplicate"] | None
    labels: tuple[str, ...]; assignees: tuple[str, ...]; author: str
    created_at: datetime; updated_at: datetime; closed_at: datetime | None
    url: str

@frozen
class LabelEvent:
    id: int; event: Literal["labeled", "unlabeled"]; label: str
    actor: str; created_at: datetime

@frozen
class PullRequest:
    number: int; node_id: str               # node_id solo para §4.3
    title: str; body: str
    state: Literal["open", "closed"]; draft: bool; merged: bool; merged_at: datetime | None
    head_branch: str; head_sha: str; base_branch: str; author: str
    labels: tuple[str, ...]; requested_reviewers: tuple[str, ...]
    updated_at: datetime; url: str
    same_repo: bool = True                  # head.repo.full_name == repo; False en un PR desde fork (04-tareas §1.1); el adaptador DEBE fijarlo
    merged_by: str | None = None            # login de quien integró; solo GET /pulls/{n} (§4.2); lo usa ci seal
    created_at: datetime | None = None      # creación del PR; Pull.created_at (04-tareas §1.1) y metrics (12-mvp §5.3)
    merge_commit_sha: str | None = None     # commit de la integración; derive_approval_record (11-trazabilidad §4.3)

@frozen
class Review:
    id: int; author: str; author_id: int
    state: Literal["APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING"]
    commit_sha: str | None                  # para detectar aprobaciones obsoletas
    submitted_at: datetime | None

@frozen
class Comment:
    id: int; author: str; body: str; created_at: datetime; updated_at: datetime; url: str

@frozen
class FileContent:
    path: str; ref: str; blob_sha: str; content: bytes

@frozen
class FileChange:
    path: str
    content: bytes | None                   # None = borrar el archivo
    executable: bool = False

@frozen
class ChangedFile:
    path: str
    status: Literal["added", "modified", "removed", "renamed", "copied", "changed", "unchanged"]
    previous_path: str | None = None
    additions: int = 0                      # líneas agregadas y quitadas (review show, 05 §9.23)
    deletions: int = 0

@frozen
class Comparison:
    base: str; head: str
    status: Literal["identical", "ahead", "behind", "diverged"]
    ahead_by: int; behind_by: int; merge_base_sha: str
    files: tuple[ChangedFile, ...]
    files_truncated: bool                   # GitHub lista a lo sumo 300 archivos
    commits: tuple[CommitInfo, ...] = ()    # solo con with_commits=True; hasta 250

@frozen
class CheckRun:
    name: str
    status: str                             # queued, in_progress, completed, ...
    conclusion: str | None                  # success, failure, neutral, cancelled, ...
    source: Literal["check_run", "workflow_run", "status"]
    url: str

@frozen
class Checks:
    sha: str; overall: Literal["success", "failure", "pending", "none"]; runs: tuple[CheckRun, ...]

@frozen
class WorkflowRun:
    id: int; name: str; event: str
    status: str; conclusion: str | None
    head_sha: str; head_branch: str | None; actor: str; url: str
    created_at: datetime | None = None      # cuándo se creó el run; doctor, REQ-CI-31 (00-marco D-65)

@frozen
class BaseRules:
    source: Literal["ruleset", "classic", "none"]
    required_approvals: int | None; require_code_owner_review: bool | None
    require_last_push_approval: bool | None; dismiss_stale_reviews_on_push: bool | None
    required_checks: tuple[str, ...] | None  # None = no se pudo leer (§3.1)
    blocks_force_push: bool | None

@frozen
class Capabilities:
    forge: ForgeKind; repo: RepoRef
    visibility: Literal["public", "private", "internal"]
    owner_type: Literal["User", "Organization"]
    viewer_permission: Literal["admin", "maintain", "write", "triage", "read"]
    default_branch: str
    rules_available: Tristate               # ¿se pueden exigir reglas o protección?
    code_owners_enforceable: Tristate
    base_rules: BaseRules | None            # reglas activas en la rama base
    profile: Literal["enforced", "advisory"]
    push_rulesets: bool                     # ¿admite push rulesets (restricción por ruta)? §3.1
    checks_api: Tristate
    transport: Literal["http", "gh"]        # §8
    write_mode: Literal["git", "api"]       # §8
    detected_at: datetime
    evidence: tuple[str, ...]               # p. ej. "GET /repos/o/r/rulesets -> 200"

class HumanConfirmation(Protocol):
    """Prueba de confirmación TTY. Solo la construye la capa CLI (05-operaciones REQ-OPS-47)."""
    operation: str
    confirmed_at: datetime

@runtime_checkable
class ForgeAdapter(Protocol):
    repo: RepoRef
    # Identidad y capacidades
    def identity(self, *, refresh: bool = False) -> Identity: ...
    def capabilities(self, *, refresh: bool = False) -> Capabilities: ...
    def server_time(self) -> datetime | None: ...  # cabecera Date de la última respuesta (05 §9.19); None si aún no hubo
    # Refs, commits y contenido
    def get_ref(self, branch: str, *, namespace: RefNamespace = "heads") -> Ref | None: ...
    def list_refs(self, prefix: str, *, namespace: RefNamespace = "heads") -> list[Ref]: ...  # p. ej. "task/"
    def create_ref(self, branch: str, sha: str, *, namespace: RefNamespace = "heads") -> Ref: ...  # §4.4
    def update_ref(self, branch: str, sha: str) -> Ref: ...             # solo avance rápido
    def delete_ref(self, branch: str, *, namespace: RefNamespace = "heads") -> bool: ...  # False si no existía
    def list_ref_activity(self, branch: str) -> list[RefActivity] | None: ...  # None si la forja no la da
    def create_empty_commit(self, parent: str, message: str) -> str: ... # árbol del padre
    def commit_files(self, branch: str, *, expected_head: str, changes: Sequence[FileChange],
                     message: str, extra_parents: Sequence[str] = ()) -> str: ...  # §4.5
    def merge_into(self, branch: str, source: str, *, message: str) -> str | None: ...
    def get_commit(self, sha: str) -> CommitInfo: ...
    def get_file(self, path: str, ref: str) -> FileContent | None: ...
    def compare(self, base: str, head: str, *, with_commits: bool = False) -> Comparison: ...
    def codeowners_errors(self, ref: str) -> list[str]: ...
    # Issues, etiquetas, asignaciones y comentarios
    def get_issue(self, number: int) -> Issue | None: ...
    def list_issues(self, *, labels: Sequence[str] = (), state: str = "all",
                    since: datetime | None = None) -> list[Issue]: ...
    def create_issue(self, title: str, body: str, *, labels: Sequence[str] = (),
                     op_id: str) -> Issue: ...                           # §4.7
    def update_issue(self, number: int, *, title: str | None = None, body: str | None = None,
                     state: str | None = None, state_reason: str | None = None) -> Issue: ...
    def add_labels(self, number: int, labels: Sequence[str]) -> tuple[str, ...]: ...
    def remove_label(self, number: int, label: str) -> tuple[str, ...]: ...
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
    def find_pull_request(self, head_branch: str, *, state: str = "all") -> PullRequest | None: ...
    def list_pull_requests(self, *, state: str = "open", base: str | None = None) -> list[PullRequest]: ...
    def list_pull_request_files(self, number: int) -> list[ChangedFile]: ...
    def list_pull_request_commits(self, number: int) -> list[CommitInfo]: ...  # hasta 250
    def pulls_for_commit(self, sha: str) -> list[PullRequest]: ...    # 00-marco D-34 h; [] si no hay PR
    def upsert_check_run(self, sha: str, name: str, *, conclusion: str, summary: str) -> None: ...  # desde H3; 00-marco D-56
    def create_pull_request(self, *, head: str, base: str, title: str, body: str,
                            draft: bool = False) -> PullRequest: ...
    def update_pull_request(self, number: int, *, title: str | None = None,
                            body: str | None = None, state: str | None = None) -> PullRequest: ...
    def mark_ready_for_review(self, number: int) -> PullRequest: ...    # reservado (§4.3)
    def request_reviewers(self, number: int, logins: Sequence[str]) -> PullRequest: ...
    def list_reviews(self, number: int) -> list[Review]: ...            # orden cronológico
    def submit_review(self, number: int, *, event: Literal["APPROVE", "REQUEST_CHANGES"],
                      body: str, commit_sha: str, confirmation: HumanConfirmation) -> Review: ...
    def get_checks(self, sha: str) -> Checks: ...                       # §4.6
    def get_workflow_run(self, run_id: int) -> WorkflowRun: ...
    def last_successful_run(self, workflow_file: str, branch: str) -> WorkflowRun | None: ...  # D-34 h
    def successful_runs(self, workflow_file: str, branch: str, *, event: str,
                        limit: int = 100) -> list[WorkflowRun]: ...  # más recientes primero
    # Lecturas de la configuración con el token de la persona (00-marco D-65; REQ-FORGE-73): solo GET.
    # Mismas firmas y mismo trato de los permisos que en ForgeAdmin (§2.2), que conserva las escrituras.
    def repo_settings(self) -> dict[str, object]: ...                 # las claves de REPO_SETTINGS que muestra la forja
    def rulesets(self) -> list[RulesetInfo]: ...                      # sin bypass_actors si la forja no los muestra
    def list_labels(self) -> list[Label]: ...
    def collaborator_permission(self, login: str) -> str | None: ...  # role_name; None si no es colaboradora
    def outside_collaborators(self) -> list[str]: ...
    def actions_policy(self, level: AdminLevel = "repo") -> ActionsPolicy | None: ...
    def workflow_permissions(self, level: AdminLevel = "repo") -> WorkflowPermissions | None: ...
    def org_settings(self) -> dict[str, object] | None: ...
    def environment(self, name: str) -> EnvironmentInfo | None: ...   # None si no existe (00-marco D-72)
```

- **REQ-FORGE-8:** Las listas DEBEN paginarse hasta agotar `Link: rel="next"`, con un tope de 10 páginas de 100 elementos. Si se alcanza el tope, el método DEBE lanzar `ForgeValidationError` y NO DEBE devolver una lista truncada en silencio. Excepción: `list_pull_request_files` admite hasta 30 páginas, los 3000 archivos que da GitHub, para que el alcance falle con `too_many_files` y no con un error de forja (06-permisos REQ-PERM-15).
- **REQ-FORGE-9:** `list_issues` NO DEBE devolver PR: GitHub trata todo PR como Issue y los marca con la clave `pull_request`. Del mismo modo, `get_issue` sobre el número de un PR DEBE lanzar `ForgeValidationError`.
- **REQ-FORGE-10:** `submit_review` DEBE lanzar `ForgeValidationError` si `confirmation` no es un `HumanConfirmation` emitido para `review approve` o `review request-changes` en los últimos 120 s (defensa en profundidad para D-09).
- **REQ-FORGE-11:** Las escrituras que GitHub puede ignorar en silencio (asignados sin permiso, etiquetas de quien no tiene push) DEBEN verificarse en la respuesta. Si el efecto no aparece, el método DEBE lanzar `ForgeValidationError`.
- Las operaciones asignan con `set_assignees`, que reemplaza el conjunto completo. `add_assignees` queda solo para `CT-ISS-01` (§10.3).
- `PullRequest.same_repo` sale de `head.repo.full_name` (`True` si la cabeza es del mismo repositorio). `find_pull_request` (`GET pulls?head={o}:{b}`) excluye los PR desde forks; `list_pull_requests` los devuelve con `same_repo=False`, y `derive_state` los ignora (`fork_pr_ignored`, 04-tareas §1.5). `FakeForge` DEBE poder sembrar los dos casos.
- **REQ-FORGE-69:** `successful_runs` DEBE pedir `status=success`, `branch` y `event` exactos (`ci seal` usa `event=push`: un run de `pull_request_target` lleva como `head_sha` la punta de la base) y devolver los runs por fecha de creación descendente.
- **REQ-FORGE-73 (00-marco D-65):** Las lecturas de configuración de `ForgeAdapter` DEBEN usar solo GET y el token de §5.1. Como en `ForgeAdmin`, `actions_policy`, `workflow_permissions` y `org_settings` devuelven `None` ante un 403 o un 404 (sin Administration o sin ser owner). `repo_settings` devuelve solo las claves que trae `GET /repos/{o}/{r}` (los ajustes de integración piden Contents W) y, si `capabilities` ya leyó ese recurso en el mismo proceso, no repite la petición. `rulesets` omite `bypass_actors` cuando la forja no los muestra (solo a quien puede editar el ruleset) y, sin rulesets en el plan, lanza `ForgeUnsupported`. `collaborator_permission` y `outside_collaborators` lanzan `ForgePermissionError` ante un 403 (la lista de colaboradores exige escritura en el repositorio). `environment` devuelve `None` si el environment no existe (404) y lanza `ForgePermissionError` ante un 403.

### 2.1 Errores

Todas las excepciones heredan de `ForgeError(kind: str, message: str, retryable: bool, http_status: int | None, hint: str | None)`. El `hint` va en el idioma del proyecto. `kind` clasifica la falla dentro del adaptador y no es un código del sobre: la traducción a los códigos del sobre (`E_*`), incluida la que depende de la operación (por ejemplo, de `RefAlreadyExists` a un error `E_CLAIM_*`), está en 05-operaciones §12.3.

| Excepción | `kind` | `retryable` | Cuándo |
|---|---|---|---|
| `NoTokenError` | `no_token` | no | Ninguna fuente de §5.1 da token |
| `ForgeAuthError` | `auth` | no | 401 |
| `ForgePermissionError` | `permission` | no | 403 sin señal de límite; `hint` incluye `X-Accepted-GitHub-Permissions` |
| `LoginChangedError` | `login_changed` | no | §6.4 |
| `ForgeNotFound` | `not_found` | no | 404 donde el método no devuelve `None` |
| `RefAlreadyExists` (`.existing: Ref`) | `ref_exists` | no | §4.4 |
| `RefConflict` (`.current: str \| None`) | `ref_conflict` | no | Actualización que no es avance rápido |
| `MergeConflict` | `merge_conflict` | no | 409 en `merge_into` |
| `PullRequestExists` (`.existing`) | `pr_exists` | no | Ya hay un PR abierto para esa cabeza |
| `NoCommitsBetween` | `no_commits` | no | PR sin commits entre base y cabeza |
| `ForgeValidationError` | `validation` | no | Otros 422 o verificaciones de REQ-FORGE-8..11 |
| `ForgeUnsupported` | `unsupported` | no | Capacidad ausente (GraphQL bloqueado, plan) |
| `RateLimited` (`.reset_at`) | `rate_limited` | sí | §7.3 |
| `ForgeUnavailable` | `unavailable` | sí | 5xx tras agotar los reintentos; 409 en `update_ref` y `delete_ref` (§4.2) |
| `NetworkTimeout` | `timeout` | sí | Timeout de conexión o de lectura |
| `NetworkUnreachable` | `unreachable` | sí | DNS o conexión rechazada |
| `NetworkTLSError` | `tls` | no | Falla de TLS (certificado o negociación) |

`BaseNotDefault` es una subclase de `ForgeValidationError` (`kind = "validation"`) que lanza `capabilities()` cuando `forge.base_branch` no es la rama por defecto (§3.2). El núcleo la traduce a `E_CONFIG_BASE_NOT_DEFAULT` (05-operaciones §12.3).

### 2.2 Interfaz de administración (solo `init`; 00-marco D-62)

Las escrituras de configuración del repositorio que pide 06-permisos §4 no están en `ForgeAdapter` (REQ-FORGE-3): están en `ForgeAdmin`, que solo construye `init`, con el token de administración que la persona teclea en la terminal (10-seguridad REQ-SEC-30).

```python
# Ajustes del repositorio que init lee y escribe (06-permisos §4.2; 11-trazabilidad §3.2)
REPO_SETTINGS = ("allow_squash_merge", "allow_merge_commit", "allow_rebase_merge",
                 "squash_merge_commit_title", "squash_merge_commit_message", "allow_auto_merge",
                 "allow_update_branch", "delete_branch_on_merge", "allow_forking", "has_wiki")
# Ajustes de la organización que init informa y no cambia (06-permisos §4.10)
ORG_SETTINGS = ("default_repository_permission", "members_can_fork_private_repositories",
                "members_can_change_repo_visibility", "members_can_delete_repositories",
                "members_can_create_public_repositories", "two_factor_requirement_enabled")
AdminLevel = Literal["repo", "org"]

@frozen
class ActionsPolicy:                        # 06-permisos §4.3 y REQ-PERM-56
    level: AdminLevel
    enabled: bool                           # repo: enabled; org: enabled_repositories != "none"
    allowed_actions: Literal["all", "local_only", "selected"] | None
    sha_pinning_required: bool | None
    github_owned_allowed: bool | None = None    # solo con allowed_actions: selected
    verified_allowed: bool | None = None
    patterns_allowed: tuple[str, ...] | None = None

@frozen
class WorkflowPermissions:                  # 06-permisos REQ-PERM-27
    default_workflow_permissions: Literal["read", "write"]
    can_approve_pull_request_reviews: bool

@frozen
class RulesetInfo:
    id: int; name: str; target: str; enforcement: str
    payload: Mapping[str, object]           # conditions, bypass_actors y rules, como los da la forja

@frozen
class EnvironmentInfo:                      # 06-permisos REQ-PERM-61 (00-marco D-72)
    name: str
    policy: Literal["all", "protected", "custom"]   # deployment_branch_policy: null, protected_branches o propias
    rules: tuple[tuple[Literal["branch", "tag"], str], ...]   # las reglas propias, solo con "custom"

SecretScope = Literal["repo", "org", "environment"]

@runtime_checkable
class ForgeAdmin(Protocol):
    repo: RepoRef
    def identity(self) -> Identity: ...                             # la cuenta del token de administración
    def repo_settings(self) -> dict[str, object]: ...               # solo claves de REPO_SETTINGS
    def update_repo_settings(self, changes: Mapping[str, object]) -> dict[str, object]: ...
    def actions_policy(self, level: AdminLevel = "repo") -> ActionsPolicy | None: ...
    def set_actions_policy(self, *, enabled: bool, allowed_actions: str,
                           sha_pinning_required: bool) -> None: ...   # solo el repositorio
    def workflow_permissions(self, level: AdminLevel = "repo") -> WorkflowPermissions | None: ...
    def set_workflow_permissions(self, *, default: Literal["read", "write"], can_approve: bool) -> None: ...
    def retention_days(self) -> int | None: ...                     # None: el endpoint no existe
    def set_retention_days(self, days: int) -> None: ...
    def collaborator_permission(self, login: str) -> str | None: ...  # role_name; None si no es colaboradora
    def add_collaborator(self, login: str, permission: Literal["pull", "triage", "push", "maintain", "admin"]
                         ) -> Literal["added", "invited"]: ...
    def outside_collaborators(self) -> list[str]: ...
    def user_id(self, login: str) -> int: ...
    def org_settings(self) -> dict[str, object] | None: ...         # solo claves de ORG_SETTINGS
    def ruleset(self, name: str) -> RulesetInfo | None: ...
    def upsert_ruleset(self, name: str, payload: Mapping[str, object]) -> int: ...
    def branch_rules(self, branch: str) -> list[dict[str, object]]: ...  # reglas activas en la rama
    def environment(self, name: str) -> EnvironmentInfo | None: ...
    def put_environment(self, name: str, *, branch: str) -> EnvironmentInfo: ...  # una sola regla: la rama
    def secret_names(self, scope: SecretScope, environment: str | None = None
                     ) -> tuple[str, ...] | None: ...   # solo nombres, nunca valores; None sin permiso
```

- **REQ-FORGE-71:** `ForgeAdmin` DEBE recibir el token explícito, nunca resolverlo de las fuentes de §5.1, y NO DEBE guardar nada en disco: ni respuestas (§7.4), ni identidad, ni capacidades. Solo `init` lo construye.
- **REQ-FORGE-72:** `update_repo_settings` DEBE rechazar con `ForgeValidationError`, sin llamar a la forja, toda clave fuera de `REPO_SETTINGS`, y verificar en la respuesta que se aplicó cada cambio (REQ-FORGE-11). `set_actions_policy` y `set_workflow_permissions` escriben solo en el repositorio: la configuración de la organización afecta a otros repos y la cambia un owner (06-permisos §4.10).
- Las lecturas que pueden no estar permitidas con el token (`actions_policy`, `workflow_permissions` y `org_settings`) devuelven `None` ante un 403 o un 404, para que `init` informe el resultado como desconocido (06-permisos REQ-PERM-56). Con un repo de una cuenta personal, las de nivel `org` devuelven `None` sin llamar a la forja. Desde 00-marco D-65, las mismas lecturas están en `ForgeAdapter` (§2); la implementación GitHub de `ForgeAdmin` las delega en su adaptador, con el token de administración.
- `add_collaborator` devuelve `invited` si GitHub crea una invitación (201) y `added` si la persona ya tenía acceso (204); `init` la usa solo con miembros de la organización (06-permisos REQ-PERM-55).
- **REQ-FORGE-74 (00-marco D-72):** `put_environment` DEBE dejar el environment con `policy: "custom"` y una sola regla, `("branch", branch)`: crea la que falta, borra las demás y verifica el resultado con una lectura (REQ-FORGE-11). No toca revisores, temporizador ni secretos. `secret_names` devuelve solo los nombres de los secretos de Actions del repositorio, de los de la organización visibles para el repositorio o de los de un environment, y `None` ante un 403 o un 404, para que `init` informe el resultado como desconocido.

## 3. Capacidades y perfiles

### 3.1 Qué se detecta y con qué evidencia

| Campo | Fuente (GitHub) | Permiso fine-grained |
|---|---|---|
| `visibility`, `owner_type`, `default_branch`, `viewer_permission` | `GET /repos/{o}/{r}` (`visibility`, `owner.type`, `default_branch`, `permissions.{admin,maintain,push,triage,pull}`) | Metadata: read |
| `rules_available` | Público → `true` (en todos los planes hay ramas protegidas y rulesets para repos públicos); interno → `true`; privado → `true` solo si el plan es visible (abajo) y distinto de `free` (`false` si es `free`); si no es visible, sondeo `GET /repos/{o}/{r}/rulesets?per_page=1`, que solo puede dar `false` (403 con "upgrade") o `null`. Un 200 de GET rulesets o de rules/branches no prueba que se exija: GitHub deja crear rulesets no aplicados en repos privados Free | Metadata: read |
| plan | `GET /orgs/{org}` → `plan.name`, que solo ven los owners de la organización. Si el repo es del propio visor, `GET /user` → `plan.name` | ninguno |
| `base_rules` | `GET /repos/{o}/{r}/rules/branches/{base}` → `source="ruleset"` si hay reglas (solo activas: excluye rulesets en `evaluate` o `disabled`); si no, `GET /repos/{o}/{r}/branches/{base}`: si `protected` → `source="classic"`, `required_checks` de `protection.required_status_checks.contexts` si viene y el resto `None`; si no, `source="none"` | Metadata: read (rules/branches); Contents: read (branches) |
| `code_owners_enforceable` | Igual que `rules_available`: CODEOWNERS obligatorio tiene el mismo condicionamiento de plan que las ramas protegidas | — |
| `push_rulesets` | `true` solo si `visibility` no es `public` y `rules_available` es `true`: GitHub solo admite push rulesets en repos privados o internos [supuesto: mismo condicionamiento de plan que los rulesets de rama]. Lo usa el ruleset `aucove-workflows` de 06-permisos | — |
| `checks_api` | `false` si el token es fine-grained (no puede usar la API de Checks). Si no, `true` hasta el primer 403 | — |

### 3.2 Algoritmo

```python
def detect(gh, base_branch: str) -> Capabilities:
    repo = gh.get_json(f"/repos/{o}/{r}")                    # 1 GET
    if repo["default_branch"] != base_branch:                # Closes #n solo cierra en la rama por defecto
        raise BaseNotDefault(default_branch=repo["default_branch"])   # → E_CONFIG_BASE_NOT_DEFAULT
    vis = repo["visibility"]                                 # public | private | internal
    ev = [f"GET /repos/{o}/{r} -> 200 visibility={vis}"]
    if vis in ("public", "internal"):
        rules = True
    else:
        plan = gh.visible_plan(repo)                         # 0-1 GET; None si no es visible
        if plan is not None:
            rules = plan != "free"                           # [supuesto] valor "free" (Q-FORGE-4)
            ev.append(f"plan={plan}")
        else:
            st, body = gh.probe(f"/repos/{o}/{r}/rulesets?per_page=1")   # 1 GET
            ev.append(f"GET rulesets -> {st}")
            msg = body.get("message", "").lower()
            if st == 403 and "upgrade" in msg:
                rules = False                                # [supuesto] (Q-FORGE-4)
            else:
                rules = None                                 # un 200 no prueba que se exija (§3.1)
    base = gh.read_base_rules(base_branch) if rules is not False else None   # 1-2 GET (§3.1)
    profile = "enforced" if rules is True else "advisory"
    return Capabilities(..., rules_available=rules, code_owners_enforceable=rules,
                        push_rulesets=(vis != "public" and rules is True),
                        base_rules=base, profile=profile, evidence=tuple(ev))
```

- **REQ-FORGE-12:** La detección DEBE basarse en respuestas de la API y NO DEBE requerir que el visor pueda leer el plan. Si el plan no es visible, DEBE decidir el sondeo.
- **REQ-FORGE-13:** La detección NO DEBE crear, modificar ni borrar nada en la forja. Los sondeos son solo GET.

### 3.3 Mapeo a perfiles

| `visibility` | `rules_available` | `profile` | Aviso |
|---|---|---|---|
| `public` / `internal` | `true` | `enforced` | `W_FORGE_RULES_NOT_APPLIED` solo si `base_rules.source == "none"` |
| `private` | `true` (plan visible distinto de `free`) | `enforced` | `W_FORGE_RULES_NOT_APPLIED` solo si `base_rules.source == "none"` |
| `private` | `false` (plan `free` o sondeo 403 "upgrade") | `advisory` | — |
| `private` | `null` | `advisory` | `W_FORGE_CAPABILITY_UNKNOWN` |

- **REQ-FORGE-14:** Si la detección no es concluyente (`rules_available = null`), el perfil DEBE ser `advisory`: el núcleo nunca promete una garantía que no comprobó.
- **REQ-FORGE-15:** El perfil describe lo que la forja **puede** exigir, no lo que ya está aplicado. Que estén aplicadas se informa en `base_rules`. Qué hacer si faltan lo decide 06-permisos.
- **REQ-FORGE-65:** `init` NO DEBE informar protecciones aplicadas por el solo hecho de que `upsert_ruleset` responda 201 si `rules_available` no es `true`.

### 3.4 Caché y revalidación

Se guarda en `.aucove/cache/capabilities.json` (clase `local`; árbol en 01-arquitectura §7.3). Ejemplo:

```json
{
  "schema": "https://aucove.invalid/schemas/capabilities/v1",
  "key": {"forge": "github", "repo": "lab-ejemplo/articulo", "base_branch": "main",
          "token_fp": "3f9a1c0b2d4e", "login": "ana-garcia"},
  "detected_at": "2026-09-29T14:05:00Z",
  "expires_at": "2026-09-30T14:05:00Z",
  "visibility": "private", "owner_type": "Organization", "viewer_permission": "admin",
  "default_branch": "main", "rules_available": true, "code_owners_enforceable": true,
  "base_rules": {"source": "ruleset", "required_approvals": 1, "require_code_owner_review": true,
                 "require_last_push_approval": true, "dismiss_stale_reviews_on_push": true,
                 "required_checks": ["aucove/build", "aucove/validate"], "blocks_force_push": true},
  "profile": "enforced", "push_rulesets": true,
  "checks_api": false, "transport": "http", "write_mode": "git",
  "evidence": ["GET /repos/lab-ejemplo/articulo -> 200 visibility=private",
               "plan=team", "GET rules/branches/main -> 200 (4 reglas)"]
}
```

- **REQ-FORGE-17:** La caché DEBE invalidarse cuando cambie cualquier campo de `key`. También vence a las 24 h.
- **REQ-FORGE-18:** `doctor`, `init` y `sync-roles` DEBEN detectar siempre de nuevo (`refresh=True`), y el CI NO DEBE usar caché en disco.
- **REQ-FORGE-19:** Un 401 DEBE invalidar la identidad y las capacidades. Un 403 o 404 en un endpoint que la caché daba por accesible DEBE provocar una sola redetección antes de devolver el error.
- **REQ-FORGE-20:** Toda operación con red DEBERÍA revalidar `GET /repos/{o}/{r}` con `If-None-Match` como máximo cada 10 min. Si cambian `visibility` o `permissions`, DEBE redetectar y avisar si cambió el perfil.
- **REQ-FORGE-21:** Si no hay caché y no hay red, `capabilities()` DEBE lanzar el error de red correspondiente. Los hooks, que no tienen red, DEBEN asumir `advisory`.

## 4. Implementación GitHub

### 4.1 Convenciones HTTP

- Base: `https://api.github.com`, o `GITHUB_API_URL` si `GITHUB_ACTIONS=true`. GitHub Enterprise Server queda reservado.
- Cabeceras obligatorias: `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2026-03-10`, `User-Agent: aucove/<versión>`, `Authorization: Bearer <token>` (esta última no se envía con el transporte `gh`, §8).
- **REQ-FORGE-22:** El adaptador DEBE fijar `X-GitHub-Api-Version: 2026-03-10`. Por eso NO DEBE usar el campo singular `assignee` ni `merge_commit_sha`, que esa versión eliminó. La versión 2022-11-28 tiene soporte hasta el 2028-03-10. Las pruebas de contrato y los spikes S1 y S3 (12-mvp §4) DEBEN enviar la misma cabecera y registrarla con su resultado; un resultado obtenido con otra versión no valida al adaptador. El script de S1 usa `gh api`, que DEBE recibir `-H "X-GitHub-Api-Version: 2026-03-10"`, como en REQ-FORGE-55.
- **REQ-FORGE-23:** Los timeouts DEBEN ser 5 s de conexión y 20 s de lectura por petición.
- **REQ-FORGE-24:** Ante un error, el adaptador DEBE registrar en el log de depuración el método, la ruta, el estado y `X-GitHub-Request-Id`, y nunca cabeceras de autorización.

### 4.2 Métodos y endpoints

Rutas relativas a `/repos/{o}/{r}` salvo que empiecen por `/`. Permisos de token fine-grained: R = read, W = write. Los marcados con `*` no aparecen en la tabla oficial consultada y se infieren; los valida `CT-AUTH-01` (§10.3) leyendo `X-Accepted-GitHub-Permissions`.

| Método | Endpoint(s) | Permiso | Éxito → resultado | Errores → excepción |
|---|---|---|---|---|
| `identity` | `GET /user` (en CI no hay llamada: REQ-FORGE-43) | ninguno | 200 | 401 → `ForgeAuthError` |
| `get_ref` | `GET git/ref/{namespace}/{branch}` | Contents R | 200 → `Ref` | 404 → `None` |
| `list_refs` | `GET git/matching-refs/{namespace}/{prefix}` | Contents R | 200 → lista (vacía si no hay) | — |
| `create_ref` | `POST git/refs` `{"ref":"refs/{namespace}/{b}","sha":…}` | Contents W | 201 | 409/422 → §4.4 |
| `update_ref` | `PATCH git/refs/heads/{b}` `{"sha":…,"force":false}` | Contents W | 200 | 422 → `RefConflict` tras GET de la ref; 409 → `ForgeUnavailable` |
| `delete_ref` | `DELETE git/refs/{namespace}/{b}` | Contents W | 204 → `True` | 422 o 404 → GET de la ref: si no existe → `False`; si existe → `ForgeValidationError` (o `RateLimited` si hay señal de límite); 409 → `ForgeUnavailable` |
| `list_ref_activity` | `GET /repos/{o}/{r}/activity?ref=refs/heads/{b}&per_page=100` → `activity_type`, `actor`, `timestamp`, `before`, `after` | Contents R | 200 → lista | 404 → `None` |
| `create_empty_commit` | `GET git/commits/{parent}` (árbol) + `POST git/commits` `{message, tree, parents:[parent]}` | Contents W | 201 → sha | 404, 422 |
| `commit_files` | `POST git/blobs`, `POST git/trees`, `POST git/commits`, `PATCH git/refs/heads/{b}` | Contents W | §4.5 | §4.5 |
| `merge_into` | `POST merges` `{base: branch, head: source, commit_message}` | Contents W | 201 → sha; 204 → `None` (ya integrado) | 409 → `MergeConflict`; 404 → `ForgeNotFound` |
| `get_commit` | `GET commits/{sha}` → `sha`, `commit.message`, `author.login`, `commit.author.date`, `commit.committer.date` | Contents R | 200 → `CommitInfo` | 404/422 → `ForgeNotFound` |
| `get_file` | `GET contents/{path}?ref={ref}`; si pesa más de 1 MB, repetir con `Accept: application/vnd.github.raw+json` | Contents R | 200 | 404 → `None` |
| `compare` | `GET compare/{base}...{head}?per_page=1`; con `with_commits=True`, sin `per_page=1`, y `commits` recibe hasta 250 | Contents R | 200 | 404 → `ForgeNotFound` |
| `codeowners_errors` | `GET codeowners/errors?ref={ref}` | Contents R | 200 → mensajes | 404 → `[]` |
| `get_issue` | `GET issues/{n}` | Issues R* | 200 | 404/410 → `None` |
| `list_issues` | `GET issues?labels=…&state=…&since=…&per_page=100` | Issues R* | 200 sin PR | — |
| `create_issue` | `POST issues` `{title, body, labels}` | Issues W (en un repo público, GitHub no lo exige: con un token sin permisos abre el Issue igual; 00-marco D-68) | 201 + REQ-FORGE-11 | 410, 422 |
| `update_issue` | `PATCH issues/{n}` (`state_reason`: `completed`, `not_planned`, `reopened`, `duplicate`) | Issues W | 200 | 422 |
| `add_labels` / `remove_label` | `POST issues/{n}/labels` / `DELETE issues/{n}/labels/{name}` | Issues W (en PR: Q-FORGE-11) | 200; volver a agregar una etiqueta presente puede registrar otro `labeled` (00-marco D-68) | `DELETE` 404 → idempotente, devuelve las actuales |
| `ensure_labels` | `GET labels/{name}`, `POST labels`, `PATCH labels/{name}` | Issues W | 200/201 → la acción de cada etiqueta (`created`, `updated` o `unchanged`) | 422 al crear → releer |
| `list_issue_events` | `GET issues/{n}/events?per_page=100`, filtrado a `labeled`/`unlabeled` | Issues R | 200 → `LabelEvent` en orden cronológico | 404/410 → `ForgeNotFound` |
| `set_assignees` | `PATCH issues/{n}` `{assignees}`: reemplaza el conjunto | Issues W | 200, si el conjunto devuelto es el pedido según `same_login` (§6.3) | si no lo es → `ForgeValidationError` (REQ-FORGE-11) |
| `add_assignees` / `remove_assignees` | `POST` / `DELETE issues/{n}/assignees` `{assignees}`; `add_assignees` solo en `CT-ISS-01` | Issues W | 201 / 200 + REQ-FORGE-11 | — |
| `list_comments` / `create_comment` / `update_comment` | `GET`, `POST issues/{n}/comments`; `PATCH issues/comments/{id}` | Issues R*/W | 200/201 | 404 |
| `get_pull_request` / `find_pull_request` | `GET pulls/{n}` (de aquí sale `merged_by`: las listas no lo traen) / `GET pulls?head={o}:{b}&state=…` | Pull requests R* | 200 | 404 → `None` |
| `list_pull_requests` / `list_pull_request_files` | `GET pulls?state=…&base=…` / `GET pulls/{n}/files` | Pull requests R* | 200 | — |
| `pulls_for_commit` | `GET commits/{sha}/pulls` (00-marco D-34 h). Da PR resumidos, sin `merged` ni `merged_by`: `merged` sale de `merged_at`, y `merged_by` solo de `get_pull_request` | Pull requests R* | 200 → lista, vacía si el commit no tiene PR | — |
| `list_pull_request_commits` | `GET pulls/{n}/commits?per_page=100` (GitHub devuelve hasta 250) | Pull requests R | 200 → `CommitInfo` | 404 → `ForgeNotFound` |
| `create_pull_request` | `POST pulls` `{title, head, base, body, draft}` | Pull requests W | 201 | 422 → §4.7 |
| `update_pull_request` | `PATCH pulls/{n}` `{title, body, state}` (no acepta `draft`) | Pull requests W | 200 | 422 |
| `mark_ready_for_review` | Reservado: ninguna operación de v0.1 lo invoca. GraphQL `markPullRequestReadyForReview` (§4.3) | Pull requests W* | PR | → `ForgeUnsupported` |
| `request_reviewers` | `POST pulls/{n}/requested_reviewers` `{reviewers}` | Pull requests W | 201 | 422 |
| `list_reviews` | `GET pulls/{n}/reviews?per_page=100` (orden cronológico) | Pull requests R | 200 | — |
| `submit_review` | `POST pulls/{n}/reviews` `{commit_id, body, event}` | Pull requests W | 200 | 422 (p. ej., el autor se aprueba a sí mismo) |
| `get_checks` | §4.6 | Actions R, Commit statuses R | 200 | — |
| `get_workflow_run` | `GET actions/runs/{run_id}` | Actions R | 200 → `WorkflowRun` | 404 → `ForgeNotFound` |
| `last_successful_run` | `GET actions/workflows/{f}/runs?branch=…&status=success&per_page=1` (00-marco D-34 h; REQ-CI-33) | Actions R | 200 → el run más reciente, o `None` si no hay | 404 → `ForgeNotFound` |
| `successful_runs` | `GET actions/workflows/{f}/runs?branch=…&event=…&status=success&per_page=…` | Actions R | 200 → lista de `WorkflowRun`, más recientes primero (REQ-FORGE-69) | 404 → `ForgeNotFound` |
| `repo_settings` | `GET /repos/{o}/{r}` (la respuesta de `capabilities` del mismo proceso, si la hay; 00-marco D-65) | Metadata R; los ajustes de integración, Contents W | 200 → las claves de `REPO_SETTINGS` que vienen | — |
| `rulesets` | `GET rulesets?includes_parents=false` y `GET rulesets/{id}` de cada uno | Metadata R (`bypass_actors`, solo a quien puede editar el ruleset) | 200 → `RulesetInfo` | 403/404 sin el plan → `ForgeUnsupported` |
| `list_labels` | `GET labels?per_page=100` | Issues R* | 200 → `Label` | — |
| `environment` | `GET environments/{name}` y, con reglas propias, `GET environments/{name}/deployment-branch-policies` (00-marco D-72) | Actions R | 200 → `EnvironmentInfo` | 404 → `None`; 403 → `ForgePermissionError` |
| `collaborator_permission`, `outside_collaborators`, `actions_policy`, `workflow_permissions`, `org_settings` | como en `ForgeAdmin` (abajo), con el token de la persona (00-marco D-65) | ídem | ídem | ídem; `outside_collaborators` exige escritura en el repo (403 → `ForgePermissionError`) |

Métodos de `ForgeAdmin` (§2.2; solo `init`, con el token de administración):

| Método | Endpoint(s) | Permiso | Éxito → resultado | Errores → excepción |
|---|---|---|---|---|
| `identity` | `GET /user` | ninguno | 200 | 401 → `ForgeAuthError` |
| `repo_settings` / `update_repo_settings` | `GET` / `PATCH /repos/{o}/{r}` | Administration R / W | 200 → las claves de `REPO_SETTINGS` | 403 → `ForgePermissionError`; cambio no aplicado → `ForgeValidationError` |
| `actions_policy` | `GET actions/permissions` y, con `selected`, `GET actions/permissions/selected-actions`; con `org`, las mismas rutas bajo `/orgs/{org}` | Administration R (org: Organization administration R) | 200 → `ActionsPolicy` | 403/404 → `None` |
| `set_actions_policy` | `PUT actions/permissions` `{enabled, allowed_actions, sha_pinning_required}` | Administration W | 204 | 403 → `ForgePermissionError` |
| `workflow_permissions` / `set_workflow_permissions` | `GET` / `PUT actions/permissions/workflow` (org: `GET /orgs/{org}/actions/permissions/workflow`) | Administration R / W | 200 / 204 | `GET` 403/404 → `None` |
| `retention_days` / `set_retention_days` | `GET` / `PUT actions/permissions/artifact-and-log-retention` (por confirmar en S5) | Administration R / W | 200 → `days` / 204 | 404 → `None` / `ForgeUnsupported`; 422 (sobre el máximo) → `ForgeValidationError` |
| `collaborator_permission` | `GET collaborators/{u}/permission` → `role_name` | Metadata R | 200 | `permission: none` o 404 → `None` |
| `add_collaborator` | `PUT collaborators/{u}` `{permission}` | Administration W | 201 → `invited`; 204 → `added` | 403, 422 |
| `outside_collaborators` | `GET collaborators?affiliation=outside` | Metadata R | 200 → logins | — |
| `user_id` | `GET /users/{u}` | ninguno | 200 → `id` | 404 → `ForgeNotFound` |
| `org_settings` | `GET /orgs/{org}` (los campos de `ORG_SETTINGS` solo los ve un owner) | ninguno | 200 → las claves presentes | 403/404 → `None` |
| `ruleset` | `GET rulesets` y `GET rulesets/{id}` | Administration R | 200 → `RulesetInfo` o `None` | 403/404 sin el plan → `ForgeUnsupported` |
| `upsert_ruleset` | `GET rulesets`, `POST rulesets` o `PUT rulesets/{id}` | Administration W | 201/200 → id (no prueba que se exija: REQ-FORGE-65) | 403/404/422 → `ForgeUnsupported` si el plan no lo permite |
| `branch_rules` | `GET rules/branches/{b}` | Metadata R | 200 → reglas activas | — |
| `environment` | como en `ForgeAdapter` (arriba) | Actions R | ídem | ídem |
| `put_environment` | `PUT environments/{name}` `{"deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}}`; `GET`, `POST` `{name, type: "branch"}` y `DELETE environments/{name}/deployment-branch-policies[/{id}]` | Administration W | 200 → `EnvironmentInfo` con una sola regla | 403 → `ForgePermissionError`; 404/422 sin environments en el plan → `ForgeUnsupported` |
| `secret_names` | `GET actions/secrets`, `GET actions/organization-secrets` o `GET environments/{name}/secrets` | Secrets R (environment: Environments R) | 200 → los nombres | 403/404 → `None` |

### 4.3 Pasar un PR de borrador a listo

Verificado: `PATCH /repos/{o}/{r}/pulls/{n}` no acepta `draft`. `POST pulls` sí acepta `draft` al crear. El cambio de borrador a listo solo existe en GraphQL: `markPullRequestReadyForReview(input: {pullRequestId})` (y el inverso, `convertPullRequestToDraft`). No hay endpoint REST.

Diseño:

- **REQ-FORGE-25:** El flujo de v0.1 NO DEBE necesitar convertir un borrador en listo. El PR de una tarea DEBE crearse con `draft=false` en `submit` (04-tareas REQ-TASK-18).
- **REQ-FORGE-26 (reservado):** Si una versión posterior implementa `mark_ready_for_review`, DEBE hacerlo con esa única mutación GraphQL (`POST /graphql`, con el `node_id` del PR) y usarlo solo cuando ya exista un PR en borrador, por ejemplo uno abierto a mano en la web. Ninguna operación de v0.1 lo invoca.
- **REQ-FORGE-27 (reservado):** En esa implementación, si GraphQL falla con 403 (en sesiones web: `This GraphQL query is not enabled for this session`) o con cualquier error, el método DEBE lanzar `ForgeUnsupported` con un `hint` que incluya la URL del PR y la indicación de marcarlo como listo en la web.
- `CT-PR-03` y `CT-WEB-03` (§10.3) no son condición para publicar v0.1.
- Se descartó emular el cambio cerrando el borrador y abriendo un PR nuevo, porque se pierden la conversación y los enlaces.

Consecuencia adicional: GitHub no permite abrir un PR sin commits entre la base y la cabeza (responde 422; comportamiento observado, no documentado, que valida `CT-PR-02`). Recién creada, `task/<n>` apunta al mismo commit que la base, así que ni siquiera un borrador se puede abrir al reclamar, salvo con el commit de reclamo de §4.4.

### 4.4 Crear una ref que ya existe (D-10)

Verificado: la documentación de `POST /repos/{o}/{r}/git/refs` lista 201, 409 y 422, pero **no dice** qué pasa si la ref ya existe. Se ha observado 422 con `"message": "Reference already exists"` [supuesto]. El adaptador no depende del estado ni del mensaje:

```python
def create_ref(self, branch: str, sha: str, *, namespace: RefNamespace = "heads") -> Ref:
    try:
        r = self._post("git/refs", {"ref": f"refs/{namespace}/{branch}", "sha": sha})
        return Ref(branch, r["object"]["sha"])
    except HttpError as e:
        if e.status not in (409, 422):
            raise self._classify(e)
        existing = self.get_ref(branch, namespace=namespace)   # GET de confirmación
        if existing is not None and existing.sha == sha:
            return existing                              # ya existe con el sha pedido: éxito
        if existing is not None:
            raise RefAlreadyExists(existing=existing)    # quien la creó primero gana
        raise ForgeValidationError(e.message)            # 422: sha o nombre inválido; 409: repo vacío
    except (NetworkTimeout, ForgeUnavailable):           # respuesta ambigua
        existing = self.get_ref(branch, namespace=namespace)
        if existing is not None and existing.sha == sha:
            return existing                              # ver "commit de reclamo"
        if existing is not None:
            raise RefAlreadyExists(existing=existing)
        raise
```

**Commit de reclamo (adoptado por 04-tareas §2.2, paso 4).** Si dos personas reclaman desde el mismo `sha` de la base, tras un timeout no se puede saber quién creó la ref. Como quien reclama crea antes un commit sin cambios (`create_empty_commit(parent=<sha base>, message=…)`, con autor = la cuenta autenticada y mensaje con el trailer `Task: #<n>`), el `sha` de la ref identifica al ganador: si la ref existe con el `sha` pedido, la creó este intento. Además, deja la rama con un commit, así que el PR se puede abrir enseguida. Cuesta una lectura y una escritura más. No es prueba de identidad: el autor de un commit se puede falsear.

- **REQ-FORGE-28:** `create_ref` DEBE lanzar `RefAlreadyExists` cada vez que la ref exista después del intento con un `sha` distinto del pedido, con cualquier código de estado. Si existe con el `sha` pedido, DEBE devolverla como éxito.
- **REQ-FORGE-29:** `create_ref` NO DEBE reintentar a ciegas un `POST git/refs`. Tras una respuesta ambigua DEBE hacer un `GET` de confirmación.
- **REQ-FORGE-30:** La versión v0.1 NO DEBE publicarse si las pruebas `CT-REF-01` a `CT-REF-04` (§10.3) no pasan contra github.com. En particular, `CT-REF-03` exige un solo ganador entre 10 creaciones concurrentes.
- **REQ-FORGE-31:** `update_ref` DEBE enviar siempre `force: false`. GitHub solo acepta el avance rápido, y eso hace de CAS: si la rama avanzó, el commit nuevo ya no desciende de ella y la actualización falla.
- `delete_ref` no tiene condición en GitHub. Entre leer y borrar hay una ventana de carrera que la operación que lo usa debe tolerar (04-tareas).

**Reclamo por push con lease (puerta web; 00-marco D-41 y D-42).** En una sesión web el proxy responde 403 a `POST git/commits` y `POST git/refs`, pero acepta `git push`. S3-bis comprobó que `git push --force-with-lease=<ref>:` (lease vacía: la ref no debe existir) tiene un solo ganador entre 10 pushes concurrentes (`CT-PUSH-01`).

- **REQ-FORGE-67:** En una sesión web (`CLAUDE_CODE_REMOTE=true`, `write_mode="git"`), `create_empty_commit` DEBE crear el commit en el clon local (`git commit-tree <parent>^{tree} -p <parent>`, con el mensaje sin alterar) con autor y committer = la identidad de `identity()` (`<login>` y `<id>+<login>@users.noreply.github.com`), y `create_ref` DEBE publicarlo con `git push --porcelain --force-with-lease=refs/heads/<b>: origin <sha>:refs/heads/<b>`. Después de un push aceptado, de un rechazo (`stale info`, `remote rejected`, `fetch first`) o de una falla ambigua, DEBE hacer el `GET` de confirmación y aplicar REQ-FORGE-28; si la ref no existe tras un rechazo, `ForgeValidationError`. Como en REQ-FORGE-29, NO DEBE repetir el push sin ese `GET`. Fuera de la web, `create_ref` sigue con `POST git/refs` (D-10).
- **REQ-FORGE-68:** En una sesión web, `update_ref` DEBE ser `git push origin <sha>:refs/heads/<b>` sin `--force` (un rechazo por no ser avance rápido da `RefConflict` tras el `GET` de la ref), y las operaciones NO DEBEN llamar a `delete_ref`, `commit_files` ni `merge_into`, que el proxy bloquea (borrar una rama da 403 también por push).

### 4.5 Commits por API (`commit_files`)

Se usa cuando `write_mode="api"` (§8). En v0.1 ninguna superficie lo usa por defecto: la web escribe con `git push` (REQ-FORGE-56; 00-marco D-41); queda para `AUCOVE_FORGE_WRITE_MODE=api` y para forjas o superficies que solo admitan escrituras por API. Algoritmo:

1. `GET git/commits/{expected_head}` → `tree.sha`.
2. Por cada cambio: si se borra, entrada `{"path", "mode":"100644", "type":"blob", "sha": null}`; borrar una ruta que no está en el árbol de `expected_head` da 422 (`GitRPC::BadObjectState`) → `ForgeValidationError` (00-marco D-68). Si es texto UTF-8 de hasta 1 MiB, entrada con `content`. Si no, `POST git/blobs {"content": <base64>, "encoding": "base64"}` y entrada con `sha`. El modo es `100755` si `executable`.
3. `POST git/trees {"base_tree": <tree.sha>, "tree": [...]}`. Sin `base_tree`, todo lo demás quedaría borrado.
4. `POST git/commits {"message", "tree", "parents": [expected_head, *extra_parents]}`. El autor por defecto es la cuenta autenticada, con la fecha actual.
5. `PATCH git/refs/heads/{branch} {"sha": <nuevo>, "force": false}`. Si responde 422, `GET` de la ref: si ya apunta al commit nuevo, hubo un reintento y es éxito. Si no, `RefConflict(current=<sha>)`.

- **REQ-FORGE-32:** `commit_files` DEBE usar `base_tree` y DEBE rechazar con `ForgeValidationError` cualquier ruta bajo `.github/workflows/`, que además exige el permiso Workflows y es de clase `lead`.
- **REQ-FORGE-33:** El mensaje DEBE llegar sin alteraciones: el adaptador no agrega ni quita trailers (11-trazabilidad).
- Blobs, árboles y commits se identifican por su contenido: repetirlos es idempotente. El único paso con estado es el 5.
- El adaptador no valida permisos por ruta. La operación que llama a `commit_files` DEBE validar antes con `check_path`.

### 4.6 Estado de los checks

Los tokens fine-grained **no pueden** usar la API de Checks (limitación documentada), y GitHub Actions informa sus resultados como check runs. Por eso:

- Si `checks_api` no es `false`: `GET commits/{sha}/check-runs?filter=latest&per_page=100` y `GET commits/{sha}/status`.
- Si es `false`, o ante el primer 403: `GET actions/runs?head_sha={sha}&per_page=100` (Actions R), con granularidad de workflow, y `GET commits/{sha}/status` (Commit statuses R).
- `overall` vale `failure` si alguna conclusión es `failure`, `timed_out`, `cancelled` o `action_required`, o si el estado combinado es `failure`. Vale `pending` si algo no terminó, `success` si todo terminó con `success`, `neutral` o `skipped`, y `none` si no hay resultados.
- **REQ-FORGE-70:** Desde H3 (00-marco D-56), `upsert_check_run` DEBE buscar el check run con ese nombre en `sha` (`GET commits/{sha}/check-runs?check_name=<name>`) y actualizarlo (`PATCH check-runs/{id}`), o crearlo si no existe (`POST check-runs`), para que haya uno solo por cabeza (09-ci REQ-CI-59). Solo lo usa `ci editor-review` con el `GITHUB_TOKEN` de Actions, que sí puede usar la API de Checks (los tokens fine-grained no).
- **REQ-FORGE-34:** `get_checks` DEBE indicar la fuente de cada resultado en `CheckRun.source` y NO DEBE mezclar sin marcar los resultados de job con los de workflow.

### 4.7 Fallas ambiguas e idempotencia

| Escritura | Idempotente por sí misma | Recuperación tras un timeout o un 5xx |
|---|---|---|
| `create_ref` | no | §4.4 |
| `update_ref`, `commit_files` (paso 5) | sí, si el `sha` es el mismo | `GET` de la ref |
| `add_labels`, `remove_label`, asignados, `update_*` | sí (conjuntos o reemplazos) | reintentar |
| `create_issue`, `create_comment` | no | Buscar el marcador `<!-- aucove:op v1 id=<op_id> -->` (gramática: 00-marco §5.14) que el adaptador agrega al final del cuerpo, con `GET issues?creator={login}&since={t0}` o `GET issues/{n}/comments?since={t0}` |
| `create_pull_request` | no | `find_pull_request(head)` |
| `submit_review` | no | `list_reviews` y buscar una revisión del visor posterior a t0 |

- **REQ-FORGE-35:** Un POST no idempotente NO DEBE reintentarse sin antes buscar el efecto de la tabla. Si lo encuentra, DEBE devolverlo como éxito.
- **REQ-FORGE-36:** Ante un 422 en `create_pull_request`, el adaptador DEBE lanzar `PullRequestExists` si `find_pull_request(head, state="open")` encuentra uno, `NoCommitsBetween` si `compare(base, head).ahead_by == 0`, y `ForgeValidationError` en cualquier otro caso.

## 5. Autenticación

### 5.1 Orden de las fuentes del token

```python
def resolve_token(env: Mapping[str, str], run) -> tuple[str, TokenSource]:
    for var in ("AUCOVE_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        value = env.get(var, "").strip()
        if value:
            return value, var
    r = run(["gh", "auth", "token", "--hostname", "github.com"], timeout=5)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip(), "gh_auth_token"
    raise NoTokenError(hint="Define GH_TOKEN con un token fine-grained o ejecuta 'gh auth login'.")
```

- **REQ-FORGE-37:** Las fuentes DEBEN consultarse en este orden: `AUCOVE_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN` y `gh auth token`. Es el mismo orden de `gh` (GH_TOKEN antes que GITHUB_TOKEN), con una variable propia al principio para usar un token distinto del de `gh`.
- **REQ-FORGE-38:** `gh auth token` DEBE ejecutarse sin shell y con un timeout de 5 s. Si falla, se pasa a `NoTokenError`, nunca a un aviso interactivo.
- El `aucove login` con device flow y llavero del sistema queda **reservado**. v0.1 no lo implementa y no ocupa nombres del registro.

### 5.2 Clasificación del token y tokens de alcance amplio

| Prefijo | `token_kind` | Cómo se determina `token_scope` |
|---|---|---|
| `github_pat_` | `fine_grained_pat` | `doctor`: `GET /user/repos?per_page=2`. Un solo repo → `repo_restricted`; si no, `owner_wide` |
| `ghp_` | `classic_pat` | `X-OAuth-Scopes` contiene `repo` → `broad` |
| `gho_` | `oauth` (lo que guarda `gh auth login`) | igual: `gh` pide como mínimo `repo`, `read:org` y `gist` → `broad` |
| `ghu_` | `app_user` | limitado a los repos de la instalación → `repo_restricted` (modo completo, reservado) |
| `ghs_` | `app_installation` (incluye `GITHUB_TOKEN`) | `ci` |
| `proxy-injected` (valor literal) | `proxy` | `unknown` (§8) |

- **REQ-FORGE-39:** Con `token_scope = "broad"`, `doctor` y la primera operación con red del día DEBEN emitir `W_AUTH_BROAD_TOKEN`, con la instrucción de crear un token fine-grained limitado al repo. En v0.1 NO DEBEN bloquear la operación (10-seguridad PUEDE endurecerlo).
- **REQ-FORGE-40:** Si `X-OAuth-Scopes` incluye `workflow`, DEBE emitirse `W_AUTH_WORKFLOW_SCOPE`: con ese alcance, el agente puede modificar los workflows.
- Riesgo residual: cualquier token con Pull requests W, aunque sea fine-grained, permite aprobar PR ajenos desde el Bash del agente. Reducir el alcance no lo elimina. El tratamiento es de 10-seguridad.

### 5.3 Permisos mínimos por operación (token fine-grained o `permissions:` de Actions)

Metadata R siempre es obligatorio. La tabla supone los flujos de 04-tareas y 05-operaciones; si un capítulo dueño cambia un flujo, debe actualizarla.

| Operación (CLI) | Contents | Issues | Pull requests | Actions | Commit statuses | Administration |
|---|---|---|---|---|---|---|
| `doctor`, `status`, `context`, `metrics`, `review list`, `review show`, `switch` | R | R | R | R | R | — |
| `validate` (CI) | R | — | R | — | — | — |
| `init` | W | W | — | — | — | W (token de administración, §2.2) |
| `task propose`, `task accept`, `task cancel` | W (solo `cancel`, si borra la ref) | W | — | — | — | — |
| `claim`, `release` | W | W | — | — | — | — |
| `save`, `update` | W | — | W (`update` con PR) | — | — | — |
| `record-assessment` | R | W | W | — | — | — |
| `submit` | W | W | W | R | R | — |
| `decision propose`, `direction propose` | W | W | W | — | — | — |
| `review approve`, `review request-changes` | R | — | W | — | — | — |
| `expire-claims` (CI) | R | W | R | R | — | — |
| `ci seal` (CI) | R | — | W | R | — | — |
| `check-path`, `hook`, `mcp --stdio` (sin red) | — | — | — | — | — | — |

`doctor` lee además, con 00-marco D-65, lo que el token permita: con el token de §5.4 ve los ajustes de integración (Contents W), los rulesets sin sus actores de bypass, las etiquetas y los colaboradores; la política de Actions, el token de los workflows y los ajustes de la organización solo los ve un token con Administration o el de un owner, y sin él quedan como desconocidos.

### 5.4 Token recomendado

- **REQ-FORGE-41:** La documentación de v0.1 DEBE recomendar un token fine-grained con **Only select repositories** (solo el repo del proyecto) y estos permisos: Metadata R, Contents RW, Issues RW, Pull requests RW, Actions R y Commit statuses R. Sin Workflows y sin Administration.
- **REQ-FORGE-42:** Para `init`, el responsable DEBERÍA usar un segundo token de vida corta con Administration RW, y revocarlo después.
- **REQ-FORGE-66:** La documentación de v0.1 DEBE incluir una guía de una página (H4) para crear el token de REQ-FORGE-41, pedir la aprobación del owner y, solo para la excepción de 00-marco D-25, usar `gh auth login` sin `--insecure-storage` y revocar el token al terminar el piloto, porque el token de `gh` no caduca solo.
- Limitaciones verificadas de los tokens fine-grained: no sirven para contribuir a repos donde el usuario es colaborador externo o de repositorio, ni para varias organizaciones a la vez, ni para la API de Checks. Consecuencia: el repo DEBERÍA pertenecer a una organización y los investigadores ser miembros. Si no, solo les queda un token de alcance amplio. El soporte de tokens fine-grained para colaboradores externos está en pausa en la hoja de ruta pública de GitHub (https://github.com/github/roadmap/issues/601), así que no se planifica con él. Exigir la membresía no cuesta más: en un repo privado de una org Team, un colaborador externo también ocupa asiento. Decisión: 00-marco D-25.
- Por defecto, una organización exige que un owner apruebe los tokens fine-grained de sus miembros (el de un owner no la necesita), y puede bloquear los clásicos. `doctor` DEBE traducir el 403 correspondiente en un `hint` que lo explique, y la guía del owner DEBE incluir aprobar los tokens (Settings de la org → Personal access tokens → Pending requests).
- Limitación: con un token sin Workflows, GitHub puede rechazar el push de un merge que trae cambios de la base en `.github/workflows/`. `update` NO DEBE reintentar: `E_FORGE_PUSH_REJECTED` con `details.reason = "workflow_permission"` y el hint "La base cambió workflows: repite `aucove update` con un token que incluya el permiso Workflows". `CT-GIT-03` (§10.3) mide qué vías rechaza GitHub. Medido (00-marco D-68): `POST merges` (`merge_into`) lo acepta y `commit_files` con `extra_parents` da 422; el `git push` del merge y `PUT pulls/{n}/update-branch` quedan por medir.

### 5.5 El token de Actions en CI

- `GITHUB_TOKEN` no es una variable de entorno por defecto: el workflow DEBE pasarla de forma explícita (`env: GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}`) [la sintaxis la fija 09-ci].
- Es un token de instalación (`ghs_`) que vence al terminar el job. Con `permissions:`, todo lo que no se declara queda en `none`.
- **REQ-FORGE-43:** `identity()` DEBE devolver `kind="ci"` solo si `GITHUB_ACTIONS == "true"`, `GITHUB_RUN_ID` no está vacío y el token empieza por `ghs_`; entonces `login = "github-actions[bot]"` (constante del módulo del nombre, 01-arquitectura §3.2) sin llamar a `GET /user`. En cualquier otro caso, `GET /user`. Las operaciones de CI se autorizan por ser CI, no por `roles.yaml`: `Authz.ci_actors = {"github-actions[bot]"}` (06-permisos).
- Límite primario: 1,000 peticiones por hora y por repositorio. Los eventos que genera `GITHUB_TOKEN` (etiquetas, comentarios) no disparan otros workflows, salvo `workflow_dispatch` y `repository_dispatch`. 09-ci no debe encadenar workflows a partir de efectos de `expire-claims`.
- **REQ-FORGE-44:** Las acciones de una persona NO DEBEN ejecutarse con un token de instalación o de bot. La autoría de commits, PR y revisiones DEBE quedar a nombre de la persona. La GitHub App con tokens de usuario (`ghu_`) queda para v0.2, previa prueba con un colaborador externo, y para el modo completo (00-marco D-25).

## 6. Identidad

### 6.1 Obtención

Salvo en CI (REQ-FORGE-43), `identity()` llama a `GET /user` y toma `login`, `id` y `type`. `type = "Bot"` → `kind="bot"`. Si el token es `ghp_` o `gho_`, lee además `X-OAuth-Scopes` de la misma respuesta.

### 6.2 Caché

`.aucove/cache/identity.json` (clase `local`; árbol en 01-arquitectura §7.3), con clave `(forge, token_fp)`. `expires_at` = `fetched_at` + 24 h: hasta entonces la identidad está verificada. Pasado `expires_at`, el adaptador la renueva con `GET /user` antes de usarla; los hooks y `check-path`, que no llaman a la red, PUEDEN seguir usándola como no verificada hasta 7 días después de `fetched_at` (07-hooks REQ-HOOK-40), y después la tratan como ausente:

```json
{"schema": "https://aucove.invalid/schemas/identity/v1", "forge": "github",
 "token_fp": "3f9a1c0b2d4e", "login": "Ana-Garcia", "id": 5829301, "kind": "user",
 "token_source": "GH_TOKEN", "token_kind": "fine_grained_pat", "token_scope": "repo_restricted",
 "oauth_scopes": null, "previous_login": null,
 "fetched_at": "2026-09-29T14:05:00Z", "expires_at": "2026-09-30T14:05:00Z"}
```

- **REQ-FORGE-45:** La caché de identidad DEBE invalidarse si cambia la huella del token o ante un 401. Pasado `expires_at`, el adaptador NO DEBE usarla sin renovarla, y nadie DEBE usarla más de 7 días después de `fetched_at`. Nunca DEBE contener el token.

### 6.3 Comparación con `roles.yaml`

```python
def same_login(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()   # los logins de GitHub son ASCII
```

- **REQ-FORGE-46:** Toda comparación entre un login de la forja y uno de `roles.yaml`, `CODEOWNERS` o una caché DEBE usar `same_login`. Una igualdad que distinga mayúsculas es un defecto.
- **REQ-FORGE-47:** `roles.yaml` DEBE leerse de la rama base (D-08). En CI y en sesiones web se lee con `get_file("roles.yaml", ref=<base_branch>)`, y en local con la ref de la base del clon.
- Si una persona de `roles.yaml` tiene `forge_id` (clave opcional ya definida en `roles.schema.json`, 03-esquemas), se compara primero por id y el login solo sirve para mostrar.

### 6.4 Cambio de login

GitHub permite cambiar el login. El id es durable ("durable user ID instead of their login, which can change over time").

| Situación | Detección | Resultado |
|---|---|---|
| El propio visor cambió de login | Misma huella y mismo `id` en caché, `login` distinto | `previous_login` se rellena. Si `roles.yaml` tiene el login anterior y no el nuevo: `LoginChangedError`, que el núcleo traduce a `E_AUTH_LOGIN_CHANGED` (05-operaciones §12.3), en operaciones de escritura, y aviso en las de lectura. El `hint` indica al responsable que actualice `roles.yaml` y ejecute `sync-roles` |
| Con `forge_id` en `roles.yaml` | El id coincide y el login no | Aviso `W_AUTH_LOGIN_CHANGED`; la operación continúa |
| Otra persona cambió de login | `codeowners_errors(base)` informa usuarios desconocidos; las revisiones llegan con otro `login` | `validate` informa el desajuste (09-ci). Sin `forge_id`, la aprobación de esa persona no se asocia a nadie de `roles.yaml` |

- **REQ-FORGE-48:** Si se detecta un cambio de login sin `forge_id`, el núcleo NO DEBE asociar el login nuevo a la entrada vieja de forma automática: una misma cadena podría pertenecer a otra cuenta (no se verificó si GitHub libera los logins abandonados).

## 7. Límites de la API

### 7.1 Límites vigentes (verificados)

- Primarios: 5,000 peticiones por hora por usuario (PAT u OAuth); 1,000 por hora por repositorio con `GITHUB_TOKEN`; 60 por hora sin autenticar.
- Secundarios: hasta 100 peticiones concurrentes; hasta 900 puntos por minuto en REST (GET, HEAD u OPTIONS valen 1 punto; POST, PATCH, PUT o DELETE, 5); hasta 90 s de CPU por cada 60 s; hasta **80 peticiones que crean contenido por minuto y 500 por hora**.
- Ambos se señalan con 403 o 429, junto con `x-ratelimit-remaining`, `x-ratelimit-reset`, `x-ratelimit-resource` y, si aplica, `retry-after`.

### 7.2 Concurrencia y espaciado

- **REQ-FORGE-49:** Las escrituras DEBEN ser seriales dentro de cada proceso. Las lecturas PUEDEN ir en paralelo, hasta 4 a la vez.
- **REQ-FORGE-50:** Las operaciones por lotes (`init`, `expire-claims`, `ensure_labels`) DEBEN esperar al menos 1 s entre escrituras, como recomienda GitHub.

### 7.3 Reintentos y backoff

| Respuesta | Clasificación | Interactivo (CLI o MCP) | CI |
|---|---|---|---|
| 403/429 con `retry-after` | secundario | Si `retry-after` ≤ 10 s, esperar y reintentar 1 vez. Si no, `RateLimited` | esperar `retry-after`, hasta 3 veces |
| 403/429 con `x-ratelimit-remaining: 0` | primario | `RateLimited(reset_at=x-ratelimit-reset)` sin reintentar | esperar hasta el reset si falta ≤ 15 min; si no, fallar |
| 403/429 de límite secundario ("secondary rate limit" en el mensaje) sin `retry-after` y con `x-ratelimit-remaining` ≠ 0 | secundario | `RateLimited(reset_at = ahora + 60 s)` sin reintentar | esperar 60, 120 y 240 s |
| otro 403 | permiso | `ForgePermissionError` | igual |
| 401 | autenticación | `ForgeAuthError` | igual |
| 500/502/503/504, timeout, DNS o conexión rechazada | transitorio | Idempotente: 3 intentos con esperas de 0.5, 1 y 2 s (±50% de jitter). No idempotente: §4.7 | 5 intentos |
| falla de TLS | `NetworkTLSError` | nunca se reintenta | igual |
| 409/422 | semántico | nunca se reintenta: tabla §4.2 (el 409 de `update_ref` y `delete_ref` se clasifica como `ForgeUnavailable`, sin reintento del adaptador) | igual |

Este apartado es la única política de reintentos; las operaciones no agregan otros (05-operaciones REQ-OPS-19).

- **REQ-FORGE-51:** El adaptador NO DEBE seguir haciendo peticiones mientras esté limitado. Un `RateLimited` DEBE bloquear el adaptador para todo el proceso hasta `reset_at`.

### 7.4 ETag y peticiones condicionales

Verificado: una petición condicional que recibe 304 no cuenta contra el límite primario si va con la cabecera `Authorization` válida.

- **REQ-FORGE-52:** Toda respuesta GET 200 con `ETag` o `Last-Modified` DEBE guardarse en `.aucove/cache/http/` (01-arquitectura §7.3), con clave `(URL con query, Accept, versión de API, token_fp)`, y reenviarse con `If-None-Match` o `If-Modified-Since`. Un 304 DEBE devolver el cuerpo guardado.
- **REQ-FORGE-53:** La caché HTTP DEBE limitarse a 2,000 entradas o 50 MB (LRU). En CI DEBE vivir solo en memoria.
- Con `If-None-Match` la respuesta siempre es fresca, así que también sirve en rutas críticas como `claim`. Aun así, después de escribir, el adaptador lee el recurso concreto (no la lista), porque las listas pueden tardar en reflejar el cambio.

### 7.5 Presupuesto de llamadas por operación

Máximos de peticiones al adaptador sin caché (L = lecturas, E = escrituras, C = escrituras que crean contenido), como fórmulas de `A` = número de refs `task/*`, `P` = número de PR abiertos y `B` = blobs binarios (§4.5). Los verifica el contador del adaptador falso (REQ-FORGE-60).

| Operación | L | E | C | Nota |
|---|---|---|---|---|
| `doctor` | ≤ 19 + P + R (+1 por `branches/{base}`), más las del tablero de `status` si su caché tiene más de 10 min o con `--refresh` | 0 | 0 | P: personas activas de `roles.yaml`; R: rulesets del repositorio. La identidad y `/user/repos` (2), las capacidades (repo, plan, sondeo y reglas: 4), `codeowners_errors`, los ajustes, la lista de rulesets, las etiquetas, el permiso de cada persona, los externos (solo con el `lead`), la política de Actions del repo y de la org (2 más con `selected`), el token de los workflows (repo y org), la org (00-marco D-65) y el último run exitoso de `expire-claims` (09-ci REQ-CI-31); el tablero de `half_states` cuesta lo de la fila de `status`; `branches/{base}` solo si `rules/branches` no da reglas (§3.1) |
| `status`, `context` (refresco) | 6 + 2·P + E | 0 | 0 | En bloque (00-marco D-61): Issues con `task`, Issue abierto con `main-broken`, pulls, `list_refs("task/")`, ref y checks de la base y, por cada PR abierto, revisiones y checks (si P > 30, sin detalle por PR). E: PR propios abiertos o integrados en los últimos 7 días, cuyos comentarios lee la copia de evidencia (05-operaciones REQ-OPS-66; solo `status`). Por cada tarea activa cuya huella cambió (en frío, todas): 2 (eventos y comentarios), 2 más si tiene ref (actividad y `compare`) y 1 por su PR abierto (comentarios). La identidad y las capacidades salen de su caché (§6.2 y §3.2) |
| `claim` | ≤ 12 + 4·A | ≤ 5 | ≤ 2 | 4·A: lecturas por cada ref `task/*` (04-tareas). Para P4, P6 y el paquete de contexto lee el tablero de `status` (00-marco D-61): el límite vale con su caché; en frío se suman las lecturas de 04 §1.1 de cada tarea activa cuya huella cambió (2 por tarea abierta, 2 más por ref y 1 por PR abierto) |
| `release` | ≤ 11 | ≤ 6 | ≤ 1 | Las 7 lecturas de 04 §1.1 de una tarea con ref, sin listar todos los PR (Issue, `find_pull_request`, ref, eventos, comentarios, actividad y `compare`); 2 más con un PR abierto (revisiones y comentarios); las etiquetas `archive/task-<n>-*`, y la ref otra vez si `save_local` subió trabajo. E: cerrar el PR, la etiqueta de archivo, borrar la ref, los asignados y `state:expired`; C: el comentario `release` |
| `save` (`git` / `api`) | 0 / 2 | 0 / 3 + B | 0 | |
| `update` (`api`) | 3 | 1 | 0 | `compare` y `merge_into` |
| `record-assessment` | ≤ 6 + A | ≤ 3 | ≤ 1 | El dueño, el estado y las citas de tareas salen del tablero de `status` (00-marco D-61): el límite vale con su caché de 10 min; sin ella se suman las lecturas en bloque del tablero y las de 04 §1.1 de cada tarea activa cuya huella cambió. En vivo solo lee los comentarios del destino, la etiqueta presente y, con `submission`, el PR abierto; A: un `get_pull_request` por PR citado |
| `submit` | ≤ 10 + 4·A | ≤ 6 | ≤ 3 | Como `claim`, lee el tablero de `status` (00-marco D-61) para el estado, las rondas, los revisores y el paquete de contexto: el límite vale con su caché; en frío se suman las lecturas de 04 §1.1 de cada tarea activa cuya huella cambió (2 por tarea abierta, 2 más por ref y 1 por PR abierto) |
| `task propose` / `accept` | 1 / 2 | 1 / 2 | 1 / 0 | `accept` lee el Issue y sus eventos. Si con eso la tarea no está `proposed` (ya aceptada, o con el Issue cerrado), suma las lecturas de 04 §1.1 de la tarea para saber su estado y no escribe nada |
| `task cancel` | ≤ 10 | ≤ 6 | ≤ 1 | Las 7 lecturas de 04 §1.1 de una tarea con ref (como `release`), 2 más con un PR abierto y las etiquetas `archive/task-<n>-*`. E: cerrar el PR, la etiqueta de archivo, borrar la ref, quitar `state:proposed` o `state:expired`, los asignados y cerrar el Issue; C: el comentario del motivo en el PR |
| `review approve`, `review request-changes` | ≤ 6 | 1 | 1 | El PR, sus archivos, los checks de la cabeza (2: los runs y el estado combinado) y las revisiones, para el resumen, el rol y la idempotencia (05 §9.24); y el PR otra vez tras el reto (`recheck_head`). C: la revisión |
| `expire-claims` (por tarea) | ≤ 8 | ≤ 3 | ≤ 1 | con 1 s entre escrituras |
| `init` | ≤ 37 + P | ≤ 21 + P | ≤ 1 | P: personas activas de `roles.yaml` además del `lead`. Lecturas: la identidad con cada token (2), las capacidades, los ajustes, la política de Actions del repo y de la org (2 más con `selected`), los permisos de los workflows (repo y org), la retención, cada colaborador y los externos, las 12 etiquetas, el id del `lead`, los 3 rulesets (hasta 3 lecturas cada uno), las reglas de la base (REQ-PERM-38), la org y los Issues con `task` (la tarea de preparación, 00-marco D-70). Escrituras: ajustes, Actions, workflows, retención, colaboradores, etiquetas y rulesets (00-marco D-62), y crear la tarea de preparación (C) y quitarle `state:proposed`, con 1 s entre escrituras; los archivos van por `git push`. `init --regenerate` no llama a la forja |

- **REQ-FORGE-54:** Una operación DEBE respetar la fórmula de §7.5; la prueba con `FakeForge` la verifica con A = 0, 5 y 30.

## 8. Sesiones web de Claude Code

### 8.1 Restricciones verificadas

- Todas las operaciones de GitHub pasan por un proxy que guarda las credenciales fuera de la VM. Medido con S1 (00-marco D-41): el proxy responde 403 («Write access to this GitHub API path is not permitted through this proxy.») a toda escritura a la API de datos de git y a contents (`POST git/blobs`, `git/commits` y `git/refs`, `PATCH`/`DELETE git/refs`, `PUT contents`); admite la API REST de Issues y de pulls; y `git push` funciona contra **cualquier rama**, no solo la de la sesión, salvo para borrarla (403). El push forzado lo limita el ruleset de GitHub (`GH013`), no el proxy. Clonar y hacer fetch funcionan con normalidad.
- La API y los assets de releases solo alcanzan los repos adjuntos a la sesión. Un asset de otro repo da 403.
- GraphQL se limita a un conjunto fijo de operaciones de PR. El resto responde 403 con `This GraphQL query is not enabled for this session`, sin importar el token. Las APIs que solo existen en GraphQL (p. ej., Projects v2) no son accesibles.
- `gh` viene preinstalado. Si el usuario no configura un token, `GH_TOKEN` y `GITHUB_TOKEN` contienen el marcador `proxy-injected` y el proxy pone la credencial real. Un script que lee `GITHUB_TOKEN` directamente obtiene el marcador, no un token usable.
- `CLAUDE_CODE_REMOTE=true` indica una sesión en la nube. Clonar y crear PR exige GitHub: una sesión no puede empujar a GitLab. En las rutinas, Claude empuja a ramas con prefijo `claude/`.
- Otros requisitos: un plan con sesiones en la nube (Pro, Max, Team o Enterprise con asiento que incluya Claude Code) y la política `allow_remote_sessions` activa; una org con Zero Data Retention no puede usar sesiones en la nube. Acceso a GitHub: la Claude GitHub App instalada en la cuenta u organización de los repos privados o, como alternativa, `/web-setup` (en Team y Enterprise requiere que un Owner active Quick web setup). En Team y Enterprise, un Owner debe activar además el conector de GitHub en Admin settings > Connectors. Una allowlist de IP de la organización hace fallar las sesiones alojadas por Anthropic.

### 8.2 Consecuencias para el adaptador

- **REQ-FORGE-55:** Si el token vale `proxy-injected`, el adaptador DEBE usar el transporte `gh`, y `AUCOVE_FORGE_TRANSPORT=http` se ignora con `W_FORGE_CAPABILITY_UNKNOWN`. Si `CLAUDE_CODE_REMOTE=true` y hay un token real en `GH_TOKEN`, PUEDE usarse `http`, pero siguen las restricciones del proxy (§8.1). El transporte `gh` invoca `gh api --include --method <M> -H "Accept: …" -H "X-GitHub-Api-Version: 2026-03-10" --input - <ruta>` y lee el estado y las cabeceras de la salida de `--include`. Fuera del caso `proxy-injected`, `AUCOVE_FORGE_TRANSPORT` PUEDE forzar `http` o `gh`.
- **REQ-FORGE-56:** En una sesión web, `write_mode` DEBE ser `git` (00-marco D-41, que invirtió este requisito tras S1): `claim` crea la ref con el push con lease de REQ-FORGE-67; `save` y `update` empujan a `task/<n>` con avance rápido; `submit`, `record-assessment` y el reflejo del Issue usan la API REST de pulls e Issues, que el proxy admite. `release` no está disponible en la web (04-tareas REQ-TASK-78). `AUCOVE_FORGE_WRITE_MODE` PUEDE forzar `git` o `api` fuera de la web y en pruebas.
- **REQ-FORGE-57:** El adaptador NO DEBE descargar assets de releases. `aucove` se instala desde PyPI, que está en la allowlist Trusted.
- Flujo en la nube (00-marco D-41; 05-operaciones): la sesión trabaja en `task/<n>` o en su rama `claude/…`, vinculada a la tarea por `local_branch` (06-permisos §2.1). `save` hace el commit en local y empuja con `git push origin HEAD:refs/heads/task/<n>` sin forzar; si no es avance rápido, trae `origin/task/<n>`, fusiona y reintenta. `update` fusiona `origin/<base>` en local, resuelve allí los conflictos y empuja igual. `submit` empuja y abre el PR por REST. El proxy no permite `commit_files`, `merge_into` ni borrar ramas (§8.1).

### 8.3 Lo que verificó S1 y lo que falta

Resuelto por S1 (00-marco D-41): el proxy no permite escrituras REST a refs, blobs, commits ni contents, y sí `git push` a cualquier rama; con el ruleset `aucove-task-branches` (06-permisos REQ-PERM-52), GitHub rechaza el push forzado (`GH013`) pero no el avance rápido sobre una `task/<n>` ajena (riesgo residual, 10-seguridad §2.3). S3-bis validó la atomicidad del reclamo por push (D-42). `CT-WEB-01` (`spikes/ct_web_02.sh`), `CT-WEB-02` y `CT-WEB-03` (§10.3) se ejecutan a mano en una sesión web; `CT-PUSH-01` corre en GitHub Actions. Sobre las credenciales, el proxy sustituye la credencial real en las peticiones de las herramientas integradas y de `gh` con el marcador `proxy-injected`. No se usa la rama `claude/…` como cabeza del PR, porque rompería "una tarea, una rama, un PR" (D-12).

Falta verificar si el proxy y la Claude GitHub App aceptan el `git push` de un merge de `update` que trae de la base cambios en `.github/workflows/**`, que exige el permiso Workflows (como `CT-GIT-03`; Q-FORGE-14). La documentación se contradecía (cloud-environments: solo se empuja a la rama actual; rutinas: el push a otras ramas se comprueba y se acepta salvo que la rama esté protegida, tenga un PR abierto de otra persona o commits de otra persona) y el comportamiento puede cambiar sin aviso, así que S1 se repite antes de H4, en la última semana de H3 (23–27/11/2026), y antes de H7, la semana del 29/03 al 02/04/2027 (00-marco D-22; Q-FORGE-13). Si una repetición da otra salida, rige la regla de tres salidas de D-22 (04-tareas §11.4).

## 9. Diferencias con GitLab y Forgejo (fase 2)

Solo se define la interfaz; la implementación queda para después (00-marco §1). "Verificado" se refiere a la fuente citada en §14.

| Método o capacidad | GitHub (v0.1) | GitLab | Forgejo |
|---|---|---|---|
| `create_ref` si ya existe | 422 observado; no documentado | `POST /projects/:id/repository/branches` → 400 "Branch already exists" (según un issue de GitLab; **no documentado**) | `POST /repos/{o}/{r}/branches` → **409 documentado** ("The branch with the same name already exists"), OpenAPI de Codeberg |
| `update_ref` con CAS | `PATCH git/refs` con `force:false` | sin endpoint para mover una rama a un sha **[no verificado]** | `git/refs` solo tiene GET (verificado) |
| `commit_files` | Git Data (blobs, trees, commits y ref) | `POST /projects/:id/repository/commits` con `actions[]` y `last_commit_id` por acción (solo en update, move y delete); no se documenta qué pasa si no coincide | `POST /repos/{o}/{r}/contents` con `files[]{operation, path, content, sha}` y `branch`/`new_branch` (verificado) |
| Borrador | `draft` al crear; "listo" solo por GraphQL | atributo `draft` (verificado en la respuesta; al crear, **no verificado**) | `draft` solo lectura; las opciones de crear y editar no lo tienen, así que se haría por prefijo del título **[no verificado]** |
| Revisiones | `pulls/{n}/reviews` (APPROVE, REQUEST_CHANGES) | API de aprobaciones; "pedir cambios" **[no verificado]** | `pulls/{index}/reviews` (verificado) |
| Checks | check runs, Actions y statuses | pipelines **[no verificado]** | `commits/{ref}/status(es)` (verificado) |
| CODEOWNERS | `.github/`, raíz o `docs/`; gana la última coincidencia; obligatorio con Team o Pro, o en repo público | Premium o Ultimate; secciones con número de aprobaciones | Gitea: regex de Go en `./CODEOWNERS`, `./docs/` o `./.gitea/`; rutas y semántica en Forgejo **[no verificado]**; se exige con `block_on_official_review_requests` (campo verificado) |
| Aprobaciones obligatorias | ruleset o protección (de pago en privados) | Premium; en Free son opcionales | `required_approvals`, `approvals_whitelist_*`, `dismiss_stale_approvals` y `apply_to_admins` en cualquier instancia (campos verificados) |
| Protección de ramas | rulesets o protección clásica | existe en Free (push y merge por rol) | `branch_protections` (verificado) |
| El autor no se aprueba a sí mismo | sí | permitido por defecto; hay un ajuste para impedirlo | **[no verificado]** |
| Sesiones web de Claude Code | sí | no pueden empujar | no |

- **REQ-FORGE-58:** Un adaptador de fase 2 DEBE pasar la misma suite de contrato de §10.3. Si una fila no se puede cumplir, `capabilities()` DEBE declararlo y los métodos afectados DEBEN lanzar `ForgeUnsupported`.
- Codeberg solo encaja en investigación abierta con licencia libre. Para proyectos privados, la opción libre es un Forgejo institucional.
- Para GitLab, `create_ref` tendría que seguir la misma regla de §4.4: tras el error, confirmar con un GET si la rama existe, sin depender del código.

## 10. Adaptador falso y suite de contrato

### 10.1 `FakeForge`

`aucove/forge/fake.py`: en memoria, determinista y seguro entre hilos. Cumple `ForgeAdapter` y añade utilidades de prueba que no forman parte del Protocol:

```python
fake = FakeForge(repo=RepoRef("lab", "articulo"), viewer="ana", profile="enforced",
                 write_mode="git", graphql=True, clock=FakeClock("2026-09-29T12:00:00Z"))
base = fake.seed_commit({"roles.yaml": b"...", "tex/main.tex": b"..."})
fake.seed_branch("main", base)
bruno = fake.as_user("bruno")                    # otra vista sobre el mismo estado
fake.set_check(sha, name="build-pdf", status="completed", conclusion="success")
fake.admin_merge(pr_number=7)                    # solo en pruebas; el núcleo no fusiona
fake.faults.add(Fault(method="create_ref", nth=1, kind="timeout_after"))
fake.faults.add(Fault(method="set_assignees", kind="race",
                      race=lambda s: s.issues[12].assignees.append("bruno")))
assert fake.calls.count(kind="E") <= 5           # presupuesto de claim (§7.5)
```

- `fake.admin(token)` da el `ForgeAdmin` del falso sobre el mismo estado (§2.2): exige administración del repo para escribir y para leer la configuración, y en un repo privado del perfil `advisory` no admite rulesets. `seed_admin(...)` cambia la configuración de partida y `seed_admin_token(token, login)` asocia un token a otra cuenta (o lo invalida con `None`).
- Las lecturas de configuración del adaptador (00-marco D-65) siguen los permisos de GitHub: los ajustes de integración y los colaboradores externos, con escritura; la política de Actions, el token de los workflows, la organización y los actores de bypass, solo si la cuenta administra y el token lo hereda (`classic_pat` u `oauth`; el fine-grained de uso diario no lleva Administration). `seed_user(token_scope=...)` fija lo que mide `GET /user/repos` (§5.2) y `seed_admin(plan_visible=False)` simula un plan que el visor no ve (§3.2).
- `create_comment` acepta hasta 65,536 caracteres, como GitHub, y rechaza los cuerpos más largos; los environments y los nombres de sus secretos se siembran con `seed_environment(name, policy, rules, secrets)` y los secretos del repositorio y de la organización con `seed_secrets(scope, names)`, y en un repo privado del perfil `advisory` `put_environment` lanza `ForgeUnsupported` (00-marco D-72); `seed_run(workflow, sha, event, conclusion)` alimenta `successful_runs`; `admin_merge` registra `merged_by`, que, como en GitHub, solo devuelve `get_pull_request` (§4.2).
- **REQ-FORGE-59:** `FakeForge` DEBE reproducir estas semánticas de GitHub: `create_ref` atómico (bajo un lock) y `RefAlreadyExists` si la ref existe; `update_ref` solo con avance rápido; `PATCH` con `assignees` reemplaza el conjunto y lo descarta en silencio si el actor no tiene push; no se abre un PR sin commits de diferencia; un PR abierto por cabeza; `PATCH` de PR sin `draft`; `Closes #n` cierra el Issue solo al integrar en la rama por defecto; revisiones en orden cronológico; el autor no puede aprobar su propio PR; los tokens fine-grained no leen check runs. Y, medido en T2 real (00-marco D-68): volver a agregar una etiqueta presente registra otro `labeled` (GitHub lo hace a veces; el falso, siempre); borrar con `commit_files` una ruta ausente da 422; en un repo público, abrir un Issue no exige el permiso Issues.
- **REQ-FORGE-60:** `FakeForge` DEBE contar las peticiones por método HTTP y por tipo (L, E, C) para verificar los presupuestos de §7.5.
- **REQ-FORGE-61:** `FakeForge` DEBE admitir fallas inyectables por método y por número de llamada: `timeout_before` (sin efecto), `timeout_after` (con efecto), `http_5xx`, `http_422`, `rate_limit_primary`, `rate_limit_secondary`, `race` (muta el estado justo antes del efecto) y `latency`.
- **REQ-FORGE-62:** Con `AUCOVE_FORGE_ADAPTER=fake`, la CLI DEBE usar `FakeForge` con el estado JSON de `AUCOVE_FORGE_FAKE_STATE`, para pruebas de punta a punta. En instalaciones de usuario, la variable DEBE ignorarse salvo que exista `AUCOVE_TESTING=1`. Con `AUCOVE_FORGE_ADAPTER=fake`, el núcleo DEBE negarse a `git push` y `git fetch` contra un remoto que no sea `file://` (`E_CONFIG_INVALID`).

### 10.2 Organización de la suite

- `tests/contract/`: se parametriza con `FakeForge` y `GitHubAdapter`. Las pruebas contra la forja real llevan `@pytest.mark.live` y usan un token de §5.4. T2 real (12-mvp) usa dos repos sandbox persistentes en `proyecto-test`: `ct-public` (perfil `enforced`) y `ct-private` (perfil `advisory`), con `AUCOVE_CONTRACT_REPO` apuntando a cada uno (`proyecto-test/ct-public`, `proyecto-test/ct-private`). `CT-CAP-01` (privado) y REQ-PERM-53 usan además `ct-private-team`, repo privado de una org en GitHub Team separada (nombre provisional `proyecto-test-team`, 00-marco D-24), con solo las 3 cuentas de máquina como miembros, de noviembre de 2026 a enero de 2027. Un token fine-grained no abarca dos organizaciones, así que esa org tiene su propio juego de tokens (12-mvp §3.3). Las pruebas crean ramas `ct/<run_id>/…` y las borran al terminar. Los Issues creados se cierran como `not_planned`.
- **REQ-FORGE-63:** Cada prueba de contrato DEBE pasar igual con `FakeForge` y con `GitHubAdapter`. Una diferencia se corrige en el falso, nunca con una excepción en la prueba.
- Preparación del sandbox real (00-marco D-68): si a la rama por defecto le faltan los archivos base de las pruebas (`roles.yaml`, `tex/main.tex` y `sections/methods/methods.tex`), la suite los agrega con un commit de `lead`. Para `CT-CAP-01`, crea en `ct-public` el ruleset `ct-sandbox-base` (prohíbe borrar la rama por defecto y los pushes forzados, sin exigir PR) con el token de `lead`, que tiene Administration. `CT-GIT-03` usa la rama fija `ct-fixtures/workflow-base`, con `.github/workflows/x.yml`, creada una vez desde la web porque los tokens de la suite no tienen el permiso Workflows; sin esa rama, la prueba se omite. Las listas de GitHub (Issues, eventos y actividad de refs) y la cabeza de un PR después de mover su rama tardan unos segundos en reflejar una escritura: las pruebas `live` releen hasta 30 s antes de comparar.
- **REQ-FORGE-64:** El CI del repositorio del framework DEBE correr las pruebas `live` cada noche y antes de cada versión. Las pruebas de inyección de fallas corren solo con el falso.

### 10.3 Catálogo mínimo

| ID | Qué verifica |
|---|---|
| `CT-REF-01` | Crear una ref nueva → `Ref` con el sha pedido |
| `CT-REF-02` | Crear una ref existente → `RefAlreadyExists` con el sha existente; registrar estado y mensaje reales |
| `CT-REF-03` | 10 hilos crean commits de reclamo distintos y la misma ref → exactamente 1 éxito y 9 `RefAlreadyExists` con el sha del ganador. Como spike S3 (00-marco D-22): al menos 100 carreras válidas, con barrera y solapamiento medido; se descartan y repiten las carreras con 403, 429, 5xx o errores de red; pasa si en cada una hay exactamente un 201, las demás son 409 o 422 y la ref, leída dos veces, apunta al commit del ganador; ritmo de unas 450 escrituras por hora o menos por credencial (con `GITHUB_TOKEN`, además, menos de 1,000 peticiones por hora por repo, §7.1). En el CI nocturno, menos carreras |
| `CT-REF-04` | `update_ref` sin avance rápido → `RefConflict`; con avance rápido → éxito; `delete_ref` dos veces → `True` y luego `False` |
| `CT-REF-05` | Reservado; solo si `CT-REF-03` falla: carrera de `PATCH` con `force=false` sobre una ref de candado desde un commit conocido (modo `patch` del spike S3). No hace falta: S3 pasó (00-marco D-40) |
| `CT-PUSH-01` | Reclamo por push con lease (00-marco D-41 y D-42; 04-tareas H-TASK-6), con una sola identidad: 10 hilos crean commits de reclamo distintos y ejecutan a la vez `git push --force-with-lease=refs/heads/<ref>: origin <sha>:refs/heads/<ref>` → exactamente un push aceptado; los demás, rechazados por el cliente (`stale info`) o por el servidor (`remote rejected`), y la ref, releída, apunta al commit del ganador. Como S3-bis (`spikes/race_push.py` y `spikes/ct-push-race.yml`): al menos 100 carreras válidas en GitHub Actions con el `GITHUB_TOKEN`; se descartan y repiten las carreras con errores de red o 5xx. Además, un segundo reclamo de la rama ya creada se rechaza sin moverla. En el CI nocturno, menos carreras |
| `CT-PR-01` | Solo en `ct-public`: crear un PR con `draft=true` → `draft` es `true`; `update_pull_request` no cambia `draft`. No corre en `ct-private`: en un repo privado de una org Free no hay PR en borrador (solo Team o Enterprise Cloud) |
| `CT-PR-02` | Crear un PR sin commits → `NoCommitsBetween`; dos veces con la misma cabeza → `PullRequestExists` |
| `CT-PR-03` | Reservado, no es condición para publicar (§4.3): `mark_ready_for_review` → `draft=false` (local) o `ForgeUnsupported` (web) |
| `CT-PR-04` | El autor se aprueba a sí mismo → rechazo (422 observado) → `ForgeValidationError` |
| `CT-ISS-01` | `add_assignees` dos veces con usuarios distintos, con el cuerpo `{"assignees": [...]}` → la respuesta lista los dos en `assignees[]` (no es un candado). No se usa el campo singular `assignee`, que la versión 2026-03-10 retiró (REQ-FORGE-22) |
| `CT-ISS-02` | `list_issues` no devuelve PR; `remove_label` de una etiqueta ausente → sin error |
| `CT-ISS-03` | `create_issue` con timeout ambiguo → se recupera por el marcador `aucove:op` sin duplicar |
| `CT-GIT-01` | `commit_files` con `base_tree` conserva los demás archivos; un borrado lo quita; borrar una ruta ausente → `ForgeValidationError`; `.github/workflows/x.yml` → `ForgeValidationError` |
| `CT-GIT-02` | `merge_into` con conflicto → `MergeConflict`; sin cambios → `None` |
| `CT-GIT-03` | Token sin Workflows; la base modifica `.github/workflows/x.yml`: medir si GitHub acepta o rechaza el `git push` del merge, `commit_files` con `extra_parents`, `POST merges` y `PUT pulls/{n}/update-branch` (§5.4) |
| `CT-ACT-01` | Crear y mover una ref por REST y comprobar que `GET /activity` registra `branch_creation` y `push` con el actor autenticado |
| `CT-CAP-01` | Repo público (`ct-public`) → `enforced`; repo privado de una organización Team (`ct-private-team`, org Team de pruebas separada, 00-marco D-24) → `enforced`, registrando `plan.name` (Q-FORGE-4) y el resultado de crear el push ruleset `aucove-workflows` (06-permisos REQ-PERM-53) |
| `CT-CAP-02` | Repo privado de una organización Free → registrar las respuestas de `GET rulesets`, `GET rules/branches/{base}` y `POST rulesets`, y confirmar `advisory` (Q-FORGE-4) |
| `CT-AUTH-01` | Por cada endpoint de §4.2, registrar `X-Accepted-GitHub-Permissions` y compararlo con la tabla. En un repo público, `create_issue` no pide permiso: con un token sin permisos, GitHub abre el Issue; en uno privado, no. En un repo privado, sin permisos, el 403 llega en la primera petición del método y la cabecera trae solo su permiso: `create_empty_commit` y `ensure_labels`, el de lectura; `get_checks`, solo `actions=read` (00-marco D-68) |
| `CT-AUTH-02` | Un token sin permiso → `ForgePermissionError` con `hint`; un token inválido → `ForgeAuthError` |
| `CT-ETAG-01` | Un GET repetido → 304 sin consumir `x-ratelimit-remaining` |
| `CT-WEB-01..03` | En una sesión web (manual; spike S1, 00-marco D-22 y D-41), con `spikes/ct_web_02.sh`, en un repo público de prueba con la Claude GitHub App y dos corridas, sin ruleset en `task/*` y con `aucove-task-branches` (06-permisos REQ-PERM-52); cada paso es independiente. 01A, API REST de Issues: comentar, asignar, quitar la asignación, etiquetar y quitar la etiqueta → 2xx. 01B, API de datos de git: `POST git/blobs`, `git/trees` (si `git/blobs` pasó), `git/commits` y `git/refs`, `PATCH` y `DELETE git/refs` (sobre una rama creada por push, porque `POST git/refs` falla) y `PUT contents` → hoy, 403 del proxy. 01C, `git push`: crear `task/<n>` con `--force-with-lease=refs/heads/task/<n>:` → aceptado; un segundo reclamo de la misma rama → rechazado; avance rápido → aceptado; push a una `task/<n>` ajena existente → aceptado (riesgo residual, 10-seguridad §2.3); push forzado → aceptado sin ruleset y rechazado con `GH013` con él. 01D, abrir un PR desde `task/<n>` con `draft=false` y cerrarlo por REST → 201 y 200. 01E, borrar la rama por push y por la API → hoy, 403. Se registran la fecha, las versiones de Claude Code, Python, uv y gh, el método de conexión (GitHub App o `/web-setup`), los estados HTTP, la salida de `git push --porcelain` y los mensajes. Pasa si coincide con 00-marco D-41; una diferencia reabre la puerta web (04-tareas §11.4). Se repite la última semana de H3 (23–27/11/2026) y del 29/03 al 02/04/2027, antes de H4 y de H7. `spikes/ct_web_01.sh` queda reemplazado. 02: HTTP directo con `proxy-injected`, como prueba de regresión. 03: `markPullRequestReadyForReview`, reservado y sin ser condición para publicar |
| `CT-ADM-01` | `ForgeAdmin` con el token de administración: `repo_settings` y `update_repo_settings` con unas claves y otra vez con las mismas (idempotente); una clave fuera de `REPO_SETTINGS` → `ForgeValidationError` sin petición (REQ-FORGE-72) |
| `CT-ADM-02` | `upsert_ruleset` dos veces con el mismo nombre → un solo ruleset; `ruleset` lo devuelve y `branch_rules` de la base da sus reglas; en un repo privado de una org Free → `ForgeUnsupported` |
| `CT-ADM-03` | Con un token sin Administration: las escrituras → `ForgePermissionError` y las lecturas que pueden no estar permitidas → `None`; con un token inválido → `ForgeAuthError` |
| `CT-ADM-04` | En `ct-public`, con el token de administración (00-marco D-72): `put_environment` dos veces con la base → un environment con una sola regla de rama, la base, que `environment` devuelve también con el token de uso diario; `secret_names` del repositorio, de la organización y del environment → nombres, o `None` sin permiso, registrando `X-Accepted-GitHub-Permissions`. En `ct-private` (org Free), registrar qué responden `PUT environments/{name}` y `GET environments/{name}` |
| `CT-CFG-01` | Con el token de uso diario de §5.4 (00-marco D-65): `repo_settings` trae los ajustes de integración; `rulesets` no muestra `bypass_actors` a quien no puede editarlos; `list_labels`; `collaborator_permission` de un colaborador (escritura) y de una cuenta que no existe (`None`); `outside_collaborators`; `actions_policy` y `workflow_permissions` → `None`, y `org_settings` sin las claves de 06-permisos §4.10 |
| `CT-CFG-02` | En un repo privado de una org Free: `rulesets` → `ForgeUnsupported`. Sin escritura: `repo_settings` sin los ajustes de integración y `outside_collaborators` → `ForgePermissionError` |
| `CT-SEAL-01` | Un PR integrado en la web: `pulls_for_commit` devuelve el PR con `merged=True` y sin `merged_by`, y `get_pull_request` da su `merged_by`. Con dos pushes a la base y un run de `seal.yml` fallido entre ellos, `successful_runs('seal.yml', base, event='push')` no lista el fallido ni los runs de `pull_request_target`. Real en H3. |

## 11. Supuestos sobre otros capítulos

| Capítulo | Supuesto |
|---|---|
| 01-arquitectura | El adaptador vive en `aucove/forge/`; la dependencia HTTP es `httpx2`; `.aucove/cache/` admite `identity.json`, `capabilities.json` y `http/` (01 §7.3); en la nube, `aucove` se instala desde PyPI |
| 03-esquemas | `forge.type` (`github`) y `forge.repo` son obligatorios; no se deducen; si `forge.repo` difiere de `origin`, `E_CONFIG_REMOTE_MISMATCH`. `roles.yaml` admite `forge_id` opcional por persona |
| 04-tareas | El reclamo usa `create_ref` y trata `RefAlreadyExists` como "ya reclamada"; usa el commit de reclamo (§4.4), en la web por push con lease (REQ-FORGE-67); no abre un PR al reclamar; calcula el estado con `Issue`, `Ref`, `PullRequest` y `Review` tal como se definen en §2 |
| 05-operaciones | Traduce las excepciones de §2.1 a sus errores (§12.3); los avisos del sobre llevan un `code` con prefijo `W_`; `submit` crea el PR con `draft=false`; `save` elige `git` o `api` según `write_mode`; valida `check_path` antes de `commit_files`; construye `HumanConfirmation` |
| 06-permisos | Da el `payload` de `upsert_ruleset`; define qué pasa si `base_rules` no está aplicado; lee `roles.yaml` de la base con `get_file` o del clon |
| 07-hooks | Los hooks no llaman a la red; con la caché vacía, asumen `advisory` |
| 09-ci | Pasa `GITHUB_TOKEN` por `env` con los permisos de §5.3; corre las pruebas `live` en el repo del framework |
| 10-seguridad | Trata el riesgo residual de los tokens con Pull requests W y decide si endurece `W_AUTH_BROAD_TOKEN` |
| 11-trazabilidad | Da el mensaje de commit completo; la estrategia de fusión decide si el commit de reclamo llega a `main` |

## 12. Identificadores de este capítulo

| Identificador | Tipo | Definición |
|---|---|---|
| `W_<ÁREA>_<NOMBRE>` | formato de código de aviso | Avisos del sobre (05-operaciones) |
| `W_AUTH_BROAD_TOKEN`, `W_AUTH_WORKFLOW_SCOPE`, `W_AUTH_LOGIN_CHANGED`, `W_FORGE_CAPABILITY_UNKNOWN`, `W_FORGE_RULES_NOT_APPLIED` | código de aviso | §3.3, §5.2, §6.4, §8.2 |
| `AUCOVE_GITHUB_TOKEN` | variable de entorno | Primera fuente del token (§5.1) |
| `AUCOVE_FORGE_TRANSPORT` (`http` o `gh`), `AUCOVE_FORGE_WRITE_MODE` (`git` o `api`) | variable de entorno | Fuerzan la detección de §8 |
| `AUCOVE_FORGE_ADAPTER` (`github` o `fake`), `AUCOVE_FORGE_FAKE_STATE`, `AUCOVE_TESTING` | variable de entorno | Pruebas de punta a punta (§10.1) |
| `AUCOVE_CONTRACT_REPO` | variable de entorno | Repo sandbox de las pruebas `live` |
| `.aucove/cache/identity.json`, `.aucove/cache/capabilities.json`, `.aucove/cache/http/` | ruta `local` | §3.4, §6.2, §7.4 (árbol en 01-arquitectura §7.3) |
| `https://aucove.invalid/schemas/capabilities/v1`, `…/identity/v1` | `$id` de esquema | Cachés locales |
| `forge_id` | clave de persona en `roles.yaml` | Id durable de la cuenta (ya definida en `roles.schema.json`, 03-esquemas) |
| `ct/<run_id>/…` | prefijo de rama | Ramas de las pruebas de contrato |
| `CT-<ÁREA>-<n>` | ID de prueba | Catálogo §10.3 |
| `CT-ACT-01`, `CT-GIT-03` | ID de prueba | §10.3: actividad de refs y merge con cambios en workflows sin el permiso Workflows |
| `CT-PUSH-01` | ID de prueba | §10.3: reclamo por push con lease (00-marco D-42) |
| `CT-SEAL-01` | ID de prueba | §10.3: PR integrado en la web y runs exitosos de `seal.yml` para `ci seal` |
| `successful_runs` | método de `ForgeAdapter` | §2 y §4.2: runs exitosos de un workflow por rama y evento, más recientes primero |
| `PullRequest.merged_by` | campo de `PullRequest` | §2: login de quien integró el PR; lo usa `ci seal` |
| REQ-FORGE-69 | requisito | §2: filtros y orden de `successful_runs` |
| `live` | marcador de pytest | Pruebas contra la forja real |
| `ForgeAdapter`, `GitHubAdapter`, `FakeForge`, `HumanConfirmation` | nombre de clase Python | §2, §10 |
| `ForgeAdmin`, `GitHubAdmin`, `FakeAdmin`, `ActionsPolicy`, `WorkflowPermissions`, `RulesetInfo`, `REPO_SETTINGS`, `ORG_SETTINGS` | nombres de Python | §2.2: la interfaz de administración de `init` (00-marco D-62) |
| `EnvironmentInfo`, `SecretScope`, `environment`, `put_environment`, `secret_names`, REQ-FORGE-74, `CT-ADM-04` | nombres de Python, métodos, requisito y prueba de contrato | El environment de la firma del proyecto y los nombres de los secretos (§2, §2.2; 00-marco D-72) |
| REQ-FORGE-71, REQ-FORGE-72 | requisito | §2.2: token explícito sin disco y claves admitidas |
| `CT-ADM-01` a `CT-ADM-03` | ID de prueba | §10.3: la interfaz de administración |
| `repo_settings`, `rulesets`, `list_labels`, `collaborator_permission`, `outside_collaborators`, `actions_policy`, `workflow_permissions`, `org_settings` | métodos de `ForgeAdapter` | §2: lecturas de configuración con el token de la persona (00-marco D-65) |
| REQ-FORGE-73 | requisito | §2: las lecturas de configuración del adaptador |
| `CT-CFG-01`, `CT-CFG-02` | ID de prueba | §10.3: las lecturas de configuración |

## 13. Preguntas abiertas

- **Q-FORGE-3:** ¿`markPullRequestReadyForReview` está en el conjunto GraphQL fijo del proxy (`CT-WEB-03`)? No bloquea v0.1: el método está reservado (§4.3).
- **Q-FORGE-4:** ¿Qué responde `GET /repos/{o}/{r}/rulesets` en un repo privado de una organización Free (403, 404 o 200 vacío)? ¿Qué valores toma `plan.name`? Un 200 ya no cuenta como prueba (§3.1). Hasta confirmarlo con `CT-CAP-02`, lo no concluyente da `advisory`.
- **Q-FORGE-8:** ¿Cuentan los blobs, árboles y commits de la API Git Data como contenido para el límite de 80 por minuto? Afecta al presupuesto de `save` en modo `api`.
- **Q-FORGE-10:** Quedan sin verificar en GitLab el código al crear una rama existente, el CAS de refs y el borrador al crear; y en Forgejo, las rutas y la semántica de CODEOWNERS y el prefijo de borrador.
- **Q-FORGE-11:** Para etiquetar un PR por `issues/{n}/labels` con un token fine-grained, ¿basta Issues W o hace falta Pull requests W? Se resuelve con `CT-AUTH-01`.
- **Q-FORGE-12 (para 09-ci):** ¿Se pueden descargar artefactos de Actions desde una sesión web, dado que la redirección apunta a un dominio de almacenamiento que quizá no está en la allowlist? v0.1 no lo necesita en el adaptador. `evidence export` (v0.2) sí lo necesitará: seguirá una sola redirección sin `Authorization`, con lista de hosts permitidos y un tope de 50 MiB; traducirá el 410 a `not_found`; y en la puerta web devolverá `E_STATE_UNSUPPORTED_DOOR`.
- **Q-FORGE-13 (para 05-operaciones):** Como el comportamiento del proxy web no está documentado y puede cambiar, ¿debe `doctor`, en una sesión web, comprobar que `git push` a `task/<n>` sigue disponible (por ejemplo, empujando una `task/<n>` existente a su mismo sha) y, si recibe 403, pasar la puerta web a solo consulta con un mensaje claro? Con S1 resuelto (00-marco D-41), se decide en H3; no entra en H2.
- **Q-FORGE-14:** En la puerta web, ¿aceptan el proxy y la Claude GitHub App el `git push` de un merge de `update` que trae de la base cambios en `.github/workflows/**`, que exige el permiso Workflows? S1 no lo probó. Si se rechaza, `update` en la web devuelve `E_FORGE_PUSH_REJECTED` y se hace desde la terminal. Se prueba en la repetición de S1 del 23–27/11/2026.

## 14. Referencias

Consultadas el 2026-09-29.

- Refs de Git: https://docs.github.com/en/rest/git/refs
- Commits, árboles y blobs de Git: https://docs.github.com/en/rest/git/commits · https://docs.github.com/en/rest/git/trees · https://docs.github.com/en/rest/git/blobs
- Pull requests (sin `draft` en la actualización; `update-branch`): https://docs.github.com/en/rest/pulls/pulls
- Revisiones: https://docs.github.com/en/rest/pulls/reviews
- Mutaciones GraphQL de PR (`markPullRequestReadyForReview`): https://docs.github.com/en/graphql/reference/pulls
- Issues, asignados, etiquetas y eventos: https://docs.github.com/en/rest/issues/issues · https://docs.github.com/en/rest/issues/assignees · https://docs.github.com/en/rest/issues/labels · https://docs.github.com/en/rest/issues/events
- Palabras clave de cierre: https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue
- Contenido, comparación y estados: https://docs.github.com/en/rest/repos/contents · https://docs.github.com/en/rest/commits/commits · https://docs.github.com/en/rest/commits/statuses
- Check runs y workflow runs: https://docs.github.com/en/rest/checks/runs · https://docs.github.com/en/rest/actions/workflow-runs
- Ramas y merges: https://docs.github.com/en/rest/branches/branches
- Reglas y rulesets: https://docs.github.com/en/rest/repos/rules · https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets
- Repositorio, organización y usuario: https://docs.github.com/en/rest/repos/repos · https://docs.github.com/en/rest/orgs/orgs · https://docs.github.com/en/rest/users/users
- Permisos de tokens fine-grained: https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens
- Gestión y limitaciones de los PAT: https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens
- Formatos de token: https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/about-authentication-to-github
- Alcances OAuth y `X-OAuth-Scopes`: https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps
- Límites de la API y buenas prácticas: https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api · https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api
- Versiones de la API y cambios incompatibles de 2026-03-10: https://docs.github.com/en/rest/about-the-rest-api/api-versions · https://docs.github.com/en/rest/about-the-rest-api/breaking-changes
- GITHUB_TOKEN, `permissions`, variables: https://docs.github.com/en/actions/concepts/security/github_token · https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax · https://docs.github.com/en/actions/reference/workflows-and-actions/variables
- CLI `gh`: https://cli.github.com/manual/gh_help_environment · https://cli.github.com/manual/gh_auth_token · https://cli.github.com/manual/gh_api · https://cli.github.com/manual/gh_auth_login
- Sesiones en la nube de Claude Code (proxy de GitHub, herramientas, allowlist): https://code.claude.com/docs/en/cloud-environments · https://code.claude.com/docs/en/claude-code-on-the-web · https://code.claude.com/docs/en/routines · https://code.claude.com/docs/en/env-vars
- GitLab: https://docs.gitlab.com/api/branches/ · https://docs.gitlab.com/api/merge_requests/ · https://docs.gitlab.com/api/commits/ · https://gitlab.com/gitlab-org/gitlab/-/work_items/407378 (fuente secundaria)
- Forgejo: https://forgejo.org/docs/latest/user/api-usage/ · https://codeberg.org/swagger.v1.json (OpenAPI de la instancia Codeberg, Forgejo 16.0.0-dev) · https://docs.gitea.com/usage/code-owners
- Comportamiento observado, no documentado ("No commits between", "Reference already exists"): foros de la comunidad de GitHub; se valida con `CT-PR-02` y `CT-REF-02`.
