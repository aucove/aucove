# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""La imagen que ejecuta REUSE queda fijada por digest.

``fsfe/reuse-action@676e2d5`` (v6.0.0) es una acción Docker (``runs.using: docker`` e
``image: Dockerfile``), y su Dockerfile empieza con ``FROM fsfe/reuse:6``, una etiqueta
mutable de Docker Hub. Fijar el SHA de la acción no fijaba la imagen que ejecutaban los
tres pasos del job ``reuse`` (10-seguridad REQ-SEC-47; 11-trazabilidad REQ-TRACE-58 y
REQ-TRACE-59). Además, ``tools/verify_action_pins.py`` solo miraba las ``image:`` y
``docker://`` de los workflows, así que no lo detectaba.

Sin red: GitHub y el registro se simulan.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests.conftest import REPO_ROOT, load_tool
from tests.unit.test_framework_workflows import CI, FakeLsRemote, job, load_doc, real_tags, run_lines, steps

RAW = "https://raw.githubusercontent.com/"
REUSE_ACTION = "fsfe/reuse-action"
REUSE_ACTION_SHA = "676e2d560c9a403aa252096d99fcab3e1132b0f5"
# Copia de lo publicado en ese commit (action.yml y el principio del Dockerfile).
REUSE_ACTION_YML = (
    "name: 'REUSE Compliance Check'\n"
    "runs:\n"
    "  using: 'docker'\n"
    "  image: 'Dockerfile'\n"
    "branding:\n"
    "  icon: 'check-circle'\n"
)
REUSE_DOCKERFILE = (
    "# comentario\n"
    "\n"
    "FROM fsfe/reuse:6\n"
    "\n"
    "COPY reuse-action-wrapper.sh /bin/reuse-action-wrapper\n"
    'ENTRYPOINT ["/bin/reuse-action-wrapper"]\n'
)
NODE_ACTION_YML = "runs:\n  using: node24\n  main: dist/index.js\n"
DIGEST = "sha256:" + "5" * 64
TEX_DIGEST = "sha256:" + "6" * 64
_DIGEST_PINNED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./_:-]*@sha256:[0-9a-f]{64}$")


@pytest.fixture(scope="module")
def pins() -> ModuleType:
    return load_tool("verify_action_pins")


class FakeNet:
    """GitHub raw (``action.yml`` y Dockerfile de un commit) y un registro v2 sin token.

    ``files``: ruta tras ``raw.githubusercontent.com/`` -> texto. Con ``node_default``, todo
    ``action.yml`` que no esté en ``files`` es el de una acción de JavaScript.
    """

    def __init__(self, files: Mapping[str, str], digests: set[str], *, node_default: bool = False) -> None:
        self.files = dict(files)
        self.digests = set(digests)
        self.node_default = node_default
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method: str, url: str, headers: Mapping[str, str]) -> Any:
        pins = load_tool("verify_action_pins")
        self.calls.append((method, url))
        if url.startswith(RAW):
            assert method == "GET"
            path = url[len(RAW):]
            text = self.files.get(path)
            if text is None and self.node_default and path.endswith("/action.yml"):
                text = NODE_ACTION_YML
            if text is None:
                return pins.HttpResponse(404, {})
            return pins.HttpResponse(200, {}, text.encode("utf-8"))
        assert method == "HEAD" and "/v2/" in url and "/manifests/" in url
        reference = url.rsplit("/manifests/", 1)[1]
        if reference in self.digests:
            return pins.HttpResponse(200, {"docker-content-digest": reference})
        return pins.HttpResponse(404, {})


def tree(tmp_path: Path, uses: str, *, tex_image: bool = False) -> Path:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text(
        f"jobs:\n  reuse:\n    steps:\n      - uses: {uses}\n", encoding="utf-8", newline="\n"
    )
    if tex_image:
        base = tmp_path / "template" / "base"
        base.mkdir(parents=True)
        (base / "project.yaml").write_text(
            f"build:\n  image: registry.example.invalid/tex@{TEX_DIGEST}\n", encoding="utf-8", newline="\n"
        )
    return tmp_path


def reuse_files(dockerfile: str = REUSE_DOCKERFILE, action_yml: str = REUSE_ACTION_YML) -> dict[str, str]:
    return {
        f"{REUSE_ACTION}/{REUSE_ACTION_SHA}/action.yml": action_yml,
        f"{REUSE_ACTION}/{REUSE_ACTION_SHA}/Dockerfile": dockerfile,
    }


def reuse_ls() -> FakeLsRemote:
    return FakeLsRemote({(REUSE_ACTION, "v6.0.0"): (REUSE_ACTION_SHA, None)})


