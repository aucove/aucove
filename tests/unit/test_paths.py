# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
# Pruebas de domain.paths: vectores de 02 §3.6 (REQ-CONV-13), canonical, apply_case_map y
# classify R0-R7. Los vectores de este archivo también los recorre check_path en
# test_check_path_table.py (REQ-PERM-54), que los carga por ruta.
from __future__ import annotations

import ast
import dataclasses
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from aucove.domain import paths
from aucove.domain.paths import (
    InvalidPath,
    OutsideRepo,
    PathInfo,
    apply_case_map,
    canonical,
    classify,
)

WINDOWS = sys.platform == "win32"
LINUX = sys.platform.startswith("linux")
DECLARED = frozenset({"methods"})
PKG = paths.__name__.split(".", 1)[0]


@dataclass(frozen=True)
class Env:
    """Entorno: raíz, separador nativo y si el sistema de archivos iguala mayúsculas."""

    name: str
    root: str
    sep: str
    ci_fs: bool


ENV_WINDOWS = Env("windows", "C:\\Users\\ana\\p", "\\", True)
ENV_LINUX = Env("linux", "/home/ana/p", "/", False)
ENV_MACOS = Env("macos", "/home/ana/p", "/", True)
NATIVE = ENV_WINDOWS if WINDOWS else (ENV_MACOS if sys.platform == "darwin" else ENV_LINUX)

# Archivos que existen en el árbol de prueba. Con ci_fs, su mapa de grafías corrige la entrada
# (REQ-CONV-9). Las grafías coinciden con las de los vectores para no alterar su resultado.
TREE = (
    "tex/main.tex",
    "sections/methods/methods.tex",
    "sections/methods/figures/f1.pdf",
    "sections/results/results.tex",
    "memory/decisions/2026-10-01-1-sampling.md",
    "memory/decisions/2026-09-01-scope.md",
    "records/approvals/x.jsonl",
    ".claude/settings.json",
    ".claude/skills/empezar/SKILL.md",
    ".github/CODEOWNERS",
    ".github/ISSUE_TEMPLATE/task.yml",
    "roles.yaml",
    "project.yaml",
    "plan.md",
)
CASE_MAP = {p.lower(): p for p in TREE}


def case_map_for(env: Env) -> dict[str, str] | None:
    return CASE_MAP if env.ci_fs else None


@dataclass(frozen=True)
class Vec:
    """Un vector de 02 §3.6. ``canonical`` None significa OutsideRepo."""

    raw: str  # '{root}' y '{sep}' se sustituyen por los del entorno
    canonical: str | None
    cls: str | None
    rule: str | None
    section: str | None = None
    task: int | None = None
    platform: str | None = None  # 'windows' o 'linux': solo en esa plataforma (REQ-CONV-13)
    root: str | None = None  # raíz propia del vector

    def raw_for(self, env: Env) -> str:
        return self.raw.replace("{root}", env.root).replace("{sep}", env.sep)

    def root_for(self, env: Env) -> str:
        return self.root if self.root is not None else env.root


