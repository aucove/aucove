# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""JSON canónico: perfil restringido de RFC 8785 (11-trazabilidad §8.3; ``evidence.canon``)."""

from __future__ import annotations

import sys

import pytest

from aucove.evidence import canon
from aucove.evidence.canon import MAX_SAFE_INTEGER, CanonicalError

# (valor, bytes canónicos esperados): vectores del perfil (sin flotantes ni claves no ASCII).
VECTORS: list[tuple[object, bytes]] = [
    (None, b"null"),
    (True, b"true"),
    (False, b"false"),
    (0, b"0"),
    (-7, b"-7"),
    (MAX_SAFE_INTEGER, b"9007199254740991"),
    (-MAX_SAFE_INTEGER, b"-9007199254740991"),
    ("", b'""'),
    ([], b"[]"),
    ({}, b"{}"),
    ((1, 2), b"[1,2]"),
    # orden de claves por punto de código (con claves ASCII, igual que por unidades UTF-16)
    ({"b": 1, "a": [True, None, "x"], "B": 0, "_": 2, "10": 3, "1": 4}, b'{"1":4,"10":3,"B":0,"_":2,"a":[true,null,"x"],"b":1}'),
    ({"z": {"y": [], "x": {}}}, b'{"z":{"x":{},"y":[]}}'),
    # escapes de ECMAScript: cortos donde existen, \u00xx en minúsculas para los demás controles
    ("\b\t\n\f\r", b'"\\b\\t\\n\\f\\r"'),
    ("\x00\x01\x1f", b'"\\u0000\\u0001\\u001f"'),
    ('"\\/', b'"\\"\\\\/"'),
    # sin escapar: DEL, no ASCII, separadores de línea Unicode y caracteres fuera del BMP
    ("\x7f", b'"\x7f"'),
    ("ñandú €", '"ñandú €"'.encode()),
    ("  ", '"  "'.encode()),
    ("\U0001f600", '"\U0001f600"'.encode()),
    # NFD se queda en NFD
    ("café", '"café"'.encode()),
]


@pytest.mark.parametrize(("value", "expected"), VECTORS)
def test_dumps_vectors(value: object, expected: bytes) -> None:
    assert canon.dumps(value) == expected
    assert canon.is_canonical(expected)


def test_rfc8785_string_vector() -> None:
    # RFC 8785 §3.2.2.2: la cadena del ejemplo, leída y vuelta a escribir.
    source = '{ "string": "\\u20ac$\\u000F\\u000aA\'\\u0042\\u0022\\u005c\\\\\\"\\/" }'
    assert canon.dumps(canon.loads(source)) == '{"string":"€$\\u000f\\nA\'B\\"\\\\\\\\\\"/"}'.encode()


@pytest.mark.parametrize("value", [1.0, 0.5, -0.0, float("nan"), float("inf"), {"a": [1, 2.5]}])
def test_floats_are_rejected(value: object) -> None:
    with pytest.raises(CanonicalError) as info:
        canon.dumps(value)
    assert info.value.reason == "float"


@pytest.mark.parametrize("value", [2**53, -(2**53), 10**30])
def test_integers_beyond_2_53_are_rejected(value: int) -> None:
    with pytest.raises(CanonicalError) as info:
        canon.dumps({"n": value})
    assert info.value.reason == "int_range"
    assert info.value.pointer == "/n"


@pytest.mark.parametrize("key", ["ñ", "€", "\U0001f600", "café"])
def test_non_ascii_keys_are_rejected(key: str) -> None:
    with pytest.raises(CanonicalError) as info:
        canon.dumps({"ok": {key: 1}})
    assert info.value.reason == "key_not_ascii"
    assert info.value.pointer.startswith("/ok/")


def test_other_violations_are_rejected() -> None:
    cases: list[tuple[object, str]] = [
        ({1: "x"}, "key_type"),
        ("\ud800", "surrogate"),
        ({"a": "x\udfff"}, "surrogate"),
        (b"bytes", "type"),
        ({"s": {1, 2}}, "type"),
    ]
    for value, reason in cases:
        with pytest.raises(CanonicalError) as info:
            canon.dumps(value)
        assert info.value.reason == reason, value


