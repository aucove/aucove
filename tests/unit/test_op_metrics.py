# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove metrics`` (05 §9.9; 12-mvp §5.2 y §5.3) contra FakeForge y la base local.

El mundo es el de ``tests.board_world`` (roles ``basic``: ana responsable, bruno suplente, carla
y dario investigadores). Los commits del repositorio local llevan la fecha fija de la identidad de
las pruebas (29/09, ``GIT_IDENTITY``), anterior a ``NOW``: los eventos de ``roles.yaml`` entran
sin ``until``.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aucove import _meta
from aucove.config.schemas import validate_result
from aucove.domain.taskbody import render_task_body
from aucove.forge.base import ChangedFile, LabelEvent
from aucove.forge.fake import Fault
from aucove.i18n import translator
from aucove.ops import metrics
from aucove.ops.registry import REGISTRY
from aucove.ops.result import Result
from tests.board_world import CI, NOW, World, at, new_world
from tests.conftest import roles_fixture

SALT = "0f" * 32
ENV = {metrics.SALT_ENV: SALT}
CARLA, ANA, BRUNO = "P-42cc034608", "P-6f28c556e3", "P-61002779d9"  # HMAC-SHA256 con la sal 0f…0f


def valid(result: Result) -> Result:
    envelope = result.to_dict()
    assert validate_result(envelope) == [], json.dumps(envelope, ensure_ascii=False, indent=1)
    return result


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def export(w: World, tmp_path: Path, *, table: str = "tasks", env: dict[str, str] | None = None,
           **kwargs: object) -> tuple[Result, list[dict[str, str]]]:
    out = tmp_path / f"{table}.csv"
    result = valid(w.run("metrics", env=ENV if env is None else env, table=table, output=str(out), **kwargs))
    assert result.ok, result.error
    text = out.read_text(encoding="utf-8")
    assert text.splitlines()[0] == ",".join(metrics.TASK_COLUMNS if table == "tasks" else metrics.EVENT_COLUMNS)
    return result, list(csv.DictReader(io.StringIO(text)))


def values(result: Result) -> dict[str, object]:
    return {m["id"]: m["value"] for m in result.data["metrics"]}  # type: ignore[index]


def merged_task(w: World, *, number: int = 12, pr: int = 20, author: str = "carla", sections: Sequence[str] = ("methods",),
                reviews: Sequence[tuple[str, str, datetime]] = (), merged_at: datetime | None = None,
                body: str = "") -> str:
    """Una tarea reclamada con su PR integrado; devuelve la cabeza del PR."""
    head = w.task(number, claimant=author, sections=sections)["head"]
    assert head is not None
    w.forge.seed_pull_request(files=[ChangedFile(f"sections/{sections[0]}/x.tex", "added")], number=pr,
                              head_branch=f"task/{number}", head_sha=head, author=author, created_at=at(-200),
                              state="closed", merged=True, merged_at=merged_at or at(-50), updated_at=at(-50),
                              body=body, merged_by="ana")
    for reviewer, state, when in reviews:
        w.forge.seed_review(pr, author=reviewer, state=state, commit_sha=head, submitted_at=when)  # type: ignore[arg-type]
    return head


def bare_task(w: World, number: int, events: Sequence[tuple[str, str, str, datetime]], *,
              labels: Sequence[str] = ("task",), created: datetime | None = None) -> None:
    """Un Issue de tarea sin reclamo, con los eventos de etiquetas ``(evento, etiqueta, actor, fecha)``."""
    body = render_task_body({"summary": "Resumen.", "sections": ["methods"], "plan_refs": [], "done": ["Hecho."]}, "es")
    seeded = [LabelEvent(100 + i, ev, label, actor, when) for i, (ev, label, actor, when) in enumerate(events)]  # type: ignore[arg-type]
    w.forge.seed_issue(number=number, title="Tarea", body=body, labels=list(labels), author="carla",
                       created_at=created or at(-600), updated_at=at(-500), events=seeded)


@pytest.fixture
def w(tmp_path: Path) -> World:
    return new_world(tmp_path / "world")


def test_it_is_registered_without_mcp() -> None:
    spec = next(s for s in REGISTRY if s.id == "metrics")
    assert (spec.cli, spec.mcp_tool, spec.effects, spec.offline) == (("metrics",), None, "none", "online")
    assert [p.name for p in spec.params] == ["since", "until", "table", "output", "no_pseudonymize"]


