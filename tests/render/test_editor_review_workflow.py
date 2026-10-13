# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Los workflows del editor del artículo sobre el render (09-ci §1, §2 y §4.5; REQ-CI-57 a REQ-CI-60).

- Solo existen con ``format: latex`` (REQ-CI-57; 01-arquitectura REQ-ARCH-100).
- ``editor-review.yml``: los disparadores, la concurrencia, el job ``evaluate`` con sus
  permisos y su línea ``ci editor-review``, sin checkout del PR; el número del PR llega por
  ``env`` (REQ-CI-7), del evento o de ``workflow_run.pull_requests`` (REQ-CI-60).
- ``editor-review-trigger.yml``: ``pull_request_review`` sin permisos, secretos ni pasos que
  lean el PR; su ``name:`` es el que espera el ``workflow_run`` (REQ-CI-60).
- La regla ``ci-policy`` no tiene hallazgos y, si está instalado, ``actionlint`` tampoco.
"""

from __future__ import annotations

import argparse
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
from aucove.ops import ci_editor_review
from tests.conftest import load_tool

WORKFLOWS = Path(".github") / "workflows"
EDITOR, TRIGGER = "editor-review.yml", "editor-review-trigger.yml"
NAME = _meta.NAME
PR_NUMBER = "${{ github.event.pull_request.number || github.event.workflow_run.pull_requests[0].number }}"
COMMAND = f'{NAME} ci editor-review --pr "$PR_NUMBER" --policy "$GITHUB_WORKSPACE/base" --summary "$GITHUB_STEP_SUMMARY"'


@dataclass(frozen=True)
class Render:
    root: Path
    texts: Mapping[str, str]
    docs: Mapping[str, dict[Any, Any]]


@pytest.fixture(scope="module")
def render(tmp_path_factory: pytest.TempPathFactory) -> Render:
    root = tmp_path_factory.mktemp("editor-review-render") / "repo"
    load_tool("render_template").render_to(root, "es")
    texts = {n: (root / WORKFLOWS / n).read_bytes().decode("utf-8") for n in (EDITOR, TRIGGER)}
    return Render(root, texts, {n: yaml.safe_load(t) for n, t in texts.items()})


def triggers(doc: Mapping[Any, Any]) -> dict[str, Any]:
    """Bloque ``on``: PyYAML (YAML 1.1) lee la clave ``on`` como ``True``."""
    value = doc.get("on", doc.get(True))
    assert isinstance(value, dict), value
    return value


@pytest.mark.parametrize("fmt", ["latex", "none"])
def test_only_with_latex(fmt: str) -> None:
    files = load_tool("render_template").render_files("en", fmt=fmt)
    present = {rel for rel in files if rel.startswith(".github/workflows/editor-review")}
    assert present == ({f".github/workflows/{EDITOR}", f".github/workflows/{TRIGGER}"} if fmt == "latex" else set())


def test_editor_review_triggers_and_concurrency(render: Render) -> None:
    doc = render.docs[EDITOR]
    assert doc["name"] == "editor-review" and doc["permissions"] == {}
    assert triggers(doc) == {
        "pull_request_target": {"types": ["opened", "synchronize", "reopened", "ready_for_review"]},
        "workflow_run": {"workflows": [render.docs[TRIGGER]["name"]], "types": ["completed"]},
    }
    assert doc["concurrency"] == {"group": "editor-review-" + PR_NUMBER, "cancel-in-progress": True}


def test_evaluate_job(render: Render) -> None:
    (job_id, job), = render.docs[EDITOR]["jobs"].items()
    assert job_id == "evaluate" and "name" not in job  # el check lo publica ci editor-review (REQ-CI-59)
    assert job["runs-on"] == "ubuntu-slim" and job["timeout-minutes"] == 5
    assert job["permissions"] == {"contents": "read", "pull-requests": "read", "checks": "write"}
    assert job["if"] == ("github.event_name == 'pull_request_target'"
                         " || github.event.workflow_run.pull_requests[0] != null")
    checkouts = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert [c["with"] for c in checkouts] == [{"path": "base", "persist-credentials": False, "fetch-depth": 100}]
    step = job["steps"][-1]
    assert step["env"] == {"GITHUB_TOKEN": "${{ github.token }}", "PR_NUMBER": PR_NUMBER}
    assert step["working-directory"] == "${{ runner.temp }}"
    assert " ".join(step["run"].split()) == COMMAND


def test_the_command_line_parses(render: Render) -> None:
    """La línea del workflow la acepta el parser de ``ci editor-review``."""
    words = shlex.split(COMMAND.replace('"$PR_NUMBER"', "12"))[3:]
    parser = argparse.ArgumentParser()
    ci_editor_review.COMMANDS[0].add_arguments(parser)
    args = parser.parse_args(words)
    assert (args.pr, args.policy, args.summary) == (12, "$GITHUB_WORKSPACE/base", "$GITHUB_STEP_SUMMARY")


def test_trigger_has_nothing_to_leak(render: Render) -> None:
    doc = render.docs[TRIGGER]
    assert doc["name"] == "editor-review-trigger" and doc["permissions"] == {}
    assert triggers(doc) == {"pull_request_review": {"types": ["submitted", "dismissed"]}}
    assert "concurrency" not in doc and "env" not in doc
    assert doc["jobs"] == {"trigger": {"runs-on": "ubuntu-slim", "timeout-minutes": 1, "permissions": {},
                                       "steps": [{"run": "exit 0"}]}}
    assert "${{" not in render.texts[TRIGGER] and "secrets" not in render.texts[TRIGGER]


def test_ci_policy_rule_has_no_findings_on_the_render(render: Render) -> None:
    from tests.unit.test_rule_ci_policy import run as run_ci_policy

    files = {f".github/workflows/{n}": t for n, t in render.texts.items()}
    assert run_ci_policy(files) == []
    # Con otro nombre, pull_request_target y workflow_run ya no se admiten (SB06).
    findings = run_ci_policy({".github/workflows/editor-copy.yml": render.texts[EDITOR]})
    assert len(findings) == 2 and {f.path for f in findings} == {".github/workflows/editor-copy.yml"}


@pytest.mark.parametrize("name", [EDITOR, TRIGGER])
def test_text_encoding(render: Render, name: str) -> None:
    data = (render.root / WORKFLOWS / name).read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf") and b"\r" not in data and b"\t" not in data
    assert data.endswith(b"\n") and not data.endswith(b"\n\n")
    assert all(line == line.rstrip() for line in data.decode("utf-8").splitlines())


@pytest.mark.skipif(shutil.which("actionlint") is None, reason="actionlint no está instalado (lo corre el CI)")
def test_actionlint_on_the_render(render: Render, tmp_path: Path) -> None:
    config = tmp_path / "actionlint.yaml"
    config.write_text("self-hosted-runner:\n  labels: [ubuntu-slim]\n", encoding="utf-8")
    files = [str(render.root / WORKFLOWS / n) for n in (EDITOR, TRIGGER)]
    proc = subprocess.run(
        ["actionlint", "-no-color", "-shellcheck=", "-pyflakes=", "-config-file", str(config), *files],
        cwd=render.root, capture_output=True, timeout=300, check=False,
    )
    assert proc.returncode == 0, proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
