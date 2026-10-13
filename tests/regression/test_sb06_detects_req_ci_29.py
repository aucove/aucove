# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""SB06 no veía violaciones de REQ-CI-29.

En los dos workflows con ``pull_request_target`` (``validate.yml`` y ``seal.yml``), la regla
``ci-policy`` dejaba pasar:

1. un ``run:`` que ejecuta algo del checkout del PR (``bash "$GITHUB_WORKSPACE/pr/tools/prep.sh"``
   o ``cd pr && make``);
2. la condición de mismo repositorio debilitada con ``|| true``, porque solo buscaba el texto;
3. un checkout en la raíz con un ``ref:`` no literal que no nombraba ``github.event``
   (``${{ steps.meta.outputs.sha }}``).

Las variantes son las de la reproducción (``r8_sb06.py``), sobre la plantilla, más las mismas
en ``validate.yml``. Sin falsos positivos: los workflows del render ``latex`` y ``none`` y los
bloques de 09-ci (con ``<PREFIJO>SPEC_DIR``) siguen sin hallazgos.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml

from aucove import _meta
from aucove.i18n import LANGS
from tests.conftest import load_tool
from tests.unit.test_rule_ci_policy import TEMPLATE_WORKFLOWS, detail, messages, run

SEAL = ".github/workflows/seal.yml"
VALIDATE = ".github/workflows/validate.yml"
SAME_REPO = "github.event.pull_request.head.repo.full_name == github.repository"
SEAL_STEP = "      - name: Sellar la entrega (OpenTimestamps)\n"
PIN_STEP = "      - name: ¿Cambia core_pin? (determinista, con el núcleo de la base)\n"
SEAL_PR_CHECKOUT = '{ ref: "${{ github.event.pull_request.head.sha }}", path: pr, persist-credentials: false, fetch-depth: 0 }'
# Bloques YAML de 09-ci con su encabezado: compile.yml (§3.2), build.yml (§3.3), validate.yml (§4.3) y seal.yml (§7.4).
SPEC_BLOCKS: dict[str, str] = {"compile.yml": "### 3.2", "build.yml": "### 3.3", "validate.yml": "### 4.3",
                               "seal.yml": "### 7.4"}


def template(name: str) -> str:
    return (TEMPLATE_WORKFLOWS / name).read_text(encoding="utf-8")


def expected(key: str, **kw: str) -> str:
    return "CI policy: " + detail(key, **kw) + "."


def variant(text: str, old: str, new: str) -> str:
    out = text.replace(old, new)
    assert out != text, old
    return out


def step_index(text: str, job: str, name: str) -> int:
    """Índice del paso ``name`` del job (para el campo del mensaje, aunque la plantilla sume pasos)."""
    steps = yaml.safe_load(text)["jobs"][job]["steps"]
    return next(i for i, step in enumerate(steps) if step.get("name") == name)


def test_the_template_has_no_findings() -> None:
    assert run({SEAL: template("seal.yml")}) == []
    assert run({VALIDATE: template("validate.yml")}) == []


@pytest.mark.parametrize(
    ("name", "anchor", "job", "script", "shown"),
    [
        ("seal.yml", SEAL_STEP, "submission", 'bash "$GITHUB_WORKSPACE/pr/tools/prep.sh"',
         "$GITHUB_WORKSPACE/pr/tools/prep.sh"),
        ("seal.yml", SEAL_STEP, "submission", "cd pr && make", "pr"),
        ("validate.yml", PIN_STEP, "pin", "./pr/tools/prep.sh", "./pr/tools/prep.sh"),
    ],
    ids=["seal-run-script", "seal-cd-make", "validate-run-script"],
)
def test_a_run_step_that_uses_the_pr_checkout_is_reported(name: str, anchor: str, job: str, script: str,
                                                          shown: str) -> None:
    text = variant(template(name), anchor, f"      - name: x\n        run: {script}\n" + anchor)
    field = f"jobs.{job}.steps[{step_index(text, job, 'x')}].run"
    found = run({f".github/workflows/{name}": text})
    assert messages(found) == expected("prt_pr_dir", field=field, dir=shown)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (SAME_REPO, SAME_REPO + " || true", expected("prt_checkout_same_repo", job="submission")),
        (SEAL_PR_CHECKOUT, '{ ref: "${{ steps.meta.outputs.sha }}", persist-credentials: false, fetch-depth: 0 }',
         expected("prt_checkout_path", job="submission")),
    ],
    ids=["or-true", "ref-in-root"],
)
def test_seal_variants_of_the_reproduction_are_reported(old: str, new: str, message: str) -> None:
    assert messages(run({SEAL: variant(template("seal.yml"), old, new)})) == message


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (f"        if: {SAME_REPO}\n", f"        if: {SAME_REPO} || true\n", expected("prt_checkout_same_repo", job="pin")),
        ("          ref: ${{ github.event.pull_request.head.sha }}\n          path: pr\n",
         "          ref: ${{ steps.meta.outputs.sha }}\n",
         expected("prt_checkout_path", job="pin")),
    ],
    ids=["or-true", "ref-in-root"],
)
def test_the_same_variants_in_validate_yml(old: str, new: str, message: str) -> None:
    text = template("validate.yml")
    assert old in text
    changed = text.replace(old, new, 1)  # solo en el job pin, el primero
    assert messages(run({VALIDATE: changed})) == message


@pytest.mark.parametrize("fmt", ["latex", "none"])
@pytest.mark.parametrize("lang", LANGS)
def test_rendered_workflows_have_no_findings(fmt: str, lang: str) -> None:
    files = load_tool("render_template").render_files(lang, fmt=fmt)
    workflows = {p: d.decode("utf-8") for p, d in files.items() if p.startswith(".github/workflows/")}
    assert {SEAL, VALIDATE, ".github/workflows/build.yml", ".github/workflows/compile.yml"} <= set(workflows)
    assert run(workflows) == []


def test_spec_blocks_have_no_findings() -> None:
    spec_dir = os.environ.get(f"{_meta.ENV_PREFIX}SPEC_DIR")
    if not spec_dir or not (Path(spec_dir) / "09-ci.md").is_file():
        pytest.skip(f"sin {_meta.ENV_PREFIX}SPEC_DIR")
    text = (Path(spec_dir) / "09-ci.md").read_text(encoding="utf-8")
    blocks: dict[str, str] = {}
    for name, heading in SPEC_BLOCKS.items():
        section = text[text.index("\n" + heading) :]
        found = re.search(r"^```yaml\n(.*?)^```", section, re.S | re.M)
        assert found is not None, heading
        blocks[f".github/workflows/{name}"] = found.group(1)
    findings = run(blocks)
    assert findings == [], messages(findings)
