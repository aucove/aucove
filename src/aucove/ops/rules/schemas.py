# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Regla ``schemas`` (09-ci §4.1; 03-esquemas §2.3-2.5, §3, §4, §6 y §7).

Valida con los esquemas del **núcleo instalado** (los empaquetados, nunca los del
árbol) el contenido del árbol validado (REQ-CI-25: la consistencia lee los archivos
del PR):

- ``project.yaml`` y ``roles.yaml`` siempre (``config.project.parse_project`` y
  ``config.roles.parse_roles``, con sus reglas internas); si falta alguno,
  ``E_CONFIG_NOT_FOUND``.
- Si los dos son válidos, la matriz de cuentas de 10-seguridad §8.3 con la
  configuración efectiva (``check_accounts`` de 10 §8.4; REQ-SEC-64, REQ-SCHEMA-91, D-48):
  ``E_VALIDATION_ACCOUNT_TYPE`` por cada persona cuyo ``account_type`` no se admite y,
  en el caso C1, el aviso ``W_VALIDATION_ACCOUNT_TYPE``. Y, con ``mode: individual``,
  una sola persona activa, la del ``lead``, sin ``deputy`` (REQ-SCHEMA-94, 00-marco D-57):
  ``E_VALIDATION_ROLES_MODE`` en ``/people`` o en ``/deputy``. Las dos reglas leen
  ``project.yaml`` y ``roles.yaml`` del mismo árbol (03 §2.5).
- Cada registro de ``memory/decisions/`` agregado o cambiado: nombre (REQ-SCHEMA-45,
  46 y 48), front matter contra ``decision.schema.json`` (REQ-SCHEMA-50), ``supersedes``
  contra la base (REQ-SCHEMA-52), secciones del cuerpo (REQ-SCHEMA-53) y autor
  (REQ-SCHEMA-54).
- Cada nota ``memory/notes/task-<n>.md`` agregada o cambiada: front matter contra
  ``session-note.schema.json`` y ``task`` (REQ-SCHEMA-56), marcadores y bloques
  (REQ-SCHEMA-57 y 60), ``login`` de cada bloque en la base (REQ-SCHEMA-58) y bloques
  ya integrados que cambiaron (REQ-SCHEMA-59, contra ``base_tree``).

Personas, dueños y registros reemplazables se resuelven con la base (03 §2.5): la
política (``RuleInput.policy``) y, para el contenido de los registros, ``base_tree``.
Los archivos se leen como texto de 5 MB como máximo y sin seguir enlaces
(``TreeView``); una entrada que no es un archivo regular no se lee.
"""

from __future__ import annotations

import functools
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ... import _meta
from ...config.accounts import account_verdict
from ...config.yamlload import FINDING_PARAMS as CONFIG_CODES
from ...domain.findings import Finding, has_errors
from ...domain.text import same_login
from ...vcs.tree import TreeView, is_regular_mode
from .base import Change, RuleInput, RuleSpec, make_finding, unavailable

if TYPE_CHECKING:
    from ...config.project import ProjectConfig
    from ...config.roles import Roles

RULE_ID = "schemas"
PROJECT_FILE = "project.yaml"
ROLES_FILE = "roles.yaml"
DECISIONS_DIR = "memory/decisions/"
NOTES_DIR = "memory/notes/"
MAX_SLUG = 48  # REQ-SCHEMA-45
MAX_NOTE_BYTES = 64 * 1024  # REQ-SCHEMA-60
MAX_BLOCK_CHARS = 4000  # REQ-SCHEMA-60

DECISION_PATH = re.compile(
    r"^memory/decisions/(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})-(?P<n>[1-9][0-9]*)-"
    r"(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)\.md$"
)
NOTE_PATH = re.compile(r"^memory/notes/task-(?P<n>[1-9][0-9]*)\.md$")
TASK_BRANCH = re.compile(r"^task/([1-9][0-9]*)$")
_MARKER_PREFIX = f"<!-- {_meta.NAME}:session"
_MARKER = re.compile(r"^<!-- " + re.escape(_meta.NAME) + r":session (\{.*\}) -->$")
_HEADING = re.compile(r"^##[ \t]+(.+?)[ \t]*#*[ \t]*$")

# Secciones obligatorias del cuerpo de un registro (03 §6.3): (es, en).
BODY_SECTIONS: tuple[tuple[str, str], ...] = (
    ("Contexto", "Context"),
    ("Decisión", "Decision"),
    ("Consecuencias", "Consequences"),
)

OWN_CODES: Mapping[str, frozenset[str]] = {
    "E_CONFIG_NOT_FOUND": frozenset({"file"}),
    "E_VALIDATION_DECISION_NAME": frozenset({"detail"}),
    "E_VALIDATION_DECISION_SUPERSEDES": frozenset({"id", "detail"}),
    "E_VALIDATION_DECISION_BODY": frozenset({"missing"}),
    "W_VALIDATION_DECISION_AUTHOR": frozenset({"login", "author"}),
    "E_VALIDATION_NOTE": frozenset({"detail"}),
    "W_VALIDATION_NOTE_REWRITTEN": frozenset({"blocks"}),
    "E_VALIDATION_ACCOUNT_TYPE": frozenset({"login", "detail"}),
    "E_VALIDATION_ROLES_MODE": frozenset({"detail"}),
    # Aviso de C1 (10 §8.3): 10-seguridad no le da código; se usa el par del error.
    "W_VALIDATION_ACCOUNT_TYPE": frozenset({"login"}),
}
CODES: Mapping[str, frozenset[str]] = {**CONFIG_CODES, **OWN_CODES}


@functools.cache
def _block_validator() -> Any:
    """Validador de ``session-note.schema.json#/$defs/session_block`` (sin red)."""
    import jsonschema

    from ...config.schemas import format_checker, registry, schema_id

    ref = {"$ref": schema_id("session-note") + "#/$defs/session_block"}
    return jsonschema.Draft202012Validator(ref, registry=registry(), format_checker=format_checker())


