# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Autoprueba del job ``audit`` de ``main-broken.yml`` (00-marco D-37; 09-ci §7.1, REQ-CI-46).

E1 sobre el render ``latex`` en ``es`` y en ``en`` (fixture ``ci``): alguien empuja un commit
directo a la base, sin PR. El checkout de la base es ``$GITHUB_WORKSPACE`` y el directorio de
trabajo, y la línea del paso «Auditar la integración» se ejecuta por subproceso con el lanzador
instalado. La política es la del commit anterior (``--before``).

- ``ci audit`` clasifica el commit como ``direct_push`` y abre un Issue con ``flag:bypass``, el
  marcador ``<!-- aucove:ci:audit v1 -->`` y la mención al ``lead`` (06-permisos REQ-PERM-23).
- Un run repetido del job no abre otro.
- Con ``--forced true`` se registra ``force_push``.
"""

from __future__ import annotations

from pathlib import Path

from aucove import _meta
from aucove.forge.fake import FakeForge
from tests.e2e.conftest import PREFIX, Launcher, git, workflow_command
from tests.e2e.template_repo import TemplateCi

# Qué líneas de los workflows ejerce este módulo (test_template_ci::test_every_workflow_command_is_exercised).
EXERCISED: tuple[str, ...] = ("ci audit",)


def _audit_issues(state: Path) -> list[object]:
    marker = _meta.ci_marker("audit")
    return [i for i in FakeForge.load(state).list_issues(labels=["flag:bypass"], state="all") if marker in i.body]


def test_the_audit_job(ci: TemplateCi, launcher: Launcher, tmp_path: Path) -> None:
    env = ci.env(tmp_path)
    state = Path(env[PREFIX + "FORGE_FAKE_STATE"])
    workspace = ci.clone_main(tmp_path / "workspace")
    (workspace / "notas-a-mano.md").write_text("Un cambio sin PR.\n", encoding="utf-8", newline="\n")
    git(workspace, ci.git_env, "add", "notas-a-mano.md")
    git(workspace, ci.git_env, "commit", "-q", "-m", "Empuje directo")
    after = git(workspace, ci.git_env, "rev-parse", "HEAD")
    job_env = {**env, "GITHUB_TOKEN": "fake-token"}
    line = workflow_command("ci audit")

    def audit(forced: str) -> str:
        run = launcher.run_line(line, {"BEFORE": ci.root_sha, "GITHUB_SHA": after, "FORCED": forced},
                                cwd=workspace, env=job_env)
        assert run.returncode == 0, run.explain()
        return run.stdout.strip()

    assert audit("false") == f"merge_audit=direct_push sha={after} pr=-"
    (issue,) = _audit_issues(state)
    assert "flag:bypass" in issue.labels and f"@{ci.lead}" in issue.body  # type: ignore[attr-defined]
    assert audit("false") == f"merge_audit=direct_push sha={after} pr=-"  # un re-run del job
    assert len(_audit_issues(state)) == 1
    assert audit("true") == f"merge_audit=force_push sha={after} pr=-"
    assert len(_audit_issues(state)) == 2
