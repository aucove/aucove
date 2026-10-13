# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``init`` e ``init --regenerate`` (05 §9.3; 06-permisos §4 y §5; 01 REQ-ARCH-87; 00-marco D-62).

El punto de partida es un repositorio recién creado desde la plantilla publicada (el render
``en`` con ``format: latex``): ``origin`` es un repo local y la forja, el falso, con la cuenta
``ana`` como administradora del repositorio y la configuración de GitHub por omisión.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from aucove import _meta
from aucove.config.project import parse_project
from aucove.config.schemas import validate_result
from aucove.forge.base import RepoRef
from aucove.forge.fake import FakeClock, FakeForge
from aucove.i18n import translator
from aucove.localstore.store import IdentityCache, Store
from aucove.ops import init as init_op
from aucove.ops.result import Result
from tests.board_world import NOW, Typed, World, at, clean_env
from tests.conftest import GitRepo, isolated_git_env, load_tool

LEAD = "ana"
REPO = RepoRef("lab-x", "estudio")
FAR = "2030-01-01T00:00:00Z"  # vencimiento de la identidad en caché


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(init_op, "_sleep", lambda seconds: None)  # REQ-FORGE-50 sin esperar


def init_world(tmp: Path, *, profile: str = "enforced", visibility: str | None = None,
               files: Mapping[str, bytes] | None = None) -> World:
    """El repo recién creado desde la plantilla, publicado en ``origin/main``, y el falso."""
    path = tmp / "repo"
    list(load_tool("render_template").render_to(path, "en", fmt="latex"))
    for rel, data in (files or {}).items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_bytes(data)
    env = isolated_git_env(tmp / "git-env")
    origin = tmp / "origin.git"
    repo = GitRepo(path, origin, env)
    repo._run(tmp, ["init", "-q", "--bare", "-b", "main", str(origin)], True)
    repo.git("init", "-q", "-b", "main")
    repo.git("remote", "add", "origin", str(origin))
    sha = repo.commit("Initial commit from the template")
    repo.push("main")
    repo.fetch()
    forge = FakeForge(REPO, viewer=LEAD, clock=FakeClock(NOW), profile=profile,  # type: ignore[arg-type]
                      visibility=visibility)  # type: ignore[arg-type]
    forge.seed_user(LEAD, permission="admin")
    forge.seed_user("bruno", permission="read")
    forge.seed_admin(fresh=True)
    base = forge.seed_commit({"README.md": b"x\n"}, [], "base", LEAD, date=at(-1000))
    forge.seed_branch("main", base)
    return World(repo=repo, forge=forge, base_sha=sha, env=clean_env(env))


@dataclass
class Terminal:
    """La persona en la terminal tras la fase 2: las frases que teclea y el token."""

    refuse: set[str] = field(default_factory=set)  # frases que no teclea
    token: str = "admin-token"
    seen: list[tuple[str, str]] = field(default_factory=list)

    def phrase(self, summary: str, phrase: str) -> bool:
        self.seen.append(("phrase", phrase))
        return phrase not in self.refuse

    def secret(self, prompt: str) -> str:
        self.seen.append(("secret", prompt))
        return self.token


def run_init(w: World, term: Terminal | None = None, *, typed: Typed | None = None, login: str = LEAD,
             dry_run: bool = False, **kwargs: object) -> Result:
    term = term or Terminal()
    inv = {"surface": "cli", "tty": True, "confirm": typed or Typed(), "phrase": term.phrase, "secret": term.secret}
    return w.run("init", login=login, lang="en", dry_run=dry_run, inv=inv, **kwargs)


def valid(result: Result) -> Result:
    envelope = result.to_dict()
    assert validate_result(envelope) == [], json.dumps(envelope, ensure_ascii=False, indent=1)
    return result


def protections(result: Result) -> dict[str, str]:
    return {p["id"]: p["action"] for p in result.data["protections"]}  # type: ignore[index]


def codes(result: Result) -> list[str]:
    return [w.code for w in result.warnings]


