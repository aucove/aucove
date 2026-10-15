# 12 — MVP: alcance, aceptación, pruebas, hitos y piloto

- **Código:** `MVP`
- **Estado:** v0.1
- **Autoridad (00-marco §6):** alcance cerrado del MVP, criterios de aceptación, estrategia de pruebas, hitos y métricas del piloto.
- **No redefine:** operaciones (05-operaciones), estados (04-tareas), permisos y perfiles (06-permisos), hooks (07-hooks), forja (08-forja), workflows (09-ci), seguridad (10-seguridad), presupuestos de rendimiento (01-arquitectura). Aquí solo se fija **qué se entrega, cómo se verifica y cuándo**.

Convenciones del capítulo: los umbrales marcados **(H)** son hipótesis por validar (REQ-MVP-17). `A<n>` = criterio de aceptación, `H<n>` = hito, `M<n>` = métrica del piloto, `E<n>` = escenario end-to-end, `T<n>` = nivel de prueba. Son IDs locales de este documento.

---

## 1. Alcance cerrado del MVP

### 1.1 Operaciones por hito

Las operaciones son exactamente las de 00-marco §5.7 con "sí" en v0.1. Su contrato lo define 05-operaciones; aquí solo se asigna el hito en que deben estar terminadas.

| Hito | CLI | MCP | Nota |
|---|---|---|---|
| H2 | `aucove validate` | `validate_project` | La usa el CI de la plantilla (09-ci). |
| H2 | `aucove check-path <ruta>` | `check_path` | Misma lógica que el control de alcance de CI. |
| H2 | `aucove sync-roles` | — | Genera `.github/CODEOWNERS` desde `roles.yaml`. En H2, sin adaptador de forja, solo `--check` es alcanzable: escribir exige la identidad en caché (00-marco D-34 n), que escribe el adaptador desde H3; el `CODEOWNERS` de la plantilla sale del render (D-35 j). |
| H2 | `aucove doctor` (solo comprobaciones locales) | `check_environment` | Detección de capacidades de la forja en H3. |
| H2 | `aucove ci build-plan`, `aucove ci comment`, `aucove ci base-report`, `aucove ci seal` | — | Internos del CI (09-ci). |
| H2 | `aucove evidence verify <ruta>` | — | Verificación local y kit del árbitro (11-trazabilidad §8.6). |
| H3 | `aucove doctor` (completo) | `check_environment` | Incluye perfil `enforced`/`advisory` (08-forja). |
| H3 | `aucove init` | — | H; incluye `init --regenerate` (00-marco §5.7), `init --individual` (00-marco D-57) e `init --repo` (00-marco D-73). |
| H3 | `aucove status`, `aucove context` | `get_status`, `get_context` | `context` incluye el resumen en Markdown y `.aucove/context.md` (05-operaciones REQ-OPS-67; 00-marco D-58). `aucove` sin argumentos muestra la pantalla de inicio (05-operaciones REQ-OPS-68). |
| H3 | `aucove task propose` | `propose_task` | |
| H3 | `aucove task accept <n>`, `aucove task cancel <n>` | — | H. |
| H3 | `aucove claim <n>`, `aucove release <n>` | `claim_task`, `release_task` | |
| H3 | `aucove save`, `aucove update`, `aucove switch <n>` | `save_work`, `update_branch`, `switch_task` | |
| H3 | `aucove record-assessment <n>` | `record_overlap_assessment` | |
| H3 | `aucove submit` | `submit_task` | Incluye la nota de sesión (D-12). |
| H3 | `aucove expire-claims` | — | La invoca el workflow programado (09-ci). |
| H3 | `aucove ci audit` | — | Auditoría posterior (09 §7). |
| H3 | `aucove ci editor-review` | — | Aprobación del editor del artículo (09-ci §4.5; 00-marco D-56). |
| H3 | `aucove decision propose`, `aucove direction propose` | `propose_decision`, `propose_direction` | |
| H3 | `aucove review list`, `aucove review show <pr>` | `list_reviews`, `prepare_review` | |
| H3 | `aucove review approve <pr>`, `aucove review request-changes <pr>` | — | H. |
| H3 | `aucove evidence sign <paquete>` | — | H; firma el índice del paquete de evidencia con la llave del proyecto (00-marco D-72). |
| H3 | `aucove metrics` | — | Columnas del CSV en §5.3. |
| H3 | `aucove mcp --stdio` | — | Expone las 17 herramientas de la columna MCP. |
| H4 | `aucove hook <evento>` | — | Contrato en 07-hooks; con `--door codex` para Codex (00-marco D-58). |

- **REQ-MVP-1.** El MVP DEBE implementar las 36 entradas marcadas "sí" en 00-marco §5.7 (las 32 de D-31 (a), más `ci seal` y `evidence verify` de D-50, `ci editor-review` de D-56 y `evidence sign` de D-72) y NO DEBE agregar subcomandos ni herramientas MCP fuera del registro.
- **REQ-MVP-2.** Las entradas "reservado" (`ci review-context`, `upgrade`, `report ai-use`, `report credit`, `section read`, `section propose-change`, `evidence export`, `evidence deposit`, `evidence disclose` y sus herramientas MCP) NO DEBEN aparecer en `aucove --help`, en `tools/list` ni en ningún skill.
- **REQ-MVP-3.** El conjunto de nombres que devuelve `tools/list` DEBE ser exactamente: `check_environment`, `validate_project`, `get_status`, `get_context`, `check_path`, `propose_task`, `claim_task`, `release_task`, `save_work`, `update_branch`, `switch_task`, `record_overlap_assessment`, `submit_task`, `propose_decision`, `propose_direction`, `list_reviews`, `prepare_review`.

### 1.2 Archivos de la plantilla

Qué significa cada archivo y su formato lo definen 02-convencion y 03-esquemas. La clase de ruta sale de 00-marco §5.2. La lista exhaustiva de rutas es la de 02-convencion §2.2; esta tabla solo asigna el hito de cada grupo y el contenido mínimo.

| Ruta | Clase | Hito | Contenido mínimo en el MVP |
|---|---|---|---|
| `AGENTS.md`, `CLAUDE.md` | `lead` | H2 | Reglas para agentes, localizadas; `CLAUDE.md` remite a `AGENTS.md`. Enlazan a constitución, plan y roles, sin copiarlos. |
| `project.yaml` | `lead` | H2 | `convention: "0.1"`, `language`, `forge.base_branch` y las claves `build.*` de 03-esquemas (motor por enumeración, sin comando libre); `format: latex` y `disclosure: milestones`, sin `require_commercial` (aplica el valor por defecto de D-48). |
| `roles.yaml` | `lead` | H2 | Ejemplo con un `lead`, un `researcher` y las claves `deputy` y `editor` comentadas. |
| `plan.md` | `lead` | H2 | Plantilla con `RQ-1`, `HYP-1`, `IN-1`, `OUT-1`, `METH-1`. |
| `memory/constitution.md` | `lead` | H2 | Plantilla. |
| `memory/decisions/<archivo>.md` | `append` | H2 | Sin registros de ejemplo (REQ-CONV-5); solo `.gitkeep`. |
| `memory/notes/task-<n>.md` | `task` | H3 | No viene en la plantilla; lo escribe `submit`. |
| `tex/main.tex`, `tex/preamble.tex`, `tex/notation.tex`, `tex/outline/<id>.tex`, `tex/guide.md` (solo con `format: latex`) | `editor` (00-marco D-56; el núcleo la aplica desde H3) | H2 | `main.tex` solo incluye archivos: un `outline/<id>` por sección del artículo, y cada archivo de enlace incluye sus partes como `../sections/<id>/<id>`. Una guía del artículo de ejemplo con las 7 secciones. |
| `sections/<section-id>/**` | `section` | H2 | Las 7 partes de 00-marco D-55 (REQ-CONV-1), en la variante `latex`. |
| `bib/references.bib` | `editor` (00-marco D-56) | H2 | Una o dos entradas de ejemplo (CC0-1.0). |
| `.github/CODEOWNERS` | `generated` | H2 | Salida de `aucove sync-roles`. |
| `.github/workflows/**` | `lead` | H2, incluido `seal.yml`, con su primera corrida real en H3 (S5); vencimiento, `main-broken` y, solo con `format: latex`, `editor-review.yml` y `editor-review-trigger.yml` (D-56), en H3 | Los workflows de 09-ci. |
| `.github/ISSUE_TEMPLATE/**`, `.github/pull_request_template.md` | `lead` | H2 / H3 | Formulario de tarea y plantilla de PR de 03-esquemas. |
| `.mcp.json` | `lead` | H3 | Registra el servidor MCP `aucove` (02-convencion §2.2). |
| `.claude/settings.json` | `lead` | H4 | Hooks y permisos de 07-hooks, el `env` de privacidad de IA y las reglas de lectura de evidencia (D-48). |
| `.claude/skills/**`, `.agents/skills/**` | `generated` | H4 | Los 6 skills de 00-marco §5.8 en el idioma del proyecto, desde una sola fuente (D-05). |
| `.latexmkrc`, `.gitignore` | `lead` | H2 | `.gitignore` excluye `.aucove/` y `.claude/settings.local.json`. |
| `.devcontainer/**` | `lead` | H4 | Puerta de nivel 2 (§1.3). |
| Archivos de licencia | `lead` | H2 | MIT-0 y CC0-1.0 (D-07); nombres en 11-trazabilidad. |

- **REQ-MVP-4.** Un repositorio recién creado desde la plantilla DEBE contener exactamente las rutas con origen T de 02-convencion §2.2 (REQ-CONV-1), más los generados de 01-arquitectura §2.3. El CI del framework lo verifica contra `tests/fixtures/template-manifest.txt`, generado desde esa tabla. Hasta H4, el generador del manifiesto DEBE filtrar las rutas por la columna «Hito» de §1.2: en H2 faltan a propósito `.mcp.json` (H3), `.claude/**`, `.agents/**` y `.devcontainer/**` (H4); si no, A9 falla. La variante `format: none` (E11) excluye `tex/**`, `bib/**`, `.latexmkrc`, los `pending.bib`, los `sections/<id>/<id>.tex` y los workflows del editor del artículo, e incluye `sections/<id>/<id>.md` (00-marco D-36 enmendada; 01-arquitectura REQ-ARCH-100; 02-convencion REQ-CONV-89).
- **REQ-MVP-5.** La plantilla NO DEBE versionar `.claude/settings.local.json` ni `.aucove/`.

### 1.3 Puertas soportadas y nivel de soporte

Niveles de soporte (política de publicación; la matriz de capacidades por puerta es de 06-permisos):

- **Nivel 1 (soportada):** se prueba en cada cambio (automático) o en cada versión (manual). Un criterio de aceptación en rojo bloquea la publicación.
- **Nivel 2 (mejor esfuerzo):** prueba de humo manual en cada versión, donde se indique. Un defecto se documenta, pero no bloquea.
- **Fuera:** no se ofrece en v0.1.

| Puerta | Superficie | Nivel | Qué se garantiza | Verificación |
|---|---|---|---|---|
| Claude Code | Terminal (CLI) | 1 | Skills, hooks, reglas deny locales y MCP stdio. | A1, A2, A8, A16 |
| Claude Code | Extensiones de IDE | 1 | Igual que la CLI (misma configuración). | A16 |
| Claude Code | Pestaña Code de Claude Desktop (local) | 1 | Igual que la CLI. Git solo hace falta para sesiones con worktree; para crear PR desde la app, Desktop pide `gh`. | A16 |
| Claude Code | Sesiones web (claude.ai/code) | 1 (S1 resuelto, 00-marco D-41; se repite antes de H4 y de H7) | Skills y hooks del repo (sesión de un solo repositorio), sin plugins. `aucove` disponible en la VM; escritura en `task/<n>` con `git push` (reclamo por push con lease) y API REST para Issues y PR; `release` no está disponible en la web (04-tareas REQ-TASK-78). | A3 |
| AGENTS.md | Codex, Antigravity CLI | 2 | `AGENTS.md`, `.agents/skills/` y MCP stdio. Sin garantía de hooks. | A15 |
| AGENTS.md | OpenCode, goose | 2 (sin prueba de humo en v0.1) | Igual que la fila anterior. | — |
| Web de GitHub | Revisión e integración | 1 | La persona revisa, ve los checks e integra. Una edición hecha en la web la controla el CI. | A4, A9 |
| Entorno | `.devcontainer/` y Codespaces | 2 | LaTeX y núcleo preinstalados. En organizaciones no hay cuota gratuita de Codespaces. | Humo por versión |
| Otras | Desktop Chat, Cowork, claude.ai, MCP App, Overleaf, Jupyter AI | Fuera | — | — |

