# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Regla ``scope``: control de alcance de un PR (09-ci §4.2; 06-permisos §2.4 y §2.5).

Por cada archivo del PR aplica ``check_change`` en modo ``ci`` con la política de la base
(REQ-CI-25), el modo de ``git ls-tree`` de la cabeza (REQ-CI-24) y
``generated_matches`` sacado del resultado previo de ``roles-sync`` y
``generated-sync``. Además falla (REQ-PERM-13..15):

- si el PR viene de un fork (sin checkout de la cabeza, el job no lee su árbol);
- si la API lista 3000 archivos o más (su tope: no se puede saber si faltan);
- si la cabeza no es ``task/<n>`` o no hay un Issue de tarea ``#<n>``;
- si la tarea no está reclamada y vigente (09 §4.2: ``proposed``, ``expired`` o
  ``cancelled`` son error) o si el autor del PR no es el reclamante;
- si alguien distinto del reclamante empujó a ``task/<n>`` (``E_CLAIM_NOT_HELD``); si la
  API de actividad no está disponible, la regla falla cerrada (REQ-CI-8).

Una ruta del PR con ``\\`` se deniega con ``E_PERM_OUTSIDE_REPO``: en git, ``\\`` no es un
separador sino parte del nombre (02 §3.1 y REQ-CONV-7; ver ``_split_git_invalid``).

Las denegaciones que no dependen de la ruta (sin tarea, reclamo, estado, persona fuera de
``people``) se informan una sola vez para el PR, no por archivo. Los hallazgos llevan los
códigos de ``perm_code`` (00-marco §5.11), más ``E_VALIDATION_TASK_BODY`` y los avisos
del cuerpo del Issue (03 §8.3).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Final

from ...config.base import to_policy
from ...domain.findings import Finding, Severity
from ...domain.permissions import Decision, Policy, Task, check_change
from ...domain.taskbody import BODY_CODE, NO_SECTIONS_CODE, UNKNOWN_FIELD_CODE
from ...domain.tasks import Derived
from ...domain.text import same_login
from ...forge.base import ForgeError
from ..snapshot import PUSH_TYPES, TaskEvidence, authz_from_roles, collect_task, task_number, to_permission_task
from .base import Change, RuleInput, RuleSpec, make_finding, unavailable

__all__ = ["CODES", "MAX_PR_FILES", "RULE", "RULES", "SCOPE_WRITABLE", "generated_matches", "run"]

RULE_ID: Final[str] = "scope"
MAX_PR_FILES: Final[int] = 3000  # tope de GET /pulls/{n}/files (REQ-PERM-15)
# Estados en los que el CI admite integrar cambios (09 §4.2: proposed, expired y cancelled
# son error). Es más estricto que WRITABLE de 06 §2.4, que también admite expired.
SCOPE_WRITABLE: Final[frozenset[str]] = frozenset({"claimed", "in_review", "changes_requested"})
# Anomalías de derive_state que significan que la tarea no está aceptada (04 §1.2, §1.5):
# con ref, derive_state sigue en el paso 4, pero para el alcance la tarea sigue propuesta.
_NOT_ACCEPTED: Final[frozenset[str]] = frozenset({"ref_on_proposed", "missing_acceptance"})
_TERMINAL: Final[frozenset[str]] = frozenset({"merged", "cancelled"})

_OUTSIDE: Final[str] = "E_PERM_OUTSIDE_REPO"
# Las rutas de scope salen de git y de la API de la forja: ahí '/' es el único separador y
# '\' es un carácter más del nombre (válido en Linux; git para Windows no puede extraerlo).
# canonical (02 §3.1) convierte '\' en '/' porque también recibe rutas del SO, así que aquí
# una '\' hace inválida la ruta (REQ-CONV-7) antes de llamar a check_change: si no,
# 'sections\m\x.tex', un archivo de la raíz (lead), se leería como de la parte m.
_GIT_NOT_SEP: Final[str] = "\\"
_NOT_ON_TASK: Final[str] = "E_STATE_NOT_ON_TASK_BRANCH"
_NOT_HELD: Final[str] = "E_CLAIM_NOT_HELD"
_BAD_STATE: Final[str] = "E_STATE_INVALID_TRANSITION"
_NOT_MEMBER: Final[str] = "E_PERM_NOT_A_MEMBER"
_INTERNAL: Final[str] = "E_INTERNAL_UNEXPECTED"
# Denegaciones de check_path que no dependen de la ruta: se informan una vez por PR.
_PR_LEVEL: Final[frozenset[str]] = frozenset({_NOT_ON_TASK, _NOT_HELD, _BAD_STATE, _NOT_MEMBER})

