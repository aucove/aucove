# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Validador propio de skills: esqueleto de T6 (12-mvp §3.7, REQ-MVP-35).

Revisa cada ``SKILL.md`` de ``.claude/skills/`` y ``.agents/skills/`` de un árbol
renderizado (un proyecto o el render de la plantilla). Reglas:

- ``name-format``: solo ``a-z``, ``0-9`` y guiones, sin guiones dobles ni en los
  extremos, 64 caracteres como máximo (REQ-MVP-35, REQ-ARCH-78).
- ``name-dir``: ``name`` igual al directorio.
- ``name-catalog``: ``name`` igual al de 00-marco §5.8 para el idioma del proyecto
  y, si el skill declara su fuente, al de esa clave.
- ``name-reserved``: no coincide con un comando integrado ni con un skill incluido
  de Claude Code (00-marco §5.8, REQ-ARCH-78).
- ``description``: de 1 a 1.024 caracteres; en ``.claude/skills``, con
  ``when_to_use``, 1.536 como máximo (REQ-ARCH-79).
- ``body-length``: cuerpo de menos de 500 líneas (REQ-MVP-35).
- ``human-op``: ninguna invocación de una operación H (00-marco §5.7). La única
  mención admitida es la forma renderizada de ``{{human:C}}``, con el texto
  «solo en tu terminal» (01 §10.2). En la fuente, ``{{cli:C}}`` con C de una
  operación H es invocación (REQ-ARCH-80).
- ``claude-tool``: ninguna mención de herramientas propias de Claude Code
  (``Edit``, ``Write``, ``MultiEdit``, ``NotebookEdit`` ni nombres ``mcp__``) en
  ``.agents/skills``.
- ``marker``: ningún marcador ``{{...}}`` sin resolver ni desconocido (REQ-ARCH-80).
- ``frontmatter``: empieza en la primera línea y es un mapa YAML (REQ-ARCH-77); en
  ``.agents/skills`` solo lleva los campos del estándar (01 §10.3).
- ``layout``: cada directorio de skill trae ``SKILL.md``.
- ``regen``: regenerar desde la fuente única no produce diferencias, como
  ``git diff --exit-code`` tras regenerar: ningún archivo cambia, falta ni sobra.
- ``language``: el idioma del proyecto se puede determinar.

La regeneración usa ``render_skills``, una transcripción de 01 §10.2-10.4, porque el
render de skills del núcleo llega en H4. ``skills-ref validate`` (REQ-MVP-34) también
queda para H4.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

import yaml

from aucove import _meta
from aucove.ops.registry import REGISTRY
from aucove.render.template import normalize_text

Lang = Literal["es", "en"]
LANGS: tuple[Lang, ...] = ("es", "en")
Target = Literal["claude", "agents"]
TARGETS: tuple[Target, ...] = ("claude", "agents")
SKILL_ROOTS: Mapping[Target, str] = {"claude": ".claude/skills", "agents": ".agents/skills"}
SKILL_FILE = "SKILL.md"

# 00-marco §5.8; las claves internas son las de 01 §10.2.
SKILL_NAMES: Mapping[str, Mapping[Lang, str]] = {
    "start": {"es": "empezar", "en": "start"},
    "status": {"es": "estado", "en": "project-status"},
    "submit": {"es": "entregar", "en": "submit"},
    "reviews": {"es": "pendientes", "en": "reviews"},
    "decision": {"es": "decision", "en": "decision"},
    "direction": {"es": "direccion", "en": "direction"},
}

# Operaciones H de 00-marco §5.7 (D-09), más las que el registro marque human_only.
HUMAN_ONLY_OPS: frozenset[str] = frozenset(
    {"init", "task accept", "task cancel", "review approve", "review request-changes"}
) | frozenset(" ".join(spec.cli) for spec in REGISTRY if spec.human_only)

