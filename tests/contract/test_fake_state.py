# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""FakeForge: Protocol, vistas, estado JSON y selección por entorno (08-forja §10.1, REQ-FORGE-62)."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from aucove import _meta
from aucove.forge.base import ChangedFile, ForgeAdapter, Label, NetworkTimeout, RefActivity, RepoRef
from aucove.forge.fake import ADAPTER_METHODS, STATE_VERSION, Fault, FakeClock, FakeForge
from aucove.ops.context import Invocation, OpContext
from aucove.ops.result import OpError
from tests.contract.conftest import LEAD, RESEARCHER_A, RESEARCHER_B, new_fake

P = _meta.ENV_PREFIX


def _protocol_members() -> set[str]:
    names = {n for n in vars(ForgeAdapter) if not n.startswith("_") and callable(getattr(ForgeAdapter, n))}
    return names


def test_fake_forge_is_a_forge_adapter() -> None:
    fake = FakeForge(RepoRef("lab", "articulo"), "ana")
    assert isinstance(fake, ForgeAdapter)
    assert isinstance(fake.as_user("bruno"), ForgeAdapter)
    assert fake.repo == RepoRef("lab", "articulo")


def test_adapter_methods_match_the_protocol() -> None:
    assert ADAPTER_METHODS == _protocol_members()


def test_constructor_validation() -> None:
    with pytest.raises(TypeError):
        FakeForge(("lab", "articulo"), "ana")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        FakeForge(RepoRef("lab", "a"), "ana", profile="open")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        FakeForge(RepoRef("lab", "a"), "")
    with pytest.raises(ValueError):
        FakeClock("2026-09-29T12:00:00")  # sin zona


def test_as_user_shares_state_calls_and_faults() -> None:
    fake = new_fake()
    bruno = fake.as_user(RESEARCHER_A)
    assert bruno.viewer == RESEARCHER_A and bruno.calls is fake.calls and bruno.faults is fake.faults
    issue = bruno.create_issue("from bruno", "b", op_id="op-b")
    assert fake.get_issue(issue.number) == issue
    assert [c.actor for c in fake.calls.entries()] == [RESEARCHER_A, LEAD]
    assert fake.as_user("BRUNO").viewer == RESEARCHER_A  # logins sin distinguir mayúsculas


def _rich_fake() -> FakeForge:
    fake = new_fake()
    base = fake.get_ref("main")
    assert base is not None
    c1 = fake.create_empty_commit(base.sha, "claim #1")
    fake.create_ref("task/1", c1)
    fake.clock.advance(30)
    c2 = fake.as_user(RESEARCHER_A).create_empty_commit(c1, "work")
    fake.as_user(RESEARCHER_A).update_ref("task/1", c2)
    fake.create_ref("archive/task-1-1", c1, namespace="tags")
    fake.seed_ref_activity("task/2", [RefActivity("force_push", "mallory", fake.clock.now(), c1, c2)])
    issue = fake.seed_issue(title="Task 1", body="cuerpo ñ", labels=["task"])
    fake.add_labels(issue.number, ["claimed"])
    fake.set_assignees(issue.number, [RESEARCHER_A])
    fake.create_comment(issue.number, "claim", op_id="claim-1")
    fake.seed_issue(title="closed", state="closed", state_reason="not_planned")
    pr = fake.seed_pull_request(
        files=[ChangedFile("a.tex", "modified"), ChangedFile("b.tex", "renamed", "old.tex")],
        head_branch="task/1",
        body=f"Closes #{issue.number}",
        labels=["needs-review"],
    )
    fake.seed_review(pr.number, author=RESEARCHER_B, state="APPROVED")
    fake.seed_review(pr.number, author=LEAD, state="PENDING")
    fake.seed_comment(pr.number, author=RESEARCHER_B, body="looks good")
    fake.set_check(c2, name="build-pdf", status="completed", conclusion="success")
    fake.seed_workflow_run("validate.yml", branch="task/1", sha=c2, conclusion="failure")
    fake.set_codeowners_errors("main", ["Unknown owner: @ghost"])
    fake.ensure_labels([Label("task", "0e8a16", "Tarea")])
    admin = fake.admin("admin-token")  # la configuración de ForgeAdmin también se guarda (08 §2.2)
    admin.update_repo_settings({"has_wiki": False, "allow_merge_commit": False})
    admin.upsert_ruleset("aucove-base", {"target": "branch", "enforcement": "active",
                                           "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
                                           "rules": [{"type": "deletion"}]})
    admin.add_collaborator("eva", "push")
    fake.seed_admin(outside=[RESEARCHER_B], retention=None, org=None, plan_visible=False)
    fake.seed_user(RESEARCHER_A, token_scope="owner_wide")  # lo que mide doctor (08 §5.2)
    fake.seed_admin_token("revoked", None)
    fake.seed_commit({"bin/data.bin": bytes(range(256))}, parents=[base.sha])
    fake.faults.add(Fault("get_issue", "timeout_before", nth=3))
    fake.faults.add(Fault("set_assignees", "race", race=lambda s: None))  # no se guarda
    return fake


