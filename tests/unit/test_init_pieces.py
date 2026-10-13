# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Piezas puras de ``init`` (05 §9.3; 06-permisos §4): etiquetas, ajustes, política de Actions,
rulesets y su verificación, y la constante de checks requeridos (REQ-PERM-34)."""

from __future__ import annotations

import re

import pytest

from aucove import _meta
from aucove.domain.checks import BUILD, EDITOR_REVIEW, VALIDATE, required_checks
from aucove.forge.base import ActionsPolicy
from aucove.i18n import translator
from aucove.ops import init as init_op
from tests.conftest import load_tool

SETUP_UV = "astral-sh/setup-uv@" + "a" * 40
CHECKOUT = "actions/checkout@" + "b" * 40


def test_required_checks_are_the_template_jobs() -> None:
    """REQ-PERM-34: una sola constante; los workflows publican esos nombres: build y validate son
    jobs, y editor-review lo publica por la API el job evaluate de editor-review.yml (REQ-CI-59)."""
    assert required_checks("none") == (BUILD, VALIDATE) and required_checks("latex")[-1] == EDITOR_REVIEW
    render = load_tool("render_template").render_files
    rendered = render("en", fmt="latex")
    names = {m.group(1) for rel, data in rendered.items() if rel.startswith(".github/workflows/")
             for m in re.finditer(r"^    name: (\S+)$", data.decode("utf-8"), re.M)}
    assert {BUILD, VALIDATE} <= names and EDITOR_REVIEW not in names
    assert f"{_meta.NAME} ci editor-review " in rendered[".github/workflows/editor-review.yml"].decode("utf-8")
    assert ".github/workflows/editor-review.yml" not in render("en", fmt="none")


def test_labels_follow_the_spec_table() -> None:
    es = init_op.labels_for(translator("es"))
    assert [lab.name for lab in es] == [name for name, _color in init_op.LABELS] and len(es) == 12
    assert "review:model" not in {lab.name for lab in es}  # reservada (00-marco §5.4)
    assert es[0].description.startswith("Tarea del") and init_op.labels_for(translator("en"))[0].description == "Project task"
    assert all(lab.description and not lab.description.startswith("init.") for lab in es)


def test_settings_changes_send_the_squash_pair() -> None:
    desired = init_op.desired_settings("private")
    assert desired["allow_forking"] is False and "allow_forking" not in init_op.desired_settings("public")
    current = dict(desired, squash_merge_commit_message="COMMIT_MESSAGES", has_wiki=True)
    assert init_op.settings_changes(current, desired) == {
        "squash_merge_commit_title": "PR_TITLE", "squash_merge_commit_message": "PR_BODY", "has_wiki": False}
    assert init_op.settings_changes(desired, desired) == {}


def test_template_actions_come_from_the_workflows() -> None:
    files = {
        ".github/workflows/a.yml": f"jobs:\n  x:\n    steps:\n      - uses: {CHECKOUT}  # v4\n".encode(),
        ".github/workflows/b.yml": f"jobs:\n  y:\n    uses: ./.github/workflows/a.yml\n  z:\n    steps:\n"
                                   f"      - uses: '{SETUP_UV}'\n".encode(),
        "README.md": f"uses: {SETUP_UV}\n".encode(),
    }
    assert init_op.template_actions(files) == [CHECKOUT, SETUP_UV]


@pytest.mark.parametrize(("org", "repo", "expected"), [
    # la de la org no se pudo leer y la del repo no muestra la restricción: desconocida
    (None, ActionsPolicy("repo", True, "all", True),
     {"level": None, "allowed_actions": "unknown", "missing": [CHECKOUT, SETUP_UV]}),
    (ActionsPolicy("org", True, "selected", True, True, False, ("astral-sh/setup-uv@*",)),
     ActionsPolicy("repo", True, "all", True), None),
    (ActionsPolicy("org", True, "selected", True, True, False, ()), ActionsPolicy("repo", True, "all", True),
     {"level": "org", "allowed_actions": "selected", "missing": [SETUP_UV]}),
    (None, ActionsPolicy("repo", True, "local_only", True),
     {"level": "repo", "allowed_actions": "local_only", "missing": [CHECKOUT, SETUP_UV]}),
    (None, None, {"level": None, "allowed_actions": "unknown", "missing": [CHECKOUT, SETUP_UV]}),
])
def test_actions_restriction(org: ActionsPolicy | None, repo: ActionsPolicy | None, expected: object) -> None:
    """REQ-PERM-56: primero la org; sin poder leerla, el resultado es desconocido."""
    assert init_op.actions_restriction(org, repo, [CHECKOUT, SETUP_UV], org_owned=True) == expected
    personal = init_op.actions_restriction(None, ActionsPolicy("repo", True, "all", True), [SETUP_UV], org_owned=False)
    assert personal is None


def test_covers_ignores_what_the_forge_adds() -> None:
    desired = init_op.base_ruleset("main", checks=required_checks("none"), individual=False, bypass_id=None)
    current = {**desired, "id": 7, "source": "lab-x/estudio",
               "rules": [{**r, "parameters": {**r.get("parameters", {}), "extra": 1}} if "parameters" in r else r
                         for r in reversed(desired["rules"])]}
    assert init_op.covers(current, desired)
    assert not init_op.covers({**current, "bypass_actors": [{"actor_id": 1}]}, desired)
    assert not init_op.covers({**current, "enforcement": "evaluate"}, desired)


def test_missing_rules() -> None:
    checks = required_checks("latex")
    full = init_op.base_ruleset("main", checks=checks, individual=False, bypass_id=None)["rules"]
    assert init_op.missing_rules(full, checks=checks, individual=False) == []
    partial = [r for r in full if r["type"] != "deletion"]
    assert init_op.missing_rules(partial, checks=checks, individual=False) == ["deletion"]
    solo = init_op.base_ruleset("main", checks=checks, individual=True, bypass_id=None)["rules"]
    assert init_op.missing_rules(solo, checks=checks, individual=True) == []
    assert "required_approving_review_count" in init_op.missing_rules(solo, checks=checks, individual=False)
    no_editor = init_op.base_ruleset("main", checks=checks[:2], individual=False, bypass_id=None)["rules"]
    assert init_op.missing_rules(no_editor, checks=checks, individual=False) == [f"check:{_meta.NAME}/editor-review"]