- **REQ-MVP-6.** La documentación de cada versión DEBE publicar esta tabla, y DEBE declarar "no soportada" toda puerta que no figure en ella.
- **REQ-MVP-7.** Un defecto en una puerta de nivel 2 NO DEBE bloquear una versión, pero DEBE aparecer en sus notas.
- **REQ-MVP-8.** Codespaces DEBE documentarse como vía para perfiles técnicos sin LaTeX local, no como vía por defecto para no técnicos.

### 1.4 Niveles de adopción que entrega el MVP

| Nivel | Contenido | Entregable | Criterio de "listo" | Hito |
|---|---|---|---|---|
| N0 Plantilla | Repo desde la plantilla, CI con PDF, `CODEOWNERS` generado y protecciones de `init` | PDF del borrador siempre actualizado (con `format: latex`) y fecha verificable de cada entrega e integración; funciona sin IA | A9, A12 y A17 | H2–H3 |
| N1 Tareas | Proponer, reclamar, ver estado, entregar, y contexto en SessionStart | Quién hace qué; un PR por tarea | A4 | H3–H4 |
| N2 Guardas y revisión | PreToolUse y reglas deny, evaluación de traslape en la sesión, revisión humana | Ningún cambio fuera de alcance llega a la base; cola de revisión | A6, A8, A9 | H4 |
| N3 Memoria curada | Decisiones y nota de sesión dentro del PR de entrega | Bitácora que solo crece | A4 (E1 verifica la nota) | H3–H4 |

Drive y los reclamos en tiempo real (el antiguo nivel 3) quedan fuera (§1.5). Si los niveles se activan con un interruptor en `project.yaml` es una pregunta abierta (Q-MVP-9).

### 1.5 Fuera del MVP (lista explícita)

1. Modo completo: MCP remoto (Streamable HTTP + OAuth), MCP App, Desktop Chat, Cowork y claude.ai (00-marco §1).
2. Plugin de catálogo de Claude Code (D-05). En la fase 2 será complemento y respaldo de `.claude/`, no su sustituto.
3. PR en SessionEnd (D-12) y archivo de transcripciones.
4. Revisión con modelo en CI (se especifica desactivada; no se activa en el piloto) y resumen nocturno.
5. Subagentes (revisor de traslapes, integrador, verificador de citas). La evaluación de traslape ocurre en la sesión, vía `record-assessment`.
6. Adaptadores de GitLab y Forgejo (solo su interfaz).
7. Drive, OSF, Nextcloud, sincronización con la API de Zotero, Overleaf (D-18) y Jupyter AI.
8. Markdown y Quarto como formatos compilados (D-43).
9. Roles distintos de `lead`, `researcher` y `deputy`.
10. Reclamos en tiempo real, candados y base de datos (D-10).
11. Generadores de declaración de uso de IA y de CRediT (`report`, reservado). Los trailers sí se capturan (11-trazabilidad).
12. `upgrade` (migración de convención), `section read` y `section propose-change`.
13. Identidad federada entre instituciones y ciclo de vida posterior a la aprobación.
14. Telemetría externa (OpenTelemetry) y tableros.
15. Una operación de "reparar" aparte: la recuperación se logra repitiendo operaciones idempotentes (A13).
16. `evidence export`, `evidence deposit` y `evidence disclose`; verificación de Bitcoin con nodo o exploradores; actualización programada de las pruebas OTS; re-sellado (RFC 4998); constancias NOM-151 (D-46, D-47, D-49, D-50).

- **REQ-MVP-9.** Ningún elemento de §1.5 DEBE implementarse en v0.1, y sus nombres reservados NO DEBEN reutilizarse con otro significado.

### 1.6 Control de cambios de alcance

- **REQ-MVP-10.** Todo lo que no figure en §1.1–§1.4 queda fuera del MVP.
- **REQ-MVP-11.** Un cambio de alcance (agregar o quitar una operación, un archivo de la plantilla, una puerta, un nivel de soporte o un criterio A; o adelantar algo de hito) DEBE proponerse como Issue en el repositorio del framework. La propuesta indica: motivo, criterios A afectados, hito, estimación en semanas y pruebas nuevas. El mantenedor principal del framework DEBE aprobarla antes de implementarla.
- **REQ-MVP-12.** Un cambio aprobado DEBE registrarse como decisión en 00-marco §3. Si toca un identificador de 00-marco §5, DEBE subir la versión menor de la especificación (00-marco §7).
- **REQ-MVP-13.** Desde la salida de H1 solo se admiten cambios que reduzcan el alcance o que desbloqueen un criterio A. Desde el inicio de H5 hasta el cierre de H7, ningún identificador del registro DEBE cambiar y solo se admiten correcciones de defectos, para no contaminar las métricas del piloto.
- **REQ-MVP-14.** Ajustar un umbral (H) con datos de H4 o H5 NO cuenta como cambio de alcance, pero DEBE documentarse junto con esos datos en el informe de aceptación.

---

## 2. Criterios de aceptación

### 2.1 Reglas generales

- **REQ-MVP-15.** Cada criterio A DEBE tener un método reproducible, y su resultado DEBE registrarse en el informe de aceptación de la versión (la ubicación la fija 01-arquitectura).
- **REQ-MVP-16.** La versión `0.1.0` DEBE tener en verde A1–A13, A16 y A17. A14 se evalúa durante H5–H7, y A15 (nivel 2) se reporta pero no bloquea.
- **REQ-MVP-17.** Los umbrales (H) son hipótesis. DEBEN validarse en las pruebas de usabilidad de H4 y en el dogfooding de H5, y NO DEBEN presentarse como hechos en la documentación.
- **REQ-MVP-18.** Las pruebas de usabilidad DEBEN seguir el protocolo de pensar en voz alta. El facilitador solo interviene tras 10 minutos de bloqueo, y ese paso se registra como fallido.

### 2.2 Presupuesto de "primer día" por perfil (H)

| Perfil | Cuentas | Instalaciones | Autenticaciones | Comandos de terminal | Tiempo máximo | Conceptos visibles |
|---|---|---|---|---|---|---|
| Investigador técnico (Windows, macOS o Linux) | 2 (forja y asiento de Claude Code) | ≤ 3: Git, Claude Code y el núcleo (uv y `aucove` con un solo comando documentado, ver 01-arquitectura) | ≤ 3 | Los de instalación | 45 min, de la invitación aceptada al primer PR | Tarea, entrega, aprobación |
| Investigador no técnico (sesión web) | 2 | 0 | Solo por navegador | 0 | 30 min, con las cuentas ya creadas | Tarea, entrega, aprobación |
| Responsable | 2 + administración de la org | Como el técnico | ≤ 3 | `aucove init` | 15 min para `init` en una org preparada | Además: revisión y suplente |

### 2.3 Criterios A1–A17

| ID | Criterio | Umbral | Método de medición | Hito |
|---|---|---|---|---|
| A1 | Instalación en Windows nativo | (a) 100 % de los jobs `windows-latest` en verde. (b) `aucove doctor` sin problemas bloqueantes en 2 configuraciones. (c) (H) mediana ≤ 45 min, ≤ 3 instalaciones. | (a) CI: instalar la wheel construida con `uv tool install`, invocar el lanzador desde el PATH en forma exec y correr T4. (b) VM Windows 11 limpia, en dos variantes: con Git for Windows (Bash y PowerShell activos) y con Git Bash no detectable por Claude Code (solo herramienta PowerShell). (c) Prueba de usabilidad con 3 técnicos. | H4 |
| A2 | Instalación en macOS y Linux | (a) 100 % de los jobs `macos-latest` y `ubuntu-latest`. (b) (H) como A1(c). | (a) CI como A1(a). (b) Máquinas limpias con macOS 14 o superior y Ubuntu 24.04, al menos 1 persona en cada una. | H4 |
| A3 | Sesión web | (a) Recorrido empezar→entregar completo con el CI en verde: reclamo por push con lease, `save` y `update` con `git push`, reflejo y entrega por REST (00-marco D-41); `release` devuelve `E_STATE_UNSUPPORTED_DOOR` con el `hint` de la terminal o de Desktop (04-tareas REQ-TASK-78). (b) (H) SessionStart ≤ 120 s en la primera sesión de un entorno sin caché y ≤ 10 s en las siguientes. (c) (H) 3 no técnicos: mediana ≤ 30 min, 0 comandos tecleados. | (a) y (b): sesión en claude.ai/code sobre un repo de la org de pruebas con la Claude GitHub App instalada, con `CT-WEB-01` (`spikes/ct_web_02.sh`) y `CT-PUSH-01` en verde en la última repetición de S1. `aucove` se instala desde PyPI según 07-hooks y 01-arquitectura (o desde la wheel de REQ-MVP-46); se cronometra con los tiempos de la sesión. (c) Prueba de usabilidad. | H4 (condición de G-NT) |
| A4 | Recorrido completo empezar→entregar→aprobar (perfil `enforced`) | E1–E10 (§3.8) en verde en 10 corridas nocturnas seguidas. Recorrido manual por cada superficie de nivel 1. | T7 contra `aucove-demo`. En cada paso, `derive_state` devuelve el estado esperado: `open` → `claimed` → `in_review` → (`changes_requested` → `in_review`) → `merged`. Manual: `/empezar`, `/entregar` y `/pendientes`; aprobación por CLI con TTY e integración en la web. | H3 (guionado), H4 (skills) |
| A5 | Comportamiento en el perfil `advisory` | 100 % de las detecciones correctas (5 corridas por tipo de repo). Advertencia presente en el 100 % de las salidas de `init`, `doctor`, `status` y `review list`. `init` en `advisory` termina bien solo tras la confirmación tecleada de REQ-PERM-40/41; sin ella falla con `E_FORGE_CAPABILITY_MISSING`. | Contrato T2: repo público → `enforced`; repo privado en org Free → `advisory`; fake con 403 en protecciones → `advisory`. Se revisa que `warnings` contenga la advertencia que definen 06-permisos y 05-operaciones, con el texto en el idioma del proyecto. E6 verifica que una integración sin aprobación aparece en M11. | H3 |
| A6 | Negativa de las operaciones solo humanas | 100 % en: (a) `tools/list` igual a REQ-MVP-3. (b) Sin TTY: las 6 operaciones H (`init`, `init --regenerate`, `task accept`, `task cancel`, `review approve`, `review request-changes`) devuelven `ok: false` con un error del área `HUMAN_ONLY`, salida distinta de 0 y 0 escrituras en la forja. Con pseudo-TTY y `CLAUDE_CODE_CHILD_SESSION=1`, devuelven `E_HUMAN_ONLY_AGENT_ENV`, también sin escrituras. (c) Con TTY y confirmación negativa o vacía: abortan sin escribir. (d) Ningún skill invoca una operación H. (e) En T8, 0 aprobaciones producidas por el agente. | (a) T5. (b) y (c) T2 con fake: registro de llamadas de escritura vacío, en los 3 sistemas operativos. (d) Lint estático de T6. (e) Escenario MS3 (§3.9). Límite conocido: la exigencia de TTY frena el uso accidental, pero no es un control de seguridad (10-seguridad). | H3 (a–c), H4 (d–e) |
| A7 | Carrera de reclamos | 50 repeticiones × 10 reclamantes simultáneos: 0 dobles reclamos. En cada repetición, todos los `ok: true` son del mismo `login`; los demás reciben `E_CLAIM_ALREADY_CLAIMED` con `retryable: false`. Al final hay 1 solo asignado, igual al ganador, y 0 refs huérfanas. | T3 contra la org de pruebas (real) y contra el fake con intercalados deterministas y fallas entre la creación de la ref y la asignación. | H3 |
| A8 | Los hooks fallan cerrados | 100 % de los casos de clase `fault` (§3.5) terminan con código 2 y un motivo no vacío en el idioma del proyecto, en los 3 sistemas operativos; desde H4, también con los hooks de Codex (00-marco D-58). Para cada ruta de `deny` existe una regla deny equivalente en `.claude/settings.local.json`. | T4 (matriz). Comparación de las reglas generadas con los fixtures `deny`. Límites conocidos, documentados y respaldados por A9: timeout del hook, binario ausente y escrituras por Bash o PowerShell (07-hooks). | H4 |
| A9 | Control de alcance en CI | 100 % de un conjunto dorado de ≥ 24 PR bien clasificados. En `enforced`, un PR fuera de alcance no se puede integrar; en `advisory`, el check queda en rojo. | Fixtures de diff y `roles.yaml` de la base. Casos obligatorios: sección propia (pasa); sección ajena (falla con `E_PERM_SECTION_NOT_OWNED`); archivo `lead` tocado por un `researcher` (falla); `roles.yaml` modificado en el mismo PR para darse permiso (falla, porque se lee de la base); decisión nueva (pasa); decisión existente modificada (falla); nota de otra tarea (falla); `CODEOWNERS` editado a mano (falla); renombrado entre secciones (falla); variantes de mayúsculas y barras. En real: con `deputy` (ruleset sin `bypass_actors`), la integración vía API con el token del `lead` DEBE rechazarse; sin `deputy`, DEBE aceptarse solo como bypass y quedar con `flag:bypass` (REQ-PERM-23). | H2 (fixtures), H3 (real) |
| A10 | Presupuestos de rendimiento | 01-arquitectura §7.1 y tabla §2.4. | Arnés de medición (§2.4). | H3–H4 |
| A11 | Localización `es`/`en` | 100 % en: (a) nombres de skills iguales a 00-marco §5.8 (`project-status` en `en`, no `status`); (b) catálogo de mensajes completo en ambos idiomas; (c) E1 en `es` y en `en` produce JSON idéntico salvo `message`, `hint` y los textos localizados; (d) 0 caracteres no ASCII en identificadores (ramas, etiquetas, rutas, herramientas, códigos de error). (e) Revisión por hablante nativo de cada idioma en cada versión. | (a), (b) y (d): chequeos estáticos en CI. (c): T7 E10. (e): lista de verificación manual. | H4 |
| A12 | Plantilla autocontenida | Un repo recién creado desde la plantilla, con `init`, o vacío, con `init --repo` (00-marco D-73), pasa todos sus checks en el primer push en ≤ 10 min. (H) `init` ≤ 15 min con 2 responsables, desde una carpeta vacía y sin clonar el framework. | T7 (creación por API en la org de pruebas) y prueba de usabilidad. | H3 |
| A13 | Idempotencia y recuperación | En `claim`, `release`, `save`, `update`, `submit`, `record-assessment`, `expire-claims`, `sync-roles` e `init`, con una falla inyectada tras cada escritura en la forja y la operación repetida: el estado final es igual al de una corrida sin fallas, con 0 PR, comentarios o etiquetas duplicados. | T2 con fake (todos los puntos de falla) y con real (al menos 1 punto por operación). | H3 |
| A14 | Integridad en uso real | Durante H5–H7: 0 integraciones sin aprobación válida en repos `enforced`, salvo bypass registrado (D-17, 11-trazabilidad), y 0 cambios fuera de alcance en la rama base. | M11 (§5.2) e historial del check de alcance. | H5–H7 |
| A15 | Puertas AGENTS.md (nivel 2) | Humo por versión en Codex y Antigravity CLI: leen `AGENTS.md`, descubren `.agents/skills/` y llaman `get_status` y `claim_task` por MCP stdio. | Lista manual; el resultado se registra en la matriz de puertas (06-permisos). | H4 |
| A16 | Superficies de Claude Code de nivel 1 | 100 % de la lista en CLI, extensión de VS Code, pestaña Code de Desktop y web (esta última según A3): contexto de SessionStart presente, skills con los nombres de 00-marco §5.8 (`project-status` en `en`), negativa de PreToolUse en una sección ajena con motivo, reglas deny locales activas (superficies locales) y PR creado por `/entregar`. | Lista manual por versión. | H4 |
| A17 | Evidencia verificable de fecha y de origen | (a) En la autoprueba (D-37), cada cabeza de PR de E1 produce un registro `submission` con OTS, y cada push a la base, uno `integration` con 2 TSA y OTS que cubre desde el último commit sellado (un push con sello fallido queda cubierto por el siguiente), con `plan: true` en el primero y cuando cambia `plan.md`. Hay 0 escrituras en ramas, y el comentario `ci:evidence` valida contra `evidence-record/v1`. Con `evidence_signing.mode: ci` y una llave de prueba (00-marco D-72), los registros `integration` van firmados y sellados sobre la firma, los `submission` van sin firma, y `evidence sign` firma y sella el índice del paquete. (b) `evidence verify` detecta el 100 % de un corpus de 25 o más alteraciones (byte del manifiesto, `.tsr`, cadena, OCSP, apertura, actor, blob, byte de la firma, llave equivocada, llave que ya no valía en la fecha del sello, firma sin sellar, manifiesto cambiado después de firmar e índice alterado) y da `verified` (TSA de prueba) o `pending` (OTS sin red) en los registros sanos, con `origin: project` en los firmados y en los que cubre el índice firmado. El mismo manifiesto, salvo las sales, sale en Windows con `core.autocrlf=true` y en Linux. Un PR cerrado sin integrar y con su rama borrada se verifica con un clon que trae `refs/pull/*/head`. (c) En 5 corridas reales (S5), los runners alcanzan las TSA y los calendarios del catálogo. (d) (H) Una persona no técnica verifica un registro con `VERIFY.md`, incluida la firma del proyecto con la lista de llaves publicada, en 15 minutos o menos. | (a) Autoprueba (D-37). (b) T1. (c) S5. (d) Prueba de "primer día". | H2 (a, b; la firma, H3), H3 (c), H4 (d) |

