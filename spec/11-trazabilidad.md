# 11 — Trazabilidad, uso de IA, CRediT y licencias

- **Código:** `TRACE`
- **Estado:** v0.1
- **Autoridad (00-marco §6):** trailers de commit, declaración de uso de IA, CRediT, libro de aprobaciones, licencias y DCO; evidencia verificable de fecha: manifiesto, compromisos, pruebas, catálogo de TSA, verificación, custodia, exportación, apertura por hitos y depósito (§8).
- **Depende de:** 03-esquemas (bloque del PR, nota de sesión, `roles.yaml`, `project.yaml`), 05-operaciones (entradas, errores y advertencias de `save` y `submit`), 06-permisos (protecciones que aplica `init`, quién puede aprobar), 07-hooks (registro del modelo), 08-forja (llamadas REST) y 09-ci (checks). Los supuestos sobre esos capítulos están en §10.

## 1. Principios

1. **Todo es declarativo.** El autor de git, los trailers, la herramienta y el modelo los declaran la persona o su entorno. Nada de eso prueba nada. La única evidencia que el núcleo no controla es la de la forja: revisiones y quién integró. Los sellos de terceros (§8) agregan una fecha verificable y la integridad de lo sellado, pero no prueban autoría. La autoría descansa en el acuerdo de autoría (§6.4), CRediT y los registros de la forja (00-marco D-49).
2. **Escritor determinista.** Los trailers los escribe el núcleo, no el modelo ni la persona a mano, con una gramática fija que CI puede validar.
3. **La IA no es autora.** Se registra como asistencia (`Assisted-by`), nunca como coautora ni con un rol CRediT. Así lo piden COPE e ICMJE (§5.3).
4. **La responsabilidad es humana.** El núcleo escribe `Signed-off-by` solo después de que la persona confirma la declaración de responsabilidad (§2.8).
5. **Se captura lo mínimo.** Nada de prompts, transcripciones, tokens ni costos.

- **REQ-TRACE-1:** El núcleo NO DEBE leer el archivo de `transcript_path` ni guardar prompts, respuestas del modelo, conteos de tokens o costos con fines de trazabilidad.

## 2. Trailers

### 2.1 Conjunto, cardinalidad y orden

| Trailer | Commit de `save` | Cuerpo del PR y commit de squash | Valor |
|---|---|---|---|
| `Task` | 1 (en `task/<n>`) | 1 | `#<n>` (número del Issue) |
| `Section` | 0..n | 0..n | `id` de sección de `roles.yaml` |
| `AI-Use` | 1 | 1 | lista de usos (§2.3) |
| `Assisted-by` | 0..n | 0..n | `<tool>:<model>` (§2.4) |
| `Signed-off-by` | 0 | 1 | `Nombre <correo>` |

`Task` lleva el número del **Issue**. El sufijo `(#<pr>)` que GitHub agrega al título del squash es el número del **PR**. Son números distintos.

- **REQ-TRACE-2:** El núcleo DEBE escribir los trailers en un único párrafo final del mensaje, separado del texto anterior por una línea en blanco, en este orden: `Task`, `Section`, `AI-Use`, `Assisted-by`, `Signed-off-by`.
- **REQ-TRACE-3:** El núcleo DEBE escribir cada clave con las mayúsculas de la tabla, seguida de `": "` (dos puntos y un espacio), con fin de línea LF y sin plegar valores en varias líneas.
- **REQ-TRACE-4:** El núcleo DEBE ordenar las líneas `Section` por orden ASCII y las `Assisted-by` por `tool` y luego por `model`, sin duplicados.

### 2.2 Gramática

ABNF (RFC 5234 con RFC 7405). `UTF8-non-ascii` es el de RFC 3629. `%s"..."` es una cadena que distingue mayúsculas (RFC 7405).

```abnf
trailer-block = [task-tr] *section-tr ai-use-tr *assisted-tr [signoff-tr]
task-tr       = %s"Task: #" %x31-39 *DIGIT LF
section-tr    = %s"Section: " slug LF
ai-use-tr     = %s"AI-Use: " use *(", " use) LF
use           = %s"none" / %s"undeclared" / %s"ideation" / %s"literature" / %s"drafting"
              / %s"editing" / %s"translation" / %s"code" / %s"analysis" / %s"figures" / %s"review"
assisted-tr   = %s"Assisted-by: " slug ":" model LF          ; slug de herramienta, máx. 32
model         = 1*128(ALPHA / DIGIT / "." / "_" / "-" / ":" / "@" / "/" / "+")
signoff-tr    = %s"Signed-off-by: " name " <" email ">" LF
name          = 1*(%x20-3B / %x3D / %x3F-7E / UTF8-non-ascii)   ; sin "<" ni ">"
email         = 1*(%x21-3B / %x3D / %x3F-7E)                   ; contiene "@"
slug          = 1*(%x61-7A / DIGIT) *("-" 1*(%x61-7A / DIGIT))
```

El separador entre herramienta y modelo es el **primer** `:` después de `Assisted-by: `. El modelo sí puede contener `:`, como en los identificadores de Bedrock (`...-v1:0`).

La gramática describe lo que escribe el núcleo; el analizador compara claves sin distinguir mayúsculas (REQ-TRACE-31), pero los valores de `AI-Use` DEBEN ir en minúsculas.

- **REQ-TRACE-5:** `none` y `undeclared` DEBEN aparecer solos en `AI-Use`. Los demás valores PUEDEN combinarse, sin repetirse y en el orden de la tabla de §2.3.
- **REQ-TRACE-6:** Si `AI-Use` es `none`, el mensaje NO DEBE contener `Assisted-by`.
- **REQ-TRACE-7:** En el cuerpo del PR y en el commit de squash, `AI-Use` NO DEBE valer `undeclared`. Si contiene un uso distinto de `none`, DEBE haber al menos un `Assisted-by`.
- **REQ-TRACE-8:** `model` DEBE ser el identificador del modelo tal como lo entrega su fuente (§2.6), o `unknown` si no hay fuente. El núcleo NO DEBE convertirlo en un nombre comercial.

### 2.3 Vocabulario de `AI-Use`

| Valor | es | en | Qué cubre | Dónde suele declararse (§5.3) |
|---|---|---|---|---|
| `none` | sin uso de IA | no AI use | Ninguna herramienta de IA generó ni modificó el contenido. | — |
| `undeclared` | sin declarar | undeclared | No se declaró el uso. Solo se permite en commits de rama; `submit` exige resolverlo. | — |
| `ideation` | ideación | ideation | Lluvia de ideas, estructura, esquemas. | Métodos o agradecimientos, según la revista |
| `literature` | búsqueda bibliográfica | literature search | Buscar, seleccionar o resumir literatura. | Métodos |
| `drafting` | redacción | drafting | Texto nuevo generado con IA y revisado por una persona. | Agradecimientos (ICMJE); Métodos (COPE) |
| `editing` | edición de lenguaje | language editing | Gramática, estilo o acortamiento de texto escrito por una persona. | Agradecimientos |
| `translation` | traducción | translation | Traducción de texto. | Agradecimientos |
| `code` | código | code | Scripts, macros LaTeX, notebooks, CI. | Métodos |
| `analysis` | análisis de datos | data analysis | Análisis, estadística o interpretación de datos. | Métodos |
| `figures` | figuras | figures | Generar o modificar figuras, diagramas o imágenes. | Métodos (revisar la política de la revista) |
| `review` | revisión | review | Crítica, verificación de consistencia o de citas. | Métodos o agradecimientos |

Cambiar este vocabulario es cambiar un identificador del registro (00-marco §7).

### 2.4 Herramientas (`tool`)

| `tool` | Herramienta | Cómo se detecta en v0.1 |
|---|---|---|
| `claude-code` | Claude Code en cualquier superficie | Variables de entorno de Claude Code (§2.6) |
| `codex` | Codex | `AUCOVE_AI_TOOL=codex` en la configuración MCP de la puerta |
| `antigravity-cli` | Antigravity CLI, que reemplazó a Gemini CLI para las cuentas personales (00-marco D-58) | `AUCOVE_AI_TOOL=antigravity-cli` en la configuración MCP; se verifica en S7 (12-mvp) |
| `gemini-cli` | Gemini CLI | `AUCOVE_AI_TOOL=gemini-cli` en la configuración MCP; en la CLI invocada desde su shell, `GEMINI_CLI=1` |
| `opencode` | OpenCode | `AUCOVE_AI_TOOL=opencode` |
| `goose` | goose | `AUCOVE_AI_TOOL=goose` |
| `other` | Cualquier otra, por ejemplo un chat web | Solo por declaración |

Solo Gemini CLI documenta una variable para sus subprocesos de shell (`GEMINI_CLI=1`); no la fija en los servidores MCP. La vía principal es `AUCOVE_AI_TOOL` en la configuración MCP: Codex `[mcp_servers.aucove] env = {AUCOVE_AI_TOOL = "codex"}`, Gemini CLI `"env": {"AUCOVE_AI_TOOL": "gemini-cli"}`, OpenCode `"environment": {"AUCOVE_AI_TOOL": "opencode"}`; goose: sin verificar (supuesto S-6, §10).

- **REQ-TRACE-9:** El núcleo DEBE aceptar en `Assisted-by` cualquier `tool` que cumpla la gramática y DEBERÍA emitir una advertencia si no está en esta tabla.

### 2.5 Quién escribe cada trailer

| Origen del commit o texto | Trailers | ¿Entra en la agregación de `submit`? |
|---|---|---|
| `claim` (commit vacío de reclamo) | `Task`, `AI-Use: none` | no |
| `save` | `Task`, `Section`, `AI-Use`, `Assisted-by` | sí |
| `update`, si crea un commit de merge desde la base | `Task` | no |
| `submit`: commit de la nota de sesión | `Task`, `AI-Use` y `Assisted-by` del contexto de la llamada | no |
| `submit`: cuerpo del PR | bloque completo (§2.7) | — |
| Otras operaciones que crean commits (`decision propose`, `sync-roles`) | como `save`; `Task` solo en `task/<n>`; `sync-roles` usa `AI-Use: none` | sí, si están en `task/<n>` |
| Commit no gestionado (git a mano, o el agente llamando a `git commit`) | los que traiga | sí; sin `AI-Use` cuenta como `undeclared` |

- **REQ-TRACE-10:** `save` DEBE derivar las líneas `Section` de las rutas `sections/<section-id>/**` que el commit modifica, no de las secciones que declara la tarea.
- **REQ-TRACE-11:** `save` NO DEBE escribir `Signed-off-by`.
- **REQ-TRACE-12:** Si una operación que crea commits no recibe una declaración de uso, el núcleo DEBE escribir `AI-Use: undeclared`, salvo en `claim` y `sync-roles`, que según la tabla anterior escriben `AI-Use: none`.

Entradas de declaración (el catálogo de operaciones lo define 05-operaciones):

| Campo MCP | Opción CLI | Tipo | Operaciones |
|---|---|---|---|
| `ai_use` | `--ai-use` | array de enum [`none`, `ideation`, `literature`, `drafting`, `editing`, `translation`, `code`, `analysis`, `figures`, `review`], `uniqueItems` (en la CLI, separado por comas) | `save`, `submit` y las que crean commits |
| `ai_tool` | `--ai-tool` | `slug`, 32 caracteres como máximo | ídem |
| `ai_model` | `--ai-model` | cadena con el patrón de `model` (§2.2) | ídem |
| `ai_note` | `--ai-note` | texto, 500 caracteres como máximo | solo `submit` |
| `sign_off` | `--sign-off` | booleano | solo `submit` |

