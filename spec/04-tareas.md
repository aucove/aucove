# 04 — Tareas: ciclo de vida, reclamo y coordinación

- **Estado:** v0.1 · **Código:** `TASK`
- **Autoridad (00-marco §6):** ciclo de vida y máquina de estados; protocolo de reclamo, liberación y vencimiento; flujo del coordinador (traslapes, cambio de dirección); actualización desde la base y conflictos.
- **No define aquí:** entradas y salidas de cada operación ni el catálogo de errores (05-operaciones), esquemas de archivos y del formulario de Issue (03-esquemas), matriz de aprobación y permisos de la forja (06-permisos), hooks (07-hooks), llamadas concretas del adaptador y detección de capacidades (08-forja), workflows (09-ci), trailers (11-trazabilidad).
- **Notación:** las llamadas REST se escriben `MÉTODO /ruta` relativas a `https://api.github.com/repos/{owner}/{repo}`. `<n>` es el número del Issue de la tarea. `base` es `project.yaml: forge.base_branch`.

## 0. Hipótesis que validan pruebas de contrato

La especificación se apoya en comportamientos de la forja que la documentación no garantiza de forma explícita. Cada uno DEBE tener una prueba de contrato contra GitHub real (catálogo en 08-forja §10.3; estrategia en 12-mvp) antes de congelar v0.1.

| ID | Hipótesis | Prueba de contrato | Si falla |
|---|---|---|---|
| H-TASK-1 | `POST /git/refs` sobre una ref que ya existe falla con 409 o 422 (el código no se usa: la documentación define 422 también como «endpoint has been spammed») y **no** modifica la ref; con N peticiones concurrentes exactamente una recibe 201. La documentación solo lista 201, 409 y 422. Se valida con el spike S3 en H0 (00-marco D-22). | `CT-REF-03`: 10 hilos crean `refs/heads/task/<n>` con 10 commits distintos. | D-10 no se sostiene; se reabre el mecanismo de reclamo. |
| H-TASK-2 | La API de actividad (`GET /activity?ref=…`) registra como `branch_creation`/`push` las refs creadas o movidas por REST, con el `actor` autenticado. | `CT-ACT-01` (08 §10.3) | El dueño se resuelve con las demás fuentes (§1.5) y la inactividad por el cambio de `head_sha` entre ejecuciones de `expire-claims` (§4.2). |
| H-TASK-3 | En una sesión web de Claude Code, el proxy permite escrituras REST de la API de Git (`/git/commits`, `/git/refs`) y de Issues/PR en el repo adjunto. | `CT-WEB-01` | La puerta web no puede reclamar por la API (§11). **Resultado (S1; 00-marco D-41):** falsa para la API de Git (403 del proxy) y verdadera para Issues y PR; la web reclama por push (H-TASK-6). |
| H-TASK-5 | En la sesión web, `PATCH /git/refs/heads/task/<n>` con `force=false` se acepta. | `CT-WEB-01` | Puerta web solo de lectura para el trabajo (§11.4). **Resultado (S1; D-41):** falsa: el proxy bloquea la API de datos de git (S1 no midió el `PATCH` aparte, porque `POST git/refs` ya falló; se mide en la repetición de S1); la web avanza la rama con `git push` (REQ-TASK-75). |
| H-TASK-6 | En la sesión web, `git push --force-with-lease=refs/heads/task/<n>: origin <claim_sha>:refs/heads/task/<n>` crea la ref solo si no existe y, con N pushes concurrentes con commits distintos, exactamente uno se acepta y la ref queda en su commit. Validada con S3-bis (00-marco D-42): 100 carreras de 10 pushes, cada una con un solo ganador. | `CT-PUSH-01` (08 §10.3) | La puerta web no puede reclamar; se reabre §11.4. |

- **REQ-TASK-1.** Ninguna implementación DEBE publicarse como v0.1 sin que `CT-REF-01` a `CT-REF-04` pasen contra GitHub (REQ-FORGE-30).
- **REQ-TASK-2.** El adaptador falso (08-forja) DEBE reproducir exactamente H-TASK-1 (fallo 422, con 409 inyectable, y ref intacta), para que las pruebas unitarias del protocolo sean válidas.

## 1. Modelo y máquina de estados

### 1.1 Instantánea de la tarea (primitivas de la forja)

El estado no se guarda (D-11): se calcula con una función pura sobre una instantánea que arma `build_task_snapshot` con el adaptador (§1.5, 08-forja §2).

| Campo | Fuente GitHub (REST) |
|---|---|
| `issue_state`, `state_reason`, `issue_author`, `labels`, `assignees` | `GET /issues/<n>` (`state_reason` ∈ `completed`, `not_planned`, `duplicate`, `reopened`, `null`) |
| `label_events` (etiqueta, acción, actor, fecha) | `GET /issues/<n>/events` (eventos `labeled`/`unlabeled`) |
| `sections`, `spec_hash` | `TaskSpec` que devuelve `parse_task_body` sobre el cuerpo del Issue (03-esquemas §8.3) |
| `issue_markers` | Comentarios del Issue con marcador (§1.4), con autor autenticado (`user.login`); `ci_verified` según §1.5 |
| `ref.head_sha` | `GET /git/ref/heads/task/<n>` (404 = no existe) |
| `ref.claim_commit_sha`, `ref.claim_commit_author` | Último commit de `GET /compare/<base>...task/<n>` cuyo asunto es el de reclamo (§2.2) |
| `ref.created_by`, `ref.last_push_at` | `GET /activity?ref=refs/heads/task/<n>` (H-TASK-2; puede faltar) |
| `pulls` (todas, cualquier estado) | `GET /pulls?head=<owner>:task/<n>&state=all`, más `GET /pulls/<pr>/reviews` y comentarios con marcador |
| `authz` (lead, deputy, dueños de sección, actores de CI) | `roles.yaml` leído de la **rama base** (D-08); actores de CI según 08-forja |

### 1.2 Estados

| Estado | Predicado de derivación (resumen de §1.5) | Transiciones permitidas | Actor y operación |
|---|---|---|---|
| `proposed` | Issue abierto con `task`, sin ref, y con `state:proposed` o sin aceptación válida. | → `open`; → `cancelled` | `lead`/`deputy`: `task accept <n>` (H) o quitar la etiqueta en la web; `task cancel <n>` (H). |
| `open` | Issue abierto, aceptado, sin ref `task/<n>`. | → `claimed`; → `cancelled` | Dueño de todas las secciones: `claim <n>`; `lead`/`deputy`: `task cancel <n>`. |
| `claimed` | Ref existe; no hay PR abierto hacia `base` (o solo uno en borrador); sin `state:expired` válida. | → `in_review`; → `open`; → `expired`; → `cancelled` | Dueño: `submit`; dueño o `lead`: `release <n>`; CI: `expire-claims`; `lead`/`deputy`: `task cancel <n>`. |
| `in_review` | Ref existe, PR abierto no borrador hacia `base`, y ninguna revisión que cuente pide cambios después de la última entrega. | → `changes_requested`; → `merged`; → `open`; → `cancelled` | Revisor que cuenta: `review request-changes <pr>` (H) o la web; humano con permiso: integración en la web; dueño o `lead`: `release <n>`; `task cancel <n>`. |
| `changes_requested` | Como `in_review`, pero la última revisión de algún revisor que cuenta es `CHANGES_REQUESTED` y es posterior a la última entrega. | → `in_review`; → `expired`; → `open`; → `cancelled` | Dueño: `submit`; CI: `expire-claims`; `release <n>`; `task cancel <n>`. |
| `expired` | Ref existe, estado base `claimed` o `changes_requested`, y `state:expired` puesta por un actor autorizado. | → `claimed`/`changes_requested` (renovación); → `open`; → `cancelled` | Dueño: `claim <n>` o `save` (quitan la etiqueta); CI: `expire-claims` si hubo actividad; dueño o `lead`: `release <n>`; `task cancel <n>`. |
| `merged` | Existe un PR de `task/<n>` del mismo repo con `merged = true`. | Terminal. | Humano con permiso, en la web (D-09). |
| `cancelled` | Issue cerrado sin PR integrado. | Terminal (reabrir en la web devuelve a `open` o `proposed`). | `lead`/`deputy`: `task cancel <n>`. |

- **REQ-TASK-3.** El núcleo DEBE calcular el estado exclusivamente con `derive_state` (§1.5); NO DEBE guardar estados en archivos, caché compartida ni etiquetas distintas de `state:proposed` y `state:expired`.
- **REQ-TASK-4.** `derive_state` DEBE ser una función pura: la misma instantánea produce siempre el mismo resultado, sin E/S ni lectura del reloj.
- **REQ-TASK-5.** Solo cuentan los PR cuyo repositorio de origen es el mismo repo; los PR desde forks se ignoran y se reportan (`fork_pr_ignored`). v0.1 supone que todas las personas tienen escritura en el repo (06-permisos).
- **REQ-TASK-80.** Desde H3, con `format: latex`, el editor del artículo cuenta como revisor en los PR que tocan el LaTeX (R2 de 06-permisos §3.2; 00-marco D-56), así que su `CHANGES_REQUESTED` también lleva la tarea a `changes_requested`. En `mode: individual` (00-marco D-57) nadie revisa: la tarea pasa de `in_review` a `merged` cuando la persona integra, sin `changes_requested`.
- **REQ-TASK-6.** La aprobación no es un estado: `in_review` expone `approved: true|false` como dato informativo; los requisitos para integrar los define 06-permisos.
- **REQ-TASK-7.** `merged` es terminal. Rehacer una tarea integrada (por ejemplo, tras una reversión) DEBE hacerse con una tarea nueva que enlace a la anterior.

### 1.3 Autoridad sobre etiquetas y marcadores

Una etiqueta o un marcador solo cuenta si su último autor está autorizado. El autor de una etiqueta es el `actor` de su último evento `labeled`; el de un marcador, el autor del comentario.

| Elemento | Cuenta si lo puso | Lo puede quitar |
|---|---|---|
| `state:proposed` (quitarla = aceptar) | cualquiera | `lead`, `deputy` |
| `state:expired` | actor de CI con marcador `expiry` verificado, `lead`, `deputy` | dueño (vía núcleo), actor de CI, `lead` |
| `flag:overlap`, `flag:direction-change` | dueño del reclamo (vía `record-assessment`), `lead`, `deputy` | `lead`, `deputy` |
| `flag:blocked` | `lead`, `deputy`, CI verificado; o el dueño con un marcador `escalation` suyo posterior al evento | los mismos |
| `ack:direction` | `lead`, `deputy` | `lead`, `deputy` |
| `ack:overlap` | `lead`, `deputy` | `lead`, `deputy` |
| `direction` | cualquiera | cualquiera |
| marcador `claim`, `submission`, `assessment` | el dueño del reclamo | — |
| marcador `release` | el dueño cuyo reclamo anula, o `lead` | — |
| marcador `expiry` | CI verificado, `lead` o `deputy` | — |
| marcador `escalation` | dueño o actor de CI | — |

- **REQ-TASK-8.** Una tarea está aceptada si y solo si el último evento `unlabeled` de `state:proposed` lo hizo `lead` o `deputy` y no hay un `labeled` posterior; si no, `derive_state` devuelve `proposed` con `missing_acceptance`.
- **REQ-TASK-9.** Un marcador de autor no autorizado DEBE ignorarse y reportarse como `forged_marker`.
- **REQ-TASK-10.** Como cualquier persona con escritura puede editar comentarios ajenos, los marcadores son evidencia a prueba de alteración visible, no a prueba de alteración (ver 10-seguridad). La identidad se toma siempre del autor del comentario, nunca del contenido.

### 1.4 Marcadores en comentarios

Formato: un comentario PUEDE contener varios marcadores; cada uno es un comentario HTML con tipo, versión y JSON en la línea siguiente.

```text
<!-- aucove:claim v1
{"task":12,"login":"ana","claim_sha":"9f2c…","base_sha":"41ab…","sections":["metodos"],"spec_hash":"sha256:c295…","claimed_at":"2026-09-29T14:02:11Z","renewed_at":null}
-->
```

El marcador `claim` lleva `spec_hash` (el de `TaskSpec` al reclamar, 03-esquemas §8.3; §1.5) y el marcador `expiry` lleva `run_id` y `head_sha` (§4.2).