SEC, LEAD, EDITOR = "section", "lead", "editor"
CONV_VECTORS: tuple[Vec, ...] = (
    Vec("sections/methods/methods.tex", "sections/methods/methods.tex", SEC, "R6", "methods"),
    Vec(
        "sections\\methods\\figures\\f1.pdf",
        "sections/methods/figures/f1.pdf",
        SEC,
        "R6",
        "methods",
    ),
    Vec("sections/methods/notes/n1.md", "sections/methods/notes/n1.md", SEC, "R6", "methods"),
    Vec("sections/methods/../../tex/main.tex", "tex/main.tex", EDITOR, "R6a"),
    Vec("sections/results/results.tex", "sections/results/results.tex", LEAD, "R7"),
    Vec("sections/Methods/x.tex", "sections/Methods/x.tex", LEAD, "R7", platform="linux"),
    # La carpeta anterior a D-43 ya no es de ninguna parte.
    Vec("tex/sections/methods/x.tex", "tex/sections/methods/x.tex", LEAD, "R7"),
    Vec("tex/outline/methods.tex", "tex/outline/methods.tex", EDITOR, "R6a"),
    Vec("tex/outline/sub/x.tex", "tex/outline/sub/x.tex", LEAD, "R7"),
    Vec("C:\\Users\\ana\\p\\TEX\\MAIN.TEX", "tex/main.tex", EDITOR, "R6a", platform="windows"),
    Vec(
        "C:\\Users\\GROSS\\p\\tex\\main.tex",
        None,
        None,
        None,
        platform="windows",
        root="C:\\Users\\Groß\\p",
    ),
    Vec("sections/methods/CLAUDE.md", "sections/methods/CLAUDE.md", LEAD, "R2"),
    Vec("sections/methods/claude.md", "sections/methods/claude.md", LEAD, "R2"),
    Vec("sections/methods/.claude/rules/x.md", "sections/methods/.claude/rules/x.md", LEAD, "R2"),
    Vec(
        "memory/decisions/2026-10-01-1-sampling.md",
        "memory/decisions/2026-10-01-1-sampling.md",
        "append",
        "R4",
    ),
    Vec("memory/decisions/old/x.md", "memory/decisions/old/x.md", LEAD, "R7"),
    Vec("records/approvals/x.jsonl", "records/approvals/x.jsonl", LEAD, "R7"),
    Vec("memory/notes/task-42.md", "memory/notes/task-42.md", "task", "R5", task=42),
    Vec("memory/notes/task-042.md", "memory/notes/task-042.md", LEAD, "R7"),
    Vec(".claude/skills/empezar/SKILL.md", ".claude/skills/empezar/SKILL.md", "generated", "R3"),
    Vec(".github/ISSUE_TEMPLATE/task.yml", ".github/ISSUE_TEMPLATE/task.yml", "generated", "R3"),
    Vec(".claude/settings.json", ".claude/settings.json", LEAD, "R7"),
    Vec(".claude/settings.local.json", ".claude/settings.local.json", "local", "R1"),
    Vec(".claude/worktrees/w1/AGENTS.md", ".claude/worktrees/w1/AGENTS.md", "local", "R1"),
    Vec("tex/build-diff/diff.pdf", "tex/build-diff/diff.pdf", "local", "R1"),
    # /home/ana/p/notes.txt con la raíz del entorno sustituida
    Vec("{root}{sep}notes.txt", "notes.txt", LEAD, "R7"),
    Vec("../otro/a.tex", None, None, None),
    Vec("/etc/passwd", None, None, None),
)


def _skip_mark(vec: Vec) -> list[pytest.MarkDecorator]:
    if vec.platform == "windows" and not WINDOWS:
        return [pytest.mark.skip(reason="vector de Windows (02 §3.6)")]
    if vec.platform == "linux" and not LINUX:
        return [pytest.mark.skip(reason="vector de Linux (02 §3.6)")]
    return []


def native_params() -> list[object]:
    """Vectores en la plataforma real; los de la otra plataforma se omiten (REQ-CONV-13)."""
    return [pytest.param(v, NATIVE, id=v.raw, marks=_skip_mark(v)) for v in CONV_VECTORS]


def emulated_params() -> list[object]:
    """Vectores de cada plataforma emulada: las funciones son puras y la plataforma es un dato."""
    out: list[object] = []
    for env in (ENV_WINDOWS, ENV_LINUX):
        for v in CONV_VECTORS:
            if v.platform in (None, env.name):
                out.append(pytest.param(v, env, id=f"{env.name}:{v.raw}"))
    return out


def resolve(vec: Vec, env: Env) -> PathInfo:
    rel = canonical(vec.raw_for(env), vec.root_for(env), env.ci_fs)
    if env.ci_fs:
        rel = apply_case_map(rel, case_map_for(env))
    return classify(rel, DECLARED, symlinked=False)


def _check_vector(vec: Vec, env: Env) -> None:
    if vec.canonical is None:
        with pytest.raises(OutsideRepo):
            resolve(vec, env)
        return
    info = resolve(vec, env)
    assert (info.path, info.cls, info.rule) == (vec.canonical, vec.cls, vec.rule)
    assert info.section == vec.section
    assert info.task == vec.task


@pytest.mark.parametrize(("vec", "env"), native_params())
def test_convention_vectors(vec: Vec, env: Env) -> None:
    _check_vector(vec, env)


@pytest.mark.parametrize(("vec", "env"), emulated_params())
def test_convention_vectors_emulated(vec: Vec, env: Env) -> None:
    _check_vector(vec, env)


