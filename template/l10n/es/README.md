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

## Prepara tu computadora

Se hace una vez en cada computadora, con la [configuración inicial de aucove](https://github.com/{{var:framework_repo}}/blob/v{{var:core_pin}}/README.md#configuración-inicial): tu cuenta de GitHub con el correo privado, git con tu identidad, el núcleo, tu token de GitHub para este repositorio en `GH_TOKEN`, el clon de este repositorio y tu agente abierto en esa carpeta.
Si prefieres que tu agente te acompañe, usa [Configurar aucove con tu agente](https://github.com/{{var:framework_repo}}/blob/v{{var:core_pin}}/docs/es/configurar-con-agente.md).

Este proyecto usa el núcleo `aucove` `{{var:core_pin}}`.
Instálalo y actualízalo solo desde una terminal abierta desde el menú Inicio (o Terminal en macOS), nunca desde la app de tu agente: en Windows, lo que se instala desde Claude Desktop queda en una copia aparte que solo ve esa app.
Antes necesitas [uv](https://docs.astral.sh/uv/).
En macOS o Linux:

```sh
LOCK="$(mktemp)"
curl -fsSL -o "$LOCK" https://github.com/{{var:framework_repo}}/releases/download/v{{var:core_pin}}/requirements-lock.txt
UV_COMPILE_BYTECODE=1 uv tool install "aucove=={{var:core_pin}}" --constraints "$LOCK"
uv tool update-shell
```

En Windows (PowerShell):

```powershell
$lock = New-TemporaryFile
Invoke-WebRequest -UseBasicParsing https://github.com/{{var:framework_repo}}/releases/download/v{{var:core_pin}}/requirements-lock.txt -OutFile $lock.FullName
$env:UV_COMPILE_BYTECODE = "1"; uv tool install "aucove=={{var:core_pin}}" --constraints $lock.FullName
uv tool update-shell
```

Cierra la terminal, abre una nueva y comprueba todo con `aucove doctor` en la carpeta del proyecto.

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
En Windows con MiKTeX, `latexmk` necesita Perl, que MiKTeX no trae (TeX Live sí): instala antes [Strawberry Perl](https://strawberryperl.com/).
Por ejemplo, con `winget install --id StrawberryPerl.StrawberryPerl -e` en una terminal abierta desde el menú Inicio; después abre una terminal nueva y reinicia la app de tu agente, que solo ve lo que se instaló antes de abrirla.

## Licencias

El código de esta plantilla (configuración, flujos de trabajo y archivos `.tex` del responsable) está bajo MIT-0, y el texto de ejemplo, bajo CC0-1.0.
El detalle por ruta está en `REUSE.toml`.
Al inicializar el proyecto, el responsable elige las licencias de su texto, su código y sus datos.

## Aviso de no afiliación

*aucove* es un proyecto independiente de código abierto. No está afiliado a Anthropic, OpenAI, Google, GitHub ni a otros proveedores mencionados, ni cuenta con su respaldo o patrocinio. Claude, Claude Code, Codex, Gemini, GitHub y las demás marcas pertenecen a sus titulares y se mencionan solo para indicar compatibilidad.
