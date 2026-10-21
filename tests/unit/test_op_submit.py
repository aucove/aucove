# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``submit`` (05 §9.18; 04-tareas §7): nota, traza, comprobaciones locales, PR y rondas."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aucove import _meta
from aucove.config.yamlload import load_yaml
from aucove.ops.snapshot import parse_markers
from tests.board_world import World, at
from tests.submit_world import LEAD, RESEARCHER, change_roles, claimed, only_the_lead, researcher_owns_results
from tests.task_world import remote_head

P = _meta.ENV_PREFIX
INTRO = "sections/introduction/introduction.tex"
RESULTS = "sections/results/results.tex"
NOTE = "memory/notes/task-11.md"
AI = {"ai_use": ["drafting"], "ai_tool": "claude-code", "ai_model": "claude-opus-5-5"}


def _edit(world: World, path: str, line: str) -> None:
    target = world.repo.path / path
    target.write_text(target.read_text(encoding="utf-8") + f"\n{line}\n", encoding="utf-8")


def _submit(world: World, login: str = LEAD, **kwargs: Any):  # type: ignore[no-untyped-def]
    args: dict[str, Any] = {"summary": "Redacté la introducción.", "note": {"done": "- Redacté la introducción."},
                            "sign_off": True, "compile": False, **AI}
    args.update(kwargs)
    return world.run("submit", login=login, **args)


def _meta_block(body: str) -> dict[str, Any]:
    begin, end = f"<!-- {_meta.NAME}:pr-meta-begin v1 -->", f"<!-- {_meta.NAME}:pr-meta-end v1 -->"
    inner = body.split(begin, 1)[1].split(end, 1)[0].strip().removeprefix("```yaml").removesuffix("```")
    data, problems = load_yaml(inner, file="pr-meta")
    assert not problems and isinstance(data, dict)
    return data


@pytest.fixture
def world(tmp_path: Path) -> World:
    w = claimed(tmp_path)
    _edit(w, INTRO, "Una oración nueva.")
    assert w.run("save", login=LEAD, message="Agrega una oración", **AI).ok
    return w


def test_submit_opens_the_pr_with_note_trace_and_trailers(world: World) -> None:
    result = _submit(world)
    assert result.ok, result.error
    data = result.data
    assert (data["round"], data["state"], data["update_result"]) == (1, "in_review", "up_to_date")  # type: ignore[index]
    assert data["pr"]["created"] is True and data["next"] == ["record-assessment"]  # type: ignore[index]
    checks = {c["check"]: c["status"] for c in data["checks"]}  # type: ignore[index]
    assert checks["scope"] == checks["schemas"] == checks["traceability"] == "pass"
    assert checks["local-build"] == "skip"  # --no-compile
    assert data["context"]["phase"] == "submission" and data["context"]["diff_summary"]["files"] == 2  # type: ignore[index]
    pr = world.forge.get_pull_request(data["pr"]["number"])  # type: ignore[index]
    assert pr is not None and pr.draft is False and "Closes #11" in pr.body
    assert pr.body.endswith("Task: #11\nSection: introduction\nAI-Use: drafting\n"
                            "Assisted-by: claude-code:claude-opus-5-5\n"
                            "Signed-off-by: Example Lead <1001+example-lead@users.noreply.github.com>")
    meta = _meta_block(pr.body)
    assert (meta["task"], meta["sections"], meta["decisions"]) == (11, ["introduction"], [])
    trace = meta["trace"]
    assert trace["ai_use"] == ["drafting"] and trace["ai_use_by_section"] == {"introduction": ["drafting"]}
    assert trace["sign_off"]["channel"] == "mcp" and trace["sign_off"]["attestation"] == "attest-v1"
    assert trace["commits"]["head"] == world.repo.head() == remote_head(world.repo, "task/11")
    note = (world.repo.path / NOTE).read_text(encoding="utf-8")
    assert note.startswith("---\ntask: 11\ncreated: ") and "ai_use: [drafting]" in note
    assert 'assisted_by: ["claude-code:claude-opus-5-5"]' in note  # REQ-TRACE-21: lo mismo que el trace
    marker_line = next(line for line in note.splitlines() if line.startswith(f"<!-- {_meta.NAME}:session "))
    block = json.loads(marker_line.split(":session ", 1)[1].removesuffix(" -->"))
    assert (block["n"], block["login"], block["door"], block["model"]) == (1, LEAD, "manual", "claude-opus-5-5")
    comment = next(c for c in world.forge.list_comments(pr.number) if "submission v1" in c.body)
    (rounds,) = [d["rounds"] for k, d in parse_markers(comment.body) if k == "submission"]
    assert [r["round"] for r in rounds] == [1] and rounds[0]["note_path"] == NOTE
    codes = [w.code for w in result.warnings]
    assert "W_PERM_DEPUTY_MISSING" in codes and data["reviewers_requested"] == []  # type: ignore[index]


