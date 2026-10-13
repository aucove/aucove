# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Registro de evidencia: carpeta ``<kind>-<commit>/`` y JSON del comentario ``ci:evidence``.

11-trazabilidad §8.5, 09-ci REQ-CI-54 y esquema ``evidence-record/v1``. Archivos de
la carpeta (nombres fijos):

- ``record.json`` (el registro, con ``manifest``, ``openings`` y los ``data`` en
  ``null`` porque van en archivos), ``manifest.json`` (bytes exactos, sin salto de línea
  final), ``openings.json`` (privado) y ``manifest.json.ots``;
- por TSA: ``tsa/<id>.tsr``, ``tsa/<id>.chain.pem``, ``tsa/<id>.root.pem`` y
  ``tsa/<id>.ocsp.der`` o ``tsa/<id>.crl.der``;
- ``VERIFY.md`` y ``open_commitments.py``, que aporta ``ci seal`` (``extra``).

La carpeta se llama ``<kind>-<commit>`` en el artefacto y ``<kind>-<commit>-a<n>`` en la
copia local ``.aucove/evidence/`` (05-operaciones REQ-OPS-66). Las pruebas son bytes
opacos con sus metadatos: aquí no se interpretan (los clientes RFC 3161 y OTS son de
otros módulos del paquete).

En el comentario, el registro lleva todo dentro: el manifiesto como texto, las
aperturas y los ``data`` en base64. Si el JSON pasa de 60 000 caracteres se quitan, en
este orden y solo hasta que quepa, ``openings``, ``manifest`` y los ``data`` de
``revocation``, y ``truncated`` pasa a ``true`` (REQ-CI-54). Como último recurso, que la
especificación no prevé, se quitan los ``data`` de las pruebas (el esquema lo admite:
«null ... si no cupo»). El JSON va en ASCII y con ``<``, ``>`` y ``&`` escapados
(``\\u003c``...): dentro de un comentario HTML ningún texto del PR, como una ruta con
``-->``, puede cerrarlo, y el largo no depende de cómo cuente la forja los caracteres.
Además, ``--`` va como ``\\u002d\\u002d`` (gramática de marcadores, 00-marco §5.14).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import shutil
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from .. import _meta
from .openings import Openings, OpeningsError, load_openings, parse_openings

__all__ = [
    "COMMENT_LIMIT",
    "KINDS",
    "MANIFEST_FILE",
    "MAX_FILE_BYTES",
    "OPENINGS_FILE",
    "OPEN_SCRIPT",
    "OTS_FILE",
    "OTS_SOURCE",
    "RECORD_FILE",
    "RECORD_SCHEMA",
    "TRUNCATION_ORDER",
    "VERIFY_FILE",
    "CommentJson",
    "Proof",
    "Record",
    "RecordError",
    "RecordFolder",
    "Revocation",
    "artifact_name",
    "chain_file",
    "comment_json",
    "find_record_dirs",
    "load_comment_json",
    "local_dirname",
    "parse_record",
    "read_record_dir",
    "record_dirname",
    "record_files",
    "revocation_file",
    "root_file",
    "tsr_file",
    "write_record_dir",
]

RECORD_SCHEMA = f"{_meta.SCHEMA_BASE}/evidence-record/v1"
KINDS: tuple[str, ...] = ("submission", "integration")
RECORD_FILE = "record.json"
MANIFEST_FILE = "manifest.json"
OPENINGS_FILE = "openings.json"
OTS_FILE = "manifest.json.ots"
VERIFY_FILE = "VERIFY.md"
OPEN_SCRIPT = "open_commitments.py"
OTS_SOURCE = "calendars"  # 'source' de una prueba OTS
COMMENT_LIMIT = 60_000  # caracteres del JSON del comentario (REQ-CI-54)
MAX_PROOFS = 4
MAX_ERROR_CHARS = 300
MAX_ATTEMPT = 999  # el nombre del artefacto admite a1..a999
MAX_FILE_BYTES = 64 * 2**20  # lectura de cada archivo de una carpeta de registro
# Lo que se quita del comentario, en orden, hasta que quepa (el último no lo prevé REQ-CI-54).
TRUNCATION_ORDER: tuple[str, ...] = ("openings", "manifest", "revocation", "proofs")

