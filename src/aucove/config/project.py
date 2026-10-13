# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``project.yaml`` efectivo (03-esquemas §3; 01-arquitectura §6.2).

``parse_project`` valida contra ``project.schema.json``, aplica las reglas
REQ-SCHEMA-16, REQ-SCHEMA-83 y REQ-SCHEMA-84 y construye la configuración
efectiva con los valores por defecto de la tabla de 03 §3.2 (REQ-SCHEMA-15: los
``default`` del esquema son informativos y ningún validador los inyecta).
Devuelve ``None`` si hay algún hallazgo de severidad ``error``.

- ``disclosure`` vale ``milestones`` si falta (00-marco D-44).
- ``data_policy.require_commercial`` no tiene un valor fijo: si falta, vale ``true``
  salvo con ``disclosure: open`` (REQ-SCHEMA-91, D-48). El esquema no puede expresar
  ese valor condicional, así que no está en ``DEFAULTS``.
- ``format`` es obligatorio (03 §3.2: sin valor por defecto) y la configuración
  efectiva lo expone tal cual; qué cambia con ``none`` lo deciden las reglas y el CI.

La compatibilidad con el núcleo instalado (REQ-SCHEMA-17) la decide
``domain.compat.check``; :func:`compat` la aplica a una configuración.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .. import _meta
from ..domain import compat as _compat
from ..domain.findings import Finding, has_errors
from ..i18n import Lang, Translator
from .schemas import quote_hint, validate
from .yamlload import SYNTAX_CODE, config_finding, default_t, line_for, load_with_lines, with_hint

INVALID_CODE = "E_CONFIG_INVALID"

DEFAULT_IMAGE = (
    "registry.gitlab.com/islandoftex/images/texlive:TL2026-2026-09-20-full"
    "@sha256:7334b00bf8e7a0996f7ddd65482363aaf7711d372e569f3ea78509619e3083ff"
)

# Valores por defecto de la tabla de 03 §3.2 (REQ-SCHEMA-15).
DEFAULTS: Mapping[str, Mapping[str, Any]] = MappingProxyType(
    {
        "forge": MappingProxyType({"base_branch": "main"}),
        "build": MappingProxyType(
            {
                "engine": "pdflatex",
                "main": "tex/main.tex",
                "image": DEFAULT_IMAGE,
                "bibliography": "biber",
                "bib_files": ("bib/references.bib",),
                "shell_escape": "disabled",
                "compile_on_submit": "if_available",
                "timeout_minutes": 10,
            }
        ),
        "limits": MappingProxyType(
            {
                "max_active_claims": 3,
                "claim_expiry_days": 14,
                "expiry_warning_days": 3,
                "handoff_hold_days": 7,
                "exclusive_sections": False,
            }
        ),
        "context": MappingProxyType({"max_chars": 8000, "recent_decisions": 5}),
        "coordinator": MappingProxyType({"require_direction_ack": False}),
        "review": MappingProxyType({"sla_hours": 72}),
        # require_commercial depende de disclosure: default_require_commercial (REQ-SCHEMA-91).
        "data_policy": MappingProxyType({"level": "internal", "human_subjects": False, "ethics_ref": None}),
        "ci": MappingProxyType({"pdf_retention_days": 14, "latexdiff": False}),
        "ci.model_review": MappingProxyType({"enabled": False, "max_turns": 4}),
    }
)

# Claves de primer nivel con valor por defecto en la tabla de 03 §3.2.
ROOT_DEFAULTS: Mapping[str, Any] = MappingProxyType({"disclosure": "milestones", "mode": "group"})


def default_require_commercial(disclosure: str) -> bool:
    """``data_policy.require_commercial`` cuando falta: ``true`` salvo con ``disclosure: open``.

    REQ-SCHEMA-91 y D-48: un repositorio que no es abierto no admite cuentas de consumo
    (10-seguridad §8.3, C1) salvo que el responsable escriba ``false`` y lo registre.
    """
    return disclosure != "open"


