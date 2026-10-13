# Datos del proyecto

Los datos crudos nunca se versionan.
Esta tabla solo describe cada conjunto de datos y dice dónde está y cómo pedir acceso.
Cada persona descarga el conjunto `<id>` en `data/<id>/`, que git ignora.

| id | description | location | access | classification | version |
|---|---|---|---|---|---|

Cómo llenar cada fila (los encabezados de la tabla no se traducen):

- `id`: identificador único en minúsculas con guiones, por ejemplo `school-survey`.
- `description`: qué contiene el conjunto, en una línea.
- `location`: URL o ubicación del original, sin credenciales ni enlaces firmados.
- `access`: cómo obtenerlo, por ejemplo «público» o «pedir acceso al responsable».
- `classification`: `public`, `internal`, `restricted` o `local_only`.
- `version`: versión o ronda, y de preferencia su suma de verificación (`sha256:…`).
