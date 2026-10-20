# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Pruebas estructurales de los workflows de la plantilla.

Autoridad: 09-ci §1-§2, §3.2-§3.3, §4.3 y §5.2; 10-seguridad REQ-SEC-36, REQ-SEC-47 y SB06;
00-marco D-37 y D-39; las variables del render, de tools/template_vars.toml.

Se leen las fuentes de ``template/base/.github/workflows`` con PyYAML: las variables
``{{var:...}}`` van dentro de cadenas, así que la fuente carga como YAML sin renderizar.
PyYAML sigue YAML 1.1 y lee la clave ``on`` como ``True``; ``triggers`` lo resuelve.
``tests/e2e/test_workflow_drift.py`` repite el cableado y las acciones sobre el render, con
actionlint.

``seal.yml`` (09-ci §7.4) tiene sus pruebas sobre el render en ``test_seal_workflow``; aquí se
comprueba su fuente con las de los demás (codificación, variables del render, URL del lock,
``sed`` del pin, cableado y artefactos) y que la convención lo exige (REQ-CI-56).
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml

from aucove import _meta
from aucove.config.project import core_pin_line

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPO_ROOT / "template" / "base" / ".github" / "workflows"
TEMPLATE_VARS_FILE = REPO_ROOT / "tools" / "template_vars.toml"

# Workflows de H2 (12-mvp §1.2); las pruebas de su forma son las de abajo.
WORKFLOW_FILES: tuple[str, ...] = ("build.yml", "compile.yml", "validate.yml")
# De H3: el vencimiento y la base rota (09-ci §6 y §7.1). Sus fuentes pasan las pruebas comunes y
# son el bloque de la spec línea por línea; su ejecución la prueban test_template_expire y
# test_template_ci.
H3_WORKFLOWS: tuple[str, ...] = ("expire-claims.yml", "main-broken.yml")
# seal.yml (09-ci §7.4; D-50 c) también es de H2, pero sus pruebas son las del sellado (ci seal).
SEAL_WORKFLOW = "seal.yml"
# Los del editor del artículo (09-ci §4.5; D-56) son de H3 y solo van con format: latex; sus pruebas
# están en test_editor_review_workflow. editor-review.yml instala el núcleo como los demás; el
# disparador no tiene nada que revisar aquí.
EDITOR_WORKFLOW, EDITOR_TRIGGER = "editor-review.yml", "editor-review-trigger.yml"
SOURCE_FILES: tuple[str, ...] = (*WORKFLOW_FILES, SEAL_WORKFLOW, EDITOR_WORKFLOW, *H3_WORKFLOWS)  # pruebas de la fuente que valen para todos

# Acciones y SHA de 09-ci §3.2-§4.3; tools/verify_action_pins.py los verifica contra GitHub (D-39).
EXPECTED_PINS: Mapping[str, tuple[str, str]] = {
    "actions/checkout": ("3d3c42e5aac5ba805825da76410c181273ba90b1", "v7.0.1"),
    "actions/upload-artifact": ("cf430e030ddbb5b0abf93d22962f4752f3646cd9", "v7.0.2"),
    "actions/download-artifact": ("9000827ccba6bdab643e8b6fd33ac0654aef8333", "v8.0.2"),
    "astral-sh/setup-uv": ("1c37ad07a6a961277cf70c0d37d6f313000f5884", "v10.3.0"),
}

CHECK_NAMES: frozenset[str] = frozenset(
    {"aucove/build", "aucove/build-compile", "aucove/validate", "aucove/validate-pin"}
)
REQUIRED_CHECKS: frozenset[str] = frozenset({"aucove/build", "aucove/validate"})

# Variables del render que puede citar cada workflow.
TEMPLATE_VARS: frozenset[str] = frozenset(
    {"framework_repo", "gitleaks_version", "gitleaks_sha256"}
)
EXPECTED_VARS_BY_FILE: Mapping[str, frozenset[str]] = {
    "build.yml": frozenset({"framework_repo"}),
    "compile.yml": frozenset({"framework_repo"}),
    "validate.yml": TEMPLATE_VARS,
    "seal.yml": frozenset({"framework_repo"}),
    "editor-review.yml": frozenset({"framework_repo"}),
    "expire-claims.yml": frozenset({"framework_repo"}),
    "main-broken.yml": frozenset({"framework_repo"}),
}

LOCK_URL = (
    "https://github.com/{{var:framework_repo}}/releases/download/v${PIN}/requirements-lock.txt"
)
GITLEAKS_URL = (
    "https://github.com/gitleaks/gitleaks/releases/download/v{{var:gitleaks_version}}"
    "/gitleaks_{{var:gitleaks_version}}_linux_x64.tar.gz"
)
GITLEAKS_CHECK = 'echo "{{var:gitleaks_sha256}}  gitleaks.tgz" | sha256sum -c -'
SED_PIN = (
    r"""sed -nE 's/^core_pin:[[:space:]]*"([0-9]+\.[0-9]+\.[0-9]+((a|b|rc)[0-9]+)?)"[[:space:]]*(#.*)?$/\1/p'"""
)
SAME_REPO_IF = "github.event.pull_request.head.repo.full_name == github.repository"
PR_HEAD_SHA = "${{ github.event.pull_request.head.sha }}"


@dataclass(frozen=True)
class ExpectedJob:
    name: str | None
    permissions: Mapping[str, str]
    runs_on: str | None
    timeout: int | None
    needs: tuple[str, ...] = ()
    uses: str | None = None


# Tabla de 09-ci §1 y §2 (permisos, runner, timeout y dependencias de cada job).
EXPECTED_JOBS: Mapping[tuple[str, str], ExpectedJob] = {
    ("build.yml", "compile"): ExpectedJob(
        name=None,
        permissions={"contents": "read"},
        runs_on=None,
        timeout=None,
        uses="./.github/workflows/compile.yml",
    ),
    ("build.yml", "report"): ExpectedJob(
        name="aucove/build",
        permissions={"contents": "read", "pull-requests": "write"},
        runs_on="ubuntu-slim",
        timeout=5,
        needs=("compile",),
    ),
    ("compile.yml", "compile"): ExpectedJob(
        name="aucove/build-compile",
        permissions={"contents": "read"},
        runs_on="ubuntu-24.04",
        timeout=15,
    ),
    ("validate.yml", "pin"): ExpectedJob(
        name="aucove/validate-pin",
        permissions={"contents": "read"},
        runs_on="ubuntu-slim",
        timeout=5,
    ),
    ("validate.yml", "validate"): ExpectedJob(
        name="aucove/validate",
        permissions={"contents": "read", "pull-requests": "write", "issues": "read"},
        runs_on="ubuntu-slim",
        timeout=10,
        needs=("pin",),
    ),
}