# Comandos integrados y skills incluidos de Claude Code (https://code.claude.com/docs/en/commands),
# instantánea de septiembre de 2026. T10 debe verificarla en cada versión (REQ-ARCH-78); es un
# superconjunto a propósito, con alias y nombres retirados, porque un falso positivo solo obliga a
# elegir otro nombre.
RESERVED_NAMES: frozenset[str] = frozenset(
    {
        # comandos integrados
        "add-dir", "agents", "allowed-tools", "app", "approved-tools", "bashes", "branch", "btw", "bug",
        "checkpoint", "chrome", "clear", "color", "compact", "config", "context", "continue", "copy",
        "cost", "desktop", "diff", "doctor", "effort", "exit", "export", "extra-usage", "fast",
        "feedback", "fork", "help", "hooks", "ide", "init", "insights", "install-github-app",
        "install-slack-app", "keybindings", "login", "logout", "mcp", "memory", "migrate-installer",
        "mobile", "model", "new", "output-style", "passes", "permissions", "plan", "plugin", "plugins",
        "pr-comments", "privacy-settings", "quit", "release-notes", "reload-plugins", "remote-control",
        "remote-env", "rename", "reset", "resume", "review", "rewind", "sandbox", "security-review",
        "settings", "skills", "stats", "status", "statusline", "stickers", "tasks", "teleport",
        "terminal-setup", "theme", "todos", "upgrade", "usage", "vim",
        # skills incluidos
        "batch", "claude-api", "code-review", "debug", "fewer-permission-prompts", "keybindings-help",
        "loop", "run", "schedule", "simplify", "update-config",
    }
)

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_DESCRIPTION_WHEN = 1536
MAX_BODY_LINES = 500  # el cuerpo debe tener MENOS de 500 líneas (REQ-MVP-35)
CLAUDE_ONLY_TOOLS: tuple[str, ...] = ("Edit", "Write", "MultiEdit", "NotebookEdit")
AGENTS_FIELDS: tuple[str, ...] = ("name", "description", "license", "metadata")
SKILL_LICENSE = "MIT-0"
PROJECT_VARS: tuple[str, ...] = ("language", "core_pin", "convention")
GENERATED_KEY = f"{_meta.NAME}-generated"
SOURCE_KEY = f"{_meta.NAME}-source"

# Texto localizado que acompaña a {{human:C}} (01 §10.2). En H4 lo fija el catálogo del render.
HUMAN_NOTE: Mapping[Lang, str] = {
    "es": "solo en tu terminal; el agente no puede ejecutarla",
    "en": "only in your terminal; the agent cannot run it",
}

MARKER_RE = re.compile(r"\{\{(tool|cli|human|skill|var):([A-Za-z0-9_ -]+)\}\}")
ANY_MARKER_RE = re.compile(r"\{\{[^{}\n]*\}\}")
_OP_ALT = "|".join(
    re.escape(op).replace(r"\ ", r"\s+") for op in sorted(HUMAN_ONLY_OPS, key=len, reverse=True)
)
HUMAN_OP_RE = re.compile(rf"(?<![\w-]){re.escape(_meta.NAME)}\s+(?:{_OP_ALT})(?![\w-])")
CLAUDE_TOOL_RE = re.compile(
    rf"(?<![\w.-])(?:{'|'.join(CLAUDE_ONLY_TOOLS)})(?![\w-])|\bmcp__[A-Za-z0-9_-]+"
)


def mcp_tools() -> frozenset[str]:
    """Herramientas MCP del registro, las únicas que puede nombrar ``{{tool:X}}``."""
    return frozenset(spec.mcp_tool for spec in REGISTRY if spec.mcp_tool)


def is_human_only(command: str) -> bool:
    """True si ``command`` (p. ej. ``'review approve 12'``) empieza por una operación H."""
    words = command.split()
    return any(words[: len(op.split())] == op.split() for op in HUMAN_ONLY_OPS)


def catalog_names(lang: Lang) -> frozenset[str]:
    return frozenset(names[lang] for names in SKILL_NAMES.values())


