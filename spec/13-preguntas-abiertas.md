# Especificación técnica v0.1 — Preguntas abiertas consolidadas

- **Estado:** v0.1
- **Fuente:** las secciones "Preguntas abiertas" de los capítulos 01 a 12.
- **Autoridad:** este archivo no define requisitos. Cada pregunta sigue siendo del capítulo que la formuló. Cuando se resuelve, se cierra en ese capítulo y sale de este archivo.

Cada entrada consolidada lleva un identificador `PA-<n>`, que solo vale dentro de este archivo, y enumera los `Q-*` originales que reúne. Las duplicadas se fusionaron en una sola entrada con todos sus IDs.

## 1. Criterios

**Bloquea el MVP** (sí o no). Una pregunta bloquea cuando sin su respuesta no se puede empezar a implementar la parte afectada, o no se puede cumplir o fechar un criterio de aceptación o un hito de 12-mvp (incluido el piloto, H5). Las demás se pueden resolver durante el desarrollo o el piloto, o quedan para v0.2, porque la especificación ya fija un valor por defecto o una alternativa.

**Quién decide.**

- **Usuario:** el usuario, o el responsable del proyecto. Son decisiones de alcance, presupuesto, institución, datos personales o licencias.
- **Equipo técnico:** el equipo que desarrolla el framework. Son decisiones de diseño que no cambian el alcance.
- **Prueba empírica:** la respuesta sale de una prueba (contrato `CT-*`, medición o spike). Si la prueba falla, se indica quién elige la alternativa.

**Recomendación.** Es la opción que se sugiere tomar si no hay otra información. No es normativa hasta que el capítulo dueño la incorpore.

## 2. Resumen

Hay 96 entradas abiertas, ninguna bloqueante, que reúnen 106 preguntas `Q-*` de los capítulos 01 a 12.

---

## 3. Preguntas que bloquean el MVP

No hay preguntas que bloqueen el MVP.

---

## 4. Preguntas que no bloquean el MVP

En cada tabla, la columna "¿Bloquea?" da el motivo en una línea.

### 4.1 Forja, API de GitHub y capacidades

