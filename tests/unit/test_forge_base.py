# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Pruebas de la interfaz de la forja (08-forja §2 y §2.1, 00-marco D-34 h y su enmienda de D-45)."""

from __future__ import annotations

import ast
import copy
import dataclasses
import inspect
import json
import pickle
import re
import subprocess
import sys
import typing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aucove.forge import base as fb

MODULE_PATH = Path(fb.__file__).resolve()
# Nombre del paquete sin escribirlo: sale del propio módulo (REQ-ARCH-19)
PACKAGE_NAME = fb.__name__.split(".")[0]
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# Tabla de 08 §2.1: (clase, kind, retryable)
# ---------------------------------------------------------------------------

ERROR_TABLE: list[tuple[str, str, bool]] = [
    ("NoTokenError", "no_token", False),
    ("ForgeAuthError", "auth", False),
    ("ForgePermissionError", "permission", False),
    ("LoginChangedError", "login_changed", False),
    ("ForgeNotFound", "not_found", False),
    ("RefAlreadyExists", "ref_exists", False),
    ("RefConflict", "ref_conflict", False),
    ("MergeConflict", "merge_conflict", False),
    ("PullRequestExists", "pr_exists", False),
    ("NoCommitsBetween", "no_commits", False),
    ("ForgeValidationError", "validation", False),
    ("ForgeUnsupported", "unsupported", False),
    ("RateLimited", "rate_limited", True),
    ("ForgeUnavailable", "unavailable", True),
    ("NetworkTimeout", "timeout", True),
    ("NetworkUnreachable", "unreachable", True),
    ("NetworkTLSError", "tls", False),
]

# kinds que traduce 05-operaciones §12.3
OPS_TABLE_KINDS = {
    "no_token", "auth", "permission", "login_changed", "not_found", "ref_exists",
    "ref_conflict", "merge_conflict", "pr_exists", "no_commits", "validation",
    "unsupported", "rate_limited", "unavailable", "timeout", "unreachable", "tls",
}


def _ref() -> fb.Ref:
    return fb.Ref(branch="task/12", sha="a" * 40)


def _pull() -> fb.PullRequest:
    return fb.PullRequest(
        number=42, node_id="PR_x", title="t", body="b", state="open", draft=False,
        merged=False, merged_at=None, head_branch="task/12", head_sha="b" * 40,
        base_branch="main", author="carla", labels=(), requested_reviewers=(),
        updated_at=NOW, url="https://example.invalid/pr/42",
    )


def _workflow_run() -> fb.WorkflowRun:
    return fb.WorkflowRun(
        id=7, name="main-broken", event="push", status="completed", conclusion="success",
        head_sha="c" * 40, head_branch="main", actor="ana", url="https://example.invalid/run/7",
    )


def _extras(name: str) -> dict[str, object]:
    # Argumentos obligatorios de las excepciones con atributos extra
    extras: dict[str, dict[str, object]] = {
        "RefAlreadyExists": {"existing": _ref()},
        "PullRequestExists": {"existing": _pull()},
        "RateLimited": {"reset_at": NOW},
        "BaseNotDefault": {"default_branch": "main"},
    }
    return extras.get(name, {})


def _make(name: str, message: str = "boom", **kw: object) -> fb.ForgeError:
    cls = getattr(fb, name)
    return cls(message, **{**_extras(name), **kw})


# ---------------------------------------------------------------------------
# Errores
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "kind", "retryable"), ERROR_TABLE)
def test_each_error_has_its_kind_and_retryable(name: str, kind: str, retryable: bool) -> None:
    cls = getattr(fb, name)
    assert issubclass(cls, fb.ForgeError)
    assert issubclass(cls, Exception)
    assert cls.kind == kind
    assert cls.retryable is retryable
    err = _make(name, hint="pista", http_status=422)
    assert err.kind == kind
    assert err.retryable is retryable
    assert err.message == "boom"
    assert err.hint == "pista"
    assert err.http_status == 422
    assert str(err) == "boom"
    with pytest.raises(fb.ForgeError) as info:
        raise err
    assert info.value is err


@pytest.mark.parametrize(("name", "kind", "retryable"), ERROR_TABLE)
def test_error_defaults(name: str, kind: str, retryable: bool) -> None:
    err = getattr(fb, name)(**_extras(name))
    assert err.message == ""
    assert err.http_status is None
    assert err.hint is None
    assert str(err) == kind  # sin mensaje, str() cae al kind


