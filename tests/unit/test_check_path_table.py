# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
# Pruebas de domain.permissions: la tabla de casos de 06 §2.4, los vectores de 02 §3.6 a través
# de check_path (REQ-PERM-54), la regla 5 (generated), REQ-PERM-11 y check_change.
from __future__ import annotations

import dataclasses
import importlib.util
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from aucove.domain import permissions
from aucove.domain.permissions import (
    CONTROL_DIRS,
    CONTROL_FILES,
    WRITABLE,
    Decision,
    Policy,
    Task,
    allowed,
    check_change,
    check_path,
    is_control_path,
)


def _load_vectors() -> ModuleType:
    # test_paths.py tiene los vectores de 02 §3.6; se carga por ruta para no depender del modo
    # de importación de pytest.
    name = "_p02_convention_vectors"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("test_paths.py"))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


V = _load_vectors()
NATIVE = V.NATIVE
PKG = V.PKG


def no_links(rel: str) -> bool:
    return False


def make_policy(
    env: Any = NATIVE,
    *,
    mode: str = "session",
    people: frozenset[str] = frozenset({"ana", "bruno", "carla"}),
    lead: str = "ana",
    deputy: str | None = "bruno",
    sections: dict[str, str] | None = None,
    base_decisions: frozenset[str] = frozenset({"memory/decisions/2026-09-01-scope.md"}),
    display: dict[str, str] | None = None,
    link_probe: Callable[[str], bool] | None = no_links,
) -> Policy:
    """Política del ejemplo de 06 §2.1 en el entorno dado; en modo ci, sin sonda de enlaces
    (en session y commit la sonda es obligatoria: sin ella, check_path deniega todo)."""
    return Policy(
        people=people,
        lead=lead,
        deputy=deputy,
        sections=(
            sections
            if sections is not None
            else {"introduction": "ana", "methods": "carla", "results": "bruno"}
        ),
        base_decisions=base_decisions,
        root=env.root,
        ci_fs=env.ci_fs,
        display=display or {},
        case_map=V.case_map_for(env),
        link_probe=None if mode == "ci" else link_probe,
    )


def claimed(number: int, claimant: str, *sections: str, state: str = "claimed") -> Task:
    return Task(number, claimant, state, frozenset(sections))


TASKS: dict[int, Task] = {
    12: claimed(12, "carla", "methods"),
    15: claimed(15, "bruno", "results", "methods"),
    18: claimed(18, "ana", "introduction"),
    22: claimed(22, "carla", "results"),
}


@dataclass(frozen=True)
class Case:
    id: str
    login: str
    raw: str
    task: int | None
    mode: Any
    allow: bool
    code: str | None
    op: Any = "modify"
    generated_matches: bool = False
    expect: dict[str, object] = field(default_factory=dict)  # atributos de Decision


# Tabla de 06 §2.4. 'op' es create en las rutas marcadas "(nuevo)" y modify en las demás.
OUT_TASK, NOT_OWNED = "E_PERM_OUTSIDE_TASK", "E_PERM_SECTION_NOT_OWNED"
TABLE: tuple[Case, ...] = (
    Case("1", "carla", "sections/methods/main.tex", 12, "session", True, None,
         expect={"path_class": "section", "section": "methods", "owner": "carla"}),
    Case("2", "carla", "sections/results/fig1.pdf", 12, "session", False, OUT_TASK,
         expect={"section": "results", "owner": "bruno"}),
    Case("3", "bruno", "sections/methods/main.tex", 15, "session", False, NOT_OWNED,
         expect={"owner": "carla"}),
    Case("4", "carla", "sections/results/a.tex", 22, "session", False, NOT_OWNED,
         expect={"owner": "bruno"}),
    Case("5", "bruno", "sections/methods/main.tex", 12, "session", False, "E_CLAIM_NOT_HELD"),
    Case("6", "carla", "sections/methods/main.tex", None, "session", False,
         "E_STATE_NOT_ON_TASK_BRANCH"),
    Case("7", "carla", "tex\\sections\\methods\\..\\..\\..\\plan.md", 12, "session", False,
         "E_PERM_LEAD_ONLY_PATH", expect={"path": "plan.md", "path_class": "lead"}),
    Case("8", "ana", "plan.md", 18, "session", True, None, expect={"confirm": False}),
    Case("9", "ana", "roles.yaml", 18, "session", True, None, expect={"confirm": True}),
    Case("9b", "ana", ".claude/settings.json", 18, "session", True, None,
         expect={"confirm": True}),
    Case("10", "bruno", "tex/notation.tex", 15, "session", True, None,
         expect={"path_class": "editor", "confirm": False}),
    Case("11", "carla", "roles.yaml", 12, "ci", False, "E_PERM_LEAD_ONLY_PATH"),
    Case("12", "carla", "memory/decisions/2026-09-29-prior.md", 12, "session", True, None,
         op="create", expect={"path_class": "append"}),
    Case("13", "carla", "memory/decisions/2026-09-01-scope.md", 12, "ci", False,
         "E_PERM_APPEND_ONLY"),
    Case("14", "carla", "memory/notes/task-15.md", 12, "session", False, OUT_TASK),
    Case("15", "ana", ".github/CODEOWNERS", 18, "session", False, "E_PERM_GENERATED_PATH",
         generated_matches=True),
    Case("16", "ana", ".github/CODEOWNERS", 18, "ci", True, None, generated_matches=True,
         expect={"path_class": "generated"}),
    Case("16-nomatch", "ana", ".github/CODEOWNERS", 18, "ci", False, "E_PERM_GENERATED_PATH"),
    Case("16b", "ana", ".github/CODEOWNERS", 18, "commit", True, None, generated_matches=True),
    *(
        Case(f"17-{who}-{i}", who, raw, None, "session", False, "E_PERM_LOCAL_PATH",
             expect={"path_class": "local"})
        for who in ("ana", "bruno", "carla")
        for i, raw in enumerate((".claude/settings.local.json", f".{PKG}/cache/identity.json"))
    ),
    Case("18", "ana", "sections/discussion/draft.tex", 18, "session", True, None,
         expect={"path_class": "lead", "section": None}),
    *(
        Case(f"19-{i}", "dave", raw, None, "session", False, "E_PERM_NOT_A_MEMBER")
        for i, raw in enumerate(
            (
                "sections/methods/main.tex",
                "plan.md",
                ".github/CODEOWNERS",
                ".claude/settings.local.json",
                "memory/notes/task-12.md",
            )
        )
    ),
    Case("20", "carla", ".git/hooks/pre-commit", 12, "session", False, "E_PERM_OUTSIDE_REPO"),
)


