# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Registro de operaciones, inputSchema e invoke (01 §3.4; 05 §2, §9; D-31, D-32 c)."""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Iterator
from pathlib import Path

import jsonschema
import pytest

from aucove import _meta
from aucove.forge.base import BaseNotDefault, ForgeAuthError, NetworkUnreachable
from aucove.ops import registry
from aucove.ops.context import Invocation, OpContext
from aucove.ops.registry import CI_MODULES, REGISTRY, OpSpec, Param, find_cli, find_tool, input_schema, invoke
from aucove.ops.result import OpError, Result, ok
from tests.conftest import REPO_ROOT

# 00-marco §5.7: filas «sí», salvo hook, mcp y ci (REQ-ARCH-22). Valor: (herramienta MCP, H).
MARCO_OPS: dict[str, tuple[str | None, bool]] = {
    "doctor": ("check_environment", False),
    "init": (None, True),
    "sync-roles": (None, False),
    "validate": ("validate_project", False),
    "status": ("get_status", False),
    "context": ("get_context", False),
    "check-path": ("check_path", False),
    "metrics": (None, False),
    "evidence.verify": (None, False),  # sin MCP (REQ-OPS-65; D-50)
    "evidence.sign": (None, True),  # solo humana, sin MCP (D-72)
    "task.propose": ("propose_task", False),
    "task.accept": (None, True),
    "task.cancel": (None, True),
    "claim": ("claim_task", False),
    "release": ("release_task", False),
    "save": ("save_work", False),
    "update": ("update_branch", False),
    "switch": ("switch_task", False),
    "record-assessment": ("record_overlap_assessment", False),
    "submit": ("submit_task", False),
    "expire-claims": (None, False),
    "decision.propose": ("propose_decision", False),
    "direction.propose": ("propose_direction", False),
    "review.list": ("list_reviews", False),
    "review.show": ("prepare_review", False),
    "review.approve": (None, True),
    "review.request-changes": (None, True),
}
RESERVED_CLI = {("upgrade",), ("report", "ai-use"), ("report", "credit"), ("section", "read"), ("section", "propose-change")}
RESERVED_TOOLS = {"read_section", "propose_section_change"}

# 05 §9.1 (efectos y sin red) y §7.1 (topes) de las operaciones registradas. evidence.verify
# no tiene tope: no tiene MCP y su data completa no admite recorte (05 §12.1); metrics tampoco:
# no tiene MCP y su data es el resumen corto de las métricas. El de context es project.yaml:
# context.max_chars, 8000 por defecto.
H2_ATTRS = {
    "doctor": ("none", "degraded", 6000),
    "init": ("forge", "online", 6000),
    "sync-roles": ("local", "local", 4000),
    "validate": ("none", "local", 12000),
    "status": ("none", "degraded", 12000),
    "context": ("none", "degraded", 8000),
    "check-path": ("none", "local", 8000),
    "metrics": ("none", "online", None),
    "evidence.verify": ("none", "degraded", None),
    "evidence.sign": ("local", "online", None),
    "task.propose": ("forge", "online", 4000),
    "claim": ("forge", "online", 8000),
    "release": ("forge", "online", 4000),
    "save": ("forge", "degraded", 4000),
    "update": ("forge", "degraded", 4000),
    "switch": ("local", "degraded", 4000),
    "record-assessment": ("forge", "online", 4000),
    "submit": ("forge", "degraded", 8000),
    "task.accept": ("forge", "online", 4000),
    "task.cancel": ("forge", "online", 4000),
    "expire-claims": ("forge", "online", 4000),
    "decision.propose": ("forge", "degraded", 4000),
    "direction.propose": ("forge", "online", 4000),
    "review.list": ("none", "degraded", 8000),
    "review.show": ("none", "online", 16000),
    "review.approve": ("forge", "online", 4000),
    "review.request-changes": ("forge", "online", 4000),
}


def test_registry_equals_marco_5_7() -> None:
    """REQ-ARCH-22: REGISTRY = operaciones «sí» de 00-marco §5.7 (completo desde metrics, en H3)."""
    assert {spec.id for spec in REGISTRY} == set(MARCO_OPS)