def test_vector_table_is_complete() -> None:
    # Las 24 primeras filas de 02 §3.6, cuatro con dos entradas. Las de R0, InvalidPath y R1 sin
    # mayúsculas las cubren las pruebas de canonical y classify de más abajo.
    assert len(CONV_VECTORS) == 28
    assert {v.platform for v in CONV_VECTORS} == {None, "windows", "linux"}


# --- canonical ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "", "a\x00b", "tex/\tx.tex", "line\nbreak", "\x1f", "~", "~/x", "./~/x", ".", "./", "a/..",
        # REQ-CONV-7: DEL y C1 (Cc), formatos bidi y de ancho cero (Cf), separadores de línea
        "a\x7fb", "a\x85b", "x\u202egpj.tex", "a\u2066b", "a\u200bb", "a\ufeffb", "a\u2028b",
    ],
)
def test_canonical_invalid(raw: str) -> None:
    with pytest.raises(InvalidPath):
        canonical(raw, "/home/ana/p", False)


@pytest.mark.parametrize(
    "raw",
    ["..", "../x", "a/../../x", "/etc/passwd", "/home/ana", "/home/ana/q/x", "C:/x", "D:\\x"],
)
def test_canonical_outside(raw: str) -> None:
    with pytest.raises(OutsideRepo):
        canonical(raw, "/home/ana/p", False)


def test_canonical_root_itself_is_invalid() -> None:
    with pytest.raises(InvalidPath):
        canonical("/home/ana/p", "/home/ana/p", False)
    with pytest.raises(InvalidPath):
        canonical("/home/ana/p/", "/home/ana/p/", False)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a/./b//c/", "a/b/c"),
        ("./a", "a"),
        ("a/~/b", "a/~/b"),  # '~' solo se rechaza como primer segmento
        ("a/b/../c", "a/c"),
        ("a\\b\\c", "a/b/c"),
        ("/home/ana/p/a/b", "a/b"),
        ("/home/ana/p//a", "a"),
        ("e\u0301.tex", "\u00e9.tex"),  # NFC
        ("an\u00e1lisis/a~b/x~1.tex.bak", "an\u00e1lisis/a~b/x~1.tex.bak"),
    ],
)
def test_canonical_normalizes(raw: str, expected: str) -> None:
    assert canonical(raw, "/home/ana/p", False) == expected


@pytest.mark.parametrize(
    "seg",
    [
        # Win32 quita el punto y el espacio final: '.git.' y '.git ' son '.git'
        ".git.", ".git ", ".git . .", "x.tex.", "roles.yaml ", "...", ".. ",
        # ':' abre un flujo alternativo o una ruta relativa a la unidad
        ".git::$INDEX_ALLOCATION", "CLAUDE.md::$DATA", "a:b", "C:x",
        # nombres cortos 8.3 de NTFS
        "GIT~1", "git~1", "CLAUDE~1", "GITHUB~1", "ROLES~1.YAM", "GI7EBA~1", "~1",
        # dispositivos de Windows, con extensión o espacios detrás
        "CON", "aux.tex", "Nul", "nul .txt", "PRN.x.y", "COM1", "lpt9.txt", "COM0", "CONOUT$",
        "com\u00b2",
        # caracteres que Win32 no admite
        "a<b", "a>b", 'a"b', "a|b", "a?b", "a*b",
    ],
)
def test_canonical_rejects_windows_aliases(seg: str) -> None:
    for raw in (seg, f"sections/methods/{seg}", f"{seg}/x.tex"):
        for ci_fs in (True, False):
            with pytest.raises(InvalidPath):
                canonical(raw, "/home/ana/p", ci_fs)
            with pytest.raises(InvalidPath):
                canonical(raw.replace("/", "\\"), "C:\\Users\\ana\\p", ci_fs)


@pytest.mark.parametrize(
    "seg",
    ["auxiliary.tex", "my.aux.tex", "con-texto", "null", "com10", "lpt", "a~b", "x~1.tex.bak",
     "longername~1", "~x", ".gitignore", ".github", "a.b.c", " lead"],
)
def test_canonical_keeps_portable_names(seg: str) -> None:
    assert canonical(f"a/{seg}", "/home/ana/p", False) == f"a/{seg}"


