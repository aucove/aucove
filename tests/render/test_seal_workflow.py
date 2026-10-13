# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``seal.yml`` sobre el render de la plantilla (09-ci §7.4, REQ-CI-50 y REQ-CI-29; 10-seguridad SB06; D-37).

- Disparadores, concurrencia y permisos exactos; ningún ``contents: write``, ``id-token``,
  secreto, Sigstore ni ``actions/attest*``, y nada que escriba en una rama (REQ-CI-50).
- El job ``submission`` solo corre para PR del mismo repositorio, lee el PR con un checkout
  en ``path: pr`` y no ejecuta nada de ese directorio (REQ-CI-29).
- Las líneas ``<nombre> ci seal`` son las de la spec y las acepta el parser de ``ci seal``;
  el núcleo se instala con el extra ``evidence`` antes de usarlo (REQ-CI-12).
- Con ``<PREFIJO>SPEC_DIR``, el render es el bloque de 09-ci §7.4 salvo los pasos de
  instalación, que en la spec son solo un nombre.
- La regla ``ci-policy`` no tiene hallazgos y, si está instalado, ``actionlint`` tampoco.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from aucove import _meta
from aucove.ops import ci_seal
from tests.conftest import load_tool

WORKFLOW = Path(".github") / "workflows" / "seal.yml"
NAME = _meta.NAME
PR_HEAD_SHA = "${{ github.event.pull_request.head.sha }}"
SAME_REPO = "github.event.pull_request.head.repo.full_name == github.repository"
PINS: Mapping[str, str] = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "astral-sh/setup-uv": "c18668ad3cf93ea998bef934396af7bb5c839dc7",
}
COMMANDS: Mapping[str, str] = {
    "submission": (
        f'{NAME} ci seal --kind submission --policy "$GITHUB_WORKSPACE/base"'
        ' --repo-dir "$GITHUB_WORKSPACE/pr" --commit "$HEAD_SHA" --base-sha "$BASE_SHA" --pr "$PR_NUMBER"'
        ' --out "$RUNNER_TEMP/evidence" --summary "$GITHUB_STEP_SUMMARY"'
    ),
    "integration": (
        f'{NAME} ci seal --kind integration --policy "$GITHUB_WORKSPACE" --repo-dir "$GITHUB_WORKSPACE"'
        ' --commit "$GITHUB_SHA" --out "$RUNNER_TEMP/evidence" --summary "$GITHUB_STEP_SUMMARY"'
    ),
}
EXPR_RE = re.compile(r"\$\{\{(.*?)\}\}", re.S)
USES_RE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<action>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@(?P<sha>\S+)\s+#\s*v\d+\.\d+\.\d+\s*$")


@dataclass(frozen=True)
class Render:
    root: Path
    text: str
    doc: dict[Any, Any]


def _render(root: Path, lang: str) -> Render:
    load_tool("render_template").render_to(root, lang)
    text = (root / WORKFLOW).read_bytes().decode("utf-8")
    return Render(root, text, yaml.safe_load(text))


@pytest.fixture(scope="module")
def render(tmp_path_factory: pytest.TempPathFactory) -> Render:
    return _render(tmp_path_factory.mktemp("seal-render") / "repo", "es")


def triggers(doc: Mapping[Any, Any]) -> dict[str, Any]:
    """Bloque ``on``: PyYAML (YAML 1.1) lee la clave ``on`` como ``True``."""
    value = doc.get("on", doc.get(True))
    assert isinstance(value, dict), value
    return value


def jobs(doc: Mapping[Any, Any]) -> dict[str, dict[str, Any]]:
    found = doc["jobs"]
    assert isinstance(found, dict)
    return found


def steps(doc: Mapping[Any, Any], job: str) -> list[dict[str, Any]]:
    return list(jobs(doc)[job]["steps"])


def action(step: Mapping[str, Any]) -> str | None:
    uses = step.get("uses")
    return uses.split("@", 1)[0] if isinstance(uses, str) else None


def flat(run: object) -> str:
    return " ".join(str(run).replace("\\\n", " ").split())


# -- disparadores, concurrencia y permisos (REQ-CI-50) ------------------------------------


def test_triggers_concurrency_and_top_level_permissions(render: Render) -> None:
    assert render.doc["name"] == "seal"
    assert triggers(render.doc) == {
        "push": {"branches": ["main"]},
        "pull_request_target": {"types": ["opened", "synchronize", "reopened", "ready_for_review"]},
    }
    assert render.doc["permissions"] == {}
    assert render.doc["concurrency"] == {
        "group": "seal-${{ github.event_name }}-${{ github.event.pull_request.head.sha || github.sha }}",
        "cancel-in-progress": False,
    }


