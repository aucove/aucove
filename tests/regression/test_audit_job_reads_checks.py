# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""El job ``audit`` de ``main-broken.yml`` puede leer los checks requeridos (09-ci §7.1; 06 REQ-PERM-59).

En una sesión de prueba (20/10/2026, 0.1.0rc4), ``aucove/base-audit`` falló en la primera
integración por PR con «The audit did not finish (`permission`)», también al repetirlo: para cada PR
integrado, ``ci audit`` lee los checks requeridos de su cabeza con ``get_checks`` (check runs y el
estado combinado), y el job solo tenía ``contents: read``, ``pull-requests: write`` e
``issues: write``. El e2e del job cubre el push directo, que no lee checks, y el falso no aplica los
permisos del ``GITHUB_TOKEN`` por job, así que nada lo detectó. Ahora el job declara exactamente
``ci_audit.JOB_PERMISSIONS``.
"""

from __future__ import annotations

from typing import Any

import yaml

from aucove.ops import ci_audit
from tests.conftest import REPO_ROOT

MAIN_BROKEN = REPO_ROOT / "template" / "base" / ".github" / "workflows" / "main-broken.yml"


def audit_job() -> dict[str, Any]:
    doc = yaml.safe_load(MAIN_BROKEN.read_text(encoding="utf-8"))
    [job] = [j for j in doc["jobs"].values() if j.get("name") == "aucove/base-audit"]
    return dict(job)


def test_the_audit_job_has_the_permissions_ci_audit_needs() -> None:
    assert audit_job()["permissions"] == dict(ci_audit.JOB_PERMISSIONS)


def test_it_can_read_the_checks_of_the_merged_pull_request() -> None:
    perms = audit_job()["permissions"]
    assert perms["checks"] == "read" and perms["statuses"] == "read"
    assert "aucove ci audit" in "\n".join(str(s.get("run", "")) for s in audit_job()["steps"])