def test_public_helpers_for_aliases() -> None:
    assert paths.is_win32_alias("GIT~1") and paths.is_win32_alias("aux.tex")
    assert not paths.is_win32_alias("auxiliary.tex") and not paths.is_win32_alias("")
    assert paths.is_git_path("sections/m/.GIT/config") and paths.is_git_path(".git")
    assert paths.is_git_path("a/git~1/x") and paths.is_git_path("a/.git. /x")
    assert not paths.is_git_path("a/.gitignore") and not paths.is_git_path("a/.github/x")


def test_canonical_windows_alias_check_skips_the_root() -> None:
    # los segmentos de la raíz (8.3, unidad) se comparan con root; no son de la ruta relativa
    assert canonical("C:\\PROGRA~1\\p\\x.tex", "C:\\PROGRA~1\\p", True) == "x.tex"


def test_canonical_root_trailing_slash_and_nfc() -> None:
    assert canonical("/home/ana/p/x", "/home/ana/p/", False) == "x"
    assert canonical("/home/Jose\u0301/p/x", "/home/Jos\u00e9/p", False) == "x"


def test_canonical_case_of_root() -> None:
    with pytest.raises(OutsideRepo):
        canonical("/HOME/ana/p/x", "/home/ana/p", False)
    assert canonical("/HOME/ana/p/x", "/home/ana/p", True) == "x"
    assert canonical("c:\\users\\ANA\\p\\x", "C:\\Users\\ana\\p", True) == "x"
    # lower, no casefold: NTFS no iguala ß y SS
    with pytest.raises(OutsideRepo):
        canonical("C:\\Users\\GROSS\\p\\x", "C:\\Users\\Groß\\p", True)


def test_canonical_windows_root_mixed_separators() -> None:
    assert canonical("C:/Users/ana/p/tex/main.tex", "C:\\Users\\ana\\p", True) == "tex/main.tex"
    # la raíz se quita antes de resolver '..': salir y volver a entrar cuenta como fuera
    with pytest.raises(OutsideRepo):
        canonical("C:\\Users\\ana\\p\\..\\p\\x", "C:\\Users\\ana\\p", True)


# --- apply_case_map -------------------------------------------------------------------------


@pytest.mark.parametrize("cmap", [None, {}])
def test_case_map_absent(cmap: dict[str, str] | None) -> None:
    assert apply_case_map("TEX/Main.tex", cmap) == "TEX/Main.tex"


def test_case_map_exact_file() -> None:
    assert apply_case_map("TEX/MAIN.TEX", CASE_MAP) == "tex/main.tex"
    assert apply_case_map(".GITHUB/codeowners", CASE_MAP) == ".github/CODEOWNERS"


def test_case_map_directory_from_file_keys() -> None:
    # archivo nuevo en carpetas existentes: se corrigen solo los segmentos que existen
    new = apply_case_map("Sections/METHODS/New.tex", CASE_MAP)
    assert new == "sections/methods/New.tex"
    assert apply_case_map(".Claude/Skills/Otro/X.md", CASE_MAP) == ".claude/skills/Otro/X.md"


def test_case_map_directory_key() -> None:
    cmap = {"Docs": "Docs", "docs": "Docs", "docs/a.md": "Docs/a.md"}
    assert apply_case_map("DOCS/b.md", cmap) == "Docs/b.md"
    assert apply_case_map("docs/A.MD", cmap) == "Docs/a.md"


def test_case_map_prefers_deepest_prefix() -> None:
    cmap = {"a/b/c.txt": "A/B/c.txt", "a/x.txt": "A/x.txt"}
    assert apply_case_map("a/b/new.txt", cmap) == "A/B/new.txt"
    assert apply_case_map("a/q/new.txt", cmap) == "A/q/new.txt"


def test_case_map_unknown_path_is_unchanged() -> None:
    assert apply_case_map("Nuevo/Archivo.TXT", CASE_MAP) == "Nuevo/Archivo.TXT"
    assert apply_case_map("NEW.md", CASE_MAP) == "NEW.md"


def test_case_map_ignores_entries_that_change_more_than_case() -> None:
    cmap = {"tex/main.tex": "otro/main.tex", "tex/x/y.tex": "tex/x/y/z.tex"}
    assert apply_case_map("TEX/MAIN.TEX", cmap) == "TEX/MAIN.TEX"
    assert apply_case_map("TEX/X/new.tex", cmap) == "TEX/X/new.tex"