def run_case(c: Case, env: Any = NATIVE) -> Decision:
    task = TASKS[c.task] if c.task is not None else None
    pol = make_policy(env, mode=c.mode)
    return check_path(
        c.login,
        c.raw,
        c.op,
        task,
        pol=pol,
        mode=c.mode,
        generated_matches=c.generated_matches,
    )


def assert_case(c: Case, d: Decision) -> None:
    assert (d.allow, d.code) == (c.allow, c.code), d
    for attr, value in c.expect.items():
        assert getattr(d, attr) == value, (attr, d)
    assert d.requires_approval_of == frozenset()
    if not c.allow:
        assert d.confirm is False


def test_table_covers_every_row_of_06() -> None:
    ids = {c.id.split("-")[0] for c in TABLE}
    assert ids >= {str(i) for i in range(1, 21)} | {"9b", "16b"}
    assert len(TABLE) >= 20


@pytest.mark.parametrize("case", TABLE, ids=lambda c: c.id)
def test_check_path_table(case: Case) -> None:
    assert_case(case, run_case(case))


@pytest.mark.parametrize("env", [V.ENV_WINDOWS, V.ENV_LINUX], ids=lambda e: e.name)
@pytest.mark.parametrize("case", TABLE, ids=lambda c: c.id)
def test_check_path_table_emulated(case: Case, env: object) -> None:
    assert_case(case, run_case(case, env))


# --- vectores de 02 §3.6 a través de check_path (REQ-PERM-54) -------------------------------


def _vector_through_check_path(vec: Any, env: Any) -> None:
    pol = make_policy(
        env,
        people=frozenset({"ana", "carla"}),
        lead="ana",
        deputy=None,
        sections={"methods": "carla"},
    )
    pol = dataclasses.replace(pol, root=vec.root_for(env))
    task = Task(12, "carla", "claimed", frozenset({"methods"}))
    d = check_path("carla", vec.raw_for(env), "modify", task, pol=pol, mode="session")
    if vec.canonical is None:  # OutsideRepo
        assert (d.allow, d.code) == (False, "E_PERM_OUTSIDE_REPO")
        assert d.path_class == "outside"
    else:
        assert (d.path, d.path_class) == (vec.canonical, vec.cls)


@pytest.mark.parametrize(("vec", "env"), V.native_params())
def test_check_path_follows_convention_vectors(vec: object, env: object) -> None:
    _vector_through_check_path(vec, env)


@pytest.mark.parametrize(("vec", "env"), V.emulated_params())
def test_check_path_follows_convention_vectors_emulated(vec: object, env: object) -> None:
    _vector_through_check_path(vec, env)


# --- regla 5: generated ---------------------------------------------------------------------

GENERATED = (".github/CODEOWNERS", ".github/ISSUE_TEMPLATE/task.yml", ".claude/skills/x/SKILL.md")


@pytest.mark.parametrize("raw", GENERATED)
@pytest.mark.parametrize("mode", ["session", "commit", "ci"])
@pytest.mark.parametrize("op", ["create", "modify", "delete"])
@pytest.mark.parametrize("login", ["ana", "bruno", "carla"])
def test_generated_denied_without_match(raw: str, mode: Any, op: Any, login: str) -> None:
    d = check_path(login, raw, op, None, pol=make_policy(mode=mode), mode=mode)
    assert (d.allow, d.code, d.path_class) == (False, "E_PERM_GENERATED_PATH", "generated")


@pytest.mark.parametrize("raw", GENERATED)
@pytest.mark.parametrize("mode", ["session", "commit", "ci"])
@pytest.mark.parametrize("op", ["create", "modify", "delete"])
@pytest.mark.parametrize("login", ["ana", "bruno", "carla"])
def test_generated_rule_5_with_match(raw: str, mode: Any, op: Any, login: str) -> None:
    pol = make_policy(mode=mode)
    d = check_path(login, raw, op, None, pol=pol, mode=mode, generated_matches=True)
    expected = op != "delete" and mode in ("commit", "ci") and login in ("ana", "bruno")
    assert d.allow is expected
    assert d.code == (None if expected else "E_PERM_GENERATED_PATH")
    assert d.path_class == "generated"


@pytest.mark.parametrize("value", ["pass", 1, [True], object()])
def test_generated_matches_must_be_true(value: Any) -> None:
    d = check_path("ana", ".github/CODEOWNERS", "modify", None, pol=make_policy(mode="ci"),
                   mode="ci", generated_matches=value)
    assert (d.allow, d.code) == (False, "E_PERM_GENERATED_PATH")