def _norm(text: str) -> str:
    return text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")


@dataclass
class _Ctx:
    """Estado de una evaluación: entrada, árbol y registros de la base (perezosos)."""

    inp: RuleInput
    tree: TreeView
    findings: list[Finding] = field(default_factory=list)
    _base: tuple[frozenset[str], dict[str, Mapping[str, object]]] | None = None

    def add(self, code: str, path: str | None, *, line: int | None = None, pointer: str | None = None,
            severity: str = "error", **params: object) -> None:
        self.findings.append(make_finding(RULE, self.inp, code, severity=severity,  # type: ignore[arg-type]
                                          path=path, line=line, pointer=pointer, **params))

    def t(self, key: str, **kw: object) -> str:
        return self.inp.t(key, **kw)

    def base_decisions(self) -> tuple[frozenset[str], dict[str, Mapping[str, object]]]:
        """IDs de los registros de la base y el front matter de los que se pueden leer."""
        if self._base is None:
            self._base = _base_decisions(self.inp)
        return self._base


# -- project.yaml y roles.yaml ----------------------------------------------------


def _config_bytes(ctx: _Ctx, name: str) -> bytes | None:
    """Contenido de ``name`` si es un archivo regular legible; si no, su hallazgo y ``None``."""
    mode = ctx.tree.entries().get(name)
    if mode is None:
        ctx.add("E_CONFIG_NOT_FOUND", name, file=name)
        return None
    if not is_regular_mode(mode):
        ctx.add("E_CONFIG_INVALID", name, detail=ctx.t("validate.schemas.not_regular"))
        return None
    data = ctx.tree.read_bytes(name)
    if data is None:
        ctx.add("E_CONFIG_INVALID", name, detail=ctx.t("validate.schemas.unreadable"))
    return data


def _check_project(ctx: _Ctx) -> ProjectConfig | None:
    from ...config.project import parse_project

    data = _config_bytes(ctx, PROJECT_FILE)
    if data is None:
        return None
    cfg, found = parse_project(data, file=PROJECT_FILE, t=ctx.inp.t, check=RULE_ID)
    ctx.findings.extend(found)
    return cfg


def _check_roles(ctx: _Ctx) -> tuple[Roles, bytes] | None:
    from ...config.roles import parse_roles

    data = _config_bytes(ctx, ROLES_FILE)
    if data is None:
        return None
    roles, found = parse_roles(data, file=ROLES_FILE, t=ctx.inp.t, check=RULE_ID)
    ctx.findings.extend(found)
    return (roles, data) if roles is not None else None


