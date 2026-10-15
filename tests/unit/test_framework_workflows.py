# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""CI del repo del framework: ci.yml, dependabot.yml, la guía de mantenimiento
(.github/MAINTAINING.md) y tools/verify_action_pins.py.

Fuentes: 01-arquitectura REQ-ARCH-6 y REQ-ARCH-25; 12-mvp §3.1, REQ-MVP-21..23;
11-trazabilidad REQ-TRACE-58 y REQ-TRACE-66; 10-seguridad REQ-SEC-44, REQ-SEC-47,
REQ-SEC-48 y REQ-SEC-51; 09-ci REQ-CI-4..7; 00-marco D-07 (c), D-29, D-37, D-38 y D-39.

Sin red: las pruebas de verify_action_pins usan salidas simuladas de ``git ls-remote``,
del ``action.yml`` de cada acción y del registro. La única que usa la red lleva el
marcador ``live``.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import tomllib
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any

import pytest

from aucove.config.yamlload import load_yaml
from aucove.domain.findings import Finding
from aucove.i18n import translator
from aucove.ops.rules import ci_policy
from aucove.ops.rules.base import RuleInput
from aucove.vcs.tree import is_regular_mode
from tests.conftest import REPO_ROOT, isolated_git_env, load_tool

GITHUB_DIR = REPO_ROOT / ".github"
WORKFLOWS = GITHUB_DIR / "workflows"
CI = WORKFLOWS / "ci.yml"
DEPENDABOT = GITHUB_DIR / "dependabot.yml"
MAINTAINING = GITHUB_DIR / "MAINTAINING.md"
TEMPLATE_VARS = REPO_ROOT / "tools" / "template_vars.toml"
TEMPLATE_PROJECT = REPO_ROOT / "template" / "base" / "project.yaml"
TEMPLATE_WORKFLOWS = REPO_ROOT / "template" / "base" / ".github" / "workflows"
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)

# D-39: las acciones creadas por GitHub y astral-sh/setup-uv. REUSE no usa su acción
# Docker (FROM con etiqueta mutable): corre en una imagen fijada por digest (REQ-SEC-47).
THIRD_PARTY_ALLOWED: frozenset[str] = frozenset({"astral-sh/setup-uv"})
JOBS: frozenset[str] = frozenset({"test", "lint", "reuse", "template-ci", "dco"})
EVENTS: tuple[str, ...] = ("pull_request", "push", "workflow_dispatch", "workflow_call")  # release.yml lo llama

