# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""release.yml y nightly.yml del repositorio del framework.

Fuentes: 01-arquitectura §2.3 y REQ-ARCH-7 a REQ-ARCH-11; 10-seguridad REQ-SEC-40 a
REQ-SEC-44, REQ-SEC-47 y REQ-SEC-83; 08-forja REQ-FORGE-64; 12-mvp REQ-MVP-22 y REQ-MVP-28;
00-marco D-39, D-66 y D-69. La regla ci-policy de la plantilla ya corre sobre estos archivos
en test_framework_workflows; aquí van las reglas propias del release.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from aucove.config.yamlload import load_yaml
from tests.conftest import REPO_ROOT

WORKFLOWS = REPO_ROOT / ".github" / "workflows"
RELEASE = WORKFLOWS / "release.yml"
NIGHTLY = WORKFLOWS / "nightly.yml"

_USES_LINE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<target>\S+)(?:\s+#\s*(?P<comment>\S+))?\s*$")
_SHA_PIN = re.compile(r"^(?P<action>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@(?P<sha>[0-9a-f]{40})$")
# 00-marco D-39: las de GitHub y estas dos, que la política de Actions permite por patrón.
THIRD_PARTY_ALLOWED = frozenset({"astral-sh/setup-uv", "pypa/gh-action-pypi-publish"})
# 12-mvp REQ-MVP-22: los únicos jobs con secretos, cada uno en su environment protegido.
SECRET_JOBS: dict[Path, dict[str, str]] = {
    RELEASE: {"template": "template-publish"},
    NIGHTLY: {"live": "forge-tests"},
}
# El único job que hereda secretos (secrets: inherit): el que llama a nightly.yml desde release.yml,
# porque un job con uses: no puede declarar environment y sin heredar no le llegan los de forge-tests.
INHERITS_SECRETS: dict[Path, dict[str, str]] = {RELEASE: {"live": "./.github/workflows/nightly.yml"}}


def load(path: Path) -> dict[Any, Any]:
    doc, findings = load_yaml(path.read_text(encoding="utf-8"), file=path.name)
    assert findings == [], [f.message for f in findings]
    assert isinstance(doc, dict)
    return doc


@pytest.fixture(scope="module")
def release() -> dict[Any, Any]:
    return load(RELEASE)


@pytest.fixture(scope="module")
def nightly() -> dict[Any, Any]:
    return load(NIGHTLY)


def jobs(doc: Mapping[Any, Any]) -> Mapping[str, Mapping[str, Any]]:
    value = doc["jobs"]
    assert isinstance(value, Mapping)
    return value


def run_text(job: Mapping[str, Any]) -> str:
    return "\n".join(str(s.get("run", "")) for s in job.get("steps") or [])


def strings(node: object) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, Mapping):
        for value in node.values():
            yield from strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from strings(value)


def needs(job: Mapping[str, Any]) -> set[str]:
    value = job.get("needs", [])
    return {value} if isinstance(value, str) else set(value)


# -- comunes -----------------------------------------------------------------------------------


@pytest.mark.parametrize("path", [RELEASE, NIGHTLY], ids=lambda p: p.name)
def test_every_uses_is_pinned_with_its_version_and_allowed(path: Path) -> None:
    found = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _USES_LINE.match(line)
        if m is None or m.group("target").startswith("./"):
            continue
        found += 1
        pin = _SHA_PIN.match(m.group("target"))
        assert pin is not None, line
        assert m.group("comment") is not None and re.fullmatch(r"v\d+(\.\d+)*", m.group("comment")), line
        action = pin.group("action")
        assert action.startswith("actions/") or action in THIRD_PARTY_ALLOWED, f"D-39: {action}"
    assert found >= 4


def test_one_pin_per_action_across_framework_workflows() -> None:
    by_action: dict[str, set[str]] = {}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for line in path.read_text(encoding="utf-8").splitlines():
            m = _USES_LINE.match(line)
            if m is not None and not m.group("target").startswith("./"):
                action, _, sha = m.group("target").partition("@")
                by_action.setdefault(action, set()).add(f"{sha} {m.group('comment')}")
    assert by_action and all(len(v) == 1 for v in by_action.values()), by_action


@pytest.mark.parametrize("path", [RELEASE, NIGHTLY], ids=lambda p: p.name)
def test_permissions_timeouts_and_checkouts(path: Path) -> None:
    doc = load(path)
    assert doc["permissions"] == {}
    assert doc["defaults"] == {"run": {"shell": "bash"}}
    for name, job in jobs(doc).items():
        assert "permissions" in job, name
        if "uses" not in job:
            assert isinstance(job.get("timeout-minutes"), int) and 1 <= job["timeout-minutes"] <= 60, name
        for step in job.get("steps") or []:
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False, name