def test_a_fresh_enforced_repository(tmp_path: Path) -> None:
    w = init_world(tmp_path)
    term = Terminal()
    typed = Typed()
    result = valid(run_init(w, term, typed=typed, language="es"))
    assert result.ok, result.error
    data = result.data
    assert data["repo"] == "lab-x/estudio" and data["profile"] == "enforced" and data["pushed"] is True  # type: ignore[index]
    assert typed.seen[0][:2] == ("init", "lab-x/estudio")
    assert term.seen == [("phrase", f"bypass {LEAD}"), ("secret", term.seen[1][1])]  # sin suplente (REQ-PERM-19)
    files = {f["path"]: f["action"] for f in data["files"]}  # type: ignore[index]
    assert set(files.values()) == {"updated", "created", "deleted"}
    # Licencias (11 §7.3; D-64): las sugeridas para un repo público, con solo los textos en uso
    assert files["LICENSES/CC-BY-4.0.txt"] == files["LICENSES/MIT.txt"] == "created"
    assert files["LICENSES/MIT-0.txt"] == "deleted" and "LICENSES/CC0-1.0.txt" not in files  # bib/ usa CC0-1.0
    assert files["LICENSE"] == files["REUSE.toml"] == files["project.yaml"] == "updated"
    assert {lab["action"] for lab in data["labels"]} == {"created"} and len(data["labels"]) == 12  # type: ignore[index]
    prot = protections(result)
    assert prot["repo_settings"] == "applied" and prot["actions_permissions"] == "applied"
    assert prot[f"ruleset:{_meta.NAME}-base"] == "applied" and prot[f"ruleset:{_meta.NAME}-task-branches"] == "applied"
    assert prot[f"ruleset:{_meta.NAME}-workflows"] == "unsupported"  # repo público: sin push rulesets
    assert prot["pull_request_target"] == "skipped"
    assert "W_PERM_DEPUTY_MISSING" in codes(result) and "W_FORGE_CHECKS_SPOOFABLE" in codes(result)
    # Lo que quedó en la forja
    admin = w.forge.admin_state()
    assert admin["settings"]["allow_merge_commit"] is False
    assert admin["settings"]["squash_merge_commit_message"] == "PR_BODY"
    assert admin["actions"]["repo"]["sha_pinning_required"] is True
    base = next(r for r in admin["rulesets"].values() if r["name"] == f"{_meta.NAME}-base")
    assert base["bypass_actors"] == [{"actor_id": w.forge.identity().id, "actor_type": "User",
                                      "bypass_mode": "pull_request"}]
    review = next(r for r in base["rules"] if r["type"] == "pull_request")["parameters"]
    assert review["required_approving_review_count"] == 1 and review["allowed_merge_methods"] == ["squash"]
    checks = next(r for r in base["rules"] if r["type"] == "required_status_checks")["parameters"]
    assert [c["context"] for c in checks["required_status_checks"]] == [
        f"{_meta.NAME}/build", f"{_meta.NAME}/validate", f"{_meta.NAME}/editor-review"]
    # Lo que quedó en git: un commit en origin/main con AI-Use: none, y el árbol limpio
    message = w.repo.git("log", "-1", "--format=%B", "origin/main")
    assert message.startswith("Configura el proyecto con init") and message.rstrip().endswith("AI-Use: none")
    assert w.repo.git("status", "--porcelain").strip() == ""
    project = (w.repo.path / "project.yaml").read_text(encoding="utf-8")
    assert "language: es" in project and "  repo: lab-x/estudio" in project
    assert any("pull_request_target" in step for step in data["pending_steps"])  # type: ignore[union-attr]
    text = init_op.render_text(result, translator("es"))
    assert text.startswith("init en lab-x/estudio") and "noreply" in text and "pull_request_target" in text


def test_a_second_run_changes_nothing(tmp_path: Path) -> None:
    w = init_world(tmp_path)
    first = run_init(w, language="es")
    assert first.ok and first.data["setup_task"] is not None  # type: ignore[index]
    w.forge.calls.clear()
    again = valid(run_init(w))
    assert again.ok and again.data["files"] == [] and again.data["pushed"] is False  # type: ignore[index]
    assert again.data["setup_task"] == first.data["setup_task"]  # type: ignore[index]  # por su marcador
    assert set(protections(again).values()) <= {"unchanged", "unsupported", "skipped"}
    assert {lab["action"] for lab in again.data["labels"]} == {"unchanged"}  # type: ignore[index]
    assert w.forge.calls.writes() == []  # REQ-PERM-25: lee, compara y no escribe


