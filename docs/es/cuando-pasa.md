# Cuando pasa

[Guía de uso](README.md) › Cuando pasa

No hace falta aprenderlo antes. Si ocurre, el agente o el CI te avisan y te proponen qué hacer.

## Con tu trabajo

<details>
<summary>Te piden cambios</summary>

Vuelves con `/empezar` y el número de la tarea, corriges y entregas otra vez. Es una ronda nueva de la misma entrega, y se sella de nuevo.

</details>

<details>
<summary>Quieres cambiar de tarea</summary>

Dile al agente «cambia a la 24»: guarda lo pendiente y trae la otra. Puedes tener hasta 3 tareas activas; las que están en revisión no cuentan.

</details>

<details>
<summary>Te vas unos días</summary>

Una tarea vence a los 14 días sin actividad, con aviso 3 días antes. Vencer no borra nada: si vuelves y guardas, sigue siendo tuya. Si no vuelves, el responsable puede pasársela a otra persona, y tu trabajo queda archivado.

</details>

<details>
<summary>Necesitas una macro o una referencia nueva</summary>

Con LaTeX, el agente escribe la forma explícita y deja el pedido de la macro para el editor del artículo. Las referencias se agregan a Zotero y se pegan en `pending.bib` de tu parte; el editor las pasa a la bibliografía común.

</details>

<details>
<summary>Trabajas desde el navegador</summary>

