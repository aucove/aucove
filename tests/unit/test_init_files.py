# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Los archivos que escribe ``init`` (05 §9.3; 01 REQ-ARCH-44, 85 y 87; 02 REQ-CONV-2 y 3).

El punto de partida es la plantilla publicada: el render ``en`` de ``format: latex`` con los
valores de ``tools/template_vars.toml``. Cada plan se aplica en memoria y el resultado pasa
por todas las reglas reales del modo ``tree`` (REQ-CONV-3: sin errores).
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import replace

import pytest

from aucove import _meta
from aucove.config.project import parse_project
from aucove.config.roles import parse_roles
from aucove.i18n import Lang, translator
from aucove.ops.init_files import TEMPLATE_TITLE, Choices, FilePlan, Kept, core_range, plan_files, plan_generated
from aucove.ops.result import OpError
from tests.conftest import REPO_ROOT, load_tool
from tests.unit.test_format_none import evaluate, found

REPO = "lab-x/Estudio_X"
LEAD = "ana-garcia"
VERSION = _meta.__version__
CHOICES = Choices(language="es", format="latex", disclosure="milestones", individual=False, repo=REPO,
                  core_pin=VERSION, lead=LEAD)
Files = dict[str, bytes]


@pytest.fixture(scope="module")
def published() -> Mapping[str, bytes]:
    """La plantilla publicada: render en de format: latex (01 §2.3)."""
    return load_tool("render_template").render_files("en", fmt="latex")


def plan(files: Mapping[str, bytes], choices: Choices = CHOICES) -> FilePlan:
    return plan_files(files.get, files.keys(), choices, t=translator(choices.language))


def apply(files: Mapping[str, bytes], result: FilePlan) -> Files:
    out = dict(files)
    for change in result.changes:
        if change.action == "deleted":
            del out[change.path]
        else:
            assert change.content is not None
            out[change.path] = change.content
    return out


def errors(files: Mapping[str, bytes], lang: Lang) -> list[tuple[str, str, str | None]]:
    return found(evaluate(files, files, lang=lang), "error")


def text(files: Mapping[str, bytes], path: str) -> str:
    return files[path].decode("utf-8")


def test_a_fresh_spanish_project(published: Mapping[str, bytes]) -> None:
    result = plan(published)
    after = apply(published, result)
    assert result.kept == () and result.roles_completed
    assert errors(after, "es") == []  # REQ-CONV-3
    project = text(after, "project.yaml")
    assert f'core: ">={VERSION},<0.2"' in project and f'core_pin: "{VERSION}"' in project  # REQ-ARCH-44
    assert "name: estudio-x\n" in project and 'title: "Estudio_X"\n' in project
    assert "language: es\n" in project and f"  repo: {REPO}\n" in project
    assert "disclosure: milestones        # private | milestones | open" in project  # el comentario sigue
    assert "require_commercial" not in project.split("data_policy:")[1].split("#")[0]  # D-48
    roles, _ = parse_roles(after["roles.yaml"])
    assert roles is not None and roles.lead == LEAD and [p.login for p in roles.people] == [LEAD]
    assert {s.owner for s in roles.sections} == {LEAD}
    assert roles.sections[0].title == "Introducción"  # roles.yaml en el idioma elegido
    assert text(after, "README.md").startswith("# Estudio_X\n\nProyecto de investigación")
    assert f"*   @{LEAD}\n" in text(after, ".github/CODEOWNERS")
    assert "Introducción" in text(after, ".github/ISSUE_TEMPLATE/task.yml")
    assert "\\section{Introducción}" in text(after, "tex/outline/introduction.tex")
    assert text(after, "sections/introduction/introduction.tex").startswith("% Parte de ejemplo «introduction»")
    assert f"https://github.com/{REPO}#readme" in text(after, ".github/ISSUE_TEMPLATE/config.yml")
    assert {c.action for c in result.changes} == {"updated"}  # nada se crea ni se borra
    assert plan(after).changes == ()  # una segunda ejecución no cambia nada (REQ-PERM-25)


def test_english_without_manuscript_and_individual(published: Mapping[str, bytes]) -> None:
    choices = replace(CHOICES, language="en", format="none", individual=True)
    result = plan(published, choices)
    after = apply(published, result)
    assert result.kept == ()
    assert errors(after, "en") == []
    assert not [p for p in after if p.startswith(("tex/", "bib/")) or p == ".latexmkrc"]
    assert not [p for p in after if p.endswith(("pending.bib", ".tex"))]
    assert after["sections/methodology/methodology.md"].startswith(b"# Proposal and methodology\n")
    project = text(after, "project.yaml")
    assert "format: none" in project and "mode: individual" in project
    assert "build:" not in project and "latexdiff" not in project and "pdf_retention_days" not in project
    cfg, _ = parse_project(after["project.yaml"])
    assert cfg is not None and cfg.mode == "individual" and cfg.format == "none"
    constitution = text(after, "memory/constitution.md")
    assert "tex/" not in constitution  # la variante none de la constitución (REQ-CONV-91)
    assert plan(after, choices).changes == ()