def _check_accounts(ctx: _Ctx, cfg: ProjectConfig, roles: Roles, roles_text: bytes) -> None:
    """Matriz de 10-seguridad §8.3 sobre las personas de ``roles.yaml`` (REQ-SEC-64).

    Usa el ``require_commercial`` efectivo (REQ-SCHEMA-91): si ``project.yaml`` no lo
    escribe, el detalle lo atribuye al valor por defecto de ``disclosure`` (D-48).
    Cada hallazgo apunta al ``account_type`` de la persona; las inactivas también
    cuentan, como en el pseudocódigo de 10 §8.4.
    """
    from ...config.yamlload import line_for, pointer_lines

    level = str(cfg.data_policy["level"])
    required = bool(cfg.data_policy["require_commercial"])
    given = cfg.raw.get("data_policy")
    explicit = isinstance(given, Mapping) and "require_commercial" in given
    lines: Mapping[str, int] | None = None
    for i, person in enumerate(roles.people):
        verdict = account_verdict(person.account_type, level, required)
        if verdict == "allowed":
            continue
        if lines is None:
            lines = pointer_lines(roles_text)
        pointer = f"/people/{i}/account_type"
        line = line_for(lines, pointer)
        if verdict == "consumer_terms":
            ctx.add("W_VALIDATION_ACCOUNT_TYPE", ROLES_FILE, line=line, pointer=pointer, severity="warning",
                    login=person.login)
            continue
        if verdict == "not_allowed":
            detail = ctx.t("validate.accounts.not_allowed", account_type=person.account_type, level=level)
        elif explicit:
            detail = ctx.t("validate.accounts.commercial", account_type=person.account_type)
        else:
            detail = ctx.t("validate.accounts.commercial_default", account_type=person.account_type)
        ctx.add("E_VALIDATION_ACCOUNT_TYPE", ROLES_FILE, line=line, pointer=pointer, login=person.login,
                detail=detail)


def _check_mode(ctx: _Ctx, cfg: ProjectConfig, roles: Roles, roles_text: bytes) -> None:
    """REQ-SCHEMA-94: con ``mode: individual``, una sola persona activa (el ``lead``) y sin ``deputy``."""
    from ...config.roles import mode_problems
    from ...config.yamlload import line_for, pointer_lines

    problems = mode_problems(roles, cfg.mode)
    if not problems:
        return
    lines = pointer_lines(roles_text)
    for pointer, reason in problems:
        ctx.add("E_VALIDATION_ROLES_MODE", ROLES_FILE, line=line_for(lines, pointer), pointer=pointer,
                detail=ctx.t(f"validate.mode.{reason}", lead=roles.lead))


# -- registros de decisión ----------------------------------------------------------


def _front_matter_of(text: str | None) -> dict[str, object] | None:
    from ...config.yamlload import split_front_matter

    if text is None:
        return None
    fm, _body, found = split_front_matter(text, file="-")
    return fm if not found and isinstance(fm, dict) else None


def _base_decisions(inp: RuleInput) -> tuple[frozenset[str], dict[str, Mapping[str, object]]]:
    """Registros que existen en la base (03 §2.5: ``supersedes`` se resuelve con la base).

    En ``tree`` y ``pr`` la existencia la da la política; en ``base`` la política es el
    propio árbol, así que se usa el estado anterior (``base_tree``). El contenido, para
    el estado efectivo, sale de ``base_tree`` cuando lo tiene.
    """
    base_tree = inp.base_tree
    if inp.mode == "base":
        paths = sorted(p for p in (base_tree.entries() if base_tree is not None else {}) if DECISION_PATH.match(p))
    else:
        paths = sorted(p for p in (inp.policy.decisions if inp.policy is not None else ()) if DECISION_PATH.match(p))
    ids = frozenset(p[len(DECISIONS_DIR):-3] for p in paths)
    records: dict[str, Mapping[str, object]] = {}
    if base_tree is not None:
        entries = base_tree.entries()
        for p in paths:
            if is_regular_mode(entries.get(p)):
                fm = _front_matter_of(base_tree.read_text(p))
                if fm is not None:
                    records[p[len(DECISIONS_DIR):-3]] = fm
    return ids, records


def _superseded(target: str, records: Mapping[str, Mapping[str, object]]) -> bool:
    """``effective_status`` de 03 §6.5: otro registro ``accepted`` de la base lo reemplaza."""
    for rid, rec in records.items():
        if rid == target or rec.get("status") != "accepted":
            continue
        sup = rec.get("supersedes")
        if isinstance(sup, list) and target in sup:
            return True
    return False


