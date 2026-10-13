# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Autenticación, identidad y capacidades (08-forja §3, §4.2, §5, §6, §7.3, §7.4, §10.3).

CT-AUTH-01, CT-AUTH-02, CT-CAP-01, CT-CAP-02 y CT-ETAG-01. CT-ETAG-01 mide la capa
HTTP del adaptador GitHub, que el falso no modela: corre solo en ``live`` (H3).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from aucove import _meta
from aucove.domain.text import same_login
from aucove.forge.base import (
    ForgeAdapter,
    ForgeAuthError,
    ForgeNotFound,
    ForgePermissionError,
    Label,
    token_sources,
)
from aucove.forge.fake import CI_LOGIN, Fault, FakeForge
from tests.contract.conftest import LEAD, OPEN_WITHOUT_PERMISSION, SANDBOX_REPOS, Sandbox


def test_ct_auth_02_invalid_token_is_auth_error(sandbox: Sandbox) -> None:
    """CT-AUTH-02: un token inválido da ForgeAuthError (401)."""
    forge = sandbox.invalid_token()
    with pytest.raises(ForgeAuthError) as info:
        forge.identity(refresh=True)
    assert info.value.http_status == 401
    assert info.value.kind == "auth" and info.value.retryable is False
    with pytest.raises(ForgeAuthError):
        forge.get_ref(sandbox.base_branch)


def test_ct_auth_02_missing_permission_is_permission_error_with_hint(sandbox: Sandbox) -> None:
    """CT-AUTH-02: un token sin permiso de escritura da ForgePermissionError con hint."""
    reader = sandbox.read_only()
    with pytest.raises(ForgePermissionError) as info:
        reader.create_ref(sandbox.branch("auth-02"), sandbox.base_sha)
    exc = info.value
    assert exc.http_status == 403
    assert exc.kind == "permission" and exc.retryable is False
    assert exc.hint and "X-Accepted-GitHub-Permissions" in exc.hint and "contents=write" in exc.hint
    assert sandbox.forge.get_ref(sandbox.branch("auth-02")) is None


def test_identity_of_the_viewer(sandbox: Sandbox) -> None:
    ident = sandbox.forge.identity()
    assert same_login(ident.login, sandbox.lead_login)
    assert ident.kind == "user"
    assert ident.id is not None
    assert ident.fetched_at.utcoffset() is not None
    sandbox.tick()
    # Caché (08 §6.2): sin refresh no hay otro GET /user, así que fetched_at no cambia.
    assert sandbox.forge.identity() == ident


# 08 §4.2, columna «Permiso», en la forma de X-Accepted-GitHub-Permissions. Solo los
# métodos de esa tabla que existen en H2 (las escrituras de PR y revisiones llegan en H3).
TABLE_4_2: dict[str, tuple[str, ...]] = {
    "get_ref": ("contents=read",),
    "list_refs": ("contents=read",),
    "create_ref": ("contents=write",),
    "update_ref": ("contents=write",),
    "delete_ref": ("contents=write",),
    "list_ref_activity": ("contents=read",),
    "create_empty_commit": ("contents=write",),
    "get_commit": ("contents=read",),
    "get_file": ("contents=read",),
    "compare": ("contents=read",),
    "codeowners_errors": ("contents=read",),
    "get_issue": ("issues=read",),
    "list_issues": ("issues=read",),
    "create_issue": ("issues=write",),
    "update_issue": ("issues=write",),
    "add_labels": ("issues=write",),
    "remove_label": ("issues=write",),
    "ensure_labels": ("issues=write",),
    "list_issue_events": ("issues=read",),
    "set_assignees": ("issues=write",),
    "add_assignees": ("issues=write",),
    "remove_assignees": ("issues=write",),
    "list_comments": ("issues=read",),
    "create_comment": ("issues=write",),
    "update_comment": ("issues=write",),
    "get_pull_request": ("pull_requests=read",),
    "find_pull_request": ("pull_requests=read",),
    "list_pull_requests": ("pull_requests=read",),
    "list_pull_request_files": ("pull_requests=read",),
    "list_pull_request_commits": ("pull_requests=read",),
    "list_reviews": ("pull_requests=read",),
    "get_checks": ("actions=read", "statuses=read"),
    "get_workflow_run": ("actions=read",),
    "successful_runs": ("actions=read",),  # D-45 (ci seal)
}
WRITES_4_2 = {m for m, perms in TABLE_4_2.items() if any(p.endswith("=write") for p in perms)}
# En un repo privado, sin permisos, el 403 llega en la primera petición del método y la cabecera
# trae solo su permiso (CT-AUTH-01 en ct-private; 00-marco D-68).
PRIVATE_FIRST_REQUEST: dict[str, tuple[str, ...]] = {
    "create_empty_commit": ("contents=read",),  # GET git/commits/{parent}
    "ensure_labels": ("issues=read",),  # GET labels/{name}
    "get_checks": ("actions=read",),  # los runs de Actions, antes que los estados
}