ProofType = Literal["ots", "rfc3161"]
ProofStatus = Literal["ok", "error"]
RevocationType = Literal["ocsp", "crl"]

_OID_RE = re.compile(r"[0-9a-f]{40}")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_SOURCE_RE = re.compile(r"[a-z0-9-]{1,32}")
_TIME_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z")
_B64_RE = re.compile(r"[A-Za-z0-9+/]*={0,2}")
_HTML_ESCAPES = {"<": "\\u003c", ">": "\\u003e", "&": "\\u0026"}
_HTML_RE = re.compile("[<>&]")


class RecordError(ValueError):
    """Registro con otra forma que ``evidence-record/v1``, o archivos que no le corresponden.

    ``reason``: ``invalid`` (``pointer`` ubica el campo), ``inconsistent`` (los bytes no
    dan la huella del registro) o ``missing`` (no hay ``record.json``).
    """

    def __init__(self, reason: str, pointer: str = "", detail: str = "") -> None:
        self.reason = reason
        self.pointer = pointer
        message = f"registro: {reason} en {pointer!r}"
        super().__init__(f"{message} ({detail})" if detail else message)


def _need(cond: bool, pointer: str, detail: str = "") -> None:
    if not cond:
        raise RecordError("invalid", pointer, detail)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# -- nombres -----------------------------------------------------------------------


def _check_kind_commit(kind: object, commit: object) -> None:
    _need(kind in KINDS, "/kind")
    _need(isinstance(commit, str) and _OID_RE.fullmatch(commit) is not None, "/commit")


def record_dirname(kind: str, commit: str) -> str:
    """``<kind>-<commit>``: carpeta del registro en el artefacto (11 §8.5)."""
    _check_kind_commit(kind, commit)
    return f"{kind}-{commit}"


def local_dirname(kind: str, commit: str, attempt: int) -> str:
    """``<kind>-<commit>-a<n>``: carpeta de la copia local en ``.aucove/evidence/`` (REQ-OPS-66)."""
    _need(_is_int(attempt) and 1 <= attempt <= MAX_ATTEMPT, "/run_attempt")
    return f"{record_dirname(kind, commit)}-a{attempt}"


def artifact_name(kind: str, commit: str, attempt: int) -> str:
    """``evidence-<kind>-<commit>-a<n>``: nombre del artefacto del run (11 §8.5)."""
    return f"evidence-{local_dirname(kind, commit, attempt)}"


def _check_source(source: str) -> str:
    _need(isinstance(source, str) and _SOURCE_RE.fullmatch(source) is not None, "/source")
    return source


def tsr_file(tsa_id: str) -> str:
    """``tsa/<id>.tsr``."""
    return f"tsa/{_check_source(tsa_id)}.tsr"


def chain_file(tsa_id: str) -> str:
    """``tsa/<id>.chain.pem``: cadena de la TSA obtenida al sellar."""
    return f"tsa/{_check_source(tsa_id)}.chain.pem"


def root_file(tsa_id: str) -> str:
    """``tsa/<id>.root.pem``: raíz del catálogo con la que se verificó."""
    return f"tsa/{_check_source(tsa_id)}.root.pem"


def revocation_file(tsa_id: str, kind: str) -> str:
    """``tsa/<id>.ocsp.der`` o ``tsa/<id>.crl.der``."""
    _need(kind in ("ocsp", "crl"), "/revocation/type")
    return f"tsa/{_check_source(tsa_id)}.{kind}.der"


# -- pruebas -----------------------------------------------------------------------