def _missing_sections(body: str, lang: str) -> list[str]:
    found = set()
    for raw in body.split("\n"):
        m = _HEADING.match(raw)
        if m:
            found.add(unicodedata.normalize("NFC", m.group(1)).casefold())
    missing = []
    for es, en in BODY_SECTIONS:
        names = {unicodedata.normalize("NFC", es).casefold(), en.casefold()}
        if not names & found:
            missing.append("## " + (es if lang == "es" else en))
    return missing


def _branch_mismatch(inp: RuleInput, path: str, n: int) -> Finding | None:
    """``E_VALIDATION_DECISION_NAME`` si la cabeza es ``task/<m>`` con ``m`` distinto de ``n``."""
    branch = TASK_BRANCH.match(inp.head_ref or "")
    if branch is None or int(branch.group(1)) == n:
        return None
    return make_finding(RULE, inp, "E_VALIDATION_DECISION_NAME", severity="error", path=path,
                        detail=inp.t("validate.decision.branch_mismatch", n=n, branch=inp.head_ref))


def branch_findings(inp: RuleInput) -> list[Finding]:
    """Registros agregados o cambiados cuyo ``<n>`` no es el de la rama ``task/<n>``.

    Es la parte de la regla que no depende de la versión del núcleo ni del contenido del
    registro, solo del nombre y de ``head_ref``. El motor la ejecuta en modo ``pr`` cuando
    toma ``schemas`` del reporte del job ``pin``, que corre con HEAD separado y no la ve
    (REQ-CI-45). Un nombre que no cumple el patrón lo informa la regla completa.
    """
    out: list[Finding] = []
    for path in _targets(inp.changes):
        m = DECISION_PATH.match(path)
        if m is None or len(m.group("slug")) > MAX_SLUG:
            continue
        mismatch = _branch_mismatch(inp, path, int(m.group("n")))
        if mismatch is not None:
            out.append(mismatch)
    return out


def _check_decision(ctx: _Ctx, path: str) -> None:
    from ...config.schemas import validate
    from ...config.yamlload import line_for, pointer_lines, split_front_matter

    rest = path[len(DECISIONS_DIR):]
    name_code = "E_VALIDATION_DECISION_NAME"
    mode = ctx.tree.entries().get(path)
    if "/" in rest:
        ctx.add(name_code, path, detail=ctx.t("validate.decision.nested"))
        return
    if rest == ".gitkeep":
        data = ctx.tree.read_bytes(path)
        if not is_regular_mode(mode) or data is None:
            ctx.add(name_code, path, detail=ctx.t("validate.schemas.not_regular"))
        elif data:
            ctx.add(name_code, path, detail=ctx.t("validate.decision.gitkeep"))
        return
    m = DECISION_PATH.match(path)
    if m is None or len(m.group("slug")) > MAX_SLUG:
        ctx.add(name_code, path, detail=ctx.t("validate.decision.bad_name"))
        return
    if not is_regular_mode(mode):
        ctx.add(name_code, path, detail=ctx.t("validate.schemas.not_regular"))
        return
    text = ctx.tree.read_text(path)
    if text is None:
        ctx.add("E_VALIDATION_FRONT_MATTER", path, detail=ctx.t("validate.schemas.unreadable"))
        return
    fm, body, found = split_front_matter(text, file=path, t=ctx.inp.t, check=RULE_ID)
    if found or fm is None:
        ctx.findings.extend(found)
        return
    schema_findings = validate("decision", fm, file=path, source_text=text, check=RULE_ID, t=ctx.inp.t)
    ctx.findings.extend(schema_findings)
    missing = _missing_sections(body, ctx.inp.t.lang)
    if missing:
        ctx.add("E_VALIDATION_DECISION_BODY", path, missing=", ".join(missing))
    if has_errors(schema_findings):
        return  # el resto necesita un front matter válido
    lines = pointer_lines(text)
    stem = rest[:-3]
    n = int(m.group("n"))
    checks = (
        ("/id", fm.get("id") != stem, "validate.decision.id_mismatch", {"expected": stem}),
        ("/date", fm.get("date") != m.group("date"), "validate.decision.date_mismatch", {"expected": m.group("date")}),
        ("/task", fm.get("task") != n, "validate.decision.task_mismatch", {"expected": n}),
    )
    for pointer, bad, key, kw in checks:
        if bad:
            ctx.add(name_code, path, line=line_for(lines, pointer), pointer=pointer, detail=ctx.t(key, **kw))
    mismatch = _branch_mismatch(ctx.inp, path, n)
    if mismatch is not None:
        ctx.findings.append(mismatch)
    _check_supersedes(ctx, path, fm, lines)
    _check_author(ctx, path, fm, lines)


