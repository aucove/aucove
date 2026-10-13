# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``tools/render_template.py``: render de la plantilla con sus generados (01 §2.3, §10.5)."""

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

from aucove.config.project import parse_project
from aucove.config.roles import parse_roles
from aucove.domain.text import sha12_canonical
from aucove.i18n import LANGS
from aucove.render.generated import GENERATED_PATHS, generated_files
from aucove.render.template import is_text
from tests.conftest import FIXTURES, REPO_ROOT, load_tool

MANIFEST = FIXTURES / "template-manifest.txt"
CONSTITUTION = "memory/constitution.md"


@pytest.fixture(scope="module")
def tool():  # type: ignore[no-untyped-def]
    return load_tool("render_template")


@pytest.fixture(scope="module")
def rendered(tool, tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Path, list[str]]]:  # type: ignore[no-untyped-def]
    out: dict[str, tuple[Path, list[str]]] = {}
    for lang in LANGS:
        target = tmp_path_factory.mktemp(f"render-{lang}") / "out"
        out[lang] = (target, tool.render_to(target, lang))
    return out


def tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def manifest() -> list[str]:
    return MANIFEST.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("lang", LANGS)
def test_paths_equal_the_manifest(rendered, lang: str) -> None:  # type: ignore[no-untyped-def]
    root, written = rendered[lang]
    assert written == manifest()
    assert sorted(tree(root)) == manifest()


