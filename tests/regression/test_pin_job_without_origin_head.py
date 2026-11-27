# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""El job ``pin`` de ``validate.yml`` y ``origin/HEAD`` (issue #31; 09-ci REQ-CI-45).

En todo PR que cambiaba ``core_pin``, ``aucove/validate`` fallaba con tres errores
``E_INTERNAL_UNEXPECTED``: el job ``pin`` corre ``validate --mode tree --tree pr``, que lee la
política de ``origin/HEAD`` del árbol, y ``actions/checkout`` no crea esa ref. ``validate``
terminaba con ``E_CONFIG_POLICY_UNAVAILABLE`` (``ref_missing``), el ``|| true`` del paso lo
ocultaba, y el job ``validate`` rechazaba el reporte por no ser un sobre válido. La autoprueba
no lo vio porque armaba los checkouts con ``git clone``, que sí crea ``origin/HEAD``.

Ahora el paso apunta ``origin/HEAD`` a la rama por defecto con ``git remote set-head``, falla si
``validate`` no pudo correr, y ``validate`` dice con qué código terminó. Además, en GitHub Actions
el núcleo toma la rama por defecto del evento cuando falta ``origin/HEAD`` (``config.base``): así
funcionan los workflows de los proyectos creados antes, sin ``set-head`` en el ``pin`` y sin
``--policy`` en ``expire-claims``, que fallaba todos los días por la misma causa. Estas pruebas usan
checkouts como los de ``actions/checkout``: ``init``, ``fetch`` y la cabeza, sin ``origin/HEAD``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aucove.ops import validate
from aucove.ops.context import Invocation, OpContext
from aucove.ops.result import dumps
from tests.conftest import GitRepo
from tests.unit.test_validate_modes import ROLES, default_probe, git, run_validate, seed_base


def runner_env(tmp_path: Path, default_branch: str = "main") -> dict[str, str]:
    """``GITHUB_ACTIONS`` y el evento que escribe el runner, con la rama por defecto del repositorio."""
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"repository": {"default_branch": default_branch}}), encoding="utf-8")
    return {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_PATH": str(event)}


def actions_checkout(repo: GitRepo, dest: Path, head: str) -> Path:
    """Lo que hace ``actions/checkout`` con ``ref: <sha>`` y ``fetch-depth: 0``."""
    dest.mkdir(parents=True)
    git(dest, repo.env, "init", "-q")
    git(dest, repo.env, "remote", "add", "origin", str(repo.origin))
    git(dest, repo.env, "fetch", "-q", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*",
        "+refs/tags/*:refs/tags/*")
    git(dest, repo.env, "checkout", "-q", "--detach", head)
    return dest


def test_the_pin_step_needs_origin_head(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    default_probe(monkeypatch)
    seed_base(git_repo)
    git_repo.git("checkout", "-q", "-b", "task/3")
    head = git_repo.commit("pr", files={"roles.yaml": ROLES.replace("lead: ana", "lead: bruno")})
    git_repo.push("task/3")
    work = tmp_path / "work"
    pr = actions_checkout(git_repo, work / "pr", head)
    assert git(pr, git_repo.env, "for-each-ref", "refs/remotes/origin/HEAD").strip() == ""

    # Lo que pasaba en el issue: validate no puede correr, y el reporte es el sobre del error.
    report = work / "pin.json"
    failed = run_validate(work, git_repo.env, tree="pr", checks=["schemas"], report=str(report))
    assert failed.error is not None and failed.error.code == "E_CONFIG_POLICY_UNAVAILABLE"
    assert failed.error.details == {"ref": "origin/HEAD", "reason": "ref_missing"}
    assert failed.exit_code() == 8  # ni 0 ni 3: ahora el paso falla
    pin = validate.read_pin_report(report)
    assert (pin.problem, pin.error) == ("failed", "E_CONFIG_POLICY_UNAVAILABLE (ref_missing)")

    # La línea nueva del paso: git -C pr remote set-head origin "$DEFAULT_BRANCH".
    git(work, git_repo.env, "-C", "pr", "remote", "set-head", "origin", "main")
    passed = run_validate(work, git_repo.env, tree="pr", checks=["schemas"], report=str(report))
    assert passed.ok, passed.to_dict()
    assert report.read_bytes() == dumps(passed)
    assert validate.read_pin_report(report).problem is None


def test_the_old_pin_step_works_with_the_event_of_the_runner(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch,
                                                             tmp_path: Path) -> None:
    """Un proyecto creado antes: su paso no tiene ``set-head``, pero el núcleo del PR lee el evento."""
    default_probe(monkeypatch)
    seed_base(git_repo)
    git_repo.git("checkout", "-q", "-b", "task/3")
    head = git_repo.commit("pr", files={"roles.yaml": ROLES.replace("lead: ana", "lead: bruno")})
    git_repo.push("task/3")
    work = tmp_path / "work"
    actions_checkout(git_repo, work / "pr", head)
    env = {**git_repo.env, **runner_env(tmp_path)}
    result = run_validate(work, env, tree="pr", checks=["schemas"])
    assert result.ok, result.to_dict()
    assert result.data is not None and result.data["base"]["ref"] == "origin/main"  # type: ignore[index]


def test_expire_claims_without_policy_reads_the_base_from_the_event(git_repo: GitRepo, tmp_path: Path) -> None:
    """``aucove expire-claims --json``, sin ``--policy``, en el checkout de la rama por defecto."""
    base = seed_base(git_repo)
    checkout = tmp_path / "workspace"
    checkout.mkdir()
    git(checkout, git_repo.env, "init", "-q")
    git(checkout, git_repo.env, "remote", "add", "origin", str(git_repo.origin))
    git(checkout, git_repo.env, "fetch", "-q", "--no-tags", "--depth", "1", "origin",
        "+refs/heads/main:refs/remotes/origin/main")
    git(checkout, git_repo.env, "checkout", "-q", "-B", "main", "refs/remotes/origin/main")

    def base_of(env: dict[str, str]) -> str:
        inv = Invocation(root=None, lang="es", json=True, dry_run=False, surface="cli", env=env, cwd=str(checkout))
        ctx = OpContext(inv)
        ctx.project()
        return ctx.base().ref

    with pytest.raises(Exception) as info:  # lo que pasaba: sin origin/HEAD no hay política
        base_of(dict(git_repo.env))
    assert getattr(info.value, "code", None) == "E_CONFIG_POLICY_UNAVAILABLE"
    assert base_of({**git_repo.env, **runner_env(tmp_path)}) == "origin/main"
    assert git(checkout, git_repo.env, "rev-parse", "HEAD").strip() == base
