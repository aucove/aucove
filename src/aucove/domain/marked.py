# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Texto marcado: saneamiento, recorte y delimitadores de datos (05 §7.2 y §8; 10-seguridad §3).

Módulo puro (REQ-ARCH-18): no genera el nonce ni lee el reloj; quien renderiza para un
modelo pasa el nonce (``secrets.token_hex(4)``, REQ-SEC-5) y lo regenera si
``nonce_clashes`` lo encuentra en el contenido.

- ``sanitize``: REQ-OPS-38 y REQ-SEC-6. NFC; quita los caracteres invisibles de IL01-IL04 y
  las secuencias de dos o más selectores de variación; quita los comentarios HTML
  (``<!-- ... -->``, también sin cerrar, para que un marcador falso no sobreviva); reemplaza
  los controles, salvo ``\\n`` y ``\\t``, por U+FFFD. Los invisibles se quitan antes que los
  comentarios: así ``<!-\\u200b-`` no esconde un comentario.
- ``clip_text``: REQ-OPS-34, por puntos de código y con el marcador
  `` [...truncated <N> chars]``.
- ``marked``: el objeto ``marked_text`` de REQ-OPS-37.
- ``data_block``: la gramática exacta de REQ-SEC-5.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final, Literal

__all__ = [
    "SOURCE_MAX",
    "TITLE_MAX",
    "Sanitized",
    "SanitizeTally",
    "Trust",
    "clip_text",
    "data_block",
    "marked",
    "nonce_clashes",
    "safe_source",
    "sanitize",
]

Trust = Literal["rules", "reviewed", "untrusted"]
TRUSTS: Final[frozenset[str]] = frozenset({"rules", "reviewed", "untrusted"})

TITLE_MAX: Final[int] = 120  # títulos de Issue, PR y decisión (05 §7.2)
SOURCE_MAX: Final[int] = 200  # maxLength de marked_text.source

# REQ-OPS-38 (IL01-IL04 de 10-seguridad §3.6.2): se eliminan.
_INVISIBLE_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x180E, 0x180E),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x2064),
    (0x2066, 0x2069),
    (0xFEFF, 0xFEFF),
    (0xE0000, 0xE007F),
    (0xE0100, 0xE01EF),
)
_INVISIBLE: Final[re.Pattern[str]] = re.compile(
    "[" + "".join(f"\\U{a:08x}-\\U{b:08x}" for a, b in _INVISIBLE_RANGES) + "]"
)
# Dos o más selectores de variación seguidos (uno solo es legítimo, p. ej. en emojis).
_VS_RUN: Final[re.Pattern[str]] = re.compile("[\ufe00-\ufe0f]{2,}")
# Comentario HTML completo o sin cerrar hasta el final del texto.
_HTML_COMMENT: Final[re.Pattern[str]] = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)
_SOURCE_BAD: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._#/:@+~-]")
_REPLACEMENT: Final[str] = "\ufffd"
_MAX_SOURCES: Final[int] = 20  # fuentes que se nombran en los details del aviso


@dataclass(frozen=True, slots=True)
class Sanitized:
    """Resultado de ``sanitize``: texto limpio y conteos de cada cambio."""

    text: str
    invisible: int  # caracteres invisibles y selectores de variación quitados
    comments: int  # comentarios HTML quitados
    controls: int  # controles reemplazados por U+FFFD
    normalized: bool  # NFC cambió el texto

    @property
    def changed(self) -> bool:
        return bool(self.invisible or self.comments or self.controls or self.normalized)


@dataclass
class SanitizeTally:
    """Suma de los conteos de varias llamadas, para los ``details`` de
    ``W_VALIDATION_UNTRUSTED_SANITIZED`` (REQ-OPS-38, REQ-SEC-6)."""

    texts: int = 0
    invisible: int = 0
    comments: int = 0
    controls: int = 0
    normalized: int = 0
    sources: list[str] = field(default_factory=list)

    def add(self, source: str, result: Sanitized) -> None:
        if not result.changed:
            return
        self.texts += 1
        self.invisible += result.invisible
        self.comments += result.comments
        self.controls += result.controls
        self.normalized += int(result.normalized)
        if len(self.sources) < _MAX_SOURCES and source not in self.sources:
            self.sources.append(source)

    def details(self) -> dict[str, object] | None:
        """``details`` del aviso; ``None`` si no se cambió ningún texto."""
        if not self.texts:
            return None
        return {
            "texts": self.texts,
            "invisible": self.invisible,
            "html_comments": self.comments,
            "controls": self.controls,
            "normalized": self.normalized,
            "sources": list(self.sources),
        }


