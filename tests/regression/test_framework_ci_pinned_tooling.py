# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""El CI del framework no prueba con herramientas ni
dependencias sin fijar (10-seguridad REQ-SEC-44, REQ-SEC-47 y REQ-SEC-51).

Antes:

- ``astral-sh/setup-uv`` no fijaba ``version`` y el repo no declara ``required-version``:
  cada run usaba la última versión de uv.
- ``uv build --wheel`` y ``uv sync`` (que instala el proyecto editable) resolvían
  ``hatchling>=1.26`` en PyPI en un entorno de build aislado, sin el lock ni hashes.
- ``uv tool install --from <rueda>`` resolvía las dependencias de ejecución sin uv.lock:
  las pruebas e2e corrían contra versiones distintas de las bloqueadas, y una versión nueva
  dentro de los rangos entraba en CI sin el enfriamiento de Dependabot.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from collections.abc import Mapping
from typing import Any

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from aucove._meta import NAME
from tests.conftest import REPO_ROOT
from tests.unit.test_framework_workflows import CI, JOBS, load_doc, run_lines, steps, uses_steps


@pytest.fixture(scope="module")
def ci() -> dict[Any, Any]:
    return load_doc(CI)


Pos = tuple[int, int]  # (índice del paso, línea dentro de su run:)


def uv_commands(ci: Mapping[Any, Any], name: str) -> list[tuple[Pos, list[str]]]:
    """``(posición, argv)`` de cada línea de ``run:`` del job que invoca uv, en orden."""
    found: list[tuple[Pos, list[str]]] = []
    for i, step in enumerate(steps(ci, name)):
        for k, line in enumerate(run_lines(step)):
            if line.startswith("uv "):
                found.append(((i, k), shlex.split(line)))
    return found


def option(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def exports(ci: Mapping[Any, Any], name: str) -> dict[str, tuple[Pos, list[str]]]:
    """Archivo de salida -> (posición, argv) de cada ``uv export`` del job."""
    out: dict[str, tuple[Pos, list[str]]] = {}
    for i, argv in uv_commands(ci, name):
        if argv[:2] == ["uv", "export"]:
            target = option(argv, "--output-file")
            assert target is not None, argv
            out[target] = (i, argv)
    return out


# ---------------------------------------------------------------------------
# uv fijado
# ---------------------------------------------------------------------------


def test_setup_uv_pins_the_same_uv_version_in_every_job(ci: Mapping[Any, Any]) -> None:
    versions: set[str] = set()
    count = 0
    for name in sorted(JOBS):
        for step in uses_steps(ci, name, "astral-sh/setup-uv"):
            count += 1
            version = (step.get("with") or {}).get("version")
            assert isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version), (name, version)
            versions.add(version)
    assert count >= 4 and len(versions) == 1, versions


# ---------------------------------------------------------------------------
# Build con el backend del lock y lanzador con las versiones del lock
# ---------------------------------------------------------------------------


def test_every_uv_build_uses_hashed_build_constraints_exported_from_the_lock(ci: Mapping[Any, Any]) -> None:
    builds = 0
    for name in sorted(JOBS):
        produced = exports(ci, name)
        for i, argv in uv_commands(ci, name):
            if argv[:2] != ["uv", "build"]:
                continue
            builds += 1
            assert "--require-hashes" in argv, (name, argv)
            target = option(argv, "--build-constraints")
            assert target is not None and target in produced, (name, argv)
            j, export = produced[target]
            assert j < i, (name, "el export va antes del build")
            assert "--frozen" in export and "--no-emit-project" in export, export
            assert option(export, "--only-group") == "dev", export  # hatchling está en el grupo dev
            assert "--no-hashes" not in export, export
    assert builds == 2  # test y template-ci


def test_every_tool_install_uses_the_runtime_constraints_of_the_lock(ci: Mapping[Any, Any]) -> None:
    installs = 0
    for name in sorted(JOBS):
        produced = exports(ci, name)
        for i, argv in uv_commands(ci, name):
            if argv[:3] != ["uv", "tool", "install"]:
                continue
            installs += 1
            target = option(argv, "--constraints")
            assert target is not None and target in produced, (name, argv)
            j, export = produced[target]
            assert j < i, (name, "el export va antes del install")
            assert "--frozen" in export and "--no-dev" in export and "--no-emit-project" in export, export
            for flag in ("--only-group", "--group", "--all-groups", "--no-hashes"):
                assert flag not in export, export
            assert argv[-1] == NAME
            # El lanzador lleva el extra evidence (la autoprueba de D-37 sella y verifica) y las
            # versiones de ese extra también salen del lock: cada extra pedido se exporta.
            source = option(argv, "--from")
            m = re.fullmatch(r".*\[([a-z0-9,-]+)\]", source or "")
            assert m is not None and m.group(1).split(",") == ["evidence"], (name, argv)
            exported = {export[k + 1] for k, flag in enumerate(export) if flag == "--extra"}
            assert set(m.group(1).split(",")) <= exported, (name, export)
    assert installs == 2  # test y template-ci


def test_project_installs_are_built_with_the_locked_backend(ci: Mapping[Any, Any]) -> None:
    for name in sorted(JOBS):
        prepared: set[tuple[str, ...]] = set()
        for _, argv in uv_commands(ci, name):
            if argv[:2] != ["uv", "sync"]:
                continue
            assert "--locked" in argv, (name, argv)
            groups = tuple(sorted(argv[k + 1] for k, a in enumerate(argv) if a == "--group"))
            if "--no-install-project" in argv:
                prepared.add(groups)
                continue
            # el proyecto se construye con el hatchling que ya instaló el lock, con hashes
            assert option(argv, "--no-build-isolation-package") == NAME, (name, argv)
            assert groups in prepared, (name, "antes, el mismo sync con --no-install-project")


def test_uv_run_never_syncs(ci: Mapping[Any, Any]) -> None:
    # un uv run que sincroniza podría volver a construir el proyecto en un entorno aislado
    runs = 0
    for name in sorted(JOBS):
        for _, argv in uv_commands(ci, name):
            if argv[:2] == ["uv", "run"]:
                runs += 1
                assert "--no-sync" in argv, (name, argv)
    assert runs >= 5


def test_the_dev_group_of_the_lock_covers_the_build_backend() -> None:
    """Las restricciones de build salen del grupo dev: cubren ``[build-system].requires``."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = {canonicalize_name(p["name"]): p for p in lock["package"]}
    project = packages[canonicalize_name(NAME)]
    closure: set[str] = set()
    pending = [canonicalize_name(d["name"]) for d in project["dev-dependencies"]["dev"]]
    while pending:
        dep = pending.pop()
        if dep in closure:
            continue
        closure.add(dep)
        pending += [canonicalize_name(d["name"]) for d in packages[dep].get("dependencies", [])]
    requires = [Requirement(r) for r in pyproject["build-system"]["requires"]]
    assert requires
    for req in requires:
        key = canonicalize_name(req.name)
        assert key in closure, f"{req.name} no está en el grupo dev de uv.lock"
        assert Version(packages[key]["version"]) in req.specifier, (req, packages[key]["version"])
    for dep in closure:
        pkg = packages[dep]
        hashes = [w.get("hash") for w in pkg.get("wheels", [])] + [(pkg.get("sdist") or {}).get("hash")]
        assert any(h and h.startswith("sha256:") for h in hashes), dep
