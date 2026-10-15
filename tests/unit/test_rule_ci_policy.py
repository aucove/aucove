# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Regla ``ci-policy`` (09-ci REQ-CI-4..7, REQ-CI-29, REQ-CI-45; 10-seguridad SB06, REQ-SEC-47)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

import pytest
import yaml

from aucove import _meta
from aucove.config.base import BaseSnapshot
from aucove.config.project import parse_project
from aucove.config.roles import parse_roles
from aucove.domain.findings import Finding
from aucove.i18n import Translator, translator
from aucove.ops.rules import ci_policy
from aucove.ops.rules.base import RuleInput
from aucove.vcs.tree import is_regular_mode
from tests.conftest import FIXTURES, REPO_ROOT, load_tool, roles_fixture

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
SHA = "0123456789abcdef0123456789abcdef01234567"
PROJECT = (FIXTURES / "config" / "project" / "minimal.yaml").read_text(encoding="utf-8")
CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1"
TEMPLATE_WORKFLOWS = REPO_ROOT / "template" / "base" / ".github" / "workflows"
WF = ".github/workflows/ci.yml"

GOOD = f"""\
name: ci
on:
  pull_request:
permissions: {{}}
jobs:
  test:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    permissions: {{ contents: read }}
    steps:
      - uses: {CHECKOUT}
        with: {{ persist-credentials: false }}
      - run: echo ok
"""


class MemTree:
    def __init__(self, files: Mapping[str, str | bytes], *, modes: Mapping[str, str] | None = None) -> None:
        self._data = {p: v.encode("utf-8") if isinstance(v, str) else v for p, v in files.items()}
        self._modes = {p: "100644" for p in self._data}
        self._modes.update(modes or {})
        self.root: Path | None = None
        self.sha: str | None = SHA
        self.reads: list[str] = []

    def entries(self) -> Mapping[str, str]:
        return MappingProxyType(dict(self._modes))

    def read_bytes(self, path: str, limit: int = 5 * 2**20) -> bytes | None:
        self.reads.append(path)
        data = self._data.get(path)
        return data if is_regular_mode(self._modes.get(path)) and data is not None and len(data) <= limit else None

    def read_text(self, path: str, limit: int = 5 * 2**20) -> str | None:
        data = self.read_bytes(path, limit)
        try:
            return data.decode("utf-8") if data is not None else None
        except UnicodeDecodeError:
            return None


def snapshot(core: str = ">=0.1.0,<0.2") -> BaseSnapshot:
    cfg, _ = parse_project(PROJECT.replace('core: ">=0.1.0,<0.2"', f'core: "{core}"').encode("utf-8"))
    roles, _ = parse_roles(roles_fixture("basic").encode("utf-8"))
    assert cfg is not None and roles is not None
    return BaseSnapshot(ref="origin/main", sha=SHA, project=cfg, roles=roles, decisions=frozenset(),
                        fallback_sha=None, source="local")


def make_input(files: Mapping[str, str | bytes], *, t: Translator | None = None, project: str | None = PROJECT,
               modes: Mapping[str, str] | None = None, policy: BaseSnapshot | None = None) -> RuleInput:
    all_files = dict(files)
    if project is not None:
        all_files.setdefault("project.yaml", project)
    return RuleInput(mode="pr", t=t or translator("en"), now=NOW, policy=policy or snapshot(),
                     tree=MemTree(all_files, modes=modes))


def run(files: Mapping[str, str | bytes], **kw: object) -> list[Finding]:
    out = list(ci_policy.RULE.run(make_input(files, **kw)))  # type: ignore[arg-type]
    assert all(f.check == "ci-policy" and f.code == "E_VALIDATION_CI_POLICY" and f.severity == "error" for f in out)
    return out


def messages(findings: Sequence[Finding]) -> str:
    return "\n".join(f.message for f in findings)


def detail(key: str, **kw: object) -> str:
    return translator("en")(f"validate.ci_policy.{key}", **kw)


