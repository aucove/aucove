# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Regla ``traceability``: los trailers de los commits del PR y la traza de su cuerpo (09-ci §4.1;
11-trazabilidad §2.10, §3.3 y §3.4; 03-esquemas §9.4).

Solo en modo ``pr``: en ``base``, el commit de squash lo valida ``ci audit`` (REQ-CI-46).

- Commits: ``git log <merge-base>..<head_sha>`` en el checkout del PR, sin el tope de 250 de la
  API; sin checkout es un error. Se eximen los commits con el mismo árbol que su primer padre
  (el de ``claim``) y las fusiones cuyo segundo padre es alcanzable desde la base (las de
  ``update``); ninguno por su asunto. Cada uno se valida en el contexto ``branch-commit``.
- Cuerpo, en el contexto ``pr-body``: los trailers, con un ``Signed-off-by`` igual al nombre
  exacto y al correo (o la forma noreply de REQ-TRACE-24) del autor del PR en ``roles.yaml`` de
  la base (``E_VALIDATION_SIGNOFF_MISMATCH``); el bloque ``pr-meta`` de 03 §9.4 (REQ-SCHEMA-76 a
  79 y 87); y el objeto ``trace`` (``E_VALIDATION_TRACE`` si falta o no cumple ``trace/v1``),
  que debe coincidir con los trailers del cuerpo y con el front matter de la nota de sesión de la
  tarea en la cabeza (REQ-TRACE-21 y REQ-TRACE-40; ``E_VALIDATION_TRACE_MISMATCH``).

Todo es error, salvo los hallazgos del cuerpo de un PR que no sale de ``task/<n>``, que son avisos
(11 §2.10). Las faltas de los trailers se informan una vez por commit, clave y motivo.

``trace_findings`` es lo común de ``pr-body`` y ``squash-commit``: ``ci audit`` lo usa con el
mensaje del commit de squash en la base (REQ-TRACE-41), donde la falta del bloque ``pr-meta`` es
aviso hasta S-10.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from ...domain.findings import Finding, Severity
from ...domain.text import same_login
from ...domain.trailers import (
    TRAILER_MISSING,
    TRAILER_SYNTAX,
    TrailerCheck,
    TrailerContext,
    parse_signoff,
    validate_trailers,
)
from ..note import note_path, parse_note
from ..prbody import parse_pr_meta
from ..snapshot import task_number
from ..trace import sections_of
from .base import RuleInput, RuleSpec, make_finding

if TYPE_CHECKING:
    from ...config.roles import Roles
    from ...vcs.git import Git

__all__ = [
    "CODES", "MAX_COMMITS", "RULE", "RULES", "BranchCommit", "body_findings", "branch_commits", "problem",
    "signer_matches", "trace_findings",
]

RULE_ID: Final[str] = "traceability"
TRACE: Final[str] = "E_VALIDATION_TRACE"
SIGNOFF: Final[str] = "E_VALIDATION_SIGNOFF_MISMATCH"
MISMATCH: Final[str] = "E_VALIDATION_TRACE_MISMATCH"
META_MISSING: Final[str] = "E_VALIDATION_PR_META_MISSING"
META: Final[str] = "E_VALIDATION_PR_META"
MAX_COMMITS: Final[int] = 10_000  # sin el tope de 250 de la API (09 §4.1); una rama de tarea no llega
_TRAILER_PARAMS = frozenset({"source", "key", "reason"})
# Código de hallazgo -> marcadores de finding.<code>.* (validate.toml).
CODES: Final[Mapping[str, frozenset[str]]] = {
    TRACE: frozenset({"reason"}),
    TRAILER_SYNTAX: _TRAILER_PARAMS,
    TRAILER_MISSING: _TRAILER_PARAMS,
    SIGNOFF: frozenset({"source", "login", "found"}),
    MISMATCH: frozenset({"field", "source"}),
    META_MISSING: frozenset(),
    META: frozenset({"reason"}),
}
_DECISION = re.compile(r"memory/decisions/([0-9]{4}-[0-9]{2}-[0-9]{2}-[1-9][0-9]*-[a-z0-9]+(?:-[a-z0-9]+)*)\.md")
_NOREPLY = re.compile(r"([1-9][0-9]*)\+([^@\s]+)@users\.noreply\.github\.com", re.IGNORECASE)


def _finding(inp: RuleInput, code: str, severity: Severity, *, path: str | None = None, **params: object) -> Finding:
    return make_finding(RULE, inp, code, severity=severity, path=path, **params)


