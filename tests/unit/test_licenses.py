# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Licencias del proyecto: ``domain.licenses`` y los datos que trae el paquete (11-trazabilidad
§7.2 y §7.3; 02-convencion S-8; 00-marco D-64)."""

from __future__ import annotations

import json
import tomllib

import pytest

from aucove import _meta
from aucove.config.datafiles import license_text, spdx_licenses, spdx_version
from aucove.domain import licenses as dl
from tests.conftest import REPO_ROOT

DATA = REPO_ROOT / "src" / _meta.NAME / "_data"
SPDX = "SPDX" + "-"  # por concatenación: reuse no la lee como etiqueta de este archivo
COMMENTS = {key: key for key in ("head", "text", "code", "template", "data", "generated")}
LIC = dl.LicenseSet('Lab "X" «UNAM»', "CC-BY-SA-4.0", "GPL-3.0-or-later", "ODbL-1.0")


# -- datos empaquetados ---------------------------------------------------------------------


def test_packaged_texts_are_the_table_of_11_7_3() -> None:
    table = {x for options in dl.OPTIONS.values() for x in options}
    assert set(dl.PACKAGED) == table - {dl.ALL_RIGHTS_RESERVED} and len(dl.PACKAGED) == 9
    files = sorted(p.name for p in (DATA / "LICENSES").iterdir())
    assert files == sorted(f"{x}.txt" for x in dl.PACKAGED)
    for license_id in dl.PACKAGED:
        data = license_text(license_id)
        assert data and data == (DATA / "LICENSES" / f"{license_id}.txt").read_bytes()
    assert license_text("LGPL-3.0-or-later") is None and license_text("../REUSE") is None


@pytest.mark.parametrize("license_id", ["Apache-2.0", "CC-BY-4.0", "CC0-1.0", "MIT-0"])
def test_packaged_texts_match_the_framework_ones(license_id: str) -> None:
    """Los que también usa el framework (y la plantilla) son el mismo archivo."""
    ours = (DATA / "LICENSES" / f"{license_id}.txt").read_bytes()
    assert ours == (REPO_ROOT / "LICENSES" / f"{license_id}.txt").read_bytes()
    template = REPO_ROOT / "template" / "base" / "LICENSES" / f"{license_id}.txt"
    if template.is_file():
        assert ours == template.read_bytes()


def test_the_spdx_list_is_a_current_copy() -> None:
    """REQ-TRACE-62: la copia de la lista de SPDX (CC0) que usa init para aceptar identificadores."""
    raw = json.loads((DATA / "spdx-licenses.json").read_text(encoding="utf-8"))
    assert raw["licenseListVersion"] == spdx_version() == "3.27.0"
    ids = spdx_licenses()
    assert len(ids) == len(raw["licenses"]) > 600
    assert all(ids.get(x) is False for x in dl.PACKAGED)  # vigentes
    assert ids["GPL-3.0"] is True and ids["GPL-3.0-or-later"] is False
    assert list(raw["licenses"]) == sorted(raw["licenses"])


# -- categorías de 02-convencion S-8 -------------------------------------------------------


@pytest.mark.parametrize(("rel", "expected"), [
    ("README.md", "text"),
    ("plan.md", "text"),
    ("sections/methods/methods.tex", "text"),
    ("sections/methods/notes/n.md", "text"),
    ("data/README.md", "text"),
    (".github/workflows/build.yml", "code"),
    (".github/CODEOWNERS", "code"),
    (".mcp.json", "code"),
    ("tex/main.tex", "code"),
    ("tex/outline/methods.tex", "text"),
    ("sections/methods/analysis/fit.py", "code"),
    ("sections/methods/analysis/derived/out.csv", "data"),
    ("bib/references.bib", "data"),
    ("sections/methods/pending.bib", "data"),
    ("sections/methods/tables/t1.csv", "data"),
    ("sections/methods/tables/t1.tex", "text"),
    ("sections/methods/tables/x/t1.csv", "text"),
    (".claude/skills/start/SKILL.md", "generated"),
    (".agents/skills/start/SKILL.md", "generated"),
    (".claude/settings.json", "code"),
])
def test_category(rel: str, expected: str) -> None:
    assert dl.category(rel) == expected


@pytest.mark.parametrize(("rel", "expected"), [
    ("README.md", True),
    ("LICENSE", False),
    ("LICENSE.md", False),
    ("docs/LICENCE-MIT", False),
    ("COPYING", False),
    ("LICENSES/MIT.txt", False),
    ("vendor/LICENSES/x.txt", False),
    ("REUSE.toml", False),
    ("fig.png.license", False),
    ("sbom.spdx.json", False),
    ("a/.git/config", False),
    ("LICENSES.md", True),
])
def test_covered(rel: str, expected: bool) -> None:
    assert dl.covered(rel) is expected


def test_in_use_skips_empty_and_ignored_files() -> None:
    files = {"README.md": b"x", "sections/m/tables/.gitkeep": b"", "LICENSES/CC0-1.0.txt": b"x",
             ".github/workflows/a.yml": b"on: push", "REUSE.toml": b"x"}
    found = dl.in_use(files, LIC, lambda rel: bool(files[rel]))
    assert found == {"CC-BY-SA-4.0": ["text"], "GPL-3.0-or-later": ["code"]}
    files.update({"bib/references.bib": b"@x", ".claude/skills/s/SKILL.md": b"---"})
    assert dl.in_use(files, LIC, lambda rel: bool(files[rel])) == {
        "CC-BY-SA-4.0": ["text"], "GPL-3.0-or-later": ["code"], "ODbL-1.0": ["data"], "MIT-0": ["generated"]}
    same = dl.LicenseSet("Ana", "CC0-1.0", "MIT", "CC0-1.0")
    assert dl.in_use(files, same, lambda rel: bool(files[rel]))["CC0-1.0"] == ["text", "data"]


def test_suggestions_and_open_licenses() -> None:
    assert [dl.suggested(c, "public") for c in dl.CATEGORIES] == ["CC-BY-4.0", "MIT", "CC0-1.0"]
    for visibility in ("private", "internal"):
        assert [dl.suggested(c, visibility) for c in dl.CATEGORIES] == [
            dl.ALL_RIGHTS_RESERVED, "MIT", dl.ALL_RIGHTS_RESERVED]
    assert dl.is_open("CC-BY-4.0") and not dl.is_open(dl.ALL_RIGHTS_RESERVED)


# -- REUSE.toml de init (REQ-TRACE-61) ------------------------------------------------------


def test_reuse_toml_tables_in_order() -> None:
    text = dl.reuse_toml(LIC, year=2027, template_code=["tex/main.tex", ".github/workflows/build.yml"],
                         comments=COMMENTS)
    assert text.startswith("# head\nversion = 1\n") and text.endswith("\n")
    data = tomllib.loads(text)
    rows = [(a["path"], a[SPDX + "FileCopyrightText"], a[SPDX + "License-Identifier"]) for a in data["annotations"]]
    owner = '2027 Lab "X" «UNAM»'
    assert rows == [
        (["**"], owner, "CC-BY-SA-4.0"),
        (list(dl.CODE_PATHS), owner, "GPL-3.0-or-later"),
        ([".github/workflows/build.yml", "tex/main.tex"], [owner, dl.TEMPLATE_HOLDER], "GPL-3.0-or-later"),
        (list(dl.DATA_PATHS), owner, "ODbL-1.0"),
        (list(dl.GENERATED_PATHS), dl.TEMPLATE_HOLDER, "MIT-0"),
    ]
    assert dl.TEMPLATE_HOLDER == f"2026 The {_meta.NAME} Authors"
    without = tomllib.loads(dl.reuse_toml(LIC, year=2027, template_code=[], comments=COMMENTS))
    assert len(without["annotations"]) == 4


def test_the_category_order_is_the_reuse_toml_order() -> None:
    """``category`` aplica las tablas del ``REUSE.toml`` en orden: gana la última que coincide."""
    tables = [("text", ["**"]), ("code", dl.CODE_PATHS), ("data", dl.DATA_PATHS), ("generated", dl.GENERATED_PATHS)]
    for rel in ("sections/a/analysis/derived/x.csv", ".claude/skills/s/x", "bib/x.bib", ".claude/x", "x.md"):
        expected = "text"
        for name, patterns in tables:
            if any(_glob(p, rel) for p in patterns):
                expected = name
        assert dl.category(rel) == expected, rel


def _glob(pattern: str, rel: str) -> bool:
    import re

    out = "".join(".*" if part == "**" else "[^/]*" if part == "*" else re.escape(part)
                  for part in re.split(r"(\*\*|\*)", pattern))
    return re.fullmatch(out, rel) is not None
