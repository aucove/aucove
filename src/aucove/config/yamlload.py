# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Cargador YAML estricto y front matter (03-esquemas §2.1-2.2).

``StrictLoader`` es el cargador de referencia de 03 §2.1 sobre ``yaml.SafeLoader``
(``CSafeLoader`` no deja interceptar los alias): sin fechas, sin los booleanos de
YAML 1.1 (``yes``, ``no``, ``on``, ``off``), sin alias ni anclas, sin claves
repetidas y sin claves que no sean texto (REQ-SCHEMA-2, REQ-SCHEMA-3). Además
rechaza las etiquetas que no dan tipos JSON (``!!binary``, ``!!set``, ``!!omap``,
``!!pairs``, etiquetas locales y claves de fusión ``<<``) y deja ``!!timestamp``
como texto. Los números son solo decimales: ``014`` (octal), ``1:30`` (sexagesimal),
``0x1F``, ``0b101``, ``1_000``, ``.inf`` y ``.nan`` quedan como texto, y ``!!bool``,
``!!int`` o ``!!float`` con un valor que no tiene la forma JSON del tipo (``!!bool yes``,
también vía ``%TAG``) son un error. ``yaml`` se importa de forma perezosa (01 §3.1).

Este módulo también construye los hallazgos de ``config/*``: ``FINDING_PARAMS``
fija los marcadores de ``finding.<code>.*`` (``config.toml``) y ``schemas``
lo publica como ``CONFIG_FINDING_CODES``.
"""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import Any

from ..domain.findings import PATH_PATTERN, Finding, Severity
from ..i18n import Translator, translator

SYNTAX_CODE = "E_CONFIG_YAML"
FRONT_MATTER_CODE = "E_VALIDATION_FRONT_MATTER"

# Códigos de hallazgo que emite config/* -> marcadores de finding.<code>.message y .hint.
# Ningún marcador se llama rule, inp, code, severity, path, line ni pointer, porque
# chocarían con los parámetros de ops.rules.base.make_finding.
FINDING_PARAMS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "E_CONFIG_YAML": frozenset({"detail"}),
        "E_CONFIG_INVALID": frozenset({"detail"}),
        "E_VALIDATION_SCHEMA": frozenset({"where", "detail"}),
        "E_VALIDATION_FRONT_MATTER": frozenset({"detail"}),
        "E_VALIDATION_ROLES_LEAD": frozenset({"detail"}),
        "E_VALIDATION_ROLES_DEPUTY": frozenset({"detail"}),
        "E_VALIDATION_DUPLICATE_LOGIN": frozenset({"login"}),
        "E_VALIDATION_DUPLICATE_ID": frozenset({"id"}),
        "E_VALIDATION_UNKNOWN_PERSON": frozenset({"login", "ref"}),
        "E_VALIDATION_INACTIVE_PERSON": frozenset({"login", "ref"}),
        "E_VALIDATION_ORCID": frozenset({"orcid"}),
        "W_VALIDATION_LOGIN_CASE": frozenset({"written", "login"}),
    }
)

MAX_SHOWN = 80  # caracteres de un valor del documento citado en un mensaje
_MAX_HINT = 500

_TIMESTAMP = "tag:yaml.org,2002:timestamp"
_BOOL = "tag:yaml.org,2002:bool"
_INT = "tag:yaml.org,2002:int"
_FLOAT = "tag:yaml.org,2002:float"
_MERGE = "tag:yaml.org,2002:merge"
_VALUE = "tag:yaml.org,2002:value"

# Escalares que dan bool, int o float; con '\Z', un '\n' final no coincide. Fuera de estas
# formas, YAML 1.1 cambia el valor sin aviso (yes, 014 octal, 1:30 sexagesimal, 0x1F,
# 0b101, 1_000, .inf, .nan): sin etiqueta quedan como texto y con etiqueta explícita
# (!!bool, !!int, !!float, también vía %TAG) son un error (REQ-SCHEMA-3).
_BOOL_RE = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)\Z")
_INT_RE = re.compile(r"^[-+]?(?:0|[1-9][0-9]*)\Z")
_FLOAT_RE = re.compile(r"^[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+][0-9]+)?\Z")
_REJECTED_TAGS = (
    "tag:yaml.org,2002:binary",
    "tag:yaml.org,2002:set",
    "tag:yaml.org,2002:omap",
    "tag:yaml.org,2002:pairs",
)


def default_t(t: Translator | None) -> Translator:
    """Sin traductor, los mensajes salen en ``en``."""
    return t if t is not None else translator("en")


def shown(value: object) -> str:
    """Valor del documento para citarlo en un mensaje: JSON de una línea, recortado."""
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    if len(text) > MAX_SHOWN:
        text = text[: MAX_SHOWN - 3] + "..."
    return text


def repo_path(file: str | None) -> str | None:
    """``file`` como ``repo_path`` con ``/``; ``None`` si no lo es (p. ej. una ruta absoluta)."""
    if not file:
        return None
    rel = file.replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel if PATH_PATTERN.match(rel) else None


def config_finding(
    code: str,
    *,
    t: Translator,
    file: str | None,
    line: int | None = None,
    pointer: str | None = None,
    severity: Severity = "error",
    check: str = "schemas",
    hint: str | None = None,
    **params: object,
) -> Finding:
    """Hallazgo localizado de ``config/*``.

    ``hint`` (ya localizado) reemplaza a ``finding.<code>.hint``. ``ValueError`` si el
    código no es de config o los marcadores no son los de ``FINDING_PARAMS``.
    """
    expected = FINDING_PARAMS.get(code)
    if expected is None:
        raise ValueError(f"código de hallazgo ajeno a config: {code!r}")
    if set(params) != set(expected):
        raise ValueError(f"marcadores de {code} distintos de {sorted(expected)}: {sorted(params)}")
    message = t(f"finding.{code}.message", **params)
    if hint is None:
        hint = t.get(f"finding.{code}.hint", **params)
    if hint is not None and len(hint) > _MAX_HINT:
        hint = hint[: _MAX_HINT - 3] + "..."
    return Finding(
        check=check,
        code=code,
        severity=severity,
        path=repo_path(file),
        line=line,
        message=message,
        hint=hint,
        pointer=pointer,
    )


def escape_pointer(token: str) -> str:
    """Un segmento de JSON Pointer (RFC 6901): ``~`` -> ``~0`` y ``/`` -> ``~1``."""
    return token.replace("~", "~0").replace("/", "~1")


def pointer_of(parts: object) -> str:
    """JSON Pointer de una secuencia de claves e índices."""
    return "".join("/" + escape_pointer(str(p)) for p in parts)  # type: ignore[attr-defined]


# -- cargador ---------------------------------------------------------------------


def _strict_error(yaml_mod: Any, reason: str, arg: object, mark: object) -> Exception:
    err = yaml_mod.constructor.ConstructorError(None, None, f"YAML estricto: {reason}", mark)
    err.strict_reason = reason
    err.strict_arg = arg
    return err  # type: ignore[no-any-return]


@functools.cache
def strict_loader() -> type[Any]:
    """Clase ``StrictLoader`` de 03 §2.1, construida al primer uso (import perezoso de ``yaml``)."""
    import yaml as yaml_mod

    class StrictLoader(yaml_mod.SafeLoader):
        """SafeLoader sin fechas, sin booleanos de YAML 1.1, sin alias ni anclas."""

    # Sin timestamps ni los booleanos, enteros y reales de YAML 1.1; '=' queda como texto.
    StrictLoader.yaml_implicit_resolvers = {
        k: [(tag, rx) for tag, rx in v if tag not in (_TIMESTAMP, _BOOL, _INT, _FLOAT, _VALUE)]
        for k, v in yaml_mod.SafeLoader.yaml_implicit_resolvers.items()
    }
    StrictLoader.add_implicit_resolver(_BOOL, _BOOL_RE, list("tTfF"))
    StrictLoader.add_implicit_resolver(_INT, _INT_RE, list("-+0123456789"))
    StrictLoader.add_implicit_resolver(_FLOAT, _FLOAT_RE, list("-+0123456789."))

    def compose_node(self: Any, parent: Any, index: Any) -> Any:  # sin alias ni anclas
        if self.check_event(yaml_mod.AliasEvent):
            event = self.peek_event()
            raise _strict_error(yaml_mod, "alias", event.anchor, event.start_mark)
        event = self.peek_event()
        anchor = getattr(event, "anchor", None)
        if anchor is not None:
            raise _strict_error(yaml_mod, "anchor", anchor, event.start_mark)
        return yaml_mod.composer.Composer.compose_node(self, parent, index)

    setattr(StrictLoader, "compose_node", compose_node)

    def construct_mapping(loader: Any, node: Any, deep: bool = False) -> Any:  # sin claves repetidas
        seen: set[str] = set()
        for key_node, _value in node.value:
            key = loader.construct_object(key_node, deep=True)  # clave completa para citarla
            if not isinstance(key, str):
                raise _strict_error(yaml_mod, "key", key, key_node.start_mark)
            if key in seen:
                raise _strict_error(yaml_mod, "duplicate", key, key_node.start_mark)
            seen.add(key)
        return yaml_mod.SafeLoader.construct_mapping(loader, node, deep=deep)

    StrictLoader.add_constructor(yaml_mod.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping)

    def reject_tag(loader: Any, node: Any) -> Any:
        reason = "merge" if node.tag == _MERGE else "tag"
        raise _strict_error(yaml_mod, reason, node.tag, node.start_mark)

    for tag in _REJECTED_TAGS:
        StrictLoader.add_constructor(tag, reject_tag)
    StrictLoader.add_constructor(None, reject_tag)  # etiquetas locales o desconocidas

    def timestamp_as_text(loader: Any, node: Any) -> Any:  # !!timestamp explícito: texto
        return loader.construct_scalar(node)

    StrictLoader.add_constructor(_TIMESTAMP, timestamp_as_text)

    # Con etiqueta explícita (!!bool, !!int, !!float, o la misma etiqueta vía %TAG), el
    # escalar tiene que tener la forma JSON del tipo; si no, es un error y no un valor
    # distinto del escrito (REQ-SCHEMA-3).
    def strict_scalar(patterns: tuple[re.Pattern[str], ...], construct: Any) -> Any:
        def constructor(loader: Any, node: Any) -> Any:
            value = loader.construct_scalar(node)
            if not isinstance(value, str) or not any(rx.match(value) for rx in patterns):
                raise _strict_error(yaml_mod, "tag_value", (node.tag, value), node.start_mark)
            return construct(loader, node)

        return constructor

    safe = yaml_mod.constructor.SafeConstructor
    StrictLoader.add_constructor(_BOOL, strict_scalar((_BOOL_RE,), safe.construct_yaml_bool))
    StrictLoader.add_constructor(_INT, strict_scalar((_INT_RE,), safe.construct_yaml_int))
    StrictLoader.add_constructor(_FLOAT, strict_scalar((_FLOAT_RE, _INT_RE), safe.construct_yaml_float))
    return StrictLoader


def normalize_text(text: str | bytes) -> tuple[str | None, bool]:
    """Texto sin BOM y con LF (REQ-SCHEMA-1). ``(None, False)`` si los bytes no son UTF-8."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            return None, False
    if text.startswith("﻿"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n"), True


def _node_lines(node: Any, offset: int = 0) -> dict[str, int]:
    """JSON Pointer -> línea (1-based) de cada valor; en los mapas, la línea de la clave."""
    import yaml as yaml_mod

    out: dict[str, int] = {}
    if node is None:
        return out
    stack: list[tuple[Any, str, int]] = [(node, "", node.start_mark.line + 1 + offset)]
    while stack:
        current, pointer, line = stack.pop()
        out.setdefault(pointer, line)
        if isinstance(current, yaml_mod.MappingNode):
            for key_node, value_node in current.value:
                key = key_node.value if isinstance(key_node, yaml_mod.ScalarNode) else str(key_node.value)
                stack.append((value_node, f"{pointer}/{escape_pointer(key)}", key_node.start_mark.line + 1 + offset))
        elif isinstance(current, yaml_mod.SequenceNode):
            for i, item in enumerate(current.value):
                stack.append((item, f"{pointer}/{i}", item.start_mark.line + 1 + offset))
    return out


def _error_line(exc: BaseException, text: str) -> int | None:
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    if mark is not None and getattr(mark, "line", None) is not None:
        return int(mark.line) + 1
    position = getattr(exc, "position", None)  # ReaderError
    if isinstance(position, int):
        return text.count("\n", 0, position) + 1
    return None


def _error_detail(exc: BaseException, t: Translator) -> str:
    import yaml as yaml_mod

    reason = getattr(exc, "strict_reason", None)
    arg = getattr(exc, "strict_arg", None)
    if reason == "alias":
        return t("config.yaml.alias")
    if reason == "anchor":
        return t("config.yaml.anchor", name=str(arg))
    if reason == "duplicate":
        return t("config.yaml.duplicate_key", key=shown(arg))
    if reason == "key":
        return t("config.yaml.non_string_key", key=shown(arg))
    if reason == "merge":
        return t("config.yaml.merge")
    if reason == "tag":
        return t("config.yaml.tag", tag=str(arg))
    if reason == "tag_value":
        tag, value = arg if isinstance(arg, tuple) else (arg, "")
        return t("config.yaml.tag_value", tag=str(tag), value=shown(value))
    if isinstance(exc, yaml_mod.reader.ReaderError):
        return t("config.yaml.character")
    if isinstance(exc, yaml_mod.MarkedYAMLError):
        problem = " ".join(p for p in (exc.context, exc.problem) if p)
        return t("config.yaml.syntax", problem=problem or type(exc).__name__)
    if isinstance(exc, RecursionError):
        return t("config.yaml.too_deep")
    return t("config.yaml.syntax", problem=type(exc).__name__)


@functools.cache
def _loader_errors() -> tuple[type[BaseException], ...]:
    import yaml as yaml_mod

    return (yaml_mod.YAMLError, ValueError, TypeError, RecursionError, OverflowError)


def load_with_lines(
    text: str | bytes,
    *,
    file: str,
    syntax_code: str = SYNTAX_CODE,
    t: Translator | None = None,
    check: str = "schemas",
    line_offset: int = 0,
) -> tuple[object | None, dict[str, int], list[Finding], str | None]:
    """Como :func:`load_yaml`, más el mapa pointer -> línea y el texto normalizado."""
    t = default_t(t)
    norm, ok = normalize_text(text)
    if norm is None or not ok:
        return None, {}, [config_finding(syntax_code, t=t, file=file, check=check, detail=t("config.yaml.not_utf8"))], None
    loader = None
    try:
        loader = strict_loader()(norm)  # el lector ya rechaza caracteres no imprimibles
        node = loader.get_single_node()
        data = loader.construct_document(node) if node is not None else None
    except _loader_errors() as exc:  # una falla de sintaxis detiene el análisis del archivo
        line = _error_line(exc, norm)
        finding = config_finding(
            syntax_code,
            t=t,
            file=file,
            line=line + line_offset if line is not None else None,
            check=check,
            detail=_error_detail(exc, t),
        )
        return None, {}, [finding], norm
    finally:
        if loader is not None:
            loader.dispose()
    return data, _node_lines(node, line_offset), [], norm


def load_yaml(
    text: str | bytes,
    *,
    file: str,
    syntax_code: str = SYNTAX_CODE,
    t: Translator | None = None,
    check: str = "schemas",
) -> tuple[object | None, list[Finding]]:
    """Carga un documento YAML con ``StrictLoader``.

    Devuelve ``(datos, [])`` o ``(None, [hallazgo])`` con ``syntax_code`` (``E_CONFIG_YAML``
    en ``project.yaml`` y ``roles.yaml``; REQ-SCHEMA-4). Un documento vacío da ``(None, [])``.
    """
    data, _lines, findings, _norm = load_with_lines(text, file=file, syntax_code=syntax_code, t=t, check=check)
    return data, findings


def _front_matter_bounds(norm: str) -> tuple[int, int] | None:
    """Índices de línea de las dos líneas ``---``; ``None`` si no hay apertura o cierre."""
    lines = norm.split("\n")
    if not lines or lines[0] != "---":
        return None
    for i in range(1, len(lines)):
        if lines[i] == "---":
            return 0, i
    return 0, -1


def split_front_matter(
    text: str | bytes,
    *,
    file: str,
    t: Translator | None = None,
    check: str = "schemas",
) -> tuple[dict | None, str, list[Finding]]:  # type: ignore[type-arg]
    """Separa el front matter de REQ-SCHEMA-5: ``(mapa, cuerpo, hallazgos)``.

    El archivo empieza exactamente con la línea ``---`` y el front matter termina en la
    siguiente línea ``---``; debe ser un mapa YAML. Si falta, está mal delimitado, no es
    un mapa o tiene un error de sintaxis: ``E_VALIDATION_FRONT_MATTER`` (REQ-SCHEMA-4).
    """
    t = default_t(t)
    norm, ok = normalize_text(text)
    if norm is None or not ok:
        detail = t("config.yaml.not_utf8")
        return None, "", [config_finding(FRONT_MATTER_CODE, t=t, file=file, check=check, detail=detail)]
    bounds = _front_matter_bounds(norm)
    if bounds is None:
        detail = t("config.front_matter.missing")
        return None, norm, [config_finding(FRONT_MATTER_CODE, t=t, file=file, line=1, check=check, detail=detail)]
    lines = norm.split("\n")
    if bounds[1] < 0:
        detail = t("config.front_matter.unterminated")
        return None, norm, [config_finding(FRONT_MATTER_CODE, t=t, file=file, line=1, check=check, detail=detail)]
    end = bounds[1]
    fm_text = "\n".join(lines[1:end]) + "\n"
    body = "\n".join(lines[end + 1 :])
    data, _lines, findings, _n = load_with_lines(
        fm_text, file=file, syntax_code=FRONT_MATTER_CODE, t=t, check=check, line_offset=1
    )
    if findings:
        return None, body, findings
    if not isinstance(data, dict):
        detail = t("config.front_matter.not_mapping")
        return None, body, [config_finding(FRONT_MATTER_CODE, t=t, file=file, line=1, check=check, detail=detail)]
    return data, body, []


def pointer_lines(source_text: str | bytes | None) -> dict[str, int]:
    """Mapa JSON Pointer -> línea de un YAML o del front matter de un archivo; vacío si no se puede."""
    if source_text is None:
        return {}
    data, lines, findings, norm = load_with_lines(source_text, file="-")
    if not findings:
        return lines
    if norm is None:
        return {}
    bounds = _front_matter_bounds(norm)
    if bounds is None or bounds[1] < 0:
        return {}
    fm_text = "\n".join(norm.split("\n")[1 : bounds[1]]) + "\n"
    _data, fm_lines, fm_findings, _n = load_with_lines(fm_text, file="-", line_offset=1)
    return {} if fm_findings else fm_lines


def line_for(lines: Mapping[str, int], pointer: str | None) -> int | None:
    """Línea del pointer o, si no está, la de su antecesor más cercano."""
    if pointer is None or not lines:
        return None
    current = pointer
    while True:
        if current in lines:
            return lines[current]
        if not current:
            return None
        current = current.rsplit("/", 1)[0]


def with_hint(finding: Finding, hint: str) -> Finding:
    """Copia del hallazgo con otra pista."""
    return replace(finding, hint=hint[:_MAX_HINT])
