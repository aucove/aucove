# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Copia local de los registros ``ci:evidence`` (05-operaciones REQ-OPS-66; 01 §7.3).

``status`` y ``submit`` leen de la forja los comentarios de los PR propios y le pasan a
este módulo cada cuerpo con su autor; aquí no se toca la red. Cada registro va a
``.aucove/evidence/<kind>-<commit>-a<n>/``:

- con ``record.json`` y, si el registro no venía truncado, ``manifest.json`` (bytes
  exactos), ``openings.json``, ``manifest.json.ots`` y los ``.tsr``, cadenas y OCSP o CRL
  decodificados de ``data``, como la carpeta del artefacto (``record.record_files``);
- solo si la carpeta no existe: una copia nunca se reescribe y ningún proceso del
  núcleo borra ``evidence/`` (REQ-ARCH-59).

Solo cuenta un comentario cuyo autor es un actor de CI (``github-actions[bot]``,
08 REQ-FORGE-43): un comentario con el marcador escrito por otra persona no es un sello.
El marcador debe coincidir con el registro (``kind``, ``commit`` y ``run_attempt``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from .. import _meta
from .record import Record, RecordError, load_comment_json, local_dirname, write_record_dir

__all__ = [
    "MARKER_KIND",
    "CopyOutcome",
    "copy_comment",
    "evidence_json",
    "parse_evidence_comment",
]

MARKER_KIND = "evidence"  # ci:evidence (00-marco §5.14)
_MARKER_RE = re.compile(
    r"<!-- " + re.escape(_meta.NAME) + r":ci:" + MARKER_KIND
    + r" v1 kind=(submission|integration) sha=([0-9a-f]{40}) attempt=([1-9][0-9]{0,2})"
)


@dataclass(frozen=True)
class EvidenceComment:
    """Primera línea del marcador y el JSON del registro."""

    kind: str
    sha: str
    attempt: int
    json_text: str


Outcome = Literal["copied", "copied_truncated", "exists", "not_evidence", "untrusted_author", "invalid"]


@dataclass(frozen=True)
class CopyOutcome:
    """Resultado de copiar un comentario; ``folder`` es la carpeta (nueva o la que ya había)."""

    outcome: Outcome
    folder: str | None = None


def parse_evidence_comment(body: str) -> EvidenceComment | None:
    """Marcador ``ci:evidence`` v1 y su JSON (segunda línea); ``None`` si el cuerpo no lo es."""
    lines = body.lstrip("\N{ZERO WIDTH NO-BREAK SPACE}").split("\n")
    if len(lines) < 3 or lines[2].strip() != "-->":
        return None
    m = _MARKER_RE.fullmatch(lines[0].rstrip("\r"))
    if m is None:
        return None
    return EvidenceComment(m[1], m[2], int(m[3]), lines[1].rstrip("\r"))


def evidence_json(body: str) -> str | None:
    """JSON ``evidence-record/v1`` de un comentario ``ci:evidence``; ``None`` si no lo es."""
    parsed = parse_evidence_comment(body)
    return parsed.json_text if parsed is not None else None


def _record_only(record: Record) -> Record:
    """El registro sin lo embebido: ``record_files`` escribe entonces solo ``record.json``."""
    proofs = tuple(
        replace(
            p,
            data=None,
            chain_pem=None,
            root_pem=None,
            revocation=replace(p.revocation, data=None) if p.revocation is not None else None,
        )
        for p in record.proofs
    )
    return replace(record, manifest=None, openings=None, proofs=proofs)


def copy_comment(root: Path, body: str, author: str, *, ci_actors: Iterable[str]) -> CopyOutcome:
    """Copia el registro del comentario ``body`` en ``root`` (``.aucove/evidence/``).

    Nunca lanza por el contenido del comentario (es de terceros): devuelve ``invalid``.
    Los errores del sistema de archivos (``OSError``) sí se propagan, para que quien llama
    los registre (REQ-OPS-66).
    """
    parsed = parse_evidence_comment(body)
    if parsed is None:
        return CopyOutcome("not_evidence")
    if not any(author.casefold() == a.casefold() for a in ci_actors):
        return CopyOutcome("untrusted_author")
    try:
        record = load_comment_json(parsed.json_text)
        if (record.kind, record.commit, record.run_attempt) != (parsed.kind, parsed.sha, parsed.attempt):
            return CopyOutcome("invalid")
        folder = local_dirname(record.kind, record.commit, record.run_attempt)
    except RecordError:
        return CopyOutcome("invalid")
    final = Path(root) / folder
    if final.exists() or final.is_symlink():
        return CopyOutcome("exists", folder)
    truncated = record.truncated
    try:
        write_record_dir(Path(root), _record_only(record) if truncated else record, name=folder)
    except FileExistsError:  # otra invocación la copió entre la comprobación y el rename
        return CopyOutcome("exists", folder)
    except RecordError:
        return CopyOutcome("invalid")
    return CopyOutcome("copied_truncated" if truncated else "copied", folder)