def test_generated_needs_no_task_but_membership() -> None:
    pol = make_policy(mode="ci")
    d = check_path("dave", ".github/CODEOWNERS", "modify", None, pol=pol, mode="ci",
                   generated_matches=True)
    assert d.code == "E_PERM_NOT_A_MEMBER"


# --- REQ-PERM-11: una excepción interna deniega ----------------------------------------------


def _boom(rel: str) -> bool:
    raise RuntimeError("sonda rota")


@pytest.mark.parametrize(
    "call",
    [
        lambda: check_path("ana", "plan.md", "modify", TASKS[18],
                           pol=make_policy(link_probe=_boom), mode="session"),
        lambda: check_path("ana", "plan.md", "rename", TASKS[18],
                           pol=make_policy(), mode="session"),
        lambda: check_path("ana", "plan.md", "modify", TASKS[18],
                           pol=make_policy(), mode="push"),
        lambda: check_path(None, "plan.md", "modify", TASKS[18],
                           pol=make_policy(), mode="session"),
        lambda: check_path("ana", 42, "modify", TASKS[18],
                           pol=make_policy(), mode="session"),
        lambda: check_path("ana", "plan.md", "modify", object(),
                           pol=make_policy(), mode="session"),
        lambda: check_path("ana", "plan.md", "modify", TASKS[18],
                           pol=None, mode="session"),
        # sin sonda de enlaces en session o commit no se puede descartar un enlace (REQ-CONV-10)
        lambda: check_path("ana", "plan.md", "modify", TASKS[18],
                           pol=make_policy(link_probe=None), mode="session"),
        lambda: check_path("ana", "plan.md", "modify", TASKS[18],
                           pol=make_policy(link_probe=None), mode="commit"),
    ],
    ids=["probe-raises", "bad-op", "bad-mode", "login-none", "path-not-str", "bad-task", "no-pol",
         "no-probe-session", "no-probe-commit"],
)
def test_internal_exception_denies(call: Callable[[], Decision]) -> None:
    d = call()
    assert isinstance(d, Decision)
    assert (d.allow, d.code) == (False, "E_INTERNAL_UNEXPECTED")
    assert d.path in ("plan.md", None)


def test_internal_exception_in_check_change_denies() -> None:
    ds = check_change("ana", "renamed", "plan.md", None, TASKS[18], pol=make_policy(),
                      mode="commit")
    assert [(d.allow, d.code, d.path) for d in ds] == [(False, "E_INTERNAL_UNEXPECTED", "plan.md")]
    ds = check_change("ana", "X", "plan.md", None, TASKS[18], pol=make_policy(), mode="commit")
    assert [(d.allow, d.code) for d in ds] == [(False, "E_INTERNAL_UNEXPECTED")]
    ds = check_change("ana", None, 7, None, TASKS[18], pol=make_policy(),
                      mode="commit")
    assert [(d.allow, d.code, d.path) for d in ds] == [(False, "E_INTERNAL_UNEXPECTED", None)]
    ds = check_change("ana", "M", 7, None, TASKS[18], pol=make_policy(), mode="commit")
    assert [(d.allow, d.code, d.path) for d in ds] == [(False, "E_INTERNAL_UNEXPECTED", None)]


# --- renombres y check_change ---------------------------------------------------------------


def test_rename_between_sections_denied_even_if_destination_is_own() -> None:
    # bruno (#15 declara results y methods) mueve un archivo de methods (de carla) a results (suyo)
    for mode in ("session", "commit", "ci"):
        ds = check_change(
            "bruno",
            "renamed",
            "sections/results/a.tex",
            "sections/methods/a.tex",
            TASKS[15],
            pol=make_policy(mode=mode),
            mode=mode,
        )
        assert [(d.allow, d.code, d.path) for d in ds] == [
            (False, NOT_OWNED, "sections/methods/a.tex"),
            (True, None, "sections/results/a.tex"),
        ]
        assert ds[0].owner == "carla"
        assert not allowed(ds)


def test_rename_from_undeclared_section_into_own() -> None:
    # carla (#12 declara methods) trae un archivo de results (fuera de su tarea) a methods
    ds = check_change("carla", "R100", "sections/methods/b.tex",
                      "sections/results/b.tex", TASKS[12], pol=make_policy(), mode="commit")
    assert [(d.allow, d.code) for d in ds] == [(False, OUT_TASK), (True, None)]
    assert not allowed(ds)


def test_rename_within_own_section_allowed() -> None:
    ds = check_change("carla", "renamed", "sections/methods/new.tex",
                      "sections/methods/old.tex", TASKS[12], pol=make_policy(), mode="commit")
    assert allowed(ds) and len(ds) == 2


def test_rename_of_generated_is_denied_even_when_matching() -> None:
    ds = check_change("ana", "renamed", ".github/CODEOWNERS2", ".github/CODEOWNERS", TASKS[18],
                      pol=make_policy(mode="ci"), mode="ci", generated_matches=True)
    assert ds[0].code == "E_PERM_GENERATED_PATH"
    assert not allowed(ds)


