# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Evidencia verificable de fecha (11-trazabilidad §8; D-45).

Manifiesto canónico, compromisos de archivos y actores, aperturas, registros,
catálogo de TSA y clientes RFC 3161 y OpenTimestamps. Solo importa ``domain``,
``vcs`` e ``i18n``; las dependencias del extra ``evidence`` se importan de forma
perezosa y nunca en la ruta caliente (01-arquitectura §3.1, REQ-ARCH-99).

Módulos (este paquete no reexporta nada: cada quien importa el módulo que usa):

- ``canon``: JSON canónico, perfil restringido de RFC 8785 (11 §8.3).
- ``openings``: compromisos ``p``, ``c`` y ``a`` con sal y aperturas privadas.
- ``manifest``: constructores ``submission`` e ``integration`` sobre objetos de git.
- ``record``: carpeta ``<kind>-<commit>/`` y JSON del comentario ``ci:evidence``.
- ``timefmt``: horas en UTC y en ``America/Mexico_City``.
"""