- **REQ-TASK-11.** El serializador DEBE reemplazar dentro del JSON cada aparición de `--` por `\u002d\u002d` (escape JSON), de modo que el comentario nunca contenga `--`, `-->` ni `--!>`; `json.loads` lo decodifica igual. El JSON va en su propia línea y `-->` en la siguiente (00-marco §5.14).
- **REQ-TASK-12.** El prefijo `aucove:` DEBE salir del módulo único del nombre (D-03).
- **REQ-TASK-13.** El núcleo DEBE mantener **un solo comentario propio por destino y por autor**: en el Issue, el "comentario de reclamo" (marcadores `claim` y `assessment` de fase `claim`); en el PR, el "comentario de entrega" (marcadores `submission` y `assessment` de fase `submission`). Lo actualiza en su lugar (`PATCH /issues/comments/{id}`) y nunca lo duplica.

### 1.5 `derive_state`

```python
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from .forge import same_login                # 08-forja §6.3

State = Literal["proposed", "open", "claimed", "in_review",
                "changes_requested", "merged", "expired", "cancelled"]
ACTIVE = frozenset({"claimed", "in_review", "changes_requested", "expired"})
CLAIM_SUBJECT = "aucove: claim #"          # del módulo del nombre (D-03)
OWNER_PRIORITY = ("activity", "claim_commit", "marker", "assignee")
CI_LABELS = frozenset({"state:expired", "flag:blocked"})
CI_MARKER_WINDOW = timedelta(minutes=10)     # etiqueta de CI ↔ marcador `expiry` (§1.3)

@dataclass(frozen=True)
class LabelEvent: label: str; action: str; actor: str; at: datetime
@dataclass(frozen=True)
class Marker:
    kind: str; author: str; created_at: datetime; updated_at: datetime; data: dict
    ci_verified: bool = False        # lo fija build_task_snapshot (ver abajo)
@dataclass(frozen=True)
class Review: author: str; state: str; submitted_at: datetime | None
@dataclass(frozen=True)
class Pull:
    number: int; state: str; merged: bool; draft: bool; author: str
    base_ref: str; same_repo: bool; created_at: datetime
    reviews: tuple[Review, ...]      # orden cronológico (REST)
    markers: tuple[Marker, ...]
@dataclass(frozen=True)
class TaskRef:
    head_sha: str
    claim_commit_sha: str | None; claim_commit_author: str | None
    created_by: str | None; last_push_at: datetime | None   # H-TASK-2
@dataclass(frozen=True)
class Authz:
    lead: str; deputy: str | None; ci_actors: frozenset[str]
    section_owner: dict[str, str]
    def lead_like(self) -> set[str]: return {x for x in (self.lead, self.deputy) if x}
@dataclass(frozen=True)
class TaskSnapshot:
    number: int; issue_author: str; issue_state: str; state_reason: str | None
    labels: frozenset[str]; label_events: tuple[LabelEvent, ...]
    assignees: tuple[str, ...]
    sections: tuple[str, ...]; spec_hash: str       # TaskSpec actual (03-esquemas §8.3)
    issue_markers: tuple[Marker, ...]; ref: TaskRef | None
    pulls: tuple[Pull, ...]; base_branch: str; authz: Authz
@dataclass(frozen=True)
class Derived:
    state: State; owner: str | None; owner_source: str | None
    pr: int | None; approved: bool; anomalies: tuple[str, ...]
    accepted_by: str | None          # actor del último `unlabeled` válido de `state:proposed`

def _in(login: str | None, group) -> bool:
    return login is not None and any(x and same_login(login, x) for x in group)

def _last(events, label, action):
    ev = [e for e in events if e.label == label and e.action == action]
    return ev[-1] if ev else None

def accepted_by(s: TaskSnapshot) -> str | None:              # REQ-TASK-8
    ev = [e for e in s.label_events if e.label == "state:proposed"]
    if ev and ev[-1].action == "unlabeled" and _in(ev[-1].actor, s.authz.lead_like()):
        return ev[-1].actor                                    # sin `labeled` posterior
    return None

def accepted(s: TaskSnapshot) -> bool:
    return accepted_by(s) is not None

def expiry_markers(s: TaskSnapshot) -> tuple[list[Marker], list[str]]:
    ok, an = [], []                                            # §1.3, fila marcador `expiry`
    for m in s.issue_markers:
        if m.kind != "expiry":
            continue
        if (m.ci_verified and _in(m.author, s.authz.ci_actors)) or _in(m.author, s.authz.lead_like()):
            ok.append(m)
        else:
            an.append("forged_marker")
    return ok, an

def label_ok(s: TaskSnapshot, label: str, allowed: set[str]) -> bool:
    ev = _last(s.label_events, label, "labeled")
    if label not in s.labels or ev is None:
        return False
    if _in(ev.actor, allowed):
        return True
    if label in CI_LABELS and _in(ev.actor, s.authz.ci_actors):  # CI: exige marcador verificado
        return any(m.ci_verified and abs(m.updated_at - ev.at) <= CI_MARKER_WINDOW
                   for m in expiry_markers(s)[0])
    return False

def blocked_ok(s: TaskSnapshot, owner: str | None) -> bool:  # §1.3, fila `flag:blocked`
    if label_ok(s, "flag:blocked", s.authz.lead_like()):
        return True
    ev = _last(s.label_events, "flag:blocked", "labeled")
    return ("flag:blocked" in s.labels and ev is not None and _in(ev.actor, {owner})
            and any(m.kind == "escalation" and same_login(m.author, owner) and m.created_at >= ev.at
                    for m in s.issue_markers))

def _claims(s: TaskSnapshot) -> list[Marker]:
    """Marcadores `claim` no anulados cuyo `login` es el autor del comentario."""
    ll = s.authz.lead_like()
    releases = [m for m in s.issue_markers if m.kind == "release"]
    def voided(c: Marker) -> bool:
        return any(r.created_at > c.created_at
                   and (same_login(r.author, c.author) or _in(r.author, ll)) for r in releases)
    return [m for m in s.issue_markers if m.kind == "claim"
            and same_login(m.data.get("login", ""), m.author) and not voided(m)]

def resolve_owner(s: TaskSnapshot) -> tuple[str | None, str | None, list[str]]:
    live = _claims(s)
    cur = s.ref.claim_commit_sha
    valid = [m for m in live if cur is None or m.data.get("claim_sha") == cur]
    found: dict[str, str] = {}
    if s.ref.created_by: found["activity"] = s.ref.created_by
    if s.ref.claim_commit_author: found["claim_commit"] = s.ref.claim_commit_author
    if valid: found["marker"] = min(valid, key=lambda m: m.created_at).author   # el primero
    if len(s.assignees) == 1: found["assignee"] = s.assignees[0]
    if not found:
        return None, None, ["ref_without_owner"]
    source = next(k for k in OWNER_PRIORITY if k in found)
    owner = found[source]
    if any(not same_login(o, owner) for o in found.values()):
        return None, None, ["owner_sources_disagree"]         # solo el `lead` libera
    if cur is not None and any(m.data.get("claim_sha") != cur and not same_login(m.author, owner)
                               for m in live):
        return None, None, ["claim_ref_replaced"]             # la ref se rehízo sin `release`
    return owner, source, ([] if "marker" in found else ["claim_without_marker"])

def effective_sections(s: TaskSnapshot, owner: str | None) -> tuple[tuple[str, ...], list[str]]:
    """claim.sections ∩ TaskSpec.sections actuales (§3, REQ-TASK-27)."""
    cur = s.ref.claim_commit_sha if s.ref else None
    mine = [m for m in _claims(s) if _in(m.author, {owner})
            and (cur is None or m.data.get("claim_sha") == cur)]
    if not mine:
        return s.sections, []                                  # sin marcador: `claim_without_marker`
    c = min(mine, key=lambda m: m.created_at)
    declared = set(c.data.get("sections", []))
    an = [] if c.data.get("spec_hash") == s.spec_hash else ["spec_changed_after_claim"]
    return tuple(x for x in s.sections if x in declared), an

def last_submission_at(pr: Pull, owner: str | None) -> tuple[datetime, bool]:
    subs = [m for m in pr.markers if m.kind == "submission" and m.author == owner]
    if not subs:
        return pr.created_at, False
    rounds = subs[-1].data.get("rounds", [])
    t = datetime.fromisoformat(rounds[-1]["submitted_at"]) if rounds else subs[-1].created_at
    return min(t, subs[-1].updated_at), True     # nunca después de la última edición

def review_outcome(s: TaskSnapshot, pr: Pull, owner: str | None) -> tuple[str, bool, list[str]]:
    who = (s.authz.lead_like() | {s.authz.section_owner.get(x) for x in s.sections}) - {None, owner}
    latest: dict[str, Review] = {}
    for r in pr.reviews:
        if r.author in who and r.state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[r.author] = r
    t_sub, via_core = last_submission_at(pr, owner)
    an = [] if via_core else ["submitted_outside_core"]
    blocking = [r for r in latest.values()
                if r.state == "CHANGES_REQUESTED" and r.submitted_at and r.submitted_at > t_sub]
    approved = not blocking and any(r.state == "APPROVED" and a in s.authz.lead_like()
                                    for a, r in latest.items())
    return ("changes_requested" if blocking else "in_review"), approved, an

def derive_state(s: TaskSnapshot) -> Derived:
    an: list[str] = []
    ll = s.authz.lead_like()
    acc = accepted_by(s)
    an += expiry_markers(s)[1]                                    # `forged_marker`
    mine = [p for p in s.pulls if p.same_repo]
    if len(mine) != len(s.pulls): an.append("fork_pr_ignored")
    merged = [p for p in mine if p.merged]
    open_prs = [p for p in mine if p.state == "open"]

    if merged:                                                    # 1. integrada
        if s.issue_state == "open":
            an.append("reopened_after_merge" if s.state_reason == "reopened"
                      else "issue_open_after_merge")
        return Derived("merged", merged[-1].author, "pr_author", merged[-1].number, True,
                       tuple(an), acc)
    if s.issue_state == "closed":                                 # 2. cancelada
        if s.state_reason not in ("not_planned", "duplicate"): an.append("closed_without_merged_pr")
        if s.ref is not None: an.append("ref_on_closed_issue")
        if open_prs: an.append("open_pr_on_closed_issue")
        return Derived("cancelled", None, None, None, False, tuple(an), acc)
    if s.ref is None:                                             # 3. sin reclamo
        if "state:proposed" in s.labels or acc is None:
            if "state:proposed" not in s.labels: an.append("missing_acceptance")
            return Derived("proposed", None, None, None, False, tuple(an), acc)
        if s.assignees: an.append("assignee_without_ref")
        if "state:expired" in s.labels: an.append("stale_expired_label")
        if open_prs: an.append("open_pr_without_ref")
        return Derived("open", None, None, None, False, tuple(an), acc)

    if "state:proposed" in s.labels: an.append("ref_on_proposed")   # 4. con reclamo
    elif acc is None: an.append("missing_acceptance")
    owner, source, oan = resolve_owner(s); an += oan
    an += effective_sections(s, owner)[1]                         # `spec_changed_after_claim`
    if owner is not None and not (len(s.assignees) == 1 and same_login(s.assignees[0], owner)):
        an.append("assignees_mismatch")
    if "flag:blocked" in s.labels and not blocked_ok(s, owner): an.append("unauthorized_label")
    if any(p.state == "closed" and not p.merged for p in mine): an.append("pr_closed_unmerged")
    to_base = [p for p in open_prs if p.base_ref == s.base_branch]
    if len(open_prs) > 1: an.append("multiple_open_prs")
    if open_prs and not to_base: an.append("pr_wrong_base")
    pr = min(to_base, key=lambda p: p.created_at) if to_base else None

    approved = False
    if pr is None or pr.draft:
        if pr is not None: an.append("draft_pr")
        base_state = "claimed"
    else:
        base_state, approved, ran = review_outcome(s, pr, owner); an += ran
    exp_ok = label_ok(s, "state:expired", ll)                     # CI solo con marcador verificado
    if "state:expired" in s.labels and not exp_ok: an.append("unauthorized_label")
    if exp_ok and base_state in ("claimed", "changes_requested"):
        state: State = "expired"
    else:
        state = base_state
        if exp_ok: an.append("stale_expired_label")
    return Derived(state, owner, source, pr.number if pr else None, approved, tuple(an), acc)
```

