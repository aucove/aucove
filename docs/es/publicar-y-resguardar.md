# Publicar y resguardar

[Guía de uso](README.md) › Publicar y resguardar

Lo que hace el responsable cuando el grupo cierra una etapa: un preprint, el envío a una revista, una tesis o el final del proyecto. A esos momentos los llamamos hitos.

Mientras el grupo trabaja, los sellos se hacen solos y quedan en GitHub. Pero GitHub borra los registros de su CI después de un tiempo (400 días en un repo privado, 90 en uno público), y lo que está en un repo privado no es público ni tiene un DOI para citarlo. Por eso, en cada hito, el responsable hace dos trabajos:

| Trabajo | Para qué | Cuándo | En la v0.1 |
|---|---|---|---|
| Los sellos | Probar qué existía y desde cuándo | En cada entrega e integración | Automático |
| Resguardar las pruebas | Que los sellos sobrevivan a GitHub, guardados en dos lugares | En cada hito y al cerrar | A mano |
| Publicar | Abrir el trabajo y darle un DOI | Cuando el grupo lo decida | A mano |

La versión 0.2 hará estos pasos con `aucove evidence export`, `deposit` y `disclose`. En la v0.1 se hacen con `git` y con GitHub CLI.

## Resguardar las pruebas

Se arma un paquete con todo lo que prueba qué se entregó y cuándo, y se guarda fuera de GitHub. Necesitas git, [GitHub CLI](https://cli.github.com) (`gh`) y tu token de uso diario, que `gh` toma de `GH_TOKEN`. En Windows, estos comandos van en Git Bash, que viene con git.

### 1. Crea una carpeta para el paquete

Fuera de la carpeta del proyecto: el paquete nunca se sube al repo. En `R` va el nombre de tu repo.

```bash
mkdir evidencia-hito1 && cd evidencia-hito1
R=labfe/ising-diluido
```

### 2. Baja los registros del sellado

Cada sello es una carpeta que el CI guarda como «artefacto». Este comando baja los de todas las corridas, antes de que GitHub los borre.

```bash
for id in $(gh run list -R $R --workflow seal.yml --limit 500 --json databaseId --jq '.[].databaseId'); do
  gh run download $id -R $R --pattern 'evidence-*' --dir artefactos/$id
done
```

### 3. Guarda los comentarios de los sellos

El CI también deja cada sello como comentario en su entrega. Se guardan todos en un archivo.

```bash
gh api --paginate repos/$R/issues/comments \
  --jq '.[] | select(.body | contains("aucove:ci:evidence"))' > comments.jsonl
```

### 4. Copia la historia completa

Toda la historia del repo en un solo archivo, incluidas las entregas que no se integraron.

```bash
git clone --mirror https://github.com/$R.git repo.git
git -C repo.git fetch origin '+refs/pull/*/head:refs/pull/*/head'
git -C repo.git bundle create ../repo.bundle --all
rm -rf repo.git
```

### 5. Calcula la huella de cada archivo

Una lista con la huella SHA-256 de todo lo que hay en el paquete.

```bash
find . -type f ! -name index.txt -print0 | xargs -0 sha256sum > index.txt
cd ..
```

### 6. Firma el índice del paquete

Con la [firma del proyecto](firma-del-proyecto.md), en los dos modos. Firmas `index.txt` con tu llave, y la firma queda sellada en una carpeta `signature/` dentro del paquete. Así queda probado que el paquete lo armó el proyecto, incluidas las entregas, que el CI no firma. Se corre en la carpeta del proyecto y en una terminal normal, no dentro de tu agente: te pide escribir una confirmación y la frase de tu llave, y nada de eso debe pasar por la IA.

```bash
cd ising-diluido
aucove evidence sign ../evidencia-hito1 --key $HOME/.ssh/aucove-ising-diluido
```

Así se ve (ejemplo abreviado):

```text
Firma del índice del paquete de evidencia (00-marco D-72).
  Paquete: C:\Users\laura\investigacion\evidencia-hito1
  `index.txt`: 214 archivos, SHA-256 `5f2c…`
  …
Para confirmar, escribe exactamente «sign evidencia-hito1» y pulsa Intro: sign evidencia-hito1
Enter passphrase: (tu frase; no se ve)
Firma del índice en `signature` (…\evidencia-hito1): la llave `llave-2026` (`SHA256:8jmj…`) firmó `index.txt`; 3 de 3 pruebas obtenidas.
```

Después de firmarlo, el paquete no se modifica: lo nuevo va en el paquete del hito siguiente. Sin la firma del proyecto, sáltate este paso.

### 7. Comprueba el paquete

Opcional, en la carpeta del proyecto. Revisa cada registro, la firma del índice y que cada archivo coincida con su huella:

```bash
aucove evidence verify ../evidencia-hito1
```

Con la firma, cada registro dice «origen: del proyecto». Cada registro trae su propio `VERIFY.md`, y la carpeta `signature/` también, así que cualquiera puede verificarlo después, incluso sin aucove ([Verificar](verificar.md)).

### 8. Guárdalo en dos lugares fuera de GitHub

Por ejemplo, el repositorio de datos de tu institución, en acceso cerrado, y tu propia copia. Zenodo no sirve para esto, porque lo que se sube ahí es público.

<details>
<summary><b>Si el repo es privado y no hay repositorio institucional:</b> depósito cifrado en OSF</summary>

Se cifra el paquete para dos personas (tú y la institución, o tú y el suplente), que guardan cada una su llave. Hace falta el programa [age](https://github.com/FiloSottile/age).

1. Cada destinatario crea su llave una sola vez con `age-keygen -o llave-age.txt`, la guarda en un lugar seguro y te pasa su parte pública, que empieza con `age1`.
2. Empaca y cifra:

   ```bash
   tar -cf evidencia-hito1.tar evidencia-hito1/
   age -r age1laura… -r age1diego… -o evidencia-hito1.tar.age evidencia-hito1.tar
   sha256sum evidencia-hito1.tar.age
   ```

3. En osf.io, crea un registro nuevo (OSF ya no crea proyectos nuevos, pero sí registros) con un título neutro, como «Paquete de evidencia, hito 1», sube solo el archivo `.age` y elige un embargo de hasta 4 años. Como va cifrado, al terminar el embargo no se revela nada.
4. Agenda una revisión antes de que termine el embargo, para renovarlo o decidir qué hacer.

</details>

### 9. Anota el resguardo en una decisión

En tu agente, con `/decision`: la fecha, dónde quedó el paquete (y, si usaste OSF, el identificador del registro y el fin del embargo) y la huella SHA-256 del archivo que guardaste. Esa decisión queda sellada en la siguiente integración.

## Publicar

Abrir el trabajo es una divulgación: antes de hacerlo, el grupo revisa la lista de apertura que fijó en el acuerdo de autoría. Con un proyecto abierto desde el inicio, el repo ya es público, y se saltan los pasos 4 y 5.

### 1. Revisa la lista de apertura y regístrala

En tu agente, con `/decision`, en este orden:

- si algo puede ser patentable, primero se presenta la solicitud (en México, ante el IMPI), porque publicar es divulgar;
- los datos personales y el dictamen de ética;
- el convenio con el financiador y la titularidad de la institución;
- la política de la revista sobre preprints;
- el acuerdo de todos los autores;
- que todos los commits usen los correos privados (noreply) de GitHub.

### 2. Marca la versión con una etiqueta

Una etiqueta de git señala el estado exacto que se publica. En tu terminal, dentro de la carpeta del proyecto:

```bash
git switch main
git pull
git tag -a v1-preprint -m "Versión enviada a arXiv"
git push origin v1-preprint
```

### 3. Sube el preprint

Usa el PDF que el CI compiló para ese commit: en GitHub, Actions → la corrida de ese commit en `main` → Artifacts. Súbelo a arXiv, medRxiv u otro servidor.

### 4. Crea el espejo público

Un repo nuevo y público con la historia hasta la etiqueta, sin Issues ni comentarios, porque los comentarios de los sellos guardan datos que deben seguir privados. Primero crea en GitHub el repo vacío (New repository, Public, sin README); aquí se llama `ising-diluido-v1`. Después:

```bash
git clone --branch v1-preprint --single-branch https://github.com/labfe/ising-diluido.git espejo
cd espejo
git remote set-url origin https://github.com/labfe/ising-diluido-v1.git
git push origin HEAD:refs/heads/main
git push origin v1-preprint
```

### 5. Pide el DOI en Zenodo

1. En zenodo.org, entra con tu cuenta de GitHub.
2. En tu perfil, abre la sección GitHub y activa el interruptor del espejo.
3. En GitHub, en el espejo, crea una Release con la etiqueta `v1-preprint` (Releases → Draft a new release → elige la etiqueta → Publish release).
4. Zenodo la archiva en unos minutos y le da un DOI. También da un DOI de concepto, que siempre apunta a la versión más reciente.

### 6. Archiva el código en Software Heritage

En archive.softwareheritage.org, abre «Save code now», pega la dirección del espejo y envíala. Queda archivado a largo plazo con un identificador propio.

### 7. Cita el DOI y regístralo

El DOI va en la sección de disponibilidad de datos y código del artículo. Anota con `/decision` la etiqueta, el DOI y la dirección del espejo.