| PA | IDs (capítulo) | Pregunta | ¿Bloquea? | Decide | Recomendación |
|---|---|---|---|---|---|
| PA-10 | Q-CI-1 (09) | Si un SHA tiene dos check runs con el mismo nombre, ¿cuál evalúa el ruleset? | No: hay que probarlo en H1 antes de confiar en REQ-CI-22, pero no cambia el diseño | Prueba empírica | Incluirlo en las pruebas de contrato del perfil `enforced`, con el caso de borrador y el del repositorio público. |
| PA-11 | Q-FORGE-4 (08) | ¿Qué responde `GET /rulesets` en un repositorio privado de una organización Free? ¿Qué valores toma `plan.name`? | No: lo no concluyente da `advisory` | Prueba empírica (`CT-CAP-02`) | Mantener `advisory` como resultado por defecto hasta tener la respuesta. |
| PA-12 | Q-PERM-7 (06) | ¿Acepta `actor_type: "User"` repositorios de cuentas personales? | No: solo afecta al perfil `enforced` sin suplente | Prueba empírica | Probarlo. Mientras tanto, recomendar repositorios de organización y declarar siempre un `deputy`. |
| PA-13 | Q-PERM-6 (06) | ¿Están disponibles en GitHub Team los rulesets de organización con "require workflows to pass"? | No: el push ruleset `aucove-workflows` cubre el caso | Prueba empírica | Probarlo en la org Team de pruebas de 00-marco D-24 (`proyecto-test-team`, que es `graphmachinelearningbp`), de noviembre de 2026 a enero de 2027. Si existen, evaluarlos para v0.2. |
| PA-14 | Q-PERM-8 (06) | ¿Se usa `dismissal_restriction` para que solo `lead` y `deputy` descarten revisiones? | No: es un refuerzo opcional | Equipo técnico | Activarlo en `enforced` si la prueba de contrato confirma que funciona en el plan del piloto. |
| PA-15 | Q-FORGE-3 (08) | ¿Está `markPullRequestReadyForReview` en el conjunto GraphQL del proxy web? | No: el método está reservado y v0.1 no usa GraphQL (DC-08) | Prueba empírica (`CT-WEB-03`) | Dejar la prueba como informativa. |
| PA-16 | Q-FORGE-8 (08) | ¿Cuentan los blobs, árboles y commits de la API Git Data para el límite de 80 creaciones por minuto? | No: afecta solo al presupuesto de `save` en modo `api` | Prueba empírica | Medirlo. Mientras tanto, suponer que sí cuentan y espaciar las peticiones. |
| PA-17 | Q-FORGE-11 (08) | Para etiquetar un PR con un token fine-grained, ¿basta Issues W o hace falta Pull requests W? | No: el token ya pide los dos permisos | Prueba empírica (`CT-AUTH-01`) | Si basta Issues W, no cambiar nada en v0.1. |
| PA-18 | Q-FORGE-12 (08) | ¿Se pueden descargar artefactos de Actions desde una sesión web? | No: v0.1 no lo necesita en el adaptador | Prueba empírica | Documentar el resultado; el PDF se consulta en la web. `evidence export` (v0.2) lo necesitará; en la puerta web devolverá `E_STATE_UNSUPPORTED_DOOR` (08-forja Q-FORGE-12). |
| PA-19 | Q-FORGE-10 (08) | Detalles sin verificar de GitLab (rama existente, CAS de refs, borrador) y de Forgejo (CODEOWNERS, borrador) | No: esos adaptadores quedan fuera de v0.1 | Prueba empírica | Resolverlo al empezar esos adaptadores. |
| PA-20 | Q-TASK-7 (04) | ¿Editar un comentario con menciones nuevas notifica en GitHub? | No: REQ-TASK-67 publica un comentario nuevo | Prueba empírica | Verificarlo solo para reducir ruido en v0.2. |
| PA-21 | Q-CI-7 (09) | ¿Los minutos de `ubuntu-slim` consumen la cuota 1:1 o según su tarifa? | No: afecta al costo, no al diseño | Prueba empírica | Medirlo en el repositorio de pruebas y ajustar la tabla de costos de 09-ci. |
| PA-97 | Q-FORGE-13 (08) | ¿Debe `doctor`, en una sesión web, comprobar que `git push` a `task/<n>` sigue disponible (por ejemplo, empujando una `task/<n>` existente a su mismo sha) y, si recibe 403, pasar la puerta web a solo consulta con un mensaje claro? | No: S1 se repite antes de H4 y antes de H7 (D-22) | Equipo técnico | Con S1 resuelto (D-41), decidirlo en H3; es un cambio para 05-operaciones y 08-forja. |
| PA-103 | Q-FORGE-14 (08) | En la web, ¿aceptan el proxy y la Claude GitHub App el `git push` de un merge de `update` que trae de la base cambios en `.github/workflows/**`? | No: si se rechaza, `update` se hace desde la terminal | Prueba empírica | Probarlo en la repetición de S1 del 23–27/11/2026 y documentar la salida. |
| PA-110 | Q-TRACE-13 (11) | S5 (H3, primera semana): ¿los runners alcanzan las TSA y los calendarios OTS? ¿Cuánto miden los comentarios y artefactos? ¿Hay endpoint de retención y recorte de `retention-days`? ¿Qué `verified_at` tiene el squash hecho en la web? ¿Se puede registrar en OSF sin proyecto? | No: si una TSA no responde, se cambia el catálogo; si `ubuntu-slim` no alcanza, el job pasa a `ubuntu-24.04` | Prueba empírica | Correrlo en la org de pruebas y registrar el resultado como decisión. |

### 4.2 Permisos, aprobaciones y revisión

| PA | IDs (capítulo) | Pregunta | ¿Bloquea? | Decide | Recomendación |
|---|---|---|---|---|---|
| PA-22 | Q-PERM-1 (06) | ¿Se exige la rama al día (`strict_required_status_checks_policy: true`) después del piloto? | No: es para después del piloto | Usuario | Mantener `false` en el piloto y decidir con los datos de CMP-7 (roturas de la base). |
| PA-23 | Q-PERM-2, Q-PERM-9 (06) | ¿Se listan los dueños de sección en `CODEOWNERS`? ¿Molesta al suplente recibir una petición de revisión en cada PR por `* @lead @deputy`? | No: v0.1 no tiene secciones ajenas (DC-01) y la línea única da la garantía nativa | Usuario, con datos del piloto | No agregar dueños a `CODEOWNERS`. Medir la molestia en el piloto; si es alta, que el suplente ajuste sus notificaciones de GitHub. |
| PA-24 | Q-PERM-3 (06) | ¿Deben las tareas del `lead` declarar también los archivos `lead` que tocan, para detectar traslapes? | No: D-19 enmendada permite no declararlos | Equipo técnico | No exigirlo en v0.1; reconsiderarlo si el piloto muestra traslapes no detectados en archivos `lead`. |
| PA-25 | Q-TASK-4 (04) | ¿Hacen falta permisos concedidos por tarea (sección ajena con permiso del dueño y doble aprobación) en una fase posterior? | No: reservado; en v0.1 ninguna tarea toca secciones ajenas (DC-01) | Usuario | Dividir tareas en v0.1 y reevaluar con los datos del piloto. En v0.1, una sección con varios autores se divide en subpartes (00-marco D-55). |
| PA-26 | Q-PERM-4, Q-CI-2 (06, 09) | ¿Se implementa el flujo por forks del perfil `advisory` en v0.2? | No: `scope` rechaza los forks en v0.1 | Usuario | Solo si el piloto tiene grupos en `advisory` que lo necesiten; sus costos están en 06 §5. |
| PA-27 | Q-PERM-5 (06) | ¿Una cuenta de máquina por persona para las sesiones de agente eliminaría el riesgo residual de 06 §6? ¿La permiten los términos de GitHub y de los proveedores? | No: el riesgo está documentado y el CI hace cumplir las reglas | Usuario, con consulta legal | No en v0.1. Revisar los términos antes de proponerlo en v0.2. |
| PA-28 | Q-TASK-3 (04) | ¿Se aceptan de forma automática las tareas propuestas por quien es dueño de todas sus secciones? | No: choca con D-09; la aceptación manual funciona | Usuario | Mantener la aceptación humana en v0.1 y medir su fricción en el piloto. |
| PA-29 | Q-TRACE-3 (11) | GitHub no descarta las aprobaciones cuando se edita el cuerpo del PR. ¿Debe `traceability` exigir que el bloque no cambie después de la última aprobación? | No: la trazabilidad sigue siendo verificable | Equipo técnico | Emitir una advertencia, no un error, en v0.1. |

