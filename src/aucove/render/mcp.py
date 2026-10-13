# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``.mcp.json`` del proyecto (01-arquitectura §10.3; 02-convencion §8.6; 05-operaciones §13).

Declara solo el servidor del núcleo, con ``<nombre> mcp --stdio`` (10-seguridad SB04). Lo
agrega el render de la plantilla junto a los generados y ``init`` lo crea si falta; es de clase
``lead``, así que después lo cambia solo el responsable. Desde H4, ``.gemini/settings.json`` y
``.codex/config.toml`` salen de la misma definición (``server``). ``mcp_problem`` dice si un
``.mcp.json`` cumple SB04; lo usa el check ``mcp_config`` de ``doctor``.
"""

from __future__ import annotations

import json
from typing import Final, Literal

from .. import _meta

__all__ = ["MCP_JSON_PATH", "McpProblem", "mcp_json", "mcp_problem", "server"]

MCP_JSON_PATH: Final = ".mcp.json"
McpProblem = Literal["invalid", "servers", "command"]


def server() -> dict[str, object]:
    """La definición del servidor MCP del núcleo (forma exec, sin shell: 07-hooks)."""
    return {"command": _meta.NAME, "args": ["mcp", "--stdio"]}


def mcp_json() -> bytes:
    """Contenido de ``.mcp.json``: JSON con sangría de 2, UTF-8, LF y un salto final (REQ-ARCH-82)."""
    return (json.dumps({"mcpServers": {_meta.MCP_SERVER_NAME: server()}}, indent=2) + "\n").encode("utf-8")


def mcp_problem(raw: bytes) -> McpProblem | None:
    """Por qué ``raw`` no cumple 10-seguridad SB04, o ``None`` si lo cumple.

    - ``invalid``: no es JSON en UTF-8 con un objeto ``mcpServers``.
    - ``servers``: declara otro servidor además del núcleo, o no declara el del núcleo.
    - ``command``: el del núcleo no es ``<nombre> mcp --stdio`` en forma exec. Fuera de
      ``command`` y ``args`` solo se admite ``"type": "stdio"``: ``env``, ``cwd`` o una ``url``
      cambiarían qué se ejecuta.
    """
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "invalid"
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return "invalid"
    if set(servers) != {_meta.MCP_SERVER_NAME}:
        return "servers"
    entry = servers[_meta.MCP_SERVER_NAME]
    if not isinstance(entry, dict) or entry.get("type", "stdio") != "stdio":
        return "command"
    return None if {k: v for k, v in entry.items() if k != "type"} == server() else "command"
