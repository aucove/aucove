# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Un marcador precedido de ``$`` no escapa al render (01 §10.2, REQ-ARCH-80).

Antes, ``(?<!\\$)`` hacía que ``${{var:x}}`` saliera intacto y sin error, y que
``${{tool:x}}`` escapara al rechazo de marcadores ajenos. Ninguna expresión
válida de GitHub Actions empieza por ``${{var:`` ni por ``${{tool:`` (el
contexto real es ``vars.``), así que no hay nada que proteger.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aucove.render.template import RenderError, render_template


def _template(root: Path, text: bytes) -> Path:
    files = {"base/x.yml": text, "l10n/es/b.md": b"b\n", "l10n/en/b.md": b"b\n"}
    for rel, data in files.items():
        target = root / "template" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return root / "template"


def test_var_marker_after_dollar_is_substituted(tmp_path: Path) -> None:
    template = _template(tmp_path, b"a: ${{var:name}}\nc: {{var:name}}\n")
    out = render_template(template, "es", {"name": "VAL"})
    assert out["x.yml"] == b"a: $VAL\nc: VAL\n"


def test_var_marker_after_dollar_without_value_is_a_render_error(tmp_path: Path) -> None:
    template = _template(tmp_path, b"a: ${{var:missing}}\n")
    with pytest.raises(RenderError, match="missing"):
        render_template(template, "es", {"name": "VAL"})


@pytest.mark.parametrize(
    "text",
    [
        b"b: ${{tool:x}}\n",
        b"b: ${{cli:status}}\n",
        b"b: ${{human:init}}\n",
        b"b: ${{skill:start}}\n",
        b"b: ${{var:bad.name}}\n",
        b"b: ${{var:}}\n",
    ],
)
def test_foreign_or_malformed_marker_after_dollar_is_a_render_error(tmp_path: Path, text: bytes) -> None:
    template = _template(tmp_path, text)
    with pytest.raises(RenderError):
        render_template(template, "es", {"name": "VAL"})


def test_real_actions_expressions_stay_untouched(tmp_path: Path) -> None:
    text = b"s: ${{ github.sha }}\nv: ${{ vars.NAME }}\nm: ${{ matrix.os }}\n"
    template = _template(tmp_path, text)
    assert render_template(template, "es", {})["x.yml"] == text
