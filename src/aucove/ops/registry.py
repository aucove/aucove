# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Registro único de operaciones y comandos internos de CI (01 §3.4, 05 §2; D-32 c).

De ``REGISTRY`` se generan el parser de la CLI, las herramientas MCP y su
``inputSchema`` (REQ-ARCH-21, REQ-OPS-1). No hay otra lista de operaciones en el
código. En H2 contenía ``doctor``, ``validate``, ``check-path`` y ``sync-roles``
(D-31), más ``evidence verify`` (D-50). H3 agrega ``status`` y ``context``, el ciclo de tareas
(``task propose``, ``accept`` y ``cancel``, ``claim``, ``release``, ``save``, ``update``,
``switch``, ``record-assessment``, ``submit`` y ``expire-claims``), ``decision propose`` y
``direction propose``, ``review`` e ``init``; ``metrics`` llega con los pasos siguientes.

Un módulo de operación exporta ``run(ctx: OpContext, **kwargs) -> Result`` y,
de forma opcional, ``render_text(result, t, *, fmt='text') -> str``.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import timezone
from types import ModuleType
from typing import TYPE_CHECKING, Literal, TextIO

from .. import _meta
from ..domain.errors import FORGE_KIND_CODES
from ..domain.trailers import DECLARABLE_USES
from ..forge.base import BaseNotDefault, ForgeError
from ..i18n import Translator, resolve_lang, translator
from .result import OpError, Result, fail, internal, make_error, make_warning

if TYPE_CHECKING:
    import argparse

    from .context import Invocation, OpContext

ParamType = Literal["string", "integer", "boolean", "array", "object"]
Effects = Literal["none", "local", "forge"]
Offline = Literal["local", "degraded", "online"]
# Candados de una invocación según sus argumentos (``spec.locks(args)`` de REQ-OPS-3).
LockKeys = Callable[[Mapping[str, object]], Sequence[str]]


@dataclass(frozen=True, slots=True)
class Param:
    """Argumento de una operación; de él salen el flag de la CLI y la propiedad MCP."""

    name: str  # snake_case; CLI --kebab-case (REQ-OPS-12) salvo cli_flag
    type: ParamType
    required: bool = False
    positional: bool = False
    repeatable: bool = False
    schema: Mapping[str, object] | None = None  # restricciones extra de JSON Schema
    cli_only: bool = False  # no se expone en el inputSchema de MCP
    default: object = None
    cli_flag: str | None = None
    choices: tuple[str, ...] = ()
    cli_short: str | None = None  # alias de una letra, p. ej. '-m' de save (05 §9.14)
    cli_csv: bool = False  # lista: en la CLI, valores separados por comas (--ai-use a,b)
    cli_input_only: bool = False  # sin flag: en la CLI solo llega por --input (objetos anidados)
    # parámetro solo de la CLI con la ruta de un archivo cuyo contenido es el de otro parámetro
    # (--summary-file da summary); con cli_file_json, el archivo es JSON (--note-file da note)
    cli_file_for: str | None = None
    cli_file_json: bool = False
    cli_negatable: bool = False  # booleano con --x y --no-x; sin ninguno queda el default (None)
    # si falta y hay terminal, la CLI lo pide en una línea (catálogo cli.ask.<op>.<param>;
    # 05 §9.24: el cuerpo de review request-changes). Con choices, la respuesta es una de ellas
    # y vacía es «sin valor» (la operación usa el suyo por omisión).
    cli_ask: bool = False
    cli_ask_unless: str | None = None  # parámetro booleano que, si es verdadero, evita la pregunta
    # su valor también elige el idioma de los mensajes si no lo fijan --lang ni el entorno
    # (01 REQ-ARCH-71: init pregunta el idioma primero)
    cli_lang: bool = False


@dataclass(frozen=True, slots=True)
class OpSpec:
    """Declaración única de una operación (D-32 c)."""

    id: str  # valor de 'op' (05 §3.2)
    cli: tuple[str, ...]
    mcp_tool: str | None
    human_only: bool
    effects: Effects
    offline: Offline
    module: str  # relativo al paquete: 'ops.check_path'
    params: tuple[Param, ...] = ()
    data_def: str = ""  # '#/$defs/data_check_path'
    budget_chars: int | None = None  # tope de 05 §7.1
    # Candados de 05 §6.4 ('repo', 'task-<n>'), fijos o en función de los argumentos;
    # registry.invoke los toma antes de run (REQ-OPS-28, REQ-OPS-29).
    locks: tuple[str, ...] | LockKeys = ()
    # Flags que la CLI admite junto con --input (05 §9.17: record-assessment <n> --phase); si el
    # JSON también los trae, deben coincidir.
    input_with: tuple[str, ...] = ()
    # Claves que se agregan al inputSchema (p. ej. el if/then de 05 §9.17).
    schema_extra: Mapping[str, object] | None = None
    # La CLI con terminal pide confirmar attest-v1 si no llega sign_off (11 REQ-TRACE-22).
    attest: bool = False