No existe `assisted_by` como entrada: las entradas `Assisted-by` se derivan según §2.6.

### 2.6 Detección de la herramienta y registro del modelo

Variables que Claude Code expone (verificadas en su documentación):

| Variable | Valor | Dónde aparece |
|---|---|---|
| `CLAUDECODE` | `1` | Subprocesos de Claude Code: Bash, PowerShell, hooks y servidores MCP stdio. **También en las terminales integradas de las extensiones de IDE.** |
| `CLAUDE_CODE_CHILD_SESSION` | `1` | Bash, PowerShell, Monitor, hooks y línea de estado. **No** en servidores MCP stdio ni en terminales de IDE. |
| `CLAUDE_CODE_SESSION_ID` | ID de sesión | Bash, hooks y MCP stdio. El servidor MCP conserva el ID con el que arrancó. |
| `CLAUDE_CODE_REMOTE` | `true` | Sesiones en la nube (web). |

No existe `$CLAUDE_MODEL`. El modelo solo llega por hooks: `SessionStart` recibe `model`, aunque puede omitirlo (por ejemplo, tras `/clear`), y `PostModelSwitch` (Claude Code v2.1.251 o posterior) recibe `to_model` cada vez que el modelo cambia en la sesión. Ambos son **declarados por la sesión**.

Registro de sesión, en `.aucove/state/sessions/<session_id>.json` (clase `local`):

```json
{"session_id": "abc123", "tool": "claude-code", "model": "claude-opus-5-5",
 "model_event": "PostModelSwitch", "updated_at": "2026-09-29T15:00:00Z"}
```

- **REQ-TRACE-13:** `aucove hook session-start` DEBE guardar `model` cuando venga, y `aucove hook post-model-switch` DEBE actualizarlo con `to_model`.
- **REQ-TRACE-14:** Para leer el modelo, el núcleo DEBE usar solo el registro de `CLAUDE_CODE_SESSION_ID` y NO DEBE usar registros de otras sesiones. Si no existe el registro de `CLAUDE_CODE_SESSION_ID`, el modelo es `unknown` (salvo que `AUCOVE_AI_MODEL` lo declare, como en `resolve_assistance`).
- **REQ-TRACE-15:** El núcleo NO DEBE atribuir `claude-code` a una invocación de la CLI que tenga `CLAUDECODE=1` sin `CLAUDE_CODE_CHILD_SESSION=1` y que no llegue por MCP, porque en la terminal de un IDE escribe la persona.
- **REQ-TRACE-16:** `AUCOVE_AI_TOOL` DEBE tener precedencia sobre la detección por `GEMINI_CLI` y por `CLAUDECODE`.
- **REQ-TRACE-17:** Si la herramienta declarada coincide con la detectada pero el modelo declarado difiere del registrado, el núcleo DEBE conservar ambas entradas `Assisted-by` y devolver la advertencia `W_VALIDATION_MODEL_MISMATCH`.

```python
def detect_tool(env: Mapping[str, str], via_mcp: bool) -> Detection | None:
    if env.get("AUCOVE_AI_TOOL"):
        return Detection(env["AUCOVE_AI_TOOL"], source="env:AUCOVE_AI_TOOL")
    if env.get("GEMINI_CLI") == "1":
        return Detection("gemini-cli", source="env:GEMINI_CLI")
    if env.get("CLAUDECODE") == "1" and (via_mcp or env.get("CLAUDE_CODE_CHILD_SESSION") == "1"):
        return Detection("claude-code", source="env:CLAUDECODE")
    return None  # sin agente detectado: CLI de la persona o terminal integrada del IDE

def resolve_assistance(det, session, env, declared_tool, declared_model) -> tuple[list[Entry], list[str]]:
    entries, warnings = [], []
    if det:
        if det.tool == "claude-code" and session and session.model:
            model, msrc = session.model, f"hook:{session.model_event}"
        elif env.get("AUCOVE_AI_MODEL"):
            model, msrc = env["AUCOVE_AI_MODEL"], "env:AUCOVE_AI_MODEL"
        else:
            model, msrc = "unknown", "none"
        entries.append(Entry(det.tool, model, det.source, msrc))
    if declared_tool or declared_model:
        tool = declared_tool or (det.tool if det else "other")
        entry = Entry(tool, declared_model or "unknown", "declared", "declared")
        if det and tool == det.tool and entry.model not in ("unknown", entries[0].model):
            warnings.append("W_VALIDATION_MODEL_MISMATCH")
        entries.append(entry)
    return normalize(entries), warnings  # deduplica, quita tool:unknown si hay tool:<modelo>, ordena
```

Si `AI-Use` es `none`, las entradas no se escriben como trailers (REQ-TRACE-6). Si además se detectó un agente, se devuelve la advertencia `W_VALIDATION_NONE_WITH_AGENT`. El caso es legítimo: el agente solo guardó texto que escribió la persona.

### 2.7 Agregación en `submit`

```python
def aggregate(commits: list[Commit], decl: Declaration) -> Trace:
    relevant = [c for c in commits
                if not c.is_merge and not c.only_touches(note_path)
                and c.tree != c.parent_tree]                  # fuera los commits vacíos (p. ej., el de `claim`)
    uses, entries, by_section = set(), set(), defaultdict(set)
    for c in relevant:
        t = parse_trailers(c.message)
        cu = set(t.ai_use or ["undeclared"])
        entries |= set(t.assisted_by) | set(co_authored_by_as_assistance(t))  # §2.9
        uses |= cu
        for s in sections_modified(c):                     # del diff, no de los trailers
            by_section[s] |= cu
    concrete = uses - {"none", "undeclared"}
    if "undeclared" in uses and not decl.ai_use:
        raise TraceError("E_VALIDATION_AI_USE_UNDECLARED")
    if decl.ai_use and not concrete <= set(decl.ai_use) - {"none"}:
        raise TraceError("E_VALIDATION_AI_USE_CONFLICT")
    final = set(decl.ai_use) if decl.ai_use else (concrete or {"none"})
    for s in by_section:                                   # `undeclared` se reemplaza por la declaración
        by_section[s] = (by_section[s] - {"undeclared"}) | (final if "undeclared" in by_section[s] else set())
        by_section[s] = (by_section[s] - {"none"}) or {"none"}
    entries |= set(decl.assisted_by)                       # §2.6 con el contexto de submit
    if final - {"none"} and not entries:
        raise TraceError("E_VALIDATION_ASSISTED_BY_MISSING")
    return Trace(ai_use=canonical(final), assisted_by=sorted(entries), ai_use_by_section=by_section)
```

- **REQ-TRACE-18:** `submit` DEBE calcular `AI-Use` como la unión de los usos de los commits de `merge-base..HEAD`, excluyendo los commits de merge, los que solo modifican `memory/notes/task-<n>.md` y los commits vacíos (mismo árbol que su padre).
- **REQ-TRACE-19:** Si la unión contiene `undeclared`, `submit` DEBE exigir una declaración explícita en `ai_use` y fallar con `E_VALIDATION_AI_USE_UNDECLARED` si no la recibe.
- **REQ-TRACE-20:** Si la declaración de `submit` omite un uso concreto que ya registra algún commit, `submit` DEBE fallar con `E_VALIDATION_AI_USE_CONFLICT`.
- **REQ-TRACE-21:** `submit` DEBE escribir los mismos `ai_use` y `assisted_by` en tres lugares: el front matter de la nota de sesión, el objeto `trace` del bloque del PR y el párrafo de trailers del cuerpo del PR.

La nota de sesión forma parte del diff que se revisa y se aprueba. Por eso su front matter es la copia más confiable de la traza (§3.4): editar el cuerpo del PR después de la aprobación no la cambia.

### 2.8 `Signed-off-by` y declaración de responsabilidad

En los repositorios de proyecto, `Signed-off-by` **no** es el DCO. Significa que la persona acepta la declaración `attest-v1`:

> **es:** Declaro que revisé personalmente el contenido de esta entrega, incluidas las partes elaboradas con asistencia de IA; que la declaración de uso de IA es correcta y completa, y que asumo la responsabilidad por este contenido ante el equipo del proyecto.
>
> **en:** I declare that I have personally reviewed the content of this submission, including any parts produced with AI assistance; that the AI-use declaration is accurate and complete; and that I take responsibility for this content before the project team.

- **REQ-TRACE-22:** `submit` DEBE escribir `Signed-off-by` solo si la persona confirmó `attest-v1` por uno de tres canales: TTY, la opción `--sign-off` o `sign_off: true` en la llamada MCP. Si no hay confirmación, DEBE fallar con `E_VALIDATION_SIGNOFF_MISSING`.
- **REQ-TRACE-72:** `E_VALIDATION_AI_USE_UNDECLARED`, `E_VALIDATION_AI_USE_CONFLICT`, `E_VALIDATION_SIGNOFF_MISSING` y `E_VALIDATION_ASSISTED_BY_MISSING` son errores de operación de `submit` (05-operaciones §12.1); `E_VALIDATION_TRAILER_*`, `E_VALIDATION_SIGNOFF_MISMATCH` y `E_VALIDATION_TRACE_MISMATCH` son códigos de hallazgo del check `traceability`.
- **REQ-TRACE-23:** Los skills de entrega DEBEN mostrar a la persona el texto `attest-v1` en su idioma y pedirle una confirmación explícita antes de llamar a `submit_task` con `sign_off: true`.
- **REQ-TRACE-24:** En `Signed-off-by`, el nombre DEBE salir de `people[].name` y el correo de `people[].email` de `roles.yaml` de la base (entrada del dueño del reclamo); si no hay `email`, el núcleo DEBE usar `<id>+<login>@users.noreply.github.com` con `<id>` = `people[].forge_id` o, si falta, el `id` de `GET /user`. El núcleo NO DEBE tomar el nombre ni el correo de `git config`.
- **REQ-TRACE-25:** El objeto `trace` DEBE registrar el canal de la confirmación: `tty`, `cli-flag` o `mcp`.
- **REQ-TRACE-26:** En repositorios públicos, el correo de `roles.yaml` DEBERÍA ser la dirección *noreply* de GitHub de la persona.

**Límite conocido:** la confirmación por `mcp` la transmite el agente. No es prueba de un acto humano (Q-TRACE-1). La guía del kernel de Linux prohíbe que un agente agregue `Signed-off-by`. Aquí el agente tampoco lo escribe: lo escribe el núcleo con los datos de `roles.yaml`, tras la confirmación que el skill le pidió a la persona.

### 2.9 Relación con el `Co-Authored-By` de Claude Code

Hechos verificados en settings-reference:

- Por defecto, Claude Code agrega a sus commits `Co-Authored-By: <name> <noreply@anthropic.com>`, donde `<name>` es el modelo en uso (por ejemplo, `Claude Sonnet 5`). Escribe `Claude` si no puede confirmar la versión y `Claude Code` si el modelo no es de Claude.
- A los PR que Claude crea les agrega `🤖 Generated with [Claude Code](https://claude.com/claude-code)`. En sesiones en la nube o de Remote Control, además añade el trailer `Claude-Session` con el enlace a la sesión.
- Se configura con `attribution.commit`, `attribution.pr` y `attribution.sessionUrl`. `attribution: false` requiere v2.1.281 o posterior. `includeCoAuthoredBy` está obsoleto desde v2.0.62.
- Las instrucciones de CLAUDE.md o de memoria prevalecen sobre estas líneas, salvo que vengan de managed settings.
- Claude solo agrega este trailer cuando **él** redacta el commit. Los commits de `save` no lo llevan, y con squash sobre `PR_BODY` los mensajes de la rama no llegan a la base (§3).