def test_declaration() -> None:
    assert ci_policy.RULE.id == "ci-policy"
    assert ci_policy.RULE.modes == frozenset({"tree", "pr", "base"})
    assert dict(ci_policy.RULE.codes) == {"E_VALIDATION_CI_POLICY": frozenset({"detail"})}


def test_good_workflow_passes() -> None:
    assert run({WF: GOOD}) == []


def test_template_workflows_pass_their_own_rule() -> None:
    files = {f".github/workflows/{p.name}": p.read_text(encoding="utf-8") for p in TEMPLATE_WORKFLOWS.glob("*.yml")}
    assert set(files) >= {".github/workflows/build.yml", ".github/workflows/compile.yml", ".github/workflows/validate.yml"}
    assert run(files) == []


def test_uses_by_tag_is_reported_with_its_line() -> None:
    (finding,) = run({WF: GOOD.replace(CHECKOUT, "actions/checkout@v6")})
    assert detail("uses_unpinned", uses="actions/checkout@v6") in finding.message
    assert finding.path == WF and finding.line == 11


def test_sha_without_version_comment() -> None:
    (finding,) = run({WF: GOOD.replace(" # v7.0.1", "")})
    assert "has no version in a comment" in finding.message


@pytest.mark.parametrize(
    ("old", "new", "key", "kw"),
    [
        ("permissions: {}\n", "", "top_permissions_missing", {}),
        ("permissions: {}\n", "permissions: read-all\n", "top_permissions_not_empty", {}),
        ("    permissions: { contents: read }\n", "", "job_permissions", {"job": "test"}),
        ("    permissions: { contents: read }\n", "    permissions: write-all\n", "job_write_all", {"job": "test"}),
        ("    timeout-minutes: 5\n", "", "job_timeout", {"job": "test"}),
        ("        with: { persist-credentials: false }\n", "", "checkout_credentials", {"job": "test"}),
        ("{ persist-credentials: false }", "{ persist-credentials: true }", "checkout_credentials", {"job": "test"}),
        ("{ persist-credentials: false }", "{ persist-credentials: false, allow-unsafe-pr-checkout: true }",
         "checkout_unsafe", {"job": "test"}),
        ("  pull_request:\n", "  pull_request:\n  workflow_run:\n    workflows: [x]\n", "workflow_run", {}),
        ("      - run: echo ok\n", "      - uses: ./.github/actions/local\n", "uses_local",
         {"uses": "./.github/actions/local"}),
        ("      - run: echo ok\n", "      - uses: docker://alpine:3\n", "uses_docker", {"uses": "docker://alpine:3"}),
        ("    runs-on: ubuntu-24.04\n", "    runs-on: ubuntu-24.04\n    container: node:20\n", "image_unpinned",
         {"image": "node:20"}),
        ("    runs-on: ubuntu-24.04\n",
         "    runs-on: ubuntu-24.04\n    services:\n      db:\n        image: postgres:16\n", "image_unpinned",
         {"image": "postgres:16"}),
        ("jobs:\n", "jobs: {}\nx:\n", "no_jobs", {}),
    ],
)
def test_policy_violations(old: str, new: str, key: str, kw: dict[str, str]) -> None:
    text = GOOD.replace(old, new)
    assert text != GOOD
    findings = run({WF: text})
    assert "CI policy: " + detail(key, **kw) + "." in [f.message for f in findings], messages(findings)
    assert all(f.path == WF for f in findings)


def test_pinned_images_pass() -> None:
    digest = "a" * 64
    text = GOOD.replace("    runs-on: ubuntu-24.04\n",
                        f"    runs-on: ubuntu-24.04\n    container:\n      image: node@sha256:{digest}\n")
    text = text.replace("      - run: echo ok\n", f"      - uses: docker://alpine@sha256:{digest}\n")
    assert run({WF: text}) == []