@dataclass(frozen=True, slots=True)
class CiCommand:
    """Subcomando interno ``ci <name>`` (09-ci). No emite el sobre (DC-23)."""

    name: str  # 'build-plan', 'pin-plan', 'comment', 'base-report'
    add_arguments: Callable[[argparse.ArgumentParser], None]
    main: Callable[[argparse.Namespace, OpContext, TextIO], int]  # 0, 1 o 3; nunca 2


_TASK_SCHEMA: Mapping[str, object] = {"minimum": 1}
_SECTION_ID: Mapping[str, object] = {"type": "string", "pattern": "^[a-z0-9]+(-[a-z0-9]+)*$"}
_PLAN_ID: Mapping[str, object] = {"type": "string", "pattern": "^(RQ|HYP|IN|OUT|METH)-[1-9][0-9]*$"}
_LOGIN_PATTERN = "^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$"
_DECISION_ID: Mapping[str, object] = {
    "type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}-[1-9][0-9]*-[a-z0-9]+(-[a-z0-9]+)*$"}
# Formas de 05 §9.17 (record_overlap_assessment) y §9.18 (note de submit_task).
_OVERLAP: Mapping[str, object] = {
    "type": "object", "additionalProperties": False, "required": ["target", "kind", "reason"],
    "properties": {
        "target": {"oneOf": [
            {"type": "object", "additionalProperties": False, "required": ["task"],
             "properties": {"task": {"type": "integer", "minimum": 1}}},
            {"type": "object", "additionalProperties": False, "required": ["pr"],
             "properties": {"pr": {"type": "integer", "minimum": 1}}},
            {"type": "object", "additionalProperties": False, "required": ["decision"],
             "properties": {"decision": {"type": "string", "pattern": "^memory/decisions/[^/]+\\.md$"}}},
            {"type": "object", "additionalProperties": False, "required": ["section"],
             "properties": {"section": {"type": "string", "pattern": "^[a-z0-9]+(-[a-z0-9]+)*$"}}},
        ]},
        "kind": {"enum": ["scope", "contradiction", "reference"]},
        "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
        "evidence": {"type": "array", "maxItems": 10, "items": {
            "type": "object", "additionalProperties": False, "required": ["path"],
            "properties": {"path": {"type": "string", "minLength": 1}, "line": {"type": "integer", "minimum": 1}}}},
    },
}
_DIRECTION: Mapping[str, object] = {
    "additionalProperties": False, "required": ["affected_ids", "criteria", "reason"],
    "properties": {
        "affected_ids": {"type": "array", "maxItems": 20, "items": _PLAN_ID},
        "criteria": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {
            "enum": ["reformulates_claim", "out_of_scope_method", "contradicts_decision", "changes_conclusions"]}},
        "reason": {"type": "string", "maxLength": 2000},
    },
}
_NOTE: Mapping[str, object] = {
    "additionalProperties": False, "required": ["done"],
    "properties": {
        "done": {"type": "string", "minLength": 1, "maxLength": 2000},
        "next": {"type": "string"},
        "decisions": {"type": "array", "items": {
            "type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}-[1-9][0-9]*-[a-z0-9]+(-[a-z0-9]+)*$"}},
        "questions": {"type": "string"},
    },
}


def _task_locks(kwargs: Mapping[str, object]) -> tuple[str, ...]:
    """``claim``, ``release`` y ``switch`` cambian de rama (``repo``) y operan sobre la tarea
    (``task-<n>``; REQ-OPS-28)."""
    task = kwargs.get("task")
    if isinstance(task, bool) or not isinstance(task, int) or task < 1:
        return ("repo",)
    return ("repo", f"task-{task}")


def _forge_task_locks(kwargs: Mapping[str, object]) -> tuple[str, ...]:
    """``record-assessment`` solo escribe en la forja sobre la tarea (``task-<n>``; REQ-OPS-28)."""
    task = kwargs.get("task")
    if isinstance(task, bool) or not isinstance(task, int) or task < 1:
        return ()
    return (f"task-{task}",)


def _repo_unless_check(kwargs: Mapping[str, object]) -> tuple[str, ...]:
    """``sync-roles`` modifica el árbol de trabajo salvo con ``--check`` (REQ-OPS-28)."""
    return () if kwargs.get("check") is True else ("repo",)


