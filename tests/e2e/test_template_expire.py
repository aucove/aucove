# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Autoprueba de ``expire-claims.yml`` (00-marco D-37; 09-ci §6; 05-operaciones §9.19; 04-tareas §4).

E1 sobre el render ``latex`` en ``es`` y en ``en`` (fixture ``ci``) más una tarea, la 30,
reclamada por el ``lead`` sin PR ni actividad desde el primer día. Con el reloj del fake a
16 días, el job ``expire`` corre como en Actions: el checkout de la rama por defecto es
``$GITHUB_WORKSPACE`` y el directorio de trabajo, y las dos líneas del paso «Vencer reclamos»
se ejecutan por subproceso con el lanzador instalado.

- ``expire-claims --json`` deja su sobre en ``$RUNNER_TEMP/expire.json`` (valida contra
  ``result``) y vence la 30 (``state:expired``); las tareas de E1 tienen PR (en revisión) y no
  vencen.
- ``ci comment --kind expire-claims`` escribe el resumen del job en el idioma del proyecto
  (``--policy .``) y no toca la forja.
"""

from __future__ import annotations

import json
from pathlib import Path

from aucove import _meta
from aucove.config.schemas import validate_result
from aucove.forge.fake import FakeForge
from aucove.i18n import translator
from tests.e2e.conftest import PREFIX, Launcher, at, seed_claimed_task, workflow_command
from tests.e2e.template_repo import TemplateCi, runner_temp

# Qué líneas de los workflows ejerce este módulo (test_template_ci::test_every_workflow_command_is_exercised).
EXERCISED: tuple[str, ...] = ("expire-claims", "ci comment --kind expire-claims")

STALE = 30  # la tarea sin PR ni actividad
DAY = 24 * 60  # minutos


def test_the_expire_job(ci: TemplateCi, launcher: Launcher, tmp_path: Path) -> None:
    env = ci.env(tmp_path)
    state = Path(env[PREFIX + "FORGE_FAKE_STATE"])
    forge = FakeForge.load(state)
    claim = forge.seed_commit({}, parents=[ci.root_sha], message=f"{_meta.CLAIM_SUBJECT}{STALE}", author=ci.lead)
    forge.seed_branch(f"task/{STALE}", claim)
    seed_claimed_task(forge, number=STALE, lead=ci.lead, claimant=ci.lead, sections=["introduction"],
                      language=ci.lang, claim_sha=claim, base_sha=ci.root_sha, head_sha=claim,
                      summary="A task nobody touched again.")
    forge.clock.set(at(16 * DAY))  # 14 días es limits.claim_expiry_days de la plantilla
    forge.save(state)
    workspace = ci.clone_main(tmp_path / "workspace")
    rt = runner_temp(tmp_path)
    summary = rt / "step-summary.md"
    variables = {"RUNNER_TEMP": str(rt), "GITHUB_STEP_SUMMARY": str(summary)}
    job_env = {**env, **variables, "GITHUB_TOKEN": "fake-token"}

    run = launcher.run_line(workflow_command("expire-claims"), variables, cwd=workspace, env=job_env)
    assert run.returncode == 0, run.explain()
    envelope = json.loads((rt / "expire.json").read_text(encoding="utf-8"))
    assert validate_result(envelope) == [], envelope
    data = envelope["data"]
    assert [e["task"] for e in data["expired"]] == [STALE] and data["warned"] == []
    issue = FakeForge.load(state).get_issue(STALE)
    assert issue is not None and "state:expired" in issue.labels
    for pr in ci.prs.values():  # en revisión: fuera del alcance (04-tareas REQ-TASK-33)
        task = FakeForge.load(state).get_issue(pr.task)
        assert task is not None and "state:expired" not in task.labels

    before = len(json.loads(state.read_text(encoding="utf-8"))["calls"])
    comment = launcher.run_line(workflow_command("ci comment --kind expire-claims"), variables, cwd=workspace,
                                env=job_env)
    assert comment.returncode == 0, comment.explain()
    assert len(json.loads(state.read_text(encoding="utf-8"))["calls"]) == before  # solo el resumen
    text = summary.read_text(encoding="utf-8")
    t = translator(ci.lang)
    assert text.startswith(f"### {_meta.NAME}/expire-claims\n")
    assert f"- {t('ci.comment.expire.list.expired')}: #{STALE} (`@{ci.lead}`)" in text
