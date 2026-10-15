# Verificar un registro

[Guía de uso](README.md) › Verificar

Para quien recibe un registro o un paquete de pruebas y quiere comprobarlo por su cuenta: un árbitro, una revista, un comité de ética o alguien del mismo grupo. No hace falta una cuenta de GitHub ni confiar en el grupo, en GitHub o en aucove.

Un **registro** es una carpeta como `integration-<commit>/`: el CI la armó al sellar una entrega o una integración. Un **paquete** es la carpeta que arma el responsable en cada hito, con muchos registros, un `index.txt` y, si el proyecto firma, una carpeta `signature/`.

Cada registro y cada carpeta de firma trae su propio `VERIFY.md`, con los pasos exactos para esa carpeta. Esta página los resume.

## Qué se comprueba

| Pregunta | Cómo se responde |
|---|---|
| ¿Existía en esa fecha? | El sello RFC 3161 de dos autoridades de tiempo (TSA) y la prueba de OpenTimestamps |
| ¿Cambió desde entonces? | La huella SHA-256 de cada archivo |
| ¿Lo emitió el proyecto? | La firma del proyecto, con la lista de llaves que publica el grupo |

La firma prueba que el registro lo emitió quien tiene la llave del proyecto. No prueba quién escribió cada cambio: eso lo cuentan el acuerdo de autoría y la historia de las entregas.

## Con aucove

Es lo más rápido. Instálalo con el extra `evidence` ([Instalación](../../README.md#instalación)) y baja del repo público del grupo, o de su registro en Zenodo, la lista de llaves `keys.yaml`. Después:

```bash
aucove evidence verify integration-3f2a…/ --keys keys.yaml
```

La respuesta da, para cada registro:

- el **veredicto** de la fecha: `verificado` si lo respalda un sello RFC 3161 válido, `pendiente` si solo tiene OpenTimestamps sin confirmar, o `falló` si algo no coincide;
- el **origen**: `del proyecto` si lo firmó una llave del proyecto que valía en la fecha del sello, `desconocido` si hay una firma pero no se pudo cotejar (falta la lista de llaves o la fecha), o `sin firma`.

Con un paquete completo, la misma orden revisa además la firma del índice y que cada archivo coincida con su huella:

```bash
aucove evidence verify evidencia-hito1 --keys keys.yaml
```

Si además tienes una copia del repo (el `repo.bundle` del paquete, o un clon), `--repo <carpeta>` comprueba los commits; con los archivos de una entrega, `--content <carpeta>` comprueba que sean los sellados.

## Sin aucove

Con herramientas comunes: `sha256sum`, `openssl` y `ssh-keygen` (el de Windows 11 y el de Git para Windows sirven). El `VERIFY.md` de cada carpeta trae los comandos con sus valores; estos son los tres pasos.

1. **La huella.** `sha256sum manifest.json` debe dar el `manifest_sha256` de `record.json`.
2. **La firma**, si la carpeta trae `manifest.json.sig`. Con el `allowed_signers` que publica el grupo y OpenSSH 9.1 o posterior:

   ```bash
   ssh-keygen -Y verify -f allowed_signers -I llave-2026 -n aucove-evidence -s manifest.json.sig -O verify-time=20261006120000Z < manifest.json
   ```

   Debe responder `Good "aucove-evidence" signature for llave-2026`. `verify-time` es la fecha del sello RFC 3161 más antiguo, que `VERIFY.md` ya trae escrita: así se comprueba que la llave valía cuando se selló. Con un OpenSSH anterior, `ssh-keygen -Y check-novalidate -n aucove-evidence -s manifest.json.sig < manifest.json` comprueba la firma, y la huella de la llave y sus fechas se comparan a mano con la lista publicada.
3. **El sello.** Con `openssl ts -verify`, como lo indica `VERIFY.md`. En un registro firmado, el sello es de `manifest.json.sig`: la firma solo verifica con esos bytes de `manifest.json`, así que el sello fecha a los dos.

En un paquete, la carpeta `signature/` se verifica igual, con `index.txt` en lugar de `manifest.json`, y después cada línea de `index.txt` con `sha256sum -c index.txt`.

## Si algo no coincide

Un `falló` dice qué comprobación no pasó y por qué. Las causas más comunes:

- el archivo cambió después de sellarlo, o se copió con otro fin de línea: la huella no coincide;
- la firma es de una llave que no está en la lista publicada, o de una que ya no valía en la fecha del sello;
- la carpeta está incompleta: falta un archivo del sello o de la firma. Esa comprobación queda sin hacer y, si no queda otro sello, el registro queda `pendiente`; no da `falló`, porque una copia parcial no es una alteración.

En cualquiera de esos casos, pide al grupo la carpeta original o su paquete del hito.