# --- problemas e informe --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Problem:
    path: str               # relativa a la raíz revisada, con '/'
    line: int | None
    rule: str
    message: str

    def __str__(self) -> str:
        where = self.path if self.line is None else f"{self.path}:{self.line}"
        return f"{where}: [{self.rule}] {self.message}"


def _sort_key(p: Problem) -> tuple[str, int, str, str]:
    return (p.path, p.line or 0, p.rule, p.message)


@dataclass(frozen=True)
class LintReport:
    skills: tuple[str, ...]            # SKILL.md revisados
    problems: tuple[Problem, ...]

    @property
    def ok(self) -> bool:
        return not self.problems

    def rules(self) -> frozenset[str]:
        return frozenset(p.rule for p in self.problems)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


# --- lectura de SKILL.md ---------------------------------------------------------------


@dataclass(frozen=True)
class SkillDoc:
    target: Target
    dir_name: str
    rel: str                                   # <raíz de skills>/<dir>/SKILL.md
    directory: Path
    text: str | None                           # None si falta o no es UTF-8
    meta: Mapping[str, object] | None          # None si el frontmatter no es válido
    body: str = ""
    body_line: int = 1
    problems: tuple[Problem, ...] = field(default_factory=tuple)

    @property
    def generated(self) -> bool:
        metadata = self.meta.get("metadata") if self.meta is not None else None
        return isinstance(metadata, dict) and metadata.get(GENERATED_KEY) == "true"


def split_frontmatter(text: str) -> tuple[str | None, str, int, str | None]:
    """(YAML del frontmatter, cuerpo, línea donde empieza el cuerpo, problema)."""
    lines = text.split("\n")
    if lines[0] != "---":
        return None, text, 1, "el frontmatter debe empezar en la primera línea con '---' (REQ-ARCH-77)"
    for i in range(1, len(lines)):
        if lines[i] == "---":
            return "\n".join(lines[1:i]), "\n".join(lines[i + 1 :]), i + 2, None
    return None, text, 1, "el frontmatter no se cierra con '---'"


def read_skill(directory: Path, target: Target) -> SkillDoc:
    rel = f"{SKILL_ROOTS[target]}/{directory.name}/{SKILL_FILE}"
    path = directory / SKILL_FILE
    if not path.is_file():
        problem = Problem(f"{SKILL_ROOTS[target]}/{directory.name}", None, "layout", f"falta {SKILL_FILE}")
        return SkillDoc(target, directory.name, rel, directory, None, None, problems=(problem,))
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        problem = Problem(rel, None, "frontmatter", "no es UTF-8")
        return SkillDoc(target, directory.name, rel, directory, None, None, problems=(problem,))
    if text.startswith("﻿"):
        problem = Problem(rel, 1, "frontmatter", "lleva BOM: el frontmatter no empieza en la primera línea (REQ-ARCH-77)")
        return SkillDoc(target, directory.name, rel, directory, text, None, text, 1, (problem,))
    fm, body, body_line, bad = split_frontmatter(text)
    if bad is not None or fm is None:
        problem = Problem(rel, 1, "frontmatter", bad or "frontmatter inválido")
        return SkillDoc(target, directory.name, rel, directory, text, None, body, body_line, (problem,))
    try:
        meta = yaml.safe_load(fm)
    except yaml.YAMLError as exc:
        problem = Problem(rel, 1, "frontmatter", f"YAML inválido: {str(exc).splitlines()[0]}")
        return SkillDoc(target, directory.name, rel, directory, text, None, body, body_line, (problem,))
    if not isinstance(meta, dict):
        problem = Problem(rel, 1, "frontmatter", "el frontmatter no es un mapa")
        return SkillDoc(target, directory.name, rel, directory, text, None, body, body_line, (problem,))
    return SkillDoc(target, directory.name, rel, directory, text, meta, body, body_line)


def discover(root: Path) -> list[SkillDoc]:
    """Directorios de skill de ``.claude/skills`` y ``.agents/skills``, en orden estable."""
    docs: list[SkillDoc] = []
    for target in TARGETS:
        base = root.joinpath(*SKILL_ROOTS[target].split("/"))
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir(), key=lambda p: p.name):
            if child.is_dir():
                docs.append(read_skill(child, target))
    return docs


