# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Textos del área ``evidence`` (EV-07; 01-arquitectura REQ-ARCH-72..74).

Cada motivo estable de la capa ``evidence`` tiene texto en ``es`` y en ``en``: los de
``ManifestError``, ``Skipped``, ``RecordError``, ``CanonicalError``, los problemas de
carpeta, ``check_manifest`` y los ``Check.reason`` de ``tsa``, ``revocation`` y ``ots``,
más los motivos propios de ``evidence verify``. Las listas se cotejan por AST con los
literales del código, para que un motivo nuevo no quede sin texto. Las claves se arman con
f-strings, así que ``test_completeness`` no las ve: esta prueba las cubre.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from functools import cache
from pathlib import Path

import pytest

from aucove import _meta
from aucove.evidence import canon, manifest
from aucove.evidence import verify as ev
from aucove.i18n import LANGS, load_catalog, placeholders
from tests.conftest import REPO_ROOT

EVIDENCE = REPO_ROOT / "src" / _meta.NAME / "evidence"

FAMILIES: dict[str, tuple[str, ...]] = {
    "evidence.manifest_error": manifest.REASONS,
    "evidence.canonical": canon.REASONS,
    "evidence.skipped": ("non_utf8", "path_too_long", "gitlink", "actor_not_utf8", "actor_too_long"),
    "evidence.record_error": ("invalid", "inconsistent", "missing"),
    "evidence.folder": ("missing", "too_large", "unreadable", "invalid"),  # RecordFolder.problems
    "evidence.manifest_problem": ev.MANIFEST_PROBLEMS,
}

# Marcadores de cada texto con datos; los demás no llevan ninguno.
PARAMS: dict[str, frozenset[str]] = {
    "evidence.check.manifest.ok": frozenset({"entries", "actors"}),
    "evidence.check.manifest.unreadable": frozenset({"problem"}),
    "evidence.check.manifest.invalid_json": frozenset({"problem"}),
    "evidence.check.manifest.schema": frozenset({"pointer"}),
    "evidence.check.manifest.record_invalid": frozenset({"problem"}),
    "evidence.check.manifest.record_schema": frozenset({"pointer"}),
    "evidence.check.manifest.record_mismatch": frozenset({"field"}),
    "evidence.check.manifest.unverifiable": frozenset({"error"}),  # el tipo del error, nunca su mensaje
    "evidence.check.tsa.ok": frozenset({"revocation"}),
    "evidence.check.tsa.unreadable": frozenset({"problem"}),
    "evidence.check.ots.pending": frozenset({"n"}),
    "evidence.check.ots.bitcoin_unverified": frozenset({"height", "merkle_root"}),
    "evidence.check.ots.unreadable": frozenset({"problem"}),
    "evidence.check.openings.ok": frozenset({"n"}),
    "evidence.check.openings.unreadable": frozenset({"problem"}),
    "evidence.check.openings.schema": frozenset({"pointer"}),
    "evidence.check.openings.mismatch": frozenset({"n", "total"}),
    "evidence.check.actors.ok": frozenset({"roles"}),
    "evidence.check.actors.mismatch": frozenset({"roles"}),
    "evidence.check.git.ok": frozenset({"blobs", "actors"}),
    "evidence.check.git.missing_base": frozenset({"blobs", "actors"}),  # lo que sí se cotejó
    "evidence.check.git.blob_mismatch": frozenset({"n"}),
    "evidence.check.git.missing_blob": frozenset({"n"}),
    "evidence.check.git.content_mismatch": frozenset({"n"}),
    "evidence.check.git.size_mismatch": frozenset({"n"}),
    "evidence.check.git.actor_mismatch": frozenset({"roles"}),
    "evidence.note.more": frozenset({"n"}),
    "evidence.check.content.unsafe_path": frozenset({"n"}),  # la ruta no se muestra (REQ-OPS-65)
    **{
        f"evidence.check.content.{reason}": frozenset({"path"})
        for reason in ev.REASONS["content"]
        if reason not in ("no_files", "no_openings", "unsafe_path")
    },
}


@cache
def catalog(lang: str) -> dict[str, str]:
    return dict(load_catalog(lang))  # type: ignore[arg-type]


def detail_keys() -> Iterator[str]:
    for check, reasons in ev.REASONS.items():
        for reason in reasons:
            yield ev.detail_key(check, reason)
    for reason in ev.MANIFEST_PROBLEMS:
        yield ev.detail_key("manifest", reason)