- **REQ-TRACE-27:** El núcleo NO DEBE escribir un trailer `Co-authored-by` que nombre a una herramienta o a un modelo de IA.
- **REQ-TRACE-28:** La plantilla NO DEBE fijar la clave `attribution` en `.claude/settings.json`. El comportamiento por defecto se conserva como señal secundaria. Una institución PUEDE fijarla en managed settings.
- **REQ-TRACE-29:** El agregador DEBE tratar un commit que tenga `Co-Authored-By` con correo `noreply@anthropic.com` como asistido por `claude-code:unknown` (`tool_source: co-authored-by`), nunca como autoría.

### 2.10 Lectura y validación

Comportamiento de git, verificado en su documentación y probado con git 2.54: el bloque de trailers es el último párrafo y debe ir precedido de una línea en blanco; las claves se comparan sin distinguir mayúsculas; `git log --format='%(trailers:key=Task,valueonly)'` ignora las líneas `---`, pero `git interpret-trailers --parse`, leyendo de la entrada estándar, las toma como separador y pierde los trailers salvo que se use `--no-divider`. Si al final hay un párrafo aparte de `Co-authored-by`, el bloque anterior deja de contar como trailers.

- **REQ-TRACE-30:** El analizador del núcleo DEBE tomar como bloque de trailers el último párrafo del mensaje. Si ese párrafo solo contiene `Co-authored-by` o `Claude-Session` y el párrafo anterior está formado solo por trailers, DEBE unir ambos.
- **REQ-TRACE-31:** El analizador DEBE comparar claves sin distinguir mayúsculas, ignorar claves desconocidas y no tratar `---` como separador. Si usa `git interpret-trailers`, DEBE pasar `--no-divider`. El analizador DEBE normalizar `\r\n` a `\n` antes de extraer trailers.
- **REQ-TRACE-32:** `validate_trailers(message, context)` DEBE implementar los tres contextos y los errores de la tabla siguiente.

| Contexto | Exige | Errores |
|---|---|---|
| `branch-commit` | `AI-Use`; `Task` en `task/<n>`; gramática de §2.2 | `E_VALIDATION_TRAILER_SYNTAX`, `E_VALIDATION_TRAILER_MISSING` |
| `pr-body` | lo anterior + REQ-TRACE-7 + `Signed-off-by` con el `name` exacto de la entrada del autor del PR en `roles.yaml` y un `email` igual a su `people[].email` o a la forma *noreply* de REQ-TRACE-24 + igualdad con la nota de sesión | los anteriores + `E_VALIDATION_SIGNOFF_MISMATCH`, `E_VALIDATION_TRACE_MISMATCH` |
| `squash-commit` | lo de `pr-body`, leyendo el mensaje del commit en la base; hasta que S-10 lo confirme, la ausencia del bloque legible por máquina es advertencia | ídem |

Como son códigos de hallazgo (REQ-TRACE-72), en `pr-body` y `squash-commit` un `AI-Use: undeclared` y un `Signed-off-by` o un `Assisted-by` exigidos que falten se reportan como `E_VALIDATION_TRAILER_MISSING`.

En PR que no salen de `task/<n>` (por ejemplo, uno abierto a mano en la web), los errores de `pr-body` se reportan como advertencias.

### 2.11 Ejemplos

Commit de `save`:

```text
Agrega criterios de inclusión al diseño muestral

Task: #42
Section: methods
AI-Use: drafting, editing
Assisted-by: claude-code:claude-opus-5-5
```

Commit de squash en `main`. El título y el cuerpo los compuso `submit`; GitHub agrega `(#57)`:

```text
Métodos: diseño muestral y criterios de inclusión (#57)

## Resumen
Se redactó el diseño muestral y se agregaron los criterios de inclusión.

<!-- bloque legible por máquina (formato: 03-esquemas), con el objeto "trace" de §3.3 -->

Task: #42
Section: methods
AI-Use: literature, drafting, editing
Assisted-by: claude-code:claude-opus-5-5
Assisted-by: other:unknown
Signed-off-by: Ana Pérez <12345+ana-perez@users.noreply.github.com>
```

## 3. Supervivencia al merge

### 3.1 Decisión: squash con el cuerpo del PR como mensaje

| Estrategia | Ventaja | Por qué se descarta o se elige |
|---|---|---|
| Commit de merge | Conserva cada commit de `save` con sus trailers. | Llena la base de commits intermedios. La aprobación sigue sin quedar en git. Revertir una tarea exige revertir un merge. |
| Rebase | Conserva los commits. | Reescribe los SHA. No hay un lugar para el bloque ni para el `Signed-off-by` de la entrega. |
| **Squash, título `PR_TITLE`, mensaje `PR_BODY`** | Un commit por tarea (D-12). El mensaje lo compone el núcleo de forma determinista y lleva el título con `(#<pr>)`, el bloque y los trailers. | **Elegida.** GitHub recomienda squash para ramas de vida corta, como `task/<n>`. La pérdida de granularidad se compensa con la nota de sesión y con `GET /repos/{owner}/{repo}/pulls/{pull_number}/commits`, que lista hasta 250 commits. |

Con squash, GitHub propone un mensaje por defecto que quien integra puede editar, y acredita como coautor a cada autor de commit del PR. No documenta en qué posición agrega esos `Co-authored-by`; REQ-TRACE-30 tolera ambos casos (Q-TRACE-2).

- **REQ-TRACE-33:** La única estrategia de integración admitida DEBE ser squash, con título por defecto `PR_TITLE` y mensaje por defecto `PR_BODY`.
- **REQ-TRACE-34:** Quien integra NO DEBE editar el párrafo de trailers en el diálogo de squash.

### 3.2 Configuración que aplica `init`

La lista de protecciones y el orden los fija 06-permisos. La llamada la implementa 08-forja.

| Campo (`PATCH /repos/{owner}/{repo}`, requiere admin) | Valor |
|---|---|
| `allow_squash_merge` | `true` |
| `allow_merge_commit` | `false` |
| `allow_rebase_merge` | `false` |
| `squash_merge_commit_title` | `PR_TITLE` (obligatorio cuando se envía `squash_merge_commit_message`) |
| `squash_merge_commit_message` | `PR_BODY` |
| Solo perfil `enforced`: regla `pull_request` del ruleset de la rama base | `allowed_merge_methods: ["squash"]` |

- **REQ-TRACE-35:** `init` DEBE aplicar los valores de esta tabla. En perfil `advisory` DEBE aplicar solo los campos del repositorio y avisar que no se puede exigir el método.
- **REQ-TRACE-36:** `aucove doctor` DEBE reportar el check `merge-settings` en `fail` (y la operación `E_CONFIG_DOCTOR_FAILED`) si `GET /repos/{owner}/{repo}` devuelve valores distintos a los de la tabla.

### 3.3 Cuerpo del PR y objeto `trace`

El formato del bloque legible por máquina, sus delimitadores y las secciones de la plantilla de PR los define 03-esquemas. Este capítulo define el objeto `trace` que va dentro del bloque y una restricción de orden: **el párrafo de trailers es lo último del cuerpo.**

- **REQ-TRACE-37:** El cuerpo de todo PR que abra el núcleo DEBE terminar en el párrafo de trailers, sin nada después.
- **REQ-TRACE-38:** El cuerpo del PR que compone el núcleo NO DEBERÍA contener líneas que empiecen con `---`.
- **REQ-TRACE-39:** El objeto `trace` DEBE validar contra `https://aucove.invalid/schemas/trace/v1`.

