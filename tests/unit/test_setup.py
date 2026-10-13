# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Preparación pendiente (00-marco D-70; 05-operaciones REQ-OPS-69)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from aucove.ops.setup import plan_is_template, setup_pending

TEMPLATE = Path(__file__).parents[2] / "template"
PLANS = sorted([*TEMPLATE.glob("l10n/*/plan.md"), *TEMPLATE.glob("formats/*/l10n/*/plan.md")])


def _roles(*active: bool) -> SimpleNamespace:
    return SimpleNamespace(people=[SimpleNamespace(active=a) for a in active])


@pytest.mark.parametrize("path", PLANS, ids=lambda p: p.relative_to(TEMPLATE).as_posix())
def test_every_template_plan_is_recognized(path: Path) -> None:
    data = path.read_bytes()
    assert plan_is_template(data)
    assert plan_is_template(data.replace(b"\n", b"\r\n"))  # tras normalizar los saltos de línea


def test_an_edited_or_missing_plan_is_not_the_template() -> None:
    data = (TEMPLATE / "l10n" / "es" / "plan.md").read_bytes()
    assert not plan_is_template(data.replace(b"- RQ-1:", b"- RQ-1: \xc2\xbfCambia la movilidad? Antes:"))
    assert not plan_is_template(None)
    assert not plan_is_template(b"")


def test_setup_pending() -> None:
    template = (TEMPLATE / "l10n" / "en" / "plan.md").read_bytes()
    written = b"# Project plan\n\n## Objective\n\nMeasure the critical exponents.\n"
    assert setup_pending(template, _roles(True, True), "group")  # el plan de la plantilla
    assert setup_pending(written, _roles(True), "group")  # un grupo con una sola persona
    assert setup_pending(written, _roles(True, False), "group")  # las bajas no cuentan
    assert not setup_pending(written, _roles(True, True), "group")
    assert not setup_pending(written, _roles(True), "individual")  # una persona es lo esperado
    assert setup_pending(template, _roles(True), "individual")