_USES_LINE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<target>\S+)(?:\s+#\s*(?P<comment>\S+))?\s*$")
_SHA_PIN = re.compile(r"^(?P<action>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_./-]+)?)@(?P<sha>[0-9a-f]{40})$")

def load_doc(path: Path) -> dict[Any, Any]:
    doc, findings = load_yaml(path.read_text(encoding="utf-8"), file=path.name)
    assert findings == [], [f.message for f in findings]
    assert isinstance(doc, dict)
    return doc


@pytest.fixture(scope="module")
def ci() -> dict[Any, Any]:
    return load_doc(CI)


@pytest.fixture(scope="module")
def pins() -> ModuleType:
    return load_tool("verify_action_pins")


def job(ci: Mapping[Any, Any], name: str) -> Mapping[str, Any]:
    value = ci["jobs"][name]
    assert isinstance(value, Mapping)
    return value


def steps(ci: Mapping[Any, Any], name: str) -> list[Mapping[str, Any]]:
    value = job(ci, name).get("steps")
    assert isinstance(value, list)
    return value


def run_lines(step: Mapping[str, Any]) -> list[str]:
    """Líneas de comando de un ``run:`` sin comentarios ni líneas vacías."""
    text = step.get("run")
    if not isinstance(text, str):
        return []
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def commands(ci: Mapping[Any, Any], name: str) -> list[tuple[int, str]]:
    """``(índice del paso, línea)`` de todos los ``run:`` del job, en orden."""
    return [(i, line) for i, step in enumerate(steps(ci, name)) for line in run_lines(step)]


def first_index(ci: Mapping[Any, Any], name: str, prefix: str) -> int:
    for i, line in commands(ci, name):
        if line.startswith(prefix):
            return i
    raise AssertionError(f"el job {name} no tiene ningún comando que empiece por {prefix!r}")


def uses_steps(ci: Mapping[Any, Any], name: str, action: str) -> list[Mapping[str, Any]]:
    return [s for s in steps(ci, name) if str(s.get("uses", "")).split("@", 1)[0] == action]


def template_vars() -> dict[str, Any]:
    return tomllib.loads(TEMPLATE_VARS.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Regla ci-policy (09-ci REQ-CI-4..7, REQ-SEC-47)
# ---------------------------------------------------------------------------


class MemTree:
    """TreeView en memoria con los workflows del framework."""

    def __init__(self, files: Mapping[str, str]) -> None:
        self._data = {p: v.encode("utf-8") for p, v in files.items()}
        self.root: Path | None = None
        self.sha: str | None = None

    def entries(self) -> Mapping[str, str]:
        return MappingProxyType({p: "100644" for p in self._data})

    def read_bytes(self, path: str, limit: int = 5 * 2**20) -> bytes | None:
        data = self._data.get(path)
        return data if data is not None and len(data) <= limit and is_regular_mode("100644") else None

    def read_text(self, path: str, limit: int = 5 * 2**20) -> str | None:
        data = self.read_bytes(path, limit)
        return data.decode("utf-8") if data is not None else None


def framework_workflows() -> dict[str, str]:
    return {
        f".github/workflows/{p.name}": p.read_text(encoding="utf-8")
        for p in sorted(WORKFLOWS.iterdir())
        if p.suffix in (".yml", ".yaml")
    }


def ci_policy_findings(files: Mapping[str, str]) -> list[Finding]:
    inp = RuleInput(mode="tree", t=translator("en"), now=NOW, tree=MemTree(files))
    return list(ci_policy.RULE.run(inp))


# REQ-CI-3 (los repos de proyecto no tienen secretos) es de la plantilla. En el framework rige
# 12-mvp REQ-MVP-22: un job con credenciales corre solo por etiqueta, por calendario o a mano, y
# con los secretos de un environment protegido. test_release_workflows comprueba en qué jobs.
SECRETS_ALLOWED_IN: frozenset[str] = frozenset({".github/workflows/nightly.yml", ".github/workflows/release.yml"})


def test_ci_policy_rule_has_no_findings_on_framework_workflows() -> None:
    files = framework_workflows()
    assert ".github/workflows/ci.yml" in files
    t = translator("en")
    secrets_message = t("validate.ci_policy.secrets", secret=ci_policy.EVIDENCE_VAR)
    # release.yml hereda los secretos solo en el job que llama a nightly.yml (test_release_workflows).
    inherit_message = t("validate.ci_policy.secrets_inherit", job="live")
    findings = [f for f in ci_policy_findings(files)
                if not (f.path in SECRETS_ALLOWED_IN and secrets_message in f.message)
                and not (f.path == ".github/workflows/release.yml" and inherit_message in f.message)]
    assert findings == [], [f"{f.path}:{f.line}: {f.message}" for f in findings]


def test_ci_policy_rule_is_not_vacuous_on_ci_yml() -> None:
    text = CI.read_text(encoding="utf-8")
    broken = text.replace("persist-credentials: false", "persist-credentials: true", 1)
    assert broken != text
    assert ci_policy_findings({".github/workflows/ci.yml": broken})


# ---------------------------------------------------------------------------
# Permisos, disparadores, fijaciones y checkout (REQ-CI-4..6, REQ-SEC-47, D-39)
# ---------------------------------------------------------------------------


def test_triggers_and_top_level_permissions(ci: Mapping[Any, Any]) -> None:
    assert ci["permissions"] == {}
    on = ci["on"]
    assert isinstance(on, Mapping)
    assert set(on) == set(EVENTS)
    assert on["push"] == {"branches": ["main"]}
    assert "pull_request_target" not in on and "workflow_run" not in on


def test_jobs_have_minimal_permissions_and_timeouts(ci: Mapping[Any, Any]) -> None:
    assert set(ci["jobs"]) == JOBS
    for name in JOBS:
        spec = job(ci, name)
        assert spec["permissions"] == {"contents": "read"}, name
        assert isinstance(spec["timeout-minutes"], int) and 1 <= spec["timeout-minutes"] <= 60, name


def test_no_secrets_and_no_event_data_in_run(ci: Mapping[Any, Any]) -> None:
    text = CI.read_text(encoding="utf-8")
    assert "secrets" not in text
    for name in JOBS:
        for _, line in commands(ci, name):
            assert "github.event" not in line and "github.head_ref" not in line, (name, line)


def test_every_uses_is_pinned_by_sha_with_its_version() -> None:
    lines = CI.read_text(encoding="utf-8").splitlines()
    found = 0
    for line in lines:
        m = _USES_LINE.match(line)
        if m is None:
            continue
        found += 1
        pin = _SHA_PIN.match(m.group("target"))
        assert pin is not None, line
        assert m.group("comment") is not None and re.match(r"^v\d+(\.\d+)*$", m.group("comment")), line
        action = pin.group("action")
        owner_repo = "/".join(action.split("/")[:2])
        assert action.startswith("actions/") or owner_repo in THIRD_PARTY_ALLOWED, f"D-39: {action}"
    assert found >= 9


def test_same_action_has_one_pin_in_ci_yml() -> None:
    by_action: dict[str, set[tuple[str, str]]] = {}
    for line in CI.read_text(encoding="utf-8").splitlines():
        m = _USES_LINE.match(line)
        if m is not None:
            action, _, sha = m.group("target").partition("@")
            by_action.setdefault(action, set()).add((sha, m.group("comment") or ""))
    assert by_action and all(len(v) == 1 for v in by_action.values()), by_action


def test_checkout_never_persists_credentials(ci: Mapping[Any, Any]) -> None:
    count = 0
    for name in JOBS:
        for step in uses_steps(ci, name, "actions/checkout"):
            count += 1
            with_ = step.get("with") or {}
            assert with_.get("persist-credentials") is False, name
            assert "allow-unsafe-pr-checkout" not in with_, name
            assert "ref" not in with_ and "repository" not in with_, name
    assert count == len(JOBS)


def test_setup_uv_without_actions_cache(ci: Mapping[Any, Any]) -> None:
    for name in ("test", "lint", "reuse", "template-ci"):
        found = uses_steps(ci, name, "astral-sh/setup-uv")
        assert len(found) == 1, name
        assert (found[0].get("with") or {}).get("enable-cache") is False, name


# ---------------------------------------------------------------------------
# Matriz de D-38 (enmienda p de REQ-ARCH-6 y REQ-MVP-21)
# ---------------------------------------------------------------------------


def test_matrix_is_complete_on_every_event(ci: Mapping[Any, Any]) -> None:
    """REQ-ARCH-6: Linux, macOS y Windows con 3.11 (D-01) y la última estable.

    Sin ``exclude``: el repo público la corre en cada PR, push y disparo manual (D-38 p; T1 y T4
    de 12-mvp §3.1).
    """
    matrix = job(ci, "test")["strategy"]["matrix"]
    assert set(matrix) == {"os", "python"}
    assert set(product(matrix["os"], matrix["python"])) == set(
        product(("ubuntu-latest", "windows-latest", "macos-latest"), ("3.11", "3.14"))
    )


def test_matrix_is_wired_to_runner_and_python(ci: Mapping[Any, Any]) -> None:
    spec = job(ci, "test")
    assert spec["runs-on"] == "${{ matrix.os }}"
    assert spec["strategy"]["fail-fast"] is False
    setup = uses_steps(ci, "test", "astral-sh/setup-uv")[0]
    assert setup["with"]["python-version"] == "${{ matrix.python }}"
    assert ci["defaults"] == {"run": {"shell": "bash"}}


# ---------------------------------------------------------------------------
# Job test: comandos uv, lanzador de la rueda, gitleaks y cobertura (REQ-MVP-23)
# ---------------------------------------------------------------------------


def test_test_job_runs_the_uv_commands_in_order(ci: Mapping[Any, Any]) -> None:
    order = [
        first_index(ci, "test", "uv lock --check"),
        first_index(ci, "test", "uv sync --locked --group dev"),
        first_index(ci, "test", "uv build --wheel"),
        first_index(ci, "test", "uv tool install --from"),
        first_index(ci, "test", "aucove --version"),
        first_index(ci, "test", "uv run --no-sync pytest"),
        first_index(ci, "test", "uv run --no-sync coverage report"),
    ]
    assert order == sorted(order), order
    lines = [line for _, line in commands(ci, "test")]
    install = next(line for line in lines if line.startswith("uv tool install"))
    assert shlex.split(install)[-1] == "aucove" and "wheels[0]" in install
    assert 'wheels=(dist/*.whl)' in lines
    assert 'uv tool dir --bin >> "$GITHUB_PATH"' in lines
    # sin el proyecto en .venv: el lanzador del PATH es el de la rueda
    sync = next(line for line in lines if line.startswith("uv sync"))
    assert "--no-install-project" in shlex.split(sync)
    for line in lines:
        if line.startswith("uv run"):
            assert "--no-sync" in shlex.split(line), line


def test_test_job_coverage_options(ci: Mapping[Any, Any]) -> None:
    lines = [line for _, line in commands(ci, "test")]
    pytest_line = next(line for line in lines if line.startswith("uv run --no-sync pytest"))
    argv = shlex.split(pytest_line)
    marker = argv[argv.index("-m") + 1]
    assert marker.startswith("not live and not model")
    assert "not docker" in marker  # la compilación TeX no corre en los PR (D-38)
    assert "--cov=aucove.domain" in argv
    assert "--cov-branch" in argv
    assert "--cov-fail-under=90" in argv
    report = next(line for line in lines if line.startswith("uv run --no-sync coverage report"))
    assert shlex.split(report)[4:] == ["report", "--include=*/aucove/domain/tasks.py", "--fail-under=100"]


def test_gitleaks_version_and_sha256_are_those_of_template_vars(ci: Mapping[Any, Any]) -> None:
    env = job(ci, "test")["env"]
    values = template_vars()
    assert env["GITLEAKS_VERSION"] == values["gitleaks_version"]
    assert env["GITLEAKS_SHA256"] == values["gitleaks_sha256"]


def test_gitleaks_is_downloaded_verified_and_only_on_linux(ci: Mapping[Any, Any]) -> None:
    [step] = [s for s in steps(ci, "test") if "gitleaks" in str(s.get("run", ""))]
    assert step["if"] == "runner.os == 'Linux'"
    script = step["run"]
    assert "gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz" in script
    assert "releases/download/v${GITLEAKS_VERSION}/" in script
    assert 'echo "${GITLEAKS_SHA256}  $dir/gitleaks.tgz" | sha256sum -c -' in script
    assert "--proto '=https'" in script
    # bloquea mientras sigan los marcadores provisionales de gitleaks
    assert "^[0-9a-f]{64}$" in script and "exit 1" in script
    assert script.index("exit 1") < script.index("curl")
    pytest_step = first_index(ci, "test", "uv run --no-sync pytest")
    assert steps(ci, "test").index(step) < pytest_step


def test_gitleaks_guard_rejects_the_c28_placeholders() -> None:
    values = template_vars()
    version_ok = re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", values["gitleaks_version"]) is not None
    sha_ok = re.fullmatch(r"[0-9a-f]{64}", values["gitleaks_sha256"]) is not None
    if values["gitleaks_version"] == "X.Y.Z":
        assert not version_ok and not sha_ok  # el job test falla en ubuntu a propósito
    else:
        assert version_ok and sha_ok


# ---------------------------------------------------------------------------
# Job lint: import-linter y actionlint sobre el render (REQ-ARCH-25, D-37)
# ---------------------------------------------------------------------------


def test_lint_job_runs_import_linter(ci: Mapping[Any, Any]) -> None:
    lines = [line for _, line in commands(ci, "lint")]
    # el proyecto editable se construye con el hatchling del lock
    assert lines.index("uv sync --locked --group dev --no-install-project") < lines.index(
        "uv sync --locked --group dev --no-build-isolation-package aucove"
    )
    assert "uv run --no-sync lint-imports --no-cache" in lines


def test_actionlint_binary_is_verified_by_sha256(ci: Mapping[Any, Any]) -> None:
    env = job(ci, "lint")["env"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", env["ACTIONLINT_VERSION"])
    assert re.fullmatch(r"[0-9a-f]{64}", env["ACTIONLINT_SHA256"])
    [step] = [s for s in steps(ci, "lint") if "actionlint.tgz" in str(s.get("run", ""))]
    script = step["run"]
    assert "actionlint_${ACTIONLINT_VERSION}_linux_amd64.tar.gz" in script
    assert 'echo "${ACTIONLINT_SHA256}  $dir/actionlint.tgz" | sha256sum -c -' in script


def test_actionlint_runs_on_framework_workflows_and_on_the_render(ci: Mapping[Any, Any]) -> None:
    lines = commands(ci, "lint")
    render = [i for i, line in lines if "tools/render_template.py" in line]
    assert render, "el job lint renderiza la plantilla"
    render_lines = [line for _, line in lines if "tools/render_template.py" in line]
    assert any("--check" in shlex.split(line) for line in render_lines)
    loop = next(line for _, line in lines if line.startswith("for lang in"))
    assert loop.startswith("for lang in en es;")
    actionlint = [(i, line) for i, line in lines if "actionlint " in line and "curl" not in line]
    on_framework = [i for i, line in actionlint if line.startswith("actionlint ") and ".github/workflows/*.yml" in line]
    on_render = [i for i, line in actionlint if 'cd "$RUNNER_TEMP/render/$lang"' in line]
    assert on_framework and on_render
    assert min(on_render) > max(render)
    for _, line in actionlint:
        assert "template/" not in line, "actionlint corre sobre el render, no sobre las fuentes"
    # el render y actionlint usan el mismo directorio
    assert any('out="$RUNNER_TEMP/render/$lang"' == line for _, line in lines)
    # el paso con shellcheck sobre los scripts normativos de 09-ci no bloquea
    informative = [s for s in steps(ci, "lint") if s.get("continue-on-error") is True]
    assert len(informative) == 1 and "-shellcheck=" not in informative[0]["run"]


# ---------------------------------------------------------------------------
# Job reuse: REUSE sobre el repo y sobre el render en y es (REQ-TRACE-58)
# ---------------------------------------------------------------------------


def test_reuse_runs_on_the_repo_and_on_both_renders(ci: Mapping[Any, Any]) -> None:
    all_steps = steps(ci, "reuse")
    assert not uses_steps(ci, "reuse", "fsfe/reuse-action")
    reuse = [
        (i, shlex.split(line)) for i, s in enumerate(all_steps) for line in run_lines(s)
        if line.startswith("docker run ")
    ]
    args = [argv[argv.index("$REUSE_IMAGE") + 1:] for _, argv in reuse]
    assert args == [["lint"], ["--root", "_render/en", "lint"], ["--root", "_render/es", "lint"]]
    assert re.fullmatch(r"fsfe/reuse:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", job(ci, "reuse")["env"]["REUSE_IMAGE"])
    render_index = next(i for i, s in enumerate(all_steps) if "tools/render_template.py" in str(s.get("run", "")))
    script = all_steps[render_index]["run"]
    assert '--out "_render/$lang"' in script and "for lang in en es; do" in script
    assert reuse[0][0] < render_index < reuse[1][0]


def test_render_dir_of_the_reuse_job_is_not_git_ignored(tmp_path: Path) -> None:
    # con la estrategia git de reuse, un render ignorado no se revisaría
    env = isolated_git_env(tmp_path)
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", "--no-index", "_render/en/README.md"],
        env=env, capture_output=True, check=False,
    )
    assert proc.returncode == 1, proc.stderr


# ---------------------------------------------------------------------------
# Job template-ci (D-37, D-38)
# ---------------------------------------------------------------------------


def test_template_ci_runs_only_on_main_and_by_hand(ci: Mapping[Any, Any]) -> None:
    spec = job(ci, "template-ci")
    assert spec["if"] == "github.event_name == 'push' || github.event_name == 'workflow_dispatch'"
    assert spec["runs-on"] == "ubuntu-latest"
    lines = [line for _, line in commands(ci, "template-ci")]
    assert "docker pull \"$image\"" in lines
    pytest_line = next(line for line in lines if "pytest" in line)
    argv = shlex.split(pytest_line)
    assert "tests/e2e" in argv and "not docker" not in argv[argv.index("-m") + 1]
    assert spec["env"]["AUCOVE_REQUIRE_DOCKER"] == "1"


def test_template_ci_pulls_the_pinned_tex_image(ci: Mapping[Any, Any], pins: ModuleType) -> None:
    lines = [line for _, line in commands(ci, "template-ci")]
    extract = next(line for line in lines if line.startswith("image="))
    m = re.search(r"sed -n 's/(?P<pattern>[^/]+)//p' (?P<file>\S+)\)", extract)
    assert m is not None and m.group("file") == "template/base/project.yaml"
    # el sed del job saca exactamente la build.image que verifica verify_action_pins
    extracted = [
        re.sub(m.group("pattern"), "", line, count=1)
        for line in TEMPLATE_PROJECT.read_text(encoding="utf-8").splitlines()
        if re.match(m.group("pattern"), line)
    ]
    assert extracted == [tex_image(pins).raw]
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./_:-]*@sha256:[0-9a-f]{64}", extracted[0]), extracted
    guard = next(s["run"] for s in steps(ci, "template-ci") if "docker pull" in str(s.get("run", "")))
    assert "@sha256:[0-9a-f]{64}$" in guard
    assert guard.index("exit 1") < guard.index("docker pull")


# ---------------------------------------------------------------------------
# Job dco (REQ-TRACE-66, D-07 c)
# ---------------------------------------------------------------------------


def test_dco_job_checks_base_to_head_and_exempts_by_pr_author(ci: Mapping[Any, Any]) -> None:
    spec = job(ci, "dco")
    assert spec["if"] == "github.event_name == 'pull_request'"
    [checkout] = uses_steps(ci, "dco", "actions/checkout")
    assert checkout["with"]["fetch-depth"] == 0
    [step] = [s for s in steps(ci, "dco") if "check_dco.py" in str(s.get("run", ""))]
    assert step["env"] == {
        "PR_AUTHOR": "${{ github.event.pull_request.user.login }}",
        "PR_HEAD": "${{ github.event.pull_request.head.sha }}",
    }
    lines = run_lines(step)
    assert 'base="$(git rev-parse HEAD^1)"' in lines
    argv = shlex.split(next(line for line in lines if "check_dco.py" in line))
    assert argv[:2] == ["python3", "tools/check_dco.py"]
    assert argv[argv.index("--base") + 1] == "$base"
    assert argv[argv.index("--head") + 1] == "$PR_HEAD"
    assert argv[argv.index("--pr-author") + 1] == "$PR_AUTHOR"
    assert "${{" not in step["run"]
    assert (REPO_ROOT / "tools" / "check_dco.py").is_file()


# ---------------------------------------------------------------------------
# dependabot.yml (REQ-SEC-51)
# ---------------------------------------------------------------------------


def test_dependabot_covers_actions_and_uv_with_a_seven_day_cooldown() -> None:
    doc = load_doc(DEPENDABOT)
    assert doc["version"] == 2
    updates = {u["package-ecosystem"]: u for u in doc["updates"]}
    assert set(updates) == {"github-actions", "uv"}
    for ecosystem, update in updates.items():
        assert update["cooldown"]["default-days"] == 7, ecosystem
        assert update["schedule"]["interval"] in ("daily", "weekly"), ecosystem
    actions_dirs = updates["github-actions"]["directories"]
    assert actions_dirs[0] == "/"
    for directory in actions_dirs[1:]:
        assert (REPO_ROOT / directory.lstrip("/")).is_dir(), directory
    assert updates["uv"]["directory"] == "/"
    assert (REPO_ROOT / "uv.lock").is_file()


# ---------------------------------------------------------------------------
# Guía de mantenimiento (.github/MAINTAINING.md; 00-marco D-54)
# ---------------------------------------------------------------------------


MAINTAINING_MARKERS: tuple[str, ...] = (
    "REQ-SEC-47", "verify_action_pins.py", "known-checksums.json", "REUSE_IMAGE",
    "D-38", "D-39", "gh api",
    "REQ-TRACE-66", "web_commit_signoff_required", "sign off on web-based commits",
    "REQ-SEC-48", "sha_pinning_required", "full-length commit SHA",
    "REQ-SEC-51", "Dependabot", "vulnerability-alerts",
    "astral-sh/setup-uv", "fsfe/reuse-action", "allowed_actions",
    "REQ-SEC-77", "private-vulnerability-reporting",
    "REQ-SEC-79", "two_factor_requirement_enabled",
    "REQ-SEC-40", "prevent_self_review", "release.yml",
    "forge-tests", "TEST_GH_TOKEN_LEAD", "REQ-FORGE-64",
    "REQ-TRACE-70", "REQ-TRACE-67", "CHANGELOG.md", "template-publish",
)


def test_maintenance_guide_lists_every_point_with_its_evidence() -> None:
    text = MAINTAINING.read_text(encoding="utf-8")
    for marker in MAINTAINING_MARKERS:
        assert marker in text, marker
    sections = re.split(r"^## ", text, flags=re.M)[1:]
    assert len(sections) >= 12
    for section in sections:
        title = section.splitlines()[0]
        assert re.search(r"^- \[ \] ", section, flags=re.M), title
        assert re.search(r"^- Evidencia:", section, flags=re.M), title
    titles = [s.splitlines()[0] for s in sections]
    assert any("Antes de cada release" in t for t in titles)


@pytest.mark.parametrize("path", [CI, DEPENDABOT, MAINTAINING])
def test_files_are_utf8_lf_without_bom(path: Path) -> None:
    data = path.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in data
    assert data.endswith(b"\n") and not data.endswith(b"\n\n")
    data.decode("utf-8")


# ---------------------------------------------------------------------------
# tools/verify_action_pins.py con salidas simuladas
# ---------------------------------------------------------------------------


IMAGE_CHALLENGE = 'Bearer realm="https://auth.example.invalid/token",service="registry.example.invalid",scope="repository:x:pull"'
RAW = "https://raw.githubusercontent.com/"
NODE_ACTION_YML = "runs:\n  using: node24\n  main: dist/index.js\n"


class FakeLsRemote:
    """``git ls-remote`` simulado: etiqueta -> (objeto de la etiqueta, commit pelado o None)."""

    def __init__(self, tags: Mapping[tuple[str, str], tuple[str, str | None]]) -> None:
        self.tags = dict(tags)
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str]) -> str:
        self.calls.append(tuple(args))
        assert args[0] == "--tags" and args[1].startswith("https://github.com/")
        repo = args[1][len("https://github.com/"):]
        tag = args[2][len("refs/tags/"):]
        assert args[3] == args[2] + "^{}"
        entry = self.tags.get((repo, tag))
        if entry is None:
            return ""
        obj, peeled = entry
        out = f"{obj}\trefs/tags/{tag}\n"
        if peeled is not None:
            out += f"{peeled}\trefs/tags/{tag}^{{}}\n"
        return out