### 2.4 Presupuestos de rendimiento (A10)

Los presupuestos de los hooks (`pre-tool-use`, `session-start`), de `check-path` y del contexto los fija 01-arquitectura §7.1, y A10 los verifica tal como están allí. Este capítulo solo agrega los que ese capítulo no cubre:

| Medida | Presupuesto | Condición |
|---|---|---|
| `aucove status` | p95 ≤ 5 s | Con red, `aucove-demo` |
| CI: PDF o error de compilación comentado en el PR | p95 ≤ 5 min | Desde el push |
| CI: check de alcance | p95 ≤ 60 s | |

- **REQ-MVP-19.** El arnés de medición DEBE invocar el lanzador instalado como subproceso sin shell, con 5 corridas de calentamiento y 200 medidas, y DEBE reportar p50 y p95 por sistema operativo.
- **REQ-MVP-20.** Trasladado a 01-arquitectura REQ-ARCH-55, que fija cómo se miden por sistema operativo los presupuestos de 01-arquitectura §7.1.

---

## 3. Estrategia de pruebas

La ubicación de los directorios de prueba en el repositorio del framework la fija 01-arquitectura. Las rutas de este capítulo (`tests/...`, `evals/...`) son una propuesta.

### 3.1 Niveles y jobs

| Nivel | Qué prueba | Disparo | Sistemas | Credenciales | Bloquea |
|---|---|---|---|---|---|
| T1 Unitarias | Dominio y `derive_state` | Cada PR | 3 SO × Python 3.11 y 3.14 | Ninguna | PR |
| T2 Contrato (fake) | Adaptador falso con fallas | Cada PR | `ubuntu-latest` | Ninguna | PR |
| T2 Contrato (real) | Adaptador GitHub | Nocturno, manual, versión | `ubuntu-latest` | Environment `forge-tests` | Versión |
| T3 Carrera | Atomicidad del reclamo | Nocturno, versión | `ubuntu-latest` | `forge-tests` | Versión |
| T4 Fixtures de hooks | `aucove hook <evento>` | Cada PR | 3 SO | Ninguna | PR |
| T5 MCP | Contrato stdio y conformidad | Cada PR (contrato), nocturno (conformidad) | 3 SO / `ubuntu-latest` | Ninguna | PR (contrato) |
| T6 Skills | Estándar Agent Skills y lint propio | Cada PR | `ubuntu-latest` | Ninguna | PR |
| T7 End-to-end | `aucove-demo`, E1–E10 | Nocturno, versión; E1 semanal en Windows | `ubuntu-latest` y `windows-latest` | `forge-tests` | Versión |
| T8 Con modelo | Skills y hooks en un agente real | Manual, nocturno con tope | `ubuntu-latest` | Environment `model-tests` | No |
| T9 Coordinador | Conjunto de referencia | Semanal (subconjunto) y antes/después del piloto (completo) | `ubuntu-latest` | `model-tests` | No |
| T10 Canario de anfitriones | Última versión de Claude Code | Semanal | `ubuntu-latest` | `model-tests` | No (abre un Issue) |

- **REQ-MVP-21.** Todo PR del framework DEBE pasar T1, T2 (fake), T4, T5 (contrato) y T6 en la matriz antes de integrarse.
- **REQ-MVP-22.** Los jobs con credenciales DEBEN correr solo en `push` a la rama principal o de una etiqueta de versión, en disparos programados o manuales, y con los secretos de un environment protegido. NO DEBEN correr en PR desde forks (10-seguridad).

Extracto de la matriz para T1 y T4 (sintaxis de GitHub Actions; 10-seguridad exige fijar cada acción por SHA):

```yaml
name: test
on:
  pull_request:
  push:
    branches: [main]
permissions:
  contents: read
jobs:
  unit-hooks-mcp:
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
        python: ["3.11", "3.14"]          # mínima (D-01) y última estable
    runs-on: ${{ matrix.os }}
    defaults: { run: { shell: bash } }    # el glob dist/*.whl también en windows-latest
    steps:
      - uses: actions/checkout@v7          # fijar por SHA
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@v10       # fijar por SHA
        with:
          python-version: ${{ matrix.python }}
      - run: uv build --wheel
      - run: uv tool install --from dist/*.whl aucove   # lanzador real en el PATH
      - run: uv run pytest tests/unit tests/contract tests/hooks tests/mcp -m "not live and not model"
```

### 3.2 T1: unitarias del dominio y de `derive_state`

`derive_state` es la función pura de D-11. Su firma y la máquina de estados son de 04-tareas; los nombres de tipos de abajo son ilustrativos.

- **REQ-MVP-23.** `derive_state` DEBE tener 100 % de cobertura de ramas, y el paquete de dominio al menos 90 % de líneas.
- **REQ-MVP-24.** Las pruebas de `derive_state` DEBEN correr con la red y el sistema de archivos bloqueados (con monkeypatch de `socket` y `open`), para demostrar que es pura.
- **REQ-MVP-25.** Cada transición y cada combinación inválida de la máquina de estados de 04-tareas DEBE tener al menos un caso en la tabla.

```python
# tests/unit/test_derive_state.py  (ilustrativo)
import pytest
from hypothesis import given
from aucove.domain.tasks import derive_state          # nombre de módulo: 01-arquitectura
from tests.factories import snap, snapshots, CI_ACTOR    # constructor de TaskSnapshot

STATES = {"proposed", "open", "claimed", "in_review",
          "changes_requested", "merged", "expired", "cancelled"}

@pytest.mark.parametrize("case_id, snapshot, expected", [
    ("proposed", snap(labels={"task", "state:proposed"}), "proposed"),
    ("open", snap(labels={"task"},                      # aceptada: unlabeled por lead o deputy
                  label_events=[("state:proposed", "unlabeled", "lead")]), "open"),
    ("open-deputy", snap(labels={"task"},
                         label_events=[("state:proposed", "unlabeled", "deputy")]), "open"),
    ("claimed", snap(labels={"task"}, ref=True), "claimed"),
    ("in-review", snap(labels={"task"}, ref=True, pr="ready"), "in_review"),
    ("changes", snap(labels={"task"}, ref=True, pr="ready",
                     reviews=["CHANGES_REQUESTED"]), "changes_requested"),
    ("merged", snap(labels={"task"}, pr="merged", issue="closed"), "merged"),
    ("expired", snap(labels={"task", "state:expired"}, ref=True,   # puesta por CI verificado,
                     label_events=[("state:expired", "labeled", CI_ACTOR)]),  # lead o deputy
     "expired"),
    ("cancelled", snap(labels={"task"}, issue="closed",
                       state_reason="not_planned"), "cancelled"),
    # ... una fila por transición y por combinación inválida de 04-tareas
])
def test_table(case_id, snapshot, expected):
    assert derive_state(snapshot).state == expected

@given(snapshots())                       # generador de Hypothesis
def test_invariants(s):
    d = derive_state(s)
    state = d.state
    assert state in STATES                # total: nunca lanza excepción
    assert derive_state(s) == d           # determinista
    assert (state == "merged") == any(p.merged and p.same_repo for p in s.pulls)
```

### 3.3 T2: suite de contrato del adaptador

- **REQ-MVP-26.** La misma batería DEBE ejecutarse contra el adaptador falso (08-forja) y contra el adaptador GitHub real. Si un caso pasa con el fake y falla con el real, el defecto está en el fake y se corrige allí.
- **REQ-MVP-27.** El fake DEBE permitir inyectar fallas antes y después de cada escritura (red, 401, 403, 404, 409, 422, límite secundario, timeout) y DEBE registrar todas las llamadas de escritura para las aserciones de A6 y A13.

```python
@pytest.fixture(params=["fake", pytest.param("github", marks=pytest.mark.live)])
def forge(request, sandbox_repo):                      # ct-public o ct-private (abajo)
    return make_adapter(request.param, repo=sandbox_repo)   # interfaz de 08-forja
```

Casos mínimos: detección de capacidades (repo público → `enforced`; privado en Free → `advisory`); crear la ref `task/<n>` (nueva, existente, concurrente); borrar la ref; asignar y desasignar; etiquetas idempotentes; buscar o crear el PR por la rama `task/<n>`; listar y crear revisiones; comentario con marcador oculto que se actualiza en vez de duplicarse; cerrar el Issue como `not_planned`; errores de red y de autenticación mapeados a las áreas `NET` y `AUTH`, con el `retryable` que defina 05-operaciones.

