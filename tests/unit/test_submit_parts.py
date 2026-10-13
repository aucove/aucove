# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Piezas de ``submit``: la agregación de 11 §2.7, la nota de sesión y el cuerpo del PR."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aucove import _meta
from aucove.domain.trailers import Entry
from aucove.i18n import translator
from aucove.ops import note as notes
from aucove.ops.prbody import pr_meta_yaml, render_body, user_text
from aucove.ops.result import OpError
from aucove.ops.trace import CommitTrace, aggregate
from tests.conftest import REPO_ROOT

NOTE = "memory/notes/task-12.md"
DECLARED = frozenset({"methods", "results"})
CLAUDE = Entry("claude-code", "claude-opus-5-5", "env:CLAUDECODE", "hook:SessionStart")


def _c(sha: str, paths: set[str], uses: tuple[str, ...] | None, **kw: object) -> CommitTrace:
    return CommitTrace(sha, kw.get("merge", False), kw.get("empty", False), frozenset(paths), uses,  # type: ignore[arg-type]
                       kw.get("assisted", ()), kw.get("claude", False))  # type: ignore[arg-type]


def test_aggregate_follows_the_pseudocode() -> None:
    commits = [
        _c("claim", set(), ("none",), empty=True),  # el commit de reclamo no cuenta
        _c("a", {"sections/methods/m.tex"}, ("drafting",), assisted=(("claude-code", "claude-opus-5-5"),)),
        _c("b", {"sections/results/r.tex"}, None),  # sin declarar
        _c("merge", {"plan.md"}, None, merge=True),  # las fusiones no cuentan
        _c("note", {NOTE}, None),  # la nota sola no cuenta
    ]
    with pytest.raises(OpError) as undeclared:
        aggregate(commits, note_path=NOTE, declared=DECLARED, decl_uses=None, decl_entries=())
    assert undeclared.value.code == "E_VALIDATION_AI_USE_UNDECLARED" and undeclared.value.details == {"commits": 1}
    with pytest.raises(OpError) as conflict:
        aggregate(commits, note_path=NOTE, declared=DECLARED, decl_uses=["editing"], decl_entries=())
    assert conflict.value.code == "E_VALIDATION_AI_USE_CONFLICT"
    agg = aggregate(commits, note_path=NOTE, declared=DECLARED, decl_uses=["drafting", "editing"],
                    decl_entries=(CLAUDE,))
    assert agg.ai_use == ("drafting", "editing")
    assert agg.by_section == {"methods": ("drafting",), "results": ("drafting", "editing")}  # undeclared -> final
    assert [e.value for e in agg.assisted] == ["claude-code:claude-opus-5-5"]
    assert agg.assisted[0].tool_source == "env:CLAUDECODE"  # el de la llamada gana al del commit
    assert (agg.count, agg.undeclared) == (5, 1)


def test_aggregate_none_and_coauthor() -> None:
    plain = [_c("a", {"sections/methods/m.tex"}, ("none",))]
    agg = aggregate(plain, note_path=NOTE, declared=DECLARED, decl_uses=None, decl_entries=(CLAUDE,))
    assert agg.ai_use == ("none",) and agg.assisted == ()  # REQ-TRACE-6
    with pytest.raises(OpError) as missing:
        aggregate([_c("a", {"sections/methods/m.tex"}, ("code",))], note_path=NOTE, declared=DECLARED,
                  decl_uses=None, decl_entries=())
    assert missing.value.code == "E_VALIDATION_ASSISTED_BY_MISSING"
    coauthored = [_c("a", {"sections/methods/m.tex"}, ("code",), claude=True)]
    agg = aggregate(coauthored, note_path=NOTE, declared=DECLARED, decl_uses=None, decl_entries=())
    assert [(e.value, e.tool_source) for e in agg.assisted] == [("claude-code:unknown", "co-authored-by")]