def test_registry_content() -> None:
    """H2 (D-31 y D-50), status y context (paso 2 de H3), task propose y claim (paso 4a),
    release, save, update y switch (paso 4b), record-assessment y submit (paso 4c), y task accept
    y cancel, expire-claims, decision propose y direction propose (paso 4d), review list y show
    (paso 5a), review approve y request-changes (paso 5b), init (paso 6c), metrics y evidence sign (D-72)."""
    assert [spec.id for spec in REGISTRY] == [
        "doctor", "init", "sync-roles", "validate", "status", "context", "check-path", "metrics", "evidence.verify",
        "evidence.sign",
        "task.propose", "task.accept", "task.cancel", "claim", "release", "save", "update", "switch",
        "record-assessment", "submit", "expire-claims", "decision.propose", "direction.propose", "review.list",
        "review.show", "review.approve", "review.request-changes",
    ]
    order = list(MARCO_OPS)
    assert [order.index(s.id) for s in REGISTRY] == sorted(order.index(s.id) for s in REGISTRY)  # orden de §5.7


@pytest.mark.parametrize("spec", REGISTRY, ids=lambda s: s.id)
def test_opspec_attributes(spec: OpSpec) -> None:
    tool, human = MARCO_OPS[spec.id]
    assert spec.mcp_tool == tool
    assert spec.human_only is human
    if spec.human_only:
        assert spec.mcp_tool is None
    assert ".".join(spec.cli) == spec.id
    assert spec.module.startswith("ops.") and _meta.NAME not in spec.module
    effects, offline, budget = H2_ATTRS[spec.id]
    assert (spec.effects, spec.offline, spec.budget_chars) == (effects, offline, budget)
    assert spec.offline in ("local", "degraded", "online")
    schema = json.loads((REPO_ROOT / "schemas" / "result.schema.json").read_text(encoding="utf-8"))
    assert spec.data_def.startswith("#/$defs/") and spec.data_def.rsplit("/", 1)[1] in schema["$defs"]
    names = [p.name for p in spec.params]
    assert len(names) == len(set(names))
    assert all(n.isidentifier() and n == n.lower() for n in names)


def test_registry_unique_and_no_reserved() -> None:
    ids = [s.id for s in REGISTRY]
    tools = [s.mcp_tool for s in REGISTRY if s.mcp_tool]
    assert len(ids) == len(set(ids)) and len(tools) == len(set(tools))
    assert not any(s.cli in RESERVED_CLI for s in REGISTRY)
    assert not any(s.mcp_tool in RESERVED_TOOLS for s in REGISTRY)
    assert not any(s.cli[0] in ("hook", "mcp", "ci") for s in REGISTRY)


def _props(op_id: str) -> tuple[dict[str, object], list[str]]:
    spec = next(s for s in REGISTRY if s.id == op_id)
    schema = input_schema(spec)
    return schema["properties"], schema["required"]  # type: ignore[return-value]


@pytest.mark.parametrize("spec", REGISTRY, ids=lambda s: s.id)
def test_input_schema_shape(spec: OpSpec) -> None:
    """REQ-OPS-42: objeto cerrado con comunes ∪ específicas y required; además, solo las claves
    que la operación declara en schema_extra (el if/then de record_overlap_assessment, 05 §9.17)."""
    schema = input_schema(spec)
    jsonschema.Draft202012Validator.check_schema(schema)
    assert list(schema) == ["type", "additionalProperties", "properties", "required", *(spec.schema_extra or {})]
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    props = schema["properties"]
    assert props["project_root"] == {"type": "string", "description": "Ruta absoluta; opcional"}  # type: ignore[index]
    assert ("dry_run" in props) == (spec.effects != "none")  # type: ignore[operator]
    if "dry_run" in props:  # type: ignore[operator]
        assert props["dry_run"] == {"type": "boolean", "default": False}  # type: ignore[index]
    assert set(schema["required"]) <= set(props)  # type: ignore[arg-type]
    cli_only = {p.name for p in spec.params if p.cli_only}
    assert not cli_only & set(props)  # type: ignore[arg-type]