def _is_control(ch: str) -> bool:
    return ch not in "\n\t" and unicodedata.category(ch) == "Cc"


def sanitize(text: str) -> Sanitized:
    """Saneamiento único de REQ-OPS-38 sobre ``text``."""
    if not isinstance(text, str):
        raise TypeError("text must be a str")
    nfc = unicodedata.normalize("NFC", text)
    normalized = nfc != text
    out, invisible = _INVISIBLE.subn("", nfc)
    out, runs = _VS_RUN.subn("", out)
    out, comments = _HTML_COMMENT.subn("", out)
    controls = sum(1 for ch in out if _is_control(ch))
    if controls:
        out = "".join(_REPLACEMENT if _is_control(ch) else ch for ch in out)
    final = unicodedata.normalize("NFC", out)  # quitar caracteres puede dejar algo componible
    return Sanitized(final, invisible + runs, comments, controls, normalized or final != out)


def clip_text(text: str, limit: int) -> tuple[str, bool]:
    """Recorta ``text`` a ``limit`` puntos de código con el marcador de REQ-OPS-34.

    Devuelve ``(texto, recortado)``. ``N`` es el número de puntos de código quitados del
    original; si ni el marcador cabe, se corta sin él.
    """
    if limit < 0:
        raise ValueError("limit must be >= 0")
    if len(text) <= limit:
        return text, False
    keep = max(0, limit - len(f" [...truncated {len(text)} chars]"))
    marker = f" [...truncated {len(text) - keep} chars]"
    if len(marker) > limit:
        return text[:limit], True
    return text[:keep] + marker, True


def safe_source(source: str) -> str:
    """``source`` apto para el objeto y para la línea ``<<<DATA``: ASCII seguro y ≤ 200."""
    cleaned = _SOURCE_BAD.sub("_", source)[:SOURCE_MAX]
    return cleaned or "_"


def marked(
    trust: Trust,
    source: str,
    text: str,
    *,
    limit: int | None = None,
    tally: SanitizeTally | None = None,
) -> dict[str, object]:
    """Objeto ``marked_text`` (REQ-OPS-37): saneado y, con ``limit``, recortado.

    Con ``trust`` ``rules`` el texto también se sanea: el saneamiento no cambia un texto
    normal y así ningún ``marked_text`` lleva caracteres invisibles.
    """
    if trust not in TRUSTS:
        raise ValueError(f"trust inválido: {trust!r}")
    src = safe_source(source)
    clean = sanitize(text)
    if tally is not None:
        tally.add(src, clean)
    body, truncated = clip_text(clean.text, limit) if limit is not None else (clean.text, False)
    out: dict[str, object] = {"trust": trust, "source": src, "text": body, "truncated": truncated}
    if clean.changed:
        out["sanitized"] = True
    return out


def nonce_clashes(nonce: str, contents: Iterable[str]) -> bool:
    """``True`` si ``nonce`` aparece en alguno de los textos (REQ-SEC-5: hay que regenerarlo)."""
    return any(nonce in c for c in contents)


def data_block(text: str, *, trust: Trust, source: str, nonce: str) -> str:
    """Bloque de REQ-SEC-5: ``<<<DATA <nonce> trust=<t> source=<s>>>>``, contenido y ``<<<END``.

    El contenido ya debe venir saneado (``sanitize``); aquí solo se cambia ``<<<`` por
    ``‹‹‹`` (U+2039), para que no pueda abrir ni cerrar un bloque. ``ValueError`` si el nonce
    aparece en el contenido o no es de 8 hexadecimales.
    """
    if trust not in ("reviewed", "untrusted"):
        raise ValueError("solo reviewed y untrusted van entre delimitadores (REQ-OPS-40)")
    if not re.fullmatch(r"[0-9a-f]{8}", nonce):
        raise ValueError("nonce inválido")
    body = text.replace("<<<", "\u2039\u2039\u2039")
    if nonce in body:
        raise ValueError("el nonce aparece en el contenido")
    return f"<<<DATA {nonce} trust={trust} source={safe_source(source)}>>>\n{body}\n<<<END {nonce}>>>"