def test_a_missing_mcp_json_is_created(tmp_path: Path) -> None:
    """01 §10.3: ``.mcp.json`` sale del generador del render; init lo crea si falta."""
    from aucove.render.mcp import MCP_JSON_PATH, mcp_json

    w = init_world(tmp_path)
    w.repo.commit("sin .mcp.json", remove=[MCP_JSON_PATH])
    w.repo.push("main")
    result = valid(run_init(w, language="es"))
    assert result.ok, result.error
    assert {"path": MCP_JSON_PATH, "action": "created"} in result.data["files"]  # type: ignore[operator]
    assert (w.repo.path / MCP_JSON_PATH).read_bytes() == mcp_json()


def test_advisory_needs_the_phrase(tmp_path: Path) -> None:
    w = init_world(tmp_path, profile="advisory")
    refused = run_init(w, Terminal(refuse={"I accept the advisory profile"}))
    assert refused.error is not None and refused.error.code == "E_FORGE_CAPABILITY_MISSING"
    assert w.forge.calls.writes() == [] and w.repo.git("status", "--porcelain").strip() == ""
    result = valid(run_init(w))
    assert result.ok and result.data["advisory_accepted"] is True  # type: ignore[index]
    cfg, _ = parse_project((w.repo.path / "project.yaml").read_bytes())
    assert cfg is not None and cfg.advisory_acceptance is not None and cfg.advisory_acceptance["by"] == LEAD
    assert set(v for k, v in protections(result).items() if k.startswith("ruleset:")) == {"unsupported"}
    assert protections(result)["repo_settings"] == "applied"  # REQ-PERM-42: también sin rulesets
    assert "W_FORGE_PROFILE_ADVISORY" in codes(result)
    assert any("memory/decisions" in step for step in result.data["pending_steps"])  # type: ignore[union-attr]


def test_individual_has_no_approvals_and_no_bypass(tmp_path: Path) -> None:
    w = init_world(tmp_path)
    term = Terminal()
    result = valid(run_init(w, term, individual=True))
    assert result.ok and ("phrase", f"bypass {LEAD}") not in term.seen
    base = next(r for r in w.forge.admin_state()["rulesets"].values() if r["name"] == f"{_meta.NAME}-base")
    review = next(r for r in base["rules"] if r["type"] == "pull_request")["parameters"]
    assert review["required_approving_review_count"] == 0 and not review["require_code_owner_review"]
    assert base["bypass_actors"] == [] and "W_PERM_DEPUTY_MISSING" not in codes(result)  # REQ-PERM-59
    assert "mode: individual" in (w.repo.path / "project.yaml").read_text(encoding="utf-8")


def test_a_declined_bypass_leaves_the_prs_blocked(tmp_path: Path) -> None:
    w = init_world(tmp_path)
    result = valid(run_init(w, Terminal(refuse={f"bypass {LEAD}"})))
    assert result.ok
    base = next(r for r in w.forge.admin_state()["rulesets"].values() if r["name"] == f"{_meta.NAME}-base")
    assert base["bypass_actors"] == []
    assert any("roles.yaml" in step for step in result.data["pending_steps"])  # type: ignore[union-attr]


def test_who_may_run_init(tmp_path: Path) -> None:
    w = init_world(tmp_path)
    w.forge.seed_user(LEAD, permission="write")
    not_admin = run_init(w)
    assert not_admin.error is not None and not_admin.error.code == "E_PERM_ROLE_REQUIRED"
    w.forge.seed_user(LEAD, permission="admin")
    w.forge.seed_admin_token("bruno-token", "bruno")
    other = run_init(w, Terminal(token="bruno-token"))
    assert other.error is not None and other.error.code == "E_AUTH_INSUFFICIENT_SCOPE"
    assert w.forge.calls.writes() == []