# Salidas de los subcomandos 'aucove ci' que escriben en $GITHUB_OUTPUT.
CI_COMMAND_OUTPUTS: Mapping[str, frozenset[str]] = {
    "build-plan": frozenset(
        {"run", "image", "engine_flag", "shell", "timeout", "main", "main_base", "sha7",
         "retention", "latexdiff", "language", "reason"}
    ),
    "pin-plan": frozenset({"run", "pin"}),
}
# Salidas documentadas de las acciones; basta con las que se citan.
ACTION_OUTPUTS: Mapping[str, frozenset[str]] = {
    "actions/checkout": frozenset({"ref", "commit"}),
    "actions/upload-artifact": frozenset({"artifact-id", "artifact-url", "artifact-digest"}),
    "actions/download-artifact": frozenset({"download-path"}),
}

WORKFLOW_FILTERS: frozenset[str] = frozenset(
    {"paths", "paths-ignore", "branches", "branches-ignore", "tags", "tags-ignore"}
)

NAME = r"[A-Za-z_][A-Za-z0-9_-]*"
EXPR_RE = re.compile(r"\$\{\{(.*?)\}\}", re.S)
REF_RE = re.compile(
    rf"(?<![\w.-])(needs|steps|jobs|inputs)\.({NAME})(?:\.({NAME}))?(?:\.({NAME}))?"
)
VAR_RE = re.compile(r"\{\{var:([a-z_][a-z0-9_]*)\}\}")
STRAY_BRACES_RE = re.compile(r"(?<!\$)\{\{(?!var:[a-z_][a-z0-9_]*\}\})")
USES_LINE_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<rest>.*)$")
PINNED_RE = re.compile(
    r"^(?P<action>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@(?P<sha>[0-9a-f]{40})"
    r"\s+#\s*(?P<version>v\d+\.\d+\.\d+)\s*$"
)
ECHO_OUTPUT_RE = re.compile(rf"""\becho\s+["']?({NAME})=.*>>\s*"?\$GITHUB_OUTPUT"?""")
CI_OUTPUT_RE = re.compile(r"""\baucove\s+ci\s+([a-z-]+)\b.*>>\s*"?\$GITHUB_OUTPUT"?""")
# Datos que controla el autor del PR (REQ-CI-7): solo pueden llegar por env o usarse en if.
AUTHOR_DATA_RE = re.compile(
    r"github\.event\.pull_request\.(?:title|body|head\.ref|head\.label|labels|user|head\.repo)"
    r"|github\.head_ref|github\.event\.(?:issue|comment|review|review_comment|head_commit|commits)"
    r"|github\.(?:triggering_)?actor\b"
)
# Ejecución de código del árbol de trabajo (REQ-CI-2 y REQ-CI-29).
EXEC_RE = re.compile(
    r"\bdocker\b|\blatexmk\b|\bmake\b|\bbash\b|\bperl\b|\bpython3?\b|\bnpm\b|\bnode\b|\bsource\b"
    r"|(?:^|[\s;&|(])\./|(?:^|[\s;&|])\.\s"
)

YamlPath = tuple[object, ...]


# ---------------------------------------------------------------------------
# Lectura
# ---------------------------------------------------------------------------


@cache
def workflow_text(name: str) -> str:
    return (WORKFLOWS_DIR / name).read_bytes().decode("utf-8")


@cache
def workflow(name: str) -> dict[Any, Any]:
    doc = yaml.safe_load(workflow_text(name))
    assert isinstance(doc, dict), f"{name}: el documento no es un mapeo"
    return doc


def triggers(doc: Mapping[Any, Any]) -> dict[str, Any]:
    """Devuelve el bloque de disparadores; en YAML 1.1 la clave 'on' se lee como True."""
    present = [key for key in ("on", True) if key in doc]
    assert len(present) == 1, f"se esperaba exactamente una clave 'on', hay {present!r}"
    value = doc[present[0]]
    if isinstance(value, str):
        return {value: None}
    if isinstance(value, list):
        return {str(event): None for event in value}
    assert isinstance(value, dict), f"'on' con tipo inesperado: {type(value).__name__}"
    return value


def jobs(name: str) -> dict[str, dict[str, Any]]:
    found = workflow(name).get("jobs")
    assert isinstance(found, dict) and found, f"{name}: sin jobs"
    return found


def steps_of(job: Mapping[str, Any]) -> list[dict[str, Any]]:
    steps = job.get("steps") or []
    assert isinstance(steps, list)
    return steps


def needs_of(job: Mapping[str, Any]) -> tuple[str, ...]:
    needs = job.get("needs")
    if needs is None:
        return ()
    if isinstance(needs, str):
        return (needs,)
    assert isinstance(needs, list)
    return tuple(str(n) for n in needs)


def callee(job: Mapping[str, Any]) -> str | None:
    """Nombre del workflow reutilizable local que llama el job, si lo hay."""
    uses = job.get("uses")
    if not isinstance(uses, str):
        return None
    prefix = "./.github/workflows/"
    assert uses.startswith(prefix), f"workflow reutilizable no local: {uses}"
    return uses[len(prefix):]


def action_of(step: Mapping[str, Any]) -> str | None:
    uses = step.get("uses")
    return uses.split("@", 1)[0] if isinstance(uses, str) else None


def all_jobs() -> Iterator[tuple[str, str, dict[str, Any]]]:
    for name in WORKFLOW_FILES:
        for job_id, job in jobs(name).items():
            yield name, job_id, job


def all_steps() -> Iterator[tuple[str, str, int, dict[str, Any]]]:
    for name, job_id, job in all_jobs():
        for index, step in enumerate(steps_of(job)):
            yield name, job_id, index, step


def steps_in(name: str) -> Iterator[tuple[str, str, int, dict[str, Any]]]:
    """Los pasos de un archivo (también de ``seal.yml``, que ``all_steps`` deja fuera)."""
    for job_id, job in jobs(name).items():
        for index, step in enumerate(steps_of(job)):
            yield name, job_id, index, step


def walk(node: object, path: YamlPath = ()) -> Iterator[tuple[YamlPath, object]]:
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, (*path, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, (*path, index))


def string_leaves(node: object, path: YamlPath = ()) -> Iterator[tuple[YamlPath, str]]:
    for leaf_path, value in walk(node, path):
        if isinstance(value, str):
            yield leaf_path, value


def expressions(path: YamlPath, value: str) -> list[str]:
    """Expresiones de una hoja; un 'if:' sin ${{ }} es una expresión completa."""
    found = EXPR_RE.findall(value)
    if not found and path and path[-1] == "if":
        found = [value]
    return found


