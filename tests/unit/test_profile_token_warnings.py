# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``W_FORGE_PROFILE_ADVISORY`` en ``status``, ``review list`` y ``context`` (12-mvp A5; 05 §12.2) y
``W_AUTH_BROAD_TOKEN`` en la primera operación con red del día (08-forja REQ-FORGE-39).

El token amplio se siembra como lo deja el adaptador de GitHub: ``identity.json`` en la caché local
con ``token_scope: broad``. El FakeForge no escribe esa caché, así que sin ella no hay aviso.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from aucove.forge.base import Identity
from aucove.forge.cache import identity_data
from aucove.localstore.store import Store
from aucove.ops.result import Result
from tests.board_world import NOW, World, new_world


def codes(result: Result) -> list[str]:
    return [w.code for w in result.warnings]


# -- el perfil advisory (A5) ---------------------------------------------------------------------


@pytest.mark.parametrize("op", ["status", "review.list"])
def test_status_and_review_list_warn_in_advisory(tmp_path: Path, op: str) -> None:
    advisory = new_world(tmp_path / "a", profile="advisory")
    result = advisory.run(op, login="ana")
    assert result.ok, result.error
    assert codes(result).count("W_FORGE_PROFILE_ADVISORY") == 1
    enforced = new_world(tmp_path / "e")
    assert "W_FORGE_PROFILE_ADVISORY" not in codes(enforced.run(op, login="ana"))


def test_context_warns_in_advisory(tmp_path: Path) -> None:
    result = new_world(tmp_path, profile="advisory").context(login="ana")
    assert "W_FORGE_PROFILE_ADVISORY" in codes(result)


# -- el token amplio (REQ-FORGE-39) ------------------------------------------------------------


def seed_identity(w: World, *, scope: str = "broad", days: int = 30) -> None:
    ident = Identity("carla", 3, "user", "env:GITHUB_TOKEN", "classic_pat", scope, ("repo",), None, NOW)  # type: ignore[arg-type]
    Store(w.repo.path).write("identity.json", identity_data(ident, "fp-1", NOW + timedelta(days=days)),
                             key={"forge": "github", "token_fp": "fp-1"})


@pytest.fixture
def w(tmp_path: Path) -> World:
    world = new_world(tmp_path)
    world.task(12, claimant="carla")
    return world


def test_the_first_networked_operation_of_the_day_warns_once(w: World) -> None:
    seed_identity(w)
    first = w.run("status", login="carla")
    (warning,) = [x for x in first.warnings if x.code == "W_AUTH_BROAD_TOKEN"]
    assert warning.details is not None and warning.details["reason"] == "token_scope"
    assert str(warning.details["token_url"]).startswith("https://github.com/settings/personal-access-tokens/new?")
    assert "W_AUTH_BROAD_TOKEN" not in codes(w.run("status", login="carla"))  # el mismo día, una vez
    tomorrow = w.run("status", login="carla", now=NOW + timedelta(days=1))
    assert "W_AUTH_BROAD_TOKEN" in codes(tomorrow)


def test_local_operations_do_not_count(w: World) -> None:
    seed_identity(w)
    result = w.run("check-path", login="carla", paths=["sections/methods/x.tex"], task=12)
    assert "W_AUTH_BROAD_TOKEN" not in codes(result)
    assert "W_AUTH_BROAD_TOKEN" in codes(w.run("status", login="carla"))  # la primera con red sigue pendiente


@pytest.mark.parametrize(("scope", "days"), [("repo_restricted", 30), ("broad", -1)])
def test_a_restricted_or_expired_identity_says_nothing(w: World, scope: str, days: int) -> None:
    seed_identity(w, scope=scope, days=days)
    assert "W_AUTH_BROAD_TOKEN" not in codes(w.run("status", login="carla"))


def test_without_the_identity_cache_there_is_no_warning(w: World) -> None:
    assert "W_AUTH_BROAD_TOKEN" not in codes(w.run("status", login="carla"))