@pytest.mark.parametrize(
    "expr",
    [
        "${{ github.event.pull_request.title }}",
        "${{ github.event.pull_request.body }}",
        "${{ github.head_ref }}",
        "${{ github.event.pull_request.head.ref }}",
        "${{ github.event.issue.title }}",
        "${{ github.event.comment.body }}",
        "${{ toJSON(github.event) }}",
    ],
)
def test_untrusted_interpolation_in_run(expr: str) -> None:
    (finding,) = run({WF: GOOD.replace("run: echo ok", f'run: echo "{expr}"')})
    assert expr in finding.message and finding.line == 13


def test_untrusted_interpolation_in_github_script_and_safe_uses() -> None:
    script = GOOD.replace(
        "      - run: echo ok\n",
        "      - uses: actions/github-script@60a0d83039c74a4aee543508d2ffcb1c3799cdea # v7.0.1\n"
        "        with:\n          script: console.log('${{ github.event.pull_request.title }}')\n",
    )
    assert len(run({WF: script})) == 1
    safe = GOOD.replace("run: echo ok", 'run: echo "$TITLE ${{ github.event.pull_request.number }}"')
    assert run({WF: safe}) == []


def test_secrets_are_forbidden() -> None:
    text = GOOD.replace("      - run: echo ok\n", "      - run: echo ok\n        env: { T: \"${{ secrets.TOKEN }}\" }\n")
    (finding,) = run({WF: text})
    assert detail("secrets", secret=f"{_meta.ENV_PREFIX}EVIDENCE_KEY") in finding.message
    inherit = GOOD + "  call:\n    uses: ./.github/workflows/ci.yml\n    permissions: {}\n    secrets: inherit\n"
    assert detail("secrets_inherit", job="call") in messages(run({WF: inherit}))
    token = GOOD.replace("      - run: echo ok\n", "      - run: echo ok\n        env: { T: \"${{ github.token }}\" }\n")
    assert run({WF: token}) == []


SEAL = ".github/workflows/seal.yml"
KEY_VAR = f"{_meta.ENV_PREFIX}EVIDENCE_KEY"
KEY_ENV = f"{_meta.NAME}-evidence"
SIGNING = (
    "name: seal\n"
    "on:\n"
    "  push:\n"
    "    branches: [main]\n"
    "permissions: {}\n"
    "jobs:\n"
    "  integration:\n"
    "    runs-on: ubuntu-slim\n"
    "    timeout-minutes: 10\n"
    f"    environment: {{ name: {KEY_ENV}, deployment: false }}\n"
    "    permissions: { contents: read }\n"
    "    steps:\n"
    "      - name: Sellar\n"
    "        env:\n"
    f"          {KEY_VAR}: ${{{{ secrets.{KEY_VAR} }}}}\n"
    f"        run: {_meta.NAME} ci seal --kind integration\n"
)


def test_the_project_key_only_in_the_seal_step_of_integration() -> None:
    """SB10 (10-seguridad; 09-ci REQ-CI-61; 00-marco D-72): la llave del proyecto solo la recibe el
    paso de ``ci seal`` del job integration de seal.yml, con su environment."""
    assert run({SEAL: SIGNING}) == []
    # en otro workflow, el environment y el secreto son hallazgos
    other = messages(run({WF: SIGNING}))
    assert detail("evidence_environment", job="integration", environment=KEY_ENV) in other
    assert detail("secrets", secret=KEY_VAR) in other
    # otro job de seal.yml no puede declarar el environment
    renamed = SIGNING.replace("  integration:\n", "  submission:\n")
    assert detail("evidence_environment", job="submission", environment=KEY_ENV) in messages(run({SEAL: renamed}))
    # el secreto en otro paso, en el env del job o con otro nombre de variable
    elsewhere = SIGNING.replace(f"        run: {_meta.NAME} ci seal --kind integration\n", "        run: echo hola\n")
    assert detail("secrets", secret=KEY_VAR) in messages(run({SEAL: elsewhere}))
    renamed_var = SIGNING.replace(f"          {KEY_VAR}: ", "          OTRA: ")
    assert detail("secrets", secret=KEY_VAR) in messages(run({SEAL: renamed_var}))
    whole = SIGNING.replace(f"${{{{ secrets.{KEY_VAR} }}}}", "${{ toJSON(secrets) }}")
    assert detail("secrets", secret=KEY_VAR) in messages(run({SEAL: whole}))
    # un environment con expresión podría resolver al de la llave
    dynamic = SIGNING.replace(f"name: {KEY_ENV}", 'name: "${{ github.event_name }}"')
    assert detail("environment_expression", job="integration") in messages(run({SEAL: dynamic}))


