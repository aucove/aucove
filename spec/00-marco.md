# Especificación técnica v0.1 — Marco

- **Estado:** v0.1
- **Nombre del proyecto:** ver §5.1.

Este archivo es **normativo**: define el alcance, las notas de diseño, el glosario y el **registro canónico de identificadores**. Todos los demás capítulos DEBEN usar los identificadores de este registro. Si un capítulo necesita uno nuevo, se agrega a este registro.

---

## 1. Propósito y alcance de v0.1

La v0.1 especifica lo necesario para construir y pilotear el **MVP en modo mínimo**:

**Dentro de v0.1**

1. **Núcleo** en Python: un solo paquete con la CLI `aucove`, un servidor MCP local (`aucove mcp --stdio`) y el punto de entrada de hooks (`aucove hook <evento>`). Toda la lógica del proyecto vive aquí.
2. **Convención de repositorio v0.1** y la **plantilla** que la implementa, con o sin manuscrito LaTeX (`format: latex | none`, D-43).
3. **Adaptador de forja para GitHub** usando solo la API REST (en las sesiones web, las refs y los commits se escriben con `git push`; D-41). Tiene dos perfiles de capacidades, elegidos por detección:
   - `enforced`: la aprobación se puede exigir (GitHub Team, Education o Enterprise, o repositorio público).
   - `advisory`: la aprobación no se puede exigir (GitHub Free con repositorio privado).
4. **Puerta Claude Code**, en todas sus superficies: terminal, extensiones de IDE, pestaña Code de Claude Desktop y sesiones web. Se entrega como archivos `.claude/` dentro de la plantilla, no como plugin de catálogo.
5. **Otras puertas** (D-58): Codex con soporte completo (instrucciones, contexto, skills, servidor MCP y hooks); Antigravity CLI, después, si el spike S7 lo confirma (Google lo puso en lugar de Gemini CLI); y cualquier otro agente que lea `AGENTS.md`, en nivel básico (instrucciones, contexto, CLI y MCP, sin hooks).
6. **CI determinista** en GitHub Actions: compilación segura, control de alcance, validación de esquemas y vencimiento de reclamos.
7. **Evidencia verificable de fecha:** el workflow `seal.yml` sella cada entrega (OpenTimestamps) y cada integración (dos TSA RFC 3161 más OpenTimestamps). Además: verificación local con kit para el árbitro (`aucove evidence verify`), copia local de los registros propios y guías de custodia y exportación, apertura por hitos y depósito cifrado (D-44 a D-49).

**Fuera de v0.1** (solo se reservan nombres o interfaces para no cerrar el camino)

- Modo completo: servidor MCP remoto (Streamable HTTP + OAuth), MCP App (tablero), Claude Desktop en su pestaña Chat, Cowork y claude.ai.
- Integración con Drive, OSF o Nextcloud; el depósito de hitos en OSF es un procedimiento manual (D-47). Overleaf como puerta de escritura. Quedan reservados para v0.2 `aucove evidence export`, `aucove evidence deposit` y `aucove evidence disclose`.
- Implementación de los adaptadores de GitLab y Forgejo. Su interfaz sí se define.
- Plugin de catálogo de Claude Code (fase 2).
- Revisión con modelo en CI: se especifica como opcional y **desactivada por defecto**.
- Markdown y Quarto como formatos compilados. v0.1 admite `format: latex` (manuscrito compilado por el CI) y `format: none` (sin manuscrito: el texto de cada parte va en Markdown y no se compila) (D-43).
- Colaboración entre instituciones con identidad federada, y el ciclo de vida posterior a la aprobación (envío a revista, revisión por pares).

## 2. Cómo leer esta especificación

- **Palabras normativas:** **DEBE**, **NO DEBE**, **DEBERÍA**, **NO DEBERÍA** y **PUEDE** equivalen a MUST, MUST NOT, SHOULD, SHOULD NOT y MAY del RFC 2119.
- **Requisitos:** se numeran `REQ-<CÓDIGO>-<n>`. El código es el del capítulo (§6), por ejemplo `REQ-OPS-12`. Un requisito se define en un solo capítulo; los demás lo citan.
- **Autoridad:** cada tema tiene un capítulo dueño (§6). Los demás capítulos lo referencian ("ver 05-operaciones §3") y no lo redefinen.
- **Idioma:** la prosa va en español. Los identificadores estables (claves de archivos, subcomandos, herramientas MCP, códigos de error, etiquetas, estados, rutas) van en **inglés ASCII** y se escriben en `código`.
- **Preguntas abiertas:** cada capítulo termina con "Preguntas abiertas" (`Q-<CÓDIGO>-<n>`), y `13-preguntas-abiertas.md` las consolida.

## 3. Notas de diseño

Cada nota resume una decisión de diseño: qué se decidió y por qué. Su identificador (D-01 a D-69 y DC-01 a DC-25) es estable: los capítulos, el código y las pruebas lo citan. Son tan normativas como los capítulos; si una nota y un capítulo difieren, el capítulo se corrige. Una nota reemplazada queda en una línea que remite a la que la reemplaza.

### 3.1 Núcleo y distribución

**D-01. Núcleo en Python ≥ 3.11, en un solo paquete.** Sirve a la CLI, al servidor MCP (stdio en v0.1) y a los hooks, para que todas las puertas apliquen las mismas reglas. Se distribuye por PyPI y se instala con `uv tool install`.

**D-02. Identificadores estables en inglés ASCII.** Skills, mensajes, contenido de la plantilla y documentación se localizan en el idioma del proyecto (`es` o `en`), que se elige en `init`.

**D-03. Nombre: `aucove`.** Sigla de «AUtoría COlaborativa VErificable»; se pronuncia au-CO-ve en español y AW-kove en inglés. Cumple 11-trazabilidad REQ-TRACE-69. Al elegirlo estaba libre en PyPI, en npm y como cuenta de GitHub, y una búsqueda preliminar de marcas no encontró registros iguales; antes de solicitar la marca conviene una búsqueda formal. El dominio `aucove.com` es de un tercero, así que el proyecto usa `.org` o `.dev`. El nombre se define en un solo lugar del código (§5.1).

**D-15. El núcleo nunca llama a un modelo.** La revisión con modelo en CI es opcional y usa una API key u OIDC en un environment protegido; nunca tokens de suscripción ni `claude -p`.

**D-16. Modo completo reservado.** Solo se reserva su forma: «cómputo sin estado + almacén cifrado».

**D-51. La especificación vive en el repo del framework.** Está en `spec/` (01-arquitectura §2.1), en CC-BY-4.0 (§2.2). `schemas/` es la copia verificada de `spec/schemas/` (`tools/sync_schemas.py` y `SHA256SUMS`). Las pruebas toman la spec de `spec/` si no se define `AUCOVE_SPEC_DIR`, así que la comparación con la spec corre también en el CI.

**D-29. El repo público del framework existe desde el primer release.** En Free, los secretos de environment solo existen en repos públicos, y en Free, Pro y Team, los revisores obligatorios de un environment también (`pypi`, REQ-SEC-40; `forge-tests`, 12-mvp §3.3). REQ-MVP-46 limita la publicación en índices, no la visibilidad (12-mvp REQ-MVP-62).

**D-54. La versión pública empezó como un repo nuevo y limpio.** El desarrollo previo (el alpha) queda privado y archivado como registro de procedencia, con todos sus commits firmados, y la versión pública es autocontenida. El corte se hizo justo antes del primer release, porque la plantilla instala el núcleo desde PyPI y baja `requirements-lock.txt` del Release público (01-arquitectura §5.1). Tuvo tres pasos: una limpieza del alpha (la spec en forma normativa y las decisiones como estas notas; las pruebas de regresión nombradas por lo que comprueban; y la lista de administración convertida en una guía de mantenimiento), el repo nuevo con un solo commit inicial hecho por una persona con `git commit -s` (11-trazabilidad REQ-TRACE-67) y los environments `pypi` y `forge-tests`, y el primer release desde ese repo. Mientras el framework vivió en el alpha privado, T2 (real), T3 y T7 corrieron en local, con los tokens fuera del repo.

**D-59. Instalación en la nube por setup script.** S2 midió que el núcleo se instala en unos segundos en una sesión en la nube, y que la imagen de la VM cambia sin aviso (versiones de Python y uv). Por eso: (a) se instala con el setup script de 01-arquitectura §5.3 y una versión fijada del índice, sin suponer versiones de Python ni de uv; (b) el setup script no instala desde el clon, porque el entorno es de la cuenta y no de un repositorio; (c) mientras una versión no esté en un índice, se instala dentro de la sesión; (d) si el setup script falla, la sesión arranca sin el núcleo, y la regla de `AGENTS.md` (01-arquitectura REQ-ARCH-40) hace que el agente se detenga y pida instalarlo; el CI sigue haciendo cumplir las reglas.

**D-60. Timeouts de la forja y tope de red por operación.** El adaptador de GitHub usa 5 s de conexión y 20 s de lectura (08-forja REQ-FORGE-23), porque algunas llamadas tardan más y un timeout en una escritura obliga a averiguar si se aplicó (08-forja §4.4 y §4.7). Las demás llamadas de red, como las TSA y OTS del sellado (09-ci REQ-CI-53), usan 15 s (01-arquitectura REQ-ARCH-62). El tope de 120 s por operación rige en CLI y MCP; en CI rigen las esperas de 08-forja §7.3.

**D-66. `core_pin` admite versiones preliminares de PEP 440.** `X.Y.Z` con un sufijo opcional `aN`, `bN` o `rcN`, en la forma normalizada (la del paquete), en el esquema de `project.yaml`, en la línea de REQ-ARCH-45 y en el `sed` de cada workflow (REQ-CI-12), para que un repo pueda fijar una rc. La compatibilidad sigue siendo la de SemVer (REQ-ARCH-42), evaluada con la semántica de `packaging`; con una preliminar instalada, `init` la usa como cota inferior de `core` (REQ-ARCH-44).

**D-69. Las versiones preliminares se publican en PyPI.** Igual que las finales: desde el repo público, por Trusted Publishing con el environment `pypi` (10-seguridad REQ-SEC-40), con `requirements-lock.txt` y `sbom.cdx.json` en su GitHub Release preliminar (01-arquitectura §5.1). Así la plantilla, sus workflows (09-ci REQ-CI-12) y el README instalan una rc sin cambios. Una versión preliminar no actualiza la rama principal de la plantilla (01-arquitectura REQ-ARCH-8).

**DC-19.** Reemplazada por D-69.

**DC-23. Subcomandos internos.** `ci build-plan|comment|base-report|audit|pin-plan|seal` son internos, como `hook`: no emiten el sobre ni tienen herramienta MCP. Su contrato lo define 09-ci. `ci review-context` queda reservado.

**DC-24. Versionado de los identificadores.** La regla del §7 rige desde la publicación de 0.1. Los cambios de identificadores del §5 anteriores a esa publicación, incluidos los de D-43 a D-50 (`sections/<id>/`, `format: none`, `disclosure`, `seal.yml`, `ci seal`, `evidence verify`, los checks `aucove/seal-*`, el marcador `ci:evidence`, `.aucove/evidence/` y los reservados `evidence export|deposit|disclose`), no suben la versión, y la convención sigue en "0.1" sin migración.

### 3.2 Puertas, agentes y contexto

**D-05. La puerta Claude Code es `.claude/` en la plantilla.** Skills y hooks que llaman al núcleo, más `.agents/skills/` para otras herramientas, generados desde una sola fuente. El plugin de catálogo queda para la fase 2, como complemento y como respaldo para las instituciones que impongan `strictPluginOnlyCustomization` (07-hooks §12): la nube no carga los plugins declarados por el repositorio, y un plugin no puede aportar reglas `deny`.

