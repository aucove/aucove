# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""A9 de punta a punta: el conjunto dorado de ``scope`` por el lanzador con el motor completo.

Autoridad: 12-mvp §2.3 A9 (fixtures, H2) y §4 fila H2; 09-ci §4.1-§4.2, REQ-CI-45.
``test_scope_golden`` ya clasifica los casos llamando a la regla ``scope`` en proceso con el
prior declarado; aquí cada caso pasa por ``aucove validate --mode pr`` como lo ejecuta el
job ``validate`` de ``validate.yml``: la línea del workflow (``WORKFLOW_COMMANDS``) con
``--policy`` = checkout de la base, ``--tree`` = cabeza del PR (sin ``--tree`` en un fork),
``--report`` en ``$RUNNER_TEMP`` y el directorio de trabajo fuera del árbol. El motor
calcula el prior (``roles-sync`` y ``generated-sync``) en vez de recibirlo.

Reglas de cada caso: las de ``checks`` del YAML o, si no declara, ``scope``, ``roles-sync``,
``generated-sync`` y ``decisions-immutable``. Siempre se agregan ``roles-sync`` y
``generated-sync`` (el motor las corre igual, como dependencias de ``scope``) para que su
estado aparezca en ``results[]`` y se pueda comparar el prior calculado con el declarado.
Estado esperado de cada regla: el de ``expect.checks``; si no lo declara, el ``prior`` del
caso para las dos reglas del prior y ``pass`` para las demás.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from aucove.config.schemas import validate_result
from aucove.i18n import translator
from aucove.ops.result import read_envelope
from tests import scenarios
from tests.conftest import isolated_git_env
from tests.e2e.conftest import (
    CI_LOGIN,
    DEFAULT_A9_CHECKS,
    PREFIX,
    PRIOR_RULES,
    Launcher,
    Run,
    base_env,
    expand,
    workflow_command,
)

CASE_PATHS = scenarios.case_paths()
CASE_IDS = [p.stem for p in CASE_PATHS]
EXIT_VALIDATION = 3  # E_VALIDATION_FAILED (área VALIDATION)
# Líneas de los workflows que ejerce este módulo (test_template_ci::test_every_workflow_command_is_exercised).
EXERCISED: tuple[str, ...] = ("validate --mode pr",)