def test_input_schema_specific_properties() -> None:
    props, required = _props("doctor")
    assert props["refresh"] == {"type": "boolean", "default": False}
    assert required == []
    props, required = _props("check-path")
    assert props["paths"] == {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "maxItems": 100}
    assert props["task"] == {"type": "integer", "minimum": 1}
    assert required == ["paths"]
    props, required = _props("validate")
    assert {k: props[k] for k in ("base", "head", "checks", "changed_only")} == {
        "base": {"type": "string"},
        "head": {"type": "string"},
        "checks": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
        "changed_only": {"type": "boolean", "default": False},
    }
    assert props["cursor"] == {"type": "string"}  # paginación común (05 §9.1)
    assert props["max_chars"] == {"type": "integer", "minimum": 2000, "maximum": 40000}
    assert "mode" not in props and "pin_report" not in props  # MCP: siempre modo tree
    assert required == []
    props, required = _props("sync-roles")
    assert set(props) == {"project_root", "dry_run"} and required == []
    # 05 §9.6: get_status, paginada.
    props, required = _props("status")
    assert {k: props[k] for k in ("mine", "task", "states", "section", "cursor", "max_chars")} == {
        "mine": {"type": "boolean", "default": False},
        "task": {"type": "integer", "minimum": 1},
        "states": {"type": "array", "items": {"enum": ["proposed", "open", "claimed", "in_review",
                                                      "changes_requested", "merged", "expired", "cancelled"]}},
        "section": {"type": "string", "pattern": "^[a-z0-9]+(-[a-z0-9]+)*$"},
        "cursor": {"type": "string"},
        "max_chars": {"type": "integer", "minimum": 2000, "maximum": 40000},
    }
    assert set(props) == {"project_root", "mine", "task", "states", "section", "cursor", "max_chars"}
    assert required == []
    # 05 §9.7: get_context.
    props, required = _props("context")
    assert props == {"project_root": props["project_root"], "refresh": {"type": "boolean", "default": False},
                     "task": {"type": "integer", "minimum": 1},
                     "phase": {"type": "string", "enum": ["claim", "submission"]}}
    assert required == []
    # 05 §9.10: propose_task.
    props, required = _props("task.propose")
    assert {k: props[k] for k in ("title", "sections", "body", "done", "plan_refs", "idempotency_key")} == {
        "title": {"type": "string", "minLength": 1, "maxLength": 120},
        "sections": {"type": "array", "items": {"type": "string", "pattern": "^[a-z0-9]+(-[a-z0-9]+)*$"},
                     "minItems": 1, "maxItems": 10},
        "body": {"type": "string", "maxLength": 2000},
        "done": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 500}, "minItems": 1,
                 "maxItems": 20},
        "plan_refs": {"type": "array", "items": {"type": "string", "pattern": "^(RQ|HYP|IN|OUT|METH)-[1-9][0-9]*$"},
                      "maxItems": 20},
        "idempotency_key": {"type": "string", "pattern": "^[0-9a-f]{16,64}$"},
    }
    assert "body_file" not in props and required == ["title", "sections", "done"]
    # 05 §9.12: claim_task.
    props, required = _props("claim")
    assert {k: props[k] for k in ("task", "checkout", "fresh")} == {
        "task": {"type": "integer", "minimum": 1},
        "checkout": {"type": "boolean", "default": True},
        "fresh": {"type": "boolean", "default": False},
    }
    assert required == ["task"]
    claim = next(s for s in REGISTRY if s.id == "claim")
    assert registry.lock_keys(claim, {"task": 12}) == ("repo", "task-12")
    status = next(s for s in REGISTRY if s.id == "status")
    assert next(p for p in status.params if p.name == "states").cli_flag == "--state"


def test_validate_cli_params() -> None:
    spec = next(s for s in REGISTRY if s.id == "validate")
    by_name = {p.name: p for p in spec.params}
    assert by_name["mode"].choices == ("tree", "pr", "base", "build-log") and by_name["mode"].default == "tree"
    assert by_name["checks"].cli_flag == "--check" and by_name["checks"].repeatable
    assert by_name["format"].choices == ("text", "github")
    assert "pin_report" in by_name and by_name["pin_report"].cli_only
    for name in ("policy", "tree", "log", "report", "before", "after", "pr", "mode", "format"):
        assert by_name[name].cli_only, name
    cp = next(s for s in REGISTRY if s.id == "check-path")
    paths = cp.params[0]
    assert (paths.name, paths.positional, paths.repeatable, paths.required) == ("paths", True, True, True)
    sr = next(s for s in REGISTRY if s.id == "sync-roles")
    assert sr.params[0].name == "check" and sr.params[0].cli_only and sr.mcp_tool is None


def test_evidence_verify_spec() -> None:
    """05 §9.26: ``evidence verify <ruta> [--repo] [--content] [--keys] [--offline] [--upgrade] [--tz]``, sin MCP."""
    spec = next(s for s in REGISTRY if s.id == "evidence.verify")
    assert (spec.cli, spec.mcp_tool, spec.human_only, spec.module) == (("evidence", "verify"), None, False, "ops.evidence_verify")
    by_name = {p.name: p for p in spec.params}
    assert list(by_name) == ["path", "repo", "content", "offline", "upgrade", "tz", "keys"]
    path = by_name["path"]
    assert (path.type, path.positional, path.required, path.repeatable) == ("string", True, True, False)
    assert by_name["offline"].type == by_name["upgrade"].type == "boolean"
    assert all(p.cli_only for p in spec.params)  # REQ-OPS-65: nada se expone por MCP
    assert spec.locks == () and find_tool("verify_evidence") is None


