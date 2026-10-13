# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Matriz nivel de dato × tipo de cuenta (10-seguridad §8.3 y §8.4; REQ-SEC-64; D-48).

Qué ``people[].account_type`` de ``roles.yaml`` admite el ``data_policy.level`` de
``project.yaml``, con el ``require_commercial`` de la configuración efectiva
(03-esquemas REQ-SCHEMA-91). Datos puros: la regla ``schemas`` convierte el veredicto
en hallazgos (``ops.rules.schemas``, el ``check_accounts`` de 10 §8.4).

- Solo hay filas para ``public`` e ``internal``: ``restricted`` y ``local_only`` no se
  admiten en v0.1 (REQ-SEC-63; el esquema ya los rechaza).
- Con ``require_commercial`` efectivo ``true`` no se admite ninguna cuenta de consumo,
  en ningún nivel (el pseudocódigo de 10 §8.4).
- C1: ``consumer_training_off`` con ``internal`` se admite solo con
  ``require_commercial`` ``false``, y con un aviso: rigen los términos de consumo.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal

# Valores de 03-esquemas §4.4 (enum de roles.schema.json).
ACCOUNT_TYPES: tuple[str, ...] = (
    "consumer_training_on", "consumer_training_off", "team", "enterprise", "api",
    "cloud_provider", "local", "none", "unknown",
)
CONSUMER_ACCOUNTS: frozenset[str] = frozenset({"consumer_training_on", "consumer_training_off"})

# Celdas ✓ y C de 10 §8.3 por nivel (ALLOWED del pseudocódigo de §8.4).
ALLOWED: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "public": frozenset(ACCOUNT_TYPES),
        "internal": frozenset(
            {"consumer_training_off", "team", "enterprise", "api", "cloud_provider", "local", "none"}
        ),
    }
)

# allowed: sin hallazgo. consumer_terms: admitida por C1, con aviso. not_allowed: la
# matriz no la admite en ese nivel. commercial_required: es de consumo y el proyecto
# exige una cuenta comercial.
AccountVerdict = Literal["allowed", "consumer_terms", "not_allowed", "commercial_required"]


def account_verdict(account_type: str, level: str, require_commercial: bool) -> AccountVerdict:
    """Veredicto de la matriz para una cuenta; ``require_commercial`` es el valor efectivo."""
    allowed = ALLOWED.get(level)
    if allowed is None or account_type not in allowed:
        return "not_allowed"
    if account_type in CONSUMER_ACCOUNTS and require_commercial:
        return "commercial_required"
    if account_type == "consumer_training_off" and level == "internal":
        return "consumer_terms"
    return "allowed"