@dataclass(frozen=True)
class Ref:
    context: str
    name: str
    prop: str | None
    sub: str | None
    where: YamlPath


def refs(node: object, path: YamlPath = ()) -> list[Ref]:
    out: list[Ref] = []
    for leaf_path, value in string_leaves(node, path):
        for expr in expressions(leaf_path, value):
            for m in REF_RE.finditer(expr):
                out.append(Ref(m.group(1), m.group(2), m.group(3), m.group(4), leaf_path))
    return out


def step_outputs(step: Mapping[str, Any]) -> tuple[frozenset[str], list[str]]:
    """(salidas que declara el paso, líneas que escriben en $GITHUB_OUTPUT sin reconocerse)."""
    action = action_of(step)
    if action is not None:
        return ACTION_OUTPUTS.get(action, frozenset()), []
    run = step.get("run")
    outputs: set[str] = set()
    unknown: list[str] = []
    if isinstance(run, str):
        for line in run.splitlines():
            if "GITHUB_OUTPUT" not in line:
                continue
            if m := ECHO_OUTPUT_RE.search(line):
                outputs.add(m.group(1))
            elif (m := CI_OUTPUT_RE.search(line)) and m.group(1) in CI_COMMAND_OUTPUTS:
                outputs.update(CI_COMMAND_OUTPUTS[m.group(1)])
            else:
                unknown.append(line.strip())
    return frozenset(outputs), unknown


def job_outputs(name: str, job_id: str) -> frozenset[str]:
    job = jobs(name)[job_id]
    target = callee(job)
    if target is not None:
        call = triggers(workflow(target)).get("workflow_call") or {}
        return frozenset(call.get("outputs") or {})
    return frozenset(job.get("outputs") or {})


def uses_lines(text: str) -> list[str]:
    return [
        m.group("rest").strip() for line in text.splitlines() if (m := USES_LINE_RE.match(line))
    ]


def install_step_index(steps: Sequence[Mapping[str, Any]]) -> int | None:
    for index, step in enumerate(steps):
        if "uv tool install" in str(step.get("run", "")):
            return index
    return None


def substitute(node: object, values: Mapping[str, str]) -> object:
    """Sustituye {{var:...}} en las cadenas de un árbol YAML ya cargado."""
    if isinstance(node, str):
        return VAR_RE.sub(lambda m: values[m.group(1)], node)
    if isinstance(node, dict):
        return {key: substitute(value, values) for key, value in node.items()}
    if isinstance(node, list):
        return [substitute(value, values) for value in node]
    return node


# ---------------------------------------------------------------------------
# Archivos y codificación
# ---------------------------------------------------------------------------


def test_workflow_files_present() -> None:
    present = {p.name for p in WORKFLOWS_DIR.iterdir() if p.is_file()}
    assert set(WORKFLOW_FILES) <= present
    assert present == set(WORKFLOW_FILES) | set(H3_WORKFLOWS) | {SEAL_WORKFLOW, EDITOR_WORKFLOW, EDITOR_TRIGGER}, (
        sorted(present))


@pytest.mark.parametrize("name", SOURCE_FILES)
def test_text_encoding(name: str) -> None:
    data = (WORKFLOWS_DIR / name).read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf"), "BOM"
    assert b"\r" not in data, "CR"
    assert b"\t" not in data, "tabulador"
    assert data.endswith(b"\n") and not data.endswith(b"\n\n"), "salto final"
    data.decode("utf-8")
    assert not any(line != line.rstrip() for line in data.decode("utf-8").splitlines())


# ---------------------------------------------------------------------------
# Disparadores (REQ-CI-10, REQ-CI-29, SB06)
# ---------------------------------------------------------------------------


def test_on_key_read_as_true_is_resolved() -> None:
    doc = yaml.safe_load("on:\n  pull_request:\n    types: [opened]\n")
    assert True in doc and "on" not in doc
    assert triggers(doc) == {"pull_request": {"types": ["opened"]}}
    assert triggers({"on": "push"}) == {"push": None}
    with pytest.raises(AssertionError):
        triggers({"name": "x"})


@pytest.mark.parametrize("name", WORKFLOW_FILES)
def test_on_key_is_yaml11_true(name: str) -> None:
    doc = workflow(name)
    assert True in doc and "on" not in doc
    assert triggers(doc)


def test_triggers() -> None:
    assert triggers(workflow("build.yml")) == {
        "pull_request": {"types": ["opened", "synchronize", "reopened", "ready_for_review"]}
    }
    assert triggers(workflow("validate.yml")) == {
        "pull_request_target": {
            "types": ["opened", "synchronize", "reopened", "edited", "ready_for_review",
                      "labeled", "unlabeled"]
        }
    }
    compile_on = triggers(workflow("compile.yml"))
    assert set(compile_on) == {"workflow_call"}
    call = compile_on["workflow_call"]
    assert call["inputs"] == {"mode": {"type": "string", "required": True}}
    assert list(call["outputs"]) == ["outcome", "skipped", "skip_reason", "pdf_url", "diff_url", "language"]


@pytest.mark.parametrize("name", WORKFLOW_FILES)
def test_no_workflow_level_filters(name: str) -> None:
    for event, config in triggers(workflow(name)).items():
        if isinstance(config, dict):
            assert not WORKFLOW_FILTERS & set(config), f"{name}: filtro en {event}"


def test_pull_request_target_only_in_validate_and_no_workflow_run() -> None:
    """SB06: ``workflow_run`` solo en ``editor-review.yml`` (desde H3, 09-ci REQ-CI-60)."""
    for path in sorted(WORKFLOWS_DIR.glob("*.y*ml")):
        events = set(triggers(yaml.safe_load(path.read_text("utf-8"))))
        if "workflow_run" in events:
            assert path.name == EDITOR_WORKFLOW, path.name
        if "pull_request_target" in events:  # 09-ci §2: validate.yml, el sellado de la entrega y el editor
            assert path.name in ("validate.yml", SEAL_WORKFLOW, EDITOR_WORKFLOW), path.name


def test_concurrency() -> None:
    assert workflow("build.yml")["concurrency"] == {
        "group": "build-${{ github.event.pull_request.number }}",
        "cancel-in-progress": True,
    }
    assert workflow("validate.yml")["concurrency"] == {
        "group": "validate-${{ github.event.pull_request.number }}",
        "cancel-in-progress": True,
    }
    assert "concurrency" not in workflow("compile.yml")  # la del llamador


# ---------------------------------------------------------------------------
# Jobs, permisos y timeouts (REQ-CI-2, REQ-CI-4, REQ-CI-11)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", WORKFLOW_FILES)
def test_top_level_permissions_empty(name: str) -> None:
    perms = workflow(name).get("permissions", "missing")
    assert perms == {} and isinstance(perms, dict)


