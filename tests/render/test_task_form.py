# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``.github/ISSUE_TEMPLATE/task.yml`` generado (03-esquemas §8.1, REQ-SCHEMA-62..64)."""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

import pytest
import yaml

from aucove import _meta
from aucove.i18n import LANGS, check_parity
from aucove.render import task_form
from aucove.render.generated import generated_files
from aucove.render.task_form import TASK_FORM_PATH, TEXT_KEYS, form_texts, render_task_form
from aucove.render.template import RenderError
from tests.conftest import REPO_ROOT

SPEC_ENV = _meta.ENV_PREFIX + "SPEC_DIR"
CATALOGS = REPO_ROOT / "src" / _meta.NAME / "i18n" / "catalogs"

# Ejemplo de 03-esquemas §8.1 (versión es), byte a byte.
EXAMPLE_ES = """\
# Clase `generated`: lo generan `init` y `sync-roles` desde roles.yaml; no lo edites a mano.
name: Tarea
description: Propón una tarea del proyecto. El responsable la acepta antes de que alguien la reclame.
labels: ["task", "state:proposed"]
body:
  - type: markdown
    attributes:
      value: |
        Una tarea declara las partes que puede modificar (una tarea, una rama, un PR).
        El núcleo lee este formulario: no cambies los títulos de los campos.
  - type: textarea
    id: summary
    attributes:
      label: Resumen
      description: Qué se hará y por qué, en 1 a 5 oraciones.
    validations:
      required: true
  - type: dropdown
    id: sections
    attributes:
      label: Partes
      description: |
        Partes que la tarea puede modificar.
        - `introduccion`: Introducción (@ana-perez)
        - `metodos`: Métodos (@Bruno-Diaz)
        - `resultados`: Resultados (@carla-soto)
      multiple: true
      options:
        - "introduccion"
        - "metodos"
        - "resultados"
    validations:
      required: true
  - type: input
    id: plan_refs
    attributes:
      label: Referencias al plan
      description: IDs de plan.md separados por comas. Déjalo vacío si no aplica.
      placeholder: "RQ-1, METH-1"
  - type: textarea
    id: done
    attributes:
      label: Criterio de terminado
      description: Condiciones verificables, una por línea y empezando con "- ".
      placeholder: "- La subsección 3.2 describe la limpieza de la ola 2025."
    validations:
      required: true
""".encode("utf-8")

EXAMPLE_SECTIONS = [
    ("introduccion", "Introducción", "ana-perez"),
    ("metodos", "Métodos", "Bruno-Diaz"),
    ("resultados", "Resultados", "carla-soto"),
]

# Etiquetas canónicas de 03 §8.1 (el id no se localiza; la etiqueta dice «Partes», D-43).
CANONICAL = {
    "es": {"summary": "Resumen", "sections": "Partes", "plan_refs": "Referencias al plan", "done": "Criterio de terminado"},
    "en": {"summary": "Summary", "sections": "Parts", "plan_refs": "Plan references", "done": "Definition of done"},
}
TYPES = {"summary": "textarea", "sections": "dropdown", "plan_refs": "input", "done": "textarea"}


def fields(data: dict) -> dict[str, dict]:
    return {item["id"]: item for item in data["body"] if "id" in item}


def test_reproduces_the_example_of_03_8_1() -> None:
    assert render_task_form(sections=EXAMPLE_SECTIONS, lang="es") == EXAMPLE_ES


def test_matches_the_spec_block_when_available() -> None:
    spec_dir = os.environ.get(SPEC_ENV)
    if not spec_dir:
        pytest.skip(f"{SPEC_ENV} no está definido")
    text = (Path(spec_dir) / "03-esquemas.md").read_bytes().replace(b"\r\n", b"\n")
    match = re.search(rb"### 8\.1 [^\n]*\n\n```yaml\n(.*?)```", text, re.S)
    assert match is not None, "no se encuentra el bloque de 03 §8.1"
    assert render_task_form(sections=EXAMPLE_SECTIONS, lang="es") == match.group(1)


@pytest.mark.parametrize("lang", LANGS)
def test_structure_ids_types_and_canonical_labels(lang: str) -> None:
    data = yaml.safe_load(render_task_form(sections=EXAMPLE_SECTIONS, lang=lang))  # type: ignore[arg-type]
    assert data["labels"] == ["task", "state:proposed"]
    assert "assignees" not in data  # REQ-SCHEMA-64
    by_id = fields(data)
    assert list(by_id) == ["summary", "sections", "plan_refs", "done"]
    for field_id, item in by_id.items():
        assert item["type"] == TYPES[field_id]
        assert item["attributes"]["label"] == CANONICAL[lang][field_id]
    assert by_id["summary"]["validations"]["required"] is True
    assert by_id["done"]["validations"]["required"] is True
    assert by_id["sections"]["attributes"]["multiple"] is True


@pytest.mark.parametrize("lang", LANGS)
def test_section_options_follow_roles_order(lang: str) -> None:
    sections = [("results", "Results", "bruno"), ("introduction", "Introduction", "ana"), ("methods", "Methods", "carla")]
    raw = render_task_form(sections=sections, lang=lang)  # type: ignore[arg-type]
    attrs = fields(yaml.safe_load(raw))["sections"]["attributes"]
    assert attrs["options"] == ["results", "introduction", "methods"]
    listing = attrs["description"].splitlines()[1:]
    assert listing == [
        "- `results`: Results (@bruno)",
        "- `introduction`: Introduction (@ana)",
        "- `methods`: Methods (@carla)",
    ]
    # Las opciones van entre comillas (REQ-SCHEMA-62).
    assert '        - "results"\n        - "introduction"\n        - "methods"\n' in raw.decode("utf-8")


