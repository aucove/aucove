# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``render.project``: la plantilla completa en el núcleo (01-arquitectura REQ-ARCH-102; 00-marco D-73).

``tools/render_template.py``, y con él el job ``template`` de ``release.yml``, e ``init --repo``
arman la plantilla con la misma función y los mismos valores, así que dan los mismos archivos.
"""

from __future__ import annotations

import tomllib

import pytest

from aucove import _meta
from aucove.config.datafiles import template_vars
from aucove.i18n import LANGS
from aucove.render.project import PUBLISHED_LANG, published_variables, render_project
from aucove.render.template import FORMATS
from tests.conftest import REPO_ROOT, load_tool


@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize("lang", LANGS)
def test_the_tool_and_init_repo_render_the_same(lang: str, fmt: str) -> None:
    assert render_project(lang, template_vars(), fmt=fmt) == load_tool("render_template").render_files(lang, fmt=fmt)


def test_template_vars_are_the_tools_file() -> None:
    expected = tomllib.loads((REPO_ROOT / "tools" / "template_vars.toml").read_text(encoding="utf-8"))
    assert dict(template_vars()) == expected


def test_published_variables_pin_the_version() -> None:
    values = published_variables("0.3.1rc2")
    assert values["core_pin"] == "0.3.1rc2" and values["core"] == ">=0.3.1rc2,<0.4"
    others = {k: v for k, v in template_vars().items() if k not in ("core", "core_pin")}
    assert {k: v for k, v in values.items() if k not in ("core", "core_pin")} == others
    with pytest.raises(ValueError):
        published_variables("0.3.1.dev1")  # no es un core_pin válido (00-marco D-66)


def test_the_published_template_of_the_installed_version() -> None:
    """``init --repo``: ``core_pin`` es la versión instalada y no queda ningún marcador sin resolver."""
    files = render_project(PUBLISHED_LANG, published_variables(_meta.__version__))
    project = files["project.yaml"].decode("utf-8")
    assert f'core_pin: "{_meta.__version__}"' in project and "language: en" in project
    assert not [rel for rel, data in files.items() if b"{{var:" in data]