class FakeRegistry:
    """Registro v2 simulado con token anónimo; ``digests`` = digests que existen.

    También sirve el ``action.yml`` de cada acción en GitHub raw: el de una acción de
    JavaScript, salvo los de ``actions`` (ruta tras ``raw.githubusercontent.com/`` -> texto).
    """

    def __init__(self, digests: set[str], tags: Mapping[str, str] | None = None, *, auth: bool = True,
                 actions: Mapping[str, str] | None = None) -> None:
        self.digests = digests
        self.tags = dict(tags or {})
        self.auth = auth
        self.actions = dict(actions or {})
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, url: str, headers: Mapping[str, str]) -> Any:
        pins = load_tool("verify_action_pins")
        self.calls.append((method, url))
        if url.startswith(RAW):
            return raw_response(pins, method, url, self.actions)
        if url.startswith("https://auth.example.invalid/token"):
            assert method == "GET"
            return pins.HttpResponse(200, {}, json.dumps({"token": "t0k"}).encode())
        assert method == "HEAD" and "/v2/" in url and "/manifests/" in url
        assert "application/vnd.oci.image.index.v1+json" in headers["Accept"]
        if self.auth and headers.get("Authorization") != "Bearer t0k":
            return pins.HttpResponse(401, {"www-authenticate": IMAGE_CHALLENGE})
        reference = url.rsplit("/manifests/", 1)[1]
        digest = reference if reference.startswith("sha256:") else self.tags.get(reference)
        if digest is None or digest not in self.digests:
            return pins.HttpResponse(404, {})
        return pins.HttpResponse(200, {"docker-content-digest": digest})