def test_parts_follow_roles(published: Mapping[str, bytes]) -> None:
    roles = text(published, "roles.yaml").replace("example-lead", LEAD).replace("example-researcher", "bruno")
    start = roles.index("sections:")
    roles = roles[:start] + (
        "sections:\n"
        "  - id: introduction\n    title: \"Introduction\"\n    owner: ana-garcia\n"
        "  - id: experiments-a\n    title: \"Field & lab experiments\"\n    owner: bruno\n"
    )
    files = {**published, "roles.yaml": roles.encode("utf-8")}
    result = plan(files)
    after = apply(files, result)
    assert not result.roles_completed and result.kept == ()
    assert errors(after, "es") == []
    assert after["roles.yaml"] == files["roles.yaml"]  # lo escribió el responsable: no se toca
    assert not [p for p in after if p.startswith(("sections/methodology/", "tex/outline/methodology"))]
    assert sorted(p for p in after if p.startswith("sections/experiments-a/")) == [
        "sections/experiments-a/experiments-a.tex", "sections/experiments-a/figures/.gitkeep",
        "sections/experiments-a/pending.bib", "sections/experiments-a/tables/.gitkeep"]
    outline = text(after, "tex/outline/experiments-a.tex")
    assert "\\section{Field \\& lab experiments}\n\\label{sec:experiments-a}\n" in outline
    assert "\\input{../sections/experiments-a/experiments-a}" in outline
    main = text(after, "tex/main.tex")
    assert "\\addbibresource{../sections/experiments-a/pending.bib}" in main and "methodology" not in main
    assert main.index("\\input{outline/introduction}") < main.index("\\input{outline/experiments-a}") \
        < main.index("\\printbibliography")
    assert plan(after).changes == ()


def test_changed_files_are_kept(published: Mapping[str, bytes]) -> None:
    readme = published["README.md"] + b"\nOur own notes.\n"
    figure = b"\x89PNG\r\n\x1a\n"
    files = {**published, "README.md": readme, "sections/results/figures/f1.png": figure}
    roles = text(files, "roles.yaml").replace("example-lead", LEAD).replace("example-researcher", "bruno")
    files["roles.yaml"] = _without_part(roles, "results").encode("utf-8")
    result = plan(files)
    after = apply(files, result)
    assert Kept("README.md", "modified") in result.kept and after["README.md"] == readme  # REQ-ARCH-85
    assert Kept("sections/results/", "part_modified") in result.kept
    assert after["sections/results/figures/f1.png"] == figure and "sections/results/results.tex" in after
    assert "\\addbibresource{../sections/results/pending.bib}" in text(after, "tex/main.tex")
    assert text(after, "AGENTS.md").startswith("# Instrucciones para agentes")


def _without_part(roles: str, part: str) -> str:
    lines = roles.split("\n")
    start = lines.index(f"  - id: {part}")
    return "\n".join(lines[:start] + lines[start + 3 :])


def test_crlf_checkouts_are_unchanged_files(published: Mapping[str, bytes]) -> None:
    """Un checkout con ``core.autocrlf`` trae CRLF: siguen siendo los archivos de la plantilla."""
    files = {p: (c.replace(b"\n", b"\r\n") if p.endswith((".md", ".tex", ".yaml")) else c) for p, c in published.items()}
    choices = replace(CHOICES, language="en")
    result = plan(files, choices)
    assert result.kept == ()
    assert "AGENTS.md" not in {c.path for c in result.changes}  # solo cambian los finales de línea
    assert plan(apply(files, result), choices).changes == ()


def test_customized_name_and_title_stay(published: Mapping[str, bytes]) -> None:
    project = text(published, "project.yaml").replace(f"name: {_meta.TEMPLATE_REPO}", "name: mi-estudio")
    project = project.replace(f'title: "{TEMPLATE_TITLE}"', 'title: "Mi estudio"')
    files = {**published, "project.yaml": project.encode("utf-8")}
    after = apply(files, plan(files))
    assert "name: mi-estudio\n" in text(after, "project.yaml") and 'title: "Mi estudio"\n' in text(after, "project.yaml")
    assert text(after, "README.md").startswith("# Mi estudio\n")


