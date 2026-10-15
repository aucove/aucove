# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Firma de OpenSSH (SSHSIG) de la firma del proyecto (``evidence.sshsig``; 11-trazabilidad §8.10; D-72).

El vector de referencia lo firmó ``ssh-keygen -Y sign`` con el espacio de nombres
``<nombre>-evidence``, así que la verificación se prueba sin la herramienta. Con
``ssh-keygen`` en el PATH se prueba además la interoperación en los dos sentidos y que la
firma de Python es la misma, byte a byte (Ed25519 no usa azar). Ninguna llave privada vive
en el repositorio: las de las pruebas salen de una semilla o de ``ssh-keygen`` en una carpeta
temporal.
"""

from __future__ import annotations

import base64
import hashlib
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("cryptography", reason="falta el extra evidence (el CI lo instala)")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from aucove.domain import signing  # noqa: E402
from aucove.evidence import sshsig  # noqa: E402
from aucove.evidence.sshsig import SignatureError, SigningError  # noqa: E402
from tests.unit.test_signing import SSH_KEYGEN_FINGERPRINT, SSH_KEYGEN_PUBLIC  # noqa: E402

# `ssh-keygen -Y sign -f <llave de SSH_KEYGEN_PUBLIC> -n <nombre>-evidence` de este mensaje.
MESSAGE = b"linea 1\nlinea 2\n"
SSH_KEYGEN_SIGNATURE = (
    b"-----BEGIN SSH SIGNATURE-----\n"
    b"U1NIU0lHAAAAAQAAADMAAAALc3NoLWVkMjU1MTkAAAAgc7hN6KZYOpsm0P0NnSMRDuiyKl\n"
    b"s+whApe8SmX7gnxV4AAAAPYXVjb3ZlLWV2aWRlbmNlAAAAAAAAAAZzaGE1MTIAAABTAAAA\n"
    b"C3NzaC1lZDI1NTE5AAAAQPqPfRe18kTAIJJwvPdCSvaxvufYcOhM1Ut4WoN4vr998W2Wnh\n"
    b"AocjeSjq9q2tcqbcRyhobP12CkOSRt/Ck7mAo=\n"
    b"-----END SSH SIGNATURE-----\n"
)


def ed25519(seed: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(seed.encode("utf-8")).digest())


def openssh_private(key: object) -> bytes:
    return key.private_bytes(  # type: ignore[attr-defined, no-any-return]
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
    )


def private(seed: str = "prueba") -> sshsig.PrivateKey:
    return sshsig.load_private_key(openssh_private(ed25519(seed)))


def _string(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def blob(
    *,
    magic: bytes = b"SSHSIG",
    version: int = 1,
    public: bytes | None = None,
    namespace: bytes = signing.NAMESPACE.encode("utf-8"),
    reserved: bytes = b"",
    hash_name: bytes = b"sha512",
    sig_type: bytes = b"ssh-ed25519",
    raw_signature: bytes = b"\x00" * 64,
    extra: bytes = b"",
) -> bytes:
    """Un blob SSHSIG con cada campo a elección (para las formas mal hechas)."""
    key = public if public is not None else signing.key_blob(b"\x01" * 32)
    return (magic + struct.pack(">I", version) + _string(key) + _string(namespace) + _string(reserved)
            + _string(hash_name) + _string(_string(sig_type) + _string(raw_signature)) + extra)


# -- verificación ---------------------------------------------------------------------------


def test_verifies_the_signature_of_ssh_keygen() -> None:
    got = sshsig.verify(SSH_KEYGEN_SIGNATURE, MESSAGE)
    assert got.public_key == signing.parse_public_key(SSH_KEYGEN_PUBLIC)
    assert (got.fingerprint, got.namespace, got.hash) == (SSH_KEYGEN_FINGERPRINT, signing.NAMESPACE, "sha512")
    # con fines de línea de Windows y sin el salto final, también
    assert sshsig.verify(SSH_KEYGEN_SIGNATURE.replace(b"\n", b"\r\n"), MESSAGE).fingerprint == SSH_KEYGEN_FINGERPRINT
    assert sshsig.verify(SSH_KEYGEN_SIGNATURE.rstrip(b"\n"), MESSAGE).fingerprint == SSH_KEYGEN_FINGERPRINT


def test_armor_wraps_like_ssh_keygen() -> None:
    lines = SSH_KEYGEN_SIGNATURE.decode("ascii").splitlines()
    raw = base64.b64decode("".join(lines[1:-1]))
    assert sshsig.armor(raw) == SSH_KEYGEN_SIGNATURE
    assert all(len(line) <= 70 for line in lines)


@pytest.mark.parametrize(
    "message", [MESSAGE + b"\n", MESSAGE.replace(b"\n", b"\r\n"), MESSAGE[:-1], b"", b"linea 1\nlinea 3\n"]
)
def test_another_message_does_not_verify(message: bytes) -> None:
    with pytest.raises(SignatureError) as info:
        sshsig.verify(SSH_KEYGEN_SIGNATURE, message)
    assert info.value.reason == "bad_signature"


def test_a_changed_byte_of_the_signature_does_not_verify() -> None:
    lines = SSH_KEYGEN_SIGNATURE.split(b"\n")
    last = bytearray(lines[-3])  # la última línea del cuerpo: dentro de la firma
    last[5] = ord("A") if last[5] != ord("A") else ord("B")
    lines[-3] = bytes(last)
    with pytest.raises(SignatureError) as info:
        sshsig.verify(b"\n".join(lines), MESSAGE)
    assert info.value.reason == "bad_signature"


def test_another_namespace_is_rejected() -> None:
    """Una firma de git (``-n git``) u otra herramienta no vale como firma del proyecto."""
    key = private()
    other = key.sign(MESSAGE, namespace="git")
    with pytest.raises(SignatureError) as info:
        sshsig.verify(other, MESSAGE)
    assert info.value.reason == "namespace"
    assert sshsig.verify(other, MESSAGE, namespace="git").namespace == "git"


def test_sign_and_verify_with_sha512_and_accept_sha256() -> None:
    key = private()
    armored = key.sign(MESSAGE)
    got = sshsig.verify(armored, MESSAGE)
    assert (got.public_key, got.hash, got.fingerprint) == (key.public_key, "sha512", key.fingerprint)
    # una firma con sha256 (la que hacen otras herramientas) también se acepta
    signed = (b"SSHSIG" + _string(signing.NAMESPACE.encode()) + _string(b"") + _string(b"sha256")
              + _string(hashlib.sha256(MESSAGE).digest()))
    raw_sig = ed25519("prueba").sign(signed)
    sha256 = sshsig.armor(blob(public=key.public_key, hash_name=b"sha256", raw_signature=raw_sig))
    assert sshsig.verify(sha256, MESSAGE).hash == "sha256"


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (sshsig.armor(blob(magic=b"SSHSIH")), "invalid"),
        (sshsig.armor(blob(version=2)), "invalid"),
        (sshsig.armor(blob(extra=b"\x00")), "invalid"),  # bytes de más
        (sshsig.armor(blob()[:-10]), "invalid"),  # cortada
        (sshsig.armor(blob(raw_signature=b"\x00" * 63)), "invalid"),
        (sshsig.armor(blob(public=signing.key_blob(b"\x01" * 32) + b"\x00")), "invalid"),
        (sshsig.armor(blob(namespace=b"\xff\xfe")), "invalid"),
        (sshsig.armor(blob(hash_name=b"md5")), "unsupported"),
        (sshsig.armor(blob(sig_type=b"rsa-sha2-512")), "unsupported"),
        (sshsig.armor(blob(public=_string(b"ssh-rsa") + _string(b"\x01" * 32))), "unsupported"),
        (b"-----BEGIN SSH SIGNATURE-----\nno es base64!\n-----END SSH SIGNATURE-----\n", "invalid"),
        (SSH_KEYGEN_SIGNATURE.replace(b"BEGIN SSH SIGNATURE", b"BEGIN PGP SIGNATURE"), "invalid"),
        (SSH_KEYGEN_SIGNATURE.replace(b"-----END SSH SIGNATURE-----\n", b""), "invalid"),
        (b"-----BEGIN SSH SIGNATURE-----\n-----END SSH SIGNATURE-----\n", "invalid"),
        ("firma".encode("utf-16"), "invalid"),
        (b"\xc3\xb1" + SSH_KEYGEN_SIGNATURE, "invalid"),
        (b" " * (sshsig.MAX_SIGNATURE_BYTES + 1), "invalid"),
        ("texto", "invalid"),
    ],
)
def test_malformed_signatures(data: object, reason: str) -> None:
    with pytest.raises(SignatureError) as info:
        sshsig.parse(data)  # type: ignore[arg-type]
    assert info.value.reason == reason
    with pytest.raises(SignatureError):
        sshsig.verify(data, MESSAGE)  # type: ignore[arg-type]


def test_without_cryptography_the_reason_is_missing_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin el extra, ni se verifica ni se firma (REQ-ARCH-99): un motivo estable, no un ImportError."""
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.asymmetric.ed25519", None)
    with pytest.raises(SignatureError) as info:
        sshsig.verify(SSH_KEYGEN_SIGNATURE, MESSAGE)
    assert info.value.reason == "missing_extra"
    with pytest.raises(SigningError) as signing_info:
        sshsig.load_private_key(b"-----BEGIN OPENSSH PRIVATE KEY-----\nx\n-----END OPENSSH PRIVATE KEY-----\n")
    assert signing_info.value.reason == "missing_extra"