def test_ct_auth_01_permission_hints_match_the_table(sandbox: Sandbox) -> None:
    """CT-AUTH-01: sin permisos, cada 403 trae X-Accepted-GitHub-Permissions y coincide con 08 §4.2.

    Ninguna escritura pasa. Una lectura puede pasar (repo público) o dar 404; si da 403,
    su permiso también se compara con la tabla.
    """
    forge = sandbox.forge
    branch = sandbox.branch("auth-01")
    sha = sandbox.claim_commit()
    forge.create_ref(branch, sha)
    issue = forge.create_issue("ct auth-01", "body", op_id=f"ct-{sandbox.run_id}-auth-01")
    comment = forge.create_comment(issue.number, "c", op_id=f"ct-{sandbox.run_id}-auth-01-c")
    n = issue.number
    me = sandbox.lead_login
    calls: dict[str, Callable[[ForgeAdapter], object]] = {
        "get_ref": lambda f: f.get_ref(branch),
        "list_refs": lambda f: f.list_refs(sandbox.branch("")),
        "create_ref": lambda f: f.create_ref(sandbox.branch("auth-01-new"), sha),
        "update_ref": lambda f: f.update_ref(branch, sha),
        "delete_ref": lambda f: f.delete_ref(branch),
        "list_ref_activity": lambda f: f.list_ref_activity(branch),
        "create_empty_commit": lambda f: f.create_empty_commit(sha, "ct: auth-01"),
        "get_commit": lambda f: f.get_commit(sha),
        "get_file": lambda f: f.get_file("roles.yaml", sha),
        "compare": lambda f: f.compare(sandbox.base_sha, sha),
        "codeowners_errors": lambda f: f.codeowners_errors(sandbox.base_branch),
        "get_issue": lambda f: f.get_issue(n),
        "list_issues": lambda f: f.list_issues(state="open"),
        "create_issue": lambda f: f.create_issue("ct auth-01 denied", "b", op_id=f"ct-{sandbox.run_id}-auth-01-x"),
        "update_issue": lambda f: f.update_issue(n, title="ct auth-01 renamed"),
        "add_labels": lambda f: f.add_labels(n, ["ct-auth"]),
        "remove_label": lambda f: f.remove_label(n, "ct-auth"),
        "ensure_labels": lambda f: f.ensure_labels([Label("ct-auth-01")]),
        "list_issue_events": lambda f: f.list_issue_events(n),
        "set_assignees": lambda f: f.set_assignees(n, [me]),
        "add_assignees": lambda f: f.add_assignees(n, [me]),
        "remove_assignees": lambda f: f.remove_assignees(n, [me]),
        "list_comments": lambda f: f.list_comments(n),
        "create_comment": lambda f: f.create_comment(n, "denied", op_id=f"ct-{sandbox.run_id}-auth-01-y"),
        "update_comment": lambda f: f.update_comment(comment.id, "denied"),
        "get_pull_request": lambda f: f.get_pull_request(n),
        "find_pull_request": lambda f: f.find_pull_request(branch),
        "list_pull_requests": lambda f: f.list_pull_requests(state="open"),
        "list_pull_request_files": lambda f: f.list_pull_request_files(n),
        "list_pull_request_commits": lambda f: f.list_pull_request_commits(n),
        "list_reviews": lambda f: f.list_reviews(n),
        "get_checks": lambda f: f.get_checks(sha),
        "get_workflow_run": lambda f: f.get_workflow_run(1),
        "successful_runs": lambda f: f.successful_runs("seal.yml", sandbox.base_branch, event="push"),
    }
    assert set(calls) == set(TABLE_4_2)
    hints: dict[str, str] = {}
    opened: list[int] = []
    with sandbox.no_permission_token() as denied:
        for method, call in calls.items():
            try:
                result = call(denied)
            except ForgePermissionError as exc:
                assert exc.http_status == 403, method
                hints[method] = exc.hint or ""
            except ForgeNotFound:
                pass  # el recurso no es visible sin permisos
            else:
                if method == "create_issue":
                    opened.append(result.number)  # type: ignore[attr-defined]
    public = forge.capabilities().visibility == "public"
    deleted = forge.delete_ref(branch)  # limpiar antes de comparar
    for number in (n, *opened):
        forge.update_issue(number, state="closed", state_reason="not_planned")
    # En un repo público, GitHub abre el Issue aunque el token no tenga el permiso Issues; en uno
    # privado, no (00-marco D-68).
    assert len(opened) == (1 if public else 0)
    assert WRITES_4_2 - OPEN_WITHOUT_PERMISSION <= set(hints)
    expected = {m: TABLE_4_2[m] if public else PRIVATE_FIRST_REQUEST.get(m, TABLE_4_2[m]) for m in hints}
    wrong = {m: h for m, h in hints.items()
             if "X-Accepted-GitHub-Permissions" not in h or not all(p in h for p in expected[m])}
    assert not wrong, wrong  # todas las diferencias de una vez
    assert deleted is True


