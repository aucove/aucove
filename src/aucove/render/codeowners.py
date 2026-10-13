# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Generación de ``.github/CODEOWNERS`` (06-permisos §4.6; 00-marco D-35).

Salida exacta del ejemplo de 06 §4.6::

    # Generado por `<nombre> sync-roles` desde roles.yaml. No editar a mano.
    # source: roles.yaml sha256:<12 hex>
    *   @<lead> @<deputy>
    # section <id>: owner @<owner>

- Una sola línea efectiva, ``*   @<lead>`` con el suplente detrás si lo hay;
  gana el último patrón que coincide, así que otra línea le quitaría rutas al
  ``lead``. Entre ``*`` y el primer dueño van tres espacios, como en el bloque
  de ejemplo (la prosa de la regla 2 escribe uno; manda el ejemplo byte a byte).
- La propiedad de las secciones va en comentarios ordenados por ``id``.
- No se localiza: la cabecera es el texto fijo de 06 §4.6, igual en ``es`` y en
  ``en`` (D-35 j). No lleva generador, versión ni marca de tiempo (REQ-ARCH-82).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from .. import _meta
from .template import RenderError, check_login, check_section_id, check_sha12, normalize_text

__all__ = ["CODEOWNERS_PATH", "render_codeowners"]

CODEOWNERS_PATH: Final = ".github/CODEOWNERS"

# Cabecera fija de 06 §4.6; el nombre sale de _meta (REQ-ARCH-19).
_HEADER: Final = f"# Generado por `{_meta.NAME} sync-roles` desde roles.yaml. No editar a mano."


def render_codeowners(
    *,
    lead: str,
    deputy: str | None,
    sections: Sequence[tuple[str, str]],
    source_sha12: str,
) -> bytes:
    """Bytes de ``CODEOWNERS`` para ``lead``, ``deputy`` y ``sections`` = ``(id, owner)``.

    Los logins se escriben con la grafía recibida (el llamador pasa la de
    ``people[].login``, 03 §4). ``source_sha12`` son los 12 hex de
    ``domain.text.sha12_canonical`` sobre ``roles.yaml`` (D-35 m).
    """
    check_login(lead, "lead")
    if deputy is not None:
        check_login(deputy, "deputy")
        if deputy.lower() == lead.lower():
            raise RenderError("deputy no puede ser el lead")
    check_sha12(source_sha12)
    owners: dict[str, str] = {}
    for item in sections:
        if isinstance(item, (str, bytes)) or not isinstance(item, Sequence) or len(item) != 2:
            raise RenderError(f"sección mal formada: {item!r} (se espera (id, owner))")
        section_id, owner = item
        check_section_id(section_id)
        check_login(owner, f"owner de {section_id}")
        if section_id in owners:
            raise RenderError(f"sección repetida: {section_id!r}")
        owners[section_id] = owner

    effective = f"*   @{lead}" + (f" @{deputy}" if deputy is not None else "")
    lines = [_HEADER, f"# source: roles.yaml sha256:{source_sha12}", effective]
    lines.extend(f"# section {sid}: owner @{owners[sid]}" for sid in sorted(owners))
    return normalize_text("\n".join(lines).encode("utf-8"))