# -- llave privada (ci seal; 09-ci REQ-CI-61) --------------------------------------------------


def test_load_private_key_from_text_or_bytes_with_any_line_ending() -> None:
    pem = openssh_private(ed25519("prueba"))
    for data in (pem, pem.decode("ascii"), pem.replace(b"\n", b"\r\n"), b"\n  " + pem + b"\n\n"):
        key = sshsig.load_private_key(data)
        assert key.public_key == private().public_key
    assert repr(key) == f"PrivateKey({key.fingerprint})"  # nada de la llave privada


BAD_PRIVATE_KEYS = {  # los datos se arman en la prueba: ningún id de pytest lleva una llave
    "empty": (lambda: b"", "key_invalid"),
    "blank": (lambda: "   \n", "key_invalid"),
    "text": (lambda: b"no es una llave", "key_invalid"),
    "too_big": (lambda: b"x" * (sshsig.MAX_PRIVATE_KEY_BYTES + 1), "key_invalid"),
    "pkcs8": (lambda: ed25519("prueba").private_bytes(  # no es del formato de OpenSSH
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()), "key_invalid"),
    "ecdsa": (lambda: openssh_private(ec.derive_private_key(7, ec.SECP256R1())), "key_type"),
}


@pytest.mark.parametrize("case", sorted(BAD_PRIVATE_KEYS))
def test_load_private_key_errors_never_carry_the_key(case: str) -> None:
    make, reason = BAD_PRIVATE_KEYS[case]
    with pytest.raises(SigningError) as info:
        sshsig.load_private_key(make())
    assert info.value.reason == reason
    assert str(info.value) == f"firma: {reason}"
    # sin la excepción de cryptography encadenada, cuyo texto podría citar la llave
    assert info.value.__cause__ is None
    assert info.value.__context__ is None or info.value.__suppress_context__


