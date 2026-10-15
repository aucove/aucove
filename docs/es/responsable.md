# Responsable

[Guía de uso](README.md) › Responsable

Preparas el proyecto una vez y, cada semana, revisas lo que entregan los demás.

> [!NOTE]
> Describe la versión 0.1 completa. Con la 0.1.0rc2, lo que aquí se pide con un atajo como `/empezar` se le pide al agente con palabras, o se hace con los comandos `aucove`.

## Preparar el proyecto

Se hace una vez, en ocho pasos y en este orden. Los cinco primeros se hacen en GitHub y en la terminal; desde el sexto trabajas con tu agente.

### 1. El espacio en GitHub

Crea una organización para el grupo (en GitHub: tu foto → Your organizations → New organization) e invita a cada persona como miembro (People → Invite member). Para que GitHub haga cumplir las reglas en un repo privado hace falta GitHub Team, que es gratis para docentes e investigadores con GitHub Education. Un repo público funciona con GitHub Free.

### 2. El acuerdo de autoría

Es un documento común y corriente, un PDF o un Word, que el grupo redacta y firma antes de empezar. No es un archivo de aucove ni vive en el repositorio, porque lleva firmas y datos personales: se guarda donde el grupo guarda sus documentos, por ejemplo la carpeta compartida de la institución. Fija:

- el orden y los criterios de autoría del artículo, con los roles de cada quien;
- cuándo y cómo se publica, y qué se revisa antes (la lista de apertura);
- quién guarda las pruebas y quién lo suple;
- el derecho de cada persona a llevarse sus pruebas si se va;
- dónde y por cuánto tiempo se guarda el paquete de pruebas.

La versión 0.1.0 trae un modelo de acuerdo en español y en inglés. Mientras tanto, el grupo lo redacta con esos puntos; tu agente puede ayudarte con un borrador, que no va al repo.

Al repo solo llega su **huella**: 64 caracteres que identifican el archivo firmado sin revelar su contenido. Calcúlala en tu terminal:

```powershell
Get-FileHash .\acuerdo-firmado.pdf -Algorithm SHA256
```

```bash
shasum -a 256 acuerdo-firmado.pdf
```

La fecha y la huella van en el renglón que trae `memory/constitution.md` en «Integridad y ética». No tienes que editarlo tú: en la entrevista del paso 6 el agente te pide las dos y llena el renglón. El primer sello del proyecto cubre ese archivo, así que queda una fecha verificable de que el acuerdo existía, sin publicarlo. Si alguien duda después, basta con calcular la huella del PDF y compararla.

### 3. El repo vacío

En GitHub, en la organización, crea un repo **vacío**: New repository, un nombre (por ejemplo `ising-diluido`) y Private, sin README, sin `.gitignore` y sin licencia. No le agregues nada: en el paso 5, `aucove init --repo` le sube el proyecto completo desde tu computadora.

Con la 0.1.0 también podrás crear el repo desde la plantilla publicada (`aucove/aucove-template`, «Use this template»), clonarlo y correr en el paso 5 `aucove init`, sin `--repo`, dentro del clon.

<details>
<summary><b>Con la 0.1.0rc2</b>, que todavía no trae <code>init --repo</code></summary>

Súbele al repo vacío el render de la plantilla de este repositorio:

```bash
git clone --branch v0.1.0rc2 --depth 1 https://github.com/aucove/aucove.git aucove-fuente
uv run --project aucove-fuente python aucove-fuente/tools/render_template.py --lang es --out ising-diluido
cd ising-diluido
git init -b main
git add -A
git commit -s -m "Plantilla de aucove 0.1.0rc2"
git remote add origin https://github.com/labfe/ising-diluido.git
git push -u origin main
```

En el paso 5, corre `aucove init`, sin `--repo`, dentro de la carpeta `ising-diluido`.

</details>

### 4. Tus dos tokens

