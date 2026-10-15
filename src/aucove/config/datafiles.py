# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Ubicación de los datos empaquetados (REQ-ARCH-3, REQ-ARCH-28; D-36).

En la rueda, ``schemas/``, ``template/`` y ``skills/`` viven en
``<paquete>/_data/<kind>`` por ``force-include``. En la instalación editable
esos datos no quedan junto al código (hatch los copia fuera del paquete fuente),
así que se buscan en la raíz del repositorio, si su ``pyproject.toml`` declara
``name == _meta.NAME``; lo mismo vale para ``template_vars.toml``, los valores de la plantilla
publicada, que en el repositorio es ``tools/template_vars.toml`` (REQ-ARCH-3 y REQ-ARCH-102; 00-marco
D-73). ``gitleaks.toml`` y ``required-paths.json`` son archivos
reales de ``src/<paquete>/_data/`` y están en los dos casos, igual que los textos de
licencia de ``init`` (``LICENSES/``, 11-trazabilidad §7.3; 00-marco D-64) y la lista de licencias
de SPDX (``spdx-licenses.json``, CC0; REQ-TRACE-62).
"""

from __future__ import annotations

import functools
import json
import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from .. import _meta
from ..domain.licenses import PACKAGED

DataKind = Literal["schemas", "template", "skills"]
DATA_KINDS: tuple[DataKind, ...] = ("schemas", "template", "skills")

_PACKAGE_DIR = Path(__file__).resolve().parent.parent
_DATA_DIR = _PACKAGE_DIR / "_data"


@functools.cache
def _repo_root() -> Path | None:
    """Raíz del repositorio del framework en una instalación editable, o ``None``."""
    root = _PACKAGE_DIR.parent.parent  # src/<paquete> -> src -> raíz
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project")
    if isinstance(project, dict) and project.get("name") == _meta.NAME:
        return root
    return None


def data_path(kind: DataKind) -> Path:
    """Directorio de datos ``kind``; ``FileNotFoundError`` si no existe en ningún lugar."""
    if kind not in DATA_KINDS:
        raise ValueError(f"tipo de datos desconocido: {kind!r}")
    packaged = _DATA_DIR / kind
    if packaged.is_dir():
        return packaged
    root = _repo_root()
    if root is not None and (root / kind).is_dir():
        return root / kind
    raise FileNotFoundError(f"no se encuentran los datos empaquetados {kind!r}")


def _data_file(name: str) -> Path:
    path = _DATA_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"falta el archivo de datos {name!r}")
    return path


TEMPLATE_VARS = "template_vars.toml"


@functools.cache
def template_vars() -> Mapping[str, str]:
    """Valores de los ``{{var:...}}`` de la plantilla publicada (``tools/template_vars.toml``).

    En la rueda es ``_data/template_vars.toml`` (``force-include``, REQ-ARCH-3); en la instalación
    editable, el de ``tools/`` del repositorio. ``FileNotFoundError`` si no está; ``ValueError`` si no
    es TOML o algún valor no es texto.
    """
    path = _DATA_DIR / TEMPLATE_VARS
    root = _repo_root()
    if not path.is_file() and root is not None:
        path = root / "tools" / TEMPLATE_VARS
    if not path.is_file():
        raise FileNotFoundError(f"no se encuentran los datos empaquetados {TEMPLATE_VARS!r}")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{TEMPLATE_VARS}: {exc}") from exc
    values: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(value, str):
            raise ValueError(f"{TEMPLATE_VARS}: el valor de {key!r} no es texto")
        values[key] = value
    return MappingProxyType(values)


def gitleaks_config() -> Path:
    """Configuración empaquetada de gitleaks (regla ``secrets``)."""
    return _data_file("gitleaks.toml")


def required_paths_file() -> Path:
    """Lista de rutas obligatorias de la convención filtrada por hito."""
    return _data_file("required-paths.json")


def license_text(license_id: str) -> bytes | None:
    """Texto empaquetado de una licencia de la tabla de 11 §7.3 (00-marco D-64), o ``None`` si no lo trae."""
    if license_id not in PACKAGED:
        return None
    try:
        return (_DATA_DIR / "LICENSES" / f"{license_id}.txt").read_bytes()
    except OSError:
        return None


@functools.cache
def _spdx() -> dict[str, object]:
    data = json.loads(_data_file("spdx-licenses.json").read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def spdx_licenses() -> Mapping[str, bool]:
    """La lista de licencias de SPDX empaquetada: identificador -> ``True`` si está obsoleto."""
    licenses = _spdx().get("licenses")
    items = licenses.items() if isinstance(licenses, dict) else ()
    return MappingProxyType({str(k): bool(v.get("deprecated")) for k, v in items if isinstance(v, dict)})


def spdx_version() -> str:
    """Versión de la lista de licencias de SPDX empaquetada (``licenseListVersion``)."""
    return str(_spdx().get("licenseListVersion", "?"))