**D-12. No hay PR en SessionEnd.** El modelo escribe la nota de sesión durante `submit`, dentro del PR de la tarea: una tarea, una rama, un PR.

**D-13. Los hooks guían al agente; el CI hace cumplir las reglas.** El control de alcance es un check requerido de CI. Los controles locales se aplican en capas y bloquean ante cualquier fallo.

**D-18. Overleaf queda fuera del MVP.**

**D-52. Hooks en forma exec en Windows.** S4 comprobó que, en Windows, el hook `"command": "aucove"` con `args` es hijo directo de `claude.exe`, sin shell, y que con el matcher `Bash|PowerShell` los hooks se disparan sobre PowerShell y un `PreToolUse` que sale con 2 bloquea (07-hooks §4.3). Claude Code busca `command` solo en el `PATH`, nunca en el directorio actual, salvo por las entradas relativas del `PATH` (`.` o `sub`), que se resuelven contra el directorio del hook; por eso `doctor` avisa de ellas (01-arquitectura REQ-ARCH-33; 10-seguridad TH-24). Un `.cmd` del `PATH` también se ejecutó en forma exec, pero como no está documentado se mantienen el requisito del `.exe` y su aviso (01-arquitectura §5.1). La prueba se repite antes de H4 y de H7, junto con la de S1 (D-22), y al subir la versión mínima de Claude Code (DC-22).

**D-58. Cada persona elige su agente y su modelo.** El contrato con cualquier agente es `AGENTS.md` (las reglas), el contexto compartido, la CLI (o su servidor MCP) y el CI, que hace cumplir las reglas (D-13); la herramienta usada queda en el trailer `Assisted-by` (11-trazabilidad REQ-TRACE-68). Hay tres niveles. (1) **Completo:** Claude Code, en todas sus superficies, y Codex, con instrucciones, contexto al iniciar, skills, servidor MCP y hooks que llaman a `aucove hook <evento>`; para Codex, `.codex/hooks.json` y `.codex/config.toml` (07-hooks §13), que cada persona aprueba una vez en `/hooks`. (2) **Después:** Antigravity CLI, con el mismo nivel, si el spike S7 lo confirma; hasta entonces, queda en el nivel básico, igual que Gemini CLI. (3) **Básico:** cualquier otro agente que lea `AGENTS.md` y pueda ejecutar comandos (OpenCode, goose, Cursor o Copilot): instrucciones, contexto, CLI y MCP si lo admite, sin hooks, así que sus errores los detiene el CI. **Contexto compartido:** `aucove context` devuelve también un resumen en Markdown (el plan, las partes y sus dueños, quién trabaja en qué, las decisiones y notas recientes y los avisos) y lo deja en `.aucove/context.md`; `AGENTS.md` pide leerlo al empezar, junto con `plan.md`, `memory/constitution.md` y, con LaTeX, la guía del artículo (D-56). **Cuentas de IA:** cada persona usa su propia cuenta, sin compartirla, con un cliente que admitan los términos de su proveedor; con una suscripción de Claude, solo Claude Code o las aplicaciones de Anthropic, y un agente de terceros que use modelos de Claude va con una clave de API. El framework nunca pide, guarda ni usa credenciales de un proveedor de IA, ni ejecuta un agente con la cuenta de una persona (D-15; 10-seguridad REQ-SEC-86). Los spikes S6 (Codex) y S7 (Antigravity CLI) comprueban, en Windows y en Linux, que el agente lee `AGENTS.md` y los skills, usa el servidor MCP, dispara los hooks y niega con exit 2, y no busca el comando en el directorio actual.

**D-61. Lecturas de `status` y `context`, y resumen de `context`.** El presupuesto de 08-forja §7.5 se cumple así: (1) `status` y `context` leen en bloque los Issues con `task`, el Issue abierto con `main-broken`, los PR, las refs `task/*`, la ref y los checks de la base y, por cada PR abierto, sus revisiones y sus checks. (2) Una tarea integrada o cancelada se deriva con eso: `derive_state` llega a `merged` o `cancelled` antes de mirar eventos o marcadores. (3) Una tarea activa se deriva con la instantánea completa solo cuando cambió su huella (el Issue con su `updated_at`, sus etiquetas y asignados; la ref; los PR con su cabeza y su `updated_at`; las revisiones; y la política de la base) o cuando la derivación tiene más de 24 h; si no, se reutiliza la de `.aucove/cache/tasks/<n>.json`. Así un `status` sin cambios cuesta 6 + 2·P lecturas (más E, los PR propios cuyos comentarios lee la copia de evidencia). (4) `summary_md` tiene su propio tope (`context.max_chars`) y no cuenta para el tope del sobre de `context`; sus bloques fijos (versión y antigüedad de los datos, identidad, rama, tarea y rutas, avisos) nunca se recortan, y los demás se quitan enteros en este orden: notas de sesión, decisiones, tareas de otros, guía del artículo, plan, partes y tareas propias. (5) La copia de evidencia (05-operaciones REQ-OPS-66) solo toma los comentarios `ci:evidence` cuyo autor es el actor de CI. (6) `claim` lee el mismo tablero para P4 y P6 (04-tareas §3) y para el paquete de contexto (04-tareas §8.2), y relee solo la tarea que reclama.

**D-70. Arranque guiado y presentación en el skill de inicio.** Preparar un proyecto no debe exigir escribir YAML a mano, y la primera sesión de cada persona debe decirle qué hacer. (1) **Preparación pendiente:** la hay si el `plan.md` de la base sigue siendo el de la plantilla o si, con `mode: group`, el `roles.yaml` de la base tiene una sola persona activa. `context` la avisa con la alerta `setup_pending` (05-operaciones REQ-OPS-69), así que cualquier agente la ve al empezar. (2) **La tarea de preparación:** si la preparación está pendiente y el repositorio no tiene ninguna tarea, `init` crea una tarea del `lead`, «Preparar el proyecto», que declara sus partes (justo después de `init`, todas), y la acepta (05-operaciones §9.3). Es la «primera tarea» de D-63. Aceptarla dentro de `init` no cambia D-09: `init` ya es solo humana, y su resumen anuncia la tarea antes de la confirmación tecleada. (3) **Arranque guiado:** con la preparación pendiente, el skill de inicio (`empezar`/`start`, §5.8) entrevista al responsable en lenguaje llano sobre el proyecto, las personas, las partes, los datos, las cuentas de IA y el estilo. Con eso redacta los archivos de clase `lead` en la tarea de preparación, los valida y la entrega como cualquier otra (04-tareas §2.4). A cualquier otra persona le explica que el proyecto lo prepara el responsable. `AGENTS.md` pide ofrecerlo (02-convencion §8.3), así que también sirve con agentes sin skills. (4) **Presentación:** si la persona no tiene tareas propias activas ni en revisión, el skill empieza con cinco líneas como máximo: sus partes, las tareas abiertas en ellas y el ciclo de empezar, trabajar y entregar. (5) No agrega operaciones, skills ni etiquetas. Usa `task propose`, `claim`, `sync-roles`, `validate`, `decision propose` y `submit`, y aceptar, aprobar e integrar siguen siendo humanos.

**DC-09. Nota de sesión.** `submit` recibe `note` como objeto estructurado y agrega un bloque; el agente no edita `memory/notes/`. El formato lo define 03-esquemas, y cada ronda nueva exige un bloque nuevo.

**DC-11. Contenido no confiable.** Objeto `marked_text` con `trust` `rules`, `reviewed` o `untrusted`, y delimitadores `<<<DATA>>>` (10-seguridad §3.3). En el paquete de contexto, `spec{summary,done}` reemplaza a `task.body_excerpt`.

**DC-12. Tope de contexto.** `context.max_chars`, 8000 por defecto, en el rango 2000..9000; `claim` y `submit` mantienen 8000.

**DC-13. Árbol `.aucove/`.** Compartido en el checkout principal. Las entradas de cada worktree usan como `<id>` los primeros 12 hex del SHA-256 de su ruta real. Hay un solo registro, `log/events.jsonl`, y las sesiones van en `state/sessions/`.

**DC-21. Hooks generados.** v0.1 genera hooks para Codex (D-58), pero no para Gemini CLI ni para Antigravity CLI. `.codex/**`, `.gemini/**`, `.mcp.json`, `opencode.json`, `.opencode/**` y `.agents/**` (salvo los skills) son clase `lead`.

**DC-22. Versión mínima de Claude Code para toda la plantilla:** 2.1.251.

### 3.3 Forja, perfiles y permisos

**D-04. GitHub, con perfiles por detección de capacidades.** El perfil es `enforced` cuando GitHub aplica reglas en la rama base y `advisory` cuando no. GitLab y Forgejo quedan detrás de la interfaz del adaptador, para una fase posterior.

**D-08. `roles.yaml` es la única fuente de autorización.** `CODEOWNERS` y las reglas locales se generan a partir de él. Todo control lee `roles.yaml` de la rama base, nunca de la rama de trabajo.

**D-09. Operaciones solo humanas.** Aprobar, pedir cambios, aceptar y cancelar tareas, e `init`. No se exponen por MCP ni como skill, y en la CLI exigen confirmación interactiva (TTY). El núcleo no fusiona: la integración se hace en la web de la forja.

**D-17. El suplente (`deputy`) aprueba los PR del responsable.** Sin suplente, el perfil `enforced` avisa que esos PR requieren un bypass registrado.

**D-25. Membresía y tokens.** Todos los participantes son miembros de la org; se los invita antes de ejecutar `init`, y `init` y `doctor` avisan si detectan colaboradores externos (`affiliation=outside`; 06-permisos REQ-PERM-55). La única excepción es `gh auth login` (token `gho_`), con `W_AUTH_BROAD_TOKEN`; nunca el token clásico. Los tokens de GitHub App (`ghu_`) quedan para v0.2. No se usa una org dentro de una cuenta Enterprise universitaria con SSO si hay coautores externos. En un repo privado de una org Team, un colaborador externo también ocupa asiento, así que exigir la membresía no cuesta más.

**D-39. Política de Actions de la organización.** Con `allowed_actions: local_only`, o `selected` sin las acciones que usa la plantilla (`actions/checkout`, `actions/upload-artifact`, `actions/download-artifact` y `astral-sh/setup-uv`), los workflows fallan al arrancar y los checks requeridos nunca pasan. `init` y `doctor` lo detectan con `GET /repos/{o}/{r}/actions/permissions` y `GET /orgs/{org}/actions/permissions`, que exigen permiso de administración (sin él, el resultado es desconocido); avisan con `W_FORGE_ACTIONS_RESTRICTED`, explican qué acciones permitir, fijadas por SHA, y no cambian la política (06-permisos §4.3 y REQ-PERM-56; 05-operaciones REQ-OPS-64; 10-seguridad §2.3). La guía documenta la configuración mínima de la org: «Allow actions created by GitHub» más `astral-sh/setup-uv`.

**D-62. Interfaz de administración de la forja (`ForgeAdmin`).** Las escrituras de configuración que hace `init` (ajustes del repositorio, solo squash con el título y el cuerpo del PR, permisos de Actions, retención, colaboradores y `upsert_ruleset`) y las lecturas que las comparan viven en una interfaz aparte, `ForgeAdmin` (08-forja §2.2). Solo la construye `init`, con el token de administración que la persona teclea en la terminal (10-seguridad REQ-SEC-30), que vive solo en memoria. El adaptador normal, el de los agentes y las demás operaciones, no puede cambiar la configuración.

**D-63. Dos ajustes de `init` (05 §9.3).** (1) La aceptación del perfil `advisory` queda en `project.yaml: forge.advisory_acceptance` (`by` y `at`), dentro del commit de `init`; el registro en `memory/decisions/` lo hace el `lead` desde su primera tarea, porque el nombre de un registro exige el número de una tarea (03-esquemas REQ-SCHEMA-46), e `init` lo deja en `data.pending_steps` (06-permisos REQ-PERM-41). (2) `data_init.files[].action` admite `deleted`: `init` borra archivos con `format: none` y las partes de la plantilla que `roles.yaml` no declara.