```json
{
  "trace": {
    "version": 1,
    "task": 42,
    "sections": ["methods"],
    "ai_use": ["literature", "drafting", "editing"],
    "ai_use_by_section": {"methods": ["drafting", "editing"]},
    "assisted_by": [
      {"tool": "claude-code", "model": "claude-opus-5-5", "tool_source": "env:CLAUDECODE", "model_source": "hook:PostModelSwitch"},
      {"tool": "other", "model": "unknown", "tool_source": "declared", "model_source": "declared"}
    ],
    "ai_note": "Búsqueda inicial de literatura en un chat web; el texto se redactó en Claude Code y lo revisé.",
    "sign_off": {"login": "ana-perez", "name": "Ana Pérez", "email": "12345+ana-perez@users.noreply.github.com",
                 "channel": "mcp", "attestation": "attest-v1", "at": "2026-09-29T15:04:05Z"},
    "commits": {"base": "9f2c1e7", "head": "a1b3d44", "count": 7, "undeclared": 2},
    "core_version": "0.1.0"
  }
}
```

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://aucove.invalid/schemas/trace/v1",
  "type": "object",
  "additionalProperties": false,
  "required": ["version", "ai_use", "assisted_by", "sign_off", "commits", "core_version"],
  "properties": {
    "version": {"const": 1},
    "task": {"type": "integer", "minimum": 1},
    "sections": {"type": "array", "uniqueItems": true, "items": {"$ref": "#/$defs/slug"}},
    "ai_use": {"type": "array", "minItems": 1, "uniqueItems": true, "items": {"$ref": "#/$defs/use"}},
    "ai_use_by_section": {"type": "object", "propertyNames": {"$ref": "#/$defs/slug"},
                          "additionalProperties": {"type": "array", "items": {"$ref": "#/$defs/use"}}},
    "assisted_by": {"type": "array", "items": {"type": "object", "additionalProperties": false,
      "required": ["tool", "model", "tool_source", "model_source"],
      "properties": {
        "tool": {"$ref": "#/$defs/slug"},
        "model": {"type": "string", "pattern": "^[A-Za-z0-9._:@/+-]{1,128}$"},
        "tool_source": {"enum": ["env:CLAUDECODE", "env:AUCOVE_AI_TOOL", "env:GEMINI_CLI", "declared", "co-authored-by"]},
        "model_source": {"enum": ["hook:SessionStart", "hook:PostModelSwitch", "env:AUCOVE_AI_MODEL", "declared", "none"]}}}},
    "ai_note": {"type": "string", "maxLength": 500},
    "sign_off": {"type": "object", "additionalProperties": false,
      "required": ["login", "name", "email", "channel", "attestation", "at"],
      "properties": {"login": {"type": "string"}, "name": {"type": "string"}, "email": {"type": "string"},
        "channel": {"enum": ["tty", "cli-flag", "mcp"]}, "attestation": {"const": "attest-v1"},
        "at": {"type": "string", "format": "date-time"}}},
    "commits": {"type": "object", "required": ["base", "head", "count", "undeclared"],
      "properties": {"base": {"type": "string"}, "head": {"type": "string"},
        "count": {"type": "integer", "minimum": 0}, "undeclared": {"type": "integer", "minimum": 0}}},
    "core_version": {"type": "string"}
  },
  "$defs": {
    "slug": {"type": "string", "pattern": "^[a-z0-9]+(-[a-z0-9]+)*$"},
    "use": {"enum": ["none", "ideation", "literature", "drafting", "editing", "translation", "code", "analysis", "figures", "review"]}
  }
}
```

### 3.4 Verificaciones y orden de lectura

- **REQ-TRACE-40:** El check `traceability` (09-ci) DEBE ejecutarse al abrir, editar y actualizar el PR, validar su cuerpo en el contexto `pr-body` y comprobar que el bloque coincide con el front matter de la nota de sesión.
- **REQ-TRACE-41:** Después de cada push a la rama base, CI DEBE validar el commit de squash en el contexto `squash-commit`, en el job `aucove/base-audit` (09-ci §7), y reportar las diferencias, que ya no puede bloquear.
- **REQ-TRACE-42:** Los generadores de reportes DEBEN leer la traza en este orden y marcar `trace_conflict` cuando las fuentes difieran: (1) front matter de la nota de sesión en el árbol integrado; (2) trailers del commit de squash; (3) objeto `trace` del PR, vía API; (4) trailers de los commits del PR, vía API.

## 4. Libro de aprobaciones

### 4.1 Decisión

En GitHub, la aprobación vive en las revisiones del PR, no en git. **Decisión: la fuente de verdad es la API de la forja.** El libro se **deriva** con una función pura al generar reportes. La copia portable dentro de git se hace por **exportación en un PR del responsable**, en hitos: antes de enviar a revista, archivar o migrar. Esa operación queda reservada en v0.1.

| Opción | Se descarta porque |
|---|---|
| Un job posterior al merge que agrega líneas a un archivo en la rama base | El bot necesitaría saltarse la protección de la base (06-permisos), y un workflow con `contents: write` sobre `main` amplía la superficie de ataque (10-seguridad). Además genera commits de ruido que obligan a `update` en cada rama. En perfil `advisory` no aporta más integridad que la API. |
| Trailers `Approved-by` escritos en el cuerpo del PR antes del merge | Cualquiera con escritura, o el agente con el token de la persona, puede editar el cuerpo. Aparece una carrera entre aprobar e integrar. En GitHub, la aprobación no se puede verificar desde git. |
| Solo la API, sin exportación | El rastro se pierde al archivar el repo como paquete git o al migrar de forja. |

Se elige esta opción porque la exportación conserva D-09 (integran personas), la revisa el suplente como cualquier PR del responsable (D-17), y cada registro guarda su URL de origen, la fecha de consulta y un hash que permite volver a verificarlo contra la API mientras exista.

- **REQ-TRACE-43:** El núcleo NO DEBE escribir trailers de aprobación ni aceptar aprobaciones declaradas en archivos del repositorio como evidencia.
- **REQ-TRACE-44:** Ningún workflow DEBE escribir registros de aprobación en la rama base.
- **REQ-TRACE-45:** La función `derive_approval_record` DEBE existir en el núcleo v0.1, ser pura y tener pruebas contra el adaptador falso (08-forja).
- **REQ-TRACE-46:** La exportación reservada `aucove report approvals` DEBE crear archivos nuevos en `records/approvals/approvals-<YYYYMMDD>.jsonl`, uno por línea y por PR, mediante un PR del responsable. En la convención 0.1, `records/approvals/` es de clase `lead` (02-convencion §13).

### 4.2 Registro `approval-record/v1`

```json
{"schema": "https://aucove.invalid/schemas/approval-record/v1",
 "pr": 57, "task": 42, "title": "Métodos: diseño muestral y criterios de inclusión",
 "author": "ana-perez", "base_branch": "main", "head_sha": "a1b3d44", "merge_commit_sha": "c0ffee1",
 "merged_by": "lead-login", "merged_at": "2026-10-02T18:20:00Z", "method": "squash", "profile": "enforced",
 "reviews": [{"login": "lead-login", "role": "lead", "state": "APPROVED",
              "submitted_at": "2026-10-02T17:55:00Z", "commit_id": "a1b3d44", "on_head": true}],
 "approved_by": ["lead-login"], "approval_status": "approved", "merge_audit": "compliant",
 "labels": ["flag:overlap", "ack:direction"], "roles_yaml_sha": "5d1e0aa",
 "source": {"forge": "github", "api": "https://api.github.com/repos/org/estudio-x/pulls/57",
            "fetched_at": "2026-12-01T10:00:00Z", "sha256": "…"}}
```

`approval_status` ∈ `approved`, `approved_stale`, `changes_requested`, `missing`, `not_merged`.

`merge_audit` ∈ `compliant`, `bypass_no_deputy`, `bypass`, `direct_push`, `force_push`: la clasificación de la auditoría posterior a la integración (06-permisos §3.5).

### 4.3 Derivación

Datos de GitHub (REST): `GET /repos/{owner}/{repo}/pulls/{pull_number}` (`merged_at`, `merged_by`, `merge_commit_sha`, `head.sha`) y `GET /repos/{owner}/{repo}/pulls/{pull_number}/reviews` (orden cronológico; `user.login`, `state`, `submitted_at`, `commit_id`). Los estados salen del enum `PullRequestReviewState` de GraphQL: `APPROVED`, `CHANGES_REQUESTED`, `COMMENTED`, `DISMISSED`, `PENDING`.

```python
EFFECTIVE = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}

def derive_approval_record(pr, reviews, roles_at_base, profile, merge_audit, fetched) -> dict:
    last = {}
    for r in sorted(reviews, key=lambda r: (r.submitted_at or "", r.id)):
        if r.state in EFFECTIVE:
            last[r.login or "ghost"] = r            # cuenta borrada -> "ghost"
    allowed = qualifying_approvers(pr, roles_at_base)  # 06-permisos §3.2 (política de la base)
    ok = [r for login, r in last.items() if r.state == "APPROVED" and login in allowed]
    if not pr.merged_at:                               status = "not_merged"
    elif any(r.state == "CHANGES_REQUESTED" for r in last.values()): status = "changes_requested"
    elif any(r.commit_id == pr.head_sha for r in ok):  status = "approved"
    elif ok:                                           status = "approved_stale"
    else:                                              status = "missing"
    return build_record(pr, last, ok, status, profile, merge_audit, roles_at_base.sha, fetched)  # merge_audit: 06 §3.5
```

- **REQ-TRACE-47:** Una revisión DEBE contar como aprobación solo si es la última revisión de esa persona con estado `APPROVED`, `CHANGES_REQUESTED` o `DISMISSED`, si ese estado es `APPROVED` y si la persona está en `qualifying_approvers`, la función de 06-permisos §3.2.
- **REQ-TRACE-48:** `roles_at_base` DEBE leerse de `roles.yaml` en el primer padre del commit de squash (D-08).

### 4.4 Otras forjas (fuera de v0.1)

En GitLab, la plantilla de commit de merge o de squash admite `%{approved_by}`, que escribe trailers `Approved-by`. El adaptador de GitLab PUEDE usarla como fuente adicional, pero la derivación por API sigue siendo la referencia.

## 5. Declaración de uso de IA

### 5.1 Datos que se capturan en v0.1

| Dato | Granularidad | Fuente (declarativa) | Dónde queda |
|---|---|---|---|
| Usos (`AI-Use`) | commit, sección, tarea | persona o agente | trailers, nota de sesión, `trace` |
| Herramienta y modelo | commit, tarea | entorno, hooks o declaración | `Assisted-by`, `trace.assisted_by` |
| Origen de la herramienta y del modelo | tarea | núcleo | `trace.assisted_by[].*_source` |
| Nota libre sobre el uso | tarea | persona | `trace.ai_note` |
| Confirmación de responsabilidad | tarea | persona | `Signed-off-by`, `trace.sign_off` |
| Secciones tocadas | commit, tarea | diff | `Section`, `trace.sections` |
| Aprobaciones | PR | API de la forja | se derivan al reportar (§4) |

- **REQ-TRACE-49:** En v0.1 el núcleo DEBE capturar para trazabilidad exactamente los datos de esta tabla.

### 5.2 Formato de `aucove report ai-use` (reservado)

Entrada: rango de commits de la base (`--since`/`--until`, SHA o etiqueta) e idioma (`es`/`en`). Salidas: JSON `ai-use-report/v1` y texto (Markdown o fragmento LaTeX) generado con las plantillas de §5.3.

```json
{"schema": "https://aucove.invalid/schemas/ai-use-report/v1",
 "repo": "org/estudio-x", "range": {"from": "v-submitted-1", "to": "c0ffee1"}, "generated_at": "2026-12-01T10:00:00Z",
 "uses": ["literature", "drafting", "editing", "code"],
 "tools": [{"tool": "claude-code", "models": ["claude-opus-5-5"], "tasks": [42, 51]}],
 "by_section": {"methods": {"uses": ["drafting", "editing"], "tasks": [42],
                            "signed_off_by": ["ana-perez"], "approved_by": ["lead-login"]}},
 "tasks": [{"task": 42, "pr": 57, "ai_use": ["literature", "drafting", "editing"],
            "assisted_by": ["claude-code:claude-opus-5-5", "other:unknown"], "signed_off_by": "ana-perez",
            "approval_status": "approved", "trace_conflict": false}],
 "gaps": {"commits_without_trace": [], "tasks_with_conflict": []},
 "caveat": "covers-repository-only"}
```

- **REQ-TRACE-50:** El reporte DEBE declarar que solo cubre el trabajo registrado en el repositorio y DEBE listar en `gaps` los commits de la base sin traza y las tareas con `trace_conflict`.
- **REQ-TRACE-51:** El texto generado DEBE nombrar la herramienta y el modelo, describir los usos y afirmar la responsabilidad humana, y NO DEBE presentar a la IA como autora.

### 5.3 Plantillas de declaración

Base normativa. COPE: las herramientas de IA no pueden figurar como autoras. Quien las use para redactar, producir imágenes o reunir y analizar datos debe declarar en Métodos (o una sección equivalente) qué herramienta usó y cómo. Los autores responden por todo el contenido. ICMJE: la declaración va en la carta de presentación y en el trabajo; la ayuda en la redacción, en agradecimientos; el uso para datos, análisis o figuras, en Métodos. Los chatbots no son autores y el material generado por IA no es una fuente primaria citable. La plantilla se elige por el uso, según la última columna de §2.3.

`{herramientas}` se reemplaza por "Claude Code (modelo claude-opus-5-5)", y `{usos}` por las etiquetas de §2.3 en el idioma elegido.

**A. Métodos (o sección equivalente)**

> **es:** *Uso de herramientas de inteligencia artificial.* En la preparación de este trabajo se usaron {herramientas} para {usos}, en las secciones {secciones}, entre {desde} y {hasta}. Al menos una de las personas autoras revisó, corrigió y aprobó cada contribución asistida antes de incorporarla. Las herramientas de IA no son autoras de este trabajo; las personas autoras asumen la responsabilidad total por su contenido.
>
> **en:** *Use of artificial intelligence tools.* In preparing this work we used {tools} for {uses}, in the sections {sections}, between {from} and {to}. At least one author reviewed, corrected and approved each assisted contribution before it was incorporated. AI tools are not authors of this work; the authors take full responsibility for its content.

**B. Agradecimientos (ayuda en la redacción)**

> **es:** Se usó {herramientas} como apoyo para {usos_redaccion}. Las personas autoras revisaron y editaron todo el texto resultante y responden por él.
>
> **en:** {tools} was used to assist with {writing_uses}. The authors reviewed and edited all resulting text and are responsible for it.

**C. Carta de presentación**

> **es:** En este trabajo se usaron herramientas de IA ({herramientas}); el uso se detalla en {seccion_declaracion}. Ninguna herramienta de IA figura como autora.
>
> **en:** AI tools ({tools}) were used in this work; their use is described in {statement_section}. No AI tool is listed as an author.

**D. Sin uso** (solo si todas las tareas del rango tienen `none`)

> **es:** Las personas autoras declaran que no usaron herramientas de IA generativa en la preparación de este trabajo.
>
> **en:** The authors declare that they did not use generative AI tools in preparing this work.

## 6. CRediT

### 6.1 Declaración en `roles.yaml`

El esquema de `roles.yaml` lo define 03-esquemas. Este capítulo fija el vocabulario y la semántica de las claves `credit` y `orcid`.

```yaml
people:
  - login: ana-perez
    name: Ana Pérez
    email: 12345+ana-perez@users.noreply.github.com
    role: lead
    account_type: team
    orcid: 0000-0002-1825-0097
    credit: [conceptualization, writing-original-draft]
  - login: luis-soto
    name: Luis Soto
    role: researcher
    account_type: team
    credit: [formal-analysis, software]
