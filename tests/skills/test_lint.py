# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""T6: validador propio de skills (12-mvp §3.7, REQ-MVP-35).

Se prueba con skills de ejemplo buenos y malos de ``tests/fixtures/skills/`` y sobre
el render de la plantilla de H2, que no trae skills y pasa sin casos.

Los fixtures no usan carpetas ``.claude`` ni ``.agents`` (Claude Code podría cargarlos
como skills del repositorio): ``claude/`` y ``agents/`` se copian a ``.claude/skills/``
y ``.agents/skills/`` en un directorio temporal, y ``${NAME}`` se sustituye por el
nombre del núcleo (D-03).
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path

import pytest

from aucove import _meta
from tests.skills import lint

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
FIXTURES = HERE.parent / "fixtures" / "skills"
SOURCE = FIXTURES / "source"
BAD = FIXTURES / "bad"
PLACEHOLDER = "${NAME}"
BASE_VARS: Mapping[str, str] = {"core_pin": "0.1.0", "convention": "0.1"}


def project_vars(lang: str) -> dict[str, str]:
    return {**BASE_VARS, "language": lang}


def _sub(data: bytes) -> bytes:
    return data.replace(PLACEHOLDER.encode("ascii"), _meta.NAME.encode("ascii"))


def materialize(src: Path, dest: Path) -> Path:
    """Copia un árbol de fixtures: ``claude/`` y ``agents/`` pasan a sus raíces de skills."""
    for path in sorted(src.rglob("*")):
        if not path.is_file() or path.name == "case.toml":
            continue
        top, _, rest = path.relative_to(src).as_posix().partition("/")
        if top == "source":
            continue  # la fuente la nombra case.toml y se copia con copy_tree
        if top not in lint.SKILL_ROOTS:
            raise AssertionError(f"fixture fuera de claude/ o agents/: {path}")
        target = dest.joinpath(*lint.SKILL_ROOTS[top].split("/"), *rest.split("/"))  # type: ignore[index]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_sub(path.read_bytes()))
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def copy_tree(src: Path, dest: Path) -> Path:
    for path in sorted(src.rglob("*")):
        if path.is_file():
            target = dest / path.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_sub(path.read_bytes()))
    return dest


def golden(lang: str) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    base = FIXTURES / "rendered" / lang
    for path in sorted(base.rglob("*")):
        if path.is_file():
            top, _, rest = path.relative_to(base).as_posix().partition("/")
            out[f"{lint.SKILL_ROOTS[top]}/{rest}"] = _sub(path.read_bytes())  # type: ignore[index]
    return out


def write_skill(
    root: Path,
    target: str,
    name: str,
    *,
    directory: str | None = None,
    description: str = "Muestra el estado del trabajo.",
    body: str = "# Estado\n\n1. Resume las tareas abiertas.\n",
    extra: str = "",
    generated: bool = True,
) -> Path:
    lines = ["---", f'name: "{name}"', f'description: "{description}"', 'license: "MIT-0"']
    if extra:
        lines.append(extra)
    if generated:
        lines += ["metadata:", f'  {lint.GENERATED_KEY}: "true"']
    lines.append("---")
    path = root.joinpath(*lint.SKILL_ROOTS[target].split("/"), directory or name, "SKILL.md")  # type: ignore[index]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n" + body, encoding="utf-8", newline="\n")
    return path


def rules_of(root: Path, lang: str = "es", **kwargs: object) -> frozenset[str]:
    return lint.lint(root, lang=lang, regen=False, **kwargs).rules()  # type: ignore[arg-type]


# --- skills de ejemplo buenos ----------------------------------------------------------


@pytest.mark.parametrize("lang", lint.LANGS)
def test_reference_render_matches_the_golden_files(lang: str) -> None:
    assert lint.render_skills(SOURCE, lang, project_vars(lang)) == golden(lang)  # type: ignore[arg-type]