### 4.3 Operaciones solo humanas y detección de la puerta

| PA | IDs (capítulo) | Pregunta | ¿Bloquea? | Decide | Recomendación |
|---|---|---|---|---|---|
| PA-30 | Q-OPS-6 (05) | ¿Se acepta el riesgo residual del TTY simulado, o las operaciones H se hacen solo en la web de la forja? | No: el riesgo está documentado (06 §6) | Usuario | Aceptarlo en v0.1, documentarlo y recomendar la web de la forja para aprobar. |
| PA-31 | Q-PERM-11 (06) | En Git Bash (mintty), `isatty()` puede dar falso para un humano. ¿Se documenta `winpty` o se admite otra señal? | No: hay alternativas en Windows | Equipo técnico | Documentar `winpty` y recomendar PowerShell o Windows Terminal. No admitir otras señales. |
| PA-32 | Q-OPS-5, Q-PERM-10 (05, 06) | ¿Cómo se detecta la puerta de forma fiable? ¿Qué variables de entorno ponen Codex, Gemini CLI, OpenCode y goose? (los valores de `CLAUDE_CODE_ENTRYPOINT` no están documentados) | No: la detección solo refuerza `E_HUMAN_ONLY_AGENT_ENV` | Prueba empírica | Registrar las variables observadas en cada puerta. Usar `AUCOVE_AI_TOOL`, que ya pasa la configuración MCP generada, y no depender de valores no documentados. |
| PA-33 | Q-TRACE-1 (11) | La confirmación de `attest-v1` por MCP la transmite el agente. ¿Basta en v0.1, o hace falta una confirmación solo humana? | No: la aprobación humana del PR sigue siendo obligatoria | Usuario | Aceptarla en v0.1 con `channel: mcp` registrado, y revisarla con los datos del piloto. |

### 4.4 Ciclo de las tareas y operaciones

| PA | IDs (capítulo) | Pregunta | ¿Bloquea? | Decide | Recomendación |
|---|---|---|---|---|---|
| PA-34 | Q-TASK-5, Q-PERM-12 (04, 06) | ¿Qué valores por defecto de `claim_expiry_days`, `max_active_claims` y `review.sla_hours` sirven a un piloto de 4 a 6 semanas? ¿Hace falta un valor por persona? | No: ya hay valores por defecto (14 días, 3 reclamos, 72 h) | Usuario | Mantenerlos en el piloto, sin valores por persona. |
| PA-35 | Q-TASK-6 (04) | ¿Hace falta una operación para editar las secciones de un Issue y una operación `revert` solo humana? | No: hoy se hace en la web y con una tarea de reversión (09 §7.3) | Usuario | Posponerlo a v0.2 y medir en el piloto cuántas veces hace falta. |
| PA-36 | Q-OPS-7 (05) | `release` no descarta trabajo empujado. ¿Hace falta una vía para que el investigador abandone una rama con commits sin `task cancel`? | No: `release` archiva el trabajo en `refs/tags/archive/...` | Equipo técnico | No en v0.1. Documentar que lo archivado no se pierde. |
| PA-37 | Q-OPS-9 (05) | ¿Basta con reejecutar las operaciones y `doctor`, o hace falta `aucove repair`? | No: las operaciones son reconciliaciones idempotentes | Equipo técnico | No agregar `repair` en v0.1. Revisarlo si el piloto muestra estados a medias que `doctor` no explique. |
| PA-38 | Q-CONV-1 (02) | ¿Se agrega `aucove section create <id>` para dar de alta una sección en un paso? | No: el alta manual está especificada (02 §4.2.1) | Usuario | Candidata para v0.2 si el alta manual falla en el piloto. |
| PA-39 | Q-CONV-3 (02) | ¿Debe `aucove update` borrar solo las entradas de `pending.bib` ya consolidadas? | No: los duplicados no hacen fallar el CI (REQ-CONV-48) | Equipo técnico | No en v0.1. Reevaluarlo junto con la consolidación de la bibliografía. |
| PA-40 | Q-CI-9 (09) | ¿Un disparador `issues: edited` que vuelva a ejecutar `validate` cuando cambien las secciones de la tarea? | No: el alcance ya usa las secciones del reclamo ∩ las actuales | Equipo técnico | No en v0.1; basta la ejecución en cada push. |
| PA-41 | Q-CI-10 (09) | ¿`direction-ack` bloquea por defecto? | No: `coordinator.require_direction_ack` ya vale `false` por defecto (03 §3.2) | Usuario | Confirmar `false`, coherente con el carácter consultivo del coordinador (COH-10). |
| PA-102 | Q-TASK-9 (04) | ¿Se fijan las secciones de una tarea en un dato que el reclamante no pueda reescribir (commit de reclamo o aceptación del `lead`) y se trata `claim_without_marker` como «sin secciones»? | No: la ampliación solo alcanza secciones del propio reclamante, queda en el historial y pasa por la revisión del PR | Equipo técnico | Decidirlo con los datos del piloto; en v0.1, documentarlo como debilidad aceptada (REQ-TASK-10). |

