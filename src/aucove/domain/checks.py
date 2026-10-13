# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Checks requeridos de la rama base (06-permisos REQ-PERM-34; 09-ci REQ-CI-9).

Una sola constante, compartida por ``init``, ``doctor`` y los workflows de la plantilla (una
prueba compara los nombres de sus jobs): ``aucove/build`` y ``aucove/validate`` y, con
``format: latex``, desde H3, ``aucove/editor-review`` (00-marco D-56).
"""

from __future__ import annotations

from typing import Final

from .. import _meta

__all__ = ["BUILD", "EDITOR_REVIEW", "GITHUB_ACTIONS_APP_ID", "VALIDATE", "required_checks"]

BUILD: Final = f"{_meta.NAME}/build"
VALIDATE: Final = f"{_meta.NAME}/validate"
EDITOR_REVIEW: Final = f"{_meta.NAME}/editor-review"
# integration_id de la app github-actions: solo cuentan los checks que emite Actions (06 §4.7).
GITHUB_ACTIONS_APP_ID: Final = 15368


def required_checks(fmt: str) -> tuple[str, ...]:
    """Checks requeridos con ``format: fmt``."""
    return (BUILD, VALIDATE, EDITOR_REVIEW) if fmt == "latex" else (BUILD, VALIDATE)
