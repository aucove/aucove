# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""El cargador no da tipos de YAML 1.1 que cambian el valor sin aviso.

03-esquemas REQ-SCHEMA-3: solo ``true`` y ``false`` son booleanos. El cargador quitaba los
resolutores implícitos de YAML 1.1, pero con una etiqueta explícita (``!!bool yes``, o una
directiva ``%TAG`` que apunta a ``tag:yaml.org,2002:``) seguía dando booleanos, y los
enteros de YAML 1.1 cambiaban el valor sin aviso: ``014`` era 12 (octal), ``1:30`` era 90
(sexagesimal), ``0b101`` era 5, ``0x1F`` era 31 y ``1_000`` era 1000.
"""

from __future__ import annotations

import pytest

from aucove.config.project import parse_project
from aucove.config.yamlload import load_yaml
from tests.conftest import FIXTURES

PROJECT = (FIXTURES / "config" / "project" / "minimal.yaml").read_text(encoding="utf-8")


@pytest.mark.parametrize("text", ["014", "010", "1:30", "0b101", "0x1F", "1_000", "-0o7", "+0x1"])
def test_yaml11_integers_stay_text(text: str) -> None:
    data, findings = load_yaml(f"a: {text}\n", file="a.yaml")
    assert findings == []
    assert data == {"a": text}


@pytest.mark.parametrize("text", ["1_000.5", "1:30.0", ".inf", "-.inf", ".nan", ".NaN"])
def test_yaml11_floats_that_are_not_decimal_stay_text(text: str) -> None:
    data, findings = load_yaml(f"a: {text}\n", file="a.yaml")
    assert findings == []
    assert data == {"a": text}


@pytest.mark.parametrize(
    ("text", "value"),
    [("0", 0), ("12", 12), ("-3", -3), ("+4", 4), ("0.1", 0.1), ("1.5e+3", 1500.0), ("-2.25", -2.25)],
)
def test_decimal_numbers_keep_their_value(text: str, value: object) -> None:
    data, findings = load_yaml(f"a: {text}\n", file="a.yaml")
    assert findings == [] and data == {"a": value}
    assert type(data["a"]) is type(value)  # type: ignore[index]


@pytest.mark.parametrize(
    "doc",
    [
        "a: !!bool yes\n",
        "a: !!bool on\n",
        "a: !!bool tRuE\n",
        "a: !!int 014\n",
        "a: !!int 0x1F\n",
        "a: !!int 1:30\n",
        "a: !!float .inf\n",
        "a: !!float 1_0.5\n",
        "%TAG !e! tag:yaml.org,2002:\n---\na: !e!bool on\n",
        "%TAG !e! tag:yaml.org,2002:\n---\na: !e!int 010\n",
    ],
)
def test_explicit_yaml11_tags_are_rejected(doc: str) -> None:
    data, findings = load_yaml(doc, file="a.yaml")
    assert data is None
    assert [f.code for f in findings] == ["E_CONFIG_YAML"]
    assert findings[0].line is not None


@pytest.mark.parametrize(
    ("doc", "value"),
    [("a: !!bool true\n", True), ("a: !!bool FALSE\n", False), ("a: !!int 12\n", 12), ("a: !!float 1\n", 1.0),
     ("a: !!float 0.5\n", 0.5), ("a: !!str 014\n", "014")],
)
def test_explicit_tags_with_json_values_still_work(doc: str, value: object) -> None:
    data, findings = load_yaml(doc, file="a.yaml")
    assert findings == [] and data == {"a": value}


def test_octal_looking_limit_is_not_silently_12() -> None:
    text = PROJECT + "limits:\n  claim_expiry_days: 014\n"
    cfg, findings = parse_project(text, file="project.yaml")
    assert cfg is None
    assert "/limits/claim_expiry_days" in {f.pointer for f in findings}
