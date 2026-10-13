# Cómo verificar este registro de evidencia

Esta carpeta es una **prueba de existencia en una fecha** del commit `{{commit}}`. Es el registro `{{kind}}` (`submission`: la cabeza de un PR; `integration`: un push a la rama base), intento {{attempt}}, del artefacto `{{artifact}}`. Para verificarlo basta con esta carpeta: no hace falta acceso al repositorio.

## Qué prueba y qué no

- **Prueba** que el contenido descrito en `manifest.json` ya existía cuando se pidieron los sellos y que no cambió después. La fecha verificable la dan los sellos de tiempo RFC 3161 y Bitcoin, por medio de OpenTimestamps; no la da el reloj de quien selló.
- **No prueba** quién es el autor ni quién es el titular del contenido. La autoría se apoya en el acuerdo de autoría, en la declaración CRediT y en los registros de la forja.
- `manifest.json` no contiene archivos, rutas ni nombres, solo huellas con sal (compromisos). `openings.json` guarda las sales, las rutas y las identidades que abren esos compromisos. Es **privado**: compártelo solo con quien deba verificar.

## Qué hay en la carpeta

| Archivo | Qué es |
|---|---|
| `manifest.json` | Lo que se selló, en sus bytes exactos. Su huella SHA-256 es `{{manifest_sha256}}`. |
| `record.json` | El índice del registro: qué pruebas se pidieron y cuáles se obtuvieron. |
| `openings.json` | Las aperturas privadas de los compromisos. |
| `manifest.json.ots` | La prueba de OpenTimestamps, si se obtuvo. |
| `tsa/<id>.tsr` | Cada sello RFC 3161, tal como lo devolvió la autoridad de sellado de tiempo (TSA). |
| `tsa/<id>.chain.pem` y `tsa/<id>.root.pem` | La cadena de certificados de la TSA y la raíz con la que se comprobó al sellar. |
| `tsa/<id>.ocsp.der` o `tsa/<id>.crl.der` | El estado de revocación del certificado de la TSA, capturado al sellar. |
| `open_commitments.py` | Un programa corto para abrir los compromisos con Python 3.8 o posterior. |

## 1. Con un solo comando

Con [uv](https://docs.astral.sh/uv/) instalado, desde esta carpeta:

```sh
uvx --from "{{name}}[evidence]=={{version}}" {{name}} evidence verify .
```

Es la versión del núcleo que selló este registro. La respuesta es `verified` (verificado: lo respalda un sello RFC 3161 válido), `pending` (pendiente) o `failed` (algo no coincide), con las horas en UTC y en la hora del centro de México. Un registro con solo OpenTimestamps queda en `pending` aunque su prueba ya esté completa: esta versión no coteja el bloque de Bitcoin, así que hazlo a mano con las órdenes del punto 3. Con red, consulta los calendarios de OpenTimestamps para completar en memoria una prueba pendiente; `--upgrade` además la guarda en `manifest.json.ots`, y `--offline` no usa la red.

## 2. Los sellos RFC 3161, con OpenSSL

<!-- tsa -->
Este registro trae estos sellos:

| TSA | Fecha verificable (UTC) | `-attime` | `sha256sum` de la raíz | Huella de la raíz (`openssl x509`) | Dónde publica la TSA su raíz |
|---|---|---|---|---|---|
{{tsa_table}}

Para cada TSA, con OpenSSL 1.1 o posterior (Git para Windows lo trae):

1. Comprueba que la raíz es la de la TSA. Los dos primeros comandos calculan los dos valores de la tabla; compara uno de ellos con la huella que publica la TSA en su página (la última columna). En macOS, `shasum -a 256` hace lo mismo que `sha256sum`.
2. Comprueba el sello. El tercer comando debe responder `Verification: OK`. `-attime` es la fecha verificable del sello en segundos desde 1970: así OpenSSL acepta un certificado que hoy ya venció pero que era válido al sellar.

```sh
{{tsa_commands}}
```

`evidence verify` comprueba además el estado de revocación guardado en `tsa/<id>.ocsp.der` o `tsa/<id>.crl.der`.
<!-- /tsa -->
<!-- no-tsa -->
Este registro no trae sellos RFC 3161. El sello de una entrega usa solo OpenTimestamps; el de una integración pide dos TSA y OpenTimestamps. `record.json` dice qué pruebas se pidieron y por qué faltó alguna.
<!-- /no-tsa -->

## 3. La prueba de OpenTimestamps

Con el cliente oficial (`pip install opentimestamps-client`):

```sh
ots --no-bitcoin verify manifest.json.ots
ots info manifest.json.ots
```

Al sellar, la prueba queda pendiente hasta que Bitcoin incluye su bloque, lo que toma unas horas. `ots upgrade manifest.json.ots` la completa cuando los calendarios ya la tienen. Una vez completa, `ots info` muestra la altura del bloque y su raíz de Merkle: compárala con la de ese bloque en un explorador de Bitcoin o en un nodo propio. Ese cotejo es el que da la fecha: `evidence verify` muestra la misma altura y la misma raíz, pero no las coteja, porque cualquiera puede escribir una atestación de Bitcoin falsa; por eso deja el registro en `pending`. Si la carpeta no trae `manifest.json.ots`, esa prueba no se obtuvo, y `record.json` dice por qué.

## 4. Abrir los compromisos sin el núcleo

```sh
python open_commitments.py manifest.json openings.json
python open_commitments.py manifest.json openings.json <carpeta>
```

La primera forma comprueba que `openings.json` es de este manifiesto y que cada apertura reproduce su compromiso. La segunda compara además cada archivo con el contenido sellado. `<carpeta>` es una copia de los archivos del commit `{{commit}}` con sus bytes de git, sin convertir los fines de línea; por ejemplo, la que deja `git -c core.autocrlf=false clone` seguido de `git checkout {{commit}}`. Cada línea dice `ok`, `different` (el archivo no es el sellado), `missing` (falta el archivo) o `not-in-manifest` (la apertura no es de este manifiesto). Una ruta absoluta o con `..` da `different` sin leerse, y lo que no se puede imprimir de una ruta se muestra escapado. El programa termina con 0 si todo coincide.

Las fórmulas, con `‖` como concatenación de bytes y SHA-256 en hexadecimal:

- ruta: `p = SHA-256("evidence.path.v1" ‖ 0x00 ‖ sal ‖ ruta)`, con la ruta en los bytes exactos que da git;
- contenido: `c = SHA-256("evidence.blob.v1" ‖ 0x00 ‖ sal ‖ contenido)`, con los bytes del archivo en git; `c` es nulo si el archivo se borró;
- actor: `a = SHA-256("evidence.actor.v1" ‖ 0x00 ‖ sal ‖ rol ‖ 0x00 ‖ valor)`, con el rol `pr_author`, `merged_by`, `author` o `committer`.

La huella de `manifest.json` (`sha256sum manifest.json`) debe ser `{{manifest_sha256}}`: la misma de `record.json` y la que sellaron las pruebas.