_FILE: Final[frozenset[str]] = frozenset({"file"})
# Código de hallazgo -> marcadores de finding.<code>.* (scope.toml).
CODES: Final[Mapping[str, frozenset[str]]] = {
    _OUTSIDE: _FILE,
    "E_PERM_SYMLINK": _FILE,
    "E_PERM_LOCAL_PATH": _FILE,
    "E_PERM_GENERATED_PATH": _FILE,
    "E_PERM_APPEND_ONLY": _FILE,
    "E_PERM_LEAD_ONLY_PATH": _FILE,
    "E_PERM_EDITOR_ONLY_PATH": _FILE,  # 00-marco D-56; check_path la emite desde H3
    "E_PERM_OUTSIDE_TASK": frozenset({"file", "task"}),
    "E_PERM_SECTION_NOT_OWNED": frozenset({"file", "section", "owner"}),
    _NOT_MEMBER: frozenset({"login"}),
    _NOT_ON_TASK: frozenset({"branch", "reason"}),
    _NOT_HELD: frozenset({"login", "task", "reason"}),
    _BAD_STATE: frozenset({"task", "state"}),
    BODY_CODE: frozenset({"task", "reason"}),
    UNKNOWN_FIELD_CODE: frozenset({"task"}),
    NO_SECTIONS_CODE: frozenset({"task"}),
}


def generated_matches(prior: Mapping[str, str]) -> bool:
    """Los archivos ``generated`` coinciden con la regeneración."""
    return prior.get("roles-sync") == "pass" and prior.get("generated-sync") == "pass"


def run(inp: RuleInput) -> list[Finding]:
    """Hallazgos de ``scope`` para el PR de ``inp`` (modo ``pr``)."""
    t = inp.t
    if inp.mode != "pr":
        return [unavailable(RULE, inp, t("scope.unavailable.mode"))]
    pr, snap = inp.pr, inp.policy
    if snap is None:
        return [unavailable(RULE, inp, t("scope.unavailable.policy"))]
    if pr is None:
        return [unavailable(RULE, inp, t("scope.unavailable.pr"))]
    tree = inp.tree
    if tree is None:  # sin checkout de la cabeza: PR desde un fork
        return [_finding(inp, _NOT_ON_TASK, branch=pr.head_branch, reason=t("scope.reason.fork"))]
    if tree.sha is None or tree.sha != pr.head_sha:
        return [unavailable(RULE, inp, t("scope.unavailable.head_mismatch", tree=tree.sha or "?", head=pr.head_sha))]
    if len(inp.changes) >= MAX_PR_FILES:
        return [unavailable(RULE, inp, t("scope.unavailable.too_many_files", limit=MAX_PR_FILES))]

    root = tree.root.as_posix() if tree.root is not None else "/"
    pol = to_policy(snap, root=root, ci_fs=False, case_map=None, link_probe=None)
    author = pr.author
    out: list[Finding] = []
    if author.lower() not in pol.people:
        out.append(_finding(inp, _NOT_MEMBER, login=author))

    task: Task | None = None
    number = task_number(pr.head_branch)
    if number is None:
        out.append(_finding(inp, _NOT_ON_TASK, branch=pr.head_branch, reason=t("scope.reason.not_task_branch")))
    else:
        if inp.forge is None:
            return [*out, unavailable(RULE, inp, t("scope.unavailable.forge"))]
        authz = authz_from_roles(snap.roles)
        try:
            evidence = collect_task(inp.forge, number, authz, base_branch=snap.project.base_branch)
        except ForgeError as exc:
            return [*out, unavailable(RULE, inp, t("scope.unavailable.forge_error", kind=exc.kind))]
        if evidence is None:
            out.append(
                _finding(inp, _NOT_ON_TASK, branch=pr.head_branch, reason=t("scope.reason.no_task", task=number))
            )
        else:
            task, task_findings = _task_checks(inp, evidence, author, pr.head_branch)
            out.extend(task_findings)

    out.extend(_file_checks(inp, pol, author, task, {f.code for f in out}))
    return out