@pytest.mark.parametrize(
    ("status", "ops"),
    [
        ("added", ["create"]),
        ("A", ["create"]),
        ("modified", ["modify"]),
        ("M", ["modify"]),
        ("T", ["modify"]),
        ("changed", ["modify"]),
        ("unchanged", ["modify"]),
        ("removed", ["delete"]),
        ("deleted", ["delete"]),
        ("D", ["delete"]),
        ("copied", ["create"]),
        ("C075", ["create"]),
    ],
)
def test_check_change_status_maps_to_op(status: str, ops: list[str]) -> None:
    # check_path no distingue create de modify; delete sí se distingue (nota propia en commit)
    path = "memory/decisions/2026-09-01-scope.md"
    ds = check_change("carla", status, path, "memory/decisions/src.md", TASKS[12],
                      pol=make_policy(), mode="commit")
    assert len(ds) == len(ops)
    codes = [d.code for d in ds]
    assert codes == ["E_PERM_APPEND_ONLY"] * len(ops)  # la base ya la tiene: nadie la toca
    # con una ruta nueva, create y modify pasan y se distingue delete por la nota propia
    note = "memory/notes/task-12.md"
    ds = check_change("carla", status, note, None, TASKS[12], pol=make_policy(), mode="commit")
    expected_allow = ops != ["delete"]
    assert [d.allow for d in ds] == [expected_allow]


@pytest.mark.parametrize("status", ["R", "R100", "R050", "renamed"])
def test_check_change_rename_statuses(status: str) -> None:
    ds = check_change("carla", status, "sections/methods/n.tex", "sections/methods/o.tex",
                      TASKS[12], pol=make_policy(), mode="commit")
    assert [d.path for d in ds] == ["sections/methods/o.tex", "sections/methods/n.tex"]


@pytest.mark.parametrize(
    "status", ["X", "U", "B", "", "Rxx", "R\u00b2", "R\u0661", "renamed-ish", "r100"]
)
def test_check_change_unknown_status_fails_closed(status: str) -> None:
    ds = check_change("carla", status, "sections/methods/n.tex", None, TASKS[12],
                      pol=make_policy(), mode="commit")
    assert [(d.allow, d.code) for d in ds] == [(False, "E_INTERNAL_UNEXPECTED")]


def test_check_change_file_mode_applies_to_destination_only() -> None:
    ds = check_change("carla", "renamed", "sections/methods/n.tex",
                      "sections/methods/o.tex", TASKS[12], pol=make_policy(mode="ci"),
                      mode="ci", file_mode="120000")
    assert [(d.allow, d.code) for d in ds] == [(True, None), (False, "E_PERM_SYMLINK")]


# --- check_change: '\' en rutas de git ------------------------------------------------------
# check_change recibe rutas de git (API de archivos del PR o git diff --name-status), donde '/'
# es el único separador y '\' es un carácter más del nombre (válido en Linux; git para Windows
# no puede extraerlo). canonical (02 §3.1) convierte '\' en '/' porque también recibe rutas del
# SO; si check_change no deniega antes, 'sections\methods\x.tex', que en git es un archivo
# de la raíz (lead), se lee como de la parte methods (REQ-CONV-7).

OUTSIDE = "E_PERM_OUTSIDE_REPO"
GIT_BACKSLASH = "sections\\methods\\x.tex"
GIT_ENVS = [V.ENV_WINDOWS, V.ENV_LINUX, V.ENV_MACOS]
_DELETES = frozenset({"removed", "deleted", "D"})


def _rows(ds: list[Decision]) -> list[tuple[bool, str | None, str | None, str | None]]:
    return [(d.allow, d.code, d.path, d.path_class) for d in ds]


@pytest.mark.parametrize("mode", ["ci", "commit"])
@pytest.mark.parametrize(
    "status",
    ["added", "A", "modified", "M", "T", "changed", "removed", "deleted", "D", "copied", "C100"],
)
@pytest.mark.parametrize(
    ("login", "task", "raw"),
    [
        # antes: parte propia (sections/methods/x.tex), permitido
        ("carla", 12, GIT_BACKSLASH),
        ("carla", 12, "sections/methods\\x.tex"),
        ("carla", 12, "sections\\methods/x.tex"),
        # antes: decisión nueva (append), permitida
        ("carla", 12, "memory\\decisions\\2026-10-02-x.md"),
        # antes: ruta lead de la raíz (x/y.tex) para la lead, permitida
        ("ana", 18, "x\\y.tex"),
        ("ana", 18, "notes\\plan.md"),
        # antes: OutsideRepo por absoluta; sigue denegada, ahora por la '\'
        ("ana", 18, "\\plan.md"),
    ],
)
def test_check_change_denies_backslash_in_git_paths(
    mode: str, status: str, login: str, task: int, raw: str
) -> None:
    fm = None if status in _DELETES else "100644"
    for env in GIT_ENVS:
        ds = check_change(login, status, raw, None, TASKS[task],
                          pol=make_policy(env, mode=mode), mode=mode, file_mode=fm)
        assert _rows(ds) == [(False, OUTSIDE, raw, "outside")], (env.name, ds)
        assert not allowed(ds)