**D-65. Lecturas de configuración en el adaptador normal.** `doctor` compara con lo esperado la configuración que aplica `init` (05-operaciones §9.2 y REQ-OPS-64; 06-permisos REQ-PERM-28, 37, 39 y 55; 11-trazabilidad REQ-TRACE-36; 10-seguridad REQ-SEC-71). Para eso, `ForgeAdapter` lee, solo con GET y con el token de la persona (08-forja §2 y REQ-FORGE-73), los ajustes del repositorio, los rulesets, las etiquetas, el permiso de cada colaborador, los colaboradores externos, la política de Actions, el token de los workflows y los ajustes de la organización. Lo que el token no puede leer queda como desconocido, sin aviso. Las escrituras siguen solo en `ForgeAdmin` (REQ-FORGE-3).

**DC-02. Checks requeridos: `aucove/build` y `aucove/validate`.** La aprobación de `lead` o `deputy` la exige GitHub con `CODEOWNERS` (`* @lead @deputy`). No hay checks propios de alcance ni de aprobaciones.

**DC-06. Excepciones de la forja.** Cambio de login → `E_AUTH_LOGIN_CHANGED`; conflicto de ref → `E_FORGE_CONFLICT`; sin commits entre ramas → `E_FORGE_UNEXPECTED`; falla de TLS → `E_NET_TLS`.

**DC-07. Identidad en CI.** No existe `E_PERM_CI_ONLY`: `expire-claims` lo ejecutan el CI, `lead` o `deputy`. Los subcomandos `ci *` y `validate --mode pr|base` actúan con la identidad de quien los invoca, y `derive_state` ignora las etiquetas y los marcadores de actores no autorizados. La identidad de CI se reconoce por `GITHUB_ACTIONS`, `GITHUB_RUN_ID` y un token `ghs_`.

**DC-08. Sin PR en borrador ni GraphQL.** Solo API REST. Un PR en borrador produce `E_STATE_PR_DRAFT`.

**DC-16. Push ruleset `aucove-workflows`.** Da bypass siempre al usuario `lead` y excluye `.github/CODEOWNERS`.

**DC-17. Aprobaciones obsoletas.** En ambos perfiles, una aprobación solo cuenta si su `commit_id` es la cabeza del PR. El campo de auditoría es `merge_audit`.

**DC-25. Solo el reclamante empuja a `task/<n>`** (`allow_update_branch: false`).

### 3.4 Partes, formato y convención

**D-06.** Reemplazada por D-43.

**D-14. Con `format: latex`, el PDF oficial sale del CI.** La compilación local es opcional, solo al entregar y nunca en cada edición. Con `format: none` no hay PDF: el registro oficial es la rama base con sus sellos (D-43, D-45).

**D-19. Una tarea declara sus partes (`sections`).** Solo puede tocar las partes declaradas cuyo dueño es quien la reclamó (rutas `sections/<id>/**`), su nota y archivos nuevos de decisión. Una tarea reclamada por `lead` o `deputy` puede además tocar rutas de clase `lead` sin declararlas (06-permisos §2.2).

**DC-01. Alcance de tarea.** En v0.1 ninguna tarea toca partes ajenas (`sections/<id>/` de otro dueño; reservado, Q-TASK-4).

**D-43. Partes con dueño y LaTeX opcional.** El centro del proyecto son los resultados de cada tarea (texto, código, datos derivados, figuras, tablas y notas), en la parte de cada investigador. Una parte es la unidad con un solo dueño; su identificador estable sigue siendo `section` (clase de ruta, trailer `Section`, clave `sections` de `roles.yaml` y códigos de error), y el texto visible dice «parte» (`es`) o «part» (`en`). Vive en `sections/<id>/`, con `figures/`, `tables/`, `analysis/`, `analysis/derived/`, `provenance.yaml` y `notes/`, opcional. `project.yaml: format` admite: (a) `latex`: manuscrito en `tex/`, compilado por el CI; el texto de cada parte va en `<id>.tex`, con su `pending.bib`, y las rutas de LaTeX empiezan con `../sections/<id>/`; (b) `none`: sin `tex/`, `bib/`, `.latexmkrc`, bloque `build`, `ci.pdf_retention_days` ni `ci.latexdiff`; el texto de cada parte va en `<id>.md`, que no se compila; el check requerido `aucove/build` termina en éxito con el motivo «sin manuscrito», las reglas de LaTeX y de bibliografía quedan en `skip`, y el lint de instrucciones IL05 e IL08 se aplica a los Markdown de las partes (10-seguridad §3.6). `markdown` y `quarto` siguen reservados como formatos compilados. La plantilla publicada es la variante `latex`, e `init --format none` produce la otra. El CI no fija `openin_any=p`: no tiene efecto desde TL2026, y en imágenes anteriores impide `../`.

**D-55. Esquema del artículo por defecto y subpartes.** La plantilla trae 7 partes, en este orden: introducción, antecedentes y trabajo previo, marco teórico, propuesta y metodología, experimentos, análisis de resultados y conclusiones (`introduction`, `background`, `theory`, `methodology`, `experiments`, `results` y `conclusions`, con títulos localizados), en los dos formatos. Es el esquema por defecto: el responsable agrega, quita, renombra o reordena partes en `roles.yaml` y las reparte antes de empezar (alta y retiro en 02-convencion §4.2.1). Cada parte tiene un solo dueño, y una persona puede tener varias. Si una sección del artículo necesita varios autores, se divide en subpartes, cada una con su dueño (por ejemplo, `experiments-a` y `experiments-b`), y con LaTeX el editor las agrupa en su sección (D-56).

**D-56. Editor del artículo (`editor`).** Con `format: latex`, una persona cuida que el artículo se lea como un solo texto. (1) **Quién:** se declara en `roles.yaml` con la clave de nivel superior `editor` (un `login` de `people`, de cualquier rol); si no se declara, es el responsable. Con `format: none` no hay editor. (2) **Sus archivos** (clase de ruta `editor`, §5.2): el esqueleto del artículo (`tex/main.tex`, `tex/preamble.tex`, `tex/notation.tex` y `bib/references.bib`), un archivo de enlace por sección del artículo, `tex/outline/<id>.tex` (el título de la sección, su entrada, el orden de sus partes y las transiciones), y la guía del artículo, `tex/guide.md`. Las partes no llevan el `\section` de su sección: lo pone el archivo de enlace (gramática en 02-convencion). (3) **No modifica las partes de otros:** revisa los PR que tocan el LaTeX y pide cambios al dueño de cada parte, para que la evidencia de quién escribió cada parte siga limpia (D-45). (4) **Su aprobación es obligatoria en los PR que tocan el LaTeX** (los `.tex` y `pending.bib` de las partes y sus propios archivos), con el check requerido `aucove/editor-review` (09-ci), que pasa si el PR no toca LaTeX o si el editor aprobó la cabeza actual. Si el autor del PR es el editor, basta la aprobación del responsable o del suplente. (5) **Cómo guía al modelo:** la guía del artículo dice qué cubre cada sección y cada parte, y fija la notación, la terminología y el tono; todas las sesiones la reciben en su contexto. El skill de edición (`editar-articulo` / `edit-article`, §5.8) detecta repeticiones, huecos y notación inconsistente, redacta las transiciones y prepara los cambios que el editor pedirá a cada dueño. Las citas entran por el `pending.bib` de cada parte, y el editor las pasa a `bib/references.bib`.

**D-57. Uso individual (`mode: individual`).** Un proyecto puede ser de una sola persona: `project.yaml: mode: individual` (por defecto, `group`), que pone `init --individual`. `roles.yaml` tiene solo al responsable, que también es el editor (D-56); con más personas, `validate` da error hasta que se vuelve a `mode: group`, para que nadie relaje los controles por accidente. El flujo es el mismo, pero sin aprobación: el ruleset `aucove-base` no exige aprobaciones ni la revisión de code owners, `aucove/editor-review` pasa solo, y la persona integra en la web en cuanto pasan los checks requeridos. No hacen falta suplente ni bypass (D-17 no aplica). Para pasar a grupo se agregan personas, se cambia a `mode: group` y se actualiza el ruleset (06-permisos).

**D-33. Reglas y modos de `validate`.** (g) Los hallazgos `conv.*` de 02-convencion van en la regla `convention` (el mismo ID que el check de `doctor`); los que duplicarían `citations`, `labels-refs` y `latex-macros` son `E_VALIDATION_CITATION`, `E_VALIDATION_LABEL` y `E_VALIDATION_MACRO`. (i) El reporte de 09-ci §4.1 lleva `"schema": "aucove.result/v1"`, el valor de `result.schema.json`. (o) `build.timeout_minutes` vale de 1 a 12, en 03-esquemas, en `project.schema.json` y en `ci build-plan`. El modo `base` sin `--before` toma como estado anterior el primer padre (`HEAD^1`) de `--tree`; en un commit raíz no hay cambios, y `decisions-immutable` y `plan-ids` pasan. El modo `tree` acepta `--tree` (por defecto, el directorio actual) y lee la política con `git -C <tree> show origin/<base>:…`. Con `format: none` (D-43), `validate` da `skip` en `citations`, `labels-refs`, `latex-macros` y `latex-log`, y en las reglas `conv.latex.*`, `conv.main.*`, `conv.latexmkrc.*` y `conv.bib.*`; por cada parte exige `<id>.md`, `figures/` y `tables/`, y advierte `conv.format.unused` si hay `tex/`, `bib/`, `.latexmkrc` o `pending.bib`. Las líneas obligatorias de `.gitignore` y `.gitattributes` no cambian con el formato. REQ-CONV-26 (una oración por línea) no se verifica en Markdown. La regla `schemas` aplica además la matriz de cuentas de 10-seguridad §8.3 (`E_VALIDATION_ACCOUNT_TYPE`, D-48).

**D-36. `conv.tree.missing` filtrado por hito hasta H4.** La lista de archivos obligatorios es la de 02-convencion §2.2 filtrada por la columna «Hito» de 12-mvp §1.2, de la misma fuente que el manifiesto de REQ-MVP-4, y por `format` (con `none` no exige `tex/**`, `bib/**`, `.latexmkrc`, `pending.bib` ni `sections/<id>/<id>.tex`, y por cada parte exige `<id>.md`, `figures/` y `tables/`). Antes de H4 no exige `.mcp.json`, `.claude/**`, `.agents/**` ni `.devcontainer/**`, para que la plantilla pase su propio `validate`. Los globs de 02 §2.2 se expanden a rutas exactas con una tabla versionada. `.github/workflows/seal.yml` es obligatorio. El filtro por hito vence al cerrar H4, cuando la plantilla está completa.

**DC-15. Compilación:** `latexmk -norc` sin `-r` y sin `.latexmkrc`; se conserva `-Werror`.

### 3.5 Tareas y flujo de trabajo

**D-10. Reclamar = crear de forma atómica la ref `task/<n>`.** La asignación del Issue solo refleja el reclamo, y un workflow programado lo vence. No hay base de datos en v0.1. En la puerta web, el reclamo es un push con lease (D-41).

**D-11. El estado de una tarea se calcula** con una función pura sobre Issue, ref, PR y revisiones. Solo `state:proposed` y `state:expired` se guardan, como etiquetas.

**D-40. El reclamo atómico funciona.** S3 corrió 100 carreras válidas de 10 hilos en GitHub Actions con el `GITHUB_TOKEN`: en cada una hubo un solo ganador y la ref quedó en su commit; las perdedoras respondieron 409 o 422. Valida D-10 y `CT-REF-03`, y no hacen falta el modo `patch` ni `CT-REF-05`. Como 100 carreras solo acotan la tasa de fallo por debajo de un 3 % por carrera (95 % de confianza), el protocolo sigue siendo defensivo (04-tareas §2.3: 409 igual que 422 y GET después del 201).