@pytest.mark.parametrize("lang", lint.LANGS)
def test_good_skills_pass(lang: str, tmp_path: Path) -> None:
    tree = materialize(FIXTURES / "rendered" / lang, tmp_path / "tree")
    report = lint.lint(tree, lang=lang, source_dir=SOURCE, project_vars=project_vars(lang))
    assert report.problems == (), [str(p) for p in report.problems]
    assert len(report.skills) == 4
    assert {s.split("/")[0] for s in report.skills} == {".claude", ".agents"}


def test_golden_files_follow_the_frontmatter_contract() -> None:
    # REQ-ARCH-77: frontmatter en la primera línea, claves en el orden de 01 §10.3.
    for lang in lint.LANGS:
        for rel, data in golden(lang).items():
            text = data.decode("utf-8")
            keys = [line.split(":", 1)[0] for line in text.split("---\n")[1].splitlines() if not line.startswith(" ")]
            assert text.startswith("---\nname: ")
            expected = ["name", "description", "license"]
            if rel.startswith(".claude/") and "decision" in rel:
                expected += ["disable-model-invocation", "argument-hint"]
            assert keys == [*expected, "metadata"], rel


# --- skills de ejemplo malos -----------------------------------------------------------


def _bad_cases() -> list[str]:
    return sorted(p.name for p in BAD.iterdir() if (p / "case.toml").is_file())


