# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove evidence sign <paquete> --key <archivo>`` (05-operaciones §9.27; 11-trazabilidad §8.10; D-72).

Solo humana (D-09) y sin MCP: la usa el admin del repositorio en cada hito para firmar el
índice del paquete de evidencia (``index.txt``) con la llave privada del proyecto, y sellar
esa firma. La operación no llama a la forja: la autoriza tener la llave, y la barrera de las
operaciones H impide que la lance un agente.

- Las llaves salen del ``project.yaml`` de la base (D-08), nunca del árbol de trabajo:
  ``evidence_signing.mode`` debe ser ``ci`` o ``admin`` y alguna llave de ``keys`` debe valer
  ahora (si no, ``E_CONFIG_EVIDENCE_KEY`` con ``mode_none`` o ``no_current_key``).
- ``<paquete>`` queda fuera del árbol de trabajo del proyecto (11-trazabilidad REQ-TRACE-85) y
  su ``index.txt`` cumple el formato de 11 §8.10; si no, ``E_VALIDATION_INPUT``.
- ``index.txt`` se lee una sola vez, en memoria: lo que se muestra en la confirmación es lo que
  se firma. La fase 2 pide teclear ``sign <carpeta>``, con el nombre de la carpeta del paquete.
- ``sign``: el núcleo nunca lee la llave privada (REQ-TRACE-94, REQ-OPS-70). Corre
  ``ssh-keygen -Y sign -f <archivo> -n <nombre>-evidence`` con los bytes del índice por la
  entrada estándar y toma la firma de la salida estándar; ``ssh-keygen`` pide la frase de paso
  en la terminal (o usa ``ssh-agent``). La firma se verifica con una llave vigente del proyecto
  antes de seguir (``E_CONFIG_EVIDENCE_KEY``: ``sign_failed`` o ``not_in_project``).
- ``seal``: la firma se sella como un registro ``integration`` (09-ci REQ-CI-53): las dos
  primeras TSA del catálogo que respondan y OpenTimestamps. Sin red, ``E_NET_OFFLINE`` antes de
  la fase 2, para no pedir la frase de paso en vano; sin ninguna prueba, ``E_NET_TIMEOUT``.
- ``write``: ``signature/`` (o ``signature-<n>/``) de una vez, con ``VERIFY.md``. Nunca
  reescribe una carpeta existente.

Con ``<PREFIJO>TESTING=1``, ``<PREFIJO>EVIDENCE_FAKE`` da la TSA y los calendarios de prueba,
como en ``ci seal`` (REQ-TRACE-89).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .. import _meta
from ..domain.signing import NAMESPACE, current_keys, find_key
from ..i18n import LANGS, Translator
from .context import env_flag
from .result import OpError, Result, ok
from .steps import Steps

if TYPE_CHECKING:
    from ..domain.signing import EvidenceKey
    from ..evidence.catalog import Catalog
    from ..evidence.package import SignatureFolder
    from ..evidence.record import Proof
    from .ci_seal import Sealers
    from .context import OpContext

__all__ = ["OP", "STEPS", "render_signature_verify", "render_text", "run"]

OP = "evidence.sign"
STEPS = ("sign", "seal", "write")
SSH_KEYGEN = "ssh-keygen"
SIGN_TIMEOUT_S = 600  # la persona teclea la frase de paso
MAX_SIGNATURE_BYTES = 16 * 1024


def _input_error(*errors: Mapping[str, object]) -> OpError:
    return OpError("E_VALIDATION_INPUT", details={"errors": [dict(e) for e in errors]})


