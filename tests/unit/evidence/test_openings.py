# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Compromisos con sal y aperturas (11-trazabilidad §8.3; ``evidence.openings``)."""

from __future__ import annotations

import hashlib
import json

import pytest

from aucove.evidence import openings as op
from aucove.evidence.openings import (
    ActorOpening,
    EntryOpening,
    Openings,
    OpeningsError,
    actor_commitment,
    blob_commitment,
    blob_hasher,
    blob_oid,
    check_actor,
    check_entry,
    load_openings,
    manifest_entries,
    parse_openings,
    path_commitment,
)
from tests.unit.evidence.support import validate

SALT = bytes(range(32))
OTHER = bytes(range(1, 33))


def test_formulas_match_the_specification() -> None:
    # 11 §8.3, escritas aparte: dominio ‖ 0x00 ‖ sal ‖ dato (y rol ‖ 0x00 en los actores)
    path = "sections/ana/café.md".encode()
    data = b"contenido\r\n"
    assert path_commitment(SALT, path) == hashlib.sha256(b"evidence.path.v1\x00" + SALT + path).hexdigest()
    assert blob_commitment(SALT, data) == hashlib.sha256(b"evidence.blob.v1\x00" + SALT + data).hexdigest()
    expected_a = hashlib.sha256(b"evidence.actor.v1\x00" + SALT + b"author\x00" + "Ana Pérez <a@x>".encode()).hexdigest()
    assert actor_commitment(SALT, "author", "Ana Pérez <a@x>") == expected_a


def test_domain_separation_same_data_gives_p_c_and_a_different() -> None:
    data = b"pr_author"
    p = path_commitment(SALT, data)
    c = blob_commitment(SALT, data)
    a = actor_commitment(SALT, "pr_author", data.decode())
    assert len({p, c, a}) == 3
    # la sal también cuenta: otra sal, otro compromiso
    assert path_commitment(OTHER, data) != p


def test_blob_hasher_accepts_the_content_in_parts() -> None:
    hasher = blob_hasher(SALT)
    for part in (b"uno ", b"dos ", b"tres"):
        hasher.update(part)
    assert hasher.hexdigest() == blob_commitment(SALT, b"uno dos tres")


@pytest.mark.parametrize("salt", [b"", b"x" * 31, b"x" * 33, "x" * 32])
def test_salts_must_have_32_bytes(salt: object) -> None:
    with pytest.raises(ValueError):
        path_commitment(salt, b"a")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        blob_commitment(salt, b"a")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        actor_commitment(salt, "author", "a")  # type: ignore[arg-type]


def test_actor_role_and_value_are_checked() -> None:
    with pytest.raises(ValueError):
        actor_commitment(SALT, "reviewer", "x")
    with pytest.raises(ValueError):
        actor_commitment(SALT, "author", "bad \udc80 surrogate")
    with pytest.raises(ValueError):
        path_commitment(SALT, b"")


def test_new_salt_has_32_random_bytes() -> None:
    salts = {op.new_salt() for _ in range(8)}
    assert len(salts) == 8 and all(len(s) == 32 for s in salts)