def raw_response(pins_mod: ModuleType, method: str, url: str, actions: Mapping[str, str]) -> Any:
    """GitHub raw simulado: ``actions`` o, si no está, el action.yml de una acción de JavaScript."""
    assert method == "GET"
    path = url[len(RAW):]
    text = actions.get(path, NODE_ACTION_YML if path.endswith("/action.yml") else None)
    if text is None:
        return pins_mod.HttpResponse(404, {})
    return pins_mod.HttpResponse(200, {}, text.encode("utf-8"))


def real_tags(pins_mod: ModuleType) -> dict[tuple[str, str], tuple[str, str | None]]:
    """Etiquetas simuladas que coinciden con lo fijado; la de setup-uv, anotada."""
    pins_found, _ = pins_mod.collect(REPO_ROOT)
    tags: dict[tuple[str, str], tuple[str, str | None]] = {}
    for pin in pins_found:
        if pin.repo == "astral-sh/setup-uv":
            tags[(pin.repo, pin.tag)] = ("9" * 40, pin.ref)  # objeto de etiqueta anotada + commit
        else:
            tags[(pin.repo, pin.tag)] = (pin.ref, None)
    return tags


def all_images(pins_mod: ModuleType) -> list[Any]:
    """Imágenes fijadas del repo: la TeX de la plantilla y la de REUSE de ci.yml."""
    _, images = pins_mod.collect(REPO_ROOT)
    return images


