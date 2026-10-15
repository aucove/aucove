# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""La plantilla completa: ``template/`` con sus generados (01-arquitectura §2.3 y REQ-ARCH-102).

``render_project`` agrega al render de ``template/`` (``render_template``) los dos archivos que
salen del ``roles.yaml`` renderizado, ``.github/CODEOWNERS`` y ``.github/ISSUE_TEMPLATE/task.yml``
(00-marco D-35 j), y ``.mcp.json`` (``render.mcp``). Es la única función que arma la plantilla: la
usan ``tools/render_template.py``, y con él el job ``template`` de ``release.yml`` y la autoprueba
(D-37), e ``init --repo``, que la escribe en una carpeta vacía (D-73). Por eso el repositorio
plantilla y ``init --repo`` dan los mismos archivos.

El sha de la cabecera de CODEOWNERS se calcula con ``domain.text.sha12_canonical`` sobre los bytes
de ``roles.yaml`` que se escriben (D-35 m). ``roles.yaml`` se lee con ``yaml.safe_load``, con
importación perezosa (01 §3.1): es la plantilla propia y de confianza.

``published_variables`` da los valores de la plantilla publicada de una versión: los de
``tools/template_vars.toml``, que la rueda lleva (REQ-ARCH-3), con ``core_pin`` igual a esa versión y
``core`` derivado como lo escribe ``init`` (REQ-ARCH-44), para que el pin, aunque sea una versión
preliminar, cumpla el rango.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final

from ..config.datafiles import data_path, template_vars
from ..domain.compat import core_range
from ..domain.text import sha12_canonical
from ..i18n import LANGS, Lang
from .generated import generated_files
from .mcp import MCP_JSON_PATH, mcp_json
from .template import RenderError, render_template

__all__ = [
    "PUBLISHED_FORMAT",
    "PUBLISHED_LANG",
    "ROLES_PATH",
    "generated_from_roles",
    "published_variables",
    "render_project",
]

ROLES_PATH: Final = "roles.yaml"
PUBLISHED_LANG: Final[Lang] = "en"  # la plantilla publicada (01 §2.3; 13 PA-60)
PUBLISHED_FORMAT: Final = "latex"  # 02-convencion REQ-CONV-1


def _login_map(people: object) -> dict[str, str]:
    """Login en minúsculas -> grafía de ``people[].login`` (03 §4)."""
    out: dict[str, str] = {}
    if isinstance(people, list):
        for person in people:
            if isinstance(person, dict) and isinstance(person.get("login"), str):
                out.setdefault(person["login"].lower(), person["login"])
    return out


def generated_from_roles(roles_bytes: bytes, lang: Lang) -> dict[str, bytes]:
    """CODEOWNERS y task.yml del ``roles.yaml`` renderizado (bytes finales)."""
    import yaml

    try:
        data = yaml.safe_load(roles_bytes.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RenderError(f"{ROLES_PATH} renderizado no es YAML válido: {exc}") from exc
    if not isinstance(data, dict):
        raise RenderError(f"{ROLES_PATH} renderizado no es un mapa")
    shown = _login_map(data.get("people"))

    def spelled(login: object, what: str) -> str:
        if not isinstance(login, str):
            raise RenderError(f"{ROLES_PATH}: {what} no es un login")
        return shown.get(login.lower(), login)

    lead = spelled(data.get("lead"), "lead")
    deputy_raw = data.get("deputy")
    deputy = None if deputy_raw is None else spelled(deputy_raw, "deputy")
    sections_raw = data.get("sections") or []
    if not isinstance(sections_raw, list):
        raise RenderError(f"{ROLES_PATH}: sections no es una lista")
    sections: list[tuple[str, str, str]] = []
    for entry in sections_raw:
        if not isinstance(entry, dict):
            raise RenderError(f"{ROLES_PATH}: sección mal formada {entry!r}")
        section_id, title = entry.get("id"), entry.get("title")
        if not isinstance(section_id, str) or not isinstance(title, str):
            raise RenderError(f"{ROLES_PATH}: sección mal formada {entry!r}")
        sections.append((section_id, title, spelled(entry.get("owner"), f"owner de {section_id}")))
    return generated_files(
        lead=lead,
        deputy=deputy,
        sections=sections,
        source_sha12=sha12_canonical(roles_bytes),
        lang=lang,
    )


def render_project(
    lang: Lang,
    variables: Mapping[str, str],
    *,
    template_dir: Path | None = None,
    fmt: str = PUBLISHED_FORMAT,
) -> dict[str, bytes]:
    """Ruta -> bytes de la plantilla completa en ``lang`` y ``format: fmt``, sin escribir nada.

    ``variables`` son los valores de ``{{var:...}}``; ``language`` siempre es ``lang``.
    ``template_dir`` es ``template/`` (por omisión, la de los datos empaquetados).
    ``RenderError`` si algo no se puede renderizar.
    """
    if lang not in LANGS:
        raise RenderError(f"idioma no admitido: {lang!r}")
    src = data_path("template") if template_dir is None else Path(template_dir)
    values = dict(variables)
    values["language"] = lang  # la plantilla publicada va en en; el idioma pedido la reemplaza
    files = render_template(src, lang, values, fmt=fmt)
    roles = files.get(ROLES_PATH)
    if roles is None:
        raise RenderError(f"la plantilla no trae {ROLES_PATH}")
    extra = {**generated_from_roles(roles, lang), MCP_JSON_PATH: mcp_json()}
    return render_template(src, lang, values, extra=extra, fmt=fmt)


def published_variables(version: str) -> dict[str, str]:
    """Los valores de la plantilla publicada de ``version`` (``core_pin`` y su ``core``, REQ-ARCH-44).

    ``ValueError`` si ``version`` no tiene la forma de ``core_pin`` (00-marco D-66) o si
    ``template_vars.toml`` no es válido; ``FileNotFoundError`` si no está.
    """
    values = dict(template_vars())
    values["core_pin"] = version
    values["core"] = core_range(version)
    return values