def test_jobs_table() -> None:
    assert {(n, j) for n, j, _ in all_jobs()} == set(EXPECTED_JOBS)
    for (name, job_id), exp in EXPECTED_JOBS.items():
        job = jobs(name)[job_id]
        where = f"{name}:{job_id}"
        assert job.get("name") == exp.name, where
        assert job.get("permissions") == dict(exp.permissions), where
        assert job.get("runs-on") == exp.runs_on, where
        assert job.get("timeout-minutes") == exp.timeout, where
        assert needs_of(job) == exp.needs, where
        assert job.get("uses") == exp.uses, where


def test_every_job_has_minimal_permissions_and_timeout() -> None:
    for name, job_id, job in all_jobs():
        where = f"{name}:{job_id}"
        perms = job.get("permissions")
        assert isinstance(perms, dict) and perms, f"{where}: sin permisos por job"
        assert set(perms.values()) <= {"read", "write"}, where
        writes = {scope for scope, level in perms.items() if level == "write"}
        assert writes <= {"pull-requests"}, f"{where}: escritura {writes}"
        runs = [str(step.get("run", "")) for step in steps_of(job)]
        if writes:
            # Solo escribe el job que comenta, y ese job no ejecuta nada del árbol.
            assert any("aucove ci comment" in run for run in runs), where
            for run in runs:
                assert not EXEC_RE.search(run), f"{where}: ejecuta código con token de escritura"
        if any(EXEC_RE.search(run) for run in runs):
            assert set(perms.values()) == {"read"}, where
            assert "github.token" not in yaml.safe_dump(job), where
        if callee(job) is None:
            timeout = job.get("timeout-minutes")
            assert isinstance(timeout, int) and 1 <= timeout <= 15, f"{where}: timeout-minutes"
        else:
            # GitHub no admite timeout-minutes en un job que llama a un workflow reutilizable;
            # el tope lo pone cada job del workflow llamado.
            assert "timeout-minutes" not in job, where
            for sub_id, sub in jobs(str(callee(job))).items():
                assert isinstance(sub.get("timeout-minutes"), int), f"{callee(job)}:{sub_id}"
                sub_perms = sub.get("permissions") or {}
                for scope, level in sub_perms.items():
                    granted = perms.get(scope)
                    assert granted == level or (granted, level) == ("write", "read"), (
                        f"{where}: el llamado pide {scope}: {level}"
                    )


def test_required_jobs_run_always() -> None:
    required = [(n, j, job) for n, j, job in all_jobs() if job.get("name") in REQUIRED_CHECKS]
    assert {job["name"] for _, _, job in required} == REQUIRED_CHECKS
    for name, job_id, job in required:
        assert job.get("if") == "always()", f"{name}:{job_id}"
        assert needs_of(job), f"{name}:{job_id}"


def test_check_names() -> None:
    names = [job["name"] for _, _, job in all_jobs() if "name" in job]
    assert len(names) == len(set(names))
    assert set(names) == CHECK_NAMES


def test_pin_job_gets_no_token() -> None:
    pin = jobs("validate.yml")["pin"]
    dumped = yaml.safe_dump(pin)
    assert "github.token" not in dumped and "GITHUB_TOKEN" not in dumped


# ---------------------------------------------------------------------------
# Acciones fijadas por SHA (REQ-CI-5, REQ-SEC-47, D-39)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", WORKFLOW_FILES)
def test_uses_pinned_with_version_comment(name: str) -> None:
    lines = uses_lines(workflow_text(name))
    yaml_uses = [str(job["uses"]) for job in jobs(name).values() if "uses" in job]
    yaml_uses += [
        str(step["uses"])
        for step in (s for j in jobs(name).values() for s in steps_of(j))
        if "uses" in step
    ]
    assert len(lines) == len(yaml_uses), f"{name}: 'uses:' fuera de la forma de bloque"
    for rest in lines:
        if rest.startswith("./"):
            # Workflow reutilizable del mismo commit: no admite SHA (solo en jobs, no en pasos).
            assert rest.removeprefix("./.github/workflows/") in WORKFLOW_FILES, rest
            continue
        m = PINNED_RE.match(rest)
        assert m, f"{name}: uses sin SHA de 40 hex y versión en comentario: {rest}"
        assert EXPECTED_PINS[m.group("action")] == (m.group("sha"), m.group("version")), rest
    for step in (s for j in jobs(name).values() for s in steps_of(j)):
        uses = step.get("uses")
        assert not (isinstance(uses, str) and uses.startswith(("./", "docker://"))), uses


def test_action_set_is_exactly_d39() -> None:
    used = {action for _, _, _, step in all_steps() if (action := action_of(step)) is not None}
    assert used == set(EXPECTED_PINS)


# ---------------------------------------------------------------------------
# Checkout y pull_request_target (REQ-CI-6, REQ-CI-29)
# ---------------------------------------------------------------------------


def test_checkout_without_persisted_credentials() -> None:
    checkouts = [(n, j, s) for n, j, _, s in all_steps() if action_of(s) == "actions/checkout"]
    assert checkouts
    for name, job_id, step in checkouts:
        assert (step.get("with") or {}).get("persist-credentials") is False, f"{name}:{job_id}"
    for name in WORKFLOW_FILES:
        assert "allow-unsafe-pr-checkout" not in workflow_text(name)


def test_pull_request_target_checkout_goes_to_pr_path() -> None:
    for job_id, job in jobs("validate.yml").items():
        pr_checkouts = 0
        for step in steps_of(job):
            if action_of(step) != "actions/checkout":
                continue
            with_ = step.get("with") or {}
            path = str(with_.get("path", ""))
            assert path.strip("./"), f"{job_id}: checkout en la raíz del workspace"
            if "ref" in with_:
                pr_checkouts += 1
                assert with_["ref"] == PR_HEAD_SHA, job_id
                assert path == "pr", job_id
                assert step.get("if") == SAME_REPO_IF, job_id
            else:
                assert path == "base", job_id
        assert pr_checkouts == 1, job_id


def test_no_step_executes_the_pr_tree() -> None:
    allowed = re.compile(
        r'\[ -d "\$GITHUB_WORKSPACE/pr" \]|--tree "\$GITHUB_WORKSPACE/pr"|--tree pr\b'
    )
    for job_id, job in jobs("validate.yml").items():
        for step in steps_of(job):
            workdir = str(step.get("working-directory", ""))
            assert not re.search(r"(^|/)pr(/|$)", workdir), f"{job_id}: {workdir}"
            run = allowed.sub("", str(step.get("run", "")))
            assert "GITHUB_WORKSPACE/pr" not in run, job_id
            assert not re.search(r"(?:^|[\s;&|(])(?:\./)?pr/", run), job_id
            assert not re.search(r"\bcd\s", run), job_id
            assert not EXEC_RE.search(run), f"{job_id}: {step.get('name')}"


