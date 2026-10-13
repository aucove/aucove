# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Los pasos de cada operación de varios pasos son los de la tabla de 05 §9.1, en su orden."""

from __future__ import annotations

import importlib
import re

import pytest

from aucove import _meta
from tests.conftest import REPO_ROOT

# operación -> (módulo, atributo con los pasos de la ruta principal, atributo de --escalate)
IMPLEMENTED = {
    "task.propose": ("task_propose", "STEPS", None),
    "claim": ("claim", "STEPS", None),
    "release": ("release", "STEPS", None),
    "save": ("save", "STEPS", None),
    "update": ("update", "STEPS", "ABORT_STEPS"),
    "switch": ("switch", "STEPS", None),
    "record-assessment": ("record_assessment", "STEPS", None),
    "submit": ("submit", "STEPS", None),
}


def _table() -> dict[str, tuple[list[str], list[str]]]:
    text = (REPO_ROOT / "spec" / "05-operaciones.md").read_text(encoding="utf-8")
    start = text.index("### 9.1 Resumen")
    end = text.index("### 9.2", start)
    rows: dict[str, tuple[list[str], list[str]]] = {}
    for line in text[start:end].splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 5 or not cells[0].startswith("`"):
            continue
        op = cells[0].split("`")[1]
        steps = re.sub(r"\s*\([^)]*\)", "", cells[4].replace("**", ""))  # sin notas: «(edita en su sitio)»
        main, _, extra = steps.partition(";")
        listed = [s.strip() for s in main.split(",") if s.strip() and s.strip() != "—"]
        alt = [s.strip() for s in extra.split(":", 1)[1].split(",")] if ":" in extra else []
        rows[op] = (listed, alt)
    return rows


@pytest.mark.parametrize("op", sorted(IMPLEMENTED))
def test_steps_follow_the_summary_table(op: str) -> None:
    module_name, main_attr, alt_attr = IMPLEMENTED[op]
    module = importlib.import_module(f"{_meta.NAME}.ops.{module_name}")
    listed, alt = _table()[op]
    assert list(getattr(module, main_attr)) == listed
    assert (list(getattr(module, alt_attr)) if alt_attr else []) == alt
    assert all(re.fullmatch(r"[a-z][a-z0-9_]*", s) for s in listed + alt)
