# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Firma de OpenSSH (SSHSIG) con la llave del proyecto (11-trazabilidad §8.10; 00-marco D-72).

El formato es el de ``PROTOCOL.sshsig`` de OpenSSH, en su forma ASCII
(``-----BEGIN SSH SIGNATURE-----``), la misma que escribe ``ssh-keygen -Y sign`` y que
comprueba ``ssh-keygen -Y verify``. Lo firmado es::

    "SSHSIG" ‖ string(espacio de nombres) ‖ string("") ‖ string(hash) ‖ string(H(mensaje))

con el espacio de nombres ``<nombre>-evidence``. Solo se admiten llaves ``ssh-ed25519``
(REQ-TRACE-90). Al firmar se usa ``sha512``, como ``ssh-keygen``; al verificar se aceptan
``sha256`` y ``sha512``.

- :func:`load_private_key` y :meth:`PrivateKey.sign` son de ``ci seal`` (09-ci REQ-CI-61): la
  llave privada llega en memoria, en el formato OpenSSH sin frase de paso, y nunca se escribe
  ni se muestra; un error al leerla da solo un motivo estable, sin el texto de la excepción.
- :func:`verify` es de ``evidence verify``, de ``evidence sign`` (que firma con ``ssh-keygen``)
  y de ``ci seal``, que verifica cada firma antes de sellarla.

``cryptography`` llega con el extra ``evidence`` y se importa dentro de las funciones
(REQ-ARCH-99): sin él, :class:`SignatureError` con el motivo ``missing_extra``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import struct
from dataclasses import dataclass
from typing import Any

from ..domain.signing import KEY_TYPE, NAMESPACE, PublicKeyError, fingerprint, key_blob, raw_key

__all__ = [
    "BEGIN",
    "END",
    "HASHES",
    "MAX_SIGNATURE_BYTES",
    "PrivateKey",
    "SignatureError",
    "SigningError",
    "SshSignature",
    "armor",
    "load_private_key",
    "parse",
    "verify",
]

MAGIC = b"SSHSIG"
VERSION = 1
BEGIN = "-----BEGIN SSH SIGNATURE-----"
END = "-----END SSH SIGNATURE-----"
HASHES: tuple[str, ...] = ("sha256", "sha512")
SIGN_HASH = "sha512"
WRAP = 70  # columnas de la forma ASCII, como ssh-keygen
MAX_SIGNATURE_BYTES = 16 * 1024  # una firma ed25519 mide unos 300 bytes; el resto es de terceros
MAX_PRIVATE_KEY_BYTES = 16 * 1024
ED25519_SIGNATURE_BYTES = 64


class SignatureError(ValueError):
    """Firma que no verifica o que no se puede leer.

    ``reason``: ``invalid`` (no tiene la forma de SSHSIG), ``namespace`` (otro espacio de
    nombres), ``unsupported`` (una llave que no es ``ssh-ed25519`` o un hash que no es
    ``sha256`` ni ``sha512``), ``bad_signature`` (no verifica con esos bytes) o
    ``missing_extra`` (falta ``cryptography``).
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"firma: {reason}")
        self.reason = reason


class SigningError(ValueError):
    """No se pudo firmar: ``key_invalid`` (no es una llave privada de OpenSSH sin frase de paso),
    ``key_type`` (no es ``ssh-ed25519``) o ``missing_extra``. Nunca lleva el texto de la llave."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"firma: {reason}")
        self.reason = reason