### 4.5 Hooks y Claude Code

| PA | IDs (capítulo) | Pregunta | ¿Bloquea? | Decide | Recomendación |
|---|---|---|---|---|---|
| PA-42 | Q-HOOK-3, Q-SEC-2 (07, 10) | ¿Se fija `disableAutoMode` en la plantilla, al menos para proyectos `internal`? | No: las reglas `deny` siguen aplicando en modo `auto` | Usuario | No fijarlo en la plantilla. Documentarlo como recomendación para `internal` y para los managed settings de la institución. |
| PA-43 | Q-HOOK-2 (07) | ¿Molesta la regla `ask` de `claim_task`, `release_task` y `submit_task`? ¿Debe incluir `propose_direction`? | No: se ajusta con datos | Usuario, con datos del piloto | Mantenerla e incluir `propose_direction`. Medir la fricción en el piloto. |
| PA-44 | Q-HOOK-4 (07) | ¿Se asocian las ramas `worktree-<nombre>` (worktrees de Claude Code y subagentes) a la tarea del checkout que las creó? | No: fuera de `task/<n>` no se editan secciones | Equipo técnico | No en v0.1: esas ramas no son de tarea. Documentar que el trabajo se integra en `task/<n>`. |
| PA-45 | Q-HOOK-6 (07) | ¿Se deniegan `git commit`, `git checkout`, `git merge` y `git push` del agente, remitiendo a `save`, `switch` y `update`? | No: el CI hace cumplir los trailers | Usuario | No denegarlos en v0.1, que cuentan como `undeclared`. Medir su frecuencia en el piloto. |
| PA-46 | Q-HOOK-7 (07) | ¿Se extiende la integridad a escrituras de otros servidores MCP en v0.2? | No: es para v0.2 | Equipo técnico | Sí en v0.2, con el matcher `^mcp__(?!aucove__)`. |
| PA-47 | Q-HOOK-8 (07) | Si el responsable cambia `forge.base_branch` después de `init`, ¿qué operación regenera las reglas estáticas? | No: es un cambio raro | Equipo técnico | `init --regenerate` en v0.1, hasta que exista `upgrade`. |
| PA-48 | Q-HOOK-9 (07) | ¿Distinguen mayúsculas las reglas `Edit(//...)` en Windows y macOS? ¿Un handler presente en ajustes gestionados y de proyecto se ejecuta una sola vez? | No: el CI es la garantía (D-13); afecta solo a L2 | Prueba empírica | Incluirlo en las fixtures de hooks de 12-mvp §3.5. |
| PA-49 | Q-ARCH-8, Q-HOOK-5 (01, 07) | En Windows y en macOS, el PATH de las apps gráficas (Claude Desktop, IDE) puede no incluir `~/.local/bin` ni reflejar `uv tool update-shell` sin cerrar sesión. ¿Lo resuelve 01-arquitectura o solo lo detecta `doctor`? | No: `doctor` lo detecta | Prueba empírica | Medirlo en la matriz de plataformas. Si falla, documentar que hay que cerrar sesión y hacer que `doctor` dé la instrucción exacta. |

### 4.6 MCP y presupuesto de contexto

