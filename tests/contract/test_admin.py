# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``ForgeAdmin`` (08-forja §2.2; 00-marco D-62): CT-ADM-01 a CT-ADM-03 con el falso.

Las escrituras de configuración están fuera del adaptador normal (REQ-FORGE-3) y solo las
hace ``init`` con el token de administración. El caso real necesita un token con
Administration RW sobre un repo del sandbox y se agrega con T2 real (infraestructura D).
"""

from __future__ import annotations

import pytest

from aucove.forge.base import (
    REPO_SETTINGS,
    ForgeAdmin,
    ForgeAdapter,
    ForgeAuthError,
    ForgePermissionError,
    ForgeUnsupported,
    ForgeValidationError,
    NoTokenError,
)
from aucove.forge.fake import FakeForge
from tests.contract.conftest import LEAD, READ_ONLY, RESEARCHER_A, new_fake

BASE_RULESET = {
    "target": "branch",
    "enforcement": "active",
    "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
    "bypass_actors": [],
    "rules": [{"type": "deletion"}, {"type": "non_fast_forward"},
              {"type": "pull_request", "parameters": {"required_approving_review_count": 1,
                                                      "require_code_owner_review": True}}],
}


def test_ct_adm_01_settings_round_trip(fake: FakeForge) -> None:
    admin = fake.admin("admin-token")
    assert isinstance(admin, ForgeAdmin) and not isinstance(fake, ForgeAdmin)
    settings = admin.repo_settings()
    assert set(settings) <= set(REPO_SETTINGS) and settings["allow_merge_commit"] is True
    after = admin.update_repo_settings({"allow_merge_commit": False, "squash_merge_commit_title": "PR_TITLE",
                                        "squash_merge_commit_message": "PR_BODY"})
    assert (after["allow_merge_commit"], after["squash_merge_commit_message"]) == (False, "PR_BODY")
    assert admin.repo_settings() == after
    fake.calls.clear()
    with pytest.raises(ForgeValidationError):
        admin.update_repo_settings({"private": False})  # REQ-FORGE-72: fuera de REPO_SETTINGS
    assert len(fake.calls) == 0  # sin llamar a la forja


def test_ct_adm_02_rulesets(fake: FakeForge) -> None:
    admin = fake.admin("admin-token")
    assert admin.ruleset("aucove-base") is None and admin.branch_rules("main") == []
    first = admin.upsert_ruleset("aucove-base", BASE_RULESET)
    again = admin.upsert_ruleset("aucove-base", BASE_RULESET)
    assert first == again  # un solo ruleset por nombre
    info = admin.ruleset("aucove-base")
    assert info is not None and info.id == first and info.enforcement == "active"
    assert info.payload["rules"] == BASE_RULESET["rules"]
    assert [r["type"] for r in admin.branch_rules("main")] == ["deletion", "non_fast_forward", "pull_request"]
    assert admin.branch_rules("task/3") == []
    caps = fake.capabilities(refresh=True)
    assert caps.base_rules is not None and caps.base_rules.require_code_owner_review is True
    assert (caps.base_rules.required_approvals, caps.base_rules.blocks_force_push) == (1, True)


def test_ct_adm_02_private_free_has_no_rulesets() -> None:
    advisory = new_fake("advisory")
    admin = advisory.admin("admin-token")
    with pytest.raises(ForgeUnsupported):
        admin.upsert_ruleset("aucove-base", BASE_RULESET)
    with pytest.raises(ForgeUnsupported):
        admin.ruleset("aucove-base")


def test_ct_adm_03_permissions(fake: FakeForge) -> None:
    fake.seed_admin_token("researcher-token", RESEARCHER_A)
    fake.seed_admin_token("revoked-token", None)
    writer = fake.admin("researcher-token")
    assert writer.identity().login == RESEARCHER_A
    with pytest.raises(ForgePermissionError):
        writer.update_repo_settings({"has_wiki": False})
    assert writer.actions_policy() is None and writer.workflow_permissions() is None  # lecturas: desconocidas
    with pytest.raises(ForgeAuthError):
        fake.admin("revoked-token").repo_settings()
    with pytest.raises(NoTokenError):
        fake.admin("  ")


def test_fake_actions_workflow_and_retention(fake: FakeForge) -> None:
    admin = fake.admin("admin-token")
    policy = admin.actions_policy()
    assert policy is not None and (policy.allowed_actions, policy.sha_pinning_required) == ("all", False)
    admin.set_actions_policy(enabled=True, allowed_actions="all", sha_pinning_required=True)
    assert admin.actions_policy().sha_pinning_required is True  # type: ignore[union-attr]
    fake.seed_admin(actions_org={"enabled_repositories": "all", "allowed_actions": "selected",
                                 "selected": {"github_owned_allowed": True, "verified_allowed": False,
                                              "patterns_allowed": ["astral-sh/setup-uv@*"]}})
    org = admin.actions_policy("org")
    assert org is not None and org.patterns_allowed == ("astral-sh/setup-uv@*",) and org.github_owned_allowed
    admin.set_workflow_permissions(default="read", can_approve=False)
    perms = admin.workflow_permissions()
    assert perms is not None and not perms.can_approve_pull_request_reviews
    admin.set_retention_days(90)
    assert admin.retention_days() == 90
    with pytest.raises(ForgeValidationError):
        admin.set_retention_days(400)  # público: como mucho 90 días (REQ-PERM-57)
    fake.seed_admin(retention=None)
    assert admin.retention_days() is None
    with pytest.raises(ForgeUnsupported):
        admin.set_retention_days(90)


def test_fake_collaborators_and_org(fake: FakeForge) -> None:
    admin = fake.admin("admin-token")
    assert admin.collaborator_permission(RESEARCHER_A) == "write"
    assert admin.collaborator_permission("nadie") is None
    assert admin.add_collaborator(READ_ONLY, "push") == "added" and admin.collaborator_permission(READ_ONLY) == "write"
    assert admin.add_collaborator("externa", "push") == "invited"
    fake.seed_admin(outside=[RESEARCHER_A])
    assert admin.outside_collaborators() == [RESEARCHER_A]
    assert admin.user_id(LEAD) == fake.identity().id
    assert admin.org_settings() is not None
    fake.seed_admin(org=None)  # el dueño es una cuenta personal: no hay nada de la org
    assert admin.org_settings() is None and admin.actions_policy("org") is None
    assert admin.workflow_permissions("org") is None


def test_the_adapter_has_no_configuration_writes(fake: FakeForge) -> None:
    """REQ-FORGE-3: el adaptador normal no cambia la configuración del repositorio."""
    adapter: ForgeAdapter = fake
    assert not hasattr(adapter, "upsert_ruleset") and not hasattr(adapter, "update_repo_settings")
