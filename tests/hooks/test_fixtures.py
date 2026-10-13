# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""T4: fixtures de hooks (12-mvp §3.5, REQ-MVP-30, REQ-MVP-31).

Los casos se cargan y validan al importar este módulo: uno mal formado hace
fallar la recolección. Mientras ``aucove hook`` salga con 2 porque sus módulos
llegan en H4, cada caso se omite con el motivo ``'aucove hook: H4'``.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from aucove import _meta
from tests.hooks import runner

HERE = Path(__file__).resolve().parent

# Recolección: el primer caso mal formado lanza runner.CaseError aquí.
CASES: tuple[runner.HookCase, ...] = runner.load_cases()


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_hook_case(case: runner.HookCase, tmp_path: Path, git_env: dict[str, str]) -> None:
    exe = runner.resolve_launcher()
    reason = runner.skip_reason(case, exe)
    if reason is not None:
        pytest.skip(reason)
    assert exe is not None
    run = runner.run_case(case, tmp_path, git_env, exe)
    problems = runner.evaluate(case, run)
    assert problems == [], (
        f"{case.id}: {problems}\nsalida={run.returncode}\n"
        f"stdout={run.stdout[:500]!r}\nstderr={run.stderr[:500]!r}"
    )


# --- los fixtures -----------------------------------------------------------------------


