# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Motor de ``validate`` con reglas falsas (09-ci §4.1, REQ-CI-8, REQ-CI-26, REQ-CI-45).

Las pruebas usan ``load_rules(modules=[...])`` con módulos falsos en ``sys.modules`` y
``evaluate`` con ``RuleSpec`` propios: no dependen de las reglas reales.
"""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

import pytest

from aucove import _meta
from aucove.config.base import BaseSnapshot
from aucove.config.project import parse_project
from aucove.config.roles import parse_roles
from aucove.config.schemas import validate_result
from aucove.domain.findings import Finding
from aucove.i18n import Translator, load_catalog, placeholders, translator
from aucove.ops import validate
from aucove.ops.result import OpError, Result, dumps, fail, make_error, ok
from aucove.ops.rules.base import Change, RuleInput, RuleSpec, make_finding
from aucove.ops.rules.catalog import PIN_RULES, finding_params, load_rules
from aucove.vcs.tree import is_regular_mode
from tests.conftest import roles_fixture
from tests.unit.test_ci_build_plan import project_yaml

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
SHA = "0123456789abcdef0123456789abcdef01234567"
ALL_MODES = frozenset({"tree", "pr", "base"})


class MemTree:
    """``TreeView`` en memoria."""

    def __init__(self, files: Mapping[str, str | bytes] = (), *, modes: Mapping[str, str] | None = None,
                 root: Path | None = None, sha: str | None = SHA) -> None:
        self._data = {p: v.encode("utf-8") if isinstance(v, str) else v for p, v in dict(files).items()}
        self._modes = {p: "100644" for p in self._data}
        self._modes.update(modes or {})
        self.root = root
        self.sha = sha

    def entries(self) -> Mapping[str, str]:
        return MappingProxyType(dict(self._modes))

    def read_bytes(self, path: str, limit: int = 5 * 2**20) -> bytes | None:
        data = self._data.get(path)
        if not is_regular_mode(self._modes.get(path)) or data is None or len(data) > limit:
            return None
        return data

    def read_text(self, path: str, limit: int = 5 * 2**20) -> str | None:
        data = self.read_bytes(path, limit)
        try:
            return data.decode("utf-8") if data is not None else None
        except UnicodeDecodeError:
            return None


def make_input(mode: str = "tree", *, t: Translator | None = None, tree: object = "default", **kw: object) -> RuleInput:
    return RuleInput(
        mode=mode,  # type: ignore[arg-type]
        t=t or translator("en"),
        now=NOW,
        tree=MemTree() if tree == "default" else tree,  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


class Recorder:
    """Reglas falsas que registran el orden de ejecución y lo que recibieron."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.seen: dict[str, RuleInput] = {}

    def rule(self, rule_id: str, *, modes: frozenset[str] = ALL_MODES, after: tuple[str, ...] = (),
             errors: int = 0, warnings: int = 0, tools: tuple[str, ...] = (), needs_forge: bool = False,
             run: Callable[[RuleInput], Sequence[Finding]] | None = None) -> RuleSpec:
        codes = {"E_VALIDATION_STUB": frozenset({"n"}), "W_VALIDATION_STUB": frozenset({"n"})}

        def default_run(inp: RuleInput) -> Sequence[Finding]:
            out = [make_finding(spec, inp, "E_VALIDATION_STUB", severity="error", path=f"e{i}.txt", n=i)
                   for i in range(errors)]
            out += [make_finding(spec, inp, "W_VALIDATION_STUB", severity="warning", path=None, n=i)
                    for i in range(warnings)]
            return out

        def wrapped(inp: RuleInput) -> Sequence[Finding]:
            self.calls.append(rule_id)
            self.seen[rule_id] = inp
            return (run or default_run)(inp)

        spec = RuleSpec(id=rule_id, modes=frozenset(modes), run=wrapped, codes=codes, after=after,  # type: ignore[arg-type]
                        tools=tools, needs_forge=needs_forge)
        return spec


