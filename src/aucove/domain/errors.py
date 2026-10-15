# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Catálogo cerrado de errores, avisos y códigos de salida (05-operaciones §5, §12).

Datos puros. ``ERRORS``, ``WARNINGS`` y ``PERM_CODES`` coinciden con los enum de
``$defs/error_code``, ``$defs/warning_code`` y ``$defs/perm_code`` de
``schemas/result.schema.json`` (una prueba lo exige). ``params`` fija los
marcadores de ``error.<CODE>.message`` y ``.hint``: en H2 son vacíos para todos,
porque 05 §12.1 no tiene textos con variables; el dato variable va en ``details``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

Area = Literal[
    "CONFIG", "AUTH", "FORGE", "CLAIM", "PERM", "STATE", "VALIDATION", "NET", "HUMAN_ONLY", "INTERNAL"
]

AREAS: tuple[Area, ...] = (
    "CONFIG", "AUTH", "FORGE", "CLAIM", "PERM", "STATE", "VALIDATION", "NET", "HUMAN_ONLY", "INTERNAL"
)


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    """Un código de error de operación (05 §12.1)."""

    code: str
    area: Area
    retryable: bool
    params: frozenset[str] = frozenset()  # marcadores de error.<code>.*; en H2, vacío


# Orden y columna R de 05 §12.1 (igual al enum del esquema).
_ERROR_TABLE: tuple[tuple[str, bool], ...] = (
    ("E_CONFIG_NOT_A_PROJECT", False),
    ("E_CONFIG_INVALID", False),
    ("E_CONFIG_CONVENTION_UNSUPPORTED", False),
    ("E_CONFIG_CORE_MISMATCH", False),
    ("E_CONFIG_GIT_UNAVAILABLE", False),
    ("E_CONFIG_REMOTE_MISMATCH", False),
    ("E_CONFIG_BASE_NOT_DEFAULT", False),
    ("E_CONFIG_POLICY_UNAVAILABLE", False),
    ("E_CONFIG_DOCTOR_FAILED", False),
    ("E_CONFIG_TOOL_MISSING", False),  # evidence sign sin el extra o sin ssh-keygen (D-72)
    ("E_CONFIG_EVIDENCE_KEY", False),  # la llave del proyecto no se puede usar (D-72)
    ("E_AUTH_NOT_AUTHENTICATED", False),
    ("E_AUTH_TOKEN_INVALID", False),
    ("E_AUTH_INSUFFICIENT_SCOPE", False),
    ("E_AUTH_LOGIN_CHANGED", False),
    ("E_FORGE_NOT_FOUND", False),
    ("E_FORGE_RATE_LIMITED", True),
    ("E_FORGE_UNAVAILABLE", True),
    ("E_FORGE_CONFLICT", True),
    ("E_FORGE_UNEXPECTED", False),
    ("E_FORGE_CAPABILITY_MISSING", False),
    ("E_FORGE_PUSH_REJECTED", False),
    ("E_NET_OFFLINE", True),
    ("E_NET_TIMEOUT", True),
    ("E_NET_TLS", False),
    ("E_CLAIM_ALREADY_CLAIMED", False),
    ("E_CLAIM_NOT_HELD", False),
    ("E_CLAIM_LIMIT_REACHED", False),
    ("E_CLAIM_NO_SECTIONS", False),
    ("E_CLAIM_SECTION_BUSY", False),
    ("E_CLAIM_RESERVED", False),
    ("E_PERM_NOT_A_MEMBER", False),
    ("E_PERM_ROLE_REQUIRED", False),
    ("E_PERM_SECTION_NOT_OWNED", False),
    ("E_PERM_OUTSIDE_TASK", False),
    ("E_PERM_LEAD_ONLY_PATH", False),
    ("E_PERM_EDITOR_ONLY_PATH", False),
    ("E_PERM_APPEND_ONLY", False),
    ("E_PERM_GENERATED_PATH", False),
    ("E_PERM_LOCAL_PATH", False),
    ("E_PERM_OUTSIDE_REPO", False),
    ("E_PERM_SYMLINK", False),
    ("E_PERM_PATH_DENIED", False),
    ("E_PERM_NOT_REVIEWER", False),
    ("E_PERM_SELF_REVIEW", False),
    ("E_PERM_COMMAND_DENIED", False),
    ("E_PERM_SHELL_UNPARSEABLE", False),
    ("E_PERM_GUARD_TAMPER", False),
    ("E_PERM_OUT_OF_SCOPE_RESTORED", False),
    ("E_PERM_OUT_OF_SCOPE_COMMITTED", False),
    ("E_STATE_NOT_A_TASK", False),
    ("E_STATE_INVALID_TRANSITION", False),
    ("E_STATE_NOT_ON_TASK_BRANCH", False),
    ("E_STATE_DIRTY_WORKTREE", False),
    ("E_STATE_MERGE_CONFLICT", False),
    ("E_STATE_MERGE_IN_PROGRESS", False),
    ("E_STATE_BASE_REWRITTEN", False),
    ("E_STATE_UNSUPPORTED_DOOR", False),
    ("E_STATE_PR_DRAFT", False),
    ("E_STATE_PR_NOT_OPEN", False),
    ("E_STATE_PR_HEAD_CHANGED", False),
    ("E_STATE_BUSY", True),
    ("E_VALIDATION_INPUT", False),
    ("E_VALIDATION_FAILED", False),
    ("E_VALIDATION_UNKNOWN_SECTION", False),
    ("E_VALIDATION_UNKNOWN_PLAN_ID", False),
    ("E_VALIDATION_ROLES_OUT_OF_SYNC", False),
    ("E_VALIDATION_NOTE_MISSING", False),
    ("E_VALIDATION_DECISION_EXISTS", False),
    ("E_VALIDATION_CURSOR_STALE", False),
    ("E_VALIDATION_ASSESSMENT", False),
    ("E_VALIDATION_AI_USE_UNDECLARED", False),
    ("E_VALIDATION_AI_USE_CONFLICT", False),
    ("E_VALIDATION_SIGNOFF_MISSING", False),
    ("E_VALIDATION_ASSISTED_BY_MISSING", False),
    ("E_VALIDATION_EVIDENCE_FAILED", False),
    ("E_HUMAN_ONLY_NO_TTY", False),
    ("E_HUMAN_ONLY_AGENT_ENV", False),
    ("E_HUMAN_ONLY_NOT_CONFIRMED", False),
    ("E_INTERNAL_UNEXPECTED", False),
    ("E_INTERNAL_HOOK_DEADLINE", False),
)