def test_state_round_trip_is_idempotent() -> None:
    """from_state(to_state()) reproduce el mismo estado, también tras pasar por JSON."""
    fake = _rich_fake()
    first = fake.to_state()
    assert first["fake_state"] == STATE_VERSION
    assert json.loads(json.dumps(first)) == first  # solo tipos JSON
    again = FakeForge.from_state(first).to_state()
    assert again == first
    assert FakeForge.from_state(json.loads(json.dumps(again))).to_state() == first
    # Claves del estado JSON.
    for key in (
        "repo", "viewer", "profile", "default_branch", "clock", "users", "commits", "refs", "ref_activity",
        "issues", "pulls", "checks", "codeowners_errors", "workflow_runs", "labels", "next_id",
    ):
        assert key in first
    assert set(first["refs"]) == {"heads/main", "heads/task/1", "tags/archive/task-1-1"}
    assert [f["kind"] for f in first["faults"]] == ["timeout_before"]  # race no es serializable
    admin = first["admin"]
    assert admin["settings"]["has_wiki"] is False and admin["retention"] is None and admin["org"] is None
    assert [r["name"] for r in admin["rulesets"].values()] == ["aucove-base"]
    assert admin["invitations"] == {"eva": "write"} and admin["outside"] == [RESEARCHER_B.casefold()]
    assert admin["plan_visible"] is False and first["users"][RESEARCHER_A]["token_scope"] == "owner_wide"
    restored = FakeForge.from_state(first)
    assert [r["type"] for r in restored.admin("admin-token").branch_rules("main")] == ["deletion"]


def test_states_without_admin_take_the_defaults() -> None:
    """Un estado anterior a 6b (sin ``admin``) toma la configuración por omisión."""
    state = new_fake().to_state()
    del state["admin"]
    restored = FakeForge.from_state(state)
    assert restored.admin_state()["settings"]["allow_merge_commit"] is True


def test_restored_fake_behaves_the_same() -> None:
    fake = _rich_fake()
    fake.get_issue(1)  # primera llamada que cuenta la falla nth=3
    restored = FakeForge.from_state(fake.to_state())
    pr = fake.find_pull_request("task/1")
    assert pr is not None
    for forge in (fake, restored):
        assert forge.get_ref("task/1") == fake.get_ref("task/1")
        assert forge.list_ref_activity("task/1") == fake.list_ref_activity("task/1")
        assert forge.find_pull_request("task/1") == pr
        assert forge.list_reviews(pr.number) == fake.list_reviews(pr.number)
        assert forge.list_pull_request_files(pr.number) == fake.list_pull_request_files(pr.number)
        assert forge.list_comments(1) == fake.list_comments(1)
        assert forge.list_issue_events(1) == fake.list_issue_events(1)
    # La falla guardada sigue contando llamadas donde quedó: la tercera falla.
    restored.get_issue(1)
    with pytest.raises(NetworkTimeout):
        restored.get_issue(1)
    # Los contadores no repiten ids.
    new_issue = restored.create_issue("new", "b", op_id="n-1")
    assert new_issue.number not in {i["number"] for i in fake.to_state()["issues"]}