def statuses(outcomes: Sequence[validate.RuleOutcome]) -> dict[str, str]:
    return {o.check: o.status for o in outcomes}


# -- load_rules y finding_params ------------------------------------------------------


def _install(monkeypatch: pytest.MonkeyPatch, name: str, **attrs: object) -> str:
    module = types.ModuleType(f"{_meta.NAME}.{name}")
    for key, value in attrs.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, f"{_meta.NAME}.{name}", module)
    return name


def test_load_rules_keeps_module_order(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = Recorder()
    a = _install(monkeypatch, "_p15_stub_a", RULES=(rec.rule("roles-sync"), rec.rule("generated-sync")))
    b = _install(monkeypatch, "_p15_stub_b", RULES=(rec.rule("schemas"),))
    rules = load_rules([b, a, b])
    assert [r.id for r in rules] == ["schemas", "roles-sync", "generated-sync"]


@pytest.mark.parametrize(
    "attrs",
    [
        {},  # sin RULES
        {"RULES": ()},  # vacío
        {"RULES": ["no es tupla"]},
        {"RULES": ("no es RuleSpec",)},
    ],
)
def test_load_rules_fails_closed_without_rules(monkeypatch: pytest.MonkeyPatch, attrs: dict[str, object]) -> None:
    name = _install(monkeypatch, "_p15_stub_bad", **attrs)
    with pytest.raises(ImportError):
        load_rules([name])


def test_load_rules_fails_closed_with_missing_module() -> None:
    with pytest.raises(ImportError):
        load_rules(["ops.rules._does_not_exist"])


def test_load_rules_rejects_invalid_declarations(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = RuleSpec(id="Bad_Id", modes=frozenset({"tree"}), run=lambda inp: [], codes={})
    name = _install(monkeypatch, "_p15_stub_decl", RULES=(bad,))
    with pytest.raises(ImportError):
        load_rules([name])
    bad_mode = RuleSpec(id="ok", modes=frozenset({"nope"}), run=lambda inp: [], codes={})  # type: ignore[arg-type]
    name = _install(monkeypatch, "_p15_stub_mode", RULES=(bad_mode,))
    with pytest.raises(ImportError):
        load_rules([name])


def test_load_rules_real_modules_and_finding_params() -> None:
    rules = load_rules()
    ids = validate.rule_ids(rules)
    assert {"schemas", "decisions-immutable", "ci-policy", "secrets"} <= set(ids)
    params = finding_params(rules)
    assert params["E_INTERNAL_UNEXPECTED"] == frozenset({"rule", "reason"})
    assert params["E_VALIDATION_SECRET"] == frozenset({"rule_id", "commit"})


def test_finding_params_detects_conflicting_placeholders() -> None:
    rec = Recorder()
    one = rec.rule("one")
    other = RuleSpec(id="two", modes=frozenset({"tree"}), run=lambda inp: [],
                     codes={"E_VALIDATION_STUB": frozenset({"other"})})
    with pytest.raises(ValueError):
        finding_params([one, other])
    clash = RuleSpec(id="three", modes=frozenset({"tree"}), run=lambda inp: [],
                     codes={"E_INTERNAL_UNEXPECTED": frozenset({"x"})})
    with pytest.raises(ValueError):
        finding_params([clash])


# -- selección y orden ------------------------------------------------------------------


def test_after_runs_dependencies_first_without_listing_them() -> None:
    rec = Recorder()
    rules = [
        rec.rule("scope", modes=frozenset({"pr"}), after=("roles-sync", "generated-sync")),
        rec.rule("schemas"),
        rec.rule("roles-sync"),
        rec.rule("generated-sync", errors=1),
    ]
    out = validate.evaluate(rules, make_input("pr"), checks=["scope"])
    assert [o.check for o in out] == ["scope"]
    assert rec.calls == ["roles-sync", "generated-sync", "scope"]
    assert dict(rec.seen["scope"].prior) == {"roles-sync": "pass", "generated-sync": "fail"}
    assert dict(rec.seen["roles-sync"].prior) == {}


def test_dependency_that_cannot_run_has_fail_prior() -> None:
    rec = Recorder()
    rules = [
        rec.rule("gate", after=("only-base", "ghost")),
        rec.rule("only-base", modes=frozenset({"base"})),
    ]
    out = validate.evaluate(rules, make_input("tree"))
    assert statuses(out) == {"gate": "pass"}  # only-base no corre en tree y no se lista
    assert dict(rec.seen["gate"].prior) == {"only-base": "fail", "ghost": "fail"}
    assert "only-base" not in rec.calls


def test_dependency_cycle_terminates_and_closes_with_fail() -> None:
    rec = Recorder()
    rules = [rec.rule("a", after=("b",)), rec.rule("b", after=("a",))]
    out = validate.evaluate(rules, make_input("tree"))
    assert [o.check for o in out] == ["a", "b"]
    assert sorted(rec.calls) == ["a", "b"]
    assert rec.seen["b"].prior["a"] == "fail"  # 'a' estaba en curso cuando corrió 'b'


def test_unknown_check_is_input_error() -> None:
    rec = Recorder()
    with pytest.raises(OpError) as info:
        validate.evaluate([rec.rule("schemas")], make_input("tree"), checks=["schemas", "nope"])
    assert info.value.code == "E_VALIDATION_INPUT"
    assert info.value.details == {"errors": [{"param": "checks", "reason": "unknown", "index": 1}]}
    assert rec.calls == []


def test_selection_order_and_skip_outside_mode() -> None:
    rec = Recorder()
    rules = [rec.rule("scope", modes=frozenset({"pr"})), rec.rule("schemas"), rec.rule("latex-log", modes=frozenset({"build-log"}))]
    out = validate.evaluate(rules, make_input("tree"), checks=["latex-log", "schemas", "scope"])
    assert [(o.check, o.status) for o in out] == [("scope", "skip"), ("schemas", "pass"), ("latex-log", "skip")]
    assert rec.calls == ["schemas"]
    # sin --check corren todas las del modo, en el orden de los módulos
    rec2 = Recorder()
    rules2 = [rec2.rule("scope", modes=frozenset({"pr"})), rec2.rule("schemas"), rec2.rule("ci-policy")]
    assert [o.check for o in validate.evaluate(rules2, make_input("tree"))] == ["schemas", "ci-policy"]


def test_rules_sharing_an_id_are_merged() -> None:
    rec = Recorder()
    rules = [rec.rule("convention", warnings=1), rec.rule("schemas"), rec.rule("convention", errors=1)]
    out = validate.evaluate(rules, make_input("tree"))
    assert [o.check for o in out] == ["convention", "schemas"]
    conv = out[0]
    assert conv.status == "fail" and len(conv.findings) == 2


# -- format: none (00-marco D-33 y D-43; 09-ci §4.1) ------------------------------------


def _snapshot(fmt: str) -> BaseSnapshot:
    cfg, problems = parse_project(project_yaml(fmt=fmt))
    roles, _ = parse_roles(roles_fixture("basic"))
    assert cfg is not None and roles is not None, problems
    return BaseSnapshot(ref="origin/main", sha=None, project=cfg, roles=roles, decisions=frozenset(),
                        fallback_sha=None, source="local")


def _latex_rules(rec: Recorder) -> list[RuleSpec]:
    return [
        rec.rule("schemas"),
        rec.rule("citations"),
        rec.rule("labels-refs"),
        rec.rule("latex-macros"),
        rec.rule("convention"),
        rec.rule("latex-log", modes=frozenset({"build-log"})),
    ]


def test_latex_rules_skip_without_manuscript() -> None:
    rec = Recorder()
    tree = MemTree({"project.yaml": project_yaml(fmt="none")})
    out = validate.evaluate(_latex_rules(rec), make_input("tree", tree=tree))
    assert statuses(out) == {
        "schemas": "pass", "citations": "skip", "labels-refs": "skip", "latex-macros": "skip", "convention": "pass",
    }
    assert rec.calls == ["schemas", "convention"]  # las de LaTeX ni siquiera se ejecutan
    assert all(o.findings == () for o in out if o.status == "skip")
    checks = ["latex-log", "citations"]
    checked = validate.evaluate(_latex_rules(Recorder()), make_input("tree", tree=tree), checks=checks)
    assert [(o.check, o.status) for o in checked] == [("citations", "skip"), ("latex-log", "skip")]


def test_latex_rules_run_with_latex_or_without_a_format() -> None:
    for files in ({"project.yaml": project_yaml(fmt="latex")}, {}, {"project.yaml": "format: none\n"}):
        rec = Recorder()
        out = validate.evaluate(_latex_rules(rec), make_input("tree", tree=MemTree(files)))
        assert {o.status for o in out} == {"pass"}, files  # un project.yaml inválido no salta reglas
        assert rec.calls == ["schemas", "citations", "labels-refs", "latex-macros", "convention"]


def test_without_a_tree_the_format_comes_from_the_policy() -> None:
    """PR desde un fork con format: none en la base: las reglas de LaTeX dan skip, no fallan."""
    out = validate.evaluate(_latex_rules(Recorder()), make_input("pr", tree=None, policy=_snapshot("none")))
    assert statuses(out)["citations"] == "skip" and statuses(out)["schemas"] == "fail"
    latex = validate.evaluate(_latex_rules(Recorder()), make_input("pr", tree=None, policy=_snapshot("latex")))
    assert statuses(latex)["citations"] == "fail"  # sin árbol, una regla de contenido falla cerrada


def test_build_log_mode_never_skips_by_format() -> None:
    assert validate.without_manuscript(make_input("build-log", tree=None)) is False
    rec = Recorder()
    out = validate.evaluate(_latex_rules(rec), make_input("build-log", tree=None))
    assert statuses(out) == {"latex-log": "pass"} and rec.calls == ["latex-log"]


# -- falla cerrada (REQ-CI-8) ---------------------------------------------------------


def test_rule_exception_fails_closed_without_its_text(t_es: Translator) -> None:
    rec = Recorder()

    def boom(inp: RuleInput) -> Sequence[Finding]:
        raise RuntimeError("token-secreto-123")

    out = validate.evaluate([rec.rule("schemas", run=boom)], make_input("tree", t=t_es))
    (result,) = out
    assert result.status == "fail"
    (finding,) = result.findings
    assert finding.code == "E_INTERNAL_UNEXPECTED" and finding.severity == "error"
    assert "token-secreto-123" not in json.dumps(result.to_dict(), ensure_ascii=False)
    assert "schemas" in finding.message and t_es("validate.reason.exception") in finding.message


def test_rule_returning_garbage_fails_closed() -> None:
    rec = Recorder()
    out = validate.evaluate([rec.rule("schemas", run=lambda inp: ["no es un hallazgo"])], make_input())  # type: ignore[list-item]
    assert out[0].status == "fail" and out[0].findings[0].code == "E_INTERNAL_UNEXPECTED"


def test_rule_needing_forge_without_forge_fails_closed() -> None:
    rec = Recorder()
    out = validate.evaluate([rec.rule("scope", modes=frozenset({"pr"}), needs_forge=True)], make_input("pr"))
    assert out[0].status == "fail" and rec.calls == []


def test_content_rules_fail_closed_without_tree() -> None:
    rec = Recorder()
    rules = [rec.rule("schemas"), rec.rule("decisions-immutable"), rec.rule("ci-policy", errors=1)]
    out = validate.evaluate(rules, make_input("pr", tree=None))
    result = {o.check: o for o in out}
    assert result["schemas"].status == "fail"
    assert result["schemas"].findings[-1].code == "E_INTERNAL_UNEXPECTED"
    assert result["decisions-immutable"].status == "pass"  # no lee contenido
    assert [f.code for f in result["ci-policy"].findings] == ["E_VALIDATION_STUB"]  # ya falla: sin duplicar


def test_tools_reach_only_the_rules_that_declare_them() -> None:
    rec = Recorder()
    rules = [rec.rule("secrets", modes=frozenset({"pr"}), tools=("gitleaks",)), rec.rule("schemas")]
    validate.evaluate(rules, make_input("pr", tools={"gitleaks": ("x",), "other": ("y",)}))
    assert dict(rec.seen["secrets"].tools) == {"gitleaks": ("x",)}
    assert dict(rec.seen["schemas"].tools) == {}


# -- --pin-report (REQ-CI-45) ---------------------------------------------------------


def _pin_envelope(results: list[dict[str, object]], *, mode: str = "tree", op_ok: bool = True) -> Result:
    total = sum(len(r["findings"]) for r in results)  # type: ignore[arg-type]
    data = {
        "valid": all(r["status"] != "fail" for r in results),
        "mode": mode,
        "pr": None,
        "base": {"ref": "origin/main", "sha": SHA},
        "head": {"ref": "task/3", "sha": SHA},
        "results": results,
        "page": {"next_cursor": None, "returned": total, "total": total},
    }
    if op_ok:
        return ok("validate", data)
    return fail("validate", make_error("E_VALIDATION_FAILED", translator("en")), data=data)


def _write(path: Path, result: Result) -> Path:
    path.write_bytes(dumps(result))
    return path


PIN_FINDING = {"path": "roles.yaml", "line": 3, "severity": "error", "code": "E_VALIDATION_SCHEMA",
               "message": "desde el pin", "pointer": "/people", "hint": None}


def _pin_rules(rec: Recorder) -> list[RuleSpec]:
    return [
        rec.rule("scope", modes=frozenset({"pr"}), after=("roles-sync", "generated-sync")),
        rec.rule("schemas"),
        rec.rule("roles-sync"),
        rec.rule("generated-sync"),
        rec.rule("ci-policy"),
    ]


def test_pin_report_supplies_results_and_prior(tmp_path: Path) -> None:
    report = _write(tmp_path / "pin.json", _pin_envelope([
        {"check": "generated-sync", "status": "pass", "findings": []},
        {"check": "roles-sync", "status": "fail", "findings": [PIN_FINDING]},
        {"check": "schemas", "status": "pass", "findings": []},
        {"check": "convention", "status": "pass", "findings": []},
    ], op_ok=False))
    pin = validate.read_pin_report(report)
    assert pin.problem is None and set(pin.outcomes) == {"generated-sync", "roles-sync", "schemas"}
    rec = Recorder()
    out = validate.evaluate(_pin_rules(rec), make_input("pr"), pin=pin)
    assert statuses(out) == {"scope": "pass", "schemas": "pass", "roles-sync": "fail",
                             "generated-sync": "pass", "ci-policy": "pass"}
    assert rec.calls == ["scope", "ci-policy"]  # las tres del pin no se ejecutan
    assert dict(rec.seen["scope"].prior) == {"roles-sync": "fail", "generated-sync": "pass"}
    roles = next(o for o in out if o.check == "roles-sync")
    assert roles.findings[0].message == "desde el pin" and roles.findings[0].pointer == "/people"


@pytest.mark.parametrize("kind", ["missing", "garbage", "not_envelope", "pr_mode", "other_op", "bad_finding"])
def test_unreadable_pin_report_fails_the_three_rules(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "pin.json"
    good = [{"check": c, "status": "pass", "findings": []} for c in sorted(PIN_RULES)]
    if kind == "garbage":
        path.write_bytes(b"{not json")
    elif kind == "not_envelope":
        path.write_text(json.dumps({"schema": _meta.RESULT_SCHEMA, "ok": True}), encoding="utf-8")
    elif kind == "pr_mode":
        _write(path, _pin_envelope(good, mode="pr"))
    elif kind == "other_op":
        env = json.loads(dumps(_pin_envelope(good)))
        env["op"] = "doctor"
        path.write_text(json.dumps(env), encoding="utf-8")
    elif kind == "bad_finding":
        env = json.loads(dumps(_pin_envelope(good)))
        env["data"]["results"][0]["findings"] = [dict(PIN_FINDING, line=0)]
        path.write_text(json.dumps(env), encoding="utf-8")
    pin = validate.read_pin_report(path)
    assert pin.problem is not None
    rec = Recorder()
    out = validate.evaluate(_pin_rules(rec), make_input("pr"), pin=pin)
    result = statuses(out)
    assert all(result[c] == "fail" for c in PIN_RULES)
    assert result["ci-policy"] == "pass"
    assert dict(rec.seen["scope"].prior) == {"roles-sync": "fail", "generated-sync": "fail"}
    for o in out:
        if o.check in PIN_RULES:
            assert [f.code for f in o.findings] == ["E_INTERNAL_UNEXPECTED"]


def test_pin_report_without_one_rule_fails_only_that_rule(tmp_path: Path) -> None:
    report = _write(tmp_path / "pin.json", _pin_envelope([
        {"check": "schemas", "status": "pass", "findings": []},
        {"check": "roles-sync", "status": "pass", "findings": []},
    ]))
    out = validate.evaluate(_pin_rules(Recorder()), make_input("pr"), pin=validate.read_pin_report(report))
    assert statuses(out)["generated-sync"] == "fail"
    assert statuses(out)["schemas"] == "pass"


def test_pin_status_pass_with_error_findings_counts_as_fail(tmp_path: Path) -> None:
    report = _write(tmp_path / "pin.json", _pin_envelope([
        {"check": c, "status": "pass", "findings": [PIN_FINDING] if c == "schemas" else []} for c in sorted(PIN_RULES)
    ]))
    pin = validate.read_pin_report(report)
    assert pin.outcomes["schemas"].status == "fail"


# -- herramientas ------------------------------------------------------


def test_resolve_tools_uses_testing_override_only_with_testing(tmp_path: Path) -> None:
    prefix = _meta.ENV_PREFIX
    argv = [sys.executable, "fake_gitleaks.py"]
    env = {prefix + "TESTING": "1", prefix + "GITLEAKS": json.dumps(argv), "PATH": ""}
    assert validate.resolve_tools(["gitleaks"], env=env, root=tmp_path, cwd=str(tmp_path)) == {"gitleaks": tuple(argv)}
    # sin TESTING=1 la variable no cuenta, y con un PATH vacío no hay herramienta
    env_off = {prefix + "GITLEAKS": json.dumps(argv), "PATH": ""}
    assert validate.resolve_tools(["gitleaks"], env=env_off, root=tmp_path, cwd=str(tmp_path)) == {}
    # una variable inválida no cae al PATH: falla cerrada
    for raw in ("not json", "[]", '"x"', "[1]"):
        bad = {prefix + "TESTING": "1", prefix + "GITLEAKS": raw, "PATH": ""}
        assert validate.resolve_tools(["gitleaks"], env=bad, root=tmp_path, cwd=str(tmp_path)) == {}


def test_resolve_tools_ignores_executables_inside_the_tree(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    outside = tmp_path / "bin"
    for d in (tree, outside):
        d.mkdir()
    name = "gitleaks.exe" if sys.platform == "win32" else "gitleaks"
    for d in (tree, outside):
        exe = d / name
        exe.write_bytes(b"")
        exe.chmod(0o755)
    env = {"PATH": f"{tree}{__import__('os').pathsep}{outside}"}
    found = validate.resolve_tools(["gitleaks"], env=env, root=tree, cwd=str(tmp_path))
    assert found == {"gitleaks": (str(outside / name),)}
    only_tree = {"PATH": str(tree)}
    assert validate.resolve_tools(["gitleaks"], env=only_tree, root=tree, cwd=str(tmp_path)) == {}


# -- argumentos -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "param", "reason"),
    [
        ({"mode": "nope"}, "mode", "enum"),
        ({"format": "html"}, "format", "enum"),
        ({"pr": 0}, "pr", "minimum"),
        ({"pr": True}, "pr", "type"),
        ({"checks": ["x"] * 51}, "checks", "maxItems"),
        ({"checks": "schemas"}, "checks", "type"),
        ({"max_chars": 10}, "max_chars", "minimum"),
        ({"max_chars": 50000}, "max_chars", "maximum"),
        ({"tree": ""}, "tree", "type"),
        ({"changed_only": "yes"}, "changed_only", "type"),
        ({"surprise": 1}, "surprise", "unknown"),
    ],
)
def test_parse_args_rejects_bad_values(kwargs: dict[str, object], param: str, reason: str) -> None:
    with pytest.raises(OpError) as info:
        validate.parse_args(kwargs)
    assert info.value.code == "E_VALIDATION_INPUT"
    assert {"param": param, "reason": reason} in info.value.details["errors"]  # type: ignore[index]


def test_parse_args_defaults_and_dedup() -> None:
    args = validate.parse_args({"checks": ["schemas", "schemas", "ci-policy"], "mode": None, "format": None,
                                "changed_only": None, "max_chars": 2000, "cursor": "c"})
    assert args.mode == "tree" and args.format == "text" and args.changed_only is False
    assert args.checks == ("schemas", "ci-policy")
    assert validate.parse_args({"checks": []}).checks is None


# -- --changed-only, sobre y salida --------------------------------------------------------


def test_only_changed_keeps_findings_of_changed_files() -> None:
    rec = Recorder()
    out = validate.evaluate([rec.rule("schemas", errors=2, warnings=1)], make_input())
    kept = validate.only_changed(out, [Change(path="e1.txt", status="modified")])
    assert [f.path for f in kept[0].findings] == ["e1.txt", None]
    assert kept[0].status == "fail"
    none = validate.only_changed(out, [Change(path="other", status="added")])
    assert none[0].status == "pass" and [f.path for f in none[0].findings] == [None]


def _result_with(findings: list[dict[str, object]]) -> Result:
    data = {
        "valid": False, "mode": "pr", "pr": 7, "base": {"ref": "main", "sha": None}, "head": {"ref": "task/7", "sha": SHA},
        "results": [{"check": "scope", "status": "fail", "findings": findings}],
        "page": {"next_cursor": None, "returned": len(findings), "total": len(findings)},
    }
    return fail("validate", make_error("E_VALIDATION_FAILED", translator("en")), data=data)


def _finding(i: int, severity: str = "error", **kw: object) -> dict[str, object]:
    base: dict[str, object] = {"path": f"tex/f{i}.tex", "line": i + 1, "severity": severity, "code": "E_PERM_OUTSIDE_TASK",
                               "message": f"m{i}", "pointer": None, "hint": None}
    base.update(kw)
    return base


def test_github_annotations_format_and_escaping() -> None:
    findings = [
        _finding(0, path="a,b:c%.tex", message="línea\nnueva %"),
        _finding(1, severity="warning", path=None, line=None, message="aviso"),
        _finding(2, line=None),
    ]
    lines, omitted = validate.github_annotations(_result_with(findings).data["results"])  # type: ignore[index]
    assert omitted == 0
    assert lines[0] == "::error file=a%2Cb%3Ac%25.tex,line=1,title=scope E_PERM_OUTSIDE_TASK::línea nueva %25"
    assert lines[1] == "::error file=tex/f2.tex,title=scope E_PERM_OUTSIDE_TASK::m2"
    assert lines[2] == "::warning title=scope E_PERM_OUTSIDE_TASK::aviso"  # errores primero


def test_github_annotations_are_capped_at_50(t_es: Translator) -> None:
    findings = [_finding(i, severity="warning") for i in range(5)] + [_finding(i) for i in range(5, 65)]
    result = _result_with(findings)
    text = validate.render_text(result, t_es, fmt="github")
    annotations = [line for line in text.splitlines() if line.startswith("::")]
    assert len(annotations) == 50
    assert all(line.startswith("::error ") for line in annotations)  # los avisos quedan fuera
    assert t_es.plural("validate.text.annotations_omitted", 15, max=50) in text
    plain = validate.render_text(result, t_es, fmt="text")
    assert not any(line.startswith("::") for line in plain.splitlines())
    assert t_es("validate.text.invalid", mode="pr") in plain


def test_render_text_lists_results_and_hints(t_en: Translator) -> None:
    result = _result_with([_finding(0, hint="Do this.")])
    text = validate.render_text(result, t_en)
    assert "scope: fail" in text and "tex/f0.tex:1" in text and "hint: Do this." in text


def test_validate_catalog_keys_used_by_engine_exist() -> None:
    for lang in ("es", "en"):
        catalog = load_catalog(lang)  # type: ignore[arg-type]
        for key in ("validate.reason.no_tree", "validate.reason.no_forge", "validate.reason.exception",
                    "validate.reason.pin_missing", "validate.reason.pin_unreadable", "validate.reason.pin_invalid",
                    "validate.reason.pin_rule_missing", "validate.text.valid", "validate.text.invalid",
                    "validate.text.result", "validate.text.finding", "validate.text.hint",
                    "validate.text.status.pass", "validate.text.status.fail", "validate.text.status.skip",
                    "validate.text.annotations_omitted.one", "validate.text.annotations_omitted.other"):
            assert key in catalog, (lang, key)
        assert placeholders(catalog["finding.E_INTERNAL_UNEXPECTED.message"]) == frozenset({"rule", "reason"})


def test_my_finding_placeholders_match_declared_codes() -> None:
    from aucove.ops.rules import ci_policy, decisions, schemas, secrets

    own = {**schemas.OWN_CODES, **decisions.RULE.codes, **ci_policy.RULE.codes, **secrets.RULE.codes}
    for lang in ("es", "en"):
        catalog = load_catalog(lang)  # type: ignore[arg-type]
        for code, params in own.items():
            for part in ("message", "hint"):
                key = f"finding.{code}.{part}"
                assert key in catalog, (lang, key)
                assert placeholders(catalog[key]) <= params, (lang, key)
            assert placeholders(catalog[f"finding.{code}.message"]) == params, (lang, code)


def test_built_envelope_validates_against_the_schema() -> None:
    rec = Recorder()
    out = validate.evaluate([rec.rule("schemas", errors=1, warnings=1), rec.rule("scope", modes=frozenset({"pr"}))],
                            make_input("tree"), checks=["schemas", "scope"])
    prepared = validate.Prepared(inp=make_input("tree"), base={"ref": "origin/main", "sha": SHA},
                                 head={"ref": "HEAD", "sha": None})
    result = validate._build_result(prepared, out, validate.Args(), translator("es"))
    env = result.to_dict()
    assert validate_result(env) == []
    assert result.ok is False and result.error is not None and result.error.code == "E_VALIDATION_FAILED"
    assert env["data"]["page"] == {"next_cursor": None, "returned": 2, "total": 2}  # type: ignore[index]
    assert [r["status"] for r in env["data"]["results"]] == ["fail", "skip"]  # type: ignore[index]
