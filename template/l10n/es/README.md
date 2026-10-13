# {{var:title}}

Proyecto de investigación colaborativo, escrito por un equipo que trabaja con asistentes de IA sobre git.
Cada persona trabaja en su parte, `sections/<section-id>/`: texto, código, figuras, tablas y notas.
El núcleo `aucove` (versión fijada: `{{var:core_pin}}`) hace el trabajo mecánico: tareas, ramas, commits, PR y validación.
Funciona con Claude Code, Codex y otras herramientas de IA que leen `AGENTS.md`.

## Qué hay en este repositorio

| Ruta | Contenido |
|---|---|
| `plan.md` | Dirección del proyecto: preguntas, hipótesis, alcance y métodos, con IDs estables. |
| `roles.yaml` | Personas, roles, suplente y dueño de cada parte. |
| `memory/constitution.md` | Principios, estilo y reglas del proyecto. |
| `memory/decisions/` | Registro de decisiones: solo se agregan archivos, nunca se modifican. |
| `AGENTS.md`, `CLAUDE.md` | Reglas para los agentes de IA. |
| `sections/<section-id>/` | Una carpeta por parte, con un solo dueño. |
| `tex/main.tex` | Manuscrito principal (con `format: latex`); solo incluye los demás archivos. |
| `tex/outline/` | Un archivo por sección del artículo, con su título y sus partes en orden (con `format: latex`). |
| `tex/guide.md` | Guía del artículo: qué cubre cada sección y cada parte, la notación y el tono (con `format: latex`). |
| `bib/references.bib` | Exportación del grupo de Zotero del proyecto (con `format: latex`). |
| `data/README.md` | Índice de los conjuntos de datos, que no se versionan. |
| `project.yaml` | Configuración: convención, idioma, formato, forja y compilación. |

## Instalar el núcleo

Necesitas [uv](https://docs.astral.sh/uv/).
En macOS o Linux:

```sh
LOCK="$(mktemp)"
curl -fsSL -o "$LOCK" https://github.com/{{var:framework_repo}}/releases/download/v{{var:core_pin}}/requirements-lock.txt
uv tool install "aucove=={{var:core_pin}}" --constraints "$LOCK"
uv tool update-shell
```

En Windows (PowerShell):

```powershell
$lock = New-TemporaryFile
Invoke-WebRequest -UseBasicParsing https://github.com/{{var:framework_repo}}/releases/download/v{{var:core_pin}}/requirements-lock.txt -OutFile $lock.FullName
uv tool install "aucove=={{var:core_pin}}" --constraints $lock.FullName
uv tool update-shell
```

Reinicia la terminal y comprueba la instalación con `aucove doctor`.

## Si eres responsable

1. Completa `roles.yaml` con las personas y los dueños de cada parte, y `plan.md` con la dirección del proyecto.
2. Ajusta `memory/constitution.md` y la sección 7 de `AGENTS.md` con las reglas propias del proyecto.
3. Ejecuta `aucove sync-roles` para regenerar `.github/CODEOWNERS` y el formulario de tareas.
4. Ejecuta `aucove validate` antes de entregar cualquier cambio.
5. Tú o tu suplente revisan y aprueban los PR; un agente nunca aprueba ni integra.
6. Con `format: latex`, también eres el editor del artículo, salvo que declares otro en `roles.yaml`: ordenas las secciones en `tex/outline/`, escribes la guía del artículo en `tex/guide.md` y pides cambios a los dueños de las partes.

## Si eres investigadora o investigador

1. Pide al responsable que te agregue en `roles.yaml` y que te asigne una parte.
2. Trabaja siempre dentro de una tarea: proponla o reclámala antes de editar.
3. Escribe solo en tu carpeta `sections/<section-id>/`, con una oración por línea. Si `project.yaml` dice `format: latex`, las rutas de `\input` e `\includegraphics` empiezan con `../sections/<section-id>/`.
4. Antes de editar un archivo, comprueba si puedes hacerlo con `aucove check-path <ruta>`.
5. Cita solo claves del grupo de Zotero; si falta una, pega su exportación en el `pending.bib` de tu parte (solo con LaTeX).

## Compilar en local

Con `format: latex`, desde la raíz del repositorio, `latexmk` usa `.latexmkrc`, que equivale a la compilación del CI.
El PDF queda en `tex/build/`, que git ignora.

## Licencias

El código de esta plantilla (configuración, flujos de trabajo y archivos `.tex` del responsable) está bajo MIT-0, y el texto de ejemplo, bajo CC0-1.0.
El detalle por ruta está en `REUSE.toml`.
Al inicializar el proyecto, el responsable elige las licencias de su texto, su código y sus datos.

## Aviso de no afiliación

*aucove* es un proyecto independiente de código abierto. No está afiliado a Anthropic, OpenAI, Google, GitHub ni a otros proveedores mencionados, ni cuenta con su respaldo o patrocinio. Claude, Claude Code, Codex, Gemini, GitHub y las demás marcas pertenecen a sus titulares y se mencionan solo para indicar compatibilidad.