# --- comprobaciones por skill ----------------------------------------------------------


def check_name(doc: SkillDoc, lang: Lang | None) -> list[Problem]:
    meta = doc.meta or {}
    name = meta.get("name")
    if not isinstance(name, str) or not name:
        return [Problem(doc.rel, 1, "name-format", "falta name o no es texto")]
    out: list[Problem] = []
    if not NAME_RE.fullmatch(name) or len(name) > MAX_NAME:
        out.append(
            Problem(
                doc.rel, 1, "name-format",
                f"name {name!r}: solo a-z, 0-9 y guiones, sin guiones dobles ni en los extremos, "
                f"{MAX_NAME} caracteres como máximo",
            )
        )
    if name != doc.dir_name:
        out.append(Problem(doc.rel, 1, "name-dir", f"name {name!r} no coincide con el directorio {doc.dir_name!r}"))
    if lang is not None:
        allowed = catalog_names(lang)
        if name not in allowed:
            out.append(
                Problem(
                    doc.rel, 1, "name-catalog",
                    f"name {name!r} no es un nombre de 00-marco §5.8 para {lang}: {', '.join(sorted(allowed))}",
                )
            )
        else:
            metadata = meta.get("metadata")
            source = metadata.get(SOURCE_KEY) if isinstance(metadata, dict) else None
            if source is not None and SKILL_NAMES.get(str(source), {}).get(lang) != name:
                out.append(
                    Problem(doc.rel, 1, "name-catalog", f"{SOURCE_KEY} {source!r} no corresponde a {name!r} en {lang}")
                )
    if name.lower() in RESERVED_NAMES:
        out.append(
            Problem(doc.rel, 1, "name-reserved", f"name {name!r} es un comando integrado o un skill incluido de Claude Code")
        )
    return out


def check_description(doc: SkillDoc) -> list[Problem]:
    meta = doc.meta or {}
    desc = meta.get("description")
    if not isinstance(desc, str) or not desc.strip():
        return [Problem(doc.rel, 1, "description", "description vacía o ausente (1 a 1.024 caracteres)")]
    out: list[Problem] = []
    if len(desc) > MAX_DESCRIPTION:
        out.append(Problem(doc.rel, 1, "description", f"description de {len(desc)} caracteres (máximo {MAX_DESCRIPTION})"))
    when = meta.get("when_to_use")
    if doc.target == "claude" and isinstance(when, str) and len(desc) + len(when) > MAX_DESCRIPTION_WHEN:
        out.append(
            Problem(
                doc.rel, 1, "description",
                f"description más when_to_use suman {len(desc) + len(when)} caracteres (máximo {MAX_DESCRIPTION_WHEN})",
            )
        )
    return out


def check_fields(doc: SkillDoc) -> list[Problem]:
    if doc.target != "agents" or doc.meta is None:
        return []
    extra = [k for k in doc.meta if k not in AGENTS_FIELDS]
    if not extra:
        return []
    return [Problem(doc.rel, 1, "frontmatter", f"campos fuera del estándar Agent Skills en .agents/skills: {extra}")]


def check_body_length(doc: SkillDoc) -> list[Problem]:
    n = len(doc.body.splitlines())
    if n < MAX_BODY_LINES:
        return []
    return [Problem(doc.rel, doc.body_line, "body-length", f"cuerpo de {n} líneas (debe tener menos de {MAX_BODY_LINES})")]