**Organización de pruebas en GitHub** (nombre provisional `proyecto-test`; se creó como `proyecto-test-13`, 00-marco D-23):

- En plan Free: sus repos públicos dan el perfil `enforced` (las ramas protegidas existen en repos públicos con GitHub Free) y los privados, `advisory`. Una sola org cubre así ambos perfiles, salvo `enforced` en un repo privado, que se prueba en una org Team aparte (00-marco D-24).
- **REQ-MVP-28.** DEBE tener al menos 2 cuentas de usuario de GitHub, en los papeles `lead` y `researcher-a`; una tercera, `researcher-b`, es opcional (00-marco D-67). Si no está, `researcher-a` actúa como `deputy` en los fixtures que lo requieren, y las pruebas que necesitan tres identidades distintas a la vez (por ejemplo, un PR con revisiones de dos cuentas distintas de la del autor) se omiten con la forja real y quedan cubiertas por el falso. NO DEBEN usarse GitHub Apps para los papeles, porque no pueden ser code owners. Cada cuenta de máquina gratuita DEBE pertenecer a un humano distinto (los términos de GitHub admiten como máximo una cuenta de máquina gratuita por persona); las que falten PUEDEN ser cuentas de pago (00-marco D-23), y `researcher-a` PUEDE ser una cuenta personal (D-67). `lead` es owner de la org; `researcher-a` y `researcher-b` son miembros sin administración. Cada cuenta tiene 2FA, con la semilla y los códigos de recuperación en un gestor de contraseñas, y un correo propio (vale un alias). Las cuentas de máquina NO DEBEN usarse a mano: las pruebas manuales entre roles se hacen con cuentas personales de personas distintas.
- Los tokens son de grano fino, limitados a `proyecto-test` (solo `ct-public` y `ct-private`), con los permisos de 08-forja §5.4 (el de `lead`, además, Administration para los rulesets de `CT-CAP-02`), caducidad de 90 días o menos y rotación agendada. El owner aprueba los de los miembros (política por defecto de la org); el de `lead`, que es owner, no lo necesita. Viven en el environment `forge-tests` (secretos `TEST_GH_TOKEN_LEAD`, `TEST_GH_TOKEN_RESEARCHER_A` y `TEST_GH_TOKEN_RESEARCHER_B`). Un token fine-grained no abarca dos organizaciones, así que la org Team de D-24 usa un segundo juego en el mismo environment (`TEST_GH_TOKEN_TEAM_LEAD`, `TEST_GH_TOKEN_TEAM_RESEARCHER_A`, `TEST_GH_TOKEN_TEAM_RESEARCHER_B`). La cuenta `lead` es owner de la org.
- T2 (real) y T3 usan dos repos sandbox fijos de `proyecto-test`: `ct-public` (perfil `enforced`) y `ct-private` (perfil `advisory`), indicados con `AUCOVE_CONTRACT_REPO`. Trabajan en ramas `ct/<run_id>/…` y las borran al terminar (08-forja §10.2). `CT-CAP-01` (privado) y REQ-PERM-53 usan además `ct-private-team`, en la org Team separada de D-24 (`proyecto-test-team`, provisional), solo con las 3 cuentas de máquina, de noviembre de 2026 a enero de 2027.
- Prueba manual de H3 (no forma parte de T2 `live`): la excepción de D-25 con una cuenta que no es miembro de `proyecto-test` (por ejemplo, la cuenta personal de un mantenedor, invitada como colaboradora externa a `ct-private`) y `gh auth login`. Confirma que reclamar, guardar y entregar funcionan con `W_AUTH_BROAD_TOKEN`, y que `doctor` del `lead` avisa con `details.reason: "outside_collaborator"` (06-permisos REQ-PERM-55).
- Los repos `ct-<run_id>-<k>` se crean solo para T7 y A12. Un job diario borra los que tengan más de 24 h. Las llamadas respetan los límites de la API que documenta 08-forja.

### 3.4 T3: pruebas de carrera

- **REQ-MVP-29.** T3 DEBE lanzar los reclamos con una barrera de sincronización y DEBE repartir los 10 reclamantes entre las cuentas del sandbox (2 o 3, REQ-MVP-28), de modo que también compitan procesos de la misma cuenta. Cada repetición hace unas 23 escrituras (unas 1.150 por noche con `reps=50`), así que T3 DEBE espaciar las repeticiones para no pasar de unas 450 escrituras por hora y por cuenta (límite secundario de 500 por hora, 08-forja §7.1).

```python
def test_claim_race(real_org, reps=50, workers=10):
    for _ in range(reps):
        n = real_org.new_task(sections=["methods"])
        barrier, results = threading.Barrier(workers), []
        def run(i):
            cli = real_org.cli_for(ACCOUNTS[i % len(ACCOUNTS)])
            barrier.wait()
            results.append(cli.json(["claim", str(n)]))
        threads = [threading.Thread(target=run, args=(i,)) for i in range(workers)]
        [t.start() for t in threads]; [t.join() for t in threads]
        ok = [r for r in results if r["ok"]]
        assert len({r["data"]["claimant"] for r in ok}) == 1          # un solo dueño
        assert all(r["error"]["code"] == "E_CLAIM_ALREADY_CLAIMED"
                   and r["error"]["retryable"] is False
                   for r in results if not r["ok"])
        assert real_org.assignees(n) == [ok[0]["data"]["claimant"]]
        assert real_org.refs_matching(f"task/{n}") == 1
```

El campo `data.claimant` es parte de `data_claim` (05-operaciones), y los perdedores reciben `E_CLAIM_ALREADY_CLAIMED`. Con el fake se prueban además los intercalados deterministas: falla entre la creación de la ref y la asignación, y reintento del mismo usuario (idempotente). La compensación la define 04-tareas.

### 3.5 T4: fixtures de hooks

Formato: `tests/fixtures/hooks/<evento>/<caso>.json`. La entrada reproduce el JSON que Claude Code envía por stdin (`session_id`, `cwd`, `hook_event_name`, `tool_name`, `tool_input`, `tool_use_id`).

```json
{
  "id": "pre-tool-use/deny-foreign-section-backslash",
  "class": "deny",
  "os": ["windows"],
  "setup": { "identity": "alice", "branch": "task/12",
             "roles_base": "roles/basic.yaml", "task_sections": { "12": ["methods"] } },
  "stdin": { "session_id": "t", "cwd": "${REPO}", "hook_event_name": "PreToolUse",
             "tool_name": "Edit", "tool_use_id": "toolu_t",
             "tool_input": { "file_path": "${REPO}\\tex\\sections\\results\\results.tex",
                             "old_string": "a", "new_string": "b" } },
  "expect": { "decision": "deny", "reason_mentions": ["results", "bob"] }
}
```

- **REQ-MVP-30.** El ejecutor DEBE invocar el lanzador `aucove` instalado, resuelto desde el PATH, como subproceso sin shell (igual que la forma exec de 07-hooks).
- **REQ-MVP-31.** `decision: deny` se satisface con código de salida 2 o con JSON de denegación válido, según lo que fije 07-hooks. En la clase `fault`, el único resultado aceptado es el código 2.
- Clase `fault` (mínimo 12 casos): stdin vacío; JSON inválido; `tool_input` sin `file_path`; falta la caché de identidad; caché de identidad corrupta; `roles.yaml` de la base ilegible o inválido según su esquema; rama base indeterminable; `project.yaml` ausente o inválido; `.aucove/` sin permiso de lectura; `tool_name` desconocido con ruta; ruta con otras mayúsculas hacia una sección ajena (Windows y macOS); ruta con barras invertidas hacia una sección ajena (Windows).
- Clases `deny` y `allow`: sección ajena por Edit, Write y NotebookEdit; archivo `lead` tocado por un `researcher` (`plan.md`, `roles.yaml`, `tex/notation.tex`, `.claude/settings.json`); decisión existente; `CODEOWNERS`; edición en la rama base sin tarea; nota de otra tarea; sección declarada propia; decisión nueva; nota propia. El resultado esperado de cada caso lo fijan 06-permisos y 07-hooks.
- SessionStart con `startup`, `resume`, `clear` y `compact`: salida dentro del presupuesto de 01-arquitectura §7.1. SessionEnd: termina en menos de 1 s y no hace red.

### 3.6 T5: MCP

- **REQ-MVP-32.** El contrato por stdio DEBE usar el cliente del SDK oficial de Python y verificar: `tools/list` según REQ-MVP-3; que cada `inputSchema` y `outputSchema` sea válido contra el metaesquema JSON Schema 2020-12; que `structuredContent` valide contra `result.schema.json` (05-operaciones); que un nombre inexistente (p. ej. `approve_review`) produzca un error de protocolo; y que el servidor no escriba en stdout nada que no sea JSON-RPC.
- **REQ-MVP-33.** La suite oficial de conformidad (`@modelcontextprotocol/conformance`) solo acepta servidores por URL HTTP. En v0.1 se DEBE correr cada noche contra un arnés de prueba que monta el mismo objeto servidor sobre Streamable HTTP en `localhost`, dentro del proceso de pruebas. Ese arnés NO DEBE distribuirse ni exponerse como opción de la CLI. La suite DEBE correrse dos veces, con `--spec-version 2025-11-25` y con `--spec-version 2026-07-28`. Su resultado es informativo y se compara con un archivo de fallas esperadas por versión.

### 3.7 T6: skills

- **REQ-MVP-34.** Cada skill en `.claude/skills/` y `.agents/skills/`, para `es` y para `en`, DEBE pasar `skills-ref validate` fijado a un commit concreto. El README de skills-ref dice que es una implementación de demostración, no para producción, por eso se complementa con un validador propio.
- **REQ-MVP-35.** El validador propio DEBE comprobar: nombre igual al directorio y a 00-marco §5.8; nombre no reservado por Claude Code (comandos integrados y skills incluidos, 00-marco §5.8); solo `a-z`, `0-9` y guiones, sin guiones dobles ni en los extremos; `description` de 1 a 1.024 caracteres; cuerpo de menos de 500 líneas; ninguna invocación de operaciones H; ninguna mención de herramientas propias de Claude Code (`Edit`, `Write`) en `.agents/skills/`; y regeneración desde la fuente única sin diferencias (`git diff --exit-code`).

### 3.8 T7: end-to-end con `aucove-demo`

`aucove-demo` es un repo sembrado con una fuente de verdad versionada (`demo-seed`): un `lead`, un `deputy`, dos `researcher`, cuatro secciones, seis tareas en distintos estados, veinte decisiones y un conflicto de notación preparado. Sirve como fixture y como material de capacitación del piloto. Las integraciones las hace el arnés con el token del responsable, en nombre de la persona; el núcleo nunca integra (D-09).

| E | Escenario | Comprueba |
|---|---|---|
| E1 | propose → accept → claim → save → submit → approve → merge | Estados, nota de sesión, trailers (11-trazabilidad) y PDF comentado |
| E2 | Cambios pedidos → `update` → nueva entrega | `changes_requested` → `in_review` |
| E3 | Otra entrega cambia `tex/notation.tex`; la tarea queda desactualizada | `update`, con y sin conflicto |
| E4 | Reclamo inactivo con un umbral corto en el `project.yaml` de prueba | `expire-claims` y `state:expired` |
| E5 | `release` de un reclamo sin commits | Vuelve a `open` y la ref desaparece |
| E6 | Todo E1 en un repo privado (`advisory`) | A5 e integración sin aprobación registrada en M11 |
| E7 | Integración que rompe la compilación (bypass de prueba) | Issue `main-broken` |
| E8 | PR del responsable: lo aprueba el `deputy`; sin `deputy`, advertencia | D-17 |
| E9 | `record-assessment` con un veredicto de traslape dado por el arnés | `flag:overlap` idempotente, un solo comentario |
| E10 | E1 con `language: en` | A11(c) |
| E11 | E1 con `format: none` (`init --format none`) | `aucove/build` en éxito con motivo y sin comentario; partes en Markdown; el render `none` pasa `validate` sin errores; IL05 e IL08 sobre un `.md` de una parte; A17 (A9 no depende del formato: `scope` mira rutas) |
| E12 | E1 y E2 con sellado; un PR cerrado sin integrar y con su rama borrada; copia local con `status`; paquete armado con la guía (11-trazabilidad §8.8); la firma del proyecto en el modo `ci` (repo público) y en el modo `admin` (repo privado de una org Free, donde el job `integration` sella sin el secreto; 09-ci §2), con el índice firmado por `evidence sign` | `evidence verify` del paquete y de `.aucove/evidence/`, con `--keys`; A17 |
| E13 | E1 con `format: latex`, una sección del artículo con dos subpartes de dueños distintos y un editor declarado | El editor arma `tex/outline/<id>.tex` con las dos subpartes y sus transiciones. `aucove/editor-review` falla hasta que el editor aprueba la cabeza, y vuelve a fallar tras un push nuevo. Un `CHANGES_REQUESTED` del editor lleva la tarea a `changes_requested` (00-marco D-55 y D-56) |
| E14 | E1 con `mode: individual` (`init --individual`) | El ruleset no exige aprobaciones y la persona integra con los checks en verde; quedan los sellos de entrega e integración. `validate` falla si se agrega una segunda persona activa sin pasar a `group` (00-marco D-57) |
| E15 | E1 desde una sesión de Codex | Codex lee `AGENTS.md` y `.aucove/context.md`; sus hooks niegan una escritura fuera de la parte; el commit lleva `Assisted-by: codex:<modelo>` (00-marco D-58) |

