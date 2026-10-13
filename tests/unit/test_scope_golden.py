# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Conjunto dorado de A9 para la regla ``scope`` (12-mvp A9; 09-ci §4.2).

Cada caso de ``tests/fixtures/scope/cases`` se arma con ``tests.scenarios`` (repositorio
git temporal y ``FakeForge``) y se clasifica llamando a la regla en proceso con el prior
del caso. Además se comprueba que los casos son coherentes: la lista de archivos
declarada es la que da git, el prior declarado es el que calculan ``roles-sync`` y
``generated-sync`` y las demás reglas declaradas dan el estado esperado.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from aucove.config.base import read_policy_dir
from aucove.domain.findings import Finding
from aucove.domain.permissions import Decision, Task
from aucove.forge.fake import Fault
from aucove.i18n import load_catalog, placeholders, translator
from aucove.ops.rules import catalog, roles_sync, scope
from aucove.ops.rules.base import Change, RuleInput, status_of
from tests import scenarios
from tests.conftest import REPO_ROOT, isolated_git_env

CASE_PATHS = scenarios.case_paths()
CASE_IDS = [p.stem for p in CASE_PATHS]

# Casos obligatorios: las 16 filas de 09 §4.2 (con los tres estados de la fila de estados)
# y la lista de 12 A9.
REQUIRED_COVERS = frozenset(
    {
        "ci-4.2:own-sections",
        "ci-4.2:foreign-section",
        "ci-4.2:own-undeclared-section",
        "ci-4.2:roles-self-grant",
        "ci-4.2:plan-md",
        "ci-4.2:workflow",
        "ci-4.2:lead-plan-decision",
        "ci-4.2:lead-bib",
        "ci-4.2:lead-roles-codeowners",
        "ci-4.2:not-task-branch",
        "ci-4.2:foreign-push",
        "ci-4.2:existing-decision",
        "ci-4.2:no-task",
        "ci-4.2:fork",
        "ci-4.2:state-proposed",
        "ci-4.2:state-expired",
        "ci-4.2:state-cancelled",
        "ci-4.2:bot",
        "a9:own-section",
        "a9:foreign-section",
        "a9:lead-file",
        "a9:roles-same-pr",
        "a9:new-decision",
        "a9:existing-decision",
        "a9:other-task-note",
        "a9:codeowners-by-hand",
        "a9:rename-across-sections",
        "a9:case-variants",
        "a9:backslash-variants",
        "advisory:pass",
        "advisory:fail",
    }
)
PRIOR_RULES = ("roles-sync", "generated-sync")


def _case(cid: str) -> dict[str, Any]:
    return scenarios.load_case(scenarios.cases_dir() / f"{cid}.yaml")


