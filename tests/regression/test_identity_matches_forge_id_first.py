# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Con ``people[].forge_id``, la identidad se compara primero por id.

08-forja §6.3 y la tabla de §6.4 (03-esquemas §4.2): si una persona de ``roles.yaml`` tiene
``forge_id``, el id decide quién es y el login solo sirve para mostrar. Si el id coincide y
el login no, la persona es la de ``roles.yaml`` y se avisa ``W_AUTH_LOGIN_CHANGED``; una
cuenta con el mismo login y otro id no es esa persona (REQ-FORGE-48). Antes, ``forge_id``
se analizaba y nadie lo usaba.

``Roles.identify`` es la resolución de config, y ``check-path`` la usa.
"""

from __future__ import annotations

import pytest

from aucove.config.roles import Identity, Roles, parse_roles
from tests.conftest import roles_fixture

ROLES_TEXT = roles_fixture("basic").replace(
    "  - login: bruno\n", "  - login: bruno\n    forge_id: 111\n"
)


@pytest.fixture(scope="module")
def roles() -> Roles:
    parsed, findings = parse_roles(ROLES_TEXT, file="roles.yaml")
    assert parsed is not None, findings
    assert parsed.person("bruno") is not None and parsed.person("bruno").forge_id == 111  # type: ignore[union-attr]
    return parsed


def test_same_login_with_another_id_is_not_that_person(roles: Roles) -> None:
    """Otra cuenta tomó el login abandonado de bruno: no hereda sus rutas."""
    assert roles.identify("bruno", 999) == Identity(None, False)


def test_same_id_with_another_login_is_that_person_with_a_warning(roles: Roles) -> None:
    """bruno renombró su cuenta a bruno2 (id 111): sigue siendo bruno, con W_AUTH_LOGIN_CHANGED."""
    ident = roles.identify("bruno2", 111)
    assert ident.person is not None and ident.person.login == "bruno"
    assert ident.login == "bruno" and ident.login_changed is True


def test_same_id_and_login(roles: Roles) -> None:
    ident = roles.identify("Bruno", 111)
    assert ident.login == "bruno" and ident.login_changed is False


def test_without_forge_id_in_roles_the_login_decides(roles: Roles) -> None:
    ident = roles.identify("carla", 5)
    assert ident.login == "carla" and ident.login_changed is False
    assert roles.identify("dave", 5) == Identity(None, False)


def test_unknown_identity_id_falls_back_to_the_login(roles: Roles) -> None:
    """Sin id en identity.json no se puede comparar por id: decide el login."""
    assert roles.identify("bruno", None).login == "bruno"


@pytest.mark.parametrize("bad_id", [True, 0, -1, "111", 1.0])
def test_a_malformed_identity_id_never_matches_by_id(roles: Roles, bad_id: object) -> None:
    assert roles.identify("bruno2", bad_id).person is None  # type: ignore[arg-type]


def test_a_repeated_forge_id_identifies_nobody() -> None:
    text = ROLES_TEXT.replace("  - login: carla\n", "  - login: carla\n    forge_id: 111\n")
    parsed, findings = parse_roles(text, file="roles.yaml")
    if parsed is None:  # el análisis ya lo rechaza: también vale
        assert findings
        return
    assert parsed.identify("bruno2", 111).person is None
