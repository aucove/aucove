# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``tools/release.py``: los pasos de ``release.yml`` que no son de uv (01-arquitectura §2.3)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from aucove import _meta
from tests.conftest import load_tool

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def rel():  # type: ignore[no-untyped-def]
    return load_tool("release")


def test_package_version_is_the_one_hatch_reads(rel) -> None:  # type: ignore[no-untyped-def]
    assert rel.package_version() == _meta.__version__
    assert rel.project_name() == _meta.NAME


@pytest.mark.parametrize(("version", "pre"), [
    ("0.1.0", False), ("0.1.0rc1", True), ("1.2.3a4", True), ("1.2.3b1", True), ("10.0.12", False),
])
def test_check_tag_accepts_the_version_and_says_if_it_is_a_prerelease(rel, version: str, pre: bool) -> None:  # type: ignore[no-untyped-def]
    assert rel.check_tag(f"v{version}", version) == (version, pre)


@pytest.mark.parametrize(("tag", "version"), [
    ("0.1.0rc1", "0.1.0rc1"),        # sin la v
    ("v0.1.0", "0.1.0rc1"),          # otra versión
    ("v0.1.0rc1", "0.1.0-rc1"),      # forma fuera de D-66
    ("v0.1.0.post1", "0.1.0.post1"),
])
def test_check_tag_rejects(rel, tag: str, version: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(rel.ReleaseError):
        rel.check_tag(tag, version)


def _sbom(name: str = "aucove", **component: object) -> dict[str, object]:
    root = {"type": "library", "bom-ref": f"{name}-1", "name": name,
            "properties": [{"name": "uv:package:is_project_root", "value": "true"}], **component}
    return {"bomFormat": "CycloneDX", "specVersion": "1.5", "metadata": {"component": root}, "components": []}


def test_sbom_version_fills_the_root_component(rel) -> None:  # type: ignore[no-untyped-def]
    doc = rel.set_sbom_version(_sbom(), "aucove", "0.1.0rc1")
    component = doc["metadata"]["component"]
    assert list(component) == ["type", "bom-ref", "name", "version", "purl", "properties"]
    assert component["version"] == "0.1.0rc1" and component["purl"] == "pkg:pypi/aucove@0.1.0rc1"
    # idempotente
    again = rel.set_sbom_version(json.loads(json.dumps(doc)), "aucove", "0.1.0rc1")
    assert again == doc


def test_sbom_version_refuses_another_package_or_version(rel) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(rel.ReleaseError):
        rel.set_sbom_version(_sbom("otro"), "aucove", "0.1.0")
    with pytest.raises(rel.ReleaseError):
        rel.set_sbom_version(_sbom(version="0.0.9"), "aucove", "0.1.0")
    with pytest.raises(rel.ReleaseError):
        rel.set_sbom_version({"metadata": {}}, "aucove", "0.1.0")


def test_sbom_version_command_rewrites_the_file(rel, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "sbom.cdx.json"
    path.write_text(json.dumps(_sbom()), encoding="utf-8")
    assert rel.main(["sbom-version", "--file", str(path), "--version", _meta.__version__]) == 0
    data = path.read_text(encoding="utf-8")
    assert data.endswith("}\n") and json.loads(data)["metadata"]["component"]["version"] == _meta.__version__
    assert rel.main(["sbom-version", "--file", str(path), "--version", "no-es-version"]) == 1


CHANGELOG = """# Changelog

## [Unreleased]

### Added

- Algo nuevo.

## [0.1.0rc1] - 2026-10-20

First public version.

### Added

- Core package.

## [0.0.1] - 2026-01-01

- Viejo.
"""


def test_notes_take_the_section_of_the_version(rel) -> None:  # type: ignore[no-untyped-def]
    notes = rel.release_notes(CHANGELOG, name="aucove", version="0.1.0rc1", repo="aucove/aucove", previous="v0.0.1")
    assert notes.startswith("First public version.\n\n### Added\n\n- Core package.\n")
    assert "Algo nuevo" not in notes and "Viejo" not in notes
    assert "pypi-attestations verify pypi --repository https://github.com/aucove/aucove pypi:aucove-0.1.0rc1-py3-none-any.whl" in notes
    assert "gh attestation verify aucove-0.1.0rc1-py3-none-any.whl -R aucove/aucove" in notes
    assert "aucove-template/compare" not in notes  # una preliminar no publica la plantilla (REQ-ARCH-8)


def test_notes_of_a_final_version_link_the_template_changes(rel) -> None:  # type: ignore[no-untyped-def]
    text = CHANGELOG.replace("[0.1.0rc1]", "[0.1.0]")
    notes = rel.release_notes(text, name="aucove", version="0.1.0", repo="aucove/aucove", previous="v0.0.1")
    assert notes.rstrip().endswith("https://github.com/aucove/aucove-template/compare/v0.0.1...v0.1.0")


def test_notes_need_the_section(rel) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(rel.ReleaseError, match="no tiene la sección"):
        rel.changelog_section(CHANGELOG, "0.2.0")
    with pytest.raises(rel.ReleaseError, match="vacía"):
        rel.changelog_section("## [0.3.0]\n\n## [0.2.0]\n- x\n", "0.3.0")


FENCE = "`" * 3  # un bloque de código, sin escribir las tres comillas invertidas dentro de la cadena
README = f"""<p align="center">
  <img src="docs/assets/logo.svg" alt="" width="104">
  <a href="LICENSE"><img alt="license" src="https://img.shields.io/badge/license-Apache--2.0-blue"></a>
  <a href="spec/">spec</a> · <a href="#english">English</a>
</p>

See [the guide](docs/en/README.md), [a section](docs/en/lead.md#setup), [Issues](https://github.com/o/r/issues),
![a chart](docs/assets/chart.png) and [us](mailto:team@example.org).

{FENCE}bash
echo "[not a link](docs/en/README.md)"
{FENCE}
"""
BLOB = "https://github.com/aucove/aucove/blob/v0.1.0/"
RAW = "https://raw.githubusercontent.com/aucove/aucove/v0.1.0/"


def test_pypi_readme_makes_relative_paths_absolute_at_the_tag(rel) -> None:  # type: ignore[no-untyped-def]
    out = rel.pypi_readme(README, repo="aucove/aucove", ref="v0.1.0")
    assert f'<img src="{RAW}docs/assets/logo.svg"' in out
    assert f'<a href="{BLOB}LICENSE">' in out
    assert '<a href="https://github.com/aucove/aucove/tree/v0.1.0/spec/">' in out  # una carpeta
    assert '<a href="#english">' in out  # las anclas quedan igual
    assert f"[the guide]({BLOB}docs/en/README.md)" in out
    assert f"[a section]({BLOB}docs/en/lead.md#setup)" in out
    assert "[Issues](https://github.com/o/r/issues)" in out and "[us](mailto:team@example.org)" in out
    assert f"![a chart]({RAW}docs/assets/chart.png)" in out
    assert 'echo "[not a link](docs/en/README.md)"' in out  # dentro de un bloque de código
    assert rel.pypi_readme(out, repo="aucove/aucove", ref="v0.1.0") == out  # aplicarla dos veces da lo mismo


def test_pypi_readme_of_the_real_readme_points_to_files_that_exist(rel) -> None:  # type: ignore[no-untyped-def]
    ref = f"v{_meta.__version__}"
    out = rel.pypi_readme((REPO_ROOT / "README.md").read_text(encoding="utf-8"), repo="aucove/aucove", ref=ref)
    urls = [a or b for a, b in re.findall(r'(?:src|href)="([^"]+)"|\]\(([^)\s]+)\)', out)]
    assert [u for u in urls if not u.startswith(("https://", "mailto:", "#"))] == []
    assert f"https://raw.githubusercontent.com/aucove/aucove/{ref}/docs/assets/logo.svg" in urls
    ours = [u.split(f"/{ref}/", 1)[1].partition("#")[0] for u in urls if f"aucove/aucove/{ref}/" in u
            or f"aucove/aucove/blob/{ref}/" in u or f"aucove/aucove/tree/{ref}/" in u]
    assert len(ours) >= 20
    assert [p for p in ours if not (REPO_ROOT / p).exists()] == []  # cada ruta existe en el árbol de la etiqueta


def test_pypi_readme_command_rewrites_the_file(rel, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "README.md"
    path.write_text('<img src="docs/assets/logo.svg">\n', encoding="utf-8")
    assert rel.main(["pypi-readme", "--file", str(path), "--repo", "aucove/aucove", "--ref", "v0.1.0"]) == 0
    assert path.read_text(encoding="utf-8") == f'<img src="{RAW}docs/assets/logo.svg">\n'
    assert rel.main(["pypi-readme", "--file", str(path), "--repo", "no es un repo", "--ref", "v0.1.0"]) == 1


def test_check_tag_command_prints_the_outputs(rel, capsys: pytest.CaptureFixture[str]) -> None:  # type: ignore[no-untyped-def]
    assert rel.main(["check-tag", "--tag", f"v{_meta.__version__}"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [f"version={_meta.__version__}", f"prerelease={'true' if rel.is_prerelease(_meta.__version__) else 'false'}"]
    assert rel.main(["check-tag", "--tag", "v9.9.9"]) == 1
