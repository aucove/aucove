# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""En ``scope``, una ``\\`` en una ruta de git no es un separador.

Las rutas que evalúa ``scope`` salen de git y de la API de la forja, donde ``/`` es el
único separador y ``\\`` es un carácter más del nombre (válido en Linux; git para Windows
no puede extraerlo). Antes, ``canonical`` (02 §3.1, pensada también para rutas del SO)
convertía ``sections\\methods\\x.tex`` en ``sections/methods/x.tex`` y la regla
lo aceptaba como archivo de la sección propia, aunque en git es un archivo de la raíz
(clase ``lead``, 02 §3.4). Ahora la ruta es inválida (REQ-CONV-7): ``E_PERM_OUTSIDE_REPO``
para cualquier autor y operación, y en un renombre el otro lado se sigue evaluando
(06 §2.4: renombrar es delete + create).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from aucove.domain.findings import Finding
from aucove.i18n import translator
from aucove.ops.rules import scope
from aucove.ops.rules.base import Change, RuleInput
from tests import scenarios
from tests.conftest import isolated_git_env

OUTSIDE = "E_PERM_OUTSIDE_REPO"
OWN_SECTION = "sections\\methods\\x.tex"


def _case(cid: str, changes: list[dict[str, Any]], *, author: str = "carla") -> dict[str, Any]:
    return {
        "id": cid,
        "description": "barras invertidas en rutas de git",
        "covers": [],
        "task": {"number": 17, "claimant": author, "sections": ["methods"]},
        "pr": {"author": author},
        "changes": changes,
        "expect": {"valid": False, "checks": {"scope": "fail"}, "codes": [OUTSIDE]},
    }


@pytest.fixture(scope="module")
def inputs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, RuleInput]:
    root = tmp_path_factory.mktemp("p18-backslash")
    env = isolated_git_env(root / "git-env")
    cases = {
        "own": _case("own", [{"status": "added", "path": OWN_SECTION, "content": "Texto.\n"}]),
        "rename": _case(
            "rename",
            [
                {
                    "status": "renamed",
                    "previous_path": "sections/results/tables.tex",
                    "path": "sections\\methods\\tables.tex",
                }
            ],
        ),
    }
    out: dict[str, RuleInput] = {}
    for cid, case in cases.items():
        sc = scenarios.build_scenario(case, root / cid, env)
        out[cid] = scenarios.scenario_rule_input(sc, translator("es"))
    return out


def _errors(findings: list[Finding]) -> list[tuple[str, str | None]]:
    return sorted(((f.code, f.path) for f in findings if f.severity == "error"), key=lambda x: (x[0], x[1] or ""))


def test_backslash_name_is_not_read_as_the_own_section(inputs: dict[str, RuleInput]) -> None:
    # El escenario del hallazgo: antes daba allow (clase section, ruta sections/methods/x.tex).
    inp = inputs["own"]
    assert inp.tree is not None and OWN_SECTION in inp.tree.entries()  # en git es un nombre de la raíz
    findings = scope.run(inp)
    assert _errors(findings) == [(OUTSIDE, None)]
    (finding,) = findings
    assert OWN_SECTION in finding.message
    assert "sections/methods/x.tex" not in f"{finding.message} {finding.hint or ''}"


def test_the_message_names_the_backslash_in_both_languages(inputs: dict[str, RuleInput]) -> None:
    for lang in ("es", "en"):
        (finding,) = scope.run(replace(inputs["own"], t=translator(lang)))
        assert finding.code == OUTSIDE
        assert "\\" in (finding.hint or "").replace(OWN_SECTION, "")


def test_rename_to_a_backslash_name_still_checks_the_delete_of_the_origin(inputs: dict[str, RuleInput]) -> None:
    # Antes: create(sections/methods/tables.tex) se permitía como sección propia.
    assert _errors(scope.run(inputs["rename"])) == [
        (OUTSIDE, None),
        ("E_PERM_OUTSIDE_TASK", "sections/results/tables.tex"),
    ]


def test_rename_from_a_backslash_name_still_checks_the_create_of_the_destination(
    inputs: dict[str, RuleInput],
) -> None:
    inp = inputs["rename"]
    assert inp.tree is not None and "sections/results/results.tex" in inp.tree.entries()
    change = Change(
        path="sections/results/results.tex",
        status="renamed",
        previous_path="sections\\methods\\old.tex",
    )
    assert _errors(scope.run(replace(inp, changes=(change,)))) == [
        (OUTSIDE, None),
        ("E_PERM_OUTSIDE_TASK", "sections/results/results.tex"),
    ]


def test_rename_between_two_backslash_names_gives_one_finding_per_path(inputs: dict[str, RuleInput]) -> None:
    change = Change(path=OWN_SECTION, status="renamed", previous_path="sections\\methods\\old.tex")
    findings = scope.run(replace(inputs["own"], changes=(change,)))
    assert _errors(findings) == [(OUTSIDE, None), (OUTSIDE, None)]
    texts = [f.message for f in findings]
    assert any(OWN_SECTION in m for m in texts)
    assert any("sections\\methods\\old.tex" in m for m in texts)


@pytest.mark.parametrize("status", ["modified", "removed"])
def test_any_operation_on_a_backslash_name_is_denied(inputs: dict[str, RuleInput], status: str) -> None:
    # Antes, borrar 'x\\y.tex' se leía como borrar x/y.tex (lead): ahora la ruta es inválida.
    inp = inputs["own"]
    change = Change(path=OWN_SECTION if status == "modified" else "x\\y.tex", status=status)  # type: ignore[arg-type]
    assert _errors(scope.run(replace(inp, changes=(change,)))) == [(OUTSIDE, None)]


def test_forward_slash_paths_of_the_own_section_still_pass(inputs: dict[str, RuleInput]) -> None:
    inp = inputs["rename"]
    change = Change(path="sections/methods/methods.tex", status="removed")
    assert scope.run(replace(inp, changes=(change,))) == []