def _check_supersedes(ctx: _Ctx, path: str, fm: Mapping[str, object], lines: Mapping[str, int]) -> None:
    from ...config.yamlload import line_for

    sup = fm.get("supersedes")
    if not isinstance(sup, list) or not sup:
        return
    ids, records = ctx.base_decisions()
    for i, target in enumerate(sup):
        pointer = f"/supersedes/{i}"
        if target not in ids:
            detail = ctx.t("validate.decision.supersedes_missing")
        elif _superseded(str(target), records):
            detail = ctx.t("validate.decision.supersedes_superseded")
        else:
            continue
        ctx.add("E_VALIDATION_DECISION_SUPERSEDES", path, line=line_for(lines, pointer), pointer=pointer,
                id=str(target), detail=detail)


def _check_person(ctx: _Ctx, path: str, login: str, ref: str, *, line: int | None, pointer: str | None) -> None:
    """REQ-SCHEMA-54 y REQ-SCHEMA-58: persona activa de ``roles.yaml`` de la base."""
    policy = ctx.inp.policy
    if policy is None:
        return
    person = policy.roles.person(login)
    if person is None:
        ctx.add("E_VALIDATION_UNKNOWN_PERSON", path, line=line, pointer=pointer, login=login, ref=ref)
    elif not person.active:
        ctx.add("E_VALIDATION_INACTIVE_PERSON", path, line=line, pointer=pointer, login=login, ref=ref)


def _check_author(ctx: _Ctx, path: str, fm: Mapping[str, object], lines: Mapping[str, int]) -> None:
    from ...config.yamlload import line_for

    author = fm.get("author")
    if not isinstance(author, str):
        return
    line = line_for(lines, "/author")
    _check_person(ctx, path, author, "author", line=line, pointer="/author")
    pr = ctx.inp.pr
    if pr is not None and not same_login(author, pr.author):
        ctx.add("W_VALIDATION_DECISION_AUTHOR", path, line=line, pointer="/author", severity="warning",
                login=author, author=pr.author)


# -- notas de sesión ------------------------------------------------------------------


@dataclass(frozen=True)
class _Block:
    line: int  # línea del marcador (1-based)
    marker: str  # JSON del marcador
    text: str  # del marcador hasta antes del siguiente


def _blocks(text: str) -> tuple[list[_Block], list[int]]:
    """Bloques de sesión del cuerpo y líneas con un marcador mal formado."""
    lines = _norm(text).split("\n")
    start = 0
    if lines and lines[0] == "---":
        end = next((i for i in range(1, len(lines)) if lines[i] == "---"), None)
        start = end + 1 if end is not None else len(lines)
    blocks: list[_Block] = []
    bad: list[int] = []
    current: tuple[int, str, list[str]] | None = None
    for i in range(start, len(lines)):
        raw = lines[i]
        if raw.startswith(_MARKER_PREFIX):
            if current is not None:
                blocks.append(_Block(current[0], current[1], "\n".join(current[2]).rstrip()))
                current = None
            m = _MARKER.match(raw)
            if m is None:
                bad.append(i + 1)
                continue
            current = (i + 1, m.group(1), [raw])
        elif current is not None:
            current[2].append(raw)
    if current is not None:
        blocks.append(_Block(current[0], current[1], "\n".join(current[2]).rstrip()))
    return blocks, bad


def _schema_where(err: Any) -> str:
    from ...config.yamlload import escape_pointer

    return "".join("/" + escape_pointer(str(p)) for p in err.absolute_path) or "/"