# ---------------------------------------------------------------------------
# verify_action_pins ve la imagen de las acciones Docker
# ---------------------------------------------------------------------------


def test_a_docker_action_built_from_a_mutable_tag_fails(tmp_path: Path, pins: ModuleType) -> None:
    root = tree(tmp_path, f"{REUSE_ACTION}@{REUSE_ACTION_SHA} # v6.0.0")
    code, text = pins.run(root, ls_remote=reuse_ls(), http=FakeNet(reuse_files(), set()), skip_images=True)
    assert code == 1, text
    assert "FROM fsfe/reuse:6" in text and "@sha256:" in text, text
    assert f"{REUSE_ACTION}@{REUSE_ACTION_SHA[:12]}" in text


def test_a_docker_action_whose_base_is_pinned_passes_and_its_image_is_verified(
    tmp_path: Path, pins: ModuleType
) -> None:
    root = tree(tmp_path, f"{REUSE_ACTION}@{REUSE_ACTION_SHA} # v6.0.0", tex_image=True)
    files = reuse_files(dockerfile=f"FROM fsfe/reuse:6@{DIGEST}\nENTRYPOINT [\"reuse\"]\n")
    net = FakeNet(files, {DIGEST, TEX_DIGEST})
    code, text = pins.run(root, ls_remote=reuse_ls(), http=net)
    assert code == 0, text
    # el digest de la base de la acción se consulta en Docker Hub, como el de la imagen TeX
    assert ("HEAD", f"https://registry-1.docker.io/v2/fsfe/reuse/manifests/{DIGEST}") in net.calls
    # y si no existe, falla
    code, text = pins.run(root, ls_remote=reuse_ls(), http=FakeNet(files, {TEX_DIGEST}))
    assert code == 1 and "el digest no existe en el registro" in text, text


@pytest.mark.parametrize(
    ("action_yml", "dockerfile", "needle"),
    [
        ("runs:\n  using: docker\n  image: docker://alpine:3\n", None, "docker://alpine:3"),
        ("runs:\n  using: \"docker\"\n  image: Dockerfile\n", "ARG BASE=alpine:3\nFROM ${BASE}\n", "FROM ${BASE}"),
        ("runs:\n  using: docker\n  image: Dockerfile\n", None, "Dockerfile"),
        ("name: x\n", None, "runs.using"),
    ],
)
def test_docker_actions_without_a_pinned_image_fail(
    tmp_path: Path, pins: ModuleType, action_yml: str, dockerfile: str | None, needle: str
) -> None:
    files = {f"{REUSE_ACTION}/{REUSE_ACTION_SHA}/action.yml": action_yml}
    if dockerfile is not None:
        files[f"{REUSE_ACTION}/{REUSE_ACTION_SHA}/Dockerfile"] = dockerfile
    root = tree(tmp_path, f"{REUSE_ACTION}@{REUSE_ACTION_SHA} # v6.0.0")
    code, text = pins.run(root, ls_remote=reuse_ls(), http=FakeNet(files, set()), skip_images=True)
    assert code == 1 and needle in text, text


@pytest.mark.parametrize(
    "dockerfile",
    [
        f"FROM --platform=linux/amd64 golang@{DIGEST} AS build\nRUN true\nFROM build\n",
        f"FROM scratch\nCOPY x /x\nFROM alpine:3.20@{DIGEST}\n",
    ],
)
def test_multi_stage_and_scratch_bases(tmp_path: Path, pins: ModuleType, dockerfile: str) -> None:
    root = tree(tmp_path, f"{REUSE_ACTION}@{REUSE_ACTION_SHA} # v6.0.0")
    net = FakeNet(reuse_files(dockerfile=dockerfile), set())
    code, text = pins.run(root, ls_remote=reuse_ls(), http=net, skip_images=True)
    assert code == 0, text


def test_missing_action_metadata_fails_closed(tmp_path: Path, pins: ModuleType) -> None:
    root = tree(tmp_path, f"{REUSE_ACTION}@{REUSE_ACTION_SHA} # v6.0.0")
    net = FakeNet({}, set())
    code, text = pins.run(root, ls_remote=reuse_ls(), http=net, skip_images=True)
    assert code == 1 and "action.yml" in text, text
    # se buscan action.yml y action.yaml, en el commit fijado
    urls = [u for _, u in net.calls]
    assert f"{RAW}{REUSE_ACTION}/{REUSE_ACTION_SHA}/action.yml" in urls
    assert f"{RAW}{REUSE_ACTION}/{REUSE_ACTION_SHA}/action.yaml" in urls


