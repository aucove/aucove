# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""El hallazgo de ``scope`` para un nombre que Windows no puede extraer.

``canonical`` (02 §3.1) rechaza los segmentos que son alias de
Win32 (``is_win32_alias``: CON, PRN, AUX, NUL, COM1-9, LPT1-9, punto o espacio final,
``<>:"|?*`` y nombres cortos 8.3 como ``GIT~1``). Por eso un PR de la dueña con
``sections/methods/aux.tex`` da ``E_PERM_OUTSIDE_REPO`` en CI. El código es correcto,
pero el mensaje solo hablaba de «fuera del repositorio, bajo .git/ o con barra invertida» y
no decía que el problema es el nombre. Ahora el mensaje y la pista, en es y en, lo dicen.

También se comprueba que el escenario de prueba no extrae esos nombres al disco (en
Windows no se pueden escribir tal cual: ``nul`` va al dispositivo y ``x.tex.`` se vuelve
``x.tex``) y que el conjunto dorado de A9 incluye el caso de ``aux.tex``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from aucove.domain.findings import Finding
from aucove.i18n import translator
from aucove.ops.rules import scope
from aucove.ops.rules.base import RuleInput
from tests import scenarios
from tests.conftest import isolated_git_env

OUTSIDE = "E_PERM_OUTSIDE_REPO"
SECTION = "sections/methods/"
# Nombres que git para Windows se niega a extraer (verify_path, is_valid_win32_path).
BAD_NAMES = (
    "aux.tex",
    "con.tex",
    "nul",
    "COM1.tex",
    "lpt9.tex",
    "x.tex.",
    "y.tex ",
    "NOTAS~1.TEX",
    "a|b.tex",
)
BAD_PATHS = tuple(SECTION + name for name in BAD_NAMES)
# Control: empieza como un dispositivo, pero no lo es.
GOOD_PATH = SECTION + "auxiliar.tex"
# Lo que el mensaje debe nombrar, en los dos idiomas.
MESSAGE_NEEDLES = ("Windows", "CON", "PRN", "AUX", "NUL", "COM1-9", "LPT1-9", "GIT~1", '<>:"|?*')


def _case() -> dict[str, Any]:
    changes = [
        {"status": "added", "path": p, "content": "Texto de la sección propia.\n"}
        for p in (*BAD_PATHS, GOOD_PATH)
    ]
    return {
        "id": "windows-reserved-names",
        "description": "nombres que Windows no puede extraer en la sección propia",
        "covers": [],
        "task": {"number": 17, "claimant": "carla", "sections": ["methods"]},
        "pr": {"author": "carla"},
        "changes": changes,
        "expect": {"valid": False, "checks": {"scope": "fail"}, "codes": [OUTSIDE]},
    }


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[scenarios.Scenario, RuleInput]:
    root = tmp_path_factory.mktemp("windows-reserved-names")
    sc = scenarios.build_scenario(_case(), root / "case", isolated_git_env(root / "git-env"))
    return sc, scenarios.scenario_rule_input(sc, translator("es"))


def _errors(findings: list[Finding]) -> list[tuple[str, str | None]]:
    return sorted((f.code, f.path) for f in findings if f.severity == "error")  # type: ignore[arg-type]


def test_scenario_keeps_the_names_in_git_and_off_the_disk(built: tuple[scenarios.Scenario, RuleInput]) -> None:
    sc, inp = built
    assert inp.tree is not None
    entries = inp.tree.entries()
    assert all(p in entries for p in BAD_PATHS)  # en git, tal cual
    # La cabeza no se extrae: en Windows esos nombres no caben en el disco tal cual.
    assert sc.checked_out is False


@pytest.mark.parametrize("path", BAD_PATHS)
def test_each_windows_name_is_unportable_for_the_scenario(path: str) -> None:
    # Antes, 'aux.tex' se escribía en el disco y 'x.tex.' quedaba como 'x.tex' (Win32 quita
    # el punto final): el árbol de trabajo no era la cabeza de git.
    assert scenarios._portable([path]) is False
    assert scenarios._portable([GOOD_PATH]) is True


def test_own_section_file_with_a_windows_name_is_outside_repo(built: tuple[scenarios.Scenario, RuleInput]) -> None:
    _sc, inp = built
    findings = scope.run(inp)
    assert _errors(findings) == sorted((OUTSIDE, p) for p in BAD_PATHS)
    assert all(f.path != GOOD_PATH for f in findings)


@pytest.mark.parametrize("lang", ["es", "en"])
def test_the_message_says_the_name_is_the_problem(built: tuple[scenarios.Scenario, RuleInput], lang: str) -> None:
    _sc, inp = built
    findings = [f for f in scope.run(replace(inp, t=translator(lang))) if f.code == OUTSIDE]
    assert len(findings) == len(BAD_PATHS)
    for f in findings:
        assert f.path is not None and f.path in f.message
        text = f.message.replace(f.path, "")
        missing = [n for n in MESSAGE_NEEDLES if n not in text]
        assert missing == [], f"{lang}: el mensaje no nombra {missing}: {f.message!r}"
        # La pista sigue explicando la barra invertida y ahora también el nombre.
        hint = f.hint or ""
        assert "\\" in hint and "Windows" in hint, hint


def test_golden_set_has_the_aux_tex_case() -> None:
    cases = [scenarios.load_case(p) for p in scenarios.case_paths()]
    hits = [
        c
        for c in cases
        if any(ch["path"] == SECTION + "aux.tex" for ch in c["changes"])
        and c["pr"]["author"] == "carla"
        and c["expect"]["checks"]["scope"] == "fail"
        and c["expect"]["codes"] == [OUTSIDE]
        and SECTION + "aux.tex" in c["expect"]["paths"]
    ]
    assert hits, "falta en el conjunto dorado el caso de sections/methods/aux.tex de la dueña"