def _load_case(name: str) -> dict[str, object]:
    return tomllib.loads((BAD / name / "case.toml").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", _bad_cases())
def test_bad_skill_is_detected(name: str, tmp_path: Path) -> None:
    case = _load_case(name)
    lang = str(case["lang"])
    tree = tmp_path / "tree"
    if "base" in case:
        materialize(FIXTURES / str(case["base"]), tree)
    materialize(BAD / name, tree)
    source = copy_tree(FIXTURES / str(case["source"]), tmp_path / "source") if "source" in case else None
    report = lint.lint(
        tree, lang=lang, source_dir=source, project_vars=project_vars(lang), regen=source is not None
    )
    assert report.rules() == frozenset(case["expect"]), [str(p) for p in report.problems]  # type: ignore[arg-type]
    for problem in report.problems:
        assert not Path(problem.path).is_absolute() and "\\" not in problem.path


def test_bad_examples_cover_every_required_check() -> None:
    covered = set().union(*(set(_load_case(n)["expect"]) for n in _bad_cases()))  # type: ignore[arg-type]
    required = {
        "name-dir", "name-catalog", "name-reserved", "name-format", "description",
        "body-length", "human-op", "claude-tool", "regen",
    }
    assert required <= covered
    assert {"description-empty", "description-too-long", "body-too-long"} <= set(_bad_cases())


# --- nombre -----------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["estado", "project-status", "a1-b2", "x" * 64])
def test_name_format_accepts(name: str) -> None:
    assert lint.NAME_RE.fullmatch(name) and len(name) <= lint.MAX_NAME


@pytest.mark.parametrize("name", ["-estado", "estado-", "project--status", "Estado", "es_tado", "es tado", "x" * 65])
def test_name_format_rejects(name: str, tmp_path: Path) -> None:
    write_skill(tmp_path, "claude", name, directory="skill")
    assert "name-format" in rules_of(tmp_path)


def test_catalog_names_follow_00_marco_5_8() -> None:
    assert lint.catalog_names("es") == {"empezar", "estado", "entregar", "pendientes", "decision", "direccion"}
    assert lint.catalog_names("en") == {"start", "project-status", "submit", "reviews", "decision", "direction"}
    for lang in lint.LANGS:
        names = [n[lang] for n in lint.SKILL_NAMES.values()]
        assert len(names) == len(set(names))
        for name in names:
            assert lint.NAME_RE.fullmatch(name) and name not in lint.RESERVED_NAMES
    # Por qué 00-marco §5.8 no usa status ni review (REQ-ARCH-78).
    assert {"status", "review", "init", "doctor", "context"} <= lint.RESERVED_NAMES


def test_catalog_name_depends_on_the_language(tmp_path: Path) -> None:
    write_skill(tmp_path, "claude", "project-status")
    assert rules_of(tmp_path, "en") == frozenset()
    assert rules_of(tmp_path, "es") == {"name-catalog"}


def test_source_metadata_must_match_the_name(tmp_path: Path) -> None:
    extra = f'metadata:\n  {lint.GENERATED_KEY}: "true"\n  {lint.SOURCE_KEY}: "submit"'
    write_skill(tmp_path, "claude", "estado", extra=extra, generated=False)
    assert rules_of(tmp_path) == {"name-catalog"}


# --- description y cuerpo --------------------------------------------------------------


def test_description_limits(tmp_path: Path) -> None:
    write_skill(tmp_path / "a", "claude", "estado", description="x" * 1024)
    assert rules_of(tmp_path / "a") == frozenset()
    write_skill(tmp_path / "b", "claude", "estado", description="x" * 1025)
    assert rules_of(tmp_path / "b") == {"description"}
    write_skill(tmp_path / "c", "claude", "estado", description="   ")
    assert rules_of(tmp_path / "c") == {"description"}
    write_skill(tmp_path / "d", "claude", "estado", description="x" * 1000, extra=f'when_to_use: "{"y" * 537}"')
    assert rules_of(tmp_path / "d") == {"description"}
    write_skill(tmp_path / "e", "claude", "estado", description="x" * 1000, extra=f'when_to_use: "{"y" * 536}"')
    assert rules_of(tmp_path / "e") == frozenset()


def test_body_must_have_fewer_than_500_lines(tmp_path: Path) -> None:
    write_skill(tmp_path / "ok", "claude", "estado", body="".join(f"{n}\n" for n in range(499)))
    assert rules_of(tmp_path / "ok") == frozenset()
    write_skill(tmp_path / "bad", "claude", "estado", body="".join(f"{n}\n" for n in range(500)))
    problems = lint.lint(tmp_path / "bad", lang="es", regen=False).problems
    assert [p.rule for p in problems] == ["body-length"]
    assert problems[0].line == 8  # primera línea del cuerpo, tras el frontmatter


# --- operaciones H y herramientas de Claude Code ---------------------------------------


@pytest.mark.parametrize(
    ("command", "human"),
    [
        ("review approve 12", True),
        ("review request-changes 12", True),
        ("init --regenerate", True),
        ("task accept 3", True),
        ("task cancel 3", True),
        ("task propose", False),
        ("review list", False),
        ("decision propose", False),
        ("validate", False),
    ],
)
def test_is_human_only(command: str, human: bool) -> None:
    assert lint.is_human_only(command) is human


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        (f"Ejecuta `{_meta.NAME} task accept 3`.", True),
        (f"Después, {_meta.NAME} init --regenerate.", True),
        (f"Usa `{_meta.NAME} review approve` (only in your terminal; the agent cannot run it).", True),
        (f"Usa `{_meta.NAME} review approve` (solo en tu terminal; el agente no puede ejecutarla).", False),
        (f"Usa `{_meta.NAME} decision propose`.", False),
        (f"Usa `mcp__{_meta.NAME}__check_path`.", False),
    ],
)
def test_human_op_detection(text: str, flagged: bool, tmp_path: Path) -> None:
    write_skill(tmp_path, "claude", "estado", body=f"# Estado\n\n{text}\n")
    assert ("human-op" in rules_of(tmp_path)) is flagged


@pytest.mark.parametrize(
    ("text", "flagged"),
    [
        ("Usa la herramienta Edit.", True),
        ("Write the summary.", True),
        ("Usa NotebookEdit.", True),
        ("Usa MultiEdit.", True),
        (f"Usa `mcp__{_meta.NAME}__check_path`.", True),
        ("Puedes edit o write libremente; revisa el Editor.", False),
        ("Usa `check_path`.", False),
    ],
)
def test_claude_tools_only_in_agents(text: str, flagged: bool, tmp_path: Path) -> None:
    body = f"# Estado\n\n{text}\n"
    write_skill(tmp_path / "agents", "agents", "estado", body=body)
    assert ("claude-tool" in rules_of(tmp_path / "agents")) is flagged
    write_skill(tmp_path / "claude", "claude", "estado", body=body)
    assert "claude-tool" not in rules_of(tmp_path / "claude")