# ---------------------------------------------------------------------------
# Datos del autor del PR (REQ-CI-7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", SOURCE_FILES)
def test_run_never_interpolates_expressions(name: str) -> None:
    for _, job_id, index, step in steps_in(name):
        run = step.get("run")
        if run is not None:
            assert "${{" not in str(run), f"{name}:{job_id}: paso {index}"


@pytest.mark.parametrize("name", SOURCE_FILES)
def test_author_data_only_through_env_or_if(name: str) -> None:
    for path, value in string_leaves(workflow(name)):
        for expr in expressions(path, value):
            if AUTHOR_DATA_RE.search(expr):
                via_env = len(path) >= 2 and path[-2] == "env"
                assert via_env or path[-1] == "if", f"{name}: {path} usa {expr.strip()}"


# ---------------------------------------------------------------------------
# Cableado (D-37): needs, steps, jobs e inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", SOURCE_FILES)
def test_github_output_writes_are_recognized(name: str) -> None:
    for _, job_id, index, step in steps_in(name):
        _, unknown = step_outputs(step)
        assert not unknown, f"{name}:{job_id}: paso {index}: {unknown}"


@pytest.mark.parametrize("name", SOURCE_FILES)
def test_wiring_needs_and_steps(name: str) -> None:
    all_ids = set(jobs(name))
    for job_id, job in jobs(name).items():
        needs = needs_of(job)
        assert set(needs) <= all_ids, f"{name}:{job_id}: needs {needs}"
        steps = steps_of(job)
        ids = [step.get("id") for step in steps]
        declared = [i for i in ids if i is not None]
        assert len(declared) == len(set(declared)), f"{name}:{job_id}: ids repetidos"

        def check_step_ref(ref: Ref, visible: Sequence[Mapping[str, Any]]) -> None:
            by_id = {str(s["id"]): s for s in visible if "id" in s}
            assert ref.name in by_id, f"{name}:{job_id}: steps.{ref.name} no existe en {ref.where}"
            if ref.prop == "outputs":
                outputs, _ = step_outputs(by_id[ref.name])
                assert ref.sub in outputs, f"{name}:{job_id}: steps.{ref.name}.outputs.{ref.sub}"
            else:
                assert ref.prop in {"outcome", "conclusion"}, ref

        # Nivel de job: needs sí; steps solo en outputs (visibles todos los pasos).
        job_level = {k: v for k, v in job.items() if k != "steps"}
        for ref in refs(job_level):
            if ref.context == "steps":
                assert ref.where[0] == "outputs", f"{name}:{job_id}: steps fuera de outputs"
                check_step_ref(ref, steps)
        for index, step in enumerate(steps):
            for ref in refs(step):
                if ref.context == "steps":
                    check_step_ref(ref, steps[:index])
        for ref in refs(job):
            if ref.context == "needs":
                assert ref.name in needs, f"{name}:{job_id}: needs.{ref.name} sin declararlo"
                if ref.prop == "outputs":
                    assert ref.sub in job_outputs(name, ref.name), (
                        f"{name}:{job_id}: needs.{ref.name}.outputs.{ref.sub} no existe"
                    )
                else:
                    assert ref.prop == "result", ref
            elif ref.context == "jobs":
                raise AssertionError(f"{name}:{job_id}: 'jobs.' fuera de workflow_call.outputs")


@pytest.mark.parametrize("name", SOURCE_FILES)
def test_wiring_workflow_call(name: str) -> None:
    call = triggers(workflow(name)).get("workflow_call")
    inputs = set((call or {}).get("inputs") or {})
    for ref in refs(workflow(name)):
        if ref.context == "inputs":
            assert call is not None and ref.name in inputs, f"{name}: inputs.{ref.name}"
        if ref.context == "jobs":
            assert call is not None and ref.where[:3] == (True, "workflow_call", "outputs"), ref
            assert ref.prop == "outputs" and ref.sub in job_outputs(name, ref.name), ref


def test_reusable_call_inputs_match() -> None:
    for name, job_id, job in all_jobs():
        target = callee(job)
        if target is None:
            continue
        assert target in WORKFLOW_FILES
        declared = triggers(workflow(target))["workflow_call"].get("inputs") or {}
        given = job.get("with") or {}
        assert set(given) <= set(declared), f"{name}:{job_id}"
        required = {k for k, spec in declared.items() if spec.get("required")}
        assert required <= set(given), f"{name}:{job_id}"
        for key, value in given.items():
            if declared[key].get("type") == "string":
                assert isinstance(value, str), f"{name}:{job_id}: {key}"
        assert "secrets" not in job, f"{name}:{job_id}"
    assert jobs("build.yml")["compile"]["with"] == {"mode": "pr"}


# ---------------------------------------------------------------------------
# Artefactos (D-37, REQ-CI-19, REQ-CI-21, REQ-CI-45)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Upload:
    workflow: str
    job: str
    name: str      # plantilla, con los ${{ }} intactos
    path: str


def uploads_in(name: str, job_ids: Sequence[str] | None = None) -> list[Upload]:
    out: list[Upload] = []
    for job_id, job in jobs(name).items():
        if job_ids is not None and job_id not in job_ids:
            continue
        target = callee(job)
        if target is not None:
            out.extend(uploads_in(target))
            continue
        for step in steps_of(job):
            if action_of(step) != "actions/upload-artifact":
                continue
            with_ = step.get("with") or {}
            path = str(with_["path"])
            if "name" in with_:
                artifact = str(with_["name"])
            elif with_.get("archive") is False:
                # Sin zip, el artefacto se llama como el archivo.
                artifact = PurePosixPath(path).name
            else:
                artifact = "artifact"
            out.append(Upload(name, job_id, artifact, path))
    return out


def upstream(name: str, job_id: str) -> list[str]:
    seen: list[str] = []
    pending = list(needs_of(jobs(name)[job_id]))
    while pending:
        current = pending.pop()
        if current not in seen:
            seen.append(current)
            pending.extend(needs_of(jobs(name)[current]))
    return seen


def sample(template: str) -> str:
    return EXPR_RE.sub("abc1234", template)