def human_invocations(text: str, lang: Lang | None) -> list[int]:
    """Offsets de las invocaciones de operaciones H en un texto renderizado.

    Se admite solo la forma de ``{{human:C}}``: `` `aucove C` (nota) `` con la nota
    del idioma (o de cualquiera, si no se conoce).
    """
    notes = [HUMAN_NOTE[lang]] if lang is not None else list(HUMAN_NOTE.values())
    found: list[int] = []
    for m in HUMAN_OP_RE.finditer(text):
        start = m.start()
        if start > 0 and text[start - 1] == "`":
            close = text.find("`", m.end())
            if close != -1 and "\n" not in text[m.end() : close]:
                after = text[close + 1 :]
                if any(after.startswith(f" ({note})") for note in notes):
                    continue
        found.append(start)
    return found


def check_text(doc: SkillDoc, lang: Lang | None) -> list[Problem]:
    text = doc.text or ""
    out: list[Problem] = []
    for offset in human_invocations(text, lang):
        out.append(
            Problem(
                doc.rel, _line_of(text, offset), "human-op",
                "invoca una operación H; solo se puede mencionar con {{human:...}} (00-marco §5.7, REQ-ARCH-80)",
            )
        )
    if doc.target == "agents":
        for m in CLAUDE_TOOL_RE.finditer(text):
            out.append(
                Problem(
                    doc.rel, _line_of(text, m.start()), "claude-tool",
                    f"menciona {m.group(0)!r}, propia de Claude Code, en .agents/skills",
                )
            )
    for m in ANY_MARKER_RE.finditer(text):
        out.append(Problem(doc.rel, _line_of(text, m.start()), "marker", f"marcador sin resolver {m.group(0)!r}"))
    return out


def check_skill(doc: SkillDoc, lang: Lang | None) -> list[Problem]:
    """Todas las comprobaciones de un ``SKILL.md`` salvo la regeneración."""
    out = list(doc.problems)
    if doc.text is None:
        return out
    if doc.meta is not None:
        out += check_name(doc, lang)
        out += check_description(doc)
        out += check_fields(doc)
    out += check_body_length(doc)
    out += check_text(doc, lang)
    return out


# --- fuente única y render de referencia (01 §10.2-10.4) -------------------------------


class RenderError(ValueError):
    """Fuente de skills que no se puede renderizar (REQ-ARCH-78..80)."""


@dataclass(frozen=True)
class SkillSource:
    key: str
    directory: Path
    meta: Mapping[str, object]

    def name(self, lang: Lang) -> str:
        return cast(Mapping[str, str], self.meta["name"])[lang]

    def description(self, lang: Lang) -> str:
        return cast(Mapping[str, str], self.meta["description"])[lang]


def _localized(meta: Mapping[str, object], field_name: str, where: str) -> None:
    table = meta.get(field_name)
    if not isinstance(table, dict) or any(not isinstance(table.get(lang), str) for lang in LANGS):
        raise RenderError(f"{where}: [{field_name}] debe traer es y en como texto")


def validate_meta(meta: Mapping[str, object], directory: Path) -> None:
    """Forma de ``skill.toml`` (01 §10.2) y reglas del render (REQ-ARCH-78, REQ-ARCH-79)."""
    where = f"{directory.name}/skill.toml"
    if meta.get("key") != directory.name:
        raise RenderError(f"{where}: key debe ser {directory.name!r}")
    _localized(meta, "name", where)
    _localized(meta, "description", where)
    for lang in LANGS:
        name = cast(Mapping[str, str], meta["name"])[lang]
        if not NAME_RE.fullmatch(name) or len(name) > MAX_NAME:
            raise RenderError(f"{where}: name.{lang} {name!r} no cumple REQ-ARCH-78")
        desc = cast(Mapping[str, str], meta["description"])[lang]
        if not desc.strip() or len(desc) > MAX_DESCRIPTION:
            raise RenderError(f"{where}: description.{lang} debe tener de 1 a {MAX_DESCRIPTION} caracteres")
    cli = meta.get("cli", [])
    if not isinstance(cli, list) or not all(isinstance(c, str) and c for c in cli):
        raise RenderError(f"{where}: cli debe ser una lista de comandos")
    claude = meta.get("claude", {})
    if not isinstance(claude, dict):
        raise RenderError(f"{where}: [claude] debe ser una tabla")
    for key, value in claude.items():
        if isinstance(value, dict):
            if any(not isinstance(value.get(lang), str) for lang in LANGS):
                raise RenderError(f"{where}: claude.{key} debe traer es y en como texto")
        elif not isinstance(value, (bool, str)):
            raise RenderError(f"{where}: claude.{key} debe ser texto, booleano o texto por idioma")