@dataclass(frozen=True)
class Revocation:
    """Estado de revocación del certificado de una TSA capturado al sellar (OCSP o CRL, en DER).

    ``data`` es ``None`` si no se tienen los bytes (comentario recortado, o un
    ``record.json`` leído sin su archivo).
    """

    type: RevocationType
    data: bytes | None = None

    def __post_init__(self) -> None:
        _need(self.type in ("ocsp", "crl"), "/revocation/type")
        _need(self.data is None or isinstance(self.data, bytes), "/revocation/data")


@dataclass(frozen=True)
class Proof:
    """Prueba de fecha del registro (``$defs/proof``): bytes opacos más sus metadatos.

    ``data`` son los bytes de ``manifest.json.ots`` o de ``tsa/<source>.tsr``; ``None`` si
    no se tienen. ``chain_pem`` y ``root_pem`` (solo ``rfc3161``) van únicamente en la
    carpeta; ``revocation``, también en el comentario. Con ``status == "error"`` solo
    quedan ``type``, ``source`` y ``error`` (hasta 300 caracteres).
    """

    type: ProofType
    source: str
    status: ProofStatus
    data: bytes | None = None
    time_utc: str | None = None
    revocation: Revocation | None = None
    error: str | None = None
    chain_pem: bytes | None = None
    root_pem: bytes | None = None

    def __post_init__(self) -> None:
        _need(self.type in ("ots", "rfc3161"), "/type")
        _check_source(self.source)
        _need(self.status in ("ok", "error"), "/status")
        for name in ("data", "chain_pem", "root_pem"):
            value = getattr(self, name)
            _need(value is None or isinstance(value, bytes), f"/{name}")
        _need(self.revocation is None or isinstance(self.revocation, Revocation), "/revocation")
        _need(
            self.time_utc is None or (isinstance(self.time_utc, str) and _TIME_RE.fullmatch(self.time_utc) is not None),
            "/time_utc",
        )
        if self.type == "ots":
            _need(self.source == OTS_SOURCE, "/source")
            _need(self.time_utc is None, "/time_utc")
            _need(self.revocation is None and self.chain_pem is None and self.root_pem is None, "/revocation")
        if self.status == "error":
            _need(isinstance(self.error, str) and 0 < len(self.error) <= MAX_ERROR_CHARS, "/error")
            _need(self.data is None and self.revocation is None, "/data")
            _need(self.chain_pem is None and self.root_pem is None, "/data")
        else:
            _need(self.error is None, "/error")

    @classmethod
    def ots(cls, data: bytes) -> Proof:
        """Prueba OTS obtenida (pendiente de Bitcoin hasta que se complete)."""
        return cls(type="ots", source=OTS_SOURCE, status="ok", data=data)

    @classmethod
    def rfc3161(
        cls,
        source: str,
        data: bytes,
        time_utc: str | None,
        *,
        revocation: Revocation | None = None,
        chain_pem: bytes | None = None,
        root_pem: bytes | None = None,
    ) -> Proof:
        """Sello RFC 3161 obtenido y verificado de la TSA ``source`` (id del catálogo)."""
        return cls(
            type="rfc3161",
            source=source,
            status="ok",
            data=data,
            time_utc=time_utc,
            revocation=revocation,
            chain_pem=chain_pem,
            root_pem=root_pem,
        )

    @classmethod
    def failed(cls, proof_type: ProofType, source: str, error: str) -> Proof:
        """Prueba que no se obtuvo; ``error`` va en una línea y se recorta a 300 caracteres."""
        text = " ".join(str(error).split()) or "error"
        if len(text) > MAX_ERROR_CHARS:
            text = text[: MAX_ERROR_CHARS - 1] + "…"
        return cls(type=proof_type, source=source, status="error", error=text)

    @property
    def file(self) -> str | None:
        """Ruta de la prueba dentro de la carpeta; ``None`` si no se obtuvo."""
        if self.status != "ok":
            return None
        return OTS_FILE if self.type == "ots" else tsr_file(self.source)


