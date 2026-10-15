# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Llaves públicas de la firma del proyecto (``domain.signing``; 00-marco D-72; 03-esquemas REQ-SCHEMA-96).

La llave y la huella de referencia salen de ``ssh-keygen`` (``ssh-keygen -l -f``), así que la
prueba no necesita la herramienta. Los blobs de las demás pruebas se arman con ``key_blob``:
el formato no exige que los 32 bytes sean un punto de la curva.
"""

from __future__ import annotations

import base64
import struct
from datetime import datetime, timedelta, timezone

import pytest

from aucove import _meta
from aucove.domain import signing
from aucove.domain.signing import EvidenceKey, KeyProblem, PublicKeyError

# Una llave de ssh-keygen y la huella que da `ssh-keygen -l -f`.
SSH_KEYGEN_PUBLIC = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHO4TeimWDqbJtD9DZ0jEQ7osipbPsIQKXvEpl+4J8Ve prueba"
SSH_KEYGEN_FINGERPRINT = "SHA256:8jmj+t4ewK6CHVVPTPUNQTmLHKs6vyRBp9mxvEH7WXY"
T0 = datetime(2026, 2, 1, tzinfo=timezone.utc)


def line(raw: bytes, comment: str = "") -> str:
    text = f"{signing.KEY_TYPE} {base64.b64encode(signing.key_blob(raw)).decode('ascii')}"
    return f"{text} {comment}" if comment else text


def entry(key_id: str, raw: bytes, since: str = "2026-02-01T00:00:00Z", until: str | None = None) -> dict[str, str]:
    item = {"id": key_id, "public_key": line(raw), "since": since}
    if until is not None:
        item["until"] = until
    return item


A, B, C = b"\x01" * 32, b"\x02" * 32, b"\x03" * 32


def test_constants_follow_the_name_of_the_framework() -> None:
    assert signing.NAMESPACE == f"{_meta.NAME}-evidence"
    assert (signing.KEY_TYPE, signing.FORMAT, signing.MODES) == ("ssh-ed25519", "sshsig", ("ci", "admin", "none"))


def test_the_fingerprint_is_the_one_of_ssh_keygen() -> None:
    blob = signing.parse_public_key(SSH_KEYGEN_PUBLIC)
    assert signing.fingerprint(blob) == SSH_KEYGEN_FINGERPRINT
    raw = signing.raw_key(blob)
    assert len(raw) == 32 and signing.key_blob(raw) == blob
    key = EvidenceKey("ci-2026", blob, T0)
    assert key.fingerprint == SSH_KEYGEN_FINGERPRINT
    assert key.public_key == SSH_KEYGEN_PUBLIC.rsplit(" ", 1)[0]  # sin el comentario


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAAAgQC", "type"),  # otro tipo
        ("ecdsa-sha2-nistp256 AAAA", "type"),
        ("sk-ssh-ed25519@openssh.com AAAA", "type"),  # FIDO: v0.2 (PA-113)
        ("ssh-ed25519", "invalid"),  # sin datos
        ("ssh-ed25519 no-es-base64!", "invalid"),
        ("ssh-ed25519 " + line(A).split()[1][:-4], "invalid"),  # recortada
        (line(A) + " ", "invalid"),  # comentario vacío
        (line(A, "x" * 201), "invalid"),  # comentario demasiado largo
        (line(A) + "\nssh-ed25519 AAAA", "invalid"),  # dos líneas
        (" " + line(A), "type"),
        (None, "invalid"),
        (42, "invalid"),
    ],
)
def test_bad_public_keys(text: object, reason: str) -> None:
    with pytest.raises(PublicKeyError) as info:
        signing.parse_public_key(text)
    assert info.value.reason == reason


def test_a_blob_of_another_type_or_with_extra_bytes_is_rejected() -> None:
    kind = b"ssh-rsa"
    rsa = struct.pack(">I", len(kind)) + kind + struct.pack(">I", 32) + A
    with pytest.raises(PublicKeyError) as info:
        signing.raw_key(rsa)
    assert info.value.reason == "type"
    for blob in (signing.key_blob(A) + b"\x00", signing.key_blob(A)[:-1], b"", b"\x00\x00\x00"):
        with pytest.raises(PublicKeyError) as info:
            signing.raw_key(blob)
        assert info.value.reason == "invalid"
    with pytest.raises(PublicKeyError):
        signing.key_blob(b"\x01" * 31)
    # el mismo blob con otra codificación (relleno de más) no es la misma línea
    noncanonical = "ssh-ed25519 " + base64.b64encode(signing.key_blob(A)).decode("ascii") + "=="
    with pytest.raises(PublicKeyError):
        signing.parse_public_key(noncanonical)


def test_a_comment_is_allowed_and_ignored() -> None:
    assert signing.parse_public_key(line(A, "ana@laptop 2026")) == signing.parse_public_key(line(A))


@pytest.mark.parametrize("text", ["2026-10-14T00:00:00Z", "1999-01-02T03:04:05Z"])
def test_times_are_utc_without_fractions(text: str) -> None:
    when = signing.parse_time(text)
    assert when.tzinfo is timezone.utc and signing.format_time(when) == text


@pytest.mark.parametrize(
    "text",
    ["2026-10-14", "2026-10-14T00:00:00", "2026-10-14T00:00:00.5Z", "2026-10-14T00:00:00+00:00",
     "2026-13-01T00:00:00Z", "2026-10-14 00:00:00Z", None, 20261014],
)
def test_bad_times(text: object) -> None:
    with pytest.raises(ValueError):
        signing.parse_time(text)


def test_format_time_converts_to_utc() -> None:
    local = datetime(2026, 10, 14, 6, 0, tzinfo=timezone(timedelta(hours=-6)))
    assert signing.format_time(local) == "2026-10-14T12:00:00Z"


def test_a_key_is_valid_from_since_and_before_until() -> None:
    key = EvidenceKey("ci-2026", signing.key_blob(A), T0, T0 + timedelta(days=30))
    assert not key.valid_at(T0 - timedelta(seconds=1))
    assert key.valid_at(T0) and key.valid_at(T0 + timedelta(days=29))
    assert not key.valid_at(T0 + timedelta(days=30))  # until no se incluye
    forever = EvidenceKey("ci-2026", signing.key_blob(A), T0)
    assert forever.valid_at(T0 + timedelta(days=3650))


def test_parse_keys_gives_the_keys_in_order() -> None:
    keys, problems = signing.parse_keys([
        entry("ci-2025", A, "2025-01-01T00:00:00Z", "2026-02-01T00:00:00Z"),
        entry("ci-2026", B),
    ])
    assert problems == ()
    assert [(k.id, k.blob, k.since, k.until) for k in keys] == [
        ("ci-2025", signing.key_blob(A), datetime(2025, 1, 1, tzinfo=timezone.utc), T0),
        ("ci-2026", signing.key_blob(B), T0, None),
    ]
    assert signing.current_keys(keys, T0) == (keys[1],)  # la anterior dejó de valer en T0
    assert signing.current_keys(keys, T0 - timedelta(days=1)) == (keys[0],)
    assert signing.find_key(keys, signing.key_blob(B)) is keys[1]
    assert signing.find_key(keys, signing.key_blob(C)) is None


@pytest.mark.parametrize(
    ("item", "problem"),
    [
        ({"id": "ci-2026", "public_key": line(A), "since": "2026-02-01T00:00:00Z", "extra": 1}, KeyProblem(1, "", "invalid")),
        ("ci-2026", KeyProblem(1, "", "invalid")),
        ({"id": "CI 2026", "public_key": line(A), "since": "2026-02-01T00:00:00Z"}, KeyProblem(1, "id", "invalid")),
        ({"id": "-ci", "public_key": line(A), "since": "2026-02-01T00:00:00Z"}, KeyProblem(1, "id", "invalid")),
        ({"id": "x" * 33, "public_key": line(A), "since": "2026-02-01T00:00:00Z"}, KeyProblem(1, "id", "invalid")),
        ({"id": "ci", "public_key": "ssh-rsa AAAA", "since": "2026-02-01T00:00:00Z"}, KeyProblem(1, "public_key", "type")),
        ({"id": "ci", "public_key": "ssh-ed25519 AAAA", "since": "2026-02-01T00:00:00Z"}, KeyProblem(1, "public_key", "invalid")),
        ({"id": "ci", "public_key": line(A)}, KeyProblem(1, "since", "invalid")),
        ({"id": "ci", "public_key": line(A), "since": "2026-02-01"}, KeyProblem(1, "since", "invalid")),
        ({"id": "ci", "public_key": line(A), "since": "2026-02-01T00:00:00Z", "until": "mañana"}, KeyProblem(1, "until", "invalid")),
        (entry("ci", A, until="2026-02-01T00:00:00Z"), KeyProblem(1, "until", "until_not_after_since")),
        (entry("ci", A, until="2026-01-01T00:00:00Z"), KeyProblem(1, "until", "until_not_after_since")),
        (entry("ok-2026", C), KeyProblem(1, "id", "duplicate_id")),
        (entry("otra", B), KeyProblem(1, "public_key", "duplicate_key")),
    ],
)
def test_parse_keys_problems(item: object, problem: KeyProblem) -> None:
    """Cada problema de REQ-SCHEMA-96, con su puntero; la llave con problemas no entra."""
    first = entry("ok-2026", B)
    keys, problems = signing.parse_keys([first, item])
    assert problems == (problem,)
    assert [k.id for k in keys] == ["ok-2026"]
    assert problem.pointer == "/1" + (f"/{problem.field}" if problem.field else "")


def test_parse_keys_rejects_what_is_not_a_list_and_too_many() -> None:
    for items in (None, "ci", b"ci", {"id": "ci"}, 7):
        assert signing.parse_keys(items) == ((), (KeyProblem(None, "", "invalid"),))
    many = [entry(f"k{i}", bytes([i + 1]) * 32) for i in range(signing.MAX_KEYS + 1)]
    assert signing.parse_keys(many) == ((), (KeyProblem(None, "", "too_many"),))
    keys, problems = signing.parse_keys(many[: signing.MAX_KEYS])
    assert len(keys) == signing.MAX_KEYS and problems == ()
    assert KeyProblem(None, "", "too_many").pointer == ""
    assert signing.parse_keys([]) == ((), ())


def test_a_null_until_is_like_no_until() -> None:
    item = entry("ci", A)
    item["until"] = None  # type: ignore[assignment]
    keys, problems = signing.parse_keys([item])
    assert problems == () and keys[0].until is None