def load_sources(source_dir: Path) -> list[SkillSource]:
    """Skills de ``skills/<key>/skill.toml``; los archivos sueltos (LICENSE) se ignoran."""
    if not source_dir.is_dir():
        raise RenderError(f"no existe la fuente de skills {source_dir}")
    sources: list[SkillSource] = []
    for directory in sorted((p for p in source_dir.iterdir() if p.is_dir()), key=lambda p: p.name):
        path = directory / "skill.toml"
        if not path.is_file():
            raise RenderError(f"{directory.name}: falta skill.toml")
        try:
            meta = tomllib.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise RenderError(f"{directory.name}/skill.toml: {exc}") from None
        validate_meta(meta, directory)
        sources.append(SkillSource(directory.name, directory, meta))
    for lang in LANGS:
        names = [s.name(lang) for s in sources]
        if len(names) != len(set(names)):
            raise RenderError(f"nombres repetidos en {lang} (REQ-ARCH-78)")
    return sources


def _yaml_value(value: object, lang: Lang, where: str) -> str:
    if isinstance(value, dict):
        value = value[lang]
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    raise RenderError(f"{where}: valor no admitido {value!r}")


def frontmatter(src: SkillSource, lang: Lang, target: Target) -> str:
    """Frontmatter de 01 §10.3: claves en el orden del ejemplo y texto como literal JSON (REQ-ARCH-77)."""
    lines = [
        "---",
        f"name: {json.dumps(src.name(lang), ensure_ascii=False)}",
        f"description: {json.dumps(src.description(lang), ensure_ascii=False)}",
        f"license: {json.dumps(SKILL_LICENSE)}",
    ]
    if target == "claude":
        claude = cast(Mapping[str, object], src.meta.get("claude", {}))
        for key, value in claude.items():
            lines.append(f"{key}: {_yaml_value(value, lang, f'{src.key}/skill.toml claude.{key}')}")
    lines += [
        "metadata:",
        f'  {GENERATED_KEY}: "true"',
        f"  {SOURCE_KEY}: {json.dumps(src.key, ensure_ascii=False)}",
        "---",
    ]
    return "\n".join(lines) + "\n"


def resolve_markers(
    body: str,
    *,
    lang: Lang,
    target: Target,
    project_vars: Mapping[str, str],
    names: Mapping[str, str],
    where: str,
) -> str:
    """Sustituye los marcadores de 01 §10.2; uno desconocido o sin resolver es ``RenderError``."""
    tools = mcp_tools()

    def sub(m: re.Match[str]) -> str:
        kind, arg = m.group(1), m.group(2)
        if kind == "tool":
            if arg not in tools:
                raise RenderError(f"{where}: {{{{tool:{arg}}}}} no es una herramienta del registro (REQ-ARCH-80)")
            return f"`mcp__{_meta.MCP_SERVER_NAME}__{arg}`" if target == "claude" else f"`{arg}`"
        if kind == "cli":
            if is_human_only(arg):
                raise RenderError(f"{where}: {{{{cli:{arg}}}}} es una operación H; usa {{{{human:...}}}} (REQ-ARCH-80)")
            return f"`{_meta.NAME} {arg}`"
        if kind == "human":
            return f"`{_meta.NAME} {arg}` ({HUMAN_NOTE[lang]})"
        if kind == "skill":
            if arg not in names:
                raise RenderError(f"{where}: {{{{skill:{arg}}}}} no es un skill de la fuente")
            return f"`/{names[arg]}`" if target == "claude" else f"`{names[arg]}`"
        if arg not in PROJECT_VARS or arg not in project_vars:
            raise RenderError(f"{where}: {{{{var:{arg}}}}} sin valor (language, core_pin o convention)")
        return project_vars[arg]

    out = MARKER_RE.sub(sub, body)
    left = ANY_MARKER_RE.search(out)
    if left is not None:
        raise RenderError(f"{where}: marcador desconocido o sin resolver {left.group(0)!r} (REQ-ARCH-80)")
    return out