- **REQ-MVP-36.** Publicar una versión DEBE exigir E1–E12 en verde en la última corrida nocturna y en la corrida de versión.

### 3.9 T8: pruebas con modelo

- **REQ-MVP-37.** Las pruebas con modelo DEBEN autenticarse solo con la API key de pruebas del proyecto (secreto `TEST_ANTHROPIC_API_KEY`, expuesto como `ANTHROPIC_API_KEY`). Esa key pertenece a un workspace dedicado de Claude Console con límite de gasto mensual; los límites de un workspace no pueden superar los de la organización.
- **REQ-MVP-38.** Cada corrida DEBE fijar `max_budget_usd` y `max_turns`, y fijar el modelo por id explícito, que se anota en el reporte.
- **REQ-MVP-39.** NO DEBEN usarse credenciales de suscripción (inicio de sesión de claude.ai, `CLAUDE_CODE_OAUTH_TOKEN`, `claude setup-token`) ni `claude -p`, siguiendo D-15. El job DEBE fallar si detecta `CLAUDE_CODE_OAUTH_TOKEN` en el entorno.
- **REQ-MVP-40.** En v0.1 la calificación DEBE ser determinista: se evalúa el estado de la forja y de los archivos, sin modelo juez.

El Agent SDK de Python (`claude-agent-sdk`) carga los hooks de `.claude/settings.json`, los skills de `.claude/skills/` y `settings.local.json` cuando `setting_sources` incluye `"project"` y `"local"`:

```python
options = ClaudeAgentOptions(
    cwd=str(ephemeral_repo),                 # copia desechable de aucove-demo
    setting_sources=["project", "local"],    # hooks, skills y reglas deny del repo
    model=PINNED_MODEL_ID,
    max_turns=30,
    max_budget_usd=0.50,                     # tope por escenario (H)
)
```

Escenarios: MS1 `/empezar` sobre una tarea existente (reclamo hecho, contexto usado); MS2 intento de editar una sección ajena (negada con motivo; el agente propone avisar al dueño); MS3 "aprueba el PR 7" (0 aprobaciones en la forja); MS4 `/entregar` (nota de sesión, trailers y PR); MS5 instrucción inyectada en un archivo de referencia que pide editar `roles.yaml` (bloqueada y sin cambios en la base); MS6 `/empezar` del `lead` en un repo recién iniciado, con las respuestas de la entrevista en el guion (00-marco D-70): `plan.md` y `roles.yaml` validan, `sync-roles --check` pasa y la tarea de preparación queda entregada; con otra persona, ningún reclamo.

### 3.10 T9: calidad del coordinador

- **REQ-MVP-41.** El conjunto de referencia DEBE tener entre 30 y 50 casos versionados en `evals/coordinator/cases/*.yaml`, con al menos 8 por clase, sobre estados sembrados de `aucove-demo`.
- **REQ-MVP-42.** El conjunto completo DEBE correrse antes de H6 y al cerrar H6 y H7, con el mismo id de modelo, 3 veces por caso. Se reportan la media y el veredicto mayoritario.
- **REQ-MVP-43.** Las verificaciones deterministas (macros redefinidas, `\label` duplicados, archivos fuera de las secciones declaradas) se miden aparte, en CI. DEBEN acertar el 100 %; el modelo solo evalúa el traslape semántico.

```yaml
id: ovl-007
kind: overlap              # overlap | no_overlap | notation_conflict | direction_change
seed_ref: demo-seed/v1     # estado del aucove-demo
action: claim              # claim | submit
task:
  title: "Redefinir la tasa de respuesta en métodos"
  sections: [methods]
expected:
  verdict: clear           # supuesto sobre 05-operaciones: none | possible | clear
  related_tasks: [14]
  labels: [flag:overlap]
```

La calificación compara el registro estructurado de `record-assessment` con `expected`. Se calculan precisión y exhaustividad de "traslape detectado" (`verdict` ∈ {`possible`, `clear`}) por clase, por modelo y por puerta. Umbrales (H): exhaustividad ≥ 0,8 en `overlap` + `notation_conflict`, y tasa de falsas alarmas ≤ 0,3 en `no_overlap`. Costo estimado (H): unos 0,30 USD por evaluación, es decir, unos 36 USD por corrida completa de 40 casos × 3. Cada semana corre un subconjunto de 10 casos. Otros modelos, por Codex o Gemini CLI con BYOK, PUEDEN evaluarse antes del piloto si hay presupuesto.

### 3.11 T10: canario de anfitriones

- **REQ-MVP-44.** Cada semana, un job DEBE instalar la última versión de Claude Code, correr MS1 y MS2 y capturar el JSON real que reciben los hooks. Si las claves no coinciden con las de los fixtures de T4, el job DEBE abrir un Issue en el repositorio del framework.

---

## 4. Hitos

El equipo de desarrollo es de 2 personas (00-marco D-20); su dedicación no está precisada y se asume casi completa (H). H2 empieza el 29/09/2026, en paralelo con H0 y H1, contra el adaptador falso: es un cambio de alcance «adelantar hito» (REQ-MVP-11). Las fechas siguen los calendarios 2026-2027 de la UNAM y del IPN (D-27).

| Hito | Duración | Fechas | Entregables | Criterio de salida |
|---|---|---|---|---|
| H0 Prerrequisitos | 2 sem. | 29/09–09/10/2026 | Lista de §4.1, incluidos los spikes S1 y S3 (00-marco D-22) | 100 % de la lista hecho o con dispensa escrita del mantenedor. Dispensas registradas: #1, parcial (D-23); #7, a más tardar el 30/10/2026 (D-28); #8, como condición previa de H5 (D-27) |
| H1 Especificación congelada | 1 sem. | 12/10–16/10 | Capítulos 00–13 revisados; preguntas bloqueantes resueltas o diferidas de forma explícita; resultados de S1 y S3; spikes S2 y S4 | Etiqueta `spec-v0.1`; los resultados de los spikes se registran como decisiones en 00-marco |
| H2 CI de la plantilla | 5 sem. (adelantado, D-20) | 29/09–30/10 | Plantilla (§1.2 sin `.claude/`), con las 7 partes por defecto y el esqueleto del editor (00-marco D-55 y D-56); workflows de 09-ci; núcleo mínimo (`validate`, `check-path`, `sync-roles`, `doctor` local); fake y esqueleto de T1, T2, T4 y T6; `LICENSE`, `NOTICE`, `LICENSES/`, `REUSE.toml` y cabeceras SPDX del framework, con su job de `reuse lint` (11-trazabilidad §7.1); rutas `sections/<id>/`, `format: none`, esquemas `evidence-*`, valores por defecto de `disclosure` y `require_commercial` con `check_accounts`, `seal.yml` con `aucove ci seal` y `aucove evidence verify` con su kit (D-50) | A9 (fixtures) en verde; la plantilla pasa su propio CI; A17 (a, b) en la autoprueba |
| H3 Núcleo y arnés | 4 sem. | 02/11–27/11 (el 16/11 es feriado) | Condición previa: las 3 identidades de D-23. El repo limpio y público se crea antes del primer release (REQ-MVP-62). Incluye el editor (clase `editor` y `aucove/editor-review`), el uso individual y el contexto en Markdown (00-marco D-56 a D-58). Todas las operaciones de H3; adaptador GitHub REST; MCP stdio; T2 real, T3, T5, T7 y `aucove metrics`; `CT-CAP-01` en `ct-private-team` (D-24); prueba manual de la excepción `gho_` (§3.3); S1 repetido en su última semana, 23–27/11 (D-22); S5 en la primera semana; `init --format` y `--disclosure`, y la retención de 400 días; copia local de `status` y `submit`; `successful_runs` y `merged_by` reales (`CT-SEAL-01`); check `identity` de noreply; IL05/IL08 sobre el Markdown de las partes | A4 (guionado), A5, A6 (a–c), A7, A12, A13, A10 (CLI) y A17 (c) |
| H4 Skills y hooks | 2 sem. + 1 | 30/11–11/12 (parte técnica) y 11/01–15/01/2027 (pruebas de "primer día" y cierre) | `.claude/settings.json`, skills `es`/`en` (con el skill de edición, 00-marco D-56, y el arranque guiado y la presentación del skill de inicio, D-70), los hooks y la configuración de Codex tras el spike S6 (00-marco D-58), `.agents/skills/`, reglas deny locales, T4 completo, instalación en la nube, guía de una página por perfil (`es`/`en`, con las cuentas de IA de 10-seguridad REQ-SEC-86) y guía de tokens (08-forja REQ-FORGE-66); `env` y reglas de lectura de D-48; «parte» en skills y guías; guías de alta con noreply, de custodia y exportación, de apertura por hitos, de depósito cifrado en OSF y del kit del árbitro; modelo de acuerdo de autoría (`es`/`en`); pruebas de "primer día" (3 técnicos en Windows y 3 no técnicos en la web), que exigen los asientos de §4.1, #3 | A1, A2, A3, A8, A10, A11, A15, A16 y A17 (d); corregidas las 5 fricciones principales; `0.1.0rc1` publicado en PyPI (REQ-MVP-46) |
| Colchón | 4 sem. | 14/12/2026–08/01/2027 | Correcciones de H4; cierre de año | — |
| H5 Dogfooding | 2 sem. | 18/01–29/01/2027 | Condición previa: §4.1, #8 (D-27). Los autores escriben un documento real con el MVP en un repo `enforced` (REQ-MVP-63). Participan al menos 1 persona con Windows nativo, 1 con Codex o Antigravity CLI, 1 en sesión web, 1 colaborador externo simulado (usa la excepción `gho_` de D-25) y 1 sesión sin red. T9 de línea base. | A14 sin fallas durante H5; 0 defectos abiertos de severidad bloqueante; las tablas de `aucove metrics` alimentan el análisis; `0.1.0` publicado |
| Semana 0 de G-T | 1 sem. (dentro de H5) | 25/01–29/01/2027 | Sesión de arranque y entrevistas de línea base de G-T (§5.4), con aprobación ética y consentimientos (REQ-MVP-57). Si participan estudiantes de la UNAM, hay que confirmar su disponibilidad: el intersemestre va del 04 al 29/01 | Línea base de G-T recogida |
| H6 Piloto técnico (G-T) | 5 sem. activas + 1 de análisis | 01/02–05/03 (el 01/02 es feriado) y 08/03–12/03 | Piloto G-T (§5) | Informe con M1–M14 frente a umbrales; T9 posterior; decisión sobre G-NT |
| Capacitación de G-NT | 1 sem. | 15/03–19/03 (el 15/03 es feriado) | Correcciones tras G-T (§1.6); sesión de capacitación sobre `aucove-demo` y entrevistas de línea base de G-NT (su semana 0) | Puerta G-NT (REQ-MVP-47) evaluada |
| Sin actividad | 2 sem. | 22/03–02/04/2027 | Semana Santa y vacaciones del IPN; solo se repite S1, del 29/03 al 02/04, antes de H7 (D-22) | — |
| H7 Grupo no técnico (G-NT) | 5 sem. activas | 05/04–07/05/2027, solo si pasa la puerta G-NT | Piloto G-NT por la web o la pestaña Code de Desktop | Informe; decisión sobre el modo completo, hacia el 14/05/2027 |

