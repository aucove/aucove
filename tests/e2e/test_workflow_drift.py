# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Deriva entre los workflows renderizados, la autoprueba y la spec (00-marco D-37 y D-39).

Sobre el render de la plantilla en ``en`` y en ``es`` (no sobre las fuentes con
``{{var:…}}``):

- Las líneas ``aucove …`` de los workflows son exactamente ``WORKFLOW_COMMANDS``, las
  que ejecuta ``test_template_ci``. Con ``AUCOVE_SPEC_DIR``, también coinciden con los
  bloques de 09-ci §3.2, §3.3, §4.3, §4.5, §6, §7.1 y §7.4.
- Desde H3, el render trae también el vencimiento y la base rota (09-ci §6 y §7.1) y, con
  ``latex``, los dos workflows del editor del artículo (§4.5): el cableado, las acciones y
  actionlint los cubren igual. Un subcomando que todavía no existe (``PENDING_COMMANDS``;
  desde 5d, ninguno) tiene que seguir sin existir; cuando llegue, sale de esa lista.
- Cableado estructural de D-37, en los cuatro workflows de H2 (también ``seal.yml``): cada
  ``needs.*.outputs.*``, ``steps.*.outputs.*`` y ``jobs.*.outputs.*`` citado existe; cada
  artefacto que se baja lo sube, con el mismo nombre, un job del que depende, y cada job de
  ``seal.yml`` sube la carpeta ``--out`` de su línea; los jobs de los checks requeridos usan
  ``if: always()``.
- Las acciones son exactamente las 4 de D-39, fijadas por SHA con la versión en comentario.
- ``actionlint`` sobre el render, si está instalado (CI).
"""

from __future__ import annotations

import fnmatch
import importlib
import os
import re
import shutil
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
import yaml

from aucove import _meta
from aucove.cli import ci as cli_ci
from aucove.i18n import LANGS
from aucove.ops.registry import CI_MODULES, find_cli
from tests.conftest import load_tool
from tests.e2e.conftest import (
    CI_COMMAND_OUTPUTS,
    PENDING_COMMANDS,
    WORKFLOW_COMMANDS,
    WorkflowCommand,
    extract_commands,
)

WORKFLOWS = ".github/workflows"
# 12-mvp §1.2; seal.yml es obligatorio desde H2 (D-36 y D-50 c). En orden alfabético.
H2_WORKFLOWS: tuple[str, ...] = ("build.yml", "compile.yml", "seal.yml", "validate.yml")
# De H3 (12-mvp §1.2): el vencimiento y la base rota y, solo con latex, los del editor del
# artículo (09-ci §4.5; D-56).
H3_WORKFLOWS: tuple[str, ...] = ("expire-claims.yml", "main-broken.yml")
EDITOR_WORKFLOWS: tuple[str, ...] = ("editor-review-trigger.yml", "editor-review.yml")
TEMPLATE_WORKFLOWS: tuple[str, ...] = tuple(sorted(H2_WORKFLOWS + H3_WORKFLOWS + EDITOR_WORKFLOWS))
# Acciones que usan los workflows de la plantilla (00-marco D-39; 06-permisos §4.3).
D39_ACTIONS: frozenset[str] = frozenset(
    {"actions/checkout", "actions/upload-artifact", "actions/download-artifact", "astral-sh/setup-uv"}
)
REQUIRED_CHECKS: frozenset[str] = frozenset({f"{_meta.NAME}/build", f"{_meta.NAME}/validate"})
# Salidas documentadas de las acciones que se citan.
ACTION_OUTPUTS: Mapping[str, frozenset[str]] = {
    "actions/checkout": frozenset({"ref", "commit"}),
    "actions/upload-artifact": frozenset({"artifact-id", "artifact-url", "artifact-digest"}),
    "actions/download-artifact": frozenset({"download-path"}),
    "astral-sh/setup-uv": frozenset({"uv-version", "uv-path", "uvx-path", "cache-hit", "python-version"}),
}

IDENT = r"[A-Za-z_][A-Za-z0-9_-]*"
EXPR_RE = re.compile(r"\$\{\{(.*?)\}\}", re.S)
REF_RE = re.compile(rf"(?<![\w.-])(needs|steps|jobs|inputs)\.({IDENT})(?:\.({IDENT}))?(?:\.({IDENT}))?")
USES_LINE_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<rest>\S+)(?P<comment>.*)$")
PINNED_RE = re.compile(r"(?P<action>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@(?P<sha>[0-9a-f]{40})")
VERSION_COMMENT_RE = re.compile(r"^\s+#\s*v\d+\.\d+\.\d+\s*$")
ECHO_OUTPUT_RE = re.compile(rf"""\becho\s+["']?({IDENT})=.*>>\s*"?\$GITHUB_OUTPUT"?""")
CI_OUTPUT_RE = re.compile(rf"""\b{re.escape(_meta.NAME)}\s+ci\s+([a-z-]+)\b.*>>\s*"?\$GITHUB_OUTPUT"?""")


