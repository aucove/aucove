# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``init --repo`` en un repo privado de una cuenta personal (issue #27; 06-permisos §4.2).

Con la 0.1.0rc5, ``init --repo <usuario>/<repo> --individual`` fallaba en cada intento con
``E_FORGE_UNEXPECTED`` (``{"kind": "validation", "status": 422}``): ``desired_settings`` pedía
``allow_forking: false`` en todo repo no público, y GitHub solo deja cambiar la política de forks
de un repo privado si es de una organización («Allow forks setting can only be changed on
org-owned private repositories»). El falso decía siempre ``owner_type: Organization`` y aceptaba la
clave, así que ninguna prueba lo vio. Ahora el falso responde como GitHub (``seed_admin(org=None)``
es una cuenta personal), y ``init`` y ``doctor`` dejan ``allow_forking`` fuera en esos repos.
"""

from __future__ import annotations

from pathlib import Path

from aucove.i18n import translator
from aucove.ops import init as init_op
from aucove.ops.doctor import desired_settings
from tests.unit.test_op_init import _no_wait as _no_wait  # noqa: F401 - fixture autouse
from tests.unit.test_op_init import protections, valid
from tests.unit.test_op_init_repo import fresh_world, run_repo


def test_init_repo_on_a_personal_private_repository(tmp_path: Path) -> None:
    w = fresh_world(tmp_path, profile="advisory")  # privado y con el plan Free, como en el issue
    w.forge.seed_admin(org=None)  # el dueño es una cuenta personal
    result = valid(run_repo(w, language="es", individual=True))
    assert result.ok, (result.error, result.data)
    assert protections(result)["repo_settings"] == "applied"
    settings = w.forge.admin("admin-token").repo_settings()
    assert settings["allow_forking"] is True  # GitHub no deja cambiarlo: no se pide
    assert settings["allow_squash_merge"] is True and settings["allow_merge_commit"] is False
    text = init_op.render_text(result, translator("es"))
    assert "None" not in text


def test_a_failed_step_names_the_repository_the_profile_and_the_step(tmp_path: Path) -> None:
    """En el issue, la vista legible de la falla decía ``init on None (None profile):``."""
    from aucove.forge.fake import Fault

    w = fresh_world(tmp_path, profile="advisory")
    w.forge.seed_admin(org=None)
    w.forge.faults.add(Fault("ensure_labels", "http_5xx", nth=1))
    failed = valid(run_repo(w, language="es", individual=True))
    assert not failed.ok and failed.data["failed_step"] == "labels"  # type: ignore[index]
    assert (failed.data["repo"], failed.data["profile"]) == ("lab-x/estudio", "advisory")  # type: ignore[index]
    t = translator("es")
    assert init_op.render_text(failed, t).split("\n") == [
        t("init.render.head", repo="lab-x/estudio", profile="advisory"),
        t("init.render.stopped", step=t("init.render.step.labels")),
    ]


def test_the_settings_of_a_personal_repository_leave_the_forks_out() -> None:
    assert "allow_forking" not in desired_settings("private", "User")
    assert "allow_forking" not in desired_settings("public", "Organization")
    assert desired_settings("private", "Organization")["allow_forking"] is False
    assert desired_settings("internal", "Organization")["allow_forking"] is False