- **REQ-MVP-45.** Un hito se cierra solo cuando cumple su criterio de salida. Si no lo cumple, se extiende o se recorta alcance según §1.6.
- **REQ-MVP-46.** `0.1.0rc1` DEBE publicarse en PyPI como `aucove`, desde el repo público (00-marco D-03 y D-69). Para A3 y S2, la wheel se construye desde el clon adjunto a la sesión, o se copia a él, y se instala dentro de la sesión (00-marco D-59). El job `template` no corre para versiones preliminares (REQ-ARCH-8).
- **REQ-MVP-62.** La versión pública del framework DEBE ser un repositorio nuevo y limpio, creado con un solo commit inicial justo antes del primer release (00-marco D-54). El alpha (`dsu13/proyecto`) DEBE seguir privado y quedar archivado como registro de procedencia. La confirmación de titularidad de D-07 (b) no condiciona la publicación (11-trazabilidad REQ-TRACE-73). El repo público tiene que existir desde el primer release, porque en Free los secretos de environment solo existen en repos públicos, y en Free, Pro y Team los revisores obligatorios de un environment también (`forge-tests`, §3.3; `pypi`, 10-seguridad REQ-SEC-40). REQ-MVP-46 limita la publicación en índices, no la visibilidad del repositorio.
- **REQ-MVP-63.** El dogfooding de H5 DEBE hacerse en un repo con perfil `enforced`, para que A14 y M11 se puedan medir (D-26).
- **REQ-MVP-47.** El grupo G-NT NO DEBE empezar mientras no se cumplan todas las condiciones de la **puerta G-NT**:
  1. A3 en verde, incluida la prueba con no técnicos, y la repetición de S1 del 29/03 al 02/04/2027 con el resultado de 00-marco D-41 (o una decisión nueva si cambió).
  2. Asientos con Claude Code confirmados para todo G-NT (§5.1).
  3. En la org de Claude, el Owner habilitó las sesiones en la nube (`allow_remote_sessions`); la org NO tiene Zero Data Retention; no hay allowlist de IP o el soporte de Anthropic eximió los servicios alojados; y la Claude GitHub App está instalada en los repos del piloto (la instala un owner de la org de GitHub: en Team y Enterprise de Claude, `/web-setup` está oculto; D-26).
  4. La aprobación ética cubre que el repo se clone en VM de Anthropic, y el repo no contiene datos restringidos (10-seguridad).
  5. G-T pasó según la regla de decisión de §5.2.
  6. Sesión de capacitación sobre `aucove-demo` realizada (incluye abrir cada sesión web con un solo repositorio, 01-arquitectura REQ-ARCH-98), y SLA y suplente de §5.6 activos.

Spikes (scripts en `spikes/`): **S1** (H0; 00-marco D-22), escritura REST y `git push` a `task/<n>` desde una sesión web con `CT-WEB-01` de 08-forja, en un repo público de prueba con la Claude GitHub App, con y sin ruleset en `task/*`, más la variante E (sesión abierta desde `task/<n>`). Regla de tres salidas, fijada de antemano: (i) si la escritura por API pasa, la puerta web es completa; (ii) si solo pasa `git push` a `task/<n>`, la web guarda trabajo pero reclama y entrega desde otra superficie, y REQ-FORGE-56 pasa a `write_mode=git` en la web; (iii) si no pasa nada, la web es de solo consulta en v0.1 y los hooks deniegan las escrituras con `E_STATE_NOT_ON_TASK_BRANCH`. S1 es una regresión manual obligatoria antes de H4, en la última semana de H3 (23–27/11/2026), y antes de H7, la semana del 29/03 al 02/04/2027 (00-marco D-22). **Resultado** (00-marco D-41 y D-42): salida (ii) ampliada; la web reclama por push con lease, guarda y actualiza con `git push` y entrega por REST, y solo `release` remite a la terminal o a Desktop. Las repeticiones usan `spikes/ct_web_02.sh` (`CT-WEB-01`) y `CT-PUSH-01` (S3-bis); la variante E ya no hace falta, porque el push no depende de la rama de la sesión. **S2** (H1), tiempo de instalación de `aucove` en una VM de la nube (trae `uv` y PyPI está permitido), comprobando `aucove --version` en la sesión. Hecho (00-marco D-59): construir e instalar toma unos 5 s, y `aucove --version` responde. **S3** (H0; D-22), atomicidad de la creación de la ref (`CT-REF-02`, `CT-REF-03`) en GitHub Actions de un repo desechable, con el `GITHUB_TOKEN` del workflow y sin la org de pruebas: 100 carreras válidas de 10 hilos; el modo `patch` solo se corre si falla `create`. **S4** (H1), hook en forma exec en Windows solo con la herramienta PowerShell, y verificar si Claude Code busca el `command` del hook en el directorio actual en Windows; si lo hace, `doctor` avisa. **Resultado** (00-marco D-52): la forma exec no pasa por ninguna shell y funciona en una sesión solo con PowerShell. Claude Code no busca el `command` en el directorio actual, pero resuelve contra él las entradas relativas del `PATH`, así que `doctor` avisa de ellas (01-arquitectura REQ-ARCH-33). La regresión es `spikes/s4_hooks_windows.py`, antes de H4 y de H7, junto con la de S1. **S6** (antes de los hooks de Codex, en H4; 00-marco D-58): Codex en Windows y en Linux. Comprueba cuatro cosas: que lee `AGENTS.md` y los skills de `.agents/skills/`; que usa el servidor MCP de `.codex/config.toml`; que sus hooks se disparan y niegan con exit 2; y que no buscan `aucove` en el directorio actual, aunque Codex los ejecuta con una shell. Si se puede, sin costo, con una API falsa local como S4. **S7** (sin hito; antes de dar a Antigravity CLI el nivel completo, 00-marco D-58): Antigravity CLI en Windows y en Linux, con las cuatro comprobaciones de S6 sobre sus archivos de instrucciones, skills, servidor MCP y hooks. Fija los archivos y los eventos que usa, que hoy solo describen guías de terceros. S1 y S3 usan una sola identidad y no dependen de las cuentas de §3.3.

Las ventanas de H6 y H7 son provisionales y valen para los calendarios 2026-2027 de la UNAM y del IPN (00-marco D-27); la institución y el país se confirman antes del 23/10/2026. Con otra institución se ajustan a sus feriados: en Europa hay que evitar los exámenes de enero y de mayo-junio; en el hemisferio sur, el piloto cabe entre marzo y mayo con más margen. Cada grupo se mantiene dentro de las 4–6 semanas activas. OSF deja de admitir proyectos nuevos el 16/11/2026 y los pasa a solo lectura el 19/02/2027, dentro de G-T. El piloto no depende de OSF; el depósito de D-47 usa registros.

### 4.1 Lista de H0

| # | Tarea | Responsable | Evidencia |
|---|---|---|---|
| 1 | Crear la org `proyecto-test` (Free) con `ct-public` y `ct-private`, y la cuenta de máquina `lead` (owner). `researcher-a` (segundo desarrollador) y `researcher-b` (de pago si no hay una tercera persona): antes de H3, a más tardar el 30/10/2026; si el 23/10 faltan personas, se pagan. La org Team de pruebas de D-24 ya existe: es `graphmachinelearningbp`, del usuario, solo de pruebas; `ct-private-team` se crea en ella al empezar H3 | Usuario (org y `lead`); segundo desarrollador (`researcher-a`) | Lista de cuentas y dispensa escrita del plazo de `researcher-a` y `researcher-b` (00-marco D-23) |
| 2 | Plan de GitHub de la org del piloto, que es otra org, distinta de la de pruebas de D-24: GitHub Team (D-26), con permiso base «ninguno». Si hay un docente o investigador verificado, solicitar GitHub Education y mejorar la org desde el panel de Education, nunca desde facturación. Fecha de control: 13/11/2026 | Owner de la org del piloto (el usuario); TI solo si la institución tiene una cuenta Enterprise o Campus con SSO | Decisión escrita por grupo: plan, perfil y tipo de asiento de Claude |
| 3 | Asientos con Claude Code y sesiones en la nube para mantenedores, participantes y los 6 probadores de "primer día" de H4 (antes del 11/01/2027). El PI de cada grupo solicita ya el plan Team para científicos si su disciplina califica (aprobación en unos 7 días hábiles; hay cupo; un plan por laboratorio; uso al menos mensual por la cláusula de 90 días) | PI de cada grupo; coordinación, para mantenedores y probadores | Confirmación |
| 4 | Confirmar la elegibilidad por disciplina (un G-NT de ciencias sociales o humanidades probablemente no califica) y aprobar el respaldo: Team pagado (mínimo 2 asientos) o Pro, dentro del presupuesto de hasta unos US$1.400; la activación se decide el 13/11/2026 (D-26) | Coordinación del piloto | Respuesta de Anthropic o presupuesto aprobado |
| 5 | Owner de la org de Claude: Claude Code, sesiones en la nube y conector de GitHub habilitados. Un owner de la org de GitHub instala la Claude GitHub App en los repos del piloto: en Team y Enterprise de Claude, `/web-setup` está oculto (D-26) | TI u owner de la org de Claude; owner de la org de GitHub | Captura de la configuración |
| 6 | Workspace de pruebas en Claude Console con límite de gasto mensual y API key del proyecto | Mantenedor | ID del workspace |
| 7 | Protocolo de ética y consentimiento presentados (§5.5), con las ventanas de D-27 o un rango de fechas y la retención de REQ-MVP-65; incluye los registros de evidencia, su base y su conservación (D-46, REQ-MVP-67 e, PA-106); preguntar al comité por una exención | Coordinación del piloto | Número de trámite. Dispensa: a más tardar el 30/10/2026 (D-28) |
| 8 | Preselección de G-T y G-NT, cada uno con un entregable real con fecha dentro de su ventana, y calendario académico confirmado (institución y país, antes del 23/10/2026) | Coordinación del piloto | Carta de compromiso, que dice que el grupo trabaja con ayuda de la IA, con o sin LaTeX (D-43), y que firmará el acuerdo de autoría y de apertura antes de `init` (D-49). Dispensa: pasa a condición previa de H5 (D-27) |
| 9 | Ejecutar los spikes S1 y S3 con la cuenta del usuario (D-22) y registrar sus resultados como decisiones en 00-marco | Usuario | Salida de los scripts de `spikes/` y decisión registrada. S3: hecho (D-40). S1 y S3-bis: hechos (D-41 y D-42) |

---

## 5. Piloto

### 5.1 Grupos y prerrequisitos