@pytest.mark.parametrize("name", SOURCE_FILES)
def test_uploads(name: str) -> None:
    ups = uploads_in(name)
    assert len({u.name for u in ups}) == len(ups), f"{name}: artefactos con el mismo nombre"
    for step in (s for j in jobs(name).values() for s in steps_of(j)):
        if action_of(step) == "actions/upload-artifact":
            with_ = step.get("with") or {}
            assert "retention-days" in with_, f"{name}: upload sin retention-days"
            assert "name" in with_ or with_.get("archive") is False, f"{name}: upload sin nombre"


def test_upload_names_follow_spec() -> None:
    names = {u.name for u in uploads_in("compile.yml")}
    assert names == {
        "pdf-${{ steps.plan.outputs.sha7 }}.pdf",
        "diff-${{ steps.plan.outputs.sha7 }}.pdf",
        "build-report-${{ steps.plan.outputs.sha7 }}",
    }
    report = next(u for u in uploads_in("compile.yml") if u.name.startswith("build-report-"))
    assert PurePosixPath(report.path).name == "diagnostics.json"
    assert {u.name for u in uploads_in("validate.yml")} == {"validate-pin"}


@pytest.mark.parametrize("name", WORKFLOW_FILES)
def test_downloads_match_an_upload(name: str) -> None:
    for job_id, job in jobs(name).items():
        for step in steps_of(job):
            if action_of(step) != "actions/download-artifact":
                continue
            with_ = step.get("with") or {}
            candidates = uploads_in(name, upstream(name, job_id))
            if "name" in with_:
                matches = [u for u in candidates if u.name == with_["name"]]
            else:
                assert "pattern" in with_, f"{name}:{job_id}: download sin name ni pattern"
                pattern = str(with_["pattern"])
                matches = [u for u in candidates if fnmatch.fnmatchcase(sample(u.name), pattern)]
            assert len(matches) == 1, f"{name}:{job_id}: {with_} coincide con {matches}"


def test_download_paths_feed_the_commands() -> None:
    report = jobs("build.yml")["report"]
    download = next(s for s in steps_of(report) if action_of(s) == "actions/download-artifact")
    assert download["with"]["path"] == "${{ runner.temp }}/reports"
    assert download["with"]["merge-multiple"] is True
    report_runs = [str(s.get("run", "")) for s in steps_of(report)]
    assert any('--report-dir "$RUNNER_TEMP/reports"' in run for run in report_runs)
    validate = jobs("validate.yml")["validate"]
    download = next(s for s in steps_of(validate) if action_of(s) == "actions/download-artifact")
    assert download["with"] == {"name": "validate-pin", "path": "${{ runner.temp }}/pin"}
    pin_upload = uploads_in("validate.yml", ["pin"])[0]
    assert pin_upload.path == "pin.json"
    validate_runs = [str(s.get("run", "")) for s in steps_of(validate)]
    assert any('--pin-report "$RUNNER_TEMP/pin/pin.json"' in run for run in validate_runs)


# ---------------------------------------------------------------------------
# Variables del render e instalación del núcleo (REQ-CI-12, REQ-SEC-36/47)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", SOURCE_FILES)
def test_template_variables(name: str) -> None:
    text = workflow_text(name)
    assert set(VAR_RE.findall(text)) == EXPECTED_VARS_BY_FILE[name]
    assert not STRAY_BRACES_RE.search(text), f"{name}: llaves fuera de ${{{{ }}}} y {{{{var:}}}}"
    for placeholder in ("{owner}", "<X.Y.Z>", "<sha256"):
        assert placeholder not in text, f"{name}: queda {placeholder}"
    # Cada variable va dentro de un run (una cadena), nunca en una clave ni en otro campo.
    for path, value in string_leaves(workflow(name)):
        if VAR_RE.search(value):
            assert path[-1] == "run", f"{name}: variable fuera de run en {path}"


def test_all_variables_are_c28() -> None:
    found = set().union(*(set(VAR_RE.findall(workflow_text(n))) for n in SOURCE_FILES))
    assert found == TEMPLATE_VARS


@pytest.mark.parametrize("name", SOURCE_FILES)
def test_lock_urls(name: str) -> None:
    text = workflow_text(name)
    urls = re.findall(r'"(https://[^"]*requirements-lock\.txt)"', text)
    assert urls and set(urls) == {LOCK_URL}
    assert text.count("requirements-lock.txt") == len(urls)


def test_gitleaks_verified_by_sha256() -> None:
    validate = jobs("validate.yml")["validate"]
    runs = [str(s.get("run", "")) for s in steps_of(validate)]
    gitleaks = [run for run in runs if "gitleaks.tgz" in run]
    assert len(gitleaks) == 1
    assert GITLEAKS_URL in gitleaks[0] and GITLEAKS_CHECK in gitleaks[0]
    assert gitleaks[0].index(GITLEAKS_CHECK) < gitleaks[0].index("tar -xzf")
    step = next(s for s in steps_of(validate) if "gitleaks.tgz" in str(s.get("run", "")))
    assert step["working-directory"] == "${{ runner.temp }}"   # no ve .gitleaks.toml del PR


def test_core_install_steps() -> None:
    install_steps = [
        (n, j, s) for n, j, _, s in all_steps() if "uv tool install" in str(s.get("run", ""))
    ]
    assert len(install_steps) == 5
    for name, job_id, step in install_steps:
        where = f"{name}:{job_id}:{step.get('name')}"
        run = str(step["run"])
        assert step.get("working-directory") == "${{ runner.temp }}", where
        assert (step.get("env") or {}).get("UV_NO_CONFIG") == "1", where
        assert "set -euo pipefail" in run and "--constraints" in run, where
        if "core_pin:" in run:
            assert SED_PIN in run and 'test -n "$PIN"' in run, where
            assert run.index('test -n "$PIN"') < run.index("uv tool install"), where
    for name, job_id, _, step in all_steps():
        run = str(step.get("run", ""))
        for line in run.splitlines():
            if "curl" in line:
                assert "-fsSL --proto '=https'" in line, f"{name}:{job_id}: {line.strip()}"


@pytest.mark.skipif(shutil.which("sed") is None, reason="sed no está instalado (lo corre el CI)")
@pytest.mark.parametrize(
    "line",
    [
        'core_pin: "0.1.3"', 'core_pin: "0.1.0rc1"  # D-66', 'core_pin: "1.2.3b4"', 'core_pin: "2.0.0a10"',
        'core_pin: "0.1.0-rc1"', 'core_pin: "0.1.0.dev1"', 'core_pin: "0.1"', "core_pin: '0.1.3'",
    ],
)
def test_sed_pin_extracts_what_core_pin_line_reads(line: str, tmp_path: Path) -> None:
    """REQ-CI-12 y 00-marco D-66: el ``sed`` de los workflows lee la línea de REQ-ARCH-45 como el núcleo.

    El guion va en un archivo (``-f``): en Windows, la línea de órdenes de un ``sed`` de MSYS
    altera las barras invertidas del argumento.
    """
    command, flags, script = shlex.split(SED_PIN)
    assert (command, flags) == ("sed", "-nE")
    text = f"name: x\n{line}\n"
    (tmp_path / "project.yaml").write_bytes(text.encode("utf-8"))
    (tmp_path / "pin.sed").write_bytes(f"{script}\n".encode())
    out = subprocess.run(["sed", flags, "-f", "pin.sed", "project.yaml"], cwd=tmp_path, capture_output=True, check=True)
    assert out.stdout.decode("utf-8").strip() == (core_pin_line(text) or "")


