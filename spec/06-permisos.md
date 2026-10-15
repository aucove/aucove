# 06 — Permisos

- **Estado:** v0.1
- **Código:** `PERM`
- **Autoridad (00-marco §6):** modelo de autorización; matriz de aprobación por perfil; protecciones que aplica `init`; operaciones solo humanas; matriz de capacidades por puerta.
- **Normativo previo:** `00-marco.md`. Los supuestos sobre capítulos que se escriben en paralelo están en §10.

---

## 1. Principios

1. **Una sola política, leída de la base** (D-08). Quién puede hacer qué sale de `roles.yaml` y `project.yaml` de la rama base. `CODEOWNERS`, las reglas locales y la configuración de la forja se derivan de ahí.
2. **Los permisos son de la persona, no del agente.** El agente que trabaja en la sesión de una persona tiene exactamente las rutas de esa persona. Lo que separa al humano de su agente son las operaciones solo humanas (§6), no las rutas.
3. **Capas** (D-13). Los controles locales guían y bloquean ante cualquier fallo. En `enforced`, lo que decide es la forja (ruleset + `CODEOWNERS`) con los checks requeridos de CI.
4. **La forja integra; el núcleo no** (D-09). Aprobar e integrar se hace en la web de la forja.

- **REQ-PERM-1.** Toda decisión de autorización DEBE calcularse con `roles.yaml` y `project.yaml` leídos de la punta de `origin/<forge.base_branch>`, nunca del árbol de trabajo ni de la cabeza de un PR.
- **REQ-PERM-2.** Un cambio en `roles.yaml` NO DEBE otorgar ni quitar permisos hasta que se integre en la base.
- **REQ-PERM-3.** Si no hay política disponible (nunca se hizo `fetch` y no hay caché), toda escritura DEBE denegarse con `E_CONFIG_POLICY_UNAVAILABLE`.
- **REQ-PERM-4.** Si `roles.yaml` o `project.yaml` de la punta de la base no valida, hooks, núcleo y CI DEBEN usar la versión válida más reciente de ese archivo en la historia de primer padre de la base (máximo 100 commits) y emitir `W_CONFIG_POLICY_FALLBACK` con el sha usado; sin versión válida, `E_CONFIG_POLICY_UNAVAILABLE` y se deniega toda escritura.

## 2. Modelo de autorización

### 2.1 Fuentes

| Dato | Fuente local (hooks, MCP, CLI) | Fuente en CI |
|---|---|---|
| Personas, roles, suplente, secciones | `git show origin/<base>:roles.yaml` tras `git fetch`, con caché por sha de la base (07-hooks) | `roles.yaml` en la punta de la base (`pull_request.base.ref`) |
| Rama base | `project.yaml: forge.base_branch` (de la base) | ídem |
| Identidad (`login`) | Adaptador de forja autenticado, con caché en `.aucove/` (08-forja) | `pull_request.user.login` |
| Tarea `<n>` | Rama actual `task/<n>`; en la nube (`CLAUDE_CODE_REMOTE=true`), la `<n>` cuyo `.aucove/cache/tasks/<n>.json` tenga `local_branch` igual a la rama actual; si no, ninguna. | Rama cabeza del PR `task/<n>` |
| Reclamante, estado y aceptación | 04-tareas (`derive_state`, `accepted_by`); localmente, `.aucove/cache/tasks/<n>.json` | 04-tareas, consultado por API |
| Decisiones existentes | `git ls-tree origin/<base> memory/decisions/` | ídem |

- **REQ-PERM-5.** El `login` DEBE venir de la sesión autenticada con la forja. NO DEBE derivarse de `git config user.email` ni de variables de entorno editables, salvo del evento de CI.
- **REQ-PERM-6.** Un `login` que no figura en `people` de `roles.yaml` NO DEBE tener permiso de escritura en ninguna ruta a través del framework.

Forma de `roles.yaml` que este capítulo supone (la autoridad es 03-esquemas; ver §10):

```yaml
schema: https://aucove.invalid/schemas/roles/v1
deputy: bruno            # opcional; un login de people con role researcher
people:
  - login: ana
    role: lead
  - login: bruno
    role: researcher
  - login: carla
    role: researcher
sections:
  - id: introduction
    owner: ana
  - id: methods
    owner: carla
  - id: results
    owner: bruno
```

Predicados derivados: `lead` (el único `login` con `role: lead`), `deputy` (valor de la clave o ninguno), `editor` (valor de la clave `editor` o ninguno; si es ninguno, el editor del artículo es el `lead`, 00-marco D-56), `approvers = {lead, deputy}` y `owner(S)` (dueño de la sección `S`).

- **REQ-PERM-7.** `deputy`, si existe, DEBE ser un `login` de `people` con `role: researcher` y distinto del `lead`. `aucove validate` DEBE fallar en caso contrario.

### 2.2 Rol × clase de ruta

Las clases son las de 00-marco §5.2. C = crear, M = modificar, B = borrar. "En tarea" quiere decir en una tarea reclamada por esa persona y en estado `claimed`, `in_review`, `changes_requested` o `expired`. La lectura no está restringida.

| Clase | `lead` | `deputy` | `researcher` |
|---|---|---|---|
| `lead` (incluye toda ruta no listada) | C/M/B en tarea propia, sin declarar | C/M/B en tarea propia, sin declarar, en nombre del `lead` | no |
| `editor` (solo `format: latex`; desde H3, 00-marco D-56) | C/M/B en tarea propia, sin declarar, si es el editor o si `roles.yaml` no declara editor | C/M/B en tarea propia, sin declarar, si es el editor o, sin editor declarado, en nombre del `lead` | C/M/B en tarea propia, sin declarar, solo si es el editor |
| `section` propia | C/M/B si la tarea la declara (D-19) | ídem | ídem |
| `section` ajena | no (reservado, Q-TASK-4) | no (reservado, Q-TASK-4) | no (reservado, Q-TASK-4) |
| `append` (`memory/decisions/*.md`) | C, y M/B mientras el archivo no exista en la base | ídem | ídem |
| `task` (`memory/notes/task-<n>.md`) | solo la nota de su tarea `<n>` | ídem | ídem |
| `generated` | en modo `commit` y `ci`, solo si el contenido es la regeneración; en `session`, nunca | ídem | no |
| `local` | nadie (la escribe el núcleo) | nadie | nadie |

**Rutas de control.** Un subconjunto de `lead` cambia los controles o las instrucciones del propio agente: `roles.yaml`, `project.yaml`, `AGENTS.md`, `CLAUDE.md`, `.mcp.json`, `opencode.json`, `.claude/**`, `.github/workflows/**`, `.devcontainer/**`, `.codex/**`, `.gemini/**`, `.opencode/**` y `.agents/**`.

- **REQ-PERM-8.** En modo `session`, una escritura permitida sobre una ruta de control DEBE devolver `confirm: true`, y la puerta DEBE pedir confirmación humana en vez de aprobarla sola. Las rutas de control se reconocen sin distinguir mayúsculas, como R2 y R3 (02 REQ-CONV-9), porque eso solo añade confirmaciones.

### 2.3 Rol × operación

Actores y marca H según 00-marco §5.7. Aquí se añaden las condiciones de permiso. El resto de las precondiciones está en 05-operaciones.

| Operación | `lead` | `deputy` | `researcher` | Condición de permiso |
|---|---|---|---|---|
| `doctor`, `status`, `context`, `check-path`, `validate`, `metrics`, `evidence verify` | sí | sí | sí | solo lectura (`evidence verify --upgrade` escribe solo en la carpeta que verifica) |
| `init` (H) | sí | no | no | `permissions.admin == true` en el repo; si no, `E_AUTH_INSUFFICIENT_SCOPE` |
| `sync-roles` | sí | sí (propuesto, §9) | no | `lead`/`deputy`; solo regenera archivos del árbol de trabajo; no escribe en la forja (REQ-PERM-30) |
| `task propose`, `decision propose`, `direction propose` | sí | sí | sí | persona en `people`; los archivos nuevos pasan por `check_path` |
| `task accept`, `task cancel` (H) | sí | sí | no | — |
| `claim` | sí | sí | sí | tarea `open`; `lead`/`deputy` pueden reclamar una tarea sin partes |
| `release` | sí (cualquier reclamo) | solo el suyo | solo el suyo | — |
| `save`, `update`, `switch`, `submit`, `record-assessment` | reclamante | reclamante | reclamante | `save` y `submit` aplican `check_path` a todo el cambio (REQ-PERM-12) |
| `expire-claims` | sí | sí | no | CI, `lead` o `deputy`; no es H; `E_PERM_ROLE_REQUIRED` en otro caso |
| `review list`, `review show` | sí | sí | dueños de partes tocadas | solo lectura |
| `review approve` (H) | sí | sí | no | no ser autor del PR (`E_PERM_SELF_REVIEW`) |
| `review request-changes` (H) | sí | sí | dueño de una parte tocada | no ser autor del PR |

- **REQ-PERM-9.** Toda operación de la tabla DEBE devolver `E_PERM_ROLE_REQUIRED` cuando la persona no cumple la columna de su rol.
- **REQ-PERM-58.** Desde H3, con `format: latex`, el editor del artículo (00-marco D-56) PUEDE ejecutar `review approve` y `review request-changes` sobre los PR que tocan el LaTeX, cualquiera que sea su rol, siempre que no sea el autor. Su aprobación cumple R2 (§3.2) y no reemplaza la de `lead` o `deputy` (R1), salvo que también lo sea.

### 2.4 `check_path(login, ruta, op, tarea)`

Es una función sin red ni efectos. Sus entradas son una instantánea de la política (§2.1) y de la tarea; en los modos `session` y `commit`, la instantánea incluye los metadatos del árbol que piden 02 REQ-CONV-9 (grafía real) y REQ-CONV-10 (enlaces). La exponen `aucove check-path` y la herramienta MCP `check_path`. Los argumentos de CLI y MCP los fija 05-operaciones.

- **REQ-PERM-10.** `check_path` NO DEBE hacer E/S de red. DEBE usar solo la instantánea de la política y de la tarea que recibe.
- **REQ-PERM-11.** Ante cualquier excepción interna, `check_path` DEBE devolver una denegación con un código del área `INTERNAL` (catálogo en 05-operaciones), nunca una autorización.

**Modos.** `session`: cada escritura de la puerta durante la sesión (hooks, MCP y CLI). `commit`: el cambio completo que `save` y `submit` van a confirmar. `ci`: los archivos del PR en la regla `scope` de 09-ci (§2.5).

**Normalización y clasificación.** `check_path` no tiene reglas de rutas propias. Lleva la entrada a su forma canónica con `canonical()` (02 §3.1; la grafía la corrige 02 REQ-CONV-9) y la clasifica con `classify(rel, declared, symlinked)` (02 §3.5), ambas del módulo `aucove.domain.paths` (00-marco D-32 (d)). `declared` son las secciones de `roles.yaml` de la base. En `session` y `commit`, un prefijo existente que sea enlace simbólico o unión cuenta como enlace (REQ-CONV-10), y la sonda de enlaces de la instantánea es obligatoria: si falta, `check_path` deniega con un código del área `INTERNAL` (REQ-PERM-11) en lugar de suponer que no hay enlaces. En `commit` y `ci` las rutas salen de git, así que `canonical` recibe `native=False` (02 §3.1). En CI el modo de cada archivo sale de `git -C <tree> ls-tree -r -z <head_sha>`, no de la API, y llega como `file_mode`.

