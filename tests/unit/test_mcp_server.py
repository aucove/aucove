# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Servidor MCP por stdio (05-operaciones §11; REQ-OPS-52 a REQ-OPS-61).

Las conversaciones van por el transporte en memoria del SDK con mensajes JSON-RPC crudos, en
las dos eras: la heredada (``initialize`` de 2025-11-25) y la moderna (el sobre ``_meta`` de
2026-07-28). Una prueba arranca además el proceso real (``aucove mcp --stdio``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import types
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import anyio
import jsonschema
import mcp_types as mt
import pytest
from mcp.shared.memory import create_client_server_memory_streams
from mcp.shared.message import SessionMessage

from aucove import _meta, entry
from aucove.config.schemas import schema, validate_result
from aucove.i18n import translator
from aucove.localstore.store import Store
from aucove.mcp import server as mcp_server
from aucove.mcp.tools import ANNOTATIONS, DESCRIPTION_MAX, INSTRUCTIONS_MAX, descriptor, mcp_specs, output_schema
from aucove.ops.context import OpContext
from aucove.ops.registry import REGISTRY, OpSpec
from aucove.ops.result import Result, make_error, ok
from tests.board_world import new_world
from tests.conftest import REPO_ROOT

P = _meta.ENV_PREFIX
VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
MODERN = {VERSION_KEY: "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}}
INIT = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "pruebas", "version": "1"}}
SPEC_TOOLS = [  # 05 §11.4, en el orden de 00-marco §5.7
    "check_environment", "validate_project", "get_status", "get_context", "check_path", "propose_task", "claim_task",
    "release_task", "save_work", "update_branch", "switch_task", "record_overlap_assessment", "submit_task",
    "propose_decision", "propose_direction", "list_reviews", "prepare_review",
]
TOOLS = SPEC_TOOLS  # REQ-OPS-50: desde el paso 5a de H3 están las 17


def _message(m: Mapping[str, Any]) -> Any:
    fields = {k: v for k, v in m.items() if k in ("id", "method", "params")}
    if "id" in m:
        return mt.JSONRPCRequest(jsonrpc="2.0", **fields)
    return mt.JSONRPCNotification(jsonrpc="2.0", **fields)


