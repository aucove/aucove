# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Sobre de resultado ``<NAME>.result/v1`` (05-operaciones §2 y §3).

``message`` y ``hint`` salen del catálogo por código (REQ-OPS-6). ``OpError`` y
``make_*`` validan el código y los marcadores al construirse (``ValueError``),
para que un error de contrato aparezca en las pruebas del paquete que lo comete.
Por la misma razón, ``fail`` exige la ``data`` completa con los errores que la
llevan según 05 §12.1 (``doctor`` y ``evidence.verify``, §9.26).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import _meta
from ..domain.errors import ERRORS, WARNING_PARAMS, WARNINGS
from ..domain.errors import exit_code as _exit_code
from ..i18n import Translator

MAX_TEXT = 500  # maxLength de message y hint (05 §7.2)
MAX_ENVELOPE_BYTES = 5 * 2**20

# Errores de 05 §12.1 cuyo sobre lleva la data completa de la operación (nunca
# failure_data ni None): el resultado de cada check o de cada registro de evidencia.
FULL_DATA_ERRORS: frozenset[str] = frozenset({"E_CONFIG_DOCTOR_FAILED", "E_VALIDATION_EVIDENCE_FAILED"})


def clip(text: str, limit: int) -> str:
    """Recorta ``text`` a ``limit`` puntos de código con el marcador de REQ-OPS-34."""
    if len(text) <= limit:
        return text
    # N <= len(text), así que este marcador nunca es más corto que el definitivo.
    keep = max(0, limit - len(f" [...truncated {len(text)} chars]"))
    marker = f" [...truncated {len(text) - keep} chars]"
    if len(marker) > limit:
        return text[:limit]
    return text[:keep] + marker


@dataclass(frozen=True)
class ErrorObj:
    """Objeto ``error`` del sobre: ``{code, message, retryable, hint, details?}``."""

    code: str
    message: str
    retryable: bool
    hint: str | None
    details: dict | None = None  # type: ignore[type-arg]

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "hint": self.hint,
        }
        if self.details is not None:
            out["details"] = self.details
        return out


@dataclass(frozen=True)
class WarningObj:
    """Aviso del sobre: ``{code, message, details?}``."""

    code: str
    message: str
    details: dict | None = None  # type: ignore[type-arg]

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"code": self.code, "message": self.message}
        if self.details is not None:
            out["details"] = self.details
        return out


@dataclass(frozen=True)
class Result:
    """Resultado de una operación; ``to_dict`` produce el sobre de 05 §3.1."""

    op: str
    ok: bool
    data: dict | None  # type: ignore[type-arg]
    warnings: tuple[WarningObj, ...] = ()
    error: ErrorObj | None = None

    def __post_init__(self) -> None:
        if not self.op:
            raise ValueError("Result.op vacío")
        if self.ok and self.error is not None:
            raise ValueError("un resultado ok no lleva error")
        if not self.ok and self.error is None:
            raise ValueError("un resultado fallido necesita error")
        if not isinstance(self.warnings, tuple):
            object.__setattr__(self, "warnings", tuple(self.warnings))

    def to_dict(self) -> dict[str, object]:
        """Sobre en el orden ``schema, ok, op, data, warnings, error``."""
        return {
            "schema": _meta.RESULT_SCHEMA,
            "ok": self.ok,
            "op": self.op,
            "data": self.data,
            "warnings": [w.to_dict() for w in self.warnings],
            "error": self.error.to_dict() if self.error is not None else None,
        }

    def exit_code(self) -> int:
        """Código de salida de 05 §5 (nunca 2)."""
        return _exit_code(self.ok, self.error.code if self.error is not None else None)


def _check_error(code: str, fmt: Mapping[str, object]) -> None:
    spec = ERRORS.get(code)
    if spec is None:
        raise ValueError(f"código de error fuera del catálogo: {code!r}")
    if set(fmt) != set(spec.params):
        raise ValueError(f"marcadores de {code} distintos de {sorted(spec.params)}: {sorted(fmt)}")


def _check_warning(code: str, fmt: Mapping[str, object]) -> None:
    if code not in WARNINGS:
        raise ValueError(f"código de aviso fuera del catálogo: {code!r}")
    params = WARNING_PARAMS.get(code, frozenset())
    if set(fmt) != set(params):
        raise ValueError(f"marcadores de {code} distintos de {sorted(params)}: {sorted(fmt)}")