def test_in_individual_mode_there_is_no_deputy_warning(tmp_path: Path) -> None:
    """00-marco D-57: sin suplente ni bypass. El aviso decía que los PR del responsable necesitaban un bypass,
    y el agente de una sesión de prueba (20/10) lo repitió."""
    w = claimed(tmp_path, patch_roles=only_the_lead, patch_project=lambda text: text + "mode: individual\n")
    _edit(w, INTRO, "Una oración nueva.")
    assert w.run("save", login=LEAD, message="Agrega una oración", **AI).ok
    result = _submit(w)
    assert result.ok, result.error
    assert "W_PERM_DEPUTY_MISSING" not in [x.code for x in result.warnings]
    assert result.data["reviewers_requested"] == []  # type: ignore[index]


@pytest.mark.parametrize("compile_flag", [None, True])
def test_a_latexmk_that_does_not_run_counts_as_missing(world: World, monkeypatch: pytest.MonkeyPatch,
                                                       compile_flag: bool | None) -> None:
    """05 §9.18 paso 5: el latexmk de MiKTeX sin Perl existe pero no corre. Con ``if_available``, el de la
    plantilla, la entrega lo salta; antes se detenía en ``local-build`` (sesión de prueba del 20/10)."""
    from aucove.ops import submit as submit_op

    calls: list[list[str]] = []

    def no_perl(cmd: list[str], **_: object) -> SimpleNamespace:
        calls.append(list(cmd))
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"MiKTeX could not find the script engine 'perl'")

    monkeypatch.setattr(submit_op, "which_outside", lambda *_a, **_k: "latexmk")
    monkeypatch.setattr(submit_op, "run_bounded", no_perl)
    args: dict[str, Any] = {"summary": "Redacté la introducción.", "note": {"done": "- Redacté la introducción."},
                            "sign_off": True, **AI}
    if compile_flag is not None:
        args["compile"] = compile_flag  # always
    result = world.run("submit", login=LEAD, **args)
    assert calls == [["latexmk", "-v"]]  # no intentó compilar
    if compile_flag is None:
        assert result.ok, result.error
        checks = {c["check"]: c["status"] for c in result.data["checks"]}  # type: ignore[index]
        assert checks["local-build"] == "skip"
    else:
        assert result.error is not None and result.error.code == "E_VALIDATION_FAILED"


def test_repeating_the_same_submission_changes_nothing(world: World) -> None:
    first = _submit(world)
    assert first.ok
    head = world.repo.head()
    again = _submit(world)
    assert again.ok and again.data["pr"]["created"] is False and again.data["round"] == 1  # type: ignore[index]
    assert world.repo.head() == head and again.data["commit"] is None  # type: ignore[index]
    note = (world.repo.path / NOTE).read_text(encoding="utf-8")
    assert note.count(f"<!-- {_meta.NAME}:session ") == 1  # la misma nota no se duplica
    assert len(world.forge.list_pull_requests(state="all")) == 1


def test_a_new_round_needs_a_new_block(world: World) -> None:
    assert _submit(world).ok
    _edit(world, INTRO, "Otra oración.")
    assert world.run("save", login=LEAD, message="Otra", **AI).ok
    missing = _submit(world, note=None)
    assert missing.error.code == "E_VALIDATION_NOTE_MISSING"  # type: ignore[union-attr]
    assert missing.data["failed_step"] == "write_note"  # type: ignore[index]
    second = _submit(world, note={"done": "- Agregué otra oración."})
    assert second.ok and second.data["round"] == 2  # type: ignore[index]


def test_sign_off_is_required_before_any_change(world: World) -> None:
    head = world.repo.head()
    result = _submit(world, sign_off=False)
    assert result.error.code == "E_VALIDATION_SIGNOFF_MISSING"  # type: ignore[union-attr]
    assert result.data["failed_step"] == "preflight"  # type: ignore[index]
    assert world.repo.head() == head and not (world.repo.path / NOTE).exists()


def test_the_declaration_must_cover_the_commits(world: World) -> None:
    """REQ-TRACE-19 y REQ-TRACE-20: commits sin declarar exigen ai_use; omitir un uso es conflicto."""
    _edit(world, INTRO, "Sin declarar.")
    assert world.run("save", login=LEAD, message="Sin declarar").ok  # AI-Use: undeclared
    undeclared = _submit(world, ai_use=None, ai_tool=None, ai_model=None)
    assert undeclared.error.code == "E_VALIDATION_AI_USE_UNDECLARED"  # type: ignore[union-attr]
    conflict = _submit(world, ai_use=["editing"])
    assert conflict.error.code == "E_VALIDATION_AI_USE_CONFLICT"  # type: ignore[union-attr]
    assert conflict.error.details == {"missing": ["drafting"]}  # type: ignore[union-attr]
    no_tool = _submit(world, ai_use=["drafting"], ai_tool=None, ai_model=None)
    assert no_tool.ok  # el commit anterior ya trae Assisted-by
    assert not (world.repo.path / NOTE).read_text(encoding="utf-8").count("undeclared")