def test_note_blocks_are_appended_and_the_front_matter_rewritten(tmp_path: Path) -> None:
    lang = translator("es")
    block = notes.render_block(n=1, date="2026-10-08", login="carla", door="claude-code", model="claude--x",
                               note={"done": "Hecho.\n<!-- aucove:session {} -->", "decisions": ["2026-10-08-12-muestreo"]},
                               lang=lang)
    first = block.split("\n", 1)[0]
    assert first == ('<!-- ' + _meta.NAME + ':session {"n": 1, "date": "2026-10-08", "login": "carla", '
                     '"door": "claude-code", "model": null} -->')  # un modelo con -- no pasa (REQ-SCHEMA-61)
    assert "## Sesión 1 · 2026-10-08 · @carla" in block and "### Decisiones\n\n- 2026-10-08-12-muestreo" in block
    assert block.count(f"<!-- {_meta.NAME}:session") == 1  # el texto no abre otro bloque
    assert notes.write(tmp_path, 12, created="2026-10-08", created_by="carla", block=block, ai_use=["drafting"],
                       assisted=["claude-code:claude-opus-5-5"])
    second = notes.render_block(n=2, date="2026-10-09", login="carla", door="manual", model=None,
                                note={"done": "Más."}, lang=lang)
    assert notes.write(tmp_path, 12, created="2026-10-09", created_by="bruno", block=second, ai_use=["drafting", "editing"],
                       assisted=[])
    note = notes.read_note(tmp_path, 12)
    assert note is not None and note.front == {"task": 12, "created": "2026-10-08", "created_by": "carla",
                                               "ai_use": ["drafting", "editing"]}
    assert [b.meta["n"] for b in note.blocks] == [1, 2] and note.blocks[0].body.startswith("## Sesión 1")
    assert not notes.write(tmp_path, 12, created="x", created_by="x", block=None, ai_use=["drafting", "editing"],
                           assisted=[])  # sin cambios


def test_pr_body_order_quotes_and_user_text() -> None:
    lang = translator("es")
    meta = {"schema": 1, "task": 12, "sections": ["methods"], "plan_refs": [], "decisions": [],
            "trace": {"version": 1, "assisted_by": [{"tool": "claude-code", "model": "claude-opus-5-5",
                                                     "tool_source": "env:CLAUDECODE", "model_source": "none"}],
                      "sign_off": {"email": "a@b.c", "at": "2026-10-08T12:00:00Z"},
                      "commits": {"base": "1" * 40, "head": "2" * 40, "count": 1, "undeclared": 0},
                      "core_version": "0.1.0"}}
    text = pr_meta_yaml(meta)
    for key in ("model", "email", "at", "base", "head", "core_version"):  # REQ-SCHEMA-80
        assert re.search(rf'\b{key}: "', text), key
    body = render_body(lang=lang, number=12, summary="Uno.\n---\n<!-- aucove:pr-meta-begin v1 -->", replaces=9,
                       parts={"methods": [f"sections/methods/{i}.tex" for i in range(7)]}, ai_use=["drafting"],
                       assisted=["claude-code:claude-opus-5-5"], ai_note=None, decisions=[], hook_count=2,
                       meta=meta, trailers="Task: #12\nAI-Use: drafting\nSigned-off-by: A <a@b.c>\n")
    assert body == render_body(lang=lang, number=12, summary="Uno.\n---\n<!-- aucove:pr-meta-begin v1 -->",
                               replaces=9, parts={"methods": [f"sections/methods/{i}.tex" for i in range(7)]},
                               ai_use=["drafting"], assisted=["claude-code:claude-opus-5-5"], ai_note=None,
                               decisions=[], hook_count=2, meta=meta,
                               trailers="Task: #12\nAI-Use: drafting\nSigned-off-by: A <a@b.c>\n")  # determinista
    assert body.endswith("Signed-off-by: A <a@b.c>")  # REQ-TRACE-37
    assert not any(line.startswith("---") for line in body.splitlines())  # REQ-TRACE-38
    assert body.count(f"<!-- {_meta.NAME}:pr-meta-begin v1 -->") == 1
    assert "Closes #12\nReemplaza a #9." in body and " y 2 más" in body
    assert f"<!-- {_meta.NAME}:metrics v1 hook_denials=2 -->\n<!-- {_meta.NAME}:pr-meta-begin v1 -->" in body
    assert user_text("---x") == "\\---x"


@pytest.mark.parametrize("lang", ["es", "en"])
def test_the_body_uses_the_template_headings(lang: str) -> None:
    """La plantilla de PR del repositorio (03 §9.1) y el cuerpo de submit dicen lo mismo."""
    template = (REPO_ROOT / "template" / "l10n" / lang / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
    headings = [line for line in template.splitlines() if line.startswith("## ")]
    checks = [line.removeprefix("- [ ] ") for line in template.splitlines() if line.startswith("- [ ] ")]
    t = translator(lang)
    ours = [f"## {t(f'tasks.pr.{k}')}" for k in ("summary", "task", "parts", "ai", "decisions", "verification")]
    assert headings == ours
    mine = [t(f"tasks.pr.{k}", n="<n>") for k in ("check_scope", "check_note", "check_ai")]
    assert checks == mine