**D-41. La puerta web escribe por git, no por la API.** S1 midió que, en una sesión web de Claude Code, el proxy responde 403 a toda escritura a la API de datos de git y a contents, mientras que la API REST de Issues y la de pulls funcionan, y `git push` funciona a cualquier rama (crear con `--force-with-lease=refs/heads/task/<n>:`, avanzar con avance rápido y empujar a ramas ajenas; el ruleset de REQ-PERM-52 rechaza el push forzado con `GH013`). Se decide: (1) en la puerta web, `write_mode=git` (08-forja REQ-FORGE-56): `claim` crea `task/<n>` con un commit de reclamo local y `git push --force-with-lease=refs/heads/task/<n>: origin <claim_sha>:refs/heads/task/<n>`; `save` y `update` empujan con avance rápido; `submit` y `record-assessment` usan la API REST de pulls e Issues, y el reflejo del Issue también va por REST; (2) fuera de la web, `claim` usa `POST git/refs` (D-10): el adaptador expone las dos vías y elige según la superficie; (3) `release` no está disponible en la puerta web en v0.1: devuelve `E_STATE_UNSUPPORTED_DOOR` con un `hint` que remite a la terminal o a la pestaña Code de Desktop, y los reclamos abandonados los vence `expire-claims`; (4) riesgos residuales (10-seguridad §2.3): cualquiera con permiso de escritura puede añadir commits con avance rápido a una `task/<n>` ajena, en `advisory` también forzar el push, y GitHub no distingue la sesión web de la persona; (5) S1 se repite antes de H4 y de H7 (D-22), con `CT-WEB-01` y `CT-PUSH-01` (08-forja §10.3).

**D-42. El reclamo por push es atómico.** S3-bis corrió 100 carreras válidas de 10 `git push --force-with-lease=<ref>:` simultáneos: en cada una hubo un solo push aceptado y la rama quedó en el commit del ganador. Valida la vía de D-41 (1) y la hipótesis H-TASK-6 de 04-tareas; como en D-40, el protocolo relee la ref después de un push aceptado (04-tareas §2.2). Su regresión es `CT-PUSH-01`.

**DC-03. Todo PR hacia la base sale de `task/<n>`.** Las reversiones se hacen dentro de una tarea, con `git revert`, y `decision propose` exige `task/<n>`. Una propuesta de dirección es un Issue con la etiqueta `direction`.

**DC-10. Commit de reclamo.** Lleva los trailers `Task` y `AI-Use: none`. Queda exento del check de trazabilidad porque su árbol es idéntico al de su padre, no por su asunto; los merges también quedan exentos.

### 3.6 Validación, CI y plantilla

**D-31. Operaciones del registro.** (a) `aucove ci pin-plan` es del MVP, porque lo usa el job `pin` de `validate.yml` (09-ci REQ-CI-45). (b) `aucove ci base-report` corre en `main-broken.yml`. (c) `aucove ci seal`, interno, y `aucove evidence verify` también son del MVP (D-50). §5.7 marca «sí» 34 entradas.

**D-32. Contratos internos del núcleo.** (c) Hay un solo `OpSpec`, que unifica 01-arquitectura §3.4 y 05-operaciones §2: `offline` toma los valores ASCII de 05 (`local`, `degraded`, `online`; D-02); no usa Pydantic, que no es dependencia directa del núcleo, y el `inputSchema` se genera de `Param`; `module` es relativo al paquete, para que el nombre literal solo esté donde lo permite REQ-ARCH-19. (d) `canonical()` y `classify()` viven en `aucove.domain.paths`. Como `domain` no hace E/S (REQ-ARCH-18), la detección de enlaces (`link_prefix`, REQ-CONV-10) y la grafía real (REQ-CONV-9) le llegan como argumentos. (e) El idioma se resuelve según REQ-ARCH-69, con `--lang` delante: `--lang`, `AUCOVE_LANG`, `project.yaml: language` de la base y `en`; nunca la configuración regional del sistema. (f) Los catálogos i18n se dividen por área (`i18n/catalogs/<lang>/<área>.toml`, cada uno de un solo paquete dueño) y se fusionan al cargar. Una clave repetida entre archivos es error de carga: `core.toml` solo tiene claves `error.*` y `warning.*`, y cada archivo de área, `finding.<código>.*` y `<área>.*`. El traductor nunca lanza ante un marcador ausente.

**D-34. Adaptador, `doctor`, `check-path` y `sync-roles`.** (h) `ForgeAdapter` (08-forja §2) incluye `pulls_for_commit(sha)` (`GET /repos/{o}/{r}/commits/{sha}/pulls`) y `last_successful_run(workflow_file, branch)` (`GET /repos/{o}/{r}/actions/workflows/{f}/runs?branch=…&status=success&per_page=1`), que necesita REQ-CI-33, y para `ci seal` (D-45), `successful_runs(workflow_file, branch, *, event, limit)` y el campo `PullRequest.merged_by`. `FakeForge` acepta comentarios de hasta 65 536 caracteres, como GitHub. `find_artifact` y `download_artifact`, que necesitará `evidence export`, quedan para v0.2. (l) `data_doctor.profile` no admite `null`: sin detección concluyente se informa `advisory` con `W_FORGE_CAPABILITY_UNKNOWN` (REQ-FORGE-14). (n) `check-path` sin identidad en caché deniega cada ruta con `E_PERM_NOT_A_MEMBER` y un motivo localizado; `sync-roles` sin identidad en caché falla al escribir con un error `E_AUTH_*` y su `hint`, y `--check` funciona igual.

**D-35. Archivos generados y licencias.** (j) `.github/CODEOWNERS` y `.github/ISSUE_TEMPLATE/task.yml` tienen origen «T, I, N» en 02-convencion §2.2: la plantilla los trae generados desde su `roles.yaml` de ejemplo, y la salida de `sync-roles` se suma a los generados de 01-arquitectura §2.3 y §10.5. La cabecera de `CODEOWNERS` es el texto fijo de 06-permisos §4.6, igual en `es` y en `en`; `task.yml` sí se localiza. (k) `schemas/` del framework va en Apache-2.0. Las fuentes de `AGENTS.md` y `CLAUDE.md` de la plantilla van en MIT-0 en el repo del framework, como pide REQ-TRACE-57; en el proyecto generado rige el `REUSE.toml` de 11-trazabilidad §7.2 (CC0-1.0). (m) El `sha256` de la línea `# source:` de `CODEOWNERS` (06 §4.6) se calcula sobre `roles.yaml` en forma canónica: sin BOM y con LF.

**D-37. Autoprueba del CI de la plantilla.** El CI del framework renderiza la plantilla y ejecuta los mismos comandos de sus workflows con el núcleo recién construido, la compilación en Docker y `FakeForge`, incluida la variante `format: none` y `seal.yml` (sus dos jobs, con una TSA local de prueba y un calendario OTS falso), y corre `aucove evidence verify` sobre los registros producidos (A17 a y b). La completan una prueba estructural sobre el render (cada `needs.*.outputs.*` citado existe, los artefactos se suben y se bajan con el mismo nombre, y los jobs requeridos usan `if: always()`) y `actionlint` sobre el render, no sobre las fuentes con `{{var:…}}`. Así se verifica que la plantilla pasa su propio CI sin depender de una Release publicada.

**D-38. Matriz del CI del framework.** Mientras el framework vivió en el alpha privado, los PR corrieron Ubuntu con Python 3.11 y 3.14 y Windows con 3.11, y macOS y la compilación TeX solo a mano (`workflow_dispatch`), porque en un repo privado los minutos de macOS y de Windows cuentan varias veces contra la cuota incluida (09-ci §9). (p) El repo público corre la matriz completa de REQ-ARCH-6, REQ-MVP-21 y T1 y T4 de 12-mvp §3.1.

**DC-04. Nombres de códigos de error.** Prevalece el catálogo de 05-operaciones §12.1 (por ejemplo, `E_PERM_LEAD_ONLY_PATH`, `E_PERM_SECTION_NOT_OWNED`, `E_PERM_GENERATED_PATH`, `E_CONFIG_POLICY_UNAVAILABLE` y `E_STATE_NOT_ON_TASK_BRANCH`).

**DC-05. Códigos de error y de hallazgo.** Los códigos de error de operación (catálogo cerrado de 05 §12.1) son distintos de los códigos de hallazgo (`findings[].code`, catálogo abierto por capítulo; §5.11).

**DC-14. Versión del núcleo en CI.** El pin se lee del `project.yaml` de la base. Un job aparte, sin permisos de escritura, corre `generated-sync`, `roles-sync` y `schemas` con el `core_pin` del PR cuando difiere y satisface el rango `core` de la base.

**DC-18. Revisión con modelo y disparadores privilegiados.** La revisión con modelo queda fuera de la plantilla v0.1; 09-ci §8 es especificación reservada, y las claves `ci.model_review.*` existen en el esquema, sin modelo por defecto. En SB06, `pull_request_target` solo se admite en `validate.yml`, `seal.yml` (D-45) y `editor-review.yml` (D-56; 09-ci §4.5), y `workflow_run` solo en `editor-review.yml`.

**DC-20. Gramática única de marcadores** (§5.14), con una sola excepción: `aucove:session` en las notas.

### 3.7 Seguridad y privacidad

**D-48. Privacidad de IA en la plantilla.** El `.claude/settings.json` de la plantilla fija `env` con `DISABLE_FEEDBACK_COMMAND=1` y `CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1`, así quedan desactivados `/feedback`, cuyo envío se conserva 5 años, y la encuesta de calidad, que si se responde «Yes» sube la transcripción con el código. Además, niega al agente la lectura de `.aucove/evidence/**`, `**/openings.json` y las carpetas de artefactos `evidence-*`. `data_policy.require_commercial` vale por defecto `true` si `disclosure` no es `open`, y `false` si lo es; la plantilla no escribe la clave, así que ese valor por defecto aplica a todo proyecto nuevo. Las cuentas de consumo (C1 de 10-seguridad §8.3) quedan fuera, salvo que el responsable escriba `require_commercial: false` y lo registre en una decisión. Se recomienda `forceLoginOrgUUID` en managed settings donde la institución lo permita, y se documenta `cleanupPeriodDays`.

**D-53. Una atestación de Bitcoin sin cotejar no fecha un registro.** Una prueba OpenTimestamps da `pending` si no tiene atestación de Bitcoin, y también si tiene la de un bloque que nadie cotejó (`bitcoin_unverified`): cualquiera puede escribir una atestación falsa, y v0.1 no coteja el bloque con un nodo ni con un explorador (D-50). Solo una TSA RFC 3161 lleva un registro a `verified`. Los registros de integración, que llevan dos TSA, pueden quedar en `verified`; los de entrega, que solo llevan OTS, quedan en `pending` hasta que alguien coteje a mano la altura del bloque y la raíz de Merkle con `VERIFY.md` (05-operaciones §9.26; 12-mvp A17 b).

### 3.8 Evidencia, trazabilidad y licencias