def test_sections_without_owner_omit_the_mention() -> None:
    attrs = fields(yaml.safe_load(render_task_form(sections=[("methods", "Métodos")], lang="es")))["sections"]["attributes"]
    assert attrs["description"].splitlines()[1:] == ["- `methods`: Métodos"]


@pytest.mark.parametrize("lang", LANGS)
def test_none_is_forbidden(lang: str) -> None:
    with pytest.raises(RenderError):
        render_task_form(sections=[("methods", "Métodos"), ("none", "Nada")], lang=lang)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "sections",
    [
        [("Methods", "Métodos")],
        [("methods", "Métodos"), ("methods", "Otra")],
        [("methods", "   ")],
        [("methods", "Métodos", "bad login")],
        [("methods",)],
        ["methods"],
    ],
)
def test_rejects_invalid_sections(sections: list) -> None:
    with pytest.raises(RenderError):
        render_task_form(sections=sections, lang="es")


@pytest.mark.parametrize("lang", LANGS)
def test_form_labels_are_known_to_the_body_parser(lang: str) -> None:
    """03 §8.3 y REQ-SCHEMA-73: el analizador reconoce cada etiqueta del formulario sin form_labels.

    La regla scope analiza el cuerpo del Issue sin las etiquetas del task.yml de la base, así
    que una etiqueta que falte en CANON deja la tarea sin partes; task propose escribe el cuerpo
    con las mismas etiquetas (LABELS).
    """
    from aucove.domain import taskbody

    data = yaml.safe_load(render_task_form(sections=EXAMPLE_SECTIONS, lang=lang))  # type: ignore[arg-type]
    labels = {field_id: item["attributes"]["label"] for field_id, item in fields(data).items()}
    assert {label: taskbody.CANON.get(label) for label in labels.values()} == {v: k for k, v in labels.items()}
    assert dict(taskbody.LABELS[lang]) == labels


@pytest.mark.parametrize("lang", LANGS)
def test_without_sections_the_field_is_an_input(lang: str) -> None:
    data = yaml.safe_load(render_task_form(sections=[], lang=lang))  # type: ignore[arg-type]
    item = fields(data)["sections"]
    assert item["type"] == "input"  # REQ-SCHEMA-63
    assert item["attributes"]["label"] == CANONICAL[lang]["sections"]
    assert "options" not in item["attributes"]


@pytest.mark.parametrize("lang", LANGS)
def test_texts_come_from_render_toml(lang: str) -> None:
    raw = tomllib.loads((CATALOGS / lang / "render.toml").read_text(encoding="utf-8"))
    table = raw["render"]["task_form"]

    def lookup(key: str) -> str:
        node: object = table
        for part in key.split("."):
            assert isinstance(node, dict)
            node = node[part]
        assert isinstance(node, str)
        return node

    texts = form_texts(lang)  # type: ignore[arg-type]
    assert set(texts) == set(TEXT_KEYS)
    for key in TEXT_KEYS:
        assert texts[key] == lookup(key)
    out = render_task_form(sections=[("methods", "Métodos", "carla")], lang=lang).decode("utf-8")  # type: ignore[arg-type]
    for key in TEXT_KEYS:
        if key == "sections.description_empty":
            continue
        for line in texts[key].splitlines():
            assert line in out, (key, line)


def test_languages_differ_and_catalogs_are_in_parity() -> None:
    es = render_task_form(sections=[("methods", "Methods")], lang="es")
    en = render_task_form(sections=[("methods", "Methods")], lang="en")
    assert es != en
    assert check_parity() == []


def test_missing_catalog_key_is_a_render_error(monkeypatch: pytest.MonkeyPatch) -> None:
    full = dict(task_form.load_catalog("en"))
    full.pop("render.task_form.done.label")
    monkeypatch.setattr(task_form, "load_catalog", lambda lang: full)
    task_form.form_texts.cache_clear()
    try:
        with pytest.raises(RenderError):
            render_task_form(sections=[], lang="en")
    finally:
        monkeypatch.undo()
        task_form.form_texts.cache_clear()


def test_titles_are_collapsed_to_one_line_and_parse_back() -> None:
    title = 'Métodos:\n "a" #\tb'
    raw = render_task_form(sections=[("methods", title, "carla")], lang="es")
    attrs = fields(yaml.safe_load(raw))["sections"]["attributes"]
    assert attrs["description"].splitlines()[1] == '- `methods`: Métodos: "a" # b (@carla)'


def test_unknown_language_is_rejected() -> None:
    with pytest.raises(RenderError):
        render_task_form(sections=[], lang="fr")  # type: ignore[arg-type]


@pytest.mark.parametrize("lang", LANGS)
def test_deterministic_utf8_lf_and_final_newline(lang: str) -> None:
    first = render_task_form(sections=EXAMPLE_SECTIONS, lang=lang)  # type: ignore[arg-type]
    assert first == render_task_form(sections=EXAMPLE_SECTIONS, lang=lang)  # type: ignore[arg-type]
    assert not first.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in first
    assert first.endswith(b"\n") and not first.endswith(b"\n\n")


def test_generated_files_task_form_is_localized() -> None:
    sections = [("introduction", "Introduction", "ana")]
    es = generated_files(lead="ana", deputy=None, sections=sections, source_sha12="0" * 12, lang="es")
    en = generated_files(lead="ana", deputy=None, sections=sections, source_sha12="0" * 12, lang="en")
    assert es[TASK_FORM_PATH] == render_task_form(sections=sections, lang="es")
    assert en[TASK_FORM_PATH] == render_task_form(sections=sections, lang="en")
    assert es[TASK_FORM_PATH] != en[TASK_FORM_PATH]
