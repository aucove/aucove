# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Catálogos ``es``/``en`` y traductor (01-arquitectura §9; 00-marco D-32 e, f).

Solo biblioteca estándar (``tomllib``): está en la ruta caliente de hooks.
Los catálogos viven en ``catalogs/<lang>/<área>.toml``, uno por área y paquete
dueño, y se fusionan al cargar con claves planas unidas por ``.``. Una clave
repetida entre archivos, una clave fuera de los prefijos de su área o un
marcador con atributos, índices o formato (REQ-ARCH-74) es ``CatalogError``.
El traductor nunca lanza: un marcador sin valor queda literal y los argumentos
sobrantes se ignoran.
"""

from __future__ import annotations

import functools
import re
import string
import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from .. import _meta

Lang = Literal["es", "en"]
LANGS: tuple[Lang, ...] = ("es", "en")
DEFAULT_LANG: Lang = "en"

# Área -> prefijos de clave admitidos en catalogs/<lang>/<área>.toml (D-32 f).
CATALOG_AREAS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "core": ("error.", "warning."),
        "config": ("config.", "finding."),
        "render": ("render.",),
        "cli": ("cli.",),
        "doctor": ("doctor.",),
        "roles": ("roles.", "finding."),
        "validate": ("validate.", "finding."),
        "convention": ("convention.", "finding."),
        "latex": ("latex.", "finding."),
        "scope": ("scope.", "finding."),
        "ci": ("ci.",),
        "base_report": ("base_report.",),
        "evidence": ("evidence.",),
        "forge": ("forge.",),
        "status": ("status.", "home."),
        "context": ("context.",),
        "mcp": ("mcp.",),
        "tasks": ("tasks.",),
        "review": ("review.",),
        "init": ("init.",),
        "metrics": ("metrics.",),
    }
)

CATALOG_DIR: Path = Path(__file__).resolve().parent / "catalogs"

_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$")
_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CODE_SPAN = re.compile(r"`([^`]*)`")
_FORMATTER = string.Formatter()


class CatalogError(Exception):
    """Catálogo mal formado: TOML inválido, clave repetida o fuera de su área, marcador inválido."""


def _flatten(table: Mapping[str, object], prefix: str, source: str, out: dict[str, str]) -> None:
    for raw_key, value in table.items():
        key = f"{prefix}{raw_key}"
        if isinstance(value, dict):
            _flatten(value, key + ".", source, out)
            continue
        if not isinstance(value, str):
            raise CatalogError(f"{source}: la clave {key!r} no es texto")
        if key in out:  # no ocurre dentro de un mismo TOML, pero se protege igual
            raise CatalogError(f"{source}: clave repetida {key!r}")
        out[key] = value


def _check_markers(text: str, key: str, source: str) -> None:
    """REQ-ARCH-74: solo marcadores ``{nombre}`` simples, sin atributos, índices ni formato."""
    try:
        parts = list(_FORMATTER.parse(text))
    except ValueError as exc:
        raise CatalogError(f"{source}: llaves mal formadas en {key!r}") from exc
    for _literal, field, spec, conversion in parts:
        if field is None:
            continue
        if not _NAME_PATTERN.match(field) or spec or conversion:
            raise CatalogError(f"{source}: marcador no admitido en {key!r}: {{{field}}}")


def _read_area(path: Path, area: str) -> dict[str, str]:
    source = path.as_posix()
    try:
        data = tomllib.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CatalogError(f"{source}: no se puede leer como TOML UTF-8") from exc
    flat: dict[str, str] = {}
    _flatten(data, "", source, flat)
    prefixes = CATALOG_AREAS[area]
    for key, text in flat.items():
        if not _KEY_PATTERN.match(key):
            raise CatalogError(f"{source}: clave no ASCII o mal formada {key!r}")
        if not key.startswith(prefixes):
            raise CatalogError(f"{source}: la clave {key!r} está fuera de los prefijos del área {area!r}")
        _check_markers(text, key, source)
    return flat


def load_catalog(lang: Lang, *, catalog_dir: Path | None = None) -> Mapping[str, str]:
    """Fusiona los catálogos por área de ``lang``. Un área ausente no aporta claves; nunca usa red."""
    if lang not in LANGS:
        raise CatalogError(f"idioma no admitido: {lang!r}")
    base = (CATALOG_DIR if catalog_dir is None else Path(catalog_dir)) / lang
    merged: dict[str, str] = {}
    owner: dict[str, str] = {}
    if not base.is_dir():
        return MappingProxyType(merged)
    for path in sorted(base.iterdir()):
        if path.is_dir() or path.suffix != ".toml":
            continue
        area = path.stem
        if area not in CATALOG_AREAS:
            raise CatalogError(f"{path.as_posix()}: área de catálogo desconocida {area!r}")
        for key, text in _read_area(path, area).items():
            if key in merged:
                raise CatalogError(f"{path.as_posix()}: la clave {key!r} ya está en {owner[key]}.toml")
            merged[key] = text
            owner[key] = area
    return MappingProxyType(merged)


def placeholders(text: str) -> frozenset[str]:
    """Nombres ``{x}`` de ``str.format`` en ``text``; vacío si las llaves están mal formadas."""
    try:
        return frozenset(field for _lit, field, _spec, _conv in _FORMATTER.parse(text) if field)
    except ValueError:
        return frozenset()


def code_spans(text: str) -> tuple[str, ...]:
    """Fragmentos entre acentos graves, en orden (REQ-ARCH-73)."""
    return tuple(_CODE_SPAN.findall(text))


def check_parity(*, catalog_dir: Path | None = None) -> list[str]:
    """Comprueba REQ-ARCH-72..74 entre ``es`` y ``en``; devuelve los problemas (vacío = bien)."""
    try:
        catalogs = {lang: load_catalog(lang, catalog_dir=catalog_dir) for lang in LANGS}
    except CatalogError as exc:
        return [str(exc)]
    es, en = catalogs["es"], catalogs["en"]
    problems: list[str] = []
    for key in sorted(set(es) - set(en)):
        problems.append(f"{key}: falta en en")
    for key in sorted(set(en) - set(es)):
        problems.append(f"{key}: falta en es")
    for key in sorted(set(es) & set(en)):
        if placeholders(es[key]) != placeholders(en[key]):
            problems.append(f"{key}: marcadores distintos entre es y en")
        if code_spans(es[key]) != code_spans(en[key]):
            problems.append(f"{key}: fragmentos entre acentos graves distintos entre es y en")
    return problems


def resolve_lang(*, cli: str | None, env: Mapping[str, str], project: str | None) -> Lang:
    """Idioma de los mensajes (REQ-ARCH-69 con --lang delante; D-32 e).

    Orden: ``--lang``, ``<PREFIJO>LANG``, ``project.yaml: language`` de la base y ``en``.
    Nunca usa la configuración regional del sistema.
    """
    for candidate in (cli, env.get(_meta.ENV_PREFIX + "LANG"), project):
        if candidate in LANGS:
            return candidate  # type: ignore[return-value]
    return DEFAULT_LANG


class _Missing(dict):  # type: ignore[type-arg]
    """Deja literal un marcador sin valor."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _format(text: str, kw: Mapping[str, object]) -> str:
    if "{" not in text and "}" not in text:
        return text
    try:
        return text.format_map(_Missing(kw))
    except Exception:  # llaves mal formadas, índices, etc.: el traductor nunca lanza
        return text