**D-07. Licencias.** Apache-2.0 para el framework. La plantilla, en MIT-0 para el código y CC0-1.0 para el texto de ejemplo, para no imponer licencia a la investigación. La documentación, en CC-BY-4.0, y las contribuciones, con DCO. `skills/` va en MIT-0 (REQ-TRACE-57), y cada `SKILL.md` generado lleva `license: MIT-0` en el frontmatter; `governance/`, en CC0-1.0. (a) `pyproject.toml` declara `license = "Apache-2.0 AND MIT-0 AND CC0-1.0"` y `license-files` con `LICENSES/*.txt`. (b) Cada desarrollador puede confirmar con su institución quién es el titular de su código (en México, art. 103 LFDA), y entonces se ajustan `NOTICE` y `SPDX-FileCopyrightText` (11-trazabilidad REQ-TRACE-73); esa confirmación no condiciona la publicación. (c) El check de DCO (REQ-TRACE-66) exime los PR que la API de GitHub atribuye a `dependabot[bot]` (el usuario que abre el PR, no el correo del commit), y quien fusiona responde por ese cambio. (d) El extra opcional `evidence` (D-45) depende de `opentimestamps` y `python-bitcoinlib` (LGPL-3.0-or-later) y de `rfc3161-client` (Apache-2.0); se instalan aparte, no se vendorizan ni se incluyen en la rueda, así que la licencia del paquete no cambia. `NOTICE`, el SBOM y `requirements-lock.txt` los listan.

**D-44. Apertura por hitos.** `project.yaml: disclosure` = `private` | `milestones` | `open`; si falta, vale `milestones`, y con `open` el esquema exige `data_policy.level: public`. El repo es privado y lleva sellos automáticos (D-45). Ningún contenido pasa a público sin una decisión en `memory/decisions/` que cumpla la checklist de 11-trazabilidad §8.9, que el responsable sigue en este orden: (1) si algo puede ser patentable, primero se presenta la solicitud ante el IMPI, porque cada apertura es una divulgación (LFPPI art. 52); (2) se revisan los datos personales y las condiciones del comité de ética; (3) el financiador, el convenio y la titularidad institucional (LFDA art. 84); (4) la política de preprints de la revista destino; (5) el consentimiento de todos los autores; (6) los correos: toda la historia usa las direcciones noreply de GitHub desde el día 1, y la historia sellada no se reescribe, así que si hay correos personales solo se abre una instantánea o el preprint. Después vienen el preprint, el espejo público, el release en Zenodo (su DOI de concepto identifica el proyecto) y Software Heritage. El espejo es un repo nuevo con la historia hasta la etiqueta del hito, sin Issues, PR ni comentarios, porque esos llevan aperturas. Con `open`, la checklist se aplica antes de hacer público el repo. En v0.1 la apertura es manual; `aucove evidence disclose` queda reservada para v0.2.

**D-45. Sellado de evidencia.** *Qué se sella:* un manifiesto `evidence-manifest/v1` en JSON canónico (RFC 8785, perfil restringido de 11-trazabilidad §8.3), con compromisos con sal y separación de dominio: (a) por archivo, uno de la ruta (los bytes exactos que da git) y otro del contenido (el blob de git, nunca el disco); (b) por actor (autor del PR, quien integró, y autor y committer del commit), uno de su identidad, para atar la entrega a una cuenta sin publicarla. Las sales, de 32 bytes y una por compromiso, van en un archivo privado de aperturas (`evidence-openings/v1`). *Cuándo:* (1) **entrega:** en cada cabeza de un PR de tarea, con OpenTimestamps, para que la investigadora tenga evidencia aunque el responsable no apruebe; (2) **integración:** en cada push a la base, con dos TSA RFC 3161 del catálogo más OpenTimestamps; el manifiesto cubre desde el último commit ya sellado hasta el nuevo, así que si un push no se pudo sellar, lo cubre el siguiente; (3) **plan:** el registro de integración lleva `plan: true` cuando su rango toca `plan.md`, `roles.yaml` o `memory/constitution.md`, o cuando es el primero del repo (al estilo de un prerregistro). *Dónde corre:* en `seal.yml`, con dos jobs: `submission`, en `pull_request_target` (la definición sale de la rama por defecto y el PR se lee como datos, REQ-CI-29), e `integration`, en `push` a la base. La concurrencia es por SHA y no cancela runs pendientes; los jobs tienen `contents: read` y no escriben en ninguna rama. No va en `main-broken.yml`, cuya concurrencia cancela runs pendientes y cuyo «último SHA en verde» (REQ-CI-33) pasaría a depender del sellado, ni en `build.yml`, cuya definición controla quien abre el PR. No hay interruptor en `project.yaml`: `seal.yml` es obligatorio (`conv.tree.missing`) y de clase `lead`, así que quitarlo exige la aprobación del suplente. *Dónde se guarda:* (a) en el artefacto `evidence-<kind>-<sha>-a<intento>` (400 días en repos privados; `init` fija esa retención): los bytes exactos del manifiesto, las aperturas, el `.ots`, los `.tsr`, la cadena y la raíz de cada TSA y su OCSP o CRL, con `VERIFY.md`; (b) en un comentario `ci:evidence` del PR, que la investigadora ve: el registro `evidence-record/v1`, con el manifiesto, las aperturas, las pruebas y el OCSP o CRL, salvo lo que no quepa; (c) en una copia local en `.aucove/evidence/`, que `status` y `submit` hacen de los PR propios. Un commit sin PR solo tiene el artefacto, y la custodia es la de D-46. *Catálogo de TSA:* vive en el núcleo y cambia con su versión; empieza con `freetsa` y `sectigo`, y DigiCert entra solo si cumple los criterios de PA-105. Guarda la cadena de cada certificado que tuvo cada TSA, para verificar pruebas viejas aunque venza el artefacto. *Horas:* en UTC, y los reportes agregan la del centro de México (`America/Mexico_City`; `--tz` la cambia). No se usan Sigstore público ni las atestaciones de GitHub, porque Rekor publicaría para siempre la identidad del workflow y el nombre del repo. `aucove evidence verify` compara el manifiesto con cada sello y las aperturas con git o con los archivos, sin regenerar el manifiesto.

**D-46. Custodia y exportación.** En cada hito, y como mínimo al cerrar el proyecto, el responsable arma un paquete de evidencia y lo guarda en al menos dos custodios fuera de GitHub: por ejemplo, el repositorio o almacenamiento institucional, en acceso cerrado, y la copia del responsable. Sin repositorio institucional, los custodios recomendados son el registro cifrado de OSF (D-47); Harvard Dataverse, con un solo DOI por proyecto, cada hito como versión, archivos restringidos, título neutro y el paquete cifrado como en D-47; y la copia del responsable. Zenodo no se recomienda para la custodia (un DOI por versión y metadatos públicos): queda para los releases públicos (11-trazabilidad §8.9). El acuerdo de autoría nombra a los custodios y a su suplente. En v0.1 el paquete se arma a mano con la guía de 11-trazabilidad §8.8 e incluye: (a) los artefactos `evidence-*` (`gh run download`); (b) los comentarios `ci:evidence` (`gh api`); (c) un `git bundle` con `refs/pull/*/head`, porque las cabezas de los PR no integrados solo viven ahí; (d) un `index.txt` con la huella SHA-256 de cada archivo. `VERIFY.md` viene en cada registro. Los artefactos del CI no son custodia: vencen a los 400 días como máximo. Cada persona tiene derecho a exportar sus propias pruebas en cada hito y antes de salir del equipo: su copia de `.aucove/evidence/`, los artefactos de sus PR y los archivos de su parte en cada registro (`git archive`), sin el bundle ni contenido de otras personas; la baja de 10-seguridad §9.1 ofrece esta exportación antes de quitar el acceso. El paquete contiene datos personales (logins, nombres, correos y horas); su base la fija la oficina jurídica, y se propone la ejecución del acuerdo de autoría y la defensa de derechos, con el consentimiento como transparencia, para que una revocación no obligue a destruir registros que prueban la autoría de otras personas. Su plazo de conservación es propio: por defecto, 10 años desde el cierre del proyecto, distinto del de D-28. Si se purga un archivo (10-seguridad §10.2), los custodios regeneran el paquete. Volver a sellarlo cada 5 años o al cambiar de algoritmo (RFC 4998) es un paso manual en v0.1, y `aucove evidence export` queda reservada para v0.2.

**D-47. Depósito cifrado de hitos en OSF (procedimiento manual).** Según 11-trazabilidad §8.9: (1) armar el paquete de evidencia (D-46); (2) cifrarlo con age (o GPG) para al menos dos destinatarios, el responsable y la institución (o, sin institución, el suplente del responsable), que custodian sus propias llaves; (3) crear un registro de OSF con embargo (máximo 4 años) que solo contenga el archivo cifrado y un título neutro; (4) registrar en la decisión del hito el identificador del registro, la fecha de fin del embargo y el SHA-256 del archivo cifrado; (5) agendar una revisión antes del fin del embargo. Como el paquete va cifrado, el fin del embargo no revela nada. OSF dejó de admitir proyectos nuevos, pero se pueden crear registros sin proyecto, y los embargados no cambian. `aucove evidence deposit` queda reservada para v0.2; será solo humana y con token local, porque REQ-CI-3 impide hacerlo desde el CI.

**D-49. Terminología y límites legales.** La spec, los mensajes y las guías dicen «fecha verificable» o «prueba de existencia en una fecha», nunca «fecha cierta», que en México es un término jurídico con requisitos propios. Los sellos prueban fecha e integridad, no autoría ni titularidad. La autoría descansa en un acuerdo de autoría y de apertura firmado al inicio del proyecto, en CRediT (11-trazabilidad §6) y en los registros de la forja. El acuerdo fija: (a) el orden y los criterios de autoría; (b) los custodios; (c) las reglas de apertura; (d) la exportación personal; (e) la base y el plazo de conservación del paquete. La huella SHA-256 del acuerdo DEBERÍA anotarse en `memory/constitution.md`, para que el primer registro con `plan: true` la selle sin publicar el acuerdo. La NOM-151 es opcional, por hito y sobre el paquete completo, y solo después de consultar a la oficina jurídica o de transferencia: su marco es mercantil (CCom art. 101 fr. IV) y el prestador no da fe pública (art. 100). La titularidad (LFDA art. 84 y normativa de la institución) se consulta antes de abrir o de registrar en el IMPI o el INDAUTOR.

**D-50. Alcance de la evidencia en v0.1 y v0.2.** v0.1 incluye: (a) partes, `format: none` y el retiro de `openin_any` (D-43); (b) los esquemas y valores por defecto de D-44 y D-48, con `check_accounts` (REQ-SEC-64); (c) `aucove ci seal`, con `seal.yml` en la plantilla; (d) `aucove evidence verify`, con su kit del árbitro; además, `init --format` y `--disclosure` con la retención de 400 días, la copia local que hacen `status` y `submit`, `successful_runs` y `merged_by` en el adaptador, el check `identity` de correos noreply en `doctor`, el lint IL05 e IL08 sobre el Markdown de las partes, y el `env`, las reglas de lectura y las guías de D-44 a D-49 con el modelo de acuerdo de autoría. Quedan para v0.2: `evidence export`, `evidence deposit` y `evidence disclose`; la verificación de Bitcoin con nodo propio o con exploradores; la actualización programada de las pruebas OTS; y el re-sellado (RFC 4998) y la constancia NOM-151. Nunca se recortan el sellado de la entrega y de la integración, los compromisos de actores, la cadena y el OCSP o CRL de cada TSA, la verificación ni el kit del árbitro.

**D-64. Las licencias de `init` (11-trazabilidad §7.3).** (1) `LICENSES/` lleva exactamente los textos que usan los archivos según el `REUSE.toml` que genera `init`, porque REUSE falla tanto con un texto sin uso como con el que falta. `LICENSES/MIT-0.txt` queda mientras haya archivos bajo MIT-0, y el texto de una categoría que todavía no tiene archivos se agrega con su primer archivo: `validate` solo lo pide entonces (REQ-TRACE-61 y REQ-TRACE-64). (2) El paquete trae los textos de las licencias de la tabla de §7.3 y una copia de la lista de licencias de SPDX (CC0); para otra licencia, `init` pide en la terminal la ruta de su texto y, sin ella, falla antes de escribir nada (REQ-TRACE-62).