def test_jobs_names_conditions_runners_and_permissions(render: Render) -> None:
    found = jobs(render.doc)
    assert list(found) == ["submission", "integration"]
    sub, integ = found["submission"], found["integration"]
    assert (sub["name"], integ["name"]) == (f"{NAME}/seal-submission", f"{NAME}/seal-integration")
    assert sub["if"] == f"github.event_name == 'pull_request_target' && {SAME_REPO}"
    assert integ["if"] == "github.event_name == 'push'"
    assert (sub["runs-on"], sub["timeout-minutes"]) == ("ubuntu-slim", 5)
    assert (integ["runs-on"], integ["timeout-minutes"]) == ("ubuntu-slim", 10)
    assert sub["permissions"] == {"contents": "read", "pull-requests": "write"}
    assert integ["permissions"] == {"contents": "read", "pull-requests": "write", "actions": "read"}
    for job in found.values():
        assert "needs" not in job and "container" not in job and "services" not in job and "secrets" not in job


def test_no_write_to_contents_identity_tokens_secrets_or_attestations(render: Render) -> None:
    text = render.text
    for forbidden in ("contents: write", "id-token", "secrets.", "secrets:", "attest", "sigstore", "cosign",
                      "git push", "update-ref", "git commit", "write-all", "workflow_run",
                      "allow-unsafe-pr-checkout"):
        assert forbidden not in text.lower(), forbidden
    for job in jobs(render.doc).values():
        assert set(job["permissions"].values()) <= {"read", "write"}
        assert {k for k, v in job["permissions"].items() if v == "write"} == {"pull-requests"}


def test_actions_are_pinned_by_sha_with_their_version(render: Render) -> None:
    used = [USES_RE.match(line) for line in render.text.splitlines() if "uses:" in line]
    assert used and all(m is not None for m in used)
    assert {m["action"]: m["sha"] for m in used if m is not None} == dict(PINS)
    for job in jobs(render.doc).values():
        for step in job["steps"]:
            if action(step) == "actions/checkout":
                assert step["with"]["persist-credentials"] is False


# -- el PR se lee como datos (REQ-CI-29) -------------------------------------------------


def test_submission_reads_the_pr_into_its_own_path_and_never_runs_it(render: Render) -> None:
    checkouts = [s for s in steps(render.doc, "submission") if action(s) == "actions/checkout"]
    assert [c["with"] for c in checkouts] == [
        {"path": "base", "persist-credentials": False, "fetch-depth": 1},
        {"ref": PR_HEAD_SHA, "path": "pr", "persist-credentials": False, "fetch-depth": 0},
    ]
    for step in steps(render.doc, "submission"):
        workdir = str(step.get("working-directory", ""))
        assert not re.search(r"(^|/)pr(/|$)", workdir), step
        run = str(step.get("run", "")).replace('--repo-dir "$GITHUB_WORKSPACE/pr"', "")
        assert "GITHUB_WORKSPACE/pr" not in run and not re.search(r"\bcd\s", run), step.get("name")
        assert not re.search(r"(?:^|[\s;&|(])(?:\./)?pr/", run), step.get("name")


def test_run_steps_take_pr_data_only_through_env(render: Render) -> None:
    for job in jobs(render.doc).values():
        for step in job["steps"]:
            if "run" in step:
                assert "${{" not in str(step["run"]), step.get("name")
    seal_env = next(s for s in steps(render.doc, "submission") if "ci seal" in str(s.get("run", "")))["env"]
    assert seal_env == {
        "GITHUB_TOKEN": "${{ github.token }}",
        "PR_NUMBER": "${{ github.event.pull_request.number }}",
        "HEAD_SHA": PR_HEAD_SHA,
        "BASE_SHA": "${{ github.event.pull_request.base.sha }}",
    }


# -- núcleo e invocaciones ----------------------------------------------------------------


@pytest.mark.parametrize(("job", "policy"), [("submission", "base/project.yaml"), ("integration", "project.yaml")])
def test_core_with_the_evidence_extra_is_installed_before_sealing(render: Render, job: str, policy: str) -> None:
    found = steps(render.doc, job)
    setup = next(i for i, s in enumerate(found) if action(s) == "astral-sh/setup-uv")
    install = next(i for i, s in enumerate(found) if "uv tool install" in str(s.get("run", "")))
    seal = next(i for i, s in enumerate(found) if "ci seal" in str(s.get("run", "")))
    upload = next(i for i, s in enumerate(found) if action(s) == "actions/upload-artifact")
    assert setup < install < seal < upload
    step = found[install]
    assert step["working-directory"] == "${{ runner.temp }}"
    assert step["env"] == {"POLICY_YAML": "${{ github.workspace }}/" + policy, "UV_NO_CONFIG": "1"}
    run = str(step["run"])
    assert "set -euo pipefail" in run and 'test -n "$PIN"' in run and "curl -fsSL --proto '=https'" in run
    assert f'uv tool install "{NAME}[evidence]==${{PIN}}" --constraints lock.txt' in run
    assert "{{var:" not in render.text
    assert found[setup]["with"] == {"enable-cache": False, "version": "0.12.17", "working-directory": "${{ runner.temp }}"}