Las clases de §1.5 las construye `build_task_snapshot(adapter, n, authz)` en el módulo de tareas a partir de los tipos de 08 §2; ese constructor pone `Marker.ci_verified = True` solo si `get_workflow_run(run_id)` cumple `path == '.github/workflows/expire-claims.yml'`, `head_branch == default_branch` y `event` ∈ {`schedule`, `workflow_dispatch`}; si no, el marcador se ignora con `forged_marker`.

Las secciones efectivas de una tarea reclamada son `claim.sections ∩ TaskSpec.sections` actuales (`effective_sections`): quitar secciones del Issue reduce el alcance y agregarlas no lo amplía. Si el `spec_hash` del marcador `claim` difiere del actual, `derive_state` agrega la anomalía `spec_changed_after_claim`.

El dueño sale de la primera fuente presente en el orden `activity` > `claim_commit` > `marker` > `assignee`, comparando logins con `same_login` (08 §6.3); del marcador cuenta el primero válido. Si dos fuentes presentes discrepan, `owner = None` con `owner_sources_disagree`; si un marcador `claim` no anulado de X tiene un `claim_sha` distinto del commit de reclamo actual y el dueño calculado no es X, `owner = None` con `claim_ref_replaced`. En ambos casos solo el `lead` puede `release <n>` (§1.6).

### 1.6 Estados inconsistentes y reconciliación

| Situación | Resultado de `derive_state` | Quién repara y cómo |
|---|---|---|
| La ref existe sin asignado | Estado según PR; dueño por las demás fuentes (§1.5); `assignees_mismatch` | El dueño en su próxima operación, o `expire-claims`: `set_assignees(n, [dueño])` (08 §2). |
| Hay dos asignados | Dueño según §1.5 (actividad > commit de reclamo > marcador > asignado único); `assignees_mismatch` | Igual: `set_assignees(n, [dueño])` (08 §2) reemplaza el conjunto. Los asignados nunca deciden el dueño si hay otra fuente. |
| El PR se cerró sin integrar | Ref existe → `claimed` (el PR cerrado se ignora); `pr_closed_unmerged` | Nadie: es un retiro. El dueño vuelve a entregar con `submit`, que abre un PR nuevo (§7). |
| La rama se borró | Sin ref → `open` (o `merged` si hubo integración); `assignee_without_ref` si quedó asignado | `expire-claims` vacía los asignados con `set_assignees(n, [])` (08 §2); `status` avisa al `lead`. El trabajo sigue en `refs/pull/<pr>/head` si hubo PR, o en la etiqueta de archivo si se liberó con el núcleo (§4.1). |
| Varios PR para la misma rama | Cuenta el PR abierto hacia `base` más antiguo; `multiple_open_prs` / `pr_wrong_base` | `status` avisa al dueño y al `lead`; el dueño cierra los sobrantes en la web. |
| Ref sin dueño identificable | `claimed`, `owner: null`; `ref_without_owner`, `owner_sources_disagree` o `claim_ref_replaced` | Solo el `lead` puede `release <n>`. |
| PR integrado con Issue abierto | `merged`; `issue_open_after_merge` | `expire-claims` cierra el Issue con `state_reason: completed`. Si el Issue se reabrió (`reopened_after_merge`) no se toca: se crea una tarea nueva (REQ-TASK-7). |
| `state:expired` en tarea no vencible | `stale_expired_label` | `expire-claims` o el dueño quitan la etiqueta. |

- **REQ-TASK-14.** Toda operación del núcleo sobre una tarea DEBE reportar las anomalías de `derive_state` en un aviso `W_STATE_ANOMALY` con `details = {task, anomalies: [...]}`; `status` las lista en `task_summary.anomalies`.
- **REQ-TASK-15.** Las reparaciones automáticas se limitan a las de la tabla anterior: fijar asignados al dueño, vaciar asignados sin ref, quitar `state:expired` obsoleta y cerrar el Issue tras la integración. Ninguna otra anomalía DEBE corregirse sin un humano.
- **REQ-TASK-16.** Una persona solo repara las anomalías de sus propias tareas; `expire-claims` (CI) repara las de todas.
- **REQ-TASK-17.** Las reparaciones automáticas NO DEBEN publicar comentarios.

## 2. Protocolo de reclamo

### 2.1 Decisión: el PR se abre al entregar, no al reclamar

| Criterio | PR en borrador al reclamar | PR solo al entregar (**elegida**) |
|---|---|---|
| Disponibilidad | Los PR en borrador en repos privados exigen GitHub Team o Enterprise Cloud: no existen en el perfil `advisory`. | Siempre. |
| Pasar a listo | Solo por GraphQL (`markPullRequestReadyForReview`); `PATCH /pulls/{n}` no acepta `draft`. El proxy de las sesiones web solo sirve un conjunto fijo de operaciones GraphQL. | No hace falta: el PR se crea como listo (`draft: false`). |
| Visibilidad del trabajo en curso | Diff del PR. | La ref `task/<n>` publicada por `save` y `GET /compare/<base>...task/<n>` dan la misma información. |
| Ruido | CI en cada push, notificaciones al abrir. | Nada hasta la entrega. |

- **REQ-TASK-18.** `claim` NO DEBE abrir un PR. El único PR de la tarea (D-12) lo crea `submit` con `draft: false`.
- **REQ-TASK-19.** El núcleo NO DEBE usar GraphQL.

### 2.2 Secuencia de llamadas

`claim <n>` (MCP `claim_task`), ejecutado por `me`:

1. **Lectura.** Arma la instantánea y calcula `d = derive_state(s)`. Lee `roles.yaml` de la base.
2. **Idempotencia.** Si `d.state ∈ ACTIVE` y `d.owner == me`: repara el reflejo (paso 7), quita `state:expired` si estaba (renovación: `renewed_at` en el marcador), hace el checkout local (paso 8) y devuelve éxito con `data.already_claimed = true`. Si el dueño es otro: `E_CLAIM_ALREADY_CLAIMED` (con `owner` y `claimed_at`).
3. **Precondiciones** (§3). Si `d.state != "open"`: `E_STATE_INVALID_TRANSITION`.
4. **Commit de reclamo.** `GET /git/ref/heads/<base>` → `base_sha`. Padre = `base_sha`, salvo traspaso (§4.3). `GET /git/commits/<padre>` → `tree`. `POST /git/commits` con `tree` del padre (commit vacío), `parents: [padre]` y **sin** `author` ni `committer` (la forja pone al usuario autenticado y la hora del servidor). Mensaje exacto:

   ```text
   aucove: claim #<n>

   claim-nonce <uuid4>

   Task: #<n>
   AI-Use: none
   ```
   Resultado: `claim_sha`, único para este intento.
5. **Candado atómico (D-10, H-TASK-1).** `POST /git/refs` con `{"ref": "refs/heads/task/12", "sha": claim_sha}`.
6. **Verificación posterior.** Relee el Issue. Si se cerró o volvió a `state:proposed` entre 1 y 5, compensa (§2.3) y devuelve `E_STATE_INVALID_TRANSITION` con `details.reason: "changed_during_claim"`.
7. **Reflejo (no atómico, reparable).** `set_assignees(n, [me])` (08 §2; reemplaza el conjunto; la forja descarta en silencio el cambio si no hay permiso de escritura, así que se relee); crea o actualiza el comentario de reclamo con el marcador `claim`.
8. **Local.** Con clon local: `git fetch origin task/<n>` y `git switch task/<n>`. Con árbol sucio se omite el checkout con el aviso `W_STATE_CHECKOUT_SKIPPED`.
9. **Respuesta.** `data`: `state`, `branch`, `claim_sha`, `context` (paquete de fase `claim`, §8.2) y `assessment_required: true`.

**Variante de la puerta web** (`write_mode=git`; 00-marco D-41 y D-42). El proxy de la sesión responde 403 a `POST /git/commits` y `POST /git/refs`, así que cambian dos pasos y el resto queda igual. Paso 4: el commit de reclamo se crea en el clon local, con el mismo padre, el árbol del padre y el mensaje exacto (`git commit-tree <padre>^{tree} -p <padre>`), y con autor y committer = la identidad de `GET /user` (`<login>` y `<id>+<login>@users.noreply.github.com`), para que la forja lo atribuya a la cuenta (`claim_commit_author`, §1.1); su fecha es la del reloj de la VM, no la del servidor. Paso 5: `git push --porcelain --force-with-lease=refs/heads/task/<n>: origin <claim_sha>:refs/heads/task/<n>`, que solo crea la ref si no existe (H-TASK-6). Tras un push aceptado, rechazado (`stale info` del cliente; `remote rejected` o `fetch first` del servidor) o ambiguo, el `GET` de la ref decide con las reglas de §2.3 (08-forja REQ-FORGE-67). Fuera de la web, el reclamo sigue por `POST /git/refs` (D-10).

### 2.3 Resultados, carreras y compensación

| Paso 5 devuelve | Acción | Resultado |
|---|---|---|
| 201 | `GET /git/ref/heads/task/<n>` de confirmación: si `sha == claim_sha` → ganó; si `sha` es otro → perdió pese al 201, lo que contradice H-TASK-1 y reabre el mecanismo de reclamo; si el `GET` falla por red, vale el 201. | Éxito / `E_CLAIM_ALREADY_CLAIMED` |
| 409 o 422 | `GET /git/ref/heads/task/<n>` (el código no decide, como en 08 §4.4): si `sha == claim_sha` → es un reintento propio que ganó; si `sha` es otro → perdió la carrera; si no existe → fue otra validación (422: sha o nombre inválido; 409: p. ej., repositorio vacío). | Éxito / `E_CLAIM_ALREADY_CLAIMED` / si no existe, `E_FORGE_UNEXPECTED` tras un 422 (`retryable` si fue límite) o `E_FORGE_CONFLICT` tras un 409 (`retryable: false`) |
| Tiempo agotado o 5xx | Recuperación según 08 §4.4 (los reintentos son del adaptador, 08 §7.3): el `GET` de confirmación decide. Si la ref existe con `claim_sha` → ganó; si existe con otro → perdió; si no existe → falla. | Éxito / `E_CLAIM_ALREADY_CLAIMED` / `E_NET_OFFLINE` (`retryable: true`). |

El `GET` después del 201 cuesta una lectura y detecta un hipotético doble 201: 100 carreras sin fallos (S3) solo acotan la tasa de fallo por debajo de un 3 % por carrera, con un 95 % de confianza.

- **REQ-TASK-20.** El ganador de una carrera se decide solo por el paso 5: quien ve la ref apuntando a su propio `claim_sha`. Ninguna otra lectura (asignados, comentarios) DEBE decidir quién ganó.
- **REQ-TASK-21.** El perdedor NO DEBE compensar nada: su commit de reclamo queda inalcanzable y la forja lo recoge.
- **REQ-TASK-22.** La única compensación del reclamo es la del paso 6: borrar la ref (`DELETE /git/refs/heads/task/<n>`) tras un GET que confirme que apunta a `claim_sha`; DELETE no es condicional en GitHub y la ventana se acepta. En la puerta web no hay compensación, porque borrar una rama da 403 por push y por la API (00-marco D-41): `claim` devuelve `E_STATE_INVALID_TRANSITION` con `details.reason: "changed_during_claim"` y `details.compensated: false`, y la ref queda como anomalía (`ref_on_closed_issue` o `ref_on_proposed`, §1.6) hasta que el dueño o el `lead` la liberen desde la terminal o la pestaña Code de Claude Desktop.
- **REQ-TASK-23.** Un fallo en el paso 7 NO DEBE deshacer el reclamo: la operación devuelve `ok: true` con el aviso `W_FORGE_REFLECTION_FAILED`, y la próxima operación del dueño o `expire-claims` lo repara.
- **REQ-TASK-24.** `claim` DEBE ser idempotente: repetirla el mismo dueño no crea commits ni refs nuevos.
- **REQ-TASK-25.** El commit de reclamo es vacío y lleva los trailers `Task: #<n>` y `AI-Use: none`; la trazabilidad lo excluye por tener el mismo árbol que su padre, no por su asunto (11 §2.7).

### 2.4 El skill de inicio (`empezar` / `start`)

