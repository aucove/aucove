# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Autoprueba de ``editor-review.yml`` (00-marco D-37 y D-56; 09-ci §4.5; 12-mvp E13).

El escenario es E1 sobre el render ``latex`` en ``es`` y en ``en`` (fixture ``ci``) con la
editora del artículo declarada en el ``roles.yaml`` de la rama por defecto: la línea
``# editor: example-researcher`` de la plantilla, sin el ``#``. El job ``evaluate`` corre como
en Actions: su ``if:`` y el ``env`` de su paso se evalúan del render con el contexto de cada
evento (``pull_request_target`` o el ``workflow_run`` del disparador), ``base/`` es la rama por
defecto y la línea es la de ``WORKFLOW_COMMANDS``, por subproceso con el lanzador instalado.

E13: ``aucove/editor-review`` falla en el PR 21 (toca la parte ``introduction``) hasta que la
editora aprueba la cabeza; vuelve a fallar tras un push nuevo, y un ``CHANGES_REQUESTED`` lo
deja en ``failure``. El PR 22 (solo ``core_pin``) no toca el LaTeX y pasa. En cada cabeza hay un
solo check run de ese nombre (REQ-CI-59), y la única escritura en la forja es ese check. Un
``workflow_run`` sin PR (un PR desde un fork) no corre el job.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from aucove import _meta
from aucove.domain.checks import EDITOR_REVIEW
from aucove.forge.fake import FakeForge
from tests.e2e.actions import condition, interpolate, step_env
from tests.e2e.conftest import PREFIX, Launcher, Run, workflow_command
from tests.e2e.template_repo import INTRO, PR_CONTENT, PR_PIN, TemplateCi, runner_temp

# Qué líneas de los workflows ejerce este módulo (test_template_ci::test_every_workflow_command_is_exercised).
EXERCISED: tuple[str, ...] = ("ci editor-review",)

WORKFLOW = Path(".github") / "workflows" / "editor-review.yml"
EDITOR = "example-researcher"
COMMENTED_EDITOR = "# editor: example-researcher"


@dataclass
class World:
    ci: TemplateCi
    launcher: Launcher
    env: dict[str, str]
    state: Path
    workspace: Path
    temp: Path
    job: Mapping[str, Any]

    def forge(self) -> FakeForge:
        return FakeForge.load(self.state)

    def edit(self) -> _Edit:
        return _Edit(self)

    def calls(self) -> int:
        return len(json.loads(self.state.read_text(encoding="utf-8"))["calls"])

    def writes_since(self, before: int) -> list[str]:
        calls = json.loads(self.state.read_text(encoding="utf-8"))["calls"][before:]
        return [c["method"] for c in calls if c["kind"] in ("E", "C")]


