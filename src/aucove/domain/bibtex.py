# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Lector mínimo de archivos ``.bib`` (02-convencion §5).

Lee entradas ``@tipo{clave, campo = valor, ...}`` (también con paréntesis) y
devuelve su tipo, clave, línea y campos, sin interpretar macros ni ejecutar
nada. El texto fuera de las entradas es comentario, igual que las líneas que
empiezan con ``%``. ``@comment``, ``@preamble`` y ``@string`` se saltan.

Tiempo lineal: un valor sin cerrar termina en la siguiente línea que empieza con
``@tipo{`` o ``@tipo(``, donde se retoma la lectura, y los bucles consultan un
``Deadline``. Solo biblioteca estándar (capa ``domain``).
"""

from __future__ import annotations

import bisect
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from .latex import Deadline

ErrorKind = Literal["syntax", "missing-key", "unterminated"]
SKIPPED_TYPES = frozenset({"comment", "preamble", "string"})
_NAME_STOP = frozenset(' \t\r\n={}(),"#%')


@dataclass(frozen=True, slots=True)
class BibEntry:
    """Una entrada bibliográfica."""

    entry_type: str  # en minúsculas
    key: str
    line: int
    fields: Mapping[str, str] = field(default_factory=dict)  # nombre en minúsculas -> valor tal cual
    field_lines: Mapping[str, int] = field(default_factory=dict)

    def canonical(self) -> str:
        """Forma normalizada para comparar entradas (tipo y campos, espacios colapsados)."""
        parts = [self.entry_type]
        for name in sorted(self.fields):
            parts.append(f"{name}={' '.join(self.fields[name].split())}")
        return "\n".join(parts)


@dataclass(frozen=True, slots=True)
class BibError:
    """Problema de sintaxis en una línea."""

    line: int
    kind: ErrorKind


@dataclass(frozen=True)
class BibFile:
    """Entradas y errores de un ``.bib``."""

    entries: tuple[BibEntry, ...]
    errors: tuple[BibError, ...]

    def keys(self) -> frozenset[str]:
        return frozenset(e.key for e in self.entries)

    def by_key(self) -> dict[str, BibEntry]:
        """Clave -> primera entrada con esa clave (biber usa la primera, 02 §5.5)."""
        out: dict[str, BibEntry] = {}
        for e in self.entries:
            out.setdefault(e.key, e)
        return out


class _Unterminated(Exception):
    pass


class _Syntax(Exception):
    pass


class _Reader:
    def __init__(self, text: str, deadline: Deadline | None) -> None:
        self.text = text
        self.n = len(text)
        self.dl = deadline or Deadline(None)
        starts = [0]
        pos = text.find("\n")
        while pos >= 0:
            starts.append(pos + 1)
            pos = text.find("\n", pos + 1)
        self.starts = starts
        self.entries: list[BibEntry] = []
        self.errors: list[BibError] = []

    def line_of(self, offset: int) -> int:
        return bisect.bisect_right(self.starts, offset)

    # -- utilidades -----------------------------------------------------------

    def _is_entry_start(self, pos: int) -> bool:
        """``@tipo{`` o ``@tipo(`` al principio de una línea (frontera de recuperación)."""
        text, n = self.text, self.n
        if pos >= n or text[pos] != "@" or (pos > 0 and text[pos - 1] != "\n"):
            return False
        k = pos + 1
        while k < n and text[k].isascii() and text[k].isalpha():
            k += 1
        if k == pos + 1:
            return False
        while k < n and text[k] in " \t":
            k += 1
        return k < n and text[k] in "{("

    def _next_entry_start(self, pos: int) -> int:
        k = self.text.find("\n@", pos)
        while k >= 0:
            self.dl.tick()
            if self._is_entry_start(k + 1):
                return k + 1
            k = self.text.find("\n@", k + 1)
        return self.n

    def _skip_ws(self, pos: int) -> int:
        text, n = self.text, self.n
        while pos < n:
            c = text[pos]
            if c in " \t\r\n":
                pos += 1
            elif c == "%":
                e = text.find("\n", pos)
                pos = n if e < 0 else e + 1
            else:
                break
        return pos

    def _in_comment_line(self, pos: int) -> bool:
        start = self.starts[self.line_of(pos) - 1]
        return self.text[start:pos].lstrip(" \t").startswith("%")

    # -- valores --------------------------------------------------------------

    def _braced(self, pos: int) -> int:
        """Grupo ``{...}`` equilibrado desde ``pos``; devuelve la posición siguiente."""
        text, n = self.text, self.n
        depth = 0
        k = pos
        while k < n:
            self.dl.tick()
            c = text[k]
            if c == "\\":
                k += 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return k + 1
            elif c == "\n" and self._is_entry_start(k + 1):
                raise _Unterminated
            k += 1
        raise _Unterminated

    def _quoted(self, pos: int) -> int:
        text, n = self.text, self.n
        depth = 0
        k = pos + 1
        while k < n:
            self.dl.tick()
            c = text[k]
            if c == "\\":
                k += 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth = max(0, depth - 1)
            elif c == '"' and depth == 0:
                return k + 1
            elif c == "\n" and self._is_entry_start(k + 1):
                raise _Unterminated
            k += 1
        raise _Unterminated

    def _value(self, pos: int, closer: str) -> tuple[str, int]:
        """Valor de un campo (partes unidas por ``#``); devuelve ``(texto, fin)``."""
        text, n = self.text, self.n
        begin = pos
        while True:
            pos = self._skip_ws(pos)
            if pos >= n:
                raise _Unterminated
            c = text[pos]
            if c == "{":
                pos = self._braced(pos)
            elif c == '"':
                pos = self._quoted(pos)
            else:
                k = pos
                while k < n and text[k] not in _NAME_STOP:
                    k += 1
                if k == pos:
                    raise _Syntax
                pos = k
            after = self._skip_ws(pos)
            if after < n and text[after] == "#":
                pos = after + 1
                continue
            return text[begin:pos].strip(), after

    # -- entradas -------------------------------------------------------------

    def _entry(self, at: int) -> int:
        """Lee la entrada que empieza en ``text[at] == '@'``; devuelve dónde seguir."""
        text, n = self.text, self.n
        k = at + 1
        while k < n and text[k] in " \t":
            k += 1
        t0 = k
        while k < n and text[k].isascii() and text[k].isalpha():
            k += 1
        entry_type = text[t0:k].lower()
        if not entry_type:
            raise _Syntax
        k = self._skip_ws(k)
        if k >= n or text[k] not in "{(":
            raise _Syntax
        opener = text[k]
        closer = "}" if opener == "{" else ")"
        if entry_type in SKIPPED_TYPES:
            if opener == "{":
                return self._braced(k)
            end = text.find(")", k)
            if end < 0:
                raise _Unterminated
            return end + 1
        k = self._skip_ws(k + 1)
        key_start = k
        while k < n and text[k] not in ",\n\r\t " and text[k] != closer:
            k += 1
        key = text[key_start:k]
        k = self._skip_ws(k)
        if not key or k >= n or text[k] not in (",", closer):
            self.errors.append(BibError(self.line_of(at), "missing-key"))
            raise _Syntax
        fields: dict[str, str] = {}
        lines: dict[str, int] = {}
        while True:
            self.dl.tick()
            if k >= n:
                raise _Unterminated
            if text[k] == closer:
                break
            if text[k] == ",":
                k = self._skip_ws(k + 1)
                continue
            name_start = k
            while k < n and text[k] not in _NAME_STOP:
                k += 1
            name = text[name_start:k].lower()
            if not name:
                raise _Syntax
            k = self._skip_ws(k)
            if k >= n or text[k] != "=":
                raise _Syntax
            value, k = self._value(k + 1, closer)
            if k < n and text[k] not in (",", closer):
                raise _Syntax  # falta la coma entre campos
            if name not in fields:
                fields[name] = value
                lines[name] = self.line_of(name_start)
        self.entries.append(
            BibEntry(
                entry_type=entry_type,
                key=key,
                line=self.line_of(at),
                fields=MappingProxyType(fields),
                field_lines=MappingProxyType(lines),
            )
        )
        return k + 1

    def run(self, max_entries: int) -> BibFile:
        pos = 0
        while len(self.entries) < max_entries:
            self.dl.tick()
            at = self.text.find("@", pos)
            if at < 0:
                break
            if self._in_comment_line(at):
                e = self.text.find("\n", at)
                pos = self.n if e < 0 else e + 1
                continue
            errors_before = len(self.errors)
            try:
                pos = self._entry(at)
            except _Unterminated:
                self.errors.append(BibError(self.line_of(at), "unterminated"))
                pos = self._next_entry_start(at + 1)
            except _Syntax:
                if len(self.errors) == errors_before:
                    self.errors.append(BibError(self.line_of(at), "syntax"))
                pos = self._next_entry_start(at + 1)
        return BibFile(entries=tuple(self.entries), errors=tuple(self.errors))


def parse_bib(text: str, *, deadline: Deadline | None = None, max_entries: int = 200_000) -> BibFile:
    """Entradas y errores de ``text`` (ver el docstring del módulo)."""
    return _Reader(text, deadline).run(max_entries)
