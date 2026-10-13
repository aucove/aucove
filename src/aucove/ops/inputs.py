# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Validación de los argumentos de una operación contra su ``inputSchema`` (05 §4 y §9.1).

La comparten la CLI (``--input`` y los flags) y el servidor MCP (``tools/call``), para que
un argumento inválido dé el mismo ``E_VALIDATION_INPUT`` con los mismos ``details`` por las
dos superficies (REQ-OPS-15, REQ-OPS-58).
"""

from __future__ import annotations

from collections.abc import Mapping

__all__ = ["input_error", "schema_errors"]


def input_error(param: str | None, reason: str, **extra: object) -> dict[str, object]:
    """Una entrada de ``details.errors`` de ``E_VALIDATION_INPUT``: ``{param, reason[, ...]}``."""
    entry: dict[str, object] = {"param": param, "reason": reason}
    entry.update(extra)
    return entry


def schema_errors(instance: object, schema: Mapping[str, object]) -> list[dict[str, object]]:
    """Errores de ``instance`` contra ``schema`` como ``{param, reason[, index]}``, en orden estable."""
    from jsonschema import Draft202012Validator  # perezoso: solo al validar argumentos

    found: dict[tuple[str, str, int], dict[str, object]] = {}
    for error in Draft202012Validator(dict(schema)).iter_errors(instance):
        path = list(error.absolute_path)
        entries: list[dict[str, object]] = []
        if not path and error.validator == "required" and isinstance(error.instance, dict):
            entries = [input_error(str(name), "required") for name in error.validator_value
                       if name not in error.instance]
        elif not path and error.validator == "additionalProperties" and isinstance(error.instance, dict):
            known = error.schema.get("properties", {}) if isinstance(error.schema, dict) else {}
            entries = [input_error(str(name), "unknown") for name in error.instance if name not in known]
        else:
            param = str(path[0]) if path else None
            if len(path) > 1 and isinstance(path[1], int):
                entries = [input_error(param, str(error.validator), index=path[1])]
            else:
                entries = [input_error(param, str(error.validator))]
        for entry in entries:
            index = entry.get("index")
            key = (str(entry["param"] or ""), str(entry["reason"]), index if isinstance(index, int) else -1)
            found.setdefault(key, entry)
    return [found[key] for key in sorted(found)]