def test_error_classes_follow_the_table() -> None:
    assert [c.__name__ for c in fb.ERROR_CLASSES] == [row[0] for row in ERROR_TABLE]
    assert dict(fb.RETRYABLE_BY_KIND) == {kind: r for _, kind, r in ERROR_TABLE}
    assert set(fb.RETRYABLE_BY_KIND) == OPS_TABLE_KINDS
    assert len({c.kind for c in fb.ERROR_CLASSES}) == len(fb.ERROR_CLASSES)
    with pytest.raises(TypeError):
        fb.RETRYABLE_BY_KIND["x"] = True  # type: ignore[index]


def test_base_not_default_is_validation_error() -> None:
    assert issubclass(fb.BaseNotDefault, fb.ForgeValidationError)
    assert issubclass(fb.BaseNotDefault, fb.ForgeError)
    err = fb.BaseNotDefault(default_branch="trunk")
    assert err.kind == "validation"
    assert err.retryable is False
    assert err.default_branch == "trunk"
    assert isinstance(err, fb.ForgeValidationError)
    with pytest.raises(fb.ForgeValidationError):
        raise err
    with pytest.raises(TypeError):
        fb.BaseNotDefault()  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        fb.BaseNotDefault(default_branch="")


def test_error_extras() -> None:
    ref = _ref()
    assert fb.RefAlreadyExists(existing=ref).existing is ref
    pr = _pull()
    assert fb.PullRequestExists(existing=pr).existing is pr
    assert fb.RefConflict().current is None
    assert fb.RefConflict(current="d" * 40).current == "d" * 40
    reset = NOW + timedelta(minutes=5)
    assert fb.RateLimited(reset_at=reset, http_status=429).reset_at == reset