def test_case_schema_is_valid_draft_2020_12() -> None:
    schema = json.loads(runner.SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_every_fixture_validates_against_the_schema() -> None:
    validator = Draft202012Validator(json.loads(runner.SCHEMA_PATH.read_text(encoding="utf-8")))
    files = runner.case_files()
    assert files, "no hay fixtures de hooks"
    for path in files:
        errors = [e.message for e in validator.iter_errors(json.loads(path.read_text(encoding="utf-8")))]
        assert errors == [], f"{path.name}: {errors}"


def test_h2_brings_the_three_skeleton_cases() -> None:
    by_class = {c.cls: c for c in CASES}
    assert set(by_class) == {"deny", "allow", "fault"}
    assert any(c.cls == "fault" and c.stdin == "" for c in CASES), "falta el caso fault con stdin vacío"
    roles_dir = (runner.FIXTURES / "roles").resolve()
    for case in CASES:
        assert case.event == "pre-tool-use"
        assert case.setup.roles_base.resolve().parent == roles_dir, "roles_base debe apuntar a un fixture de tests/fixtures/roles"
        assert set(case.os) == {"windows", "macos", "linux"}


def test_roles_base_accepts_name_and_path() -> None:
    by_name = runner.resolve_roles_base("basic")
    by_path = runner.resolve_roles_base("roles/basic.yaml")
    assert by_name == by_path == runner.FIXTURES / "roles" / "basic.yaml"
    with pytest.raises(runner.CaseError, match="no existe tests/fixtures"):
        runner.resolve_roles_base("missing")


# --- casos mal formados ---------------------------------------------------------------


def _good_case() -> dict[str, object]:
    path = runner.CASES_DIR / "pre-tool-use" / "deny-foreign-section-edit.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _cases_dir(tmp: Path) -> Path:
    fixtures = tmp / "fixtures"
    shutil.copytree(runner.FIXTURES / "roles", fixtures / "roles")
    cases = fixtures / "hooks"
    (cases / "pre-tool-use").mkdir(parents=True)
    shutil.copy2(runner.SCHEMA_PATH, cases / runner.SCHEMA_NAME)
    return cases


def _write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def _mutate(fn: Callable[[dict[str, object]], None]) -> dict[str, object]:
    case = _good_case()
    fn(case)
    return case


def _set(path: str, value: object) -> Callable[[dict[str, object]], None]:
    def apply(case: dict[str, object]) -> None:
        *parents, last = path.split(".")
        node: dict[str, object] = case
        for key in parents:
            node = node[key]  # type: ignore[assignment]
        node[last] = value

    return apply


def _drop(path: str) -> Callable[[dict[str, object]], None]:
    def apply(case: dict[str, object]) -> None:
        *parents, last = path.split(".")
        node: dict[str, object] = case
        for key in parents:
            node = node[key]  # type: ignore[assignment]
        del node[last]

    return apply


MALFORMED: Mapping[str, Callable[[dict[str, object]], None]] = {
    "sin-expect": _drop("expect"),
    "clase-desconocida": _set("class", "maybe"),
    "os-vacio": _set("os", []),
    "os-desconocido": _set("os", ["plan9"]),
    "clave-extra": _set("extra", True),
    "setup-clave-extra": _set("setup.base_branch", "main"),
    "sin-identity": _drop("setup.identity"),
    "login-invalido": _set("setup.identity", "carla soto"),
    "tarea-no-numerica": _set("setup.task_sections", {"doce": ["methods"]}),
    "seccion-invalida": _set("setup.task_sections", {"12": ["Methods"]}),
    "roles-base-ausente": _set("setup.roles_base", "roles/nope.yaml"),
    "stdin-numero": _set("stdin", 7),
    "stdin-sin-tool-name": _drop("stdin.tool_name"),
    "evento-equivocado": _set("stdin.hook_event_name", "PostToolUse"),
    "decision-desconocida": _set("expect.decision", "block"),
    "deny-con-allow": _set("expect.decision", "allow"),
    "mencion-vacia": _set("expect.reason_mentions", [""]),
    "id-distinto-de-la-ruta": _set("id", "pre-tool-use/otro-caso"),
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_malformed_case_is_rejected(name: str, tmp_path: Path) -> None:
    cases = _cases_dir(tmp_path)
    _write(cases / "pre-tool-use" / "deny-foreign-section-edit.json", _mutate(MALFORMED[name]))
    with pytest.raises(runner.CaseError):
        runner.load_cases(cases)


def test_class_and_decision_must_agree(tmp_path: Path) -> None:
    cases = _cases_dir(tmp_path)
    fault = _mutate(_set("class", "fault"))
    _write(cases / "pre-tool-use" / "deny-foreign-section-edit.json", fault)
    runner.load_cases(cases)  # fault + deny: válido
    fault_allow = _mutate(_set("class", "fault"))
    _set("expect", {"decision": "allow"})(fault_allow)
    _write(cases / "pre-tool-use" / "deny-foreign-section-edit.json", fault_allow)
    with pytest.raises(runner.CaseError, match="schema"):
        runner.load_cases(cases)
    allow_with_reason = _mutate(_set("class", "allow"))
    _set("expect", {"decision": "allow", "reason_mentions": ["x"]})(allow_with_reason)
    _write(cases / "pre-tool-use" / "deny-foreign-section-edit.json", allow_with_reason)
    with pytest.raises(runner.CaseError):
        runner.load_cases(cases)


@pytest.mark.parametrize(
    ("rel", "content"),
    [
        ("pre-tool-use/deny-foreign-section-edit.json", b"{\"id\": "),
        ("pre-tool-use/deny-foreign-section-edit.json", b"\xef\xbb\xbf{}"),
        ("pre-tool-use/deny-foreign-section-edit.json", b"{\"id\": 1, \"id\": 2}"),
        ("pre-tool-use/deny-foreign-section-edit.json", b"\xff\xfe"),
    ],
    ids=["json-invalido", "bom", "clave-repetida", "no-utf8"],
)
def test_unreadable_case_is_rejected(rel: str, content: bytes, tmp_path: Path) -> None:
    cases = _cases_dir(tmp_path)
    (cases / rel).write_bytes(content)
    with pytest.raises(runner.CaseError):
        runner.load_cases(cases)


def test_case_outside_an_event_directory_is_rejected(tmp_path: Path) -> None:
    cases = _cases_dir(tmp_path)
    good = _good_case()
    _write(cases / "deny-foreign-section-edit.json", good)
    with pytest.raises(runner.CaseError, match="<evento>/<caso>"):
        runner.load_cases(cases)
    (cases / "deny-foreign-section-edit.json").unlink()
    _write(cases / "pre-tool-use" / "extra" / "deny-foreign-section-edit.json", good)
    with pytest.raises(runner.CaseError):
        runner.load_cases(cases)


def test_unknown_event_directory_is_rejected(tmp_path: Path) -> None:
    cases = _cases_dir(tmp_path)
    case = _good_case()
    case["id"] = "pre-edit/deny-foreign-section-edit"
    _write(cases / "pre-edit" / "deny-foreign-section-edit.json", case)
    with pytest.raises(runner.CaseError):
        runner.load_cases(cases)


def _copy_harness(root: Path) -> Path:
    """Copia el ejecutor, este módulo y los fixtures a ``root`` con la misma estructura."""
    (root / "tests" / "hooks").mkdir(parents=True)
    (root / "tests" / "__init__.py").write_bytes(b"")
    shutil.copy2(HERE / "runner.py", root / "tests" / "hooks" / "runner.py")
    shutil.copy2(HERE / "test_fixtures.py", root / "tests" / "hooks" / "test_fixtures.py")
    shutil.copytree(runner.CASES_DIR, root / "tests" / "fixtures" / "hooks")
    shutil.copytree(runner.FIXTURES / "roles", root / "tests" / "fixtures" / "roles")
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    return root


def _collect(root: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = os.pathsep.join([str(root), str(runner.REPO_ROOT / "src")])
    return subprocess.run(
        [
            sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
            "-c", str(root / "pytest.ini"), "--rootdir", str(root), "--import-mode=importlib",
            "tests/hooks/test_fixtures.py",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )


def test_malformed_case_makes_collection_fail(tmp_path: Path) -> None:
    root = _copy_harness(tmp_path / "harness")
    ok = _collect(root)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    bad = _mutate(_drop("expect"))
    _write(root / "tests" / "fixtures" / "hooks" / "pre-tool-use" / "zz-malformed.json", bad)
    failed = _collect(root)
    output = failed.stdout + failed.stderr
    assert failed.returncode != 0, output
    assert "CaseError" in output and "zz-malformed.json" in output, output


# --- el ejecutor: sin shell y con el lanzador del PATH --------------------------------


_SUBPROCESS_CALLS = frozenset(
    {"subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call", "subprocess.check_output"}
)
_SHELL_CALLS = frozenset(
    {"os.system", "os.popen", "os.startfile", "subprocess.getoutput", "subprocess.getstatusoutput"}
)


def test_runner_never_uses_shell() -> None:
    tree = ast.parse(Path(runner.__file__).read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    spawns = [c for c in calls if ast.unparse(c.func) in _SUBPROCESS_CALLS]
    assert spawns, "el ejecutor no lanza subprocesos"
    problems: list[str] = []
    for call in calls:
        name = ast.unparse(call.func)
        if name in _SHELL_CALLS:
            problems.append(f"línea {call.lineno}: {name}")
        for kw in call.keywords:
            if kw.arg is None and name in _SUBPROCESS_CALLS:
                problems.append(f"línea {call.lineno}: **kwargs en {name}")
            if kw.arg == "shell" and not (isinstance(kw.value, ast.Constant) and kw.value.value is False):
                problems.append(f"línea {call.lineno}: shell={ast.unparse(kw.value)}")
    for call in spawns:
        if not any(kw.arg == "shell" for kw in call.keywords):
            problems.append(f"línea {call.lineno}: {ast.unparse(call.func)} sin shell=False explícito")
        if call.args and not isinstance(call.args[0], (ast.List, ast.Call)):
            problems.append(f"línea {call.lineno}: argv no es una lista")
    assert problems == []


def test_invoke_passes_an_argv_list_without_shell(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def fake_run(argv: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        seen["argv"] = argv
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 2, b"", b"blocked")  # type: ignore[arg-type]

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    exe = str(tmp_path / "bin" / _meta.NAME)
    run = runner.invoke(exe, ("hook", "pre-tool-use"), b"{}", cwd=tmp_path, env={"A": "1"}, timeout_s=5.0)
    assert seen["argv"] == [exe, "hook", "pre-tool-use"]
    assert seen["shell"] is False
    assert seen["input"] == b"{}"
    assert seen["timeout"] == 5.0
    assert run.returncode == 2 and run.stderr == b"blocked" and run.argv[0] == exe


def test_invoke_reports_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def slow(argv: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(argv, 5.0, output=b"", stderr=None)  # type: ignore[arg-type]

    monkeypatch.setattr(runner.subprocess, "run", slow)
    run = runner.invoke("x", ("hook", "pre-tool-use"), b"", cwd=tmp_path, env={}, timeout_s=5.0)
    assert run.returncode is None
    case = CASES[0]
    assert runner.evaluate(case, run) and "timeout" in runner.evaluate(case, run)[0]


def _fake_launcher(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if runner.current_os() == "windows":
        path = directory / f"{_meta.NAME}.exe"
        path.write_bytes(b"")
    else:
        path = directory / _meta.NAME
        path.write_bytes(b"#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    return path


def test_resolve_launcher_searches_only_absolute_path_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """REQ-MVP-30: el lanzador sale de las carpetas absolutas del PATH, nunca del directorio actual.

    ``shutil.which`` de Python 3.11 en Windows antepone el directorio actual; el
    ejecutor usa ``find_launcher`` de ``doctor``, que no lo hace.
    """
    here = _fake_launcher(tmp_path / "aqui")
    monkeypatch.chdir(here.parent)
    launcher = _fake_launcher(tmp_path / "bin")
    found = runner.resolve_launcher(str(tmp_path / "bin"))
    assert found is not None and os.path.isabs(found)
    assert Path(found).resolve() == launcher.resolve()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert runner.resolve_launcher(str(empty)) is None  # el de 'aqui' (directorio actual) no cuenta
    assert runner.resolve_launcher(os.pathsep.join([".", "", str(empty)])) is None  # entradas relativas
    monkeypatch.setenv("PATH", str(empty))
    installed = runner.resolve_launcher()  # PATH sin lanzador: None o el del directorio de scripts
    assert installed is None or Path(installed).resolve().parent != here.resolve().parent


def test_skip_reason_while_hooks_are_not_available(monkeypatch: pytest.MonkeyPatch) -> None:
    case = CASES[0]

    def result(code: int | None, stderr: bytes) -> Callable[..., runner.HookRun]:
        return lambda *a, **k: runner.HookRun(("x", "hook"), code, b"", stderr, 0.1, 30.0)

    monkeypatch.setattr(runner, "invoke", result(2, b"x hook: no disponible ... / x hook: not available in this version (0.1.0)."))
    assert runner.probe("x") == runner.HOOK_H4_REASON == f"{_meta.NAME} hook: H4"
    monkeypatch.setattr(runner, "invoke", result(0, b""))
    assert runner.probe("x") is None
    monkeypatch.setattr(runner, "invoke", result(2, b"otro motivo"))
    assert runner.probe("x") is None
    assert runner.skip_reason(case, None) is not None
    other = next(o for o in ("windows", "macos", "linux") if o != runner.current_os())
    alien = runner.HookCase(case.id, case.event, case.cls, (other,), case.setup, case.stdin, case.expect, case.path)  # type: ignore[arg-type]
    assert "solo corre en" in (runner.skip_reason(alien, "x") or "")


def test_installed_launcher_matches_the_milestone() -> None:
    exe = runner.resolve_launcher()
    if exe is None:
        pytest.skip(f"{_meta.NAME} no está instalado")
    try:
        has_hooks = importlib.util.find_spec(f"{_meta.NAME}.hooks.main") is not None
    except ModuleNotFoundError:
        has_hooks = False
    expected = None if has_hooks else runner.HOOK_H4_REASON
    assert runner.probe(exe) == expected


# --- evaluación ------------------------------------------------------------------------


def _case(cls: str) -> runner.HookCase:
    return next(c for c in CASES if c.cls == cls)


def _run(code: int | None, stdout: bytes = b"", stderr: bytes = b"") -> runner.HookRun:
    return runner.HookRun(("x",), code, stdout, stderr, 0.1, 5.0)


def _deny_json(reason: str) -> bytes:
    body = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    return json.dumps(body).encode("utf-8") + b"\n"


def test_evaluate_deny() -> None:
    deny = _case("deny")
    reason = "No puedes modificar sections/results/results.tex: la parte results es de @bruno."
    assert runner.evaluate(deny, _run(2, _deny_json(reason), reason.encode("utf-8"))) == []
    assert runner.evaluate(deny, _run(2, b"", reason.encode("utf-8"))) == []
    assert runner.evaluate(deny, _run(0, _deny_json(reason))) == []
    assert runner.evaluate(deny, _run(0))  # sin decisión
    assert runner.evaluate(deny, _run(2, b"", b"bloqueado"))  # la razón no menciona results ni bruno
    assert runner.evaluate(deny, _run(1, b"", reason.encode("utf-8")))  # REQ-HOOK-13


def test_evaluate_allow() -> None:
    allow = _case("allow")
    assert runner.evaluate(allow, _run(0)) == []
    assert runner.evaluate(allow, _run(2, b"", b"bloqueado"))
    assert runner.evaluate(allow, _run(0, _deny_json("x")))
    allow_json = b'{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}\n'
    assert any("REQ-HOOK-6" in p for p in runner.evaluate(allow, _run(0, allow_json)))


def test_evaluate_fault_accepts_only_exit_2() -> None:
    fault = _case("fault")
    assert runner.evaluate(fault, _run(2, b"", b"error")) == []
    assert runner.evaluate(fault, _run(0, _deny_json("error")))  # JSON de denegación sin salida 2
    assert runner.evaluate(fault, _run(0))
    assert runner.evaluate(fault, _run(1))


def test_evaluate_checks_stdout_format() -> None:
    fault = _case("fault")
    assert any("REQ-HOOK-12" in p for p in runner.evaluate(fault, _run(2, b"no json")))
    assert any("REQ-HOOK-12" in p for p in runner.evaluate(fault, _run(2, b"\xef\xbb\xbf{}\n")))
    assert any("REQ-HOOK-12" in p for p in runner.evaluate(fault, _run(2, b"[]\n")))
    assert any("REQ-HOOK-12" in p for p in runner.evaluate(fault, _run(2, b"{}")))


# --- preparación del repositorio -------------------------------------------------------


def test_render_stdin_substitutes_the_repo(tmp_path: Path) -> None:
    deny = _case("deny")
    data = json.loads(runner.render_stdin(deny.stdin, tmp_path).decode("utf-8"))
    assert data["cwd"] == str(tmp_path)
    assert data["tool_input"]["file_path"] == f"{tmp_path}/sections/results/results.tex"
    assert runner.REPO_PLACEHOLDER not in json.dumps(data)
    assert runner.render_stdin(_case("fault").stdin, tmp_path) == b""


def test_hook_env_is_offline_and_clean() -> None:
    env = runner.hook_env({"PATH": "p", _meta.ENV_PREFIX + "LANG": "es"})
    assert env == {"PATH": "p", _meta.ENV_PREFIX + "OFFLINE": "1"}


def test_prepare_repo_builds_the_base_and_the_branch(tmp_path: Path, git_env: dict[str, str]) -> None:
    if shutil.which("git") is None:
        pytest.skip("git no está disponible")
    case = _case("deny")
    repo = runner.prepare_repo(case, tmp_path / "case", git_env)
    assert (repo / "roles.yaml").read_bytes() == case.setup.roles_base.read_bytes()
    project = (repo / "project.yaml").read_text(encoding="utf-8")
    assert "{{var:" not in project and "base_branch: main" in project
    for sid in ("introduction", "methods", "results"):
        assert (repo / "sections" / sid / f"{sid}.tex").is_file()

    def git(*args: str) -> str:
        return runner._git(repo, git_env, *args)

    assert git("rev-parse", "--abbrev-ref", "HEAD") == case.setup.branch
    assert git("rev-parse", "origin/main") == git("rev-parse", "main")
    assert git("status", "--porcelain") == ""


def test_seed_local_state_round_trips_through_the_store(tmp_path: Path, git_env: dict[str, str]) -> None:
    # ``localstore.store`` es de H2: se importa directamente, sin
    # ``importorskip``, para que un módulo renombrado o borrado haga fallar la prueba en
    # lugar de omitirla. Solo la falta de git la omite.
    from aucove.localstore import store as store_mod

    if shutil.which("git") is None:
        pytest.skip("git no está disponible")
    case = _case("deny")
    repo = runner.prepare_repo(case, tmp_path / "case", git_env)
    runner.seed_local_state(repo, case.setup)
    store = store_mod.Store(repo)
    ident = store.identity(token_fp=None)
    assert ident is not None and ident.login == "carla"
    task = store.task(12)
    assert task is not None
    assert (task.claimant, task.state, task.sections, task.local_branch) == ("carla", "claimed", ("methods",), "task/12")
    no_identity = runner.HookSetup(None, case.setup.branch, case.setup.roles_base, case.setup.task_sections)
    other = tmp_path / "other"
    other.mkdir()
    runner.seed_local_state(other, no_identity)
    assert store_mod.Store(other).identity(token_fp=None) is None