@pytest.mark.parametrize(("name", "job_id"), sorted(EXPECTED_JOBS))
def test_core_installed_before_use(name: str, job_id: str) -> None:
    job = jobs(name)[job_id]
    if callee(job) is not None:
        return
    steps = steps_of(job)
    install = install_step_index(steps)
    setup = next((i for i, s in enumerate(steps) if action_of(s) == "astral-sh/setup-uv"), None)
    runs = [str(s.get("run", "")) for s in steps]
    uses_core = [
        i for i, run in enumerate(runs)
        if re.search(r"\baucove\s", run) and "uv tool install" not in run
    ]
    assert uses_core, f"{name}:{job_id}"
    assert setup is not None and install is not None and setup < install < min(uses_core)


@pytest.mark.parametrize(("job_id", "policy"), [("submission", "base/project.yaml"), ("integration", "project.yaml")])
def test_seal_installs_the_core_like_req_ci_12(job_id: str, policy: str) -> None:
    """09-ci §7.4: cada job de seal.yml instala el núcleo como REQ-CI-12, con el extra evidence.

    El pin sale del ``project.yaml`` de la política (la rama por defecto en ``submission``) con
    el mismo ``sed``, se comprueba antes de instalar y el lock se baja de la Release del pin.
    """
    steps = steps_of(jobs(SEAL_WORKFLOW)[job_id])
    (install,) = [s for s in steps if "uv tool install" in str(s.get("run", ""))]
    run = str(install["run"])
    assert install["working-directory"] == "${{ runner.temp }}"
    assert install["env"] == {"POLICY_YAML": "${{ github.workspace }}/" + policy, "UV_NO_CONFIG": "1"}
    assert run.startswith("set -euo pipefail\n")
    assert f'{SED_PIN} "$POLICY_YAML"' in run
    assert run.index('test -n "$PIN"') < run.index("curl") < run.index("uv tool install")
    assert f'"{LOCK_URL}"' in run and "curl -fsSL --proto '=https' -o lock.txt" in run
    assert 'uv tool install "aucove[evidence]==${PIN}" --constraints lock.txt' in run


@pytest.mark.parametrize("fmt", ["latex", "none"])
def test_seal_workflow_is_required_by_the_convention(fmt: str) -> None:
    """REQ-CI-56: sin seal.yml, ``conv.tree.missing`` (D-36), con LaTeX o sin él; no hay interruptor."""
    from tests.conftest import load_tool
    from tests.unit.test_rule_convention import of, run_on_render

    path = f".github/workflows/{SEAL_WORKFLOW}"
    render = {"es": load_tool("render_template").render_files("es", fmt=fmt)}
    assert path in render["es"] and of(run_on_render(render), "conv.tree.missing") == []
    missing = of(run_on_render(render, {path: None}), "conv.tree.missing")
    assert [(f.path, f.severity) for f in missing] == [(path, "error")]
    # project.yaml no tiene ninguna clave para apagar el sellado (03-esquemas §3).
    schema = json.loads((REPO_ROOT / "schemas" / "project.schema.json").read_text(encoding="utf-8"))
    # evidence_signing elige quién firma (00-marco D-72), no apaga el sellado.
    assert not [key for key in schema["properties"]
                if ("seal" in key or "evidence" in key) and key != "evidence_signing"]


@pytest.mark.parametrize("name", WORKFLOW_FILES)
def test_no_secrets_containers_or_services(name: str) -> None:
    text = workflow_text(name)
    assert "secrets." not in text and "secrets:" not in text
    for job_id, job in jobs(name).items():
        assert "container" not in job and "services" not in job, f"{name}:{job_id}"


# ---------------------------------------------------------------------------
# Compilación en sandbox (REQ-CI-13, REQ-CI-14, REQ-CI-18..23)
# ---------------------------------------------------------------------------


def test_compile_sandbox() -> None:
    steps = steps_of(jobs("compile.yml")["compile"])
    by_id = {str(s["id"]): s for s in steps if "id" in s}
    docker_runs = [str(s["run"]) for s in steps if "docker run" in str(s.get("run", ""))]
    assert len(docker_runs) == 2   # compilación y latexdiff, con las mismas opciones
    for run in docker_runs:
        flat = " ".join(run.replace("\\\n", " ").split())
        for option in ("--network none", "--read-only", "--cap-drop ALL",
                       "--security-opt no-new-privileges", "--pids-limit", "--memory",
                       '--user "$(id -u):$(id -g)"', "-e openout_any=p",
                       '-e shell_escape="$SE"', '-v "$GITHUB_WORKSPACE:/work"'):
            assert option in flat, option
        # REQ-CI-14 y D-43: sin openin_any (sin efecto desde TL2026; en imágenes anteriores, p
        # prohíbe el '..' de ../sections/).
        assert "openin_any" not in flat
        assert "latexmk -norc -cd" in flat
        assert "RUNNER_TEMP:" not in flat and "HOME:" not in flat
    tex = " ".join(str(by_id["tex"]["run"]).replace("\\\n", " ").split())
    latexmk = (
        'latexmk -norc -cd "$ENGINE_FLAG" "$SHELL_FLAG" -interaction=nonstopmode '
        '-halt-on-error -file-line-error -Werror -outdir=build "$MAIN"'
    )
    assert latexmk in tex   # REQ-CI-14
    assert by_id["tex"]["timeout-minutes"] == "${{ fromJSON(steps.plan.outputs.timeout) }}"
    assert by_id["tex"]["continue-on-error"] is True
    assert by_id["diff"]["continue-on-error"] is True
    assert by_id["diag"]["if"].startswith("always()")
    assert "--mode build-log" in str(by_id["diag"]["run"])
    assert "openin_any" not in workflow_text("compile.yml")


