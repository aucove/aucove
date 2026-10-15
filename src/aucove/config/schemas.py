# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Registro de esquemas y validación con JSON Schema (03-esquemas §2.3).

- Draft 2020-12 con aserción de ``format`` (REQ-SCHEMA-6). El ``FormatChecker`` debe
  cubrir cada ``format`` de los esquemas empaquetados; si falta un verificador (por
  ejemplo, sin ``rfc3339-validator``), falla en vez de ignorar el formato.
- ``pattern`` se evalúa con la semántica de ECMA-262 para ``$`` (fin de la entrada, sin
  aceptar un ``\\n`` final), no con la de ``re`` de Python (:func:`ecma_pattern`).
- ``referencing.Registry`` local, construido con ``schemas/`` empaquetado y sin función
  de descarga: ningún ``$ref`` ni ``$id`` se resuelve por red (REQ-ARCH-28, REQ-SCHEMA-7).
  Incluye los tres esquemas de evidencia de 11-trazabilidad §8 (REQ-SCHEMA-93).
- Se informan todos los errores de un archivo como ``E_VALIDATION_SCHEMA``, con JSON
  Pointer (RFC 6901) y línea cuando hay texto fuente (REQ-SCHEMA-8, REQ-SCHEMA-9).
- Un número sin comillas donde el esquema pide texto lleva la pista "escríbelo entre
  comillas"; el validador nunca lo convierte (REQ-SCHEMA-89, REQ-ARCH-29).

