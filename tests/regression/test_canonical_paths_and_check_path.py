# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Rutas canónicas, clasificación y check_path.

Cada sección reproduce un defecto ya corregido:

1. Alias de Win32/NTFS (punto o espacio final, ``:`` de flujos, nombres cortos 8.3) que se
   saltaban R0 y el paso 1 de check_path con ``.git``.
2. El mismo aliasing con los archivos de instrucciones (R2), las rutas locales (R1), las
   generadas (R3) y las rutas de control (REQ-PERM-8).
3. R1 distinguía mayúsculas: ``.Aucove/...`` (con el nombre real del paquete) era ``lead``.
4. Nombres que Windows no puede extraer (CON, AUX, NUL, COMn, LPTn, ``<>:"|?*``, punto o
   espacio final) pasaban en CI.
5. ``.git`` en un segmento no inicial (repo anidado) no era R0.
6. Sin ``link_probe`` en session o commit, check_path asumía que no había enlaces.
7. ``canonical`` solo rechazaba los controles C0: DEL, C1 y los formatos bidi pasaban.

Las funciones son puras: la plataforma es un dato (raíz, ``ci_fs`` y mapa de grafías), así
que los casos de Windows corren en cualquier sistema.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from aucove import _meta
from aucove.domain.paths import InvalidPath, canonical, classify
from aucove.domain.permissions import Decision, Policy, Task, check_change, check_path

WIN_ROOT = "C:\\Users\\ana\\p"
LINUX_ROOT = "/home/ana/p"
OUTSIDE = "E_PERM_OUTSIDE_REPO"
INTERNAL = "E_INTERNAL_UNEXPECTED"
LOCAL = "E_PERM_LOCAL_PATH"
CACHE = _meta.CACHE_DIR  # '.' + nombre del paquete

# Archivos versionados del árbol de prueba: dan el mapa de grafías de REQ-CONV-9 (git ls-files)
TREE = (
    "tex/main.tex",
    "sections/methods/methods.tex",
    "sections/introduction/introduction.tex",
    ".claude/settings.json",
    ".github/CODEOWNERS",
    ".github/workflows/build.yml",
    "roles.yaml",
    "project.yaml",
    "plan.md",
)
CASE_MAP = {p.lower(): p for p in TREE}

T12 = Task(12, "carla", "claimed", frozenset({"methods"}))
T18 = Task(18, "ana", "claimed", frozenset({"introduction"}))
T15 = Task(15, "bruno", "claimed", frozenset())


def no_links(rel: str) -> bool:
    return False


_DEFAULT_PROBE: Any = object()


def policy(
    *, windows: bool = True, ci: bool = False, link_probe: Any = _DEFAULT_PROBE
) -> Policy:
    """ana es lead, bruno deputy y carla dueña de methods; en Windows, con mapa de grafías."""
    probe = (None if ci else no_links) if link_probe is _DEFAULT_PROBE else link_probe
    return Policy(
        people=frozenset({"ana", "bruno", "carla"}),
        lead="ana",
        deputy="bruno",
        sections={"introduction": "ana", "methods": "carla"},
        base_decisions=frozenset(),
        root=WIN_ROOT if windows else LINUX_ROOT,
        ci_fs=windows,
        case_map=CASE_MAP if windows else None,
        link_probe=probe,
    )


def every_mode(login: str, raw: str, task: Task | None, op: Any = "create") -> list[Decision]:
    """Decisiones de ``raw`` en los tres modos y en las dos plataformas emuladas."""
    out: list[Decision] = []
    for windows in (True, False):
        for mode in ("session", "commit", "ci"):
            pol = policy(windows=windows, ci=(mode == "ci"))
            out.append(check_path(login, raw, op, task, pol=pol, mode=mode))
    return out


# --- 1. alias de Win32/NTFS de .git (R0 y paso 1) -------------------------------------------

GIT_ALIASES = (
    ".git./hooks/pre-commit",
    ".git::$INDEX_ALLOCATION/hooks/pre-push",
    "GIT~1/hooks/pre-rebase",
    "git~1/config",
    ".git /hooks/y",
    ".git . ./config",
    ".GIT../x",
    ".git:stream",
    "C:\\Users\\ana\\p\\.git.\\hooks\\pre-commit",
)


@pytest.mark.parametrize("raw", GIT_ALIASES)
def test_git_aliases_are_invalid_paths(raw: str) -> None:
    for ci_fs in (True, False):
        with pytest.raises(InvalidPath):
            canonical(raw, WIN_ROOT, ci_fs)


@pytest.mark.parametrize("raw", GIT_ALIASES)
@pytest.mark.parametrize(("login", "task"), [("ana", T18), ("bruno", T15), ("carla", T12)])
def test_git_aliases_denied_for_everyone(raw: str, login: str, task: Task) -> None:
    for d in every_mode(login, raw, task):
        assert (d.allow, d.code) == (False, OUTSIDE), d


