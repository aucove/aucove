# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Generación de ``.github/ISSUE_TEMPLATE/task.yml`` (03-esquemas §8.1).

- Los ``id`` de los campos no se localizan; las etiquetas y los demás textos
  salen de ``catalogs/<lang>/render.toml`` (claves ``render.task_form.*``).
  Una clave que falta es ``RenderError``: el formulario generado no puede
  llevar el nombre de una clave en lugar del texto.
- ``sections`` es un ``dropdown`` múltiple con las ``options`` de
  ``roles.yaml`` en su orden y entre comillas, y la lista de la
  ``description`` con el id, el título y, si se conoce, el dueño
  (REQ-SCHEMA-62, como el ejemplo de 03 §8.1). Sin partes, es un
  ``input`` con el mismo ``id`` y la misma etiqueta (REQ-SCHEMA-63).
  El ``id`` sigue siendo ``sections`` y la etiqueta visible dice «Partes»
  o «Parts» (D-43).
- El id ``none`` está prohibido: GitHub escribe ``None`` en un ``dropdown``
  sin respuesta (03 §8.2).
- Etiquetas ``task`` y ``state:proposed``; sin ``assignees`` (REQ-SCHEMA-64).
- Salida determinista en UTF-8 sin BOM, con LF y salto final (REQ-ARCH-82).
"""

from __future__ import annotations

import functools
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final

from ..i18n import LANGS, CatalogError, Lang, load_catalog
from .template import RenderError, check_login, check_section_id, normalize_text

__all__ = ["TASK_FORM_PATH", "TEXT_KEYS", "render_task_form", "form_texts"]

TASK_FORM_PATH: Final = ".github/ISSUE_TEMPLATE/task.yml"

_PREFIX: Final = "render.task_form."
# Claves que necesita el formulario (sin el prefijo render.task_form.).
TEXT_KEYS: Final[tuple[str, ...]] = (
    "header",
    "name",
    "description",
    "intro",
    "summary.label",
    "summary.description",
    "sections.label",
    "sections.description",
    "sections.description_empty",
    "plan_refs.label",
    "plan_refs.description",
    "plan_refs.placeholder",
    "done.label",
    "done.description",
    "done.placeholder",
)

_INDENT_BLOCK: Final = " " * 8
_WS: Final[re.Pattern[str]] = re.compile(r"\s+")
_NUMBER: Final[re.Pattern[str]] = re.compile(r"[-+]?(\.?[0-9]|0[xob]|\.(inf|nan))", re.IGNORECASE)
# Palabras que YAML 1.1 (PyYAML) o 1.2 no leen como cadena si van sin comillas.
_YAML_WORDS: Final = frozenset(
    {"y", "yes", "n", "no", "true", "false", "on", "off", "null", "~", "=", "<<"}
)
_PLAIN_FIRST_FORBIDDEN: Final = frozenset("-?:,[]{}#&*!|>'\"%@`")


@functools.cache
def form_texts(lang: Lang) -> Mapping[str, str]:
    """Textos de ``render.task_form.*`` de ``lang`` (sin respaldo en otro idioma)."""
    if lang not in LANGS:
        raise RenderError(f"idioma no admitido: {lang!r}")
    try:
        catalog = load_catalog(lang)
    except CatalogError as exc:
        raise RenderError(f"catálogo {lang} inválido: {exc}") from exc
    texts: dict[str, str] = {}
    for key in TEXT_KEYS:
        text = catalog.get(_PREFIX + key)
        if text is None or not text.strip():
            raise RenderError(f"falta {_PREFIX}{key} en el catálogo {lang}")
        texts[key] = text
    return MappingProxyType(texts)


def _one_line(text: str) -> str:
    """Una línea: controles y separadores de línea a espacio, espacios colapsados."""
    cleaned = "".join(
        " " if unicodedata.category(ch) in ("Cc", "Zl", "Zp") else ch for ch in text
    )
    return _WS.sub(" ", cleaned).strip()


def _plain_ok(text: str) -> bool:
    """``text`` se puede escribir como escalar plano de YAML y se lee como la misma cadena."""
    if not text or text != text.strip() or text != _one_line(text):
        return False
    if text[0] in _PLAIN_FIRST_FORBIDDEN:
        return False
    if ": " in text or " #" in text or text.endswith(":") or "\t" in text:
        return False
    if text.lower() in _YAML_WORDS or _NUMBER.match(text):
        return False
    return True


def _scalar(text: str) -> str:
    """Escalar plano si es seguro; si no, cadena JSON (que YAML lee entre comillas dobles)."""
    return text if _plain_ok(text) else json.dumps(text, ensure_ascii=False)


def _quoted(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def _block(lines: Sequence[str]) -> list[str]:
    """Escalar literal ``|`` con sangría de 8 espacios; las líneas vacías van sin espacios."""
    return [_INDENT_BLOCK + line if line else "" for line in lines]


def _block_lines(text: str) -> list[str]:
    lines = [_one_line(line) for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        raise RenderError("bloque de texto vacío en el formulario de tarea")
    return lines


def _sections(sections: Sequence[Sequence[str]]) -> list[tuple[str, str, str | None]]:
    """Valida ``(id, title)`` o ``(id, title, owner)`` y devuelve la lista normalizada."""
    seen: set[str] = set()
    out: list[tuple[str, str, str | None]] = []
    for item in sections:
        if isinstance(item, (str, bytes)) or not isinstance(item, Sequence) or len(item) not in (2, 3):
            raise RenderError(f"parte mal formada: {item!r} (se espera (id, title) o (id, title, owner))")
        section_id = check_section_id(item[0])
        title = item[1]
        if not isinstance(title, str) or not _one_line(title):
            raise RenderError(f"título vacío o inválido en la parte {section_id!r}")
        owner = item[2] if len(item) == 3 else None
        if owner is not None:
            check_login(owner, f"owner de {section_id}")
        if section_id in seen:
            raise RenderError(f"parte repetida: {section_id!r}")
        seen.add(section_id)
        out.append((section_id, _one_line(title), owner))
    return out


def render_task_form(*, sections: Sequence[Sequence[str]], lang: Lang) -> bytes:
    """Bytes de ``task.yml`` en ``lang`` para ``sections`` en el orden de ``roles.yaml``.

    Cada parte es ``(id, title)`` o ``(id, title, owner)``; con el dueño, la
    lista de la ``description`` lo muestra como ``(@owner)``, igual que el
    ejemplo de 03 §8.1. ``render.generated.generated_files`` siempre lo pasa.
    """
    t = form_texts(lang)
    items = _sections(sections)
    header = _one_line(t["header"])

    lines: list[str] = [
        f"# {header}",
        f"name: {_scalar(_one_line(t['name']))}",
        f"description: {_scalar(_one_line(t['description']))}",
        'labels: ["task", "state:proposed"]',
        "body:",
        "  - type: markdown",
        "    attributes:",
        "      value: |",
        *_block(_block_lines(t["intro"])),
        "  - type: textarea",
        "    id: summary",
        "    attributes:",
        f"      label: {_scalar(_one_line(t['summary.label']))}",
        f"      description: {_scalar(_one_line(t['summary.description']))}",
        "    validations:",
        "      required: true",
    ]
    sections_label = _scalar(_one_line(t["sections.label"]))
    if items:
        listing = [_one_line(t["sections.description"])]
        for section_id, title, owner in items:
            suffix = f" (@{owner})" if owner is not None else ""
            listing.append(f"- `{section_id}`: {title}{suffix}")
        lines += [
            "  - type: dropdown",
            "    id: sections",
            "    attributes:",
            f"      label: {sections_label}",
            "      description: |",
            *_block(listing),
            "      multiple: true",
            "      options:",
            *(f"        - {_quoted(section_id)}" for section_id, _title, _owner in items),
            "    validations:",
            "      required: true",
        ]
    else:
        # REQ-SCHEMA-63: GitHub no admite un dropdown sin opciones. Sin validación
        # obligatoria: no hay ningún id válido que escribir.
        lines += [
            "  - type: input",
            "    id: sections",
            "    attributes:",
            f"      label: {sections_label}",
            f"      description: {_scalar(_one_line(t['sections.description_empty']))}",
        ]
    lines += [
        "  - type: input",
        "    id: plan_refs",
        "    attributes:",
        f"      label: {_scalar(_one_line(t['plan_refs.label']))}",
        f"      description: {_scalar(_one_line(t['plan_refs.description']))}",
        f"      placeholder: {_quoted(_one_line(t['plan_refs.placeholder']))}",
        "  - type: textarea",
        "    id: done",
        "    attributes:",
        f"      label: {_scalar(_one_line(t['done.label']))}",
        f"      description: {_scalar(_one_line(t['done.description']))}",
        f"      placeholder: {_quoted(_one_line(t['done.placeholder']))}",
        "    validations:",
        "      required: true",
    ]
    return normalize_text("\n".join(lines).encode("utf-8"))
