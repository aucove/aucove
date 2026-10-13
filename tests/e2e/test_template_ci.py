# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Autoprueba del CI de la plantilla, variante ``latex`` (00-marco D-37; 12-mvp §4 fila H2, REQ-MVP-4).

El escenario es E1 sobre el render en ``en`` y en ``es`` (``template_repo.build_template_ci``):
un repositorio con el render como commit raíz de ``main``, un remoto ``origin`` bare
(``file://``) y tres PR del ``lead`` desde ramas de tarea: el 21 cambia la parte
``introduction``, el 22 sube ``core_pin`` y el 23 mueve ``tex/notation.tex`` fuera de
``tex/``. ``FakeForge`` lleva los mismos commits, las tareas reclamadas y los PR. Las pruebas
de ``secrets`` sobre fusiones arman además, en una copia del remoto y del estado del fake, el
PR 24 (tarea 10): ``task/10`` integra ``main`` con ``git merge --no-commit`` y la fusión
agrega un secreto (REQ-SEC-36).

Luego se ejecutan por subproceso, con el lanzador instalado y desde un directorio fuera del
árbol (``$RUNNER_TEMP`` o ``$GITHUB_WORKSPACE``, según el paso), las mismas líneas de los
workflows (``WORKFLOW_COMMANDS``, que ``test_workflow_drift`` ata al render) y los mismos
checkouts que hacen sus pasos (``base/`` y ``pr/`` de ``validate.yml``; el merge commit con
profundidad 2 de ``compile.yml`` y ``build.yml``), también las de ``main-broken.yml`` (H3).
La compilación real con Docker y el ``gitleaks`` real solo corren si están instalados (CI); en
local se omiten.

La variante ``format: none`` (E11) está en ``test_template_none`` y los dos jobs de
``seal.yml`` con ``evidence verify`` (A17), en ``test_template_seal``. Entre todos los
módulos de ``tests/e2e`` ejercen cada línea de los workflows
(``test_every_workflow_command_is_exercised``).
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import random
import re
import shutil
import string
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from aucove import _meta
from aucove.forge.fake import FakeForge
from aucove.i18n import translator
from aucove.ops.diagnostics import load_diagnostics
from aucove.ops.rules import secrets
from aucove.ops.rules.base import RuleInput
from aucove.vcs.tree import git_tree
from tests.conftest import FIXTURES
from tests.e2e.conftest import (
    CI_COMMAND_OUTPUTS,
    PIN_RULES,
    PENDING_COMMANDS,
    PREFIX,
    WORKFLOW_COMMANDS,
    Launcher,
    Run,
    WorkflowCommand,
    comments_with_marker,
    expand,
    git,
    parse_outputs,
    read_json,
    seed_claimed_task,
    seed_git_history,
    seed_pull,
    without,
    workflow_command,
)
from tests.e2e.template_repo import (
    BASE_RULES,
    EMPTY_OUTPUTS_STEP,
    EXIT_VALIDATION,
    FAKE_URL,
    INTRO,
    MOVED_NOTATION,
    NOTATION,
    POLICY_STEP,
    PR_CONTENT,
    PR_PIN,
    PR_RENAME,
    PR_RULES,
    TEX_STEP,
    TREE_RULES,
    Pr,
    TemplateCi,
    build_plan,
    changed_diff_args,
    compile_yml,
    next_patch,
    runner_temp,
)
from tests.e2e.template_repo import envelope as _envelope
from tests.e2e.template_repo import findings as _findings
from tests.e2e.template_repo import statuses as _statuses
from tests.e2e.template_repo import step_script as _step_script

MANIFEST = FIXTURES / "template-manifest.txt"
BUILD_LOGS = FIXTURES / "build-logs"
E2E_DIR = Path(__file__).resolve().parent

# Qué líneas de los workflows ejerce este módulo (test_every_workflow_command_is_exercised).
EXERCISED: tuple[str, ...] = (
    "ci comment --kind build",
    "ci build-plan",
    "validate --mode build-log",
    "ci pin-plan",
    "validate --mode tree",
    "validate --mode pr",
    "ci comment --kind validate",
    "validate --mode base",
    "ci base-report",
)

# Valores de las salidas de build-plan (09-ci §3.2, REQ-CI-16 a REQ-CI-19; D-33 o).
ENGINE_FLAGS = {"pdflatex": "-pdf", "lualatex": "-pdflua", "xelatex": "-pdfxe"}
SHELL_FLAGS = {"disabled": "-no-shell-escape", "restricted": "-shell-restricted"}
BASE_RETENTION = "90"  # REQ-CI-19
IMAGE_RE = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")
PIN_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?")  # 00-marco D-66
# El sed de 'Instalar el núcleo' (09-ci REQ-CI-12), traducido a una expresión de Python.
SED_PIN_RE = re.compile(r'^core_pin:[ \t]*"([0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?)"[ \t]*(#.*)?$', re.M)

PR_MERGE = 24  # tarea 10: fusión de main en task/10 que agrega un secreto (solo en su escenario)
LEAK_FILE = "notes/leak.txt"  # data/* lo ignora el .gitignore de la plantilla
CHECK_NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


# -- el repositorio recién creado ---------------------------------------------------------


def test_repository_from_the_template_has_exactly_the_manifest(ci: TemplateCi) -> None:
    """REQ-MVP-4 y D-36: el render y el commit raíz tienen exactamente las rutas del manifiesto."""
    manifest = MANIFEST.read_text(encoding="utf-8").splitlines()
    assert ci.rendered == manifest
    tracked = git(ci.root / "dev", ci.git_env, "ls-tree", "-r", "--name-only", ci.root_sha).splitlines()
    assert sorted(tracked) == manifest  # ni .gitignore ni .gitattributes dejan fuera un archivo


def test_validate_tree_has_no_errors(launcher: Launcher, ci: TemplateCi, tmp_path: Path) -> None:
    checkout = ci.clone_main(tmp_path / "main")
    outside = runner_temp(tmp_path)
    run = launcher.run(
        ["validate", "--mode", "tree", "--tree", str(checkout), "--report", str(outside / "tree.json")],
        cwd=outside, env=ci.env(tmp_path),
    )
    report = _envelope(outside / "tree.json", run)
    assert run.returncode == 0, run.explain()
    assert report["ok"] is True and report["data"]["mode"] == "tree"
    assert set(_statuses(report)) == TREE_RULES
    assert _findings(report, "error") == []


@pytest.mark.parametrize("target", ["root", f"merge-{PR_CONTENT}", f"merge-{PR_PIN}"])
def test_validate_base_root_commit_and_with_parent(
    launcher: Launcher, ci: TemplateCi, tmp_path: Path, target: str
) -> None:
    """``validate --mode base --policy . --tree .`` de main-broken.yml (09-ci §7.1; D-33)."""
    if target == "root":
        checkout = ci.clone_main(tmp_path / "base")
        before = None
    else:
        pr = ci.prs[int(target.split("-")[1])]
        checkout = ci.checkout_merge(tmp_path / "base", pr)
        before = ci.root_sha
    rt = runner_temp(tmp_path)
    line = workflow_command("validate --mode base")
    run = launcher.run_line(line, {"RUNNER_TEMP": str(rt)}, cwd=checkout, env=ci.env(tmp_path))
    report = _envelope(rt / "base-validate.json", run)
    assert run.returncode == 0, run.explain()
    assert report["ok"] is True and report["data"]["mode"] == "base"
    assert report["data"]["base"]["sha"] == before
    assert set(_statuses(report)) == BASE_RULES
    assert _findings(report, "error") == []


def test_sync_roles_check_from_outside_the_tree(launcher: Launcher, ci: TemplateCi, tmp_path: Path) -> None:
    checkout = ci.clone_main(tmp_path / "main")
    run = launcher.run(["--root", str(checkout), "sync-roles", "--check"], cwd=runner_temp(tmp_path),
                       env=ci.env(tmp_path))
    assert run.returncode == 0, run.explain()
    assert git(checkout, ci.git_env, "status", "--porcelain") == ""


# -- compile.yml y build.yml --------------------------------------------------------------


@pytest.mark.parametrize("number", [PR_CONTENT, PR_PIN, PR_RENAME])
def test_policy_steps_read_the_base_of_the_merge_commit(ci: TemplateCi, tmp_path: Path, number: int) -> None:
    """``git show HEAD^1:project.yaml`` y ``git diff --name-only --no-renames HEAD^1 HEAD`` (REQ-CI-16).

    El ``git diff`` es el del paso del workflow (``changed_diff_args``): en el PR 23, el
    renombre aparece con sus dos rutas.
    """
    pr = ci.prs[number]
    checkout = ci.checkout_merge(tmp_path / "compile", pr)
    shown = subprocess.run(["git", "show", "HEAD^1:project.yaml"], cwd=checkout, env=ci.git_env,
                           capture_output=True, check=True).stdout
    assert shown == ci.project_bytes  # la política es la base, nunca la cabeza del PR
    args = changed_diff_args(checkout)
    assert "--no-renames" in args  # 09-ci §3.2 (REQ-CI-8)
    changed = git(checkout, ci.git_env, *args).splitlines()
    assert changed == pr.changed
    # 'Instalar el núcleo': el sed del workflow extrae el pin de la política.
    pins = SED_PIN_RE.findall(shown.decode("utf-8"))
    assert [p for p, _comment in pins] == [ci.project.core_pin]


@pytest.mark.parametrize(
    ("number", "mode"),
    [
        # D-43: el PR 21 solo cambia su parte, sections/introduction/, que es entrada de la compilación (09-ci §3.2).
        (PR_CONTENT, "pr"),
        (PR_PIN, "pr"),
        (PR_RENAME, "pr"),
        (PR_CONTENT, "base"),
    ],
)
def test_build_plan_emits_valid_values(
    launcher: Launcher, ci: TemplateCi, tmp_path: Path, number: int, mode: str
) -> None:
    pr = ci.prs[number]
    _checkout, _env, pairs = build_plan(launcher, ci, pr, tmp_path, mode, github_sha=pr.merge_sha)
    assert [k for k, _ in pairs] == list(CI_COMMAND_OUTPUTS["build-plan"])
    out = dict(pairs)
    build = ci.project.build
    assert out["run"] == "true"  # el PR cambia tex/ o project.yaml; en la base siempre se compila
    assert IMAGE_RE.fullmatch(out["image"]) and out["image"] == build.image
    assert out["engine_flag"] == ENGINE_FLAGS[build.engine]
    assert out["shell"] == SHELL_FLAGS[build.shell_escape]
    assert 1 <= int(out["timeout"]) <= 12 and out["timeout"] == str(build.timeout_minutes)
    assert out["main"] == build.main and out["main_base"] == Path(build.main).stem
    assert out["sha7"] == pr.merge_sha[:7]
    expected_retention = str(ci.project.ci.get("pdf_retention_days", 14)) if mode == "pr" else BASE_RETENTION
    assert out["retention"] == expected_retention
    assert out["latexdiff"] == ("true" if ci.project.ci.get("latexdiff") else "false")
    assert out["language"] == ci.lang


def test_build_plan_takes_sha7_from_git_without_github_sha(
    launcher: Launcher, ci: TemplateCi, tmp_path: Path
) -> None:
    pr = ci.prs[PR_CONTENT]
    _checkout, _env, pairs = build_plan(launcher, ci, pr, tmp_path, "pr")
    assert dict(pairs)["sha7"] == pr.merge_sha[:7]


def test_build_plan_compiles_a_pr_that_moves_a_tex_file_out_of_tex(
    launcher: Launcher, ci: TemplateCi, tmp_path: Path
) -> None:
    """``git mv tex/notation.tex old/notation.tex``: ``changed.txt`` lista el origen y se compila (REQ-CI-8).

    Sin ``--no-renames``, git detecta el renombre y solo lista ``old/notation.tex``, que no es
    una entrada de la compilación: ``build-plan`` daría ``run=false`` y ``aucove/build``
    quedaría en verde sin compilar, aunque ``main.tex`` haga ``\\input{notation}``.
    """
    pr = ci.prs[PR_RENAME]
    checkout, _env, pairs = build_plan(launcher, ci, pr, tmp_path, "pr", github_sha=pr.merge_sha)
    changed = (runner_temp(tmp_path) / "changed.txt").read_text(encoding="utf-8").splitlines()
    note = f"memory/notes/task-{pr.task}.md"  # la nota de sesión que deja submit
    assert changed == [note, MOVED_NOTATION, NOTATION]
    assert dict(pairs)["run"] == "true"
    # El caso es sensible: con la detección de renombres por defecto, el origen no aparece.
    assert git(checkout, ci.git_env, "diff", "--name-only", "HEAD^1", "HEAD").splitlines() == [note, MOVED_NOTATION]


def _build_log(launcher: Launcher, checkout: Path, env: Mapping[str, str], rt: Path, main_base: str,
               log: Path) -> tuple[Run, Path]:
    """Paso 'Diagnóstico estructurado' de compile.yml con ``log`` como log de la compilación."""
    target = checkout / "tex" / "build" / f"{main_base}.log"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(log, target)
    report = rt / "out" / "diagnostics.json"
    report.unlink(missing_ok=True)
    run = launcher.run_line(workflow_command("validate --mode build-log"),
                            {"MAIN_BASE": main_base, "RUNNER_TEMP": str(rt)}, cwd=checkout, env=env)
    return run, report


@pytest.mark.parametrize("log", sorted(p.name for p in BUILD_LOGS.glob("*.log")))
def test_build_log_diagnostics(launcher: Launcher, ci: TemplateCi, tmp_path: Path, log: str) -> None:
    """``validate --mode build-log`` sobre los logs de ``BUILD_LOGS`` deja un diagnostics.json válido."""
    pr = ci.prs[PR_CONTENT]
    checkout, env, pairs = build_plan(launcher, ci, pr, tmp_path, "pr", github_sha=pr.merge_sha)
    run, report_path = _build_log(launcher, checkout, env, runner_temp(tmp_path), dict(pairs)["main_base"],
                                  BUILD_LOGS / log)
    report = _envelope(report_path, run)
    assert report["data"]["mode"] == "build-log"
    assert [r["check"] for r in report["data"]["results"]] == ["latex-log"]
    diagnostics = load_diagnostics(report_path)
    assert diagnostics is not None
    errors = [d for d in diagnostics if d.severity == "error"]
    if log == "clean.log":
        assert errors == [] and run.returncode == 0, run.explain()
    else:
        assert errors, diagnostics
        assert run.returncode == EXIT_VALIDATION, run.explain()  # el workflow lo tolera con '|| true'
        assert any(line.startswith("::error") for line in run.stdout.splitlines()), run.stdout


# -- validate.yml -------------------------------------------------------------------------


def pin_plan(launcher: Launcher, ci: TemplateCi, ws: Path, env: Mapping[str, str], rt: Path) -> dict[str, str]:
    output = rt / "github-output"
    output.write_bytes(b"")
    tree = ["--tree", str(ws / "pr")] if (ws / "pr").is_dir() else []  # TREE=(); [ -d … ] && TREE=(…)
    run = launcher.run_line(workflow_command("ci pin-plan"),
                            {"GITHUB_WORKSPACE": str(ws), "GITHUB_OUTPUT": str(output)},
                            cwd=rt, env=env, arrays={"TREE": tree})
    assert run.returncode == 0, run.explain()
    pairs = parse_outputs(output.read_text(encoding="utf-8"))
    assert [k for k, _ in pairs] == list(CI_COMMAND_OUTPUTS["pin-plan"])
    out = dict(pairs)
    assert out["run"] in ("true", "false") and PIN_RE.fullmatch(out["pin"]), out
    return out


@pytest.mark.parametrize(
    ("number", "fork", "expected_run"),
    [(PR_CONTENT, False, "false"), (PR_CONTENT, True, "false"), (PR_PIN, False, "true")],
)
def test_pin_plan(launcher: Launcher, ci: TemplateCi, tmp_path: Path, number: int, fork: bool,
                  expected_run: str) -> None:
    ws = ci.workspace(tmp_path, ci.prs[number], fork=fork)
    out = pin_plan(launcher, ci, ws, ci.env(tmp_path), runner_temp(tmp_path))
    assert out["run"] == expected_run
    if expected_run == "true":
        assert out["pin"] == next_patch(ci.project.core_pin) != ci.project.core_pin


def run_pin_job(launcher: Launcher, ci: TemplateCi, ws: Path, env: Mapping[str, str]) -> tuple[Run, Path]:
    """Paso 'Reglas que dependen de la versión del núcleo' (cwd = $GITHUB_WORKSPACE)."""
    run = launcher.run_line(workflow_command("validate --mode tree"), {}, cwd=ws, env=env)
    return run, ws / "pin.json"


def test_pin_job_leaves_a_valid_report(launcher: Launcher, ci: TemplateCi, tmp_path: Path) -> None:
    ws = ci.workspace(tmp_path, ci.prs[PR_PIN])
    env = ci.env(tmp_path)
    assert pin_plan(launcher, ci, ws, env, runner_temp(tmp_path))["run"] == "true"
    run, pin = run_pin_job(launcher, ci, ws, env)
    assert pin.is_file() and pin.stat().st_size > 0  # test -s pin.json
    report = _envelope(pin, run)
    assert run.returncode == 0, run.explain()
    assert report["op"] == "validate" and report["data"]["mode"] == "tree"
    assert _statuses(report) == {rule: "pass" for rule in PIN_RULES}


def validate_pr(launcher: Launcher, ws: Path, rt: Path, env: Mapping[str, str], number: int, *,
                pin_ran: bool) -> tuple[Run, Path]:
    """Paso 'Validar' de validate.yml (cwd = $RUNNER_TEMP, nunca dentro del árbol del PR)."""
    tree = ["--tree", str(ws / "pr")] if (ws / "pr").is_dir() else []
    pinrep = ["--pin-report", str(rt / "pin" / "pin.json")] if pin_ran else []
    run = launcher.run_line(
        workflow_command("validate --mode pr"),
        {"PR_NUMBER": str(number), "GITHUB_WORKSPACE": str(ws), "RUNNER_TEMP": str(rt)},
        cwd=rt, env=env, arrays={"TREE": tree, "PINREP": pinrep},
    )
    return run, rt / "validate.json"


@pytest.mark.parametrize("number", [PR_CONTENT, PR_PIN])
def test_validate_pr_without_pin_report(launcher: Launcher, ci: TemplateCi, tmp_path: Path, number: int) -> None:
    ws = ci.workspace(tmp_path, ci.prs[number])
    rt = runner_temp(tmp_path)
    env = ci.env(tmp_path)
    env["FAKE_GITLEAKS_LOG"] = str(tmp_path / "gitleaks-call.json")
    run, path = validate_pr(launcher, ws, rt, env, number, pin_ran=False)
    report = _envelope(path, run)
    assert run.returncode == 0, run.explain()
    assert report["ok"] is True and report["data"]["pr"] == number
    assert _statuses(report) == {rule: "pass" for rule in PR_RULES}
    assert not [line for line in run.stdout.splitlines() if line.startswith("::error")]
    # secrets usó el doble de gitleaks (AUCOVE_GITLEAKS), no un binario del sistema.
    call = read_json(tmp_path / "gitleaks-call.json")
    assert call["argv"][0] == "git"


def test_validate_pr_consumes_the_pin_report(launcher: Launcher, ci: TemplateCi, tmp_path: Path) -> None:
    pr = ci.prs[PR_PIN]
    ws = ci.workspace(tmp_path, pr)
    rt = runner_temp(tmp_path)
    env = ci.env(tmp_path)
    assert pin_plan(launcher, ci, ws, env, rt)["run"] == "true"
    pin_run, pin = run_pin_job(launcher, ci, ws, env)
    assert pin_run.returncode == 0, pin_run.explain()
    (rt / "pin").mkdir()
    shutil.copyfile(pin, rt / "pin" / "pin.json")  # upload-artifact validate-pin + download-artifact
    run, path = validate_pr(launcher, ws, rt, env, pr.number, pin_ran=True)
    report = _envelope(path, run)
    assert run.returncode == 0, run.explain()
    assert _statuses(report) == {rule: "pass" for rule in PR_RULES}

    # El reporte es el dato: si dice que roles-sync falló, validate lo toma tal cual
    # aunque el árbol esté sincronizado (REQ-CI-45).
    tampered = read_json(pin)
    for result in tampered["data"]["results"]:
        if result["check"] == "roles-sync":
            result["status"] = "fail"
    (rt / "pin" / "pin.json").write_text(json.dumps(tampered), encoding="utf-8")
    run, path = validate_pr(launcher, ws, rt, env, pr.number, pin_ran=True)
    report = _envelope(path, run)
    assert run.returncode == EXIT_VALIDATION, run.explain()
    statuses = _statuses(report)
    assert statuses["roles-sync"] == "fail"
    assert {k: v for k, v in statuses.items() if k != "roles-sync"} == {
        rule: "pass" for rule in PR_RULES - {"roles-sync"}
    }


def test_validate_pr_with_pin_ran_and_no_report(launcher: Launcher, ci: TemplateCi, tmp_path: Path) -> None:
    """``PIN_RAN=true`` sin reporte legible: las tres reglas del pin fallan (REQ-CI-45, REQ-CI-8)."""
    pr = ci.prs[PR_PIN]
    ws = ci.workspace(tmp_path, pr)
    rt = runner_temp(tmp_path)
    run, path = validate_pr(launcher, ws, rt, ci.env(tmp_path), pr.number, pin_ran=True)
    report = _envelope(path, run)
    assert run.returncode == EXIT_VALIDATION, run.explain()
    statuses = _statuses(report)
    assert {rule: statuses[rule] for rule in PIN_RULES} == {rule: "fail" for rule in PIN_RULES}
    assert {k: v for k, v in statuses.items() if k not in PIN_RULES} == {
        rule: "pass" for rule in PR_RULES - set(PIN_RULES)
    }
    for f in _findings(report, "error"):
        assert f["check"] in PIN_RULES and f["code"] == "E_INTERNAL_UNEXPECTED", f


# El binario real solo está en el job test del CI del framework (Linux), que lo baja
# verificado por sha256; en local estas pruebas no pueden correr.
REAL_GITLEAKS = shutil.which("gitleaks")
needs_real_gitleaks = pytest.mark.skipif(REAL_GITLEAKS is None, reason="gitleaks no está instalado (solo en CI)")
FAKE_GITLEAKS_TOOL: tuple[str, ...] = (sys.executable, str(FIXTURES / "tools" / "fake_gitleaks.py"))


def validate_secrets(launcher: Launcher, ws: Path, rt: Path, env: Mapping[str, str],
                     number: int) -> tuple[Run, dict[str, Any]]:
    """Paso 'Validar' de validate.yml con ``--check secrets`` (cwd = $RUNNER_TEMP)."""
    ex = expand(workflow_command("validate --mode pr").line,
                {"PR_NUMBER": str(number), "GITHUB_WORKSPACE": str(ws), "RUNNER_TEMP": str(rt)},
                {"TREE": ["--tree", str(ws / "pr")], "PINREP": []})
    run = launcher.run([*ex.argv, "--check", "secrets"], cwd=rt, env=env)
    return run, _envelope(rt / "validate.json", run)


@needs_real_gitleaks
def test_secrets_with_the_real_gitleaks(launcher: Launcher, ci: TemplateCi, tmp_path: Path) -> None:
    ws = ci.workspace(tmp_path, ci.prs[PR_CONTENT])
    env = without(ci.env(tmp_path), PREFIX + "GITLEAKS")
    run, report = validate_secrets(launcher, ws, runner_temp(tmp_path), env, PR_CONTENT)
    assert run.returncode == 0, run.explain()
    assert _statuses(report) == {"secrets": "pass"}


def fake_leak() -> str:
    """Patrón del doble de gitleaks; se arma por partes para que ningún escáner lo vea aquí."""
    return "FAKE" + "LEAK" + f"{PR_MERGE:016d}"


def github_token() -> str:
    """Token ``ghp_`` de 36 caracteres para la regla ``github-pat`` de gitleaks.

    Sale de un generador con semilla fija: no está escrito en el código y es el mismo en cada
    corrida. Alterna letra y dígito, así que no forma palabras (``stopwords``) y su entropía
    queda muy por encima del umbral de la regla.
    """
    rng = random.Random("p21-merge-secret")
    return "ghp_" + "".join(rng.choice(string.ascii_letters) + rng.choice(string.digits) for _ in range(18))


@dataclass(frozen=True)
class MergeSecret:
    ci: TemplateCi  # copia del escenario con el remoto y el estado del fake de la fusión
    pr: Pr
    base_sha: str  # punta de main, que es el merge-base del PR


def merge_secret_scenario(ci: TemplateCi, tmp: Path, secret_line: str) -> MergeSecret:
    """``task/10`` integra ``main`` con ``git merge --no-commit`` y agrega ``notes/leak.txt`` en la fusión.

    Es el flujo de ``update`` (05-operaciones): ``main`` avanzó con otra tarea y la rama de la
    tarea lo integra. Se trabaja sobre una copia del remoto y del estado del fake, sin tocar
    el escenario compartido. La cabeza del PR 24 es el commit de fusión; ``validate`` analiza
    ``<merge-base>..<cabeza>`` y gitleaks solo ve el archivo si recibe
    ``--diff-merges=first-parent`` (REQ-SEC-36).
    """
    genv = ci.git_env
    tmp.mkdir(parents=True)
    origin = tmp / "origin.git"
    shutil.copytree(ci.origin, origin)
    dev = tmp / "dev"
    git(tmp, genv, "clone", "-q", "--branch", "main", origin.as_uri(), str(dev))
    lead = ci.lead
    task = 10
    branch = f"task/{task}"

    def g(*args: str) -> str:
        return git(dev, genv, *args)

    def commit(message: str, *, allow_empty: bool = False) -> str:
        g("add", "-A")
        g("commit", "-q", "--no-verify", f"--author={lead} <{lead}@example.invalid>", "-m", message,
          *(["--allow-empty"] if allow_empty else []))
        return g("rev-parse", "HEAD")

    readme = dev / "data" / "README.md"
    readme.write_bytes(readme.read_bytes() + b"\nAnother task already merged into main.\n")
    base_sha = commit("Another task")
    g("push", "-q", "origin", "main")
    g("checkout", "-q", "-b", branch, ci.root_sha)
    claim_sha = commit(f"{_meta.CLAIM_SUBJECT}{task}", allow_empty=True)
    (dev / INTRO).write_bytes((dev / INTRO).read_bytes() + b"\nThis sentence was added by task 10.\n")
    commit(f"Task {task}")
    g("merge", "-q", "--no-ff", "--no-commit", "main")
    (dev / LEAK_FILE).parent.mkdir(parents=True, exist_ok=True)
    (dev / LEAK_FILE).write_text(secret_line + "\n", encoding="utf-8", newline="\n")
    head_sha = commit(f"Merge branch 'main' into {branch}")
    assert len(g("rev-list", "--parents", "-n", "1", head_sha).split()) == 3  # es una fusión
    assert g("ls-tree", "--name-only", head_sha, LEAK_FILE) == LEAK_FILE  # la fusión lo trae
    assert g("merge-base", "main", head_sha) == base_sha
    g("push", "-q", "origin", branch)
    g("checkout", "-q", "--detach", "main")
    g("merge", "-q", "--no-ff", "--no-edit", "-m", f"Merge {head_sha} into {base_sha}", branch)
    merge_sha = g("rev-parse", "HEAD")
    g("push", "-q", "origin", f"{merge_sha}:refs/pull/{PR_MERGE}/merge")
    files = (("modified", INTRO), ("added", LEAK_FILE))  # diff de tres puntos contra main
    pr = Pr(PR_MERGE, task, branch, claim_sha, head_sha, merge_sha, files)

    forge = FakeForge.load(ci.state0)
    seed_git_history(forge, dev, genv, (f"^{ci.root_sha}", "main", branch))
    forge.seed_branch(ci.project.base_branch, base_sha)
    forge.seed_branch(branch, head_sha)
    seed_claimed_task(
        forge, number=task, lead=lead, claimant=lead, sections=["introduction"], language=ci.lang,
        claim_sha=claim_sha, base_sha=ci.root_sha, head_sha=head_sha,
        summary=f"Task {task} of the template self-test.",
    )
    seed_pull(forge, number=PR_MERGE, author=lead, head_branch=branch, head_sha=head_sha, files=files,
              base_branch=ci.project.base_branch)
    state0 = tmp / "fake-state-merge.json"
    forge.save(state0)
    copy = dataclasses.replace(ci, root=tmp, origin=origin, prs={PR_MERGE: pr}, state0=state0)
    return MergeSecret(copy, pr, base_sha)


def secrets_rule_without_merge_diffs(scenario: MergeSecret, ws: Path, tool: Sequence[str],
                                     monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """La regla ``secrets`` en proceso, con ``LOG_OPTS`` sin ``--diff-merges=first-parent``.

    Demuestra que el escenario es sensible: sin esa opción, ``git log -p`` no muestra el diff
    del commit de fusión y gitleaks no ve el secreto.
    """
    for key, value in scenario.ci.git_env.items():
        if key.startswith("GIT_"):
            monkeypatch.setenv(key, value)
    reduced = tuple(o for o in secrets.LOG_OPTS if o != "--diff-merges=first-parent")
    assert len(reduced) == len(secrets.LOG_OPTS) - 1, secrets.LOG_OPTS
    monkeypatch.setattr(secrets, "LOG_OPTS", reduced)
    tree_dir = ws / "pr"
    inp = RuleInput(mode="pr", t=translator("en"), now=CHECK_NOW, tree=git_tree(tree_dir, scenario.pr.head_sha),
                    base_tree=git_tree(tree_dir, scenario.base_sha), head_sha=scenario.pr.head_sha,
                    tools={"gitleaks": tuple(tool)})
    return list(secrets.RULE.run(inp))


def assert_secret_in_the_merge(run: Run, report: Mapping[str, Any], scenario: MergeSecret) -> None:
    assert run.returncode == EXIT_VALIDATION, run.explain()
    assert _statuses(report) == {"secrets": "fail"}
    errors = _findings(report, "error")
    assert [(f["code"], f["path"]) for f in errors] == [("E_VALIDATION_SECRET", LEAK_FILE)], errors
    assert scenario.pr.head_sha[:12] in errors[0]["message"], errors[0]  # el commit de fusión


def test_secrets_reports_a_secret_added_by_a_merge_in_the_task_branch(
    launcher: Launcher, ci: TemplateCi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Con el doble de gitleaks (``<PREFIJO>GITLEAKS``), que corre ``git log -p`` con el mismo ``--log-opts``."""
    leak = fake_leak()
    scenario = merge_secret_scenario(ci, tmp_path / "scenario", f"clave = {leak}")
    ws = scenario.ci.workspace(tmp_path, scenario.pr)
    run, report = validate_secrets(launcher, ws, runner_temp(tmp_path), scenario.ci.env(tmp_path), PR_MERGE)
    assert_secret_in_the_merge(run, report, scenario)
    assert leak not in run.stdout + run.stderr + json.dumps(report)  # --redact: nunca el secreto
    assert secrets_rule_without_merge_diffs(scenario, ws, FAKE_GITLEAKS_TOOL, monkeypatch) == []


@needs_real_gitleaks
def test_secrets_with_the_real_gitleaks_in_a_merge_commit(
    launcher: Launcher, ci: TemplateCi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gitleaks 8.30.1 informa el ``ghp_`` que agrega la fusión gracias a ``--diff-merges=first-parent``."""
    assert REAL_GITLEAKS is not None
    token = github_token()
    # Solo el token, sin una palabra clave delante (``token =``), para que lo informe una
    # única regla (github-pat) y no también generic-api-key.
    scenario = merge_secret_scenario(ci, tmp_path / "scenario", token)
    ws = scenario.ci.workspace(tmp_path, scenario.pr)
    env = without(scenario.ci.env(tmp_path), PREFIX + "GITLEAKS")
    run, report = validate_secrets(launcher, ws, runner_temp(tmp_path), env, PR_MERGE)
    assert_secret_in_the_merge(run, report, scenario)
    assert token not in run.stdout + run.stderr + json.dumps(report)
    assert secrets_rule_without_merge_diffs(scenario, ws, (REAL_GITLEAKS,), monkeypatch) == []


# -- comentarios (build.yml y validate.yml) -----------------------------------------------


def test_ci_comment_leaves_one_comment_per_kind(launcher: Launcher, ci: TemplateCi, tmp_path: Path) -> None:
    pr = ci.prs[PR_CONTENT]
    # validate.yml: Validar + Comentario localizado (dos corridas: re-run del job).
    ws = ci.workspace(tmp_path / "v", pr)
    rt_v = runner_temp(tmp_path / "v")
    env = ci.env(tmp_path)
    state = Path(env[PREFIX + "FORGE_FAKE_STATE"])
    run, _ = validate_pr(launcher, ws, rt_v, env, pr.number, pin_ran=False)
    assert run.returncode == 0, run.explain()
    for _ in range(2):
        comment = launcher.run_line(
            workflow_command("ci comment --kind validate"),
            {"PR_NUMBER": str(pr.number), "GITHUB_WORKSPACE": str(ws), "RUNNER_TEMP": str(rt_v),
             "GITHUB_STEP_SUMMARY": str(rt_v / "summary.md")},
            cwd=ws, env={**env, "GITHUB_TOKEN": "fake-token"},
        )
        assert comment.returncode == 0, comment.explain()
    assert len(comments_with_marker(state, pr.number, "validate")) == 1

    # compile.yml + build.yml: plan, diagnóstico, artefacto build-report-<sha7> y comentario.
    tmp_b = tmp_path / "b"
    checkout, _env, pairs = build_plan(launcher, ci, pr, tmp_b, "pr", github_sha=pr.merge_sha)
    plan = dict(pairs)
    rt_b = runner_temp(tmp_b)
    _run, diagnostics = _build_log(launcher, checkout, env, rt_b, plan["main_base"], BUILD_LOGS / "fatal-error.log")
    reports = rt_b / "reports"
    reports.mkdir()
    shutil.copyfile(diagnostics, reports / "diagnostics.json")  # download-artifact merge-multiple
    expected = [d for d in load_diagnostics(diagnostics) or [] if d.severity == "error"]
    assert expected
    variables = {
        "PR_NUMBER": str(pr.number), "LANG_CODE": plan["language"], "OUTCOME": "failure",
        "PDF_URL": "", "DIFF_URL": "", "SKIP_REASON": plan["reason"], "RUN_URL": FAKE_URL, "RUNNER_TEMP": str(rt_b),
        "GITHUB_STEP_SUMMARY": str(rt_b / "summary.md"),
    }
    for _ in range(2):
        comment = launcher.run_line(workflow_command("ci comment --kind build"), variables, cwd=checkout,
                                    env={**env, "GITHUB_TOKEN": "fake-token"})
        assert comment.returncode == 0, comment.explain()
    bodies = comments_with_marker(state, pr.number, "build")
    assert len(bodies) == 1
    # El comentario de build lee el diagnostics.json: aparece el primer error.
    first = expected[0]
    assert (first.file or "") in bodies[0] and first.message[:40] in bodies[0], bodies[0]
    assert len(comments_with_marker(state, pr.number, "validate")) == 1
    assert (rt_b / "summary.md").read_text(encoding="utf-8").strip()


# -- main-broken.yml (H3) con ci base-report de H2 (D-31 b) -------------------------------


def test_ci_base_report_opens_and_closes_the_issue(launcher: Launcher, ci: TemplateCi, tmp_path: Path) -> None:
    # ``ci base-report`` es de H2 (D-31 b, REQ-MVP-21): se importa directamente, sin
    # ``importorskip``, para que un módulo renombrado o borrado haga fallar la prueba en
    # lugar de omitirla.
    from aucove.ops import ci_base_report

    assert [c.name for c in ci_base_report.COMMANDS] == ["base-report"]
    checkout = ci.clone_main(tmp_path / "main")
    rt = runner_temp(tmp_path)
    (rt / "reports").mkdir()
    env = ci.env(tmp_path)
    state = Path(env[PREFIX + "FORGE_FAKE_STATE"])
    line = workflow_command("ci base-report")
    marker = _meta.ci_marker("main-broken")

    def report(build: str, validate: str, run_id: int) -> dict[str, str]:
        run = launcher.run_line(
            line,
            {"GITHUB_SHA": ci.root_sha, "BUILD": build, "BUILD_REASON": "", "VALIDATE": validate, "PDF_URL": "",
             "RUN_URL": f"{FAKE_URL.rsplit('/', 1)[0]}/{run_id}", "RUNNER_TEMP": str(rt),
             "GITHUB_STEP_SUMMARY": str(rt / "summary.md")},
            cwd=checkout, env={**env, "GITHUB_TOKEN": "fake-token"},
        )
        assert run.returncode == 0, run.explain()
        out = dict(parse_outputs(run.stdout))
        assert set(out) == {"status", "action", "issue"}, run.stdout
        return out

    def marked(state_filter: str) -> list[Any]:
        forge = FakeForge.load(state)
        return [i for i in forge.list_issues(state=state_filter) if marker in i.body]

    first = report("failure", "success", 1)
    assert first["status"] == "broken" and first["action"] == "created", first
    issues = marked("open")
    assert len(issues) == 1 and str(issues[0].number) == first["issue"]
    assert "main-broken" in issues[0].labels and ci.lead in issues[0].assignees  # asignado al lead
    second = report("success", "failure", 2)  # otra corrida roja: comenta en el mismo Issue
    assert second["status"] == "broken" and second["action"] == "commented", second
    assert [i.number for i in marked("open")] == [issues[0].number]  # exactamente uno (REQ-CI-32)
    third = report("success", "success", 3)
    assert third["status"] == "green" and third["action"] == "closed", third
    assert marked("open") == []  # resuelto por <sha> y cerrado (REQ-CI-34)
    assert len(marked("all")) == 1


# -- compilación real (CI, con Docker) ------------------------------------------------------


def _docker_missing() -> str | None:
    if not sys.platform.startswith("linux"):
        return "la compilación en el sandbox de REQ-CI-13/14 corre en Linux (CI)"
    if shutil.which("docker") is None or shutil.which("bash") is None:
        return "no hay docker o bash en el PATH"
    return None


def test_sandbox_steps_are_in_compile_yml(ci: TemplateCi) -> None:
    """Los pasos que lee la autoprueba existen en el render y no están vacíos (también sin Docker)."""
    workflow = compile_yml(ci.root / "dev")
    for step in (POLICY_STEP, TEX_STEP):
        assert _step_script(workflow, step).strip(), step
    assert "rm -rf tex/build" in _step_script(workflow, name=EMPTY_OUTPUTS_STEP)


@pytest.mark.docker
def test_template_compiles_in_the_sandbox(launcher: Launcher, ci: TemplateCi, tmp_path: Path) -> None:
    """Pasos 'Imagen fijada', 'Salidas de TeX vacías', 'Compilar en sandbox' y 'Diagnóstico' de compile.yml.

    La compilación usa -Werror, como en el workflow. Es la prueba de humo de 02-convencion §4.7
    con la imagen por defecto (TL2026): con -cd en tex/, biber resuelve ../bib/references.bib y
    ../sections/introduction/pending.bib, y \\input{../sections/...} compila sin openin_any (D-43).

    Sin Docker se omite, salvo con ``<PREFIJO>REQUIRE_DOCKER=1`` (job template-ci del CI del
    framework), donde falla.
    """
    reason = _docker_missing()
    if reason is not None:
        if os.environ.get(PREFIX + "REQUIRE_DOCKER") == "1":
            pytest.fail(reason)
        pytest.skip(reason)
    pr = ci.prs[PR_CONTENT]
    checkout, env, pairs = build_plan(launcher, ci, pr, tmp_path, "pr", github_sha=pr.merge_sha)
    plan = dict(pairs)
    workflow = compile_yml(checkout)
    subprocess.run(["docker", "pull", plan["image"]], check=True, timeout=1800)
    step_env = {
        **os.environ,
        "IMAGE": plan["image"], "ENGINE_FLAG": plan["engine_flag"], "SHELL_FLAG": plan["shell"],
        "MAIN": plan["main"], "GITHUB_WORKSPACE": str(checkout),
    }
    empty = subprocess.run(["bash", "-c", _step_script(workflow, name=EMPTY_OUTPUTS_STEP)], cwd=checkout,
                           env=step_env, capture_output=True, timeout=60, check=False)
    assert empty.returncode == 0, empty.stderr.decode("utf-8", "replace")
    tex = subprocess.run(["bash", "-c", _step_script(workflow, TEX_STEP)], cwd=checkout, env=step_env,
                         capture_output=True, timeout=60 * int(plan["timeout"]), check=False)
    rt = runner_temp(tmp_path)
    log = checkout / "tex" / "build" / f"{plan['main_base']}.log"
    assert log.is_file(), tex.stderr.decode("utf-8", "replace")
    run = launcher.run_line(workflow_command("validate --mode build-log"),
                            {"MAIN_BASE": plan["main_base"], "RUNNER_TEMP": str(rt)}, cwd=checkout, env=env)
    diagnostics = load_diagnostics(rt / "out" / "diagnostics.json")
    assert diagnostics is not None, run.explain()
    assert [d for d in diagnostics if d.severity == "error"] == [], diagnostics
    assert tex.returncode == 0, tex.stderr.decode("utf-8", "replace")
    assert run.returncode == 0, run.explain()
    assert (checkout / "tex" / "build" / f"{plan['main_base']}.pdf").is_file()


# -- cobertura de las líneas de los workflows -----------------------------------------------


def _exercised(path: Path) -> tuple[str, ...]:
    """``EXERCISED`` de un módulo de ``tests/e2e``, leído del código (sin importarlo); vacío si no lo declara."""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        else:
            continue
        if isinstance(target, ast.Name) and target.id == "EXERCISED" and value is not None:
            found = ast.literal_eval(value)
            assert isinstance(found, tuple) and all(isinstance(p, str) for p in found), path.name
            return found
    return ()


def _workflow_command_calls(path: Path) -> list[str]:
    """El prefijo de cada llamada ``workflow_command("…")`` con un literal.

    Una llamada con un prefijo calculado (como las de esta prueba) no cuenta como ejercida.
    """
    out: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "workflow_command":
            first = node.args[0] if node.args else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                out.append(first.value)
    return out


def _matches(command: WorkflowCommand, prefix: str) -> bool:
    wanted = f"{_meta.NAME} {prefix}"
    return command.line == wanted or command.line.startswith(wanted + " ")


def test_every_workflow_command_is_exercised() -> None:
    """Entre todos los módulos de ``tests/e2e``, cada línea de los workflows la ejerce una prueba (D-37).

    Cada módulo declara en ``EXERCISED`` los prefijos de las líneas que ejecuta. Cada línea de
    ``WORKFLOW_COMMANDS`` coincide con exactamente uno de los prefijos declarados; cada
    prefijo nombra exactamente una línea y lo ejecuta alguna llamada ``workflow_command`` de
    ``tests/e2e``; y cada una de esas llamadas usa un prefijo declarado. La excepción son las
    líneas de ``PENDING_COMMANDS``, cuyo subcomando todavía no existe (desde 5d, ninguna).
    """
    modules = sorted(E2E_DIR.glob("test_*.py"))
    declared = {path.name: _exercised(path) for path in modules}
    assert declared[Path(__file__).name] == EXERCISED
    exercised = sorted({prefix for prefixes in declared.values() for prefix in prefixes})
    calls = [call for path in sorted(E2E_DIR.glob("*.py")) for call in _workflow_command_calls(path)]
    for command in WORKFLOW_COMMANDS:
        if any(_matches(command, p) for p in PENDING_COMMANDS):
            assert not any(_matches(command, p) for p in exercised), command
            continue
        matches = [p for p in exercised if _matches(command, p)]
        assert len(matches) == 1, f"{command} no lo ejerce ninguna prueba de tests/e2e: {matches} ({declared})"
    for prefix in exercised:
        workflow_command(prefix)  # cada prefijo nombra exactamente una línea
        assert prefix in calls, f"{prefix!r} está en EXERCISED, pero ninguna prueba lo ejecuta"
    for prefix in calls:
        assert prefix in exercised, f"workflow_command({prefix!r}) sin declararlo en EXERCISED"
