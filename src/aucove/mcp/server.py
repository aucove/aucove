# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove mcp --stdio``: el servidor MCP (05-operaciones §11).

Es el único módulo que importa el SDK oficial ``mcp`` v2 (01 REQ-ARCH-16); las herramientas
salen de ``tools``, que no lo importa. Qué pone el SDK y qué pone este módulo:

- **Eras (REQ-OPS-52).** ``Server.run`` es de doble era y la primera petición decide: con el
  sobre ``_meta`` de 2026-07-28, la moderna (``server/discover`` y la versión en cada
  petición); con ``initialize``, la heredada (2025-11-25 y anteriores) durante todo el
  proceso. Con una versión no admitida, el SDK responde ``-32022`` y lista solo las
  modernas; ``_LegacyInSupported`` agrega 2025-11-25, como pide REQ-OPS-52.
- **Capacidades (REQ-OPS-53).** Solo se registran ``tools/list`` y ``tools/call``: el SDK
  declara ``tools`` con ``listChanged: false`` y nada más. Se quita la middleware de
  OpenTelemetry que el SDK trae por defecto, porque el núcleo no tiene telemetría.
- **stdout (REQ-OPS-54).** Mientras sirve, el transporte stdio del SDK apunta el fd 1 a
  stderr y el fd 0 al dispositivo nulo: solo el transporte escribe JSON-RPC en stdout. Con
  EOF en stdin, ``run`` termina.
- **Cancelación (REQ-OPS-55).** El SDK nunca responde a una petición cancelada. La operación
  corre en un hilo con ``Invocation.cancel``, que se activa al cancelar: una operación de
  varios pasos se detiene en el borde del siguiente paso.
- **Resultados (REQ-OPS-56 a 58).** ``tools/list`` y ``server/discover`` llevan ``ttlMs`` y
  ``cacheScope: private``; ``tools/call`` devuelve el sobre en ``structuredContent`` y en
  ``content`` (JSON compacto), con ``isError = not ok``, y en la era moderna el SDK agrega
  ``resultType: "complete"``. Una herramienta desconocida es el error ``-32602``.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import os
import sys
import threading
from collections.abc import Mapping, Sequence
from typing import Any

from .. import _meta
from ..i18n import Translator, translator
from ..ops.context import Invocation, OpContext
from ..ops.registry import REGISTRY, OpSpec
from .tools import call_tool, descriptor, instructions, mcp_specs

__all__ = ["LIST_TTL_MS", "SUPPORTED_VERSIONS", "build_server", "run", "serve", "server_translator"]

SUPPORTED_VERSIONS = ("2026-07-28", "2025-11-25")  # REQ-OPS-52
LIST_TTL_MS = 3_600_000  # REQ-OPS-56
EXIT_USAGE = 3  # REQ-OPS-15


def _say(text: str) -> None:
    try:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()
    except (OSError, UnicodeError, ValueError):
        pass


def server_translator(env: Mapping[str, str], cwd: str) -> Translator:
    """Idioma del servidor: el de ``<PREFIJO>LANG`` o el del proyecto de ``cwd`` (REQ-OPS-60)."""
    ctx = OpContext(Invocation(root=None, lang=None, json=True, dry_run=False, surface="mcp", env=dict(env), cwd=cwd))
    try:
        return ctx.t()
    except Exception:  # noqa: BLE001 - fuera de un proyecto o sin base legible
        return translator("en")
    finally:
        ctx.close()