# --- 2. alias de Win32 de instrucciones, rutas locales, generadas y de control --------------

SECTION_ALIASES = (
    "sections/methods/CLAUDE.md.",
    "sections/methods/AGENTS.md ",
    "sections/methods/GEMINI.md::$DATA",
    "sections/methods/.claude./rules/a.md",
    "sections/methods/CLAUDE~1/rules/c.md",
    "sections/methods/AGENTS~1.MD",
    "sections/methods/.mcp.json:x",
)


@pytest.mark.parametrize("raw", SECTION_ALIASES)
def test_researcher_cannot_plant_instructions_through_aliases(raw: str) -> None:
    for d in every_mode("carla", raw, T12):
        assert (d.allow, d.code) == (False, OUTSIDE), d


LEAD_ALIASES = (
    "roles.yaml.",
    "roles.yaml ",
    "ROLES~1.YAM",
    "project.yaml::$DATA",
    ".github./workflows/ci.yml",
    "GITHUB~1/workflows/ci.yml",
    ".github/CODEOWNERS.",
    f"{CACHE}./cache/identity.json",
    "tex/build./main.pdf",
    "CLAUDE.local.md.",
)


@pytest.mark.parametrize("raw", LEAD_ALIASES)
@pytest.mark.parametrize(("login", "task"), [("ana", T18), ("bruno", T15)])
def test_staff_cannot_skip_confirm_local_or_generated_through_aliases(
    raw: str, login: str, task: Task
) -> None:
    for d in every_mode(login, raw, task, op="modify"):
        assert (d.allow, d.code) == (False, OUTSIDE), d


def test_plain_spellings_keep_their_spec_behavior() -> None:
    pol = policy()
    d = check_path("ana", "roles.yaml", "modify", T18, pol=pol, mode="session")
    assert (d.allow, d.confirm) == (True, True)
    d = check_path("ana", ".github/workflows/ci.yml", "create", T18, pol=pol, mode="session")
    assert (d.allow, d.confirm) == (True, True)
    d = check_path("ana", ".github/CODEOWNERS", "modify", T18, pol=pol, mode="session")
    assert (d.allow, d.code, d.path_class) == (False, "E_PERM_GENERATED_PATH", "generated")
    d = check_path("carla", "sections/methods/CLAUDE.md", "create", T12, pol=pol,
                   mode="session")
    assert (d.allow, d.code, d.path_class) == (False, "E_PERM_LEAD_ONLY_PATH", "lead")


# --- 3. R1 sin distinguir mayúsculas --------------------------------------------------------

LOCAL_SPELLINGS = (
    "." + CACHE[1:].capitalize() + "/cache/identity.json",  # '.Aucove' (capitalize no cambia '.')
    CACHE.upper() + "/policy.json",
    "claude.local.md",
    "Claude.Local.MD",
    "TEX/BUILD/main.pdf",
    "Tex/Build-Diff/diff.pdf",
    ".claude/Settings.Local.json",
    ".CLAUDE/Worktrees/w1/AGENTS.md",
)


@pytest.mark.parametrize("rel", LOCAL_SPELLINGS)
def test_local_rule_ignores_case(rel: str) -> None:
    info = classify(rel, frozenset({"methods"}), symlinked=False)
    assert (info.cls, info.rule) == ("local", "R1")


@pytest.mark.parametrize("raw", LOCAL_SPELLINGS)
@pytest.mark.parametrize(("login", "task"), [("ana", T18), ("bruno", T15)])
def test_staff_cannot_write_local_paths_in_other_case(raw: str, login: str, task: Task) -> None:
    for d in every_mode(login, raw, task, op="modify"):
        assert (d.allow, d.code, d.path_class) == (False, LOCAL, "local"), d


# --- 4. nombres que Windows no puede extraer ------------------------------------------------

NON_PORTABLE = (
    "aux.tex",
    "con.tex",
    "nul",
    "NUL.tar.gz",
    "Aux .tex",
    "prn.bib",
    "COM1.tex",
    "com9",
    "lpt1.txt",
    "LPT9",
    "com\u00b9.tex",
    "conin$",
    "CONOUT$.log",
    "x.tex.",
    "x.tex ",
    "a:b.tex",
    "fig?.pdf",
    "fig*.pdf",
    'x"y.tex',
    "a<b.tex",
    "a>b.tex",
    "a|b.tex",
    "CON/x.tex",
)


@pytest.mark.parametrize("name", NON_PORTABLE)
def test_non_portable_names_fail_scope_in_ci(name: str) -> None:
    raw = f"sections/methods/{name}"
    for windows in (True, False):
        pol = policy(windows=windows, ci=True)
        ds = check_change("carla", "added", raw, None, T12, pol=pol, mode="ci")
        assert [(d.allow, d.code) for d in ds] == [(False, OUTSIDE)], ds
        d = check_path("carla", raw, "create", T12, pol=policy(windows=windows), mode="session")
        assert (d.allow, d.code) == (False, OUTSIDE)


