# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Carga de las reglas de ``validate`` y registro de marcadores.

``load_rules`` importa cada módulo de ``RULE_MODULES`` relativo al paquete
(``_meta.NAME``) y reúne sus ``RULES`` en el orden de la lista: ese orden es el de
``results[]``. Falla cerrada: un módulo ausente, sin ``RULES`` o con
una declaración inválida lanza ``ImportError`` y ``validate`` no corre ninguna regla.

``finding_params`` une los marcadores de ``RuleSpec.codes`` con los de ``config/*``
(``CONFIG_FINDING_CODES``) y el hallazgo compartido de ``rules.base.unavailable``;
un código con marcadores distintos en dos lugares es un error de contrato.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType

from ... import _meta
from .base import MODES, RULE_MODULES, UNAVAILABLE_CODE, UNAVAILABLE_PARAMS, RuleSpec

# Reglas que el job pin puede evaluar con otra versión del núcleo (09-ci REQ-CI-45).
PIN_RULES: frozenset[str] = frozenset({"schemas", "roles-sync", "generated-sync"})

_RULE_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_TOOL_NAME = re.compile(r"^[a-z0-9]+([_-][a-z0-9]+)*$")


def _check_spec(module: str, spec: object) -> RuleSpec:
    """La declaración es un ``RuleSpec`` bien formado; si no, ``ImportError`` (falla cerrada)."""
    if not isinstance(spec, RuleSpec):
        raise ImportError(f"{module}: RULES contiene algo que no es RuleSpec")
    problems: list[str] = []
    if not isinstance(spec.id, str) or not _RULE_ID.match(spec.id):
        problems.append("id")
    if not isinstance(spec.modes, frozenset) or not spec.modes or not spec.modes <= set(MODES):
        problems.append("modes")
    if not callable(spec.run):
        problems.append("run")
    if not isinstance(spec.codes, Mapping) or not all(
        isinstance(code, str) and isinstance(params, frozenset) for code, params in spec.codes.items()
    ):
        problems.append("codes")
    if not isinstance(spec.after, tuple) or not all(isinstance(a, str) and _RULE_ID.match(a) for a in spec.after):
        problems.append("after")
    if not isinstance(spec.tools, tuple) or not all(isinstance(n, str) and _TOOL_NAME.match(n) for n in spec.tools):
        problems.append("tools")
    if problems:
        raise ImportError(f"{module}: RuleSpec {getattr(spec, 'id', '?')!r} inválido en {', '.join(problems)}")
    return spec


def load_rules(modules: Sequence[str] = RULE_MODULES) -> tuple[RuleSpec, ...]:
    """Reglas de ``modules`` (relativos a ``_meta.NAME``), en su orden.

    ``ImportError`` si un módulo no existe o no se puede importar, si no exporta
    ``RULES``, si ``RULES`` no es una tupla no vacía de ``RuleSpec`` o si alguna
    declaración es inválida.
    """
    out: list[RuleSpec] = []
    for name in dict.fromkeys(modules):  # sin repetir un módulo
        if not isinstance(name, str) or not name:
            raise ImportError(f"nombre de módulo de reglas inválido: {name!r}")
        try:
            module = importlib.import_module("." + name, _meta.NAME)
        except ImportError:
            raise
        except Exception as exc:  # un módulo que falla al importarse cuenta como ausente
            raise ImportError(f"no se puede importar el módulo de reglas {name}") from exc
        rules = getattr(module, "RULES", None)
        if not isinstance(rules, tuple) or not rules:
            raise ImportError(f"el módulo {name} no exporta RULES")
        out.extend(_check_spec(name, spec) for spec in rules)
    return tuple(out)


def finding_params(rules: Sequence[RuleSpec]) -> Mapping[str, frozenset[str]]:
    """Código de hallazgo -> marcadores de ``finding.<code>.*`` de todas las fuentes.

    ``ValueError`` si un mismo código declara marcadores distintos en dos lugares.
    """
    from ...config.schemas import CONFIG_FINDING_CODES

    merged: dict[str, frozenset[str]] = {}
    origin: dict[str, str] = {}

    def add(code: str, params: frozenset[str], source: str) -> None:
        params = frozenset(params)
        if code in merged and merged[code] != params:
            raise ValueError(
                f"el código {code} tiene marcadores distintos en {origin[code]} y {source}: "
                f"{sorted(merged[code])} frente a {sorted(params)}"
            )
        merged.setdefault(code, params)
        origin.setdefault(code, source)

    add(UNAVAILABLE_CODE, UNAVAILABLE_PARAMS, "rules.base")
    for code, params in CONFIG_FINDING_CODES.items():
        add(code, params, "config")
    for rule in rules:
        for code, params in rule.codes.items():
            add(code, params, f"regla {rule.id}")
    return MappingProxyType(merged)