Necesitas el token de uso diario, igual que todos ([los pasos están en Investigador](investigador.md#tu-token-de-github)). Para `init` necesitas además un token de administración, que solo se usa ese día:

- el mismo formulario, con el permiso **Administration** en «Read and write», y **Secrets** y **Environments** en «Read-only», además de los otros, y una expiración de 1 día;
- no lo guardes en `GH_TOKEN`: lo pegas cuando `init` lo pide, y no se muestra ni se guarda;
- al terminar `init`, bórralo en GitHub.

Como dueño de la organización, tus tokens no necesitan aprobación. Los de tu equipo sí: los apruebas en la organización, Settings → Personal access tokens → Pending requests.

### 5. `aucove init --repo`

Se corre una sola vez, en una terminal normal (PowerShell en Windows, Terminal en macOS), dentro de una carpeta vacía que será la del proyecto. No se corre dentro de tu agente, a propósito: te pide escribir confirmaciones y pegar el token de administración, y nada de eso debe pasar por la IA. Tu agente entra en el paso siguiente.

```bash
mkdir ising-diluido
cd ising-diluido
aucove init --repo labfe/ising-diluido
```

`init` arma en la carpeta la plantilla de la versión instalada con tus respuestas y, cuando confirmas, crea el repositorio de git y lo sube al repo vacío en un solo commit. Para subirlo, git usa tus credenciales de GitHub: si nunca has subido nada a GitHub desde esta computadora, corre antes `git ls-remote https://github.com/labfe/ising-diluido.git`, que te pide iniciar sesión, o `gh auth setup-git` si usas GitHub CLI.

> [!TIP]
> Si prefieres que tu agente te acompañe desde el principio, ábrelo en la carpeta vacía antes de este paso: puede ayudarte a instalar aucove y explicarte cada pregunta. `init` lo corres tú en una terminal aparte y, al terminar, reinicias el agente en la misma carpeta para que cargue las herramientas del proyecto y tu token.

Opciones: `--language es` para no contestar el idioma, `--format none` sin LaTeX, `--disclosure open` para un proyecto abierto desde el inicio e `--individual` para un proyecto de una sola persona.

Así se ve (ejemplo abreviado; lo que escribes va después de cada pregunta):

```text
PS C:\Users\laura\investigacion\ising-diluido> aucove init --repo labfe/ising-diluido
Idioma del proyecto / Project language (es/en): es
Titular del copyright (persona, grupo o institución) [Los autores de ising-diluido]: Laboratorio de Física Estadística
Licencia de texto (manuscrito, plan.md, memoria y lo demás) [LicenseRef-AllRightsReserved]. Opciones: …: (Intro)
Licencia de código [MIT]. Opciones: …: (Intro)
Licencia de datos [LicenseRef-AllRightsReserved]. Opciones: …: (Intro)
init configurará labfe/ising-diluido (perfil enforced):
- Carpeta: C:\Users\laura\investigacion\ising-diluido está vacía; init crea ahí el proyecto y su repositorio de git, con la rama `main` y `origin` en https://github.com/labfe/ising-diluido.git.
- Proyecto: idioma es, formato latex, apertura milestones, modo group.
- roles.yaml: quedas (lmendez) como responsable, única persona y dueño de todas las partes; agrega a tu equipo después.
- Licencias: texto LicenseRef-AllRightsReserved, código MIT y datos LicenseRef-AllRightsReserved; titular: Laboratorio de Física Estadística. …
- Archivos: 70 nuevos, 0 cambiados y 0 borrados; se suben a `main` en un commit.
- GitHub: ajustes del repositorio (solo squash, con el título y el cuerpo del PR), Actions, colaboradores y etiquetas.
- Protecciones: los rulesets de la base, de las ramas de tarea y de los workflows, y su verificación.
- Tarea de preparación: crea y acepta «Preparar el proyecto», tuya y con todas las partes, para completar con el agente el plan, las personas y las partes.
Después te pediré un token de administración de GitHub de vida corta; no se muestra ni se guarda.
Para confirmar, escribe exactamente «init labfe/ising-diluido» y pulsa Intro: init labfe/ising-diluido
No hay suplente en roles.yaml: tus PR no tienen quién los apruebe. Si confirmas, quedarás (lmendez) como actor de bypass …
Para confirmar, escribe exactamente «bypass lmendez» y pulsa Intro: bypass lmendez
Token de administración de GitHub (permisos Administration de lectura y escritura, y Secrets y Environments de lectura; no se muestra ni se guarda): (pegas el token; no se ve)
init en labfe/ising-diluido (perfil enforced):
Archivos (70):
  created: .gitattributes
  created: .github/CODEOWNERS
  …
Etiquetas creadas o actualizadas: task, state:proposed, state:expired, …
  applied: repo_settings
  applied: ruleset:aucove-base
  …
Los archivos se subieron a la rama base.
Pendiente:
  - Prepara el proyecto en la tarea #1, «Preparar el proyecto»: abre tu agente en la carpeta del proyecto y pídele que te guíe para completar el plan, las personas y las partes.
Antes de trabajar: cada persona activa el correo noreply de GitHub y firma el acuerdo de autoría y de apertura (00-marco D-44 y D-49).
```

Qué dejó: la carpeta del proyecto, con su repositorio de git conectado al de GitHub, como si lo hubieras clonado; GitHub configurado (protecciones, etiquetas, cuánto tiempo se guardan las pruebas y el environment `aucove-evidence` para la firma del proyecto); los archivos del proyecto en un commit, y tu tarea de preparación, la #1, ya aceptada. Ahora borra el token de administración en GitHub.

Si `init` falla antes de subir el commit, deja la carpeta vacía otra vez: corrige lo que dice el error y repítelo. Si falla después, por ejemplo al aplicar las protecciones, repite el mismo comando en la misma carpeta y retoma donde quedó. [Si algo falla](cuando-pasa.md#si-algo-falla) explica los errores más comunes.

### 6. Abre tu agente y prepara el proyecto

En la misma carpeta, abre tu agente. No hay que copiar ni instalar nada más: la carpeta que dejó `init` ya trae lo que conecta al agente con aucove (`AGENTS.md`, `CLAUDE.md` y `.mcp.json`), y aucove ya está instalado en tu computadora. Si ya tenías el agente abierto en esa carpeta, reinícialo: las herramientas del proyecto y tu token se leen al arrancar.

```bash
claude
```

Al arrancar, el agente recibe el aviso «Preparación pendiente» y te ofrece preparar el proyecto. Con la 0.1.0 escribes `/empezar`; con la 0.1.0rc2, se lo pides con palabras: «prepara el proyecto en la tarea 1». Te entrevista en lenguaje normal:

> **Laura:** `/empezar`
>
> **Agente:** El proyecto está recién iniciado: el plan es el de la plantilla y solo estás tú. Reclamo tu tarea de preparación, la #1, y te hago unas preguntas. ¿De qué trata el proyecto y qué pregunta quieren responder?
>
> **Laura:** Simulaciones Monte Carlo del Ising 2D con dilución. Queremos saber si los exponentes siguen la universalidad fuerte o la débil.
>
> **Agente:** ¿Quiénes participan, y con qué cuenta de GitHub?
>
> **Laura:** Diego Ramírez, dramirez. Es mi suplente.
>
> **Agente:** ¿Cómo se reparten el artículo?
>
> **Laura:** Yo, la introducción, el modelo y las conclusiones. Diego, los métodos y los resultados.
>
> **Agente:** ¿Qué cuenta de IA usa cada uno?
>
> **Laura:** Los dos tenemos Claude Pro.
>
> **Agente:** Pro es un plan de consumo. En un repo privado, aucove pide por defecto cuentas con términos comerciales, como Claude Team, en las que el proveedor no entrena modelos con sus conversaciones. Pueden pasar a Team, o desactivar el entrenamiento en las dos cuentas y registrar la excepción. ¿Qué prefieren?
>
> **Laura:** Desactivamos el entrenamiento.
>
> **Agente:** Anoto que los dos usan Pro sin entrenamiento y registro la decisión. ¿Los datos incluyen información de personas?
>
> **Laura:** No. Son simulaciones que viven en el clúster del instituto.
>
> **Agente:** Entonces los datos crudos se quedan en el clúster, y en `data/README.md` anoto dónde están y a quién pedir acceso. ¿Tienes la fecha y la huella del acuerdo de autoría?
>
> **Laura:** Lo firmamos el 2 de noviembre. La huella es 3be1f0…
>
> *Siguen con el estilo de escritura. Al final, el agente escribe los archivos y los revisa.*
>
> **Agente:** Escribí `plan.md`, `roles.yaml`, la constitución y `data/README.md`; regeneré los archivos que salen de `roles.yaml` y la validación pasa. ¿Entrego la tarea 1?

Laura no tuvo que conocer claves como `account_type` o `data_policy`: el agente tradujo sus respuestas. Después de la entrega, revisa los cambios en GitHub y los integra con «Squash and merge»; mientras no haya suplente, GitHub la deja integrar su propia entrega por el bypass que confirmó en `init`.

### 7. Las primeras tareas

> **Laura:** Propón las primeras tareas a partir del plan.
>
> **Agente:** Propongo tres: «Método: Wolff y promedios de desorden» (parte methods, de Diego; METH-1), «Resultados: cruces del cumulante de Binder» (results, de Diego; RQ-1) e «Introducción y modelo» (introduction y model, tuyas). ¿Las creo?
>
> **Laura:** Sí.
>
> *El agente pide permiso para cada una y abre en GitHub las tareas #2, #3 y #4.*
>
> **Agente:** Quedaron propuestas. Para que se puedan reclamar, acéptalas tú: es una decisión que el agente no puede tomar.

Para aceptar una tarea en GitHub:

1. Abre el repo y entra a la pestaña **Issues**.
2. Abre la tarea, por ejemplo la #2.
3. A la derecha, en **Labels**, pulsa el engranaje y desmarca `state:proposed`. La tarea queda abierta para que su dueño la reclame.

O en tu terminal, con una confirmación escrita:

```text
PS C:\Users\laura\investigacion\ising-diluido> aucove task accept 2
Vas a aceptar la tarea #2 «Método: Wolff y promedios de desorden»: se quitará la etiqueta `state:proposed` y quedará abierta para reclamarla.
Para confirmar, escribe exactamente «accept 2» y pulsa Intro: accept 2
```

### 8. Avisa a tu equipo

Comprueba que cada persona aceptó la invitación a la organización y pásale la [guía del investigador](investigador.md): instalar, crear su token, clonar y abrir su agente. Aprueba sus tokens cuando aparezcan en Pending requests. Si alguien trabajará desde el navegador, instala la app de Claude para GitHub en el repo.

Cuando termines, prepara la [firma del proyecto](firma-del-proyecto.md), que toma unos 15 minutos: con ella, quien revise el trabajo desde afuera comprueba que cada registro lo emitió tu proyecto, y no solo cuándo existía.

## Cada semana

- **Revisar entregas.** En tu agente, `/pendientes` te resume cada entrega: qué cambió, la nota de sesión, los checks y los avisos de choque. Luego, en GitHub, abre la entrega en Pull requests, mira la pestaña «Files changed» y el enlace al PDF que dejó el CI, pulsa «Review changes» → «Approve» y, por último, «Squash and merge». Si algo no está bien, elige «Request changes» con un comentario y la persona corrige en la misma entrega. El plazo esperado es de 72 horas.
- **Nadie aprueba lo suyo.** Tus entregas las aprueba el suplente.
- **Aceptar tareas.** Cualquiera puede proponer tareas. Tú o el suplente las aceptan como en el paso 7.
- **Decisiones y rumbo.** `/decision` registra una decisión del grupo; una decisión no se borra, se reemplaza con otra. `/direccion` propone un cambio en el plan; si lo aceptas, editas `plan.md` en una tarea tuya.
- **Si el proyecto usa LaTeX.** Alguien cuida que el artículo se lea como un solo texto: el editor, que eres tú salvo que el grupo nombre a otra persona. Su aprobación hace falta en las entregas que tocan LaTeX, y `/editar-articulo` le ayuda a encontrar repeticiones, huecos y notación inconsistente.

## El tablero del proyecto

Hay dos formas de ver cómo va el proyecto. En una terminal, dentro de la carpeta del proyecto, escribe `aucove` solo, sin nada más: imprime un tablero en texto. No se escribe en el chat del agente; en Claude Code puedes correrlo desde el chat como `! aucove`.

```text
PS C:\Users\laura\investigacion\ising-diluido> aucove
aucove 0.1.0

Proyecto: Exponentes criticos del Ising diluido
Identidad: @lmendez (lead)

Tareas abiertas (1):
  #9 [abierta] Algoritmo de Wolff (partes: methods)
Quien trabaja en que (2; * = tuyas):
  #8 [en revision] @dramirez: Metodos en el Ising diluido (partes: methods)
 *#3 [reclamada] @lmendez: Introduccion y modelo (partes: introduction, model)
Partes y duenos:
  introduction (Introduccion): @lmendez
  model (Modelo): @lmendez
  methods (Metodos): @dramirez
  results (Resultados): @dramirez
  conclusions (Conclusiones): @lmendez
Sellos de tus entregas en `.aucove/evidence/` (1):
  submission-4f2a9c1…-a1

Siguiente paso: abre tu agente en esta carpeta y usa el skill `empezar`, o reclama una tarea con `aucove claim <n>`.
```

En el chat del agente, escribe `/estado` o pregúntale con palabras. Lee los mismos datos y te contesta con un resumen:

> **Laura:** ¿Cómo va el proyecto?
>
> **Agente:** Hay una tarea abierta, la 9, y dos en curso: Diego entregó la 8 y espera tu revisión, y tú tienes reclamada la 3. No hay avisos de choque y la versión común compila. ¿Preparo la revisión de la 8?

## Lo que solo decide una persona

Configurar el repo con `init`, aceptar o cancelar tareas, aprobar, pedir cambios e integrar. El agente puede dejarlo todo listo, pero la decisión es tuya o del suplente.