Un renombre es `delete(old)` + `create(new)`: se evalúan las dos llamadas y basta una denegación para rechazarlo.

**Orden de evaluación** (gana la primera regla que aplica):

1. Fuera de la raíz, ruta inválida (02 REQ-CONV-7, incluidos los alias de Windows) o con un segmento `.git` en cualquier posición, o un alias suyo (02 §3.3 R0) → `E_PERM_OUTSIDE_REPO`.
2. Enlace simbólico o `file_mode == "120000"` → `E_PERM_SYMLINK`.
3. `login` fuera de `people` → `E_PERM_NOT_A_MEMBER`.
4. `local` → `E_PERM_LOCAL_PATH`.
5. `generated`: `create`/`modify` permitido solo si `mode` ∈ {`commit`, `ci`}, el actor está en `approvers` y `generated_matches` es verdadero; si no (o si es `delete`), `E_PERM_GENERATED_PATH`.
6. Sin tarea → `E_STATE_NOT_ON_TASK_BRANCH`.
7. Reclamante distinto → `E_CLAIM_NOT_HELD`.
8. Estado fuera de `WRITABLE` → `E_STATE_INVALID_TRANSITION` (`details.state`).
9. `task`: en modo `session`, `memory/notes/**` → `E_PERM_OUTSIDE_TASK` (la nota la escribe `submit`, DC-09); en los modos `commit` y `ci`, la nota propia admite `create` y `modify`, y `delete` u otra nota → `E_PERM_OUTSIDE_TASK`.
10. `append`: `create` si la ruta no está en la base; `modify`/`delete` solo si tampoco está en la base; si no, `E_PERM_APPEND_ONLY`.
11. `section` no declarada por la tarea → `E_PERM_OUTSIDE_TASK`.
12. `section` de otro dueño → `E_PERM_SECTION_NOT_OWNED` (`details.owner`).
12b. Desde H3 (02 §3.3 R6a): `editor` sin ser el editor del artículo (o, si `roles.yaml` no lo declara, el `lead` o el `deputy`) → `E_PERM_EDITOR_ONLY_PATH` (`details.editor`). Antes de H3, `classify` devuelve `lead` para esas rutas y, como el editor por defecto es el `lead`, el resultado coincide.
13. `lead` sin ser `lead` ni `deputy` → `E_PERM_LEAD_ONLY_PATH`.
14. `lead` de `lead` o `deputy` → permitido, con `confirm = (mode == "session" and ruta de control)` (§2.2).

```python
from collections.abc import Callable
from dataclasses import dataclass
from aucove.domain.paths import canonical, classify, is_git_path, InvalidPath, OutsideRepo  # 02 §3.1, §3.5; módulo: 00-marco D-32 (d)

@dataclass(frozen=True)
class Task:   # 04-tareas; estado de 00-marco §5.5
    number: int; claimant: str | None; state: str
    sections: frozenset[str]            # claim.sections ∩ TaskSpec.sections actuales (04 §1.5)

@dataclass(frozen=True)
class Policy:  # instantánea de la base (sections: id -> owner) y del árbol donde se evalúa
    people: frozenset[str]; lead: str; deputy: str | None
    sections: dict[str, str]; base_decisions: frozenset[str]
    root: str; ci_fs: bool
    link_probe: Callable[[str], bool] | None = None   # 02 REQ-CONV-10; obligatoria en session y commit (D-32 d)
    editor: str | None = None                         # 00-marco D-56; None = el editor es el lead (desde H3)
    mode: str = "group"                               # 00-marco D-57; "individual" quita las aprobaciones (§3.6)
    @property
    def approvers(self) -> frozenset[str]: return frozenset(x for x in (self.lead, self.deputy) if x)

@dataclass(frozen=True)
class Decision:
    allow: bool; code: str | None; path: str | None; path_class: str | None = None
    section: str | None = None; owner: str | None = None; confirm: bool = False
    requires_approval_of: frozenset[str] = frozenset()   # reservado; siempre vacío en v0.1
    warning: str | None = None

CONTROL_FILES = {"roles.yaml", "project.yaml", "AGENTS.md", "CLAUDE.md", ".mcp.json", "opencode.json"}
CONTROL_DIRS = (".claude/", ".github/workflows/", ".devcontainer/", ".codex/", ".gemini/", ".opencode/", ".agents/")
WRITABLE = {"claimed", "in_review", "changes_requested", "expired"}

def check_path(login: str, raw_path: str, op: str, task: Task | None, *, pol: Policy,
               mode: str, file_mode: str | None = None, generated_matches: bool = False) -> Decision:
    # op: "create" | "modify" | "delete"; mode: "session" | "commit" | "ci"
    # file_mode: modo git ("100644", "120000"...); en CI sale de git ls-tree, no de la API
    try:
        rel = canonical(raw_path, pol.root, pol.ci_fs, native=(mode == "session"))   # 02 §3.1; grafía: 02 REQ-CONV-9
    except (InvalidPath, OutsideRepo):
        return Decision(False, "E_PERM_OUTSIDE_REPO", raw_path)
    if is_git_path(rel):                                       # '.git' en cualquier segmento y sus alias (02 §3.3 R0)
        return Decision(False, "E_PERM_OUTSIDE_REPO", rel)
    if file_mode == "120000" or (mode != "ci" and pol.link_probe(rel)):   # 02 REQ-CONV-10; sin sonda, TypeError → INTERNAL (REQ-PERM-11)
        return Decision(False, "E_PERM_SYMLINK", rel, "lead")
    info = classify(rel, frozenset(pol.sections), symlinked=False)             # 02 §3.5
    cls, sec = info.cls, info.section
    deny = lambda code, **kw: Decision(False, code, rel, cls, sec, **kw)
    if login not in pol.people:
        return deny("E_PERM_NOT_A_MEMBER")
    staff = login in pol.approvers
    if cls == "local":
        return deny("E_PERM_LOCAL_PATH")
    if cls == "generated":
        ok = op in ("create", "modify") and mode in ("commit", "ci") and staff and generated_matches
        return Decision(True, None, rel, cls) if ok else deny("E_PERM_GENERATED_PATH")
    if task is None:
        return deny("E_STATE_NOT_ON_TASK_BRANCH")
    if task.claimant != login:
        return deny("E_CLAIM_NOT_HELD")
    if task.state not in WRITABLE:
        return deny("E_STATE_INVALID_TRANSITION")             # el llamador pone task.state en details.state
    if cls == "task":
        own = info.task == task.number and op in ("create", "modify")
        return Decision(True, None, rel, cls) if own else deny("E_PERM_OUTSIDE_TASK")
    if cls == "append":                                        # create; modify/delete solo si no está en la base
        return deny("E_PERM_APPEND_ONLY") if rel in pol.base_decisions else Decision(True, None, rel, cls)
    if cls == "section":
        owner = pol.sections[sec]
        if sec not in task.sections:
            return deny("E_PERM_OUTSIDE_TASK", owner=owner)
        if owner != login:
            return deny("E_PERM_SECTION_NOT_OWNED", owner=owner)
        return Decision(True, None, rel, cls, sec, owner)
    if cls == "editor":                                        # desde H3 (02 §3.3 R6a; 00-marco D-56)
        editors = frozenset({pol.editor}) if pol.editor else pol.approvers
        if login not in editors:
            return deny("E_PERM_EDITOR_ONLY_PATH", owner=pol.editor or pol.lead)
        return Decision(True, None, rel, cls)
    if not staff:                                              # cls == "lead"
        return deny("E_PERM_LEAD_ONLY_PATH")
    low = rel.lower()                                          # sin distinguir mayúsculas (REQ-PERM-8)
    control = low in {x.lower() for x in CONTROL_FILES} or low.startswith(tuple(x.lower() for x in CONTROL_DIRS))
    return Decision(True, None, rel, cls, confirm=(mode == "session" and control))
```

**Tabla de casos** (política del ejemplo de §2.1). Tareas: #12 la reclamó `carla` y declara solo `methods`. #15 la reclamó `bruno` y declara `results` y `methods`. #18 la reclamó `ana` y declara `introduction`. #22 la reclamó `carla` y declara `results`. Todas están en estado `claimed`. `op` es `create` en las rutas marcadas "(nuevo)" y `modify` en las demás.