def declared_checks(path: Path) -> list[str] | None:
    """``checks`` tal como lo escribe el YAML (``load_case`` pone ``[scope]`` por defecto)."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    checks = raw.get("checks") if isinstance(raw, Mapping) else None
    return None if checks is None else [str(c) for c in checks]


def checks_to_run(path: Path) -> tuple[str, ...]:
    declared = declared_checks(path)
    base = declared if declared is not None else list(DEFAULT_A9_CHECKS)
    return tuple(dict.fromkeys([*base, *PRIOR_RULES]))


def expected_statuses(case: Mapping[str, Any], checks: tuple[str, ...]) -> dict[str, str]:
    expect_checks: Mapping[str, str] = case["expect"]["checks"]
    out: dict[str, str] = {}
    for check in checks:
        if check in expect_checks:
            out[check] = expect_checks[check]
        elif check in PRIOR_RULES:
            out[check] = case["prior"][check]
        else:
            out[check] = "pass"
    return out


@dataclass
class Outcome:
    case: Mapping[str, Any]
    checks: tuple[str, ...]
    run: Run
    report: dict[str, Any] | None


def _run_case(launcher: Launcher, path: Path, root: Path) -> Outcome:
    case = scenarios.load_case(path)
    work = root / case["id"]  # GITHUB_WORKSPACE: base/ y pr/ como en validate.yml
    sc = scenarios.build_scenario(case, work, isolated_git_env(root / "git-build" / case["id"]))
    runner_temp = work / "runner"
    runner_temp.mkdir()
    env = base_env(work)
    sc.forge.as_user(CI_LOGIN).save(Path(env[PREFIX + "FORGE_FAKE_STATE"]))
    checks = checks_to_run(path)
    line = workflow_command("validate --mode pr")
    variables = {
        "PR_NUMBER": str(case["pr"]["number"]),
        "GITHUB_WORKSPACE": str(work),
        "RUNNER_TEMP": str(runner_temp),
    }
    tree = ["--tree", str(sc.pr_dir)] if sc.pr_dir is not None else []
    ex = expand(line.line, variables, {"TREE": tree, "PINREP": []})
    extra = [arg for check in checks for arg in ("--check", check)]
    run = launcher.run([*ex.argv, *extra], cwd=runner_temp, env=env)
    report = read_envelope(runner_temp / "validate.json")
    return Outcome(case, checks, run, report)


class _Results:
    def __init__(self, launcher: Launcher, root: Path) -> None:
        translator("es"), translator("en")  # catálogos cargados antes de los hilos
        self._out: dict[str, Outcome] = {}
        self._errors: dict[str, BaseException] = {}
        with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
            futures = {p.stem: pool.submit(_run_case, launcher, p, root) for p in CASE_PATHS}
        for cid, fut in futures.items():
            try:
                self._out[cid] = fut.result()
            except Exception as exc:  # noqa: BLE001 (se vuelve a lanzar en la prueba del caso)
                self._errors[cid] = exc

    def get(self, cid: str) -> Outcome:
        if cid in self._errors:
            raise self._errors[cid]
        return self._out[cid]


@pytest.fixture(scope="module")
def results(launcher: Launcher, tmp_path_factory: pytest.TempPathFactory) -> _Results:
    return _Results(launcher, tmp_path_factory.mktemp("a9"))


def _results_by_check(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    data = report.get("data") or {}
    return {str(r["check"]): r for r in data.get("results") or []}


def _annotation_property(text: str) -> str:
    """Escape de las propiedades de los comandos de flujo de GitHub Actions."""
    for old, esc in (("%", "%25"), ("\r", "%0D"), ("\n", "%0A"), (":", "%3A"), (",", "%2C")):
        text = text.replace(old, esc)
    return text


def test_golden_set_is_the_expected_one() -> None:
    cases = [scenarios.load_case(p) for p in CASE_PATHS]
    assert len([c for c in cases if c["profile"] == "enforced"]) >= 24
    advisory = [c for c in cases if c["profile"] == "advisory"]
    assert len(advisory) >= 2
    assert {c["expect"]["checks"]["scope"] for c in advisory} == {"pass", "fail"}


@pytest.mark.parametrize("cid", CASE_IDS)
def test_case_is_classified_by_the_launcher(results: _Results, cid: str) -> None:
    out = results.get(cid)
    case = out.case
    assert out.report is not None, f"sin validate.json legible\n{out.run.explain()}"
    assert validate_result(out.report) == [], out.report
    assert out.report["op"] == "validate"
    by_check = _results_by_check(out.report)
    expected = expected_statuses(case, out.checks)
    got = {check: str(r["status"]) for check, r in by_check.items()}
    assert got == expected, f"{cid}: {got} != {expected}\n{out.run.explain()}"

    valid = all(status == "pass" for status in expected.values())
    assert out.report["ok"] is valid
    assert out.report["data"]["valid"] is valid
    assert out.report["data"]["mode"] == "pr" and out.report["data"]["pr"] == case["pr"]["number"]
    if valid:
        assert out.run.returncode == 0, out.run.explain()
    else:
        assert out.run.returncode == EXIT_VALIDATION, out.run.explain()
        assert out.report["error"]["code"] == "E_VALIDATION_FAILED"


@pytest.mark.parametrize("cid", CASE_IDS)
def test_scope_findings_match_the_case(results: _Results, cid: str) -> None:
    out = results.get(cid)
    assert out.report is not None, out.run.explain()
    expect = out.case["expect"]
    findings = list(_results_by_check(out.report)["scope"]["findings"])
    errors = [f for f in findings if f["severity"] == "error"]
    assert sorted({f["code"] for f in errors}) == expect["codes"], findings
    assert sorted({f["path"] for f in errors if f["path"]}) == expect["paths"], findings
    assert sorted({f["code"] for f in findings if f["severity"] == "warning"}) == expect["warnings"]
    texts = " ".join(f"{f['message']} {f.get('hint') or ''}" for f in errors)
    for needle in expect["mentions"]:
        assert needle in texts, f"{needle!r} no aparece en {texts!r}"


@pytest.mark.parametrize("cid", CASE_IDS)
def test_engine_prior_is_the_declared_one(results: _Results, cid: str) -> None:
    out = results.get(cid)
    assert out.report is not None, out.run.explain()
    by_check = _results_by_check(out.report)
    computed = {rule: str(by_check[rule]["status"]) for rule in PRIOR_RULES}
    assert computed == {rule: out.case["prior"][rule] for rule in PRIOR_RULES}


@pytest.mark.parametrize("cid", CASE_IDS)
def test_github_annotations_point_to_the_failing_paths(results: _Results, cid: str) -> None:
    """``--format github``: una anotación ``::error`` por cada ruta que deniega scope (REQ-CI-26)."""
    out = results.get(cid)
    lines = out.run.stdout.splitlines()
    for path in out.case["expect"]["paths"]:
        prop = f"file={_annotation_property(path)}"
        assert any(line.startswith("::error ") and prop in line for line in lines), out.run.stdout
    if out.run.returncode == 0:
        assert not any(line.startswith("::error") for line in lines), out.run.stdout


def test_all_cases_are_well_classified(results: _Results) -> None:
    """Criterio de A9: el 100 % de los casos bien clasificados por el lanzador."""
    wrong: list[str] = []
    for cid in CASE_IDS:
        try:
            out = results.get(cid)
        except Exception as exc:  # noqa: BLE001
            wrong.append(f"{cid}: {type(exc).__name__}")
            continue
        if out.report is None:
            wrong.append(f"{cid}: sin reporte")
            continue
        got = {c: str(r["status"]) for c, r in _results_by_check(out.report).items()}
        if got != expected_statuses(out.case, out.checks):
            wrong.append(f"{cid}: {got}")
        elif (out.run.returncode == 0) != all(v == "pass" for v in got.values()):
            wrong.append(f"{cid}: salida {out.run.returncode}")
    assert wrong == [], f"{len(CASE_IDS) - len(wrong)}/{len(CASE_IDS)} bien clasificados: {wrong}"