def test_without_salt_it_fails_before_reading(w: World) -> None:
    """REQ-MVP-54: sin la sal y sin --no-pseudonymize, E_CONFIG_INVALID con un hint."""
    result = valid(w.run("metrics", env={}))
    assert result.error is not None and result.error.code == "E_CONFIG_INVALID"
    assert result.error.details == {"variable": metrics.SALT_ENV, "reason": "missing"}
    assert metrics.SALT_ENV in result.error.message and "--no-pseudonymize" in (result.error.hint or "")
    assert len(w.forge.calls) == 0


@pytest.mark.parametrize("salt", ["ab" * 31, "a" * 65, "zz" * 32])
def test_a_short_or_malformed_salt_is_invalid(w: World, salt: str) -> None:
    result = w.run("metrics", env={metrics.SALT_ENV: salt})
    assert result.error is not None and result.error.code == "E_CONFIG_INVALID"
    assert result.error.details == {"variable": metrics.SALT_ENV, "reason": "invalid"}


def test_pseudonyms_are_the_hmac_of_the_lowercase_login() -> None:
    key = bytes.fromhex(SALT)
    assert metrics.pseudonym(key, "carla") == metrics.pseudonym(key, "Carla") == CARLA
    assert (metrics.pseudonym(key, "ana"), metrics.pseudonym(key, "bruno")) == (ANA, BRUNO)


def test_a_merged_task_row(w: World, tmp_path: Path) -> None:
    """12-mvp §5.3: las columnas de una tarea aceptada, reclamada, revisada e integrada."""
    marker = f"<!-- {_meta.NAME}:metrics v1 hook_denials=3 -->"
    merged_task(w, body=f"Resumen.\n\n{marker}\n", reviews=[("bruno", "CHANGES_REQUESTED", at(-150)),
                                                         ("bruno", "APPROVED", at(-100))])
    result, rows = export(w, tmp_path)
    (row,) = rows
    assert row == {
        "schema_version": "1", "repo": "example-org/demo", "profile": "enforced", "task": "12", "state": "merged",
        "sections": "methods", "proposed_by": CARLA, "created_at": iso(at(-600)), "accepted_at": iso(at(-590)),
        "claimant": CARLA, "claimed_at": iso(at(-399)), "claims_count": "1", "expired_count": "0", "pr": "20",
        "pr_created_at": iso(at(-200)), "submitted_at": iso(at(-200)), "first_review_at": iso(at(-150)),
        "first_reviewer_role": "deputy", "changes_requested_count": "1", "approved_at": iso(at(-100)),
        "merged_at": iso(at(-50)), "merged_without_approval": "false", "cancelled_at": "", "flag_overlap": "false",
        "overlap_outcome": "", "flag_direction_change": "false", "hook_denials": "3", "t_delivery_h": "5.82",
        "t_review_h": "0.83",
    }
    got = values(result)
    assert (got["M1"], got["M2_median"], got["M2_p90"], got["M3"], got["M8"], got["M11"]) == (5.82, 0.83, 0.83, 1.0, 3.0, 0)
    assert result.data["since"] is None and result.data["until"] == iso(NOW)  # type: ignore[index]


def test_a_merge_without_a_valid_approval(w: World, tmp_path: Path) -> None:
    """M11: sin aprobación, o con una posterior a la integración, cuenta."""
    merged_task(w, reviews=[("bruno", "APPROVED", at(-10))])  # después de integrar (at(-50))
    result, (row,) = export(w, tmp_path)
    assert (row["merged_without_approval"], row["approved_at"], row["first_review_at"]) == ("true", "", iso(at(-10)))
    assert values(result)["M11"] == 1


def test_the_lead_without_a_deputy_is_a_registered_bypass(tmp_path: Path) -> None:
    """06 §3.4: el PR del lead sin suplente no cuenta como integrado sin aprobación."""
    w = new_world(tmp_path / "world", extra_files={"roles.yaml": roles_fixture("no-deputy")})
    merged_task(w, author="ana", sections=("introduction",))
    result, (row,) = export(w, tmp_path)
    assert row["merged_without_approval"] == "false" and values(result)["M11"] == 0


