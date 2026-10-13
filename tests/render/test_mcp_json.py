# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``.mcp.json`` del render de la plantilla (01-arquitectura §10.3; 02-convencion §8.6;
05-operaciones §13; 10-seguridad SB04): solo el servidor del núcleo, en forma exec, y
``mcp_problem``, la comprobación de SB04 que usa ``doctor``."""

from __future__ import annotations

import json

import pytest

from aucove import _meta
from aucove.domain.licenses import category
from aucove.domain.paths import classify
from aucove.render.mcp import MCP_JSON_PATH, mcp_json, mcp_problem, server
from tests.conftest import load_tool


@pytest.mark.parametrize("fmt", ["latex", "none"])
@pytest.mark.parametrize("lang", ["es", "en"])
def test_the_render_has_the_mcp_server(lang: str, fmt: str) -> None:
    files = load_tool("render_template").render_files(lang, fmt=fmt)
    assert files[MCP_JSON_PATH] == mcp_json()  # sin idioma ni formato


def test_only_the_core_server_in_exec_form() -> None:
    """SB04: un solo servidor, ``<nombre> mcp --stdio``, sin shell ni variables."""
    data = json.loads(mcp_json().decode("utf-8"))
    assert data == {"mcpServers": {_meta.MCP_SERVER_NAME: {"command": _meta.NAME, "args": ["mcp", "--stdio"]}}}
    assert server() == data["mcpServers"][_meta.MCP_SERVER_NAME]
    raw = mcp_json()
    assert raw.endswith(b"}\n") and b"\r" not in raw and not raw.startswith(b"\xef\xbb\xbf")


def test_mcp_json_is_lead_class_code() -> None:
    """Lo cambia solo el responsable (02 §2.2) y su licencia es la de ``code`` (02 S-8)."""
    assert classify(MCP_JSON_PATH, frozenset(), symlinked=False).cls == "lead"
    assert category(MCP_JSON_PATH) == "code"


def _with(entry: object, **others: object) -> bytes:
    return json.dumps({"mcpServers": {_meta.MCP_SERVER_NAME: entry, **others}}).encode("utf-8")


CORE = {"command": _meta.NAME, "args": ["mcp", "--stdio"]}


@pytest.mark.parametrize(("raw", "problem"), [
    (mcp_json(), None),
    (_with({**CORE, "type": "stdio"}), None),  # la forma explícita de Claude Code
    (b"\xff\xfe", "invalid"),
    (b"[]", "invalid"),
    (b'{"mcpServers": []}', "invalid"),
    (b'{"mcpServers": {}}', "servers"),
    (_with(CORE, otro={"command": "otro"}), "servers"),
    (json.dumps({"mcpServers": {"otro": CORE}}).encode(), "servers"),
    (_with({"command": _meta.NAME, "args": ["mcp"]}), "command"),
    (_with({"command": "sh", "args": ["-c", f"{_meta.NAME} mcp --stdio"]}), "command"),  # con shell
    (_with({**CORE, "env": {"PATH": "."}}), "command"),
    (_with({**CORE, "type": "http"}), "command"),
    (_with([CORE]), "command"),
])
def test_mcp_problem_is_sb04(raw: bytes, problem: str | None) -> None:
    """10-seguridad SB04: solo el servidor del núcleo, ``<nombre> mcp --stdio``, en forma exec."""
    assert mcp_problem(raw) == problem
