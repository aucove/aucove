# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""El hint de ``E_STATE_UNSUPPORTED_DOOR`` sigue a D-41.

S1 cambió la puerta web (00-marco D-41): ahora sí reclama, guarda y entrega, y lo único
que no admite es ``release`` (04-tareas REQ-TASK-78), además de ``switch`` (REQ-TASK-77).
La Sugerencia de 05-operaciones §12.1 se enmendó para remitir a la terminal o a la pestaña
Code de Claude Desktop y, solo para cambiar de tarea, a una sesión nueva desde
``task/<n>``. El catálogo seguía con la Sugerencia anterior, que solo hablaba de abrir una
sesión nueva, así que ``release`` en la web daba un hint que no servía para liberar.

Solo usa la biblioteca estándar, como ``tests/unit/test_core_catalog.py``. Con
``AUCOVE_SPEC_DIR`` también compara con la fila de la spec.
"""

from __future__ import annotations

import os
from pathlib import Path

from tests.unit.test_core_catalog import (
    CODE_SPAN,
    MAX_TEXT,
    SPANISH_ONLY,
    SPEC_ENV,
    _spec_rows,
    _texts,
)

KEY = "error.E_STATE_UNSUPPORTED_DOOR.hint"
# Copia literal de la Sugerencia de 05-operaciones §12.1 tras D-41 y REQ-TASK-78.
SPEC_HINT = (
    "Hazlo desde la terminal o la pestaña Code de Claude Desktop; para cambiar de "
    "tarea en la web, abre una sesión nueva desde la rama `task/<n>`."
)
# Sugerencias anteriores a D-41, que ya no sirven para ``release``.
OLD_ES = "Abre una sesión nueva desde la rama `task/<n>`."
OLD_EN = "Open a new session from the `task/<n>` branch."


def test_es_hint_is_amended_spec_suggestion() -> None:
    hint = _texts("es")[KEY]
    assert hint != OLD_ES, "sigue la Sugerencia anterior a D-41"
    assert hint == SPEC_HINT
    spec = os.environ.get(SPEC_ENV)
    if spec and (Path(spec) / "05-operaciones.md").is_file():
        # La constante de arriba no debe quedar obsoleta frente a la spec.
        assert _spec_rows("12.1", "E_")["E_STATE_UNSUPPORTED_DOOR"][3] == SPEC_HINT


def test_en_hint_translates_amended_suggestion() -> None:
    hint = _texts("en")[KEY]
    assert hint != OLD_EN, "sigue la traducción anterior a D-41"
    # REQ-TASK-78: remite a la terminal o a la pestaña Code de Claude Desktop.
    assert "terminal" in hint
    assert "Code tab of Claude Desktop" in hint
    # REQ-TASK-77: para cambiar de tarea en la web, una sesión nueva desde task/<n>.
    assert "new session" in hint and "web" in hint
    # REQ-ARCH-73: mismos fragmentos de código que en es, en el mismo orden.
    assert CODE_SPAN.findall(hint) == CODE_SPAN.findall(SPEC_HINT) == ["task/<n>"]
    assert not set(CODE_SPAN.sub("", hint)) & SPANISH_ONLY
    assert len(hint) <= MAX_TEXT