def build_server(
    *,
    env: Mapping[str, str],
    cwd: str,
    t: Translator | None = None,
    registry: Sequence[OpSpec] = REGISTRY,
) -> Any:
    """``mcp.server.Server`` con las herramientas del registro (05 §11)."""
    import anyio
    import mcp_types as types
    from mcp.server import CacheHint, Server
    from mcp.shared.exceptions import MCPError
    from mcp_types.jsonrpc import INVALID_PARAMS

    lang = t or server_translator(env, cwd)
    specs = {spec.mcp_tool: spec for spec in mcp_specs(registry)}
    listed: list[Any] = []

    async def list_tools(ctx: Any, params: Any) -> Any:
        if not listed:  # se arma la primera vez: los outputSchema no se necesitan antes
            listed.extend(types.Tool.model_validate(descriptor(spec, lang)) for spec in specs.values())
        return types.ListToolsResult(tools=list(listed))

    async def call(ctx: Any, params: Any) -> Any:
        spec = specs.get(params.name)
        if spec is None:
            raise MCPError(code=INVALID_PARAMS, message=f"Unknown tool: {params.name}")
        cancel = threading.Event()
        work = functools.partial(call_tool, spec, params.arguments, env=env, cwd=cwd, cancel=cancel)
        try:
            result = await anyio.to_thread.run_sync(work, abandon_on_cancel=True)
        except anyio.get_cancelled_exc_class():
            cancel.set()  # la operación sigue en su hilo hasta el borde del siguiente paso
            raise
        envelope = result.to_dict()
        text = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            structured_content=envelope,
            is_error=not result.ok,
        )

    hint = CacheHint(ttl_ms=LIST_TTL_MS, scope="private")
    server = Server(
        _meta.MCP_SERVER_NAME,
        version=_meta.__version__,
        instructions=instructions(lang),
        cache_hints={"tools/list": hint, "server/discover": hint},
        on_list_tools=list_tools,
        on_call_tool=call,
    )
    server.middleware = []  # sin la middleware de OpenTelemetry: no hay telemetría
    return server


def _with_legacy(item: Any) -> Any:
    """Agrega 2025-11-25 a ``data.supported`` de un error ``-32022`` (REQ-OPS-52)."""
    import mcp_types as types
    from mcp.shared.message import SessionMessage
    from mcp_types.jsonrpc import UNSUPPORTED_PROTOCOL_VERSION

    if not isinstance(item, SessionMessage):
        return item
    message = item.message
    if not isinstance(message, types.JSONRPCError) or message.error.code != UNSUPPORTED_PROTOCOL_VERSION:
        return item
    data = message.error.data
    if hasattr(data, "model_dump"):
        data = data.model_dump(by_alias=True, mode="json", exclude_none=True)
    if not isinstance(data, Mapping) or not isinstance(data.get("supported"), list):
        return item
    supported = list(dict.fromkeys([*data["supported"], *SUPPORTED_VERSIONS]))
    error = message.error.model_copy(update={"data": {**data, "supported": supported}})
    return dataclasses.replace(item, message=message.model_copy(update={"error": error}))


class _LegacyInSupported:
    """Flujo de escritura del servidor que pasa cada mensaje por ``_with_legacy``."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    async def send(self, item: Any, /) -> None:
        await self._inner.send(_with_legacy(item))

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def __aenter__(self) -> _LegacyInSupported:
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> bool | None:
        return await self._inner.__aexit__(*exc)  # type: ignore[no-any-return]


async def serve(server: Any, read_stream: Any, write_stream: Any) -> None:
    """Sirve una conexión, en la era que elija el cliente, hasta que se cierre la lectura."""
    options = server.create_initialization_options()
    # El SDK declara experimental: {}; REQ-OPS-53 pide declarar solo tools.
    options.capabilities.experimental = None
    await server.run(read_stream, _LegacyInSupported(write_stream), options)


async def _serve_stdio(server: Any) -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await serve(server, read_stream, write_stream)


def run(argv: Sequence[str]) -> int:
    """``mcp --stdio``; sin ``--stdio`` es un error de uso (salida 3, REQ-OPS-15)."""
    if list(argv) != ["--stdio"]:
        _say(f"uso: {_meta.NAME} mcp --stdio / usage: {_meta.NAME} mcp --stdio")
        return EXIT_USAGE
    import anyio

    server = build_server(env=dict(os.environ), cwd=os.getcwd())
    anyio.run(_serve_stdio, server)
    return 0
