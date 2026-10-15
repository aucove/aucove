# Cómo verificar la firma de este paquete de evidencia

Esta carpeta, `{{folder}}`, es la **firma del proyecto** sobre el índice del paquete (`index.txt`), con sus sellos de tiempo. La firmó la llave `{{key_id}}`, de huella `{{key}}`. El índice lista {{files}} archivos, y su huella SHA-256 es `{{index_sha256}}`. Para verificarla basta con el paquete: no hace falta acceso al repositorio.

## Qué prueba y qué no

- **Prueba** que quien tiene la llave privada del proyecto firmó este índice, y que el índice y cada archivo que lista ya existían en la fecha de los sellos y no cambiaron después. La fecha verificable la dan los sellos de tiempo RFC 3161 y Bitcoin, por medio de OpenTimestamps.
- **No prueba** quién escribió cada cambio ni quién es el titular del contenido. La autoría se apoya en el acuerdo de autoría, en la declaración CRediT y en los registros de la forja.
- Compara la huella de la llave con la que publica el grupo en su repositorio público o en su registro de Zenodo: solo así sabes que la llave es la del proyecto.

## Qué hay en la carpeta

| Archivo | Qué es |
|---|---|
| `signature.json` | El índice de la firma: la huella de `index.txt`, la llave y las pruebas que se pidieron. |
| `index.txt.sig` | La firma de OpenSSH (SSHSIG) de `../index.txt`. Es lo que sellaron las pruebas. |
| `index.txt.sig.ots` | La prueba de OpenTimestamps, si se obtuvo. |
| `tsa/<id>.tsr` | Cada sello RFC 3161, tal como lo devolvió la autoridad de sellado de tiempo (TSA). |
| `tsa/<id>.chain.pem` y `tsa/<id>.root.pem` | La cadena de certificados de la TSA y la raíz con la que se comprobó al sellar. |
| `tsa/<id>.ocsp.der` o `tsa/<id>.crl.der` | El estado de revocación del certificado de la TSA, capturado al sellar. |

## 1. Con un solo comando

Con [uv](https://docs.astral.sh/uv/) instalado, desde la carpeta del paquete (la que tiene `index.txt`):

```sh
uvx --from "{{name}}[evidence]=={{version}}" {{name}} evidence verify . --keys <lista>
```

`<lista>` es la lista de llaves que publica el grupo (un archivo YAML con la clave `keys`). La respuesta verifica cada registro del paquete y esta firma, y da el origen de cada registro: `project` si lo cubre una firma del proyecto que valía en la fecha del sello.

## 2. Los archivos del paquete, con sha256sum

Desde la carpeta del paquete:

```sh
sha256sum -c index.txt
```

Cada línea debe terminar en `OK`. En macOS, `shasum -a 256 -c index.txt` hace lo mismo. Una línea `FAILED` es un archivo que cambió después de armarse el índice.

## 3. La firma, con ssh-keygen

Desde la carpeta del paquete, con el archivo `allowed_signers` que publica el grupo y OpenSSH 9.1 o posterior (el `ssh-keygen` de Windows 11 y el de Git para Windows lo son):

```sh
ssh-keygen -Y verify -f allowed_signers -I {{key_id}} -n {{namespace}} -s {{folder}}/index.txt.sig -O verify-time={{verify_time}} < index.txt
```

Debe responder `Good "{{namespace}}" signature for {{key_id}}`. `verify-time` es la fecha verificable del sello RFC 3161 más antiguo: así `ssh-keygen` comprueba que la llave valía cuando se firmó, aunque hoy ya se haya rotado o revocado. Sin ese archivo, este comando comprueba la firma y muestra la huella de la llave, que comparas a mano con la publicada:

```sh
ssh-keygen -Y check-novalidate -n {{namespace}} -s {{folder}}/index.txt.sig < index.txt
```

## 4. Los sellos RFC 3161, con OpenSSL

<!-- tsa -->
Esta firma trae estos sellos:

| TSA | Fecha verificable (UTC) | `-attime` | `sha256sum` de la raíz | Huella de la raíz (`openssl x509`) | Dónde publica la TSA su raíz |
|---|---|---|---|---|---|
{{tsa_table}}

Desde esta carpeta, con OpenSSL 1.1 o posterior: compara una de las dos huellas de la raíz con la que publica la TSA, y el último comando de cada TSA debe responder `Verification: OK`.

```sh
{{tsa_commands}}
```
<!-- /tsa -->
<!-- no-tsa -->
Esta firma no trae sellos RFC 3161: ninguna TSA respondió al firmar. `signature.json` dice qué pruebas se pidieron. Sin un sello RFC 3161, `evidence verify` deja la firma en `pending`.
<!-- /no-tsa -->

## 5. La prueba de OpenTimestamps

Desde esta carpeta, con el cliente oficial (`pip install opentimestamps-client`):

```sh
ots --no-bitcoin verify index.txt.sig.ots
ots info index.txt.sig.ots
```

La prueba queda pendiente hasta que Bitcoin incluye su bloque, lo que toma unas horas; `ots upgrade index.txt.sig.ots` la completa. Una vez completa, compara la altura y la raíz de Merkle que muestra `ots info` con las de ese bloque en un explorador de Bitcoin o en un nodo propio.