def _b64(data: bytes | None, embed: bool) -> str | None:
    return base64.b64encode(data).decode("ascii") if embed and data is not None else None


def _proof_document(proof: Proof, *, embed: bool) -> dict[str, Any]:
    revocation = None
    if proof.revocation is not None:
        revocation = {
            "type": proof.revocation.type,
            "file": revocation_file(proof.source, proof.revocation.type),
            "data": _b64(proof.revocation.data, embed),
        }
    return {
        "type": proof.type,
        "source": proof.source,
        "status": proof.status,
        "file": proof.file,
        "data": _b64(proof.data, embed),
        "time_utc": proof.time_utc,
        "revocation": revocation,
        "error": proof.error,
    }


# -- registro ------------------------------------------------------------------------


@dataclass(frozen=True)
class Record:
    """Registro de un sello (``evidence-record/v1``), con los bytes que tenga.

    ``manifest`` son los bytes exactos de ``manifest.json`` y ``openings``, las aperturas;
    los dos pueden faltar (``None``) en un registro leído de un comentario recortado o
    de una carpeta incompleta. La coherencia con ``manifest_sha256`` se exige al escribir
    (``record_files``), no al construir: quien verifica decide qué hacer con un registro
    alterado.
    """

    kind: str
    commit: str
    pr: int | None
    task: int | None
    plan: bool
    manifest_sha256: str
    run_id: int
    run_attempt: int
    proofs: tuple[Proof, ...]
    manifest: bytes | None = None
    openings: Openings | None = None
    truncated: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "proofs", tuple(self.proofs))
        _check_kind_commit(self.kind, self.commit)
        for name in ("pr", "task"):
            value = getattr(self, name)
            _need(value is None or (_is_int(value) and value >= 1), f"/{name}")
        _need(isinstance(self.plan, bool), "/plan")
        _need(isinstance(self.truncated, bool), "/truncated")
        sha = self.manifest_sha256
        _need(isinstance(sha, str) and _HEX64_RE.fullmatch(sha) is not None, "/manifest_sha256")
        _need(_is_int(self.run_id) and self.run_id >= 1, "/run_id")
        _need(_is_int(self.run_attempt) and 1 <= self.run_attempt <= MAX_ATTEMPT, "/run_attempt")
        _need(1 <= len(self.proofs) <= MAX_PROOFS and all(isinstance(p, Proof) for p in self.proofs), "/proofs")
        _need(self.manifest is None or isinstance(self.manifest, bytes), "/manifest")
        _need(self.openings is None or isinstance(self.openings, Openings), "/openings")

    @property
    def artifact(self) -> str:
        """``evidence-<kind>-<commit>-a<run_attempt>``."""
        return artifact_name(self.kind, self.commit, self.run_attempt)

    @property
    def dirname(self) -> str:
        """``<kind>-<commit>``."""
        return record_dirname(self.kind, self.commit)

    @property
    def local_dirname(self) -> str:
        """``<kind>-<commit>-a<run_attempt>``."""
        return local_dirname(self.kind, self.commit, self.run_attempt)

    def document(self, *, embed: bool) -> dict[str, Any]:
        """Objeto ``evidence-record/v1``.

        ``embed=False``: la forma de ``record.json`` (``manifest``, ``openings`` y los
        ``data`` en ``null``). ``embed=True``: la del comentario antes de recortar, con
        todo lo que se tenga dentro.
        """
        manifest: str | None = None
        if embed and self.manifest is not None:
            try:
                manifest = self.manifest.decode("utf-8")
            except UnicodeDecodeError:
                raise RecordError("invalid", "/manifest", "no es UTF-8") from None
        return {
            "schema": RECORD_SCHEMA,
            "kind": self.kind,
            "commit": self.commit,
            "pr": self.pr,
            "task": self.task,
            "plan": self.plan,
            "manifest_sha256": self.manifest_sha256,
            "manifest": manifest,
            "openings": self.openings.to_json() if embed and self.openings is not None else None,
            "proofs": [_proof_document(proof, embed=embed) for proof in self.proofs],
            "truncated": self.truncated,
            "run_id": self.run_id,
            "run_attempt": self.run_attempt,
            "artifact": self.artifact,
        }


