# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Preparación pendiente de un proyecto (00-marco D-70; 05-operaciones REQ-OPS-69).

Un proyecto tiene la preparación pendiente si su ``plan.md`` sigue siendo el de la plantilla
(igual, tras normalizar los saltos de línea, al de algún idioma y formato) o si, con
``mode: group``, ``roles.yaml`` tiene una sola persona activa. Lo usan ``context``, con la base
local, para la alerta ``setup_pending``, e ``init``, con el árbol que deja, para crear la
tarea de preparación.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING

from ..config.datafiles import data_path
from ..i18n import LANGS
from ..render.template import match_source

if TYPE_CHECKING:
    from ..config.roles import Roles

__all__ = ["PLAN_FILE", "plan_is_template", "setup_pending"]

PLAN_FILE = "plan.md"


@functools.cache
def _plan_sources() -> tuple[bytes, ...]:
    """El ``plan.md`` de la plantilla en cada idioma y en cada variante por formato.

    Sin la plantilla empaquetada no hay con qué comparar y el plan no cuenta como pendiente:
    ``context`` no falla por eso (REQ-OPS-43).
    """
    try:
        root = data_path("template")
    except FileNotFoundError:
        return ()
    found: list[bytes] = []
    for lang in LANGS:
        paths = [root / "l10n" / lang / PLAN_FILE, *sorted((root / "formats").glob(f"*/l10n/{lang}/{PLAN_FILE}"))]
        found.extend(path.read_bytes() for path in paths if path.is_file())
    return tuple(dict.fromkeys(found))


def plan_is_template(data: bytes | None) -> bool:
    """``True`` si ``data`` es el ``plan.md`` de la plantilla; un plan que falta no lo es."""
    if data is None:
        return False
    return any(match_source(source, data) is not None for source in _plan_sources())


def setup_pending(plan: bytes | None, roles: Roles, mode: str) -> bool:
    """REQ-OPS-69: el plan de la plantilla o, en ``group``, una sola persona activa."""
    if plan_is_template(plan):
        return True
    active = sum(1 for person in roles.people if person.active)
    return mode != "individual" and active <= 1