``jsonschema`` y ``referencing`` se importan de forma perezosa (01 §3.1).
"""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Iterator, Mapping
from typing import TYPE_CHECKING, Any, Literal, get_args

from .. import _meta
from ..domain.findings import Finding
from ..i18n import Translator
from .datafiles import data_path
from .yamlload import FINDING_PARAMS, config_finding, default_t, escape_pointer, line_for, pointer_lines, shown

if TYPE_CHECKING:
    import jsonschema
    import referencing

SchemaKind = Literal[
    "project", "roles", "result", "decision", "session-note", "pr-meta",
    "evidence-manifest", "evidence-openings", "evidence-record", "evidence-signature",
]
SCHEMA_KINDS: tuple[SchemaKind, ...] = get_args(SchemaKind)

# Parte de config del registro de marcadores de hallazgo (la otra está en RuleSpec.codes).
CONFIG_FINDING_CODES: Mapping[str, frozenset[str]] = FINDING_PARAMS

SCHEMA_CODE = "E_VALIDATION_SCHEMA"


def schema_id(kind: str) -> str:
    """``$id`` de un esquema: ``<SCHEMA_BASE>/<kind>/v1``."""
    return f"{_meta.SCHEMA_BASE}/{kind}/v1"


TRACE_ID = schema_id("trace")


def _trace_schema() -> dict[str, Any]:
    """Esquema ``trace/v1`` de 11-trazabilidad §3.3 (lo cita ``pr-meta``; 03 §9, L1016).

    Se transcribe aquí porque ``schemas/`` no lo trae todavía. Los valores que llevan
    el prefijo de las variables de entorno se derivan de ``_meta`` (REQ-ARCH-19).
    """
    env = "env:" + _meta.ENV_PREFIX
    slug = {"$ref": "#/$defs/slug"}
    use = {"$ref": "#/$defs/use"}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": TRACE_ID,
        "type": "object",
        "additionalProperties": False,
        "required": ["version", "ai_use", "assisted_by", "sign_off", "commits", "core_version"],
        "properties": {
            "version": {"const": 1},
            "task": {"type": "integer", "minimum": 1},
            "sections": {"type": "array", "uniqueItems": True, "items": slug},
            "ai_use": {"type": "array", "minItems": 1, "uniqueItems": True, "items": use},
            "ai_use_by_section": {
                "type": "object",
                "propertyNames": slug,
                "additionalProperties": {"type": "array", "items": use},
            },
            "assisted_by": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["tool", "model", "tool_source", "model_source"],
                    "properties": {
                        "tool": slug,
                        "model": {"type": "string", "pattern": "^[A-Za-z0-9._:@/+-]{1,128}$"},
                        "tool_source": {
                            "enum": ["env:CLAUDECODE", env + "AI_TOOL", "env:GEMINI_CLI", "declared", "co-authored-by"]
                        },
                        "model_source": {
                            "enum": ["hook:SessionStart", "hook:PostModelSwitch", env + "AI_MODEL", "declared", "none"]
                        },
                    },
                },
            },
            "ai_note": {"type": "string", "maxLength": 500},
            "sign_off": {
                "type": "object",
                "additionalProperties": False,
                "required": ["login", "name", "email", "channel", "attestation", "at"],
                "properties": {
                    "login": {"type": "string"},
                    "name": {"type": "string"},
                    "email": {"type": "string"},
                    "channel": {"enum": ["tty", "cli-flag", "mcp"]},
                    "attestation": {"const": "attest-v1"},
                    "at": {"type": "string", "format": "date-time"},
                },
            },
            "commits": {
                "type": "object",
                "required": ["base", "head", "count", "undeclared"],
                "properties": {
                    "base": {"type": "string"},
                    "head": {"type": "string"},
                    "count": {"type": "integer", "minimum": 0},
                    "undeclared": {"type": "integer", "minimum": 0},
                },
            },
            "core_version": {"type": "string"},
        },
        "$defs": {
            "slug": {"type": "string", "pattern": "^[a-z0-9]+(-[a-z0-9]+)*$"},
            "use": {
                "enum": [
                    "none", "ideation", "literature", "drafting", "editing",
                    "translation", "code", "analysis", "figures", "review",
                ]
            },
        },
    }


@functools.cache
def _schemas() -> Mapping[str, Mapping[str, Any]]:
    """``$id`` -> contenido de cada esquema empaquetado (más ``trace/v1`` si falta)."""
    out: dict[str, Mapping[str, Any]] = {}
    for path in sorted(data_path("schemas").glob("*.schema.json")):
        contents = json.loads(path.read_bytes().decode("utf-8-sig"))
        sid = contents.get("$id") if isinstance(contents, dict) else None
        if not isinstance(sid, str):
            raise ValueError(f"{path.name}: esquema sin $id")
        out[sid] = contents
    out.setdefault(TRACE_ID, _trace_schema())
    missing = [kind for kind in SCHEMA_KINDS if schema_id(kind) not in out]
    if missing:
        raise FileNotFoundError(f"faltan esquemas empaquetados: {missing}")
    return out


def schema(kind: SchemaKind) -> Mapping[str, Any]:
    """Contenido del esquema ``kind``."""
    return _schemas()[schema_id(kind)]


@functools.cache
def registry() -> referencing.Registry:
    """Registro local de ``referencing`` con los esquemas empaquetados; nunca descarga nada."""
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    resources = [
        (sid, Resource.from_contents(contents, default_specification=DRAFT202012))
        for sid, contents in _schemas().items()
    ]
    return Registry().with_resources(resources).crawl()  # sin 'retrieve': un $ref ajeno falla


# Palabras clave cuyo valor es un subesquema o una lista o mapa de subesquemas.
_SUBSCHEMA_MAPS = ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas")
_SUBSCHEMA_KEYS = (
    "items", "prefixItems", "contains", "not", "if", "then", "else", "additionalProperties",
    "propertyNames", "unevaluatedItems", "unevaluatedProperties", "allOf", "anyOf", "oneOf",
)


def _formats_in(node: object, out: set[str]) -> None:
    if isinstance(node, list):
        for item in node:
            _formats_in(item, out)
        return
    if not isinstance(node, dict):
        return
    fmt = node.get("format")
    if isinstance(fmt, str):
        out.add(fmt)
    for key in _SUBSCHEMA_MAPS:
        sub = node.get(key)
        if isinstance(sub, dict):
            for value in sub.values():
                _formats_in(value, out)
    for key in _SUBSCHEMA_KEYS:
        if key in node:
            _formats_in(node[key], out)


@functools.cache
def formats_used() -> frozenset[str]:
    """Todos los ``format`` de los esquemas empaquetados (hoy: date, date-time, email y uri)."""
    found: set[str] = set()
    for contents in _schemas().values():
        _formats_in(contents, found)
    return frozenset(found)


@functools.cache
def format_checker() -> jsonschema.FormatChecker:
    """``FormatChecker`` de draft 2020-12; ``RuntimeError`` si no cubre algún formato usado."""
    from jsonschema import Draft202012Validator

    checker = Draft202012Validator.FORMAT_CHECKER
    missing = sorted(formats_used() - set(checker.checkers))
    if missing:  # sin el verificador, jsonschema aceptaría cualquier valor en silencio
        raise RuntimeError(f"sin verificador de format para {missing}: instala jsonschema[format-nongpl]")
    return checker


def ecma_pattern(pattern: str) -> str:
    """``pattern`` de JSON Schema traducido a ``re`` de Python con el ``$`` de ECMA-262.

    En ECMA-262 (la semántica de ``pattern`` en 2020-12), ``$`` sin la bandera ``m`` solo
    coincide al final de la entrada; en Python también antes de un ``\\n`` final, y
    ``"metodos\\n"`` cumpliría ``^[a-z0-9]+(-[a-z0-9]+)*$``. Cada ``$`` que no está escapado
    ni dentro de una clase de caracteres pasa a ``\\Z``. Las clases se delimitan como las
    lee ``re`` (un ``]`` justo después de ``[`` o ``[^`` es literal).
    """
    out: list[str] = []
    i, n = 0, len(pattern)
    in_class = False
    while i < n:
        c = pattern[i]
        if c == "\\" and i + 1 < n:  # escape: se copia tal cual (también dentro de una clase)
            out.append(pattern[i : i + 2])
            i += 2
            continue
        if in_class:
            in_class = c != "]"
            out.append(c)
        elif c == "[":
            in_class = True
            j = i + 1
            if j < n and pattern[j] == "^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            out.append(pattern[i:j])
            i = j
            continue
        else:
            out.append(r"\Z" if c == "$" else c)
        i += 1
    return "".join(out)


@functools.cache
def _ecma_regex(pattern: str) -> re.Pattern[str]:
    return re.compile(ecma_pattern(pattern))


def _pattern_keyword(validator: Any, patrn: str, instance: object, schema: object) -> Iterator[Any]:
    """``pattern`` con :func:`ecma_pattern` (reemplaza al de ``jsonschema``, que usa ``re.search``)."""
    from jsonschema.exceptions import ValidationError

    if validator.is_type(instance, "string") and not _ecma_regex(patrn).search(instance):  # type: ignore[arg-type]
        yield ValidationError(f"{instance!r} does not match {patrn!r}")


@functools.cache
def validator_class() -> type[jsonschema.Draft202012Validator]:
    """Draft 2020-12 con ``pattern`` según ECMA-262 (REQ-SCHEMA-6, REQ-SCHEMA-9).

    ``propertyNames`` aplica el mismo ``pattern``. ``patternProperties`` sigue con
    ``re.search``: ningún esquema empaquetado lo usa.
    """
    from jsonschema import Draft202012Validator, validators

    return validators.extend(Draft202012Validator, {"pattern": _pattern_keyword})  # type: ignore[no-any-return]


@functools.cache
def validator(kind: SchemaKind) -> jsonschema.Draft202012Validator:
    """Validador draft 2020-12 del esquema ``kind``, con el registro local y ``format`` activo."""
    return validator_class()(dict(schema(kind)), registry=registry(), format_checker=format_checker())


# -- descripción localizada de un error --------------------------------------------

_TYPE_OF = ((bool, "boolean"), (int, "integer"), (float, "number"), (str, "string"), (list, "array"), (dict, "object"))


def _json_type(value: object) -> str:
    if value is None:
        return "null"
    for py, name in _TYPE_OF:
        if isinstance(value, py):
            return name
    return type(value).__name__


def _type_names(expected: object, t: Translator) -> str:
    names = [expected] if isinstance(expected, str) else list(expected or [])  # type: ignore[call-overload]
    text = ""
    for name in names:
        label = t(f"config.type.{name}")
        text = label if not text else t("config.schema.type_or", first=text, second=label)
    return text


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _wants_string(err: Any) -> bool:
    """El error es de tipo y el esquema admitía texto (directamente o en una alternativa)."""
    if err.validator == "type":
        expected = err.validator_value
        types = [expected] if isinstance(expected, str) else list(expected)
        return "string" in types
    if err.validator in ("anyOf", "oneOf"):
        return any(_wants_string(sub) for sub in err.context or ())
    return False


def _contains_const(sub: object) -> tuple[str, object] | None:
    """``{"properties": {k: {"const": v}}, "required": [k]}`` -> ``(k, v)``."""
    if not isinstance(sub, dict):
        return None
    props = sub.get("properties")
    if not isinstance(props, dict) or len(props) != 1:
        return None
    (field, rule), = props.items()
    if isinstance(rule, dict) and set(rule) == {"const"}:
        return field, rule["const"]
    return None


_SIMPLE_LIMITS = {
    "minLength": "min_length",
    "maxLength": "max_length",
    "minimum": "minimum",
    "maximum": "maximum",
    "exclusiveMinimum": "exclusive_minimum",
    "exclusiveMaximum": "exclusive_maximum",
    "minItems": "min_items",
    "maxItems": "max_items",
    "minProperties": "min_properties",
    "maxProperties": "max_properties",
}


def _extra_keys(err: Any) -> list[str]:
    instance = err.instance
    if not isinstance(instance, dict):
        return []
    props = err.schema.get("properties", {}) if isinstance(err.schema, dict) else {}
    patterns = err.schema.get("patternProperties", {}) if isinstance(err.schema, dict) else {}
    return [
        str(k)
        for k in instance
        if k not in props and not any(re.search(p, str(k)) for p in patterns)
    ]


def _describe(err: Any, t: Translator) -> Iterator[tuple[str, str, bool]]:
    """``(pointer, detalle localizado, pista de comillas)`` de un error de jsonschema."""
    pointer = "".join("/" + escape_pointer(str(p)) for p in err.absolute_path)
    kind = err.validator
    value = err.validator_value
    instance = err.instance
    if kind == "required" and isinstance(instance, dict):
        for key in value:
            if key not in instance:
                yield pointer, t("config.schema.required", key=str(key)), False
        return
    if kind == "additionalProperties" and value is False:
        extras = _extra_keys(err)
        for key in extras:
            yield f"{pointer}/{escape_pointer(key)}", t("config.schema.additional", key=key), False
        if extras:
            return
    quote = _is_number(instance) and _wants_string(err)
    if kind == "type":
        detail = t("config.schema.type", expected=_type_names(value, t), actual=t(f"config.type.{_json_type(instance)}"))
    elif kind == "enum":
        detail = t("config.schema.enum", value=shown(instance), allowed=", ".join(shown(v) for v in value))
    elif kind == "const":
        detail = t("config.schema.const", expected=shown(value))
    elif kind == "pattern":
        detail = t("config.schema.pattern", value=shown(instance))
    elif kind in _SIMPLE_LIMITS:
        detail = t(f"config.schema.{_SIMPLE_LIMITS[kind]}", limit=str(value))
    elif kind == "uniqueItems":
        detail = t("config.schema.unique_items")
    elif kind == "format":
        detail = t("config.schema.format", value=shown(instance), format=str(value))
    elif kind == "not":
        detail = t("config.schema.not", value=shown(instance))
    elif kind == "anyOf":
        detail = t("config.schema.any_of")
    elif kind == "oneOf":
        detail = t("config.schema.one_of")
    elif kind in ("contains", "minContains", "maxContains"):
        schema_obj = err.schema if isinstance(err.schema, dict) else {}
        if kind == "maxContains":
            limit, key = value, "max_contains"
        else:
            limit, key = schema_obj.get("minContains", 1), "min_contains"
        const = _contains_const(schema_obj.get("contains"))
        if const is not None:
            detail = t(f"config.schema.{key}_const", limit=str(limit), field=const[0], value=shown(const[1]))
        else:
            detail = t(f"config.schema.{key}", limit=str(limit))
    else:
        detail = t("config.schema.other", keyword=str(kind))
    yield pointer, detail, quote


def quote_hint(t: Translator) -> str:
    """Pista de REQ-SCHEMA-16 y REQ-SCHEMA-89."""
    return t("config.hint.quote")


def validate(
    kind: SchemaKind,
    instance: object,
    *,
    file: str,
    source_text: str | bytes | None = None,
    check: str = "schemas",
    t: Translator | None = None,
    lines: Mapping[str, int] | None = None,
) -> list[Finding]:
    """Todos los errores de ``instance`` contra el esquema ``kind``, como ``E_VALIDATION_SCHEMA``.

    ``source_text`` (el YAML, o el archivo con front matter) da la línea de cada pointer;
    ``lines`` evita volver a analizarlo si el llamador ya tiene el mapa. Sin ``t``, en ``en``.
    """
    t = default_t(t)
    if lines is None:
        lines = pointer_lines(source_text) if source_text is not None else {}
    seen: set[tuple[str, str]] = set()
    findings: list[Finding] = []
    for err in validator(kind).iter_errors(instance):
        for pointer, detail, quote in _describe(err, t):
            if (pointer, detail) in seen:
                continue
            seen.add((pointer, detail))
            findings.append(
                config_finding(
                    SCHEMA_CODE,
                    t=t,
                    file=file,
                    line=line_for(lines, pointer),
                    pointer=pointer,
                    check=check,
                    hint=quote_hint(t) if quote else None,
                    where=pointer or t("config.schema.root"),
                    detail=detail,
                )
            )
    findings.sort(key=lambda f: (f.line or 0, f.pointer or "", f.message))
    return findings


def validate_result(obj: Mapping[str, object]) -> list[str]:
    """Errores del sobre contra ``result.schema.json`` (REQ-OPS-5); lista vacía = válido."""
    errors = sorted(
        validator("result").iter_errors(obj),
        key=lambda e: ([str(p) for p in e.absolute_path], e.message),
    )
    out: list[str] = []
    for err in errors:
        pointer = "".join("/" + escape_pointer(str(p)) for p in err.absolute_path)
        out.append(f"{pointer or '/'}: {err.message}")
    return out