@pytest.mark.parametrize("lang", LANGS)
def test_render_is_deterministic(tool, rendered, lang: str, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    root, _ = rendered[lang]
    again = tmp_path / "again"
    tool.render_to(again, lang)
    assert tree(again) == tree(root)


@pytest.mark.parametrize("lang", LANGS)
def test_generated_files_come_from_the_rendered_roles(rendered, lang: str) -> None:  # type: ignore[no-untyped-def]
    root, _ = rendered[lang]
    files = tree(root)
    roles_bytes = files["roles.yaml"]
    roles = yaml.safe_load(roles_bytes)
    expected = generated_files(
        lead=roles["lead"],
        deputy=roles.get("deputy"),
        sections=[(s["id"], s["title"], s["owner"]) for s in roles["sections"]],
        source_sha12=sha12_canonical(roles_bytes),
        lang=lang,  # type: ignore[arg-type]
    )
    for path in GENERATED_PATHS:
        assert files[path] == expected[path], path
    assert f"# source: roles.yaml sha256:{sha12_canonical(roles_bytes)}\n".encode() in files[".github/CODEOWNERS"]
    options = yaml.safe_load(files[".github/ISSUE_TEMPLATE/task.yml"])["body"][2]["attributes"]["options"]
    assert options == [s["id"] for s in roles["sections"]]


def test_sha_matches_the_roles_parser(rendered) -> None:  # type: ignore[no-untyped-def]
    # config.roles es de H2: se importa en la cabecera, sin importorskip.
    root, _ = rendered["en"]
    text = (root / "roles.yaml").read_text(encoding="utf-8")
    parsed, _findings = parse_roles(text)
    assert parsed is not None
    assert f"sha256:{parsed.source_sha12}\n".encode() in (root / ".github" / "CODEOWNERS").read_bytes()


def test_codeowners_is_shared_and_task_form_is_localized(rendered) -> None:  # type: ignore[no-untyped-def]
    es, en = tree(rendered["es"][0]), tree(rendered["en"][0])
    def body(data: bytes) -> list[bytes]:
        return [line for line in data.split(b"\n") if not line.startswith(b"# source: ")]

    # D-35 j: CODEOWNERS no tiene texto localizado; solo cambia el sha de su roles.yaml, que desde
    # 00-marco D-55 se localiza (los títulos de las partes y los comentarios).
    assert body(es[".github/CODEOWNERS"]) == body(en[".github/CODEOWNERS"])
    assert es[".github/CODEOWNERS"] != en[".github/CODEOWNERS"]
    assert es[".github/ISSUE_TEMPLATE/task.yml"] != en[".github/ISSUE_TEMPLATE/task.yml"]
    assert es["AGENTS.md"] != en["AGENTS.md"]
    assert b"language: es\n" in es["project.yaml"]
    assert b"language: en\n" in en["project.yaml"]


@pytest.mark.parametrize("lang", LANGS)
def test_text_output_is_utf8_lf_with_final_newline(rendered, lang: str) -> None:  # type: ignore[no-untyped-def]
    for rel, data in tree(rendered[lang][0]).items():
        if not is_text(data) or not data:
            continue
        assert b"\r" not in data, rel
        assert not data.startswith(b"\xef\xbb\xbf"), rel
        assert data.endswith(b"\n") and not data.endswith(b"\n\n"), rel
        assert b"{{var:" not in data, rel


def test_check_passes_on_a_fresh_render_and_ignores_git_and_crlf(tool, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    out = tmp_path / "out"
    tool.render_to(out, "en")
    assert tool.main(["--lang", "en", "--out", str(out), "--check"]) == 0
    (out / ".git").mkdir()
    (out / ".git" / "HEAD").write_bytes(b"ref: refs/heads/main\n")
    readme = out / "README.md"
    readme.write_bytes(readme.read_bytes().replace(b"\n", b"\r\n"))  # checkout con CRLF (REQ-ARCH-11)
    assert tool.main(["--lang", "en", "--out", str(out), "--check"]) == 0


@pytest.mark.parametrize("change", ["modify", "extra", "missing", "language"])
def test_check_detects_a_difference(tool, tmp_path: Path, change: str, capsys: pytest.CaptureFixture[str]) -> None:  # type: ignore[no-untyped-def]
    out = tmp_path / "out"
    tool.render_to(out, "en")
    lang = "en"
    if change == "modify":
        target = out / ".github" / "CODEOWNERS"
        target.write_bytes(target.read_bytes() + b"* @mallory\n")
    elif change == "extra":
        (out / "notes.txt").write_bytes(b"x\n")
    elif change == "missing":
        (out / "plan.md").unlink()
    else:
        lang = "es"
    assert tool.main(["--lang", lang, "--out", str(out), "--check"]) == 1
    assert capsys.readouterr().err.strip()


def test_main_writes_and_applies_set_and_core_pin(tool, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:  # type: ignore[no-untyped-def]
    out = tmp_path / "out"
    code = tool.main(["--lang", "es", "--out", str(out), "--set", "title=Estudio X", "--core-pin", "9.8.7"])
    assert code == 0
    assert capsys.readouterr().out.splitlines() == manifest()
    project = yaml.safe_load((out / "project.yaml").read_bytes())
    assert project["title"] == "Estudio X"
    assert project["core_pin"] == "9.8.7"
    assert project["core"] == ">=9.8.7,<10"  # derivado como lo escribe init (REQ-ARCH-44)
    assert project["language"] == "es"
    # --check con las mismas opciones pasa; sin ellas, difiere.
    assert tool.main(["--lang", "es", "--out", str(out), "--check", "--set", "title=Estudio X", "--core-pin", "9.8.7"]) == 0
    assert tool.main(["--lang", "es", "--out", str(out), "--check"]) == 1


@pytest.mark.parametrize(("pin", "core"), [("0.1.0rc1", ">=0.1.0rc1,<0.2"), ("0.2.0", ">=0.2.0,<0.3")])
def test_core_pin_derives_a_core_that_it_satisfies(tool, tmp_path: Path, pin: str, core: str) -> None:  # type: ignore[no-untyped-def]
    """El render del release con una versión preliminar da un project.yaml válido (REQ-SCHEMA-83; D-66).

    Con el ``core`` de ``template_vars.toml``, ``0.1.0rc1`` no cumplía ``>=0.1.0`` y el
    project.yaml quedaba inválido (ensayo del release de 0.1.0rc1).
    """
    out = tmp_path / "out"
    assert tool.main(["--lang", "en", "--out", str(out), "--core-pin", pin]) == 0
    raw = (out / "project.yaml").read_bytes()
    cfg, findings = parse_project(raw)
    assert findings == [] and cfg is not None
    assert (cfg.core, cfg.core_pin) == (core, pin)


def test_explicit_core_wins_over_the_derived_one(tool, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    out = tmp_path / "out"
    assert tool.main(["--lang", "en", "--out", str(out), "--core-pin", "0.1.3", "--set", "core=~=0.1.3"]) == 0
    project = yaml.safe_load((out / "project.yaml").read_bytes())
    assert (project["core"], project["core_pin"]) == ("~=0.1.3", "0.1.3")


@pytest.mark.parametrize("pin", ["0.1.0-rc1", "0.1", "v0.1.0"])
def test_core_pin_outside_the_admitted_forms_is_an_error(tool, tmp_path: Path, pin: str,
                                                          capsys: pytest.CaptureFixture[str]) -> None:  # type: ignore[no-untyped-def]
    out = tmp_path / "out"
    assert tool.main(["--lang", "en", "--out", str(out), "--core-pin", pin]) == 1
    assert "--core-pin" in capsys.readouterr().err
    assert not out.exists()


def test_main_refuses_a_non_empty_output(tool, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_bytes(b"x\n")
    assert tool.main(["--lang", "en", "--out", str(out)]) == 1
    assert (out / "keep.txt").read_bytes() == b"x\n"


@pytest.mark.parametrize(
    "argv",
    [
        ["--lang", "fr", "--out", "x"],
        ["--out", "x"],
        ["--lang", "en", "--out", "x", "--set", "novalue"],
    ],
)
def test_usage_errors_exit_with_1(tool, tmp_path: Path, argv: list[str]) -> None:  # type: ignore[no-untyped-def]
    argv = [str(tmp_path / a) if a == "x" else a for a in argv]
    assert tool.main(argv) == 1


def find_reuse() -> str | None:
    exe = "reuse.exe" if sys.platform == "win32" else "reuse"
    local = REPO_ROOT / ".venv-tools" / ("Scripts" if sys.platform == "win32" else "bin") / exe
    return str(local) if local.is_file() else shutil.which("reuse")


@pytest.mark.parametrize("lang", LANGS)
def test_full_render_with_generated_files_passes_reuse_lint(rendered, lang: str) -> None:  # type: ignore[no-untyped-def]
    """REQ-TRACE-58 sobre el render completo, con CODEOWNERS y task.yml (sin red; se omite sin reuse)."""
    reuse = find_reuse()
    if reuse is None:
        pytest.skip("reuse no está instalado (.venv-tools o PATH)")
    proc = subprocess.run(
        [reuse, "--root", str(rendered[lang][0]), "lint"], capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# -- variante format: none (REQ-ARCH-100; 00-marco D-43; REQ-MVP-4) ------------------------------


@pytest.fixture(scope="module")
def rendered_none(tool, tmp_path_factory: pytest.TempPathFactory) -> dict[str, tuple[Path, list[str]]]:  # type: ignore[no-untyped-def]
    out: dict[str, tuple[Path, list[str]]] = {}
    for lang in LANGS:
        target = tmp_path_factory.mktemp(f"render-none-{lang}") / "out"
        out[lang] = (target, tool.render_to(target, lang, fmt="none"))
    return out


def none_manifest() -> list[str]:
    """Manifiesto de la variante none desde la tabla de expansión (gen_template_manifest)."""
    gen = load_tool("gen_template_manifest")
    table = tomllib.loads((REPO_ROOT / "tools" / "manifest_expand.toml").read_text(encoding="utf-8"))
    paths, _required = gen.build(table, table["milestone"], "none")
    return paths


@pytest.mark.parametrize("lang", LANGS)
def test_none_paths_equal_the_none_manifest(rendered_none, lang: str) -> None:  # type: ignore[no-untyped-def]
    root, written = rendered_none[lang]
    assert written == none_manifest() == sorted(tree(root))
    assert "sections/introduction/introduction.md" in written
    assert not any(p.startswith(("tex/", "bib/")) or p == ".latexmkrc" or p.endswith(("pending.bib", ".tex"))
                   for p in written)


@pytest.mark.parametrize("lang", LANGS)
def test_none_project_yaml_has_no_build(rendered_none, rendered, lang: str) -> None:  # type: ignore[no-untyped-def]
    """REQ-SCHEMA-90 y REQ-CONV-2: format: none, sin build, ci.pdf_retention_days ni ci.latexdiff."""
    none_text = (rendered_none[lang][0] / "project.yaml").read_text(encoding="utf-8")
    latex_text = (rendered[lang][0] / "project.yaml").read_text(encoding="utf-8")
    cfg, problems = parse_project(none_text)
    assert cfg is not None and cfg.format == "none", problems
    data = yaml.safe_load(none_text)
    assert "build" not in data and "ci" not in data  # ci solo tenía pdf_retention_days y latexdiff
    assert data["language"] == lang and data["disclosure"] == "milestones"

    def without_block(lines: list[str], key: str) -> list[str]:
        start = lines.index(f"{key}:")
        end = start + 1
        while end < len(lines) and lines[end].startswith("  "):
            end += 1
        return lines[:start] + lines[end:]

    # Todo lo demás, comentarios incluidos, queda igual y en el mismo orden.
    latex_lines = ["format: none" if line == "format: latex" else line for line in latex_text.split("\n")]
    assert none_text.split("\n") == without_block(without_block(latex_lines, "build"), "ci")


@pytest.mark.parametrize("lang", LANGS)
def test_none_render_shares_everything_else(rendered_none, rendered, lang: str) -> None:  # type: ignore[no-untyped-def]
    none, latex = tree(rendered_none[lang][0]), tree(rendered[lang][0])
    # project.yaml se reescribe y la constitución tiene su variante (REQ-CONV-91).
    for path in set(none) & set(latex) - {"project.yaml", CONSTITUTION}:
        assert none[path] == latex[path], path  # CODEOWNERS, task.yml, workflows y textos, iguales
    assert none[CONSTITUTION] != latex[CONSTITUTION]


@pytest.mark.parametrize("lang", LANGS)
def test_none_constitution_does_not_cite_the_manuscript(rendered_none, rendered, lang: str) -> None:  # type: ignore[no-untyped-def]
    """02-convencion REQ-CONV-91 y §7.1: con none, ni ``tex/``, ni ``bib/``, ni el PDF; la notación va en la sección 4."""
    text = (rendered_none[lang][0] / CONSTITUTION).read_text(encoding="utf-8")
    assert "tex/" not in text and "bib/" not in text and "pdf" not in text.lower()
    assert ".tex" not in text and "notation.tex" not in text
    truth = text.split("\n## 7.", 1)[1]
    assert {"es": "la sección 4", "en": "section 4"}[lang] in truth
    # Con latex, el texto es el de l10n/ (renderizado), con su enlace a tex/notation.tex.
    source = (REPO_ROOT / "template" / "l10n" / lang / CONSTITUTION).read_bytes()
    latex = (rendered[lang][0] / CONSTITUTION).read_bytes()
    assert latex == source and b"[tex/notation.tex](../tex/notation.tex)" in latex


def test_main_with_format_none_and_check(tool, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:  # type: ignore[no-untyped-def]
    out = tmp_path / "out"
    assert tool.main(["--lang", "es", "--out", str(out), "--format", "none"]) == 0
    assert capsys.readouterr().out.splitlines() == none_manifest()
    assert tool.main(["--lang", "es", "--out", str(out), "--check", "--format", "none"]) == 0
    assert tool.main(["--lang", "es", "--out", str(out), "--check"]) == 1  # la variante latex difiere
    assert tool.main(["--lang", "es", "--out", str(tmp_path / "x"), "--format", "quarto"]) == 1


@pytest.mark.parametrize("lang", LANGS)
def test_none_render_passes_reuse_lint(rendered_none, lang: str) -> None:  # type: ignore[no-untyped-def]
    """REQ-TRACE-58 también sin manuscrito: REUSE.toml cubre `sections/*/*.md` (sin red; se omite sin reuse)."""
    reuse = find_reuse()
    if reuse is None:
        pytest.skip("reuse no está instalado (.venv-tools o PATH)")
    proc = subprocess.run(
        [reuse, "--root", str(rendered_none[lang][0]), "lint"], capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=120, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_missing_variable_exits_with_1(tool, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    vars_file = tmp_path / "vars.toml"
    vars_file.write_text('name = "x"\n', encoding="utf-8")
    assert tool.main(["--lang", "en", "--out", str(tmp_path / "out"), "--vars", str(vars_file)]) == 1
    assert not (tmp_path / "out").exists()