@pytest.mark.parametrize("mode", ["ci", "commit"])
@pytest.mark.parametrize("status", ["renamed", "R", "R100", "R050"])
def test_check_change_rename_with_backslash_origin_still_checks_destination(
    mode: str, status: str
) -> None:
    for env in GIT_ENVS:
        pol = make_policy(env, mode=mode)
        old = "sections\\methods\\old.tex"
        ds = check_change("carla", status, "sections/methods/new.tex", old, TASKS[12],
                          pol=pol, mode=mode, file_mode="100644")
        assert _rows(ds) == [
            (False, OUTSIDE, old, "outside"),
            (True, None, "sections/methods/new.tex", "section"),
        ], env.name
        assert not allowed(ds)
        # el destino se sigue evaluando con sus reglas: results no es de la tarea 12
        ds = check_change("carla", status, "sections/results/a.tex", old, TASKS[12],
                          pol=pol, mode=mode, file_mode="100644")
        assert _rows(ds) == [
            (False, OUTSIDE, old, "outside"),
            (False, OUT_TASK, "sections/results/a.tex", "section"),
        ], env.name
        # y con su modo: el modo del cambio es el del destino
        ds = check_change("carla", status, "sections/methods/new.tex", old, TASKS[12],
                          pol=pol, mode=mode, file_mode="120000")
        assert [(d.allow, d.code) for d in ds] == [(False, OUTSIDE), (False, "E_PERM_SYMLINK")]


@pytest.mark.parametrize("mode", ["ci", "commit"])
@pytest.mark.parametrize("status", ["renamed", "R", "R100", "R050"])
def test_check_change_rename_to_backslash_destination_still_checks_origin(
    mode: str, status: str
) -> None:
    new = "sections\\methods\\tables.tex"
    for env in GIT_ENVS:
        pol = make_policy(env, mode=mode)
        # antes: create(sections/methods/tables.tex) se permitía como parte propia
        ds = check_change("carla", status, new, "sections/results/tables.tex", TASKS[12],
                          pol=pol, mode=mode, file_mode="100644")
        assert _rows(ds) == [
            (False, OUT_TASK, "sections/results/tables.tex", "section"),
            (False, OUTSIDE, new, "outside"),
        ], env.name
        ds = check_change("carla", status, new, "sections/methods/old.tex", TASKS[12],
                          pol=pol, mode=mode, file_mode="100644")
        assert _rows(ds) == [
            (True, None, "sections/methods/old.tex", "section"),
            (False, OUTSIDE, new, "outside"),
        ], env.name
        assert not allowed(ds)


@pytest.mark.parametrize("mode", ["ci", "commit"])
@pytest.mark.parametrize("status", ["renamed", "R", "R100", "R050"])
def test_check_change_rename_between_backslash_names_denies_both(mode: str, status: str) -> None:
    old = "sections\\methods\\old.tex"
    for env in GIT_ENVS:
        ds = check_change("carla", status, GIT_BACKSLASH, old, TASKS[12],
                          pol=make_policy(env, mode=mode), mode=mode, file_mode="100644")
        assert _rows(ds) == [
            (False, OUTSIDE, old, "outside"),
            (False, OUTSIDE, GIT_BACKSLASH, "outside"),
        ], env.name


def test_check_change_backslash_rule_does_not_depend_on_mode() -> None:
    # check_change solo recibe rutas de git, también si lo llaman en session
    ds = check_change("carla", "added", GIT_BACKSLASH, None, TASKS[12],
                      pol=make_policy(V.ENV_WINDOWS), mode="session")
    assert _rows(ds) == [(False, OUTSIDE, GIT_BACKSLASH, "outside")]


def test_check_change_backslash_keeps_internal_errors() -> None:
    # un estado desconocido, un renombre sin origen o un modo inválido siguen siendo
    # E_INTERNAL_UNEXPECTED, como en check_path
    pol = make_policy(V.ENV_LINUX, mode="ci")
    for status, prev in (("X", None), ("renamed", None), ("R100", "")):
        ds = check_change("carla", status, GIT_BACKSLASH, prev, TASKS[12], pol=pol, mode="ci")
        assert _rows(ds) == [(False, "E_INTERNAL_UNEXPECTED", GIT_BACKSLASH, None)]
    ds = check_change("carla", "added", GIT_BACKSLASH, None, TASKS[12], pol=pol,
                      mode="push")  # type: ignore[arg-type]
    assert _rows(ds) == [(False, "E_INTERNAL_UNEXPECTED", GIT_BACKSLASH, None)]
    ds = check_change("carla", "added", "sections/methods/x.tex", None, TASKS[12], pol=pol,
                      mode="push")  # type: ignore[arg-type]
    assert _rows(ds) == [(False, "E_INTERNAL_UNEXPECTED", "sections/methods/x.tex", None)]


@pytest.mark.parametrize(
    "raw", [GIT_BACKSLASH, "C:\\Users\\ana\\p\\sections\\methods\\x.tex",
            "c:\\users\\ANA\\p\\sections/methods\\x.tex"],
)
def test_check_path_still_normalizes_os_paths_with_backslash(raw: str) -> None:
    # check_path recibe rutas del SO (el hook en session): en Windows '\' es el separador
    d = check_path("carla", raw, "create", TASKS[12], pol=make_policy(V.ENV_WINDOWS),
                   mode="session")
    assert (d.allow, d.code, d.path, d.path_class, d.section, d.owner) == (
        True, None, "sections/methods/x.tex", "section", "methods", "carla")


# --- reglas sueltas de 06 §2.4 --------------------------------------------------------------


def test_outside_repo_keeps_raw_path() -> None:
    d = check_path("carla", "../otro/a.tex", "modify", TASKS[12], pol=make_policy(),
                   mode="session")
    assert (d.allow, d.code, d.path, d.path_class) == (
        False, "E_PERM_OUTSIDE_REPO", "../otro/a.tex", "outside")
    for raw in ("", "a\x00b", "~/x"):
        d = check_path("carla", raw, "create", TASKS[12], pol=make_policy(), mode="session")
        assert (d.allow, d.code) == (False, "E_PERM_OUTSIDE_REPO")