def test_a_truncated_private_key_is_invalid() -> None:
    pem = openssh_private(ed25519("prueba"))
    lines = pem.split(b"\n")
    cut = b"\n".join(lines[:2] + lines[-2:])
    with pytest.raises(SigningError) as info:
        sshsig.load_private_key(cut)
    assert info.value.reason == "key_invalid"


# -- interoperación con ssh-keygen ------------------------------------------------------------

SSH_KEYGEN = shutil.which("ssh-keygen")
needs_ssh_keygen = pytest.mark.skipif(SSH_KEYGEN is None, reason="sin ssh-keygen en el PATH")


def _ssh_keygen(*args: str, stdin: bytes | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[bytes]:
    assert SSH_KEYGEN is not None
    return subprocess.run([SSH_KEYGEN, *args], input=stdin, capture_output=True, cwd=cwd, timeout=60, check=False)


@pytest.fixture
def keygen_key(tmp_path: Path) -> Path:
    """Llave ``ssh-ed25519`` sin frase de paso que crea ``ssh-keygen`` (con sus permisos)."""
    path = tmp_path / "keys" / "id_ed25519"
    path.parent.mkdir()
    done = _ssh_keygen("-q", "-t", "ed25519", "-N", "", "-C", "", "-f", str(path))
    assert done.returncode == 0, done.stderr
    return path


@needs_ssh_keygen
def test_python_signs_like_ssh_keygen_byte_for_byte(keygen_key: Path) -> None:
    by_tool = _ssh_keygen("-Y", "sign", "-f", str(keygen_key), "-n", signing.NAMESPACE, stdin=MESSAGE)
    assert by_tool.returncode == 0, by_tool.stderr
    key = sshsig.load_private_key(keygen_key.read_bytes())
    assert key.sign(MESSAGE) == by_tool.stdout
    got = sshsig.verify(by_tool.stdout, MESSAGE)
    public = keygen_key.with_name(keygen_key.name + ".pub").read_text(encoding="ascii").split()[:2]
    assert got.public_key == signing.parse_public_key(" ".join(public)) == key.public_key


@needs_ssh_keygen
def test_ssh_keygen_verifies_the_signature_of_python(keygen_key: Path, tmp_path: Path) -> None:
    """El kit del árbitro (11-trazabilidad §8.6): ``ssh-keygen -Y verify`` con ``allowed_signers``."""
    key = sshsig.load_private_key(keygen_key.read_bytes())
    sig = tmp_path / "manifest.json.sig"
    sig.write_bytes(key.sign(MESSAGE))
    public = keygen_key.with_name(keygen_key.name + ".pub").read_text(encoding="ascii").split()[:2]
    allowed = tmp_path / "allowed_signers"
    allowed.write_text(f"ci-2026 {' '.join(public)}\n", encoding="ascii", newline="\n")
    ok = _ssh_keygen("-Y", "verify", "-f", str(allowed), "-I", "ci-2026", "-n", signing.NAMESPACE,
                     "-s", str(sig), stdin=MESSAGE)
    assert ok.returncode == 0, ok.stderr
    assert b"Good" in ok.stdout + ok.stderr
    bad = _ssh_keygen("-Y", "verify", "-f", str(allowed), "-I", "ci-2026", "-n", signing.NAMESPACE,
                      "-s", str(sig), stdin=MESSAGE + b"x")
    assert bad.returncode != 0
    other_namespace = _ssh_keygen("-Y", "verify", "-f", str(allowed), "-I", "ci-2026", "-n", "git",
                                  "-s", str(sig), stdin=MESSAGE)
    assert other_namespace.returncode != 0


@needs_ssh_keygen
def test_an_encrypted_private_key_is_not_loaded(tmp_path: Path) -> None:
    """En CI la llave va sin frase de paso; una con frase da ``key_invalid``, sin pedirla."""
    path = tmp_path / "id_cifrada"
    done = _ssh_keygen("-q", "-t", "ed25519", "-N", "una frase de prueba", "-C", "", "-f", str(path))
    assert done.returncode == 0, done.stderr
    with pytest.raises(SigningError) as info:
        sshsig.load_private_key(path.read_bytes())
    assert info.value.reason == "key_invalid"