def test_a_proposed_task_has_empty_cells(w: World, tmp_path: Path) -> None:
    w.task(30, accepted=False)
    _result, (row,) = export(w, tmp_path)
    assert (row["state"], row["accepted_at"], row["claimant"], row["claims_count"], row["pr"]) == ("proposed", "", "", "0", "")
    assert row["merged_without_approval"] == row["changes_requested_count"] == row["t_delivery_h"] == ""


def test_overlap_outcomes(w: World, tmp_path: Path) -> None:
    """M6 y M7: confirmados, no detectados, pendientes y descartados."""
    bare_task(w, 41, [("labeled", "flag:overlap", CI, at(-500)), ("labeled", "ack:overlap", "ana", at(-400))],
              labels=("task", "flag:overlap", "ack:overlap"))
    bare_task(w, 42, [("labeled", "ack:overlap", "ana", at(-400))], labels=("task", "ack:overlap"))
    bare_task(w, 43, [("labeled", "flag:overlap", CI, at(-500))], labels=("task", "flag:overlap"))
    bare_task(w, 44, [("labeled", "flag:overlap", CI, at(-500)), ("unlabeled", "flag:overlap", "ana", at(-400))])
    bare_task(w, 45, [("labeled", "flag:direction-change", "carla", at(-500))])
    result, rows = export(w, tmp_path)
    got = {int(r["task"]): (r["flag_overlap"], r["overlap_outcome"], r["flag_direction_change"]) for r in rows}
    assert got == {41: ("true", "confirmed", "false"), 42: ("false", "missed", "false"),
                   43: ("true", "pending", "false"), 44: ("true", "dismissed", "false"), 45: ("false", "", "true")}
    m = values(result)
    assert (m["M6"], m["M6_missed"], m["M7"]) == (0.5, 1, 2)


def test_the_events_of_a_task(w: World, tmp_path: Path) -> None:
    merged_task(w, reviews=[("bruno", "CHANGES_REQUESTED", at(-150)), ("bruno", "APPROVED", at(-100))])
    _result, rows = export(w, tmp_path, table="events")
    mine = [(r["event"], r["actor"], r["actor_role"], r["pr"], r["label"]) for r in rows if r["task"] == "12"]
    assert mine == [
        ("label_added", CARLA, "researcher", "", "state:proposed"),
        ("label_added", CARLA, "researcher", "", "task"),
        ("task_created", CARLA, "researcher", "", ""),
        ("label_removed", ANA, "lead", "", "state:proposed"),
        ("task_accepted", ANA, "lead", "", ""),
        ("commit", CARLA, "researcher", "20", ""),
        ("claimed", CARLA, "researcher", "", ""),
        ("commit", CARLA, "researcher", "20", ""),
        ("pr_opened", CARLA, "researcher", "20", ""),
        ("submitted", CARLA, "researcher", "20", ""),
        ("review_changes_requested", BRUNO, "deputy", "20", ""),
        ("review_approved", BRUNO, "deputy", "20", ""),
        ("merged", ANA, "lead", "20", ""),
    ]
    assert [r["occurred_at"] for r in rows] == sorted(r["occurred_at"] for r in rows)
    added = {r["actor"]: r["actor_role"] for r in rows if r["event"] == "role_added"}
    assert added == {ANA: "lead", BRUNO: "deputy", CARLA: "researcher", metrics.pseudonym(bytes.fromhex(SALT), "dario"): "researcher"}


def test_role_changes_in_the_history_of_the_base(w: World, tmp_path: Path) -> None:
    """role_added y role_removed salen de las versiones de roles.yaml en la base."""
    basic = roles_fixture("basic")
    extra = basic.replace("sections:\n", "  - login: elena\n    name: \"Elena Ruiz\"\n    role: researcher\n"
                                         "    account_type: none\nsections:\n")
    w.repo.commit("suma a elena", files={"roles.yaml": extra})
    w.repo.commit("roles roto", files={"roles.yaml": "version: 1\npeople: 3\n"})
    w.repo.commit("quita a elena", files={"roles.yaml": basic})
    w.repo.push("main")
    w.repo.fetch()
    _result, rows = export(w, tmp_path, table="events", env={}, no_pseudonymize=True)
    roles = [(r["event"], r["actor"]) for r in rows if r["event"].startswith("role_")]
    # el roles.yaml roto no cambia quién está; en el mismo segundo, role_added va antes
    assert roles == [("role_added", x) for x in ("ana", "bruno", "carla", "dario", "elena")] + [("role_removed", "elena")]


