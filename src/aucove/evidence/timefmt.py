# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Horas de la evidencia: UTC y la del centro de México (11-trazabilidad §8.4; D-45).

Las horas se guardan en UTC (RFC 3339 con ``Z``). Los reportes agregan la hora de
``America/Mexico_City`` (o la de ``--tz``) con ``zoneinfo``. En Windows la base de
zonas sale del paquete ``tzdata`` (extra ``evidence``); si no hay base, solo se
muestra UTC (01-arquitectura §4) y ``zone_status`` lo distingue de una zona que no
existe. Los textos de los reportes los arma quien llama, con el idioma del proyecto.
"""

from __future__ import annotations

import re
import zoneinfo
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

__all__ = [
    "DEFAULT_TZ",
    "Times",
    "ZoneStatus",
    "format_utc",
    "parse_utc",
    "times",
    "utc_now",
    "zone",
    "zone_status",
]

DEFAULT_TZ = "America/Mexico_City"

ZoneStatus = Literal["ok", "unavailable", "invalid"]

# RFC 3339 en UTC, con fracciones opcionales de hasta 6 dígitos (genTime de una TSA).
_UTC_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,6}))?Z"
)


def utc_now() -> datetime:
    """Hora actual en UTC, sin microsegundos."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def _aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("la hora debe llevar zona")
    return dt.astimezone(timezone.utc)


def format_utc(dt: datetime, *, fraction: bool = False) -> str:
    """``AAAA-MM-DDTHH:MM:SSZ``: RFC 3339 en UTC.

    Sin ``fraction``, las fracciones se truncan: es la forma de ``created_at`` del
    manifiesto. Con ``fraction``, se conservan los microsegundos que haya, sin ceros a la
    derecha (``...:01.25Z``): la forma de ``time_utc`` de una prueba RFC 3161 (``genTime``).
    ``ValueError`` con una hora sin zona.
    """
    u = _aware_utc(dt)
    text = f"{u.year:04d}-{u.month:02d}-{u.day:02d}T{u.hour:02d}:{u.minute:02d}:{u.second:02d}"
    if fraction and u.microsecond:
        text += "." + f"{u.microsecond:06d}".rstrip("0")
    return text + "Z"


def parse_utc(text: str) -> datetime:
    """Hora UTC de ``AAAA-MM-DDTHH:MM:SS[.ffffff]Z``; ``ValueError`` con otra forma o fecha imposible."""
    m = _UTC_RE.fullmatch(text) if isinstance(text, str) else None
    if m is None:
        raise ValueError(f"hora UTC inválida: {text!r}")
    year, month, day, hour, minute, second, frac = m.groups()
    micro = int((frac or "0").ljust(6, "0"))
    return datetime(
        int(year), int(month), int(day), int(hour), int(minute), int(second), micro, tzinfo=timezone.utc
    )


def _load(name: str) -> zoneinfo.ZoneInfo | None:
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError, UnicodeError):
        return None


def zone(name: str = DEFAULT_TZ) -> zoneinfo.ZoneInfo | None:
    """Zona ``name`` de la base de zonas; ``None`` si no hay base o la zona no existe."""
    if not isinstance(name, str) or not name:
        return None
    return _load(name)


def zone_status(name: str = DEFAULT_TZ) -> ZoneStatus:
    """``ok`` si la zona se carga; ``invalid`` si hay base pero no tiene esa zona (o el
    nombre no es válido); ``unavailable`` si no hay base de zonas (Windows sin ``tzdata``).
    """
    if zone(name) is not None:
        return "ok"
    return "invalid" if _load("UTC") is not None else "unavailable"


@dataclass(frozen=True)
class Times:
    """Una hora en UTC y, si hay base de zonas, en la zona pedida."""

    utc: datetime
    local: datetime | None
    zone: str | None

    @property
    def utc_text(self) -> str:
        """``AAAA-MM-DDTHH:MM:SSZ``."""
        return format_utc(self.utc)

    @property
    def local_text(self) -> str | None:
        """``AAAA-MM-DDTHH:MM:SS±HH:MM`` (ISO 8601 con el desfase); ``None`` sin zona."""
        return self.local.isoformat(timespec="seconds") if self.local is not None else None


def times(dt: datetime, tz: str = DEFAULT_TZ) -> Times:
    """``dt`` en UTC y en la zona ``tz``; sin base de zonas (o con una zona que no existe), solo UTC."""
    u = _aware_utc(dt)
    z = zone(tz)
    if z is None:
        return Times(utc=u, local=None, zone=None)
    return Times(utc=u, local=u.astimezone(z), zone=tz)
