# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Lectura de ``diagnostics.json`` (09-ci REQ-CI-21).

``diagnostics.json`` es el sobre ``<NAME>.result/v1`` que escribe
``validate --mode build-log --log <f> --report <diagnostics.json>`` (05 §9.5):
``data.mode = 'build-log'`` y ``results = [{check: 'latex-log', status, findings}]``.
Los cinco campos de REQ-CI-21 (archivo, línea, severidad, mensaje y regla) son
``path``, ``line``, ``severity``, ``message`` y ``results[].check``. La escritura
es de la regla ``latex-log``; la lectura, de ``ci comment``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .. import _meta
from .result import clip, read_envelope

MAX_DIAGNOSTICS = 200
MAX_MESSAGE = 300
MAX_BYTES = 5 * 2**20


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """Un diagnóstico de la compilación."""

    file: str | None
    line: int | None
    severity: Literal["error", "warning"]
    message: str
    rule: str  # results[].check


def clip_message(s: str) -> str:
    """Una sola línea (espacios colapsados) de como máximo ``MAX_MESSAGE`` caracteres."""
    return clip(" ".join(s.split()), MAX_MESSAGE)


def _diagnostic(rule: str, finding: object) -> Diagnostic:
    if not isinstance(finding, Mapping):
        raise ValueError("hallazgo que no es un objeto")
    path = finding.get("path")
    line = finding.get("line")
    severity = finding.get("severity")
    message = finding.get("message")
    if path is not None and not isinstance(path, str):
        raise ValueError("path inválido")
    if line is not None and (isinstance(line, bool) or not isinstance(line, int) or line < 1):
        raise ValueError("line inválida")
    if severity not in ("error", "warning"):
        raise ValueError("severity inválida")
    if not isinstance(message, str):
        raise ValueError("message inválido")
    return Diagnostic(file=path, line=line, severity=severity, message=clip_message(message), rule=rule)


def diagnostics_from_envelope(env: Mapping[str, object]) -> list[Diagnostic]:
    """Diagnósticos de un sobre de ``validate --mode build-log``; ``ValueError`` si no lo es."""
    if env.get("schema") != _meta.RESULT_SCHEMA or env.get("op") != "validate":
        raise ValueError("no es un sobre de validate")
    data = env.get("data")
    if not isinstance(data, Mapping) or data.get("mode") != "build-log":
        raise ValueError("no es un sobre de build-log")
    results = data.get("results")
    if not isinstance(results, list):
        raise ValueError("results ausente")
    out: list[Diagnostic] = []
    for result in results:
        if not isinstance(result, Mapping):
            raise ValueError("resultado que no es un objeto")
        rule = result.get("check")
        findings = result.get("findings")
        if not isinstance(rule, str) or not rule or not isinstance(findings, list):
            raise ValueError("resultado sin check o findings")
        for finding in findings:
            out.append(_diagnostic(rule, finding))
    return out[:MAX_DIAGNOSTICS]


def load_diagnostics(path: Path) -> list[Diagnostic] | None:
    """Diagnósticos de ``path``; ``None`` si falta, supera 5 MB o no es un sobre de build-log válido."""
    env = read_envelope(path, max_bytes=MAX_BYTES)
    if env is None:
        return None
    try:
        return diagnostics_from_envelope(env)
    except ValueError:
        return None