@pytest.mark.parametrize("repo", ["ct-public", "ct-private-team"])
def test_ct_cap_01_enforced_repos(repo_sandbox: Callable[[str], ForgeAdapter], repo: str) -> None:
    """CT-CAP-01: ct-public y ct-private-team (org Team de pruebas, 00-marco D-24) -> enforced.

    En ``live`` registra además ``plan.name`` (Q-FORGE-4) y el resultado de crear el push
    ruleset de REQ-PERM-53 (H3).
    """
    visibility, profile = SANDBOX_REPOS[repo]
    caps = repo_sandbox(repo).capabilities(refresh=True)
    assert profile == "enforced"
    assert caps.visibility == visibility and caps.profile == "enforced"
    assert caps.rules_available is True and caps.code_owners_enforceable is True
    assert caps.base_rules is not None and caps.base_rules.source in ("ruleset", "classic")
    assert caps.evidence


def test_ct_cap_02_private_free_repo_is_advisory(repo_sandbox: Callable[[str], ForgeAdapter]) -> None:
    """CT-CAP-02: ct-private (org Free) -> advisory, sin reglas legibles en la base (Q-FORGE-4)."""
    caps = repo_sandbox("ct-private").capabilities(refresh=True)
    assert caps.visibility == "private" and caps.profile == "advisory"
    assert caps.rules_available is False and caps.base_rules is None
    assert caps.evidence


def test_ct_etag_01_conditional_get_is_free(live_sandbox: Sandbox) -> None:
    """CT-ETAG-01 (live, H3): un GET repetido da 304 sin consumir x-ratelimit-remaining (08 §7.4, REQ-FORGE-52)."""
    probe = live_sandbox.http_probe()
    first = live_sandbox.forge.get_ref(live_sandbox.base_branch)
    remaining = probe.rate_remaining()
    assert live_sandbox.forge.get_ref(live_sandbox.base_branch) == first
    assert probe.last_status() == 304
    assert probe.rate_remaining() == remaining


# --- Solo con el falso -------------------------------------------------------------


def test_fake_injected_401_on_a_write(fake: FakeForge) -> None:
    """CT-AUTH-02 (falso): una falla 401 inyectada da ForgeAuthError."""
    fake.faults.add(Fault("create_ref", "http_401"))
    with pytest.raises(ForgeAuthError) as info:
        fake.create_ref("task/1", fake.to_state()["refs"]["heads/main"])
    assert info.value.http_status == 401
    assert fake.get_ref("task/1") is None


def test_fake_injected_403_has_permission_hint(fake: FakeForge) -> None:
    """CT-AUTH-02 (falso): una 403 inyectada da ForgePermissionError con hint."""
    fake.faults.add(Fault("add_labels", "http_403"))
    issue = fake.seed_issue(title="t")
    with pytest.raises(ForgePermissionError) as info:
        fake.add_labels(issue.number, ["task"])
    assert info.value.hint == "X-Accepted-GitHub-Permissions: issues=write"
    assert fake.get_issue(issue.number).labels == ()  # type: ignore[union-attr]


def test_fake_identity_token_source_and_kind(fake: FakeForge) -> None:
    ident = fake.identity()
    assert ident.token_source == token_sources(_meta.ENV_PREFIX)[0]
    assert ident.token_kind == "fine_grained_pat"
    assert ident.token_scope == "repo_restricted"
    assert fake.calls.count("L", "identity") == 1
    fake.identity()
    assert fake.calls.count("L", "identity") == 1  # en caché
    fake.identity(refresh=True)
    assert fake.calls.count("L", "identity") == 2


def test_fake_ci_identity_makes_no_request(fake: FakeForge) -> None:
    """REQ-FORGE-43: en CI no hay llamada a GET /user."""
    ci = fake.as_user(CI_LOGIN)
    ident = ci.identity()
    assert ident.kind == "ci" and ident.id is None and ident.token_scope == "ci"
    assert ident.token_source == "GITHUB_TOKEN"
    assert fake.calls.count(method="identity") == 0


def test_fake_capabilities_follow_profile(fake: FakeForge) -> None:
    """Perfil enforced o advisory (08 §3.3); el token fine-grained no lee check runs (§3.1)."""
    caps = fake.capabilities()
    assert caps.profile == "enforced" and caps.visibility == "public"
    assert caps.forge == "fake" and caps.default_branch == fake.default_branch
    assert caps.checks_api is False
    assert caps.viewer_permission == "admin"
    advisory = FakeForge(fake.repo, LEAD, profile="advisory")
    caps = advisory.capabilities()
    assert caps.profile == "advisory" and caps.rules_available is False and caps.base_rules is None