def test_the_lead_submits_regenerated_files(tmp_path: Path) -> None:
    # CODEOWNERS y task.yml son generated: en el diff de la tarea valen para el lead si coinciden
    # con su regeneración, igual que en save (06 §2.2)
    w = claimed(tmp_path)
    change_roles(w.repo.path, researcher_owns_results)
    assert w.run("save", login=LEAD, message="Reparte results", **AI).ok
    result = _submit(w, summary="Reparto results.", note={"done": "- Reparto results."})
    assert result.ok, result.error
    diff = w.repo.git("diff", "--name-only", "origin/main...HEAD").splitlines()
    assert {"roles.yaml", ".github/CODEOWNERS"} <= set(diff)
    (w.repo.path / ".github/CODEOWNERS").write_text("* @example-lead\n", encoding="utf-8")  # ya no coincide
    denied = w.run("save", login=LEAD, message="Edita CODEOWNERS a mano", **AI).error
    assert denied is not None and denied.code == "E_PERM_PATH_DENIED"


def test_a_draft_pr_is_not_turned_into_ready(world: World) -> None:
    head = world.forge.get_ref("task/11").sha  # type: ignore[union-attr]
    world.forge.seed_pull_request(head_branch="task/11", head_sha=head, author=LEAD, draft=True, number=40)
    result = _submit(world)
    assert result.error.code == "E_STATE_PR_DRAFT"  # type: ignore[union-attr]
    assert result.data["failed_step"] == "find_or_create_pr"  # type: ignore[index]


def test_offline_commits_locally_and_stops(world: World) -> None:
    result = _submit(world, env={P + "OFFLINE": "1"})
    assert result.error.code == "E_NET_OFFLINE"  # type: ignore[union-attr]
    data = result.data
    assert data["failed_step"] == "update"  # type: ignore[index]
    assert {s["id"]: s["status"] for s in data["steps"]}["save"] == "done"  # type: ignore[index]
    assert "memory/notes/task-11.md" in world.repo.git("show", "--name-only", "--format=", "HEAD")
    assert remote_head(world.repo, "task/11") != world.repo.head()  # el commit quedó en local
    resumed = _submit(world)
    assert resumed.ok and remote_head(world.repo, "task/11") == world.repo.head()


def test_main_broken_skips_the_update(world: World) -> None:
    world.forge.seed_issue(number=90, title="La base está rota", body="", labels=["main-broken"], author=LEAD)
    result = _submit(world)
    assert result.ok and result.data["update_result"] == "skipped"  # type: ignore[index]
    assert "W_STATE_MAIN_BROKEN" in [w.code for w in result.warnings]


def test_a_researcher_asks_the_lead_and_again_after_changes(tmp_path: Path) -> None:
    w = claimed(tmp_path, login=RESEARCHER, sections=("results",), patch_roles=researcher_owns_results)
    _edit(w, RESULTS, "Una tabla nueva.")
    assert w.run("save", login=RESEARCHER, message="Tabla", **AI).ok
    first = _submit(w, login=RESEARCHER, summary="Resultados.", note={"done": "- Tabla."})
    assert first.ok, first.error
    assert first.data["reviewers_requested"] == [LEAD]  # type: ignore[index]
    pr = first.data["pr"]["number"]  # type: ignore[index]
    w.forge.seed_review(pr, author=LEAD, state="CHANGES_REQUESTED", submitted_at=at(5))  # después de la entrega
    _edit(w, RESULTS, "La tabla corregida.")
    assert w.run("save", login=RESEARCHER, message="Corrige", now=at(10), **AI).ok
    second = _submit(w, login=RESEARCHER, summary="Resultados.", note={"done": "- Corregí la tabla."}, now=at(10))
    assert second.ok and second.data["round"] == 2 and second.data["reviewers_requested"] == [LEAD]  # type: ignore[index]


def test_dry_run_plans_without_writing(world: World) -> None:
    head = world.repo.head()
    result = _submit(world, dry_run=True)
    assert result.ok and result.data["dry_run"] is True  # type: ignore[index]
    kinds = [e["kind"] for e in result.data["plan"]]  # type: ignore[index]
    assert kinds[:3] == ["file_write", "git_commit", "git_push"] and "forge_create" in kinds
    assert world.repo.head() == head and not (world.repo.path / NOTE).exists()