def test_broken_base_issues(w: World, tmp_path: Path) -> None:
    """M12: los Issues main-broken, con su número en detail."""
    w.forge.seed_issue(number=70, title="La base no compila", body="x", labels=["main-broken"], author=CI,
                       created_at=at(-300), closed_at=at(-240), state="closed", state_reason="completed")
    result, rows = export(w, tmp_path, table="events")
    broken = [(r["event"], r["actor"], r["detail"]) for r in rows if r["event"].startswith("main_broken")]
    key = bytes.fromhex(SALT)
    assert broken == [("main_broken_opened", metrics.pseudonym(key, CI), "70"), ("main_broken_closed", "", "70")]
    m = values(result)
    assert (m["M12_count"], m["M12_median"]) == (1, 1.0)


def test_since_and_until_select_tasks_and_events(w: World, tmp_path: Path) -> None:
    bare_task(w, 50, [], created=NOW - timedelta(days=3))
    bare_task(w, 51, [])
    result, rows = export(w, tmp_path, since="2026-10-08")
    assert [r["task"] for r in rows] == ["51"]
    assert (result.data["since"], result.data["until"]) == ("2026-10-08T00:00:00Z", iso(NOW))  # type: ignore[index]
    result, rows = export(w, tmp_path, until="2026-10-07")
    assert [r["task"] for r in rows] == ["50"] and result.data["until"] == "2026-10-07T23:59:59Z"  # type: ignore[index]
    _result, events = export(w, tmp_path, table="events", since="2026-10-08", until="2026-10-08")
    assert {r["task"] for r in events if r["task"]} == {"51"}


@pytest.mark.parametrize(("kwargs", "param", "reason"), [
    ({"since": "08/10/2026"}, "since", "pattern"),
    ({"until": "2026-02-30"}, "until", "range"),
    ({"since": "2026-10-09", "until": "2026-10-08"}, "until", "range"),
    ({"table": "people"}, "table", "enum"),
    ({"output": ""}, "output", "type"),
])
def test_input_errors(w: World, kwargs: dict[str, object], param: str, reason: str) -> None:
    result = valid(w.run("metrics", env=ENV, **kwargs))
    assert result.error is not None and result.error.code == "E_VALIDATION_INPUT"
    assert result.error.details == {"errors": [{"param": param, "reason": reason}]}


def test_without_pseudonyms_the_logins_go_as_they_are(w: World, tmp_path: Path) -> None:
    merged_task(w)
    _result, (row,) = export(w, tmp_path, env={}, no_pseudonymize=True)
    assert (row["proposed_by"], row["claimant"]) == ("carla", "carla")


def test_dry_run_and_no_output_write_nothing(w: World, tmp_path: Path) -> None:
    out = tmp_path / "tasks.csv"
    assert valid(w.run("metrics", env=ENV, output=str(out), dry_run=True)).ok
    assert valid(w.run("metrics", env=ENV)).ok
    assert not out.exists()


def test_an_unwritable_output_is_an_input_error(w: World, tmp_path: Path) -> None:
    result = w.run("metrics", env=ENV, output=str(tmp_path))  # una carpeta
    assert result.error is not None and result.error.code == "E_VALIDATION_INPUT"
    assert result.error.details == {"errors": [{"param": "output", "reason": "unwritable"}]}


def test_without_network_it_fails(w: World) -> None:
    w.forge.faults.add(Fault("*", "unreachable"))
    result = valid(w.run("metrics", env=ENV))
    assert result.error is not None and result.error.code == "E_NET_OFFLINE"


def test_the_text_summary(w: World) -> None:
    merged_task(w, reviews=[("bruno", "APPROVED", at(-100))])
    result = w.run("metrics", env=ENV, since="2026-10-01")
    text = metrics.render_text(result, translator("es"))
    lines = text.splitlines()
    assert lines[0].startswith("Métricas del 2026-10-01T00:00:00Z") and len(lines) == 1 + len(result.data["metrics"])  # type: ignore[index]
    assert lines[1] == "  M1 tiempo de entrega, mediana: 5.82 h"
    assert "  M11 integraciones sin aprobación válida: 0" in lines