@pytest.mark.parametrize("path", [RELEASE, NIGHTLY], ids=lambda p: p.name)
def test_secrets_only_in_protected_environment_jobs_and_never_on_prs(path: Path) -> None:
    """REQ-MVP-22: credenciales solo por etiqueta, calendario o a mano, en environments protegidos."""
    doc = load(path)
    on = doc.get("on", doc.get(True))
    assert not {"pull_request", "pull_request_target", "workflow_run"} & set(on)
    allowed = SECRET_JOBS[path]
    for name, job in jobs(doc).items():
        uses_secrets = any("secrets." in s for s in strings(job))
        if name in allowed:
            assert uses_secrets and job["environment"] == allowed[name], name
        else:
            assert not uses_secrets, name
        inherited = INHERITS_SECRETS.get(path, {}).get(name)
        if inherited is None:
            assert "secrets" not in job, name  # nada de secrets: inherit
        else:
            assert job.get("uses") == inherited and job.get("secrets") == "inherit", name
    top = {k: v for k, v in doc.items() if k != "jobs"}
    assert not any("secrets." in s for s in strings(top))


# -- release.yml --------------------------------------------------------------------------------


def test_release_runs_only_on_version_tags(release: Mapping[Any, Any]) -> None:
    on = release.get("on", release.get(True))
    assert on == {"push": {"tags": ["v*"]}}


def test_release_jobs_and_their_order(release: Mapping[Any, Any]) -> None:
    j = jobs(release)
    assert set(j) == {"ci", "live", "build", "pypi", "template", "gh-release"}
    assert j["ci"]["uses"] == "./.github/workflows/ci.yml"  # la matriz completa (REQ-ARCH-6)
    assert j["live"]["uses"] == "./.github/workflows/nightly.yml"  # REQ-FORGE-64: antes de cada versión
    assert needs(j["build"]) == {"ci", "live"}
    assert needs(j["pypi"]) == {"build"}
    assert needs(j["template"]) == {"build", "pypi"}  # REQ-ARCH-7: después de PyPI
    assert needs(j["gh-release"]) == {"build", "pypi", "template"}


def test_only_the_pypi_job_gets_an_oidc_token(release: Mapping[Any, Any]) -> None:
    """REQ-SEC-40 y REQ-SEC-41: Trusted Publishing desde el environment pypi."""
    for name, job in jobs(release).items():
        perms = job["permissions"]
        if name == "pypi":
            assert perms == {"contents": "read", "id-token": "write", "attestations": "write"}
            assert job["environment"]["name"] == "pypi"
        else:
            assert "id-token" not in perms and "attestations" not in perms, name
    assert jobs(release)["gh-release"]["permissions"] == {"contents": "write"}


def test_pypi_job_attests_provenance_and_sbom_and_keeps_pep740(release: Mapping[Any, Any]) -> None:
    steps = jobs(release)["pypi"]["steps"]
    attest = [s for s in steps if str(s.get("uses", "")).startswith("actions/attest@")]
    assert [s["with"] for s in attest] == [
        {"subject-path": "dist/*"},
        {"subject-path": "dist/*", "sbom-path": "assets/sbom.cdx.json"},
    ]
    [publish] = [s for s in steps if str(s.get("uses", "")).startswith("pypa/gh-action-pypi-publish@")]
    assert publish["with"] == {"packages-dir": "dist/"}  # sin attestations: false (REQ-SEC-42)
    assert steps.index(publish) > steps.index(attest[-1])


