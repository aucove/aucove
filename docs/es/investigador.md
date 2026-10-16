# Investigador

[Guía de uso](README.md) › Investigador

Lo que haces tú: preparar tu computadora una vez y, después, conversar con tu agente.

> [!NOTE]
> Describe la versión 0.1 completa. Los atajos como `/empezar` o `/entregar` llegan con la 0.1.0 para Claude Code y Codex. Con la 0.1.0rc3, o con otro agente, se le pide lo mismo con palabras, por ejemplo «empecemos con la tarea 8» o «entrega la tarea», o se hace con los comandos `aucove`.

## Antes del primer día

Se hace una vez. Hay dos formas de trabajar:

- **En tu computadora.** Es lo habitual. Instalas git, tu agente y aucove, creas tu token de GitHub, clonas el repo del proyecto y abres el agente en esa carpeta. Lo que el agente ejecuta, como tus scripts de análisis, corre en tu computadora y con tus datos. Son los cinco pasos de abajo.
- **Desde el navegador.** Entras a claude.ai/code y eliges el repo del proyecto. La sesión instala aucove sola, así que no instalas nada ni creas token. Lo que el agente ejecuta corre en una máquina de la nube que solo ve el repo. Necesitas un plan de Claude que incluya Claude Code en la web (Pro, Max o Team; en Enterprise, un asiento con Claude Code) y que el responsable haya instalado la app de Claude en el repo.

