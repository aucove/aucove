# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""JSON canónico: perfil restringido de RFC 8785 (11-trazabilidad §8.3; D-45).

El manifiesto se sella como bytes exactos, así que su forma no admite variantes. El
perfil restringe la entrada para que la forma canónica de RFC 8785 (JCS) se reduzca
a reglas simples:

- objetos con claves de texto ASCII y ordenadas (con claves ASCII, el orden por
  unidades UTF-16 de RFC 8785 §3.2.3 es el de los puntos de código);
- enteros con valor absoluto menor que 2^53, en decimal y sin exponente;
- sin flotantes, ni siquiera ``1.0``, ``NaN`` o ``Infinity``;
- texto Unicode válido, sin sustitutos sueltos (I-JSON, RFC 7493), con los escapes de
  ECMAScript: ``\\"``, ``\\\\``, ``\\b``, ``\\f``, ``\\n``, ``\\r`` y ``\\t``, y ``\\u00xx``
  en minúsculas para el resto de los controles; lo demás va tal cual, en UTF-8;
- ``true``, ``false`` y ``null``; listas en su orden; sin espacios ni salto de línea final.

Lo que viole el perfil es ``CanonicalError`` (con un ``reason`` estable y el JSON
Pointer del valor), nunca una conversión silenciosa. ``loads`` lee con las mismas
restricciones y además rechaza claves repetidas; ``is_canonical`` dice si unos
bytes son exactamente su forma canónica (la verificación no regenera el manifiesto,
REQ-TRACE-79: solo compara).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

__all__ = ["MAX_SAFE_INTEGER", "REASONS", "CanonicalError", "dumps", "is_canonical", "loads"]

MAX_SAFE_INTEGER = 2**53 - 1  # el mayor entero que un double representa sin pérdida

# Códigos de CanonicalError.reason.
REASONS: tuple[str, ...] = (
    "float",  # número con fracción o exponente, o un float de Python
    "int_range",  # entero con valor absoluto de 2^53 o más
    "key_type",  # clave que no es texto
    "key_not_ascii",  # clave con caracteres no ASCII
    "surrogate",  # texto con un sustituto suelto (no es Unicode válido)
    "type",  # valor que JSON no representa (bytes, conjuntos, objetos...)
    "duplicate_key",  # clave repetida al leer
    "encoding",  # bytes que no son UTF-8 válido
    "syntax",  # texto que no es JSON
)

_ESCAPE_RE = re.compile(r'[\x00-\x1f"\\]')
_SURROGATE_RE = re.compile("[\ud800-\udfff]")
_SHORT_ESCAPES: Mapping[str, str] = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}
_MAX_INT_DIGITS = len(str(MAX_SAFE_INTEGER)) + 1  # con el signo


class CanonicalError(ValueError):
    """Valor o texto fuera del perfil restringido de RFC 8785.

    ``reason`` es uno de ``REASONS``; ``pointer``, el JSON Pointer (RFC 6901) del
    valor (``""`` es la raíz; al leer, la ubicación no siempre se conoce).
    """

    def __init__(self, reason: str, pointer: str = "", detail: str = "") -> None:
        self.reason = reason
        self.pointer = pointer
        message = f"JSON canónico: {reason} en {pointer!r}"
        super().__init__(f"{message} ({detail})" if detail else message)


def _escape_key(key: str) -> str:
    return key.replace("~", "~0").replace("/", "~1")


def _escape_char(match: re.Match[str]) -> str:
    ch = match.group()
    return _SHORT_ESCAPES.get(ch) or f"\\u{ord(ch):04x}"


def _string(text: str, pointer: str) -> str:
    if _SURROGATE_RE.search(text):
        raise CanonicalError("surrogate", pointer)
    return '"' + _ESCAPE_RE.sub(_escape_char, text) + '"'


def _emit(value: object, out: list[str], pointer: str) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):  # bool ya salió arriba
        number = int(value)
        if not -MAX_SAFE_INTEGER <= number <= MAX_SAFE_INTEGER:
            raise CanonicalError("int_range", pointer)
        out.append(str(number))
    elif isinstance(value, float):
        raise CanonicalError("float", pointer)
    elif isinstance(value, str):
        out.append(_string(value, pointer))
    elif isinstance(value, Mapping):
        keys = list(value)
        for key in keys:
            if not isinstance(key, str):
                raise CanonicalError("key_type", pointer, type(key).__name__)
            if not key.isascii():
                raise CanonicalError("key_not_ascii", f"{pointer}/{_escape_key(key)}")
        out.append("{")
        for i, key in enumerate(sorted(keys)):
            if i:
                out.append(",")
            child = f"{pointer}/{_escape_key(key)}"
            out.append(_string(key, child))
            out.append(":")
            _emit(value[key], out, child)
        out.append("}")
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _emit(item, out, f"{pointer}/{i}")
        out.append("]")
    else:
        raise CanonicalError("type", pointer, type(value).__name__)


def dumps(value: object) -> bytes:
    """Bytes canónicos de ``value`` (UTF-8, sin espacios ni salto de línea final).

    Admite ``None``, ``bool``, ``int``, ``str``, mapeos con claves de texto ASCII y
    listas o tuplas. Cualquier otra cosa, o un valor fuera del perfil, es ``CanonicalError``.
    """
    out: list[str] = []
    _emit(value, out, "")
    return "".join(out).encode("utf-8")


# -- lectura estricta ------------------------------------------------------------------


def _reject_float(text: str) -> Any:
    raise CanonicalError("float", "", text[:40])


def _reject_constant(text: str) -> Any:
    raise CanonicalError("float", "", text)


def _parse_int(text: str) -> int:
    if len(text) > _MAX_INT_DIGITS:  # antes de convertir: int() de un texto enorme es lento
        raise CanonicalError("int_range", "", text[:40])
    number = int(text)
    if not -MAX_SAFE_INTEGER <= number <= MAX_SAFE_INTEGER:
        raise CanonicalError("int_range", "", text)
    return number


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise CanonicalError("duplicate_key", "", key[:80])
        out[key] = value
    return out


def loads(data: bytes | bytearray | memoryview | str) -> Any:
    """Lee JSON con las restricciones del perfil; ``CanonicalError`` si no las cumple.

    Los bytes se decodifican como UTF-8 estricto (una marca BOM inicial es error de
    sintaxis). Rechaza flotantes, ``NaN``/``Infinity``, enteros fuera de rango, claves
    repetidas o no ASCII y sustitutos sueltos. No exige la forma canónica: para eso,
    ``is_canonical``. El texto puede ser de terceros: un anidamiento tan hondo que agota
    la recursión, al leerlo o al recorrerlo, también es ``syntax``, nunca una excepción
    suelta.
    """
    if isinstance(data, str):
        text = data
    else:
        try:
            text = bytes(data).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CanonicalError("encoding", "", str(exc)) from None
    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
            parse_int=_parse_int,
        )
        dumps(value)  # claves no ASCII y sustitutos sueltos, con su ubicación
    except CanonicalError:
        raise
    except (ValueError, RecursionError) as exc:  # JSONDecodeError es un ValueError
        raise CanonicalError("syntax", "", str(exc)[:120]) from None
    return value


def is_canonical(data: bytes | bytearray | memoryview) -> bool:
    """``True`` si ``data`` cumple el perfil y es exactamente su forma canónica."""
    raw = bytes(data)
    try:
        return dumps(loads(raw)) == raw
    except (CanonicalError, RecursionError):
        return False