def test_build_job_checks_the_tag_and_builds_with_hashed_constraints(release: Mapping[Any, Any]) -> None:
    job = jobs(release)["build"]
    text = run_text(job)
    assert 'python3 tools/release.py check-tag --tag "$TAG" >> "$GITHUB_OUTPUT"' in text
    tag_step = next(s for s in job["steps"] if s.get("id") == "version")
    assert tag_step["env"] == {"TAG": "${{ github.ref_name }}"}  # por env, nunca interpolado en run
    assert "uv lock --check" in text
    readme = 'python3 tools/release.py pypi-readme --repo "$REPO" --ref "$TAG"'
    assert text.index(readme) < text.index("uv build ")  # la descripción de PyPI, antes del build
    readme_step = next(s for s in job["steps"] if "pypi-readme" in str(s.get("run", "")))
    assert readme_step["env"] == {"REPO": "${{ github.repository }}", "TAG": "${{ github.ref_name }}"}
    assert "uv export --quiet --frozen --only-group dev --no-emit-project" in text
    assert 'uv build --build-constraints "$RUNNER_TEMP/uv-build-constraints.txt" --require-hashes' in text
    assert ("--no-dev --no-emit-project --extra evidence --format requirements.txt "
            "--output-file assets/requirements-lock.txt") in text
    assert "--format cyclonedx1.5 --output-file assets/sbom.cdx.json" in text
    assert 'python3 tools/release.py sbom-version --file assets/sbom.cdx.json --version "$VERSION"' in text
    assert 'tools/render_template.py --lang en --core-pin "$VERSION" --out build/template' in text
    assert job["outputs"] == {"version": "${{ steps.version.outputs.version }}",
                              "prerelease": "${{ steps.version.outputs.prerelease }}"}
    uploads = [s["with"]["name"] for s in job["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
    assert uploads == ["dist", "assets", "template"]


def test_template_job_only_for_final_versions(release: Mapping[Any, Any]) -> None:
    """REQ-ARCH-8 a REQ-ARCH-10 y REQ-SEC-83."""
    job = jobs(release)["template"]
    assert job["if"] == "needs.build.outputs.prerelease == 'false'"
    assert job["environment"] == "template-publish"
    [app] = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/create-github-app-token@")]
    assert app["with"]["repositories"] == "aucove-template"
    assert app["with"]["permission-contents"] == "write" and app["with"]["permission-workflows"] == "write"
    text = run_text(job)
    assert text.index("--jq .is_template") < text.index("git -C published push")  # REQ-ARCH-9 antes de escribir
    assert 'git -C published commit --quiet -s -m "aucove v${VERSION}"' in text
    assert 'git -C published tag -a "v${VERSION}" -m "aucove v${VERSION}"' in text
    assert 'git -C published push --atomic origin HEAD:refs/heads/main "refs/tags/v${VERSION}"' in text
    assert 'git -C published ls-remote --tags origin "v${VERSION}"' in text


def test_gh_release_uses_the_changelog_and_attaches_the_assets(release: Mapping[Any, Any]) -> None:
    job = jobs(release)["gh-release"]
    assert "needs.pypi.result == 'success'" in job["if"] and "needs.template.result == 'skipped'" in job["if"]
    text = run_text(job)
    assert 'python3 tools/release.py notes --version "$VERSION" --repo "$REPO"' in text
    assert "gh release create" in text and "--verify-tag" in text and "flags+=(--prerelease)" in text
    assert "assets/requirements-lock.txt assets/sbom.cdx.json" in text


# -- nightly.yml --------------------------------------------------------------------------------


def test_nightly_triggers(nightly: Mapping[Any, Any]) -> None:
    on = nightly.get("on", nightly.get(True))
    assert set(on) == {"schedule", "workflow_dispatch", "workflow_call"}
    [cron] = on["schedule"]
    assert re.fullmatch(r"\d+ \d+ \* \* \*", cron["cron"])  # cada noche (REQ-FORGE-64)


def test_live_job_runs_the_contract_suite_against_both_sandboxes(nightly: Mapping[Any, Any]) -> None:
    # De a una corrida sobre los sandbox, también la que llama release.yml con otra ref.
    assert nightly["concurrency"] == {"group": "nightly", "cancel-in-progress": False}
    job = jobs(nightly)["live"]
    assert job["environment"] == "forge-tests"
    assert job["strategy"]["matrix"] == {"sandbox": ["public", "private"]}
    # Un sandbox después del otro, y los dos aunque el primero falle: las mismas cuentas, la misma cuota.
    assert job["strategy"]["max-parallel"] == 1 and job["strategy"]["fail-fast"] is False
    assert "vars.CONTRACT_REPO_PUBLIC" in job["env"]["AUCOVE_CONTRACT_REPO"]
    assert "vars.CONTRACT_REPO_PRIVATE" in job["env"]["AUCOVE_CONTRACT_REPO"]
    test_step = job["steps"][-1]
    assert test_step["run"].startswith("uv run --no-sync pytest -m live")
    assert set(test_step["env"]) == {
        "TEST_GH_TOKEN_LEAD", "TEST_GH_TOKEN_RESEARCHER_A", "TEST_GH_TOKEN_RESEARCHER_B",
        "TEST_GH_TOKEN_READ_ONLY", "TEST_GH_TOKEN_NO_PERMISSION", "TEST_GH_TOKEN_TEAM_LEAD",
    }


def test_template_drift_renders_the_last_stable_tag(nightly: Mapping[Any, Any]) -> None:
    """REQ-ARCH-11; sin versión final, solo un aviso (REQ-ARCH-8)."""
    job = jobs(nightly)["template-drift"]
    # Desde release.yml (el push de la etiqueta) la plantilla de esa versión aún no se publicó.
    assert job["if"] == "github.event_name != 'push'"
    text = run_text(job)
    assert "grep -E '^v[0-9]+\\.[0-9]+\\.[0-9]+$'" in text  # sin preliminares
    assert "::notice::" in text
    assert ('tools/render_template.py --lang en --core-pin "${TAG#v}" --out "$RUNNER_TEMP/published" --check'
            in text)
    assert "github.com/${OWNER}/aucove-template.git" in text


@pytest.mark.parametrize("path", [RELEASE, NIGHTLY], ids=lambda p: p.name)
def test_run_scripts_never_change_directory(path: Path) -> None:
    """``git -C`` o ``working-directory`` en vez de ``cd`` (shellcheck SC2164, que actionlint corre en el CI)."""
    data = load(path)
    for name, job in jobs(data).items():
        assert not re.search(r"(^|[;&|(]\s*|\s)cd\s", run_text(job), re.MULTILINE), name


@pytest.mark.parametrize("path", [RELEASE, NIGHTLY], ids=lambda p: p.name)
def test_files_are_utf8_lf_without_bom(path: Path) -> None:
    data = path.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf") and b"\r" not in data and data.endswith(b"\n")
    data.decode("utf-8")
