# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""El valor por defecto de ``framework_repo`` es el repositorio público del framework.

Los cinco pasos «Instalar el núcleo» de build.yml, compile.yml y validate.yml (09-ci
REQ-CI-12) y el README bajan ``https://github.com/{{var:framework_repo}}/releases/download/
v<pin>/requirements-lock.txt`` y lo aplican a ``uv tool install --constraints`` (10-seguridad
REQ-SEC-45). Quien controla ese repositorio decide qué dependencias se instalan en el CI de
todos los proyectos, incluido validate.yml en ``pull_request_target``.

El valor por defecto era ``example-org/aucove``, y la cuenta ``example-org`` existe en GitHub.
Mientras el repositorio del framework no existió, el valor fue un marcador con un dueño que
GitHub no admite, para que la descarga fallara en cerrado. Desde el corte (00-marco D-54), es
el repositorio público ``aucove/aucove``, del que salen los releases (D-69): nunca un dueño de
ejemplo.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VARS_FILE = REPO_ROOT / "tools" / "template_vars.toml"
TEMPLATE = REPO_ROOT / "template"
FRAMEWORK_REPO = "aucove/aucove"  # el repositorio público del framework (00-marco D-54)

# Dueños de ejemplo que sí existen o pueden registrarse; ninguno sirve de valor por defecto.
EXAMPLE_OWNERS = frozenset({"example-org", "example", "example-com", "your-org", "org", "owner"})

# Fuentes que usan {{var:framework_repo}} (los workflows y el README de la plantilla).
USERS: tuple[str, ...] = (
    "base/.github/workflows/build.yml",
    "base/.github/workflows/compile.yml",
    "base/.github/workflows/validate.yml",
    "l10n/es/README.md",
    "l10n/en/README.md",
)
LOCK_URL_RE = re.compile(r"https://github\.com/([^/\s\"']+)/([^/\s\"']+)/releases/download/[^\s\"']*requirements-lock\.txt")


def _framework_repo() -> str:
    data = tomllib.loads(VARS_FILE.read_text(encoding="utf-8"))
    return str(data["framework_repo"])


def test_default_framework_repo_is_the_public_framework_repo() -> None:
    value = _framework_repo()
    assert value == FRAMEWORK_REPO
    assert value.partition("/")[0].lower() not in EXAMPLE_OWNERS


def test_every_lock_url_uses_the_framework_repo() -> None:
    value = _framework_repo()
    seen = 0
    for rel in USERS:
        text = (TEMPLATE / rel).read_bytes().decode("utf-8")
        assert "{{var:framework_repo}}" in text, rel
        rendered = text.replace("{{var:framework_repo}}", value)
        urls = LOCK_URL_RE.findall(rendered)
        assert urls, rel
        for owner, repo in urls:
            seen += 1
            assert f"{owner}/{repo}" == value, rel
    assert seen >= 9  # 5 en los workflows y 2 en cada README


def test_value_has_no_shell_or_yaml_metacharacters() -> None:
    """El valor va dentro de un ``run`` y de bloques de shell del README."""
    assert re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", _framework_repo())
