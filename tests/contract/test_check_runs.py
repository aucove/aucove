# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``upsert_check_run`` (08-forja REQ-FORGE-70; 00-marco D-56).

Solo lo usa ``ci editor-review`` con el ``GITHUB_TOKEN`` de Actions: los tokens
fine-grained no pueden usar la API de Checks (08 §4.6), así que con las cuentas del
sandbox la respuesta es 403 tanto en el falso como en GitHub. El camino con el token
de Actions se prueba con el falso.
"""

from __future__ import annotations

import pytest

from aucove import _meta
from aucove.forge.base import ForgePermissionError
from aucove.forge.fake import CI_LOGIN, FakeForge
from tests.contract.conftest import Sandbox

CHECK = f"{_meta.NAME}/editor-review"


def test_fine_grained_token_cannot_write_check_runs(sandbox: Sandbox) -> None:
    with pytest.raises(ForgePermissionError) as info:
        sandbox.forge.upsert_check_run(sandbox.base_sha, CHECK, conclusion="success", summary="ok")
    assert info.value.http_status == 403


def test_fake_keeps_one_check_run_per_head(fake: FakeForge) -> None:
    ci = fake.as_user(CI_LOGIN)
    base = fake.get_ref(fake.default_branch)
    assert base is not None
    ci.upsert_check_run(base.sha, CHECK, conclusion="failure", summary="falta la aprobación")
    ci.upsert_check_run(base.sha, CHECK, conclusion="success", summary="aprobado")
    runs = [r for r in ci.get_checks(base.sha).runs if r.name == CHECK]
    assert [(r.status, r.conclusion, r.source) for r in runs] == [("completed", "success", "check_run")]
    assert fake.check_summary(base.sha, CHECK) == "aprobado"
    assert fake.calls.count(kind="E", method="upsert_check_run") == 2