def test_action_in_a_subdirectory_is_read_from_its_path(tmp_path: Path, pins: ModuleType) -> None:
    sha = "1" * 40
    root = tree(tmp_path, f"o/r/sub/dir@{sha} # v1")
    files = {f"o/r/{sha}/sub/dir/action.yml": "runs:\n  using: composite\n  steps: []\n"}
    code, text = pins.run(root, ls_remote=FakeLsRemote({("o/r", "v1"): (sha, None)}), http=FakeNet(files, set()),
                          skip_images=True)
    assert code == 0, text


# ---------------------------------------------------------------------------
# ci.yml corre REUSE desde una imagen fijada por digest
# ---------------------------------------------------------------------------


def reuse_image() -> str:
    env = job(load_doc(CI), "reuse").get("env") or {}
    value = env.get("REUSE_IMAGE")
    assert isinstance(value, str), "el job reuse declara REUSE_IMAGE"
    return value


def test_ci_does_not_use_the_docker_action_with_a_mutable_base() -> None:
    text = CI.read_text(encoding="utf-8")
    assert f"uses: {REUSE_ACTION}@" not in text


def test_reuse_image_is_pinned_by_digest_with_its_version() -> None:
    image = reuse_image()
    assert _DIGEST_PINNED.match(image), image
    # REQ-TRACE-59: herramienta externa con versión fijada, visible en la etiqueta
    assert re.match(r"^fsfe/reuse:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}$", image), image


def test_reuse_job_runs_lint_three_times_from_the_pinned_image() -> None:
    ci = load_doc(CI)
    all_steps = steps(ci, "reuse")
    runs: list[tuple[int, list[str]]] = []
    for i, step in enumerate(all_steps):
        assert "uses" not in step or not str(step["uses"]).startswith("docker://"), step
        for line in run_lines(step):
            if line.startswith("docker run "):
                runs.append((i, shlex.split(line)))
    assert len(runs) == 3, runs
    args = []
    for _, argv in runs:
        assert "$REUSE_IMAGE" in argv, argv
        k = argv.index("$REUSE_IMAGE")
        args.append(argv[k + 1:])
        assert "--rm" in argv and "--network" in argv and argv[argv.index("--network") + 1] == "none"
        # mismo dueño que el checkout: git no rechaza el repo por «dubious ownership»
        assert argv[argv.index("--user") + 1] == "$(id -u):$(id -g)"
        assert argv[argv.index("-v") + 1] == "$GITHUB_WORKSPACE:/data" and argv[argv.index("-w") + 1] == "/data"
    assert args == [["lint"], ["--root", "_render/en", "lint"], ["--root", "_render/es", "lint"]]
    # la guarda del digest y el pull van antes del primer docker run
    guard_i = next(i for i, s in enumerate(all_steps) if 'docker pull "$REUSE_IMAGE"' in str(s.get("run", "")))
    guard = all_steps[guard_i]["run"]
    assert "@sha256:[0-9a-f]{64}$" in guard and guard.index("exit 1") < guard.index("docker pull")
    assert guard_i < runs[0][0]
    render_i = next(i for i, s in enumerate(all_steps) if "tools/render_template.py" in str(s.get("run", "")))
    assert runs[0][0] < render_i < runs[1][0]


def test_verify_action_pins_collects_and_checks_the_reuse_image(pins: ModuleType) -> None:
    image = reuse_image()
    _, images = pins.collect(REPO_ROOT)
    found = [i for i in images if i.where.file == ".github/workflows/ci.yml"]
    assert [i.raw for i in found] == [image]
    [ref] = found
    assert (ref.registry, ref.repository) == ("registry-1.docker.io", "fsfe/reuse")
    assert pins.image_form_error(ref) is None


def test_whole_repo_verifies_with_the_real_metadata_of_the_reuse_action(pins: ModuleType) -> None:
    """Con la acción de REUSE tal como es (Docker con FROM mutable), el repo pasa: ya no la usa."""
    _, images = pins.collect(REPO_ROOT)
    files = reuse_files()
    net = FakeNet(files, {i.digest for i in images if i.digest}, node_default=True)
    code, text = pins.run(REPO_ROOT, ls_remote=FakeLsRemote(real_tags(pins)), http=net)
    assert code == 0, text


def test_an_unpinned_image_variable_is_a_form_error(tmp_path: Path, pins: ModuleType,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "a.yml").write_text(
        "jobs:\n  x:\n    env:\n      REUSE_IMAGE: fsfe/reuse:6\n    steps:\n"
        "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n",
        encoding="utf-8", newline="\n",
    )
    assert pins.main(["--list", "--root", str(tmp_path)]) == 1
    assert "fsfe/reuse:6 no está fijada por @sha256:" in capsys.readouterr().err