El skill de inicio es la entrada de cada persona al flujo (00-marco §5.8 y D-70). Antes de reclamar, consulta el contexto (`get_context`) y sigue la primera fila que se cumpla:

| Situación | Qué hace |
|---|---|
| Alerta `setup_pending` (05-operaciones REQ-OPS-69) y la persona es el `lead` | Arranque guiado (abajo). |
| Alerta `setup_pending` y la persona no es el `lead` | Explica que el proyecto lo prepara el responsable y no reclama nada. |
| `my_tasks` vacío: sin tareas propias activas ni en revisión | Una presentación de cinco líneas como máximo: sus partes, las tareas abiertas en ellas y el ciclo de empezar, trabajar y entregar. Después, el reclamo de siempre. |
| Cualquier otro caso | El reclamo de siempre: `claim <n>`, o `task propose` si la tarea no existe. |

**Arranque guiado.**

1. Busca la tarea de preparación: abierta, sin reclamar y con el título que le pone `init` en el idioma del proyecto (05-operaciones §9.3). Si no existe, por ejemplo porque el repositorio ya tenía tareas, la propone con `task propose` y las partes que son suyas, y le pide al responsable que la acepte. Luego la reclama.
2. Entrevista al responsable en bloques cortos, de uno en uno, con valores por defecto, y no pregunta lo que ya está escrito:
   - el proyecto: título, objetivo, preguntas, hipótesis, alcance y métodos (`plan.md`, con la gramática de 03-esquemas §5);
   - las personas: nombre y cuenta de la forja, quién es el suplente y, con `format: latex`, quién edita el artículo (`roles.yaml`);
   - las partes: conservar, renombrar, quitar o dividir las de la plantilla, y el dueño de cada una (`roles.yaml` y las carpetas `sections/<id>/`);
   - la cuenta de IA de cada persona (`account_type`, 03-esquemas §4.4). Si alguna es de consumo y `disclosure` no es `open`, presenta las dos salidas de 10-seguridad: cuentas comerciales, o `require_commercial: false` registrado en una decisión;
   - los datos: qué son, dónde viven y si son sensibles (`data/README.md` y `data_policy` de `project.yaml`);
   - el estilo de escritura, y la fecha y la huella del acuerdo de autoría (`memory/constitution.md`; 11-trazabilidad REQ-TRACE-74);
   - de forma opcional, reglas propias para los agentes (sección 7 de `AGENTS.md`).
3. Escribe los archivos, regenera los generados con `sync-roles`, corre `validate` y le resume al responsable lo que cambió. Con el perfil `advisory`, o con la excepción de las cuentas de consumo, registra la decisión con `decision propose` (00-marco D-48 y D-63).
4. Entrega la tarea con el flujo de §7. El responsable la integra en la web; mientras no haya suplente, con el bypass de 00-marco D-17.
5. Propone las primeras tareas a partir del plan (`task propose`) y recuerda lo que sigue a mano: invitar a las personas y aceptar las tareas.

- **REQ-TASK-81.** El skill de inicio DEBE elegir su camino con la tabla de esta sección, en ese orden, y NO DEBE ejecutar operaciones solo humanas (00-marco D-09): las indica con el marcador `human:` de 01-arquitectura §10.2.
- **REQ-TASK-82.** El arranque guiado DEBE preguntar en lenguaje llano y traducir las respuestas a los archivos y claves de 03-esquemas. NO DEBE pedirle al responsable que escriba YAML ni nombres de claves.
- **REQ-TASK-83.** La presentación DEBE salir del contexto que ya leyó el skill, sin lecturas extra de la forja.

## 3. Precondiciones del reclamo, secciones ajenas y límites

| # | Precondición | Error si falla |
|---|---|---|
| P1 | `me` figura en `roles.yaml` (base) con `role: researcher` o `lead`. | `E_PERM_NOT_A_MEMBER` |
| P2 | La tarea declara al menos una parte, salvo que `me` sea `lead` o `deputy`; todas existen en `roles.yaml` (D-19). | `E_CLAIM_NO_SECTIONS` / `E_VALIDATION_UNKNOWN_SECTION` |
| P3 | `me` es dueño de **todas** las secciones declaradas. Rige también para el `lead`. | `E_PERM_SECTION_NOT_OWNED` |
| P4 | Reclamos activos de `me` (estados `claimed`, `changes_requested` y `expired`; `in_review` no cuenta) < `limits.max_active_claims`. | `E_CLAIM_LIMIT_REACHED` |
| P5 | No hay un traspaso vigente a otra persona (§4.3). | `E_CLAIM_RESERVED` |
| P6 | Si `limits.exclusive_sections: true`, ninguna otra tarea activa declara una de las mismas secciones. | `E_CLAIM_SECTION_BUSY` |

**Tarea que toca la sección de otra persona.** v0.1 no tiene permisos concedidos por tarea (reservado; Q-TASK-4). Ante P3, el error DEBE incluir `data.foreign_sections: [{section, owner}]` y `data.options`, en este orden de preferencia:

1. `split`: la parte ajena pasa a una tarea nueva (`task propose` con esas secciones y "Parte de #<n>"), y la persona o el `lead` quitan esas secciones del Issue original en la web. Reducir secciones es seguro. Agregarlas después del reclamo no amplía el alcance mientras el marcador `claim` conserve sus `sections`, porque el alcance usa `claim.sections ∩` las actuales (§1.5); pero el marcador y el cuerpo del Issue los puede editar quien tiene escritura (REQ-TASK-10), y sin marcador el alcance son las secciones actuales (`claim_without_marker`). La ampliación solo alcanza secciones del propio reclamante (P3, con `roles.yaml` de la base), queda en el historial de ediciones y pasa por la revisión del PR; fijar las secciones en un dato que el reclamante no pueda reescribir es Q-TASK-9.
2. `owner_claims`: si todas las secciones son de otra persona, la reclama ella.
3. `reassign_section`: el `lead` cambia el dueño en `roles.yaml` (PR del responsable) y la tarea se reclama después.

- **REQ-TASK-26.** El agente NO DEBE resolver P3 por su cuenta: presenta las opciones a la persona y, si ella lo pide, ejecuta `task propose` para la parte ajena.
- **REQ-TASK-27.** El alcance efectivo es (secciones declaradas efectivas ∩ secciones cuyo dueño es el reclamante, según `roles.yaml` de la base) ∪ {`memory/notes/task-<n>.md`} ∪ {archivos nuevos en `memory/decisions/`} ∪ (si el reclamante es `lead` o `deputy`: rutas de clase `lead`, sin declararlas; Q-PERM-3). Las rutas `generated` solo cambian por regeneración, que verifica el CI. Lo aplican `check_path` (06) y el alcance de CI (09).
- **REQ-TASK-28.** P4 se evalúa antes del paso 5. Una carrera del mismo usuario que lo supere en uno se tolera y se reporta como aviso `W_CLAIM_LIMIT_EXCEEDED`, sin compensar.
- **REQ-TASK-29.** Para contar P4, el núcleo DEBERÍA listar primero `GET /git/matching-refs/heads/task/` y calcular el dueño solo de esas tareas.
- **REQ-TASK-30.** Si `limits.exclusive_sections` es `false` (valor por defecto), el reclamo que comparte sección con otra tarea activa del mismo dueño procede con el aviso `W_CLAIM_SECTION_SHARED`.

## 4. Liberación, vencimiento, reasignación y cancelación

### 4.1 `release <n>` (dueño o `lead`)

1. Precondición: `d.state ∈ ACTIVE` y el actor es `d.owner` o `lead`; si no, `E_CLAIM_NOT_HELD`. Si la tarea ya está `open`, éxito sin cambios.
2. Si el actor es el dueño y hay cambios locales sin guardar en `task/<n>`: `save` primero.
3. Si hay un PR abierto: `PATCH /pulls/<pr>` con `state: closed`.
4. **Archivo del trabajo.** Si `compare(<base>...task/<n>)` contiene algún commit distinto de los de reclamo: `POST /git/refs` con `refs/tags/archive/task-<n>-<k>` apuntando a `head_sha`, con `k` = 1 + número de etiquetas `archive/task-<n>-*` (`GET /git/matching-refs/tags/archive/task-<n>-`). Si responde 422, reintenta con `k+1`.
5. `DELETE /git/refs/heads/task/<n>`.
6. `set_assignees(n, [])` (08 §2); quita `state:expired` si está.
7. Publica un comentario del actor con el marcador `release`: `{"task", "released_by", "previous_owner", "reason", "archive_ref", "handoff_to", "released_at"}`.
8. Local: si el actor es el dueño y está en `task/<n>`, `git switch <base>` y borra la rama local (el trabajo quedó en el archivo).

- **REQ-TASK-31.** `release` NO DEBE perder commits publicados: todo trabajo distinto del commit de reclamo queda en `refs/tags/archive/task-<n>-<k>` antes de borrar la ref.
- **REQ-TASK-32.** `save`, `update`, `switch` y `submit` DEBEN comprobar, antes de empujar, que `task/<n>` existe en el remoto y que su dueño es `me`. Si la ref no existe, devuelven `E_CLAIM_NOT_HELD` con `details.reason: "ref_missing"` y NO DEBEN recrearla con `git push`.

### 4.2 Inactividad y vencimiento (`expire-claims`, CI programado)

`last_activity_at` es el máximo de:

| Señal | Fuente | Confianza |
|---|---|---|
| Reclamo | fecha de autor del commit de reclamo (hora del servidor, §2.2 paso 4; en la puerta web, la del reloj de la VM, §2.2 variante web) | alta |
| Renovación | `updated_at` del comentario de reclamo cuando el dueño repite `claim <n>` | alta |
| Último push a `task/<n>` | `ref.last_push_at` (API de actividad, H-TASK-2) | alta |
| Devolución | `submitted_at` de la revisión `CHANGES_REQUESTED` vigente (solo en `changes_requested`) | alta |

Sin API de actividad, cuenta como actividad la ejecución de `expire-claims` en la que `head_sha` difiere del del último marcador `expiry`; esa ejecución guarda el `head_sha` nuevo y su `last_activity_at` en el marcador, sin menciones, para que la siguiente no vuelva a contarla.

```python
def expire_claims(tasks, now, cfg):          # cfg = project.yaml: limits.*
    for s in tasks:                           # Issues abiertos con etiqueta task
        d = derive_state(s)
        reconcile(s, d)                       # REQ-TASK-15
        if blocked_ok(s, d.owner):            # `flag:blocked` válida (§1.3): el reloj se detiene
            continue
        last = last_activity_at(s, d)
        idle_days = (now - last).total_seconds() / 86400
        if d.state in ("claimed", "changes_requested"):
            if idle_days >= cfg.claim_expiry_days:
                add_label(s, "state:expired")
                upsert_expiry_comment(s, phase="expired", mention=[d.owner, s.authz.lead])
            elif (idle_days >= cfg.claim_expiry_days - cfg.expiry_warning_days
                  and not warned_since(s, last)):
                upsert_expiry_comment(s, phase="warning", mention=[d.owner])
        elif d.state == "expired" and last > labeled_at(s, "state:expired"):
            remove_label(s, "state:expired")  # hubo actividad fuera del núcleo
```

- **REQ-TASK-33.** Solo `claimed` y `changes_requested` vencen. `in_review` NO DEBE vencer, porque la espera es del revisor.
- **REQ-TASK-34.** Una tarea con `flag:blocked` válida según §1.3 NO DEBE vencer ni recibir avisos.
- **REQ-TASK-35.** Vencer NO DEBE borrar la ref ni cambiar asignados: solo agrega `state:expired` y actualiza un único comentario con el marcador `expiry` (`{task, phase, idle_days, last_activity_at, head_sha, run_id}`, con `phase` ∈ `warning`, `expired`). El dueño renueva con `claim <n>` o `save`, que quitan la etiqueta; liberar una tarea vencida de otra persona es decisión del `lead` (capa correctiva).
- **REQ-TASK-36.** `expire-claims` DEBE ser idempotente: dos ejecuciones seguidas no cambian nada ni publican nada nuevo.
- **REQ-TASK-37.** El workflow que lo ejecuta lo define 09-ci. Los programados corren sobre la rama por defecto, se pueden retrasar con carga y en repos públicos se desactivan tras 60 días sin actividad; por eso `status` DEBE mostrar la fecha de la última ejecución de `expire-claims` si tiene más de 48 h.