class Translator:
    """Traductor de un idioma con respaldo en ``en`` y, al final, en la propia clave."""

    __slots__ = ("lang", "_catalog_dir", "_catalogs", "_primary", "_fallback")

    def __init__(
        self,
        lang: Lang,
        *,
        catalog_dir: Path | None = None,
        catalogs: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        self.lang: Lang = lang if lang in LANGS else DEFAULT_LANG
        self._catalog_dir = catalog_dir
        self._catalogs = catalogs
        self._primary: Mapping[str, str] | None = None
        self._fallback: Mapping[str, str] | None = None

    def _load(self, lang: Lang) -> Mapping[str, str]:
        if self._catalogs is not None:
            return self._catalogs.get(lang, {})
        try:
            return load_catalog(lang, catalog_dir=self._catalog_dir)
        except Exception:
            # Un catálogo roto no debe tumbar la operación (ni la ruta de hooks):
            # se responde con las claves. La prueba de i18n lo detecta en CI.
            return {}

    def _lookup(self, key: str) -> str | None:
        if self._primary is None:
            self._primary = self._load(self.lang)
        text = self._primary.get(key)
        if text is None and self.lang != DEFAULT_LANG:
            if self._fallback is None:
                self._fallback = self._load(DEFAULT_LANG)
            text = self._fallback.get(key)
        return text

    def __call__(self, key: str, /, **kw: object) -> str:
        """Texto de ``key`` en ``lang``, luego en ``en`` y, si no, la propia clave."""
        try:
            text = self._lookup(key)
        except Exception:
            text = None
        if text is None:
            return key
        return _format(text, kw)

    def get(self, key: str, /, **kw: object) -> str | None:
        """Como ``__call__``, pero ``None`` si la clave no existe en ningún idioma."""
        try:
            text = self._lookup(key)
        except Exception:
            return None
        if text is None:
            return None
        return _format(text, kw)

    def plural(self, key: str, n: int, /, **kw: object) -> str:
        """``<key>.one`` si ``n == 1``; si no, ``<key>.other`` (regla válida en es y en)."""
        kw.setdefault("n", n)
        return self(f"{key}.one" if n == 1 else f"{key}.other", **kw)

    def __repr__(self) -> str:
        return f"Translator({self.lang!r})"


@functools.cache
def translator(lang: Lang) -> Translator:
    """Traductor del catálogo empaquetado, uno por idioma y proceso."""
    return Translator(lang)


def catalog_keys(lang: Lang) -> frozenset[str]:
    """Claves del catálogo empaquetado de ``lang``."""
    return frozenset(load_catalog(lang))