def _string(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


class _Reader:
    """Lectura de los ``string`` y ``uint32`` del formato de SSH (RFC 4251 §5)."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def raw(self, size: int) -> bytes:
        end = self.offset + size
        if end > len(self.data):
            raise SignatureError("invalid")
        out, self.offset = self.data[self.offset : end], end
        return out

    def uint32(self) -> int:
        (value,) = struct.unpack(">I", self.raw(4))
        return int(value)

    def string(self) -> bytes:
        return self.raw(self.uint32())

    def done(self) -> None:
        if self.offset != len(self.data):
            raise SignatureError("invalid")


def _signed_data(namespace: str, hash_name: str, message: bytes) -> bytes:
    digest = hashlib.sha512(message).digest() if hash_name == "sha512" else hashlib.sha256(message).digest()
    return MAGIC + _string(namespace.encode("utf-8")) + _string(b"") + _string(hash_name.encode("ascii")) + _string(digest)


def armor(blob: bytes) -> bytes:
    """Forma ASCII de una firma SSHSIG, con salto de línea final."""
    text = base64.b64encode(blob).decode("ascii")
    lines = [text[i : i + WRAP] for i in range(0, len(text), WRAP)]
    return ("\n".join([BEGIN, *lines, END]) + "\n").encode("ascii")


@dataclass(frozen=True)
class SshSignature:
    """Una firma SSHSIG leída: la llave pública (su blob), el espacio de nombres, el hash y la firma."""

    public_key: bytes
    namespace: str
    hash: str
    signature: bytes

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.public_key)


def parse(data: bytes) -> SshSignature:
    """Lee la forma ASCII de una firma SSHSIG; ``SignatureError("invalid")`` si no la tiene.

    No verifica nada: solo exige la forma exacta (preámbulo, versión 1, campos completos y sin
    bytes de más), una llave ``ssh-ed25519`` y un hash admitido.
    """
    if not isinstance(data, bytes) or len(data) > MAX_SIGNATURE_BYTES:
        raise SignatureError("invalid")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise SignatureError("invalid") from None
    lines = [line.strip() for line in text.replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    if len(lines) < 3 or lines[0] != BEGIN or lines[-1] != END:
        raise SignatureError("invalid")
    try:
        blob = base64.b64decode("".join(lines[1:-1]), validate=True)
    except (binascii.Error, ValueError):
        raise SignatureError("invalid") from None
    reader = _Reader(blob)
    if reader.raw(len(MAGIC)) != MAGIC or reader.uint32() != VERSION:
        raise SignatureError("invalid")
    public_key = reader.string()
    namespace = reader.string()
    reader.string()  # reservado
    hash_name = reader.string()
    signature_blob = reader.string()
    reader.done()
    try:
        raw_key(public_key)
    except PublicKeyError as exc:
        raise SignatureError("unsupported" if exc.reason == "type" else "invalid") from None
    if hash_name.decode("ascii", "replace") not in HASHES:
        raise SignatureError("unsupported")
    sig = _Reader(signature_blob)
    sig_type = sig.string()
    raw_signature = sig.string()
    sig.done()
    if sig_type != KEY_TYPE.encode("ascii"):
        raise SignatureError("unsupported")
    if len(raw_signature) != ED25519_SIGNATURE_BYTES:
        raise SignatureError("invalid")
    try:
        namespace_text = namespace.decode("utf-8")
    except UnicodeDecodeError:
        raise SignatureError("invalid") from None
    return SshSignature(public_key, namespace_text, hash_name.decode("ascii"), raw_signature)


def verify(data: bytes, message: bytes, *, namespace: str = NAMESPACE) -> SshSignature:
    """Comprueba la firma ``data`` (forma ASCII) sobre los bytes exactos de ``message``.

    Devuelve la firma leída, con la llave que la hizo; quién es esa llave lo decide quien
    llama, con la lista de llaves del proyecto (REQ-TRACE-90), nunca con la llave que trae.
    """
    parsed = parse(data)
    if parsed.namespace != namespace:
        raise SignatureError("namespace")
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        raise SignatureError("missing_extra") from None
    public = Ed25519PublicKey.from_public_bytes(raw_key(parsed.public_key))
    try:
        public.verify(parsed.signature, _signed_data(namespace, parsed.hash, message))
    except InvalidSignature:
        raise SignatureError("bad_signature") from None
    return parsed


@dataclass(frozen=True)
class PrivateKey:
    """Llave privada ``ssh-ed25519`` en memoria; ``repr`` no muestra nada de ella."""

    _key: Any
    public_key: bytes

    def __repr__(self) -> str:
        return f"PrivateKey({fingerprint(self.public_key)})"

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.public_key)

    def sign(self, message: bytes, *, namespace: str = NAMESPACE) -> bytes:
        """Firma SSHSIG de los bytes exactos de ``message``, en su forma ASCII."""
        signature = self._key.sign(_signed_data(namespace, SIGN_HASH, message))
        blob = (
            MAGIC + struct.pack(">I", VERSION) + _string(self.public_key) + _string(namespace.encode("utf-8"))
            + _string(b"") + _string(SIGN_HASH.encode("ascii"))
            + _string(_string(KEY_TYPE.encode("ascii")) + _string(signature))
        )
        return armor(blob)


def load_private_key(data: bytes | str) -> PrivateKey:
    """Llave privada de OpenSSH, ``ssh-ed25519`` y sin frase de paso (09-ci REQ-CI-61).

    ``SigningError`` con un motivo estable; nunca propaga la excepción de ``cryptography``,
    cuyo texto podría citar algo de la llave.
    """
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if not isinstance(raw, bytes) or not raw.strip() or len(raw) > MAX_PRIVATE_KEY_BYTES:
        raise SigningError("key_invalid")
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_ssh_private_key
    except ImportError:
        raise SigningError("missing_extra") from None
    try:
        key = load_ssh_private_key(raw.replace(b"\r\n", b"\n").strip() + b"\n", password=None)
    except Exception:  # noqa: BLE001 - con frase de paso, otro formato o dañada: sin su texto
        raise SigningError("key_invalid") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise SigningError("key_type")
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return PrivateKey(key, key_blob(public))