def test_compile_fails_if_tex_or_sections_is_a_link() -> None:
    """09-ci §3.2 y REQ-CI-48: antes de vaciar las salidas, ni tex/ ni sections/ pueden ser enlaces."""
    steps = steps_of(jobs("compile.yml")["compile"])
    empty = next(s for s in steps if str(s.get("name", "")).startswith("Salidas de TeX vacías"))
    lines = [line.strip() for line in str(empty["run"]).splitlines()]
    cleanup = lines.index("rm -rf tex/build tex/build-diff tex/diff.tex")
    assert "test ! -L tex" in lines[:cleanup] and "test ! -L sections" in lines[:cleanup]
    tex_step = next(i for i, s in enumerate(steps) if s.get("id") == "tex")
    assert steps.index(empty) < tex_step


def test_skip_reason_goes_from_build_plan_to_ci_comment() -> None:
    """REQ-CI-49: reason de build-plan -> skip_reason de compile.yml -> --skip-reason de build.yml."""
    compile_job = jobs("compile.yml")["compile"]
    assert compile_job["outputs"]["skip_reason"] == "${{ steps.plan.outputs.reason }}"
    call = triggers(workflow("compile.yml"))["workflow_call"]
    assert call["outputs"]["skip_reason"] == {"value": "${{ jobs.compile.outputs.skip_reason }}"}
    comment = next(s for s in steps_of(jobs("build.yml")["report"]) if "ci comment" in str(s.get("run", "")))
    assert comment["env"]["SKIP_REASON"] == "${{ needs.compile.outputs.skip_reason }}"
    assert '--skip-reason "$SKIP_REASON"' in " ".join(str(comment["run"]).split())
    # El veredicto no cambia: compile en verde con skipped=true deja aucove/build en éxito.
    verdict = next(s for s in steps_of(jobs("build.yml")["report"]) if s.get("name") == "Veredicto")
    assert '[ "$SKIPPED" = true ] && exit 0' in str(verdict["run"])


def test_pin_job_plan_and_report() -> None:
    steps = steps_of(jobs("validate.yml")["pin"])
    assert jobs("validate.yml")["pin"]["outputs"] == {"ran": "${{ steps.plan.outputs.run }}"}
    rules = next(s for s in steps if "--report pin.json" in str(s.get("run", "")))
    run = " ".join(str(rules["run"]).replace("\\\n", " ").split())
    for check in ("generated-sync", "roles-sync", "schemas"):
        assert f"--check {check}" in run
    assert "--mode tree" in run and "test -s pin.json" in run


# ---------------------------------------------------------------------------
# Render con las variables de tools/template_vars.toml
# ---------------------------------------------------------------------------


def render_values() -> dict[str, str]:
    """Valores de tools/template_vars.toml si existe; si no, valores de muestra."""
    if TEMPLATE_VARS_FILE.is_file():
        data = tomllib.loads(TEMPLATE_VARS_FILE.read_text("utf-8"))
        return {key: str(data[key]) for key in TEMPLATE_VARS if key in data}
    return {"framework_repo": "example-org/aucove", "gitleaks_version": "0.0.0",
            "gitleaks_sha256": "0" * 64}


@pytest.mark.parametrize("name", SOURCE_FILES)
def test_render_keeps_structure(name: str) -> None:
    values = render_values()
    if not TEMPLATE_VARS <= set(values):
        pytest.fail(f"tools/template_vars.toml no define {sorted(TEMPLATE_VARS - set(values))}")
    rendered = VAR_RE.sub(lambda m: values[m.group(1)], workflow_text(name))
    assert "{{var:" not in rendered
    assert not STRAY_BRACES_RE.search(rendered)
    assert yaml.safe_load(rendered) == substitute(workflow(name), values)


def test_template_vars_file_defines_workflow_variables() -> None:
    if not TEMPLATE_VARS_FILE.is_file():
        pytest.skip("tools/template_vars.toml todavía no existe")
    data = tomllib.loads(TEMPLATE_VARS_FILE.read_text("utf-8"))
    assert TEMPLATE_VARS <= set(data)
    assert all(isinstance(data[key], str) and data[key] for key in TEMPLATE_VARS)


# ---------------------------------------------------------------------------
# Texto de 09-ci, comentarios incluidos
# ---------------------------------------------------------------------------

# Encabezado de 09-ci tras el que va el bloque de cada workflow. seal.yml no entra: en §7.4 sus
# pasos de instalación son solo un nombre (lo compara test_seal_workflow, paso a paso).
SPEC_SECTIONS: Mapping[str, str] = {
    "compile.yml": "### 3.2", "build.yml": "### 3.3", "validate.yml": "### 4.3",
    EDITOR_WORKFLOW: "### 4.5", EDITOR_TRIGGER: "### 4.5", "expire-claims.yml": "## 6.", "main-broken.yml": "### 7.1",
}


# Marcadores de variables de la fuente y su forma en los bloques de 09-ci.
SPEC_MARKERS: Mapping[str, str] = {
    "{{var:framework_repo}}": "{owner}/" + _meta.NAME,
    "{{var:gitleaks_version}}": "<X.Y.Z>",
    "{{var:gitleaks_sha256}}": "<sha256 fijado por init>",
}


@pytest.mark.parametrize("name", (*WORKFLOW_FILES, EDITOR_WORKFLOW, EDITOR_TRIGGER, *H3_WORKFLOWS))
def test_source_is_the_spec_block_line_by_line(name: str) -> None:
    """La fuente es el bloque de 09-ci también en los comentarios, salvo los marcadores de variables.

    Así no se desvían textos que ninguna otra prueba lee, como la lista de salidas del paso
    ``plan`` de ``compile.yml``, que termina en ``language, reason``.
    """
    spec_dir = os.environ.get(f"{_meta.ENV_PREFIX}SPEC_DIR")
    if not spec_dir or not (Path(spec_dir) / "09-ci.md").is_file():
        pytest.skip(f"sin {_meta.ENV_PREFIX}SPEC_DIR")
    lines = (Path(spec_dir) / "09-ci.md").read_text(encoding="utf-8").split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith(SPEC_SECTIONS[name]))
    # El bloque de la sección cuyo name: es el del archivo (§4.5 trae los dos del editor).
    wanted = f"name: {name.removesuffix('.yml')}"
    begin = next(i for i in range(start, len(lines)) if lines[i].strip() == "```yaml"
                 and next(x for x in lines[i + 1:] if x.startswith("name:")) == wanted)
    end = next(i for i in range(begin + 1, len(lines)) if lines[i].strip() == "```")
    source = workflow_text(name)
    for marker, spelled in SPEC_MARKERS.items():
        source = source.replace(marker, spelled)
    assert source.rstrip("\n").split("\n") == lines[begin + 1 : end]
    if name == "compile.yml":
        plan = next(line for line in source.split("\n") if line.strip().startswith("id: plan"))
        assert plan.endswith("latexdiff, language, reason")