| # | login | Ruta (entrada) | Tarea | Modo | Resultado |
|---|---|---|---|---|---|
| 1 | carla | `sections/methods/main.tex` | 12 | session | permite |
| 2 | carla | `sections/results/fig1.pdf` | 12 | session | `E_PERM_OUTSIDE_TASK` |
| 3 | bruno | `sections/methods/main.tex` | 15 | session | `E_PERM_SECTION_NOT_OWNED` |
| 4 | carla | `sections/results/a.tex` | 22 | session | `E_PERM_SECTION_NOT_OWNED` |
| 5 | bruno | `sections/methods/main.tex` | 12 | session | `E_CLAIM_NOT_HELD` |
| 6 | carla | `sections/methods/main.tex` | — (en `main`) | session | `E_STATE_NOT_ON_TASK_BRANCH` |
| 7 | carla | `tex\sections\methods\..\..\..\plan.md` | 12 | session | se normaliza a `plan.md`: `E_PERM_LEAD_ONLY_PATH` |
| 8 | ana | `plan.md` | 18 | session | permite |
| 9 | ana | `roles.yaml` | 18 | session | permite con `confirm: true` (la puerta pregunta) |
| 9b | ana | `.claude/settings.json` | 18 | session | `check_path` permite con `confirm`, pero en Claude Code lo deniega L1 (07 §5) |
| 10 | bruno | `tex/notation.tex` | 15 | session | permite (suplente; clase `editor` sin editor declarado, así que la cambian el responsable y el suplente) |
| 11 | carla | `roles.yaml` | 12 | ci | `E_PERM_LEAD_ONLY_PATH` |
| 12 | carla | `memory/decisions/2026-09-29-prior.md` (nuevo) | 12 | session | permite |
| 13 | carla | `memory/decisions/2026-09-01-scope.md` (existe en la base) | 12 | ci | `E_PERM_APPEND_ONLY` |
| 14 | carla | `memory/notes/task-15.md` | 12 | session | `E_PERM_OUTSIDE_TASK` |
| 15 | ana | `.github/CODEOWNERS` | 18 | session | `E_PERM_GENERATED_PATH` |
| 16 | ana | `.github/CODEOWNERS` | 18 | ci | permite si `generated_matches` (el contenido lo verifica el check de sincronía) |
| 16b | ana | `.github/CODEOWNERS` regenerado | 18 | commit | permite |
| 17 | cualquiera | `.claude/settings.local.json` o `.aucove/cache/identity.json` | — | session | `E_PERM_LOCAL_PATH` |
| 18 | ana | `sections/discussion/draft.tex` (sección aún no declarada) | 18 | session | permite como clase `lead` |
| 19 | dave (no está en `people`) | cualquier ruta | — | session | `E_PERM_NOT_A_MEMBER` |
| 20 | carla | `.git/hooks/pre-commit` | 12 | session | `E_PERM_OUTSIDE_REPO` |
| 21 | carla | `sections/methods/.git/config` (nuevo) | 12 | session | `E_PERM_OUTSIDE_REPO` (R0: `.git` en cualquier segmento) |
| 22 | ana | `.git./hooks/pre-commit`, `GIT~1/hooks/pre-rebase` o `.git::$INDEX_ALLOCATION/hooks/pre-push` (nuevo) | 18 | session | `E_PERM_OUTSIDE_REPO` (alias de Windows, 02 REQ-CONV-7) |
| 23 | ana | `.Aucove/cache/identity.json` | 18 | session | `E_PERM_LOCAL_PATH` (R1 sin distinguir mayúsculas) |
| 24 | carla | `sections/methods/aux.tex` (nuevo) | 12 | ci | `E_PERM_OUTSIDE_REPO` (nombre que Windows no puede extraer) |
| 25 | carla | `sections\methods\x.tex` (nuevo; en git, un archivo de la raíz con ese nombre) | 12 | ci | `E_PERM_OUTSIDE_REPO` (`\` en una ruta de git, 02 §3.1) |

Esta prueba recorre los vectores de 02-convencion a través de `check_path`, para que la clasificación no se desvíe de la convención:

```python
@pytest.mark.parametrize("vec", CONV_VECTORS, ids=lambda v: v.raw)   # 02 §3.6; declared = {"methods"}
def test_check_path_follows_convention_vectors(vec, tree):
    # tree: árbol temporal con la raíz del vector sustituida; los vectores (Windows)/(Linux) se omiten en otra plataforma
    pol = tree.policy(people={"ana", "carla"}, lead="ana", deputy=None, sections={"methods": "carla"})
    task = Task(12, "carla", "claimed", frozenset({"methods"}))
    d = check_path("carla", vec.raw, "modify", task, pol=pol, mode="session")
    if vec.canonical is None:                                          # OutsideRepo
        assert (d.allow, d.code) == (False, "E_PERM_OUTSIDE_REPO")
    else:
        assert (d.path, d.path_class) == (vec.canonical, vec.cls)
```

- **REQ-PERM-54.** La suite de pruebas DEBE recorrer con `check_path` todos los vectores de 02 §3.6 y todos los casos de la tabla de este apartado.

### 2.5 Dónde se aplica

| Punto | Modo | Qué evalúa | Efecto de una denegación | Autoridad |
|---|---|---|---|---|
| Reglas `deny` en `.claude/settings.local.json` | session | las clases enumerables: secciones no declaradas, rutas `lead` listadas en §5.2, `generated`, `local` | Claude Code bloquea la herramienta | 07-hooks |
| Hook `PreToolUse` | session | cada escritura de las herramientas de edición y, por heurística, de Bash y PowerShell | `deny` con la razón localizada | 07-hooks |
| `save` y `submit` (CLI y MCP) | commit | todo el cambio: `git diff --name-status` contra el merge-base, más lo preparado y los archivos sin seguimiento | la operación falla sin commit ni push | 05-operaciones |
| Check de alcance en CI | ci | archivos del PR: modos por `git ls-tree`; lista de archivos por API (`GET /repos/{o}/{r}/pulls/{n}/files`; un renombre es `delete` + `create`) | el check falla | 09-ci |

- **REQ-PERM-12.** `save` y `submit` DEBEN negarse a hacer commit o push si alguna ruta del cambio recibe `allow: false`, y DEBEN listar todas las rutas denegadas con su código.
- **REQ-PERM-13.** El check de alcance DEBE fallar si la rama cabeza no tiene la forma `task/<n>`, si el autor del PR no es el reclamante o si alguien distinto del reclamante empujó a la rama (09 regla `scope`).
- **REQ-PERM-14.** El check de alcance DEBE evaluarse con la política de la base y la lista de archivos obtenida por API. NO DEBE ejecutar código de la rama del PR.
- **REQ-PERM-15.** Si el PR supera los 3000 archivos que devuelve la API, el check de alcance DEBE fallar. Para eso `list_pull_request_files` pagina hasta 30 páginas de 100 (08-forja REQ-FORGE-8): un PR de 1001 a 3000 archivos se evalúa entero, y uno de más de 3000 falla con el motivo `too_many_files`.

## 3. Matriz de aprobación

### 3.1 Reglas de la forja que la condicionan (GitHub)

El autor de un PR no puede aprobarlo, y con varios code owners en un patrón basta la aprobación de uno cualquiera. `require_last_push_approval` exige que el último push lo apruebe alguien distinto de quien lo hizo. `dismiss_stale_reviews_on_push` descarta las aprobaciones cuando cambia el diff, lo que incluye "Update branch" si la base trae cambios nuevos.

Por eso `CODEOWNERS` asigna **todas** las rutas a `lead` y `deputy` (§4.6). La garantía "ningún PR entra sin aprobación de `lead` o `deputy`" la da GitHub de forma nativa, y un PR no la puede falsificar. Como no hay tareas sobre secciones ajenas en v0.1, GitHub cubre toda la matriz.

### 3.2 Requisitos de aprobación

```python
def required_approvals(pr, pol: Policy) -> list[frozenset[str]]:
    """Hace falta al menos una aprobación vigente de cada conjunto: R1 y, desde H3, R2 (00-marco D-56)."""
    if pol.mode == "individual":                           # 00-marco D-57: sin aprobaciones (§3.6)
        return []
    reqs = [pol.approvers - {pr.author}]                   # R1: lead o deputy, distinto del autor
    if pol.editor and pr.author != pol.editor and pr.touches_latex:   # R2: el editor, si el PR toca LaTeX
        reqs.append(frozenset({pol.editor}))
    return reqs

def qualifying_approvers(pr, pol: Policy) -> frozenset[str]:
    return frozenset().union(*required_approvals(pr, pol))

def evaluate_approvals(pr, reviews, pol: Policy) -> dict:
    # pr.head_sha: head al integrar en la auditoría (§3.5); head actual en review show. Igual en ambos perfiles.
    latest = {}
    for r in sorted(reviews, key=lambda r: r.submitted_at):      # GET /repos/{o}/{r}/pulls/{n}/reviews
        if r.state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[r.user] = r                                   # la revisión completa
    approved = {u for u, r in latest.items()
                if r.state == "APPROVED" and r.commit_id == pr.head_sha and u != pr.author}
    qualifying = qualifying_approvers(pr, pol)
    blocking = {u for u, r in latest.items() if r.state == "CHANGES_REQUESTED" and u in qualifying}
    reqs = required_approvals(pr, pol)
    return {
        "ok": all(r & approved for r in reqs) and not blocking,
        "missing": [sorted(r) for r in reqs if r and not (r & approved)],
        "unsatisfiable": [] if all(reqs) else ["no_deputy"],   # autor = lead y no hay deputy
        "blocking": sorted(blocking),
    }
```

`pr.touches_latex` es verdadero con `format: latex` cuando el PR toca un `.tex` o el `pending.bib` de una parte, o un archivo de clase `editor`. Si el autor es el editor, R2 no se agrega: basta R1. Sin editor declarado, R1 ya lo cubre, porque el editor es el `lead`.

Una aprobación cuenta solo si se emitió sobre `pr.head_sha` (`commit_id`), en ambos perfiles: en la auditoría (§3.5) es la cabeza al integrar y en `review show`, la cabeza actual. Un `CHANGES_REQUESTED` vigente bloquea solo si viene de un login de `qualifying_approvers`.

- **REQ-PERM-16.** `review show`, `review list` y la auditoría (§3.5) DEBEN calcular `evaluate_approvals` con la política de la base.
- **REQ-PERM-17.** Toda etiqueta con valor de autorización (`ack:direction` y el retiro de `state:proposed`) DEBE validarse con el actor del evento en `GET /repos/{o}/{r}/issues/{n}/events`, que debe pertenecer a `approvers`, y no por su mera presencia; en los PR lo valida la regla `direction-ack` de `aucove/validate` (09).

### 3.3 Matriz por tipo de PR

El tipo lo dan los archivos tocados. Si un PR cae en varias filas, se suman los requisitos. "No permitido" significa que el check de alcance falla (§2.4).

| Tipo de PR | Autor `researcher` | Autor `deputy` | Autor `lead` | `enforced`: cómo se exige | `advisory` |
|---|---|---|---|---|---|
| Contenido de secciones propias (con nota y decisiones nuevas) | 1 de {`lead`, `deputy`} | `lead` | `deputy`; sin suplente, bypass (§3.4) | GitHub: `CODEOWNERS` + 1 aprobación + último push | consultivo; auditoría (§3.5) |
| Contenido con una sección ajena `S` | reservado (no permitido en v0.1) | reservado (no permitido en v0.1) | reservado (no permitido en v0.1) | — | — |
| Archivos `lead` (`tex/main.tex`, `tex/preamble.tex`, `tex/notation.tex`, `bib/references.bib`, `memory/constitution.md`, `AGENTS.md`, `CLAUDE.md`, `project.yaml`, `.latexmkrc`, `.gitignore` y rutas no listadas), en una tarea `task/<n>` del autor | no permitido | `lead` | `deputy`; sin suplente, bypass | GitHub + alcance | consultivo |
| Archivos del editor (`tex/main.tex`, `tex/preamble.tex`, `tex/notation.tex`, `tex/outline/*.tex`, `tex/guide.md` y `bib/references.bib`; desde H3, 00-marco D-56), en una tarea `task/<n>` del editor | solo si es el editor: 1 de {`lead`, `deputy`} | `lead` | `deputy`; sin suplente, bypass | GitHub + alcance + `aucove/editor-review` | consultivo |
| Decisiones y notas (`memory/decisions/*.md` nuevos, `memory/notes/task-<n>.md`) | 1 de {`lead`, `deputy`} | `lead` | `deputy` | GitHub; la inmutabilidad la verifica un check de 09-ci | consultivo |
| `plan.md` o `roles.yaml` (con `CODEOWNERS` regenerado), en una tarea `task/<n>` del autor | no permitido | `lead` | `deputy`; sin suplente, bypass | GitHub + alcance + sincronía de roles | consultivo |
| CI y agentes (`.github/workflows/**`, `.github/ISSUE_TEMPLATE/**`, `.github/pull_request_template.md`, `.claude/**`, `.agents/**`, `.mcp.json`, `.devcontainer/**`) | no permitido | `lead`; no permitido cuando existe `aucove-workflows` (en `.github/workflows/**`, §4.7b) | `deputy`; sin suplente, bypass | GitHub + `aucove-workflows` (§4.7b); sin push rulesets, revisar el diff es la única defensa contra un workflow que falsifique checks | consultivo |

A todos los tipos se suman dos condiciones. Si el PR lleva `flag:direction-change`, se exige `ack:direction` puesta por `lead` o `deputy` (flujo en 04-tareas, validación según REQ-PERM-17). Un PR que cambia `plan.md` DEBERÍA incluir una decisión nueva; `review show` avisa si falta.

Con `format: latex`, desde H3, a todo PR que toca LaTeX se suma R2: la aprobación del editor del artículo si está declarado y no es el autor (00-marco D-56). La exige el check requerido `aucove/editor-review` (09-ci), porque el editor no es code owner (§4.6). Con `mode: individual` no hay aprobaciones (§3.6).

Reversión: tarea normal de `lead` o `deputy`; la aprueba el otro (D-17).

- **REQ-PERM-18.** `aucove review show` DEBE destacar en su salida todo PR que toque rutas de control, y DEBE indicar qué requisitos de `required_approvals` faltan.

### 3.4 Sin suplente: bypass registrado

Si `roles.yaml` no declara `deputy`, los PR del `lead` no tienen aprobador posible (D-17).

- **REQ-PERM-19.** Sin `deputy`, `init` DEBE advertir que los PR del `lead` necesitarán bypass, y DEBE pedir una confirmación tecleada antes de agregar al `lead` como actor de bypass.
- **REQ-PERM-20.** El actor de bypass DEBE ser solo el `lead`, con `actor_type: "User"` y `bypass_mode: "pull_request"`. NO DEBEN usarse `"always"` ni `"exempt"`, porque `exempt` no genera registro de auditoría.
- **REQ-PERM-21.** Con `deputy` declarado, la lista `bypass_actors` del ruleset `aucove-base` DEBE estar vacía. `doctor` DEBE advertir si no lo está y pedir que se vuelva a ejecutar `init`.
- **REQ-PERM-22.** Antes de usar el bypass, el `lead` DEBERÍA tener todos los checks requeridos en verde. La auditoría (§3.5) marca los bypass con checks en rojo.

Si el `lead` rechaza el bypass, sus PR quedan bloqueados hasta que se declare un suplente. Es un estado válido.

### 3.5 Auditoría posterior a la integración

Un workflow que se dispara con `push` a la base usa la definición de la base, que ya está revisada. Ese workflow clasifica cada cambio (implementación: job `aucove/base-audit` de 09 §7; derivación del registro en 11-trazabilidad):

| `merge_audit` | Condición |
|---|---|
| `compliant` | PR integrado con `evaluate_approvals(...).ok` verdadero al integrarse |
| `bypass_no_deputy` | PR del `lead`, sin `deputy`, integrado sin aprobación |
| `bypass` | cualquier otro PR integrado sin cumplir la matriz |
| `direct_push` | commit en la base sin PR asociado (`GET /repos/{o}/{r}/commits/{sha}/pulls`) |
| `force_push` | `before` no es ancestro de `after` |

- **REQ-PERM-23.** Todo `merge_audit` distinto de `compliant` DEBE marcarse con `flag:bypass` (en el PR, o en un Issue nuevo si no hay PR) con mención a `lead` y `deputy`, y quedar derivable por `derive_approval_record` (11 §4.3); ningún workflow escribe archivos y la exportación a `records/approvals/` está reservada.

### 3.6 Uso individual (`mode: individual`)

Con `mode: individual` (00-marco D-57; 03-esquemas REQ-SCHEMA-94) hay una sola persona activa, el `lead`, que también es el editor. No hay a quién pedir aprobación: el flujo conserva el PR, el CI y los sellos, pero no la aprobación. Desde H3, con `init` (EV-11).

- **REQ-PERM-59.** Con `mode: individual`, `required_approvals` DEBE devolver una lista vacía (§3.2), e `init` DEBE crear `aucove-base` (§4.7) con `required_approving_review_count: 0`, `require_code_owner_review: false` y `require_last_push_approval: false`, sin `bypass_actors`. Las demás reglas, incluidos los checks requeridos, no cambian, y §3.4 no aplica. La auditoría (§3.5) clasifica como `compliant` toda integración por PR con los checks requeridos en verde.
- **REQ-PERM-60.** Al pasar de `individual` a `group`, el `lead` DEBE volver a ejecutar `init` para restaurar las reglas de aprobación de §4.7. Mientras no lo haga, `doctor` DEBE avisar con `W_FORGE_RULES_NOT_APPLIED`.

## 4. Configuración de GitHub que aplica `init` (perfil `enforced`)

### 4.1 Precondiciones y orden

`init` es H (§6). El perfil lo decide la detección de capacidades (08-forja).

- **REQ-PERM-24.** `init` DEBE verificar que `forge.base_branch` es la rama por defecto del repositorio. Si no lo es, DEBE fallar con `E_CONFIG_BASE_NOT_DEFAULT`.
- **REQ-PERM-25.** Cada paso de `init` DEBE ser idempotente: lee el estado, compara y escribe solo si difiere. DEBE informar `applied`, `unchanged`, `skipped` o `failed` por paso.
- **REQ-PERM-26.** `init` DEBE aplicar los rulesets como última protección, después de publicar en la base los archivos generados (`CODEOWNERS`).

Orden: (1) ajustes del repo, (2) permisos de Actions (`actions/permissions` con `sha_pinning_required`, `actions/permissions/workflow` y, en repos públicos, la política que permite `pull_request_target`) y el environment `aucove-evidence` (§4.3b), (3) colaboradores, (4) etiquetas, (5) archivos generados en la base, (6) rulesets, (7) verificación (§4.11), (8) la tarea de preparación (05-operaciones §9.3; 00-marco D-70) e informe de los ajustes de organización (§4.10).

### 4.2 Ajustes del repositorio

`PATCH /repos/{owner}/{repo}`:

```json
{
  "allow_squash_merge": true,
  "allow_merge_commit": false,
  "allow_rebase_merge": false,
  "squash_merge_commit_title": "PR_TITLE",
  "squash_merge_commit_message": "PR_BODY",
  "allow_auto_merge": false,
  "allow_update_branch": false,
  "delete_branch_on_merge": true,
  "allow_forking": false,
  "has_wiki": false
}
```

- `allow_auto_merge: false`: un agente no puede programar una integración que ocurra sin un clic humano (D-09).
- `allow_update_branch: false`: oculta "Update branch". Si el `lead` actualizara el PR de otro, pasaría a ser el último en empujar y no podría aprobarlo. La actualización la hace el reclamante con `aucove update`.
- `has_wiki: false`: la wiki es contenido que se edita sin PR y se volvería superficie de inyección. Es DEBERÍA; `init` PUEDE omitirlo si el `lead` lo pide.
- `allow_forking: false` solo aplica a repos privados.

### 4.3 Permisos de Actions

`PUT /repos/{owner}/{repo}/actions/permissions` con `{"enabled": true, "allowed_actions": <actual o el de 10-seguridad>, "sha_pinning_required": true}` (leer con GET y escribir solo si difiere). En repos públicos, `init` DEBE crear o actualizar una política de Actions del repositorio que permita `pull_request_target` (endpoint fijado por prueba de contrato de 08). Mientras esa prueba no fije el endpoint, `init` lo deja en `data.pending_steps`, con el ajuste en la web.

`default_workflow_permissions` no limita los `permissions:` explícitos de los workflows del propio repositorio.

`PUT /repos/{owner}/{repo}/actions/permissions/workflow`:

```json
{ "default_workflow_permissions": "read", "can_approve_pull_request_reviews": false }
```

- **REQ-PERM-27.** `init` DEBE fijar `can_approve_pull_request_reviews: false`, para que ningún workflow pueda aprobar PR con `GITHUB_TOKEN`.
- **REQ-PERM-51.** `init` DEBE fijar `sha_pinning_required: true`.
- **REQ-PERM-57.** En repos privados, `init` DEBE fijar en 400 días la retención de artefactos y registros de Actions: `PUT /repos/{owner}/{repo}/actions/permissions/artifact-and-log-retention` con `{"days": 400}`, leyendo antes el valor y escribiendo solo si es menor (endpoint por confirmar en S5). Si la organización impone un máximo menor o el endpoint no existe, lo agrega a `data.pending_steps` con el ajuste en la web. En repos públicos, el máximo es 90 días. `seal.yml` pide 400 y la forja lo recorta al máximo vigente (09-ci §7.4).
- **REQ-PERM-56.** Antes del paso (2), `init` DEBE leer la política de Actions efectiva: `GET /repos/{owner}/{repo}/actions/permissions` y, si el repo es de una organización, `GET /orgs/{org}/actions/permissions`; con `allowed_actions: "selected"`, además `GET …/actions/permissions/selected-actions` del mismo nivel. Si la política impide alguna acción de la tabla de abajo (`local_only`, o `selected` sin ella), `init` DEBE agregar `W_FORGE_ACTIONS_RESTRICTED` con `details = {"level": "org" | "repo", "allowed_actions": <valor>, "missing": ["<acción>@<sha>", …]}` y la configuración mínima en el `hint`, y NO DEBE ampliar la política por su cuenta: la de la org afecta a otros repos (§4.10) y la del repo la decide el `lead`. Esos endpoints exigen permiso de administración. Si una lectura falla por permisos y la otra no muestra ya la restricción, el resultado es desconocido y el aviso lleva `"level": null`, `"allowed_actions": "unknown"` y la tabla completa en `missing`. Se implementa en H3, con `init`; en H2 solo se reserva el código (00-marco D-39).

Acciones que necesitan los workflows de la plantilla (09-ci). Sus SHA son los de 09-ci y cambian con él: `missing` y el `hint` DEBEN construirse con los `uses:` del render de la plantilla, no con una lista copiada a mano. Los workflows reutilizables locales (`./.github/workflows/compile.yml`) corren con cualquier política.

| Acción | Autor | ¿La cubre «Allow actions created by GitHub»? |
|---|---|---|
| `actions/checkout` | GitHub | sí |
| `actions/upload-artifact` | GitHub | sí |
| `actions/download-artifact` | GitHub | sí |
| `astral-sh/setup-uv` | tercero | no: se agrega por patrón |

**Configuración mínima de la organización** (la aplica un owner; `init` la imprime en el `hint` y la guía de una página la documenta): en *Settings → Actions → General → Policies*, elegir «Allow <org>, and select non-<org>, actions and reusable workflows», marcar «Allow actions created by GitHub», dejar sin marcar «Allow actions by Marketplace verified creators» y agregar el patrón `astral-sh/setup-uv@*`; además, activar «Require actions to be pinned to a full-length commit SHA» (10-seguridad REQ-SEC-48), que es lo que obliga a fijarlas por SHA. Por API: `PUT /orgs/{org}/actions/permissions` con `"allowed_actions": "selected"`, y `PUT /orgs/{org}/actions/permissions/selected-actions` con `{"github_owned_allowed": true, "verified_allowed": false, "patterns_allowed": ["astral-sh/setup-uv@*"]}`. No se recomienda nunca «Allow all actions» (10-seguridad TH-12).

Texto del aviso (el catálogo lo toma de aquí; `{level}` es `org` o `repo`, y `{missing}`, la lista separada por comas):

- `es`: «La política de Actions ({level}) no permite: {missing}. Los workflows de la plantilla no pueden correr. Pide a un owner que permita esas acciones, fijadas por SHA (configuración mínima en la guía).» Si el resultado es desconocido: «No se pudo leer la política de Actions (hace falta permiso de administración). Confirma con un owner que permite: {missing}.»
- `en`: «The Actions policy ({level}) does not allow: {missing}. The template workflows cannot run. Ask an owner to allow these actions, pinned by SHA (minimum settings in the guide).» If the result is unknown: «Could not read the Actions policy (admin access is required). Check with an owner that it allows: {missing}.»

Cada workflow declara sus permisos en su bloque `permissions:` (09-ci). La lista de acciones permitidas es de 10-seguridad.

### 4.3b Environment `aucove-evidence` (00-marco D-72)

El job `integration` de `seal.yml` declara el environment `aucove-evidence` (09-ci REQ-CI-61). Un workflow que nombra un environment que no existe lo crea sin reglas ni secretos, y entonces cualquier rama podría usarlo; por eso `init` lo crea antes, con su regla.

- **REQ-PERM-61.** Si el repositorio admite environments (es público, o privado con Pro, Team o Enterprise: los mismos casos que el perfil `enforced`), `init` DEBE crear o actualizar `aucove-evidence`, sea cual sea `evidence_signing.mode`, con `deployment_branch_policy: {"protected_branches": false, "custom_branch_policies": true}` y una sola regla de despliegue, de tipo `branch`, igual a `forge.base_branch`, y DEBE borrar cualquier otra regla. No le pone revisores ni temporizador. Antes, con el token de administración, `init` DEBE leer los nombres de los secretos de Actions del repositorio y de los de la organización visibles para el repositorio, y fallar con `E_CONFIG_EVIDENCE_KEY` (`details.reason: repo_secret`) si alguno se llama `AUCOVE_EVIDENCE_KEY` (10-seguridad REQ-SEC-87). Con `mode: ci`, si el environment no tiene ese secreto, `init` lo agrega a `data.pending_steps`: el admin lo pone en la web o con `gh secret set AUCOVE_EVIDENCE_KEY --env aucove-evidence`, porque `init` nunca escribe secretos. Si una lectura de secretos falla por permisos, el resultado es desconocido, y la comprobación queda en `data.pending_steps` para hacerla a mano.

### 4.4 Colaboradores

Precondición (00-marco D-25): cada persona de `people` ya es miembro de la organización, con la invitación aceptada (REQ-PERM-55).

`PUT /repos/{owner}/{repo}/collaborators/{username}` con `permission`:

| Persona | `permission` | Motivo |
|---|---|---|
| `lead` | `admin` (precondición de `init`; no se modifica) | administrar el ruleset y los ajustes |
| `deputy` | `push` | code owner: necesita escritura explícita |
| `researcher` | `push` | crear la ref `task/<n>` (D-10) y empujar |

- **REQ-PERM-28.** Toda persona de `people` DEBE tener acceso directo de escritura al repo, porque los code owners necesitan escritura explícita. `doctor` e `init` DEBEN informar a quien no lo tenga (`GET .../collaborators/{u}/permission`).
- **REQ-PERM-29.** `init` NO DEBE quitar colaboradores. Informa las diferencias, y la baja se hace según 10-seguridad.
- **REQ-PERM-55.** Antes de `init`, el owner de la organización DEBE invitar a la organización a cada persona de `people` (`PUT /orgs/{org}/memberships/{username}` o desde la web; solo un owner puede hacerlo), y la persona DEBE aceptar la invitación. Si no, el `PUT .../collaborators/{u}` de esta sección la deja como colaboradora externa y su token fine-grained no sirve (08-forja §5.4). `init` y `doctor`, ejecutados por el `lead`, DEBEN listar `GET /repos/{o}/{r}/collaborators?affiliation=outside` y, si aparece alguna persona de `people`, emitir `W_AUTH_BROAD_TOKEN` con `details.reason: "outside_collaborator"` y `details.logins`, sin código nuevo. La única excepción admitida es la de 00-marco D-25: `gh auth login` (`gho_`), nunca el token clásico.
- **REQ-PERM-30.** `sync-roles` solo regenera, desde el árbol de trabajo de la tarea, los archivos `generated` que dependen de `roles.yaml` (`.github/CODEOWNERS`, `.github/ISSUE_TEMPLATE/task.yml`). No escribe en la forja; con red solo verifica logins. Invitaciones y permisos los concilia solo `init` (H), que se vuelve a ejecutar tras integrar un cambio de `roles.yaml`.

### 4.5 Etiquetas

`POST /repos/{owner}/{repo}/labels` con `name`, `color` y `description`. Si la respuesta es 422 `already_exists`, se usa `PATCH /repos/{owner}/{repo}/labels/{name}`. La descripción se localiza según `project.yaml: language` (D-02); el nombre no.

| Etiqueta | Color | Descripción (`es`) | Descripción (`en`) |
|---|---|---|---|
| `task` | `1d76db` | Tarea del proyecto | Project task |
| `direction` | `c5def5` | Propuesta de cambio de dirección | Direction change proposal |
| `state:proposed` | `fbca04` | Propuesta; falta que la acepte el responsable | Proposed; awaiting lead acceptance |
| `state:expired` | `bfbfbf` | Reclamo vencido por inactividad | Claim expired due to inactivity |
| `flag:overlap` | `d93f0b` | Posible traslape (consultivo) | Possible overlap (advisory) |
| `flag:direction-change` | `5319e7` | Posible cambio de dirección (consultivo) | Possible direction change (advisory) |
| `flag:blocked` | `b60205` | Bloqueada por otra tarea o decisión | Blocked by another task or decision |
| `flag:instructions` | `d4c5f9` | Posibles instrucciones ocultas (consultivo) | Possible hidden instructions (advisory) |
| `ack:direction` | `0e8a16` | El responsable reconoció el cambio de dirección | Lead acknowledged the direction change |
| `ack:overlap` | `c2e0c6` | El responsable o el suplente confirmó el traslape | Lead or deputy confirmed the overlap |
| `main-broken` | `b60205` | La rama base no compila o no valida | Base branch fails build or validation |
| `flag:bypass` (nueva, §9) | `e99695` | Integrado sin cumplir la matriz de aprobación | Merged without meeting the approval matrix |

`init` crea todas las etiquetas de 00-marco §5.4 salvo las reservadas.

### 4.6 Generación de `CODEOWNERS`

Reglas:

1. Ruta: `.github/CODEOWNERS`, de clase `generated`.
2. Una sola línea efectiva: `*` seguido de tres espacios, `@<lead>`, un espacio y `@<deputy>` (`*   @<lead> @<deputy>`, como en el ejemplo), o `*   @<lead>` si no hay suplente; estos son los bytes que compara REQ-PERM-31. No hay más líneas efectivas en v0.1. Como gana el último patrón que coincide, cualquier otra línea podría quitarle rutas al `lead`.
3. La propiedad de las secciones va como comentario, ordenada por `id`. Sirve de documentación y no tiene efecto.
4. Cabecera `# source: roles.yaml sha256:<12 hex>`, con el `sha256` de la forma canónica de `roles.yaml`, sin `generator:` ni versión.
5. UTF-8, finales de línea LF, salto de línea final. Salida determinista: con la misma entrada, los mismos bytes.

```text
# Generado por `aucove sync-roles` desde roles.yaml. No editar a mano.
# source: roles.yaml sha256:3f9a1c0b7e2d
*   @ana @bruno
# section introduction: owner @ana
# section methods: owner @carla
# section results: owner @bruno
```

- **REQ-PERM-31.** El check de sincronía de roles (09-ci) DEBE fallar si `.github/CODEOWNERS` no es idéntico, byte a byte tras normalizar CRLF a LF (como los demás archivos `generated`, 01-arquitectura REQ-ARCH-88), a la regeneración desde el `roles.yaml` de la cabeza del PR. `sync-roles --check`, `doctor` y la regla `roles-sync` comparan igual en todos sus modos. También DEBE fallar si `GET /repos/{o}/{r}/codeowners/errors?ref=<head>` devuelve errores.

La regeneración usa la cabeza a propósito, para que el PR que cambia `roles.yaml` incluya su `CODEOWNERS`. La autorización sigue usando la base (REQ-PERM-1).

### 4.7 Ruleset de la rama base

`POST /repos/{owner}/{repo}/rulesets` o, si ya existe uno con ese nombre, `PUT /repos/{owner}/{repo}/rulesets/{ruleset_id}`:

```json
{
  "name": "aucove-base",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["refs/heads/main"], "exclude": [] } },
  "bypass_actors": [],
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" },
    { "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 1,
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": true,
        "require_last_push_approval": true,
        "required_review_thread_resolution": false,
        "allowed_merge_methods": ["squash"] } },
    { "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": false,
        "do_not_enforce_on_create": false,
        "required_status_checks": [
          { "context": "aucove/build", "integration_id": 15368 },
          { "context": "aucove/validate", "integration_id": 15368 } ] } }
  ]
}
```

- `refs/heads/main` se sustituye por `refs/heads/<forge.base_branch>`.
- Sin suplente, con la confirmación de REQ-PERM-19: `"bypass_actors": [{"actor_id": <id numérico del lead, GET /users/{login}>, "actor_type": "User", "bypass_mode": "pull_request"}]`.
- `integration_id: 15368` es la app `github-actions`. Así solo cuentan los checks que emite Actions. Sin ese valor, cualquier colaborador con escritura podría crear un estado de commit con el mismo `context` a través de la API.
- `strict_required_status_checks_policy: false`: si se exigiera que la rama estuviera al día, cada integración obligaría a actualizar y volver a aprobar todos los PR abiertos. El riesgo de romper la base al combinar dos PR lo cubre `main-broken` (09-ci). Ver Q-PERM-1.
- Los administradores del repo no se saltan un ruleset salvo que figuren en `bypass_actors`.
- Con `mode: individual`, los tres parámetros de aprobación cambian según REQ-PERM-59. Con `format: latex`, desde H3, `required_status_checks` incluye además `aucove/editor-review` (00-marco D-56).

- **REQ-PERM-32.** En el perfil `enforced`, el ruleset `aucove-base` DEBE estar `active` y contener exactamente las reglas de este apartado.
- **REQ-PERM-33.** `init` NO DEBE crear reglas `update` ni `creation` sobre la rama base, porque impedirían integrar PR.

### 4.7b Otros rulesets (perfil `enforced`)

- **REQ-PERM-52.** `init` DEBE crear `{"name":"aucove-task-branches","target":"branch","enforcement":"active","conditions":{"ref_name":{"include":["refs/heads/task/*"],"exclude":[]}},"bypass_actors":[],"rules":[{"type":"non_fast_forward"}]}`.
- **REQ-PERM-53.** Si `capabilities().push_rulesets` es verdadero, `init` DEBE crear `{"name":"aucove-workflows","target":"push","enforcement":"active","bypass_actors":[{"actor_id":<id del lead>,"actor_type":"User","bypass_mode":"always"}],"rules":[{"type":"file_path_restriction","parameters":{"restricted_file_paths":[".github/workflows/**"]}}]}`; es la única excepción a REQ-PERM-20; `.github/CODEOWNERS` no se restringe. Sin push rulesets, `init` y `doctor` emiten `W_FORGE_CHECKS_SPOOFABLE` y la auditoría (§3.5) recalcula alcance y aprobaciones.

### 4.8 Checks requeridos

`aucove/build` y `aucove/validate` (autoridad: 09-ci). Con `format: latex`, desde H3, también `aucove/editor-review` (00-marco D-56). Correspondencia: `aucove-scope` → regla `scope`; aprobaciones → GitHub (CODEOWNERS); `aucove-gate` → innecesario (REQ-CI-11).

- **REQ-PERM-34.** Los nombres de los checks requeridos DEBEN vivir en una sola constante del núcleo, compartida por `init`, `doctor` y la generación de workflows. La constante contiene `("aucove/build", "aucove/validate")`; desde H3 le suma `aucove/editor-review` cuando `format: latex`.

Queda un riesgo: un PR que modifica `.github/workflows/**` (con `aucove-workflows`, solo uno del `lead`) puede agregar un job con el mismo nombre que un check requerido. Ese PR exige la aprobación de `lead` o `deputy` por `CODEOWNERS`, y `review show` lo destaca (REQ-PERM-18). Ver Q-PERM-6.

### 4.9 Método de integración y trazabilidad

Solo se admite squash, forzado en el ruleset (`allowed_merge_methods`) y en los ajustes del repo. El título del commit es el del PR y el cuerpo es el del PR (`PR_BODY`). Así, los trailers que `submit` escribe al final del cuerpo del PR (formato en 11-trazabilidad) quedan en el commit de la base.

- **REQ-PERM-36.** `init` DEBE aplicar los ajustes de §4.2 (salvo `has_wiki`, que es DEBERÍA), incluidos `squash_merge_commit_title: "PR_TITLE"` y `squash_merge_commit_message: "PR_BODY"`.

Quien integra puede editar el mensaje en la web, y la API de merge acepta `commit_message`. Detectar que se perdieron los trailers le toca al check de trazabilidad posterior a la integración (11-trazabilidad y 09-ci).

### 4.10 Ajustes de organización recomendados

`init` no los cambia, porque afectan a otros repos. Los lee con `GET /orgs/{org}` (el detalle completo requiere ser owner), informa las diferencias e imprime el comando para el owner.

| Ajuste | Valor | API |
|---|---|---|
| Permiso base de los miembros | `none` | `PATCH /orgs/{org}` `default_repository_permission` |
| Forks de repos privados | prohibidos | `members_can_fork_private_repositories: false` |
| Cambiar la visibilidad de repos | solo owners | `members_can_change_repo_visibility: false` |
| Borrar repos | solo owners | `members_can_delete_repositories: false` |
| Crear repos públicos | solo owners (DEBERÍA) | `members_can_create_public_repositories: false` |
| Token de Actions y aprobación | `read`, sin aprobar | `PUT /orgs/{org}/actions/permissions/workflow` |
| Acciones permitidas | `selected`: las de GitHub más `astral-sh/setup-uv` (§4.3, REQ-PERM-56) | `PUT /orgs/{org}/actions/permissions` y `…/actions/permissions/selected-actions` |
| 2FA obligatoria | activada | solo en la web (supuesto: no hay endpoint REST de escritura) |

- **REQ-PERM-37.** `doctor` DEBE informar cada ajuste de esta tabla que no coincida, si el usuario puede leerlo.

### 4.11 Verificación y deriva

- **REQ-PERM-38.** Tras aplicar, `init` DEBE verificar las reglas efectivas con `GET /repos/{o}/{r}/rules/branches/{base}`, y DEBE fallar si falta alguna regla de §4.7.
- **REQ-PERM-39.** `doctor` DEBE comparar el ruleset, los ajustes del repo, las etiquetas, los errores de `CODEOWNERS` y los permisos de los colaboradores con lo esperado, e informar cada diferencia. Los rulesets activos los puede leer cualquiera con acceso de lectura, así que un `researcher` puede detectar que alguien desactivó la protección.

## 5. Perfil `advisory`

GitHub Free con repo privado no ofrece rulesets, ramas protegidas, code owners obligatorios ni checks requeridos.

| Garantía | `enforced` | `advisory` |
|---|---|---|
| Todo cambio a la base entra por PR | ruleset | **no**: quien tenga escritura, o su agente, puede empujar directo o editar en la web |
| Aprobación de `lead` o `deputy` | `CODEOWNERS` + 1 aprobación | **no**: se puede integrar sin aprobar |
| Checks que bloquean | `required_status_checks` | **no**: corren y se ven en rojo, pero no bloquean |
| Aprobación del editor del artículo (D-56) | `aucove/editor-review` requerido | **no**: el check corre y se ve, pero no bloquea |
| Sin force push ni borrado de la base | `non_fast_forward`, `deletion` | **no** |
| Aprobación posterior al último push | `require_last_push_approval` | **no** |
| Solo squash, sin auto-merge | ruleset + ajustes del repo | **sí**, por los ajustes del repo (§4.2) |
| Actions no aprueba PR | §4.3 | **sí** |
| Revisión pedida al dueño | `CODEOWNERS` + `submit` | solo `submit` (pide revisores por API) |
| Revisión con modelo en CI | reservada | **no** |

**Compensaciones:**

1. Checks consultivos: los mismos workflows y los mismos nombres. `review show` muestra su resultado.
2. Auditoría (§3.5): todo `direct_push`, `force_push` o `bypass` queda registrado con `flag:bypass` y se avisa al `lead`.
3. Capas locales iguales que en `enforced` (§6): el núcleo nunca empuja a la base, y los hooks deniegan push a la base y comandos de integración.
4. Aviso permanente: `doctor`, `status` y el contexto de SessionStart muestran una línea fija, por ejemplo "Perfil advisory: la forja no exige la aprobación; la auditoría registra las integraciones sin revisión".
5. Mejora detectada: si la forja pasa a ofrecer `enforced` (por ejemplo, GitHub Team gratuito vía GitHub Education), `doctor` lo detecta y recomienda volver a ejecutar `init`. La mejora vía Education se hace desde el panel de GitHub Education, sobre una organización («Upgrade your academic organizations»), nunca desde la página de facturación (00-marco D-26).

- **REQ-PERM-40.** En el perfil `advisory`, `init` DEBE mostrar la tabla de garantías perdidas y exigir que el `lead` teclee la frase de aceptación localizada ("acepto el perfil advisory" o "I accept the advisory profile"). Sin ella, DEBE terminar con `E_FORGE_CAPABILITY_MISSING`.
- **REQ-PERM-41.** La aceptación DEBE quedar versionada en `project.yaml: forge.advisory_acceptance` (`by` y `at`), dentro del commit de `init`. El `lead` la registra además en una decisión de `memory/decisions/` desde su primera tarea, porque el nombre de un registro exige el número de una tarea (03-esquemas REQ-SCHEMA-46); `init` lo deja en `data.pending_steps` (00-marco D-63).
- **REQ-PERM-42.** En `advisory`, `init` DEBE aplicar igualmente §4.2 a §4.6, para que pasar a `enforced` solo requiera el ruleset.

**Modo por forks (alternativa reservada, no implementada en v0.1).** Solo el `lead` tiene escritura, y los investigadores abren PR desde forks. Impide que un investigador integre, pero rompe el reclamo por creación de ref (D-10), que exige escritura en el repo canónico. Además, los workflows de PR desde forks reciben un token de solo lectura y no reciben secretos, así que las etiquetas y comentarios de CI necesitarían `pull_request_target`. También choca con la recomendación de prohibir forks de repos privados (§4.10), y el `lead` sigue pudiendo empujar directo. Ver Q-PERM-4.

## 6. Operaciones solo humanas: aplicación en capas

Son aprobar, pedir cambios, aceptar y cancelar tareas, `init` (D-09) y firmar el índice del paquete de evidencia (D-72). Integrar tampoco la hace el núcleo. A esto se suman los cambios de protección (ruleset, colaboradores, ajustes), que solo hace `init`.

| Capa | Mecanismo | Cubre | No cubre | Autoridad |
|---|---|---|---|---|
| 1. MCP | Las operaciones H no existen como herramienta ni como skill | el agente vía MCP | el agente vía shell | 05-operaciones |
| 2. CLI | TTY, contexto no agente y frase tecleada (abajo) | `aucove review approve` invocado por Bash o PowerShell | pseudo-TTY, variables borradas, `gh` y la API directos | este capítulo, 05-operaciones |
| 3. Permisos y hooks | Reglas `deny` estáticas en `.claude/settings.json` y `PreToolUse` sobre `Bash\|PowerShell` | las formas habituales de la tabla de abajo | formas ofuscadas (`sh -c`, ruta completa, variables) y puertas sin hooks | 07-hooks |
| 4. Forja | Ruleset sin bypass (con suplente), code owners, aprobación del último push | push directo a la base (incluso admin); integrar sin aprobación | el agente del `lead` o del `deputy` aprobando PR ajenos con su token | §4 |
| 5. Auditoría | §3.5 y deriva en `doctor` (§4.11) | detección posterior | prevención | 09-ci, 11-trazabilidad |

**Intenciones que DEBEN denegarse.** Esta tabla es la autoridad de 07-hooks §4.3, que define cómo se detectan, incluidas las variantes de PowerShell:

| Intención | Formas mínimas |
|---|---|
| Integrar | `gh pr merge` (con `--admin` y `--auto`); `gh api` con `PUT` a `repos/*/pulls/*/merge`; GraphQL `mergePullRequest`, `enablePullRequestAutoMerge`, `enqueuePullRequest`; `git push` cuyo destino sea la base (`main`, `HEAD:main`, `refs/heads/main`) |
| Aprobar, pedir cambios o descartar | `gh pr review` con `--approve`/`-a` o `--request-changes`/`-r`; `gh api` con `POST`/`PUT` a `repos/*/pulls/*/reviews*`; GraphQL `addPullRequestReview`, `submitPullRequestReview`, `dismissPullRequestReview` |
| Aceptar o cancelar tareas, reconocer dirección | `gh issue edit` con `--remove-label state:proposed` o `--add-label ack:direction`; `gh issue close` sobre un Issue `task` |
| Cambiar protecciones | `gh api` con método de escritura a `*/rulesets*`, `*/branches/*/protection*`, `*/collaborators/*`, `*/actions/permissions*`, `PATCH repos/{o}/{r}`; `gh repo edit` |
| Operaciones H de la CLI | `aucove init`, `aucove review approve`, `aucove review request-changes`, `aucove task accept`, `aucove task cancel`, `aucove evidence sign` |
| Firmar con la llave del proyecto (D-72) | `ssh-keygen` con `-Y sign` |
| HTTP directo a la forja | `curl`, `wget`, `Invoke-RestMethod` o `Invoke-WebRequest` a `api.github.com` con un método distinto de `GET` |

Comprobación de la capa 2, `require_human`, igual para todas las operaciones H. Se aplica en dos fases (05-operaciones §10 la cita):

1. **Fase 1, `human_precheck`**, antes de leer credenciales y de cualquier llamada de red: `CLAUDE_CODE_CHILD_SESSION` no vacía, `CI=true` o `GITHUB_ACTIONS=true` → `E_HUMAN_ONLY_AGENT_ENV`; stdin o stdout sin TTY → `E_HUMAN_ONLY_NO_TTY`.
2. **Fase 2, `human_confirm`**, tras las lecturas: resumen a stderr (con `head_sha` y el estado del CI en las revisiones) y reto `<op> <target>`, con `op` ∈ {`approve`, `request-changes`, `accept`, `cancel`, `init`, `sign`} y `target` = `<pr>`, `<n>`, `<owner>/<repo>` o el nombre de la carpeta del paquete. Hay un solo intento, y EOF equivale a `E_HUMAN_ONLY_NOT_CONFIRMED`. En `advisory`, `init` pide una segunda frase, «acepto el perfil advisory» / «I accept the advisory profile» (REQ-PERM-40); sin suplente (REQ-PERM-19), la frase `bypass <login del lead>`. Devuelve `HumanConfirmation(operation, confirmed_at)`.

```python
import os, sys
from dataclasses import dataclass
from datetime import datetime, timezone

H_OPS = {"approve", "request-changes", "accept", "cancel", "init", "sign"}

@dataclass(frozen=True)
class HumanConfirmation:                  # la exige 08-forja (REQ-FORGE-10)
    operation: str; confirmed_at: datetime

def human_precheck() -> None:
    """Fase 1: antes de credenciales y red."""
    if (os.environ.get("CLAUDE_CODE_CHILD_SESSION")                       # no vacía
            or os.environ.get("CI") == "true" or os.environ.get("GITHUB_ACTIONS") == "true"):
        raise HumanOnly("E_HUMAN_ONLY_AGENT_ENV")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise HumanOnly("E_HUMAN_ONLY_NO_TTY")

def _type_once(phrase: str, code: str) -> None:
    try:
        typed = input(t("confirm.type_exactly", challenge=phrase))       # un solo intento
    except EOFError:
        raise HumanOnly("E_HUMAN_ONLY_NOT_CONFIRMED")
    if typed.strip() != phrase:
        raise HumanOnly(code)

def human_confirm(op: str, target: str, summary: str, *, advisory: bool = False,
                  bypass_lead: str | None = None) -> HumanConfirmation:
    """Fase 2: tras las lecturas. target: '<pr>', '<n>', '<owner>/<repo>' o la carpeta del paquete."""
    if op not in H_OPS:
        raise ValueError(op)
    print(summary, file=sys.stderr)                    # en revisiones, con head_sha y estado del CI
    _type_once(f"{op} {target}", "E_HUMAN_ONLY_NOT_CONFIRMED")             # p. ej. "approve 42"
    if op == "init" and advisory:                                          # REQ-PERM-40
        _type_once(t("confirm.accept_advisory"), "E_FORGE_CAPABILITY_MISSING")
    if op == "init" and bypass_lead:                                       # sin suplente, REQ-PERM-19
        _type_once(f"bypass {bypass_lead}", "E_HUMAN_ONLY_NOT_CONFIRMED")
    return HumanConfirmation(operation=op, confirmed_at=datetime.now(timezone.utc))
```

- **REQ-PERM-43.** Las operaciones H NO DEBEN aceptar opciones que salten la confirmación (`--yes`, `--force` o la entrada por tubería).
- **REQ-PERM-44.** La detección de contexto de agente DEBE usar `CLAUDE_CODE_CHILD_SESSION` y NO DEBE usar `CLAUDECODE`, porque las extensiones de IDE ponen `CLAUDECODE=1` también en sus terminales integradas, donde trabaja el humano.
- **REQ-PERM-45.** `review approve` DEBE mostrar el resumen del PR para un sha concreto y enviar `POST /repos/{o}/{r}/pulls/{n}/reviews` con `event: "APPROVE"` y `commit_id` igual a ese sha.
- **REQ-PERM-46.** Las herramientas MCP `prepare_review` y `list_reviews` DEBEN devolver el enlace web del PR como vía canónica para aprobar e integrar.

**Riesgo residual.** En modo mínimo, el agente del `lead` o del `deputy` corre con el token de esa persona. Puede aprobar PR ajenos y luego integrarlos, y la forja lo atribuye a la persona. Los permisos de GitHub no separan abrir un PR de aprobarlo, ni empujar de integrar. Las capas 1 a 3 solo son fricción; la documentación de Claude Code declara frágiles las reglas de Bash que restringen argumentos. Lo mismo pasa en las sesiones web, donde `gh` sale por el proxy con las credenciales reales. Como mitigación, `lead` y `deputy` DEBERÍAN trabajar en el modo de permisos por defecto, que pide confirmación para Bash, y no en `bypassPermissions` ni `auto`. Donde exista sandbox (macOS, Linux, WSL2), la credencial se aísla (10-seguridad), y la auditoría (§3.5) detecta después. El riesgo se registra en 10-seguridad. Ver Q-PERM-5.

## 7. Matriz de capacidades por puerta

"Garantía al integrar" es igual en todas las puertas: en `enforced` la dan la forja y los checks, y en `advisory` solo la auditoría. Lo que cambia es el control **durante la sesión**.

| Puerta | Hooks | Skills | MCP stdio | git local | Compilación local | Control en sesión |
|---|---|---|---|---|---|---|
| Claude Code CLI | sí (`.claude/settings.json`); en Windows, matcher `Bash\|PowerShell` | `.claude/skills/` | `.mcp.json` | sí | opcional (LaTeX o devcontainer) | capas 1-3 y núcleo en `save`/`submit` |
| Extensión VS Code | sí (comparte ajustes con la CLI) | sí | sí | sí | opcional | igual que la CLI |
| JetBrains | sí (ejecuta `claude` en la terminal del IDE) | sí | sí | sí | opcional | igual que la CLI |
| Desktop, pestaña Code (local) | sí ("hooks and skills… apply to both") | sí | `.mcp.json` y `claude_desktop_config.json` | sí; git para worktrees y `gh` para PR | opcional | igual que la CLI; en Windows no lee perfiles de PowerShell, así que `aucove` debe estar en el PATH del usuario |
| Sesión web (nube) | sí, los de `.claude/settings.json` en sesiones de un solo repo; `settings.local.json` no viaja | `.claude/skills/` | `.mcp.json` (un repo) | clon en VM; `git push` a cualquier rama, sin poder borrarlas; la API de datos de git, bloqueada (S1, 00-marco D-41) | no (PDF de CI, D-14) | capas 1-3 solo si `aucove` se instala en la VM (01-arquitectura); si no, ninguna; `gh` con credenciales reales vía proxy |
| Codex | existen (PreToolUse deniega Bash, `apply_patch` y MCP), y v0.1 los entrega desde H4 en `.codex/hooks.json` (00-marco D-58): cada persona aprueba la definición una vez en `/hooks`, porque Codex confía por hash; S6 lo verifica | `.agents/skills/` | `.codex/config.toml` `[mcp_servers.aucove]` | sí | opcional | capas 1-2 y núcleo en `save`/`submit` |
| Gemini CLI | existen (BeforeTool `deny`), con huella y aviso en hooks de proyecto; v0.1 no los entrega, porque desde el 18/06/2026 Gemini CLI solo atiende claves de API de pago y licencias empresariales (00-marco D-58) | `.agents/skills/` (alias) | `.gemini/settings.json` `mcpServers` | sí | opcional | ídem; AGENTS.md requiere `context.fileName` |
| Antigravity CLI | por verificar en S7 (12-mvp); si pasa, se entregan después de los de Codex (00-marco D-58) | por verificar en S7 | por verificar en S7 | sí | opcional | hasta S7, solo el núcleo en `save`/`submit`; la garantía es el CI |
| OpenCode | no hay hooks; hay plugins JS en `.opencode/plugins/` (`tool.execute.before`) que se cargan sin confirmación, y reglas `permission.bash`; v0.1 no los entrega | `.agents/skills/` (también lee `.claude/skills/`) | `opencode.json` `mcp` `type: local` | sí | opcional | ídem |
| goose | existen (PreToolUse, desde 2026-05) en `.agents/plugins/<n>/hooks/hooks.json`; su capacidad de bloquear no está verificada | `.agents/skills/` | extensiones stdio | sí | opcional | ídem |
| Otro agente que lee `AGENTS.md` (nivel básico, 00-marco D-58) | no | según el agente (`.agents/skills/`) | si lo admite | sí | opcional | solo el núcleo en `save`/`submit`; la garantía es el CI |
| Web de GitHub | no | no | no | no | no | ninguno: es la vía canónica para aprobar e integrar |

En Codex y Gemini CLI, cualquier cambio en la definición de un hook lo deja inactivo hasta que cada usuario lo vuelva a aprobar. Como v0.1 entrega los de Codex (00-marco D-58) y después, si S7 lo confirma, los de Antigravity CLI, la definición DEBE ser estable: siempre `aucove hook <evento> --door <puerta>`. goose, OpenCode y Gemini CLI se documentan solo con API key o modelos locales (ver 10-seguridad). Desktop en su pestaña Chat, Cowork y claude.ai quedan fuera de v0.1 (reservados para el modo completo).

- **REQ-PERM-47.** `doctor` DEBE informar la puerta detectada y su fila de esta matriz. Si no puede detectarla, DEBE informar "desconocida" con el control de sesión más bajo.
- **REQ-PERM-48.** Si un hook no puede cargar la política o el núcleo, DEBE bloquear la escritura (07-hooks).

## 8. Autoaprobación, suplente, ausencia y SLA

**Autoaprobación.** En `mode: individual` no hay aprobaciones (§3.6). En los demás casos, GitHub la impide. El núcleo la rechaza antes con `E_PERM_SELF_REVIEW`, para dar un error claro. El `lead` NO DEBERÍA empujar a la rama de otra persona, porque quedaría como último en empujar y su aprobación no contaría.

**Suplente.** Aprueba cualquier PR, incluidos los del `lead`, pide cambios, acepta y cancela tareas, y modifica archivos `lead` en su propia tarea con la aprobación del `lead`. No ejecuta `init` ni es actor de bypass. En v0.1 hay un solo suplente.

**Ausencia del responsable.**

| Duración | Qué pasa |
|---|---|
| Menor que el SLA | El `deputy` revisa los PR de otros (es code owner de `*`). Los PR del `lead` esperan. |
| Prolongada | El `deputy` abre una tarea que cambia `lead` en `roles.yaml`. Solo puede aprobarla el `lead` actual, así que, si no está, se aplica el procedimiento de emergencia. |
| Emergencia o sucesión | Un owner de la organización se agrega temporalmente como actor de bypass (`pull_request`), integra el cambio de `roles.yaml` y lo quita. Luego se ejecuta `init` para restaurar el ruleset. Queda como `bypass` en la auditoría y como incidente (10-seguridad). |

**SLA de revisión.** `project.yaml: review.sla_hours` (propuesto, §9; por defecto `72`). El reloj empieza cuando el PR pasa a `in_review` y se reinicia con cada `submit`.

- **REQ-PERM-49.** `review list` y `status` DEBEN marcar como vencido todo PR `in_review` que supere `review.sla_hours`. A partir de 2 × SLA, DEBEN sugerir pedir la revisión al `deputy`.
- **REQ-PERM-50.** Vencer el SLA NO DEBE cambiar ningún permiso ni aprobación, ni integrar nada.

Un workflow programado PUEDE comentar en los PR vencidos mencionando a `lead` y `deputy` (09-ci). Un PR en revisión no hace vencer el reclamo (04-tareas).

## 9. Identificadores de este capítulo

| Identificador | Tipo | Definición |
|---|---|---|
| `aucove-base` | nombre de ruleset | Ruleset de la rama base que crea `init` (§4.7) |
| `aucove-task-branches`, `aucove-workflows` | nombres de ruleset | Ruleset de `refs/heads/task/*` y push ruleset de `.github/workflows/**` (§4.7b) |
| `flag:bypass` | etiqueta | Integrado sin cumplir la matriz (§3.5) |
| `session`, `commit`, `ci` | valores del modo de `check_path` | §2.4 |
| `allow`, `code`, `path`, `path_class`, `section`, `owner`, `confirm`, `requires_approval_of`, `warning` | campos de la decisión de `check_path` | §2.4 |
| `merge_audit`; valores `compliant`, `bypass_no_deputy`, `bypass`, `direct_push`, `force_push` | resultado de la auditoría | §3.5; job `aucove/base-audit` (09 §7); derivable por `derive_approval_record` (11 §4.3) |
| `forge.advisory_acceptance` (`by`, `at`) | clave de `project.yaml` | REQ-PERM-41 |
| `review.sla_hours` | clave de `project.yaml` | §8 |
| `E_PERM_NOT_A_MEMBER`, `E_PERM_SECTION_NOT_OWNED`, `E_PERM_OUTSIDE_TASK`, `E_PERM_LEAD_ONLY_PATH`, `E_PERM_APPEND_ONLY`, `E_PERM_GENERATED_PATH`, `E_PERM_LOCAL_PATH` (los de `perm_code` de 05-operaciones), más `E_PERM_OUTSIDE_REPO` y `E_PERM_SYMLINK` | códigos de error | `retryable: false`; catálogo en 05-operaciones |
| `E_HUMAN_ONLY_AGENT_ENV`, `E_HUMAN_ONLY_NO_TTY`, `E_HUMAN_ONLY_NOT_CONFIRMED` | códigos de error | §6 |
| `editor` (clase de ruta), `E_PERM_EDITOR_ONLY_PATH`, R2 (aprobación del editor), REQ-PERM-58 | clase, código de error, requisito de aprobación y requisito | §2.2, §2.4, §3.2 y §3.3; 00-marco D-56; desde H3 |
| REQ-PERM-59, REQ-PERM-60 | requisitos del uso individual | §3.6; 00-marco D-57; desde H3 |
| `E_CONFIG_POLICY_UNAVAILABLE`, `W_CONFIG_POLICY_FALLBACK` | código de error y aviso | REQ-PERM-3, REQ-PERM-4 |
| `W_FORGE_CHECKS_SPOOFABLE` | aviso | REQ-PERM-53 |
| `W_FORGE_ACTIONS_RESTRICTED` | aviso | REQ-PERM-56 (00-marco D-39) |
| `REQ-PERM-57` | requisito | Retención de 400 días de artefactos y registros de Actions en repos privados (§4.3; 00-marco D-46) |
| `E_CONFIG_BASE_NOT_DEFAULT` | código de error | REQ-PERM-24 |
| REQ-PERM-61, environment `aucove-evidence`, `E_CONFIG_EVIDENCE_KEY` (`repo_secret`) | requisito, environment y código de error | El environment de la firma del proyecto, con la rama base como única regla, y la llave nunca como secreto del repositorio (§4.3b; 00-marco D-72) |
| `sign` | valor de `op` del reto de §6 | `aucove evidence sign` (00-marco D-72) |
| Actor de `sync-roles`: `lead` / `deputy` | cambio en 00-marco §5.7 | el suplente regenera `CODEOWNERS` cuando edita `roles.yaml` en nombre del `lead` |
| `.mcp.json`, `.codex/**`, `.gemini/**`, `opencode.json`, `.opencode/**`, `.agents/plugins/**` → clase `lead` | agregar a 00-marco §5.2 | ya son `lead` por defecto; se listan porque son rutas de control |

## 10. Supuestos sobre otros capítulos

- **02-convencion:** las partes viven en `sections/<section-id>/`; no se admiten enlaces simbólicos en el repo. `canonical()` y `classify()` viven en `aucove.domain.paths`, que no hace E/S, así que la detección de enlaces de REQ-CONV-10 le llega a `check_path` como `Policy.link_probe` (00-marco D-32 (d)).
- **03-esquemas:** `roles.yaml` tiene la forma de §2.1 (`deputy`, `people[].login`, `people[].role`, `sections[].id`, `sections[].owner`). El formulario de tarea tiene un campo `sections`. `project.yaml` admite `forge.base_branch`, `forge.advisory_acceptance`, `review.sla_hours` y `language`.
- **04-tareas:** expone `claimant(n)`, `state(n)` y `accepted_by(n)`, este último verificado con el actor del evento. Una tarea creada por un `researcher` sin aceptación no tiene `accepted_by` en `approvers`. `update` integra la base con merge, sin reescribir historia. Borrar `task/<n>` después de integrar no altera el estado `merged`. D-19 (enmendada) permite rutas `lead` en tareas de `lead`/`deputy` (Q-PERM-3).
- **05-operaciones:** los argumentos de `check-path` y `check_path` (tarea explícita; modo y `login` solo en CI); el catálogo incluye los códigos de §9; las operaciones H llaman a `require_human`; `submit` pide revisión al `lead` o, si el autor es el `lead`, al `deputy`, y a quienes pidieron cambios (05 §9.18 paso 9).
- **07-hooks:** genera `settings.local.json` desde `check_path` y lo regenera en SessionStart, salvo en la nube (`CLAUDE_CODE_REMOTE=true`), donde ese archivo no se lee y no hay L2 (07-hooks §12). Las reglas `deny` de §6 van en `.claude/settings.json`. Los hooks bloquean ante cualquier fallo y traducen `confirm: true` en `ask`.
- **08-forja:** `capabilities()` indica si hay rulesets, code owners obligatorios, checks requeridos y actor de bypass `User`, y expone `push_rulesets`. La identidad se entrega como `login`. La sesión web empuja a `task/<n>` con `git push` y reclama por push con lease (00-marco D-41 y D-42).
- **09-ci:** implementa `aucove/build` y `aucove/validate` (con `scope` y `direction-ack`), la sincronía de roles y la auditoría `aucove/base-audit`. La regla `scope` no ejecuta código de la cabeza.
- **10-seguridad:** registra el riesgo residual de §6, el aislamiento de credenciales, las bajas, el incidente de emergencia y las acciones permitidas.
- **11-trazabilidad:** acepta squash con `PR_BODY`, con los trailers al final del cuerpo del PR. `derive_approval_record` (11 §4.3) deriva `merge_audit`.
- **01-arquitectura:** registra el servidor MCP en cada puerta, instala `aucove` en la VM de la sesión web y ubica la constante de nombres de checks.

## 11. Preguntas abiertas

- **Q-PERM-1.** ¿Exigir la rama al día (`strict_required_status_checks_policy: true`) tras el piloto? Evita que dos PR en verde rompan la base al combinarse, pero obliga a volver a aprobar tras cada integración.
- **Q-PERM-2.** ¿Listar a los dueños de sección en `CODEOWNERS` para que GitHub les pida revisión? Debilitaría la garantía nativa de §3.1. Alternativa verificada: `required_reviewers` por patrón en la regla `pull_request`; requiere equipos, así que v0.1 no la usa.
- **Q-PERM-3.** ¿Deben las tareas del `lead` declarar también los archivos `lead` que tocan, para detectar traslapes?
- **Q-PERM-4.** ¿Se implementa el modo por forks para `advisory` en una fase posterior, pese a sus costos (§5)?
- **Q-PERM-5.** ¿Una cuenta de máquina por persona para las sesiones de agente, sin rol de code owner, eliminaría el riesgo residual de §6? ¿Es aceptable según los términos de GitHub y de cada proveedor de IA?
- **Q-PERM-6.** ¿Están disponibles en GitHub Team los rulesets de organización con "require workflows to pass"? Evitarían que un PR falsifique checks. No se pudo verificar.
- **Q-PERM-7.** ¿Acepta `actor_type: "User"` repos de cuentas personales? Si no, ¿el perfil `enforced` sin suplente exige un repo de organización? Verificado: el enum de `actor_type` incluye `User`; falta probarlo en repos de cuentas personales.
- **Q-PERM-8.** ¿Usar `dismissal_restriction` en la regla `pull_request` para que solo `lead` y `deputy` descarten revisiones? Verificado: existe `pull_request.parameters.dismissal_restriction = {"enabled": true, "allowed_actors": [...]}`; falta decidir si se usa.
- **Q-PERM-9.** ¿Molesta al suplente recibir una petición de revisión en cada PR (por `* @lead @deputy`)?
- **Q-PERM-10.** ¿Cómo se detecta la puerta de forma fiable? Por ejemplo, los valores de `CLAUDE_CODE_ENTRYPOINT` no están documentados.
- **Q-PERM-11.** En Git Bash (mintty), `isatty()` puede devolver falso para un humano. ¿Se documenta `winpty` o se admite otra señal?
- **Q-PERM-12.** ¿Qué valor por defecto para `review.sla_hours`?

## 12. Referencias

Consultadas el 2026-09-29.

- **GitHub, rulesets:** REST y `bypass_mode`/`exempt`: https://docs.github.com/en/rest/repos/rules · reglas disponibles: https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets · acerca de: https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets · bypass: https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository · `exempt` sin auditoría: https://github.blog/changelog/2025-09-10-github-ruleset-exemptions-and-repository-insights-updates/ · usuarios como actores de bypass: https://github.blog/changelog/2026-05-07-repository-rulesets-user-bypass-and-branch-renaming
- **GitHub, revisión:** ramas protegidas (último push, aprobaciones obsoletas, plan): https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches · code owners: https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners · autoaprobación: https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/reviewing-changes-in-pull-requests/approving-a-pull-request-with-required-reviews · job omitido = éxito: https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/about-status-checks
- **GitHub, REST:** repositorio: https://docs.github.com/en/rest/repos/repos#update-a-repository · organización: https://docs.github.com/en/rest/orgs/orgs#update-an-organization · Actions: https://docs.github.com/en/rest/actions/permissions · colaboradores: https://docs.github.com/en/rest/collaborators/collaborators · PR (3000 archivos, merge): https://docs.github.com/en/rest/pulls/pulls · app `github-actions` (id 15368): https://api.github.com/apps/github-actions
- **GitHub, planes:** https://docs.github.com/en/get-started/learning-about-github/githubs-plans · https://docs.github.com/en/education/about-github-education/github-education-for-teachers/about-github-education-for-teachers
- **Claude Code:** permisos (reglas frágiles, deny primero, PowerShell): https://code.claude.com/docs/en/permissions · hooks: https://code.claude.com/docs/en/hooks · variables de entorno: https://code.claude.com/docs/en/env-vars · sesiones web: https://code.claude.com/docs/en/cloud-environments · Desktop: https://code.claude.com/docs/en/desktop · VS Code: https://code.claude.com/docs/en/vs-code · JetBrains: https://code.claude.com/docs/en/jetbrains
- **Codex:** hooks: https://learn.chatgpt.com/docs/hooks · MCP: https://developers.openai.com/codex/mcp
- **Gemini CLI:** hooks: https://geminicli.com/docs/hooks/reference/ · seguridad de hooks de proyecto: https://geminicli.com/docs/hooks/ · configuración: https://geminicli.com/docs/reference/configuration.md
- **OpenCode:** https://opencode.ai/docs/plugins/ · https://opencode.ai/docs/permissions/ · https://opencode.ai/docs/mcp-servers/ · https://opencode.ai/docs/rules/
- **goose:** hooks: https://goose-docs.ai/blog/2026/05/14/goose-hooks · AGENTS.md: https://goose-docs.ai/docs/guides/context-engineering/using-goosehints · skills: https://goose-docs.ai/docs/guides/context-engineering/using-skills
- **Skills:** https://agentskills.io/specification

**Supuestos no verificados en fuente oficial:** que no exista un endpoint REST para exigir 2FA; la capacidad de bloqueo de los hooks de goose; que `actor_type: "User"` funcione en repos personales; el comportamiento de `isatty()` en mintty.