class _Edit:
    """Cambia el estado del fake entre corridas: ``with world.edit() as forge: ...``."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.forge = world.forge()

    def __enter__(self) -> FakeForge:
        return self.forge

    def __exit__(self, *exc: object) -> None:
        if exc[0] is None:
            self.forge.save(self.world.state)


@pytest.fixture
def world(ci: TemplateCi, launcher: Launcher, tmp_path: Path) -> World:
    env = ci.env(tmp_path)
    ws = tmp_path / "workspace"
    ws.mkdir()
    ci.clone_main(ws / "base", depth=100)
    roles = ws / "base" / "roles.yaml"
    text = roles.read_text(encoding="utf-8")
    assert text.count(COMMENTED_EDITOR) == 1, "la plantilla ya no trae la línea del editor comentada"
    roles.write_text(text.replace(COMMENTED_EDITOR, COMMENTED_EDITOR[2:]), encoding="utf-8", newline="\n")
    doc = yaml.safe_load((ci.root / "dev" / WORKFLOW).read_text(encoding="utf-8"))
    return World(ci, launcher, env, Path(env[PREFIX + "FORGE_FAKE_STATE"]), ws, runner_temp(tmp_path),
                 doc["jobs"]["evaluate"])


def contexts(event: str, pr: int | None, temp: Path) -> dict[str, Any]:
    if event == "pull_request_target":
        payload: dict[str, Any] = {"pull_request": {"number": pr}}
    else:
        payload = {"workflow_run": {"pull_requests": [] if pr is None else [{"number": pr}]}}
    return {"github": {"event_name": event, "event": payload, "token": "fake-token"},
            "runner": {"temp": str(temp)}}


def evaluate(w: World, event: str, pr: int | None) -> Run | None:
    """El job ``evaluate`` para ``event``; ``None`` si su ``if:`` no lo deja correr."""
    ctx = contexts(event, pr, w.temp)
    if not condition(w.job.get("if"), ctx):
        return None
    step = w.job["steps"][-1]
    assert interpolate(step["working-directory"], ctx) == str(w.temp)
    summary = w.temp / "step-summary.md"
    variables = {"GITHUB_WORKSPACE": str(w.workspace), "GITHUB_STEP_SUMMARY": str(summary),
                 "GITHUB_REPOSITORY": w.ci.project.forge_repo, **step_env(step, ctx)}
    return w.launcher.run_line(workflow_command("ci editor-review"), variables, cwd=w.temp,
                               env={**w.env, **variables})


def check(w: World, sha: str) -> tuple[str | None, str]:
    forge = w.forge()
    runs = [r for r in forge.get_checks(sha).runs if r.name == EDITOR_REVIEW]
    assert len(runs) <= 1, runs  # REQ-CI-59: uno solo por cabeza
    return (runs[0].conclusion if runs else None), forge.check_summary(sha, EDITOR_REVIEW) or ""


def outputs(run: Run | None) -> dict[str, str]:
    assert run is not None
    return dict(line.split("=", 1) for line in run.stdout.splitlines())


def test_e13_editor_review(world: World) -> None:
    pr = world.ci.prs[PR_CONTENT]
    before = world.calls()
    run = evaluate(world, "pull_request_target", pr.number)
    assert outputs(run) == {"conclusion": "failure", "reason": "missing"} and run.returncode == 0, run.explain()  # type: ignore[union-attr]
    conclusion, summary = check(world, pr.head_sha)
    assert conclusion == "failure" and f"@{EDITOR}" in summary and pr.head_sha[:12] in summary
    assert (world.temp / "step-summary.md").read_text(encoding="utf-8") == summary
    assert world.writes_since(before) == ["upsert_check_run"]  # solo el check: nada en ramas ni comentarios

    # La editora aprueba: pull_request_review -> workflow_run del disparador -> verde.
    with world.edit() as forge:
        forge.seed_review(pr.number, author=EDITOR, state="APPROVED", commit_sha=pr.head_sha)
    assert outputs(evaluate(world, "workflow_run", pr.number)) == {"conclusion": "success", "reason": "approved"}
    assert check(world, pr.head_sha)[0] == "success"

    # Un push nuevo: pull_request_target (synchronize) en la cabeza nueva, sin aprobación.
    with world.edit() as forge:
        head2 = forge.seed_commit({INTRO: b"\\section*{x}\nOtra frase.\n"}, parents=[pr.head_sha],
                                  message="more", author=world.ci.lead)
        forge.update_ref(pr.branch, head2)
    assert outputs(evaluate(world, "pull_request_target", pr.number)) == {"conclusion": "failure", "reason": "missing"}
    assert check(world, head2)[0] == "failure" and check(world, pr.head_sha)[0] == "success"

    # La editora pide cambios sobre la cabeza nueva.
    with world.edit() as forge:
        forge.seed_review(pr.number, author=EDITOR, state="CHANGES_REQUESTED", commit_sha=head2)
    run = evaluate(world, "workflow_run", pr.number)
    assert outputs(run) == {"conclusion": "failure", "reason": "changes_requested"}
    assert check(world, head2)[0] == "failure"


def test_a_pr_without_latex_passes(world: World) -> None:
    pr = world.ci.prs[PR_PIN]  # solo cambia core_pin en project.yaml
    assert outputs(evaluate(world, "pull_request_target", pr.number)) == {"conclusion": "success", "reason": "no_latex"}
    assert check(world, pr.head_sha)[0] == "success"


def test_workflow_run_without_pull_requests_does_not_run(world: World) -> None:
    before = world.calls()
    assert evaluate(world, "workflow_run", None) is None
    assert world.calls() == before


def test_messages_are_in_the_project_language(world: World) -> None:
    pr = world.ci.prs[PR_CONTENT]
    evaluate(world, "pull_request_target", pr.number)
    summary = check(world, pr.head_sha)[1]
    expected = "falta la aprobación" if world.ci.lang == "es" else "the approval of"
    assert expected in summary and f"{_meta.NAME} review approve {pr.number}" in summary