def test_a_roles_file_with_another_lead(tmp_path: Path) -> None:
    roles = load_tool("render_template").render_files("en", fmt="latex")["roles.yaml"].decode("utf-8")
    roles = roles.replace("example-lead", "carla").replace("example-researcher", "bruno")
    w = init_world(tmp_path, files={"roles.yaml": roles.encode("utf-8")})
    result = run_init(w)
    assert result.error is not None and result.error.code == "E_PERM_ROLE_REQUIRED"


def test_restricted_actions_policy(tmp_path: Path) -> None:
    w = init_world(tmp_path)
    w.forge.seed_admin(actions_repo={"enabled": True, "allowed_actions": "selected", "sha_pinning_required": True,
                                     "selected": {"github_owned_allowed": True, "verified_allowed": False,
                                                  "patterns_allowed": []}})
    result = valid(run_init(w))
    assert result.ok
    (warning,) = [x for x in result.warnings if x.code == "W_FORGE_ACTIONS_RESTRICTED"]
    assert warning.details is not None and warning.details["level"] == "repo"
    assert all(m.startswith("astral-sh/setup-uv@") for m in warning.details["missing"])  # type: ignore[union-attr]
    assert protections(result)["actions_permissions"] == "unchanged"  # ya fija el SHA: no amplía la política


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    w = init_world(tmp_path)
    term = Terminal()
    result = valid(run_init(w, term, dry_run=True))
    assert result.ok and result.data["dry_run"] is True and term.seen == []  # type: ignore[index]
    kinds = {e["kind"] for e in result.data["plan"]}  # type: ignore[index]
    assert {"file_write", "git_commit", "git_push", "forge_update"} <= kinds
    assert w.forge.calls.writes() == [] and w.repo.git("status", "--porcelain").strip() == ""


def test_regenerate_writes_only_generated_files(tmp_path: Path) -> None:
    w = init_world(tmp_path)
    assert run_init(w).ok
    Store(w.repo.path).write_identity(IdentityCache(LEAD, w.forge.identity().id, FAR), token_fp=None)
    roles = (w.repo.path / "roles.yaml").read_text(encoding="utf-8")
    (w.repo.path / "roles.yaml").write_text(roles.replace('"Introduction"', '"Opening"'), encoding="utf-8")
    w.forge.calls.clear()
    result = valid(run_init(w, regenerate=True))
    assert result.ok and w.forge.calls.entries() == []  # REQ-ARCH-87: sin la forja
    assert [f["path"] for f in result.data["files"]] == [".github/CODEOWNERS", ".github/ISSUE_TEMPLATE/task.yml"]  # type: ignore[index]
    assert codes(result) == ["W_STATE_INIT_FILES_PENDING"]
    assert "Opening" in (w.repo.path / ".github/ISSUE_TEMPLATE/task.yml").read_text(encoding="utf-8")
    mixed = run_init(w, regenerate=True, language="es")
    assert mixed.error is not None and mixed.error.code == "E_VALIDATION_INPUT"


def test_init_is_human_only(tmp_path: Path) -> None:
    w = init_world(tmp_path)
    mcp = w.run("init", login=LEAD)
    assert mcp.error is not None and mcp.error.code == "E_HUMAN_ONLY_NO_TTY"
    assert w.forge.calls.writes() == []


def _team_roles() -> bytes:
    roles = load_tool("render_template").render_files("en", fmt="latex")["roles.yaml"].decode("utf-8")
    roles = roles.replace("example-lead", LEAD).replace("example-researcher", "bruno")
    people_end = roles.index("sections:")
    carla = '  - login: carla\n    name: "Carla"\n    role: researcher\n    account_type: team\n    credit: []\n'
    return (roles[:people_end] + carla + roles[people_end:]).encode("utf-8")