def registry_with_all_images(pins_mod: ModuleType, **kwargs: Any) -> FakeRegistry:
    images = all_images(pins_mod)
    return FakeRegistry({i.digest for i in images}, {i.tag: i.digest for i in images if i.tag}, **kwargs)


def tex_image(pins_mod: ModuleType) -> Any:
    _, images = pins_mod.collect(REPO_ROOT)
    [image] = [i for i in images if i.where.file == "template/base/project.yaml"]
    return image


def test_collect_finds_every_pin_of_framework_and_template(pins: ModuleType) -> None:
    found, images = pins.collect(REPO_ROOT)
    files = {p.where.file for p in found}
    assert ".github/workflows/ci.yml" in files
    for wf in ("build.yml", "compile.yml", "validate.yml"):
        assert f"template/base/.github/workflows/{wf}" in files
    assert all(pins.pin_form_error(p) is None for p in found)
    count = sum(1 for p in sorted(WORKFLOWS.glob("*.yml")) + sorted(TEMPLATE_WORKFLOWS.glob("*.yml"))
                for line in p.read_text(encoding="utf-8").splitlines()
                if (m := _USES_LINE.match(line)) and not m.group("target").startswith("./"))
    assert len(found) == count
    image = tex_image(pins)
    assert image.registry == "registry.gitlab.com"
    assert image.repository == "islandoftex/images/texlive"
    assert image.digest is not None and re.fullmatch(r"sha256:[0-9a-f]{64}", image.digest)
    assert pins.image_form_error(image) is None