class OpError(Exception):
    """Error de operación con código del catálogo; ``registry.invoke`` lo convierte en ``fail``."""

    code: str
    details: dict | None  # type: ignore[type-arg]
    data: dict | None  # type: ignore[type-arg]
    warnings: tuple[WarningObj, ...]
    fmt: dict[str, object]

    def __init__(
        self,
        code: str,
        *,
        details: dict | None = None,  # type: ignore[type-arg]
        data: dict | None = None,  # type: ignore[type-arg]
        warnings: Sequence[WarningObj] = (),
        **fmt: object,
    ) -> None:
        _check_error(code, fmt)
        super().__init__(code)
        self.code = code
        self.details = details
        self.data = data
        self.warnings = tuple(warnings)
        self.fmt = dict(fmt)


def make_error(code: str, t: Translator, *, details: dict | None = None, **fmt: object) -> ErrorObj:  # type: ignore[type-arg]
    """Objeto ``error`` localizado desde ``error.<code>.message`` y ``.hint``."""
    _check_error(code, fmt)
    message = clip(t(f"error.{code}.message", **fmt), MAX_TEXT) or code
    hint = t.get(f"error.{code}.hint", **fmt)
    return ErrorObj(
        code=code,
        message=message,
        retryable=ERRORS[code].retryable,
        hint=clip(hint, MAX_TEXT) if hint is not None else None,
        details=details,
    )


def make_warning(code: str, t: Translator, *, details: dict | None = None, **fmt: object) -> WarningObj:  # type: ignore[type-arg]
    """Aviso localizado desde ``warning.<code>``."""
    _check_warning(code, fmt)
    message = clip(t(f"warning.{code}", **fmt), MAX_TEXT) or code
    return WarningObj(code=code, message=message, details=details)


def ok(op: str, data: dict, warnings: Sequence[WarningObj] = ()) -> Result:  # type: ignore[type-arg]
    """Resultado exitoso."""
    return Result(op=op, ok=True, data=data, warnings=tuple(warnings), error=None)


def fail(
    op: str,
    err: ErrorObj,
    *,
    data: dict | None = None,  # type: ignore[type-arg]
    warnings: Sequence[WarningObj] = (),
) -> Result:
    """Resultado fallido, con ``data`` completa, ``failure_data`` o ``None`` (05 §3.1).

    ``ValueError`` si ``err`` es de ``FULL_DATA_ERRORS`` y no trae la ``data`` completa.
    """
    if err.code in FULL_DATA_ERRORS and (data is None or "failed_step" in data):
        raise ValueError(f"{err.code} lleva la data completa de {op!r} (05 §12.1)")
    return Result(op=op, ok=False, data=data, warnings=tuple(warnings), error=err)


def internal(op: str, exc: BaseException, t: Translator) -> Result:
    """``E_INTERNAL_UNEXPECTED`` con ``details.trace_id``; nunca incluye el texto de ``exc``.

    ``registry.invoke`` registra el fallo en ``events.jsonl`` (``kind: op``, ``code``, ``ts``);
    la lista cerrada de 01 §11 no tiene campo para el ``trace_id``, así que se cruzan por hora.
    """
    del exc  # su texto puede tener datos sensibles (URL con tokens, rutas)
    trace_id = uuid.uuid4().hex[:12]
    return fail(op, make_error("E_INTERNAL_UNEXPECTED", t, details={"trace_id": trace_id}))


def dumps(r: Result) -> bytes:
    """JSON compacto en UTF-8 sin BOM, en una línea, más ``b'\\n'`` (REQ-OPS-9)."""
    text = json.dumps(r.to_dict(), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return text.encode("utf-8") + b"\n"


def read_envelope(path: Path, *, max_bytes: int = MAX_ENVELOPE_BYTES) -> dict | None:  # type: ignore[type-arg]
    """Lectura mínima de un sobre escrito por ``--report``.

    ``None`` si el archivo falta, supera ``max_bytes``, no es JSON UTF-8, no es un
    objeto o su ``schema`` no es ``_meta.RESULT_SCHEMA``.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read(max_bytes + 1)
    except OSError:
        return None
    if len(raw) > max_bytes:
        return None
    try:
        obj = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(obj, dict) or obj.get("schema") != _meta.RESULT_SCHEMA:
        return None
    return obj