@pytest.mark.parametrize(
    "raw",
    [".git/config", ".git", ".GIT/hooks/pre-commit", ".Git/x",
     # en cualquier segmento, como verify_path de git (repo anidado)
     "sections/methods/.git/config", "sections/methods/.git", "tex/.GIT/hooks/x"],
)
def test_git_dir_is_outside_repo_for_everyone(raw: str) -> None:
    for login in ("ana", "bruno", "carla"):
        d = check_path(login, raw, "modify", TASKS[18], pol=make_policy(), mode="session")
        assert (d.allow, d.code, d.path_class) == (False, "E_PERM_OUTSIDE_REPO", "lead")


@pytest.mark.parametrize(
    "raw",
    [".git./hooks/pre-commit", ".git::$INDEX_ALLOCATION/hooks/pre-push", "GIT~1/hooks/x",
     ".git /hooks/y", "sections/methods/CLAUDE.md.", "sections/methods/CLAUDE~1/r.md",
     "roles.yaml.", ".github./workflows/ci.yml", "sections/methods/aux.tex"],
)
def test_windows_aliases_are_outside_repo_in_every_mode(raw: str) -> None:
    # Win32 quita el punto o espacio final, ':' abre flujos y NTFS resuelve los nombres 8.3:
    # canonical los rechaza en toda plataforma (InvalidPath)
    for env in (V.ENV_WINDOWS, V.ENV_LINUX):
        for mode in ("session", "commit", "ci"):
            for login, task in (("ana", 18), ("carla", 12)):
                d = check_path(login, raw, "create", TASKS[task],
                               pol=make_policy(env, mode=mode), mode=mode)
                assert (d.allow, d.code, d.path_class) == (
                    False, "E_PERM_OUTSIDE_REPO", "outside"), d


def test_symlink_from_file_mode_in_every_mode() -> None:
    for mode in ("session", "commit", "ci"):
        d = check_path("carla", "sections/methods/l.tex", "create", TASKS[12],
                       pol=make_policy(mode=mode), mode=mode, file_mode="120000")
        assert (d.allow, d.code, d.path_class) == (False, "E_PERM_SYMLINK", "lead")
    d = check_path("carla", "sections/methods/l.tex", "create", TASKS[12],
                   pol=make_policy(mode="ci"), mode="ci", file_mode="100755")
    assert d.allow


def test_symlink_probe_in_session_and_commit_but_not_ci() -> None:
    seen: list[str] = []

    def probe(rel: str) -> bool:
        seen.append(rel)
        return rel.startswith("sections/methods/linked/")

    raw = "sections/methods/linked/x.tex"
    for mode in ("session", "commit"):
        pol = make_policy(link_probe=probe)
        d = check_path("carla", raw, "modify", TASKS[12], pol=pol, mode=mode)
        assert (d.allow, d.code) == (False, "E_PERM_SYMLINK")
    assert seen == [raw, raw]
    # en ci el modo sale de ls-tree: la sonda no se consulta aunque exista
    pol = make_policy(link_probe=probe)
    d = check_path("carla", raw, "modify", TASKS[12], pol=pol, mode="ci")
    assert d.allow and seen == [raw, raw]


def test_probe_receives_canonical_case_mapped_path() -> None:
    seen: list[str] = []
    pol = make_policy(V.ENV_WINDOWS, link_probe=lambda rel: seen.append(rel) or False)
    check_path("ana", "C:\\Users\\ana\\p\\TEX\\Main.TEX", "modify", TASKS[18], pol=pol,
               mode="session")
    assert seen == ["tex/main.tex"]


@pytest.mark.parametrize("state", sorted(WRITABLE))
def test_writable_states_allow(state: str) -> None:
    task = claimed(12, "carla", "methods", state=state)
    d = check_path("carla", "sections/methods/a.tex", "modify", task, pol=make_policy(),
                   mode="session")
    assert d.allow


@pytest.mark.parametrize("state", ["proposed", "open", "merged", "cancelled", "unknown"])
def test_non_writable_states_deny(state: str) -> None:
    task = claimed(12, "carla", "methods", state=state)
    d = check_path("carla", "sections/methods/a.tex", "modify", task, pol=make_policy(),
                   mode="session")
    assert (d.allow, d.code, d.section) == (False, "E_STATE_INVALID_TRANSITION", "methods")


def test_unclaimed_task_is_not_held() -> None:
    task = Task(12, None, "claimed", frozenset({"methods"}))
    d = check_path("carla", "sections/methods/a.tex", "modify", task, pol=make_policy(),
                   mode="session")
    assert d.code == "E_CLAIM_NOT_HELD"


@pytest.mark.parametrize(
    ("mode", "op", "note", "allow"),
    [
        ("session", "create", 12, False),  # la nota la escribe submit (DC-09)
        ("session", "modify", 12, False),
        ("commit", "create", 12, True),
        ("commit", "modify", 12, True),
        ("commit", "delete", 12, False),
        ("commit", "modify", 15, False),
        ("ci", "create", 12, True),
        ("ci", "modify", 12, True),
        ("ci", "delete", 12, False),
        ("ci", "create", 13, False),
    ],
)
def test_task_note_rule_9(mode: Any, op: Any, note: int, allow: bool) -> None:
    raw = f"memory/notes/task-{note}.md"
    d = check_path("carla", raw, op, TASKS[12], pol=make_policy(mode=mode), mode=mode)
    assert d.allow is allow
    assert d.code == (None if allow else OUT_TASK)
    assert d.path_class == "task"


