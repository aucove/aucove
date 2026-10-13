# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Cuerpo del PR que escribe ``submit`` (03-esquemas §9; 11-trazabilidad §3.3; 12-mvp §5.3).

El cuerpo se arma completo desde la plantilla localizada en cada entrega, de forma
determinista: con las mismas entradas da el mismo texto (REQ-SCHEMA-80). Orden de 03 §9.2:
las secciones de la plantilla (``Closes #<n>`` en «Tarea»), la línea de métricas
``hook_denials``, el bloque ``pr-meta`` con el objeto ``trace`` y, al final y sin nada después,
el párrafo de trailers con ``Signed-off-by`` (REQ-TRACE-37).

El bloque se serializa con ``yaml.safe_dump(..., allow_unicode=True, sort_keys=False,
default_flow_style=None)`` y siempre entre comillas dobles ``commits.base``, ``commits.head``,
``core_version``, ``model``, ``email`` y ``at`` (REQ-SCHEMA-80). El texto de la persona no
puede abrir un marcador del núcleo ni una línea con ``---`` (REQ-TRACE-38).

``parse_pr_meta`` es el analizador de 03 §9.4: exactamente un bloque entre los marcadores, con un
solo bloque de código ``yaml``, cargado con el cargador estricto; los demás marcadores del cuerpo
se ignoran (REQ-SCHEMA-88).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from .. import _meta
from ..i18n import Translator

__all__ = [
    "PR_META_BEGIN", "PR_META_END", "QUOTED_KEYS", "hook_denials", "parse_pr_meta", "pr_meta_yaml", "render_body",
    "user_text",
]

QUOTED_KEYS = frozenset({"base", "head", "core_version", "model", "email", "at"})  # REQ-SCHEMA-80
PART_FILES_MAX = 5
_MARKER = f"<!-- {_meta.NAME}:"
PR_META_BEGIN = f"{_MARKER}pr-meta-begin v1 -->"  # REQ-SCHEMA-75
PR_META_END = f"{_MARKER}pr-meta-end v1 -->"
_PR_META = re.compile(re.escape(PR_META_BEGIN) + r"[ \t]*\n```ya?ml[ \t]*\n(.*?)\n```[ \t]*\n" + re.escape(PR_META_END),
                      re.S)
_LOG_FILES = ("events.jsonl", "events.1.jsonl", "events.2.jsonl", "events.3.jsonl")


def user_text(text: str) -> str:
    """Texto de la persona apto para el cuerpo: sin marcadores del núcleo ni líneas ``---``."""
    lines = []
    for line in text.replace("\r\n", "\n").strip().split("\n"):
        line = line.replace(_MARKER, "&lt;!-- " + _MARKER[5:])
        lines.append("\\" + line if line.startswith("---") else line)
    return "\n".join(lines)


class _Quoted(str):
    """Cadena que el bloque escribe siempre entre comillas dobles."""


def _quote(node: object, key: str | None = None) -> object:
    if isinstance(node, Mapping):
        return {k: _quote(v, str(k)) for k, v in node.items()}
    if isinstance(node, list):
        return [_quote(v, key) for v in node]
    if isinstance(node, str) and key in QUOTED_KEYS:
        return _Quoted(node)
    return node


def pr_meta_yaml(meta: Mapping[str, Any]) -> str:
    """El bloque ``pr-meta/v1`` en YAML (03 §9.3), con las comillas de REQ-SCHEMA-80."""
    import yaml

    class Dumper(yaml.SafeDumper):
        pass

    Dumper.add_representer(
        _Quoted, lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", str(v), style='"'))
    text = yaml.dump(_quote(dict(meta)), Dumper=Dumper, allow_unicode=True, sort_keys=False,
                     default_flow_style=None, width=1_000_000)
    return str(text)


def parse_pr_meta(body: str) -> tuple[dict[str, Any] | None, Literal["missing", "malformed"] | None]:
    """El bloque ``pr-meta`` del cuerpo (03 §9.4, REQ-SCHEMA-76).

    ``(datos, None)``; ``(None, "missing")`` sin el marcador de apertura; ``(None, "malformed")``
    con más de un bloque, sin el bloque de código ``yaml`` o con un YAML que no es un mapa.
    """
    from ..config.yamlload import load_yaml

    text = body.replace("\r\n", "\n")
    count = text.count(PR_META_BEGIN)
    if count == 0:
        return None, "missing"
    found = _PR_META.findall(text)
    if count != 1 or len(found) != 1:
        return None, "malformed"
    data, problems = load_yaml(found[0], file="pr-meta")
    if problems or not isinstance(data, dict):
        return None, "malformed"
    return data, None


def hook_denials(cache_root: Path, number: int, since: str | None) -> int:
    """Negativas de hooks de la tarea desde ``since`` en el registro local (12-mvp §5.3)."""
    count = 0
    for name in _LOG_FILES:
        try:
            lines = (cache_root / "log" / name).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if (isinstance(event, dict) and event.get("kind") == "hook" and event.get("result") == "deny"
                    and event.get("task") == number and (since is None or str(event.get("ts", "")) >= since)):
                count += 1
    return count


def render_body(
    *,
    lang: Translator,
    number: int,
    summary: str,
    replaces: int | None,
    parts: Mapping[str, Sequence[str]],
    ai_use: Sequence[str],
    assisted: Sequence[str],
    ai_note: str | None,
    decisions: Sequence[str],
    hook_count: int,
    meta: Mapping[str, Any],
    trailers: str,
) -> str:
    """Cuerpo completo del PR de la tarea ``number``."""
    out = [f"## {lang('tasks.pr.summary')}", "", user_text(summary), "", f"## {lang('tasks.pr.task')}", "",
           f"Closes #{number}"]
    if replaces is not None:
        out.append(lang("tasks.pr.replaces", pr=replaces))
    out += ["", f"## {lang('tasks.pr.parts')}", ""]
    if parts:
        for section, files in sorted(parts.items()):
            shown = ", ".join(f"`{f}`" for f in list(files)[:PART_FILES_MAX])
            more = lang("tasks.pr.more", n=len(files) - PART_FILES_MAX) if len(files) > PART_FILES_MAX else ""
            out.append("- " + lang("tasks.pr.part", section=section, files=shown) + more)
    else:
        out.append(lang("tasks.pr.no_parts"))
    out += ["", f"## {lang('tasks.pr.ai')}", ""]
    if list(ai_use) == ["none"]:
        out.append(lang("tasks.pr.ai_none"))
    else:
        tools = ", ".join(assisted) or "-"
        out.append(lang("tasks.pr.ai_uses", uses=", ".join(ai_use), tools=tools))
    if ai_note:
        out += ["", user_text(ai_note)]
    out += ["", f"## {lang('tasks.pr.decisions')}", ""]
    out += [f"- {d}" for d in decisions] if decisions else [lang("tasks.pr.no_decisions")]
    out += ["", f"## {lang('tasks.pr.verification')}", ""]
    out += [f"- [x] {lang(key, n=number)}" for key in ("tasks.pr.check_scope", "tasks.pr.check_note", "tasks.pr.check_ai")]
    out += [
        "",
        f"{_MARKER}metrics v1 hook_denials={hook_count} -->",
        PR_META_BEGIN,
        "```yaml",
        pr_meta_yaml(meta).rstrip("\n"),
        "```",
        PR_META_END,
        "",
        trailers.rstrip("\n"),
    ]
    return "\n".join(out)
