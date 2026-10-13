# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``pattern`` se evalúa con la semántica de ECMA-262 (JSON Schema).

En Python, ``$`` también coincide antes de un ``\\n`` final; en ECMA-262, que es la semántica
de ``pattern`` en JSON Schema 2020-12, no. Con un escalar en bloque (``|``), los campos con
patrón anclado aceptaban ``"metodos\\n"``, ``"ANA\\n"`` o ``"main\\n"``, y eso rompía la unicidad
de REQ-SCHEMA-26 y REQ-SCHEMA-27 y los nombres de rama de ``config.base``.
"""

from __future__ import annotations

import pytest

from aucove.config import base as base_mod
from aucove.config.project import parse_project
from aucove.config.roles import parse_roles
from aucove.config.schemas import validate
from tests.conftest import FIXTURES

PROJECT = (FIXTURES / "config" / "project" / "minimal.yaml").read_text(encoding="utf-8")

ROLES_NL = """\
version: 1
lead: ana
people:
  - login: ana
    name: "Ana"
    role: lead
    account_type: team
  - login: |
      ANA
    name: "Ana 2"
    role: researcher
    account_type: team
sections:
  - id: |
      metodos
    title: "Métodos"
    owner: ana
  - id: metodos
    title: "Métodos 2"
    owner: ana
"""


def test_roles_with_trailing_newlines_are_invalid() -> None:
    roles, findings = parse_roles(ROLES_NL, file="roles.yaml")
    assert roles is None
    pointers = {f.pointer for f in findings if f.code == "E_VALIDATION_SCHEMA"}
    assert {"/people/1/login", "/sections/0/id"} <= pointers, findings


@pytest.mark.parametrize(
    ("old", "new", "pointer"),
    [
        ("name: demo\n", "name: |\n  demo\n", "/name"),
        ("  repo: example-org/demo\n", "  repo: |\n    example-org/demo\n", "/forge/repo"),
        ("  repo: example-org/demo\n", "  repo: example-org/demo\n  base_branch: |\n    main\n", "/forge/base_branch"),
    ],
)
def test_project_with_trailing_newlines_is_invalid(old: str, new: str, pointer: str) -> None:
    assert old in PROJECT
    cfg, findings = parse_project(PROJECT.replace(old, new, 1), file="project.yaml")
    assert cfg is None
    assert pointer in {f.pointer for f in findings if f.code == "E_VALIDATION_SCHEMA"}, findings


def test_values_without_newline_still_match() -> None:
    cfg, findings = parse_project(PROJECT, file="project.yaml")
    assert cfg is not None, findings


def test_negated_patterns_also_use_ecma_semantics() -> None:
    """``not: {pattern: "\\\\.lock$"}``: con ECMA, ``x.lock\\n`` no coincide con el patrón negado."""
    from aucove.config.schemas import validator

    cls = type(validator("project"))
    v = cls({"not": {"pattern": r"\.lock$"}})
    assert v.is_valid("x.lock\n")
    assert not v.is_valid("x.lock")
    assert not cls({"pattern": "^a$"}).is_valid("a\n")
    assert cls({"pattern": "^a$"}).is_valid("a")
    assert cls({"pattern": "[$]"}).is_valid("$")  # '$' dentro de una clase es literal
    assert cls({"pattern": r"^\$$"}).is_valid("$")  # '\$' escapado es literal
    assert not cls({"pattern": r"^\$$"}).is_valid("$\n")


@pytest.mark.parametrize("name", ["main\n", "feature/x\n", "main\r"])
def test_branch_names_with_trailing_newline_are_rejected(name: str) -> None:
    assert base_mod._valid_branch(name) is False
    assert base_mod._valid_branch(name.strip()) is True


def test_schema_validate_reports_the_pointer() -> None:
    findings = validate("project", {"forge": {"base_branch": "main\n"}}, file="project.yaml")
    assert "/forge/base_branch" in {f.pointer for f in findings}