def test_blob_oid_is_git_hash_object() -> None:
    assert blob_oid(b"hello world\n") == "3b18e512dba79e4c8300dd08aeb37f8e728b8dad"
    assert blob_oid(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


def _sample() -> Openings:
    return Openings(
        manifest_sha256="ab" * 32,
        repository="org-x/repo.name",
        entries=(
            EntryOpening(path="café.txt", salt=SALT, blob="1" * 40, size=12),
            EntryOpening(path="borrado.txt", salt=OTHER, blob=None, size=None),
        ),
        actors=(ActorOpening(role="pr_author", value="ana-p", salt=bytes(32)),),
    )


def test_openings_roundtrip_keeps_nfd_paths() -> None:
    sample = _sample()
    data = sample.dumps()
    assert data.endswith(b"\n") and b"\r" not in data
    assert "café.txt".encode() in data  # tal cual, sin normalizar ni escapar
    loaded = load_openings(data)
    assert loaded == sample
    assert loaded.entries[0].path_bytes == b"cafe\xcc\x81.txt"
    assert validate("evidence-openings", json.loads(data)) == []


@pytest.mark.parametrize(
    ("mutate", "pointer"),
    [
        (lambda o: o.update(schema="x"), "/schema"),
        (lambda o: o.update(manifest_sha256="AB" * 32), "/manifest_sha256"),
        (lambda o: o.update(repository="sin-barra"), "/repository"),
        (lambda o: o["entries"][0].update(salt="00" * 31), "/entries/0/salt"),
        (lambda o: o["entries"][0].update(path=""), "/entries/0/path"),
        (lambda o: o["entries"][0].update(path="x" * 4097), "/entries/0/path"),
        (lambda o: o["entries"][1].update(blob="z" * 40), "/entries/1/blob"),
        (lambda o: o["entries"][0].update(size=True), "/entries/0/size"),
        (lambda o: o["actors"][0].update(r="reviewer"), "/actors/0/r"),
        (lambda o: o["actors"][0].update(value="x" * 321), "/actors/0/value"),
        (lambda o: o.update(entries={}), "/entries"),
    ],
)
def test_parse_openings_reports_the_pointer(mutate, pointer: str) -> None:  # type: ignore[no-untyped-def]
    obj = _sample().to_json()
    mutate(obj)
    with pytest.raises(OpeningsError) as info:
        parse_openings(obj)
    assert info.value.pointer == pointer


def test_salts_never_appear_in_a_repr() -> None:
    sample = _sample()
    text = repr(sample)  # REQ-OPS-65: un repr puede acabar en un log o en un mensaje
    for salt in (SALT, OTHER, bytes(32)):
        assert salt.hex() not in text and repr(salt) not in text
    assert "café.txt" in text  # el resto sí se ve


def test_load_openings_rejects_bad_bytes() -> None:
    for data in (b"\xff", b"{", b"[]"):
        with pytest.raises(OpeningsError):
            load_openings(data)


def test_load_openings_turns_hostile_json_into_openings_error() -> None:
    """Un entero de más de 4300 cifras (``ValueError`` de Python) o un anidamiento
    profundo son ``OpeningsError``, nunca una excepción suelta.

    El anidamiento falla al leer el JSON o al validarlo, según la versión de Python: en 3.11 el
    analizador en C de ``json`` corta en el límite de recursión; 3.14, que lo limita por la pila
    de C, lee 5000 niveles y el error lo da el esquema. Se prueban las dos rutas, también con el
    objeto que entrega 3.14.
    """
    obj = _sample().to_json()
    text = json.dumps(obj).replace('"size": 12', '"size": ' + "1" * 5000)
    assert "1" * 5000 in text
    with pytest.raises(OpeningsError) as info:
        load_openings(text.encode())
    assert info.value.pointer == ""
    for data in (b"[" * 100_000 + b"]" * 100_000, b'{"entries": ' + b"[" * 5000 + b"]" * 5000 + b"}"):
        with pytest.raises(OpeningsError) as info:
            load_openings(data)
        assert info.value.pointer in ("", "/schema")
    deep: list[object] = []
    for _ in range(5000):
        deep = [deep]
    with pytest.raises(OpeningsError) as info:
        parse_openings({"entries": deep})  # lo que lee 3.14
    assert info.value.pointer == "/schema"


def test_path_limit_is_4096_bytes_of_utf8_not_characters() -> None:
    """C-3 (11 §8.3): el tope de la ruta se mide en bytes UTF-8, los de git; 2049 «é» son 4098 bytes."""
    assert op.MAX_PATH_BYTES == 4096
    for path, ok in (("é" * 2048, True), ("x" * 4096, True), ("é" * 2049, False), ("x" * 4095 + "é", False)):
        obj = _sample().to_json()
        obj["entries"][0]["path"] = path
        if ok:
            assert parse_openings(obj).entries[0].path_bytes == path.encode("utf-8")
            assert validate("evidence-openings", obj) == []
        else:
            with pytest.raises(OpeningsError) as info:
                parse_openings(obj)
            assert info.value.pointer == "/entries/0/path"
    obj = _sample().to_json()
    obj["entries"][0]["path"] = "a\udc80b"  # sustituto suelto: no es UTF-8
    with pytest.raises(OpeningsError):
        parse_openings(obj)


def test_check_entry_and_actor() -> None:
    content = b"datos del archivo"
    entry = EntryOpening(path="a.txt", salt=SALT, blob=blob_oid(content), size=len(content))
    gone = EntryOpening(path="b.txt", salt=OTHER, blob=None, size=None)
    manifest = {
        "entries": [{"p": entry.p(), "c": entry.c(content)}, {"p": gone.p(), "c": None}],
        "actors": [{"r": "author", "a": actor_commitment(SALT, "author", "Ana <a@x>")}],
    }
    entries = manifest_entries(manifest)
    assert check_entry(entry, entries, content).content is True
    assert check_entry(entry, entries, content + b"!").content is False
    assert check_entry(entry, entries).content is None  # sin bytes no se compara
    assert check_entry(entry, entries).found is True
    assert check_entry(gone, entries).content is True  # borrado en los dos lados
    renamed = EntryOpening(path="A.txt", salt=SALT, blob=entry.blob, size=entry.size)
    assert check_entry(renamed, entries, content).found is False
    revived = EntryOpening(path="b.txt", salt=OTHER, blob="1" * 40, size=3)
    assert check_entry(revived, entries, b"abc").content is False  # el manifiesto dice borrado
    actors = manifest["actors"]
    assert check_actor(ActorOpening("author", "Ana <a@x>", SALT), actors)
    assert not check_actor(ActorOpening("committer", "Ana <a@x>", SALT), actors)
    assert not check_actor(ActorOpening("author", "Ana <b@x>", SALT), actors)
