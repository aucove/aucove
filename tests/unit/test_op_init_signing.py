# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``init`` y el environment de la firma del proyecto (06-permisos §4.3b, REQ-PERM-61; 00-marco D-72).

``init`` crea ``<nombre>-evidence`` con la rama base como única regla en cualquier modo
(un workflow que lo nombra lo crearía sin reglas), rechaza la llave como secreto del
repositorio o de la organización y, con ``mode: ci``, deja como paso pendiente agregar el
secreto al environment: nunca escribe secretos. Usa el mundo de ``test_op_init``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aucove import _meta
from aucove.forge.fake import FakeAdmin
from aucove.i18n import translator
from aucove.ops.protections import EVIDENCE_ENVIRONMENT, EVIDENCE_SECRET
from tests.board_world import World
from tests.unit.test_op_init import _no_wait as _no_wait  # noqa: F401 - fixture autouse
from tests.unit.test_op_init import init_world, protections, run_init, valid

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHO4TeimWDqbJtD9DZ0jEQ7osipbPsIQKXvEpl+4J8Ve"


def with_signing(w: World, mode: str) -> None:
    """``evidence_signing`` en el ``project.yaml`` de la base (03-esquemas)."""
    path = w.repo.path / "project.yaml"
    block = (f"evidence_signing:\n  mode: {mode}\n  keys:\n    - id: ci-2026\n"
             f'      public_key: "{KEY}"\n      since: "2026-02-01T00:00:00Z"\n')
    w.repo.commit("evidence_signing", files={"project.yaml": path.read_text(encoding="utf-8") + block})
    w.repo.push("main")
    w.repo.fetch()


def environment(w: World) -> dict[str, object] | None:
    return w.forge.admin_state()["environments"].get(EVIDENCE_ENVIRONMENT)


def secret_steps(result: object) -> list[str]:
    steps = result.data["pending_steps"]  # type: ignore[attr-defined]
    return [s for s in steps if EVIDENCE_SECRET in s]


def test_init_creates_the_environment_with_the_base_as_its_only_rule(tmp_path: Path) -> None:
    w = init_world(tmp_path)
    result = valid(run_init(w, language="es"))
    assert result.ok, result.error
    assert protections(result)["environment"] == "applied"
    assert environment(w) == {"policy": "custom", "rules": [["branch", "main"]], "secrets": []}
    assert secret_steps(result) == []  # mode none: nadie firma en CI
    again = valid(run_init(w))
    assert protections(again)["environment"] == "unchanged"


def test_an_environment_open_to_other_branches_is_closed(tmp_path: Path) -> None:
    w = init_world(tmp_path)
    w.forge.seed_environment(EVIDENCE_ENVIRONMENT, policy="all")
    result = valid(run_init(w, language="es"))
    assert result.ok and protections(result)["environment"] == "applied"
    assert environment(w) == {"policy": "custom", "rules": [["branch", "main"]], "secrets": []}
    w2 = init_world(tmp_path / "otra")
    w2.forge.seed_environment(EVIDENCE_ENVIRONMENT, rules=[("branch", "main"), ("branch", "dev")])
    result = valid(run_init(w2, language="es"))
    assert protections(result)["environment"] == "applied"
    env = environment(w2)
    assert env is not None and env["rules"] == [["branch", "main"]]


@pytest.mark.parametrize("scope", ["repo", "org"])
def test_the_key_as_a_secret_of_the_repo_or_the_org_stops_init(tmp_path: Path, scope: str) -> None:
    """REQ-SEC-87: la llave del proyecto solo va en el environment, donde solo la lee la rama base."""
    w = init_world(tmp_path)
    w.forge.seed_secrets(scope, [EVIDENCE_SECRET, "OTRO"])  # type: ignore[arg-type]
    result = valid(run_init(w, language="es"))
    assert not result.ok and result.error is not None
    assert (result.error.code, result.error.details) == ("E_CONFIG_EVIDENCE_KEY", {"reason": "repo_secret",
                                                                                  "scope": scope})
    assert environment(w) is None  # antes de crear el environment


def test_ci_mode_without_the_secret_leaves_a_pending_step(tmp_path: Path) -> None:
    w = init_world(tmp_path)
    with_signing(w, "ci")
    result = valid(run_init(w, language="es"))
    assert result.ok, result.error
    t = translator("es")
    assert secret_steps(result) == [t("init.pending.evidence_secret", secret=EVIDENCE_SECRET,
                                      environment=EVIDENCE_ENVIRONMENT)]
    assert "gh secret set" in secret_steps(result)[0]
    # con el secreto en el environment, nada queda pendiente
    w2 = init_world(tmp_path / "con-secreto")
    with_signing(w2, "ci")
    w2.forge.seed_environment(EVIDENCE_ENVIRONMENT, rules=[("branch", "main")], secrets=[EVIDENCE_SECRET])
    again = valid(run_init(w2, language="es"))
    assert again.ok and secret_steps(again) == [] and protections(again)["environment"] == "unchanged"


def test_admin_mode_needs_no_secret(tmp_path: Path) -> None:
    w = init_world(tmp_path)
    with_signing(w, "admin")
    result = valid(run_init(w, language="es"))
    assert result.ok and protections(result)["environment"] == "applied" and secret_steps(result) == []


def test_unreadable_secret_names_leave_a_pending_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = init_world(tmp_path)
    monkeypatch.setattr(FakeAdmin, "secret_names", lambda self, scope, environment=None: None)
    result = valid(run_init(w, language="es"))
    assert result.ok, result.error
    t = translator("es")
    steps = result.data["pending_steps"]  # type: ignore[index]
    for scope in ("repo", "org"):
        assert t(f"init.pending.evidence_secrets_{scope}", secret=EVIDENCE_SECRET) in steps


def test_a_private_free_repository_has_no_environment(tmp_path: Path) -> None:
    """Sin environments (privado en el plan Free) no hay dónde guardar la llave: ``unsupported``."""
    w = init_world(tmp_path, profile="advisory", visibility="private")
    with_signing(w, "ci")
    result = valid(run_init(w, language="es"))
    assert result.ok, result.error
    assert protections(result)["environment"] == "unsupported"
    assert environment(w) is None and secret_steps(result) == []


def test_the_names_follow_the_framework() -> None:
    assert EVIDENCE_ENVIRONMENT == f"{_meta.NAME}-evidence"
    assert EVIDENCE_SECRET == f"{_meta.ENV_PREFIX}EVIDENCE_KEY"