def _task_checks(inp: RuleInput, ev: TaskEvidence, author: str, branch: str) -> tuple[Task, list[Finding]]:
    """Cuerpo del Issue, estado, reclamante y pushes de ``task/<n>``."""
    t = inp.t
    number = ev.snapshot.number
    out: list[Finding] = []
    for issue in ev.body.issues:
        if issue.code == BODY_CODE:
            reason = t(f"scope.body.{issue.reason}", field=issue.field or "?", line=issue.line or 0)
            out.append(_finding(inp, BODY_CODE, task=number, reason=reason))
        elif issue.code == UNKNOWN_FIELD_CODE:
            if not any(f.code == UNKNOWN_FIELD_CODE for f in out):  # uno por Issue
                out.append(_finding(inp, UNKNOWN_FIELD_CODE, severity="warning", task=number))
    if ev.body.spec is not None and not ev.body.sections:
        out.append(_finding(inp, NO_SECTIONS_CODE, severity="warning", task=number))

    task, derived = to_permission_task(ev.snapshot)
    state = _scope_state(derived)
    task = replace(task, state=state)
    if state not in SCOPE_WRITABLE:
        out.append(_finding(inp, _BAD_STATE, task=number, state=state))
    # El autor y los pushes se comprueban también con un estado que no admite integrar:
    # son fallas independientes (REQ-PERM-13).
    owner = task.claimant
    if owner is None or not same_login(author, owner):
        reason = (
            t("scope.claim.author", claimant=owner) if owner is not None else t("scope.claim.author_unclaimed")
        )
        out.append(_finding(inp, _NOT_HELD, login=author, task=number, reason=reason))
    if owner is None:
        return task, out
    if ev.activity is None:  # la forja no da la actividad de la rama: falla cerrada (REQ-CI-8)
        out.append(unavailable(RULE, inp, t("scope.unavailable.activity", branch=branch)))
        return task, out
    seen: set[str] = set()
    for event in ev.activity:
        if event.activity_type not in PUSH_TYPES or same_login(event.actor, owner):
            continue
        actor = event.actor or t("scope.claim.unknown_actor")
        if actor.lower() in seen:
            continue
        seen.add(actor.lower())
        reason = t("scope.claim.push", branch=branch, claimant=owner)
        out.append(_finding(inp, _NOT_HELD, login=actor, task=number, reason=reason))
    return task, out


def _scope_state(derived: Derived) -> str:
    """Estado para el alcance: una tarea con ref pero sin aceptación válida sigue ``proposed``."""
    if derived.state not in _TERMINAL and _NOT_ACCEPTED & set(derived.anomalies):
        return "proposed"
    return derived.state


def _file_checks(
    inp: RuleInput, pol: Policy, author: str, task: Task | None, reported: set[str]
) -> list[Finding]:
    """``check_change`` por archivo, con los modos de ``git ls-tree`` de la cabeza."""
    t = inp.t
    tree = inp.tree
    assert tree is not None
    entries = tree.entries()
    matches = generated_matches(inp.prior)
    out: list[Finding] = []
    for change in inp.changes:
        invalid, rest = _split_git_invalid(change)
        out.extend(_finding(inp, _OUTSIDE, path=None, file=raw) for raw in invalid)
        if rest is None:
            continue
        ch = rest
        mode: str | None = None
        if ch.status != "removed":
            mode = entries.get(ch.path)  # REQ-CI-24: el modo sale del árbol, no de la API
            if mode is None:
                out.append(unavailable(RULE, inp, t("scope.unavailable.missing_mode", file=ch.path), path=_path(ch.path)))
                continue
        decisions = check_change(
            author,
            ch.status,
            ch.path,
            ch.previous_path,
            task,
            pol=pol,
            mode="ci",
            file_mode=mode,
            generated_matches=matches,
        )
        for d in decisions:
            if d.allow:
                continue
            finding = _denial(inp, d, ch, task, author, reported)
            if finding is not None:
                out.append(finding)
    return out


