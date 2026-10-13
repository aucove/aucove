# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Regla ``plan-ids`` (09-ci §4.1; 03-esquemas §5).

Falla si se borra un ID del merge-base o se reactiva uno retirado, pasa con
``base_tree`` ``None`` y no da errores sobre el render de la plantilla de H2.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aucove.config.base import BaseSnapshot
from aucove.config.project import parse_project
from aucove.config.roles import parse_roles
from aucove.domain.findings import Finding, has_errors
from aucove.i18n import Lang, Translator, load_catalog, placeholders, translator
from aucove.ops.rules import plan_ids
from aucove.ops.rules.base import RuleInput, status_of
from aucove.vcs.git import Git
from aucove.vcs.tree import git_tree
from tests.conftest import GitRepo, load_tool

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "convention" / "plan"
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
RULE = plan_ids.RULE


class DictTree:
    """``TreeView`` en memoria: ruta -> bytes, con modos opcionales."""

    def __init__(self, files: Mapping[str, bytes | str], modes: Mapping[str, str] | None = None) -> None:
        self.files = {p: (c.encode("utf-8") if isinstance(c, str) else c) for p, c in files.items()}
        self.modes = dict(modes or {})
        self.root: Path | None = None
        self.sha: str | None = None

    def entries(self) -> Mapping[str, str]:
        out = {p: "100644" for p in self.files}
        out.update(self.modes)
        return out

    def read_bytes(self, path: str, limit: int = 5 * 2**20) -> bytes | None:
        mode = self.entries().get(path)
        if mode is None or not mode.startswith("100") or path not in self.files:
            return None
        data = self.files[path]
        return data if len(data) <= limit else None

    def read_text(self, path: str, limit: int = 5 * 2**20) -> str | None:
        data = self.read_bytes(path, limit)
        try:
            return data.decode("utf-8") if data is not None else None
        except UnicodeDecodeError:
            return None


@pytest.fixture(scope="module")
def render() -> Mapping[str, Mapping[str, bytes]]:
    tool = load_tool("render_template")
    return {lang: tool.render_files(lang) for lang in ("es", "en")}


def snapshot(files: Mapping[str, bytes], decisions: frozenset[str] = frozenset()) -> BaseSnapshot:
    cfg, _ = parse_project(files["project.yaml"])
    roles, _ = parse_roles(files["roles.yaml"])
    assert cfg is not None and roles is not None
    return BaseSnapshot(
        ref="origin/main", sha=None, project=cfg, roles=roles, decisions=decisions, fallback_sha=None, source="local"
    )


def run(
    head: str | None,
    base: str | None = None,
    *,
    has_base: bool = True,
    t: Translator | None = None,
    policy: BaseSnapshot | None = None,
    extra: Mapping[str, str] | None = None,
) -> list[Finding]:
    head_files: dict[str, bytes | str] = dict(extra or {})
    if head is not None:
        head_files["plan.md"] = head
    base_files: dict[str, bytes | str] = {}
    if base is not None:
        base_files["plan.md"] = base
    inp = RuleInput(
        mode="pr",
        t=t or translator("es"),
        now=NOW,
        policy=policy,
        tree=DictTree(head_files),
        base_tree=DictTree(base_files) if has_base else None,
    )
    return list(RULE.run(inp))


def codes(findings: list[Finding]) -> list[str]:
    return [f.code for f in findings]


PLAN = "# Plan\n\n- RQ-1: pregunta\n- RQ-2 (retired): ~~vieja~~\n- HYP-1: hipótesis (RQ-1)\n"


# -- registro y catálogo ----------------------------------------------------------------------


def test_rule_spec() -> None:
    assert plan_ids.RULES == (RULE,)
    assert RULE.id == "plan-ids"
    assert RULE.modes == frozenset({"tree", "pr", "base"})
    assert not RULE.needs_forge and RULE.after == () and RULE.tools == ()


@pytest.mark.parametrize("lang", ["es", "en"])
def test_every_code_has_localized_text_with_its_markers(lang: Lang) -> None:
    catalog = load_catalog(lang)
    for code, params in RULE.codes.items():
        message = catalog[f"finding.{code}.message"]
        hint = catalog.get(f"finding.{code}.hint", "")
        assert placeholders(message) | placeholders(hint) == params, code


def test_duplicate_id_reuses_the_config_marker() -> None:
    from aucove.config.schemas import CONFIG_FINDING_CODES

    assert CONFIG_FINDING_CODES["E_VALIDATION_DUPLICATE_ID"] == RULE.codes["E_VALIDATION_DUPLICATE_ID"]