| PA | IDs (capítulo) | Pregunta | ¿Bloquea? | Decide | Recomendación |
|---|---|---|---|---|---|
| PA-50 | Q-OPS-2 (05) | ¿Entrega Claude Code al modelo `structuredContent`, el bloque de texto o ambos? Si son ambos, los topes de 05 §7.1 deberían bajar a la mitad. | No: solo ajusta los topes | Prueba empírica | Medirlo con `/context` en H2 y ajustar la tabla de 05 §7.1. |
| PA-51 | Q-OPS-11 (05) | ¿Atiende el SDK elegido a clientes `initialize` 2025-11-25 y 2026-07-28 en el mismo proceso stdio? | No: se puede fijar una sola versión del protocolo | Prueba empírica | Probarlo con ambos tipos de cliente en H1. |
| PA-52 | Q-OPS-8 (05) | ¿Es correcto `openWorldHint: false` para herramientas que devuelven contenido de terceros? | No: es una pista, no un control | Equipo técnico | Mantener `false`: el contenido de terceros ya va marcado como `untrusted` (DC-11). |
| PA-53 | Q-MVP-10 (12) | ¿Se acepta el arnés HTTP en proceso para la conformidad MCP mientras v0.1 solo tenga stdio? | No: es una decisión de pruebas | Equipo técnico | Aceptarlo, con una prueba de humo real por stdio. |

### 4.7 Instalación, arquitectura y plataformas

| PA | IDs (capítulo) | Pregunta | ¿Bloquea? | Decide | Recomendación |
|---|---|---|---|---|---|
| PA-54 | Q-ARCH-5 (01) | El responsable que instala una versión menor nueva antes de integrar el PR que sube `core` tendrá los hooks cerrados. ¿Se acepta, o se lee `core` del árbol de trabajo en la rama que lo modifica? | No: el caso es raro y tiene salida manual | Equipo técnico | Aceptarlo en v0.1 y documentar el orden: primero integrar el PR que sube `core`, después instalar. |
| PA-55 | Q-ARCH-3 (01) | El setup script de la nube fija un solo `PIN` por entorno. ¿Un entorno por proyecto o un rango compartido? | No: afecta solo a quien trabaja en varios proyectos | Equipo técnico | Un entorno por proyecto. |
| PA-57 | Q-ARCH-7 (01) | ¿Hace falta un ejecutable autónomo para el piloto en Windows sin uv? | No: la instalación con uv está especificada | Usuario | No en v0.1. Si el piloto lo pide, tipo *onedir* y cumpliendo 01 §7.1. |
| PA-58 | Q-ARCH-9 (01) | ¿Es alcanzable el p95 < 300 ms con Microsoft Defender en equipos institucionales? | No: hay alternativas (catálogos precompilados, menos imports) | Prueba empírica | Medirlo en la semana 1 de desarrollo. |
| PA-59 | Q-ARCH-1 (01) | OpenCode lee `.claude/skills/` y `.agents/skills/` y exige nombres únicos. VS Code con GitHub Copilot también lee las dos rutas (y `.github/skills/`); qué hace con un mismo nombre en una misma raíz no está documentado. ¿Se acepta el duplicado o se documenta una sola ruta? | No: OpenCode es de mejor esfuerzo | Equipo técnico | Documentar que OpenCode use solo `.agents/skills/`. |
| PA-60 | Q-ARCH-6 (01) | ¿La plantilla publicada va en `en` o en `es`? | No: `init` localiza cualquiera de los dos | Usuario | `en`: la plantilla es pública y `init` la localiza. |
| PA-62 | Q-SEC-6 (10) | ¿De dónde lee `doctor` la "versión mínima segura" sin agregar un servicio? | No: `doctor` puede omitir el chequeo | Equipo técnico | De los avisos de seguridad de GitHub del repositorio del framework, con caché y sin fallar si no hay red. |
| PA-63 | Q-SEC-7 (10) | ¿Verifica `uv tool install --constraints` los hashes del archivo de restricciones? Si no, ¿se ofrece un venv con `--require-hashes`? | No: el lock fija versiones exactas | Prueba empírica | Verificarlo. Si no verifica hashes, documentar la instalación con `uv pip install --require-hashes` en un venv como opción endurecida. |

### 4.8 Convención LaTeX, bibliografía y compilación