### 4.3 Reasignación por el responsable (traspaso)

Reasignar a otra persona casi siempre exige cambiar al dueño de las secciones (P3). El flujo es:

1. `lead`: `release <n> --handoff <login> --reason "<texto>"`. El marcador `release` guarda `handoff_to` y `archive_ref`.
2. `lead`: si hace falta, cambia el dueño de las secciones en `roles.yaml` (PR del responsable, 06-permisos).
3. La persona indicada: `claim <n>`. Si hay traspaso vigente con `archive_ref`, el commit de reclamo usa como padre la cabeza del archivo, así que continúa el trabajo anterior; con `--fresh` parte de la base.

- **REQ-TASK-38.** Mientras el traspaso esté vigente (`limits.handoff_hold_days` desde el marcador), solo `handoff_to` o el `lead` pueden reclamar la tarea (P5).
- **REQ-TASK-39.** El `lead` NO DEBE reclamar en nombre de otra persona: el reclamo lo crea siempre quien trabajará (00-marco §4, "Reclamo").

### 4.4 Aceptación y cancelación (solo humanas, D-09)

- **REQ-TASK-40.** `task accept <n>` DEBE validar P2 sobre el Issue antes de quitar `state:proposed`. Si falla, devuelve `E_VALIDATION_UNKNOWN_SECTION` y no cambia nada. Si el Issue nunca tuvo `state:proposed`, `task accept` DEBE agregarla y quitarla para dejar la aceptación registrada.
- **REQ-TASK-41.** `task cancel <n>` DEBE, en este orden: cerrar el PR abierto si lo hay, archivar y borrar la ref como en §4.1 pasos 4–5, quitar `state:proposed` y `state:expired`, y cerrar el Issue con `state_reason: not_planned`. Sobre una tarea `cancelled` con `ref_on_closed_issue` u `open_pr_on_closed_issue`, `task cancel` DEBE completar los pasos que falten en vez de devolver `changed:false`.
- **REQ-TASK-42.** El formulario de Issue de tarea (03-esquemas) DEBE aplicar las etiquetas `task` y `state:proposed`, y NO DEBE declarar `assignees`. Las etiquetas deben existir antes (las crea `init`), porque la forja no aplica etiquetas inexistentes.

## 5. Varias tareas por persona, `switch` y retomar el trabajo

- **REQ-TASK-43.** La tarea actual es la rama `task/<n>` activa del clon; el núcleo NO DEBE guardar otro estado local para saberlo, salvo en la puerta web (§11.3). Si la rama activa no es de tarea, no hay tarea actual.
- **REQ-TASK-44.** Una persona PUEDE tener hasta `limits.max_active_claims` tareas activas, más las que estén en `in_review`.

`switch <n>` (MCP `switch_task`):

1. `n` debe estar en `ACTIVE` con dueño `me`; si no, `E_CLAIM_NOT_HELD`.
2. Si el árbol está sucio: si la rama actual es una tarea de `me`, ejecuta `save`; en cualquier otra rama, `E_STATE_DIRTY_WORKTREE` con `hint`.
3. `git fetch origin task/<n>`; `git switch task/<n>` (crea la rama local de seguimiento si falta).
4. Sincroniza: si la local está detrás, avanza rápido; si está delante, empuja; si divergen, fusiona el remoto en la local como en §6.2.
5. Devuelve el paquete de contexto de la tarea (07-hooks decide si SessionStart lo recarga).

**Retomar en otra máquina o sesión.** Lo publicado es lo único que viaja. En la otra máquina, `switch <n>` (o `claim <n>`, que es idempotente) trae la rama.

- **REQ-TASK-45.** `submit`, `switch` y `release` DEBEN dejar el trabajo en el remoto antes de terminar. El fin de sesión NO ejecuta `save`: SessionEnd solo registra pendientes (`dirty`, `unpushed`) en `.aucove/state/worktrees/<id>/pending.json` en menos de 1 s, y el siguiente SessionStart los avisa (07 §4.6). Los skills generados (01) DEBEN terminar cada bloque de edición con `save_work`; en la puerta web, antes de terminar cada respuesta que modificó archivos.
- **REQ-TASK-46.** El núcleo DEBE admitir dos clones o `git worktree` de la misma persona con tareas distintas, porque cada clon deduce su tarea de su propia rama. El núcleo no crea worktrees en v0.1.

## 6. Actualización desde la base (`update`)

### 6.1 Decisión: fusión (merge), nunca rebase

- **REQ-TASK-47.** `update` DEBE integrar la base con `git merge --no-edit origin/<base>` y NO DEBE usar rebase ni `push --force`.

Justificación, pensando en usuarios no técnicos: (1) la fusión resuelve los conflictos **una vez**; el rebase puede pedirlos commit por commit; (2) no reescribe lo que el revisor ya vio ni exige empuje forzado, que en las sesiones web puede estar bloqueado y en varias máquinas pierde trabajo; (3) conserva los trailers de cada commit (11-trazabilidad); (4) es lo mismo que hace el botón "Update branch" de GitHub (`PUT /pulls/{n}/update-branch` fusiona la base en la rama). El historial con commits de fusión no importa si el `lead` integra con *squash* (06-permisos).

### 6.2 Algoritmo y clasificación de conflictos

1. Si hay cambios sin guardar, `save`. Comprueba REQ-TASK-32.
2. `git fetch origin <base> task/<n>`; sincroniza la local con el remoto (§5, paso 4).
3. `git merge --no-edit origin/<base>`. Sin conflictos: valida (paso 5) y empuja; si el push se rechaza por cambios de la base en `.github/workflows/`, ver 08 §5.4.
4. Con conflictos: lista `git diff --name-only --diff-filter=U` y clasifica cada archivo:

| Clase del archivo (00-marco §5.2) | Resolución | Quién |
|---|---|---|
| `section` dentro del alcance (REQ-TASK-27) | `agent`: el agente resuelve en la sesión y muestra el resultado a la persona. En la práctica solo choca con otra tarea del mismo dueño ya integrada. | agente |
| `lead`, si el reclamante es `lead` o `deputy` (dentro del alcance, REQ-TASK-27) | `agent` | agente |
| `task` (`memory/notes/task-<n>.md`) | `agent` | agente |
| `append` (`memory/decisions/`), agregado en ambos lados | `auto_rename`: el núcleo renombra el archivo propio con un nombre nuevo válido (03-esquemas) | núcleo |
| Cualquier ruta fuera del alcance (`lead` si el reclamante no es `lead` ni `deputy`, `generated`, sección ajena) | `auto_base`: se toma la versión de la base y el diff propio se guarda en `.aucove/work/dropped/<id>/<YYYYMMDDTHHMMSSZ>-task-<n>.patch`; aviso `W_STATE_OUT_OF_SCOPE_DROPPED` | núcleo |

5. **Validación posterior.** Tras resolver, `validate` limitado a la tarea (esquemas, marcadores de conflicto, alcance). La compilación local es opcional (D-14).
6. Si quedan conflictos `agent`, `update` devuelve `ok: false`, `E_STATE_MERGE_CONFLICT`, `data.conflicts: [{path, class, resolution}]` y deja la fusión en curso. El agente edita y ejecuta `update --continue`, que exige que no queden marcadores `<<<<<<<`, `=======` ni `>>>>>>>`, hace el commit de fusión y empuja. `update --abort` ejecuta `git merge --abort`.

- **REQ-TASK-48.** El agente NO DEBE resolver conflictos fuera del alcance: las resoluciones `auto_*` son deterministas y las hace el núcleo.
- **REQ-TASK-49.** Si en el remoto `task/<n>` avanzó desde otra máquina, `save` DEBE fusionar el remoto en la local (mismo algoritmo) antes de empujar, nunca forzar.

### 6.3 Escalamiento al responsable

Se escala cuando (a) la persona decide que el agente no pudo resolver un conflicto `agent`, o (b) tras fusionar, la compilación o `validate` fallan por un cambio de la base en un archivo `lead` (por ejemplo, se quitó de `tex/notation.tex` una macro que la sección usa) y el arreglo no cabe en el alcance.

- **REQ-TASK-50.** `update --abort --escalate "<motivo>"` DEBE abortar la fusión, agregar `flag:blocked` a la tarea y publicar un comentario con el marcador `escalation` (`{"task", "reason", "files", "base_sha", "suspect_commits"}`) que mencione al `lead`.
- **REQ-TASK-51.** El siguiente `update` exitoso del dueño DEBE quitar `flag:blocked` si la última marca fue una escalación suya; si la puso el `lead`, la quita el `lead`.

### 6.4 Archivos compartidos que suelen chocar

Por D-19, solo las tareas reclamadas por `lead` o `deputy` editan archivos `lead`, así que los choques **textuales** entre personas solo pueden darse entre esas tareas; lo que queda son rupturas **semánticas** y choques entre tareas del mismo dueño.

| Archivo | Riesgo | Regla en v0.1 |
|---|---|---|
| `tex/main.tex`, `tex/outline/<id>.tex` | Alta de secciones nuevas y orden de sus partes | Solo en una tarea de `lead` o `deputy` y, desde H3, del editor del artículo (00-marco D-56). La operación de alta la define 02-convencion. |
| `tex/preamble.tex`, `tex/notation.tex` | Paquetes o macros que cambian bajo los pies de una sección | Solo en una tarea de `lead` o `deputy` y, desde H3, del editor del artículo; quien necesita una macro la pide con `decision propose` o una tarea para el editor. Dónde viven las macros locales de una sección lo define 02-convencion. |
| `bib/references.bib` | Claves que desaparecen al reexportar Zotero | Solo en una tarea de `lead` o `deputy` y, desde H3, del editor del artículo (02-convencion §5.5); las citas no resueltas las detecta 09-ci. |
| `memory/decisions/` | Dos archivos con el mismo nombre | `auto_rename` (§6.2). |
| Sección propia con dos tareas activas | Choque textual entre tareas propias | Aviso al reclamar (REQ-TASK-30), resolución `agent`. |
| `\label` duplicados entre secciones | Rompen la base al integrar | Prefijo por sección (02-convencion) y lint (09-ci). |

## 7. Flujo de entrega (`submit`)

Antes de llamar a `submit`, el skill `entregar`/`submit` (01-arquitectura lo genera) indica al modelo que redacte la nota de sesión (la pasa a `submit` como `note`) y, si hubo decisiones, que las registre con `decision propose`.

1. **Precondiciones.** Rama actual `task/<n>` (si no, `E_STATE_NOT_ON_TASK_BRANCH`), dueño `me`, estado `claimed`, `changes_requested` o `expired` (renueva). En `in_review` se permite: actualiza la entrega sin pedir revisión otra vez.
2. **Guardar.** `save` (commit con trailers de 11-trazabilidad y push). Comprueba REQ-TASK-32.
3. **Actualizar.** `update` (§6). Con conflictos se detiene: los workflows `pull_request` no corren si el PR tiene conflictos. Si hay un Issue `main-broken` abierto, omite este paso con el aviso `W_STATE_MAIN_BROKEN` (§10.3).
4. **Comprobaciones locales** (rápidas, reflejan las de CI): todos los archivos de `git diff --name-only origin/<base>...HEAD` dentro del alcance (`check_path`); sin marcadores de conflicto; archivos de decisión solo **nuevos** y válidos; compilación local solo con `format: latex` y si `project.yaml` la activa (D-14). Si fallan: `E_PERM_PATH_DENIED` u otro `E_VALIDATION_*`, sin crear el PR.
5. **Nota de sesión.** `submit` recibe la nota como objeto `note` (05 §9.18) y la escribe en `memory/notes/task-<n>.md`, que DEBE cumplir su esquema (03-esquemas); una ronda nueva exige un bloque nuevo. Si falta: `E_VALIDATION_NOTE_MISSING` con la plantilla en `hint`.
6. **Decisiones.** Valida los archivos nuevos de `memory/decisions/` del diff (03-esquemas); que ningún archivo existente cambió lo exige además 09-ci.
7. **PR.** Si no hay PR abierto hacia la base: `POST /pulls` con `head: "task/<n>"`, `base`, `draft: false`, título = título del Issue y cuerpo según la plantilla de PR (03-esquemas), que DEBE contener `Closes #<n>`. Un PR cerrado anterior no se reabre: el nuevo lo enlaza ("Reemplaza a #<pr>").
8. **Borrador.** Si hay un PR abierto en borrador: `E_STATE_PR_DRAFT`, `retryable: false`, `hint` "márcalo como listo en la web del PR".
9. **Registro.** Crea o actualiza el comentario de entrega con el marcador `submission`: `{"task", "rounds": [{"round", "head_sha", "submitted_at", "local_checks": {…}, "note_path"}]}`.
10. **Revisión.** `POST /pulls/<pr>/requested_reviewers`: al `lead`; si el dueño es el `lead`, al `deputy` (D-17; sin suplente, aviso de 06-permisos). En una nueva ronda, también a quienes pidieron cambios.
11. **Respuesta.** `data`: `pr`, `url`, `round`, `state: "in_review"`, `context` (paquete de fase `submission`) y `assessment_required: true`. El modelo evalúa y llama a `record-assessment` (§8.3).