# -- render ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Render:
    lang: str
    root: Path
    texts: Mapping[str, str]  # archivo -> texto
    docs: Mapping[str, dict[Any, Any]]  # archivo -> YAML cargado


@pytest.fixture(scope="module", params=LANGS)
def render(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Render:
    lang = str(request.param)
    out = tmp_path_factory.mktemp(f"render-{lang}") / "repo"
    load_tool("render_template").render_to(out, lang)
    texts = {p.name: p.read_bytes().decode("utf-8") for p in sorted((out / WORKFLOWS).iterdir()) if p.is_file()}
    docs = {name: yaml.safe_load(text) for name, text in texts.items()}
    return Render(lang, out, texts, docs)


def triggers(doc: Mapping[Any, Any]) -> dict[str, Any]:
    """Bloque ``on``; PyYAML (YAML 1.1) lee la clave ``on`` como ``True``."""
    value = doc.get("on", doc.get(True))
    if isinstance(value, str):
        return {value: None}
    if isinstance(value, list):
        return {str(v): None for v in value}
    assert isinstance(value, dict), value
    return value


def jobs(r: Render, name: str) -> dict[str, dict[str, Any]]:
    found = r.docs[name].get("jobs")
    assert isinstance(found, dict) and found, name
    return found


def steps_of(job: Mapping[str, Any]) -> list[dict[str, Any]]:
    return list(job.get("steps") or [])


def needs_of(job: Mapping[str, Any]) -> tuple[str, ...]:
    needs = job.get("needs")
    if needs is None:
        return ()
    return (needs,) if isinstance(needs, str) else tuple(str(n) for n in needs)


def callee(job: Mapping[str, Any]) -> str | None:
    uses = job.get("uses")
    if not isinstance(uses, str):
        return None
    prefix = f"./{WORKFLOWS}/"
    assert uses.startswith(prefix), f"workflow reutilizable que no es del mismo repo: {uses}"
    return uses[len(prefix):]


def action_of(step: Mapping[str, Any]) -> str | None:
    uses = step.get("uses")
    return uses.split("@", 1)[0] if isinstance(uses, str) else None


def walk(node: object, path: tuple[object, ...] = ()) -> Iterator[tuple[tuple[object, ...], object]]:
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, (*path, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, (*path, index))


@dataclass(frozen=True)
class Ref:
    context: str
    name: str
    prop: str | None
    sub: str | None
    where: tuple[object, ...]


def refs(node: object) -> list[Ref]:
    out: list[Ref] = []
    for path, value in walk(node):
        if not isinstance(value, str):
            continue
        exprs = EXPR_RE.findall(value)
        if not exprs and path and path[-1] == "if":
            exprs = [value]  # un 'if:' sin ${{ }} es una expresión completa
        for expr in exprs:
            for m in REF_RE.finditer(expr):
                out.append(Ref(m.group(1), m.group(2), m.group(3), m.group(4), path))
    return out


def step_outputs(step: Mapping[str, Any]) -> frozenset[str]:
    action = action_of(step)
    if action is not None:
        return ACTION_OUTPUTS.get(action, frozenset())
    outputs: set[str] = set()
    for line in str(step.get("run") or "").splitlines():
        if "GITHUB_OUTPUT" not in line:
            continue
        if m := ECHO_OUTPUT_RE.search(line):
            outputs.add(m.group(1))
        elif (m := CI_OUTPUT_RE.search(line)) and m.group(1) in CI_COMMAND_OUTPUTS:
            outputs.update(CI_COMMAND_OUTPUTS[m.group(1)])
        else:
            raise AssertionError(f"escritura en $GITHUB_OUTPUT que la prueba no reconoce: {line.strip()}")
    return frozenset(outputs)


def job_outputs(r: Render, name: str, job_id: str) -> frozenset[str]:
    job = jobs(r, name)[job_id]
    target = callee(job)
    if target is not None:
        return frozenset((triggers(r.docs[target]).get("workflow_call") or {}).get("outputs") or {})
    return frozenset(job.get("outputs") or {})


# -- líneas 'aucove …' -------------------------------------------------------------------


def test_render_has_the_template_workflows(render: Render) -> None:
    assert tuple(sorted(render.docs)) == TEMPLATE_WORKFLOWS


def test_workflows_are_not_localized_and_fully_rendered(render: Render, tmp_path: Path) -> None:
    other = "es" if render.lang == "en" else "en"
    out = tmp_path / other
    load_tool("render_template").render_to(out, other)
    for name, text in render.texts.items():
        assert (out / WORKFLOWS / name).read_bytes().decode("utf-8") == text, name
        assert "{{var:" not in text and "{{" not in text.replace("${{", ""), name


def test_commands_are_exactly_workflow_commands(render: Render) -> None:
    assert extract_commands(render.docs) == list(WORKFLOW_COMMANDS)
    for command in WORKFLOW_COMMANDS:
        assert command.line.split(" ", 1)[0] == _meta.NAME, command


def test_every_command_runs_with_a_known_subcommand() -> None:
    """Cada línea invoca una operación del registro o un subcomando de ``ci``."""
    ci_names = {
        c.name
        for m in CI_MODULES
        for c in importlib.import_module(f".{m}", _meta.NAME).COMMANDS
    }
    for command in WORKFLOW_COMMANDS:
        words = command.line.split()[1:]
        if words[0] == "ci":
            pending = f"ci {words[1]}" in PENDING_COMMANDS
            assert (words[1] in ci_names) is not pending, command
            assert words[1] in cli_ci.KNOWN_COMMANDS, command
        else:
            assert find_cli(words) is not None, command


def _spec_blocks(spec_dir: Path) -> dict[str, dict[Any, Any]]:
    """Bloques YAML de 09-ci con un workflow completo, por su ``name:``."""
    text = (spec_dir / "09-ci.md").read_text(encoding="utf-8")
    out: dict[str, dict[Any, Any]] = {}
    for block in re.findall(r"^```yaml\n(.*?)^```", text, re.S | re.M):
        try:
            doc = yaml.safe_load(block)
        except yaml.YAMLError:
            continue
        if isinstance(doc, dict) and "jobs" in doc and isinstance(doc.get("name"), str):
            out[f"{doc['name']}.yml"] = doc
    return out


def test_commands_match_the_spec() -> None:
    spec_dir = os.environ.get(f"{_meta.ENV_PREFIX}SPEC_DIR")
    if not spec_dir or not (Path(spec_dir) / "09-ci.md").is_file():
        pytest.skip(f"sin {_meta.ENV_PREFIX}SPEC_DIR: rigen WORKFLOW_COMMANDS y el render")
    blocks = _spec_blocks(Path(spec_dir))
    assert set(TEMPLATE_WORKFLOWS) <= set(blocks), sorted(blocks)
    assert extract_commands({n: blocks[n] for n in TEMPLATE_WORKFLOWS}) == list(WORKFLOW_COMMANDS)


# -- cableado estructural (D-37) ------------------------------------------------------------


@pytest.mark.parametrize("name", TEMPLATE_WORKFLOWS)
def test_needs_steps_and_jobs_outputs_exist(render: Render, name: str) -> None:
    all_ids = set(jobs(render, name))
    call = triggers(render.docs[name]).get("workflow_call")
    for job_id, job in jobs(render, name).items():
        needs = needs_of(job)
        assert set(needs) <= all_ids, f"{name}:{job_id}: needs {needs}"
        steps = steps_of(job)

        def check_step(ref: Ref, visible: Sequence[Mapping[str, Any]]) -> None:
            by_id = {str(s["id"]): s for s in visible if "id" in s}
            assert ref.name in by_id, f"{name}:{job_id}: steps.{ref.name} no existe ({ref.where})"
            if ref.prop == "outputs":
                assert ref.sub in step_outputs(by_id[ref.name]), f"{name}:{job_id}: steps.{ref.name}.outputs.{ref.sub}"
            else:
                assert ref.prop in ("outcome", "conclusion"), ref

        for ref in refs({k: v for k, v in job.items() if k != "steps"}):
            if ref.context == "steps":
                assert ref.where[0] == "outputs", f"{name}:{job_id}: steps fuera de outputs"
                check_step(ref, steps)
        for index, step in enumerate(steps):
            for ref in refs(step):
                if ref.context == "steps":
                    check_step(ref, steps[:index])
        for ref in refs(job):
            if ref.context == "needs":
                assert ref.name in needs, f"{name}:{job_id}: needs.{ref.name} sin declararlo"
                if ref.prop == "outputs":
                    assert ref.sub in job_outputs(render, name, ref.name), (
                        f"{name}:{job_id}: needs.{ref.name}.outputs.{ref.sub} no existe"
                    )
                else:
                    assert ref.prop == "result", ref
    inputs = set((call or {}).get("inputs") or {})
    for ref in refs(render.docs[name]):
        if ref.context == "inputs":
            assert call is not None and ref.name in inputs, f"{name}: inputs.{ref.name}"
        elif ref.context == "jobs":
            assert call is not None and ref.where[:3] == (True, "workflow_call", "outputs"), ref
            assert ref.prop == "outputs" and ref.sub in job_outputs(render, name, ref.name), ref


def test_reusable_workflow_calls_match_their_inputs(render: Render) -> None:
    for name in TEMPLATE_WORKFLOWS:
        for job_id, job in jobs(render, name).items():
            target = callee(job)
            if target is None:
                continue
            assert target in render.docs, f"{name}:{job_id}: {target} no está en el render"
            declared = triggers(render.docs[target])["workflow_call"].get("inputs") or {}
            given = job.get("with") or {}
            assert set(given) <= set(declared), f"{name}:{job_id}"
            assert {k for k, v in declared.items() if v.get("required")} <= set(given), f"{name}:{job_id}"


@dataclass(frozen=True)
class Upload:
    job: str
    name: str  # con los ${{ }} sin evaluar
    path: str


def uploads(r: Render, name: str, job_ids: Sequence[str] | None = None) -> list[Upload]:
    out: list[Upload] = []
    for job_id, job in jobs(r, name).items():
        if job_ids is not None and job_id not in job_ids:
            continue
        target = callee(job)
        if target is not None:
            out.extend(uploads(r, target))
            continue
        for step in steps_of(job):
            if action_of(step) != "actions/upload-artifact":
                continue
            with_ = step.get("with") or {}
            path = str(with_["path"])
            if "name" in with_:
                artifact = str(with_["name"])
            elif with_.get("archive") is False:
                artifact = PurePosixPath(path).name  # sin zip, el artefacto se llama como el archivo
            else:
                artifact = "artifact"
            out.append(Upload(job_id, artifact, path))
    return out


def upstream(r: Render, name: str, job_id: str) -> list[str]:
    seen: list[str] = []
    pending = list(needs_of(jobs(r, name)[job_id]))
    while pending:
        current = pending.pop()
        if current not in seen:
            seen.append(current)
            pending.extend(needs_of(jobs(r, name)[current]))
    return seen


@pytest.mark.parametrize("name", TEMPLATE_WORKFLOWS)
def test_artifacts_are_downloaded_with_the_uploaded_name(render: Render, name: str) -> None:
    ups = uploads(render, name)
    assert len({u.name for u in ups}) == len(ups), f"{name}: dos artefactos con el mismo nombre"
    for job_id, job in jobs(render, name).items():
        for step in steps_of(job):
            if action_of(step) != "actions/download-artifact":
                continue
            with_ = step.get("with") or {}
            candidates = uploads(render, name, upstream(render, name, job_id))
            if "name" in with_:
                matches = [u for u in candidates if u.name == with_["name"]]
            else:
                pattern = str(with_["pattern"])
                matches = [u for u in candidates if fnmatch.fnmatchcase(EXPR_RE.sub("abc1234", u.name), pattern)]
            assert len(matches) == 1, f"{name}:{job_id}: {with_} coincide con {matches}"


def test_artifact_names_used_by_the_commands(render: Render) -> None:
    """Los artefactos que leen o escriben los comandos: diagnostics.json, pin.json
    (REQ-CI-45) y la carpeta de evidencia de cada job de seal.yml (REQ-CI-54)."""
    report = next(u for u in uploads(render, "compile.yml") if u.name.startswith("build-report-"))
    assert PurePosixPath(report.path).name == "diagnostics.json"
    assert uploads(render, "validate.yml") == [Upload("pin", "validate-pin", "pin.json")]
    assert any("--report-dir \"$RUNNER_TEMP/reports\"" in c.line for c in WORKFLOW_COMMANDS)
    assert any("--pin-report \"$RUNNER_TEMP/pin/pin.json\"" in str(s.get("run", ""))
               for s in steps_of(jobs(render, "validate.yml")["validate"]))
    # seal.yml: cada job sube lo que su línea escribe en --out, con el nombre que cita el registro.
    seal = {u.job: u for u in uploads(render, "seal.yml")}
    assert set(seal) == {"submission", "integration"}
    for job, upload in seal.items():
        (command,) = [c for c in WORKFLOW_COMMANDS if (c.workflow, c.job) == ("seal.yml", job)]
        assert upload.path == "${{ runner.temp }}/evidence" and '--out "$RUNNER_TEMP/evidence"' in command.line
        assert upload.name.startswith(f"evidence-{job}-") and upload.name.endswith("-a${{ github.run_attempt }}")


def test_required_checks_run_always(render: Render) -> None:
    found: dict[str, Mapping[str, Any]] = {}
    for name in TEMPLATE_WORKFLOWS:
        for job_doc in jobs(render, name).values():
            if job_doc.get("name") in REQUIRED_CHECKS:
                found[str(job_doc["name"])] = job_doc
    assert set(found) == REQUIRED_CHECKS
    for check, job in found.items():
        assert job.get("if") == "always()", check
        assert needs_of(job), check


# -- acciones (D-39) ------------------------------------------------------------------------


def test_actions_are_exactly_the_four_of_d39_pinned_by_sha(render: Render) -> None:
    used: dict[str, set[str]] = {}
    for name in TEMPLATE_WORKFLOWS:
        for line in render.texts[name].splitlines():
            m = USES_LINE_RE.match(line)
            if not m:
                continue
            rest = m.group("rest")
            if rest.startswith("./"):
                assert rest.removeprefix(f"./{WORKFLOWS}/") in render.docs, rest  # solo a nivel de job
                continue
            pinned = PINNED_RE.fullmatch(rest)
            assert pinned, f"{name}: acción sin SHA de 40 hex: {rest}"
            assert VERSION_COMMENT_RE.match(m.group("comment")), f"{name}: sin versión en comentario: {line}"
            used.setdefault(pinned.group("action"), set()).add(pinned.group("sha"))
        for job in jobs(render, name).values():
            for step in steps_of(job):
                uses = step.get("uses")
                assert not (isinstance(uses, str) and uses.startswith(("./", "docker://"))), uses
    assert set(used) == D39_ACTIONS
    assert all(len(shas) == 1 for shas in used.values()), used  # un único SHA por acción


# -- actionlint (CI) ------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint no está instalado (solo en CI)")
def test_actionlint_on_the_render(render: Render, tmp_path: Path) -> None:
    config = tmp_path / "actionlint.yaml"
    # ubuntu-slim es una etiqueta de GitHub que versiones viejas de actionlint no conocen.
    config.write_text("self-hosted-runner:\n  labels: [ubuntu-slim]\n", encoding="utf-8")
    files = [str(render.root / WORKFLOWS / n) for n in TEMPLATE_WORKFLOWS]
    proc = subprocess.run(
        ["actionlint", "-no-color", "-shellcheck=", "-pyflakes=", "-config-file", str(config), *files],
        cwd=render.root, capture_output=True, timeout=300, check=False,
    )
    output = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 0, output


def test_canonical_list_is_well_formed() -> None:
    assert len(set(WORKFLOW_COMMANDS)) == len(WORKFLOW_COMMANDS)
    assert all(isinstance(c, WorkflowCommand) and c.workflow in TEMPLATE_WORKFLOWS for c in WORKFLOW_COMMANDS)
    for prefix in PENDING_COMMANDS:  # cada pendiente nombra exactamente una línea
        assert len([c for c in WORKFLOW_COMMANDS if c.line.startswith(f"{_meta.NAME} {prefix} ")]) == 1, prefix