def test_skill_directory_without_skill_md(tmp_path: Path) -> None:
    (tmp_path / ".claude" / "skills" / "estado").mkdir(parents=True)
    assert rules_of(tmp_path) == {"layout"}


def test_unparseable_frontmatter(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "skills" / "estado" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"---\nname: [\n---\n# x\n")
    assert rules_of(tmp_path) == {"frontmatter"}
    path.write_bytes(b"---\nname: estado\n# sin cierre\n")
    assert rules_of(tmp_path) == {"frontmatter"}
    path.write_bytes(b"\xef\xbb\xbf---\nname: estado\n---\n")
    assert rules_of(tmp_path) == {"frontmatter"}
    path.write_bytes(b"---\n- a\n---\n")
    assert rules_of(tmp_path) == {"frontmatter"}


# --- regeneración -----------------------------------------------------------------------


def _good_tree(tmp: Path, lang: str = "es") -> Path:
    return materialize(FIXTURES / "rendered" / lang, tmp / "tree")


def _regen(tree: Path, lang: str = "es", source: Path = SOURCE) -> list[lint.Problem]:
    report = lint.lint(tree, lang=lang, source_dir=source, project_vars=project_vars(lang))
    return [p for p in report.problems if p.rule == "regen"]


def test_regeneration_detects_a_missing_file(tmp_path: Path) -> None:
    tree = _good_tree(tmp_path)
    (tree / ".agents" / "skills" / "estado" / "SKILL.md").unlink()
    problems = _regen(tree)
    assert [(p.path, p.message.split(":")[0]) for p in problems] == [(".agents/skills/estado/SKILL.md", "falta")]


def test_regeneration_detects_an_extra_file(tmp_path: Path) -> None:
    tree = _good_tree(tmp_path)
    (tree / ".claude" / "skills" / "estado" / "notes.md").write_text("x\n", encoding="utf-8")
    assert [p.path for p in _regen(tree)] == [".claude/skills/estado/notes.md"]


def test_regeneration_detects_a_changed_line(tmp_path: Path) -> None:
    tree = _good_tree(tmp_path)
    path = tree / ".claude" / "skills" / "decision" / "SKILL.md"
    path.write_bytes(path.read_bytes().replace(b"disable-model-invocation: true", b"disable-model-invocation: false"))
    problems = _regen(tree)
    assert [(p.path, p.line) for p in problems] == [(".claude/skills/decision/SKILL.md", 5)]


def test_regeneration_detects_crlf(tmp_path: Path) -> None:
    tree = _good_tree(tmp_path)
    path = tree / ".agents" / "skills" / "decision" / "SKILL.md"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert [p.path for p in _regen(tree)] == [".agents/skills/decision/SKILL.md"]


def test_regeneration_ignores_skills_the_project_wrote(tmp_path: Path) -> None:
    tree = _good_tree(tmp_path)
    write_skill(tree, "claude", "direccion", description="Propón un cambio de dirección.", generated=False)
    assert _regen(tree) == []


def test_regeneration_reports_a_render_failure(tmp_path: Path) -> None:
    source = copy_tree(SOURCE, tmp_path / "source")
    body = source / "status" / "body.es.md"
    body.write_text(body.read_text(encoding="utf-8") + "\n{{tool:read_section}}\n", encoding="utf-8", newline="\n")
    problems = _regen(_good_tree(tmp_path), source=source)
    assert len(problems) == 1 and "no se pudo regenerar" in problems[0].message
    assert "read_section" in problems[0].message  # reservada: nunca se registra (REQ-OPS-41)


def test_empty_source_and_tree_pass(tmp_path: Path) -> None:
    source = tmp_path / "skills"
    source.mkdir()
    (source / "LICENSE").write_text("texto\n", encoding="utf-8")
    report = lint.lint(tmp_path, lang="en", source_dir=source, project_vars=project_vars("en"))
    assert report.problems == () and report.skills == ()


# --- render de referencia y fuente -----------------------------------------------------