PORTABLE = (
    "auxiliary.tex",
    "aux-notes.tex",
    "my.aux.tex",
    "con-texto.tex",
    "null.tex",
    "com10.tex",
    "lpt.tex",
    "figure~a.pdf",
    "notes~old.md",
    "x~1.tex.bak",
    "very-long-name~1.tex",
    "a~/b.tex",
    "an\u00e1lisis.tex",
    "e\u0301.tex",
)


@pytest.mark.parametrize("name", PORTABLE)
def test_portable_names_still_allowed(name: str) -> None:
    raw = f"sections/methods/{name}"
    for d in every_mode("carla", raw, T12):
        assert d.allow, d
        assert d.path_class == "section"


# --- 5. .git en cualquier segmento (repo anidado) -------------------------------------------

NESTED_GIT = (
    "sections/methods/.git/config",
    "sections/methods/.git",
    "sections/methods/sub/.GIT/hooks/x",
    "sections/methods/.Git/HEAD",
    "plan/.git/config",
)


@pytest.mark.parametrize("raw", NESTED_GIT)
def test_nested_git_is_r0_for_everyone(raw: str) -> None:
    info = classify(raw, frozenset({"methods"}), symlinked=False)
    assert (info.cls, info.rule, info.section) == ("lead", "R0", None)
    for login, task in (("ana", T18), ("bruno", T15), ("carla", T12)):
        for d in every_mode(login, raw, task):
            assert (d.allow, d.code, d.path_class) == (False, OUTSIDE, "lead"), d


@pytest.mark.parametrize(
    "rel", ["sections/methods/.gitignore", "sections/methods/git/x.tex",
            "sections/methods/x.git", "sections/methods/.github/x.tex"],
)
def test_names_that_only_look_like_git_are_not_r0(rel: str) -> None:
    assert classify(rel, frozenset({"methods"}), symlinked=False).rule == "R6"


# --- 6. sin sonda de enlaces en session o commit, se deniega --------------------------------


@pytest.mark.parametrize("mode", ["session", "commit"])
def test_missing_link_probe_fails_closed(mode: Any) -> None:
    for windows in (True, False):
        pol = policy(windows=windows, link_probe=None)
        d = check_path("carla", "sections/methods/link/x.tex", "create", T12, pol=pol,
                       mode=mode)
        assert (d.allow, d.code) == (False, INTERNAL)
        ds = check_change("carla", "M", "sections/methods/a.tex", None, T12, pol=pol,
                          mode=mode)
        assert [(x.allow, x.code) for x in ds] == [(False, INTERNAL)]


def test_ci_needs_no_link_probe() -> None:
    pol = policy(windows=False, link_probe=None)
    d = check_path("carla", "sections/methods/a.tex", "create", T12, pol=pol, mode="ci")
    assert d.allow
    d = check_path("carla", "sections/methods/a.tex", "create", T12, pol=pol, mode="ci",
                   file_mode="120000")
    assert (d.allow, d.code) == (False, "E_PERM_SYMLINK")


def test_probe_is_still_consulted_in_session() -> None:
    seen: list[str] = []
    probe: Callable[[str], bool] = lambda rel: seen.append(rel) or True  # noqa: E731
    d = check_path("carla", "sections/methods/link/x.tex", "create", T12,
                   pol=policy(link_probe=probe), mode="session")
    assert (d.allow, d.code) == (False, "E_PERM_SYMLINK")
    assert seen == ["sections/methods/link/x.tex"]


# --- 7. caracteres de control y de formato (REQ-CONV-7) -------------------------------------

CONTROL_LIKE = (
    "a\x7fb.tex",  # DEL
    "a\x85b.tex",  # NEL (C1): rompe líneas en YAML y en registros
    "a\x9fb.tex",
    "x\u202egpj.tex",  # RLO: «Trojan Source»
    "a\u202ab.tex",
    "a\u2066b.tex",
    "a\u2069b.tex",
    "a\u200bb.tex",  # espacio de ancho cero
    "a\u200cb.tex",  # HFS+ ignora ZWNJ al comparar nombres
    "a\ufeffb.tex",
    "a\u00adb.tex",  # guion suave
    "a\u2028b.tex",  # separador de línea
    "a\u2029b.tex",
)


@pytest.mark.parametrize("name", CONTROL_LIKE)
def test_control_and_format_characters_are_rejected(name: str) -> None:
    raw = f"sections/methods/{name}"
    for ci_fs in (True, False):
        with pytest.raises(InvalidPath):
            canonical(raw, LINUX_ROOT, ci_fs)
    for d in every_mode("carla", raw, T12):
        assert (d.allow, d.code) == (False, OUTSIDE), d


def test_hfs_ignorable_git_alias_is_rejected() -> None:
    for raw in (".g\u200cit/config", ".git\u200d/hooks/x", "\ufeff.git/HEAD"):
        for d in every_mode("ana", raw, T18):
            assert (d.allow, d.code) == (False, OUTSIDE), d