def test_error_extras_are_checked() -> None:
    with pytest.raises(TypeError):
        fb.RefAlreadyExists()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        fb.RefAlreadyExists(existing="task/12")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        fb.RefAlreadyExists(_ref())  # type: ignore[arg-type, call-arg]  # el mensaje va primero
    with pytest.raises(TypeError):
        fb.PullRequestExists(existing=_ref())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        fb.RefConflict(current=5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        fb.RateLimited()  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        fb.RateLimited(reset_at=datetime(2026, 9, 29, 12, 0))  # sin zona horaria
    with pytest.raises(ValueError):
        fb.RateLimited(reset_at="2026-09-29T12:00:00Z")  # type: ignore[arg-type]


def test_subclasses_cannot_override_kind() -> None:
    with pytest.raises(TypeError):
        fb.ForgeAuthError("x", kind="tls")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        fb.ForgeAuthError("x", retryable=True)  # type: ignore[call-arg]


def test_forge_error_base_signature() -> None:
    # Firma de 08 §2.1: ForgeError(kind, message, retryable, http_status, hint)
    err = fb.ForgeError("auth", "bad credentials", False, 401, "renueva el token")
    assert (err.kind, err.message, err.retryable, err.http_status, err.hint) == (
        "auth", "bad credentials", False, 401, "renueva el token",
    )
    assert fb.ForgeError("timeout", "slow").retryable is True  # retryable sale de la tabla
    assert not isinstance(err, fb.ForgeAuthError)


@pytest.mark.parametrize(
    ("args", "kwargs", "exc"),
    [
        (("bogus", "m"), {}, ValueError),
        (("auth", "m", True), {}, ValueError),
        (("rate_limited", "m", False), {}, ValueError),
        (("auth", 3), {}, TypeError),
        (("auth", "m"), {"http_status": 99}, ValueError),
        (("auth", "m"), {"http_status": 600}, ValueError),
        (("auth", "m"), {"http_status": True}, ValueError),
        (("auth", "m"), {"http_status": "401"}, ValueError),
        (("auth", "m"), {"hint": 1}, TypeError),
    ],
)
def test_forge_error_base_validation(args: tuple, kwargs: dict, exc: type[Exception]) -> None:
    with pytest.raises(exc):
        fb.ForgeError(*args, **kwargs)


def test_error_repr_has_no_hint_leak() -> None:
    err = fb.ForgeNotFound("missing", http_status=404, hint="h")
    assert repr(err) == "ForgeNotFound(kind='not_found', message='missing', http_status=404)"


@pytest.mark.parametrize("name", [row[0] for row in ERROR_TABLE] + ["BaseNotDefault"])
def test_errors_survive_pickle_and_copy(name: str) -> None:
    err = _make(name, "msg", http_status=503, hint="pista")
    for clone in (pickle.loads(pickle.dumps(err)), copy.copy(err), copy.deepcopy(err)):
        assert type(clone) is type(err)
        assert clone.__dict__ == err.__dict__
        assert clone.args == err.args
        assert str(clone) == str(err)


def test_base_error_survives_pickle() -> None:
    err = fb.ForgeError("unavailable", "down", True, 502, None)
    clone = pickle.loads(pickle.dumps(err))
    assert type(clone) is fb.ForgeError
    assert clone.__dict__ == err.__dict__


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

# Miembros de 08 §2 más los dos de D-34 (h)
SPEC_MEMBERS = {
    "repo",
    "identity", "capabilities",
    "server_time",  # desde H3: la cabecera Date, para expire-claims (05 §9.19)
    "get_ref", "list_refs", "create_ref", "update_ref", "delete_ref", "list_ref_activity",
    "create_empty_commit", "commit_files", "merge_into", "get_commit", "get_file", "compare",
    "codeowners_errors",
    "get_issue", "list_issues", "create_issue", "update_issue", "add_labels", "remove_label",
    "ensure_labels", "list_issue_events", "set_assignees", "add_assignees", "remove_assignees",
    "list_comments", "create_comment", "update_comment",
    "get_pull_request", "find_pull_request", "list_pull_requests", "list_pull_request_files",
    "list_pull_request_commits", "create_pull_request", "update_pull_request",
    "mark_ready_for_review", "request_reviewers", "list_reviews", "submit_review", "get_checks",
    "get_workflow_run", "successful_runs",  # successful_runs: D-34, enmienda de D-45 (ci seal)
    "upsert_check_run",  # desde H3 (D-56, REQ-FORGE-70)
}
D34H_MEMBERS = {"pulls_for_commit", "last_successful_run"}
# 08 §2 (00-marco D-65; REQ-FORGE-73): las lecturas de la configuración, solo GET, para doctor, con
# el environment de la firma del proyecto (00-marco D-72)
D65_READS = {
    "repo_settings", "rulesets", "list_labels", "collaborator_permission", "outside_collaborators",
    "actions_policy", "workflow_permissions", "org_settings", "environment",
}
# 08 §2.2 (00-marco D-62): la interfaz de administración que solo construye init
ADMIN_MEMBERS = {
    "repo", "identity", "repo_settings", "update_repo_settings", "actions_policy", "set_actions_policy",
    "workflow_permissions", "set_workflow_permissions", "retention_days", "set_retention_days",
    "collaborator_permission", "add_collaborator", "outside_collaborators", "user_id", "org_settings",
    "ruleset", "upsert_ruleset", "branch_rules",
    "environment", "put_environment", "secret_names",  # 06 §4.3b (00-marco D-72)
}


def _protocol_methods() -> set[str]:
    return {
        n for n, v in vars(fb.ForgeAdapter).items()
        if not n.startswith("_") and inspect.isfunction(v)
    }


def _protocol_members() -> set[str]:
    return _protocol_methods() | {
        n for n in fb.ForgeAdapter.__annotations__ if not n.startswith("_")
    }


def test_protocol_members_are_exactly_the_spec() -> None:
    assert _protocol_members() == SPEC_MEMBERS | D34H_MEMBERS | D65_READS


def test_protocol_has_no_merge_delete_or_settings_methods() -> None:
    # REQ-FORGE-3: ni fusionar PR, ni borrar Issues, ni cambiar ajustes del repositorio; desde D-65
    # sí lee la configuración (las lecturas de D65_READS), pero sin escribirla
    members = _protocol_members() - D65_READS
    forbidden = re.compile(
        r"merge_(pull|pr)|^merge$|squash|rebase|delete_(issue|comment|label|repo)"
        r"|(^|_)(settings?|config|protection|visibility|default_branch|collaborator|webhook"
        r"|secret|transfer|archive_repo)(_|$)|update_repo|edit_repo|set_repo|delete_repo"
    )
    assert sorted(m for m in members if forbidden.search(m)) == []
    # Las únicas escrituras con 'merge' o 'delete' son merge_into (ramas) y delete_ref
    assert {m for m in members if "merge" in m} == {"merge_into"}
    assert {m for m in members if "delete" in m} == {"delete_ref"}
    # Ninguna escritura de configuración: están en ForgeAdmin (08 §2.2; 00-marco D-62)
    assert {m for m in members if "ruleset" in m} == set()
    assert not members & (ADMIN_MEMBERS - {"repo", "identity"})
    # Las lecturas de D-65 que también tiene ForgeAdmin conservan su nombre (mismas firmas)
    assert D65_READS & ADMIN_MEMBERS == D65_READS - {"rulesets", "list_labels"}


def test_admin_protocol_members_are_exactly_the_spec() -> None:
    methods = {n for n, v in vars(fb.ForgeAdmin).items() if not n.startswith("_") and inspect.isfunction(v)}
    assert methods | set(fb.ForgeAdmin.__annotations__) == ADMIN_MEMBERS
    for name in sorted(methods):
        assert "return" in typing.get_type_hints(getattr(fb.ForgeAdmin, name)), name
    assert set(fb.REPO_SETTINGS) >= {"allow_squash_merge", "squash_merge_commit_message", "allow_auto_merge"}


def test_new_methods_signatures() -> None:
    sig = inspect.signature(fb.ForgeAdapter.pulls_for_commit)
    assert list(sig.parameters) == ["self", "sha"]
    hints = typing.get_type_hints(fb.ForgeAdapter.pulls_for_commit)
    assert hints == {"sha": str, "return": list[fb.PullRequest]}

    sig = inspect.signature(fb.ForgeAdapter.last_successful_run)
    assert list(sig.parameters) == ["self", "workflow_file", "branch"]
    hints = typing.get_type_hints(fb.ForgeAdapter.last_successful_run)
    assert hints == {"workflow_file": str, "branch": str, "return": fb.WorkflowRun | None}


def test_successful_runs_signature() -> None:
    """08 §2 (D-45): ``event`` y ``limit`` solo por nombre; ``limit`` vale 100 por omisión."""
    sig = inspect.signature(fb.ForgeAdapter.successful_runs)
    assert list(sig.parameters) == ["self", "workflow_file", "branch", "event", "limit"]
    assert sig.parameters["event"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["limit"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["event"].default is inspect.Parameter.empty
    assert sig.parameters["limit"].default == 100
    hints = typing.get_type_hints(fb.ForgeAdapter.successful_runs)
    assert hints == {"workflow_file": str, "branch": str, "event": str, "limit": int, "return": list[fb.WorkflowRun]}


@pytest.mark.parametrize("name", sorted(SPEC_MEMBERS - {"repo"} | D34H_MEMBERS))
def test_protocol_type_hints_resolve(name: str) -> None:
    hints = typing.get_type_hints(getattr(fb.ForgeAdapter, name))
    assert "return" in hints


def test_protocol_repo_annotation() -> None:
    assert typing.get_type_hints(fb.ForgeAdapter)["repo"] is fb.RepoRef


def _dummy_adapter(skip: str | None = None) -> type:
    ns: dict[str, object] = {}
    for name in _protocol_methods():
        if name != skip:
            ns[name] = lambda self, *a, **k: None
    cls = type("Dummy", (), ns)
    return cls


def test_protocol_is_runtime_checkable() -> None:
    obj = _dummy_adapter()()
    obj.repo = fb.RepoRef(owner="o", name="r")
    assert isinstance(obj, fb.ForgeAdapter)


@pytest.mark.parametrize("missing", sorted(D34H_MEMBERS | {"get_ref", "ensure_labels", "successful_runs"}))
def test_protocol_rejects_incomplete_adapter(missing: str) -> None:
    obj = _dummy_adapter(skip=missing)()
    obj.repo = fb.RepoRef(owner="o", name="r")
    assert not isinstance(obj, fb.ForgeAdapter)


def test_protocol_requires_repo() -> None:
    assert not isinstance(_dummy_adapter()(), fb.ForgeAdapter)


# ---------------------------------------------------------------------------
# Datos
# ---------------------------------------------------------------------------

FIELDS: dict[str, list[str]] = {
    "RepoRef": ["owner", "name"],
    "Identity": ["login", "id", "kind", "token_source", "token_kind", "token_scope",
                 "oauth_scopes", "previous_login", "fetched_at"],
    "Label": ["name", "color", "description"],
    "Ref": ["branch", "sha"],
    "RefActivity": ["activity_type", "actor", "timestamp", "before", "after"],
    "CommitInfo": ["sha", "message", "author_login", "author_date", "committer_date"],
    "Issue": ["number", "title", "body", "state", "state_reason", "labels", "assignees",
              "author", "created_at", "updated_at", "closed_at", "url"],
    "LabelEvent": ["id", "event", "label", "actor", "created_at"],
    "PullRequest": ["number", "node_id", "title", "body", "state", "draft", "merged",
                    "merged_at", "head_branch", "head_sha", "base_branch", "author", "labels",
                    "requested_reviewers", "updated_at", "url", "same_repo", "merged_by",
                    "created_at", "merge_commit_sha"],  # created_at: 04 §1.1 y 12-mvp §5.3; merge_commit_sha: 11 §4.3
    "Review": ["id", "author", "author_id", "state", "commit_sha", "submitted_at"],
    "Comment": ["id", "author", "body", "created_at", "updated_at", "url"],
    "FileContent": ["path", "ref", "blob_sha", "content"],
    "FileChange": ["path", "content", "executable"],
    "ChangedFile": ["path", "status", "previous_path", "additions", "deletions"],
    "Comparison": ["base", "head", "status", "ahead_by", "behind_by", "merge_base_sha",
                   "files", "files_truncated", "commits"],
    "CheckRun": ["name", "status", "conclusion", "source", "url"],
    "Checks": ["sha", "overall", "runs"],
    "WorkflowRun": ["id", "name", "event", "status", "conclusion", "head_sha", "head_branch",
                    "actor", "url", "created_at"],  # created_at: doctor, 09-ci REQ-CI-31 (D-65)
    "BaseRules": ["source", "required_approvals", "require_code_owner_review",
                  "require_last_push_approval", "dismiss_stale_reviews_on_push",
                  "required_checks", "blocks_force_push"],
    "Capabilities": ["forge", "repo", "visibility", "owner_type", "viewer_permission",
                     "default_branch", "rules_available", "code_owners_enforceable",
                     "base_rules", "profile", "push_rulesets", "checks_api", "transport",
                     "write_mode", "detected_at", "evidence"],
}


@pytest.mark.parametrize("name", sorted(FIELDS))
def test_dataclass_fields_frozen_and_slots(name: str) -> None:
    cls = getattr(fb, name)
    assert dataclasses.is_dataclass(cls)
    assert [f.name for f in dataclasses.fields(cls)] == FIELDS[name]
    assert vars(cls)["__dataclass_params__"].frozen is True
    assert "__slots__" in vars(cls)
    hints = typing.get_type_hints(cls)
    assert list(hints) == FIELDS[name]


def test_dataclass_defaults() -> None:
    assert fb.Label("task") == fb.Label("task", "ededed", "")
    assert fb.FileChange("a.tex", None).executable is False
    assert fb.ChangedFile("a.tex", "added").previous_path is None
    cmp_ = fb.Comparison("b", "h", "ahead", 1, 0, "m" * 40, (), False)
    assert cmp_.commits == ()
    # same_repo (04-tareas §1.1, REQ-TASK-5) va al final con valor por defecto: los
    # campos de 08 §2 se siguen pasando en orden, y un fork se dice con False
    assert _pull().same_repo is True
    assert typing.get_type_hints(fb.PullRequest)["same_repo"] is bool
    fork = dataclasses.replace(_pull(), same_repo=False)
    assert fork.same_repo is False and fork != _pull()
    assert fb.PullRequestExists(existing=fork).existing.same_repo is False
    # merged_by (08 §2, D-45) también va al final, con None por omisión: las listas no lo traen
    assert _pull().merged_by is None
    assert typing.get_type_hints(fb.PullRequest)["merged_by"] == (str | None)
    merged = dataclasses.replace(_pull(), state="closed", merged=True, merged_at=NOW, merged_by="ana")
    assert merged.merged_by == "ana" and merged != dataclasses.replace(merged, merged_by=None)
    # created_at (08 §2) también va al final, con None por omisión
    assert _pull().created_at is None
    assert typing.get_type_hints(fb.PullRequest)["created_at"] == (datetime | None)
    assert _pull().merge_commit_sha is None  # 11 §4.3, también al final


def test_dataclass_instances_are_immutable_and_hashable() -> None:
    ref = _ref()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ref.sha = "x"  # type: ignore[misc]
    assert not hasattr(ref, "__dict__")
    assert hash(ref) == hash(fb.Ref("task/12", "a" * 40))
    run = _workflow_run()
    assert pickle.loads(pickle.dumps(run)) == run
    assert copy.deepcopy(_pull()) == _pull()


def test_type_aliases() -> None:
    assert typing.get_args(fb.ForgeKind) == ("github", "gitlab", "forgejo", "fake")
    assert typing.get_args(fb.RefNamespace) == ("heads", "tags")
    assert set(typing.get_args(fb.TokenKind)) == {
        "fine_grained_pat", "classic_pat", "oauth", "app_user", "app_installation",
        "proxy", "unknown",
    }
    assert fb.Tristate == (bool | None)
    assert fb.NULL_SHA == "0" * 40


def test_token_sources_order() -> None:
    prefix = PACKAGE_NAME.upper() + "_"
    assert fb.token_sources(prefix) == (
        prefix + "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "gh_auth_token",
    )
    assert fb.token_sources("X_")[0] == "X_GITHUB_TOKEN"


@pytest.mark.parametrize("bad", ["", "_", "abc_", "ABC", "1A_", "A-B_", None])
def test_token_sources_rejects_bad_prefix(bad: object) -> None:
    with pytest.raises(ValueError):
        fb.token_sources(bad)  # type: ignore[arg-type]


def test_exports() -> None:
    for name in fb.__all__:
        assert hasattr(fb, name), name
    exported = set(fb.__all__)
    assert {row[0] for row in ERROR_TABLE} <= exported
    assert set(FIELDS) <= exported
    assert {"ForgeAdapter", "ForgeError", "BaseNotDefault", "HumanConfirmation"} <= exported


# ---------------------------------------------------------------------------
# Dependencias, nombre y formato del archivo
# ---------------------------------------------------------------------------

FORBIDDEN_IMPORTS = {"httpx", "httpx2", "truststore"}


def _imported_top_modules(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names.add("." * node.level + (node.module or ""))
            elif node.module:
                names.add(node.module.split(".")[0])
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Name, ast.Attribute))
            and getattr(node.func, "id", getattr(node.func, "attr", "")) in {
                "__import__", "import_module",
            }
        ):
            names.add("<dynamic import>")
    return names


def test_module_imports_only_stdlib() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported = _imported_top_modules(tree)
    assert not imported & FORBIDDEN_IMPORTS
    assert "<dynamic import>" not in imported
    assert not [m for m in imported if m.startswith(".")]  # ni siquiera módulos propios
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}


