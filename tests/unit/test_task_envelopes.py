# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Los sobres de las operaciones del ciclo de tareas validan contra ``result.schema.json``
(REQ-OPS-5), con éxito, con ``failure_data`` y con ``--dry-run``."""

from __future__ import annotations

import json
from pathlib import Path

from aucove.config.schemas import validate_result
from aucove.ops.result import Result
from tests.submit_world import LEAD
from tests.submit_world import claimed as template_claimed
from tests.task_world import add_claim, claimed

AI = {"ai_use": ["drafting"], "ai_tool": "claude-code", "ai_model": "claude-opus-5-5"}


def valid(result: Result) -> Result:
    envelope = result.to_dict()
    assert validate_result(envelope) == [], json.dumps(envelope, ensure_ascii=False, indent=1)
    return result


def test_save_update_switch_release_envelopes(tmp_path: Path) -> None:
    w = claimed(tmp_path)
    w.task(12, claimant="carla", sections=("methods",))
    add_claim(w, 12, checkout=False)
    methods = w.repo.path / "sections/methods/methods.tex"
    methods.write_text("% cambio\n", encoding="utf-8")
    assert valid(w.run("save", message="Cambio", dry_run=True)).ok
    assert valid(w.run("save", message="Cambio", **AI)).ok
    assert not valid(w.run("save", message="x", login="bruno")).ok  # failure_data
    assert valid(w.run("update")).ok
    assert valid(w.run("update", abort=True)).ok
    assert valid(w.run("switch", task=12)).ok
    assert valid(w.run("switch", task=11, dry_run=True)).ok
    assert valid(w.run("release", task=12)).ok
    assert valid(w.run("release", task=12)).ok  # ya open: changed false
    assert not valid(w.run("release", task=11, login="bruno")).ok


def test_record_assessment_envelopes(tmp_path: Path) -> None:
    w = claimed(tmp_path)
    w.task(12, claimant="bruno", sections=("results",))
    overlap = {"target": {"task": 12}, "kind": "scope", "reason": "Las dos tocan lo mismo."}
    assert valid(w.run("record-assessment", task=11, phase="claim", verdict="possible", overlaps=[overlap])).ok
    assert valid(w.run("record-assessment", task=11, phase="claim", verdict="none", dry_run=True)).ok
    assert not valid(w.run("record-assessment", task=11, phase="claim", verdict="none", login="bruno")).ok


def test_submit_envelopes(tmp_path: Path) -> None:
    w = template_claimed(tmp_path)
    intro = w.repo.path / "sections/introduction/introduction.tex"
    intro.write_text(intro.read_text(encoding="utf-8") + "\nUna oración.\n", encoding="utf-8")
    assert w.run("save", login=LEAD, message="Oración", **AI).ok
    args = {"summary": "Resumen.", "note": {"done": "- Hecho."}, "compile": False, **AI}
    assert not valid(w.run("submit", login=LEAD, **args)).ok  # sin sign_off
    assert valid(w.run("submit", login=LEAD, sign_off=True, dry_run=True, **args)).ok
    assert valid(w.run("submit", login=LEAD, sign_off=True, **args)).ok


def test_accept_cancel_direction_and_decision_envelopes(tmp_path: Path) -> None:
    from tests.board_world import human, new_world

    w = new_world(tmp_path / "board")
    w.task(5, accepted=False)
    w.task(6, claimant="carla", pr=40)
    assert valid(human(w, "task.accept", task=5, dry_run=True)).ok
    assert valid(human(w, "task.accept", task=5)).ok
    assert valid(human(w, "task.accept", task=5)).ok  # changed: false
    assert not valid(human(w, "task.accept", task=6)).ok
    assert valid(human(w, "task.cancel", task=6, dry_run=True)).ok
    assert valid(human(w, "task.cancel", task=6, reason="No sigue")).ok
    assert valid(human(w, "task.cancel", task=6)).ok  # ya cancelada
    direction = {"title": "Agregar RQ-3", "kind": "add", "change": "Una pregunta nueva.", "rationale": "Hace falta."}
    assert valid(w.run("direction.propose", **direction)).ok
    assert valid(w.run("direction.propose", dry_run=True, **{**direction, "title": "Otra"})).ok
    assert not valid(w.run("direction.propose", **{**direction, "plan_refs": ["RQ-9"]})).ok
    c = claimed(tmp_path / "task")
    record = {"title": "Una decisión", "summary": "Resumen.", "context": "Contexto.", "decision": "Decisión.",
              "consequences": "Consecuencias."}
    assert valid(c.run("decision.propose", dry_run=True, **record)).ok
    assert valid(c.run("decision.propose", **record)).ok
    assert not valid(c.run("decision.propose", **{**record, "decision": "Otra."})).ok  # failure_data