def _area_from_code(code: str) -> Area:
    rest = code[2:]
    if rest.startswith("HUMAN_ONLY_"):
        return "HUMAN_ONLY"
    head = rest.split("_", 1)[0]
    for area in AREAS:
        if area == head:
            return area
    raise ValueError(f"código sin área conocida: {code}")


ERRORS: Mapping[str, ErrorSpec] = MappingProxyType(
    {code: ErrorSpec(code, _area_from_code(code), retryable) for code, retryable in _ERROR_TABLE}
)

# Orden de 05 §12.2 (igual al enum del esquema). W_FORGE_ACTIONS_RESTRICTED está
# reservado por D-39: nadie lo emite en H2.
_WARNING_TABLE: tuple[str, ...] = (
    "W_NET_OFFLINE",
    "W_NET_STALE_CACHE",
    "W_OUTPUT_TRUNCATED",
    "W_FORGE_REFLECTION_FAILED",
    "W_FORGE_PROFILE_ADVISORY",
    "W_FORGE_CAPABILITY_UNKNOWN",
    "W_FORGE_RULES_NOT_APPLIED",
    "W_FORGE_CHECKS_SPOOFABLE",
    "W_FORGE_ACTIONS_RESTRICTED",
    "W_AUTH_BROAD_TOKEN",
    "W_AUTH_WORKFLOW_SCOPE",
    "W_AUTH_LOGIN_CHANGED",
    "W_CONFIG_POLICY_FALLBACK",
    "W_PERM_DEPUTY_MISSING",
    "W_STATE_CHECKOUT_SKIPPED",
    "W_STATE_INIT_FILES_PENDING",
    "W_STATE_ANOMALY",
    "W_STATE_OUT_OF_SCOPE_DROPPED",
    "W_STATE_MAIN_BROKEN",
    "W_CLAIM_EXPIRING",
    "W_CLAIM_LIMIT_EXCEEDED",
    "W_CLAIM_SECTION_SHARED",
    "W_VALIDATION_UNTRUSTED_SANITIZED",
    "W_VALIDATION_CI_FAILING",
    "W_VALIDATION_UNCITED_OVERLAP",
    "W_VALIDATION_UNKNOWN_PLAN_IDS",
    "W_VALIDATION_MODEL_MISMATCH",
    "W_VALIDATION_NONE_WITH_AGENT",
    "W_CONFIG_TOOL_MISSING",
    "W_VALIDATION_EVIDENCE_WEAK",
)