def test_find_cli_and_tool() -> None:
    fake = (
        OpSpec("task.propose", ("task", "propose"), "propose_task", False, "forge", "online", "ops.task_propose"),
        OpSpec("task.accept", ("task", "accept"), None, True, "forge", "online", "ops.task_accept"),
        OpSpec("claim", ("claim",), "claim_task", False, "forge", "online", "ops.claim"),
    )
    spec, used = find_cli(["task", "propose", "--title", "x"], fake)  # type: ignore[misc]
    assert spec.id == "task.propose" and used == 2
    assert find_cli(["claim", "3"], fake) == (fake[2], 1)
    assert find_cli(["task"], fake) is None
    assert find_cli([], fake) is None
    assert find_cli(["validate", "--json"])[0].id == "validate"  # type: ignore[index]
    assert find_tool("claim_task", fake) is fake[2]
    assert find_tool("accept", fake) is None
    assert find_tool("check_path").id == "check-path"  # type: ignore[union-attr]
    assert find_tool("sync_roles") is None


def test_ci_modules_are_data() -> None:
    assert CI_MODULES == (
        "ops.ci_plan", "ops.ci_comment", "ops.ci_base_report", "ops.ci_seal", "ops.ci_editor_review", "ops.ci_audit",
    )


# -- invoke ---------------------------------------------------------------------------


class _Ctx(OpContext):
    def __init__(self, inv: Invocation) -> None:
        super().__init__(inv)
        self.closed = 0

    def close(self) -> None:
        self.closed += 1
        super().close()


def _inv(**kw: object) -> Invocation:
    base = {"root": None, "lang": "en", "json": True, "dry_run": False, "surface": "cli", "env": {}, "cwd": "."}
    base.update(kw)
    return Invocation(**base)  # type: ignore[arg-type]


@pytest.fixture
def op_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[types.ModuleType]:
    name = f"{_meta.NAME}.ops._test_op"
    module = types.ModuleType(name)
    monkeypatch.setitem(sys.modules, name, module)
    yield module


def _spec(**kw: object) -> OpSpec:
    base = {"id": "doctor", "cli": ("doctor",), "mcp_tool": None, "human_only": False,
            "effects": "none", "offline": "local", "module": "ops._test_op"}
    base.update(kw)
    return OpSpec(**base)  # type: ignore[arg-type]


def test_invoke_ok_and_kwargs(op_module: types.ModuleType) -> None:
    seen: dict[str, object] = {}

    def run(ctx: OpContext, **kwargs: object) -> Result:
        seen.update(kwargs)
        return ok("doctor", {"x": 1})

    op_module.run = run  # type: ignore[attr-defined]
    ctx = _Ctx(_inv())
    r = invoke(_spec(), ctx, refresh=True)
    assert r.ok and r.data == {"x": 1} and seen == {"refresh": True}
    assert ctx.closed == 1


def test_invoke_operror_becomes_fail(op_module: types.ModuleType) -> None:
    def run(ctx: OpContext, **kwargs: object) -> Result:
        raise OpError("E_CONFIG_NOT_A_PROJECT", details={"start": "."}, data=None)

    op_module.run = run  # type: ignore[attr-defined]
    ctx = _Ctx(_inv())
    r = invoke(_spec(), ctx)
    assert not r.ok and r.error is not None
    assert r.error.code == "E_CONFIG_NOT_A_PROJECT" and r.error.details == {"start": "."}
    assert r.exit_code() == 8 and ctx.closed == 1


@pytest.mark.parametrize("exc", [RuntimeError("secret-token-123"), KeyError("secret-token-123"), SystemExit(5)])
def test_invoke_hides_unexpected_exceptions(op_module: types.ModuleType, exc: BaseException) -> None:
    def run(ctx: OpContext, **kwargs: object) -> Result:
        raise exc

    op_module.run = run  # type: ignore[attr-defined]
    ctx = _Ctx(_inv())
    r = invoke(_spec(), ctx)
    assert r.error is not None and r.error.code == "E_INTERNAL_UNEXPECTED"
    assert "secret-token-123" not in json.dumps(r.to_dict())
    assert ctx.closed == 1