- **REQ-TASK-52.** `submit` DEBE ser idempotente: si el PR ya existe y `head_sha` no cambió desde la última ronda, no crea ronda ni pide revisión otra vez.
- **REQ-TASK-53.** `Closes #<n>` solo cierra el Issue al integrar en la rama por defecto: ver 09-ci REQ-CI-1 (la base DEBE ser la rama por defecto).
- **REQ-TASK-54.** El núcleo NO DEBE integrar ni aprobar (D-09): `submit` termina en `in_review`.
- **REQ-TASK-79.** El sello de la entrega lo hace el job `submission` de `seal.yml` sobre cada cabeza del PR (09-ci §7.4; 00-marco D-45), no `submit`. En v0.1, `submit` NO DEBE pedir sellos a una TSA ni subir pruebas, y el sello local con OpenTimestamps queda reservado para v0.2. El mensaje localizado de `submit` DEBERÍA decir tres cosas: que el sello aparecerá como comentario del PR en unos minutos, que cada ronda nueva se sella otra vez y que `aucove status` guarda una copia en `.aucove/evidence/` (05-operaciones REQ-OPS-66).

## 8. El coordinador

### 8.1 Tres capas

| Capa | Qué hace | Dónde corre | ¿Bloquea? |
|---|---|---|---|
| Preventiva | Reclamo atómico, dueño único por sección, P1–P6, alcance efectivo (REQ-TASK-27). | Núcleo, al reclamar y al guardar. | Sí (son precondiciones). |
| Detectiva determinista | Hechos sin modelo en el paquete: tareas activas con secciones comunes, archivos tocados por otras ramas activas (`compare`), base adelantada, `main-broken`. En CI: alcance, compilación, esquemas, lints (09-ci). | Núcleo y CI. | Solo los checks requeridos de CI (D-13). |
| Detectiva semántica | El modelo de la sesión compara la tarea o el diff con el paquete y registra su evaluación. | Sesión de quien reclama o entrega (el núcleo nunca llama a un modelo, D-15). | No: consultiva. |
| Correctiva | Liberar, cancelar, pedir cambios, reconocer la dirección, integrar. | El `lead` (humano). | Decide el humano. |

### 8.2 Paquete de contexto

Lo devuelven `claim` y `submit` en `data.context`, y `context --task <n> --phase claim|submission` (MCP `get_context`). Su forma es `context_package` de `result.schema.json` (05 §8); el ejemplo es ilustrativo y el esquema manda.

```json
{
  "phase": "submission",
  "task": {"number": 12, "state": "in_review", "owner": "ana", "sections": ["metodos"],
           "title": {"trust": "untrusted", "source": "issue#12.title", "text": "Describir el muestreo", "truncated": false},
           "spec": {"summary": {"trust": "untrusted", "source": "issue#12.summary", "text": "…", "truncated": false},
                    "done": {"trust": "untrusted", "source": "issue#12.done", "text": "…", "truncated": false}}},
  "me": {"login": "ana", "role": "researcher", "sections": ["metodos", "datos"]},
  "plan": [{"id": "HYP-2", "status": "active",
            "text": {"trust": "rules", "source": "plan.md#HYP-2", "text": "El efecto es mayor en zonas rurales.", "truncated": false}}],
  "active_tasks": [
    {"number": 15, "state": "claimed", "owner": "bea", "sections": ["resultados"], "pr": null,
     "title": {"trust": "untrusted", "source": "issue#15.title", "text": "Tabla 2 por región", "truncated": false},
     "changed_files": ["sections/resultados/resultados.tex"]}],
  "related_sections": [
    {"id": "resultados", "owner": "bea", "path": "sections/resultados/",
     "labels": ["res:tabla-region"], "macros": ["\\tasa"],
     "excerpt": {"trust": "reviewed", "source": "sections/resultados/resultados.tex", "text": "…", "truncated": true}}],
  "recent_decisions": [
    {"file": "memory/decisions/2026-09-20-7-muestreo-estratificado.md", "date": "2026-09-20", "author": "ana", "pr": 9,
     "title": {"trust": "reviewed", "source": "memory/decisions/2026-09-20-7-muestreo-estratificado.md#title", "text": "Muestreo estratificado", "truncated": false},
     "summary": {"trust": "reviewed", "source": "memory/decisions/2026-09-20-7-muestreo-estratificado.md#summary", "text": "…", "truncated": false}}],
  "deterministic_findings": [
    {"kind": "same_file_active", "path": "sections/metodos/metodos.tex", "tasks": [12, 18]}],
  "diff_summary": {"files": 3, "additions": 120, "deletions": 8,
                   "excerpt": {"trust": "untrusted", "source": "compare:main...task/12", "text": "…", "truncated": true}}
}
```

`task.spec.summary` y `task.spec.done` salen del `TaskSpec` de la tarea (03-esquemas §8.3), con un máximo de 2000 caracteres cada uno (10-seguridad REQ-SEC-7).

- **REQ-TASK-55.** Todo texto del paquete DEBE ir como objeto `{"trust", "source", "text", "truncated"}` (10-seguridad REQ-SEC-3): texto de Issues, PR, comentarios o ramas no integradas → `untrusted`; plan y roles de la base → `rules`; secciones y decisiones integradas → `reviewed` (10 §3.2).
- **REQ-TASK-56.** El paquete NO DEBE incluir cuerpos de comentarios; de las tareas ajenas solo título, estado, dueño, secciones y archivos tocados.
- **REQ-TASK-57.** Cuando el paquete se muestre como texto (SessionStart, 07-hooks), DEBE renderizarse con los delimitadores de 10-seguridad REQ-SEC-5.
- **REQ-TASK-58.** `related_sections` = secciones de otras tareas activas y secciones con decisiones o IDs de plan en común con la tarea y, desde H3, con `format: latex`, las demás partes de su misma sección del artículo (subpartes, 00-marco D-55; `tex/outline/`); de cada una: dueño, `\label` y `\newcommand` definidos (extracción determinista por expresión regular) y un extracto de hasta 600 caracteres.
- **REQ-TASK-59.** Si la respuesta supera el tope de 05-operaciones §7.1 (8000 caracteres en `claim` y `submit`), el paquete se recorta en este orden: extractos, decisiones más allá de las 5 más recientes, tareas activas sin secciones ni IDs en común; cada texto recortado lleva `truncated: true`.

### 8.3 Evaluación en la sesión y `record-assessment`

El skill indica al modelo: (1) comparar la tarea (en `claim`) o el diff (en `submission`) con cada tarea activa, sección relacionada y decisión reciente; (2) buscar traslape de alcance, contradicciones (la misma variable o macro definida distinto) y referencias inconsistentes; (3) aplicar los criterios de dirección de §9; (4) citar siempre un objeto concreto; (5) llamar a `record-assessment <n> --phase <fase>` (MCP `record_overlap_assessment`) con:

```json
{
  "task": 12,
  "phase": "submission",
  "head_sha": "c0ffee…",
  "verdict": "possible",
  "overlaps": [
    {"target": {"task": 15}, "kind": "contradiction",
     "reason": "metodos define \\tasa por 1000 habitantes; resultados la usa por 100.",
     "evidence": [{"path": "sections/metodos/metodos.tex", "line": 42}]}],
  "direction": {"affected_ids": ["HYP-2"], "criteria": ["reformulates_claim"],
                "reason": "El párrafo nuevo afirma lo contrario de HYP-2 sin decisión que lo respalde."},
  "model": "declarado por la sesión",
  "door": "claude-code"
}
```

`verdict` ∈ `none`, `possible`, `clear`. `kind` ∈ `scope`, `contradiction`, `reference`. `target` es uno de `{"task": n}`, `{"pr": n}`, `{"decision": ruta}` o `{"section": id}`. `criteria` ∈ `reformulates_claim`, `out_of_scope_method`, `contradicts_decision`, `changes_conclusions`.

El núcleo valida, calcula el veredicto efectivo, guarda y aplica etiquetas. Resultado en el comentario (fase `submission`, en el PR):

```text
<!-- aucove:submission v1
{"task":12,"rounds":[{"round":2,"head_sha":"c0ffee…","submitted_at":"2026-10-03T09:12:40Z","local_checks":{"scope":"pass","schemas":"pass"},"note_path":"memory/notes/task-12.md"}]}
-->
<!-- aucove:assessment v1
{"task":12,"phase":"submission","head_sha":"c0ffee…","verdict":"possible","effective_verdict":"possible","overlaps":[…],"direction":{…},"assessed_by":"ana","model":"…","door":"claude-code","recorded_at":"2026-10-03T09:13:05Z"}
-->
**Entrega 2 de #12.**

**Posible traslape (consultivo).** Con #15: `\tasa` se define distinto (metodos.tex:42).
**Posible cambio de dirección (consultivo).** Afecta a `HYP-2`.
Evaluación hecha por la sesión de @ana; puede equivocarse. Decide el responsable.
```

- **REQ-TASK-60.** `record-assessment` DEBE rechazar con `E_VALIDATION_ASSESSMENT` una evaluación de fase `submission` cuyo `head_sha` no sea la cabeza actual del PR.
- **REQ-TASK-61.** Solo el dueño del reclamo PUEDE registrar la evaluación (el comentario debe ser suyo, §1.3).
- **REQ-TASK-62.** La evaluación de fase `claim` va en el comentario de reclamo del Issue; la de fase `submission`, en el comentario de entrega del PR. Una evaluación nueva reemplaza a la anterior de la misma fase; las anteriores quedan en el historial de ediciones de la forja.
- **REQ-TASK-63.** `review list` y `review show` (MCP `prepare_review`) DEBEN mostrar "sin evaluación registrada" si el PR no tiene marcador `assessment` para su `head_sha` actual. 09-ci PUEDE exponerlo como check no requerido.

### 8.4 Reglas de `flag:overlap` y "silencioso salvo traslape"

- **REQ-TASK-64.** `record-assessment` DEBE agregar `flag:overlap` al destino (el Issue en `claim`, el PR en `submission`) si y solo si `verdict ∈ {possible, clear}` y al menos una entrada de `overlaps` cita un objeto que existe: una tarea en `ACTIVE`, o `merged` en los últimos 30 días; un PR del repo; una decisión o una sección de la base. Si ninguna cita es válida, el veredicto efectivo pasa a `none` con el aviso `W_VALIDATION_UNCITED_OVERLAP`.
- **REQ-TASK-65.** El núcleo NO DEBE quitar nunca `flag:overlap` ni `flag:direction-change`: los quita un humano (`lead` o `deputy`).
- **REQ-TASK-66.** Un traslape en fase `claim` no impide el reclamo: el reclamo procede, se etiqueta el Issue y se avisa.
- **REQ-TASK-67.** Solo cuando una evaluación **agrega** una etiqueta `flag:*` que no estaba, el núcleo publica un comentario nuevo y breve que menciona al `lead` y a los dueños de las tareas citadas. En cualquier otro caso solo edita su comentario, sin menciones nuevas.

