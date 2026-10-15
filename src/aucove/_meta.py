# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Nombre provisional y metadatos del núcleo (01-arquitectura §3.2, D-03).

Es el ÚNICO módulo de ``src/`` con el nombre literal (REQ-ARCH-19): todo lo
demás deriva de ``NAME``. No importa nada, para que cualquier capa, incluida
la ruta caliente de hooks, pueda usarlo sin costo.
"""

NAME = "aucove"
__version__ = "0.1.0rc2"  # hatch lee este valor ([tool.hatch.version] path); REQ-ARCH-20
SUPPORTED_CONVENTIONS: tuple[str, ...] = ("0.1",)
MCP_SERVER_NAME = NAME  # Claude Code: mcp__<NAME>__<tool>
CACHE_DIR = "." + NAME  # directorio de caché local (01 §7)
ENV_PREFIX = NAME.upper() + "_"  # prefijo de las variables de entorno
SCHEMA_BASE = f"https://{NAME}.invalid/schemas"
TEMPLATE_REPO = NAME + "-template"
RESULT_SCHEMA = f"{NAME}.result/v1"  # const de result.schema.json (05 §3.1)
CLAIM_SUBJECT = f"{NAME}: claim #"  # asunto del commit de reclamo (04 §1.5)

# Caracteres admitidos en el tipo de un marcador de CI (00-marco §5.14).
_MARKER_KIND_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


def ci_marker(kind: str) -> str:
    """Marcador del comentario único del CI (09-ci REQ-CI-20, REQ-CI-27, REQ-CI-32).

    ``kind`` es, por ejemplo, ``build`` o ``validate``; debe cumplir
    ``[a-z0-9-]+`` y empezar por letra (gramática de 00-marco §5.14).
    """
    if not kind or not kind[0].isalpha() or not set(kind) <= _MARKER_KIND_CHARS:
        raise ValueError(f"tipo de marcador inválido: {kind!r}")
    return f"<!-- {NAME}:ci:{kind} v1 -->"
