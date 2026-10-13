# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Horas de la evidencia en UTC y en la del centro de México (11-trazabilidad §8.4)."""

from __future__ import annotations

import sys
import zoneinfo
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone

import pytest

from aucove.evidence import timefmt

NOON_UTC = datetime(2026, 10, 6, 18, 0, 0, tzinfo=timezone.utc)


def test_format_and_parse_utc() -> None:
    assert timefmt.format_utc(NOON_UTC.replace(microsecond=987654)) == "2026-10-06T18:00:00Z"
    assert timefmt.format_utc(NOON_UTC.replace(microsecond=250000), fraction=True) == "2026-10-06T18:00:00.25Z"
    assert timefmt.format_utc(NOON_UTC, fraction=True) == "2026-10-06T18:00:00Z"
    precise = NOON_UTC.replace(microsecond=1)
    assert timefmt.parse_utc(timefmt.format_utc(precise, fraction=True)) == precise
    cdmx = timezone(timedelta(hours=-6))
    assert timefmt.format_utc(datetime(2026, 10, 6, 12, 0, tzinfo=cdmx)) == "2026-10-06T18:00:00Z"
    assert timefmt.parse_utc("2026-10-06T18:00:00Z") == NOON_UTC
    assert timefmt.parse_utc("2026-10-06T18:00:00.5Z") == NOON_UTC.replace(microsecond=500000)
    for bad in ("2026-10-06T18:00:00", "2026-10-06 18:00:00Z", "2026-13-06T18:00:00Z", "2026-10-06T18:00:00.1234567Z"):
        with pytest.raises(ValueError):
            timefmt.parse_utc(bad)
    with pytest.raises(ValueError):
        timefmt.format_utc(datetime(2026, 10, 6, 18, 0))  # sin zona


def test_utc_now_is_aware_and_without_microseconds() -> None:
    now = timefmt.utc_now()
    assert now.tzinfo is not None and now.utcoffset() == timedelta(0) and now.microsecond == 0


def test_times_in_mexico_city() -> None:
    if timefmt.zone_status() != "ok":
        pytest.skip("sin base de zonas horarias")
    t = timefmt.times(NOON_UTC)
    assert t.utc_text == "2026-10-06T18:00:00Z"
    assert t.zone == timefmt.DEFAULT_TZ
    assert t.local_text == "2026-10-06T12:00:00-06:00"  # sin horario de verano desde 2022
    other = timefmt.times(NOON_UTC, "Europe/Madrid")
    assert other.local_text == "2026-10-06T20:00:00+02:00"


def test_unknown_zone_gives_only_utc() -> None:
    if timefmt.zone_status() != "ok":
        pytest.skip("sin base de zonas horarias")
    assert timefmt.zone_status("Marte/Olympus_Mons") == "invalid"
    assert timefmt.zone_status("../etc/passwd") == "invalid"
    t = timefmt.times(NOON_UTC, "Marte/Olympus_Mons")
    assert t.local is None and t.local_text is None and t.utc_text == "2026-10-06T18:00:00Z"


@pytest.fixture
def no_tz_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Sin base de zonas: ni rutas del sistema ni el paquete ``tzdata`` (Windows sin el extra)."""
    zoneinfo.ZoneInfo.clear_cache()
    monkeypatch.setitem(sys.modules, "tzdata", None)  # import tzdata -> ImportError
    for name in [n for n in sys.modules if n.startswith("tzdata.")]:
        monkeypatch.delitem(sys.modules, name)
    zoneinfo.reset_tzpath(to=[])
    try:
        yield
    finally:
        zoneinfo.reset_tzpath()
        zoneinfo.ZoneInfo.clear_cache()


def test_without_tz_database_only_utc(no_tz_database: None) -> None:
    assert timefmt.zone() is None
    assert timefmt.zone_status() == "unavailable"
    t = timefmt.times(NOON_UTC)
    assert t.utc_text == "2026-10-06T18:00:00Z"
    assert t.local is None and t.zone is None and t.local_text is None