| PA | IDs (capítulo) | Pregunta | ¿Bloquea? | Decide | Recomendación |
|---|---|---|---|---|---|
| PA-64 | Q-SEC-8 (10) | TeX Live 2026 no impide lecturas arbitrarias. ¿Basta `--network=none` con montaje de solo lectura, o hay que restringir `TEXINPUTS`? | No: la compilación ya corre aislada y sin secretos | Equipo técnico | Agregar `TEXINPUTS` sin `//` del sistema si no rompe la plantilla (probarlo con PA-66). |
| PA-65 | Q-CI-4 (09) | ¿Resuelve bien `latexdiff --flatten` los `\input` relativos a `tex/` dentro del contenedor? | No: `ci.latexdiff` es opcional y vale `false` | Prueba empírica | Probarlo con la plantilla real antes de recomendar `ci.latexdiff: true`. |
| PA-66 | Q-CI-6 (09) | ¿Funciona biber (binario PAR) con `--read-only` y `tmpfs` ejecutable? | No: hay que probarlo en H2, pero no cambia el diseño | Prueba empírica | Probarlo con la imagen fijada en H2. |
| PA-67 | Q-CI-5 (09) | ¿Compensa cachear la imagen (`docker load`) frente a `docker pull`? | No: es una optimización | Prueba empírica | Medirlo; usar `docker pull` hasta tener datos. |
| PA-68 | Q-CI-11 (09) | ¿Se publica el PDF de la base en una Release con URL estable? | No: el PDF está en los artefactos del CI | Usuario | No en v0.1, porque exigiría `contents: write` en `main-broken`. Reevaluarlo en el piloto con G-NT. |
| PA-69 | Q-CONV-7 (02) | ¿Se admite BibTeX (`build.bibliography: bibtex`) para revistas que exigen `.bbl` de BibTeX? | No: el envío a revista queda fuera de v0.1 | Usuario | Resolverlo en el ciclo de envío (reservado). |
| PA-70 | Q-CONV-4 (02) | ¿Se verifica contra la API de Zotero que cada clave pendiente exista en el grupo? | No: requiere un secreto y no puede ir en el job de compilación | Equipo técnico | No en v0.1. |
| PA-71 | Q-CONV-5 (02) | ¿Conserva la exportación de la biblioteca web de Zotero la clave nativa? | No: afecta solo a quien no usa Zotero de escritorio | Prueba empírica | Verificarlo y documentar el resultado. |
| PA-72 | Q-CONV-2 (02) | ¿Se admiten en v0.2 macros locales por sección con prefijo obligatorio? | No: es para v0.2 | Usuario | Decidirlo con los datos del piloto sobre `notation.tex`. |
| PA-73 | Q-CONV-8 (02) | ¿Pasa `latexindent -m -k` a check bloqueante en v0.2? | No: es para v0.2 | Equipo técnico | Mantenerlo consultivo en v0.1. |
| PA-74 | Q-CONV-9 (02) | ¿Se calibran con el piloto los umbrales de 1 MiB, 5 MiB, 20 MiB y 32 KiB? | No: ya hay valores | Prueba empírica | Registrar los tamaños reales del piloto y ajustarlos en v0.2. |
| PA-75 | Q-CONV-10 (02) | ¿Un proyecto con artículo y suplemento necesita varios `main`, o basta un apéndice? | No: basta un apéndice en v0.1 | Usuario | Un apéndice en v0.1. |
| PA-76 | Q-SCHEMA-5 (03) | ¿Se agrega `paths.sections` a `project.yaml` al abrir Markdown o Quarto? | No: v0.1 admite `latex` y `none`, y las partes viven en `sections/<id>/` sin depender del formato (D-43) | Equipo técnico | Sin objeto para `none`. Para Markdown o Quarto compilados se decide al abrirlos, con la convención, el job y la imagen que exija ese formato; IL05 e IL08 ya se aplican al Markdown de las partes. |

### 4.9 Esquemas y metadatos

| PA | IDs (capítulo) | Pregunta | ¿Bloquea? | Decide | Recomendación |
|---|---|---|---|---|---|
| PA-77 | Q-SCHEMA-2, Q-TRACE-7 (03, 11) | CRediT en `roles/v2`: ¿se agrega el grado de contribución (`lead`, `equal`, `supporting`)? ¿Cómo se declara a personas sin cuenta en la forja? | No: `roles/v1` tiene `credit` sin grado | Usuario | Para `roles/v2`: agregar el grado y una lista `contributors[]` aparte. |
| PA-78 | Q-SCHEMA-7, Q-SEC-5 (03, 10) | ¿Debe `account_type` ser una lista por herramienta? ¿Cómo se mapean al `account_type` los términos de OpenAI, Google y otros proveedores? | No: `account_type` toma el peor caso, y `unknown` solo vale con datos `public` | Usuario, con consulta legal | Mantener un solo valor en v0.1. Verificar los términos de los proveedores que use el piloto antes de empezarlo. |
| PA-79 | Q-SCHEMA-9, Q-TRACE-6 (03, 11) | ¿Se declara `ai_use` por sección en lugar de por PR? ¿Alcanza el vocabulario de `AI-Use` para las políticas de las revistas del piloto (en especial `figures` y `review`)? | No: el vocabulario se puede ampliar sin romper la versión | Usuario | Declararlo por PR en v0.1 (una tarea suele ser una sección) y revisar las políticas de las revistas del piloto. |
| PA-80 | Q-SCHEMA-3 (03) | ¿Se admiten logins antiguos de GitHub que no cumplen las reglas actuales? | No: es un caso raro | Equipo técnico | Relajar el patrón solo si aparece un caso real. |
| PA-81 | Q-SCHEMA-4 (03) | Con más de 30 secciones, ¿sigue siendo usable el `dropdown`, o conviene un `input` validado por el núcleo? | No: los proyectos del piloto tienen pocas secciones | Equipo técnico | Mantener el `dropdown`. |
| PA-82 | Q-SCHEMA-6 (03) | ¿Fecha local o UTC en el nombre de la decisión? | No: ya se eligió la local (REQ-SCHEMA-49) | Equipo técnico | Confirmar la fecha local. |
| PA-83 | Q-SCHEMA-8 (03) | ¿Necesita `plan.md` un prefijo para el objetivo (p. ej. `GOAL-`)? | No: agregarlo después no rompe la versión | Usuario | No en v0.1. |
| PA-84 | Q-MVP-9 (12) | ¿Los niveles N0–N3 se activan con un interruptor en `project.yaml` o solo se documentan? | No: son una guía de adopción | Usuario | Solo documentarlos en v0.1. |