WARNINGS: frozenset[str] = frozenset(_WARNING_TABLE)
WARNING_ORDER: tuple[str, ...] = _WARNING_TABLE

# Marcadores de warning.<CODE>; en H2, ninguno.
WARNING_PARAMS: Mapping[str, frozenset[str]] = MappingProxyType(
    {code: frozenset() for code in _WARNING_TABLE}
)

# Motivos de denegación de una ruta que devuelve check_path (06 §2.4; $defs/perm_code).
PERM_CODES: frozenset[str] = frozenset(
    {
        "E_PERM_OUTSIDE_REPO",
        "E_PERM_SYMLINK",
        "E_PERM_NOT_A_MEMBER",
        "E_PERM_LOCAL_PATH",
        "E_PERM_GENERATED_PATH",
        "E_STATE_NOT_ON_TASK_BRANCH",
        "E_CLAIM_NOT_HELD",
        "E_STATE_INVALID_TRANSITION",
        "E_PERM_OUTSIDE_TASK",
        "E_PERM_APPEND_ONLY",
        "E_PERM_SECTION_NOT_OWNED",
        "E_PERM_LEAD_ONLY_PATH",
        "E_PERM_EDITOR_ONLY_PATH",
        "E_CONFIG_POLICY_UNAVAILABLE",
        "E_INTERNAL_UNEXPECTED",
    }
)

# Tabla de 05 §5. El 2 está reservado para el subcomando 'hook' y no aparece aquí.
EXIT_BY_AREA: Mapping[Area, int] = MappingProxyType(
    {
        "INTERNAL": 1,
        "VALIDATION": 3,
        "PERM": 4,
        "AUTH": 5,
        "STATE": 6,
        "CLAIM": 6,
        "FORGE": 7,
        "NET": 7,
        "CONFIG": 8,
        "HUMAN_ONLY": 9,
    }
)

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_INTERRUPTED = 130  # Ctrl-C (05 §5)


def area_of(code: str) -> Area:
    """Área de un código de error; ``KeyError`` si el código no está en ``ERRORS``."""
    return ERRORS[code].area


def exit_code(ok: bool, code: str | None) -> int:
    """Código de salida según 05 §5: 0 si ``ok``; si no, el del área. Nunca devuelve 2."""
    if ok:
        return EXIT_OK
    spec = ERRORS.get(code) if code is not None else None
    if spec is None:
        return EXIT_INTERNAL
    return EXIT_BY_AREA[spec.area]


# Traducción de ForgeError.kind (08 §2.1) a códigos de operación: las 16 filas de
# 05 §12.3. 'pr_exists' no es error y 'ref_exists' depende de la operación.
FORGE_KIND_CODES: Mapping[str, str | None] = MappingProxyType(
    {
        "no_token": "E_AUTH_NOT_AUTHENTICATED",
        "auth": "E_AUTH_TOKEN_INVALID",
        "permission": "E_AUTH_INSUFFICIENT_SCOPE",
        "login_changed": "E_AUTH_LOGIN_CHANGED",
        "not_found": "E_FORGE_NOT_FOUND",
        "ref_exists": "E_FORGE_CONFLICT",
        "ref_conflict": "E_FORGE_CONFLICT",
        "merge_conflict": "E_STATE_MERGE_CONFLICT",
        "pr_exists": None,
        "no_commits": "E_FORGE_UNEXPECTED",
        "validation": "E_FORGE_UNEXPECTED",
        "unsupported": "E_FORGE_CAPABILITY_MISSING",
        "rate_limited": "E_FORGE_RATE_LIMITED",
        "unavailable": "E_FORGE_UNAVAILABLE",
        "timeout": "E_NET_TIMEOUT",
        "unreachable": "E_NET_OFFLINE",
        "tls": "E_NET_TLS",
    }
)


def code_for_forge_kind(kind: str, op: str) -> str | None:
    """Código de operación para una excepción de la forja (05 §12.3, REQ-OPS-63).

    Devuelve ``None`` cuando no es un error (``pr_exists``) y para ``ref_exists`` en
    ``claim``, que resuelve el paso ``create_ref`` de 05 §9.12 (H3). Un ``kind``
    desconocido se trata como respuesta no prevista (``E_FORGE_UNEXPECTED``).
    """
    if kind == "ref_exists" and op == "claim":
        return None
    if kind not in FORGE_KIND_CODES:
        return "E_FORGE_UNEXPECTED"
    return FORGE_KIND_CODES[kind]
