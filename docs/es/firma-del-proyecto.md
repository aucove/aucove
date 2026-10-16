# La firma del proyecto

[Guía de uso](README.md) › La firma del proyecto

Los sellos prueban **cuándo** existía cada entrega y que no cambió. La firma del proyecto prueba además **de quién** es el registro: que lo emitió tu proyecto, y no alguien que armó una carpeta parecida. Quien lo revise desde afuera (un árbitro, una revista o un comité) lo comprueba con la llave pública que publica el grupo, sin confiar en GitHub, en aucove ni en ustedes.

Lo prepara una vez quien administra el repo, que casi siempre es el responsable. Toma unos 15 minutos.

> [!NOTE]
> La firma del proyecto está disponible desde la versión 0.1.0rc2.

## Qué se firma y quién guarda la llave

| Tu repo | Modo | Qué se firma | Dónde vive la llave privada |
|---|---|---|---|
| Público, o privado con GitHub Team | `ci` | Cada integración, sola, en el CI; y el índice de cada hito | En tu computadora y en un secreto protegido de GitHub |
| Privado con GitHub Free | `admin` | El índice del paquete de cada hito | Solo en tu computadora |

GitHub Team es gratis para docentes e investigadores con GitHub Education. Las entregas (lo que se sella en cada PR) no se firman en ningún modo, a propósito: su job no recibe secretos. Las cubre la firma del índice del hito.

## 1. Crea la llave

En tu terminal. La llave se crea en tu computadora y nunca se sube al repo:

```bash
ssh-keygen -t ed25519 -C "firma de ising-diluido" -f $HOME/.ssh/aucove-ising-diluido
```

Te pide una frase de paso: elige una buena y guárdala en tu gestor de contraseñas. Quedan dos archivos:

- `aucove-ising-diluido`, la **llave privada**. Es la única que firma: no la compartas ni la subas a ningún lado, y guarda un respaldo cifrado, por ejemplo en tu gestor de contraseñas.
- `aucove-ising-diluido.pub`, la **llave pública**. Solo sirve para verificar, así que esa sí se publica.

La huella identifica la llave en pocas letras:

```text
PS C:\Users\laura> ssh-keygen -lf $HOME/.ssh/aucove-ising-diluido.pub
256 SHA256:8jmj+t4ewK6CHVVPTPUNQTmLHKs6vyRBp9mxvEH7WXY firma de ising-diluido (ED25519)
```

No cargues esta llave en `ssh-agent` en una computadora donde corren agentes de IA: así nada firma sin que tú teclees la frase de paso.

## 2. Agrega la llave pública al proyecto

Va en `project.yaml`. Pídeselo a tu agente en una tarea tuya, por ejemplo la de preparación: «agrega la firma del proyecto en modo ci con esta llave pública», y le pegas el contenido del `.pub`. Queda así:

```yaml
evidence_signing:
  mode: ci
  keys:
    - id: llave-2026
      public_key: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHO4TeimWDqbJtD9DZ0jEQ7osipbPsIQKXvEpl+4J8Ve firma de ising-diluido"
      since: "2026-10-15T00:00:00Z"
```

`id` es un nombre corto para la llave y `since`, desde cuándo vale: hoy, en hora UTC. Con un repo privado en GitHub Free, el modo es `admin`. Al integrarse, el cambio queda sellado como cualquier otro.

## 3. Con el modo `ci`: guarda la llave en GitHub

`init` ya creó el environment `aucove-evidence`, que solo puede usar la rama base. Falta ponerle la llave privada como secreto. El CI no puede teclear la frase de paso, así que el secreto va sin ella: haces una copia temporal, le quitas la frase, la subes y la borras. En Git Bash (Windows) o en tu terminal:

```bash
cp $HOME/.ssh/aucove-ising-diluido llave-ci
ssh-keygen -p -f llave-ci -N ""
```

`ssh-keygen -p` te pide la frase actual. Luego, en GitHub: el repo → Settings → Environments → `aucove-evidence` → Add environment secret, con el nombre `AUCOVE_EVIDENCE_KEY`, y pegas todo el contenido de `llave-ci`. Al terminar, borra la copia:

```bash
rm llave-ci
```

Si `init` te dejó pendiente agregar este secreto, es este paso. La llave nunca va como secreto del repositorio ni de la organización: esos los puede leer un workflow de cualquier rama, y `init` se niega a seguir si la encuentra ahí.

## 4. Comprueba

`aucove doctor` revisa el modo, que una llave valga hoy y, con `ci`, el environment:

```text
[ok] evidence_signing: Firma del proyecto: modo `ci`; llaves vigentes: llave-2026. El environment `aucove-evidence` solo admite la rama `main`.
```

En la siguiente integración, el resumen del job de sellado dice «Firmado por el proyecto con la llave `llave-2026`». Si dice que quedó sin firma, el mismo resumen explica por qué ([Cuando pasa](cuando-pasa.md#si-algo-falla)).

## 5. Publica la llave pública

Para que alguien de afuera verifique sin confiar en ustedes, la lista de llaves vive también fuera del proyecto, en un repo público del grupo archivado en Zenodo. Se hace una vez:

1. En GitHub, crea un repo público, por ejemplo `labfe/llaves-ising-diluido`.
2. Súbele tres archivos:
   - `keys.yaml`, la misma lista que en `project.yaml`:

     ```yaml
     keys:
       - id: llave-2026
         public_key: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHO4TeimWDqbJtD9DZ0jEQ7osipbPsIQKXvEpl+4J8Ve firma de ising-diluido"
         since: "2026-10-15T00:00:00Z"
     ```

   - `allowed_signers`, para verificar solo con `ssh-keygen`. Lleva una línea por llave, con la fecha `since` escrita sin guiones ni dos puntos:

     ```text
     llave-2026 namespaces="aucove-evidence",valid-after="20261015000000Z" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHO4TeimWDqbJtD9DZ0jEQ7osipbPsIQKXvEpl+4J8Ve
     ```

   - `README.md`, con el nombre del grupo y del proyecto, y la huella de cada llave (`SHA256:…`).
3. Archívalo en Zenodo como en [Publicar](publicar-y-resguardar.md#5-pide-el-doi-en-zenodo): activa el repo en tu perfil de Zenodo y crea una Release. El DOI le pone fecha a la lista y la liga a los integrantes del grupo con su ORCID.

Cada vez que cambie la lista, actualiza los tres archivos y crea otra Release.

## 6. En cada hito, firma el índice del paquete

En los dos modos, al armar el paquete de pruebas de un hito, lo firmas tú, con un comando: [Publicar y resguardar, paso 6](publicar-y-resguardar.md#6-firma-el-índice-del-paquete). Esa firma cubre todo lo que el CI no firmó.

## Cambiar la llave, o anular una que te robaron

Una llave nunca se borra de la lista: así, lo que firmó antes se sigue verificando.

- **Para cambiarla** (por ejemplo, cada año o si cambia quien administra): crea una llave nueva (paso 1) y agrégala con `since` de hoy. A la anterior ponle `until` con la misma fecha. Con el modo `ci`, cambia el secreto (paso 3). Al final, actualiza la lista publicada (paso 5).
- **Si te la roban o la pierdes**: ponle `until` con la fecha desde la que ya no es confiable (la del robo, si la sabes), crea una nueva y actualiza todo igual. Lo firmado y sellado antes de esa fecha sigue valiendo, porque el sello prueba que ya existía; lo que alguien firme después con la llave robada no verifica.

```yaml
    - id: llave-2026
      public_key: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHO4TeimWDqbJtD9DZ0jEQ7osipbPsIQKXvEpl+4J8Ve firma de ising-diluido"
      since: "2026-10-15T00:00:00Z"
      until: "2027-10-15T00:00:00Z"
```

Qué prueba la firma, y qué no: prueba que el registro lo emitió quien tiene la llave del proyecto. No prueba quién escribió cada cambio; eso lo cuentan el acuerdo de autoría y la historia de las entregas.