def test_same_script_same_state() -> None:
    """Determinista: el mismo guion da el mismo estado."""
    assert _rich_fake().to_state() == _rich_fake().to_state()


def test_save_and_load_are_atomic(tmp_path: Path) -> None:
    fake = _rich_fake()
    path = tmp_path / "state" / "forge.json"
    fake.save(path)
    raw = path.read_bytes()
    assert raw.endswith(b"\n") and not raw.startswith(b"\xef\xbb\xbf") and b"\r\n" not in raw
    assert FakeForge.load(path).to_state() == fake.to_state()
    fake.create_issue("after save", "b", op_id="later")
    fake.save(path)
    assert [p.name for p in path.parent.iterdir()] == ["forge.json"]  # sin temporales
    assert any(i["title"] == "after save" for i in json.loads(path.read_text("utf-8"))["issues"])


def test_minimal_hand_written_state(tmp_path: Path) -> None:
    path = tmp_path / "forge.json"
    sha = "a" * 40
    path.write_text(
        json.dumps({"fake_state": 1, "repo": {"owner": "lab", "name": "x"}, "viewer": "ana", "refs": {"heads/main": sha}}),
        encoding="utf-8",
    )
    fake = FakeForge.load(path)
    assert fake.viewer == "ana" and fake.default_branch == "main" and fake.profile == "enforced"
    ref = fake.get_ref("main")
    assert ref is not None and ref.sha == sha
    with pytest.raises(ValueError):
        FakeForge.from_state({"fake_state": 99, "repo": {"owner": "lab", "name": "x"}, "viewer": "ana"})


def test_clock_is_saved_and_advances_only_on_request() -> None:
    fake = new_fake()
    start = fake.clock.now()
    fake.create_issue("t", "b", op_id="t-1")
    assert fake.clock.now() == start
    fake.clock.advance(timedelta(minutes=5))
    restored = FakeForge.from_state(fake.to_state())
    assert restored.clock.now() == start + timedelta(minutes=5)
    with pytest.raises(ValueError):
        fake.clock.advance(-1)


# --- REQ-FORGE-62: selección del falso por entorno ------------------------------------


def _ctx(tmp_path: Path, env: dict[str, str]) -> OpContext:
    inv = Invocation(root=None, lang="en", json=True, dry_run=False, surface="cli", env=env, cwd=str(tmp_path))
    return OpContext(inv)


def test_fake_adapter_variable_is_ignored_without_testing(tmp_path: Path) -> None:
    """Sin <PREFIJO>TESTING=1, <PREFIJO>FORGE_ADAPTER=fake se ignora: no se lee ni se escribe el estado."""
    state = tmp_path / "forge.json"
    state.write_text("not json: reading it would fail", encoding="utf-8")
    for testing in (None, "0", "true", "yes"):
        env = {P + "FORGE_ADAPTER": "fake", P + "FORGE_FAKE_STATE": str(state)}
        if testing is not None:
            env[P + "TESTING"] = testing
        ctx = _ctx(tmp_path, env)
        with pytest.raises(OpError) as info:
            ctx.forge()
        assert info.value.code == "E_CONFIG_NOT_A_PROJECT"  # siguió la ruta de forge.type, no la del falso
        ctx.close()
    assert state.read_text(encoding="utf-8") == "not json: reading it would fail"


def test_fake_adapter_is_loaded_and_saved_with_testing(tmp_path: Path) -> None:
    state = tmp_path / "forge.json"
    new_fake().save(state)
    env = {P + "TESTING": "1", P + "FORGE_ADAPTER": "fake", P + "FORGE_FAKE_STATE": str(state)}
    ctx = _ctx(tmp_path, env)
    forge = ctx.forge()
    assert isinstance(forge, FakeForge) and isinstance(forge, ForgeAdapter)
    forge.create_issue("from the cli", "body", op_id="e2e-1")
    ctx.close()
    ctx.close()  # idempotente
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert [i["title"] for i in saved["issues"]] == ["from the cli"]
    assert [c["method"] for c in saved["calls"]] == ["create_issue"]  # las escrituras quedan para las aserciones
