# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Enlaces y grafía real de las rutas (02 REQ-CONV-9, REQ-CONV-10).

- ``link_probe(root)`` da la sonda que usa ``domain.permissions.check_path``: una ruta
  canónica cuenta como enlace si algún prefijo existente es un enlace simbólico o, en
  Windows, un punto de reanálisis (uniones incluidas).
- ``case_map(git)`` da el mapa ``minúsculas -> grafía real`` de ``git ls-files`` para
  ``domain.paths.apply_case_map`` (claves con ``str.lower``, valores en NFC).
"""

from __future__ import annotations

import os
import stat
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .git import Git

__all__ = ["case_map", "is_link", "link_probe"]

# FILE_ATTRIBUTE_REPARSE_POINT (0x400): existe en stat solo en Windows.
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def is_link(st: os.stat_result) -> bool:
    """``True`` si el resultado de ``lstat`` es un enlace o, en Windows, un punto de reanálisis."""
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def link_probe(root: Path) -> Callable[[str], bool]:
    """Sonda de enlaces bajo ``root`` para rutas canónicas relativas con ``/``.

    Recorre cada prefijo con ``lstat`` hasta el primero que no existe. Un segmento
    inválido (vacío, ``.``, ``..`` o con NUL) o un error distinto de «no existe» cuenta
    como enlace: la sonda falla cerrada, porque un enlace se clasifica R0.
    """
    base = os.path.abspath(os.fspath(root))

    def probe(rel: str) -> bool:
        if not rel:
            return False
        segments = rel.split("/")
        if any(s in ("", ".", "..") or "\0" in s for s in segments):
            return True
        current = base
        for seg in segments:
            current = os.path.join(current, seg)
            try:
                st = os.lstat(current)
            except (FileNotFoundError, NotADirectoryError):
                return False
            except (OSError, ValueError):
                return True
            if is_link(st):
                return True
            if not stat.S_ISDIR(st.st_mode):
                return False  # un archivo: lo que sigue no puede existir
        return False

    return probe


def case_map(git: Git) -> dict[str, str]:
    """``ruta.lower() -> ruta`` (NFC) de los archivos rastreados (REQ-CONV-9).

    Solo archivos: ``apply_case_map`` deduce de ellos las carpetas. Si dos rutas
    rastreadas difieren solo en mayúsculas, gana la primera en el orden de git.
    """
    out: dict[str, str] = {}
    for path in git.ls_files():
        real = unicodedata.normalize("NFC", path)
        out.setdefault(real.lower(), real)
    return out