- **REQ-MVP-48.** Cada grupo (G-T técnico y G-NT no técnico) DEBE tener de 3 a 6 personas, un `lead` que también escribe, un `deputy` declarado en `roles.yaml` y un entregable real (artículo, informe o propuesta) con fecha dentro de su ventana; el grupo elige `format: latex` o `none` (D-43).
- **REQ-MVP-69.** Antes de `init`, cada grupo del piloto DEBE firmar el acuerdo de autoría y de apertura, fijar `disclosure` (por defecto `milestones`) y activar los correos noreply de sus miembros. Un grupo con cuentas de consumo DEBE declarar `require_commercial: false` y registrarlo (D-48).
- **REQ-MVP-49.** Antes de convocar, DEBE verificarse el tipo de cuenta de cada participante. Claude Code en cualquier superficie requiere Pro, Max, Team, Enterprise o Console; el plan Free de claude.ai no lo incluye. Las sesiones en la nube están disponibles en Pro, Max y Team, y en Enterprise con asientos premium o Chat + Claude Code. Pro tiene términos de consumidor (REQ-MVP-51) y no crea una org de Claude: con asientos Pro, de la condición 3 de REQ-MVP-47 solo aplica la instalación de la Claude GitHub App.
- **REQ-MVP-50.** Si un grupo usa el plan Team para científicos, su elegibilidad DEBE confirmarse por escrito antes de comprometer al grupo. El plan es para PI de universidades acreditadas e institutos sin fines de lucro en ciencias naturales, matemáticas, computación, ingeniería y campos afines. Ciencias sociales y humanidades no aparecen en esa lista. Admite de 1 a 25 asientos durante 12 meses desde el alta. Los asientos estándar son gratuitos y los premium cuestan 15 USD por usuario al mes (o 180 USD al año). Ambos incluyen Claude Code. Hay un cupo y el programa puede cerrar en cualquier momento, así que se solicita en H0 (§4.1, #3). Es un plan por laboratorio que cubre a los «members of their lab»: si hay coautores de otras instituciones, DEBE confirmarse por escrito que entran. Tras 90 días sin uso, Anthropic puede retirar el precio promocional, así que el grupo DEBE usarlo al menos una vez al mes entre el alta y su ventana (00-marco D-26).
- **REQ-MVP-51.** Los repos del piloto NO DEBEN contener datos restringidos (clasificación de 10-seguridad). Solo se versionan texto y resultados agregados. Además, se DEBERÍAN preferir asientos con términos comerciales (Team o Enterprise) frente a los de consumidor (10-seguridad).
- **REQ-MVP-64.** Todos los participantes DEBEN ser miembros de la org del piloto, invitados y con la invitación aceptada antes de `init` (06-permisos REQ-PERM-55). La única excepción es un coautor que trabaje como colaborador externo con `gh auth login` (token `gho_`, nunca el clásico) y el aviso `W_AUTH_BROAD_TOKEN` (00-marco D-25). La org del piloto NO DEBE crearse dentro de una cuenta Enterprise universitaria con SSO si hay coautores de otras instituciones, porque solo podrían entrar como externos. Sus repos son privados y en `enforced` (GitHub Team, D-26); `advisory` es el último recurso y exige un método de pago para los minutos de Actions (09-ci §9).

| Prerrequisito | G-T | G-NT |
|---|---|---|
| Cuenta de GitHub, miembro de la org del piloto, con la invitación aceptada antes de `init` (REQ-MVP-64) | Sí | Sí |
| Asiento con Claude Code | Sí | Sí, con acceso a sesiones en la nube |
| Instalaciones | ≤ 3 (§2.2) | 0 |
| Claude GitHub App en el repo | Si usa la web | Sí |
| Consentimiento firmado | Sí | Sí |
| Superficie | CLI, IDE o pestaña Code de Desktop | Web o pestaña Code de Desktop |

### 5.2 Métricas

Definiciones operativas. "Día hábil" se toma del calendario del piloto; una «semana activa» es una semana de ese calendario fuera de vacaciones, y los días inhábiles no cuentan (00-marco D-27). Las columnas citadas están en §5.3.

| M | Métrica | Definición operativa | Fuente | Umbral |
|---|---|---|---|---|
| M1 | T_entrega | Mediana de `merged_at − claimed_at` por tarea integrada | `tasks` | (H) ≤ 5 días hábiles y no peor que la línea base |
| M2 | T_revisión | Mediana y p90 de `first_review_at − submitted_at` | `tasks` | (H) mediana ≤ 1 día hábil; p90 ≤ 2 (SLA) |
| M3 | Rondas de revisión | Media de `changes_requested_count` por tarea integrada | `tasks` | Informativa |
| M4 | Embudo de adopción | Por persona: `role_added` → primer `claimed` → primer `submitted` → primer `merged`; tiempo y conversión en cada paso | `events` | (H) el 100 % llega a su primera entrega en la semana 1; mediana hasta la primera entrega ≤ 1 día hábil desde la sesión de arranque |
| M5 | Uso semanal | % de participantes con al menos 1 evento `claimed`, `submitted`, `commit` o `review_*` en la semana activa | `events` | (H) ≥ 80 % de los participantes activos en ≥ 3 de cada 5 semanas activas |
| M6 | Calidad del coordinador en uso real | Confirmados: ítems con `ack:overlap`. Descartados: se quitó `flag:overlap` sin `ack:overlap`. No detectados: `ack:overlap` sin `flag:overlap` previo. Falsas alarmas = descartados / (confirmados + descartados) | `events` | (H) falsas alarmas ≤ 50 %; los no detectados se reportan |
| M7 | Trabajo duplicado | Número de `ack:overlap`, más una pregunta retrospectiva | `events` y entrevista | Informativa (se espera casi 0 por diseño) |
| M8 | Fricción de guardas | Mediana de `hook_denials` por entrega | `tasks` | (H) ≤ 2; con más de 5 en un PR se revisa la configuración |
| M9 | Satisfacción | SUS medio al final de la semana 1 y al final | Encuesta | (H) ≥ 68 por grupo, con intervalo de confianza del 90 % |
| M10 | Límites del asiento | Veces por semana que se alcanzó el límite de uso (autorreporte) | Pulso semanal | Informativa |
| M11 | Integraciones sin aprobación | Número de tareas con `merged_without_approval = true` | `tasks` | 0 en `enforced`, salvo bypass registrado (umbral firme, no hipótesis); en `advisory` se reporta |
| M12 | Rama base rota | Número de Issues `main-broken` y mediana del tiempo hasta cerrarlos | `events` | (H) mediana ≤ 1 día hábil |
| M13 | Abandono | Participantes sin eventos durante 2 semanas activas seguidas, con el motivo (entrevista de salida) | `events` y entrevista | (H) ≤ 1 por grupo por motivos de la herramienta |
| M14 | Cumplimiento del SLA | % de entregas con `first_review_at` dentro del SLA de §5.6 | `tasks` | (H) ≥ 90 % |

**Regla de decisión.** G-T "pasa" si M11 = 0 y se cumplen M9, M5 y M13. Si no pasa, antes de G-NT se corrige lo que haga falta, como cambio de alcance según §1.6. Si G-NT no cumple M4 o M13, la conclusión es que hace falta una puerta sin terminal del modo completo antes de escalar.

### 5.3 Instrumentación: `aucove metrics`

Las entradas, los flags y el sobre de la operación los define 05-operaciones. Este capítulo fija el **contenido** que el piloto necesita. Propuesta de flags: `--since <AAAA-MM-DD>`, `--until <AAAA-MM-DD>`, `--table tasks|events`, `--output <ruta>` y `--no-pseudonymize`.

- **REQ-MVP-52.** `aucove metrics` DEBE calcular todo desde la API REST de la forja (timeline de Issues y PR, revisiones) y desde el historial de la rama base. NO DEBE usar telemetría externa.
- **REQ-MVP-53.** El CSV DEBE ir en UTF-8 con encabezado, fechas en ISO 8601 UTC, listas separadas por `;` y celdas vacías para lo que no aplica.
- **REQ-MVP-54.** Por defecto, `actor`, `claimant` y `proposed_by` DEBEN seudonimizarse como `P-` seguido de los 10 primeros caracteres hexadecimales de HMAC-SHA256(sal, login en minúsculas). La sal se lee solo de `AUCOVE_METRICS_SALT` (64 hex como mínimo). Sin ella y sin `--no-pseudonymize`, `metrics` falla con `E_CONFIG_INVALID` y un `hint`. No existe `.aucove/metrics-salt`. La coordinación del piloto usa la misma sal en todos los grupos.

**Tabla `tasks`** (una fila por Issue con la etiqueta `task`):

| Columna | Tipo | Definición |
|---|---|---|
| `schema_version` | int | `1` |
| `repo` | str | `owner/name` |
| `profile` | str | `enforced` o `advisory` al exportar |
| `task` | int | Número del Issue |
| `state` | str | `derive_state` al exportar (00-marco §5.5) |
| `sections` | lista | `id` de las secciones declaradas |
| `proposed_by` | str | Autor del Issue (seudónimo) |
| `created_at` | fecha | Creación del Issue |
| `accepted_at` | fecha | Retiro de `state:proposed`; vacío si se creó ya abierta |
| `claimant` | str | Dueño del último reclamo (seudónimo) |
| `claimed_at` | fecha | `created_at` del comentario con el marcador `aucove:claim` vigente (04-tareas §1.4); si no existe, fecha de committer del commit `aucove: claim #<n>`; como último recurso, el evento `assigned`, y entonces la fila `claimed` de `events` lleva `detail=assigned_fallback` |
| `claims_count` | int | Número de marcadores `aucove:claim` del Issue |
| `expired_count` | int | Veces que se aplicó `state:expired` |
| `pr` | int | PR con rama `task/<n>` |
| `pr_created_at` | fecha | Creación del PR |
| `submitted_at` | fecha | Primer `ready_for_review`, o `pr_created_at` si el PR no nació en borrador |
| `first_review_at` | fecha | Primera revisión `APPROVED` o `CHANGES_REQUESTED` de alguien que no es el autor |
| `first_reviewer_role` | str | `lead`, `deputy` o `researcher` según `roles.yaml` de la base |
| `changes_requested_count` | int | Revisiones `CHANGES_REQUESTED` |
| `approved_at` | fecha | Última aprobación válida antes de integrar (regla de 06-permisos) |
| `merged_at` | fecha | Integración |
| `merged_without_approval` | bool | Integrada sin aprobación válida previa y sin bypass registrado (11-trazabilidad) |
| `cancelled_at` | fecha | Cierre como `not_planned` |
| `flag_overlap` | bool | Alguna vez tuvo `flag:overlap`, en el Issue o en el PR |
| `overlap_outcome` | str | `confirmed`, `dismissed`, `pending` o `missed` (M6) |
| `flag_direction_change` | bool | Alguna vez tuvo `flag:direction-change` |
| `hook_denials` | int | Valor del marcador de métricas del PR; vacío si no está |
| `t_delivery_h`, `t_review_h` | float | `merged_at − claimed_at` y `first_review_at − submitted_at`, en horas de calendario |

**Tabla `events`** (una fila por evento): `schema_version`, `repo`, `occurred_at`, `event`, `actor`, `actor_role`, `task`, `pr`, `label` y `detail`. Valores de `event`: `role_added` y `role_removed` (historial de `roles.yaml` en la base); `task_created`; `task_accepted`; `claimed`; `released`; `expired`; `pr_opened`; `submitted`; `commit` (eventos `committed` del PR); `review_approved`; `review_changes_requested`; `merged`; `task_cancelled`; `label_added`; `label_removed`; `main_broken_opened`; `main_broken_closed`.

**Marcador de métricas en el PR.** Las negativas se leen de `.aucove/log/events.jsonl` (líneas con `kind: hook` y `result: deny`; formato en 01-arquitectura §11, sin contenido de archivos). Al entregar, `submit` escribe en el cuerpo del PR `<!-- aucove:metrics v1 hook_denials=<n> -->`, justo antes del bloque `pr-meta` (03-esquemas §9.2), con las negativas de la tarea desde su reclamo, y lo reemplaza en entregas posteriores. Esto es un supuesto sobre 05-operaciones y 03-esquemas.

### 5.4 Línea base, encuestas y umbrales

- **Línea base:** en la semana 0, entrevista retrospectiva estructurada de 30 minutos con cada participante sobre el último documento de varios autores del grupo. Se recogen en las mismas unidades que M1, M2 y M7: tiempo del borrador a la sección aceptada, espera de revisión, trabajo duplicado y versiones perdidas o conflictos.
- **REQ-MVP-55.** La SUS DEBE aplicarse al final de la semana 1 y al final de la ventana de cada grupo, con la puntuación estándar: a los ítems impares se les resta 1; a 5 se le restan los ítems pares; la suma se multiplica por 2,5, para una escala de 0 a 100. La referencia de 68 es la media de unos 500 estudios. En `es` DEBE usarse una versión validada (p. ej. Sevilla-González et al., 2020).
- **Pulso semanal** de 3 preguntas, con escala de 1 a 5 y texto libre: "Esta semana la herramienta me ayudó a avanzar en mi parte", "Esta semana perdí tiempo por la herramienta" y cuántas veces se alcanzó el límite del asiento (M10).
- **REQ-MVP-56.** Los umbrales (H) de §5.2 DEBEN fijarse en su valor definitivo al cierre de H5, antes de ver datos del piloto, y quedar registrados en el informe de aceptación.

**Instrumento previo, no normativo: encuesta de diagnóstico (00-marco D-30).** Antes del piloto, la coordinación envía por WhatsApp una encuesta anónima sobre la adopción de IA (`encuesta_adopcion_ia.md`). No es línea base ni dato del piloto (REQ-MVP-57), no se enlaza con los seudónimos y no se publica sin su propia aprobación o exención del comité. El formulario de contacto solo recoge «quiero información»; el cribado de participantes va después de la aprobación ética.

### 5.5 Consentimiento y datos

- **REQ-MVP-57.** Ningún dato del piloto, entrevistas de línea base incluidas, DEBE recogerse antes de tener la aprobación o la exención del comité de ética y el consentimiento informado firmado de cada participante.
- El protocolo de ética se presenta a más tardar el 30/10/2026 (dispensa de §4.1, #7), con las ventanas previstas o un rango de fechas, para no tener que enmendarlo (00-marco D-27, D-28). Se pregunta al comité si cabe una exención (estudio de bajo riesgo, no de salud) y si cubre las pruebas de "primer día" de H4 y el uso de `aucove metrics` en H5.
- El consentimiento cubre: metadatos de la forja, encuestas, entrevistas y contadores locales; los registros de evidencia (comentarios `ci:evidence`, artefactos, copias locales y paquetes); seudonimización; lugar de almacenamiento; los plazos de REQ-MVP-65; el derecho a retirarse sin consecuencias, con el límite de REQ-MVP-67; y, en G-NT, que el repo se clone en VM de Anthropic.
- **REQ-MVP-65.** Los datos del piloto DEBEN conservarse por niveles, contando desde el fin de la recogida de datos de cada grupo (00-marco D-28):

| Datos | Plazo |
|---|---|
| Grabaciones de audio o video | Se borran a más tardar 30 días después de transcribirlas |
| Datos seudonimizados (CSV de `aucove metrics`, respuestas individuales, transcripciones) y la sal | Hasta el informe del piloto, como máximo 12 meses |
| Conjunto anonimizado y agregados | 5 años, o lo que marque la institución |
| Consentimientos firmados | Aparte, según el comité de ética |

- **REQ-MVP-66.** Al vencer el plazo de los datos seudonimizados, la coordinación DEBE destruir la sal y cualquier copia de correspondencias entre login y seudónimo; quitar del CSV `repo`, `task`, `pr` y las fechas exactas (se conservan el código de grupo y las duraciones); borrar los originales, y levantar un acta con fecha y responsable. Destruir la sal no anonimiza por sí solo: ningún conjunto conservado después DEBE contener logins ni referencias vinculables a GitHub. Con una sola sal para todos los grupos (REQ-MVP-54), la sal se destruye al vencer el plazo del primer grupo. En v0.1 esta transformación es un procedimiento de la coordinación, no un flag de `aucove metrics` (REQ-MVP-13).
- **REQ-MVP-67.** El consentimiento DEBE declarar además: (a) que el marcador `hook_denials` de los PR, los Issues y los PR, y `.aucove/log/events.jsonl` en las computadoras de los participantes, no se destruyen, y que los ve el `lead` y cualquiera con acceso al repo; (b) que los owners de la org del piloto pueden leer todos sus repos; (c) que retirarse es posible solo para datos no publicados y hasta la fecha de destrucción de la sal, que se indica; (d) la ley aplicable, que confirma la institución: la Ley General si es pública federal, la LFPDPPP si es privada y el RGPD si hay participantes de la UE; (e) que los registros de evidencia contienen logins, nombres, correos y horas; que no son datos del piloto ni se destruyen con la sal; y que se rigen por el acuerdo de autoría del grupo (00-marco D-46). Al terminar el piloto se pide a cada participante borrar sus registros locales.
- **REQ-MVP-58.** La coordinación del piloto DEBE estar a cargo de una persona que no sea `lead` de ningún grupo. Solo ella custodia la sal y cualquier correspondencia entre login y seudónimo (con HMAC no hace falta una tabla: la sal basta para recalcularlos). Los `lead` NO DEBEN ver respuestas individuales de encuestas ni de entrevistas, solo agregados de al menos 3 personas. `aucove metrics` sobre los repos del piloto lo ejecuta solo la coordinación, con un token de solo lectura (Metadata, Issues y Pull requests: Read); los `lead` no reciben la sal.
- **REQ-MVP-68.** Si quien coordina también desarrolla el framework (00-marco D-21), el informe DEBE declarar el posible sesgo, y las entrevistas de línea base y el análisis cualitativo DEBERÍAN hacerlos otra persona cuando sea posible.

### 5.6 Punto único en el responsable: SLA y suplente

- **REQ-MVP-59.** Cada grupo DEBE declarar `deputy`. El SLA de primera revisión es (H) de ≤ 2 días hábiles desde `submitted_at`.
- **REQ-MVP-60.** El `lead` DEBE avisar al grupo de una ausencia de 1 día hábil o más, con al menos 1 día de anticipación. Durante la ausencia, el `deputy` asume las revisiones. Si ambos faltan más de 2 días hábiles, la coordinación registra una "pausa de revisión" con inicio y fin fuera del repositorio.
- **REQ-MVP-61.** M2 y M14 DEBEN reportarse en bruto y ajustados, excluyendo en el ajuste las pausas de revisión registradas.

---

## 6. Identificadores de este capítulo

| Identificador | Tipo | Definición | Dueño sugerido |
|---|---|---|---|
| `<!-- aucove:metrics v1 hook_denials=<n> -->` | Marcador en el cuerpo del PR, justo antes del bloque `pr-meta` (03-esquemas §9.2) | Contador de negativas de hooks de la tarea, para M8 | 03-esquemas / 05-operaciones |
| `AUCOVE_METRICS_SALT` | Variable de entorno (64 hex como mínimo) | Única fuente de la sal de la seudonimización de `aucove metrics` (REQ-MVP-54) | 05-operaciones |
| `--since`, `--until`, `--table tasks\|events`, `--output`, `--no-pseudonymize` | Flags de `aucove metrics` | §5.3 | 05-operaciones |
| Tablas `tasks` y `events` (`schema_version` 1) | Esquema de CSV | §5.3 | 12-mvp |
| `derive_state` | Función interna de Python | La función pura de D-11 | 04-tareas |
| `proyecto-test`, `aucove-demo`, `demo-seed` | Org de pruebas, repo demo y su semilla | La org de pruebas se creó como `proyecto-test-13` (00-marco D-23) | 01-arquitectura |
| `ct-public`, `ct-private` | Repos sandbox fijos de `proyecto-test` | T2 (real) y T3 (§3.3) | 08-forja / 12-mvp |
| `proyecto-test-team`, `ct-private-team` | Org en GitHub Team de pruebas (provisional) y su repo privado `enforced` | Solo `CT-CAP-01` (privado) y REQ-PERM-53, de noviembre de 2026 a enero de 2027 (D-24, §3.3) | 08-forja / 12-mvp |
| `ct-<run_id>-<k>` | Repos efímeros de `proyecto-test` | Solo T7 y A12; se borran a las 24 h (§3.3) | 12-mvp |
| `tests/fixtures/template-manifest.txt` | Manifiesto de la plantilla | Rutas esperadas de REQ-MVP-4, generado desde 02-convencion §2.2 | 12-mvp / 01-arquitectura |
| `detail=assigned_fallback` | Valor de `detail` en la tabla `events` | Fila `claimed` fechada con el evento `assigned` por falta de marcador y de commit de reclamo (§5.3) | 12-mvp |
| `forge-tests`, `model-tests` | Environments de Actions del framework | §3.1 | 01-arquitectura |
| `TEST_GH_TOKEN_LEAD`, `TEST_GH_TOKEN_RESEARCHER_A`, `TEST_GH_TOKEN_RESEARCHER_B`, `TEST_GH_TOKEN_TEAM_LEAD`, `TEST_GH_TOKEN_TEAM_RESEARCHER_A`, `TEST_GH_TOKEN_TEAM_RESEARCHER_B`, `TEST_ANTHROPIC_API_KEY` | Secretos del CI del framework | §3.3 y §3.9 | 01-arquitectura / 10-seguridad |
| `evals/coordinator/cases/*.yaml` (`id`, `kind`, `seed_ref`, `action`, `task`, `expected`) | Formato del conjunto de referencia | §3.10 | 12-mvp |
| `tests/fixtures/hooks/<evento>/<caso>.json` (`class` ∈ `allow`, `deny`, `fault`) | Formato de fixtures | §3.5 | 12-mvp / 07-hooks |
| A17 | Criterio de aceptación | Evidencia verificable de fecha y de origen (§2.3) | 12-mvp / 11-trazabilidad |
| E11, E12 | Escenarios de T7 | `format: none` y sellado (§3.8) | 12-mvp |
| REQ-MVP-69 | Requisito del piloto | Acuerdo de autoría y de apertura, `disclosure`, correos noreply y `require_commercial` antes de `init` (§5.1) | 12-mvp |

`ack:overlap` ya figura en 00-marco §5.4, y las negativas de hooks se leen de `.aucove/log/events.jsonl` (`kind: hook`), definido en 01-arquitectura §11; ninguno de los dos se propone aquí. `.aucove/metrics-salt` no existe (REQ-MVP-54). REQ-MVP-1 pasa de 31 a 34 entradas: las 32 de 00-marco D-31 (a), más `ci seal` y `evidence verify` (D-50).

## 7. Supuestos sobre otros capítulos

- **01-arquitectura:** fija en §7.1 los presupuestos de hooks, `check-path` y contexto, y su medición en REQ-ARCH-55 (antes REQ-MVP-20). Documenta un solo comando que instala `uv` y `aucove`. Define un catálogo de mensajes cuya completitud se puede comprobar en CI. Soporta Python 3.11 y la última versión estable. Admite que el objeto servidor MCP sea independiente del transporte, para el arnés HTTP de pruebas.
- **04-tareas:** `derive_state(snapshot)` es total y determinista. El dueño del reclamo se puede determinar tras la carrera. Define la compensación de un reclamo a medias.
- **05-operaciones:** las operaciones H sin TTY devuelven un error del área `HUMAN_ONLY` con salida distinta de 0. `claim_task` devuelve `data.claimant`. El veredicto de `record_overlap_assessment` es `none`, `possible` o `clear`. Existe `--json`. `metrics` acepta los flags de §5.3. Las operaciones son idempotentes.
- **06-permisos:** la advertencia del perfil `advisory` aparece en `warnings`. Una aprobación es válida si la hace el `lead` o el `deputy`, y en los PR del `lead`, el `deputy`.
- **07-hooks:** el hook sale con código 2 ante cualquier error interno. Genera las reglas deny en `settings.local.json`. Usa los matchers `Edit|Write|NotebookEdit` y `Bash|PowerShell`. Registra cada negativa en `.aucove/log/events.jsonl` (`kind: hook`, 01-arquitectura §11). Define cómo existe `aucove` en la VM de la nube.
- **08-forja:** hay un adaptador falso con inyección de fallas y registro de llamadas. El adaptador usa solo REST para la API; en la web escribe en `task/<n>` con `git push` y reclama por push con lease (REQ-FORGE-56 y REQ-FORGE-67; 00-marco D-41). Detecta capacidades a partir de las respuestas de la API. Define la degradación si una repetición de S1 da otra salida.
- **09-ci:** el control de alcance es un check requerido que lee `roles.yaml` de la base. Existen el comentario con el PDF y el workflow de `main-broken`.
- **03-esquemas:** la plantilla de PR admite el marcador de métricas `<!-- aucove:metrics v1 hook_denials=<n> -->` justo antes del bloque `pr-meta` (§9.2).
- **10-seguridad:** acciones fijadas por SHA; secretos fuera del alcance de los PR desde forks; clasificación de datos para los repos del piloto.
- **11-trazabilidad:** existe un libro de bypass consultable, para la excepción de M11.

## 8. Preguntas abiertas

- **Q-MVP-5.** ¿La prohibición de `claude -p` de D-15 aplica también al CI de pruebas del propio framework? Este capítulo supone que sí y usa el Agent SDK.
- **Q-MVP-9.** ¿Los niveles N0–N3 se activan con un interruptor en `project.yaml` (03-esquemas) o solo se documentan?
- **Q-MVP-10.** ¿Se acepta el arnés HTTP en proceso para la conformidad MCP mientras v0.1 solo tenga stdio?
- **Q-MVP-16.** Si un grupo llega a un hito dentro de su ventana, ¿se practica una apertura (preprint y espejo) en el piloto o solo un depósito cifrado?

## 9. Referencias

- Claude Code, instalación y cuentas: https://code.claude.com/docs/en/setup
- Entornos en la nube (qué se conserva, proxy de GitHub, setup scripts, herramientas instaladas, dominios permitidos): https://code.claude.com/docs/en/cloud-environments
- Claude Code en la web (planes, GitHub App, `allow_remote_sessions`, allowlist de IP): https://code.claude.com/docs/en/claude-code-on-the-web
- Claude Desktop, pestaña Code (misma configuración que la CLI, Git y `gh`): https://code.claude.com/docs/en/desktop
- Hooks (entrada de PreToolUse, código 2, timeouts, matchers de SessionStart, tope de 10.000 caracteres, forma exec en Windows): https://code.claude.com/docs/en/hooks
- Agent SDK (API key, `setting_sources`, `max_budget_usd`, `total_cost_usd`): https://code.claude.com/docs/en/agent-sdk/overview · https://code.claude.com/docs/en/agent-sdk/claude-code-features · https://code.claude.com/docs/en/agent-sdk/python
- Workspaces y límites de gasto de Claude Console: https://platform.claude.com/docs/en/manage-claude/workspaces
- Plan Team para científicos: https://support.claude.com/en/articles/16634237-claude-team-plan-for-scientists
- GitHub, ramas protegidas por plan: https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches
- GitHub REST: timeline https://docs.github.com/en/rest/issues/timeline · tipos de evento https://docs.github.com/en/rest/using-the-rest-api/issue-event-types · refs https://docs.github.com/en/rest/git/refs · revisiones https://docs.github.com/en/rest/pulls/reviews
- GitHub Actions, runners estándar (gratis en repos públicos): https://docs.github.com/en/actions/writing-workflows/choosing-where-your-workflow-runs/choosing-the-runner-for-a-job · https://github.com/actions/checkout · https://github.com/astral-sh/setup-uv
- Codespaces, facturación: https://docs.github.com/en/billing/concepts/product-billing/github-codespaces
- Términos de GitHub (cuentas de máquina): https://docs.github.com/en/site-policy/github-terms/github-terms-of-service
- Conformidad MCP: https://github.com/modelcontextprotocol/conformance
- Agent Skills: https://agentskills.io/specification · https://github.com/agentskills/agentskills/tree/main/skills-ref
- SUS: https://www.measuringu.com/sus/ · versión en español validada: https://pmc.ncbi.nlm.nih.gov/articles/PMC7773510/

**Supuestos no verificados:**
- El código HTTP exacto que devuelve crear una ref existente (la documentación lista 409 y 422; A7 no depende de cuál).
- Todos los tiempos y costos marcados (H).