### 4.10 Seguridad y datos

| PA | IDs (capítulo) | Pregunta | ¿Bloquea? | Decide | Recomendación |
|---|---|---|---|---|---|
| PA-85 | Q-CI-12 (09) | ¿Qué hacer si GitHub endurece más `pull_request_target`? | No: es un riesgo futuro con plan B (`workflow_run`) | Equipo técnico | Vigilar el changelog de GitHub. El plan B exige rehacer 09 §5.2, y los checks de `workflow_run` no cuentan como requeridos. |
| PA-86 | Q-CI-3 (09) | ¿Una lista de bots admitidos (Dependabot) solo para `.github/workflows/**`, o los reemplaza el responsable? | No: el responsable puede actualizar los SHA | Usuario | No hay lista en v0.1: el responsable aplica las actualizaciones en una tarea propia. |
| PA-87 | Q-SEC-1 (10) | ¿Un preprocesador de PDFs que detecte texto invisible antes de que el agente los lea? | No: es para una versión posterior | Equipo técnico | Candidato para v0.2. |
| PA-88 | Q-SEC-3 (10) | ¿Un escáner de datos personales como `warning` en CI, con listas por país? | No: es para una versión posterior | Usuario | Candidato para v0.2, empezando por correos y teléfonos. |
| PA-89 | Q-SEC-9 (10) | ¿Un servidor de laboratorio con Claude Code preinstalado cuenta como "servicio" según la página legal? | No: no es una puerta de v0.1 | Usuario, con consulta legal | Consultarlo antes de ofrecerlo (SEG-18). |
| PA-104 | Q-SEC-10 (10) | ¿Se empaquetan en v0.2 las reglas de gitleaks sin su lista global de rutas permitidas, con su licencia MIT y una comprobación al cambiar la versión fijada? | No: en v0.1 se acepta como riesgo residual (10-seguridad §2.3) | Equipo técnico | Sí en v0.2, si el piloto versiona figuras o PDF con texto. |
| PA-105 | Q-SEC-11 (10) | Criterios del catálogo de TSA y su revisión anual | No: el catálogo inicial tiene 2 TSA, y OTS es independiente | Equipo técnico | Política publicada, certificado de larga vigencia, términos de uso compatibles con documentos y sin registro de identidad; DigiCert entra solo si los cumple. |
| PA-106 | Q-TRACE-11 (11) | Base y plazo de conservación del paquete de evidencia | No para el código (hay un valor por defecto de 10 años, D-46); sí para el protocolo de ética del 30/10 | Usuario, con la oficina jurídica | Proponer la ejecución del acuerdo de autoría y la defensa de derechos, con el consentimiento como transparencia; resolver antes del 30/10/2026. |

### 4.11 Trazabilidad y licencias