def test_pointer_escapes_tilde_and_slash() -> None:
    with pytest.raises(CanonicalError) as info:
        canon.dumps({"a/b": {"c~d": [0, 1.5]}})
    assert info.value.pointer == "/a~1b/c~0d/1"


def test_bool_is_not_an_integer() -> None:
    assert canon.dumps([True, 1, False, 0]) == b"[true,1,false,0]"


def test_loads_rejects_profile_violations() -> None:
    cases: list[tuple[bytes, str]] = [
        (b'{"a":1.0}', "float"),
        (b'{"a":1e3}', "float"),
        (b"[NaN]", "float"),
        (b"[-Infinity]", "float"),
        (b"9007199254740992", "int_range"),
        (b"1" * 400, "int_range"),
        (b'{"a":1,"a":2}', "duplicate_key"),
        ('{"ñ":1}'.encode(), "key_not_ascii"),
        (b'{"\\u00f1":1}', "key_not_ascii"),
        (b'["\\ud800"]', "surrogate"),
        (b'{"a":"\xff"}', "encoding"),
        (b"\xef\xbb\xbf{}", "syntax"),  # BOM
        (b"{", "syntax"),
        (b"[" * 100_000, "syntax"),
    ]
    for data, reason in cases:
        with pytest.raises(CanonicalError) as info:
            canon.loads(data)
        assert info.value.reason == reason, data[:20]


def test_a_recursion_error_while_walking_the_value_is_syntax(monkeypatch: pytest.MonkeyPatch) -> None:
    """``json`` puede leer un anidamiento que el recorrido de ``dumps`` ya no alcanza
    a recorrer; ese ``RecursionError`` también es ``CanonicalError("syntax")``."""

    def deep(_value: object) -> bytes:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(canon, "dumps", deep)
    with pytest.raises(CanonicalError) as info:
        canon.loads(b"[[[]]]")
    assert info.value.reason == "syntax"
    assert canon.is_canonical(b"[[[]]]") is False


def _stack_depth() -> int:
    frame, depth = sys._getframe(), 0
    while frame is not None:
        frame, depth = frame.f_back, depth + 1
    return depth


def test_nesting_near_the_recursion_limit_never_escapes() -> None:
    """r1: alrededor del límite de recursión (al leer o al recorrer) solo hay valores o ``syntax``."""
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(_stack_depth() + 150)  # límite bajo: la ventana entre json y dumps queda a la vista
    try:
        reasons = set()
        for depth in range(1, 200):
            data = b"[" * depth + b"]" * depth
            try:
                assert canon.loads(data) is not None
            except CanonicalError as exc:
                reasons.add(exc.reason)
            assert canon.is_canonical(data) in (True, False)
    finally:
        sys.setrecursionlimit(limit)
    assert reasons == {"syntax"}


@pytest.mark.parametrize(
    "data",
    [
        b'{"a": 1}',  # espacio
        b'{"a":1}\n',  # salto de línea final
        b'{"b":1,"a":2}',  # claves sin ordenar
        b'["\\u0041"]',  # escape innecesario
        b'["\\u001F"]',  # hex en mayúsculas
        b'["\\/"]',  # escape innecesario de '/'
        b"-0",
        b"01",
        b"1.0",
    ],
)
def test_is_canonical_rejects_variants(data: bytes) -> None:
    assert not canon.is_canonical(data)


def test_roundtrip_of_a_manifest_shaped_value() -> None:
    value = {
        "schema": "https://example.invalid/schemas/evidence-manifest/v1",
        "commits": ["a" * 40, "b" * 40],
        "entries": [{"p": "0" * 64, "c": None}, {"p": "f" * 64, "c": "1" * 64}],
        "repository_id": 123456789,
        "base": None,
    }
    data = canon.dumps(value)
    assert b" " not in data and not data.endswith(b"\n")
    assert canon.loads(data) == value
    assert canon.is_canonical(data)
    assert canon.dumps(canon.loads(data)) == data