def test_pull_request_target_only_in_validate_yml_and_seal_yml() -> None:
    """SB06 (10-seguridad, D-45): ``validate.yml``, el sello de la entrega en ``seal.yml`` y, desde
    H3, la aprobación del editor en ``editor-review.yml`` (09-ci §4.5)."""
    prt = GOOD.replace("  pull_request:\n", "  pull_request_target:\n")
    (finding,) = run({WF: prt})
    assert detail("pull_request_target") in finding.message
    assert "seal.yml" in detail("pull_request_target") and "editor-review.yml" in detail("pull_request_target")
    assert run({".github/workflows/validate.yml": prt}) == []
    assert run({".github/workflows/seal.yml": prt}) == []
    assert run({".github/workflows/editor-review.yml": prt}) == []
    assert ci_policy.PRT_WORKFLOWS == frozenset(
        {".github/workflows/validate.yml", ".github/workflows/seal.yml", ".github/workflows/editor-review.yml"})
    # Otro nombre, otra extensión u otra grafía son otros workflows para GitHub.
    for other in (".github/workflows/Seal.yml", ".github/workflows/seal.yaml", ".github/workflows/seal-2.yml"):
        assert [f.path for f in run({other: prt})] == [other], other


WORKFLOW_RUN = GOOD.replace("  pull_request:\n", "  workflow_run:\n    workflows: [editor-review-trigger]\n")
EDITOR_REVIEW = ".github/workflows/editor-review.yml"


def test_workflow_run_only_in_editor_review_yml() -> None:
    """SB06 desde H3: ``workflow_run`` solo en ``editor-review.yml`` (09-ci REQ-CI-60)."""
    assert ci_policy.WORKFLOW_RUN_WORKFLOWS == frozenset({EDITOR_REVIEW})
    assert run({EDITOR_REVIEW: WORKFLOW_RUN}) == []
    for other in (WF, ".github/workflows/validate.yml", ".github/workflows/seal.yml",
                  ".github/workflows/editor-review-trigger.yml", ".github/workflows/Editor-Review.yml"):
        (finding,) = run({other: WORKFLOW_RUN})
        assert detail("workflow_run") in finding.message, other


def test_workflow_run_checkouts_follow_req_ci_29() -> None:
    """Con ``workflow_run`` el job tiene token de escritura: un checkout del PR se revisa como
    con ``pull_request_target`` (y en ese evento la condición de mismo repositorio nunca vale)."""
    pr_checkout = WORKFLOW_RUN.replace(
        "        with: { persist-credentials: false }\n",
        "        with: { persist-credentials: false, ref: '${{ github.event.workflow_run.head_sha }}' }\n")
    keys = {f.message for f in run({EDITOR_REVIEW: pr_checkout})}
    assert keys == {"CI policy: " + detail("prt_checkout_path", job="test") + ".",
                    "CI policy: " + detail("prt_checkout_same_repo", job="test") + "."}


@pytest.mark.parametrize("fmt", ["latex", "none"])
def test_the_rendered_template_passes(fmt: str) -> None:
    """Los workflows del render (con los del editor solo en ``latex``) cumplen la regla."""
    files = load_tool("render_template").render_files("es", fmt=fmt)
    workflows = {rel: data.decode("utf-8") for rel, data in files.items() if rel.startswith(".github/workflows/")}
    assert (EDITOR_REVIEW in workflows) is (fmt == "latex")
    assert run(workflows) == []