# -- criterios de aceptación -----------------------------------------------------------------


def test_same_plan_passes() -> None:
    assert run(PLAN, PLAN) == []


def test_fails_when_an_id_of_the_merge_base_is_deleted() -> None:
    head = PLAN.replace("- RQ-2 (retired): ~~vieja~~\n", "")
    findings = run(head, PLAN)
    assert codes(findings) == ["E_VALIDATION_PLAN_ID_REMOVED"]
    assert findings[0].path == "plan.md" and findings[0].line is None
    assert "RQ-2" in findings[0].message
    assert status_of(findings) == "fail"


def test_fails_when_a_retired_id_is_reactivated() -> None:
    head = PLAN.replace("- RQ-2 (retired): ~~vieja~~", "- RQ-2: vieja")
    findings = run(head, PLAN)
    assert codes(findings) == ["E_VALIDATION_PLAN_RETIRED_CHANGED"]
    assert findings[0].line == 4 and findings[0].severity == "error"


def test_passes_with_base_tree_none() -> None:
    assert run(PLAN, has_base=False) == []
    assert run(PLAN.replace("- RQ-2 (retired): ~~vieja~~\n", ""), has_base=False) == []


@pytest.mark.parametrize("lang", ["es", "en"])
def test_template_render_has_no_errors(render: Mapping[str, Mapping[str, bytes]], lang: Lang) -> None:
    files = render[lang]
    policy = snapshot(files)
    for base in (None, files):
        inp = RuleInput(
            mode="tree",
            t=translator(lang),
            now=NOW,
            policy=policy,
            tree=DictTree(files),
            base_tree=DictTree(base) if base is not None else None,
        )
        findings = list(RULE.run(inp))
        assert not has_errors(findings), [f.message for f in findings]
        assert findings == []


def test_template_plan_passes_in_git_tree(git_repo: GitRepo, render: Mapping[str, Mapping[str, bytes]]) -> None:
    files = render["en"]
    first = git_repo.commit("base", files={"plan.md": files["plan.md"]})
    second = git_repo.commit("head", files={"plan.md": files["plan.md"].decode("utf-8") + "- RQ-2: nueva\n"})
    git = Git(git_repo.path, env=git_repo.env)
    inp = RuleInput(
        mode="pr",
        t=translator("en"),
        now=NOW,
        tree=git_tree(git_repo.path, second, git=git),
        base_tree=git_tree(git_repo.path, first, git=git),
    )
    assert list(RULE.run(inp)) == []
    removed = git_repo.commit("remove", files={"plan.md": "# Plan\n"})
    inp = RuleInput(
        mode="pr",
        t=translator("en"),
        now=NOW,
        tree=git_tree(git_repo.path, removed, git=git),
        base_tree=git_tree(git_repo.path, first, git=git),
    )
    assert sorted(codes(list(RULE.run(inp)))) == ["E_VALIDATION_PLAN_ID_REMOVED"] * 5


# -- resto de las reglas de 03 §5 ---------------------------------------------------------------


def test_reused_number_fails() -> None:
    base = "- RQ-1: a\n- RQ-3: c\n"
    findings = run(base + "- RQ-2: reutiliza\n", base)
    assert codes(findings) == ["E_VALIDATION_PLAN_ID_REUSED"]
    assert "3" in findings[0].message and findings[0].line == 3


def test_text_change_is_only_a_warning() -> None:
    findings = run(PLAN.replace("- RQ-1: pregunta", "- RQ-1: pregunta reformulada"), PLAN)
    assert codes(findings) == ["W_VALIDATION_PLAN_TEXT_CHANGED"]
    assert findings[0].severity == "warning" and status_of(findings) == "pass"


def test_deleted_plan_removes_every_base_id() -> None:
    findings = run(None, PLAN)
    assert codes(findings) == ["E_VALIDATION_PLAN_ID_REMOVED"] * 3


def test_missing_plan_without_base_passes() -> None:
    assert run(None, None) == []
    assert run(None, has_base=False) == []


def test_syntax_errors_and_duplicates() -> None:
    head = "- RQ-1: a\n- rq-2: minúsculas\n- RQ-1: repetido\n"
    findings = run(head, has_base=False)
    assert [(f.code, f.line) for f in findings] == [
        ("E_VALIDATION_PLAN_SYNTAX", 2),
        ("E_VALIDATION_DUPLICATE_ID", 3),
    ]
    assert "- rq-2: minúsculas" in findings[0].message
    assert "RQ-1" in findings[1].message


