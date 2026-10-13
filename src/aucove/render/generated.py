# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Única fuente de los archivos ``generated`` que dependen de ``roles.yaml``.

``generated_files`` produce ``.github/CODEOWNERS`` (sin idioma, 06 §4.6 y
D-35 j) y ``.github/ISSUE_TEMPLATE/task.yml`` (localizado, 03 §8.1). La usan
``sync-roles``, la regla ``roles-sync`` y el render de la plantilla (como
``extra`` de ``render_template``), para que los tres produzcan los mismos bytes.
Es una función pura sobre datos planos: no lee ``roles.yaml`` ni importa
``config``; el llamador pasa los logins con la grafía de ``people[].login``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from ..i18n import Lang
from .codeowners import CODEOWNERS_PATH, render_codeowners
from .task_form import TASK_FORM_PATH, render_task_form
from .template import RenderError, normalize_text

__all__ = ["GENERATED_PATHS", "RenderError", "generated_files", "normalize_text"]

GENERATED_PATHS: Final[tuple[str, str]] = (CODEOWNERS_PATH, TASK_FORM_PATH)


def generated_files(
    *,
    lead: str,
    deputy: str | None,
    sections: Sequence[tuple[str, str, str]],
    source_sha12: str,
    lang: Lang,
) -> dict[str, bytes]:
    """Ruta -> bytes de los dos generados; ``sections`` = ``(id, title, owner)`` en el orden de ``roles.yaml``."""
    triples: list[tuple[str, str, str]] = []
    for item in sections:
        if isinstance(item, (str, bytes)) or not isinstance(item, Sequence) or len(item) != 3:
            raise RenderError(f"sección mal formada: {item!r} (se espera (id, title, owner))")
        triples.append((item[0], item[1], item[2]))
    return {
        CODEOWNERS_PATH: render_codeowners(
            lead=lead,
            deputy=deputy,
            sections=[(section_id, owner) for section_id, _title, owner in triples],
            source_sha12=source_sha12,
        ),
        TASK_FORM_PATH: render_task_form(sections=triples, lang=lang),
    }