# -- los commits del PR ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BranchCommit:
    """Un commit de ``merge-base..head`` y si la regla lo exime (09 §4.1)."""

    sha: str
    message: str
    exempt: bool


def branch_commits(git: Git, merge_base: str, head: str, *, base: str) -> list[BranchCommit]:
    """Los commits de ``merge_base..head``, del más viejo al más nuevo.

    Exentos: el mismo árbol que el primer padre, o dos padres o más con el segundo alcanzable
    desde ``base``. ``ValueError`` con más de ``MAX_COMMITS`` o si falta un objeto; los errores
    de git se propagan.
    """
    from ...vcs.git import EMPTY_TREE, parse_commit

    listed = git.run("rev-list", "--reverse", "--topo-order", f"--max-count={MAX_COMMITS + 1}", f"{merge_base}..{head}")
    shas = listed.stdout.decode("ascii", "replace").split()
    if len(shas) > MAX_COMMITS:
        raise ValueError("too_many")
    raws = git.cat_file_objects(shas)
    headers = {}
    messages: dict[str, str] = {}
    for sha in shas:
        found = raws.get(sha)
        if found is None:
            raise ValueError("missing_object")
        headers[sha] = parse_commit(found[1])
        parts = found[1].split(b"\n\n", 1)
        messages[sha] = parts[1].decode("utf-8", "replace") if len(parts) == 2 else ""
    trees = {sha: h.tree for sha, h in headers.items()}
    outside = sorted({h.parents[0] for h in headers.values() if h.parents and h.parents[0] not in headers})
    for sha, found in git.cat_file_objects(outside).items():
        if found is not None:
            trees[sha] = parse_commit(found[1]).tree
    out: list[BranchCommit] = []
    for sha in shas:
        h = headers[sha]
        parent_tree = trees.get(h.parents[0]) if h.parents else EMPTY_TREE
        empty = parent_tree is not None and h.tree == parent_tree
        from_base = len(h.parents) >= 2 and git.is_ancestor(h.parents[1], base)
        out.append(BranchCommit(sha, messages[sha], empty or from_base))
    return out


def _reason(inp: RuleInput, reason: str, task: int | None) -> str:
    if reason == "other_task":
        return inp.t("validate.trace.reason.other_task", task=task)
    return inp.t(f"validate.trace.reason.{reason}")


def trailer_findings(inp: RuleInput, check: TrailerCheck, source: str, severity: Severity,
                     task: int | None) -> list[Finding]:
    """Las faltas de ``validate_trailers`` como hallazgos, una vez por clave y motivo."""
    out: list[Finding] = []
    seen: set[tuple[str, str, str]] = set()
    for p in check.problems:
        if (p.code, p.key, p.reason) not in seen:
            seen.add((p.code, p.key, p.reason))
            out.append(_finding(inp, p.code, severity, source=source, key=p.key, reason=_reason(inp, p.reason, task)))
    return out


def _commit_findings(inp: RuleInput, task: int | None) -> list[Finding]:
    from ...vcs.git import Git, GitError, GitUnavailable

    tree = inp.tree
    merge_base = inp.base_tree.sha if inp.base_tree is not None else None
    if tree is None or tree.root is None or not merge_base or not inp.head_sha:
        return [_finding(inp, TRACE, "error", reason=inp.t("validate.trace.reason.no_tree"))]
    try:
        git = Git(Path(tree.root), offline=True)
        commits = branch_commits(git, merge_base, inp.head_sha, base=inp.base_sha or merge_base)
    except ValueError as exc:
        if str(exc) == "too_many":
            return [_finding(inp, TRACE, "error", reason=inp.t("validate.trace.reason.too_many", max=MAX_COMMITS))]
        return [_finding(inp, TRACE, "error", reason=inp.t("validate.trace.reason.git"))]
    except (GitError, GitUnavailable, LookupError, OSError):
        return [_finding(inp, TRACE, "error", reason=inp.t("validate.trace.reason.git"))]
    out: list[Finding] = []
    for c in commits:
        if not c.exempt:
            check = validate_trailers(c.message, "branch-commit", task=task)
            out += trailer_findings(inp, check, inp.t("validate.trace.source.commit", sha=c.sha[:12]), "error", task)
    return out


# -- el cuerpo del PR -----------------------------------------------------------------------------