def all_keys() -> list[str]:
    keys = set(detail_keys())
    keys |= {f"{family}.{code}" for family, codes in FAMILIES.items() for code in codes}
    keys |= {f"evidence.verdict.{v}" for v in ("verified", "pending", "failed")}
    keys |= {f"evidence.confidence.{c}" for c in ("strong", "weak")}
    keys |= {f"evidence.kind.{k}" for k in ("submission", "integration")}
    keys |= {f"evidence.status.{s}" for s in ("pass", "weak", "pending", "skip", "fail")}
    keys |= {f"evidence.check_name.{c}" for c in ev.CHECKS}
    keys |= {f"evidence.note.{n}" for n in ("more", "upgraded", "upgrade_dry_run", "upgrade_not_written", "completed")}
    return sorted(keys)


@pytest.mark.parametrize("lang", LANGS)
def test_every_key_has_text(lang: str) -> None:
    missing = [key for key in all_keys() if key not in catalog(lang)]
    assert missing == []


def test_placeholders() -> None:
    problems: list[str] = []
    for key in all_keys():
        expected = PARAMS.get(key, frozenset())
        for lang in LANGS:
            text = catalog(lang).get(key, "")
            if placeholders(text) != expected:
                problems.append(f"{key} ({lang}): {sorted(placeholders(text))} != {sorted(expected)}")
    assert problems == []


def test_wording_rules() -> None:
    """«fecha verificable», nunca «fecha cierta» (REQ-TRACE-75); «parte», nunca «sección»."""
    for lang in LANGS:
        for key, text in catalog(lang).items():
            if key.startswith("evidence."):
                lowered = text.lower()
                assert "fecha cierta" not in lowered and "sección" not in lowered and "section" not in lowered, key


# -- los motivos del código tienen texto -------------------------------------------------------------


def _calls(path: Path) -> Iterator[tuple[str, str, ast.Call]]:
    """``(función que llama, nombre llamado, llamada)`` de cada llamada de ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Call):
                callee = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                yield func.name, callee, node


def _strings(node: ast.AST | None) -> set[str]:
    """Cadenas literales de ``node``, sin las partes fijas de los f-strings (se cubren aparte)."""
    if node is None:
        return set()
    parts = {id(v) for n in ast.walk(node) if isinstance(n, ast.JoinedStr) for v in n.values}
    return {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in parts}


def literal_reasons(module: str, callee: str, position: int, *, inside: str = "") -> set[str]:
    """Motivos literales que se pasan a ``callee`` en ``position`` dentro de ``evidence/<module>.py``."""
    found: set[str] = set()
    for func, name, call in _calls(EVIDENCE / f"{module}.py"):
        if name == callee and func.startswith(inside) and len(call.args) > position:
            found |= _strings(call.args[position])
    return found


def test_check_reasons_of_tsa_revocation_and_catalog_have_text() -> None:
    reasons = literal_reasons("tsa", "Check", 2) | literal_reasons("revocation", "Check", 2) | literal_reasons("catalog", "Check", 2)
    reasons |= literal_reasons("tsa", "_TokenError", 0, inside="_check_token")  # los que llegan a verify_token
    reasons |= {"ocsp_invalid", "crl_invalid"}  # f"{kind}_invalid" en revocation._judge
    assert reasons and reasons <= set(ev.REASONS["tsa"]), sorted(reasons - set(ev.REASONS["tsa"]))


def test_check_reasons_of_ots_have_text() -> None:
    reasons = literal_reasons("ots", "Check", 2) | {"missing_extra"}  # skip_check
    assert reasons and reasons <= set(ev.REASONS["ots"]), sorted(reasons - set(ev.REASONS["ots"]))


def test_manifest_record_canonical_and_skipped_reasons_have_text() -> None:
    assert literal_reasons("manifest", "ManifestError", 0) <= set(manifest.REASONS)
    assert literal_reasons("canon", "CanonicalError", 0) <= set(canon.REASONS)
    assert literal_reasons("record", "RecordError", 0) <= set(FAMILIES["evidence.record_error"])
    skipped = literal_reasons("manifest", "Skipped", 0)
    tree = ast.parse((EVIDENCE / "manifest.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):  # _skip_reason devuelve los de las rutas
        if isinstance(node, ast.FunctionDef) and node.name == "_skip_reason":
            skipped |= {s for ret in ast.walk(node) if isinstance(ret, ast.Return) for s in _strings(ret.value)}
    assert skipped and skipped <= set(FAMILIES["evidence.skipped"]), sorted(skipped - set(FAMILIES["evidence.skipped"]))
    assert set(manifest.check_manifest({"entries": [{"p": "b"}, {"p": "a"}, {"p": "a"}], "actors": [{"r": "x"}, {"r": "x"}],
                                        "commits": []})) <= set(ev.MANIFEST_PROBLEMS)