def _check_note(ctx: _Ctx, path: str) -> None:
    from ...config.schemas import validate
    from ...config.yamlload import line_for, pointer_lines, split_front_matter

    m = NOTE_PATH.match(path)
    if m is None:
        return  # otros archivos de memory/notes/ son de conv.memory.unexpected-file (02 REQ-CONV-70)
    code = "E_VALIDATION_NOTE"
    if not is_regular_mode(ctx.tree.entries().get(path)):
        ctx.add(code, path, detail=ctx.t("validate.schemas.not_regular"))
        return
    data = ctx.tree.read_bytes(path)
    if data is None:
        ctx.add(code, path, detail=ctx.t("validate.schemas.unreadable"))
        return
    if len(data) > MAX_NOTE_BYTES:
        ctx.add(code, path, detail=ctx.t("validate.note.too_large"))
        return
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        ctx.add(code, path, detail=ctx.t("validate.schemas.unreadable"))
        return
    fm, _body, found = split_front_matter(text, file=path, t=ctx.inp.t, check=RULE_ID)
    if found or fm is None:
        ctx.findings.extend(found)
        return
    schema_findings = validate("session-note", fm, file=path, source_text=text, check=RULE_ID, t=ctx.inp.t)
    ctx.findings.extend(schema_findings)
    n = int(m.group("n"))
    if not has_errors(schema_findings) and fm.get("task") != n:
        lines = pointer_lines(text)
        ctx.add(code, path, line=line_for(lines, "/task"), pointer="/task",
                detail=ctx.t("validate.note.task_mismatch", expected=n))
    blocks, bad = _blocks(text)
    for line in bad:
        ctx.add(code, path, line=line, detail=ctx.t("validate.note.bad_marker"))
    previous_date: str | None = None
    for index, block in enumerate(blocks, 1):
        try:
            meta = json.loads(block.marker)
        except (ValueError, RecursionError):
            ctx.add(code, path, line=block.line, detail=ctx.t("validate.note.marker_json", index=index))
            continue
        errors = sorted(_block_validator().iter_errors(meta), key=lambda e: [str(p) for p in e.absolute_path])
        if errors:
            ctx.add(code, path, line=block.line,
                    detail=ctx.t("validate.note.marker_schema", index=index, where=_schema_where(errors[0])))
            continue
        if meta["n"] != index:
            ctx.add(code, path, line=block.line,
                    detail=ctx.t("validate.note.sequence", index=index, found=meta["n"], expected=index))
        if previous_date is not None and meta["date"] < previous_date:
            ctx.add(code, path, line=block.line, detail=ctx.t("validate.note.date_order", index=index))
        previous_date = meta["date"]
        if len(block.text) > MAX_BLOCK_CHARS:
            ctx.add(code, path, line=block.line, detail=ctx.t("validate.note.block_too_large", index=index))
        _check_person(ctx, path, meta["login"], ctx.t("validate.note.block_ref", index=index),
                      line=block.line, pointer=None)
    _check_rewritten(ctx, path, blocks)


def _check_rewritten(ctx: _Ctx, path: str, blocks: Sequence[_Block]) -> None:
    """REQ-SCHEMA-59: los bloques que ya estaban en ``base_tree`` no deben cambiar."""
    base_tree = ctx.inp.base_tree
    if base_tree is None or not is_regular_mode(base_tree.entries().get(path)):
        return
    base_text = base_tree.read_text(path)
    if base_text is None:
        return
    before, _bad = _blocks(base_text)
    changed = sum(1 for i, old in enumerate(before) if i >= len(blocks) or blocks[i].text != old.text)
    if changed:
        ctx.add("W_VALIDATION_NOTE_REWRITTEN", path, severity="warning", blocks=changed)


# -- regla ----------------------------------------------------------------------------


def _targets(changes: Sequence[Change]) -> list[str]:
    paths = {ch.path for ch in changes if ch.status != "removed"}
    return sorted(p for p in paths if p.startswith((DECISIONS_DIR, NOTES_DIR)))


def _run(inp: RuleInput) -> Sequence[Finding]:
    tree = inp.tree
    if tree is None:
        return [unavailable(RULE, inp, inp.t("validate.reason.no_tree"))]
    ctx = _Ctx(inp, tree)
    cfg = _check_project(ctx)
    roles = _check_roles(ctx)
    if cfg is not None and roles is not None:  # sin configuración efectiva no hay matriz
        _check_accounts(ctx, cfg, *roles)
        _check_mode(ctx, cfg, *roles)
    for path in _targets(inp.changes):
        if path.startswith(DECISIONS_DIR):
            _check_decision(ctx, path)
        else:
            _check_note(ctx, path)
    return ctx.findings


RULE = RuleSpec(
    id=RULE_ID,
    modes=frozenset({"tree", "pr", "base"}),
    run=_run,
    codes=CODES,
)

RULES: tuple[RuleSpec, ...] = (RULE,)