@pytest.mark.parametrize(
    ("exc", "code", "details"),
    [
        (ForgeAuthError("bad credentials ghp_secret", http_status=401), "E_AUTH_TOKEN_INVALID", {"kind": "auth", "status": 401}),
        (NetworkUnreachable("offline"), "E_NET_OFFLINE", {"kind": "unreachable"}),
        (BaseNotDefault("x", default_branch="trunk"), "E_CONFIG_BASE_NOT_DEFAULT", {"default_branch": "trunk"}),
    ],
)
def test_invoke_translates_forge_errors(
    op_module: types.ModuleType, exc: Exception, code: str, details: dict[str, object]
) -> None:
    # REQ-OPS-63: un ForgeError que la operación no tradujo sale por su kind, sin su texto
    def run(ctx: OpContext, **kwargs: object) -> Result:
        raise exc

    op_module.run = run  # type: ignore[attr-defined]
    r = invoke(_spec(), _Ctx(_inv()))
    assert r.error is not None and (r.error.code, r.error.details) == (code, details)
    assert "ghp_secret" not in json.dumps(r.to_dict())


def test_invoke_missing_module_and_bad_return(op_module: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _Ctx(_inv())
    r = invoke(_spec(module="ops._does_not_exist_xyz"), ctx)
    assert r.error is not None and r.error.code == "E_INTERNAL_UNEXPECTED" and ctx.closed == 1
    op_module.run = lambda ctx, **kw: {"not": "a result"}  # type: ignore[attr-defined]
    ctx2 = _Ctx(_inv())
    assert invoke(_spec(), ctx2).error.code == "E_INTERNAL_UNEXPECTED"  # type: ignore[union-attr]
    op_module.run = lambda ctx, **kw: ok("validate", {})  # type: ignore[attr-defined]
    assert invoke(_spec(), _Ctx(_inv())).error.code == "E_INTERNAL_UNEXPECTED"  # type: ignore[union-attr]


def test_invoke_close_error_is_internal(op_module: types.ModuleType) -> None:
    op_module.run = lambda ctx, **kw: ok("doctor", {})  # type: ignore[attr-defined]

    class Boom(OpContext):
        def close(self) -> None:
            raise OSError("disk full at /secret")

    r = invoke(_spec(), Boom(_inv()))
    assert r.error is not None and r.error.code == "E_INTERNAL_UNEXPECTED"
    assert "secret" not in json.dumps(r.to_dict())


def test_invoke_keyboard_interrupt_still_closes(op_module: types.ModuleType) -> None:
    def run(ctx: OpContext, **kwargs: object) -> Result:
        raise KeyboardInterrupt

    op_module.run = run  # type: ignore[attr-defined]
    ctx = _Ctx(_inv())
    with pytest.raises(KeyboardInterrupt):
        invoke(_spec(), ctx)
    assert ctx.closed == 1


@pytest.mark.parametrize(
    ("inv_kw", "code"),
    [
        ({"surface": "cli", "tty": True}, "E_HUMAN_ONLY_NOT_CONFIRMED"),
        ({"surface": "cli", "tty": False}, "E_HUMAN_ONLY_NO_TTY"),  # 06 §6, fase 1
        ({"surface": "mcp"}, "E_HUMAN_ONLY_NO_TTY"),
        ({"surface": "cli", "env": {"CLAUDE_CODE_CHILD_SESSION": "1"}}, "E_HUMAN_ONLY_AGENT_ENV"),
        ({"surface": "cli", "env": {"GITHUB_ACTIONS": "true"}}, "E_HUMAN_ONLY_AGENT_ENV"),
    ],
)
def test_invoke_human_only_barrier(op_module: types.ModuleType, inv_kw: dict[str, object], code: str) -> None:
    called: list[int] = []
    op_module.run = lambda ctx, **kw: called.append(1) or ok("task.accept", {})  # type: ignore[attr-defined]
    spec = _spec(id="task.accept", cli=("task", "accept"), human_only=True, effects="forge", offline="online")
    ctx = _Ctx(_inv(**inv_kw))
    r = invoke(spec, ctx)
    assert r.error is not None and r.error.code == code and r.exit_code() == 9
    assert called == [] and ctx.closed == 1
    confirmed = _Ctx(_inv(human_confirmed=True))
    assert invoke(spec, confirmed).ok and called == [1]


def test_load_op_module_is_relative_to_package(op_module: types.ModuleType) -> None:
    assert registry.load_op_module(_spec()) is op_module


def test_registry_module_does_not_import_argparse_eagerly() -> None:
    import ast

    tree = ast.parse(Path(registry.__file__).read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {a.name for n in top if isinstance(n, ast.Import) for a in n.names}
    assert "argparse" not in names