def test_pull_request_target_found_when_yaml_reads_on_as_true() -> None:
    """Un cargador YAML 1.1 lee ``on`` como ``True``: la regla no pierde los disparadores."""
    prt = GOOD.replace("  pull_request:\n", "  pull_request_target:\n")
    doc = yaml.safe_load(prt)
    assert True in doc and "on" not in doc
    inp = make_input({WF: prt})
    findings = ci_policy.check_document(inp, WF, doc, inp.tree.entries(), raw_lines=prt.split("\n"))  # type: ignore[union-attr]
    assert [f.message for f in findings] == ["CI policy: " + detail("pull_request_target") + "."]
    assert ci_policy.check_document(inp, ".github/workflows/validate.yml", doc, inp.tree.entries(),  # type: ignore[union-attr]
                                    raw_lines=prt.split("\n")) == []


PRT = f"""\
name: validate
on:
  pull_request_target:
permissions: {{}}
jobs:
  validate:
    runs-on: ubuntu-24.04
    timeout-minutes: 5
    permissions: {{ contents: read }}
    steps:
      - name: PR
        if: github.event.pull_request.head.repo.full_name == github.repository
        uses: {CHECKOUT}
        with:
          ref: ${{{{ github.event.pull_request.head.sha }}}}
          path: pr
          persist-credentials: false
      - run: echo ok
        working-directory: ${{{{ runner.temp }}}}
"""
VALIDATE_WF = ".github/workflows/validate.yml"


@pytest.mark.parametrize("path", [VALIDATE_WF, ".github/workflows/seal.yml"])
def test_pull_request_target_checkout_rules(path: str) -> None:
    """REQ-CI-29 vale igual en los dos workflows que admiten ``pull_request_target``."""
    assert run({path: PRT}) == []
    no_path = PRT.replace("          path: pr\n", "")
    assert detail("prt_checkout_path", job="validate") in messages(run({path: no_path}))
    any_repo = PRT.replace("        if: github.event.pull_request.head.repo.full_name == github.repository\n", "")
    assert detail("prt_checkout_same_repo", job="validate") in messages(run({path: any_repo}))
    inside = PRT.replace("${{ runner.temp }}", "pr/tex")
    assert "works inside the checkout of the PR" in messages(run({path: inside}))


def test_seal_submission_job_with_the_same_repo_condition_at_job_level() -> None:
    """09-ci §7.4: la condición de mismo repositorio va en el ``if:`` del job, no del paso."""
    seal = PRT.replace(
        "    runs-on: ubuntu-24.04\n",
        "    if: github.event_name == 'pull_request_target'"
        " && github.event.pull_request.head.repo.full_name == github.repository\n    runs-on: ubuntu-24.04\n",
    ).replace("        if: github.event.pull_request.head.repo.full_name == github.repository\n", "")
    assert run({".github/workflows/seal.yml": seal}) == []
    assert run({".github/workflows/seal.yml": seal.replace(" && github.event.pull_request.head.repo.full_name"
                                                           " == github.repository", "")}) != []


# -- REQ-CI-29 endurecido: condición, ref no literal y usos del directorio del PR ------------------

SAME_REPO = "github.event.pull_request.head.repo.full_name == github.repository"
SAME_REPO_IF = f"        if: {SAME_REPO}\n"
PR_REF = "          ref: ${{ github.event.pull_request.head.sha }}\n"
RUN_STEP = "      - run: echo ok\n"
DOCKER_BUILD = "docker/build-push-action@" + "a" * 40 + " # v6.0.0"


def with_run(script: str) -> str:
    """``PRT`` con ``script`` como cuerpo del paso ``run`` (el paso 1 del job ``validate``)."""
    body = "".join(f"          {line}\n" for line in script.split("\n"))
    return PRT.replace("      - run: echo ok\n", "      - run: |\n" + body)


