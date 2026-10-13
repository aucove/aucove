# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Hallazgo de un validador (03-esquemas §2.4; 05-operaciones §9.5).

Tipo único para los problemas de los validadores de configuración y para los
hallazgos de ``validate``. ``check`` no se serializa dentro de ``findings[]``:
agrupa los hallazgos en ``results[]`` (``$defs/data_validate``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

Severity = Literal["error", "warning"]
SEVERITIES: tuple[Severity, ...] = ("error", "warning")

# Patrones de $defs/data_validate (findings[].code y .pointer) y de $defs/repo_path.
CODE_PATTERN = re.compile(r"^(conv\.[a-z0-9-]+\.[a-z0-9-]+|[EW]_[A-Z]+(_[A-Z0-9]+)+|(IL|TX|SB)[0-9]{2})$")
POINTER_PATTERN = re.compile(r"^(/([^~/]|~[01])*)*$")
PATH_PATTERN = re.compile(r"^[^/\\][^\\]*$")
MAX_HINT = 500


@dataclass(frozen=True, slots=True)
class Finding:
    """Un hallazgo ya localizado. Valida su forma al construirse (``ValueError``)."""

    check: str  # id de regla de 09 §4.1, o 'convention' para conv.* (D-33 g)
    code: str  # patrón de findings[].code del esquema
    severity: Severity
    path: str | None  # repo_path con '/'
    line: int | None
    message: str  # ya localizado
    hint: str | None = None
    pointer: str | None = None  # JSON Pointer (RFC 6901)

    def __post_init__(self) -> None:
        if not self.check:
            raise ValueError("Finding.check vacío")
        if not isinstance(self.code, str) or not CODE_PATTERN.match(self.code):
            raise ValueError(f"código de hallazgo inválido: {self.code!r}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"severidad inválida: {self.severity!r}")
        if self.path is not None and (not isinstance(self.path, str) or not PATH_PATTERN.match(self.path)):
            raise ValueError(f"ruta de hallazgo inválida: {self.path!r}")
        if self.line is not None and (isinstance(self.line, bool) or not isinstance(self.line, int) or self.line < 1):
            raise ValueError(f"línea inválida: {self.line!r}")
        if not isinstance(self.message, str):
            raise ValueError("Finding.message debe ser texto")
        if self.hint is not None and (not isinstance(self.hint, str) or len(self.hint) > MAX_HINT):
            raise ValueError("Finding.hint inválido o de más de 500 caracteres")
        if self.pointer is not None and (not isinstance(self.pointer, str) or not POINTER_PATTERN.match(self.pointer)):
            raise ValueError(f"JSON Pointer inválido: {self.pointer!r}")

    def to_dict(self) -> dict[str, object]:
        """Forma de ``findings[]`` en ``data_validate`` (sin ``check``)."""
        return {
            "path": self.path,
            "line": self.line,
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "pointer": self.pointer,
            "hint": self.hint,
        }


def has_errors(fs: Iterable[Finding]) -> bool:
    """``True`` si algún hallazgo tiene severidad ``error``."""
    return any(f.severity == "error" for f in fs)