def _local(ctx: OpContext, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else Path(ctx.inv.cwd) / path


def _inside(path: Path, root: Path) -> bool:
    """``path`` es ``root`` o está debajo, con las rutas reales."""
    try:
        real, base = os.path.realpath(path), os.path.realpath(root)
        return os.path.commonpath([real, base]) == base
    except ValueError:
        return False


def _key_error(reason: str, **extra: object) -> OpError:
    return OpError("E_CONFIG_EVIDENCE_KEY", details={"reason": reason, **extra})


def _offline(env: Mapping[str, str], sealers: Sealers, catalog: Catalog | None) -> bool:
    """Sin red: ``<PREFIJO>OFFLINE=1`` o el nombre de la primera TSA del catálogo no resuelve.

    Con los dobles de las pruebas (``transport``) no se consulta el DNS.
    """
    import socket

    if env_flag(env, "OFFLINE") == "1":
        return True
    if sealers.transport is not None or catalog is None:
        return False
    entries = catalog.sealing()
    host = urlsplit(entries[0].url).hostname if entries else None
    if not host:
        return False
    try:
        socket.getaddrinfo(host, 443)
    except OSError:
        return True
    return False


def _ssh_sign(exe: str, key: Path, data: bytes, env: Mapping[str, str]) -> bytes:
    """Firma con ``ssh-keygen -Y sign``: los bytes por la entrada estándar y la firma por la salida.

    La salida de error y la terminal quedan para ``ssh-keygen``, que pide ahí la frase de paso;
    el núcleo nunca ve la llave ni la frase (REQ-TRACE-94).
    """
    try:
        proc = subprocess.run(
            [exe, "-Y", "sign", "-f", str(key), "-n", NAMESPACE],
            input=data, stdout=subprocess.PIPE, env=dict(env), check=False, timeout=SIGN_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        raise _key_error("sign_failed") from None
    if proc.returncode != 0 or not proc.stdout or len(proc.stdout) > MAX_SIGNATURE_BYTES:
        raise _key_error("sign_failed")
    return proc.stdout


def _check_signature(signature: bytes, data: bytes, keys: Sequence[EvidenceKey]) -> tuple[EvidenceKey, str, str]:
    """La firma verifica con una llave vigente del proyecto: ``(llave, hash, huella)``."""
    from ..evidence import sshsig

    try:
        got = sshsig.verify(signature, data)
    except sshsig.SignatureError:
        raise _key_error("sign_failed") from None
    known = find_key(keys, got.public_key)
    if known is None:
        raise _key_error("not_in_project", key=got.fingerprint)
    return known, got.hash, got.fingerprint


def run(ctx: OpContext, *, package: object = None, key: object = None) -> Result:
    """``evidence sign <paquete> --key <archivo>``."""
    errors = [{"param": name, "reason": "type"} for name, value in (("package", package), ("key", key))
              if not isinstance(value, str) or not value]
    if errors:
        raise _input_error(*errors)
    assert isinstance(package, str) and isinstance(key, str)
    t = ctx.t()
    project = ctx.project()
    assert project is not None
    signing = ctx.base().project.evidence_signing
    if signing.mode == "none":
        raise _key_error("mode_none")
    now = ctx.inv.now()
    keys = current_keys(signing.keys, now)
    if not keys:
        raise _key_error("no_current_key")
    root, key_path = _local(ctx, package), _local(ctx, key)
    if not root.is_dir():
        errors.append({"param": "package", "reason": "not_found"})
    elif _inside(root, project.root):
        errors.append({"param": "package", "reason": "inside_project"})  # REQ-TRACE-85
    if not key_path.is_file():
        errors.append({"param": "key", "reason": "not_found"})
    if errors:
        raise _input_error(*errors)

    from ..evidence import verify as ev
    from ..evidence.catalog import load_catalog
    from ..evidence.package import PackageIndexError, parse_index, read_index

    data, problem = read_index(root)
    if data is None:
        raise _input_error({"param": "package", "reason": "no_index", "problem": problem or "missing"})
    try:
        index = parse_index(data)
    except PackageIndexError as exc:
        raise _input_error({"param": "package", "reason": "bad_index", "line": exc.line or 0}) from None
    if ev.missing_extra():
        raise OpError("E_CONFIG_TOOL_MISSING", details={"tool": "evidence"})
    exe = shutil.which(SSH_KEYGEN, path=ctx.inv.env.get("PATH"))
    if exe is None:
        raise OpError("E_CONFIG_TOOL_MISSING", details={"tool": SSH_KEYGEN})

    from .ci_seal import sealers_from_env

    env = ctx.inv.env
    sealers = sealers_from_env(env)
    catalog = sealers.catalog if sealers.catalog is not None else load_catalog()
    if _offline(env, sealers, catalog):
        raise OpError("E_NET_OFFLINE")
    index_sha = hashlib.sha256(data).hexdigest()
    lang = _files_lang(ctx)
    steps = Steps(ctx, OP, STEPS, deadline_s=None)  # la persona teclea la frase de paso
    if steps.dry_run:
        steps.effect("file_write", str(root / "signature"), t("evidence.sign.effect", folder=str(root)))
        return steps.dry_result()
    summary = t(
        "evidence.sign.summary", package=str(root), sha256=index_sha, files=len(index), key=str(key_path),
        keys=", ".join(f"{k.id} ({k.fingerprint})" for k in keys),
    )
    ctx.confirm_human("sign", root.name, summary)

    def sign() -> tuple[bytes, EvidenceKey, str, str]:
        signature = _ssh_sign(exe, key_path, data, env)
        known, hash_name, fingerprint = _check_signature(signature, data, keys)
        return signature, known, hash_name, fingerprint

    signature, known, hash_name, fingerprint = steps.run("sign", sign)

    def seal() -> list[Proof]:
        from ..evidence.record import INDEX_SIG_FILE
        from .ci_seal import request_proofs

        proofs, _rows = request_proofs(signature, "integration", sealers, subject=INDEX_SIG_FILE)
        if not any(p.status == "ok" for p in proofs):
            raise OpError("E_NET_TIMEOUT", details={"proofs": [_proof_brief(p) for p in proofs]})
        return proofs

    proofs = steps.run("seal", seal)

    def write() -> Path:
        from ..evidence.package import SignatureFolder, next_signature_dirname, write_signature_dir
        from ..evidence.record import INDEX_SIG_FILE, VERIFY_FILE, Signature

        folder = SignatureFolder(index_sha, len(index), Signature(INDEX_SIG_FILE, hash_name, fingerprint, known.id,
                                                                   signature), tuple(proofs))
        name = next_signature_dirname(root)
        verify = render_signature_verify(folder, name, lang, catalog).encode("utf-8")
        try:
            return write_signature_dir(root, folder, name=name, extra={VERIFY_FILE: verify})
        except OSError as exc:
            raise OpError("E_VALIDATION_INPUT", details={"errors": [{"param": "package", "reason": "not_writable",
                                                                      "error": type(exc).__name__}]}) from None

    written = steps.run("write", write)
    return ok(OP, {
        "package": str(root),
        "folder": written.name,
        "index_sha256": index_sha,
        "files": len(index),
        "key": fingerprint,
        "key_id": known.id,
        "proofs": [_proof_brief(p) for p in proofs],
    }, [])


def _proof_brief(proof: Proof) -> dict[str, object]:
    return {"type": proof.type, "source": proof.source, "status": proof.status,
            "time_utc": proof.time_utc, "error": proof.error}


def _files_lang(ctx: OpContext) -> str:
    """Idioma de ``VERIFY.md``: ``project.yaml: language`` de la base, nunca ``<PREFIJO>LANG`` (REQ-ARCH-70)."""
    lang = getattr(ctx.base().project, "language", None)
    return lang if lang in LANGS else "en"


# -- VERIFY.md de la carpeta de firma (11-trazabilidad REQ-TRACE-82) -------------------------


def render_signature_verify(folder: SignatureFolder, name: str, lang: str, catalog: Catalog | None = None) -> str:
    """``VERIFY.md`` de una carpeta de firma, de ``_data/evidence/VERIFY.signature.<lang>.md``."""
    from .ci_seal import EVIDENCE_DATA, fill_template, tsa_commands, tsa_row, verify_time

    template = (EVIDENCE_DATA / f"VERIFY.signature.{lang if lang in LANGS else 'en'}.md").read_text(encoding="utf-8")
    stamps = [p for p in folder.proofs if p.type == "rfc3161" and p.status == "ok"]
    values = {
        "name": _meta.NAME,
        "version": _meta.__version__,
        "folder": name,
        "files": str(folder.files),
        "index_sha256": folder.signed_sha256,
        "key_id": folder.signature.key_id,
        "key": folder.signature.key,
        "namespace": NAMESPACE,
        "verify_time": verify_time(stamps),
        "tsa_table": "\n".join(tsa_row(p, catalog) for p in stamps),
        "tsa_commands": "\n\n".join(tsa_commands(p, catalog, "index.txt.sig") for p in stamps),
    }
    return fill_template(template, values, {"tsa": bool(stamps), "no-tsa": not stamps})


# -- vista legible ---------------------------------------------------------------------------


def render_text(result: Result, t: Translator, *, fmt: str = "text") -> str:
    """Vista legible de ``data_evidence_sign``: la carpeta escrita, la llave y cada prueba."""
    del fmt
    data = result.data or {}
    if data.get("dry_run"):
        return "\n".join(item.get("summary", "") for item in data.get("plan", []))
    proofs = data.get("proofs", [])
    lines = [t("evidence.sign.done", folder=data.get("folder", ""), package=data.get("package", ""),
               key_id=data.get("key_id", ""), key=data.get("key", ""),
               ok=sum(p.get("status") == "ok" for p in proofs), total=len(proofs))]
    for proof in proofs:
        label = "RFC 3161" if proof.get("type") == "rfc3161" else "OpenTimestamps"
        state = proof.get("time_utc") or proof.get("status")
        lines.append(t("evidence.sign.proof", proof=label, source=proof.get("source", ""), state=state))
    if not any(p.get("type") == "rfc3161" and p.get("status") == "ok" for p in proofs):
        lines.append(t("evidence.sign.no_tsa"))
    return "\n".join(lines)