### 3.9 Pruebas y validación empírica

**D-22. Pruebas empíricas S1 y S3.** **S1** (`CT-WEB-01`): un repo público de prueba con la Claude GitHub App, con y sin ruleset en `task/*`, más la variante de una sesión abierta desde `task/<n>`, con una regla de tres salidas fijada de antemano: (i) si la escritura por API pasa, la puerta web es completa; (ii) si solo pasa `git push` a `task/<n>`, la web escribe por git (resultado en D-41); (iii) si no pasa nada, la web es de solo consulta en v0.1 (04-tareas §11.4). S1 se repite antes de H4 y antes de H7. **S3** (`CT-REF-02`, `CT-REF-03`): en GitHub Actions con el `GITHUB_TOKEN`, 100 carreras válidas de 10 hilos (resultado en D-40).

**D-23. Identidades de prueba.** Una org de pruebas en Free (nombre por `AUCOVE_CONTRACT_REPO`, 08-forja §10.2), con `ct-public` (`enforced`) y `ct-private` (`advisory`). Papeles: `lead`, una cuenta de máquina, owner de la org; `researcher-a` y, opcional, `researcher-b` (D-67). No se usan GitHub Apps para los papeles, porque no pueden ser code owners. Custodia: 2FA con la semilla en un gestor de contraseñas, correo propio y tokens de 90 días o menos.

**D-24. Org de pruebas `enforced` en privado.** El perfil `enforced` en un repo privado (`CT-CAP-01`, REQ-PERM-53) se prueba en `ct-private-team`, dentro de una org en GitHub Team **separada** de la org de pruebas en Free, porque el plan es por organización y pasar esa org a Team dejaría sin repo privado en Free a `CT-CAP-02` y a la matriz `advisory`. Las cuentas de prueba necesitan un segundo juego de tokens fine-grained para esa org (12-mvp §3.3). Una política de Actions «solo acciones de repositorios de la org» hace fallar `actions/checkout`: es un caso real para las pruebas de detección de D-39, y hay que ajustarla (06-permisos §4.3) antes de correr el CI de la plantilla ahí.

**D-67. Validación con dos identidades.** Las pruebas con GitHub real (T2, y T3 y T7 cuando tengan arnés) corren con dos cuentas: `lead` y `researcher-a`, que puede ser una cuenta personal, miembro sin administración y con tokens fine-grained limitados a `ct-public` y `ct-private`. `researcher-b` es opcional: si existe, corren también las pruebas que necesitan tres identidades distintas a la vez, como un PR con revisiones de dos cuentas distintas de la del autor; si no, esas pruebas se omiten con la forja real y quedan cubiertas por el falso. Sin `researcher-b`, `researcher-a` hace de `deputy`. El framework no depende del número de personas (con una, `mode: individual`, D-57): el mínimo de tres era de las pruebas (REQ-MVP-28 y REQ-MVP-29).

**D-68. Conductas de GitHub medidas por T2 real.** La suite de contrato contra GitHub real pasa todo lo aplicable en `ct-public` y `ct-private`. Midió cuatro conductas que el falso reproduce (REQ-FORGE-63; 08-forja §4.2, §4.5 y §10.2): (a) en un repo público, GitHub abre un Issue aunque el token no tenga el permiso Issues; en uno privado, no; (b) al volver a agregar una etiqueta presente, GitHub a veces registra otro `labeled`, así que quien la vuelve a poner puede pasar a ser su autor para las reglas que miran el último `labeled` (04-tareas §1.3); es lo mismo que quitarla y ponerla, así que el modelo de amenazas no cambia, y el falso lo registra siempre, el caso más exigente; (c) borrar con `commit_files` una ruta que no existe da 422; (d) las listas (Issues, eventos y actividad de refs) y la cabeza de un PR después de mover su rama tardan unos segundos en reflejar una escritura; (e) en un repo privado, sin permisos, el 403 llega en la primera petición del método y la cabecera trae solo su permiso: `create_empty_commit` y `ensure_labels` informan el de lectura, y `get_checks`, solo `actions=read`. `CT-GIT-03` midió que, con un token sin Workflows, `POST merges` acepta traer de la base un cambio en `.github/workflows/` y `commit_files` con `extra_parents` da 422. La suite prepara el sandbox: siembra los archivos base, crea el ruleset `ct-sandbox-base` en `ct-public` y usa una rama fija con un workflow para `CT-GIT-03` (08-forja §10.2).

### 3.10 Plan del MVP y piloto

**D-20. Equipo y calendario.** Desarrollan 2 personas, con el calendario de hitos de 12-mvp §4.

**D-21. Coordinación del piloto.** Quien coordina el piloto custodia la sal (REQ-MVP-54, REQ-MVP-58) y no puede ser `lead` de ningún grupo del piloto (REQ-MVP-58). Si además desarrolla el framework, el informe declara el posible sesgo y, cuando sea posible, las entrevistas de línea base y el análisis cualitativo los hace otra persona (12-mvp REQ-MVP-68).