def test_long_syntax_lines_are_clipped() -> None:
    line = "- rq-2: " + "x" * 400
    [finding] = run(line + "\n", has_base=False)
    assert len(finding.message) < 300 and "truncated" in finding.message


def test_unknown_reference_and_retired_reference() -> None:
    head = "- RQ-1: a (HYP-7)\n- RQ-2 (retired): ~~b~~\n- HYP-1: cita RQ-2\n- HYP-2 (retired): ~~cita RQ-2~~\n"
    findings = run(head, has_base=False)
    assert [(f.code, f.line, f.severity) for f in findings] == [
        ("E_VALIDATION_UNKNOWN_PLAN_ID", 1, "error"),
        ("W_VALIDATION_PLAN_REF_RETIRED", 3, "warning"),
    ]


def test_retirement_decision_must_exist(render: Mapping[str, Mapping[str, bytes]]) -> None:
    head = "- RQ-1 (retired: 2026-10-06-12-quitar-rq1): ~~a~~\n"
    assert codes(run(head, has_base=False)) == ["E_VALIDATION_UNKNOWN_DECISION"]
    in_tree = {"memory/decisions/2026-10-06-12-quitar-rq1.md": "---\n---\n"}
    assert run(head, has_base=False, extra=in_tree) == []
    policy = snapshot(render["es"], decisions=frozenset({"memory/decisions/2026-10-06-12-quitar-rq1.md"}))
    assert run(head, has_base=False, policy=policy) == []


def test_decision_in_the_base_tree_counts() -> None:
    head = "- RQ-1 (retired: 2026-10-06-12-quitar-rq1): ~~a~~\n"
    inp = RuleInput(
        mode="base",
        t=translator("en"),
        now=NOW,
        tree=DictTree({"plan.md": head}),
        base_tree=DictTree({"plan.md": head, "memory/decisions/2026-10-06-12-quitar-rq1.md": "x"}),
    )
    assert list(RULE.run(inp)) == []


def test_example_of_03_with_its_decision_passes() -> None:
    text = (FIXTURES / "example-03.md").read_text(encoding="utf-8")
    decision = {"memory/decisions/2026-10-06-12-fusionar-rq3-en-rq1.md": "x"}
    assert run(text, text, extra=decision) == []


def test_crlf_and_bom_do_not_change_the_result() -> None:
    head = "\ufeff" + PLAN.replace("\n", "\r\n")
    assert run(head, PLAN) == []


@pytest.mark.parametrize("lang", ["es", "en"])
def test_messages_are_localized(lang: Lang) -> None:
    [finding] = run(PLAN.replace("- RQ-2 (retired): ~~vieja~~\n", ""), PLAN, t=translator(lang))
    catalog = load_catalog(lang)
    assert finding.message == catalog["finding.E_VALIDATION_PLAN_ID_REMOVED.message"].format(id="RQ-2")
    assert finding.hint == catalog["finding.E_VALIDATION_PLAN_ID_REMOVED.hint"]


# -- falla cerrada ------------------------------------------------------------------------------


def test_without_tree_the_rule_is_unavailable() -> None:
    inp = RuleInput(mode="pr", t=translator("es"), now=NOW, tree=None)
    [finding] = RULE.run(inp)
    assert finding.code == "E_INTERNAL_UNEXPECTED" and finding.severity == "error"
    assert finding.check == "plan-ids"


def test_unreadable_plan_is_unavailable() -> None:
    # texto latino válido como str: se analiza normalmente, no es «ilegible»
    assert run("- RQ-1: café ÿþ\n", has_base=False) == []
    inp = RuleInput(mode="pr", t=translator("es"), now=NOW, tree=DictTree({"plan.md": b"\xff\xfe"}), base_tree=None)
    [finding] = RULE.run(inp)
    assert finding.code == "E_INTERNAL_UNEXPECTED" and finding.path == "plan.md"
    inp = RuleInput(
        mode="pr",
        t=translator("es"),
        now=NOW,
        tree=DictTree({"plan.md": PLAN}),
        base_tree=DictTree({"plan.md": b"\xff\xfe"}),
    )
    [finding] = RULE.run(inp)
    assert finding.code == "E_INTERNAL_UNEXPECTED" and finding.path == "plan.md"
