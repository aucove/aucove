# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Presupuesto por respuesta: medida, ajuste y cursores (05 §7.1 y §7.3; REQ-OPS-31..36)."""

from __future__ import annotations

import base64
import json

import pytest

from aucove import _meta
from aucove.i18n import translator
from aucove.ops.fit import decode_cursor, encode_cursor, envelope_size, fit, snap_of
from aucove.ops.result import OpError, make_warning, ok

T = translator("es")


def _warning(budget: int) -> object:
    return make_warning("W_OUTPUT_TRUNCATED", T, details={"budget": budget, "trimmed": ["items"],
                                                         "omitted": {"items": 9999}})


def test_envelope_size_is_the_compact_serialization() -> None:
    data = {"a": "ñ" * 3, "b": [1, 2]}
    expected = json.dumps(ok("doctor", data).to_dict(), ensure_ascii=False, separators=(",", ":"))
    assert envelope_size("doctor", data) == len(expected)
    assert expected.startswith('{"schema":"' + _meta.RESULT_SCHEMA)


def test_fit_keeps_order_and_trims_from_the_end() -> None:
    items = [{"n": i, "text": "x" * 80} for i in range(100)]
    data: dict[str, object] = {"items": list(items), "other": [1]}
    budget = 3000
    result = fit("status", data, budget=budget, trim_order=["items"], truncated_warning=_warning(budget))  # type: ignore[arg-type]
    kept = data["items"]
    assert isinstance(kept, list) and kept == items[: len(kept)] and 0 < len(kept) < 100
    assert result.omitted == {"items": 100 - len(kept)} and result.trimmed == ["items"]
    assert envelope_size("status", data, [_warning(budget)]) <= budget  # type: ignore[list-item]
    data["items"] = items[: len(kept) + 1]
    assert envelope_size("status", data, [_warning(budget)]) > budget  # type: ignore[list-item]


def test_fit_does_nothing_when_it_fits() -> None:
    data: dict[str, object] = {"items": [1, 2, 3]}
    result = fit("status", data, budget=4000, trim_order=["items"], truncated_warning=_warning(4000))  # type: ignore[arg-type]
    assert result.omitted == {} and data["items"] == [1, 2, 3]


def test_fit_follows_the_trim_order_and_excludes_keys() -> None:
    data: dict[str, object] = {
        "first": ["a" * 100] * 30,
        "second": ["b" * 100] * 30,
        "summary_md": "z" * 50_000,
    }
    budget = 2500
    result = fit("context", data, budget=budget, trim_order=["first", "second"], truncated_warning=_warning(budget),  # type: ignore[arg-type]
                 exclude=["summary_md"])
    assert data["first"] == [] and 0 < len(data["second"]) < 30  # type: ignore[arg-type]
    assert result.trimmed == ["first", "second"]
    assert data["summary_md"] == "z" * 50_000
    # 05 §7.3: con la primera lista basta si recortarla ya alcanza; la segunda queda entera.
    data = {"first": ["a" * 100] * 30, "second": ["b" * 100] * 30}
    result = fit("context", data, budget=4000, trim_order=["first", "second"], truncated_warning=_warning(4000))  # type: ignore[arg-type]
    assert 0 < len(data["first"]) < 30 and len(data["second"]) == 30  # type: ignore[arg-type]
    assert result.trimmed == ["first"]


def test_cursor_round_trip_and_errors() -> None:
    snap = snap_of([3, 5, 8])
    assert len(snap) == 16 and snap != snap_of([3, 5])
    cursor = encode_cursor("status", "tasks", 40, snap)
    assert "=" not in cursor
    payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    assert payload == {"v": 1, "op": "status", "list": "tasks", "offset": 40, "snap": snap}
    assert decode_cursor(cursor, op="status", list_name="tasks", snap=snap) == 40
    with pytest.raises(OpError) as stale:
        decode_cursor(cursor, op="status", list_name="tasks", snap=snap_of([3, 5]))
    assert stale.value.code == "E_VALIDATION_CURSOR_STALE"
    for bad in ("", "%%%", "x" * 600, encode_cursor("validate", "tasks", 1, snap),
                encode_cursor("status", "reviews", 1, snap), encode_cursor("status", "tasks", -1, snap)):
        with pytest.raises(OpError) as info:
            decode_cursor(bad, op="status", list_name="tasks", snap=snap)
        assert info.value.code == "E_VALIDATION_INPUT"
    odd = base64.urlsafe_b64encode(b'{"v":1,"op":"status","list":"tasks","offset":true,"snap":"x"}').decode()
    with pytest.raises(OpError):
        decode_cursor(odd, op="status", list_name="tasks", snap="x")
