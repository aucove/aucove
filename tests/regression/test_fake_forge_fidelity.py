# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Fidelidad de ``FakeForge`` frente a GitHub (08-forja REQ-FORGE-59, REQ-FORGE-63).

Cada bloque reproduce un defecto confirmado del falso: con él, las pruebas de las
operaciones veían en el falso un comportamiento que GitHub no tiene (archivos del
PR congelados tras un push, un PR abierto sin rama cabeza, refs en conflicto de
directorio, la rama base sin protección, un límite de una cuenta que bloquea a
todas, etc.). El último bloque comprueba que las pruebas de contrato compartidas
no dependen de los logins del falso (REQ-MVP-26) y que la caché de identidad se
verifica de verdad.
"""

from __future__ import annotations

import importlib
from datetime import timedelta

import pytest

from aucove.forge.base import (
    ChangedFile,
    ForgePermissionError,
    ForgeUnavailable,
    ForgeValidationError,
    Identity,
    LabelEvent,
    NetworkTimeout,
    RateLimited,
    Ref,
    RefConflict,
    RepoRef,
)
from aucove.forge.fake import CI_LOGIN, FakeClock, FakeForge, Fault
from tests.contract.conftest import (
    BASE_FILES,
    LEAD,
    READ_ONLY,
    REPO,
    RESEARCHER_A,
    RESEARCHER_B,
    START,
    Accounts,
    fake_sandbox_of,
    new_fake,
)


def _main(fake: FakeForge) -> str:
    ref = fake.get_ref(fake.default_branch)
    assert ref is not None
    return ref.sha


# --- 1. Los archivos del PR siguen a su cabeza (GET pulls/{n}/files es el diff de tres puntos) ---


def test_pr_files_follow_a_push_to_its_head_branch() -> None:
    fake = new_fake()
    base = _main(fake)
    m_tex = "sections/methods/m.tex"
    h1 = fake.seed_commit({**BASE_FILES, m_tex: b"m\n"}, parents=[base], message="h1")
    fake.seed_branch("task/3", h1)
    pr = fake.seed_pull_request(files=[ChangedFile(m_tex, "added")], head_branch="task/3")
    h2 = fake.seed_commit({**BASE_FILES, m_tex: b"m\n", "plan.md": b"x\n"}, parents=[h1], message="h2")
    fake.update_ref("task/3", h2)

    moved = fake.get_pull_request(pr.number)
    assert moved is not None and moved.head_sha == h2
    files = fake.list_pull_request_files(pr.number)
    assert files == [ChangedFile("plan.md", "added"), ChangedFile(m_tex, "added")]
    assert files == list(fake.compare("main", "task/3").files)

    # Otro push que quita m.tex: la lista vuelve a seguir a la cabeza, también tras guardar el estado.
    h3 = fake.seed_commit({**BASE_FILES, "plan.md": b"x\n"}, parents=[h2], message="h3")
    fake.update_ref("task/3", h3)
    restored = FakeForge.from_state(fake.to_state())
    assert restored.list_pull_request_files(pr.number) == [ChangedFile("plan.md", "added")]


def test_seed_pull_request_without_files_takes_them_from_the_graph() -> None:
    fake = new_fake()
    base = _main(fake)
    head = fake.seed_commit({**BASE_FILES, "tex/main.tex": b"v2\n"}, parents=[base])
    fake.seed_branch("task/4", head)
    pr = fake.seed_pull_request(head_branch="task/4")
    assert fake.list_pull_request_files(pr.number) == [ChangedFile("tex/main.tex", "modified")]


# --- 2. Borrar la rama cabeza cierra el PR abierto -------------------------------------------


def test_deleting_the_head_branch_closes_the_open_pull_request() -> None:
    fake = new_fake()
    c = fake.create_empty_commit(_main(fake), "w")
    fake.create_ref("task/5", c)
    pr = fake.seed_pull_request(files=[], head_branch="task/5")
    other = fake.seed_pull_request(files=[], head_branch="task/6", head_sha=c)
    fake.clock.advance(60)

    assert fake.delete_ref("task/5") is True
    closed = fake.get_pull_request(pr.number)
    assert closed is not None and closed.state == "closed" and closed.merged is False
    assert closed.updated_at == fake.clock.now()
    assert fake.find_pull_request("task/5", state="open") is None
    saved = next(p for p in fake.to_state()["pulls"] if p["number"] == pr.number)
    assert saved["closed_at"] == saved["updated_at"] and saved["merged_at"] is None
    still_open = fake.get_pull_request(other.number)
    assert still_open is not None and still_open.state == "open"


# --- 3. Conflicto de directorio y archivo entre refs --------------------------------------------


def test_create_ref_rejects_directory_file_conflicts() -> None:
    fake = new_fake()
    c1 = fake.create_empty_commit(_main(fake), "c1")
    fake.create_ref("task", c1)
    with pytest.raises(ForgeValidationError) as info:
        fake.create_ref("task/12", c1)
    assert info.value.http_status == 422
    assert fake.get_ref("task/12") is None

    fake.create_ref("work/12", c1)
    with pytest.raises(ForgeValidationError):
        fake.create_ref("work/12/x", c1)
    with pytest.raises(ForgeValidationError):
        fake.create_ref("work", c1)
    # Otro espacio de nombres no choca: refs/tags/task/12 convive con refs/heads/task.
    assert fake.create_ref("task/12", c1, namespace="tags") == Ref("task/12", c1)
    assert fake.list_refs("task") == [Ref("task", c1)]


# --- 4. Rama por defecto y ruleset de la base ---------------------------------------------------


@pytest.mark.parametrize("profile", ["enforced", "advisory"])
def test_default_branch_cannot_be_deleted(profile: str) -> None:
    fake = new_fake(profile)
    base = _main(fake)
    with pytest.raises(ForgeValidationError) as info:
        fake.delete_ref("main")
    assert info.value.http_status == 422
    assert fake.get_ref("main") == Ref("main", base)


def test_enforced_base_rejects_direct_updates() -> None:
    fake = new_fake("enforced")
    base = _main(fake)
    bruno = fake.as_user(RESEARCHER_A)
    child = bruno.create_empty_commit(base, "direct to main")
    with pytest.raises(RefConflict) as info:
        bruno.update_ref("main", child)
    assert info.value.current == base
    with pytest.raises(RefConflict):
        fake.update_ref("main", child)  # el ruleset no tiene bypass: tampoco el admin
    assert _main(fake) == base
    # Las demás ramas siguen admitiendo el avance rápido.
    fake.create_ref("task/1", base)
    assert bruno.update_ref("task/1", child) == Ref("task/1", child)


def test_advisory_base_accepts_fast_forward() -> None:
    fake = new_fake("advisory")
    child = fake.create_empty_commit(_main(fake), "direct")
    assert fake.as_user(RESEARCHER_A).update_ref("main", child) == Ref("main", child)


# --- 6. Fallas después del efecto y fallas combinadas -----------------------------------------


def test_http_5xx_after_the_effect_is_confirmed_by_create_ref() -> None:
    fake = new_fake()
    c = fake.create_empty_commit(_main(fake), "claim")
    fake.calls.clear()
    fake.faults.add(Fault("create_ref", "http_5xx_after", nth=1))
    assert fake.create_ref("task/2", c) == Ref("task/2", c)  # 502 con la ref creada: éxito tras el GET
    assert fake.calls.count("L", "create_ref") == 1
    assert [c.outcome for c in fake.calls.entries() if c.http == "POST"] == ["http_5xx_after"]


def test_http_5xx_after_the_effect_on_add_labels_keeps_the_effect() -> None:
    fake = new_fake()
    n = fake.seed_issue(title="t").number
    fake.faults.add(Fault("add_labels", "http_5xx_after", nth=1))
    with pytest.raises(ForgeUnavailable):
        fake.add_labels(n, ["task"])
    issue = fake.get_issue(n)
    assert issue is not None and issue.labels == ("task",)


def test_two_faults_on_the_same_call_are_not_dropped_silently() -> None:
    fake = new_fake()
    c = fake.create_empty_commit(_main(fake), "claim")
    fake.faults.add(Fault("create_ref", "http_422", nth=1))
    fake.faults.add(Fault("create_ref", "http_5xx", nth=1))
    with pytest.raises(ValueError, match="same call"):
        fake.create_ref("task/2", c)
    assert fake.get_ref("task/2") is None


def test_race_and_a_raising_fault_both_apply() -> None:
    fake = new_fake()
    c = fake.create_empty_commit(_main(fake), "claim")
    seen: list[object] = []
    fake.faults.add(Fault("create_ref", "race", nth=1, race=seen.append))
    fake.faults.add(Fault("create_ref", "http_5xx", nth=1))
    with pytest.raises(ForgeUnavailable):
        fake.create_ref("task/2", c)
    assert len(seen) == 1
    assert fake.get_ref("task/2") is None


# --- 8. El límite de una cuenta no bloquea a las demás -------------------------------------------


def test_rate_limit_blocks_only_the_affected_account() -> None:
    fake = new_fake()
    fake.faults.add(Fault("get_ref", "rate_limit_secondary", nth=1))
    with pytest.raises(RateLimited):
        fake.get_ref("main")
    assert fake.as_user(RESEARCHER_A).get_ref("main") is not None
    with pytest.raises(RateLimited):
        fake.get_issue(1)
    restored = FakeForge.from_state(fake.to_state())
    with pytest.raises(RateLimited):
        restored.get_ref("main")
    assert restored.as_user(RESEARCHER_A).get_ref("main") is not None
    fake.clock.advance(timedelta(seconds=60))
    assert fake.get_ref("main") is not None


# --- 9. Constructor de 08 §10.1 ---------------------------------------------------------------


def test_constructor_of_the_spec_example() -> None:
    fake = FakeForge(
        repo=RepoRef("lab", "articulo"), viewer="ana", profile="enforced",  # type: ignore[call-arg]
        write_mode="git", graphql=True, clock=FakeClock("2026-09-29T12:00:00Z"),
    )
    assert fake.capabilities().write_mode == "git" and fake.graphql is True
    web = FakeForge(REPO, "ana", write_mode="api", graphql=False, transport="gh")  # type: ignore[call-arg]
    caps = web.capabilities()
    assert caps.write_mode == "api" and caps.transport == "gh" and web.graphql is False
    restored = FakeForge.from_state(web.to_state())
    assert restored.graphql is False and restored.capabilities().write_mode == "api"
    with pytest.raises(ValueError):
        FakeForge(REPO, "ana", write_mode="svn")  # type: ignore[call-arg]


# --- 10. Alcance de los tokens classic y oauth (08 §5.2) ----------------------------------------


def test_identity_scope_of_classic_oauth_and_app_user_tokens() -> None:
    fake = new_fake()
    fake.seed_user("eva", token_kind="classic_pat")
    eva = fake.as_user("eva").identity()
    assert eva.token_scope == "broad" and eva.oauth_scopes == ("repo",)
    fake.seed_user("gus", token_kind="oauth")
    gus = fake.as_user("gus").identity()
    assert gus.token_scope == "broad" and gus.oauth_scopes is not None and "repo" in gus.oauth_scopes
    fake.seed_user("hal", token_kind="classic_pat", scopes=["repo", "workflow"])  # type: ignore[call-arg]
    assert fake.as_user("hal").identity().oauth_scopes == ("repo", "workflow")
    fake.seed_user("ivo", token_kind="app_user")
    ivo = fake.as_user("ivo").identity()
    assert ivo.token_scope == "repo_restricted" and ivo.oauth_scopes is None
    ana = fake.identity()
    assert ana.token_scope == "repo_restricted" and ana.oauth_scopes is None
    restored = FakeForge.from_state(fake.to_state())
    assert restored.as_user("hal").identity().oauth_scopes == ("repo", "workflow")


# --- 11. Revisiones PENDING ajenas --------------------------------------------------------------


def test_list_reviews_hides_pending_reviews_of_other_accounts() -> None:
    fake = new_fake()
    c = fake.create_empty_commit(_main(fake), "w")
    fake.create_ref("task/1", c)
    pr = fake.seed_pull_request(files=[], head_branch="task/1", author=RESEARCHER_A)
    fake.seed_review(pr.number, author=RESEARCHER_B, state="APPROVED")
    fake.seed_review(pr.number, author=RESEARCHER_A, state="PENDING")
    fake.seed_review(pr.number, author=LEAD, state="PENDING")

    def seen(login: str) -> list[tuple[str, str]]:
        return [(r.author, r.state) for r in fake.as_user(login).list_reviews(pr.number)]

    assert seen(LEAD) == [(RESEARCHER_B, "APPROVED"), (LEAD, "PENDING")]
    assert seen(RESEARCHER_A) == [(RESEARCHER_B, "APPROVED"), (RESEARCHER_A, "PENDING")]
    assert seen(RESEARCHER_B) == [(RESEARCHER_B, "APPROVED")]


# --- 12. La paginación cuenta lo que GitHub devuelve, no lo que queda tras filtrar -------------


def _seed_pulls(fake: FakeForge, count: int, **fields: object) -> None:
    base = _main(fake)
    for i in range(count):
        fake.seed_pull_request(files=[], head_branch=f"pr/{i}", head_sha=base, **fields)


def test_list_issues_pages_include_pull_requests() -> None:
    fake = new_fake()
    for i in range(250):
        fake.seed_issue(title=f"i{i}")
    _seed_pulls(fake, 60)
    fake.calls.clear()
    assert len(fake.list_issues()) == 250
    assert fake.calls.count("L", "list_issues") == 4  # 310 elementos: 4 páginas de 100


def test_list_issues_cap_counts_pull_requests() -> None:
    fake = new_fake()
    for i in range(950):
        fake.seed_issue(title=f"i{i}")
    _seed_pulls(fake, 100)
    with pytest.raises(ForgeValidationError):
        fake.list_issues()  # 1050 elementos: GitHub necesita 11 páginas (REQ-FORGE-8)
    # Con el filtro de etiqueta, los PR sin la etiqueta no cuentan.
    assert fake.list_issues(labels=["absent"]) == []


def test_list_issue_events_pages_include_other_event_types() -> None:
    fake = new_fake()
    when = fake.clock.now()
    events = [LabelEvent(10_000 + i, "labeled", f"l{i}", LEAD, when) for i in range(99)]
    n = fake.seed_issue(title="t", events=events).number
    fake.update_issue(n, state="closed", state_reason="not_planned")  # evento closed
    fake.update_issue(n, state="open")  # evento reopened
    fake.calls.clear()
    assert len(fake.list_issue_events(n)) == 99
    assert fake.calls.count("L", "list_issue_events") == 2  # 101 eventos en la API


# --- 13. Los endpoints de Issues aceptan números de PR -------------------------------------------


def test_assignees_and_update_issue_accept_pull_request_numbers() -> None:
    fake = new_fake()
    c = fake.create_empty_commit(_main(fake), "w")
    fake.create_ref("task/1", c)
    pr = fake.seed_pull_request(files=[], head_branch="task/1")

    assigned = fake.set_assignees(pr.number, [RESEARCHER_A])
    assert assigned.number == pr.number and assigned.assignees == (RESEARCHER_A,) and "/pull/" in assigned.url
    assert fake.add_assignees(pr.number, [RESEARCHER_B]).assignees == (RESEARCHER_A, RESEARCHER_B)
    assert fake.remove_assignees(pr.number, [RESEARCHER_A]).assignees == (RESEARCHER_B,)
    renamed = fake.update_issue(pr.number, title="nuevo título")
    assert renamed.title == "nuevo título"
    closed = fake.update_issue(pr.number, state="closed")
    assert closed.state == "closed"
    got = fake.get_pull_request(pr.number)
    assert got is not None and got.title == "nuevo título" and got.state == "closed" and got.merged is False
    with pytest.raises(ForgeValidationError):
        fake.get_issue(pr.number)  # REQ-FORGE-9 sigue valiendo para get_issue


# --- 14. create_issue se recupera por el marcador op (08 §4.7) -----------------------------------


def test_create_issue_recovers_by_op_marker_after_timeout_after() -> None:
    fake = new_fake()
    fake.faults.add(Fault("create_issue", "timeout_after", nth=1))
    created = fake.create_issue("t", "b", labels=["task"], op_id="op-7")
    assert [i.number for i in fake.list_issues() if "id=op-7 " in i.body] == [created.number]
    assert created.labels == ("task",)
    assert fake.calls.count("C", "create_issue") == 1
    assert fake.calls.count("L", "create_issue") == 1  # GET issues?creator=…&since=…


@pytest.mark.parametrize(
    ("kind", "exc"), [("http_5xx", ForgeUnavailable), ("timeout_before", NetworkTimeout)]
)
def test_create_issue_without_effect_reraises_the_retryable_cause(kind: str, exc: type[Exception]) -> None:
    fake = new_fake()
    fake.faults.add(Fault("create_issue", kind, nth=1))  # type: ignore[arg-type]
    with pytest.raises(exc) as info:
        fake.create_issue("t", "b", op_id="op-8")
    assert info.value.retryable is True  # type: ignore[attr-defined]
    assert fake.list_issues() == []


def test_create_issue_recovery_ignores_issues_of_other_accounts() -> None:
    fake = new_fake()

    def rival(state: object) -> None:
        fake.as_user(RESEARCHER_A).create_issue("t", "b", op_id="op-9")

    fake.faults.add(Fault("create_issue", "race", nth=1, race=rival))
    fake.faults.add(Fault("create_issue", "timeout_before", nth=1))
    with pytest.raises(NetworkTimeout):
        fake.create_issue("t", "b", op_id="op-9")
    assert [i.author for i in fake.list_issues()] == [RESEARCHER_A]


# --- 15. Las cuentas implícitas no reciben write ------------------------------------------------


def test_implicit_accounts_get_read_access_only() -> None:
    fake = new_fake()
    base = _main(fake)
    mallory = fake.as_user("mallory")
    assert mallory.capabilities().viewer_permission == "read"
    with pytest.raises(ForgePermissionError):
        mallory.create_ref("task/9", base)
    assert fake.get_ref("task/9") is None

    issue = fake.seed_issue(title="t", author="eva")
    assert fake.as_user("eva").capabilities().viewer_permission == "read"
    with pytest.raises(ForgeValidationError):
        fake.set_assignees(issue.number, ["zed"])  # cuenta conocida, sin acceso ni participación
    fake.as_user("zed").capabilities()
    with pytest.raises(ForgeValidationError):
        fake.set_assignees(issue.number, ["zed"])
    fake.seed_comment(issue.number, author="zed", body="hola")
    assert fake.set_assignees(issue.number, ["zed"]).assignees == ("zed",)  # quien comentó es asignable

    # La cuenta del constructor, la del CI y las sembradas con seed_user conservan su permiso.
    assert FakeForge(REPO, "zoe").capabilities().viewer_permission == "write"
    assert fake.as_user(CI_LOGIN).capabilities().viewer_permission == "write"
    assert fake.as_user(READ_ONLY).capabilities().viewer_permission == "read"
    restored = FakeForge.from_state(fake.to_state())
    assert restored.as_user("mallory").capabilities().viewer_permission == "read"


# --- 7 y 16. Las pruebas de contrato compartidas usan los logins del sandbox -------------------

OTHER_ACCOUNTS = Accounts(lead="Lead-X", researcher_a="res-a", researcher_b="res-b", read_only="reader-z")


def _other_sandbox() -> object:
    return fake_sandbox_of(new_fake(accounts=OTHER_ACCOUNTS), accounts=OTHER_ACCOUNTS)


@pytest.mark.parametrize(
    ("module", "name"),
    [
        ("test_auth", "test_identity_of_the_viewer"),
        ("test_activity", "test_ct_act_01_rest_writes_register_creation_and_push"),
        ("test_git_api", "test_create_empty_commit_keeps_the_parent_tree"),
        ("test_pulls", "test_get_and_list_pull_requests"),
        ("test_pulls", "test_list_reviews_in_chronological_order"),
        ("test_pulls", "test_pull_request_files_follow_pushes"),
    ],
)
def test_shared_contract_tests_do_not_depend_on_fake_logins(module: str, name: str) -> None:
    test = getattr(importlib.import_module(f"tests.contract.{module}"), name)
    test(_other_sandbox())


@pytest.mark.parametrize(
    "name",
    [
        "test_ct_iss_01_add_assignees_twice_lists_both",
        "test_labels_are_idempotent",
        "test_assign_and_unassign",
        "test_marker_comment_is_updated_not_duplicated",
    ],
)
def test_shared_issue_tests_do_not_depend_on_fake_logins(name: str) -> None:
    module = importlib.import_module("tests.contract.test_issues")
    sandbox = _other_sandbox()
    issue = sandbox.forge.create_issue("ct issue", "body", op_id=f"reg-{name}")  # type: ignore[attr-defined]
    getattr(module, name)(sandbox, issue)


class _NoIdentityCache(FakeForge):
    def identity(self, *, refresh: bool = False) -> Identity:
        return super().identity(refresh=True)


def test_identity_cache_check_is_not_tautological() -> None:
    from tests.contract.test_auth import test_identity_of_the_viewer

    fake = _NoIdentityCache(REPO, LEAD, clock=FakeClock(START))
    fake.seed_user(LEAD, permission="admin")
    fake.seed_branch("main", fake.seed_commit(BASE_FILES, message="base"))
    with pytest.raises(AssertionError):
        test_identity_of_the_viewer(fake_sandbox_of(fake))


# --- 17. El esqueleto de T2 nombra los casos de 08 §10.3 que faltaban ----------------------------


@pytest.mark.parametrize(
    ("module", "name"),
    [
        ("test_auth", "test_ct_cap_01_enforced_repos"),
        ("test_auth", "test_ct_cap_02_private_free_repo_is_advisory"),
        ("test_auth", "test_ct_auth_01_permission_hints_match_the_table"),
        ("test_auth", "test_ct_etag_01_conditional_get_is_free"),
        ("test_git_api", "test_ct_git_03_workflow_files_without_workflow_permission"),
    ],
)
def test_missing_catalog_cases_have_a_contract_test(module: str, name: str) -> None:
    assert callable(getattr(importlib.import_module(f"tests.contract.{module}"), name, None))