@pytest.mark.parametrize(
    "condition",
    [
        SAME_REPO,
        "github.repository == github.event.pull_request.head.repo.full_name",
        "${{ " + SAME_REPO + " }}",
        "always() && " + SAME_REPO,
        "github.event_name != 'push' && (" + SAME_REPO + ")",
        "(github.event_name == 'pull_request_target' && (" + SAME_REPO + "))",
    ],
)
def test_same_repo_condition_joined_with_and_passes(condition: str) -> None:
    assert run({VALIDATE_WF: PRT.replace(SAME_REPO_IF, f"        if: {condition}\n")}) == []


@pytest.mark.parametrize(
    "condition",
    [
        SAME_REPO + " || true",  # la condición deja de obligar
        "always() || " + SAME_REPO,
        SAME_REPO + " && !cancelled()",
        '"!(github.event.pull_request.head.repo.full_name != github.repository)"',
        "github.event.pull_request.head.repo.full_name != github.repository",
        "(" + SAME_REPO + ") == true",
        "format('{0}', github.event_name == 'x' && (" + SAME_REPO + "))",  # una llamada no se abre
        "github.event_name != '" + SAME_REPO + "'",  # dentro de una cadena
        "${{ " + SAME_REPO + " }} && true",  # texto y expresión: una cadena, siempre verdadera
    ],
)
def test_weakened_same_repo_condition_is_reported(condition: str) -> None:
    findings = run({VALIDATE_WF: PRT.replace(SAME_REPO_IF, f"        if: {condition}\n")})
    assert messages(findings) == "CI policy: " + detail("prt_checkout_same_repo", job="validate") + "."


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("ref", "${{ steps.meta.outputs.sha }}"),  # el sha llega por otra salida
        ("ref", "${{ github.event.pull_request.base.sha }}"),  # la base la elige el autor del PR
        ("ref", "refs/pull/7/head"),
        ("repository", "${{ github.event.pull_request.head.repo.full_name }}"),
    ],
)
def test_a_non_literal_ref_is_content_of_the_pr(key: str, value: str) -> None:
    own_dir = PRT.replace(PR_REF, f"          {key}: {value}\n")
    assert run({VALIDATE_WF: own_dir}) == []  # en su carpeta y solo del mismo repositorio
    in_root = own_dir.replace("          path: pr\n", "")
    assert messages(run({VALIDATE_WF: in_root})) == "CI policy: " + detail("prt_checkout_path", job="validate") + "."


def test_a_literal_ref_is_not_content_of_the_pr() -> None:
    text = PRT.replace(PR_REF, "          ref: main\n").replace("          path: pr\n", "").replace(SAME_REPO_IF, "")
    assert run({VALIDATE_WF: text}) == []


@pytest.mark.parametrize("path", ["${{ env.PR_DIR }}", "pr/..", "./", "../outside", "${{ github.workspace }}"])
def test_the_pr_checkout_needs_a_literal_folder_inside_the_workspace(path: str) -> None:
    findings = run({VALIDATE_WF: PRT.replace("          path: pr\n", f'          path: "{path}"\n')})
    assert messages(findings) == "CI policy: " + detail("prt_checkout_path", job="validate") + "."
    workspace = PRT.replace("          path: pr\n", "          path: ${{ github.workspace }}/pr\n")
    assert run({VALIDATE_WF: workspace}) == []


@pytest.mark.parametrize(
    ("script", "shown"),
    [
        ('bash "$GITHUB_WORKSPACE/pr/tools/prep.sh"', "$GITHUB_WORKSPACE/pr/tools/prep.sh"),
        ("cd pr && make", "pr"),
        ("make -C pr", "pr"),
        ("./pr/run.sh", "./pr/run.sh"),
        ("source pr/env.sh", "pr/env.sh"),
        ('"${GITHUB_WORKSPACE}"/pr/x.sh', '${GITHUB_WORKSPACE}"/pr/x.sh'),
        ("${{ github.workspace }}/pr/x.sh", "${{ github.workspace }}/pr/x.sh"),
        ("cd base/../pr", "pr"),
        ('echo "$GITHUB_WORKSPACE/pr/bin" >> "$GITHUB_PATH"', "$GITHUB_WORKSPACE/pr/bin"),
        ("python3 pr/check.py --tree pr", "pr/check.py"),
        ("aucove validate --tree pr\nbash \\\n  pr/x.sh", "pr/x.sh"),
        ("gh pr comment 1", "pr"),  # conservadora: la palabra suelta cuenta, salvo tras --mode
    ],
)
def test_a_run_that_uses_the_pr_checkout_is_reported(script: str, shown: str) -> None:
    findings = run({VALIDATE_WF: with_run(script)})
    expected = detail("prt_pr_dir", field="jobs.validate.steps[1].run", dir=shown)
    assert [f.message for f in findings] == [f"CI policy: {expected}."]
    assert findings[0].line == 18  # la línea del run:


@pytest.mark.parametrize(
    "script",
    [
        # Las líneas de 09-ci §4.3 y §7.4: el directorio del PR solo como dato.
        "aucove validate --mode tree --check schemas --tree pr --report pin.json || true",
        'TREE=(); [ -d "$GITHUB_WORKSPACE/pr" ] && TREE=(--tree "$GITHUB_WORKSPACE/pr")',
        'aucove validate --mode pr --pr "$PR_NUMBER" "${TREE[@]}"',
        'aucove ci seal --kind submission --repo-dir "$GITHUB_WORKSPACE/pr" --pr "$PR_NUMBER"',
        "aucove validate --check schemas \\\n  --tree=pr",
        "test -d pr && [[ ! -e pr/x ]] && echo ok",
        # El mismo nombre en otra ruta, otra palabra u otra variable no es el checkout.
        'cat "$RUNNER_TEMP/pr/x" pr.json prep.sh pr-1 "$PR_NUMBER" "${PR_DIR}"',
    ],
)
def test_the_pr_checkout_as_data_passes(script: str) -> None:
    assert run({VALIDATE_WF: with_run(script)}) == []


@pytest.mark.parametrize(
    ("old", "new", "field", "shown"),
    [
        # BASH_ENV: bash lo ejecuta al empezar cada paso run:.
        (RUN_STEP, RUN_STEP + '        env: { BASH_ENV: "$GITHUB_WORKSPACE/pr/env.sh" }\n',
         "jobs.validate.steps[1].env.BASH_ENV", "$GITHUB_WORKSPACE/pr/env.sh"),
        (RUN_STEP, RUN_STEP + "        shell: bash pr/wrap.sh {0}\n", "jobs.validate.steps[1].shell", "pr/wrap.sh"),
        (RUN_STEP, f"      - uses: {DOCKER_BUILD}\n        with: {{ context: pr, push: false }}\n" + RUN_STEP,
         "jobs.validate.steps[1].with.context", "pr"),
        ("    steps:\n", "    env: { NODE_OPTIONS: --require ./pr/hook.js }\n    steps:\n",
         "jobs.validate.env.NODE_OPTIONS", "./pr/hook.js"),
        ("    steps:\n", '    defaults: { run: { shell: "bash ./pr/sh {0}" } }\n    steps:\n',
         "jobs.validate.defaults.run.shell", "./pr/sh"),
        ("jobs:\n", "env: { PYTHONPATH: pr }\njobs:\n", "env.PYTHONPATH", "pr"),
        ("jobs:\n", 'defaults: { run: { shell: "bash ./pr/sh {0}" } }\njobs:\n', "defaults.run.shell", "./pr/sh"),
    ],
)
def test_env_shell_and_action_inputs_that_use_the_pr_checkout(old: str, new: str, field: str, shown: str) -> None:
    text = PRT.replace(old, new, 1)
    assert text != PRT
    expected = detail("prt_pr_dir", field=field, dir=shown)
    assert [f.message for f in run({VALIDATE_WF: text})] == [f"CI policy: {expected}."]


