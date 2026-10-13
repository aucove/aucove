# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Herramientas MCP del registro, sin el SDK (05-operaciones §11.2 a §11.4).

Todo sale de ``ops.registry.REGISTRY``: una herramienta por operación con ``mcp_tool``, en el
orden de 00-marco §5.7 (REQ-OPS-56). Este módulo no importa el SDK MCP, para que se pueda
probar solo y para que cambiar de SDK toque solo ``server`` (REQ-ARCH-16).

- ``descriptor``: ``name``, ``title`` y ``description`` localizados (máx. 600), el
  ``inputSchema`` del registro (REQ-OPS-42), el ``outputSchema`` y las cuatro anotaciones
  explícitas de REQ-OPS-59.
- ``output_schema``: ``result.schema.json`` empaquetado con ``op`` fijado (REQ-OPS-57). Se
  quita ``$id``, se dejan las reglas generales del sobre y la de la operación, y solo las
  definiciones que esa parte alcanza: el esquema completo pesa unos 110 KB y con 17
  herramientas ``tools/list`` no cabría en el contexto.
- ``call_tool``: valida los argumentos contra el ``inputSchema`` (un error es un sobre con
  ``E_VALIDATION_INPUT``, no un error de protocolo; REQ-OPS-58), corre la operación con
  ``registry.invoke`` y deja una línea ``kind: mcp`` en ``events.jsonl`` (REQ-OPS-54).
"""

from __future__ import annotations

import copy
import dataclasses
import functools
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from ..i18n import Translator, translator
from ..ops.context import Invocation, OpContext
from ..ops.inputs import input_error, schema_errors
from ..ops.registry import REGISTRY, OpSpec, input_schema, invoke
from ..ops.result import Result, fail, make_error

__all__ = [
    "ANNOTATIONS",
    "DESCRIPTION_MAX",
    "INSTRUCTIONS_MAX",
    "call_tool",
    "descriptor",
    "instructions",
    "mcp_specs",
    "output_schema",
]

DESCRIPTION_MAX = 600  # REQ-OPS-56
INSTRUCTIONS_MAX = 2048  # REQ-OPS-60: Claude Code trunca lo que pasa de ahí

# REQ-OPS-59: (readOnlyHint, destructiveHint, idempotentHint, openWorldHint) de las 17
# herramientas de 05 §11.4, aunque el registro todavía no las tenga todas.
_READ_ONLY = (True, False, True, False)
_ADDITIVE = (False, False, True, False)
_DESTRUCTIVE = (False, True, True, False)
ANNOTATIONS: Mapping[str, tuple[bool, bool, bool, bool]] = {
    **dict.fromkeys(("check_environment", "validate_project", "get_status", "get_context", "check_path",
                     "list_reviews", "prepare_review"), _READ_ONLY),
    **dict.fromkeys(("propose_task", "switch_task", "record_overlap_assessment", "propose_decision",
                     "propose_direction"), _ADDITIVE),
    **dict.fromkeys(("claim_task", "release_task", "save_work", "update_branch", "submit_task"), _DESTRUCTIVE),
}

_REF = "#/$defs/"


def mcp_specs(registry: Sequence[OpSpec] = REGISTRY) -> list[OpSpec]:
    """Operaciones con herramienta MCP, en el orden del registro (el de 00-marco §5.7)."""
    return [spec for spec in registry if spec.mcp_tool is not None and not spec.human_only]


def _rule_name(op_id: str) -> str:
    return "rule_" + op_id.replace("-", "_").replace(".", "_")


def _refs(node: object) -> Iterator[str]:
    """Nombres de ``$defs`` que cita ``node`` (``#/$defs/<nombre>``)."""
    if isinstance(node, Mapping):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(_REF):
            yield ref[len(_REF):]
        for value in node.values():
            yield from _refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refs(value)


@functools.cache
def _output_schema(op_id: str) -> Mapping[str, Any]:
    from ..config.schemas import schema

    full = schema("result")
    defs = full["$defs"]
    rule = _rule_name(op_id)
    if rule not in defs:
        raise KeyError(f"sin {rule} en result.schema.json")
    out: dict[str, Any] = {k: copy.deepcopy(v) for k, v in full.items() if k not in ("$id", "$defs", "allOf")}
    out["properties"]["op"] = {"const": op_id}
    general = [r for r in full["allOf"] if not (isinstance(r, Mapping) and str(r.get("$ref", "")).startswith(_REF))]
    out["allOf"] = [*copy.deepcopy(general), {"$ref": _REF + rule}]
    needed: set[str] = set()
    pending = list(_refs(out))
    while pending:
        name = pending.pop()
        if name in needed:
            continue
        needed.add(name)
        pending.extend(_refs(defs[name]))
    out["$defs"] = {name: copy.deepcopy(defs[name]) for name in sorted(needed)}
    return out


def output_schema(op_id: str) -> dict[str, Any]:
    """``outputSchema`` autocontenido de la operación ``op_id`` (REQ-OPS-57); una copia nueva."""
    return copy.deepcopy(dict(_output_schema(op_id)))


def descriptor(spec: OpSpec, t: Translator) -> dict[str, Any]:
    """Herramienta de ``tools/list`` en la forma JSON del protocolo."""
    name = spec.mcp_tool
    assert name is not None
    read_only, destructive, idempotent, open_world = ANNOTATIONS[name]
    return {
        "name": name,
        "title": t(f"mcp.tool.{name}.title"),
        "description": t(f"mcp.tool.{name}.description")[:DESCRIPTION_MAX],
        "inputSchema": input_schema(spec),
        "outputSchema": output_schema(spec.id),
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
            "idempotentHint": idempotent,
            "openWorldHint": open_world,
        },
    }


