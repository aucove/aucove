# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Nombre y metadatos derivados de ``_meta.NAME`` (01 §3.2, REQ-ARCH-19, REQ-ARCH-20)."""

from __future__ import annotations

import ast
import re
import tomllib

import pytest

from aucove import _meta
from tests.conftest import REPO_ROOT

SRC = REPO_ROOT / "src" / _meta.NAME


def test_name_and_version() -> None:
    assert _meta.NAME == "aucove"
    assert _meta.__version__ == "0.1.0rc1"  # la primera versión preliminar (00-marco D-66 y D-69)
    assert _meta.SUPPORTED_CONVENTIONS == ("0.1",)


def test_derived_values() -> None:
    name = _meta.NAME
    assert _meta.MCP_SERVER_NAME == name
    assert _meta.CACHE_DIR == "." + name
    assert _meta.ENV_PREFIX == name.upper() + "_"
    assert _meta.SCHEMA_BASE == f"https://{name}.invalid/schemas"
    assert _meta.TEMPLATE_REPO == name + "-template"
    assert _meta.RESULT_SCHEMA == f"{name}.result/v1"
    assert _meta.CLAIM_SUBJECT == f"{name}: claim #"


def test_ci_marker() -> None:
    assert _meta.ci_marker("build") == f"<!-- {_meta.NAME}:ci:build v1 -->"
    assert _meta.ci_marker("main-broken") == f"<!-- {_meta.NAME}:ci:main-broken v1 -->"
    for bad in ("", "Build", "ci build", "-x", "a_b", "x-->"):
        with pytest.raises(ValueError):
            _meta.ci_marker(bad)


def test_meta_imports_nothing() -> None:
    tree = ast.parse((SRC / "_meta.py").read_text(encoding="utf-8"))
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert imports == []


def test_result_schema_const_matches() -> None:
    import json

    schema = json.loads((REPO_ROOT / "schemas" / "result.schema.json").read_text(encoding="utf-8"))
    assert schema["properties"]["schema"]["const"] == _meta.RESULT_SCHEMA
    assert schema["$id"].startswith(_meta.SCHEMA_BASE + "/")


def test_pyproject_uses_meta() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    assert project["name"] == _meta.NAME
    assert "version" in project["dynamic"] and "version" not in project
    assert data["tool"]["hatch"]["version"]["path"] == f"src/{_meta.NAME}/_meta.py"
    assert project["scripts"] == {_meta.NAME: f"{_meta.NAME}.entry:main"}
    assert project["license"] == "Apache-2.0 AND MIT-0 AND CC0-1.0"
    # REQ-TRACE-56: solo los textos de las licencias de `license`; CC-BY-4.0 es de spec/, que no se empaqueta
    assert project["license-files"] == [
        "LICENSE", "NOTICE", "LICENSES/Apache-2.0.txt", "LICENSES/MIT-0.txt", "LICENSES/CC0-1.0.txt",
    ]
    for path in project["license-files"]:
        assert (REPO_ROOT / path).is_file(), path
    assert project["requires-python"] == ">=3.11"


def test_pyproject_dependencies_match_spec() -> None:
    # Extracto normativo de 01-arquitectura §4.
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["dependencies"] == [
        "PyYAML>=6.0.2,<7",
        "jsonschema[format-nongpl]>=4.23,<5",
        "packaging>=24",
        "httpx2>=2.13,<3",
        "truststore>=0.10,<1",
        "mcp>=2.2,<3",
    ]
    # el extra evidence (D-07 d): cryptography firma y verifica la firma del proyecto (D-72)
    assert data["project"]["optional-dependencies"] == {"evidence": [
        "rfc3161-client>=1.0,<2", "opentimestamps>=0.4.5,<0.5", "cryptography>=44",
        "tzdata>=2025.2; sys_platform == 'win32'",
    ]}
    dev = {re.split(r"[<>=!~\[ ]", item, maxsplit=1)[0] for item in data["dependency-groups"]["dev"]}
    assert dev == {"pytest", "hypothesis", "pytest-cov", "pytest-xdist", "import-linter", "hatchling", "editables"}
    wheel = data["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == [f"src/{_meta.NAME}"]
    assert wheel["force-include"] == {
        "skills": f"{_meta.NAME}/_data/skills",
        "template": f"{_meta.NAME}/_data/template",
        "schemas": f"{_meta.NAME}/_data/schemas",
    }


def test_pytest_settings() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ini = data["tool"]["pytest"]["ini_options"]
    assert ini["pythonpath"] == ["src", "."]
    assert ini["testpaths"] == ["tests"]
    assert ini["addopts"] == "-ra --import-mode=importlib"
    assert ini["xfail_strict"] is True
    markers = {m.split(":", 1)[0] for m in ini["markers"]}
    assert markers == {"live", "model", "docker"}


def test_no_importlib_metadata_in_src() -> None:
    """REQ-ARCH-20: la versión sale de _meta, nunca de importlib.metadata."""
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [f"{node.module}.{a.name}" for a in node.names]
            else:
                continue
            if any(n == "importlib.metadata" or n.startswith("importlib.metadata.") for n in names):
                offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert offenders == []


def test_package_exposes_version() -> None:
    import aucove

    assert aucove.__version__ == _meta.__version__
