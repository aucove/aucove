# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Serialización de los marcadores de tarea en comentarios (00-marco §5.14; 04-tareas §1.4).

Forma con JSON: la primera línea ``<!-- <NAME>:<tipo> v1``, el objeto JSON en la siguiente y
``-->`` en la tercera. Dentro del JSON, cada ``--`` va como ``\\u002d\\u002d`` (REQ-TASK-11),
así el comentario nunca contiene ``--``, ``-->`` ni ``--!>`` y ``json.loads`` lo decodifica
igual. El análisis es el de ``ops.snapshot.parse_markers``; este módulo solo escribe.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Final

from .. import _meta

__all__ = ["MARKER_KINDS", "render_marker", "replace_marker"]

MARKER_KINDS: Final[frozenset[str]] = frozenset(
    {"claim", "release", "submission", "assessment", "expiry", "escalation"}
)


def render_marker(kind: str, data: Mapping[str, object]) -> str:
    """Marcador ``kind`` v1 con ``data`` (sin salto de línea final)."""
    if kind not in MARKER_KINDS:
        raise ValueError(f"tipo de marcador desconocido: {kind!r}")
    payload = json.dumps(dict(data), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    payload = payload.replace("--", "\\u002d\\u002d")
    return f"<!-- {_meta.NAME}:{kind} v1\n{payload}\n-->"


def _block(kind: str) -> re.Pattern[str]:
    return re.compile(
        r"<!-- " + re.escape(_meta.NAME) + ":" + re.escape(kind) + r" v1[^\n]*\n[^\n]*\n[ \t]*-->"
    )


def replace_marker(body: str, kind: str, data: Mapping[str, object]) -> str:
    """``body`` con el marcador ``kind`` reemplazado por uno nuevo, o agregado al principio.

    Lo usan los comentarios con estado que el núcleo edita en su sitio (REQ-TASK-13): cada
    tipo aparece una sola vez en el comentario. Conserva el resto del texto.
    """
    marker = render_marker(kind, data)
    pattern = _block(kind)
    if pattern.search(body):
        return pattern.sub(lambda _m: marker, body, count=1)
    return marker + ("\n" + body if body else "")
