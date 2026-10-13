# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Regla ``decisions-immutable`` (09-ci §4.1; 02-convencion REQ-CONV-67; D-33)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

import pytest

from aucove.config.base import BaseSnapshot
from aucove.config.project import parse_project
from aucove.config.roles import parse_roles
from aucove.domain.findings import Finding
from aucove.i18n import Translator, translator
from aucove.ops.rules import decisions
from aucove.ops.rules.base import Change, RuleInput
from tests.conftest import FIXTURES, roles_fixture

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
SHA = "0123456789abcdef0123456789abcdef01234567"
RECORD = "memory/decisions/2026-10-01-3-usar-biblatex.md"
OLD = "memory/decisions/2026-09-01-1-anterior.md"


def snapshot(existing: Sequence[str] = (OLD,)) -> BaseSnapshot:
    cfg, _ = parse_project((FIXTURES / "config" / "project" / "minimal.yaml").read_bytes())
    roles, _ = parse_roles(roles_fixture("basic").encode("utf-8"))
    assert cfg is not None and roles is not None
    return BaseSnapshot(ref="origin/main", sha=SHA, project=cfg, roles=roles, decisions=frozenset(existing),
                        fallback_sha=None, source="local")


def run(changes: Sequence[Change], *, mode: str = "pr", t: Translator | None = None,
        policy: BaseSnapshot | None = None) -> list[Finding]:
    inp = RuleInput(mode=mode, t=t or translator("es"), now=NOW, policy=policy or snapshot(),  # type: ignore[arg-type]
                    changes=tuple(changes))
    out = list(decisions.RULE.run(inp))
    assert all(f.check == "decisions-immutable" and f.code == "E_VALIDATION_DECISION_IMMUTABLE" for f in out)
    return out


def test_declaration() -> None:
    assert decisions.RULE.id == "decisions-immutable"
    assert decisions.RULE.modes == frozenset({"pr", "base"})
    assert decisions.RULES == (decisions.RULE,)


def test_added_records_pass() -> None:
    assert run([Change(RECORD, "added"), Change("tex/a.tex", "modified")]) == []


@pytest.mark.parametrize("status", ["modified", "removed"])
def test_modified_or_removed_records_fail_for_anyone(status: str, t_en: Translator) -> None:
    (finding,) = run([Change(OLD, status)], t=t_en)  # type: ignore[arg-type]
    assert finding.path == OLD and finding.severity == "error"
    assert t_en(f"validate.decisions.status.{status}") in finding.message


@pytest.mark.parametrize(
    ("change", "path"),
    [
        (Change("memory/decisions/2026-09-01-1-otro-nombre.md", "renamed", OLD), "memory/decisions/2026-09-01-1-otro-nombre.md"),
        (Change("archive/anterior.md", "renamed", OLD), OLD),  # sale de la carpeta
        (Change(RECORD, "renamed", "docs/borrador.md"), RECORD),  # entra en la carpeta
    ],
)
def test_renames_into_or_out_of_decisions_fail(change: Change, path: str) -> None:
    (finding,) = run([change])
    assert finding.path == path


def test_paths_are_compared_without_case() -> None:
    assert len(run([Change("Memory/Decisions/2026-09-01-1-anterior.md", "modified")])) == 1
    assert run([Change("memory/decisionsX/a.md", "modified"), Change("memory/notes/task-3.md", "modified")]) == []


def test_pr_cannot_add_a_record_that_exists_in_the_base() -> None:
    (finding,) = run([Change(OLD, "added")])
    assert translator("es")("validate.decisions.overwrite") in finding.message
    assert run([Change(OLD, "added")], mode="base") == []  # en base la política es el propio árbol


def test_root_commit_without_changes_passes() -> None:
    assert run([], mode="base") == []


def test_findings_are_sorted_by_path() -> None:
    out = run([Change("memory/decisions/b.md", "removed"), Change("memory/decisions/a.md", "modified")])
    assert [f.path for f in out] == ["memory/decisions/a.md", "memory/decisions/b.md"]