def render_skills(source_dir: Path, lang: Lang, project_vars: Mapping[str, str]) -> dict[str, bytes]:
    """Render de referencia de 01 §10.4: ``<raíz de skills>/<name>/SKILL.md`` -> bytes."""
    sources = load_sources(source_dir)
    names = {s.key: s.name(lang) for s in sources}
    out: dict[str, bytes] = {}
    for src in sources:
        body_path = src.directory / f"body.{lang}.md"
        try:
            body = body_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RenderError(f"{src.key}/body.{lang}.md: {exc}") from None
        for target in TARGETS:
            text = resolve_markers(
                body, lang=lang, target=target, project_vars=project_vars, names=names,
                where=f"{src.key}/body.{lang}.md",
            )
            rel = f"{SKILL_ROOTS[target]}/{src.name(lang)}/{SKILL_FILE}"
            out[rel] = normalize_text((frontmatter(src, lang, target) + text).encode("utf-8"))
    return out


def check_sources(source_dir: Path, *, label: str | None = None) -> list[Problem]:
    """Marcadores de los cuerpos fuente: invocación H con ``{{cli:}}`` y marcadores desconocidos."""
    if not source_dir.is_dir():
        return []
    prefix = label if label is not None else source_dir.name
    tools = mcp_tools()
    keys = {p.name for p in source_dir.iterdir() if p.is_dir()}
    out: list[Problem] = []
    for directory in sorted((p for p in source_dir.iterdir() if p.is_dir()), key=lambda p: p.name):
        for lang in LANGS:
            path = directory / f"body.{lang}.md"
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # lo informa la regeneración
            rel = f"{prefix}/{directory.name}/{path.name}"
            for m in ANY_MARKER_RE.finditer(text):
                line = _line_of(text, m.start())
                known = MARKER_RE.fullmatch(m.group(0))
                if known is None:
                    out.append(Problem(rel, line, "marker", f"marcador desconocido {m.group(0)!r} (REQ-ARCH-80)"))
                    continue
                kind, arg = known.group(1), known.group(2)
                if kind == "cli" and is_human_only(arg):
                    out.append(Problem(rel, line, "human-op", f"{m.group(0)} invoca una operación H; usa {{{{human:{arg}}}}}"))
                elif kind == "tool" and arg not in tools:
                    out.append(Problem(rel, line, "marker", f"{m.group(0)} no es una herramienta del registro"))
                elif kind == "skill" and arg not in keys:
                    out.append(Problem(rel, line, "marker", f"{m.group(0)} no es un skill de la fuente"))
                elif kind == "var" and arg not in PROJECT_VARS:
                    out.append(Problem(rel, line, "marker", f"{m.group(0)}: solo language, core_pin y convention"))
    return out


# --- regeneración sin diferencias ------------------------------------------------------


Renderer = Callable[[Path, Lang, Mapping[str, str]], Mapping[str, bytes]]


def _first_difference(a: bytes, b: bytes) -> int:
    la, lb = a.split(b"\n"), b.split(b"\n")
    for i, (x, y) in enumerate(zip(la, lb)):
        if x != y:
            return i + 1
    return min(len(la), len(lb)) + 1