def converse(server: Any, messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Envía los mensajes en orden; tras cada petición espera su respuesta."""

    async def main() -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        async with create_client_server_memory_streams() as (client, served):
            c_read, c_write = client
            async with anyio.create_task_group() as tg:
                tg.start_soon(mcp_server.serve, server, *served)
                for m in messages:
                    await c_write.send(SessionMessage(message=_message(m)))
                    if "id" in m:
                        with anyio.fail_after(120):
                            reply = await c_read.receive()
                        out.append(reply.message.model_dump(by_alias=True, exclude_none=True, mode="json"))
                await c_write.aclose()
        return out

    return anyio.run(main)


@pytest.fixture(scope="module")
def server_es() -> Any:
    return mcp_server.build_server(env={}, cwd=str(REPO_ROOT), t=translator("es"))


def test_legacy_era(server_es: Any) -> None:
    init, listed, bad, unknown = converse(server_es, [
        {"id": 1, "method": "initialize", "params": INIT},
        {"method": "notifications/initialized"},
        {"id": 2, "method": "tools/list"},
        {"id": 3, "method": "tools/call", "params": {"name": "check_path", "arguments": {"paths": ["x"], "otra": 1}}},
        {"id": 4, "method": "tools/call", "params": {"name": "no_existe", "arguments": {}}},
    ])
    result = init["result"]
    assert result["protocolVersion"] == "2025-11-25"
    assert result["capabilities"] == {"tools": {"listChanged": False}}  # REQ-OPS-53
    assert result["serverInfo"] == {"name": _meta.MCP_SERVER_NAME, "version": _meta.__version__}
    assert result["instructions"] == translator("es")("mcp.instructions")
    tools = listed["result"]["tools"]
    assert [t["name"] for t in tools] == TOOLS
    assert "ttlMs" not in listed["result"]  # solo en la era moderna
    # REQ-OPS-58: argumentos inválidos son un sobre con isError, no un error de protocolo.
    call = bad["result"]
    assert call["isError"] is True and "resultType" not in call
    envelope = call["structuredContent"]
    assert envelope["error"]["code"] == "E_VALIDATION_INPUT"
    assert envelope["error"]["details"] == {"errors": [{"param": "otra", "reason": "unknown"}]}
    assert json.loads(call["content"][0]["text"]) == envelope and call["content"][0]["type"] == "text"
    assert validate_result(envelope) == []
    assert unknown["error"]["code"] == -32602  # solo una herramienta desconocida


def test_modern_era(server_es: Any) -> None:
    discover, listed, called, future = converse(server_es, [
        {"id": 1, "method": "server/discover", "params": {"_meta": MODERN}},
        {"id": 2, "method": "tools/list", "params": {"_meta": MODERN}},
        {"id": 3, "method": "tools/call", "params": {"_meta": MODERN, "name": "check_environment",
                                                      "arguments": {"refresh": "no"}}},
        {"id": 4, "method": "tools/list", "params": {"_meta": {**MODERN, VERSION_KEY: "2099-01-01"}}},
    ])
    found = discover["result"]
    assert found["supportedVersions"] == ["2026-07-28"]
    assert found["capabilities"] == {"tools": {"listChanged": False}}
    assert found["instructions"] == translator("es")("mcp.instructions")
    assert (found["ttlMs"], found["cacheScope"]) == (3600000, "private")  # REQ-OPS-56
    assert (listed["result"]["ttlMs"], listed["result"]["cacheScope"]) == (3600000, "private")
    assert [t["name"] for t in listed["result"]["tools"]] == TOOLS
    assert called["result"]["resultType"] == "complete" and called["result"]["isError"] is True
    error = future["error"]
    assert error["code"] == -32022
    assert error["data"] == {"supported": ["2026-07-28", "2025-11-25"], "requested": "2099-01-01"}


def test_initialize_on_a_modern_connection_names_both_eras(server_es: Any) -> None:
    _discover, init = converse(server_es, [
        {"id": 1, "method": "server/discover", "params": {"_meta": MODERN}},
        {"id": 2, "method": "initialize", "params": INIT},
    ])
    assert init["error"]["code"] == -32022
    assert init["error"]["data"]["supported"] == ["2026-07-28", "2025-11-25"]


# -- herramientas (REQ-OPS-56, REQ-OPS-57 y REQ-OPS-59) -----------------------------------------


def test_every_spec_tool_has_its_annotations() -> None:
    assert set(ANNOTATIONS) == set(SPEC_TOOLS)
    for name in SPEC_TOOLS[:5] + ["list_reviews", "prepare_review"]:
        assert ANNOTATIONS[name] == (True, False, True, False), name
    for name in ("claim_task", "release_task", "save_work", "update_branch", "submit_task"):
        assert ANNOTATIONS[name][:2] == (False, True), name
    assert not any(a[3] for a in ANNOTATIONS.values())  # openWorldHint: false en todas


@pytest.mark.parametrize("lang", ["es", "en"])
def test_descriptors(lang: str) -> None:
    t = translator(lang)
    specs = mcp_specs()
    assert [s.mcp_tool for s in specs] == TOOLS
    for spec in specs:
        d = descriptor(spec, t)
        assert list(d) == ["name", "title", "description", "inputSchema", "outputSchema", "annotations"]
        assert d["title"] and d["title"] != d["name"] and 0 < len(d["description"]) <= DESCRIPTION_MAX
        assert d["annotations"] == dict(zip(("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"),
                                            ANNOTATIONS[d["name"]]))
        jsonschema.Draft202012Validator.check_schema(d["outputSchema"])
    other = translator("en" if lang == "es" else "es")
    assert descriptor(specs[0], t)["title"] != descriptor(specs[0], other)["title"]


@pytest.mark.parametrize("lang", ["es", "en"])
def test_submit_task_carries_the_whole_attestation(lang: str) -> None:
    """11 REQ-TRACE-23: sin los skills de entrega, el agente solo tiene la descripción para mostrar attest-v1.

    En una sesión de prueba (20/10/2026, 0.1.0rc4), la descripción nombraba attest-v1 sin su texto.
    """
    t = translator(lang)
    [spec] = [s for s in mcp_specs() if s.mcp_tool == "submit_task"]
    description = descriptor(spec, t)["description"]
    assert description == t("mcp.tool.submit_task.description")  # cabe en DESCRIPTION_MAX, sin recorte
    assert t("cli.attest.text") in description and "sign_off" in description


def _refs(node: object) -> list[str]:
    if isinstance(node, dict):
        found = [node["$ref"]] if isinstance(node.get("$ref"), str) else []
        return found + [r for v in node.values() for r in _refs(v)]
    if isinstance(node, list):
        return [r for v in node for r in _refs(v)]
    return []


@pytest.mark.parametrize("op_id", ["doctor", "validate", "status", "context", "check-path", "task.propose",
                                   "claim"])
def test_output_schema_is_self_contained(op_id: str) -> None:
    bundled = output_schema(op_id)
    assert "$id" not in bundled and bundled["properties"]["op"] == {"const": op_id}
    refs = _refs(bundled)
    assert all(r.startswith("#/$defs/") for r in refs)
    assert {r[len("#/$defs/"):] for r in refs} <= set(bundled["$defs"])
    assert len(json.dumps(bundled)) < len(json.dumps(schema("result"))) / 3
    validator = jsonschema.Draft202012Validator(bundled)
    error = make_error("E_VALIDATION_INPUT", translator("es"), details={"errors": []})
    failed = Result(op=op_id, ok=False, data=None, error=error).to_dict()
    assert list(validator.iter_errors(failed)) == []
    assert list(validator.iter_errors({**failed, "op": "otra"}))


def test_instructions_are_the_normative_text() -> None:
    text = (REPO_ROOT / "spec" / "05-operaciones.md").read_text(encoding="utf-8")
    quoted = next(line[2:] for line in text.splitlines() if line.startswith("> Servidor del proyecto"))
    assert translator("es")("mcp.instructions") == quoted.strip()
    for lang in ("es", "en"):
        assert len(translator(lang)("mcp.instructions")) <= INSTRUCTIONS_MAX


# -- llamadas reales ------------------------------------------------------------------------------


def test_tools_call_runs_the_operation(tmp_path: Path) -> None:
    world = new_world(tmp_path)
    world.task(12, claimant="carla", title="Describir el muestreo")
    state = tmp_path / "fake.json"
    world.forge.save(state)
    env = {**world.env, P + "TESTING": "1", P + "FORGE_ADAPTER": "fake", P + "FORGE_FAKE_STATE": str(state)}
    server = mcp_server.build_server(env=env, cwd=str(tmp_path), t=translator("es"))
    _init, called = converse(server, [
        {"id": 1, "method": "initialize", "params": INIT},
        {"id": 2, "method": "tools/call", "params": {"name": "get_status",
                                                      "arguments": {"project_root": str(world.repo.path)}}},
    ])
    result = called["result"]
    envelope = result["structuredContent"]
    assert result["isError"] is False and envelope["ok"] is True and envelope["op"] == "status"
    assert [t["number"] for t in envelope["data"]["tasks"]] == [12]
    assert json.loads(result["content"][0]["text"]) == envelope
    lines = (Store(world.repo.path).root / "log" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    assert any(e["kind"] == "mcp" and e.get("tool") == "get_status" and e["result"] == "ok" for e in events)
    assert any(e["kind"] == "op" and e.get("name") == "status" for e in events)


# -- cancelación (REQ-OPS-55) --------------------------------------------------------------------

_SEEN: list[bool] = []


def _slow_run(ctx: OpContext, **_kwargs: object) -> Result:
    deadline = time.monotonic() + 30
    while not ctx.cancelled() and time.monotonic() < deadline:
        time.sleep(0.01)
    _SEEN.append(ctx.cancelled())
    return ok("status", {"cancelled": ctx.cancelled()})


def test_a_cancelled_call_is_never_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType(f"{_meta.NAME}.ops.slow_mcp_test")
    module.run = _slow_run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    spec = next(s for s in REGISTRY if s.id == "status")
    slow = OpSpec(id="status", cli=spec.cli, mcp_tool="get_status", human_only=False, effects="none",
                  offline="degraded", module="ops.slow_mcp_test", data_def=spec.data_def, budget_chars=12000)
    server = mcp_server.build_server(env={}, cwd=str(REPO_ROOT), t=translator("es"), registry=[slow])
    _SEEN.clear()

    async def main() -> list[dict[str, Any]]:
        out = []
        async with create_client_server_memory_streams() as (client, served):
            c_read, c_write = client
            async with anyio.create_task_group() as tg:
                tg.start_soon(mcp_server.serve, server, *served)
                for m in ({"id": 1, "method": "initialize", "params": INIT},
                          {"method": "notifications/initialized"},
                          {"id": 2, "method": "tools/call", "params": {"name": "get_status", "arguments": {}}}):
                    await c_write.send(SessionMessage(message=_message(m)))
                with anyio.fail_after(60):
                    out.append(await c_read.receive())  # la respuesta a initialize
                await anyio.sleep(0.2)
                for m in ({"method": "notifications/cancelled", "params": {"requestId": 2}},
                          {"id": 3, "method": "ping"}):
                    await c_write.send(SessionMessage(message=_message(m)))
                with anyio.fail_after(60):
                    out.append(await c_read.receive())
                with anyio.move_on_after(1):  # nada más debe llegar
                    out.append(await c_read.receive())
                await c_write.aclose()
        return [m.message.model_dump(by_alias=True, exclude_none=True, mode="json") for m in out]

    replies = anyio.run(main)
    assert [r.get("id") for r in replies] == [1, 3]  # nunca hubo respuesta para el 2
    deadline = time.monotonic() + 30
    while not _SEEN and time.monotonic() < deadline:
        time.sleep(0.01)
    assert _SEEN == [True]  # la operación vio la cancelación (Invocation.cancel)


# -- proceso real ---------------------------------------------------------------------------------


def test_stdio_process_speaks_only_json_rpc_and_ends_on_eof(tmp_path: Path) -> None:
    """Conversa como un cliente: cada petición espera su respuesta; luego EOF y el proceso sale."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(P)}
    # El paquete puede no estar instalado en el venv (en CI solo está el lanzador): src/ primero.
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT / "src"), env.get("PYTHONPATH")]))
    started = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-m", f"{_meta.NAME}.entry", "mcp", "--stdio"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(tmp_path), env=env,
    )
    assert proc.stdin is not None and proc.stdout is not None

    def send(message: Mapping[str, Any]) -> None:
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", **message}) + "\n").encode("utf-8"))  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

    def reply() -> dict[str, Any]:
        line = proc.stdout.readline()  # type: ignore[union-attr]
        assert line, proc.stderr.read().decode("utf-8", "replace")  # type: ignore[union-attr]
        return json.loads(line)  # type: ignore[no-any-return]

    try:
        send({"id": 1, "method": "initialize", "params": INIT})
        first = reply()
        first_ms = (time.monotonic() - started) * 1000
        send({"method": "notifications/initialized"})
        send({"id": 2, "method": "tools/list"})
        listed = reply()
        send({"id": 3, "method": "ping"})
        pong = reply()
        proc.stdin.close()
        assert proc.wait(timeout=60) == 0  # EOF: termina (REQ-OPS-54)
        rest = proc.stdout.read()
    finally:
        if proc.poll() is None:
            proc.kill()
    assert [first["id"], listed["id"], pong["id"]] == [1, 2, 3] and rest.strip() == b""
    assert first["result"]["serverInfo"]["name"] == _meta.MCP_SERVER_NAME
    assert [t["name"] for t in listed["result"]["tools"]] == TOOLS
    # La meta de 01 §7.1 es responder la primera petición en menos de 2 s (DEBERÍA): aquí se
    # comprueba con holgura, porque las máquinas del CI varían; la medida exacta se reporta.
    assert first_ms < 30_000, first_ms


def test_mcp_without_stdio_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert entry.main(["mcp"]) == 3
    assert entry.main(["mcp", "--stdio", "extra"]) == 3
    assert "mcp --stdio" in capsys.readouterr().err


def test_the_sdk_is_imported_only_by_the_mcp_package() -> None:
    """REQ-ARCH-16: además del contrato de import-linter, ``aucove.mcp.tools`` no lo importa."""
    code = (
        "import sys; import aucove.mcp.tools; "
        "print(any(m == 'mcp' or m.startswith('mcp.') for m in sys.modules))"
    )
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "src"))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120, env=env)
    assert out.stdout.strip() == "False"


def test_threading_event_is_wired_into_the_invocation() -> None:
    event = threading.Event()
    from aucove.ops.context import Invocation

    inv = Invocation(root=None, lang=None, json=True, dry_run=False, surface="mcp", cancel=event)
    ctx = OpContext(inv)
    assert not ctx.cancelled()
    event.set()
    assert ctx.cancelled()
    assert not OpContext(Invocation(root=None, lang=None, json=True, dry_run=False, surface="cli")).cancelled()