def test_session_denies_any_notes_path_even_for_staff() -> None:
    # regla 9: en session, memory/notes/** -> E_PERM_OUTSIDE_TASK, aunque la clase sea lead
    d = check_path("ana", "memory/notes/README.md", "modify", TASKS[18], pol=make_policy(),
                   mode="session")
    assert (d.allow, d.code, d.path_class) == (False, OUT_TASK, "lead")
    d = check_path("ana", "memory/notes/README.md", "modify", TASKS[18], pol=make_policy(),
                   mode="commit")
    assert d.allow


@pytest.mark.parametrize(
    ("raw", "op", "allow"),
    [
        ("memory/decisions/2026-10-02-new.md", "create", True),
        ("memory/decisions/2026-10-02-new.md", "modify", True),  # aún no está en la base
        ("memory/decisions/2026-10-02-new.md", "delete", True),
        ("memory/decisions/2026-09-01-scope.md", "create", False),
        ("memory/decisions/2026-09-01-scope.md", "modify", False),
        ("memory/decisions/2026-09-01-scope.md", "delete", False),
    ],
)
def test_append_rule_10(raw: str, op: Any, allow: bool) -> None:
    for login, task in (("carla", 12), ("ana", 18)):
        d = check_path(login, raw, op, TASKS[task], pol=make_policy(mode="ci"), mode="ci")
        assert d.allow is allow
        assert d.code == (None if allow else "E_PERM_APPEND_ONLY")


@pytest.mark.parametrize("raw", sorted(CONTROL_FILES) + [d + "x.yml" for d in CONTROL_DIRS])
def test_control_paths_confirm_only_in_session(raw: str) -> None:
    assert is_control_path(raw)
    for login, task in (("ana", 18), ("bruno", 15)):
        d = check_path(login, raw, "modify", TASKS[task], pol=make_policy(), mode="session")
        if d.path_class != "lead":  # p. ej. .claude/skills/** es generated y .claude/... R2
            continue
        assert (d.allow, d.confirm) == (True, True), d
        d = check_path(login, raw, "modify", TASKS[task], pol=make_policy(), mode="commit")
        assert (d.allow, d.confirm) == (True, False), d


def test_control_paths_are_case_insensitive() -> None:
    assert is_control_path("ROLES.YAML") and is_control_path(".GitHub/Workflows/x.yml")
    assert not is_control_path("tex/roles.yaml") and not is_control_path(".github/x.yml")
    d = check_path("ana", ".DEVCONTAINER/devcontainer.json", "create", TASKS[18],
                   pol=make_policy(V.ENV_LINUX), mode="session")
    assert (d.allow, d.confirm) == (True, True)


def test_lead_only_for_researcher_in_every_mode() -> None:
    for mode in ("session", "commit", "ci"):
        d = check_path("carla", "plan.md", "modify", TASKS[12], pol=make_policy(mode=mode),
                       mode=mode)
        assert (d.allow, d.code, d.path_class) == (False, "E_PERM_LEAD_ONLY_PATH", "lead")


def test_lead_path_without_deputy() -> None:
    pol = make_policy(deputy=None)
    assert pol.approvers == frozenset({"ana"})
    d = check_path("bruno", "plan.md", "modify", TASKS[15], pol=pol, mode="session")
    assert d.code == "E_PERM_LEAD_ONLY_PATH"


# --- clase editor (paso 12b; 02 §3.3 R6a; 00-marco D-56) ------------------------------------

EDITOR_PATHS = ("tex/main.tex", "tex/preamble.tex", "tex/notation.tex", "tex/outline/methods.tex",
                "tex/guide.md", "bib/references.bib")


@pytest.mark.parametrize("raw", EDITOR_PATHS)
@pytest.mark.parametrize("mode", ["session", "commit", "ci"])
def test_editor_paths_without_a_declared_editor(raw: str, mode: str) -> None:
    """Sin editor declarado, la clase editor la cambian el responsable y el suplente."""
    pol = make_policy(mode=mode)
    for login, task in (("ana", TASKS[18]), ("bruno", TASKS[15])):
        d = check_path(login, raw, "modify", task, pol=pol, mode=mode)
        assert (d.allow, d.path_class, d.confirm) == (True, "editor", False), (login, d)
    d = check_path("carla", raw, "modify", TASKS[12], pol=pol, mode=mode)
    assert (d.allow, d.code, d.path_class, d.owner) == (False, "E_PERM_EDITOR_ONLY_PATH", "editor", "ana")


@pytest.mark.parametrize("raw", EDITOR_PATHS)
def test_declared_editor_owns_the_editor_paths(raw: str) -> None:
    """Con editor declarado, solo él: ni el responsable ni el suplente (06 §2.4, paso 12b)."""
    pol = dataclasses.replace(make_policy(), editor="Carla")
    assert pol.editor == "carla" and pol.editors == frozenset({"carla"})
    d = check_path("carla", raw, "modify", TASKS[12], pol=pol, mode="session")
    assert (d.allow, d.path_class) == (True, "editor")
    for login, task in (("ana", TASKS[18]), ("bruno", TASKS[15])):
        d = check_path(login, raw, "modify", task, pol=pol, mode="session")
        assert (d.allow, d.code, d.owner) == (False, "E_PERM_EDITOR_ONLY_PATH", "carla"), (login, d)