def signer_matches(value: str, login: str, roles: Roles) -> bool:
    """REQ-TRACE-32: el nombre exacto de ``people[].name`` del autor y su ``people[].email`` o la
    forma noreply de REQ-TRACE-24 (con su ``forge_id`` si lo declara)."""
    person = roles.person(login)
    parsed = parse_signoff(value)
    if person is None or parsed is None or parsed[0] != person.name:
        return False
    email = parsed[1]
    if person.email and email.casefold() == person.email.casefold():
        return True
    m = _NOREPLY.fullmatch(email)
    return (m is not None and same_login(m.group(2), person.login)
            and (person.forge_id is None or int(m.group(1)) == person.forge_id))


def _decisions_added(inp: RuleInput) -> list[str]:
    return sorted({m.group(1) for c in inp.changes if c.status in ("added", "renamed")
                   for m in [_DECISION.fullmatch(c.path)] if m})


def _sections_touched(inp: RuleInput) -> list[str]:
    assert inp.policy is not None
    declared = frozenset(s.id for s in inp.policy.roles.sections)
    return sorted(sections_of((p for c in inp.changes for p in (c.path, c.previous_path) if p), declared))


def _same_as_trailers(inp: RuleInput, trace: Mapping[str, Any], check: TrailerCheck, severity: Severity,
                      source: str) -> list[Finding]:
    """REQ-TRACE-21: el objeto ``trace`` y los trailers del mismo texto dicen lo mismo."""
    tr = check.trailers
    fields: list[str] = []
    if tr.ai_use is not None and tuple(trace.get("ai_use") or ()) != tr.ai_use:
        fields.append("ai_use")
    if {(a.get("tool"), a.get("model")) for a in trace.get("assisted_by") or ()} != set(tr.assisted):
        fields.append("assisted_by")
    if tr.task is not None and trace.get("task") != tr.task:
        fields.append("task")
    if sorted(trace.get("sections") or ()) != sorted(tr.sections):
        fields.append("sections")
    sign = trace.get("sign_off") or {}
    if check.signed_off is not None and parse_signoff(check.signed_off) != (sign.get("name"), sign.get("email")):
        fields.append("sign_off")
    return [_finding(inp, MISMATCH, severity, field=f, source=source) for f in fields]


def _same_as_note(inp: RuleInput, trace: Mapping[str, Any], task: int, severity: Severity) -> list[Finding]:
    """REQ-TRACE-40: el bloque coincide con el front matter de la nota de sesión de la cabeza."""
    if inp.tree is None:
        return []  # sin checkout ya hay un error (los commits)
    path = note_path(task)
    text = inp.tree.read_text(path)
    if text is None:
        return [_finding(inp, TRACE, severity, path=path, reason=inp.t("validate.trace.reason.note_missing", path=path))]
    front = parse_note(text.replace("\r\n", "\n"), path).front
    if front is None:
        return [_finding(inp, TRACE, severity, path=path, reason=inp.t("validate.trace.reason.note_invalid", path=path))]
    source = inp.t("validate.trace.source.note", path=path)
    out: list[Finding] = []
    if [str(x) for x in front.get("ai_use") or ()] != list(trace.get("ai_use") or ()):  # type: ignore[union-attr]
        out.append(_finding(inp, MISMATCH, severity, path=path, field="ai_use", source=source))
    assisted = sorted(f"{a.get('tool')}:{a.get('model')}" for a in trace.get("assisted_by") or ())
    if sorted(str(x) for x in front.get("assisted_by") or ()) != assisted:  # type: ignore[union-attr]
        out.append(_finding(inp, MISMATCH, severity, path=path, field="assisted_by", source=source))
    return out


