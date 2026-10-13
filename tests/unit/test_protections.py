# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Lo que aplica ``init`` y revisa ``doctor`` (06-permisos §4.7; 08-forja §3.1): ``base_rules_missing``.

Las demás piezas de ``ops.protections`` (ajustes, etiquetas, Actions, rulesets) tienen sus
pruebas en ``test_init_pieces``, que las usa a través de ``init``.
"""

from __future__ import annotations

import pytest

from aucove.domain.checks import required_checks
from aucove.forge.base import BaseRules, rules_summary
from aucove.ops import init as init_op
from aucove.ops import protections
from aucove.ops.protections import base_rules_missing, base_ruleset, missing_rules

APPROVALS = ["required_approving_review_count", "require_code_owner_review", "require_last_push_approval"]


def _summary(*, individual: bool, fmt: str = "latex") -> BaseRules:
    rules = base_ruleset("main", checks=required_checks(fmt), individual=individual, bypass_id=None)["rules"]
    return rules_summary(rules)


@pytest.mark.parametrize("individual", [False, True])
def test_the_base_ruleset_lacks_nothing(individual: bool) -> None:
    rules = base_ruleset("main", checks=required_checks("latex"), individual=individual, bypass_id=None)["rules"]
    assert missing_rules(rules, checks=required_checks("latex"), individual=individual) == []
    assert base_rules_missing(rules_summary(rules), checks=required_checks("latex"), individual=individual) == []


def test_group_mode_on_the_individual_ruleset_lacks_the_approvals() -> None:
    """06 REQ-PERM-60: con ``mode: group``, las reglas de ``individual`` no alcanzan."""
    assert base_rules_missing(_summary(individual=True), checks=required_checks("latex"), individual=False) == APPROVALS


def test_missing_checks_and_force_push() -> None:
    summary = _summary(individual=False, fmt="none")  # sin editor-review
    missing = base_rules_missing(summary, checks=required_checks("latex"), individual=False)
    assert missing == [f"check:{required_checks('latex')[-1]}"]
    no_ff = BaseRules("ruleset", 1, True, True, True, tuple(required_checks("none")), False)
    assert base_rules_missing(no_ff, checks=required_checks("none"), individual=False) == ["non_fast_forward"]


def test_classic_protection_only_counts_what_github_reports() -> None:
    """08 §3.1: con protección clásica, lo que no informa la forja (``None``) no falta."""
    classic = BaseRules("classic", None, None, None, None, None, None)
    assert base_rules_missing(classic, checks=required_checks("latex"), individual=False) == []
    with_checks = BaseRules("classic", None, None, None, None, ("x",), None)
    assert base_rules_missing(with_checks, checks=("x", "y"), individual=False) == ["check:y"]


def test_init_uses_the_same_pieces() -> None:
    """``init`` y ``doctor`` comparan contra lo mismo (06 REQ-PERM-39)."""
    for name in ("BASE_RULESET", "LABELS", "base_ruleset", "covers", "desired_settings", "missing_rules"):
        assert getattr(init_op, name) is getattr(protections, name)