def test_collaborators_and_the_call_budget(tmp_path: Path) -> None:
    """06 §4.4 (REQ-PERM-28, 29 y 55) y 08 §7.5: P personas además del responsable."""
    w = init_world(tmp_path, files={"roles.yaml": _team_roles()})
    w.forge.seed_admin(outside=["bruno"])
    result = valid(run_init(w))
    assert result.ok, result.error
    prot = protections(result)
    assert prot["collaborator:bruno"] == "applied" and prot["collaborator:carla"] == "applied"
    details = {p["id"]: p["detail"] for p in result.data["protections"]}  # type: ignore[index]
    assert details["collaborator:bruno"] == "added" and details["collaborator:carla"] == "invited"
    assert any("carla" in step for step in result.data["pending_steps"])  # type: ignore[union-attr]
    (broad,) = [x for x in result.warnings if x.code == "W_AUTH_BROAD_TOKEN"]
    assert broad.details == {"reason": "outside_collaborator", "logins": ["bruno"]}
    entries = w.forge.calls.entries()
    people = 2
    assert result.data["setup_task"] is not None  # type: ignore[index]  # plan.md sigue siendo el de la plantilla
    assert sum(c.kind == "L" for c in entries) <= 37 + people
    assert sum(c.kind in ("E", "C") for c in entries) <= 21 + people
    assert sum(c.kind == "C" for c in entries) <= 1  # la tarea de preparación (00-marco D-70)


# -- la tarea de preparación (00-marco D-70; 05 §9.3) ------------------------------------------------


def _setup_issue(w: World, number: int) -> Any:
    issue = w.forge.get_issue(number)
    assert issue is not None
    return issue


def test_init_creates_and_accepts_the_setup_task(tmp_path: Path) -> None:
    from aucove.domain.taskbody import parse_task_body

    w = init_world(tmp_path)
    typed = Typed()
    result = valid(run_init(w, typed=typed, language="es"))
    assert result.ok, result.error
    number = result.data["setup_task"]  # type: ignore[index]
    issue = _setup_issue(w, number)  # type: ignore[arg-type]
    assert issue.title == "Preparar el proyecto" and issue.author == LEAD
    assert issue.labels == ("task",)  # aceptada: sin state:proposed
    events = [(e.event, e.label, e.actor) for e in w.forge.list_issue_events(number)]  # type: ignore[arg-type]
    assert events[-1] == ("unlabeled", "state:proposed", LEAD)  # 04 REQ-TASK-8: la aceptó el lead
    spec = parse_task_body(issue.body, {})
    roles = (w.repo.path / "roles.yaml").read_text(encoding="utf-8")
    assert spec["sections"] and all(f"id: {s}" in roles for s in spec["sections"])  # todas las partes
    assert len(spec["done"]) == 4 and spec["plan_refs"] == []
    assert f"{_meta.NAME}:op v1 id=init-setup" in issue.body
    assert "Preparar el proyecto" in typed.seen[0][2]  # el resumen de la fase 2 la anuncia (D-09)
    assert f"#{number}" in result.data["pending_steps"][0]  # type: ignore[index]


def test_no_setup_task_when_the_repository_already_has_tasks(tmp_path: Path) -> None:
    w = init_world(tmp_path)
    w.forge.seed_issue(title="Una tarea anterior", body="### Resumen\n\nAlgo.\n", labels=["task"], author=LEAD)
    result = valid(run_init(w))
    assert result.ok and result.data["setup_task"] is None  # type: ignore[index]
    assert all(method != "create_issue" for method, _args in w.forge.calls.writes())


def test_no_setup_task_when_the_project_is_already_prepared(tmp_path: Path) -> None:
    plan = load_tool("render_template").render_files("en", fmt="latex")["plan.md"].decode("utf-8")
    plan = plan.replace("Describe in one paragraph", "We measure the critical exponents. Describe in one paragraph")
    w = init_world(tmp_path, files={"roles.yaml": _team_roles(), "plan.md": plan.encode("utf-8")})
    result = valid(run_init(w))
    assert result.ok and result.data["setup_task"] is None  # type: ignore[index]
    assert all(method != "create_issue" for method, _args in w.forge.calls.writes())


def test_individual_projects_only_need_their_plan(tmp_path: Path) -> None:
    w = init_world(tmp_path)
    result = valid(run_init(w, individual=True))
    assert result.ok and result.data["setup_task"] is not None  # type: ignore[index]  # el plan de la plantilla


def test_dry_run_announces_the_setup_task(tmp_path: Path) -> None:
    w = init_world(tmp_path)
    result = valid(run_init(w, dry_run=True))
    assert result.ok
    assert any(e["kind"] == "forge_create" and e["target"] == "issue" for e in result.data["plan"])  # type: ignore[index]
    assert w.forge.calls.writes() == []