_PROBE = r"""
import importlib.util, json, sys
path = sys.argv[1]
before = set(sys.modules)
spec = importlib.util.spec_from_file_location("forge_base_probe", path)
mod = importlib.util.module_from_spec(spec)
sys.modules["forge_base_probe"] = mod
spec.loader.exec_module(mod)
new = sorted({m.split(".")[0] for m in set(sys.modules) - before} - {"forge_base_probe"})
print(json.dumps({"new": new, "stdlib": sorted(sys.stdlib_module_names)}))
"""


def _run_probe(*flags: str) -> dict[str, list[str]]:
    proc = subprocess.run(
        [sys.executable, "-B", *flags, "-c", _PROBE, str(MODULE_PATH)],  # -B: sin .pyc
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_import_does_not_load_http_clients() -> None:
    out = _run_probe("-I")
    new = set(out["new"])
    assert not new & FORBIDDEN_IMPORTS
    assert new <= set(out["stdlib"])


def test_module_imports_without_site_packages() -> None:
    # Con -S no hay site-packages: solo puede importar la biblioteca estándar
    out = _run_probe("-I", "-S")
    assert not set(out["new"]) & FORBIDDEN_IMPORTS


def test_no_name_literal_in_module() -> None:
    # REQ-ARCH-19: el nombre solo lo escribe _meta.py (tampoco en mayúsculas)
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    offenders = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and PACKAGE_NAME.lower() in node.value.lower()
    ]
    assert offenders == []


@pytest.mark.parametrize("path", [MODULE_PATH, Path(__file__).resolve()])
def test_file_conventions(path: Path) -> None:
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw
    assert raw.endswith(b"\n") and not raw.endswith(b"\n\n")
    raw.decode("utf-8")
    tag = "SPDX" + "-"
    lines = raw.decode("utf-8").splitlines()
    assert lines[0] == f"# {tag}FileCopyrightText: 2026 The {PACKAGE_NAME} Authors"
    assert lines[1] == f"# {tag}License-Identifier: Apache-2.0"