| PA | IDs (capítulo) | Pregunta | ¿Bloquea? | Decide | Recomendación |
|---|---|---|---|---|---|
| PA-90 | Q-TRACE-2 (11) | ¿Dónde agrega GitHub los `Co-authored-by` en un squash con `PR_BODY`? ¿Copia los `Co-Authored-By` de los commits? | No: se tolera con REQ-TRACE-30 | Prueba empírica | Probarlo con un squash real (S-10 de 11-trazabilidad). |
| PA-91 | Q-TRACE-4, Q-TRACE-9 (11) | ¿Se materializa el libro de aprobaciones en v0.1? ¿Se registra en `approval-record` la evidencia de bypass (API de *rule suites*)? | No: la función pura y `merge_audit` ya dan la información | Usuario | No materializarlo en v0.1, y dejar *rule suites* para después. |
| PA-92 | Q-TRACE-5 (11) | ¿Es correcta la licencia `LicenseRef-AllRightsReserved` para `text` en repositorios privados? | No: es un valor por defecto conservador | Usuario, con la oficina jurídica | Validarla con la oficina jurídica de la institución del piloto antes de H5. |
| PA-93 | Q-TRACE-8 (11) | ¿Con qué mecanismo se verifica el DCO en el repositorio del framework? | No: afecta solo al repositorio del framework | Equipo técnico | Un workflow propio con el analizador de 11 §2.10, sin apps de terceros, que exima los PR que la API atribuye a `dependabot[bot]` (D-07 c). |
| PA-107 | Q-TRACE-12 (11) | ¿NOM-151 por hito? | No | Usuario, tras la consulta jurídica. Diferida: se retoma si el usuario lo pide | No comprarla para el piloto (D-49). |
| PA-108 | Q-CI-13 (09) | ¿Actualización diaria de las pruebas OTS? | No: `verify --upgrade` y la guía las completan | Equipo técnico | v0.2. |
| PA-109 | Q-MVP-16 (12) | ¿Se practica una apertura en el piloto? | No | Usuario | Solo si un grupo llega a un hito; si no, depósito cifrado. |
| PA-111 | Q-TRACE-14 (11) | ¿Qué verificación de Bitcoin se agrega en v0.2? | No: en v0.1 OTS da `weak` y `VERIFY.md` explica el cotejo manual | Equipo técnico | Nodo propio opcional más dos exploradores coincidentes, marcados de menor confianza. |
| PA-113 | Q-TRACE-15 (11) | ¿Se admiten llaves físicas FIDO (`sk-ssh-ed25519@openssh.com`) para firmar el índice en el modo `admin`? | No: v0.1 firma con `ssh-ed25519` y frase de paso | Equipo técnico | v0.2: es la mejor defensa contra un agente que firme por su cuenta (10-seguridad TH-34). |

### 4.12 Revisión con modelo (reservada)

| PA | IDs (capítulo) | Pregunta | ¿Bloquea? | Decide | Recomendación |
|---|---|---|---|---|---|
| PA-94 | Q-CI-8 (09) | ¿Respeta `claude-code-action` `--max-budget-usd` en su modo de automatización? | No: la revisión con modelo queda fuera de la plantilla v0.1 (DC-18) | Prueba empírica | Resolverlo al activar la revisión con modelo. |
| PA-95 | Q-MVP-5 (12) | ¿La prohibición de `claude -p` (D-15) aplica también al CI de pruebas del framework? | No: 12-mvp ya supone que sí y usa el Agent SDK | Usuario | Confirmar que sí aplica. |

### 4.13 Lista de verificación de las pruebas empíricas

Las entradas que decide una prueba se agrupan aquí para planificar H1 y H2: PA-10, PA-11 (`CT-CAP-02`), PA-12, PA-13, PA-15 (`CT-WEB-03`), PA-16, PA-17 (`CT-AUTH-01`), PA-18, PA-20, PA-21, PA-32, PA-48, PA-49, PA-50, PA-51, PA-58, PA-63, PA-65, PA-66, PA-67, PA-71, PA-74, PA-90, PA-94, PA-103, PA-110.

---

## 5. Cómo usar este archivo

1. **Antes de implementar,** hay que resolver las entradas de §3; hoy no hay ninguna. Después de la salida de H1, REQ-MVP-13 solo admite reducciones de alcance.
2. **Para cerrar una pregunta,** se edita el capítulo dueño: se agrega o modifica el requisito que la resuelve, si hace falta, y se quita el `Q-*`. Si la resolución es una decisión de diseño, se registra como nota en 00-marco §3. Después se quita su entrada de este archivo. Un `Q-*` no se cierra solo aquí.
3. **Si una respuesta cambia un identificador del registro** (00-marco §5), se aplica la regla de versionado de 00-marco §7. Los cambios hechos antes de publicar la 0.1 no suben la versión (DC-24).
4. **Para agregar una pregunta nueva,** se escribe en la sección "Preguntas abiertas" del capítulo dueño, con un número nuevo de su código (00-marco §5.12): los de los `Q-*` cerrados no se reutilizan. Luego se agrega aquí, en el tema que corresponda, con un `PA-<n>` nuevo. Los números `PA-` no se reutilizan.
5. **Para fusionar duplicadas,** se mantiene una sola entrada `PA-` con todos los `Q-*`, y cada capítulo conserva su `Q-*` apuntando a la misma resolución.
6. **Revisión periódica:** al cerrar cada hito (H1 a H5) se recorre §4.13 y se actualiza cada entrada con el resultado de su prueba.