```

| Valor de `credit` (slug oficial de credit.niso.org) | Nombre oficial | es (orientativo) |
|---|---|---|
| `conceptualization` | Conceptualization | Conceptualización |
| `data-curation` | Data curation | Curación de datos |
| `formal-analysis` | Formal analysis | Análisis formal |
| `funding-acquisition` | Funding acquisition | Obtención de financiamiento |
| `investigation` | Investigation | Investigación |
| `methodology` | Methodology | Metodología |
| `project-administration` | Project administration | Administración del proyecto |
| `resources` | Resources | Recursos |
| `software` | Software | Software |
| `supervision` | Supervision | Supervisión |
| `validation` | Validation | Validación |
| `visualization` | Visualization | Visualización |
| `writing-original-draft` | Writing – original draft | Redacción – borrador original |
| `writing-review-editing` | Writing – review & editing | Redacción – revisión y edición |

El grado de contribución (`lead`, `equal`, `supporting`) no forma parte de ANSI/NISO Z39.104-2022; CRediT lo describe como práctica opcional (https://credit.niso.org/implementing-credit/) y JATS lo expresa con `@degree-contribution`. Queda reservado para `roles/v2` (Q-SCHEMA-2).

- **REQ-TRACE-52:** El núcleo NO DEBE deducir roles CRediT del historial; la única fuente DEBE ser `credit` en `roles.yaml` de la rama base.
- **REQ-TRACE-53:** `aucove validate` DEBE rechazar un valor de `credit` fuera de la tabla, un valor repetido en el `credit` de una misma persona y un `orcid` que no cumpla `^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$` o su dígito de control ISO 7064 11,2.

### 6.2 Confirmación del responsable

`roles.yaml` es de clase `lead`: cambia solo por un PR del responsable que aprueba el suplente, o con el bypass que defina 06-permisos. Así, lo que está en la base ya pasó por el responsable. Además, la confirmación final se da al generar el reporte.

- **REQ-TRACE-54:** `aucove report credit` (reservado) DEBE exigir que el responsable confirme por TTY y DEBE registrar en su salida el SHA de `roles.yaml` usado, el `login` de quien confirmó y la fecha.
- **REQ-TRACE-55:** El reporte PUEDE incluir como "evidencia informativa", en un campo separado, la actividad por persona (secciones de las que es dueña, tareas integradas, revisiones), pero NO DEBE asignar ni sugerir roles a partir de ella.

### 6.3 Formato de `aucove report credit` (reservado)

```json
{"schema": "https://aucove.invalid/schemas/credit-report/v1", "roles_yaml_sha": "5d1e0aa",
 "confirmed_by": "ana-perez", "confirmed_at": "2026-12-01T10:05:00Z",
 "contributors": [{"name": "Ana Pérez", "orcid": "0000-0002-1825-0097",
                   "credit": ["conceptualization", "writing-original-draft"]}],
 "evidence": {"ana-perez": {"sections_owned": ["introduction"], "tasks_merged": 6, "reviews": 14}}}
```

Texto por persona (en): `Ana Pérez: Conceptualization, Writing – original draft. Luis Soto: Formal analysis, Software.` El texto por rol agrupa a las personas bajo cada rol, en el orden de la tabla de §6.1.

### 6.4 Acuerdo de autoría y de apertura

Se firma al iniciar el proyecto, fuera del repo, porque contiene firmas. Fija el orden y los criterios de autoría (CRediT), las reglas de apertura (D-44), los custodios y su suplente, la exportación personal, y la base y el plazo de conservación del paquete de evidencia (D-46). La guía de H4 incluye un modelo de acuerdo en `es` y `en`.

- **REQ-TRACE-74:** La plantilla de `memory/constitution.md` DEBE tener un renglón para la fecha y el SHA-256 del acuerdo; el primer registro con `plan: true` los sella (§8.2).

## 7. Licencias

### 7.1 Framework: Apache-2.0

- **REQ-TRACE-56:** El código del núcleo DEBE publicarse bajo Apache-2.0, con `LICENSE` y `NOTICE` en la raíz, `license = "Apache-2.0 AND MIT-0 AND CC0-1.0"` (la rueda incluye la plantilla y los skills; D-07 a) y `license-files` en `pyproject.toml` con `LICENSE`, `NOTICE` y los textos de `LICENSES/` de esas tres licencias, sin otros (guía de ejemplos de licencias de packaging.python.org), y cabeceras `SPDX-License-Identifier` y `SPDX-FileCopyrightText` en cada archivo fuente, con el titular que resulte de REQ-TRACE-73.
- **REQ-TRACE-57:** Los recursos que el núcleo copia o genera dentro de un proyecto (plantillas de skills, de `AGENTS.md`, de workflows y de `settings.json`) DEBEN estar bajo MIT-0 en el repositorio del framework, para que el proyecto no herede obligaciones de Apache-2.0.
- **REQ-TRACE-58:** El repositorio del framework DEBE cumplir REUSE 3.3 (`reuse lint` sin errores), con `LICENSES/` y `REUSE.toml`. El CI del framework DEBE correr `reuse lint` también sobre el render de `template/` (01-arquitectura §10.5), con la imagen de REUSE fijada por `@sha256:` (o con una acción fijada por SHA cuya imagen también lo esté; REQ-SEC-47): fijar el SHA de una acción Docker no fija la imagen de su `FROM`.
- **REQ-TRACE-59:** El núcleo NO DEBE importar el paquete `reuse`, que incluye código GPL-3.0-or-later. CI PUEDE invocarlo como herramienta externa con versión fijada.
- **REQ-TRACE-73:** Cada desarrollador DEBERÍA confirmar con su institución quién es el titular de los derechos patrimoniales de su código (en México, salvo pacto en contrario, el art. 103 de la LFDA los atribuye al empleador cuando el programa se crea en ejercicio de sus funciones), y `NOTICE` y `SPDX-FileCopyrightText` DEBEN reflejar a ese titular cuando se confirme (00-marco D-07 b). La confirmación no bloquea hacer público el repositorio, aceptar PR externos ni publicar la plantilla; si una institución lo pide, se atiende con una carta aclaratoria. No es una conclusión legal.

### 7.2 Plantilla: MIT-0 (código) y CC0-1.0 (texto de ejemplo)

- **REQ-TRACE-60:** La plantilla DEBE incluir `LICENSE` con el texto de MIT-0, `LICENSES/MIT-0.txt`, `LICENSES/CC0-1.0.txt` y un `REUSE.toml` generado desde la clasificación de rutas de 02-convencion (respuesta a S-8: `code`, `data` y el resto `text`), como este, sin cabeceras SPDX por archivo:

```toml
version = 1

[[annotations]]   # texto de ejemplo, notas y `.md` de las partes, y configuración: CC0-1.0 (regla general)
path = ["**", "sections/*/notes/**", "sections/*/*.md"]
SPDX-FileCopyrightText = "2026 The aucove Authors"
SPDX-License-Identifier = "CC0-1.0"

[[annotations]]   # código: MIT-0 (en REUSE.toml gana la última tabla que coincide)
path = [".github/**", ".claude/**", ".agents/**", ".devcontainer/**", ".latexmkrc",
        ".gitignore", ".gitattributes", ".mcp.json", "tex/main.tex", "tex/preamble.tex",
        "tex/notation.tex", "sections/*/analysis/**"]
SPDX-FileCopyrightText = "2026 The aucove Authors"
SPDX-License-Identifier = "MIT-0"