| Señal | Canal | ¿Bloquea? |
|---|---|---|
| Sin hallazgos | Solo el marcador dentro del comentario de reclamo o de entrega (que existe de todos modos); sin menciones. | No |
| Traslape, contradicción, referencia inconsistente | `flag:overlap` + detalle en el comentario propio + un aviso con menciones (REQ-TASK-67). | No |
| Cambio de dirección | `flag:direction-change` + detalle + aviso. | No, salvo la regla opcional de §9. |
| Fuera de alcance, compilación, esquemas | Checks de CI (09-ci). | Sí, si son requeridos (perfil `enforced`). |
| Base rota | Issue `main-broken` (09-ci) y primer lugar en `status`. | No por sí mismo. |
| Vencimiento | `state:expired` + comentario único `expiry`. | No |

## 9. Cambio de dirección

- **REQ-TASK-68.** El criterio operativo es la lista cerrada de `criteria` de §8.3, siempre relativa a IDs de `plan.md` de la base (00-marco §5.9): `reformulates_claim` (agrega, quita o reformula una afirmación que responde o contradice un `RQ-*` o `HYP-*`); `out_of_scope_method` (introduce datos, población o método que no cubren `IN-*`/`METH-*`, o que cubre un `OUT-*`); `contradicts_decision` (contradice una decisión integrada que cita un ID del plan); `changes_conclusions` (cambia resumen o conclusiones sobre un `RQ-*`).
- **REQ-TASK-69.** `flag:direction-change` se aplica si y solo si `direction.affected_ids` contiene al menos un ID que existe en `plan.md` de la base y no está retirado. Los IDs inexistentes se descartan con el aviso `W_VALIDATION_UNKNOWN_PLAN_IDS`; sin IDs válidos no hay etiqueta.
- **REQ-TASK-70.** `ack:direction` significa que el `lead` o el `deputy` revisaron el PR como decisión de dirección. Solo cuenta si la agregó uno de ellos después del último `labeled` de `flag:direction-change` (según los eventos del Issue).
- **REQ-TASK-71.** La regla `direction-ack` de `aucove/validate` es opcional y está **desactivada** por defecto (`coordinator.require_direction_ack: false`). Activada, falla si el PR tiene `flag:direction-change` sin un `ack:direction` válido, y es un bloqueo blando decidido de forma explícita. Lo implementa 09-ci con los eventos `labeled` y `unlabeled` del PR.
- **REQ-TASK-72.** `direction propose` abre un Issue con la etiqueta `direction` (05 §9.21) que cita los IDs afectados. Si se acepta, el `lead` crea y reclama una tarea `task/<n>` en la que edita `plan.md` y registra la decisión.

## 10. Casos de fallo

### 10.1 PR devuelto (`changes_requested`)

1. `status` y el contexto de inicio de sesión muestran la tarea en `changes_requested` y el resumen de la revisión (no confiable, con `author_role: lead`). La persona decide qué pide al agente.
2. El agente corrige dentro del alcance y ejecuta `save`; el reloj de vencimiento corre desde la revisión (§4.2).
3. `submit` abre la ronda `k+1`, vuelve a pedir revisión a quien pidió cambios y el estado pasa a `in_review`.
- **REQ-TASK-73.** Un PR devuelto NO DEBE cerrarse para empezar de nuevo: se sigue en el mismo PR (una tarea, una rama, un PR).

### 10.2 CI en rojo

El CI no es un estado: la tarea sigue en `in_review` y `status` muestra `ci: failure` con los checks que fallaron y el enlace al registro.

| Check que falla | Qué hace el agente |
|---|---|
| Alcance | Revierte los archivos fuera del alcance (`git checkout origin/<base> -- <ruta>`), `save`. |
| Compilación o lint en la sección propia | Corrige en la sesión, `save`. |
| Compilación por la base (también falla en la base) | Nada en la tarea: ver §10.3. |
| Conflicto con la base (el PR no es fusionable y el CI no corre) | `update` (§6). |

- **REQ-TASK-74.** `review show` DEBE mostrar `behind_by` (con `compare`) y recomendar `update` antes de integrar si el PR está detrás de la base, porque dos PR en verde por separado pueden romperla juntos.

### 10.3 La base se rompe tras integrar dos PR

1. El workflow de la base (09-ci) falla y abre o actualiza un único Issue `main-broken` con los PR integrados desde el último verde.
2. `status` lo muestra primero a todos. `submit` no fusiona la base mientras siga abierto (§7, paso 3) y avisa con `W_STATE_MAIN_BROKEN`. `update` avisa y fusiona solo si la persona lo confirma.
3. El `lead` elige, siempre en una tarea `task/<n>`: **revertir** (reversión: 09 §7.3; la aprobación sigue 06-permisos) o **arreglar hacia adelante** (una tarea nueva que reclama el dueño de la sección afectada, o una tarea del `lead` si el arreglo está en archivos `lead`).
4. Mientras `main-broken` siga abierto, el `lead` NO DEBERÍA integrar otros PR que no sean el arreglo; `review list` lo avisa.
5. Cuando la base vuelve a verde, 09-ci cierra el Issue. La tarea revertida sigue `merged`; rehacerla es una tarea nueva (REQ-TASK-7). Si la reversión cambia la dirección, el `lead` DEBERÍA registrar una decisión.

## 11. Puerta web (sesiones de Claude Code en la nube)

### 11.1 Hechos verificados

- **Verificado con S1** (00-marco D-41): el proxy responde 403 a toda escritura a la API de datos de git y a contents (`POST git/blobs`, `git/commits` y `git/refs`, `PATCH`/`DELETE git/refs`, `PUT contents`); la API REST de Issues y la de pulls funcionan; `git push` funciona a cualquier rama, incluida una `task/<n>` ajena, y respeta el ruleset `aucove-task-branches` (un push forzado da `GH013`); borrar una rama da 403 por push y por la API. Clonar y traer funcionan.
- El proxy solo sirve un conjunto fijo de operaciones GraphQL de PR; el resto recibe 403 y la respuesta sugiere la alternativa REST (`gh api repos/{owner}/{repo}/...`).
- Las peticiones a la API de GitHub solo llegan a repos adjuntos a la sesión.
- Al iniciar una sesión se puede elegir la rama de partida ("start Claude from a feature branch"), y lo probable es que la sesión trabaje en una rama nueva `claude/...` creada desde la elegida. Como `git push` alcanza cualquier rama (S1), la tarea no depende de la rama de la sesión: `claim` cambia el clon a `task/<n>` (§2.2, paso 8) o, con el árbol sucio, vincula la rama de la sesión con `local_branch` (§11.3).
- La documentación se contradecía: la de rutinas dice que el push a otra rama se comprueba y se rechaza si está protegida, si otra persona tiene un PR abierto desde ella o si tiene commits de otra persona, y la de cloud-environments, que solo se puede empujar a la rama actual. S1 midió que el proxy acepta el push a cualquier rama, también a una `task/<n>` con commits de otra persona, y que el push forzado lo limita el ruleset `aucove-task-branches` de GitHub, no el proxy (08-forja §8.3).
- En una sesión con un solo repo se cargan los hooks de `.claude/settings.json` y los skills de `.claude/skills/`. PyPI está en la lista Trusted, así que `aucove` se instala desde PyPI (01-arquitectura, 07-hooks).

Conclusión (00-marco D-41): la sesión web no puede escribir por la API de Git, pero sí empujar a `task/<n>`; reclama, guarda y entrega, y no puede liberar (REQ-TASK-78).

### 11.2 Reclamo y entrega en la web

- **REQ-TASK-75.** En la puerta web (`write_mode=git`; 08-forja REQ-FORGE-56; 00-marco D-41), `claim` DEBE crear el commit de reclamo en local y la ref con el push con lease de §2.2 (variante web; H-TASK-6); `save` y `update` DEBEN empujar a `task/<n>` con avance rápido (§11.3); `submit` y `record-assessment` DEBEN usar la API REST de pulls e Issues, igual que el reflejo del Issue (asignados, etiquetas y marcadores). Ninguna operación DEBE usar en la web la API de datos de git (`git/blobs`, `git/trees`, `git/commits`, `git/refs`) ni `contents`, que el proxy bloquea.
- **REQ-TASK-76.** En la puerta web, el skill NO DEBE usar el botón "Create PR" de la sesión: el PR lo crea `submit` desde `task/<n>`. Un PR creado desde una rama `claude/...` no es de tarea y `derive_state` lo ignora.
- **REQ-TASK-77.** La puerta web trabaja con **una sesión por tarea**: `switch` devuelve `E_STATE_UNSUPPORTED_DOOR` con `hint` "abre una sesión nueva desde la rama task/<n>".
- **REQ-TASK-78.** En la puerta web, `release` NO está disponible en v0.1: DEBE devolver `E_STATE_UNSUPPORTED_DOOR` con un `hint` que remita a la terminal o a la pestaña Code de Claude Desktop, porque borrar una rama da 403 por push y por la API (00-marco D-41). `claim`, `save`, `update`, `submit` y `record-assessment` sí están disponibles (REQ-TASK-75). Los reclamos abandonados en la web los vence `expire-claims` (§4.2). Pedir la liberación con un marcador en el Issue que ejecute un workflow queda reservado para v0.2, porque añade un workflow con escritura disparado por comentarios.

### 11.3 Empujar el trabajo

En la web, `write_mode=git` (08 §8.2; 00-marco D-41): `save` y `update` empujan a `task/<n>` con `git push origin HEAD:refs/heads/task/<n>`, sin `--force`; si se rechaza por no ser avance rápido, traen `origin/task/<n>`, fusionan y reintentan. La tarea local es la rama actual `task/<n>` o, si la sesión sigue en su rama `claude/...`, la de `.aucove/cache/tasks/<n>.json: local_branch` (06 §2.1).

### 11.4 Resultado de S1 y repeticiones

S1 dio la salida (ii) ampliada de la regla de tres salidas (00-marco D-22 y D-41): la escritura por la API de Git está bloqueada, pero `git push` alcanza cualquier rama y la API de Issues y PR funciona. Por eso la puerta web reclama por push con lease (atómico según S3-bis, D-42), guarda y actualiza con `git push` y entrega y registra la evaluación por REST; solo `release` remite a la terminal o a la pestaña Code de Claude Desktop (REQ-TASK-78). No hace falta relajar el invariante «el PR sale de `task/<n>`». Como el comportamiento del proxy no está documentado y puede cambiar sin aviso, S1 se repite antes de H4, en la última semana de H3 (23–27/11/2026), y antes de H7, la semana del 29/03 al 02/04/2027 (00-marco D-22), con `CT-WEB-01` (`spikes/ct_web_02.sh`) y `CT-PUSH-01` (08-forja §10.3). Si una repetición da otra salida, rige la regla de D-22: con (i), la web puede volver a escribir por la API; con (iii), queda de solo consulta (`status`, `context`, `review list`/`show`; 12-mvp) y el trabajo se hace en la terminal o en la pestaña Code de Claude Desktop.

## 12. Supuestos sobre otros capítulos