def test_working_directory_inside_the_pr_checkout_also_by_default() -> None:
    no_dir = PRT.replace("        working-directory: ${{ runner.temp }}\n", "")
    for old, new in (("jobs:\n", "defaults: { run: { working-directory: ./pr/tex } }\njobs:\n"),
                     ("    steps:\n", "    defaults: { run: { working-directory: pr } }\n    steps:\n")):
        findings = run({VALIDATE_WF: no_dir.replace(old, new, 1)})
        assert [f.line for f in findings] == [19], messages(findings)  # el paso run:, no el checkout
        assert "works inside the checkout of the PR" in messages(findings)
    # Un paso con su propio working-directory no hereda el de defaults.
    assert run({VALIDATE_WF: PRT.replace("jobs:\n", "defaults: { run: { working-directory: pr } }\njobs:\n")}) == []


def test_without_a_pr_checkout_the_folder_name_is_free() -> None:
    """Las menciones solo cuentan en un job con checkout del PR (y solo con pull_request_target)."""
    no_checkout = with_run("cd pr && make").replace(PR_REF, "")
    assert run({VALIDATE_WF: no_checkout}) == []
    assert run({WF: GOOD.replace("run: echo ok", "run: cd pr && make")}) == []


def test_reusable_workflows() -> None:
    caller = GOOD + "  compile:\n    uses: ./.github/workflows/compile.yml\n    permissions: { contents: read }\n"
    findings = run({WF: caller})
    assert detail("reusable_missing", uses="./.github/workflows/compile.yml") in messages(findings)
    compile_wf = GOOD.replace("  pull_request:\n", "  workflow_call:\n")
    assert run({WF: caller, ".github/workflows/compile.yml": compile_wf}) == []
    remote = GOOD + "  other:\n    uses: org/repo/.github/workflows/x.yml@main\n    permissions: {}\n"
    assert "is not pinned to a 40-character SHA" in messages(run({WF: remote}))


def test_unreadable_or_non_regular_workflows() -> None:
    assert detail("yaml", problem="x").split("(")[0] in messages(run({WF: "on: [\n"}))
    findings = run({WF: GOOD}, modes={WF: "120000"})
    assert [f.message for f in findings] == ["CI policy: " + detail("not_regular") + "."]
    assert detail("unreadable") in messages(run({WF: b"\xff\xfe"}))
    assert detail("not_mapping") in messages(run({WF: "- a\n"}))


def test_symlinked_workflow_is_not_read() -> None:
    inp = make_input({WF: GOOD}, modes={WF: "120000"})
    list(ci_policy.RULE.run(inp))
    assert WF not in inp.tree.reads  # type: ignore[union-attr]


def test_only_first_level_yaml_files_are_workflows() -> None:
    bad = GOOD.replace(CHECKOUT, "actions/checkout@v6")
    assert run({".github/workflows/sub/x.yml": bad, ".github/workflows/notes.md": "x", ".github/other.yml": bad}) == []
    assert len(run({".github/workflows/x.YAML": bad})) == 1


def test_core_pin_of_the_tree_must_satisfy_core_of_the_base() -> None:
    assert run({}) == []
    outside = PROJECT.replace('core_pin: "0.1.0"', 'core_pin: "0.2.0"')
    (finding,) = run({}, project=outside)
    assert finding.path == "project.yaml" and finding.line == 4
    assert detail("core_pin_core", pin="0.2.0", core=">=0.1.0,<0.2") in finding.message
    # la base manda: con otro core en la base, el mismo pin pasa
    assert run({}, project=outside, policy=snapshot(">=0.1.0,<0.3")) == []
    (finding,) = run({}, project=PROJECT.replace('core_pin: "0.1.0"', "core_pin: latest"))
    assert detail("core_pin_line") in finding.message


def test_messages_are_localized(t_es: Translator) -> None:
    (finding,) = run({WF: GOOD.replace(CHECKOUT, "actions/checkout@v6")}, t=t_es)
    assert finding.message.startswith("Política de CI: ")


def test_no_tree_fails_closed() -> None:
    inp = RuleInput(mode="pr", t=translator("en"), now=NOW, policy=snapshot())
    (finding,) = ci_policy.RULE.run(inp)
    assert finding.code == "E_INTERNAL_UNEXPECTED"