# -- comentario ci:evidence (REQ-CI-54) --------------------------------------------


@dataclass(frozen=True)
class CommentJson:
    """JSON del comentario ``ci:evidence``: el texto, si se recortó y qué se quitó (en orden)."""

    text: str
    truncated: bool
    dropped: tuple[str, ...]


def _comment_text(document: Mapping[str, Any]) -> str:
    text = json.dumps(document, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    text = _HTML_RE.sub(lambda m: _HTML_ESCAPES[m.group()], text)
    # 00-marco §5.14: dentro del JSON de un marcador, '--' va como --. Un '-' solo
    # aparece dentro de cadenas (no hay números negativos), así que el JSON sigue valiendo.
    return text.replace("--", "\\u002d\\u002d")


def _drop(document: dict[str, Any], step: str) -> bool:
    """Quita del documento lo que corresponde a ``step``; ``True`` si había algo que quitar."""
    if step in ("openings", "manifest"):
        present = document.get(step) is not None
        document[step] = None
        return present
    removed = False
    for proof in document["proofs"]:
        holder = proof.get("revocation") if step == "revocation" else proof
        if isinstance(holder, dict) and holder.get("data") is not None:
            holder["data"] = None
            removed = True
    return removed


def comment_json(record: Record, *, limit: int = COMMENT_LIMIT) -> CommentJson:
    """JSON ``evidence-record/v1`` para el comentario, recortado para no pasar de ``limit`` caracteres.

    Quita, en el orden de ``TRUNCATION_ORDER`` y solo hasta que quepa, ``openings``,
    ``manifest``, los ``data`` de ``revocation`` y, como último recurso, los ``data`` de las
    pruebas; si quita algo, ``truncated`` es ``true``. El marcador y la línea visible del
    comentario los agrega quien lo publica.
    """
    document = record.document(embed=True)
    text = _comment_text(document)
    dropped: list[str] = []
    for step in TRUNCATION_ORDER:
        if len(text) <= limit:
            break
        if _drop(document, step):
            dropped.append(step)
            document["truncated"] = True
            text = _comment_text(document)
    return CommentJson(text=text, truncated=bool(document["truncated"]), dropped=tuple(dropped))


# -- lectura de un registro -----------------------------------------------------------


def _decode_b64(value: object, pointer: str) -> bytes | None:
    if value is None:
        return None
    _need(isinstance(value, str) and _B64_RE.fullmatch(value) is not None, pointer)
    assert isinstance(value, str)
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise RecordError("invalid", pointer) from None


def _parse_revocation(obj: object, source: str, pointer: str) -> Revocation | None:
    if obj is None:
        return None
    _need(isinstance(obj, Mapping), pointer)
    assert isinstance(obj, Mapping)
    kind = obj.get("type")
    _need(kind in ("ocsp", "crl"), pointer + "/type")
    _need(obj.get("file") == revocation_file(source, str(kind)), pointer + "/file")
    return Revocation(type=kind, data=_decode_b64(obj.get("data"), pointer + "/data"))  # type: ignore[arg-type]


def _parse_proof(obj: object, pointer: str) -> Proof:
    _need(isinstance(obj, Mapping), pointer)
    assert isinstance(obj, Mapping)
    source = obj.get("source")
    _need(isinstance(source, str) and _SOURCE_RE.fullmatch(source) is not None, pointer + "/source")
    assert isinstance(source, str)
    try:
        proof = Proof(
            type=obj.get("type"),  # type: ignore[arg-type]
            source=source,
            status=obj.get("status"),  # type: ignore[arg-type]
            data=_decode_b64(obj.get("data"), pointer + "/data"),
            time_utc=obj.get("time_utc"),
            revocation=_parse_revocation(obj.get("revocation"), source, pointer + "/revocation"),
            error=obj.get("error"),
        )
    except RecordError as exc:
        if exc.pointer.startswith(pointer):
            raise
        raise RecordError(exc.reason, pointer + exc.pointer) from None
    _need(obj.get("file") == proof.file, pointer + "/file")  # la ruta sale del tipo y del id, nunca del texto
    return proof


def parse_record(obj: object) -> Record:
    """Registro desde su objeto ``evidence-record/v1`` (comentario o ``record.json``).

    Decodifica el manifiesto (texto a bytes UTF-8), las aperturas y los ``data`` en
    base64, y exige que ``file`` y ``artifact`` sean los que corresponden. No reemplaza
    a la validación con el esquema; ``RecordError`` con el JSON Pointer del problema.
    """
    _need(isinstance(obj, Mapping), "")
    assert isinstance(obj, Mapping)
    _need(obj.get("schema") == RECORD_SCHEMA, "/schema")
    manifest = obj.get("manifest")
    _need(manifest is None or isinstance(manifest, str), "/manifest")
    manifest_bytes: bytes | None = None
    if isinstance(manifest, str):
        try:
            manifest_bytes = manifest.encode("utf-8")
        except UnicodeEncodeError:
            raise RecordError("invalid", "/manifest") from None
    openings_obj = obj.get("openings")
    openings: Openings | None = None
    if openings_obj is not None:
        try:
            openings = parse_openings(openings_obj)
        except OpeningsError as exc:
            raise RecordError("invalid", "/openings" + exc.pointer) from None
    proofs_obj = obj.get("proofs")
    _need(isinstance(proofs_obj, list), "/proofs")
    assert isinstance(proofs_obj, list)
    proofs = tuple(_parse_proof(item, f"/proofs/{i}") for i, item in enumerate(proofs_obj))
    record = Record(
        kind=obj.get("kind"),  # type: ignore[arg-type]
        commit=obj.get("commit"),  # type: ignore[arg-type]
        pr=obj.get("pr"),
        task=obj.get("task"),
        plan=obj.get("plan"),  # type: ignore[arg-type]
        manifest_sha256=obj.get("manifest_sha256"),  # type: ignore[arg-type]
        run_id=obj.get("run_id"),  # type: ignore[arg-type]
        run_attempt=obj.get("run_attempt"),  # type: ignore[arg-type]
        proofs=proofs,
        manifest=manifest_bytes,
        openings=openings,
        truncated=obj.get("truncated"),  # type: ignore[arg-type]
    )
    _need(obj.get("artifact") == record.artifact, "/artifact")
    return record


def load_comment_json(text: str | bytes) -> Record:
    """Registro desde el JSON de un comentario ``ci:evidence`` (o de un ``record.json``).

    El texto es de terceros: si no es UTF-8 ni JSON, trae un entero de más de 4300 cifras
    (``ValueError`` de Python) o anida tanto que agota la recursión, es
    ``RecordError("invalid")``, nunca una excepción suelta.
    """
    try:
        raw = text.decode("utf-8") if isinstance(text, bytes) else text
        obj = json.loads(raw)
    except (ValueError, RecursionError) as exc:  # UnicodeDecodeError y JSONDecodeError son ValueError
        raise RecordError("invalid", "", str(exc)[:120]) from None
    return parse_record(obj)


# -- escritura de la carpeta -------------------------------------------------------------


_RESERVED = frozenset({RECORD_FILE, MANIFEST_FILE, OPENINGS_FILE, OTS_FILE})


def _check_extra_name(name: object) -> str:
    _need(isinstance(name, str) and bool(name), "/extra", repr(name))
    assert isinstance(name, str)
    pure = PurePosixPath(name)
    ok = (
        not pure.is_absolute()
        and "\\" not in name
        and ":" not in name
        and all(part not in ("", ".", "..") for part in name.split("/"))
        and name not in _RESERVED
        and pure.parts[0] != "tsa"
    )
    _need(ok, "/extra", name)
    return name


def record_files(record: Record, *, extra: Mapping[str, bytes] | None = None) -> dict[str, bytes]:
    """Archivos de la carpeta del registro: ruta relativa (con ``/``) -> bytes.

    ``RecordError("inconsistent")`` si el manifiesto no da ``manifest_sha256`` o las
    aperturas son de otro manifiesto. ``extra`` agrega archivos propios de ``ci seal``
    (``VERIFY.md``, ``open_commitments.py``): rutas relativas que no pisan las del registro.
    """
    if record.manifest is not None and hashlib.sha256(record.manifest).hexdigest() != record.manifest_sha256:
        raise RecordError("inconsistent", "/" + MANIFEST_FILE)
    if record.openings is not None and record.openings.manifest_sha256 != record.manifest_sha256:
        raise RecordError("inconsistent", "/" + OPENINGS_FILE)
    document = record.document(embed=False)
    files: dict[str, bytes] = {
        RECORD_FILE: (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    }
    if record.manifest is not None:
        files[MANIFEST_FILE] = record.manifest
    if record.openings is not None:
        files[OPENINGS_FILE] = record.openings.dumps()
    for proof in record.proofs:
        if proof.file is not None and proof.data is not None:
            files[proof.file] = proof.data
        if proof.chain_pem is not None:
            files[chain_file(proof.source)] = proof.chain_pem
        if proof.root_pem is not None:
            files[root_file(proof.source)] = proof.root_pem
        if proof.revocation is not None and proof.revocation.data is not None:
            files[revocation_file(proof.source, proof.revocation.type)] = proof.revocation.data
    for name, data in (extra or {}).items():
        _need(isinstance(data, bytes), "/extra", str(name))
        files[_check_extra_name(name)] = data
    return files


def _check_dirname(name: str) -> str:
    _need(
        isinstance(name, str)
        and bool(name)
        and name not in (".", "..")
        and not any(sep in name for sep in ("/", "\\", ":", "\0")),
        "/name",
        repr(name),
    )
    return name


def write_record_dir(
    root: Path, record: Record, *, extra: Mapping[str, bytes] | None = None, name: str | None = None
) -> Path:
    """Escribe la carpeta del registro dentro de ``root`` y la devuelve.

    Se llama ``<kind>-<commit>`` (o ``name``, p. ej. ``record.local_dirname`` en la copia
    local). Se escribe en una carpeta temporal hermana que se renombra al final: nunca
    queda a medias con el nombre final. ``FileExistsError`` si ya existe.
    """
    files = record_files(record, extra=extra)
    folder = _check_dirname(record.dirname if name is None else name)
    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    final = base / folder
    if final.exists() or final.is_symlink():
        raise FileExistsError(str(final))
    tmp = base / f".{folder}.tmp-{secrets.token_hex(4)}"
    try:
        tmp.mkdir()
        for rel, data in files.items():
            target = tmp.joinpath(*rel.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        _replace_dir(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return final


_REPLACE_RETRIES = 3  # en Windows, un antivirus o el indexador pueden retener un archivo recién escrito
_REPLACE_WAIT_S = 0.05


def _replace_dir(tmp: Path, final: Path) -> None:
    attempts = 1 + (_REPLACE_RETRIES if os.name == "nt" else 0)
    for attempt in range(attempts):
        try:
            os.replace(tmp, final)
            return
        except PermissionError:
            if attempt + 1 == attempts:
                raise
            time.sleep(_REPLACE_WAIT_S)


# -- lectura de la carpeta ------------------------------------------------------------


@dataclass(frozen=True)
class RecordFolder:
    """Carpeta de registro leída.

    ``record`` sale de ``record.json`` con los bytes de los archivos que había (o, si
    falta un archivo, lo que el JSON traía dentro). ``problems`` son pares ``(archivo,
    código)`` con ``missing``, ``too_large``, ``unreadable`` o ``invalid``: no
    interrumpen la lectura, para que quien verifica informe cada cosa.
    """

    path: Path
    record: Record
    problems: tuple[tuple[str, str], ...]


def _read_file(folder: Path, rel: str, limit: int) -> tuple[bytes | None, str | None]:
    path = folder.joinpath(*rel.split("/"))
    try:
        if not path.is_file():
            return None, "missing"
        if path.stat().st_size > limit:
            return None, "too_large"
        data = path.read_bytes()
    except OSError:
        return None, "unreadable"
    if len(data) > limit:
        return None, "too_large"
    return data, None


def read_record_dir(path: Path, *, max_file_bytes: int = MAX_FILE_BYTES) -> RecordFolder:
    """Lee la carpeta de un registro (``<kind>-<commit>/`` o ``<kind>-<commit>-a<n>/``).

    ``RecordError`` si ``record.json`` falta (``missing``) o no se entiende (``invalid``).
    Lo demás que falte o no sirva va a ``problems``; los archivos tienen precedencia sobre
    lo que ``record.json`` traiga dentro.
    """
    folder = Path(path)
    raw, problem = _read_file(folder, RECORD_FILE, max_file_bytes)
    if raw is None:
        raise RecordError("missing" if problem == "missing" else "invalid", "/" + RECORD_FILE, problem or "")
    record = load_comment_json(raw)
    problems: list[tuple[str, str]] = []

    def take(rel: str, current: bytes | None) -> bytes | None:
        data, issue = _read_file(folder, rel, max_file_bytes)
        if data is not None:
            return data
        if current is None or issue != "missing":
            problems.append((rel, issue or "missing"))
        return current

    manifest = take(MANIFEST_FILE, record.manifest)
    openings = record.openings
    data, issue = _read_file(folder, OPENINGS_FILE, max_file_bytes)
    if data is not None:
        try:
            openings = load_openings(data)
        except OpeningsError:
            problems.append((OPENINGS_FILE, "invalid"))
            openings = None
    elif openings is None or issue != "missing":
        problems.append((OPENINGS_FILE, issue or "missing"))

    proofs: list[Proof] = []
    for proof in record.proofs:
        if proof.status != "ok":
            proofs.append(proof)
            continue
        assert proof.file is not None
        updated = replace(proof, data=take(proof.file, proof.data))
        if proof.type == "rfc3161":
            revocation = proof.revocation
            if revocation is not None:
                rel = revocation_file(proof.source, revocation.type)
                revocation = replace(revocation, data=take(rel, revocation.data))
            updated = replace(
                updated,
                revocation=revocation,
                chain_pem=take(chain_file(proof.source), None),
                root_pem=take(root_file(proof.source), None),
            )
        proofs.append(updated)
    record = replace(record, manifest=manifest, openings=openings, proofs=tuple(proofs))
    return RecordFolder(path=folder, record=record, problems=tuple(problems))


def find_record_dirs(root: Path, *, max_depth: int = 3) -> list[Path]:
    """Carpetas de registro (las que tienen ``record.json``) en ``root`` o debajo, ordenadas.

    Baja como mucho ``max_depth`` niveles (un paquete con artefactos con registros tiene
    tres) y no sigue enlaces; no entra en una carpeta de registro.
    """
    found: list[Path] = []

    def visit(directory: Path, depth: int) -> None:
        if (directory / RECORD_FILE).is_file():
            found.append(directory)
            return
        if depth >= max_depth:
            return
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            return
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                visit(Path(entry.path), depth + 1)

    visit(Path(root), 0)
    return found