def _source_with(tmp: Path, *, toml: str | None = None, body_es: str | None = None) -> Path:
    source = copy_tree(SOURCE, tmp / "source")
    if toml is not None:
        (source / "status" / "skill.toml").write_text(toml, encoding="utf-8", newline="\n")
    if body_es is not None:
        (source / "status" / "body.es.md").write_text(body_es, encoding="utf-8", newline="\n")
    return source


STATUS_TOML = """key = "status"
cli = ["status"]

[name]
es = "estado"
en = "project-status"

[description]
es = "Muestra el estado."
en = "Show the status."
"""


@pytest.mark.parametrize(
    ("toml", "body", "match"),
    [
        (STATUS_TOML.replace('key = "status"', 'key = "estado"'), None, "key"),
        (STATUS_TOML.replace('en = "project-status"\n', ""), None, "name"),
        (STATUS_TOML.replace('es = "estado"', 'es = "Estado"'), None, "REQ-ARCH-78"),
        (STATUS_TOML.replace('en = "project-status"', 'en = "decision"'), None, "repetidos"),
        (STATUS_TOML.replace('es = "Muestra el estado."', 'es = ""'), None, "description"),
        (STATUS_TOML + "\n[claude]\nargument-hint = 3\n", None, "claude.argument-hint"),
        (None, "{{foo:bar}}\n", "desconocido"),
        (None, "{{cli:review approve}}\n", "REQ-ARCH-80"),
        (None, "{{tool:read_section}}\n", "registro"),
        (None, "{{skill:start}}\n", "skill"),
        (None, "{{var:title}}\n", "var:title"),
    ],
)
def test_render_rejects_invalid_sources(toml: str | None, body: str | None, match: str, tmp_path: Path) -> None:
    source = _source_with(tmp_path, toml=toml, body_es=body)
    with pytest.raises(lint.RenderError, match=match):
        lint.render_skills(source, "es", project_vars("es"))


def test_render_needs_every_body(tmp_path: Path) -> None:
    source = _source_with(tmp_path)
    (source / "status" / "body.en.md").unlink()
    with pytest.raises(lint.RenderError, match="body.en.md"):
        lint.render_skills(source, "en", project_vars("en"))


def test_render_resolves_markers_per_target() -> None:
    names = {"status": "estado"}
    kw = {"lang": "es", "project_vars": project_vars("es"), "names": names, "where": "t"}
    body = "{{tool:check_path}} {{skill:status}} {{cli:validate}} {{human:init}} {{var:core_pin}}"
    claude = lint.resolve_markers(body, target="claude", **kw)  # type: ignore[arg-type]
    agents = lint.resolve_markers(body, target="agents", **kw)  # type: ignore[arg-type]
    n = _meta.NAME
    assert claude == (
        f"`mcp__{n}__check_path` `/estado` `{n} validate` "
        f"`{n} init` (solo en tu terminal; el agente no puede ejecutarla) 0.1.0"
    )
    assert agents == (
        f"`check_path` `estado` `{n} validate` `{n} init` (solo en tu terminal; el agente no puede ejecutarla) 0.1.0"
    )


def test_check_sources_reports_markers(tmp_path: Path) -> None:
    body = "a {{cli:task accept 3}}\nb {{foo:bar}}\nc {{human:review approve}}\nd {{tool:nope}}\ne {{var:title}}\n"
    source = _source_with(tmp_path, body_es=body)
    found = [(p.path, p.line, p.rule) for p in lint.check_sources(source)]
    assert found == [
        ("source/status/body.es.md", 1, "human-op"),
        ("source/status/body.es.md", 2, "marker"),
        ("source/status/body.es.md", 4, "marker"),
        ("source/status/body.es.md", 5, "marker"),
    ]


# --- idioma y punto de entrada ---------------------------------------------------------


def test_language_comes_from_project_yaml(tmp_path: Path) -> None:
    tree = _good_tree(tmp_path, "en")
    (tree / "project.yaml").write_text('convention: "0.1"\ncore_pin: "0.1.0"\nlanguage: en\n', encoding="utf-8")
    report = lint.lint(tree, source_dir=SOURCE)
    assert report.problems == ()
    (tree / "project.yaml").unlink()
    assert lint.lint(tree, source_dir=SOURCE).rules() == {"language"}


