# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Compatibilidad entre el núcleo instalado y ``project.yaml`` (01-arquitectura §6.3).

Puro y solo con la biblioteca estándar: ``domain`` no puede importar ``packaging``
(01 §3.1 y ``tests/unit/test_layers.py``). Por eso este módulo implementa el
subconjunto de PEP 440 que admite ``project.schema.json`` en ``core``: los
operadores ``~=``, ``==``, ``!=``, ``<=``, ``>=``, ``<`` y ``>``, sin ``===``, sin
época ni versión local en el especificador y con ``.*`` solo en ``==`` y ``!=``.
La versión instalada se interpreta con la gramática completa de PEP 440.

Las comparaciones siguen a ``packaging.specifiers`` 26.3 (la de ``uv.lock``) con
``prereleases=True``: la versión instalada ya está en el sistema, y PEP 440 no
excluye entonces las prerreleases. ``tests/unit/test_compat.py`` compara este
módulo con ``packaging`` sobre muchos casos. En 26.3, ``<V`` y ``>V`` ya no
excluyen las pre y post de toda la release base de ``V``, como en 26.0, sino
solo la familia de ``V`` (véase :func:`_matches`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Literal

from .._meta import SUPPORTED_CONVENTIONS

CompatLevel = Literal["ok", "warning", "error"]

CONVENTION_UNSUPPORTED = "E_CONFIG_CONVENTION_UNSUPPORTED"
CORE_MISMATCH = "E_CONFIG_CORE_MISMATCH"


@dataclass(frozen=True, slots=True)
class Compat:
    """Resultado de :func:`check`: ``code`` es ``None`` salvo en ``level == 'error'``."""

    level: CompatLevel
    code: str | None


# Gramática de PEP 440 (versión instalada), con sus grafías alternativas.
_VERSION_RE = re.compile(
    r"""
    ^\s*v?
    (?:(?P<epoch>[0-9]+)!)?
    (?P<release>[0-9]+(?:\.[0-9]+)*)
    (?P<pre>[-_.]?(?P<pre_l>alpha|beta|preview|pre|a|b|c|rc)[-_.]?(?P<pre_n>[0-9]+)?)?
    (?P<post>(?:-(?P<post_n1>[0-9]+))|(?:[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n2>[0-9]+)?))?
    (?P<dev>[-_.]?dev[-_.]?(?P<dev_n>[0-9]+)?)?
    (?:\+(?P<local>[a-z0-9]+(?:[-_.][a-z0-9]+)*))?
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)

_PRE_LETTERS = {"a": "a", "alpha": "a", "b": "b", "beta": "b", "c": "rc", "rc": "rc", "pre": "rc", "preview": "rc"}
_PRE_RANK = {"a": 0, "b": 1, "rc": 2}

# Un especificador del esquema: operador y versión ya normalizada (sin época ni local).
_SPEC_RE = re.compile(r"^\s*(~=|==|!=|<=|>=|<|>)\s*(\S+?)\s*$")
_WILDCARD_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*\.\*$")
_OPERATORS = ("~=", "==", "!=", "<=", ">=", "<", ">")


@dataclass(frozen=True, slots=True)
class Version:
    """Versión PEP 440 ya normalizada."""

    epoch: int
    release: tuple[int, ...]
    pre: tuple[str, int] | None = None  # ('a' | 'b' | 'rc', n)
    post: int | None = None
    dev: int | None = None
    local: tuple[int | str, ...] | None = None

    @property
    def is_prerelease(self) -> bool:
        return self.pre is not None or self.dev is not None

    @property
    def is_postrelease(self) -> bool:
        return self.post is not None

    def public(self) -> Version:
        """La misma versión sin la parte local."""
        return Version(self.epoch, self.release, self.pre, self.post, self.dev, None)

    def base(self) -> Version:
        """Solo época y release (``base_version`` de packaging)."""
        return Version(self.epoch, self.release)

    def key(self) -> tuple[object, ...]:
        """Clave de orden de PEP 440 (la de ``packaging.version._cmpkey``)."""
        release = self.release
        while len(release) > 1 and release[-1] == 0:
            release = release[:-1]
        # Una dev sin pre ni post va antes que cualquier prerrelease de su release.
        if self.pre is None and self.post is None and self.dev is not None:
            pre: tuple[int, int, int] = (0, 0, 0)
        elif self.pre is None:
            pre = (2, 0, 0)
        else:
            pre = (1, _PRE_RANK[self.pre[0]], self.pre[1])
        post = (0, 0) if self.post is None else (1, self.post)
        dev = (1, 0) if self.dev is None else (0, self.dev)
        if self.local is None:
            local: tuple[tuple[int, int, str], ...] = ()
        else:
            local = tuple((1, p, "") if isinstance(p, int) else (0, 0, p) for p in self.local)
        return (self.epoch, release, pre, post, dev, local)


def parse_version(text: str) -> Version | None:
    """Versión de PEP 440, o ``None`` si ``text`` no lo es."""
    if not isinstance(text, str):
        return None
    m = _VERSION_RE.match(text)
    if m is None:
        return None
    pre: tuple[str, int] | None = None
    if m.group("pre_l"):
        pre = (_PRE_LETTERS[m.group("pre_l").lower()], int(m.group("pre_n") or 0))
    post: int | None = None
    if m.group("post"):
        post = int(m.group("post_n1") or m.group("post_n2") or 0)
    dev: int | None = None
    if m.group("dev"):
        dev = int(m.group("dev_n") or 0)
    local: tuple[int | str, ...] | None = None
    if m.group("local"):
        parts = re.split(r"[-_.]", m.group("local").lower())
        local = tuple(int(p) if p.isdigit() else p for p in parts)
    return Version(
        epoch=int(m.group("epoch") or 0),
        release=tuple(int(x) for x in m.group("release").split(".")),
        pre=pre,
        post=post,
        dev=dev,
        local=local,
    )


@dataclass(frozen=True, slots=True)
class Specifier:
    """Un especificador de ``core``; ``wildcard`` vale para ``==V.*`` y ``!=V.*``."""

    operator: str
    version: Version
    wildcard: bool = False


def parse_specifiers(text: str) -> tuple[Specifier, ...] | None:
    """Especificadores de ``core`` separados por comas, o ``None`` si alguno no es válido."""
    if not isinstance(text, str) or not text.strip():
        return None
    out: list[Specifier] = []
    for part in text.split(","):
        m = _SPEC_RE.match(part)
        if m is None:
            return None
        op, raw = m.group(1), m.group(2)
        if raw.endswith(".*"):
            if op not in ("==", "!=") or not _WILDCARD_RE.match(raw):
                return None
            release = tuple(int(x) for x in raw[:-2].split("."))
            out.append(Specifier(op, Version(0, release), wildcard=True))
            continue
        version = parse_version(raw)
        if version is None or version.local is not None or version.epoch != 0 or raw.lower().startswith("v"):
            return None
        if op == "~=" and len(version.release) < 2:
            return None
        out.append(Specifier(op, version))
    return tuple(out)


def _prefix_match(prospective: Version, epoch: int, release: tuple[int, ...]) -> bool:
    """``==release.*``: la release de ``prospective``, rellena con ceros, empieza por ``release``."""
    if prospective.epoch != epoch:
        return False
    padded = prospective.release + (0,) * max(0, len(release) - len(prospective.release))
    return padded[: len(release)] == release


def _matches(spec: Specifier, v: Version) -> bool:
    target = spec.version
    op = spec.operator
    if op == "~=":
        prefix = target.release[:-1]
        return v.public().key() >= target.key() and _prefix_match(v, target.epoch, prefix)
    if op in ("==", "!="):
        if spec.wildcard:
            equal = _prefix_match(v, target.epoch, target.release)
        else:
            equal = v.public().key() == target.key()
        return equal if op == "==" else not equal
    if op == "<=":
        return v.public().key() <= target.key()
    if op == ">=":
        return v.public().key() >= target.key()
    # packaging 26.3 evalúa ``<`` y ``>`` con rangos (``_ranges.standard_ranges``; «<V/>V carve
    # out V's family», en ``specifiers._fast_match``). 26.0 excluía de toda la release base de V
    # las pre (en ``<``) y las post y locales (en ``>``).
    if op == "<":
        # Si V no es prerrelease, el borde es V.dev0: <3.1 no admite 3.1.dev0 ni 3.1a1, y
        # <3.1.post1 admite 3.1a1 y 3.1.post0, pero no 3.1.post1.dev0.
        bound = target if target.is_prerelease else replace(target, dev=0)
        return v.key() < bound.key()
    # op == ">"
    if target.dev is not None or target.post is not None:
        # >V.devN y >V.postN empiezan en V.dev(N+1) y en V.post(N+1).dev0: solo quedan
        # fuera V y sus versiones locales (>3.1.dev0 admite 3.1+abc y 3.1.post0).
        return v.public().key() > target.key()
    # >V con V final o prerrelease: fuera V+local y V.postN, la misma release y la misma pre
    # (>3.1a1 no admite 3.1a1.post0, pero sí 3.1b1.post0, 3.1.post0 y 3.1+abc).
    family = v.base().key() == target.base().key() and v.pre == target.pre
    return v.key() > target.key() and not family


def satisfies(version: str, specifiers: str) -> bool:
    """``True`` si ``version`` cumple todos los especificadores de ``specifiers``.

    Devuelve ``False`` si alguna de las dos cadenas no es válida (falla cerrada).
    """
    v = parse_version(version)
    specs = parse_specifiers(specifiers)
    if v is None or specs is None:
        return False
    return all(_matches(s, v) for s in specs)


def same_version(a: str, b: str) -> bool:
    """Igualdad de PEP 440 (``0.1`` == ``0.1.0``); ``False`` si alguna no es válida."""
    va, vb = parse_version(a), parse_version(b)
    return va is not None and vb is not None and va.key() == vb.key()


def check(installed: str, convention: str, core: str, pin: str | None) -> Compat:
    """Algoritmo de 01 §6.3 sobre cadenas ya validadas por el esquema.

    Una versión o un rango que no se pueden interpretar dan ``E_CONFIG_CORE_MISMATCH``:
    la ruta de hooks no puede quedar abierta (REQ-ARCH-47).
    """
    if convention not in SUPPORTED_CONVENTIONS:
        return Compat(level="error", code=CONVENTION_UNSUPPORTED)
    if not satisfies(installed, core):
        return Compat(level="error", code=CORE_MISMATCH)
    if pin is not None and not same_version(installed, pin):
        return Compat(level="warning", code=None)  # dentro del rango: solo aviso
    return Compat(level="ok", code=None)


# core_pin (01 REQ-ARCH-45; 00-marco D-66): X.Y.Z con un sufijo opcional aN, bN o rcN.
PIN_VERSION = re.compile(r"(?P<major>[0-9]+)\.(?P<minor>[0-9]+)\.(?P<patch>[0-9]+)(?:(?:a|b|rc)[0-9]+)?")


def core_range(version: str) -> str:
    """``core`` de REQ-ARCH-44 para la versión instalada ``X.Y.Z`` (o una pre-release, 00-marco D-66)."""
    found = PIN_VERSION.fullmatch(version)
    if found is None:
        raise ValueError(f"versión sin la forma X.Y.Z[aN|bN|rcN]: {version!r}")
    major, minor = int(found.group("major")), int(found.group("minor"))
    upper = f"{major}.{minor + 1}" if major == 0 else f"{major + 1}"
    return f">={version},<{upper}"
