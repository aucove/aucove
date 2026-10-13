# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Escáner de fuentes LaTeX que no ejecuta nada (02-convencion §4.6; 09-ci REQ-CI-24).

El texto se trata siempre como datos. ``scan`` hace una sola pasada lineal y
devuelve una copia «limpia» del texto, del mismo largo y con los mismos saltos
de línea, en la que los comentarios (``%`` no escapado hasta el fin de línea),
el contenido de los entornos literales (``verbatim``, ``Verbatim``,
``lstlisting``...) y el de ``\\verb<c>...<c>`` quedan en blanco. Sobre esa copia
se leen las secuencias de control con su nombre completo (``\\def`` no coincide
con ``\\definition``) y sus argumentos, y se buscan las secuencias ``^^``.

Tiempo acotado: el escáner no usa expresiones regulares con cuantificadores
anidados; cada expresión se aplica a cadenas de largo acotado (``MAX_ARG_CHARS``
o una línea) y los bucles consultan un ``Deadline``, que lanza ``ScanTimeout``
al vencer. Solo biblioteca estándar (capa ``domain``).
"""

from __future__ import annotations

import bisect
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field

MAX_ARG_CHARS = 4096  # largo máximo de un argumento {...} o [...] que se lee
_CHECK_EVERY = 2048  # pasos entre consultas al reloj

# Entornos cuyo contenido no se analiza (§4.6 y variantes con asterisco de fancyvrb).
VERBATIM_ENVS: frozenset[str] = frozenset(
    {
        "verbatim",
        "verbatim*",
        "Verbatim",
        "Verbatim*",
        "BVerbatim",
        "BVerbatim*",
        "LVerbatim",
        "LVerbatim*",
        "lstlisting",
    }
)
_INLINE_VERBATIM = frozenset({"verb", "lstinline"})

# Entornos donde no rige una oración por línea (§4.3), además de los literales.
NON_PROSE_ENVS: frozenset[str] = frozenset(
    {
        "equation",
        "equation*",
        "align",
        "align*",
        "alignat",
        "alignat*",
        "flalign",
        "flalign*",
        "gather",
        "gather*",
        "multline",
        "multline*",
        "eqnarray",
        "eqnarray*",
        "math",
        "displaymath",
        "split",
        "subequations",
        "tabular",
        "tabular*",
        "tabularx",
        "longtable",
        "array",
        "tikzpicture",
        "axis",
        "comment",
    }
)

# Abreviaturas tras las que un punto no cierra oración (REQ-CONV-27).
ABBREVIATIONS: tuple[str, ...] = (
    "e.g.",
    "i.e.",
    "et al.",
    "etc.",
    "vs.",
    "cf.",
    "Fig.",
    "Eq.",
    "Sec.",
    "Tab.",
    "p.",
    "pp.",
    "No.",
    "Dr.",
    "Sr.",
    "Sra.",
    "aprox.",
    "núm.",
    "pág.",
)

# Gramática de etiquetas de 02 §4.4; se aplica a cadenas de hasta MAX_LABEL caracteres.
LABEL_TYPES: tuple[str, ...] = ("sec", "fig", "tab", "eq", "lst", "alg", "thm", "itm", "fn")
LABEL_PATTERN = re.compile(
    r"^(sec|fig|tab|eq|lst|alg|thm|itm|fn):([a-z0-9]+(?:-[a-z0-9]+)*)(?::([a-z0-9]+(?:-[a-z0-9]+)*))?$"
)
SECTION_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_LABEL = 256


class ScanTimeout(Exception):
    """Se agotó el tiempo del análisis (REQ-CI-24: expresiones con tiempo acotado)."""


class Deadline:
    """Plazo de un análisis; ``check`` lanza ``ScanTimeout`` al vencer.

    Con ``seconds=None`` no vence nunca. ``clock`` es inyectable para las pruebas.
    """

    __slots__ = ("_clock", "_end", "_ticks")

    def __init__(self, seconds: float | None, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._end = None if seconds is None else clock() + seconds
        self._ticks = 0

    def check(self) -> None:
        """Lanza ``ScanTimeout`` si el plazo ya venció."""
        if self._end is not None and self._clock() > self._end:
            raise ScanTimeout

    def tick(self) -> None:
        """Como ``check``, pero consulta el reloj solo cada ``_CHECK_EVERY`` llamadas."""
        self._ticks += 1
        if self._ticks >= _CHECK_EVERY:
            self._ticks = 0
            self.check()

    @property
    def expired(self) -> bool:
        return self._end is not None and self._clock() > self._end


_NO_DEADLINE = Deadline(None)


@dataclass(frozen=True, slots=True)
class Command:
    """Secuencia de control con nombre de letras (``\\name``), fuera de comentarios y literales."""

    name: str  # sin la barra invertida
    offset: int  # posición de la barra invertida
    end: int  # posición siguiente al nombre
    line: int  # 1 = primera línea


@dataclass(frozen=True, slots=True)
class Args:
    """Argumentos leídos tras una secuencia de control."""

    star: bool
    options: tuple[str, ...]  # contenido de cada [...]
    groups: tuple[str, ...]  # contenido de cada {...} obligatorio leído
    braced: bool  # True si el primer argumento obligatorio iba entre llaves
    end: int
    complete: bool = True  # False si un grupo quedó sin cerrar o superó MAX_ARG_CHARS


@dataclass(frozen=True)
class GroupIndex:
    """Posiciones de los delimitadores no escapados de un texto, para leer grupos en O(log n).

    ``close`` asocia cada ``{`` con su ``}`` (``-1`` si no cierra); ``opens``,
    ``brackets`` y ``parens`` son las posiciones ordenadas de ``{``, ``]`` y ``)``.
    """

    close: Mapping[int, int]
    opens: Sequence[int]
    brackets: Sequence[int]
    parens: Sequence[int]


_GROUP_TOKEN = re.compile(r"\\.|[{}\])]", re.DOTALL)


def group_index(text: str, *, deadline: Deadline | None = None) -> GroupIndex:
    """Índice de delimitadores de ``text`` en una sola pasada lineal."""
    dl = deadline or _NO_DEADLINE
    close: dict[int, int] = {}
    stack: list[int] = []
    opens: list[int] = []
    brackets: list[int] = []
    parens: list[int] = []
    for m in _GROUP_TOKEN.finditer(text):
        dl.tick()
        c = m.group()
        if c[0] == "\\":
            continue  # carácter escapado: \{, \}, \] o \)
        pos = m.start()
        if c == "{":
            stack.append(pos)
            opens.append(pos)
        elif c == "}":
            if stack:
                close[stack.pop()] = pos
        elif c == "]":
            brackets.append(pos)
        else:
            parens.append(pos)
    for pos in stack:
        close[pos] = -1
    return GroupIndex(close=close, opens=opens, brackets=brackets, parens=parens)


@dataclass(frozen=True)
class Scan:
    """Resultado de ``scan``: texto limpio, secuencias de control y líneas con ``^^``."""

    text: str  # mismo largo y saltos que el original; comentarios y literales en blanco
    commands: tuple[Command, ...]
    carets: tuple[int, ...]  # líneas con ``^^`` fuera de comentarios y literales
    line_starts: tuple[int, ...]
    _index: list[GroupIndex] = field(default_factory=list, repr=False, compare=False)

    def index(self, deadline: Deadline | None = None) -> GroupIndex:
        """Índice de delimitadores del texto limpio, calculado una sola vez."""
        if not self._index:
            self._index.append(group_index(self.text, deadline=deadline))
        return self._index[0]

    def line_of(self, offset: int) -> int:
        """Línea (desde 1) de una posición del texto."""
        return bisect.bisect_right(self.line_starts, offset)

    def args(
        self,
        cmd: Command,
        *,
        groups: int = 1,
        options: bool = True,
        multi: bool = False,
        deadline: Deadline | None = None,
    ) -> Args:
        """Argumentos de ``cmd``: ``*``, ``[...]`` y hasta ``groups`` grupos ``{...}``.

        Con ``multi`` lee la forma de citas múltiples de biblatex
        (``\\cites(pre)(post)[a][b]{k1}[c]{k2}...``) y devuelve todos los grupos.
        """
        return read_args(
            self.text,
            cmd.end,
            groups=groups,
            options=options,
            multi=multi,
            deadline=deadline,
            index=self.index(deadline),
        )

    def lines(self) -> list[str]:
        """Líneas del texto limpio, sin el salto final."""
        return self.text.split("\n")

    def environments(self, *, deadline: Deadline | None = None) -> Iterator[tuple[str, Command]]:
        """``(nombre, comando)`` de cada ``\\begin{...}`` y ``\\end{...}`` (nombre con prefijo ``+``/``-``)."""
        dl = deadline or _NO_DEADLINE
        for cmd in self.commands:
            if cmd.name not in ("begin", "end"):
                continue
            dl.tick()
            a = self.args(cmd, groups=1, options=False, deadline=dl)
            if a.groups:
                sign = "+" if cmd.name == "begin" else "-"
                yield sign + a.groups[0].strip(), cmd


def _line_starts(text: str) -> tuple[int, ...]:
    starts = [0]
    find = text.find
    pos = find("\n")
    while pos >= 0:
        starts.append(pos + 1)
        pos = find("\n", pos + 1)
    return tuple(starts)


def blank(s: str) -> str:
    """``s`` con cada carácter salvo ``\\n`` reemplazado por un espacio."""
    return "\n".join(" " * len(part) for part in s.split("\n"))


def _is_letter(c: str, at_letter: bool, expl: bool) -> bool:
    if ("a" <= c <= "z") or ("A" <= c <= "Z"):
        return True
    if at_letter and c == "@":
        return True
    return expl and c in "_:"


_SPECIAL = re.compile(r"[\\%]")


def _skip_spaces(text: str, pos: int, limit: int) -> int:
    while pos < limit and text[pos] in " \t":
        pos += 1
    return pos


def scan(text: str, *, deadline: Deadline | None = None) -> Scan:
    """Analiza ``text`` sin ejecutar nada; ver el docstring del módulo."""
    dl = deadline or _NO_DEADLINE
    n = len(text)
    out: list[str] = []
    commands: list[Command] = []
    starts = _line_starts(text)
    at_letter = False
    expl = False
    pos = 0
    search = _SPECIAL.search
    while pos < n:
        dl.tick()
        m = search(text, pos)
        if m is None:
            out.append(text[pos:])
            break
        i = m.start()
        out.append(text[pos:i])
        if text[i] == "%":
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            pos = j
            continue
        # Barra invertida: palabra de control o símbolo de control.
        if i + 1 >= n:
            out.append("\\")
            pos = n
            break
        c = text[i + 1]
        if not _is_letter(c, at_letter, expl):
            out.append(text[i : i + 2])
            pos = i + 2
            continue
        j = i + 2
        while j < n and _is_letter(text[j], at_letter, expl):
            j += 1
        name = text[i + 1 : j]
        out.append(text[i:j])
        commands.append(Command(name=name, offset=i, end=j, line=bisect.bisect_right(starts, i)))
        pos = j
        if name == "makeatletter":
            at_letter = True
        elif name == "makeatother":
            at_letter = False
        elif name == "ExplSyntaxOn":
            expl = True
        elif name == "ExplSyntaxOff":
            expl = False
        elif name in _INLINE_VERBATIM:
            pos = _inline_verbatim(text, j, name, out)
        elif name == "begin":
            pos = _verbatim_env(text, j, out)
    clean = "".join(out)
    carets = _carets(clean, starts, dl)
    return Scan(text=clean, commands=tuple(commands), carets=carets, line_starts=starts)


def _line_end(text: str, pos: int) -> int:
    e = text.find("\n", pos)
    return len(text) if e < 0 else e


def _inline_verbatim(text: str, pos: int, name: str, out: list[str]) -> int:
    """``\\verb<c>...<c>`` y ``\\lstinline``: deja en blanco el delimitador y el contenido."""
    eol = _line_end(text, pos)
    k = pos
    if k < eol and text[k] == "*" and name == "verb":
        k += 1
    if name == "lstinline":
        k = _skip_spaces(text, k, eol)
        if k < eol and text[k] == "[":
            close = text.find("]", k, eol)
            k = eol if close < 0 else close + 1
    if k >= eol or text[k] in " \t":
        out.append(text[pos:k])
        return k
    delim = "}" if text[k] == "{" else text[k]
    close = text.find(delim, k + 1, eol)
    stop = eol if close < 0 else close + 1
    out.append(text[pos:k])
    out.append(" " * (stop - k))
    return stop


def _verbatim_env(text: str, pos: int, out: list[str]) -> int:
    """Tras ``\\begin``: si abre un entorno literal, deja su contenido en blanco hasta ``\\end{...}``."""
    eol = _line_end(text, pos)
    k = _skip_spaces(text, pos, eol)
    if k >= eol or text[k] != "{":
        return pos
    close = text.find("}", k + 1, min(eol, k + 66))
    if close < 0:
        return pos
    env = text[k + 1 : close].strip()
    if env not in VERBATIM_ENVS:
        return pos
    out.append(text[pos : close + 1])
    terminator = "\\end{" + env + "}"
    stop = text.find(terminator, close + 1)
    if stop < 0:
        stop = len(text)
    out.append(blank(text[close + 1 : stop]))
    return stop


def _carets(clean: str, starts: Sequence[int], dl: Deadline) -> tuple[int, ...]:
    lines: list[int] = []
    pos = clean.find("^^")
    while pos >= 0:
        dl.tick()
        line = bisect.bisect_right(starts, pos)
        if not lines or lines[-1] != line:
            lines.append(line)
        # Salta al final de la línea: basta un hallazgo por línea.
        eol = clean.find("\n", pos)
        if eol < 0:
            break
        pos = clean.find("^^", eol)
    return tuple(lines)


def _skip_ws(text: str, pos: int) -> int:
    """Salta espacios y a lo sumo un salto de línea (una línea en blanco es ``\\par``)."""
    n = len(text)
    newlines = 0
    while pos < n:
        c = text[pos]
        if c in " \t\r":
            pos += 1
        elif c == "\n":
            if newlines:
                return pos
            newlines += 1
            pos += 1
        else:
            break
    return pos


def _read_group(
    text: str, pos: int, opener: str, idx: GroupIndex, dl: Deadline
) -> tuple[str | None, int]:
    """Grupo que empieza en ``text[pos] == opener``; ``(None, pos)`` si no cierra.

    ``{...}`` es un grupo equilibrado. ``[...]`` y ``(...)`` terminan en el primer
    ``]`` o ``)`` que no está dentro de llaves. Cada caso se resuelve con
    búsquedas binarias en ``idx``, así que un grupo sin cerrar no se recorre.
    """
    limit = pos + 1 + MAX_ARG_CHARS  # el cierre debe quedar antes de esta posición
    if opener == "{":
        end = idx.close.get(pos, -1)
        if end < 0 or end >= limit:
            return None, pos
        return text[pos + 1 : end], end + 1
    closers = idx.brackets if opener == "[" else idx.parens
    k = pos + 1
    while k < limit:
        dl.tick()
        i = bisect.bisect_left(closers, k)
        end = closers[i] if i < len(closers) else len(text)
        j = bisect.bisect_left(idx.opens, k)
        brace = idx.opens[j] if j < len(idx.opens) else len(text)
        if end < brace:
            if end >= limit:
                break
            return text[pos + 1 : end], end + 1
        if brace >= limit:
            break
        after = idx.close.get(brace, -1)
        if after < 0:
            break  # una llave sin cerrar: el grupo no termina
        k = after + 1
    return None, pos


def read_args(
    text: str,
    pos: int,
    *,
    groups: int = 1,
    options: bool = True,
    multi: bool = False,
    deadline: Deadline | None = None,
    index: GroupIndex | None = None,
) -> Args:
    """Lee ``*``, ``[...]`` y grupos ``{...}`` desde ``pos`` (ver ``Scan.args``).

    ``index`` es el ``group_index`` de ``text``; sin él se calcula (O(n)).
    """
    dl = deadline or _NO_DEADLINE
    idx = index if index is not None else group_index(text, deadline=dl)
    n = len(text)
    k = _skip_ws(text, pos)
    star = False
    if k < n and text[k] == "*":
        star = True
        k = _skip_ws(text, k + 1)
    opts: list[str] = []
    found: list[str] = []
    complete = True
    braced = False
    if multi:
        # Notas globales de las citas múltiples: (pre)(post).
        for _ in range(2):
            if k < n and text[k] == "(":
                value, after = _read_group(text, k, "(", idx, dl)
                if value is None:
                    return Args(star, tuple(opts), tuple(found), braced, k, False)
                k = _skip_ws(text, after)
    while True:
        while options and k < n and text[k] == "[":
            value, after = _read_group(text, k, "[", idx, dl)
            if value is None:
                return Args(star, tuple(opts), tuple(found), braced, k, False)
            opts.append(value)
            k = _skip_ws(text, after)
        if (not multi and len(found) >= groups) or k >= n or text[k] != "{":
            break
        value, after = _read_group(text, k, "{", idx, dl)
        if value is None:
            complete = False
            break
        if not found:
            braced = True
        found.append(value)
        k = after
        if not multi and len(found) >= groups:
            break
        k = _skip_ws(text, k)
    return Args(star, tuple(opts), tuple(found), braced, k, complete)


def split_keys(arg: str) -> list[str]:
    """Claves separadas por comas, sin espacios alrededor y sin vacías."""
    return [part.strip() for part in arg.split(",") if part.strip()]


@dataclass(frozen=True, slots=True)
class Label:
    kind: str
    section: str
    name: str | None


def parse_label(text: str) -> Label | None:
    """Etiqueta de 02 §4.4 (``tipo:sección[:nombre]``); ``None`` si no cumple la gramática."""
    if len(text) > MAX_LABEL:
        return None
    m = LABEL_PATTERN.match(text)
    if m is None:
        return None
    return Label(kind=m.group(1), section=m.group(2), name=m.group(3))


def _mask_math(line: str, closer: str | None) -> tuple[str, str | None]:
    """Deja en blanco la matemática de una línea en una sola pasada.

    ``closer`` es el cierre pendiente de la línea anterior (``\\]`` o ``$$``); se
    devuelve el que queda abierto al final. La matemática en línea (``$...$`` y
    ``\\(...\\)``) no pasa a la línea siguiente.
    """
    out: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        c = line[i]
        if c == "\\" and i + 1 < n:
            pair = line[i : i + 2]
            if closer is None:
                if pair in ("\\[", "\\("):
                    closer = "\\]" if pair == "\\[" else "\\)"
                    out.append("  ")
                else:
                    out.append(pair)
            else:
                if pair == closer:
                    closer = None
                out.append("  ")
            i += 2
            continue
        if c == "$":
            double = line.startswith("$$", i)
            if closer is None:
                closer = "$$" if double else "$"
            elif closer == "$$" and double:
                closer = None
            elif closer == "$":
                closer = None
            else:
                double = False
            step = 2 if double else 1
            out.append(" " * step)
            i += step
            continue
        out.append(c if closer is None else " ")
        i += 1
    if closer in ("$", "\\)"):
        closer = None
    return "".join(out), closer


def prose_lines(sc: Scan, *, deadline: Deadline | None = None) -> Iterator[tuple[int, str]]:
    """``(línea, texto)`` de las líneas de prosa: fuera de matemática, tablas, TikZ y literales."""
    dl = deadline or _NO_DEADLINE
    skip_lines: set[int] = set()
    stack: list[tuple[str, int]] = []
    for env, cmd in sc.environments(deadline=dl):
        name = env[1:]
        if env[0] == "+":
            if name in NON_PROSE_ENVS or name in VERBATIM_ENVS:
                stack.append((name, cmd.line))
        elif stack and stack[-1][0] == name:
            opened = stack.pop()[1]
            if not stack:
                skip_lines.update(range(opened, cmd.line + 1))
    if stack:
        skip_lines.update(range(stack[0][1], len(sc.line_starts) + 1))
    closer: str | None = None
    for number, raw in enumerate(sc.lines(), start=1):
        dl.tick()
        if number in skip_lines:
            continue
        masked, closer = _mask_math(raw, closer)
        yield number, masked


def _ends_with_abbreviation(line: str, dot: int) -> bool:
    # Compara en su sitio, sin copiar el prefijo: el costo por candidato no depende
    # del largo de la línea (REQ-CI-24).
    end = dot + 1
    for abbr in ABBREVIATIONS:
        if line.endswith(abbr, 0, end):
            before = end - len(abbr) - 1
            if before < 0 or not line[before].isalpha():
                return True
    return False


# Candidato a fin de oración: un signo seguido de espacios o tabuladores. La expresión
# no tiene cuantificadores anidados y se aplica a una sola línea: tiempo lineal.
_SENTENCE_END = re.compile(r"[.!?][ \t]+")


def _iter_sentence_breaks(line: str, dl: Deadline) -> Iterator[int]:
    n = len(line)
    for m in _SENTENCE_END.finditer(line):
        dl.tick()
        k = m.end()
        if k >= n:
            continue
        nxt = line[k]
        if not (nxt.isupper() or nxt in "¿¡\\"):
            continue
        i = m.start()
        if line[i] == "." and _ends_with_abbreviation(line, i):
            continue
        yield i


def sentence_breaks(line: str, *, deadline: Deadline | None = None) -> list[int]:
    """Columnas (desde 0) de un ``[.!?]`` seguido de espacio y de mayúscula, ``¿``, ``¡`` o ``\\``.

    Es la heurística de REQ-CONV-27, con las abreviaturas de ``ABBREVIATIONS``. El
    tiempo es lineal en el largo de la línea y ``deadline`` se consulta en cada
    candidato.
    """
    return list(_iter_sentence_breaks(line, deadline or _NO_DEADLINE))


def first_sentence_break(line: str, *, deadline: Deadline | None = None) -> int | None:
    """La primera columna de ``sentence_breaks`` o ``None``; se detiene al encontrarla."""
    return next(_iter_sentence_breaks(line, deadline or _NO_DEADLINE), None)