def test_base_branch_in_workflows(published: Mapping[str, bytes]) -> None:
    project = text(published, "project.yaml").replace("base_branch: main", "base_branch: trunk")
    files = {**published, "project.yaml": project.encode("utf-8")}
    after = apply(files, plan(files))
    assert "branches: [trunk]          # = project.yaml: forge.base_branch" in text(after, ".github/workflows/seal.yml")


def test_refusals(published: Mapping[str, bytes]) -> None:
    none = apply(published, plan(published, replace(CHOICES, format="none")))
    with pytest.raises(OpError) as caught:
        plan(none, CHOICES)  # REQ-CONV-88: volver a LaTeX es una tarea del responsable
    assert caught.value.code == "E_VALIDATION_INPUT"
    assert caught.value.details == {"errors": [{"param": "format", "reason": "format_change"}]}
    two = text(published, "roles.yaml").replace("example-lead", LEAD).replace("example-researcher", "bruno")
    with pytest.raises(OpError) as caught:
        plan({**published, "roles.yaml": two.encode("utf-8")}, replace(CHOICES, individual=True))
    assert caught.value.code == "E_CONFIG_INVALID" and caught.value.details["reason"] == "roles_mode"
    broken = {**published, "roles.yaml": b"version: 1\nlead: [\n"}
    with pytest.raises(OpError) as caught:
        plan(broken)
    assert caught.value.code == "E_CONFIG_INVALID" and caught.value.details["path"] == "roles.yaml"
    with pytest.raises(OpError) as caught:
        plan(published, replace(CHOICES, core_pin="0.1.0-rc1"))  # sin normalizar (REQ-ARCH-45, D-66)
    assert caught.value.details["reason"] == "core_pin_version"


def test_prerelease_core_pin(published: Mapping[str, bytes]) -> None:
    """00-marco D-66: init fija una pre-release y la usa como cota inferior de ``core`` (REQ-ARCH-44)."""
    after = apply(published, plan(published, replace(CHOICES, core_pin="0.1.0rc1")))
    project = text(after, "project.yaml")
    assert 'core: ">=0.1.0rc1,<0.2"' in project and 'core_pin: "0.1.0rc1"' in project
    cfg, findings = parse_project(after["project.yaml"])
    assert findings == [] and cfg is not None and cfg.core_pin == "0.1.0rc1"


def test_regenerate_writes_only_generated_files(published: Mapping[str, bytes]) -> None:
    after = apply(published, plan(published))
    roles = text(after, "roles.yaml")
    generated = (b'---\nname: "empezar"\nmetadata:\n  ' + _meta.NAME.encode() + b'-generated: "true"\n---\nBody\n')
    own = b'---\nname: "mine"\n---\nBody\n'
    files = {**after, "roles.yaml": roles.replace("title: \"Introducción\"", "title: \"Entrada\"").encode("utf-8"),
             ".claude/skills/empezar/SKILL.md": generated, ".claude/skills/empezar/notes.md": b"x\n",
             ".agents/skills/mine/SKILL.md": own}
    changes = plan_generated(files.get, files.keys(), t=translator("es"))
    assert [(c.path, c.action) for c in changes] == [  # CODEOWNERS cita el sha de roles.yaml
        (".claude/skills/empezar/SKILL.md", "deleted"), (".claude/skills/empezar/notes.md", "deleted"),
        (".github/CODEOWNERS", "updated"), (".github/ISSUE_TEMPLATE/task.yml", "updated")]
    assert plan_generated(after.get, after.keys(), t=translator("es")) == ()


def test_published_defaults() -> None:
    values = tomllib.loads((REPO_ROOT / "tools" / "template_vars.toml").read_text(encoding="utf-8"))
    assert values["title"] == TEMPLATE_TITLE and values["name"] == _meta.TEMPLATE_REPO


@pytest.mark.parametrize(("version", "expected"), [
    ("0.1.0", ">=0.1.0,<0.2"), ("0.1.3", ">=0.1.3,<0.2"), ("1.2.3", ">=1.2.3,<2"), ("0.10.0", ">=0.10.0,<0.11"),
    ("0.1.0rc1", ">=0.1.0rc1,<0.2"), ("1.2.3b4", ">=1.2.3b4,<2"), ("2.0.0a1", ">=2.0.0a1,<3")])  # D-66
def test_core_range(version: str, expected: str) -> None:
    assert core_range(version) == expected  # REQ-ARCH-44


@pytest.mark.parametrize("version", ["0.1.0-rc1", "0.1.0.dev1", "0.1.0RC1", "0.1", "0.1.0rc"])
def test_core_range_rejects_other_versions(version: str) -> None:
    with pytest.raises(ValueError):
        core_range(version)  # solo X.Y.Z o X.Y.Z{a,b,rc}N normalizada (D-66)
