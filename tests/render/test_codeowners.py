# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``.github/CODEOWNERS`` generado (06-permisos §4.6; 00-marco D-35 j y m)."""

from __future__ import annotations

import inspect
import os
import re
from pathlib import Path

import pytest

from aucove import _meta
from aucove.render.codeowners import CODEOWNERS_PATH, render_codeowners
from aucove.render.generated import GENERATED_PATHS, generated_files
from aucove.render.template import RenderError

SPEC_ENV = _meta.ENV_PREFIX + "SPEC_DIR"

# Bloque de ejemplo de 06-permisos §4.6, byte a byte (tres espacios tras '*').
EXAMPLE = (
    f"# Generado por `{_meta.NAME} sync-roles` desde roles.yaml. No editar a mano.\n"
    "# source: roles.yaml sha256:3f9a1c0b7e2d\n"
    "*   @ana @bruno\n"
    "# section introduction: owner @ana\n"
    "# section methods: owner @carla\n"
    "# section results: owner @bruno\n"
).encode("utf-8")

SECTIONS = [("introduction", "ana"), ("methods", "carla"), ("results", "bruno")]


def example() -> bytes:
    return render_codeowners(lead="ana", deputy="bruno", sections=SECTIONS, source_sha12="3f9a1c0b7e2d")


def test_reproduces_the_example_of_06_4_6() -> None:
    assert example() == EXAMPLE


def test_matches_the_spec_block_when_available() -> None:
    spec_dir = os.environ.get(SPEC_ENV)
    if not spec_dir:
        pytest.skip(f"{SPEC_ENV} no está definido")
    text = (Path(spec_dir) / "06-permisos.md").read_bytes().replace(b"\r\n", b"\n")
    match = re.search(rb"### 4\.6 .*?```text\n(.*?)```", text, re.S)
    assert match is not None, "no se encuentra el bloque de ejemplo de 06 §4.6"
    assert example() == match.group(1)


def test_single_effective_line_and_no_language_parameter() -> None:
    effective = [line for line in example().decode("utf-8").splitlines() if not line.startswith("#")]
    assert effective == ["*   @ana @bruno"]
    assert "lang" not in inspect.signature(render_codeowners).parameters


def test_without_deputy_only_the_lead_owns_everything() -> None:
    out = render_codeowners(lead="ana", deputy=None, sections=SECTIONS, source_sha12="3f9a1c0b7e2d")
    lines = out.decode("utf-8").splitlines()
    assert lines[2] == "*   @ana"
    assert [line for line in lines if not line.startswith("#")] == ["*   @ana"]


def test_sections_are_sorted_by_id_and_keep_the_login_spelling() -> None:
    out = render_codeowners(
        lead="Ana", deputy="Bruno-Diaz", sections=[("results", "Bruno-Diaz"), ("abstract", "Ana")],
        source_sha12="0123456789ab",
    )
    lines = out.decode("utf-8").splitlines()
    assert lines[2:] == ["*   @Ana @Bruno-Diaz", "# section abstract: owner @Ana", "# section results: owner @Bruno-Diaz"]


def test_no_sections_keeps_header_and_effective_line() -> None:
    out = render_codeowners(lead="ana", deputy=None, sections=[], source_sha12="3f9a1c0b7e2d")
    assert out.decode("utf-8").splitlines()[1:] == ["# source: roles.yaml sha256:3f9a1c0b7e2d", "*   @ana"]


def test_deterministic_utf8_lf_and_final_newline() -> None:
    first, second = example(), example()
    assert first == second
    assert not first.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in first
    assert first.endswith(b"\n") and not first.endswith(b"\n\n")
    first.decode("utf-8")


def test_header_has_no_generator_version_or_timestamp() -> None:
    header = example().decode("utf-8").splitlines()[:2]
    assert _meta.__version__ not in "\n".join(header)
    assert "generator:" not in "\n".join(header)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source_sha12": "3F9A1C0B7E2D"},
        {"source_sha12": "3f9a1c0b7e2"},
        {"lead": "ana bruno"},
        {"lead": "-ana"},
        {"deputy": "@bruno"},
        {"deputy": "ANA"},
        {"sections": [("none", "ana")]},
        {"sections": [("Methods", "ana")]},
        {"sections": [("methods", "carla"), ("methods", "ana")]},
        {"sections": [("methods", "carla\n* @mallory")]},
        {"sections": [("methods",)]},
    ],
)
def test_rejects_unsafe_input(kwargs: dict[str, object]) -> None:
    args: dict[str, object] = {"lead": "ana", "deputy": "bruno", "sections": SECTIONS, "source_sha12": "3f9a1c0b7e2d"}
    args.update(kwargs)
    with pytest.raises(RenderError):
        render_codeowners(**args)  # type: ignore[arg-type]


def test_generated_files_codeowners_is_the_same_in_es_and_en() -> None:
    sections = [("introduction", "Introducción", "ana"), ("methods", "Métodos", "carla"), ("results", "Resultados", "bruno")]
    es = generated_files(lead="ana", deputy="bruno", sections=sections, source_sha12="3f9a1c0b7e2d", lang="es")
    en = generated_files(lead="ana", deputy="bruno", sections=sections, source_sha12="3f9a1c0b7e2d", lang="en")
    assert tuple(es) == GENERATED_PATHS == tuple(en)
    assert GENERATED_PATHS[0] == CODEOWNERS_PATH
    assert es[CODEOWNERS_PATH] == en[CODEOWNERS_PATH] == EXAMPLE