| Capítulo | Supuesto |
|---|---|
| 01-arquitectura | Genera los skills `empezar`/`start` y `entregar`/`submit` con las instrucciones de §2.4, §7 y §8.3 desde una sola fuente; cada bloque de edición termina con `save_work` (REQ-TASK-45). Define cómo se instala `aucove` desde PyPI en la sesión web. |
| 02-convencion | Define la clase de cada ruta que usa §6.2; el prefijo de `\label` por sección; dónde viven las macros locales de una sección; la operación de alta de sección. |
| 03-esquemas | El formulario de tarea tiene el campo "Partes" (`id: sections`; ids de `roles.yaml`), aplica `task` y `state:proposed` y no declara `assignees`. `parse_task_body` devuelve `TaskSpec` con `spec_hash` (§8.3). La propuesta de dirección es un Issue con la etiqueta `direction` que crea `direction propose` (05 §9.21); no hay formulario `direction.yml` en v0.1. Existen el esquema de la nota de sesión, las reglas de nombre de decisiones y la plantilla de PR con `Closes #<n>`. `project.yaml` admite las claves `limits.*` y `coordinator.*` de §13. |
| 05-operaciones | Cataloga todos los errores y avisos que usa este capítulo, que no propone `E_*` propios (§13), y los parámetros `--handoff`, `--reason`, `--fresh`, `--continue`, `--abort`, `--escalate`, `--phase`, `--task` y `note`. Define `context_package` en `result.schema.json` y fija el tope por respuesta que usa REQ-TASK-59. |
| 06-permisos | Todas las personas tienen escritura en el repo (necesaria para crear refs, asignar y etiquetar). `check_path` aplica REQ-TASK-27. Las ramas `task/*` no exigen revisión, pero el ruleset `aucove-task-branches` prohíbe el push forzado. Recomienda integrar con *squash* y activar el borrado automático de ramas integradas. Define la aprobación de los PR del responsable y de reversión. |
| 07-hooks | SessionStart muestra el paquete con los delimitadores de REQ-TASK-57 y avisa los pendientes que registró SessionEnd (07 §4.6). SessionEnd no ejecuta `save`: solo registra `dirty` y `unpushed` en `.aucove/state/worktrees/<id>/pending.json` (REQ-TASK-45). Se recarga el contexto tras `switch`. |
| 08-forja | Define los tipos de §2 con los que `build_task_snapshot` arma la instantánea de §1.1 (incluida la API de actividad si existe), `get_workflow_run`, `set_assignees` y `same_login` (§6.3); expone los actores de CI (p. ej. `github-actions[bot]`), detecta la sesión en la nube y en ella usa `write_mode=git`: reclamo por push con lease y escritura con `git push` (§8.2; 00-marco D-41). Su adaptador falso cumple REQ-TASK-2. |
| 09-ci | Workflow programado `.github/workflows/expire-claims.yml` que ejecuta `expire-claims` (con `issues: write`); el alcance aplica REQ-TASK-27; no exime commits por su asunto; ignora ramas `claude/*`; workflow de la base con el Issue `main-broken`; REQ-CI-1 (la base es la rama por defecto); regla opcional `direction-ack` de `aucove/validate`; `seal.yml` sella cada cabeza de PR (job `submission`) y cada push a la base (job `integration`), con checks informativos (D-45). |
| 10-seguridad | Trata los marcadores como evidencia alterable por quien tiene escritura (REQ-TASK-10). Define los niveles `rules`, `reviewed` y `untrusted` (§3.2) y el marcado del paquete de contexto (REQ-SEC-3, REQ-SEC-5). |
| 11-trazabilidad | Formato de los trailers `Task` y `AI-Use`; la trazabilidad excluye el commit de reclamo por tener el mismo árbol que su padre (11 §2.7). |
| 12-mvp | Incluye las pruebas de contrato de §0 (con `CT-PUSH-01`) en la estrategia de pruebas, las repeticiones de S1 y la degradación de la puerta web si una repetición da otra salida (§11.4). |

## 13. Identificadores de este capítulo

| Identificador | Tipo | Definición |
|---|---|---|
| `limits.max_active_claims` | clave de `project.yaml` (entero, por defecto 3) | Máximo de reclamos activos por persona (P4). |
| `limits.claim_expiry_days` | clave de `project.yaml` (entero, por defecto 14) | Días de inactividad para vencer (§4.2). |
| `limits.expiry_warning_days` | clave de `project.yaml` (entero, por defecto 3) | Días de aviso antes del vencimiento. |
| `limits.exclusive_sections` | clave de `project.yaml` (booleano, por defecto `false`) | Prohíbe dos tareas activas sobre una misma sección (P6). |
| `limits.handoff_hold_days` | clave de `project.yaml` (entero, por defecto 7) | Vigencia de un traspaso (§4.3). |
| `coordinator.require_direction_ack` | clave de `project.yaml` (booleano, por defecto `false`) | Activa la regla `direction-ack`. |
| `direction-ack` | regla de `aucove/validate` | Regla opcional de §9. |
| `ack:overlap` | etiqueta de la forja | El `lead` o el `deputy` reconocieron el traslape señalado (§1.3). |
| `aucove:claim`, `aucove:release`, `aucove:submission`, `aucove:assessment`, `aucove:expiry`, `aucove:escalation` (`v1`) | marcadores de comentario | Registros estructurados de §1.4. |
| `aucove: claim #<n>` | asunto de commit | Commit vacío de reclamo (§2.2). |
| `refs/tags/archive/task-<n>-<k>` | ref | Archivo del trabajo liberado o cancelado. |
| `claim`, `submission` | valores de `phase` | Fase de la evaluación. |
| `none`, `possible`, `clear` | valores de `verdict` | Veredicto de traslape. |
| `scope`, `contradiction`, `reference` | valores de `kind` | Tipo de traslape. |
| `reformulates_claim`, `out_of_scope_method`, `contradicts_decision`, `changes_conclusions` | valores de `criteria` | Criterios de cambio de dirección. |
| `agent`, `auto_rename`, `auto_base` | valores de `resolution` | Resolución de conflictos (§6.2). |
| `E_CLAIM_ALREADY_CLAIMED`, `E_CLAIM_LIMIT_REACHED`, `E_CLAIM_NO_SECTIONS`, `E_CLAIM_SECTION_BUSY`, `E_CLAIM_RESERVED`, `E_CLAIM_NOT_HELD`, `E_STATE_INVALID_TRANSITION`, `E_STATE_DIRTY_WORKTREE`, `E_STATE_NOT_ON_TASK_BRANCH`, `E_STATE_MERGE_CONFLICT`, `E_STATE_UNSUPPORTED_DOOR`, `E_STATE_PR_DRAFT`, `E_VALIDATION_UNKNOWN_SECTION`, `E_VALIDATION_NOTE_MISSING`, `E_VALIDATION_ASSESSMENT`, `E_FORGE_UNEXPECTED`, `E_FORGE_CONFLICT`, `E_PERM_NOT_A_MEMBER`, `E_PERM_SECTION_NOT_OWNED`, `E_PERM_PATH_DENIED`, `E_NET_OFFLINE` | códigos de error canónicos (05-operaciones §12.1) | Los que usa este capítulo; no propone `E_*` propios. |
| `fork_pr_ignored`, `missing_acceptance`, `assignee_without_ref`, `stale_expired_label`, `open_pr_without_ref`, `ref_on_proposed`, `ref_without_owner`, `owner_sources_disagree`, `claim_without_marker`, `claim_ref_replaced`, `assignees_mismatch`, `pr_closed_unmerged`, `multiple_open_prs`, `pr_wrong_base`, `draft_pr`, `submitted_outside_core`, `unauthorized_label`, `issue_open_after_merge`, `reopened_after_merge`, `closed_without_merged_pr`, `ref_on_closed_issue`, `open_pr_on_closed_issue`, `forged_marker`, `spec_changed_after_claim` | códigos de anomalía (valores de `details.anomalies` de `W_STATE_ANOMALY`) | Salidas de `derive_state` (§1.5, §1.6). |
| `W_STATE_ANOMALY`, `W_FORGE_REFLECTION_FAILED`, `W_STATE_CHECKOUT_SKIPPED`, `W_CLAIM_LIMIT_EXCEEDED`, `W_CLAIM_SECTION_SHARED`, `W_STATE_OUT_OF_SCOPE_DROPPED`, `W_VALIDATION_UNCITED_OVERLAP`, `W_VALIDATION_UNKNOWN_PLAN_IDS`, `W_STATE_MAIN_BROKEN` | códigos de aviso (05-operaciones §12.2) | Avisos de las operaciones de este capítulo. |

## 14. Preguntas abiertas

- **Q-TASK-3.** ¿Se permite la aceptación automática de tareas propuestas por el dueño de todas sus secciones? Reduciría la fricción del piloto, pero choca con D-09.
- **Q-TASK-4.** ¿Hacen falta permisos concedidos por tarea (una sección ajena con permiso del dueño y doble aprobación) en una fase posterior, o basta con dividir?
- **Q-TASK-5.** ¿Qué valores por defecto de `claim_expiry_days` y `max_active_claims` sirven a un piloto de 4 a 6 semanas? ¿Hace falta un valor por persona en `roles.yaml`?
- **Q-TASK-6.** ¿Hace falta una operación para editar las secciones de un Issue (hoy se hace en la web) y una operación `revert` solo humana?
- **Q-TASK-7.** ¿Editar un comentario con menciones nuevas notifica en GitHub? REQ-TASK-67 evita depender de eso publicando un comentario nuevo, pero conviene verificarlo para reducir ruido.
- **Q-TASK-9.** El alcance efectivo usa `claim.sections ∩` las secciones actuales (§1.5), pero el marcador `claim` y el cuerpo del Issue los puede editar quien tiene escritura (REQ-TASK-10), y sin marcador el alcance son las secciones actuales. ¿Se fijan las secciones en un dato que el reclamante no pueda reescribir (el commit de reclamo o la aceptación del `lead`) y se trata `claim_without_marker` como «sin secciones»? No bloquea v0.1: la ampliación solo alcanza secciones del propio reclamante, queda en el historial y pasa por la revisión del PR.

## 15. Referencias

Consultadas el 2026-09-29.

- GitHub REST, referencias Git (crear, obtener, actualizar con `force`, borrar; códigos 409/422): https://docs.github.com/en/rest/git/refs
- GitHub REST, commits Git (autor por defecto = usuario autenticado con la hora actual): https://docs.github.com/en/rest/git/commits
- GitHub REST, pull requests (`PATCH` sin `draft`; `head` como `owner:rama`; `update-branch` fusiona la base; PR en borrador en repos privados solo con Team o Enterprise Cloud): https://docs.github.com/en/rest/pulls/pulls
- GitHub GraphQL, `markPullRequestReadyForReview` y `convertPullRequestToDraft`; enumeración `PullRequestReviewState` (`APPROVED`, `CHANGES_REQUESTED`, `COMMENTED`, `DISMISSED`, `PENDING`): https://docs.github.com/en/graphql/reference/pulls
- GitHub REST, revisiones (lista en orden cronológico) y solicitudes de revisión: https://docs.github.com/en/rest/pulls/reviews · https://docs.github.com/en/rest/pulls/review-requests
- GitHub REST, Issues (`state_reason`; `assignees` reemplaza el conjunto y se descarta sin permiso de escritura): https://docs.github.com/en/rest/issues/issues
- GitHub REST, asignados ("Users already assigned to an issue are not replaced", hasta 10): https://docs.github.com/en/rest/issues/assignees
- GitHub REST, actividad del repositorio (`activity_type`, `actor`, `timestamp`): https://docs.github.com/en/rest/repos/repos#list-repository-activities
- GitHub REST, comparar commits (`status`, `ahead_by`, hasta 300 archivos): https://docs.github.com/en/rest/commits/commits#compare-two-commits
- GitHub REST, fusionar una rama (201/204/409): https://docs.github.com/en/rest/branches/branches#merge-a-branch
- Palabras de cierre, solo al integrar en la rama por defecto: https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue
- Eventos de Actions (`schedule` en la rama por defecto, mínimo 5 min, retrasos, desactivación a los 60 días en repos públicos; `pull_request` no corre con conflictos): https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- Formularios de Issue (las `labels` deben existir): https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-issue-forms
- Borrar y restaurar ramas de un PR: https://docs.github.com/en/enterprise-cloud@latest/repositories/configuring-branches-and-merges-in-your-repository/managing-branches-in-your-repository/deleting-and-restoring-branches-in-a-pull-request
- Comentarios editables por quien tiene escritura: https://docs.github.com/en/communities/moderating-comments-and-conversations/managing-disruptive-comments
- Claude Code, entornos en la nube (proxy de GitHub: según la documentación, push solo a la rama de trabajo, que S1 desmintió (00-marco D-41); GraphQL restringido, alcance por repo; qué se hereda del repo; PyPI en Trusted): https://code.claude.com/docs/en/cloud-environments
- Claude Code en la nube (limitaciones, teleport): https://code.claude.com/docs/en/claude-code-on-the-web
- Inicio rápido web (elegir rama de partida; crear PR normal o en borrador): https://code.claude.com/docs/en/web-quickstart
- Rutinas (ramas `claude/`; comprobaciones al empujar a otra rama): https://code.claude.com/docs/en/routines

**No verificado (tratado como supuesto):** H-TASK-2 (H-TASK-1 la verificó S3, D-40; H-TASK-3 y H-TASK-5 las resolvió S1, D-41; H-TASK-6, S3-bis, D-42); que el actor de los workflows con `GITHUB_TOKEN` sea `github-actions[bot]`; que borrar por API la rama de un PR abierto cierre el PR (la documentación solo habla de la interfaz); el comportamiento de las notificaciones al editar comentarios (Q-TASK-7).
