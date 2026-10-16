# Configurar aucove con tu agente

[Guía de uso](README.md) › Configurar con tu agente

Tu agente puede acompañarte en la [configuración inicial](../../README.md#configuración-inicial) del README: te pregunta en qué paso vas, comprueba lo que ya hiciste, te dice qué sigue según tu papel y te explica cada error. Aquí están los mensajes que le pegas, en orden. Sirven con cualquier agente que pueda correr comandos y abrir una página web, como Claude Code, Codex o Gemini CLI.

## Lo que haces tú

El agente no hace estos pasos, a propósito. Te dice cuándo y cómo, y los haces tú:

- **Los clics en GitHub:** crear la organización, el repo y tus tokens.
- **Instalar git, uv y aucove**, en una terminal abierta desde el menú Inicio. En Windows, lo que se instala desde la app del agente queda en una copia que solo ve esa app.
- **Guardar tu token.** Nunca lo pegues en el chat: el agente solo comprueba que existe, sin mostrarlo.
- **Correr `aucove init`**, en una terminal o en el panel Terminal de Claude Desktop. Pide confirmaciones escritas y el token de administración, que no deben pasar por la IA.

El agente sí comprueba, con comandos que solo leen, tu identidad de git, la versión de aucove, si existe `GH_TOKEN` y, ya con el proyecto, `aucove doctor`.

## Dónde abrirlo

Ábrelo en la carpeta donde guardas tus proyectos, por ejemplo `investigacion`, no dentro de la carpeta del proyecto: `init` necesita una carpeta vacía, y la app del agente puede guardar ahí sus propios archivos.

La app solo ve lo que instalas o guardas antes de abrirla. Por eso, cada vez que instales algo o guardes tu token, ciérrala del todo y vuelve a abrirla en la misma carpeta; a Claude Desktop, desde su ícono en la bandeja del sistema. El agente te lo recuerda. En macOS, Claude Desktop no toma `GH_TOKEN` de `~/.zshrc`: guárdalo también en su editor de entorno local, como dice el paso 6 del README.

## 1. Durante la configuración

Pega este mensaje cada vez que abras el agente para configurar, con tu papel y el repo del proyecto en lugar de lo que va entre `< >`. El agente te pregunta en qué paso vas y sigue desde ahí.

```text
Voy a configurar aucove (https://github.com/aucove/aucove) para el proyecto <organización>/<repo>, como <responsable o investigador>. Acompáñame con la configuración inicial del README, paso por paso y en orden: https://github.com/aucove/aucove#configuración-inicial
- Pregúntame en qué paso voy. Antes de cada paso, pregúntame si ya lo hice y, si se puede, compruébalo con comandos que solo leen.
- No instales nada, no cambies la configuración de git y no crees archivos: dime qué hacer y lo hago yo en mi terminal.
- Nunca me pidas el token ni muestres su valor. De GH_TOKEN, comprueba solo si existe.
- Cuando instale algo o guarde el token, recuérdame cerrar esta app del todo y volver a abrirla, porque antes no lo verás. Al volver, te pegaré este mensaje otra vez.
- aucove init lo corro yo en una terminal. Tú me dices cuándo y me explicas sus preguntas.
```

Si tu agente no puede abrir páginas web, pégale también la sección «Configuración inicial» del README.

Así empieza, por ejemplo, con Laura como responsable:

> **Laura:** *(pega el mensaje)*
>
> **Agente:** ¿En qué paso vas? ¿Ya tienes cuenta de GitHub con «Keep my email addresses private» activado?
>
> **Laura:** Sí. También creé la organización `labfe`, pero no el repo.
>
> **Agente:** Entonces vamos con el repo vacío: en la organización, Repositories → New repository, con el nombre `ising-diluido` y Private, sin README, sin `.gitignore` y sin licencia. Avísame cuando esté.
>
> *Siguen paso por paso. Después de instalar aucove y guardar su token, Laura cierra la app, la abre otra vez y pega el mensaje de nuevo.*
>
> **Agente:** git tiene tu nombre y tu dirección `noreply`, `aucove --version` dice la versión del README y `GH_TOKEN` existe. Sigue el paso 7: crea el token de administración y, en una terminal, corre `aucove init --repo labfe/ising-diluido --dry-run`. Si algo falla, pégame lo que diga.

## 2. Si algo falla

Pega lo que dijo la terminal. Ninguna salida de aucove muestra tu token.

```text
Esto dijo la terminal al correr <el comando>. ¿Qué significa y qué hago?
<pega aquí la salida>
```

## 3. Después de `init` (responsable)

Abre otra sesión del agente, ahora dentro de la carpeta del proyecto, para que cargue sus reglas y sus herramientas. Pega:

```text
Comprueba con aucove doctor que todo esté listo. Si lo está, prepara conmigo el proyecto en la tarea 1.
```

El agente revisa tu entorno y te entrevista para completar el plan, las personas y las partes, como en [Responsable › Abre tu agente y prepara el proyecto](responsable.md#3-abre-tu-agente-y-prepara-el-proyecto).

## 4. Avisar a tu equipo (responsable)

```text
Redacta el mensaje para avisarle a mi equipo que el proyecto <organización>/<repo> está listo. Que acepten la invitación de GitHub y sigan como investigadores la configuración inicial de aucove (https://github.com/aucove/aucove#configuración-inicial), solos o con su agente (https://github.com/aucove/aucove/blob/main/docs/es/configurar-con-agente.md).
```

Cuando sus tokens aparezcan como pendientes, apruébalos en la organización: **Settings** → **Personal access tokens** → **Pending requests**.

## 5. Tu primera sesión (investigador)

Cuando hayas clonado el proyecto, en el paso 7, abre otra sesión del agente dentro de esa carpeta y pega:

```text
Comprueba con aucove doctor que todo esté listo y dime cuáles son mis partes y mis tareas.
```

Desde ahí sigue [un día de trabajo](investigador.md#un-día-de-trabajo-en-física).

## 6. Cuando venza tu token

```text
Venció mi token de aucove. Acompáñame a crear otro y a guardarlo en GH_TOKEN, con los pasos 5 y 6 de https://github.com/aucove/aucove#configuración-inicial, sin pedirme el token.
```