REGISTRY: tuple[OpSpec, ...] = (
    OpSpec(
        id="doctor",
        cli=("doctor",),
        mcp_tool="check_environment",
        human_only=False,
        effects="none",
        offline="degraded",
        module="ops.doctor",
        params=(Param("refresh", "boolean", default=False),),
        data_def="#/$defs/data_doctor",
        budget_chars=6000,
    ),
    # 05 §9.3: la configuración del proyecto y de la forja; solo humana, sin MCP (D-09).
    OpSpec(
        id="init",
        cli=("init",),
        mcp_tool=None,
        human_only=True,
        effects="forge",
        offline="online",
        module="ops.init",
        params=(
            Param("language", "string", choices=("es", "en"), cli_ask=True, cli_ask_unless="regenerate",
                  cli_lang=True),
            Param("format", "string", choices=("latex", "none")),
            Param("disclosure", "string", choices=("private", "milestones", "open")),
            Param("individual", "boolean", default=False),
            Param("regenerate", "boolean", default=False),
        ),
        data_def="#/$defs/data_init",
        budget_chars=6000,
        locks=("repo",),
    ),
    OpSpec(
        id="sync-roles",
        cli=("sync-roles",),
        mcp_tool=None,
        human_only=False,
        effects="local",
        offline="local",
        module="ops.sync_roles",
        params=(Param("check", "boolean", default=False, cli_only=True),),
        data_def="#/$defs/data_sync_roles",
        budget_chars=4000,
        locks=_repo_unless_check,
    ),
    OpSpec(
        id="validate",
        cli=("validate",),
        mcp_tool="validate_project",
        human_only=False,
        effects="none",
        offline="local",
        module="ops.validate",
        params=(
            Param("mode", "string", default="tree", cli_only=True, choices=("tree", "pr", "base", "build-log")),
            Param("base", "string"),
            Param("head", "string"),
            Param("checks", "array", repeatable=True, cli_flag="--check",
                  schema={"items": {"type": "string"}, "maxItems": 50}),
            Param("changed_only", "boolean", default=False),
            Param("policy", "string", cli_only=True),
            Param("tree", "string", cli_only=True),
            Param("pr", "integer", cli_only=True, schema={"minimum": 1}),
            Param("before", "string", cli_only=True),
            Param("after", "string", cli_only=True),
            Param("log", "string", cli_only=True),
            Param("report", "string", cli_only=True),
            Param("pin_report", "string", cli_only=True),
            Param("format", "string", default="text", cli_only=True, choices=("text", "github")),
            # Paginación común de 05 §9.1 (en H2, page.next_cursor siempre es null).
            Param("cursor", "string"),
            Param("max_chars", "integer", schema={"minimum": 2000, "maximum": 40000}),
        ),
        data_def="#/$defs/data_validate",
        budget_chars=12000,
    ),
    # 05 §9.6 y REQ-OPS-68: solo lee; sin red responde con la caché (degradada).
    OpSpec(
        id="status",
        cli=("status",),
        mcp_tool="get_status",
        human_only=False,
        effects="none",
        offline="degraded",
        module="ops.status",
        params=(
            Param("mine", "boolean", default=False),
            Param("task", "integer", schema=_TASK_SCHEMA),
            Param("states", "array", repeatable=True, cli_flag="--state", schema={
                "items": {"enum": ["proposed", "open", "claimed", "in_review", "changes_requested", "merged",
                                   "expired", "cancelled"]},
            }),
            Param("section", "string", schema={"pattern": "^[a-z0-9]+(-[a-z0-9]+)*$"}),
            Param("cursor", "string"),
            Param("max_chars", "integer", schema={"minimum": 2000, "maximum": 40000}),
        ),
        data_def="#/$defs/data_status",
        budget_chars=12000,
    ),
    # 05 §9.7: nunca falla por la red ni por la forja (REQ-OPS-43). El tope es
    # project.yaml: context.max_chars (defecto 8000). --task y --phase llegan con claim y
    # submit, que comparten el paquete de 04 §8.2.
    OpSpec(
        id="context",
        cli=("context",),
        mcp_tool="get_context",
        human_only=False,
        effects="none",
        offline="degraded",
        module="ops.get_context",
        params=(
            Param("refresh", "boolean", default=False),
            Param("task", "integer", schema=_TASK_SCHEMA),
            Param("phase", "string", choices=("claim", "submission")),
        ),
        data_def="#/$defs/data_context",
        budget_chars=8000,
    ),
    OpSpec(
        id="check-path",
        cli=("check-path",),
        mcp_tool="check_path",
        human_only=False,
        effects="none",
        offline="local",
        module="ops.check_path",
        params=(
            Param("paths", "array", required=True, positional=True, repeatable=True,
                  schema={"items": {"type": "string", "minLength": 1}, "minItems": 1, "maxItems": 100}),
            Param("task", "integer", schema=_TASK_SCHEMA),
        ),
        data_def="#/$defs/data_check_path",
        budget_chars=8000,
    ),
    # 05 §9.9 y 12-mvp §5.3: las tablas del piloto, sin MCP; solo lee (el CSV de --output es
    # local). Sin tope: no tiene MCP y su data es el resumen corto de las métricas.
    OpSpec(
        id="metrics",
        cli=("metrics",),
        mcp_tool=None,
        human_only=False,
        effects="none",
        offline="online",
        module="ops.metrics",
        params=(
            Param("since", "string", cli_only=True, schema={"pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}),
            Param("until", "string", cli_only=True, schema={"pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"}),
            Param("table", "string", default="tasks", cli_only=True, choices=("tasks", "events")),
            Param("output", "string", cli_only=True, schema={"minLength": 1}),
            Param("no_pseudonymize", "boolean", default=False, cli_only=True),
        ),
        data_def="#/$defs/data_metrics",
    ),
    # 05 §9.26 (D-50): sin herramienta MCP, porque las aperturas son privadas (REQ-OPS-65).
    # Solo lee (05 §9.1: «— (L con --upgrade)»); sin tope: no tiene MCP y su data completa
    # no admite recorte (05 §12.1).
    OpSpec(
        id="evidence.verify",
        cli=("evidence", "verify"),
        mcp_tool=None,
        human_only=False,
        effects="none",
        offline="degraded",
        module="ops.evidence_verify",
        params=(
            Param("path", "string", required=True, positional=True, cli_only=True, schema={"minLength": 1}),
            Param("repo", "string", cli_only=True, schema={"minLength": 1}),
            Param("content", "string", cli_only=True, schema={"minLength": 1}),
            Param("offline", "boolean", default=False, cli_only=True),
            Param("upgrade", "boolean", default=False, cli_only=True),
            Param("tz", "string", cli_only=True, schema={"minLength": 1}),
        ),
        data_def="#/$defs/data_evidence_verify",
    ),
    # 05 §9.10: crea el Issue de la tarea; la idempotencia es el marcador op (§6.1).
    OpSpec(
        id="task.propose",
        cli=("task", "propose"),
        mcp_tool="propose_task",
        human_only=False,
        effects="forge",
        offline="online",
        module="ops.task_propose",
        params=(
            Param("title", "string", required=True, schema={"minLength": 1, "maxLength": 120}),
            Param("sections", "array", required=True, repeatable=True, cli_flag="--section",
                  schema={"items": _SECTION_ID, "minItems": 1, "maxItems": 10}),
            Param("body", "string", schema={"maxLength": 8000}),
            Param("body_file", "string", cli_only=True),
            Param("plan_refs", "array", repeatable=True, cli_flag="--plan-ref",
                  schema={"items": _PLAN_ID, "maxItems": 20}),
            Param("idempotency_key", "string", schema={"pattern": "^[0-9a-f]{16,64}$"}),
        ),
        data_def="#/$defs/data_task_propose",
        budget_chars=4000,
    ),
    # 05 §9.11 y 04-tareas §4.4: solo humanas (D-09), sin MCP. La fase 2 de require_human
    # la pide la operación tras sus lecturas (ctx.confirm_human).
    OpSpec(
        id="task.accept",
        cli=("task", "accept"),
        mcp_tool=None,
        human_only=True,
        effects="forge",
        offline="online",
        module="ops.task_accept",
        params=(Param("task", "integer", required=True, positional=True, schema=_TASK_SCHEMA),),
        data_def="#/$defs/data_task_accept",
        budget_chars=4000,
        locks=_forge_task_locks,
    ),
    OpSpec(
        id="task.cancel",
        cli=("task", "cancel"),
        mcp_tool=None,
        human_only=True,
        effects="forge",
        offline="online",
        module="ops.task_cancel",
        params=(
            Param("task", "integer", required=True, positional=True, schema=_TASK_SCHEMA),
            Param("reason", "string", schema={"maxLength": 500}),
        ),
        data_def="#/$defs/data_task_cancel",
        budget_chars=4000,
        locks=_forge_task_locks,
    ),
    # 05 §9.12 y 04-tareas §2: el reclamo atómico de la tarea.
    OpSpec(
        id="claim",
        cli=("claim",),
        mcp_tool="claim_task",
        human_only=False,
        effects="forge",
        offline="online",
        module="ops.claim",
        params=(
            Param("task", "integer", required=True, positional=True, schema=_TASK_SCHEMA),
            Param("checkout", "boolean", default=True),
            Param("fresh", "boolean", default=False),
        ),
        data_def="#/$defs/data_claim",
        budget_chars=8000,
        locks=_task_locks,
    ),
    # 05 §9.13 y 04-tareas §4.1: libera el reclamo; --handoff solo en la CLI y del lead (§4.3).
    OpSpec(
        id="release",
        cli=("release",),
        mcp_tool="release_task",
        human_only=False,
        effects="forge",
        offline="online",
        module="ops.release",
        params=(
            Param("task", "integer", required=True, positional=True, schema=_TASK_SCHEMA),
            Param("reason", "string", schema={"maxLength": 500}),
            Param("handoff", "string", cli_only=True, schema={"pattern": _LOGIN_PATTERN}),
        ),
        data_def="#/$defs/data_release",
        budget_chars=4000,
        locks=_task_locks,
    ),
    # 05 §9.14: commit con los trailers de 11-trazabilidad §2.5 y push a task/<n>.
    OpSpec(
        id="save",
        cli=("save",),
        mcp_tool="save_work",
        human_only=False,
        effects="forge",
        offline="degraded",
        module="ops.save",
        params=(
            Param("message", "string", required=True, cli_short="-m", schema={"minLength": 1, "maxLength": 200}),
            Param("ai_use", "array", cli_csv=True, schema={"items": {"enum": list(DECLARABLE_USES)}, "uniqueItems": True}),
            Param("ai_tool", "string", schema={"pattern": "^[a-z0-9]+(-[a-z0-9]+)*$", "maxLength": 32}),
            Param("ai_model", "string", schema={"pattern": "^[A-Za-z0-9._:@/+-]{1,128}$"}),
            Param("paths", "array", repeatable=True, schema={"items": {"type": "string"}, "maxItems": 200}),
        ),
        data_def="#/$defs/data_save",
        budget_chars=4000,
        locks=("repo",),
    ),
    # 05 §9.15 y 04-tareas §6: integra la base con merge, nunca rebase.
    OpSpec(
        id="update",
        cli=("update",),
        mcp_tool="update_branch",
        human_only=False,
        effects="forge",
        offline="degraded",
        module="ops.update",
        params=(
            Param("continue", "boolean", default=False),
            Param("abort", "boolean", default=False),
            Param("escalate", "string", schema={"minLength": 1, "maxLength": 500}),
        ),
        data_def="#/$defs/data_update",
        budget_chars=4000,
        locks=("repo",),
    ),
    # 05 §9.16 y 04-tareas §5: cambia a otra tarea propia.
    OpSpec(
        id="switch",
        cli=("switch",),
        mcp_tool="switch_task",
        human_only=False,
        effects="local",
        offline="degraded",
        module="ops.switch",
        params=(Param("task", "integer", required=True, positional=True, schema=_TASK_SCHEMA),),
        data_def="#/$defs/data_switch",
        budget_chars=4000,
        locks=_task_locks,
    ),
    # 05 §9.17 y 04-tareas §8.3: la evaluación de traslape que hizo la sesión. En la CLI, la
    # evaluación llega por --input, junto con <n> y --phase.
    OpSpec(
        id="record-assessment",
        cli=("record-assessment",),
        mcp_tool="record_overlap_assessment",
        human_only=False,
        effects="forge",
        offline="online",
        module="ops.record_assessment",
        params=(
            Param("task", "integer", required=True, positional=True, schema=_TASK_SCHEMA),
            Param("phase", "string", required=True, choices=("claim", "submission")),
            Param("head_sha", "string", cli_input_only=True, schema={"pattern": "^[0-9a-f]{40}([0-9a-f]{24})?$"}),
            Param("verdict", "string", required=True, cli_input_only=True, choices=("none", "possible", "clear")),
            Param("overlaps", "array", cli_input_only=True, schema={"maxItems": 20, "items": _OVERLAP}),
            Param("direction", "object", cli_input_only=True, schema=_DIRECTION),
            Param("model", "string", cli_input_only=True, schema={"minLength": 1, "maxLength": 128}),
            Param("door", "string", cli_input_only=True,
                  schema={"pattern": "^[a-z0-9]+(-[a-z0-9]+)*$", "maxLength": 32}),
        ),
        data_def="#/$defs/data_record_assessment",
        budget_chars=4000,
        locks=_forge_task_locks,
        input_with=("task", "phase"),
        schema_extra={"if": {"properties": {"phase": {"const": "submission"}}}, "then": {"required": ["head_sha"]}},
    ),
    # 05 §9.18 y 04-tareas §7: nota, save, update, comprobaciones locales y el PR.
    OpSpec(
        id="submit",
        cli=("submit",),
        mcp_tool="submit_task",
        human_only=False,
        effects="forge",
        offline="degraded",
        module="ops.submit",
        params=(
            Param("summary", "string", required=True, schema={"minLength": 1, "maxLength": 4000}),
            Param("summary_file", "string", cli_only=True, cli_file_for="summary"),
            Param("title", "string", schema={"maxLength": 120}),
            Param("note", "object", cli_input_only=True, schema=_NOTE),
            Param("note_file", "string", cli_only=True, cli_file_for="note", cli_file_json=True),
            Param("ai_use", "array", cli_csv=True, schema={"items": {"enum": list(DECLARABLE_USES)}, "uniqueItems": True}),
            Param("ai_tool", "string", schema={"pattern": "^[a-z0-9]+(-[a-z0-9]+)*$", "maxLength": 32}),
            Param("ai_model", "string", schema={"pattern": "^[A-Za-z0-9._:@/+-]{1,128}$"}),
            Param("ai_note", "string", schema={"maxLength": 500}),
            Param("sign_off", "boolean", default=False),
            Param("compile", "boolean", cli_only=True, cli_negatable=True),
        ),
        data_def="#/$defs/data_submit",
        budget_chars=8000,
        locks=("repo",),
        attest=True,
    ),
    # 05 §9.19 y 04-tareas §4.2: la corre el CI programado (09-ci §6), el lead o el deputy. Sin
    # MCP; --now solo con la forja falsa.
    OpSpec(
        id="expire-claims",
        cli=("expire-claims",),
        mcp_tool=None,
        human_only=False,
        effects="forge",
        offline="online",
        module="ops.expire_claims",
        params=(
            Param("policy", "string", cli_only=True, schema={"minLength": 1}),
            Param("now", "string", cli_only=True, schema={"minLength": 1}),
        ),
        data_def="#/$defs/data_expire_claims",
        budget_chars=4000,
    ),
    # 05 §9.20 y 03-esquemas §6: el registro de decisión en la rama de la tarea (D-19).
    OpSpec(
        id="decision.propose",
        cli=("decision", "propose"),
        mcp_tool="propose_decision",
        human_only=False,
        effects="forge",
        offline="degraded",
        module="ops.decision_propose",
        params=(
            Param("title", "string", required=True, schema={"minLength": 1, "maxLength": 120}),
            Param("summary", "string", required=True, schema={"minLength": 1, "maxLength": 280}),
            Param("context", "string", required=True, schema={"minLength": 1, "maxLength": 4000}),
            Param("decision", "string", required=True, schema={"minLength": 1, "maxLength": 4000}),
            Param("consequences", "string", required=True, schema={"minLength": 1, "maxLength": 4000}),
            Param("status", "string", default="accepted", choices=("accepted", "rejected")),
            Param("alternatives", "array", repeatable=True,
                  schema={"items": {"type": "string", "maxLength": 1000}, "maxItems": 10}),
            Param("supersedes", "array", repeatable=True, cli_flag="--supersedes",
                  schema={"items": _DECISION_ID, "uniqueItems": True, "maxItems": 20}),
            Param("plan_refs", "array", repeatable=True, cli_flag="--plan-ref",
                  schema={"items": _PLAN_ID, "maxItems": 20}),
        ),
        data_def="#/$defs/data_decision_propose",
        budget_chars=4000,
        locks=("repo",),
    ),
    # 05 §9.21 y 04-tareas §9: el Issue de cambio de dirección; plan.md no se toca.
    OpSpec(
        id="direction.propose",
        cli=("direction", "propose"),
        mcp_tool="propose_direction",
        human_only=False,
        effects="forge",
        offline="online",
        module="ops.direction_propose",
        params=(
            Param("title", "string", required=True, schema={"minLength": 1, "maxLength": 120}),
            Param("kind", "string", required=True, choices=("add", "modify", "retire")),
            Param("change", "string", required=True, schema={"minLength": 1, "maxLength": 4000}),
            Param("rationale", "string", required=True, schema={"minLength": 1, "maxLength": 4000}),
            Param("plan_refs", "array", repeatable=True, cli_flag="--plan-ref",
                  schema={"items": _PLAN_ID, "maxItems": 20}),
        ),
        data_def="#/$defs/data_direction_propose",
        budget_chars=4000,
    ),
    # 05 §9.22 y 06-permisos §3.2: los PR que esperan al actor, del tablero de status.
    OpSpec(
        id="review.list",
        cli=("review", "list"),
        mcp_tool="list_reviews",
        human_only=False,
        effects="none",
        offline="degraded",
        module="ops.review_list",
        params=(
            Param("all", "boolean", default=False),
            Param("cursor", "string"),
            Param("max_chars", "integer", schema={"minimum": 2000, "maximum": 40000}),
        ),
        data_def="#/$defs/data_review_list",
        budget_chars=8000,
    ),
    # 05 §9.23: solo lectura; remite a la web para aprobar (REQ-OPS-45).
    OpSpec(
        id="review.show",
        cli=("review", "show"),
        mcp_tool="prepare_review",
        human_only=False,
        effects="none",
        offline="online",
        module="ops.review_show",
        params=(
            Param("pr", "integer", required=True, positional=True, schema={"minimum": 1}),
            Param("cursor", "string"),
            Param("max_chars", "integer", schema={"minimum": 2000, "maximum": 40000}),
        ),
        data_def="#/$defs/data_review_show",
        budget_chars=16000,
    ),
    # 05 §9.24 y 06-permisos §3.2 y §6: solo humanas (D-09), sin MCP. La fase 2 la pide la
    # operación tras sus lecturas; el núcleo nunca integra (REQ-OPS-46).
    OpSpec(
        id="review.approve",
        cli=("review", "approve"),
        mcp_tool=None,
        human_only=True,
        effects="forge",
        offline="online",
        module="ops.review_approve",
        params=(
            Param("pr", "integer", required=True, positional=True, schema={"minimum": 1}),
            Param("body", "string", schema={"maxLength": 8000}),
            Param("body_file", "string", cli_only=True, cli_file_for="body"),
        ),
        data_def="#/$defs/data_review_approve",
        budget_chars=4000,
    ),
    OpSpec(
        id="review.request-changes",
        cli=("review", "request-changes"),
        mcp_tool=None,
        human_only=True,
        effects="forge",
        offline="online",
        module="ops.review_request_changes",
        params=(
            Param("pr", "integer", required=True, positional=True, schema={"minimum": 1}),
            Param("body", "string", schema={"maxLength": 8000}, cli_ask=True),
            Param("body_file", "string", cli_only=True, cli_file_for="body"),
        ),
        data_def="#/$defs/data_review_request_changes",
        budget_chars=4000,
    ),
)

# Módulos de ops que exportan COMMANDS: tuple[CiCommand, ...].
CI_MODULES: tuple[str, ...] = (
    "ops.ci_plan", "ops.ci_comment", "ops.ci_base_report", "ops.ci_seal", "ops.ci_editor_review", "ops.ci_audit",
)


def find_cli(words: Sequence[str], registry: Sequence[OpSpec] = REGISTRY) -> tuple[OpSpec, int] | None:
    """Operación cuyo ``cli`` es el prefijo más largo de ``words``, con el número de palabras usadas."""
    best: tuple[OpSpec, int] | None = None
    for spec in registry:
        n = len(spec.cli)
        if n <= len(words) and tuple(words[:n]) == spec.cli and (best is None or n > best[1]):
            best = (spec, n)
    return best


def find_tool(name: str, registry: Sequence[OpSpec] = REGISTRY) -> OpSpec | None:
    """Operación expuesta como la herramienta MCP ``name``."""
    for spec in registry:
        if spec.mcp_tool is not None and spec.mcp_tool == name:
            return spec
    return None


def param_schema(p: Param) -> dict[str, object]:
    """Propiedad JSON Schema de un ``Param``."""
    prop: dict[str, object] = {"type": p.type}
    if p.choices:
        prop["enum"] = list(p.choices)
    if p.schema:
        prop.update(p.schema)
    if p.default is not None:
        prop["default"] = p.default
    return prop


def input_schema(spec: OpSpec) -> dict[str, object]:
    """``inputSchema`` de REQ-OPS-42: comunes (``project_root``; ``dry_run`` con efectos) y específicas."""
    properties: dict[str, object] = {
        "project_root": {"type": "string", "description": "Ruta absoluta; opcional"},
    }
    if spec.effects != "none":
        properties["dry_run"] = {"type": "boolean", "default": False}
    required: list[str] = []
    for p in spec.params:
        if p.cli_only:
            continue
        properties[p.name] = param_schema(p)
        if p.required:
            required.append(p.name)
    schema: dict[str, object] = {"type": "object", "additionalProperties": False, "properties": properties,
                                 "required": required}
    schema.update(spec.schema_extra or {})
    return schema


def lock_keys(spec: OpSpec, kwargs: Mapping[str, object]) -> tuple[str, ...]:
    """Candados que toma ``spec`` con los argumentos ``kwargs`` (05 §6.4)."""
    locks = spec.locks
    return tuple(locks(kwargs) if callable(locks) else locks)


def load_op_module(spec: OpSpec) -> ModuleType:
    """Importa el módulo de la operación, relativo al paquete (``_meta.NAME``)."""
    return importlib.import_module("." + spec.module, _meta.NAME)


def human_precheck(inv: Invocation) -> str | None:
    """Fase 1 de ``require_human`` (06-permisos §6): el código ``E_HUMAN_ONLY_*`` o ``None``.

    Contexto de agente (``CLAUDE_CODE_CHILD_SESSION`` no vacía, ``CI`` o ``GITHUB_ACTIONS``
    en ``true``; nunca ``CLAUDECODE``, REQ-OPS-49) y después la TTY de stdin y stdout. Solo
    la superficie ``cli`` puede tener una persona delante.
    """
    from .context import stdio_is_tty

    env = inv.env
    if env.get("CLAUDE_CODE_CHILD_SESSION") or env.get("CI") == "true" or env.get("GITHUB_ACTIONS") == "true":
        return "E_HUMAN_ONLY_AGENT_ENV"
    if inv.surface != "cli":
        return "E_HUMAN_ONLY_NO_TTY"
    if not (inv.tty if inv.tty is not None else stdio_is_tty()):
        return "E_HUMAN_ONLY_NO_TTY"
    return None


def _human_barrier(spec: OpSpec, inv: Invocation) -> str | None:
    """Barrera de las operaciones H antes de leer nada (05 §10; REQ-OPS-3, REQ-ARCH-23).

    Con ``--dry-run`` basta la fase 1 (REQ-OPS-13, REQ-OPS-47). Sin ``--dry-run`` hace falta
    la fase 2, el reto tecleado tras las lecturas: la operación la pide con
    ``ctx.confirm_human`` antes de su primer efecto, y solo la CLI la ofrece
    (``Invocation.confirm``). Sin ella no se ejecuta nada (falla cerrada). Solo ``cli`` puede
    traer ``human_confirmed`` (REQ-ARCH-23).
    """
    if not spec.human_only or (inv.human_confirmed and inv.surface == "cli"):
        return None
    code = human_precheck(inv)
    if code is not None or inv.dry_run:
        return code
    if inv.confirm is not None and inv.surface == "cli":
        return None  # la fase 2 la hace la operación, tras sus lecturas
    return "E_HUMAN_ONLY_NOT_CONFIRMED"


def _safe_t(ctx: OpContext) -> Translator:
    try:
        return ctx.t()
    except Exception:
        try:
            return translator(resolve_lang(cli=ctx.inv.lang, env=ctx.inv.env, project=None))
        except Exception:
            return translator("en")


def forge_error_code(exc: ForgeError) -> tuple[str, dict[str, object]]:
    """Código de 05 §12.3 y ``details`` de un ``ForgeError`` que la operación no tradujo.

    ``BaseNotDefault`` va por su tipo (E_CONFIG_BASE_NOT_DEFAULT). ``ref_exists`` llega aquí
    como E_FORGE_CONFLICT: en ``claim`` lo resuelve la propia operación (05 §9.12).
    """
    if isinstance(exc, BaseNotDefault):
        return "E_CONFIG_BASE_NOT_DEFAULT", {"default_branch": exc.default_branch}
    details: dict[str, object] = {"kind": exc.kind}
    if exc.http_status is not None:
        details["status"] = exc.http_status
    return FORGE_KIND_CODES.get(exc.kind) or "E_FORGE_UNEXPECTED", details


def _dispatch(spec: OpSpec, ctx: OpContext, kwargs: Mapping[str, object]) -> Result:
    try:
        barrier = _human_barrier(spec, ctx.inv)
        if barrier is not None:
            return fail(spec.id, make_error(barrier, _safe_t(ctx)))
        module = load_op_module(spec)
        from .context import local_locks

        # REQ-OPS-3: tras la fase 1 y la resolución del proyecto, y antes de leer el estado.
        with local_locks(ctx, lock_keys(spec, kwargs), spec.id):
            result = module.run(ctx, **kwargs)
        if not isinstance(result, Result) or result.op != spec.id:
            raise TypeError("la operación no devolvió un Result propio")
        return result
    except OpError as exc:
        try:
            err = make_error(exc.code, _safe_t(ctx), details=exc.details, **exc.fmt)
            return fail(spec.id, err, data=exc.data, warnings=exc.warnings)
        except Exception as inner:
            return internal(spec.id, inner, _safe_t(ctx))
    except ForgeError as exc:  # REQ-OPS-63: lo que la operación no tradujo, por su kind
        try:
            code, details = forge_error_code(exc)
            return fail(spec.id, make_error(code, _safe_t(ctx), details=details))
        except Exception as inner:
            return internal(spec.id, inner, _safe_t(ctx))
    except KeyboardInterrupt:
        raise
    except (Exception, SystemExit) as exc:  # nunca se propaga (REQ-OPS-3)
        return internal(spec.id, exc, _safe_t(ctx))


def _log_op(spec: OpSpec, ctx: OpContext, result: Result, started: float) -> None:
    """Línea ``kind: op`` en ``.aucove/log/events.jsonl`` del proyecto (REQ-ARCH-90).

    Solo campos de la lista cerrada de 01 §11 (nunca el texto de una excepción). Sin
    proyecto no se escribe nada. Nunca falla ni cambia el resultado (REQ-ARCH-92).
    """
    try:
        project = ctx.project(required=False)
        if project is None:
            return
        from ..obs.events import emit

        emit(
            project.root,
            kind="op",
            name=spec.id,
            result="ok" if result.ok else "error",
            code=result.error.code if result.error is not None else None,
            ms=(time.monotonic() - started) * 1000,
        )
    except Exception:  # noqa: BLE001 - registrar nunca cambia el resultado
        return


BROAD_TOKEN_FILE = "broad-token.json"  # día del último W_AUTH_BROAD_TOKEN (REQ-FORGE-39)


def _broad_token(spec: OpSpec, ctx: OpContext, result: Result) -> Result:
    """REQ-FORGE-39: con un token de alcance ``broad``, la primera operación con red del día agrega
    ``W_AUTH_BROAD_TOKEN`` con el enlace del token fine-grained del repositorio (``doctor`` lo da
    siempre por su cuenta). No llama a la forja: lee ``token_scope`` de ``identity.json``, la caché
    vigente que deja el adaptador, y recuerda el día (UTC) en la caché local. Nunca falla ni
    bloquea la operación.
    """
    try:
        if (spec.offline == "local" or getattr(ctx, "_forge", None) is None
                or any(w.code == "W_AUTH_BROAD_TOKEN" for w in result.warnings)):
            return result
        project = ctx.project(required=False)
        if project is None:
            return result
        store = ctx.store()
        env = store.read_envelope("identity.json")
        data = env.get("data") if env is not None else None
        now = ctx.inv.now()
        if not isinstance(data, dict) or data.get("token_scope") != "broad":
            return result
        expires = data.get("expires_at")
        if not isinstance(expires, str) or expires <= now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"):
            return result  # una identidad vencida puede ser de otro token
        today = now.astimezone(timezone.utc).date().isoformat()
        key = {"warning": "W_AUTH_BROAD_TOKEN"}
        seen = store.read(BROAD_TOKEN_FILE, key=key)
        if seen is not None and seen.get("day") == today:
            return result
        store.write(BROAD_TOKEN_FILE, {"day": today}, key=key)
        from .doctor import token_link

        details: dict[str, object] = {"reason": "token_scope", "token_kind": data.get("token_kind")}
        repo = ctx.base().project.forge_repo
        if repo:
            details["token_url"] = token_link(repo)
        return replace(result, warnings=(*result.warnings, make_warning("W_AUTH_BROAD_TOKEN", _safe_t(ctx),
                                                                        details=details)))
    except Exception:  # noqa: BLE001 - un aviso nunca cambia el resultado
        return result


def invoke(spec: OpSpec, ctx: OpContext, **kwargs: object) -> Result:
    """Ejecuta la operación y devuelve siempre un ``Result``; llama siempre a ``ctx.close()``.

    Orden de REQ-OPS-3: barrera humana (fase 1), proyecto y candados de ``spec.locks``
    (``E_STATE_BUSY``) y ``run``. ``human_only`` sin confirmación da el ``E_HUMAN_ONLY_*`` de
    05 §12.1; ``OpError`` da ``fail``; cualquier otra excepción, ``E_INTERNAL_UNEXPECTED`` sin
    su texto. Cada invocación deja una línea en el registro de eventos (REQ-ARCH-90).
    """
    started = time.monotonic()
    close_error: BaseException | None = None
    try:
        result = _broad_token(spec, ctx, _dispatch(spec, ctx, kwargs))
    finally:
        try:
            ctx.close()
        except Exception as exc:
            close_error = exc
    if close_error is not None:
        result = internal(spec.id, close_error, _safe_t(ctx))
    _log_op(spec, ctx, result, started)
    return result