@pytest.mark.parametrize("job", ["submission", "integration"])
def test_seal_lines_are_the_spec_ones_and_parse(render: Render, job: str) -> None:
    (step,) = [s for s in steps(render.doc, job) if "ci seal" in str(s.get("run", ""))]
    assert step["working-directory"] == "${{ runner.temp }}"  # nunca dentro de un árbol
    line = flat(step["run"])
    assert line == COMMANDS[job]
    words = shlex.split(line)
    assert words[:3] == [NAME, "ci", "seal"]
    parser = argparse.ArgumentParser()
    ci_seal._add_arguments(parser)
    values = {"GITHUB_WORKSPACE": "/w", "RUNNER_TEMP": "/t", "GITHUB_STEP_SUMMARY": "/s", "GITHUB_SHA": "a" * 40,
              "HEAD_SHA": "b" * 40, "BASE_SHA": "c" * 40, "PR_NUMBER": "7"}
    argv = [re.sub(r"\$([A-Z_]+)", lambda m: values[m[1]], w) for w in words[3:]]
    ns = parser.parse_args(argv)
    assert ns.kind == job and ns.out == "/t/evidence" and ns.summary == "/s"


@pytest.mark.parametrize("job", ["submission", "integration"])
def test_artifact_of_each_attempt(render: Render, job: str) -> None:
    (upload,) = [s for s in steps(render.doc, job) if action(s) == "actions/upload-artifact"]
    sha = PR_HEAD_SHA if job == "submission" else "${{ github.sha }}"
    assert upload["if"] == "always()"
    assert upload["with"] == {
        "name": f"evidence-{job}-{sha}-a${{{{ github.run_attempt }}}}",
        "path": "${{ runner.temp }}/evidence",
        "retention-days": 400,
        "if-no-files-found": "ignore",
    }


# -- coherencia con la spec, la regla y el otro idioma ------------------------------------------


def _spec_block(spec_dir: Path) -> dict[Any, Any]:
    text = (spec_dir / "09-ci.md").read_text(encoding="utf-8")
    for block in re.findall(r"^```yaml\n(.*?)^```", text, re.S | re.M):
        try:
            doc = yaml.safe_load(block)
        except yaml.YAMLError:
            continue
        if isinstance(doc, dict) and doc.get("name") == "seal" and "jobs" in doc:
            return doc
    raise AssertionError("09-ci no tiene el bloque de seal.yml")


def test_render_is_the_spec_block(render: Render) -> None:
    spec_dir = os.environ.get(f"{_meta.ENV_PREFIX}SPEC_DIR")
    if not spec_dir or not (Path(spec_dir) / "09-ci.md").is_file():
        pytest.skip(f"sin {_meta.ENV_PREFIX}SPEC_DIR")
    spec = _spec_block(Path(spec_dir))
    assert triggers(spec) == triggers(render.doc)
    for key in ("name", "permissions", "concurrency"):
        assert spec[key] == render.doc[key], key
    assert list(jobs(spec)) == list(jobs(render.doc))
    for job_id, spec_job in jobs(spec).items():
        job = jobs(render.doc)[job_id]
        assert {k: v for k, v in spec_job.items() if k != "steps"} == {k: v for k, v in job.items() if k != "steps"}
        assert len(spec_job["steps"]) == len(job["steps"]), job_id
        for spec_step, step in zip(spec_job["steps"], job["steps"]):
            if set(spec_step) == {"name"}:  # el paso de instalación de REQ-CI-12: en la spec, solo su nombre
                assert spec_step["name"] == step["name"] and "uv tool install" in str(step["run"])
            else:
                assert spec_step == step, (job_id, spec_step.get("name"))


def test_workflow_is_not_localized(render: Render, tmp_path: Path) -> None:
    other = _render(tmp_path / "en", "en")
    assert other.text == render.text


def test_ci_policy_rule_has_no_findings_on_the_render(render: Render) -> None:
    from tests.unit.test_rule_ci_policy import run as run_ci_policy

    assert run_ci_policy({".github/workflows/seal.yml": render.text}) == []
    # Con otro nombre, pull_request_target ya no se admite (SB06).
    findings = run_ci_policy({".github/workflows/seal-copy.yml": render.text})
    assert [f.path for f in findings] == [".github/workflows/seal-copy.yml"]


def test_text_encoding(render: Render) -> None:
    data = (render.root / WORKFLOW).read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf") and b"\r" not in data and b"\t" not in data
    assert data.endswith(b"\n") and not data.endswith(b"\n\n")
    assert all(line == line.rstrip() for line in data.decode("utf-8").splitlines())


@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint no está instalado (lo corre el CI)")
def test_actionlint_on_the_render(render: Render, tmp_path: Path) -> None:
    config = tmp_path / "actionlint.yaml"
    config.write_text("self-hosted-runner:\n  labels: [ubuntu-slim]\n", encoding="utf-8")
    proc = subprocess.run(
        ["actionlint", "-no-color", "-shellcheck=", "-pyflakes=", "-config-file", str(config), str(render.root / WORKFLOW)],
        cwd=render.root, capture_output=True, timeout=300, check=False,
    )
    assert proc.returncode == 0, proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