def _meta_findings(inp: RuleInput, meta: Mapping[str, Any], task: int | None, check: TrailerCheck,
                   severity: Severity, trailers_source: str) -> list[Finding]:
    """03 §9.4 (REQ-SCHEMA-77 a 79 y 87) y el objeto ``trace`` (REQ-TRACE-39)."""
    from ...config.schemas import validate as schema_validate

    t = inp.t
    found = schema_validate("pr-meta", dict(meta), file="pr-meta", t=t, check=RULE_ID)
    bad_trace = [f for f in found if (f.pointer or "").startswith("/trace")]
    out = [_finding(inp, META, severity, reason=f.message) for f in found if not (f.pointer or "").startswith("/trace")]
    if task is not None and meta.get("task") != task:
        out.append(_finding(inp, META, severity, reason=t("validate.trace.reason.meta_task", task=task)))
    decisions = _decisions_added(inp)
    listed = meta.get("decisions")
    if isinstance(listed, list) and sorted({str(x) for x in listed}) != decisions:
        out.append(_finding(inp, META, severity, reason=t("validate.trace.reason.meta_decisions",
                                                          expected=", ".join(decisions) or "-")))
    sections = _sections_touched(inp)
    listed = meta.get("sections")
    if isinstance(listed, list) and sorted({str(x) for x in listed}) != sections:
        out.append(_finding(inp, META, severity, reason=t("validate.trace.reason.meta_sections",
                                                          expected=", ".join(sections) or "-")))
    trace = meta.get("trace")
    if trace is None:
        return [*out, _finding(inp, TRACE, severity, reason=t("validate.trace.reason.trace_missing"))]
    if bad_trace or not isinstance(trace, dict):
        return [*out, *(_finding(inp, TRACE, severity, reason=t("validate.trace.reason.trace_invalid", detail=f.message))
                        for f in bad_trace)]
    if "task" in trace and trace["task"] != meta.get("task"):
        out.append(_finding(inp, META, severity, reason=t("validate.trace.reason.meta_trace_task")))
    if "sections" in trace and sorted(trace["sections"]) != sorted(meta.get("sections") or ()):
        out.append(_finding(inp, META, severity, reason=t("validate.trace.reason.meta_trace_sections")))
    out += _same_as_trailers(inp, trace, check, severity, trailers_source)
    if task is not None:
        out += _same_as_note(inp, trace, task, severity)
    return out


def trace_findings(inp: RuleInput, text: str, *, context: TrailerContext, author: str, task: int | None,
                   severity: Severity, source: str, missing_block: Severity | None = None) -> list[Finding]:
    """El cuerpo del PR (``pr-body``) o el mensaje del commit de squash (``squash-commit``), REQ-TRACE-32.

    El commit de squash tiene asunto (el título del PR) y el cuerpo no. ``missing_block`` es la
    severidad de la falta del bloque ``pr-meta`` (en ``squash-commit``, aviso hasta S-10); por
    omisión, ``severity``.
    """
    assert inp.policy is not None
    squash = context == "squash-commit"
    check = validate_trailers(text, context, task=task, subject=squash)
    out = trailer_findings(inp, check, source, severity, task)
    if check.signed_off is not None and not signer_matches(check.signed_off, author, inp.policy.roles):
        out.append(_finding(inp, SIGNOFF, severity, source=source, login=author, found=check.signed_off))
    meta, problem = parse_pr_meta(text)
    if problem == "missing":
        return [*out, _finding(inp, META_MISSING, missing_block or severity)]
    if meta is None:
        return [*out, _finding(inp, META, severity, reason=inp.t("validate.trace.reason.meta_malformed"))]
    trailers_source = inp.t("validate.trace.source.commit_trailers" if squash else "validate.trace.source.trailers")
    return out + _meta_findings(inp, meta, task, check, severity, trailers_source)


def body_findings(inp: RuleInput, body: str, *, author: str, task: int | None, severity: Severity) -> list[Finding]:
    """El cuerpo del PR en el contexto ``pr-body`` (REQ-TRACE-32 y REQ-TRACE-40)."""
    return trace_findings(inp, body, context="pr-body", author=author, task=task, severity=severity,
                          source=inp.t("validate.trace.source.body"))


def problem(inp: RuleInput, reason: str) -> Finding:
    """Un ``E_VALIDATION_TRACE`` de error con el motivo ``validate.trace.reason.<reason>``."""
    return _finding(inp, TRACE, "error", reason=inp.t(f"validate.trace.reason.{reason}"))


def _run(inp: RuleInput) -> Sequence[Finding]:
    pr = inp.pr
    if pr is None or inp.policy is None:
        return [_finding(inp, TRACE, "error", reason=inp.t("validate.trace.reason.no_pr"))]
    task = task_number(pr.head_branch) if pr.same_repo else None
    severity: Severity = "error" if task is not None else "warning"  # 11 §2.10: PR que no sale de task/<n>
    return [*_commit_findings(inp, task), *body_findings(inp, pr.body or "", author=pr.author, task=task,
                                                        severity=severity)]


RULE = RuleSpec(id=RULE_ID, modes=frozenset({"pr"}), run=_run, codes=CODES)

RULES: tuple[RuleSpec, ...] = (RULE,)