def test_editor_class_keeps_the_task_rules() -> None:
    """Los pasos 6 a 8 van antes: el editor también necesita su tarea reclamada y escribible."""
    pol = dataclasses.replace(make_policy(), editor="carla")
    assert check_path("carla", "tex/main.tex", "modify", None, pol=pol, mode="session").code == (
        "E_STATE_NOT_ON_TASK_BRANCH")
    assert check_path("carla", "tex/main.tex", "modify", TASKS[15], pol=pol, mode="session").code == (
        "E_CLAIM_NOT_HELD")
    merged = claimed(12, "carla", "methods", state="merged")
    assert check_path("carla", "tex/main.tex", "modify", merged, pol=pol, mode="session").code == (
        "E_STATE_INVALID_TRANSITION")


# --- logins sin distinguir mayúsculas y grafía para mostrar ---------------------------------


def test_logins_are_case_insensitive() -> None:
    task = Task(12, "Carla", "claimed", frozenset({"methods"}))
    d = check_path("CARLA", "sections/methods/a.tex", "modify", task, pol=make_policy(),
                   mode="session")
    assert d.allow
    d = check_path("Ana", "roles.yaml", "modify", Task(18, "ana", "claimed", frozenset()),
                   pol=make_policy(), mode="session")
    assert (d.allow, d.confirm) == (True, True)


def test_policy_normalizes_and_keeps_display() -> None:
    pol = Policy(
        people=frozenset({"Ana", "Bruno", "carla"}),
        lead="Ana",
        deputy="Bruno",
        sections={"methods": "Carla", "results": "BRUNO"},
        base_decisions=frozenset(),
        root=NATIVE.root,
        ci_fs=NATIVE.ci_fs,
    )
    assert pol.people == frozenset({"ana", "bruno", "carla"})
    assert (pol.lead, pol.deputy) == ("ana", "bruno")
    assert dict(pol.sections) == {"methods": "carla", "results": "bruno"}
    assert pol.approvers == frozenset({"ana", "bruno"})
    # people manda sobre la grafía de sections (REQ-SCHEMA-12); si no, la primera vista
    assert pol.shown("bruno") == "Bruno"
    assert pol.shown("carla") == "carla"
    assert pol.shown("nadie") == "nadie"
    assert pol.shown(None) is None


def test_decision_owner_uses_display_spelling() -> None:
    pol = make_policy(display={"carla": "Carla", "Bruno": "Bruno"})
    d = check_path("bruno", "sections/methods/a.tex", "modify", TASKS[15], pol=pol,
                   mode="session")
    assert (d.code, d.owner) == (NOT_OWNED, "Carla")
    d = check_path("carla", "sections/results/a.tex", "modify", TASKS[12], pol=pol,
                   mode="session")
    assert (d.code, d.owner) == (OUT_TASK, "Bruno")
    d = check_path("carla", "sections/methods/a.tex", "modify", TASKS[12], pol=pol,
                   mode="session")
    assert (d.allow, d.owner) == (True, "Carla")


# --- grafía real en sistemas que no distinguen mayúsculas (REQ-CONV-9) ----------------------


def test_case_map_is_applied_only_on_case_insensitive_fs() -> None:
    raw = "Sections/METHODS/a.tex"
    win = check_path("carla", raw, "modify", TASKS[12], pol=make_policy(V.ENV_WINDOWS),
                     mode="session")
    assert (win.allow, win.path, win.section) == (True, "sections/methods/a.tex", "methods")
    lin = check_path("carla", raw, "modify", TASKS[12], pol=make_policy(V.ENV_LINUX),
                     mode="session")
    assert (lin.allow, lin.code, lin.path) == (False, "E_PERM_LEAD_ONLY_PATH", raw)


def test_case_map_cannot_loosen_generated() -> None:
    d = check_path("carla", ".GITHUB/codeowners", "modify", TASKS[12],
                   pol=make_policy(V.ENV_LINUX), mode="session")
    assert (d.code, d.path_class) == ("E_PERM_GENERATED_PATH", "generated")


@pytest.mark.parametrize(
    "raw",
    [f".{PKG.capitalize()}/cache/identity.json", f".{PKG.upper()}/policy.json",
     "claude.local.md", "TEX/BUILD/main.pdf", ".claude/Settings.Local.json"],
)
def test_local_paths_in_other_case_are_local_for_staff(raw: str) -> None:
    # las rutas local no están en el mapa de grafías (no se versionan): R1 ignora mayúsculas
    for env in (V.ENV_WINDOWS, V.ENV_LINUX):
        for login, task in (("ana", 18), ("bruno", 15)):
            d = check_path(login, raw, "modify", TASKS[task], pol=make_policy(env),
                           mode="session")
            assert (d.allow, d.code, d.path_class) == (False, "E_PERM_LOCAL_PATH", "local"), d


# --- códigos: los del catálogo de perm_code -------------------------------------------------

_REPO = Path(__file__).resolve().parents[2]
_SCHEMA_CANDIDATES = (
    _REPO / "schemas" / "result.schema.json",
    _REPO.parent / "spec" / "schemas" / "result.schema.json",
)


def test_codes_are_perm_codes() -> None:
    schema = next((p for p in _SCHEMA_CANDIDATES if p.is_file()), None)
    if schema is None:
        pytest.skip("result.schema.json no disponible")
    perm = set(json.loads(schema.read_text(encoding="utf-8"))["$defs"]["perm_code"]["enum"])
    emitted = {c.code for c in TABLE if c.code} | {"E_INTERNAL_UNEXPECTED", "E_PERM_SYMLINK"}
    assert emitted <= perm
    source = Path(permissions.__file__).read_text(encoding="utf-8")
    literal = set(re.findall(r'"(E_[A-Z_]+)"', source))
    assert literal <= perm, literal - perm