# --- classify -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "cls", "rule", "section", "task"),
    [
        (".git/config", "lead", "R0", None, None),
        (".git", "lead", "R0", None, None),
        (".GIT/hooks/pre-commit", "lead", "R0", None, None),  # como git: sin mayúsculas
        ("tex/.git/x", "lead", "R0", None, None),  # en cualquier segmento, como git
        ("sections/methods/.GIT", "lead", "R0", None, None),
        ("sections/methods/git~1/config", "lead", "R0", None, None),
        ("sections/methods/.git./x", "lead", "R0", None, None),
        ("sections/methods/.git:x/y", "lead", "R0", None, None),
        ("sections/methods/.gitignore", "section", "R6", "methods", None),
        (f".{PKG}/cache/identity.json", "local", "R1", None, None),
        (".claude/worktrees/w/x.tex", "local", "R1", None, None),
        ("CLAUDE.local.md", "local", "R1", None, None),
        ("tex/build/main.pdf", "local", "R1", None, None),
        # R1 sin distinguir mayúsculas, como R2 y R3 (las rutas local no están en el mapa)
        (f".{PKG.capitalize()}/cache/identity.json", "local", "R1", None, None),
        ("claude.LOCAL.md", "local", "R1", None, None),
        (".Claude/Settings.Local.json", "local", "R1", None, None),
        ("TEX/Build-Diff/d.pdf", "local", "R1", None, None),
        (".CLAUDE/WorkTrees/w/x", "local", "R1", None, None),
        ("tex/buildx/main.pdf", "lead", "R7", None, None),
        ("tex/CLAUDE.local.md", "lead", "R2", None, None),
        ("docs/.CURSOR/rules.md", "lead", "R2", None, None),
        ("sections/methods/.mcp.json", "lead", "R2", None, None),
        ("sections/methods/Agents.Override.MD", "lead", "R2", None, None),
        ("AGENTS.md", "lead", "R7", None, None),  # en la raíz, R7 (ruta lead de 00 §5.2)
        (".mcp.json", "lead", "R7", None, None),
        (".github/CODEOWNERS", "generated", "R3", None, None),
        (".github/codeowners", "generated", "R3", None, None),
        (".GITHUB/Issue_Template/TASK.YML", "generated", "R3", None, None),
        (".agents/skills/x/SKILL.md", "generated", "R3", None, None),
        (".claude/skills/SKILL.md", "generated", "R3", None, None),
        (".claude/skills", "lead", "R7", None, None),
        (".Claude/skills/x/SKILL.md", "lead", "R7", None, None),  # el patrón de skills distingue
        ("memory/decisions/x.md", "append", "R4", None, None),
        ("memory/decisions/.x.md", "lead", "R7", None, None),
        ("memory/decisions/x.txt", "lead", "R7", None, None),
        ("memory/decisions", "lead", "R7", None, None),
        ("memory/notes/task-1.md", "task", "R5", None, 1),
        ("memory/notes/task-0.md", "lead", "R7", None, None),
        ("memory/notes/task-7.txt", "lead", "R7", None, None),
        ("sections/methods/x", "section", "R6", "methods", None),
        ("sections/methods/notes/n1.md", "section", "R6", "methods", None),
        ("sections/methods/analysis/derived/d.csv", "section", "R6", "methods", None),
        ("sections/methods", "lead", "R7", None, None),
        ("sections/x.tex", "lead", "R7", None, None),
        ("sections/met_hods/x.tex", "lead", "R7", None, None),
        ("sections/results/x.tex", "lead", "R7", None, None),
        ("Sections/methods/x.tex", "lead", "R7", None, None),  # R6 distingue mayúsculas
        # D-43: la carpeta anterior es una ruta lead más, también para una parte declarada
        ("tex/sections/methods/x.tex", "lead", "R7", None, None),
        ("tex/sections/methods/methods.tex", "lead", "R7", None, None),
        ("plan.md", "lead", "R7", None, None),
    ],
)
def test_classify_rules(
    rel: str, cls: Any, rule: str, section: str | None, task: int | None
) -> None:
    declared = frozenset({"methods", "met_hods"})
    assert classify(rel, declared, symlinked=False) == PathInfo(rel, cls, rule, section, task)


