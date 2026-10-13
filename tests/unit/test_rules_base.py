# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Tipos y ayudas de las reglas, y el hallazgo."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

import pytest

from aucove.domain.findings import Finding, has_errors
from aucove.i18n import Translator
from aucove.ops.rules.base import (
    MODES,
    RULE_MODULES,
    Change,
    RuleInput,
    RuleSpec,
    make_finding,
    status_of,
    unavailable,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


def _t(lang: str = "es") -> Translator:
    return Translator(
        lang,  # type: ignore[arg-type]
        catalogs={
            "es": {
                "finding.E_VALIDATION_LABEL.message": "Etiqueta `{label}` sin definir.",
                "finding.E_VALIDATION_LABEL.hint": "Define `{label}` con `\\label`.",
                "finding.conv.tree.missing.message": "Falta {file}.",
                "finding.E_INTERNAL_UNEXPECTED.message": "La regla {rule} no pudo evaluarse: {reason}",
            },
            "en": {
                "finding.E_VALIDATION_LABEL.message": "Undefined label `{label}`.",
                "finding.E_INTERNAL_UNEXPECTED.message": "Rule {rule} could not be evaluated: {reason}",
                "finding.E_INTERNAL_UNEXPECTED.hint": "Report {rule}.",
            },
        },
    )


def _run(inp: RuleInput) -> Sequence[Finding]:
    return ()


RULE = RuleSpec(
    id="labels-refs",
    modes=frozenset({"tree", "pr", "base"}),
    run=_run,
    codes={"E_VALIDATION_LABEL": frozenset({"label"}), "conv.tree.missing": frozenset({"file"})},
)


def _inp(lang: str = "es") -> RuleInput:
    return RuleInput(mode="tree", t=_t(lang), now=NOW)


def test_rule_modules_are_data() -> None:
    assert RULE_MODULES == (
        "ops.rules.scope", "ops.rules.schemas", "ops.rules.roles_sync", "ops.rules.decisions",
        "ops.rules.plan_ids", "ops.rules.latex", "ops.rules.instructions", "ops.rules.secrets",
        "ops.rules.traceability", "ops.rules.ci_policy", "ops.rules.coordinator", "ops.rules.convention",
        "ops.rules.latex_log",
    )
    assert MODES == ("tree", "pr", "base", "build-log")


def test_rule_input_defaults() -> None:
    inp = _inp()
    assert inp.changes == () and dict(inp.prior) == {} and dict(inp.tools) == {}
    assert inp.policy is None and inp.tree is None and inp.forge is None and inp.log_path is None
    assert Change("a.tex", "renamed", "b.tex", "100644").previous_path == "b.tex"


def test_make_finding_localizes_message_and_hint() -> None:
    f = make_finding(RULE, _inp(), "E_VALIDATION_LABEL", severity="error",
                     path="sections/methods/methods.tex", line=4, label="fig:a")
    assert f.check == "labels-refs"
    assert f.message == "Etiqueta `fig:a` sin definir."
    assert f.hint == "Define `fig:a` con `\\label`."
    assert f.to_dict() == {
        "path": "sections/methods/methods.tex", "line": 4, "severity": "error",
        "code": "E_VALIDATION_LABEL", "message": "Etiqueta `fig:a` sin definir.", "pointer": None,
        "hint": "Define `fig:a` con `\\label`.",
    }
    g = make_finding(RULE, _inp(), "conv.tree.missing", severity="warning", path=None, file="AGENTS.md")
    assert g.message == "Falta AGENTS.md." and g.hint is None and g.path is None


def test_make_finding_falls_back_to_en_and_key() -> None:
    f = make_finding(RULE, _inp("en"), "E_VALIDATION_LABEL", severity="error", path=None, label="x")
    assert f.message == "Undefined label `x`." and f.hint is None


def test_make_finding_rejects_undeclared_code_or_markers() -> None:
    with pytest.raises(ValueError):
        make_finding(RULE, _inp(), "E_VALIDATION_MACRO", severity="error", path=None)
    with pytest.raises(ValueError):
        make_finding(RULE, _inp(), "E_VALIDATION_LABEL", severity="error", path=None)  # falta label
    with pytest.raises(ValueError):
        make_finding(RULE, _inp(), "E_VALIDATION_LABEL", severity="error", path=None, label="a", extra="b")


def test_unavailable() -> None:
    f = unavailable(RULE, _inp(), "falta la herramienta", path="refs.bib")
    assert (f.code, f.severity, f.check, f.path) == ("E_INTERNAL_UNEXPECTED", "error", "labels-refs", "refs.bib")
    assert f.message == "La regla labels-refs no pudo evaluarse: falta la herramienta"
    assert f.hint == "Report labels-refs."  # respaldo en en
    assert status_of([f]) == "fail"


def test_status_of() -> None:
    warn = make_finding(RULE, _inp(), "conv.tree.missing", severity="warning", path=None, file="x")
    assert status_of([]) == "pass"
    assert status_of([warn]) == "pass"
    assert has_errors([warn]) is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"code": "bad code"},
        {"code": "E_lower_case"},
        {"severity": "info"},
        {"path": "/abs/path"},
        {"path": "tex\\win"},
        {"line": 0},
        {"line": True},
        {"pointer": "no-slash"},
        {"hint": "h" * 501},
        {"check": ""},
    ],
)
def test_finding_validates_its_shape(kwargs: dict[str, object]) -> None:
    base: dict[str, object] = {"check": "schemas", "code": "E_VALIDATION_SCHEMA", "severity": "error",
                               "path": "roles.yaml", "line": 3, "message": "m", "hint": None, "pointer": "/people/0"}
    base.update(kwargs)
    with pytest.raises(ValueError):
        Finding(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize("code", ["conv.tree.missing", "E_VALIDATION_SCHEMA", "W_VALIDATION_LOGIN_CASE", "IL01", "SB12"])
def test_finding_accepts_schema_codes(code: str) -> None:
    f = Finding("x", code, "warning", None, None, "m", pointer="/a~1b/0")
    assert f.to_dict()["code"] == code