Todo funciona igual, salvo soltar una tarea, que se hace desde la terminal o desde Claude Desktop. Abre cada sesión con un solo repositorio: con varios, no se cargan las reglas del proyecto. Recuerda que en la web el código corre en una máquina de la nube que solo ve el repo ([Dónde corre el código](como-funciona.md#dónde-corre-el-código)).

</details>

## Con otras personas

<details>
<summary>Tu trabajo choca con otra tarea</summary>

Al reclamar y al entregar, el agente compara tu trabajo con las tareas activas. Si ve un posible choque, como una misma macro definida de dos formas, lo deja como aviso en la entrega y menciona al responsable. No bloquea nada: el responsable decide, casi siempre con una decisión.

</details>

<details>
<summary>Alguien cambió algo que usas</summary>

Pide «actualiza» para traer lo nuevo a tu tarea. Si el cambio está en un archivo que no es tuyo y rompe tu trabajo, el agente no lo arregla por su cuenta: escala al responsable y la tarea queda marcada como bloqueada, sin vencer, hasta que se resuelva.

</details>

<details>
<summary>La versión común deja de compilar</summary>

El CI abre un aviso con los commits sospechosos, y todos lo ven en `/estado`. Quien lo rompió entrega un arreglo. Cuando vuelve a compilar, el aviso se cierra solo.

</details>

<details>
<summary>El plan ya no encaja</summary>

Con `/direccion` propones el cambio y citas la pregunta o la hipótesis afectada. El responsable decide y edita el plan. Las entregas que tocan esa parte del plan quedan marcadas para revisarlas con cuidado.

</details>

## Datos y personas

<details>
<summary>¿Dónde van los datos del proyecto?</summary>

Los datos crudos nunca entran al repositorio, sean de simulaciones, mediciones, encuestas o expedientes. Se quedan donde ya viven: tu computadora, el clúster, el servidor del hospital o un repositorio de datos. `data/README.md` dice qué son, dónde están y cómo pedir acceso.

Al repo llegan el código del análisis, los resultados agregados (tablas, figuras y métricas) y, en `provenance.yaml`, qué script produjo cada resultado y la huella SHA-256 de los datos que usó. Ningún archivo del repo puede pasar de 5 MiB; los modelos y los conjuntos grandes van a su propio repositorio, como Hugging Face, Zenodo o el de tu institución.

</details>

<details>
<summary>¿Puede el agente ver mis datos?</summary>

Usar datos reales es lo normal. Lo que cambia según qué tan sensibles son es si la IA puede verlos. Lo fija el responsable al preparar el proyecto:

- **Públicos o internos**, inéditos pero sin datos personales, que es lo más común: sí. El agente puede leerlos y analizarlos en tu computadora, con una cuenta comercial o, si el grupo lo acordó, con una de consumo sin entrenamiento.
- **Restringidos**: datos personales aunque estén seudonimizados, de salud, bajo un convenio de confidencialidad o con condiciones del comité de ética. En la v0.1 no se le dan a la IA, salvo con un contrato institucional sin retención de datos y el visto bueno del comité, y siempre fuera del repositorio. Lo habitual es que el agente ayude a escribir el código con datos de prueba de la misma forma (datos sintéticos) y que la persona lo corra sobre los datos reales, sin IA, donde estén.
- **Solo locales**, que no pueden salir de la institución: solo con modelos que corren en la propia infraestructura.

Recuerda que lo que el agente lee, incluida la salida de los comandos que corre, llega al modelo ([Dónde corre el código](como-funciona.md#dónde-corre-el-código)).

</details>

<details>
<summary>Alguien no usa IA</summary>

Puede trabajar con aucove sin ningún agente. Hace lo mismo que los demás en «Antes del primer día» (instalar aucove, crear su token y clonar el repo), salvo el agente, y escribe sus partes con el editor que prefiera, como TeXstudio o VS Code, dentro de la carpeta del proyecto. Su día se ve así:

```bash
aucove                                    # el tablero: tareas y partes
aucove claim 30                           # reclama la tarea 30 y crea su rama
# … edita sus archivos con su editor …
aucove save -m "Antecedentes clínicos" --ai-use none
aucove submit --summary "Antecedentes: criterios Sepsis-3" --note-file ../nota.json --ai-use none
```

`nota.json` es su nota de sesión, un archivo pequeño fuera de la carpeta del proyecto:

```json
{"done": "Redacté los antecedentes clínicos con los criterios Sepsis-3.", "next": "Agregar la tabla de criterios."}
```

Si le piden cambios, vuelve a la tarea con `aucove switch 30`, corrige, guarda y vuelve a entregar. Revisa y aprueba en la web de GitHub, como cualquier otra persona. Sus commits dicen «AI-Use: none»; sin esa opción quedarían como «no declarado».

</details>

<details>
<summary>Alguien deja el proyecto</summary>

Antes de quitarle el acceso, se exportan sus pruebas: su copia de los sellos, los registros de sus entregas y sus partes. Con eso puede probar después qué hizo y cuándo.

</details>

<details>
<summary>Hay una disputa de prioridad</summary>

Un árbitro comprueba, sin instalar el proyecto, que un archivo es exactamente el que se selló en una fecha: usa `aucove evidence verify` con el registro de ese día y la apertura de ese archivo. El resto del trabajo sigue oculto.

</details>

## Si algo falla

<details>
<summary><code>aucove doctor</code> dice que no hay token, o que GitHub lo rechazó</summary>

Falta el token, venció o se borró. Crea uno nuevo con los pasos de [Tu token de GitHub](investigador.md#tu-token-de-github), déjalo en `GH_TOKEN` y vuelve a abrir la terminal y tu agente.

</details>

<details>
<summary>El token es nuevo, pero GitHub responde que no tienes permiso</summary>

Lo más probable es que la organización todavía no lo apruebe. El responsable lo aprueba en la organización: Settings → Personal access tokens → Pending requests. Si no es eso, revisa que el token incluya el repo del proyecto.

</details>

<details>
<summary>El agente no ve el contexto del proyecto</summary>

Corre `aucove doctor` en la terminal antes de editar: dice qué falta (la instalación, el token o los permisos) y cómo arreglarlo. Revisa también que abriste el agente dentro de la carpeta del proyecto.

</details>

<details>
<summary>aucove no está instalado o la versión no coincide</summary>

El agente se detiene y te pide instalarlo con el comando de la guía. No lo instala por su cuenta.

</details>

<details>
<summary>El sellado dice que una integración quedó sin la firma del proyecto</summary>

Lo dicen el resumen del job, un aviso amarillo en Actions y el comentario de la entrega, con el motivo. El registro se selló igual, porque la fecha va primero, y la firma del índice del hito lo cubre después. Para que las siguientes se firmen:

- `no_secret`: falta el secreto `AUCOVE_EVIDENCE_KEY` en el environment `aucove-evidence`. Agrégalo con el [paso 3](firma-del-proyecto.md#3-con-el-modo-ci-guarda-la-llave-en-github).
- `key_mismatch`: la llave del secreto no es una llave vigente de `project.yaml`, por ejemplo porque la cambiaste en un lado y no en el otro. Revisa que coincidan las dos.
- `sign_failed`: el secreto no es una llave `ssh-ed25519` sin frase de paso. Vuelve a subirla sin la frase.

</details>

<details>
<summary><code>init</code> se detiene porque la llave del proyecto es un secreto del repo o de la organización</summary>

Un secreto del repo o de la organización lo puede leer un workflow de cualquier rama, así que la llave no puede estar ahí. Bórralo en GitHub (Settings → Secrets and variables → Actions), ponlo en el environment `aucove-evidence` ([paso 3](firma-del-proyecto.md#3-con-el-modo-ci-guarda-la-llave-en-github)) y vuelve a correr `aucove init`. Si la llave estuvo expuesta, cámbiala.

</details>

<details>
<summary>Perdiste la llave del proyecto, o te la robaron</summary>

Ponle fecha de fin a la llave y crea otra: [Cambiar la llave, o anular una que te robaron](firma-del-proyecto.md#cambiar-la-llave-o-anular-una-que-te-robaron). Lo firmado y sellado antes de esa fecha sigue valiendo.

</details>