def test_classify_symlinked_is_r0() -> None:
    info = classify("sections/methods/x.tex", DECLARED, symlinked=True)
    assert (info.cls, info.rule, info.section) == ("lead", "R0", None)


def test_classify_rule_order_local_before_instructions() -> None:
    # R1 gana a R2 y R3: todo lo de .claude/worktrees es local
    assert classify(".claude/worktrees/w/.claude/skills/x.md", DECLARED, False).rule == "R1"


def test_path_info_is_frozen_with_slots() -> None:
    info = PathInfo("a", "lead", "R7")
    assert not hasattr(info, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.cls = "local"  # type: ignore[misc]


def test_local_prefix_derives_from_package_name() -> None:
    assert paths.LOCAL_PREF[0] == f".{PKG}/"


# --- pureza (REQ-ARCH-18) y nombre (REQ-ARCH-19) --------------------------------------------

DOMAIN_DIR = Path(paths.__file__).resolve().parent
STDLIB_OK = {"__future__", "re", "unicodedata", "collections.abc", "dataclasses", "typing"}
FORBIDDEN_CALLS = {"open", "print", "input", "eval", "exec", "compile", "__import__", "breakpoint"}


@pytest.mark.parametrize("name", ["paths.py", "permissions.py"])
def test_module_is_pure_stdlib(name: str) -> None:
    tree = ast.parse((DOMAIN_DIR / name).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert {a.name for a in node.names} <= STDLIB_OK, ast.dump(node)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                assert node.module == "paths", ast.dump(node)  # solo el módulo hermano
            else:
                assert node.module in STDLIB_OK, ast.dump(node)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in FORBIDDEN_CALLS, node.func.id


@pytest.mark.parametrize("name", ["paths.py", "permissions.py"])
def test_module_text_conventions(name: str) -> None:
    data = (DOMAIN_DIR / name).read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in data
    assert data.endswith(b"\n") and not data.endswith(b"\n\n")
    text = data.decode("utf-8")
    assert text.startswith("# SPDX" + "-FileCopyrightText: 2026 The ")
    assert text.splitlines()[1] == "# SPDX" + "-License-Identifier: Apache-2.0"
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert PKG not in node.value, node.value  # REQ-ARCH-19


# --- propiedades ----------------------------------------------------------------------------

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st
except ImportError:  # pragma: no cover - hypothesis es una dependencia de desarrollo
    given = None

if given is not None:
    _SEGMENT = st.sampled_from(
        ["", ".", "..", "~", "a", "B", "tex", "sections", "methods", "\u00e9", "e\u0301",
         ".git", "GIT~1", "x.", "a:b", "nul", "\u202e"]
    )
    _RAW = st.lists(_SEGMENT, min_size=1, max_size=8).flatmap(
        lambda segs: st.sampled_from(["/", "\\"]).map(lambda sep: sep.join(segs))
    )

    @settings(database=None)  # sin escribir en .hypothesis del repo
    @given(_RAW, st.booleans())
    def test_canonical_properties(raw: str, ci_fs: bool) -> None:
        try:
            rel = canonical(raw, "/home/ana/p", ci_fs)
        except (InvalidPath, OutsideRepo):
            return
        segs = rel.split("/")
        assert all(s not in ("", ".", "..") for s in segs)
        assert "\\" not in rel and not rel.startswith("/")
        assert segs[0] != "~"
        assert canonical(rel, "/home/ana/p", ci_fs) == rel  # idempotente
        info = classify(rel, DECLARED, symlinked=False)
        assert info.path == rel and info.rule in {f"R{i}" for i in range(8)}

    @settings(database=None)
    @given(st.text(max_size=40), st.booleans())
    def test_canonical_only_raises_its_errors(raw: str, ci_fs: bool) -> None:
        try:
            rel = canonical(raw, "C:\\Users\\ana\\p", ci_fs)
        except (InvalidPath, OutsideRepo):
            return
        # nada de lo que acepta es un control, un formato o un alias de Windows
        assert not any(unicodedata.category(c) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for c in rel)
        for seg in rel.split("/"):
            assert seg[-1] not in ". " and not set(seg) & set('<>:"|?*')