def instructions(t: Translator) -> str:
    """``instructions`` del servidor en el idioma de ``t`` (REQ-OPS-60)."""
    return t("mcp.instructions")[:INSTRUCTIONS_MAX]


def _safe_t(ctx: OpContext) -> Translator:
    try:
        return ctx.t()
    except Exception:  # noqa: BLE001 - sin proyecto legible, el idioma cae a 'en'
        return translator("en")


def call_tool(
    spec: OpSpec,
    arguments: object,
    *,
    env: Mapping[str, str],
    cwd: str,
    cancel: threading.Event | None = None,
) -> Result:
    """``tools/call`` de la herramienta de ``spec``: el sobre de la operación (REQ-OPS-58).

    ``project_root`` elige el proyecto (REQ-OPS-4) y ``dry_run`` existe solo en las que tienen
    efectos (REQ-OPS-42). Los parámetros ``cli_only`` no están en el ``inputSchema``: toman su
    valor por defecto. Nunca lanza: ``invoke`` convierte cualquier falla en un sobre.
    """
    started = time.monotonic()
    args = dict(arguments) if isinstance(arguments, Mapping) else None
    if arguments is None:
        args = {}
    root = args.get("project_root") if args is not None else None
    inv = Invocation(
        root=root if isinstance(root, str) else None,
        lang=None,
        json=True,
        dry_run=False,
        surface="mcp",
        env=dict(env),
        cwd=cwd,
        cancel=cancel,
    )
    errors = [input_error(None, "type")] if args is None else schema_errors(args, input_schema(spec))
    if errors or args is None:
        ctx = OpContext(inv)
        try:
            result = fail(spec.id, make_error("E_VALIDATION_INPUT", _safe_t(ctx), details={"errors": errors}))
        finally:
            ctx.close()
    else:
        dry_run = args.pop("dry_run", False) is True
        args.pop("project_root", None)
        kwargs = {p.name: args[p.name] if p.name in args else copy.deepcopy(p.default) for p in spec.params}
        result = invoke(spec, OpContext(dataclasses.replace(inv, dry_run=dry_run)), **kwargs)
    OpContext(inv).emit(
        kind="mcp",
        tool=spec.mcp_tool,
        result="ok" if result.ok else "error",
        code=result.error.code if result.error is not None else None,
        ms=(time.monotonic() - started) * 1000,
    )
    return result
