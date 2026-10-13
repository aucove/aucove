# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Lecturas de la configuración en el adaptador normal (08-forja §2; 00-marco D-65; REQ-FORGE-73).

CT-CFG-01 corre igual con el falso y con GitHub (REQ-FORGE-63), con el token de uso diario
de 08 REQ-FORGE-41: fine-grained, sin Administration. CT-CFG-02 (un repo privado de una org
Free) corre con el falso; el real se agrega con T2 en ``ct-private`` (infraestructura B a E).
"""

from __future__ import annotations

import secrets

import pytest

from aucove.forge.base import (
    REPO_SETTINGS,
    ActionsPolicy,
    ForgePermissionError,
    ForgeUnsupported,
    Label,
    RulesetInfo,
    WorkflowPermissions,
)
from tests.contract.conftest import READ_ONLY, RESEARCHER_A, Sandbox, new_fake

WRITE_ROLES = ("write", "maintain", "admin")
TRACE_KEYS = ("allow_squash_merge", "allow_merge_commit", "allow_rebase_merge",
              "squash_merge_commit_title", "squash_merge_commit_message")


def test_ct_cfg_01_reads_with_the_daily_token(sandbox: Sandbox) -> None:
    lead, researcher = sandbox.forge, sandbox.other
    settings = researcher.repo_settings()
    assert set(settings) <= set(REPO_SETTINGS)
    assert all(key in settings for key in TRACE_KEYS)  # con escritura (Contents W) se ven
    rules = lead.capabilities().rules_available  # ct-private, en Free, no tiene rulesets (CT-CFG-02)
    for adapter in (lead, researcher):
        if rules:
            rulesets = adapter.rulesets()
            assert all(isinstance(r, RulesetInfo) and r.name and r.enforcement in ("active", "evaluate", "disabled")
                       for r in rulesets)
        else:
            with pytest.raises(ForgeUnsupported):
                adapter.rulesets()
        assert all(isinstance(lab, Label) for lab in adapter.list_labels())
        assert not adapter.org_settings()  # None (cuenta personal) o sin las claves de 06 §4.10
    # Con el token diario (sin Administration), lo reservado a la administración queda desconocido.
    # El token de lead del sandbox real sí tiene Administration (12-mvp §3.3; 00-marco D-68).
    assert researcher.actions_policy() is None and researcher.workflow_permissions() is None
    if rules:
        assert all("bypass_actors" not in r.payload for r in researcher.rulesets())  # solo quien puede editarlos
    assert researcher.collaborator_permission(sandbox.accounts.researcher_a) in WRITE_ROLES
    assert researcher.collaborator_permission(f"ghost-{secrets.token_hex(6)}") is None
    assert isinstance(researcher.outside_collaborators(), list)


def test_ct_cfg_02_private_free_and_without_write_access() -> None:
    advisory = new_fake("advisory")
    with pytest.raises(ForgeUnsupported):
        advisory.rulesets()
    reader = advisory.as_user(READ_ONLY)
    assert all(key not in reader.repo_settings() for key in TRACE_KEYS)  # sin escritura no se ven
    with pytest.raises(ForgePermissionError):
        reader.outside_collaborators()


def test_fake_administration_needs_a_token_that_inherits_it() -> None:
    """Con el token de ``gh auth login`` (D-25) o uno clásico, la cuenta que administra lee la
    política de Actions, el token de los workflows, la organización y los actores de bypass."""
    fake = new_fake()
    admin = fake.admin("admin-token")
    admin.upsert_ruleset("aucove-base", {"target": "branch", "enforcement": "active",
                                           "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
                                           "bypass_actors": [], "rules": [{"type": "deletion"}]})
    assert "bypass_actors" not in fake.rulesets()[0].payload  # fine-grained, sin Administration
    fake.seed_user(fake.viewer, permission="admin", token_kind="oauth")
    assert isinstance(fake.actions_policy(), ActionsPolicy)
    assert isinstance(fake.workflow_permissions(), WorkflowPermissions)
    assert fake.org_settings() and "bypass_actors" in fake.rulesets()[0].payload
    writer = fake.as_user(RESEARCHER_A)
    writer.seed_user(RESEARCHER_A, token_kind="oauth")
    assert writer.actions_policy() is None  # escribe, pero no administra