**D-26. Plan de GitHub y asientos de Claude del piloto.** La org del piloto es otra org, distinta de las de pruebas, en GitHub Team: pagado o, si hay un docente o investigador verificado, gratis vía GitHub Education (mejorando la org desde el panel de Education, nunca desde facturación). Permiso base de los miembros: ninguno. Repos privados en `enforced`; el dogfooding de H5 DEBE hacerse en un repo `enforced` (12-mvp REQ-MVP-63). Para Claude, el PI solicita el plan Team para científicos si su disciplina califica; el respaldo es Team pagado o, solo si el `project.yaml` del grupo declara `data_policy.require_commercial: false` y el responsable lo registra en una decisión, una cuenta de consumo (D-48). Los mantenedores y los probadores de «primer día» de H4 necesitan asientos con Claude Code y sesiones en la nube. `advisory` es el último recurso y no es gratis: exige un método de pago para los minutos de Actions (09-ci §9), y en él M11 solo se reporta. En Team y Enterprise de Claude, `/web-setup` está oculto, así que un owner de GitHub instala la Claude GitHub App en los repos del piloto (12-mvp §4.1, #5).

**D-27. Ventana del piloto.** G-T del 01/02 al 05/03/2027 y G-NT del 05/04 al 07/05/2027, provisionales para la UNAM y el IPN. M5 y M13 se miden sobre semanas activas del calendario del piloto, sin vacaciones ni días inhábiles. El piloto no depende de OSF.

**D-28. Retención de los datos del piloto, por niveles,** contada desde el fin de la recogida de datos de cada grupo (12-mvp REQ-MVP-65 a REQ-MVP-67): audio o video, se borra como máximo 30 días después de transcribirlo; datos seudonimizados (el CSV de `aucove metrics`, las respuestas individuales y las transcripciones) y la sal, hasta el informe y como máximo 12 meses; al vencer, se destruye la sal, se quitan del CSV el repo, los números de Issue y de PR y las fechas exactas, se borran los originales y se levanta un acta; anonimizados y agregados, 5 años o lo que diga la institución; los consentimientos se guardan aparte. El consentimiento declara que el marcador `hook_denials` de los PR, los Issues y los PR, y `.aucove/log/events.jsonl` en las computadoras, no se destruyen. Ley aplicable: la Ley General si la institución es pública federal, la LFPDPPP si es privada y el RGPD si hay participantes de la UE. Los registros de evidencia (comentarios `ci:evidence`, artefactos `evidence-*`, copias en `.aucove/evidence/` y paquetes de evidencia) no son datos del piloto: tienen su propia base y su propio plazo (D-46), no se destruyen con la sal, y el consentimiento lo declara (12-mvp REQ-MVP-67 e).

**D-30. Encuesta de diagnóstico previa.** Una encuesta anónima sobre la adopción de IA, previa al piloto: no es línea base ni dato del piloto (REQ-MVP-57), no se enlaza con los seudónimos y no se publica sin su propia aprobación o exención. El formulario de contacto solo recoge «quiero información»; el cribado de participantes va después de la aprobación ética. Es un instrumento previo, no normativo (12-mvp §5.4).

## 4. Glosario

| Término (es) | Identificador | Definición |
|---|---|---|
| Proyecto (de investigación) | `project` | Repositorio creado desde la plantilla que reúne los resultados de las partes y, con `format: latex`, un manuscrito. |
| Responsable | `lead` | Persona que dirige el proyecto, aprueba e integra, y es dueña de los archivos de clase `lead`. Hay exactamente una. |
| Suplente | `deputy` | Persona opcional que puede aprobar los PR del responsable y reemplazarlo en revisiones. |
| Editor del artículo | `editor` | Con `format: latex`, persona que cuida que el artículo se lea como un solo texto: es dueña de los archivos de clase `editor`, escribe la guía del artículo y los enlaces entre partes, pide cambios a los dueños y aprueba los PR que tocan el LaTeX. Si no se declara, lo es el responsable (D-56). |
| Sección del artículo | `article section` | Apartado del manuscrito; por defecto hay 7 (D-55). Agrupa una o más partes, con el título, el orden y el texto de enlace de `tex/outline/<id>.tex` (D-56). |
| Guía del artículo | `article guide` | `tex/guide.md`: qué cubre cada sección y cada parte, la notación, la terminología y el tono. La escribe el editor y la reciben todas las sesiones (D-56). |
| Investigador | `researcher` | Persona colaboradora. Reclama tareas y es dueña de sus partes. |
| Persona | `person` | Entrada de `roles.yaml`, identificada por `login` (usuario de la forja). |
| Parte (en la prosa de v0.1, también «sección») | `section` | Unidad del proyecto con **un** dueño. Su carpeta, `sections/<id>/`, reúne texto, código, datos derivados, figuras, tablas y notas. `id` en kebab-case ASCII. El texto visible dice «parte» (`es`) o «part» (`en`), y la carpeta conserva el identificador estable (D-43). |
| Tarea | `task` | Issue de la forja con la etiqueta `task`. Su identificador es el número del Issue (`<n>`). |
| Reclamo | `claim` | Existencia de la ref `refs/heads/task/<n>`, creada por quien reclama. |
| Entrega | `submission` | PR de la tarea marcado como listo para revisión. |
| Revisión | `review` | Revisión del PR en la forja: aprobar o pedir cambios. |
| Integración | `merge` | Fusión del PR en la rama base. Solo la hace un humano, en la web de la forja. |
| Decisión | `decision` | Registro inmutable en `memory/decisions/`. |
| Nota de sesión | `session note` | Resumen del trabajo de una tarea en `memory/notes/task-<n>.md`. |
| Constitución | `constitution` | Principios, estilo y reglas del proyecto. Enlaza al plan y a los roles, no los copia. |
| Plan / dirección | `plan` | `plan.md`: preguntas, hipótesis, alcance y métodos, con IDs estables. |
| Traslape | `overlap` | Dos tareas que abordan el mismo alcance o se contradicen. |
| Coordinador | `coordinator` | Conjunto de verificaciones preventivas y detectivas (diseño §11). En v0.1 **no** es un proceso aparte. |
| Puerta | `door` | Cliente por el que una persona trabaja (Claude Code, Codex, web de la forja...). |
| Núcleo | `core` | El paquete Python `aucove`. |
| Forja | `forge` | Plataforma de repositorios (GitHub en v0.1). |
| Perfil de capacidades | `capability profile` | `enforced` o `advisory`, según lo que la forja pueda exigir. |
| Convención | `convention` | Estructura y reglas del repositorio. Versión en `project.yaml: convention`. |
| Operación solo humana | `human-only operation` | Operación que un agente no puede ejecutar (D-09). |
| Fecha verificable | — | Prueba, comprobable por cualquiera con sellos de terceros, de que un contenido existía en una fecha y no cambió. No es «fecha cierta» en sentido jurídico (D-49). |
| Manifiesto de evidencia | `evidence manifest` | JSON canónico con compromisos con sal de archivos y actores; es lo que se sella (D-45). |
| Apertura (de un compromiso) | `opening` | Ruta o identidad y su sal, que abren un compromiso. Es privada. |
| Registro de evidencia | `evidence record` | Manifiesto, aperturas y pruebas (`.ots`, `.tsr`, cadena, OCSP o CRL) de una entrega o de una integración. |
| Paquete de evidencia | `evidence package` | Conjunto de registros, bundle e índice que se guarda con los custodios (D-46). |
| Custodio | `custodian` | Persona o institución que guarda paquetes fuera de GitHub (D-46). |
| Política de apertura | `disclosure` | `private`, `milestones` u `open` (D-44). |
| Acuerdo de autoría | `authorship agreement` | Acuerdo firmado al inicio sobre autoría, apertura, custodia, exportación y conservación (D-49). |

## 5. Registro canónico de identificadores

### 5.1 Nombre

| Uso | Valor |
|---|---|
| Distribución PyPI y paquete importable | `aucove` |
| Binario de la CLI | `aucove` |
| Nombre del servidor MCP | `aucove` (Claude Code expone las herramientas como `mcp__aucove__<tool>`) |
| Directorio de caché local (en `.gitignore`) | `.aucove/` |
| Prefijo de variables de entorno | `AUCOVE_` |
| Variables de entorno | `AUCOVE_LANG`, `AUCOVE_OFFLINE`, `AUCOVE_LOG`, `AUCOVE_HOOK_TRACE`, `AUCOVE_GITHUB_TOKEN`, `AUCOVE_FORGE_TRANSPORT`, `AUCOVE_FORGE_WRITE_MODE`, `AUCOVE_FORGE_ADAPTER`, `AUCOVE_FORGE_FAKE_STATE`, `AUCOVE_TESTING`, `AUCOVE_CONTRACT_REPO`, `AUCOVE_AI_TOOL`, `AUCOVE_AI_MODEL`, `AUCOVE_METRICS_SALT`; reservada: `AUCOVE_MODEL_REVIEW` (variable de repositorio) |
| Esquemas JSON (`$id`) | `https://aucove.invalid/schemas/<nombre>/v1` |
| Nombres derivados | `aucove-template`, `proyecto-test`, `proyecto-test-team` (org Team de pruebas, D-24), `aucove-demo`, `demo-seed` |
| Remoto git canónico | `origin` |

El nombre es `aucove` (D-03). DEBE vivir en un único módulo del código (01-arquitectura §3.2) y en `pyproject.toml`, y cambiarlo NO DEBE requerir cambios de lógica. La org de pruebas conserva el nombre con el que se creó, `proyecto-test-13`; puede renombrarse, porque las pruebas no fijan su nombre (D-23).

### 5.2 Archivos y rutas de la plantilla

Clases de ruta (qué significa cada una: 02-convencion; quién puede hacer qué con cada clase: 06-permisos):

- `lead`: solo la modifica el responsable (o el suplente, en su nombre).
- `section`: la modifica el dueño de la parte.
- `editor`: la modifica el editor del artículo o, si no se declara, el responsable (D-56). Solo existe con `format: latex`.
- `append`: cualquiera PUEDE **agregar** archivos nuevos; nadie modifica ni borra los existentes.
- `task`: la modifica quien tiene el reclamo de la tarea `<n>`.
- `generated`: la genera el núcleo; nadie la edita a mano.
- `local`: está en `.gitignore`; nunca se versiona.

| Ruta | Clase |
|---|---|
| `AGENTS.md`, `CLAUDE.md` | `lead` |
| `project.yaml` | `lead` |
| `roles.yaml` | `lead` |
| `plan.md` | `lead` |
| `memory/constitution.md` | `lead` |
| `memory/decisions/<archivo>.md` | `append` |
| `memory/notes/task-<n>.md` | `task` |
| `tex/main.tex`, `tex/preamble.tex`, `tex/notation.tex`, `tex/outline/<id>.tex` y `tex/guide.md` (solo con `format: latex`) | `editor` (D-56) |
| `sections/<section-id>/**` (`<id>.tex` y `pending.bib` con `format: latex`, o `<id>.md` con `format: none`; `provenance.yaml`, `figures/`, `tables/`, `analysis/`, `notes/`; detalle en 02-convencion §4.2) | `section` |
| `bib/references.bib` (solo con `format: latex`) | `editor` (exportado del grupo de Zotero; proceso en 02-convencion; D-56) |
| `.github/CODEOWNERS` | `generated` |
| `.github/workflows/**`, `.github/ISSUE_TEMPLATE/config.yml`, `.github/pull_request_template.md` | `lead` |
| `.github/ISSUE_TEMPLATE/task.yml` | `generated` (lo regenera `sync-roles` desde `roles.yaml`) |
| `.claude/settings.json` | `lead` (contenido base generado por el núcleo) |
| `.claude/skills/**`, `.agents/skills/**` | `generated` |
| `.mcp.json`, `.gemini/settings.json`, `.codex/**`, `opencode.json`, `.opencode/**`, `.agents/**` salvo `.agents/skills/**` | `lead` (contenido base generado; contenido: 07-hooks; render: 01-arquitectura) |
| `.latexmkrc` (solo con `format: latex`), `.devcontainer/**`, `.gitignore` | `lead` |
| `README.md`, `.gitattributes`, `LICENSE`, `LICENSES/**`, `REUSE.toml`, `data/README.md`, `tex/assets/**`, `memory/decisions/.gitkeep`, `memory/notes/.gitkeep`, `records/approvals/**` (reservado) | `lead` |
| `.claude/settings.local.json` | `local` (reglas deny por persona generadas por el núcleo) |
| `CLAUDE.local.md`, `.claude/worktrees/**`, `tex/build/**`, `tex/build-diff/**`, `data/<id>/` | `local` |
| `.aucove/` | `local` (árbol en 01-arquitectura §7.3; compartido por todos los worktrees; incluye la copia local de evidencia `.aucove/evidence/`, que no es caché (D-45)) |

Toda ruta que no coincida con esta tabla es de clase `lead` por defecto.

### 5.3 Ramas

- **Rama base:** `project.yaml: forge.base_branch`, por defecto `main`.
- **Rama de tarea:** `task/<n>`, donde `<n>` es el número del Issue. Es fija, sin sufijo descriptivo; la descripción va en el título del PR.
- **Refs de archivo:** `refs/tags/archive/task-<n>-<k>` (`k` = 1, 2…), con el trabajo de una tarea liberada o cancelada (04-tareas §4.1).
- Todo PR hacia la rama base DEBE tener como cabeza `task/<n>` (D-12); v0.1 no tiene otras ramas de trabajo.

### 5.4 Etiquetas de la forja

| Etiqueta | Significado |
|---|---|
| `task` | El Issue es una tarea del framework. |
| `direction` | Issue de propuesta de cambio de dirección; la pone `propose_direction`. |
| `state:proposed` | Tarea propuesta, pendiente de que el responsable la acepte. |
| `state:expired` | Reclamo vencido por inactividad. |
| `flag:overlap` | Posible traslape detectado (consultivo). |
| `flag:direction-change` | El PR podría cambiar la dirección de forma implícita (consultivo). |
| `flag:blocked` | La tarea está bloqueada por otra o por una decisión. |
| `flag:bypass` | Integrado sin cumplir la matriz de aprobación. |
| `flag:instructions` | El lint encontró posibles instrucciones ocultas (consultiva). |
| `ack:direction` | El responsable reconoció el cambio de dirección. |
| `ack:overlap` | `lead` o `deputy` confirma que un traslape señalado es real; quitar `flag:overlap` sin ella significa «descartado» (semántica en 04-tareas). |
| `main-broken` | Issue automático: la rama base no compila o no pasa validación. |
| `review:model` | Reservada: pedido de revisión con modelo. |

`init` crea todas las etiquetas de esta tabla salvo las reservadas (06-permisos §4.5).

### 5.5 Estados de una tarea (calculados; ver 04-tareas)

`proposed`, `open`, `claimed`, `in_review`, `changes_requested`, `merged`, `expired`, `cancelled`.

### 5.6 Roles y perfiles

- Valores de `role` en `roles.yaml`: `lead`, `researcher`. El suplente se declara con la clave de nivel superior `deputy` (un `login`), y el editor del artículo, con `editor` (un `login`; si falta, es el responsable; solo con `format: latex`; D-56).
- Perfiles de capacidades: `enforced`, `advisory`.
- Modos de proyecto (`project.yaml: mode`): `group`, por defecto, e `individual` (D-57).

### 5.7 Operaciones

H = operación solo humana (D-09): no se expone por MCP y en la CLI exige TTY y confirmación. "—" = no se expone por MCP.

| CLI | Herramienta MCP | Actor | H | v0.1 |
|---|---|---|---|---|
| `aucove doctor` | `check_environment` | cualquiera | | sí |
| `aucove init` (incluye `init --regenerate`) | — | `lead` | H | sí |
| `aucove sync-roles` | — | `lead` / `deputy` | | sí |
| `aucove validate` | `validate_project` | cualquiera / CI | | sí |
| `aucove status` | `get_status` | cualquiera | | sí |
| `aucove context` | `get_context` | cualquiera (lo usa SessionStart) | | sí |
| `aucove check-path <ruta>` | `check_path` | cualquiera (lo usan los hooks) | | sí |
| `aucove metrics` | — | cualquiera | | sí |
| `aucove evidence verify <ruta>` | — | cualquiera (también fuera de un proyecto) | | sí |
| `aucove task propose` | `propose_task` | cualquiera | | sí |
| `aucove task accept <n>` | — | `lead` / `deputy` | H | sí |
| `aucove task cancel <n>` | — | `lead` / `deputy` | H | sí |
| `aucove claim <n>` | `claim_task` | `researcher` / `lead` | | sí |
| `aucove release <n>` | `release_task` | dueño del reclamo / `lead` | | sí |
| `aucove save` | `save_work` | dueño del reclamo | | sí |
| `aucove update` | `update_branch` | dueño del reclamo | | sí |
| `aucove switch <n>` | `switch_task` | dueño del reclamo | | sí |
| `aucove record-assessment <n>` | `record_overlap_assessment` | sesión de quien reclama o entrega | | sí |
| `aucove submit` | `submit_task` | dueño del reclamo | | sí |
| `aucove expire-claims` | — | CI (programado) / `lead` / `deputy` | | sí |
| `aucove decision propose` | `propose_decision` | cualquiera | | sí |
| `aucove direction propose` | `propose_direction` | cualquiera | | sí |
| `aucove review list` | `list_reviews` | `lead` / `deputy` / dueños | | sí |
| `aucove review show <pr>` | `prepare_review` | `lead` / `deputy` / dueños | | sí |
| `aucove review approve <pr>` | — | `lead` / `deputy` | H | sí |
| `aucove review request-changes <pr>` | — | `lead` / `deputy` / dueño | H | sí |
| `aucove hook <evento>` | — | la puerta (interno) | | sí |
| `aucove mcp --stdio` | — | la puerta (interno) | | sí |
| `aucove ci build-plan` | — | CI (interno) | | sí |
| `aucove ci comment` | — | CI (interno) | | sí |
| `aucove ci base-report` | — | CI (interno) | | sí |
| `aucove ci audit` | — | CI (interno) | | sí |
| `aucove ci pin-plan` | — | CI (interno) | | sí |
| `aucove ci seal` | — | CI (interno) | | sí |
| `aucove ci editor-review` | — | CI (interno) | | sí (desde H3; D-56) |
| `aucove ci review-context` | — | CI (interno) | | reservado |
| `aucove upgrade` | — | `lead` | | reservado |
| `aucove report ai-use`, `aucove report credit` | — | `lead` | | reservado |
| `aucove evidence export`, `aucove evidence deposit`, `aucove evidence disclose` | — | `lead` / cualquiera (`export --mine`) | H (`deposit`, `disclose`) | reservado (v0.2) |
| `aucove section read`, `aucove section propose-change` | `read_section`, `propose_section_change` | cualquiera | | reservado (modo completo) |

`aucove hook <evento>` (incluido el valor interno `refresh`), `aucove mcp --stdio` y `aucove ci <subcomando>` son internos: no emiten el sobre de resultado. El contrato de `ci` lo define 09-ci. `evidence verify` no tiene herramienta MCP: las aperturas son privadas y no deben entrar al contexto de un modelo (D-45, 05-operaciones REQ-OPS-65).

Las herramientas MCP siguen la forma `snake_case` verbo_sustantivo. Las marcadas "reservado" NO DEBEN implementarse en v0.1, pero sus nombres no se reutilizan.

### 5.8 Skills localizados (v0.1)

Los nombres usan minúsculas ASCII y guiones (estándar Agent Skills). Se generan según `project.yaml: language`.

| Operación principal | `es` | `en` | Usuario típico |
|---|---|---|---|
| `claim` (o `task propose` si la tarea no existe); con la preparación pendiente, el arranque guiado (D-70) | `empezar` | `start` | investigador; el responsable al preparar el proyecto |
| `status` | `estado` | `project-status` | todos |
| `submit` | `entregar` | `submit` | investigador |
| `review list` / `review show` | `pendientes` | `reviews` | responsable |
| `decision propose` | `decision` | `decision` | todos |
| `direction propose` | `direccion` | `direction` | todos |
| `review show` y `context` sobre una sección del artículo (D-56) | `editar-articulo` | `edit-article` | editor |

Ningún nombre DEBE coincidir con un comando integrado o un skill incluido de Claude Code (01-arquitectura REQ-ARCH-78).

### 5.9 IDs en `plan.md`

La sintaxis la define 03-esquemas. Los prefijos son:

- `RQ-<n>`: pregunta de investigación.
- `HYP-<n>`: hipótesis.
- `IN-<n>`: dentro del alcance.
- `OUT-<n>`: fuera del alcance.
- `METH-<n>`: método.

Los IDs no se reutilizan. Un elemento retirado se marca como tal, no se borra.

### 5.10 Trailers de commit

El formato y la semántica los define 11-trazabilidad. Los nombres son: `Task`, `Section`, `Assisted-by`, `AI-Use`, `Signed-off-by`. `Section` nombra la parte (D-43).

### 5.11 Resultados y errores de operaciones

- **Sobre:** `schema`, `ok`, `op`, `data`, `warnings`, `error`.
- **Objeto `error`:** `code`, `message`, `retryable`, `hint` y `details` (opcional).
- **Avisos:** objetos `{code, message, details?}` con `code` = `W_<ÁREA>_<NOMBRE>`; áreas: las de los errores más `OUTPUT`; catálogo cerrado en 05-operaciones §12.2.
- **Códigos de error de operación:** `E_<ÁREA>_<NOMBRE>` con áreas `CONFIG`, `AUTH`, `FORGE`, `CLAIM`, `PERM`, `STATE`, `VALIDATION`, `NET`, `HUMAN_ONLY`, `INTERNAL`; catálogo cerrado en 05-operaciones §12.1.
- **Códigos de hallazgo:** los que un validador pone en `data.results[].findings[].code`: `E_`/`W_` de `CONFIG` o `VALIDATION`, `conv.*` (02-convencion) e `IL`/`TX`/`SB` (10-seguridad); en la regla `scope`, los valores de `perm_code` de `result.schema.json` que devuelve `check_path`. Catálogo abierto, publicado por cada capítulo. Una operación con hallazgos `error` falla con `E_VALIDATION_FAILED` (o, fuera de `validate`, `E_CONFIG_INVALID` con `details.code`).
- No hay otras áreas (en particular, no existe `TRACE`).

### 5.12 Códigos de capítulo (para `REQ-` y `Q-`)

`ARCH`, `CONV`, `SCHEMA`, `TASK`, `OPS`, `PERM`, `HOOK`, `FORGE`, `CI`, `SEC`, `TRACE`, `MVP`.

### 5.13 CI: checks, workflows, rulesets y environments

Autoridad: 09-ci (workflows y checks) y 06-permisos (rulesets).

- **Checks requeridos** (perfil `enforced`): `aucove/build`, `aucove/validate` y, con `format: latex`, `aucove/editor-review` (D-56).
- **Checks informativos:** `aucove/build-compile`, `aucove/validate-pin`, `aucove/base-validate`, `aucove/base-audit`, `aucove/base-report`, `aucove/expire-claims`, `aucove/seal-submission`, `aucove/seal-integration`. Reservado: `aucove/model-review`.
- **Artefactos de evidencia:** `evidence-submission-<sha>-a<n>` y `evidence-integration-<sha>-a<n>` (09-ci §7.4).
- **Workflows de la plantilla:** `.github/workflows/{build,compile,validate,seal,main-broken,expire-claims}.yml` y, con `format: latex`, `editor-review.yml` y `editor-review-trigger.yml` (D-56; 09-ci §4.5). Reservado: `model-review.yml`.
- **Rulesets que crea `init`:** `aucove-base` (rama base), `aucove-task-branches` (`refs/heads/task/*`, solo `non_fast_forward`) y `aucove-workflows` (push ruleset sobre `.github/workflows/**`, si la forja lo admite).
- **Environment reservado:** `model-review`.
- **Environments del repo del framework:** `pypi`, `template-publish`, `forge-tests`, `model-tests`.
- No se registran `aucove-scope`, `aucove-approvals` ni `aucove-gate`.

### 5.14 Marcadores

Gramática (canónica aquí; uso: 05-operaciones §6.1; serialización: 04-tareas REQ-TASK-11):

- Forma de una línea: `<!-- aucove:<tipo> v<N>( <clave>=<valor>)* -->`.
- Forma con JSON: la primera línea como la anterior pero sin `-->`, una línea con un objeto JSON y una línea `-->`.
- `tipo` = `[a-z][a-z0-9-]*(:[a-z0-9-]+)?`, `clave` = `[a-z_]+`, `valor` = `[A-Za-z0-9._-]+`.
- Dentro del JSON, toda secuencia `--` se escribe `\u002d\u002d` (escape JSON; 04-tareas REQ-TASK-11), de modo que el comentario nunca contenga `--`, `-->` ni `--!>`.

| Tipo | Dónde y uso | Autoridad |
|---|---|---|
| `claim`, `release`, `submission`, `assessment`, `expiry`, `escalation` | Comentarios de Issue o PR, con JSON. | 04-tareas |
| `op` | `id=<op_id>`; idempotencia. | 08-forja |
| `ci:build`, `ci:validate`, `ci:main-broken`, `ci:expire-claims`, `ci:audit`; reservado: `ci:model-review` | Comentarios del CI. | 09-ci |
| `ci:evidence` (claves `kind`, `sha`, `attempt`; con JSON `evidence-record/v1`) | Comentario del sellado en el PR. | 09-ci §7.4, 11-trazabilidad §8.5 |
| `pr-meta-begin`, `pr-meta-end` | Cuerpo del PR. | 03-esquemas |
| `metrics` | `hook_denials=<n>`. | 12-mvp |

Excepción: `<!-- aucove:session {JSON} -->` en una línea, dentro de las notas (03-esquemas §7.2).

Retirados: `task-proposal`, `direction`, `expired`, el bloque `aucove:submission` del cuerpo del PR y `aucove:op=<uuid>`.

## 6. Mapa de capítulos

| Archivo | Código | Es la autoridad sobre |
|---|---|---|
| `00-marco.md` | — | Alcance, decisiones, glosario y registro de identificadores |
| `01-arquitectura.md` | `ARCH` | Módulos del paquete, distribución e instalación, versiones y compatibilidad (convención ↔ núcleo), presupuestos de rendimiento, i18n, comportamiento sin red, generación de `.claude/` y `.agents/skills/` desde una fuente, estructura del repo del framework |
| `02-convencion.md` | `CONV` | Árbol de la plantilla y significado de cada archivo y clase de ruta; formato del proyecto (`format`) y carpeta de cada parte; convenciones de escritura LaTeX (con `format: latex`); bibliografía, figuras, datos y notebooks; memoria; migración de convención |
| `03-esquemas.md` + `schemas/*.schema.json` | `SCHEMA` | `project.yaml`, `roles.yaml`, sintaxis de `plan.md`, registro de decisión, nota de sesión, formulario de Issue de tarea, plantilla de PR |
| `04-tareas.md` | `TASK` | Ciclo de vida y máquina de estados; protocolo de reclamo, liberación y vencimiento; flujo del coordinador (traslapes, cambio de dirección); actualización desde la base y conflictos |
| `05-operaciones.md` + `schemas/result.schema.json` | `OPS` | Catálogo de operaciones (entradas, precondiciones, efectos, idempotencia, salida); sobre de resultado; catálogo de errores; códigos de salida; exposición por MCP; presupuesto de contexto por respuesta |
| `06-permisos.md` | `PERM` | Modelo de autorización; matriz de aprobación por perfil; protecciones que aplica `init`; operaciones solo humanas; matriz de capacidades por puerta |
| `07-hooks.md` | `HOOK` | Contrato de `aucove hook <evento>`; contenido de `.claude/settings.json` y `settings.local.json`; controles locales en capas; Windows; caché de contexto |
| `08-forja.md` | `FORGE` | Interfaz del adaptador; detección de capacidades; implementación GitHub (REST); identidad y autenticación; límites de la API; restricciones de sesiones en la nube; diferencias con GitLab y Forgejo; adaptador falso |
| `09-ci.md` | `CI` | Workflows y checks: compilación segura, alcance, sincronía de roles, esquemas, decisiones inmutables, citas, lint de instrucciones, trazabilidad, vencimiento, `main-broken`, sellado de evidencia (`seal.yml`), revisión con modelo opcional |
| `10-seguridad.md` | `SEC` | Modelo de amenazas y requisitos; tipos de cuenta y clasificación de datos; cadena de suministro; bajas; incidentes; proceso de seguridad del proyecto |
| `11-trazabilidad.md` | `TRACE` | Trailers; declaración de uso de IA; CRediT; libro de aprobaciones; licencias y DCO; evidencia verificable de fecha: manifiesto, compromisos, pruebas, catálogo de TSA, verificación, custodia, exportación, apertura por hitos y depósito (§8) |
| `12-mvp.md` | `MVP` | Alcance cerrado del MVP; criterios de aceptación; estrategia de pruebas; hitos; métricas del piloto |
| `13-preguntas-abiertas.md` | — | Consolidación de todas las preguntas abiertas |

Para `claim`, `release`, `save`, `update`, `switch`, `submit`, `record-assessment`, `expire-claims`, `task accept` y `task cancel`, 04-tareas es la autoridad sobre la semántica (estados, precondiciones, pasos, compensación y marcadores) y 05-operaciones sobre la invocación (argumentos CLI y MCP, sobre, `data`, errores, códigos de salida y presupuesto); 05 DEBE transcribir 04 sin contradecirlo. La barrera de las operaciones solo humanas la define 06-permisos §6. El árbol de `.aucove/` lo define 01-arquitectura §7.3.

## 7. Versionado

- **Especificación:** `0.1`. Cualquier cambio de un identificador del §5 sube la versión menor (DC-24).
- **Convención:** `project.yaml: convention: "0.1"`. La regla de compatibilidad entre versiones de convención y del núcleo la define 01-arquitectura.
- **Núcleo:** SemVer. La salida JSON de las operaciones está versionada por `schema` (05-operaciones).