class _Built:
    """Escenarios y ``RuleInput`` armados una vez por módulo.

    Cada escenario cuesta unas 15 llamadas a git, así que se arman en paralelo con hilos
    (el tiempo es espera de subprocesos). Una falla al armar un caso solo hace fallar las
    pruebas de ese caso.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.env = isolated_git_env(root / "git-env")
        self._cache: dict[str, scenarios.Scenario] = {}
        self._inputs: dict[str, RuleInput] = {}
        self._errors: dict[str, BaseException] = {}
        self._n = 0

    def _make(self, cid: str) -> tuple[scenarios.Scenario, RuleInput]:
        sc = scenarios.build_scenario(_case(cid), self.root / cid, self.env)
        return sc, scenarios.scenario_rule_input(sc, translator("es"))

    def prebuild(self, ids: list[str]) -> None:
        translator("es"), translator("en")  # catálogos cargados antes de los hilos
        with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
            futures = {cid: pool.submit(self._make, cid) for cid in ids}
        for cid, fut in futures.items():
            try:
                self._cache[cid], self._inputs[cid] = fut.result()
            except Exception as exc:  # noqa: BLE001 (se vuelve a lanzar en la prueba del caso)
                self._errors[cid] = exc

    def get(self, cid: str) -> scenarios.Scenario:
        if cid in self._errors:
            raise self._errors[cid]
        if cid not in self._cache:
            self._cache[cid], self._inputs[cid] = self._make(cid)
        return self._cache[cid]

    def input(self, cid: str, lang: str = "es", **prior: str) -> RuleInput:
        """``RuleInput`` del caso; ``prior`` reemplaza claves del prior declarado."""
        self.get(cid)
        inp = self._inputs[cid]
        return replace(inp, t=translator(lang), prior={**inp.prior, **prior})  # type: ignore[arg-type]

    def run(self, cid: str, lang: str = "es", **prior: str) -> list[Finding]:
        return list(scope.run(self.input(cid, lang, **prior)))

    def fresh(self, case: Mapping[str, object]) -> scenarios.Scenario:
        """Escenario nuevo (sin caché), para las pruebas que cambian la forja o el caso."""
        self._n += 1
        return scenarios.build_scenario(case, self.root / f"fresh-{self._n}", self.env)


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> _Built:
    b = _Built(tmp_path_factory.mktemp("scope-cases"))
    b.prebuild(CASE_IDS)
    return b


def _errors(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == "error"]


def _summary(findings: list[Finding]) -> tuple[str, list[str], list[str], list[str]]:
    errors = _errors(findings)
    return (
        status_of(findings),
        sorted({f.code for f in errors}),
        sorted({f.path for f in errors if f.path}),
        sorted({f.code for f in findings if f.severity == "warning"}),
    )


def _run(sc: scenarios.Scenario, lang: str = "es", **prior: str) -> list[Finding]:
    inp = scenarios.scenario_rule_input(sc, translator(lang), prior=prior or None)  # type: ignore[arg-type]
    return list(scope.run(inp))


def _with(case_id: str, **changes: object) -> dict[str, Any]:
    case = _case(case_id)
    case.update(changes)
    return case


# -- forma del conjunto dorado --------------------------------------------------------


def test_case_files_load_and_ids_match_file_names() -> None:
    cases = [scenarios.load_case(p) for p in CASE_PATHS]
    assert len({c["id"] for c in cases}) == len(cases)
    assert all(c["description"] for c in cases)


def test_golden_set_has_at_least_24_enforced_and_2_advisory_cases() -> None:
    cases = [scenarios.load_case(p) for p in CASE_PATHS]
    enforced = [c for c in cases if c["profile"] == "enforced"]
    advisory = [c for c in cases if c["profile"] == "advisory"]
    assert len(enforced) >= 24
    assert len(advisory) >= 2
    assert {c["expect"]["checks"]["scope"] for c in advisory} == {"pass", "fail"}


def test_mandatory_cases_of_09_and_a9_are_covered() -> None:
    covered = set().union(*(scenarios.load_case(p)["covers"] for p in CASE_PATHS))
    assert REQUIRED_COVERS <= covered, sorted(REQUIRED_COVERS - covered)


def test_case_format_rejects_typos_and_inconsistent_expectations() -> None:
    good = _case("ci-01-own-sections-and-note")
    with pytest.raises(ValueError, match="claves desconocidas"):
        scenarios.normalize_case({**good, "expected": {}})
    with pytest.raises(ValueError, match="valid no coincide"):
        scenarios.normalize_case({**good, "expect": {"valid": False, "checks": {"scope": "pass"}}})
    with pytest.raises(ValueError, match="sin codes"):
        scenarios.normalize_case({**good, "expect": {"valid": False, "checks": {"scope": "fail"}}})
    with pytest.raises(ValueError, match="previous_path"):
        scenarios.normalize_case({**good, "changes": [{"status": "renamed", "path": "a.tex"}]})
    with pytest.raises(ValueError, match="merge_base"):
        scenarios.normalize_case({**good, "pr": {"author": "carla", "merge_base": True}})


# -- clasificación (criterio: 100 %) --------------------------------------------------


@pytest.mark.parametrize("cid", CASE_IDS)
def test_case_is_classified_as_expected(built: _Built, cid: str) -> None:
    sc = built.get(cid)
    expect = sc.case["expect"]  # type: ignore[index]
    findings = built.run(cid)
    status, codes, paths, warnings = _summary(findings)
    assert status == expect["checks"]["scope"], [f.to_dict() for f in findings]
    assert codes == expect["codes"]
    assert paths == expect["paths"]
    assert warnings == expect["warnings"]
    texts = " ".join(f"{f.message} {f.hint or ''}" for f in _errors(findings))
    for needle in expect["mentions"]:
        assert needle in texts, f"{needle!r} no aparece en {texts!r}"
    assert all(f.check == "scope" for f in findings)


@pytest.mark.parametrize("cid", CASE_IDS)
def test_classification_does_not_depend_on_language(built: _Built, cid: str) -> None:
    es = [(f.code, f.severity, f.path) for f in built.run(cid, "es")]
    en = [(f.code, f.severity, f.path) for f in built.run(cid, "en")]
    assert es == en


@pytest.mark.parametrize("cid", CASE_IDS)
def test_declared_changes_are_the_three_dot_diff_of_git(built: _Built, cid: str) -> None:
    sc = built.get(cid)
    declared = sorted((ch["status"], ch["path"], ch["previous_path"]) for ch in sc.case["changes"])  # type: ignore[union-attr]
    assert sorted(scenarios.diff_changes(sc)) == declared


@pytest.mark.parametrize("cid", CASE_IDS)
def test_declared_prior_is_what_roles_sync_and_generated_sync_compute(built: _Built, cid: str) -> None:
    sc = built.get(cid)
    inp = built.input(cid)
    by_id = {r.id: r for r in roles_sync.RULES}
    computed = {rid: status_of(list(by_id[rid].run(inp))) for rid in PRIOR_RULES}
    assert computed == {rid: sc.case["prior"][rid] for rid in PRIOR_RULES}  # type: ignore[index]


_OTHER_CHECKS = [
    (cid, check)
    for cid in CASE_IDS
    for check in _case(cid)["expect"]["checks"]
    if check not in ("scope", *PRIOR_RULES)
]


@pytest.mark.parametrize(("cid", "check"), _OTHER_CHECKS)
def test_other_declared_checks_give_the_expected_status(built: _Built, cid: str, check: str) -> None:
    # roles_sync y catalog se importan en la cabecera: son módulos de H2, no dependencias
    # opcionales, y A9 (12-mvp §2.3) exige el 100 % del conjunto dorado, donde un skip no
    # cuenta como pase. Por lo mismo, una regla declarada en el caso que falte del catálogo
    # es un fallo.
    rules = [r for r in catalog.load_rules() if r.id == check]
    if not rules:
        pytest.fail(f"la regla {check} del caso {cid} no está en el catálogo")
    sc = built.get(cid)
    inp = built.input(cid)
    findings = [f for r in rules for f in r.run(inp)]
    assert status_of(findings) == sc.case["expect"]["checks"][check]  # type: ignore[index]


def test_findings_are_deterministic(built: _Built) -> None:
    first = [f.to_dict() for f in built.run("ci-04-roles-self-grant")]
    assert first == [f.to_dict() for f in built.run("ci-04-roles-self-grant")]


# -- criterios de aceptación ----------------------------------------------------------


def test_hand_edited_codeowners_is_denied_only_because_of_the_prior(built: _Built) -> None:
    # Criterio 2: con roles-sync en fail, la ruta generated se deniega aunque el autor sea lead...
    cid = "a9-05-codeowners-by-hand-lead"
    assert _summary(built.run(cid))[1] == ["E_PERM_GENERATED_PATH"]
    # ...y con los dos priors en pass, el mismo PR pasaría scope: lo decide el prior.
    assert _summary(built.run(cid, **{"roles-sync": "pass", "generated-sync": "pass"}))[0] == "pass"
    assert _summary(built.run(cid, **{"roles-sync": "pass", "generated-sync": "fail"}))[1] == ["E_PERM_GENERATED_PATH"]


def test_lead_pr_changing_roles_and_regenerated_codeowners_passes_scope(built: _Built) -> None:
    cid = "ci-09-lead-roles-and-codeowners"
    assert _summary(built.run(cid))[0] == "pass"
    status, codes, paths, _ = _summary(built.run(cid, **{"roles-sync": "fail"}))
    assert (status, codes) == ("fail", ["E_PERM_GENERATED_PATH"])
    assert paths == [".github/CODEOWNERS", ".github/ISSUE_TEMPLATE/task.yml"]


def test_authorization_uses_the_base_roles_even_if_the_pr_changes_them(built: _Built) -> None:
    sc = built.get("ci-04-roles-self-grant")
    assert sc.pr_dir is not None and sc.checked_out
    inp = built.input("ci-04-roles-self-grant")
    assert "E_PERM_SECTION_NOT_OWNED" in {f.code for f in scope.run(inp)}
    # Control: con la política de la cabeza (lo que la regla NO debe hacer), results sería de carla.
    head_policy = read_policy_dir(sc.pr_dir, t=translator("es"))
    assert head_policy.roles.section_owner()["results"] == "carla"
    head_codes = {f.code for f in scope.run(replace(inp, policy=head_policy))}
    assert "E_PERM_SECTION_NOT_OWNED" not in head_codes


def test_rename_is_delete_of_old_plus_create_of_new(built: _Built) -> None:
    into = _errors(built.run("a9-07-rename-into-foreign-section"))
    out_of = _errors(built.run("a9-08-rename-out-of-foreign-section"))
    assert [(f.code, f.path) for f in into] == [("E_PERM_SECTION_NOT_OWNED", "sections/results/sampling.tex")]
    assert [(f.code, f.path) for f in out_of] == [("E_PERM_SECTION_NOT_OWNED", "sections/results/tables.tex")]


def test_file_modes_come_from_git_ls_tree_not_from_the_api(built: _Built) -> None:
    inp = built.input("a9-15-symlink-own-section")
    assert inp.tree is not None and inp.tree.entries()["sections/methods/link.tex"] == "120000"
    lying = tuple(replace(ch, file_mode="100644") for ch in inp.changes)
    assert [f.code for f in scope.run(replace(inp, changes=lying))] == ["E_PERM_SYMLINK"]

    regular = built.input("ci-01-own-sections-and-note")
    lying = tuple(replace(ch, file_mode="120000") for ch in regular.changes)
    assert scope.run(replace(regular, changes=lying)) == []


def test_a_file_missing_from_the_head_tree_fails_closed(built: _Built) -> None:
    inp = built.input("ci-01-own-sections-and-note")
    ghost = (*inp.changes, Change("sections/methods/ghost.tex", "added"))
    findings = scope.run(replace(inp, changes=ghost))
    assert [(f.code, f.path) for f in findings] == [("E_INTERNAL_UNEXPECTED", "sections/methods/ghost.tex")]
    assert "ls-tree" in findings[0].message


def test_foreign_push_names_the_pusher(built: _Built) -> None:
    findings = _errors(built.run("ci-11-foreign-push"))
    assert [f.code for f in findings] == ["E_CLAIM_NOT_HELD"]
    assert findings[0].message.startswith("bruno ")


def test_activity_api_404_fails_closed(built: _Built) -> None:
    sc = built.fresh(_with("ci-01-own-sections-and-note", faults=[{"method": "list_ref_activity", "kind": "http_404"}]))
    assert sc.forge.list_ref_activity("task/17") is None
    findings = _run(sc)
    assert [f.code for f in findings] == ["E_INTERNAL_UNEXPECTED"]
    assert "task/17" in findings[0].message


def test_injected_activity_fault_is_an_error(built: _Built) -> None:
    findings = built.run("a9-17-activity-fault")
    assert [(f.code, f.severity) for f in findings] == [("E_INTERNAL_UNEXPECTED", "error")]


def test_forge_error_while_reading_the_task_fails_closed(built: _Built) -> None:
    sc = built.fresh(_with("ci-01-own-sections-and-note", faults=[{"method": "get_issue", "kind": "http_5xx"}]))
    findings = _run(sc)
    assert [f.code for f in findings] == ["E_INTERNAL_UNEXPECTED"]


def test_owner_sources_that_disagree_leave_the_task_without_claimant(built: _Built) -> None:
    # bruno creó la rama y carla tiene el marcador: owner_sources_disagree (04 §1.5).
    case = _with("ci-01-own-sections-and-note", pushes=[{"actor": "bruno", "type": "branch_creation"}])
    findings = _errors(_run(built.fresh(case)))
    assert {f.code for f in findings} == {"E_CLAIM_NOT_HELD"}


def test_more_than_the_api_file_limit_fails(built: _Built) -> None:
    inp = built.input("ci-01-own-sections-and-note")
    many = tuple(Change(f"sections/methods/gen/{i}.tex", "added") for i in range(scope.MAX_PR_FILES))
    findings = scope.run(replace(inp, changes=many))
    assert [f.code for f in findings] == ["E_INTERNAL_UNEXPECTED"]
    assert str(scope.MAX_PR_FILES) in findings[0].message
    # Por debajo del tope la regla evalúa cada archivo (aquí, ausentes del árbol).
    fewer = scope.run(replace(inp, changes=many[:10]))
    assert len(fewer) == 10 and all(str(scope.MAX_PR_FILES) not in f.message for f in fewer)


# -- cuerpo del Issue y casos de borde ------------------------------------------------


_REPEATED = "### Resumen\n\nUno.\n\n### Secciones\n\nmethods\n\n### Resumen\n\nDos.\n"
_PREAMBLE = (
    "Texto suelto antes del formulario.\n\n### Resumen\n\nResumen.\n\n### Secciones\n\nmethods\n\n"
    "### Criterio de terminado\n\n- Compila.\n"
)
_NO_SECTIONS = "### Resumen\n\nResumen.\n\n### Secciones\n\n_No response_\n\n### Criterio de terminado\n\n- Compila.\n"


def _task_case(body: str) -> dict[str, Any]:
    case = _case("ci-01-own-sections-and-note")
    case["task"] = {**case["task"], "body": body}
    return case


def test_repeated_field_in_the_issue_body_is_an_error(built: _Built) -> None:
    findings = _run(built.fresh(_task_case(_REPEATED)))
    codes = [f.code for f in _errors(findings)]
    assert codes[0] == "E_VALIDATION_TASK_BODY"
    assert "summary" in findings[0].message
    # Sin TaskSpec la tarea no declara secciones: la sección propia queda fuera de alcance.
    assert "E_PERM_OUTSIDE_TASK" in codes


def test_text_before_the_form_is_only_a_warning(built: _Built) -> None:
    findings = _run(built.fresh(_task_case(_PREAMBLE)))
    assert _summary(findings) == ("pass", [], [], ["W_VALIDATION_TASK_UNKNOWN_FIELD"])


def test_task_without_sections_warns_and_cannot_touch_sections(built: _Built) -> None:
    findings = _run(built.fresh(_task_case(_NO_SECTIONS)))
    status, codes, _paths, warnings = _summary(findings)
    assert (status, codes, warnings) == ("fail", ["E_PERM_OUTSIDE_TASK"], ["W_VALIDATION_TASK_NO_SECTIONS"])


def test_cancelled_task_reports_the_state_not_a_foreign_claim(built: _Built) -> None:
    findings = _errors(built.run("ci-17-state-cancelled"))
    assert [f.code for f in findings] == ["E_STATE_INVALID_TRANSITION"]
    assert findings[0].path is None


def test_bad_state_and_foreign_author_are_both_reported(built: _Built) -> None:
    case = _with("ci-16-state-expired", pr={"author": "bruno", "draft": True})
    codes = {f.code for f in _errors(_run(built.fresh(case)))}
    assert codes == {"E_STATE_INVALID_TRANSITION", "E_CLAIM_NOT_HELD"}


# -- entradas que la regla no puede evaluar ------------------------------------------


@pytest.fixture
def base_input(built: _Built) -> RuleInput:
    return built.input("ci-01-own-sections-and-note")


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        ({"mode": "tree"}, "modo pr"),
        ({"policy": None}, "política"),
        ({"pr": None}, "PR"),
        ({"forge": None}, "forja"),
    ],
)
def test_missing_inputs_fail_closed(base_input: RuleInput, change: dict[str, object], needle: str) -> None:
    findings = scope.run(replace(base_input, **change))  # type: ignore[arg-type]
    assert [f.code for f in findings] == ["E_INTERNAL_UNEXPECTED"]
    assert needle in findings[0].message


def test_tree_that_is_not_the_pr_head_fails_closed(base_input: RuleInput) -> None:
    assert base_input.pr is not None
    pr = replace(base_input.pr, head_sha="0" * 39 + "1")
    findings = scope.run(replace(base_input, pr=pr))
    assert [f.code for f in findings] == ["E_INTERNAL_UNEXPECTED"]


def test_each_foreign_pusher_is_reported_once(built: _Built) -> None:
    pushes = [
        {"actor": "carla", "type": "push"},
        {"actor": "bruno", "type": "push"},
        {"actor": "BRUNO", "type": "force_push"},
        {"actor": None, "type": "push"},
    ]
    findings = _errors(_run(built.fresh(_with("ci-11-foreign-push", pushes=pushes))))
    assert [f.code for f in findings] == ["E_CLAIM_NOT_HELD", "E_CLAIM_NOT_HELD"]
    assert findings[0].message.startswith("bruno ")
    assert "desconocido" in findings[1].message


@pytest.mark.parametrize("code", sorted(scope._PR_LEVEL))
def test_pr_level_denials_have_a_fail_closed_fallback(base_input: RuleInput, code: str) -> None:
    # Normalmente las informa la comprobación del PR; si no, la primera ruta la informa una vez.
    task = Task(17, "carla", "claimed", frozenset({"methods"}))
    reported: set[str] = set()
    d = Decision(False, code, "sections/methods/methods.tex")
    ch = Change("sections/methods/methods.tex", "modified")
    first = scope._denial(base_input, d, ch, task, "carla", reported)
    assert first is not None and (first.code, first.path) == (code, "sections/methods/methods.tex")
    assert scope._denial(base_input, d, ch, task, "carla", reported) is None
    assert scope._denial(base_input, d, ch, None, "carla", set()) is not None


@pytest.mark.parametrize("code", [None, "E_INTERNAL_UNEXPECTED", "E_PERM_NOT_IN_THE_CONTRACT"])
def test_unexpected_denials_are_errors(base_input: RuleInput, code: str | None) -> None:
    d = Decision(False, code, None)
    f = scope._denial(base_input, d, Change("a/b.tex", "added"), None, "carla", set())
    assert f is not None and (f.code, f.severity, f.path) == ("E_INTERNAL_UNEXPECTED", "error", "a/b.tex")


def test_finding_paths_are_repo_paths_or_none() -> None:
    assert scope._path("tex/a.tex") == "tex/a.tex"
    assert scope._path(None) is None
    assert scope._path("") is None
    assert scope._path("/etc/passwd") is None
    assert scope._path("tex\\a.tex") is None


def test_generated_matches_needs_both_priors_in_pass() -> None:
    assert scope.generated_matches({"roles-sync": "pass", "generated-sync": "pass"}) is True
    assert scope.generated_matches({"roles-sync": "pass"}) is False
    assert scope.generated_matches({"roles-sync": "fail", "generated-sync": "pass"}) is False
    assert scope.generated_matches({}) is False


# -- declaración y catálogo -----------------------------------------------------------


def test_rule_declaration() -> None:
    assert scope.RULES == (scope.RULE,)
    assert scope.RULE.id == "scope"
    assert scope.RULE.modes == frozenset({"pr"})
    assert scope.RULE.after == ("roles-sync", "generated-sync")
    assert scope.RULE.needs_forge is True
    assert scope.MAX_PR_FILES == 3000


@pytest.mark.parametrize("lang", ["es", "en"])
def test_catalog_has_every_finding_with_the_declared_placeholders(lang: str) -> None:
    cat = load_catalog(lang)  # type: ignore[arg-type]
    for code, params in scope.CODES.items():
        assert placeholders(cat[f"finding.{code}.message"]) == params, code
        hint = cat.get(f"finding.{code}.hint")
        if hint is not None:
            assert placeholders(hint) <= params, code


def test_catalog_has_every_scope_key_used_by_the_rule() -> None:
    source = (REPO_ROOT / "src" / "aucove" / "ops" / "rules" / "scope.py").read_text(encoding="utf-8")
    used = set(re.findall(r"""t\(\s*["'](scope\.[a-z_.]+)["']""", source)) | {"scope.body.repeated_field"}
    es, en = load_catalog("es"), load_catalog("en")
    for key in sorted(used):
        assert key in es and key in en, key
        assert placeholders(es[key]) == placeholders(en[key]), key
    scope_keys = {k for k in es if k.startswith("scope.")}
    assert scope_keys == {k for k in en if k.startswith("scope.")}
    assert scope_keys == used