def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = _good_tree(tmp_path, "es")
    assert lint.main([str(good), "--lang", "es", "--source", str(SOURCE)]) == 1  # sin project.yaml: faltan vars
    (good / "project.yaml").write_text('convention: "0.1"\ncore_pin: "0.1.0"\nlanguage: es\n', encoding="utf-8")
    assert lint.main([str(good), "--source", str(SOURCE)]) == 0
    assert "4 skills revisados, 0 problemas" in capsys.readouterr().out
    write_skill(good, "claude", "review")
    assert lint.main([str(good), "--no-regen"]) == 1
    assert "[name-reserved]" in capsys.readouterr().out


# --- el render de la plantilla de H2 ---------------------------------------------------


def _render_template(out: Path, lang: str) -> Path:
    if not (REPO_ROOT / "tools" / "render_template.py").is_file():
        pytest.skip("tools/render_template.py no existe todavía")
    from tests.conftest import load_tool

    load_tool("render_template").render_to(out, lang)
    return out


@pytest.mark.parametrize("lang", lint.LANGS)
def test_template_render_passes_without_cases(lang: str, tmp_path: Path) -> None:
    root = _render_template(tmp_path / lang, lang)
    assert lint.read_project_vars(root).get("language") == lang
    report = lint.lint(root)  # idioma de project.yaml y fuente empaquetada (skills/)
    assert report.problems == (), [str(p) for p in report.problems]
    source_skills = [p for p in lint.default_source().iterdir() if p.is_dir()]
    if not source_skills:  # H2: la plantilla no trae skills (12-mvp §4)
        assert report.skills == ()
        assert not (root / ".claude" / "skills").exists() and not (root / ".agents" / "skills").exists()


# --- coherencia con la spec (con AUCOVE_SPEC_DIR) ------------------------------------


def _spec_dir() -> Path:
    value = os.environ.get(_meta.ENV_PREFIX + "SPEC_DIR")
    if not value or not (Path(value) / "00-marco.md").is_file():
        pytest.skip(f"{_meta.ENV_PREFIX}SPEC_DIR no apunta a la spec")
    return Path(value)


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    nxt = re.search(r"^#{2,3} ", text[start + len(heading) :], re.M)
    return text[start : start + len(heading) + (nxt.start() if nxt else len(text))]


def _rows(section: str) -> list[list[str]]:
    rows = []
    for line in section.splitlines():
        if line.startswith("|") and not re.match(r"^\|[-| ]+\|$", line):
            rows.append([c.strip() for c in line.strip("|").split("|")])
    return rows[1:]  # sin la cabecera


def test_skill_names_match_the_spec() -> None:
    spec = _spec_dir()
    marco = (spec / "00-marco.md").read_text(encoding="utf-8")
    rows = _rows(_section(marco, "### 5.8"))
    pairs = [(r[1].strip("`"), r[2].strip("`")) for r in rows]
    arch = (spec / "01-arquitectura.md").read_text(encoding="utf-8")
    keys_line = re.search(r"<key> = ([a-z, -]+) \(clave interna", arch)
    assert keys_line is not None
    keys = [k.strip() for k in keys_line.group(1).split(",")]
    assert {k: {"es": es, "en": en} for k, (es, en) in zip(keys, pairs)} == lint.SKILL_NAMES


def test_human_only_operations_match_the_spec() -> None:
    marco = (_spec_dir() / "00-marco.md").read_text(encoding="utf-8")
    ops: set[str] = set()
    for row in _rows(_section(marco, "### 5.7")):
        if len(row) >= 4 and row[3] == "H":
            command = re.match(r"`([^`]+)`", row[0])
            assert command is not None
            words = [w for w in command.group(1).split()[1:] if not w.startswith("<")]
            ops.add(" ".join(words))
    assert ops == lint.HUMAN_ONLY_OPS