La diferencia está en [Cómo funciona › Dónde corre el código](como-funciona.md#dónde-corre-el-código).

### 1. Acepta la invitación y protege tu correo

GitHub te manda por correo la invitación a la organización del grupo: acéptala. Después, en GitHub, ve a Settings → Emails, marca «Keep my email addresses private» y usa en git la dirección `noreply` que te muestra esa página. Así tu correo personal no queda en la historia del proyecto.

```bash
git config --global user.email "<número>+<tu-usuario>@users.noreply.github.com"
```

### 2. Instala git, tu agente y aucove

git y tu agente de IA (por ejemplo Claude Code, Codex, Gemini CLI o GitHub Copilot) se instalan como cualquier programa. aucove se instala en dos pasos, primero uv y después aucove, con comandos que fijan la versión exacta y sus dependencias: están en la [instalación del README](../../README.md#instalación). Después cierra y vuelve a abrir la terminal y tu agente.

### 3. Crea tu token de GitHub

Es la llave con la que aucove trabaja en GitHub en tu nombre. Los pasos están en [Tu token de GitHub](#tu-token-de-github), más abajo.

### 4. Clona el repo del proyecto y comprueba

Elige una carpeta tuya, clona ahí el repo del grupo (no el de aucove) y pide a aucove que revise que todo esté listo.

```bash
git clone https://github.com/labfe/ising-diluido.git
cd ising-diluido
aucove doctor
```

Así se ve (abreviado):

```text
PS C:\Users\dramirez\investigacion\ising-diluido> aucove doctor
Núcleo 0.1.0 · C:\Users\dramirez\investigacion\ising-diluido · perfil enforced
[ok] identity: @dramirez, con un token fine-grained de `GH_TOKEN` (solo este repositorio).
…
```

Si algo falta, cada línea dice qué y cómo arreglarlo, por ejemplo «[falla] identity: No hay token de la forja…» con el enlace para crearlo.

### 5. Abre tu agente en esa carpeta

```bash
claude
```

O el comando de tu agente, como `codex`, `copilot` o `gemini`; o abre la carpeta en tu editor, o elígela en la pestaña Code de Claude Desktop. La primera vez que escribes `/empezar`, el agente te da una bienvenida corta: tus partes, tus tareas y cómo se trabaja.

## Tu token de GitHub

Se hace una vez, y otra vez al vencer. Es una llave que le das a aucove para que actúe en GitHub en tu nombre: crear la rama de tu tarea, guardar tu trabajo y abrir tu entrega. La crea cada persona que trabaja desde su computadora; quien trabaja en la web no la necesita. Se limita al repo del proyecto y a los permisos justos.

1. Abre el formulario de GitHub con los permisos ya marcados (`aucove doctor` también te da este enlace). Cambia `<org>` y `<repo>` por los de tu proyecto:

   ```text
   https://github.com/settings/personal-access-tokens/new?name=aucove-<repo>&description=aucove+(<repo>)&target_name=<org>&expires_in=30&contents=write&issues=write&pull_requests=write&actions=read&statuses=read
   ```

   A mano: en GitHub, Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token.
2. En **Resource owner**, elige la organización del proyecto. En **Expiration**, 30 días (como máximo, 90).
3. En **Repository access**, elige «Only select repositories» y marca solo el repo del proyecto. El enlace no puede elegirlo por ti.
4. Revisa los permisos del repositorio: **Contents**, **Issues** y **Pull requests** en «Read and write»; **Actions** y **Commit statuses** en «Read». **Metadata** queda en «Read» solo. No agregues ningún otro.
5. Pulsa **Generate token** y copia el token, que empieza con `github_pat_`. Guárdalo en tu gestor de contraseñas: GitHub no te lo vuelve a mostrar.
6. Si la organización pide aprobar los tokens (lo normal), el tuyo queda pendiente hasta que el responsable lo apruebe.
7. Déjalo donde aucove lo busca, de una de estas dos formas:
   - **Variable de entorno `GH_TOKEN`.** En Windows, busca «variables de entorno» en el menú Inicio, abre «Editar las variables de entorno de esta cuenta» y crea una nueva con nombre `GH_TOKEN` y el token como valor. En macOS o Linux, agrega `export GH_TOKEN="github_pat_…"` al final de `~/.zshrc` (o `~/.bashrc`) con un editor de texto. Cierra y vuelve a abrir la terminal y tu agente.
   - **Con GitHub CLI**, que lo guarda en el llavero del sistema: corre `gh auth login`, elige GitHub.com y HTTPS, luego «Paste an authentication token», y pega el token.
8. Comprueba con `aucove doctor` en la carpeta del proyecto: debe decir «token fine-grained» y «solo este repositorio».

> [!WARNING]
> Nunca pegues el token en el chat con tu agente ni en un archivo del proyecto. Si se te escapa, bórralo en GitHub (Settings → Developer settings → Fine-grained tokens) y crea otro. Cuando venza, `aucove doctor` te avisa y repites estos pasos. Tu agente corre con tus permisos y en principio podría leerlo: por eso el token se limita a un solo repo y sin permisos de administración.

## Un día de trabajo en física

Diego es posdoc. Con Laura, la responsable, escribe un artículo sobre los exponentes críticos del modelo de Ising diluido, y tiene a su cargo los métodos y los resultados. Trabaja en su laptop con Claude Code.

> *Diego abre la terminal en la carpeta del proyecto y escribe `claude`. Al arrancar, el agente ya sabe quién es Diego, cuáles son sus partes y en qué trabaja cada quien.*
>
> **Diego:** `/empezar`
>
> **Agente:** Tienes dos tareas abiertas en tus partes: la 8, «Métodos en el Ising diluido», y la 9, «Algoritmo de Wolff». Laura está con la introducción. ¿Con cuál empezamos?
>
> **Diego:** La 8.
>
> **Agente:** Para reclamarla necesito tu permiso.
>
> *Diego acepta en el aviso de permisos.*
>
> **Agente:** La tarea 8 es tuya. Ya leí el plan: la pregunta principal, RQ-1, es si los exponentes siguen la universalidad fuerte o la débil.
>
> **Diego:** Busca trabajos de 2010 a 2025 sobre los exponentes críticos del Ising 2D diluido. Hazme una tabla con el método, los tamaños de red, el valor de ν y si apoyan la universalidad fuerte o la débil.
>
> **Agente:** Encontré 14 trabajos en arXiv y en revistas. La tabla está en `sections/methods/literatura.md`, con el DOI o el número de arXiv de cada uno. No coinciden: siete apoyan la universalidad fuerte, cinco la débil y dos no concluyen.
>
> **Diego:** ¿Por qué crees que no coinciden? ¿Qué podríamos aportar nosotros?
>
> **Agente:** Difieren en tres cosas: el tamaño de las redes, cómo tratan las correcciones logarítmicas y cuántas realizaciones del desorden promedian. Podríamos aportar redes más grandes, un ajuste que separe las correcciones logarítmicas o una comparación entre dilución de sitios y de enlaces. Esta última queda fuera del alcance del plan (OUT-1).
>
> **Diego:** Lo de las redes grandes ya es la tarea 9. Desarrolla el ajuste: ¿qué forma usarías y quién la ha usado?
>
> *Discuten la forma del ajuste un buen rato. Diego corrige una suposición del agente y se quedan con una versión que tiene un parámetro menos.*
>
> **Diego:** Haz un script que grafique ν contra la concentración de dilución con los valores de la tabla y sus barras de error.
>
> **Agente:** Escribí `sections/methods/analysis/nu_vs_p.py` y lo corrí. La figura quedó en `sections/methods/figures/nu-vs-p.pdf`, anotada en `provenance.yaml` con el script que la produjo. Guardé el trabajo.
>
> *El script corrió en la laptop de Diego, con su Python y sus archivos.*
>
> **Diego:** Redacta el párrafo de métodos que justifica el ajuste y cita los tres trabajos principales.
>
> **Agente:** Dos de esos trabajos no están en la bibliografía del grupo. Agrégalos a Zotero con estos DOI y pega lo que exportes en `pending.bib` de tu parte. Mientras, dejo el párrafo con esas dos citas marcadas.
>
> *Diego los agrega a Zotero con su DOI y pega la exportación. El agente completa las citas.*
>
> **Diego:** `/entregar`
>
> **Agente:** Redacté la nota de sesión: qué buscamos y con qué criterio, por qué descartamos comparar sitios y enlaces, y que falta probar el ajuste con nuestros datos. Comparé tu entrega con las demás tareas activas y no veo choques. Para abrir la entrega necesito tu permiso.
>
> *Diego acepta. La entrega queda abierta en GitHub como un PR, y el CI la revisa, compila el PDF y deja el sello:*
>
> **Sello de entrega** · PR #14 · tarea 8 · 18/11/2026, 17:42 UTC
>
> *Laura recibe el aviso, revisa la entrega en GitHub y la integra. Si le pidiera cambios, Diego volvería con `/empezar 8`, corregiría y entregaría otra vez.*

## Un día de trabajo en machine learning

Pablo hace el doctorado en otra universidad. Con Valeria, la responsable, arma un benchmark abierto de modelos de lenguaje pequeños, y tiene a su cargo los experimentos y los resultados. Trabaja con Codex en una estación con GPU.

> **Pablo:** `/empezar 7`
>
> **Agente:** La tarea 7, «Ablación de la longitud de contexto», es tuya. Un aviso: Valeria tiene activa la tarea 6, que cambia la partición de prueba. Si se integra antes que la tuya, tus cifras van a cambiar.
>
> **Pablo:** Antes de programar: ¿qué longitudes probarías, y con cuántas semillas? Tengo una GPU de 24 GB y quiero que todo corra en una noche.
>
> **Agente:** 128, 256 y 512 tokens con 5 semillas caben en unas 6 horas con el tamaño de lote actual. Con 1024 no alcanza la memoria sin bajar el lote; la dejaría para otra tarea.
>
> **Pablo:** De acuerdo. Agrega la opción `--max-len` a `train.py` y un script que lance las 15 corridas.
>
> **Agente:** Listo, están en `sections/experiments/analysis/`. Las corridas largas conviene que las lances tú, fuera de esta sesión.
>
> *Pablo lanza el script en su estación antes de irse: las 15 corridas usan su GPU, no la nube. A la mañana siguiente:*
>
> **Pablo:** Junta los resultados: media y desviación de la F1 por longitud, en una tabla y una gráfica.
>
> **Agente:** La tabla y la gráfica están en tu parte de experimentos, anotadas en `provenance.yaml`. Los modelos entrenados pesan 300 MB cada uno y no entran al repo: se quedan en tu disco o en Hugging Face, y Valeria anota en `data/README.md` dónde están.
>
> **Pablo:** ¿Qué conclusión sacarías?
>
> **Agente:** La F1 sube de 128 a 256 tokens. Entre 256 y 512 la diferencia es menor que la variación entre semillas, así que no la daría por real.
>
> **Pablo:** Escríbelo así en resultados, con la tabla.
>
> **Pablo:** `/entregar`
>
> **Agente:** Al comparar tu entrega con las tareas activas veo un posible choque con la 6 de Valeria: si cambia la partición, estas cifras cambian. Lo dejo como aviso en la entrega para que ella decida.
>
> *Valeria integra primero su tarea 6. Pablo pide «actualiza», vuelve a correr el script y entrega otra vez. La segunda entrega ya cuadra.*

## Lo que escribes o dices

| En el chat del agente | Qué pasa |
|---|---|
| `/empezar` | Te muestra tus tareas, reclama la que elijas (o propone una nueva) y lee el plan. |
| Lo que necesites | Buscar, leer, discutir ideas, escribir, programar y correr código. |
| `/entregar` | Escribe la nota de sesión, busca choques con otras tareas y abre la entrega para revisión. |
| «guarda» | Opcional: el agente guarda solo al terminar cada bloque de cambios. |
| «actualiza» | Trae a tu tarea lo nuevo de la versión común. El agente lo sugiere cuando hace falta. |
| `/estado` | Qué está abierto, quién hace qué y qué espera revisión. |
| `/decision` | Registra una decisión del grupo, como una convención de notación. |

Lo que empieza con `aucove`, como `aucove doctor`, es un comando de terminal: se escribe en PowerShell o en la Terminal, no en el chat. En Claude Code puedes correr un comando desde el chat si lo empiezas con `!`, por ejemplo `! aucove`.

## Qué queda guardado

- **En el repo:** tus archivos (texto, tablas, código y figuras); cada guardado, con la herramienta y el modelo de IA que usaste; la nota de cada entrega, y las decisiones.
- **Fuera del repo:** tu conversación con el agente, que queda en tu cuenta o en tu computadora; los datos crudos, que `data/README.md` describe, y los archivos de más de 5 MiB.
- **Sin que hagas nada:** el sello de cada entrega y de cada integración.

## Lo que el agente no hace

- Tocar las partes de otras personas. Si hace falta un cambio ahí, te ayuda a pedírselo a su dueño.
- Aprobar, aceptar tareas o integrar. Eso lo decide una persona.
- Inventar referencias. Las citas salen de la bibliografía del grupo.
- Subir datos personales, credenciales o datos crudos.
- Instalar o actualizar aucove.