# REQ-ARCH-45 / REQ-SCHEMA-83 (la misma línea que extrae el CI, REQ-CI-12).
CORE_PIN_LINE = re.compile(r'^core_pin: "([0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?)"( *#.*)?$')  # D-66
_CORE_PIN_KEY = re.compile(r"""^(?:core_pin|"core_pin"|'core_pin')[ \t]*:""")
# REQ-SCHEMA-16: clave de nivel superior cuyo valor empieza sin comillas.
_UNQUOTED_TOP = re.compile(r"""^(?:convention|core|core_pin)[ \t]*:[ \t]*(?!["'#])\S""")


@dataclass(frozen=True)
class BuildConfig:
    engine: str
    main: str
    image: str
    bibliography: str
    bib_files: tuple[str, ...]
    shell_escape: str
    compile_on_submit: str
    timeout_minutes: int


@dataclass(frozen=True)
class ProjectConfig:
    """Configuración efectiva: todas las claves de 03 §3.2 con su valor o el de defecto.

    ``data_policy["require_commercial"]`` es el valor efectivo de REQ-SCHEMA-91; si el
    archivo lo escribe, ``raw["data_policy"]`` lo conserva.
    """

    convention: str
    core: str
    core_pin: str
    name: str
    title: str
    language: Lang
    format: str  # latex | none (D-43)
    disclosure: str  # private | milestones | open (D-44)
    forge_type: str
    forge_repo: str
    base_branch: str
    build: BuildConfig
    limits: Mapping[str, object]
    context: Mapping[str, object]
    ci: Mapping[str, object]
    data_policy: Mapping[str, object]
    licenses: Mapping[str, str] | None
    raw: Mapping[str, object]
    coordinator: Mapping[str, object] = field(default_factory=lambda: DEFAULTS["coordinator"])
    review: Mapping[str, object] = field(default_factory=lambda: DEFAULTS["review"])
    advisory_acceptance: Mapping[str, str] | None = None
    mode: str = "group"  # group | individual (D-57); sus reglas (REQ-SCHEMA-94) llegan en H3


def _lines_of(text: str) -> list[str]:
    return text.split("\n")


def core_pin_line(text: str) -> str | None:
    """Versión de la única línea ``core_pin`` de nivel superior con el formato de REQ-ARCH-45.

    Sin YAML: la usan CI y scripts. ``None`` si no hay ninguna línea ``core_pin`` de nivel
    superior, si hay más de una o si la que hay no cumple el formato exacto.
    """
    norm = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    candidates = [line for line in _lines_of(norm) if _CORE_PIN_KEY.match(line)]
    if len(candidates) != 1:
        return None
    m = CORE_PIN_LINE.match(candidates[0])
    return m.group(1) if m else None


