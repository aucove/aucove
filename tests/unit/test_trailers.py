# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Trailers de los commits del núcleo y detección de la herramienta (11-trazabilidad §2)."""

from __future__ import annotations

import pytest

from aucove import _meta
from aucove.domain.trailers import (
    Detection,
    Entry,
    SessionModel,
    canonical_uses,
    commit_message,
    detect_tool,
    resolve_assistance,
    trailer_block,
)

P = _meta.ENV_PREFIX


def test_uses_follow_the_table_order() -> None:
    assert canonical_uses(["review", "drafting", "editing"]) == ("drafting", "editing", "review")
    assert canonical_uses(["none"]) == ("none",)


@pytest.mark.parametrize(("uses", "reason"), [
    (["none", "drafting"], "solo"),
    (["undeclared", "code"], "solo"),
    (["code", "code"], "duplicate"),
    (["poetry"], "unknown"),
    ([], "empty"),
])
def test_invalid_declarations(uses: list[str], reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        canonical_uses(uses)


def test_detection_order() -> None:
    """REQ-TRACE-15 y REQ-TRACE-16: la variable declarada gana; CLAUDECODE solo con MCP o subproceso."""
    assert detect_tool({P + "AI_TOOL": "codex", "CLAUDECODE": "1"}, via_mcp=True) == Detection("codex", f"env:{P}AI_TOOL")
    assert detect_tool({"GEMINI_CLI": "1", "CLAUDECODE": "1"}, via_mcp=True).tool == "gemini-cli"  # type: ignore[union-attr]
    assert detect_tool({"CLAUDECODE": "1"}, via_mcp=True).tool == "claude-code"  # type: ignore[union-attr]
    assert detect_tool({"CLAUDECODE": "1", "CLAUDE_CODE_CHILD_SESSION": "1"}, via_mcp=False).tool == "claude-code"  # type: ignore[union-attr]
    assert detect_tool({"CLAUDECODE": "1"}, via_mcp=False) is None  # terminal del IDE: escribe la persona
    assert detect_tool({P + "AI_TOOL": "No Valida"}, via_mcp=False) is None


def test_session_model_and_declarations() -> None:
    det = Detection("claude-code", "env:CLAUDECODE")
    entries, warnings = resolve_assistance(det, SessionModel("claude-opus-5-5", "PostModelSwitch"), {}, None, None)
    assert [e.value for e in entries] == ["claude-code:claude-opus-5-5"] and warnings == []
    assert entries[0].model_source == "hook:PostModelSwitch"
    # REQ-TRACE-17: misma herramienta y otro modelo declarado: se conservan las dos y se avisa
    entries, warnings = resolve_assistance(det, SessionModel("claude-opus-5-5", None), {}, "claude-code", "claude-fable-5-1")
    assert [e.value for e in entries] == ["claude-code:claude-fable-5-1", "claude-code:claude-opus-5-5"]
    assert warnings == ["W_VALIDATION_MODEL_MISMATCH"]
    # sin registro de sesión, el modelo es unknown salvo que la variable lo declare (REQ-TRACE-14)
    entries, _ = resolve_assistance(det, None, {}, None, None)
    assert [e.value for e in entries] == ["claude-code:unknown"]
    entries, _ = resolve_assistance(det, None, {P + "AI_MODEL": "claude-haiku-5-5"}, None, None)
    assert [e.value for e in entries] == ["claude-code:claude-haiku-5-5"]
    # tool:unknown se quita si hay tool:<modelo>, sin aviso porque la sesión no registró modelo;
    # sin herramienta detectada, la declarada es "other"
    entries, warnings = resolve_assistance(det, None, {}, None, "claude-opus-5-5")
    assert [e.value for e in entries] == ["claude-code:claude-opus-5-5"] and warnings == []
    entries, _ = resolve_assistance(None, None, {}, None, "gpt-x")
    assert [e.value for e in entries] == ["other:gpt-x"]


def test_block_order_and_grammar() -> None:
    """REQ-TRACE-2 a REQ-TRACE-4 y REQ-TRACE-6."""
    entries = [Entry("codex", "o9", "declared", "declared"), Entry("claude-code", "claude-opus-5-5", "x", "y")]
    block = trailer_block(task=42, sections=["results", "methods", "methods"], ai_use=["editing", "drafting"],
                          assisted=entries)
    assert block == (
        "Task: #42\nSection: methods\nSection: results\nAI-Use: drafting, editing\n"
        "Assisted-by: claude-code:claude-opus-5-5\nAssisted-by: codex:o9\n"
    )
    assert trailer_block(task=7, sections=[], ai_use=["none"], assisted=entries) == "Task: #7\nAI-Use: none\n"
    message = commit_message("  Agrega criterios\r\n", trailer_block(task=7, sections=[], ai_use=["code"], assisted=[]))
    assert message == "Agrega criterios\n\nTask: #7\nAI-Use: code\n"


def test_parse_takes_the_last_paragraph_and_ignores_unknown_keys() -> None:
    """REQ-TRACE-30 y REQ-TRACE-31: el último párrafo, claves sin mayúsculas, sin --- como separador."""
    from aucove.domain.trailers import parse_trailers

    message = ("Asunto\n\nCuerpo con Clave: no cuenta.\n---\n\ntask: #42\nSECTION: methods\nAI-Use: drafting, editing\n"
               "Assisted-by: claude-code:anthropic.claude-v1:0\nAssisted-by: mala sintaxis\nRandom: x\n")
    t = parse_trailers(message.replace("\n", "\r\n"))
    assert (t.task, t.sections, t.ai_use) == (42, ("methods",), ("drafting", "editing"))
    assert t.assisted == (("claude-code", "anthropic.claude-v1:0"),)  # el primer ':' separa (§2.2)
    joined = parse_trailers("Asunto\n\nTask: #7\nAI-Use: code\n\nCo-authored-by: Claude <noreply@anthropic.com>\n")
    assert joined.task == 7 and joined.co_authors == ("noreply@anthropic.com",)
    assert parse_trailers("Asunto\n\nTexto normal al final.").ai_use is None
    assert parse_trailers("Solo un asunto").task is None
    assert parse_trailers("Asunto\n\nAI-Use: none, code").ai_use is None  # inválido: cuenta como undeclared


# -- validate_trailers (REQ-TRACE-32) ----------------------------------------------------------------

SIGNED = "Signed-off-by: Ana Pérez <12345+ana-perez@users.noreply.github.com>\n"
CLAUDE = Entry("claude-code", "claude-opus-5-5", "env:CLAUDECODE", "hook:SessionStart")


def _problems(message: str, context: str = "branch-commit", **kw: object) -> list[tuple[str, str, str]]:
    from aucove.domain.trailers import validate_trailers

    check = validate_trailers(message, context, **kw)  # type: ignore[arg-type]
    return [(p.code, p.key, p.reason) for p in check.problems]


def test_what_the_core_writes_is_valid() -> None:
    block = trailer_block(task=42, sections=["methods"], ai_use=["drafting", "editing"], assisted=[CLAUDE])
    assert _problems(commit_message("Agrega criterios", block), task=42) == []
    body = "## Resumen\n\nTexto.\n\n" + block + SIGNED
    assert _problems(body, "pr-body", task=42, subject=False) == []
    assert _problems("Título (#57)\n\n" + body, "squash-commit", task=42) == []


def test_a_branch_commit_needs_ai_use_and_its_task() -> None:
    assert _problems("Asunto sin trailers") == [("E_VALIDATION_TRAILER_MISSING", "AI-Use", "missing")]
    assert _problems("Asunto\n\nAI-Use: code\n", task=12) == [("E_VALIDATION_TRAILER_MISSING", "Task", "missing")]
    assert _problems("Asunto\n\nTask: #13\nAI-Use: code\n", task=12) == [
        ("E_VALIDATION_TRAILER_MISSING", "Task", "other_task")]
    assert _problems("Asunto\n\nAI-Use: undeclared\n") == []  # en la rama se admite


@pytest.mark.parametrize(("lines", "key", "reason"), [
    ("Task:#12\nAI-Use: code", "Task", "separator"),
    ("Task: #012\nAI-Use: code", "Task", "grammar"),
    ("Section: Methods\nAI-Use: code", "Section", "grammar"),
    ("AI-Use: Drafting", "AI-Use", "grammar"),
    ("AI-Use: drafting,editing", "AI-Use", "grammar"),
    ("AI-Use: editing, drafting", "AI-Use", "use_order"),
    ("AI-Use: none, code", "AI-Use", "solo"),
    ("AI-Use: code, code", "AI-Use", "duplicate"),
    ("AI-Use: magic", "AI-Use", "unknown"),
    ("AI-Use: code\nAssisted-by: Claude Code:x", "Assisted-by", "grammar"),
    ("AI-Use: code\nSigned-off-by: Ana <ana>", "Signed-off-by", "grammar"),
    ("AI-Use: code\nTask: #12", "Task", "key_order"),
    ("AI-Use: code\nAI-Use: code", "AI-Use", "repeated"),
    ("AI-Use: none\nAssisted-by: claude-code:x", "Assisted-by", "with_none"),
])
def test_grammar_order_and_cardinality(lines: str, key: str, reason: str) -> None:
    found = _problems(f"Asunto\n\n{lines}\n")
    assert ("E_VALIDATION_TRAILER_SYNTAX", key, reason) in found


def test_keys_ignore_case_and_unknown_keys() -> None:
    message = "Asunto\n\nai-use: code\nassisted-by: codex:gpt-6\nChange-Id: I123\n\nCo-authored-by: X <x@example.org>\n"
    assert _problems(message) == []


def test_a_submission_needs_a_declaration_assistance_and_signoff() -> None:
    assert _problems("Cuerpo\n\nAI-Use: undeclared\n" + SIGNED, "pr-body", subject=False) == [
        ("E_VALIDATION_TRAILER_MISSING", "AI-Use", "undeclared")]
    assert _problems("Cuerpo\n\nAI-Use: code\n" + SIGNED, "pr-body", subject=False) == [
        ("E_VALIDATION_TRAILER_MISSING", "Assisted-by", "missing")]
    assert _problems("Cuerpo\n\nAI-Use: none\n", "pr-body", subject=False) == [
        ("E_VALIDATION_TRAILER_MISSING", "Signed-off-by", "missing")]
    assert _problems("AI-Use: none\n" + SIGNED, "pr-body", subject=False) == []  # un cuerpo no tiene asunto


def test_parse_signoff() -> None:
    from aucove.domain.trailers import parse_signoff, validate_trailers

    assert parse_signoff("Ana Pérez <a@b.org>") == ("Ana Pérez", "a@b.org")
    assert parse_signoff("Ana <ab.org>") is None and parse_signoff("Ana<a@b.org>") is None
    check = validate_trailers("Cuerpo\n\nAI-Use: none\n" + SIGNED, "pr-body", subject=False)
    assert check.signed_off == SIGNED.split(": ", 1)[1].strip()