[[annotations]]   # datos derivados del análisis: CC0-1.0
path = ["sections/*/analysis/derived/**"]
SPDX-FileCopyrightText = "2026 The aucove Authors"
SPDX-License-Identifier = "CC0-1.0"
```

Se usa `REUSE.toml` en lugar de cabeceras para que `init` pueda volver a anotar el proyecto sin tocar cada archivo. Con la precedencia por defecto (`closest`), un archivo que traiga su propia cabecera SPDX, como código de terceros, conserva la suya. Las figuras o el código de terceros que se agreguen después (por ejemplo, en `tex/assets/**` o `sections/<id>/figures/**`) se anotan con un archivo `.license` junto al original o con una entrada propia en `REUSE.toml`; la guía lo explica, porque `validate` no puede detectarlo.

### 7.3 `init`: licencias del proyecto

`init` es una operación solo humana (D-09). Pregunta tres licencias y el titular del copyright:

| Categoría | Sugerida si el repo es público | Sugerida si es privado | Otras opciones de la lista |
|---|---|---|---|
| `text` (manuscrito, `plan.md`, memoria) | `CC-BY-4.0` | `LicenseRef-AllRightsReserved` | `CC-BY-SA-4.0`, `CC0-1.0` |
| `code` | `MIT` | `MIT` | `Apache-2.0`, `BSD-3-Clause`, `GPL-3.0-or-later`, `MIT-0` |
| `data` | `CC0-1.0` | `LicenseRef-AllRightsReserved` | `CC-BY-4.0`, `ODbL-1.0` |

```yaml
# project.yaml (clave propuesta a 03-esquemas)
licenses:
  holder: "Los autores de estudio-x"
  text: CC-BY-4.0
  code: MIT
  data: CC0-1.0
```

- **REQ-TRACE-61:** `init` DEBE escribir `licenses` en `project.yaml`; reemplazar `LICENSE` por un resumen en texto plano de las tres licencias y sus rutas; dejar en `LICENSES/` exactamente los textos que usan los archivos según ese `REUSE.toml` (REUSE falla tanto con un texto sin uso como con el que falta; 00-marco D-64), y regenerar `REUSE.toml` con una regla general de `text` seguida de las reglas de `code` y de `data`. La última tabla DEBE cubrir los archivos `generated` (`.claude/skills/**`, `.agents/skills/**`) con MIT-0 y el titular «The aucove Authors», y `LICENSES/MIT-0.txt` se conserva mientras haya archivos bajo MIT-0 (desde H4, los skills generados). En las demás rutas que vienen de la plantilla y quedan bajo la licencia `code` del proyecto, `init` añade «The aucove Authors» como segundo titular (REUSE admite varios avisos).
- **REQ-TRACE-62:** `init` DEBE aceptar cualquier identificador de la lista SPDX (el núcleo incluye una copia, CC0) o un `LicenseRef-*` con su texto en `LICENSES/`, y DEBE generar `LICENSES/LicenseRef-AllRightsReserved.txt` cuando se elija esa opción. El paquete trae los textos de las licencias de la tabla; para otra, `init` pide la ruta de su texto y, sin ella, falla antes de escribir nada (D-64).
- **REQ-TRACE-63:** Antes de confirmar una licencia abierta para `text`, `init` DEBERÍA mostrar un aviso: conviene revisar la política de licencias y de preprints de la revista de destino.
- **REQ-TRACE-64:** `aucove validate` DEBE fallar con `E_VALIDATION_LICENSE_UNKNOWN` si un valor de `licenses` no es un identificador SPDX válido ni un `LicenseRef-*`, o si falta su archivo en `LICENSES/` habiendo archivos de esa categoría (D-64).

Las rutas de `code` y `data` salen de 02-convencion. El `.bib` DEBERÍA exportarse sin el campo `abstract`, para no redistribuir texto de terceros (el proceso lo define 02-convencion).

### 7.4 Documentación: CC-BY-4.0

- **REQ-TRACE-65:** La documentación del framework (`docs/**`) DEBE publicarse bajo CC-BY-4.0, y los ejemplos pensados para copiarse (`docs/examples/**`), bajo MIT-0, anotados en `REUSE.toml`.

### 7.5 DCO y política de contribuciones asistidas por IA (repo del framework)

- **REQ-TRACE-66:** Todo commit de un PR al repositorio del framework DEBE llevar `Signed-off-by` (DCO 1.1, https://developercertificate.org/) con el mismo nombre y correo del autor del commit, y un check de CI DEBE verificarlo. Quedan exentos los PR que la API de GitHub atribuye a `dependabot[bot]` (el usuario que abre el PR, no el correo del commit, que se puede falsificar); quien los integra responde por ese cambio (00-marco D-07 c). Así se resuelve el choque con REQ-SEC-51, que obliga a usar Dependabot, cuyos commits no cumplen la regla de nombre y correo. El repositorio del framework DEBE activar además la firma obligatoria de los commits hechos en la web de GitHub (*require contributors to sign off on web-based commits*).
- **REQ-TRACE-67:** `CONTRIBUTING.md` (en español e inglés) DEBE establecer que los agentes de IA NO DEBEN agregar `Signed-off-by`; que la persona revisa y prueba todo lo generado y responde por ello; que los commits asistidos llevan `Assisted-by: <tool>:<model>` con la gramática de §2.2, y que la IA no figura como `Co-authored-by`.
- **REQ-TRACE-68:** El `.claude/settings.json` del repositorio del framework DEBE fijar `"attribution": {"commit": ""}`, y su `AGENTS.md` DEBE pedir a los agentes el trailer `Assisted-by`.

Aquí la política es más estricta que en la plantilla (REQ-TRACE-28) porque en el framework cada commit llega a la rama principal y el DCO es un requisito legal de la contribución. El trailer `Assisted-by` de este capítulo es un formato propio documentado. La guía actual del kernel de Linux usa `Assisted-by: LLM [TOOL1] [TOOL2]`, sin agente ni modelo; lo que se adopta del kernel es la regla de que el `Signed-off-by` es solo humano.

### 7.6 Aviso de no afiliación y neutralidad del nombre

- **REQ-TRACE-69:** El nombre definitivo (D-03) DEBE ser ASCII en minúsculas y NO DEBE contener, sin distinguir mayúsculas, `claude`, `anthropic`, `codex`, `openai`, `gemini`, `google`, `github`, `gitlab` ni `copilot`, tampoco en nombres de plugin, catálogo, paquete o imagen.
- **REQ-TRACE-70:** Antes de la primera publicación, los mantenedores DEBEN verificar que el nombre esté disponible en PyPI, npm y GHCR y en al menos una base de marcas (EUIPO, USPTO u OMPI), y registrar el resultado en el repositorio del framework.
- **REQ-TRACE-71:** El README del framework, el de la plantilla y la portada de la documentación DEBEN incluir el aviso de no afiliación, y las compatibilidades DEBEN describirse en texto plano ("funciona con Claude Code"), sin logos de terceros.

> **es:** *aucove* es un proyecto independiente de código abierto. No está afiliado a Anthropic, OpenAI, Google, GitHub ni a otros proveedores mencionados, ni cuenta con su respaldo o patrocinio. Claude, Claude Code, Codex, Gemini, GitHub y las demás marcas pertenecen a sus titulares y se mencionan solo para indicar compatibilidad.
>
> **en:** *aucove* is an independent open source project. It is not affiliated with, endorsed by, or sponsored by Anthropic, OpenAI, Google, GitHub, or any other provider mentioned. Claude, Claude Code, Codex, Gemini, GitHub, and other marks belong to their respective owners and are mentioned only to indicate compatibility.

## 8. Evidencia verificable de fecha

### 8.1 Alcance y límites

Los sellos prueban que un contenido existía en una fecha y que no cambió después: fecha e integridad. No prueban autoría ni titularidad (§1, principio 1; §6.4).

Los compromisos ocultan rutas, contenidos e identidades, pero `tree`, `commit` y `commits` van sin sal. Quien conozca todos los demás archivos del árbol podría confirmar una suposición sobre un archivo de poca entropía. Es un riesgo menor y aceptado en v0.1.

- **REQ-TRACE-75:** Los textos del núcleo, la plantilla y las guías DEBEN decir «fecha verificable» o «prueba de existencia en una fecha», nunca «fecha cierta», que en México es un término jurídico con requisitos propios.

La NOM-151 es opcional, por hito y solo tras consultar a la oficina jurídica o de transferencia (D-49). No se recomienda para el piloto.

### 8.2 Qué se sella y cuándo

Sella el workflow `seal.yml` de la plantilla, con `aucove ci seal` (09-ci §7.4).

| `kind` | Qué se sella | Pruebas | Job de `seal.yml` |
|---|---|---|---|
| `submission` | Cada cabeza de un PR de tarea del mismo repo (eventos `opened`, `synchronize`, `reopened` y `ready_for_review`) | OpenTimestamps | `submission` |
| `integration` | Cada push a la base, desde el último commit sellado (09-ci REQ-CI-52) | 2 TSA del catálogo (§8.7) más OpenTimestamps | `integration` |

El registro de integración lleva `plan: true` cuando su rango toca `plan.md`, `roles.yaml` o `memory/constitution.md`, o cuando es el primero del repo. Así el plan queda sellado como un prerregistro.

- **REQ-TRACE-76:** `init` DEBERÍA correr con `plan.md` ya redactado, para que el primer registro selle el plan del día 1.

### 8.3 Manifiesto y compromisos

El manifiesto (`evidence-manifest/v1`) es JSON canónico según RFC 8785, con un perfil restringido: claves ASCII, enteros menores que 2^53, sin flotantes, sin espacios y sin salto de línea final. En lugar de rutas, contenidos e identidades en claro, lleva compromisos con sal y separación de dominio (‖ es la concatenación de bytes):

- `p` = SHA-256("evidence.path.v1" ‖ 0x00 ‖ sal ‖ bytes exactos de la ruta según `git diff-tree -z`, sin normalizar);
- `c` = SHA-256("evidence.blob.v1" ‖ 0x00 ‖ sal ‖ bytes del blob), y `c = null` si el archivo se borró;
- `a` = SHA-256("evidence.actor.v1" ‖ 0x00 ‖ sal ‖ rol ‖ 0x00 ‖ valor UTF-8), con los roles `pr_author`, `merged_by`, `author` y `committer`.

Las entradas van ordenadas por `p`. Una ruta que no sea UTF-8 válido se omite y se informa en el resumen (`skipped`). Igual se tratan los gitlinks (submódulos), las rutas de más de 4096 bytes y los actores cuya identidad pase de 320 caracteres o no se pueda decodificar. Las sales van en un archivo privado de aperturas (`openings.json`, `evidence-openings/v1`): con ellas se prueba un archivo o un actor sin revelar los demás ni dejar que se adivinen.

- **REQ-TRACE-77:** Las huellas DEBEN salir de los blobs de git (`git cat-file`, sin filtros), nunca del disco.
- **REQ-TRACE-78:** Cada sal DEBE tener 32 bytes de `secrets.token_bytes` y nunca reutilizarse. Hay una por entrada, compartida por los `p` y `c` de esa entrada (la separación de dominio impide que coincidan; `evidence-openings/v1` guarda un `salt` por entrada), y una por actor.
- **REQ-TRACE-79:** Se DEBEN guardar los bytes exactos del manifiesto, y la verificación NUNCA lo regenera.

### 8.4 Pruebas

Las dos clases de prueba sellan el SHA-256 de los bytes exactos de `manifest.json`.

- **OpenTimestamps (OTS):** el cliente agrega su nonce y se usan al menos 2 de los 4 calendarios por defecto. La prueba queda pendiente hasta que Bitcoin incluye su bloque, y se completa con `ots upgrade` o con `aucove evidence verify --upgrade`.
- **RFC 3161:** SHA-256, nonce de 64 bits y `certReq: true`. Antes de guardar la respuesta se verifican la huella, el nonce, la política del catálogo y la cadena hasta su raíz, válida en `genTime`. Se guardan el `.tsr`, `chain.pem`, `root.pem` y `ocsp.der` o `crl.der`. Timeout de 15 s, un reintento y una solicitud por TSA y por run (Sectigo pide 15 s entre solicitudes). Como mucho se intentan 3 TSA por run, para que un registro no pase de 4 pruebas.
- **Horas:** van en UTC, y los reportes agregan la del centro de México (`America/Mexico_City`).
- **REQ-TRACE-80:** Para cada TSA, `ci seal` DEBE guardar la cadena y el OCSP o la CRL obtenidos al sellar. Si no consigue ninguno de los dos, la prueba queda `ok` y `verify` la marca `weak`.
- **REQ-TRACE-88:** La verificación de un `.tsr`, al sellar y en `evidence verify`, DEBE construir ella misma la ruta de certificación, firma por firma y con cada certificado vigente en `genTime`, hasta una raíz del catálogo. Los certificados de la respuesta o de `chain.pem` son solo candidatos, nunca anclas de confianza: `rfc3161-client` 1.0.9 trata las intermedias como anclas, y un autofirmado colado como intermedia hacía verificar un sello falso. La verificación DEBE comprobar además el atributo ESS `signingCertificate` o `signingCertificateV2`, igual que `openssl ts -verify`. Cuando una TSA entrega SET de certificados que no están en orden DER (Sectigo), se reordenan en memoria, porque esos SET no van firmados, y el `.tsr` se guarda tal como llegó. La búsqueda tiene topes, porque los candidatos vienen de terceros: 32 certificados distintos (de más, la prueba falla con `chain` sin buscar) y 64 verificaciones de firma.
- **REQ-TRACE-89:** Con `AUCOVE_TESTING=1`, `AUCOVE_EVIDENCE_FAKE` nombra un archivo Python que define `transport()` y, si quiere, `catalog()` y `calendars()`. `ci seal` y `evidence verify` DEBEN usar esos dobles en lugar de los clientes reales y del catálogo: una TSA y unos calendarios de prueba, sin red y sin esperas. Es el gancho de la autoprueba de 00-marco D-37. Sin `AUCOVE_TESTING=1`, la variable DEBE ignorarse, igual que la del FakeForge (08-forja REQ-FORGE-62).

### 8.5 Dónde se guarda

Cada registro es una carpeta `<kind>-<commit>/` con:

- `record.json` (`evidence-record/v1`), `manifest.json`, `openings.json` y `manifest.json.ots`;
- por cada TSA, `tsa/<id>.tsr`, `tsa/<id>.chain.pem`, `tsa/<id>.root.pem` y `tsa/<id>.ocsp.der` o `tsa/<id>.crl.der`;
- `VERIFY.md` y `open_commitments.py` (§8.6).

Si `--out` ya tiene esa carpeta, por otro intento del run en el mismo directorio, `ci seal` usa `<kind>-<commit>-a<n>/`, donde `<n>` es el intento.

El registro queda en tres lugares:

1. el artefacto `evidence-<kind>-<commit>-a<n>`, donde `<n>` es el intento del run (400 días como máximo);
2. el comentario `ci:evidence` del PR, con el JSON `evidence-record/v1`, recortado según 09-ci REQ-CI-54;
3. la copia local `.aucove/evidence/` de cada persona (05-operaciones REQ-OPS-66).

Un commit sin PR solo tiene el artefacto.

- **REQ-TRACE-81:** Ningún registro DEBE escribirse en una rama del repo.

### 8.6 Verificación y kit del árbitro

`aucove evidence verify` da los veredictos de 05-operaciones §9.26. Para quien verifica sin el framework, cada registro trae un kit del árbitro. `VERIFY.md` está localizado y escrito en lenguaje llano. Contiene:

- qué prueba el registro y qué no (§8.1);
- un solo comando: `uvx --from "aucove[evidence]==X.Y.Z" aucove evidence verify .`, con la versión del núcleo que selló. Un registro con solo OTS da `pending` hasta que se coteja a mano su bloque de Bitcoin (05-operaciones §9.26);
- para cada TSA, comparar el SHA-256 de `tsa/<id>.root.pem` con la huella que publica la TSA (que `VERIFY.md` y el catálogo listan) y correr `openssl ts -verify -data manifest.json -in tsa/<id>.tsr -CAfile tsa/<id>.root.pem -untrusted tsa/<id>.chain.pem -attime <genTime en segundos desde 1970>`;
- para OTS, `ots --no-bitcoin verify manifest.json.ots` (o `ots info`), que muestra el bloque, y cotejar la raíz de Merkle de ese bloque en un explorador o en un nodo propio;
- las fórmulas de §8.3 y `python open_commitments.py manifest.json openings.json [carpeta]`.

- **REQ-TRACE-82:** `ci seal` DEBE copiar `VERIFY.md` (en el idioma del proyecto) y `open_commitments.py` en cada carpeta de registro. El script DEBE tener 40 líneas o menos, usar solo la biblioteca estándar de Python 3.8 o posterior y funcionar sin el núcleo. Cada línea de su salida empieza con una palabra fija, que no se localiza: `ok`, `different` (el archivo no es el sellado), `missing` (falta el archivo) o `not-in-manifest` (la apertura no es de este manifiesto). `VERIFY.md` las explica en el idioma del proyecto. El script termina en 0 si todo coincide; si no, en 1.

### 8.7 Catálogo de TSA

El catálogo vive en el núcleo, en `aucove/_data/tsa-catalog.json`. Cada entrada tiene `id`, `name`, `url`, `policy_oids`, `terms_url`, `fingerprint_source` (dónde publica la TSA su raíz), `seal` (si se usa para sellos nuevos), `roots[]` (PEM y SHA-256) y `certs[]` (PEM, SHA-256 y vigencia de cada certificado que tuvo). Al inicio trae `freetsa` y `sectigo`, con `seal: true`; DigiCert entra solo tras PA-105. La raíz de `sectigo` es USERTrust RSA Certification Authority, no la R46 autofirmada, porque el atributo ESS de sus respuestas declara la R46 firmada por USERTrust, y con la R46 autofirmada como raíz `openssl ts -verify` falla. `ci seal` usa las entradas con `seal: true` en orden. Cambiar el catálogo exige una versión nueva del núcleo (`core_pin`).

- **REQ-TRACE-83:** Retirar una TSA (`seal: false`) NUNCA DEBE borrar sus certificados: las pruebas viejas se verifican con la cadena guardada o con la del catálogo.

### 8.8 Paquete de evidencia, custodia y exportación (manual en v0.1)

En cada hito, y como mínimo al cerrar el proyecto, el responsable arma el paquete de evidencia (D-46):

1. `gh run download` de cada artefacto `evidence-*`, antes de que venza.
2. `gh api --paginate repos/{o}/{r}/issues/comments`, filtrando el marcador `aucove:ci:evidence`, a `comments.jsonl`.
3. `git fetch origin '+refs/pull/*/head:refs/pull/*/head'` y `git bundle create repo.bundle --all`, que incluye las cabezas de los PR no integrados.
4. `index.txt` con el `sha256sum` de cada archivo; opcionalmente, `ots stamp index.txt`.
5. Guardar el paquete en al menos dos custodios fuera de GitHub y registrar en una decisión la fecha, los custodios y el SHA-256 del paquete. Sin repositorio institucional, los custodios recomendados son: el registro cifrado de OSF (00-marco D-47); Harvard Dataverse, con un DOI por proyecto, cada hito como versión, acceso restringido, título neutro y el paquete cifrado; y la copia del responsable (00-marco D-46). Zenodo da un DOI por versión y deja públicos los metadatos: se usa para los releases públicos (§8.9), no para la custodia.

Cada persona puede pedir su exportación personal en cada hito y antes de salir del equipo. Reúne `.aucove/evidence/`, los artefactos de los PR propios y `git archive <commit> sections/<id>/` de cada registro propio, sin bundle ni contenido de otras personas.

- **REQ-TRACE-84:** La guía de custodia (H4) DEBE describir el armado del paquete y de la exportación personal con `gh` y `git`, sin el núcleo.
- **REQ-TRACE-85:** El paquete y la exportación personal DEBEN guardarse fuera del árbol de trabajo del repo y nunca versionarse.

### 8.9 Apertura por hitos y depósito cifrado

La checklist de 00-marco D-44 va como plantilla de decisión: IMPI primero si algo puede ser patentable; datos personales y ética; financiador, convenio y titularidad; política de preprints de la revista; consentimiento de los autores, y correos noreply. Esa plantilla incluye además:

- el procedimiento del espejo: un push, hasta la etiqueta del hito, a un repo público nuevo;
- el del release en Zenodo y el de Software Heritage;
- el del depósito cifrado en OSF (00-marco D-47);
- la NOM-151 opcional (§8.1).

- **REQ-TRACE-86:** Una apertura DEBE registrarse como decisión que enumere la checklist cumplida y la etiqueta del hito.
- **REQ-TRACE-87:** El espejo público NO DEBE incluir Issues, PR ni comentarios del repo privado, porque llevan aperturas.

### 8.10 Reservado para v0.2

Quedan para v0.2, como operaciones o automatismos del núcleo: `aucove evidence export` (con `find_artifact` y `download_artifact`), `aucove evidence deposit` y `aucove evidence disclose`; la verificación de Bitcoin con nodo propio o con exploradores; la actualización programada de OTS; el re-sellado según RFC 4998, y la constancia NOM-151.

## 9. Identificadores de este capítulo

| Identificador | Tipo | Definición |
|---|---|---|
| `none`, `undeclared`, `ideation`, `literature`, `drafting`, `editing`, `translation`, `code`, `analysis`, `figures`, `review` | valores de `AI-Use` | Vocabulario de §2.3 |
| `claude-code`, `codex`, `antigravity-cli`, `gemini-cli`, `opencode`, `goose`, `other` | valores de `tool` | Herramientas de §2.4 |
| `unknown` | valor de `model` | Modelo sin fuente |
| `AUCOVE_AI_TOOL`, `AUCOVE_AI_MODEL` | variables de entorno | Herramienta y modelo declarados por la configuración de la puerta o por la persona |
| `.aucove/state/sessions/<session_id>.json` | caché `local` | Registro de sesión con el modelo (§2.6) |
| `aucove hook post-model-switch` | evento de hook | Actualiza el modelo del registro de sesión (evento `PostModelSwitch` de Claude Code) |
| `ai_use`, `ai_tool`, `ai_model`, `ai_note`, `sign_off` / `--ai-use`, `--ai-tool`, `--ai-model`, `--ai-note`, `--sign-off` | entradas MCP / opciones CLI | Declaración de uso y confirmación (§2.5) |
| `attest-v1` | ID de texto | Declaración de responsabilidad (§2.8) |
| `tty`, `cli-flag`, `mcp` | valores de `trace.sign_off.channel` | Canal de la confirmación |
| `branch-commit`, `pr-body`, `squash-commit` | contextos de validación | §2.10 |
| `trace/v1`, `approval-record/v1`, `ai-use-report/v1`, `credit-report/v1` | esquemas (`https://aucove.invalid/schemas/<nombre>/v1`) | §3.3, §4.2, §5.2, §6.3 |
| `E_VALIDATION_AI_USE_UNDECLARED`, `E_VALIDATION_AI_USE_CONFLICT`, `E_VALIDATION_SIGNOFF_MISSING`, `E_VALIDATION_ASSISTED_BY_MISSING` | códigos de error de operación | De `submit`; para el catálogo de 05-operaciones §12.1 (REQ-TRACE-72) |
| `E_VALIDATION_TRAILER_SYNTAX`, `E_VALIDATION_TRAILER_MISSING`, `E_VALIDATION_SIGNOFF_MISMATCH`, `E_VALIDATION_TRACE_MISMATCH` | códigos de hallazgo | Del check `traceability` (REQ-TRACE-72) |
| `E_VALIDATION_LICENSE_UNKNOWN` | código de hallazgo | Check de `validate` (00-marco §5.11); no entra en el catálogo de 05-operaciones |
| `merge-settings` | check de `doctor` | Método de integración del repositorio (REQ-TRACE-36) |
| `W_VALIDATION_MODEL_MISMATCH`, `W_VALIDATION_NONE_WITH_AGENT` | advertencias | El formato de advertencias es de 05-operaciones |
| `trace_conflict` | marca de reporte | Las fuentes de traza difieren (§3.4) |
| `approved`, `approved_stale`, `changes_requested`, `missing`, `not_merged` | valores de `approval_status` | §4.2 |
| `merge_audit` | clave de `approval-record/v1` | Clasificación de 06-permisos §3.5: `compliant`, `bypass_no_deputy`, `bypass`, `direct_push`, `force_push` (§4.2) |
| `aucove report approvals` | operación (reservada) | Exporta el libro de aprobaciones por PR del responsable |
| `records/approvals/approvals-<YYYYMMDD>.jsonl` | ruta (reservada), clase `lead` en la convención 0.1 | Libro exportado |
| `LICENSE`, `LICENSES/**`, `REUSE.toml` | rutas, clase `lead` | Licencias del proyecto; las escribe `init` |
| `licenses` (`holder`, `text`, `code`, `data`) | clave de `project.yaml` | §7.3 |
| `people[].name`, `people[].email`, `people[].orcid`, `people[].credit[]` (lista de slugs) | claves de `roles.yaml` | §2.8 y §6.1 |
| 14 slugs CRediT de §6.1 | valores | CRediT |
| `ai_use`, `assisted_by` | claves del front matter de la nota de sesión | Copia revisada de la traza (§2.7) |
| `LicenseRef-AllRightsReserved` | licencia personalizada | Todos los derechos reservados (§7.3) |
| `submission`, `integration` | valores de `kind` | Registros de entrega y de integración (§8.2) |
| `evidence-manifest/v1`, `evidence-openings/v1`, `evidence-record/v1` | esquemas (03-esquemas REQ-SCHEMA-93) | Manifiesto, aperturas y registro (§8.3, §8.5) |
| `evidence-<kind>-<commit>-a<n>` | artefactos de Actions | Carpeta del registro; 400 días como máximo (§8.5) |
| `<!-- aucove:ci:evidence v1 kind=<kind> sha=<commit> attempt=<n>` … `-->` | marcador de comentario (`ci:evidence`) | Registro `evidence-record/v1` en el PR (§8.5; 09-ci REQ-CI-54) |
| `.aucove/evidence/` | ruta local | Copia de los registros de los PR propios (§8.5; 05-operaciones REQ-OPS-66) |
| `VERIFY.md`, `open_commitments.py` | archivos de cada registro | Kit del árbitro (§8.6) |
| `aucove/_data/tsa-catalog.json` | catálogo del núcleo | TSA para sellar y verificar (§8.7) |
| `pr_author`, `merged_by`, `author`, `committer` | roles de actor | Compromisos `a` (§8.3) |
| `AUCOVE_EVIDENCE_FAKE` | variable de entorno (solo con `AUCOVE_TESTING=1`) | Dobles de TSA y calendarios de la autoprueba (§8.4, REQ-TRACE-89) |
| REQ-TRACE-74 a REQ-TRACE-89 | requisitos | §6.4 (REQ-TRACE-74) y §8 (REQ-TRACE-75 a REQ-TRACE-89) |

## 10. Supuestos sobre otros capítulos

- **S-1 (03-esquemas):** El bloque legible por máquina del PR lleva el objeto `trace` de §3.3, va antes del párrafo de trailers y la plantilla de PR no usa líneas `---`. El front matter de la nota de sesión incluye `ai_use` y `assisted_by`. `roles.yaml` incluye `name`, `email`, `orcid` y `credit` por persona. `project.yaml` incluye `licenses`.
- **S-2 (05-operaciones):** `save`, `submit` y las operaciones que crean commits aceptan las entradas de §2.5. El catálogo incorpora los errores y advertencias de §9. `doctor` hace la verificación de REQ-TRACE-36 y `validate` las de REQ-TRACE-53 y REQ-TRACE-64.
- **S-3 (06-permisos):** La configuración de §3.2 entra en las protecciones que aplica `init`. 06 define `qualifying_approvers(pr, pol)` (§3.2) y aclara que el PR de exportación del responsable lo aprueba el suplente (D-17).
- **S-4 (07-hooks):** `.claude/settings.json` registra los eventos `SessionStart` y `PostModelSwitch` de Claude Code hacia `aucove hook session-start` y `aucove hook post-model-switch`, y no fija `attribution`. Los skills de entrega muestran `attest-v1` y piden confirmación.
- **S-5 (08-forja):** El adaptador expone `get_pull`, `list_reviews`, `list_pull_commits` y `update_merge_settings`, más el ruleset `allowed_merge_methods` en perfil `enforced`. El adaptador falso los simula.
- **S-6 (01-arquitectura):** La configuración MCP que se genera para Codex, Gemini CLI, OpenCode y goose pasa `AUCOVE_AI_TOOL` en su bloque de entorno (`env` en Codex y Gemini CLI, `environment` en OpenCode; §2.4). Solo queda por verificar que goose lo admita. La caché `.aucove/state/sessions/` entra en el diseño de la caché. El repositorio del framework incluye `LICENSE`, `NOTICE`, `REUSE.toml`, `CONTRIBUTING.md` y el check de DCO.
- **S-7 (09-ci):** El check `traceability` implementa REQ-TRACE-40, y el job posterior al push `aucove/base-audit` implementa REQ-TRACE-41. Ninguno escribe en la base.
- **S-8 (02-convencion):** Define las rutas de `code` y `data` para `REUSE.toml`, clasifica `LICENSE`, `LICENSES/**` y `REUSE.toml` como `lead`, y reserva `records/approvals/` como `lead` en la convención 0.1 (02-convencion §13).
- **S-9 (04-tareas):** Si `update` integra la base, lo hace con un commit de merge o con rebase. Los commits de merge y los commits vacíos (como el de `claim`) quedan fuera de la agregación.
- **S-10 (12-mvp):** Las pruebas cubren el analizador (incluidos `---` y un párrafo final de `Co-authored-by`), la agregación y un squash real en GitHub para confirmar (a) dónde agrega los `Co-authored-by` (Q-TRACE-2), (b) que `<!-- aucove:pr-meta-begin v1 -->` y `<!-- aucove:pr-meta-end v1 -->` llegan intactos al mensaje y (c) si el mensaje contiene `\r\n`.

## 11. Preguntas abiertas

- **Q-TRACE-1:** La confirmación de `attest-v1` por MCP la transmite el agente. ¿Basta en v0.1, o conviene una confirmación solo humana (por ejemplo, que la persona marque el PR como listo en la web)?
- **Q-TRACE-2:** ¿Dónde agrega GitHub los `Co-authored-by` en un squash con `PR_BODY`, y copia también los `Co-Authored-By` que traen los mensajes de los commits? No está documentado. Se tolera con REQ-TRACE-30 y se prueba en el piloto.
- **Q-TRACE-3:** GitHub no descarta las aprobaciones cuando se edita el cuerpo del PR. ¿Debe `traceability` exigir que el bloque no cambie después de la última aprobación?
- **Q-TRACE-4:** ¿Se materializa el libro de aprobaciones ya en v0.1 (por ejemplo, con una exportación mensual) o basta la función pura y la operación reservada?
- **Q-TRACE-5:** ¿Es correcta la licencia sugerida para `text` en repos privados (`LicenseRef-AllRightsReserved`)? Hay que validarla con la oficina jurídica de la institución del piloto.
- **Q-TRACE-6:** ¿Alcanza el vocabulario de `AI-Use` para las políticas de las revistas de los grupos del piloto? Hay que revisar en especial `figures` y `review`.
- **Q-TRACE-7:** ¿Cómo se declaran en CRediT las personas sin cuenta en la forja, como alguien que obtuvo el financiamiento? ¿Con una lista `contributors[]` aparte en `roles.yaml`?
- **Q-TRACE-8:** ¿Con qué mecanismo se verifica el DCO en el repositorio del framework: una app de terceros o un workflow propio con el analizador de §2.10?
- **Q-TRACE-9:** ¿Se registra la evidencia de bypass (API de *rule suites* de rulesets) en `approval-record`, o queda para después de v0.1?
- **Q-TRACE-11:** ¿Qué base y qué plazo de conservación fija la oficina jurídica para el paquete de evidencia? Se propone la ejecución del acuerdo de autoría y la defensa de derechos, con el consentimiento como transparencia. Debe resolverse antes del 30/10/2026 (protocolo de ética).
- **Q-TRACE-12:** ¿Conviene una NOM-151 por hito en algún grupo, tras la consulta jurídica? (Diferida: queda fuera de la consulta jurídica actual).
- **Q-TRACE-13:** Por confirmar en S5: ¿los runners `ubuntu-slim` alcanzan FreeTSA, Sectigo y los calendarios OTS? ¿Cuánto miden los comentarios y artefactos reales? ¿Existe el endpoint de retención de 06-permisos REQ-PERM-57, y `upload-artifact` recorta `retention-days` al máximo del repo? ¿Qué `verified_at` tiene un squash hecho en la web? ¿Se puede crear un registro de OSF sin proyecto después del 16/11/2026? (La guía de OSF, consultada el 2026-10-07, dice que sí; S5 lo comprueba en la práctica).
- **Q-TRACE-14:** ¿Qué verificación de Bitcoin se agrega en v0.2: nodo propio, dos exploradores o ambos?

## 12. Referencias

Consultadas el 2026-09-29.

- Claude Code, settings reference, sección "Git and attribution" (`attribution`, `includeCoAuthoredBy`): https://code.claude.com/docs/en/settings-reference
- Claude Code, hooks (campos comunes, `SessionStart.model`, `PostModelSwitch`, variables de los hooks, `CLAUDE_ENV_FILE`): https://code.claude.com/docs/en/hooks
- Claude Code, variables de entorno (`CLAUDECODE`, `CLAUDE_CODE_CHILD_SESSION`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_REMOTE`): https://code.claude.com/docs/en/env-vars
- Claude Code, legal and compliance (uso de nombres y logos): https://code.claude.com/docs/en/legal-and-compliance
- GitHub, configurar el squash: https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/configuring-commit-squashing-for-pull-requests
- GitHub, mensaje de squash editable: https://docs.github.com/en/pull-requests/reference/pull-request-merges
- GitHub, fusionar un PR: https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/incorporating-changes-from-a-pull-request/merging-a-pull-request
- GitHub Changelog, opciones de mensaje por defecto (título y número): https://github.blog/changelog/2022-08-23-new-options-for-controlling-the-default-commit-message-when-merging-a-pull-request/
- GitHub Changelog, coautores en squash: https://github.blog/changelog/2019-12-19-improved-attribution-when-squashing-commits/
- GitHub REST, actualizar un repositorio (`squash_merge_commit_*`): https://docs.github.com/en/rest/repos/repos#update-a-repository
- GitHub REST, rulesets (`allowed_merge_methods`): https://docs.github.com/en/rest/repos/rules
- GitHub REST, PR (obtener, commits con máximo de 250): https://docs.github.com/en/rest/pulls/pulls
- GitHub REST, revisiones de PR: https://docs.github.com/en/rest/pulls/reviews
- GitHub, commits con varios autores: https://docs.github.com/en/pull-requests/committing-changes-to-your-project/creating-and-editing-commits/creating-a-commit-with-multiple-authors
- GitLab, plantillas de commit (`%{approved_by}`): https://docs.gitlab.com/user/project/merge_requests/commit_templates/
- RFC 7405, Case-Sensitive String Support in ABNF (`%s"..."`): https://www.rfc-editor.org/rfc/rfc7405
- git-interpret-trailers: https://git-scm.com/docs/git-interpret-trailers (comportamiento de `---` y de un párrafo final probado con git 2.54)
- Kernel de Linux, AI Coding Assistants: https://docs.kernel.org/process/coding-assistants.html
- COPE, Authorship and AI tools: https://publicationethics.org/guidance/cope-position/authorship-and-ai-tools (la página devolvió 403 al acceso directo; su contenido se verificó con el extracto indexado)
- ICMJE, AI use by authors: https://www.icmje.org/recommendations/browse/artificial-intelligence/ai-use-by-authors.html
- ICMJE, roles de autores y colaboradores: https://www.icmje.org/recommendations/browse/roles-and-responsibilities/defining-the-role-of-authors-and-contributors.html
- CRediT (roles y slugs): https://credit.niso.org/ · ANSI/NISO Z39.104-2022: https://www.niso.org/publications/z39104-2022-credit · Implementación de CRediT (grado de contribución como práctica opcional): https://credit.niso.org/implementing-credit/ · Grados (fuente secundaria): https://casrai.org/news/credit-degree-of-contribution-qualifiers-lead-equal-supporting
- REUSE 3.3: https://reuse.software/spec-3.3/
- DCO 1.1: https://developercertificate.org/
- PEP 639 en la guía de empaquetado: https://packaging.python.org/en/latest/guides/writing-pyproject-toml/#license
- Paquete `reuse` en PyPI (licencias): https://pypi.org/project/reuse/
- Lista SPDX (datos): https://github.com/spdx/license-list-data
