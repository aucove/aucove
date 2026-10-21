# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Texto puro del dominio: slugs ASCII, logins, ORCID y forma canónica.

Solo stdlib y sin E/S (REQ-ARCH-18).

- ``ascii_slug``: 01-arquitectura §9.5 (REQ-ARCH-76).
- ``slugify``: 03-esquemas REQ-SCHEMA-13, transcripción literal.
- ``same_login``: 08-forja §6.3 (REQ-FORGE-46); vive aquí para respetar las capas.
- ``orcid_ok``: 03-esquemas §4.5 (REQ-SCHEMA-29), ISO/IEC 7064 MOD 11-2.
- ``sha12_canonical``: forma canónica única de D-35 (m).
"""

import hashlib
import re
import unicodedata

__all__ = [
    "ascii_slug",
    "orcid_ok",
    "same_login",
    "sha12_canonical",
    "slugify",
]

# Letras que NFKD no descompone (01 §9.5). Se incluyen las mayúsculas para que el
# mapeo no dependa del orden con respecto a pasar a minúsculas.
_ASCII_MAP: dict[str, str] = {
    "ß": "ss",
    "ẞ": "ss",
    "æ": "ae",
    "Æ": "ae",
    "œ": "oe",
    "Œ": "oe",
    "ø": "o",
    "Ø": "o",
    "đ": "d",
    "Đ": "d",
    "ł": "l",
    "Ł": "l",
}
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_ORCID_DIGITS = re.compile(r"[0-9]{15}[0-9X]")
_BOM = b"\xef\xbb\xbf"


def ascii_slug(s: str, max_len: int = 48) -> str:
    """Slug ASCII de 01 §9.5; ``x`` si queda vacío."""
    if max_len < 1:
        raise ValueError("max_len must be >= 1")
    t = unicodedata.normalize("NFKD", s)
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    t = "".join(_ASCII_MAP.get(c, c) for c in t)
    t = _NON_ALNUM.sub("-", t.lower()).strip("-")
    return t[:max_len].rstrip("-") or "x"


def slugify(text: str, max_len: int = 48) -> str:
    """Slug de REQ-SCHEMA-13 (03 §2.6), tal cual lo fija la spec."""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()  # ñ -> n, á -> a
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    if len(t) > max_len:  # corta en el último guion que cabe, sin partir una palabra
        cut = t[: max_len + 1].rfind("-")
        t = t[:cut] if cut > 0 else t[:max_len]
    return t or "decision"


def same_login(a: str | None, b: str | None) -> bool:
    """Compara dos logins sin distinguir mayúsculas (08 §6.3).

    ``None`` o un valor que no sea cadena nunca coincide con nada: así un campo
    ausente o mal formado de un marcador no identifica a nadie.
    """
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return a.strip().casefold() == b.strip().casefold()  # los logins de GitHub son ASCII


def orcid_ok(orcid: str) -> bool:
    """Dígito de control ISO/IEC 7064 MOD 11-2 (03 §4.5).

    Devuelve ``False`` (en vez de lanzar) si, sin guiones, no quedan 15 dígitos
    ASCII y un dígito o ``X`` final.
    """
    d = orcid.replace("-", "") if isinstance(orcid, str) else ""
    if _ORCID_DIGITS.fullmatch(d) is None:
        return False
    total = 0
    for ch in d[:-1]:
        total = (total + int(ch)) * 2
    check = (12 - total % 11) % 11
    return d[-1] == ("X" if check == 10 else str(check))


def sha12_canonical(data: bytes) -> str:
    """Primeros 12 hex (minúsculas) del sha256 de la forma canónica de D-35 (m).

    Forma canónica: sin BOM UTF-8 inicial y con fin de línea LF (CRLF y CR
    sueltos pasan a LF). No añade ni quita el salto final.
    """
    if data.startswith(_BOM):
        data = data[len(_BOM) :]
    data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(data).hexdigest()[:12]
