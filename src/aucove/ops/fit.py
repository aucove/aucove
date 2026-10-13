# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Presupuesto de contexto por respuesta: medida, ajuste y cursores (05 §7.1 y §7.3).

- ``envelope_size``: REQ-OPS-31, puntos de código de la serialización compacta del sobre.
- ``fit``: el algoritmo de 05 §7.3. Recorta las listas en el orden de la operación, del
  final hacia el principio (así se conserva el orden determinista de REQ-OPS-35), y
  devuelve qué recortó. La medida incluye ya el aviso ``W_OUTPUT_TRUNCATED`` que se
  agregaría, para que el resultado final quepa con él.
- ``encode_cursor`` y ``decode_cursor``: REQ-OPS-36. El cursor es base64url, sin relleno,
  de ``{"v":1,"op","list","offset","snap"}``, y ``snap`` son los primeros 16 hex del
  SHA-256 de los IDs ordenados de la lista completa.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from .. import _meta
from .result import OpError, WarningObj

__all__ = [
    "FitResult",
    "decode_cursor",
    "encode_cursor",
    "envelope_size",
    "fit",
    "snap_of",
]

MAX_CURSOR_CHARS = 512  # un cursor propio mide menos de 200


def envelope_size(op: str, data: object, warnings: Sequence[WarningObj] = ()) -> int:
    """Tamaño de REQ-OPS-31 del sobre ``ok`` con ``data`` y ``warnings``."""
    envelope = {
        "schema": _meta.RESULT_SCHEMA,
        "ok": True,
        "op": op,
        "data": data,
        "warnings": [w.to_dict() for w in warnings],
        "error": None,
    }
    return len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), allow_nan=False))


@dataclass
class FitResult:
    """Lo que hizo ``fit``: elementos quitados por lista (vacío si no recortó nada)."""

    omitted: dict[str, int] = field(default_factory=dict)

    @property
    def trimmed(self) -> list[str]:
        return [k for k, n in self.omitted.items() if n]

    def warning_details(self, budget: int) -> dict[str, object]:
        """``details`` de ``W_OUTPUT_TRUNCATED`` (05 §7.3)."""
        return {"budget": budget, "trimmed": self.trimmed, "omitted": {k: n for k, n in self.omitted.items() if n}}


def _largest_fitting(n: int, fits: Callable[[int], bool]) -> int:
    """Mayor ``k`` en ``[0, n]`` con ``fits(k)``; ``fits`` es monótona (si ``k`` cabe, cabe ``k-1``)."""
    lo, hi = 0, n
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid - 1
    return lo


def fit(
    op: str,
    data: dict[str, object],
    *,
    budget: int,
    trim_order: Sequence[str],
    warnings: Sequence[WarningObj] = (),
    truncated_warning: WarningObj,
    exclude: Iterable[str] = (),
) -> FitResult:
    """Recorta en su lugar las listas ``trim_order`` de ``data`` hasta que el sobre quepa.

    ``truncated_warning`` es el ``W_OUTPUT_TRUNCATED`` que se agregaría: se mide con él para
    que, si hace falta recortar, el resultado final no se pase. ``exclude`` son claves de
    ``data`` que no cuentan para el tope (p. ej. ``summary_md`` de ``context``, que tiene
    el suyo; REQ-OPS-67). Los límites por campo (§7.2) los aplica quien arma ``data``.
    """
    skipped = {k: data[k] for k in exclude if k in data}

    def size(extra: Sequence[WarningObj]) -> int:
        measured = {k: v for k, v in data.items() if k not in skipped}
        return envelope_size(op, measured, [*warnings, *extra])

    result = FitResult()
    if size(()) <= budget:
        return result
    for key in trim_order:
        items = data.get(key)
        if not isinstance(items, list) or not items:
            continue
        full = list(items)

        def fits(k: int, key: str = key, full: list[object] = full) -> bool:
            data[key] = full[:k]
            return size((truncated_warning,)) <= budget

        keep = _largest_fitting(len(full), fits)
        data[key] = full[:keep]
        result.omitted[key] = len(full) - keep
        if size((truncated_warning,)) <= budget:
            break
    return result


def snap_of(ids: Iterable[object]) -> str:
    """``snap`` de REQ-OPS-36: SHA-256 de los IDs en su orden, 16 hex."""
    canon = json.dumps([str(x) for x in ids], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("ascii")).hexdigest()[:16]


def encode_cursor(op: str, list_name: str, offset: int, snap: str) -> str:
    """Cursor de REQ-OPS-36 para seguir en ``offset``."""
    payload = {"v": 1, "op": op, "list": list_name, "offset": offset, "snap": snap}
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, *, op: str, list_name: str, snap: str) -> int:
    """``offset`` de un cursor de esta operación y esta lista.

    Un cursor mal formado, de otra operación o de otra lista da ``E_VALIDATION_INPUT``; uno
    cuyo ``snap`` ya no es el del estado actual, ``E_VALIDATION_CURSOR_STALE`` (REQ-OPS-36).
    """
    bad = OpError("E_VALIDATION_INPUT", details={"errors": [{"param": "cursor", "reason": "invalid"}]})
    if not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_CHARS:
        raise bad
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw.decode("ascii"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise bad from None
    if not isinstance(payload, Mapping) or set(payload) != {"v", "op", "list", "offset", "snap"}:
        raise bad
    offset = payload["offset"]
    if (
        payload["v"] != 1
        or payload["op"] != op
        or payload["list"] != list_name
        or isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
        or not isinstance(payload["snap"], str)
    ):
        raise bad
    if payload["snap"] != snap:
        raise OpError("E_VALIDATION_CURSOR_STALE")
    return offset