def _as_int(value: object) -> object:
    """Un entero que YAML leyó como ``10.0`` (el esquema lo admite como integer) vuelve a ``int``."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _section(raw: Mapping[str, Any], name: str) -> dict[str, Any]:
    given = raw.get(name)
    out = dict(DEFAULTS[name])
    if isinstance(given, dict):
        out.update(given)
    return {k: _as_int(v) for k, v in out.items()}


def _effective(raw: Mapping[str, Any]) -> ProjectConfig:
    forge = _section(raw, "forge")
    build = _section(raw, "build")
    ci = _section(raw, "ci")
    model_review = dict(DEFAULTS["ci.model_review"])
    if isinstance(ci.get("model_review"), dict):
        model_review.update(ci["model_review"])
    ci["model_review"] = MappingProxyType({k: _as_int(v) for k, v in model_review.items()})
    licenses = raw.get("licenses")
    acceptance = forge.get("advisory_acceptance")
    disclosure = raw.get("disclosure", ROOT_DEFAULTS["disclosure"])
    data_policy = _section(raw, "data_policy")
    data_policy.setdefault("require_commercial", default_require_commercial(disclosure))
    return ProjectConfig(
        convention=raw["convention"],
        core=raw["core"],
        core_pin=raw["core_pin"],
        name=raw["name"],
        title=raw["title"],
        language=raw["language"],
        format=raw["format"],
        disclosure=disclosure,
        forge_type=forge["type"],
        forge_repo=forge["repo"],
        base_branch=forge["base_branch"],
        build=BuildConfig(
            engine=build["engine"],
            main=build["main"],
            image=build["image"],
            bibliography=build["bibliography"],
            bib_files=tuple(build["bib_files"]),
            shell_escape=build["shell_escape"],
            compile_on_submit=build["compile_on_submit"],
            timeout_minutes=int(build["timeout_minutes"]),
        ),
        limits=MappingProxyType(_section(raw, "limits")),
        context=MappingProxyType(_section(raw, "context")),
        ci=MappingProxyType(ci),
        data_policy=MappingProxyType(data_policy),
        licenses=MappingProxyType(dict(licenses)) if isinstance(licenses, dict) else None,
        raw=MappingProxyType(dict(raw)),
        coordinator=MappingProxyType(_section(raw, "coordinator")),
        review=MappingProxyType(_section(raw, "review")),
        advisory_acceptance=MappingProxyType(dict(acceptance)) if isinstance(acceptance, dict) else None,
        mode=raw.get("mode", ROOT_DEFAULTS["mode"]),
    )


def _quote_syntax_hint(finding: Finding, norm: str | None, t: Translator) -> Finding:
    """REQ-SCHEMA-16: un error de sintaxis en ``convention``, ``core`` o ``core_pin`` sin comillas."""
    if norm is None or finding.line is None:
        return finding
    lines = _lines_of(norm)
    if 1 <= finding.line <= len(lines) and _UNQUOTED_TOP.match(lines[finding.line - 1]):
        return with_hint(finding, quote_hint(t))
    return finding


def _rules(
    raw: Mapping[str, Any], norm: str, lines: Mapping[str, int], *, file: str, t: Translator, check: str
) -> list[Finding]:
    """REQ-SCHEMA-83 y REQ-SCHEMA-84 sobre un documento que ya cumple el esquema."""
    out: list[Finding] = []

    def invalid(pointer: str, detail: str, hint: str | None = None) -> None:
        out.append(
            config_finding(
                INVALID_CODE, t=t, file=file, line=line_for(lines, pointer), pointer=pointer,
                check=check, hint=hint, detail=detail,
            )
        )

    pin, core = raw["core_pin"], raw["core"]
    if core_pin_line(norm) != pin:
        unquoted = any(_UNQUOTED_TOP.match(line) and _CORE_PIN_KEY.match(line) for line in _lines_of(norm))
        invalid("/core_pin", t("config.project.core_pin_line"), quote_hint(t) if unquoted else None)
    if _compat.parse_specifiers(core) is None:
        invalid("/core", t("config.project.core_invalid", core=core))
    elif not _compat.satisfies(pin, core):
        invalid("/core_pin", t("config.project.core_pin_range", pin=pin, core=core))
    limits = _section(raw, "limits")
    warning, expiry = limits["expiry_warning_days"], limits["claim_expiry_days"]
    if not warning < expiry:
        invalid(
            "/limits/expiry_warning_days",
            t("config.project.expiry_warning", warning=str(warning), expiry=str(expiry)),
        )
    return out


def parse_project(
    text: str | bytes,
    *,
    file: str = "project.yaml",
    t: Translator | None = None,
    check: str = "schemas",
) -> tuple[ProjectConfig | None, list[Finding]]:
    """Configuración efectiva de ``project.yaml`` y todos sus hallazgos.

    ``(None, hallazgos)`` si hay algún error: sintaxis (``E_CONFIG_YAML``), esquema
    (``E_VALIDATION_SCHEMA``) o reglas de 03 §3.3 (``E_CONFIG_INVALID``). Sin ``t``, en ``en``.
    """
    t = default_t(t)
    raw, lines, findings, norm = load_with_lines(text, file=file, syntax_code=SYNTAX_CODE, t=t, check=check)
    if findings:
        return None, [_quote_syntax_hint(f, norm, t) for f in findings]
    assert norm is not None
    findings = validate("project", raw, file=file, check=check, t=t, lines=lines)
    if not findings and isinstance(raw, dict):
        findings = _rules(raw, norm, lines, file=file, t=t, check=check)
    if has_errors(findings) or not isinstance(raw, dict):
        return None, findings
    return _effective(raw), findings


def compat(cfg: ProjectConfig, installed: str = _meta.__version__) -> _compat.Compat:
    """Compatibilidad de 01 §6.3 entre ``installed`` y el ``project.yaml`` (de la base, REQ-ARCH-46)."""
    return _compat.check(installed, cfg.convention, cfg.core, cfg.core_pin)