def check_regeneration(
    root: Path,
    docs: Sequence[SkillDoc],
    source_dir: Path,
    lang: Lang,
    project_vars: Mapping[str, str],
    *,
    render: Renderer = render_skills,
    label: str | None = None,
) -> list[Problem]:
    """Equivale a regenerar y correr ``git diff --exit-code`` sobre los skills generados.

    Cuentan los directorios marcados con ``<nombre>-generated: "true"`` y los que la
    regeneración produce; los skills propios del proyecto, sin la marca, no.
    """
    prefix = label if label is not None else source_dir.name
    try:
        expected = dict(render(source_dir, lang, project_vars))
    except (RenderError, OSError, UnicodeDecodeError) as exc:
        return [Problem(prefix, None, "regen", f"no se pudo regenerar desde la fuente: {exc}")]
    expected_dirs = {rel.rsplit("/", 1)[0] for rel in expected}
    actual: dict[str, bytes] = {}
    for doc in docs:
        skill_dir = f"{SKILL_ROOTS[doc.target]}/{doc.dir_name}"
        if not (doc.generated or skill_dir in expected_dirs):
            continue
        for path in sorted(doc.directory.rglob("*")):
            if path.is_file():
                actual[path.relative_to(root).as_posix()] = path.read_bytes()
    out: list[Problem] = []
    for rel in sorted(set(expected) | set(actual)):
        if rel not in actual:
            out.append(Problem(rel, None, "regen", "falta: la regeneración lo crearía"))
        elif rel not in expected:
            out.append(Problem(rel, None, "regen", "sobra: la regeneración desde la fuente no lo produce"))
        elif actual[rel] != expected[rel]:
            line = _first_difference(actual[rel], expected[rel])
            out.append(Problem(rel, line, "regen", "difiere de la regeneración desde la fuente"))
    return out


# --- punto de entrada ------------------------------------------------------------------


def read_project_vars(root: Path) -> dict[str, str]:
    """``language``, ``core_pin`` y ``convention`` del ``project.yaml`` de ``root``, si están."""
    try:
        data = yaml.safe_load((root / "project.yaml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in PROJECT_VARS if isinstance(data.get(k), str)}


def default_source() -> Path:
    """Fuente única empaquetada (``skills/`` en el repositorio del framework)."""
    from aucove.config.datafiles import data_path

    return data_path("skills")


def lint(
    root: Path,
    *,
    lang: str | None = None,
    source_dir: Path | None = None,
    project_vars: Mapping[str, str] | None = None,
    regen: bool = True,
    render: Renderer = render_skills,
) -> LintReport:
    """Revisa los skills de ``root``. Sin ``lang`` lo toma de ``project.yaml``."""
    variables = dict(read_project_vars(root) if project_vars is None else project_vars)
    chosen = lang if lang is not None else variables.get("language")
    problems: list[Problem] = []
    valid_lang: Lang | None = None
    if chosen in LANGS:
        valid_lang = cast(Lang, chosen)
        variables.setdefault("language", valid_lang)
    else:
        problems.append(Problem("project.yaml", None, "language", f"idioma desconocido o ausente: {chosen!r}"))
    docs = discover(root)
    for doc in docs:
        problems += check_skill(doc, valid_lang)
    if regen and valid_lang is not None:
        source = default_source() if source_dir is None else source_dir
        problems += check_sources(source)
        problems += check_regeneration(root, docs, source, valid_lang, variables, render=render)
    return LintReport(
        skills=tuple(d.rel for d in docs if d.text is not None),
        problems=tuple(sorted(problems, key=_sort_key)),
    )


def _print(lines: Iterable[str]) -> None:
    for line in lines:
        sys.stdout.write(line + "\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validador propio de skills (T6, REQ-MVP-35).")
    parser.add_argument("root", type=Path, help="raíz del árbol renderizado")
    parser.add_argument("--lang", choices=LANGS, help="idioma; por defecto, el de project.yaml")
    parser.add_argument("--source", type=Path, help="fuente única de skills; por defecto, la empaquetada")
    parser.add_argument("--no-regen", action="store_true", help="omite la comprobación de regeneración")
    args = parser.parse_args(argv)
    report = lint(args.root, lang=args.lang, source_dir=args.source, regen=not args.no_regen)
    _print(str(p) for p in report.problems)
    _print([f"{len(report.skills)} skills revisados, {len(report.problems)} problemas"])
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover - uso como script
    raise SystemExit(main())