def _split_git_invalid(ch: Change) -> tuple[list[str], Change | None]:
    """Rutas de ``ch`` con ``\\`` (inválidas en git) y el cambio que queda por evaluar.

    Sin rutas inválidas, el cambio sigue igual. En un renombre con un solo lado inválido, el
    otro lado se sigue evaluando como ``check_change`` lo haría (06 §2.4: renombrar es
    delete del origen sin modo + create del destino con su modo).
    """
    renamed = ch.status == "renamed" and ch.previous_path is not None
    sides = [ch.previous_path, ch.path] if renamed else [ch.path]
    invalid = [p for p in sides if p is not None and _GIT_NOT_SEP in p]
    if not invalid:
        return [], ch
    if not renamed or len(invalid) == len(sides):
        return invalid, None
    if _GIT_NOT_SEP in ch.path:
        assert ch.previous_path is not None
        return invalid, Change(path=ch.previous_path, status="removed")
    return invalid, Change(path=ch.path, status="added", file_mode=ch.file_mode)


def _denial(
    inp: RuleInput, d: Decision, ch: Change, task: Task | None, author: str, reported: set[str]
) -> Finding | None:
    """Hallazgo de una denegación; ``None`` si ya se informó para todo el PR."""
    t = inp.t
    code = d.code or _INTERNAL
    shown = d.path if d.path else ch.path
    path = _path(shown)
    if code in _PR_LEVEL:
        if code in reported:
            return None
        reported.add(code)
        return _pr_level_fallback(inp, code, author, task, path)
    if code not in CODES:  # E_INTERNAL_UNEXPECTED u otro código fuera del contrato
        return unavailable(RULE, inp, t("scope.unavailable.check_failed", file=shown), path=path)
    params: dict[str, object] = {"file": shown}
    if code == "E_PERM_OUTSIDE_TASK":
        params["task"] = task.number if task is not None else "?"
    elif code == "E_PERM_SECTION_NOT_OWNED":
        params["section"] = d.section or "?"
        params["owner"] = d.owner or "?"
    return _finding(inp, code, severity="error", path=path, **params)


def _pr_level_fallback(inp: RuleInput, code: str, author: str, task: Task | None, path: str | None) -> Finding:
    """Denegación del PR que no cubrió ninguna comprobación previa (falla cerrada)."""
    t = inp.t
    number: object = task.number if task is not None else "?"
    branch = inp.pr.head_branch if inp.pr is not None else "?"
    if code == _NOT_MEMBER:
        return _finding(inp, code, path=path, login=author)
    if code == _NOT_ON_TASK:
        return _finding(inp, code, path=path, branch=branch, reason=t("scope.reason.not_task_branch"))
    if code == _BAD_STATE:
        return _finding(inp, code, path=path, task=number, state=task.state if task is not None else "?")
    claimant = task.claimant if task is not None else None
    reason = t("scope.claim.author", claimant=claimant) if claimant else t("scope.claim.author_unclaimed")
    return _finding(inp, code, path=path, login=author, task=number, reason=reason)


def _finding(
    inp: RuleInput,
    code: str,
    *,
    severity: Severity = "error",
    path: str | None = None,
    **params: object,
) -> Finding:
    return make_finding(RULE, inp, code, severity=severity, path=path, line=None, pointer=None, **params)


def _path(p: str | None) -> str | None:
    """Ruta válida para ``findings[].path`` (repo_path), o ``None``."""
    if not p or p.startswith("/") or "\\" in p:
        return None
    return p


RULE: Final[RuleSpec] = RuleSpec(
    id=RULE_ID,
    modes=frozenset({"pr"}),
    run=run,
    codes=CODES,
    needs_forge=True,
    after=("roles-sync", "generated-sync"),
)
RULES: tuple[RuleSpec, ...] = (RULE,)