def test_list_mode_needs_no_network(pins: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    def no_net(*_: object) -> str:
        raise AssertionError("--list no usa la red")

    assert pins.main(["--list", "--root", str(REPO_ROOT)], ls_remote=no_net, http=no_net) == 0
    out = capsys.readouterr().out
    assert "astral-sh/setup-uv@" in out and "registry.gitlab.com/" in out and "fsfe/reuse:" in out


def test_all_pins_verify_with_simulated_outputs(pins: ModuleType) -> None:
    ls = FakeLsRemote(real_tags(pins))
    registry = registry_with_all_images(pins)
    code, text = pins.run(REPO_ROOT, ls_remote=ls, http=registry)
    assert code == 0, text
    assert "ERROR  " not in text and "aviso  " not in text
    assert "astral-sh/setup-uv@" in text and "existe" in text
    assert "registry-1.docker.io/fsfe/reuse@sha256:" in text and "registry.gitlab.com/" in text
    # una consulta por (repo, etiqueta, sha), con la etiqueta y su forma pelada
    assert len(ls.calls) == len(set(ls.calls))
    assert any(c[0] == "GET" and "/token" in c[1] for c in registry.calls)  # token anónimo
    # el action.yml de cada acción, una vez por (acción, sha) y en el commit fijado
    pins_found, _ = pins.collect(REPO_ROOT)
    raw = [u for _, u in registry.calls if u.startswith(RAW)]
    assert len(raw) == len(set(raw)) == len({(p.action, p.ref) for p in pins_found})
    assert all(f"/{p.ref}/" in " ".join(raw) for p in pins_found)


def test_a_sha_that_is_not_the_tag_fails(pins: ModuleType) -> None:
    image = tex_image(pins)
    tags = real_tags(pins)
    key = next(k for k in tags if k[0] == "actions/checkout")
    tags[key] = ("0123456789abcdef0123456789abcdef01234567", None)  # la etiqueta apunta a otro commit
    code, text = pins.run(REPO_ROOT, ls_remote=FakeLsRemote(tags), http=registry_with_all_images(pins))
    assert code == 1
    assert re.search(r"ERROR  actions/checkout@[0-9a-f]{12}: v[\d.]+ apunta a 0123456789ab", text), text


def test_a_nonexistent_tag_fails(pins: ModuleType) -> None:
    image = tex_image(pins)
    tags = real_tags(pins)
    del tags[next(k for k in tags if k[0] == "astral-sh/setup-uv")]
    code, text = pins.run(REPO_ROOT, ls_remote=FakeLsRemote(tags), http=registry_with_all_images(pins))
    assert code == 1 and "astral-sh/setup-uv: la etiqueta" in text and "no existe" in text


def test_an_annotated_tag_is_peeled(pins: ModuleType) -> None:
    commit = "676e2d560c9a403aa252096d99fcab3e1132b0f5"
    ls = FakeLsRemote({("fsfe/reuse-action", "v6.0.0"): ("9a55738c6490d015afe69ad187520395d2b7a1fa", commit)})
    assert pins.resolve_tag(ls, "fsfe/reuse-action", "v6.0.0") == commit
    # pinear el objeto de la etiqueta y no el commit no vale
    out = pins.Outcome()
    pin = pins.Pin("fsfe/reuse-action", "9a55738c6490d015afe69ad187520395d2b7a1fa", "v6.0.0",
                   pins.Where("x.yml", 1))
    pins.verify_pins([pin], ls, out)
    assert out.errors and not out.ok


def test_network_errors_fail_closed(pins: ModuleType) -> None:
    def broken(_: Sequence[str]) -> str:
        raise pins.NetError("sin red")

    code, text = pins.run(REPO_ROOT, ls_remote=broken, http=registry_with_all_images(pins))
    assert code == 1 and "no se pudo comprobar (sin red)" in text


def test_a_missing_image_digest_fails(pins: ModuleType) -> None:
    code, text = pins.run(REPO_ROOT, ls_remote=FakeLsRemote(real_tags(pins)), http=FakeRegistry(set()))
    assert code == 1 and "el digest no existe en el registro" in text


def test_a_moved_image_tag_is_only_a_note(pins: ModuleType) -> None:
    image = tex_image(pins)
    other = "sha256:" + "a" * 64
    registry = FakeRegistry({i.digest for i in all_images(pins)} | {other}, {image.tag: other})
    code, text = pins.run(REPO_ROOT, ls_remote=FakeLsRemote(real_tags(pins)), http=registry)
    assert code == 0, text
    assert f"la etiqueta {image.tag} apunta hoy a sha256:aaaaaaaaaaaa" in text


def test_a_registry_digest_mismatch_fails(pins: ModuleType) -> None:
    image = tex_image(pins)

    def lying(method: str, url: str, headers: Mapping[str, str]) -> Any:
        if url.startswith(RAW):
            return raw_response(pins, method, url, {})
        return pins.HttpResponse(200, {"docker-content-digest": "sha256:" + "b" * 64})

    code, text = pins.run(REPO_ROOT, ls_remote=FakeLsRemote(real_tags(pins)), http=lying)
    assert code == 1 and "otro digest" in text and image.digest not in text.split("otro digest")[1]


def test_token_realm_must_be_https(pins: ModuleType) -> None:
    def http(method: str, url: str, headers: Mapping[str, str]) -> Any:
        return pins.HttpResponse(401, {"www-authenticate": 'Bearer realm="http://auth.example.invalid/t"'})

    image = pins.parse_image("registry.example.invalid/a/b@sha256:" + "c" * 64, pins.Where("p.yaml", 1))
    out = pins.Outcome()
    pins.verify_images([image], http, out)
    assert out.errors and "https" in out.errors[0]


def test_form_errors_in_a_tree(tmp_path: Path, pins: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "a.yml").write_text(
        "jobs:\n  x:\n    steps:\n"
        "      - uses: actions/checkout@v7\n"
        "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1\n"
        "      - uses: ./.github/workflows/b.yml\n"
        "      - uses: docker://alpine:3\n",
        encoding="utf-8",
    )
    assert pins.main(["--list", "--root", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "actions/checkout@v7 no está fijada por un SHA" in err
    assert "no lleva la versión en un comentario" in err
    assert "docker://alpine:3 no está fijada por @sha256:" in err
    assert "b.yml" not in err  # el reutilizable local no se fija


@pytest.mark.parametrize(
    ("raw", "registry", "repository", "tag", "digest"),
    [
        ("alpine:3", "registry-1.docker.io", "library/alpine", "3", None),
        ("docker://ghcr.io/o/r:v1@sha256:" + "d" * 64, "ghcr.io", "o/r", "v1", "sha256:" + "d" * 64),
        ("localhost:5000/x/y@sha256:" + "e" * 64, "localhost:5000", "x/y", None, "sha256:" + "e" * 64),
        ("someone/img", "registry-1.docker.io", "someone/img", None, None),
    ],
)
def test_parse_image(pins: ModuleType, raw: str, registry: str, repository: str, tag: str | None,
                     digest: str | None) -> None:
    image = pins.parse_image(raw, pins.Where("f", 1))
    assert (image.registry, image.repository, image.tag, image.digest) == (registry, repository, tag, digest)


@pytest.mark.live
def test_pins_verify_against_github_and_the_registry(pins: ModuleType) -> None:
    """Manual, con red: ``pytest -m live tests/unit/test_framework_workflows.py``."""
    code, text = pins.run(REPO_ROOT)
    assert code == 0, text
