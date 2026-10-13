# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Subcomando interno ``ci audit`` (09-ci §7.1, REQ-CI-46; 06-permisos §3.5 y REQ-PERM-23).

Lo llama el job ``audit`` (``aucove/base-audit``) de ``main-broken.yml`` en cada push a la
base, con ``--before``, ``--after`` y ``--forced`` del evento. El directorio actual es el checkout
de la base con la historia completa (``fetch-depth: 0``): a diferencia de los jobs de un PR, ahí no
hay árbol ajeno, así que se usa su git sin ``--policy`` (``CiContext`` no resuelve proyecto). La
política leída queda como la de la invocación (``OpContext.use_policy``): de ella salen la forja y
el repositorio. Clasifica cómo entró cada commit (``merge_audit`` de 06 §3.5):

- La política es la de ``--before``: ``project.yaml`` y ``roles.yaml`` de ese commit, con el
  respaldo de REQ-PERM-4 (``config.base.read_policy_at``).
- ``--forced true``: ``force_push``, una vez por push (``before`` ya no es ancestro de ``after``
  y sus commits pueden no estar en el checkout). Si ``before`` falta, la política sale de
  ``after``: para la marca de un ``force_push`` solo da la base, la mención y el idioma.
- Si no, cada commit de primer padre de ``before..after`` (hasta ``MAX_COMMITS``) con su PR
  (``pulls_for_commit``). Sin un PR integrado en la base, ``direct_push``. Con él:
  ``evaluate_approvals`` (06 §3.2) con la cabeza al integrar y las revisiones anteriores a la
  integración, y el alcance recalculado: los archivos del PR contra la política, con la tarea de
  ``task/<n>`` como estaba en revisión y los modos del commit integrado (``check_change`` en modo
  ``ci``, como la regla ``scope``). Todo en orden: ``compliant``; ``bypass_no_deputy`` si el PR es
  del ``lead``, sin suplente, y solo falta la aprobación que nadie podía dar (06 §3.4); si no,
  ``bypass``. Con ``mode: individual`` no hay aprobaciones: es ``compliant`` con el alcance en
  regla y los checks requeridos en verde en la cabeza (REQ-PERM-59). Los checks en rojo se
  informan también en un bypass (REQ-PERM-22).

Todo lo que no es ``compliant`` se marca con ``flag:bypass`` (REQ-PERM-23): en el PR, con un
comentario que lleva el marcador ``<!-- aucove:ci:audit v1 -->`` y menciona al ``lead`` y al
``deputy``; sin PR, en un Issue nuevo con la etiqueta, el marcador y la mención. Las
escrituras llevan el marcador ``op`` del commit: un run repetido no duplica nada (08 §4.7).

En el mismo job, el commit de squash de cada PR integrado se valida en el contexto
``squash-commit`` (REQ-TRACE-41; 11 §2.10): lo de ``pr-body`` sobre el mensaje del commit en la
base, con el árbol integrado (la nota de sesión) y los archivos del PR, y la falta del bloque
``pr-meta`` como aviso hasta que S-10 lo confirme. Ya no puede bloquear: las diferencias se
informan como anotaciones del run y en el resumen.

Salida: una línea ``merge_audit=<valor> sha=<sha> pr=<n|->`` por commit en stdout y, después, las
anotaciones ``::error``/``::warning`` de la traza (como ``validate --format github``); el resumen
del job con ``--summary``. 0 si auditó todo (con o sin bypass: el check es informativo), 1 si no
pudo (sin política legible o con un error de la forja) y 3 en un error de uso; nunca 2.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TextIO

from .. import _meta
from ..domain.checks import required_checks
from ..domain.text import same_login
from ..forge.base import NULL_SHA, ForgeError
from ..i18n import Translator, resolve_lang, translator
from .ci_comment import append_summary
from .ci_plan import EXIT_ERROR, EXIT_OK
from .registry import CiCommand
from .result import OpError

if TYPE_CHECKING:
    import argparse

    from ..config.base import BaseSnapshot
    from ..domain.findings import Finding
    from ..forge.base import ChangedFile, ForgeAdapter, PullRequest
    from ..vcs.git import Git
    from .context import OpContext

__all__ = [
    "COMMANDS", "LABEL", "MARKER_KIND", "MAX_COMMITS", "Audit", "MergeAudit", "audit_commit", "main", "squash_findings",
]

MergeAudit = Literal["compliant", "bypass_no_deputy", "bypass", "direct_push", "force_push"]
LABEL = "flag:bypass"  # 00-marco §5.4
MARKER_KIND = "audit"  # <!-- aucove:ci:audit v1 --> (REQ-CI-46)
MAX_COMMITS = 100  # commits de un push que se auditan uno por uno
_SHA = re.compile(r"[0-9a-f]{40}")
_NOT_ON_TASK = "E_STATE_NOT_ON_TASK_BRANCH"
_NOT_HELD = "E_CLAIM_NOT_HELD"


@dataclass(frozen=True)
class Audit:
    """La clasificación de un commit de la base (06 §3.5) y lo que la explica."""

    sha: str
    merge_audit: MergeAudit
    pr: int | None = None
    author: str | None = None
    missing: tuple[str, ...] = ()  # quiénes podían dar las aprobaciones que faltaron
    scope: tuple[str, ...] = ()  # códigos de las denegaciones del alcance recalculado
    checks_red: tuple[str, ...] = ()  # checks requeridos sin éxito en la cabeza (REQ-PERM-22)
    trace: tuple[Finding, ...] = ()  # el commit de squash en el contexto squash-commit (REQ-TRACE-41)


def _say(text: str) -> None:
    try:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()
    except (OSError, UnicodeError, ValueError):
        pass


def _op_marker(op_id: str) -> str:
    """Marcador ``op`` de las escrituras de la forja (08 §4.7)."""
    return f"<!-- {_meta.NAME}:op v1 id={op_id} -->"


# -- la clasificación -------------------------------------------------------------------------


def _generated_match(git: Git, root: Path, sha: str) -> bool:
    """Los archivos ``generated`` del commit coinciden con su regeneración."""
    from ..vcs.tree import git_tree
    from .rules.roles_sync import compare_files, regenerate, tree_reader

    read, exists = tree_reader(git_tree(root, sha, git=git))
    regen = regenerate(read)
    if regen.expected is None:
        return False
    return all(s.status == "same" for s in compare_files(read, regen.expected, exists=exists, normalize_eol=True))


def scope_denials(forge: ForgeAdapter, git: Git, root: Path, snap: BaseSnapshot, pull: PullRequest,
                  files: Sequence[ChangedFile], sha: str) -> tuple[str, ...]:
    """El alcance del PR integrado, recalculado con la política de la base (REQ-CI-46).

    Como la regla ``scope`` (09 §4.2), salvo que la tarea ya está integrada: se evalúa como
    estaba en revisión, con su reclamante y sus secciones efectivas, y los modos salen del commit
    integrado ``sha``.
    """
    from ..config.base import to_policy
    from ..domain.permissions import check_change
    from ..vcs.tree import git_tree
    from .rules.scope import MAX_PR_FILES
    from .snapshot import authz_from_roles, collect_task, task_number, to_permission_task

    if len(files) >= MAX_PR_FILES:  # REQ-PERM-15: no se puede saber si faltan archivos
        return ("too_many_files",)
    number = task_number(pull.head_branch)
    if number is None or not pull.same_repo:
        return (_NOT_ON_TASK,)
    evidence = collect_task(forge, number, authz_from_roles(snap.roles), base_branch=snap.project.base_branch)
    if evidence is None:
        return (_NOT_ON_TASK,)
    task, _derived = to_permission_task(evidence.snapshot)
    if task.claimant is None or not same_login(task.claimant, pull.author):
        return (_NOT_HELD,)
    task = replace(task, state="in_review")  # el estado al integrar; después es merged (04 §1.5)
    pol = to_policy(snap, root=root.as_posix(), ci_fs=False, case_map=None, link_probe=None)
    modes = git_tree(root, sha, git=git).entries()
    matches = _generated_match(git, root, sha)
    out: set[str] = set()
    for f in files:
        mode = None if f.status == "removed" else modes.get(f.path)
        if f.status != "removed" and mode is None:
            out.add("missing_mode")
            continue
        for d in check_change(pull.author, f.status, f.path, f.previous_path, task, pol=pol, mode="ci",
                              file_mode=mode, generated_matches=matches):
            if not d.allow:
                out.add(d.code or "E_INTERNAL_UNEXPECTED")
    return tuple(sorted(out))


def _red_checks(forge: ForgeAdapter, head_sha: str, snap: BaseSnapshot) -> tuple[str, ...]:
    """Los checks requeridos (06 REQ-PERM-34) que no terminaron en éxito en la cabeza."""
    green = {r.name for r in forge.get_checks(head_sha).runs if r.conclusion == "success"}
    return tuple(c for c in required_checks(snap.project.format) if c not in green)


def squash_findings(git: Git, root: Path, snap: BaseSnapshot, sha: str, pull: PullRequest,
                    files: Sequence[ChangedFile], t: Translator, now: datetime) -> tuple[Finding, ...]:
    """REQ-TRACE-41: el commit de squash ``sha`` del PR ``pull`` en el contexto ``squash-commit``.

    Lo de ``pr-body`` sobre el mensaje del commit, con el árbol integrado (la nota de sesión) y los
    archivos del PR; la falta del bloque ``pr-meta`` es aviso hasta S-10, y en un PR que no sale de
    ``task/<n>`` todo es aviso (11 §2.10). Si git no puede leer el commit, un ``E_VALIDATION_TRACE``.
    """
    from ..vcs.git import GitError
    from ..vcs.tree import git_tree
    from .rules.base import RuleInput
    from .rules.traceability import problem, trace_findings
    from .snapshot import task_number
    from .validate import changes_from_api

    inp = RuleInput(mode="base", t=t, now=now, policy=snap)
    try:
        found = git.cat_file_objects([sha]).get(sha)
        if found is None:
            return (problem(inp, "git"),)
        parts = found[1].split(b"\n\n", 1)
        message = parts[1].decode("utf-8", "replace") if len(parts) == 2 else ""
        tree = git_tree(root, sha, git=git)
        changes = changes_from_api(files, tree.entries())
    except (GitError, LookupError, OSError, ValueError):
        return (problem(inp, "git"),)
    inp = replace(inp, tree=tree, changes=changes)
    task = task_number(pull.head_branch) if pull.same_repo else None
    return tuple(trace_findings(inp, message, context="squash-commit", author=pull.author, task=task,
                                severity="error" if task is not None else "warning",
                                source=t("validate.trace.source.squash", sha=sha[:12]), missing_block="warning"))


def audit_pull(forge: ForgeAdapter, git: Git, root: Path, snap: BaseSnapshot, sha: str, pull: PullRequest, *,
               t: Translator | None = None, now: datetime | None = None) -> Audit:
    """``merge_audit`` del PR integrado ``pull``, cuyo commit en la base es ``sha`` (06 §3.5).

    Con ``t`` y ``now``, además, la traza del commit de squash (``squash_findings``).
    """
    from .reviews import approval_at_merge

    files = forge.list_pull_request_files(pull.number)
    merge = approval_at_merge(snap, pull, files, forge.list_reviews(pull.number))
    approval = merge.approval
    scope = scope_denials(forge, git, root, snap, pull, files, sha)
    red = _red_checks(forge, pull.head_sha, snap)
    verdict: MergeAudit
    if snap.project.mode == "individual":  # REQ-PERM-59: sin aprobaciones, con los checks requeridos en verde
        verdict = "compliant" if not scope and not red else "bypass"
    elif approval.ok and not scope:
        verdict = "compliant"
    elif not scope and merge.no_deputy:
        verdict = "bypass_no_deputy"  # 06 §3.4: nadie podía aprobar el PR del lead
    else:
        verdict = "bypass"
    trace = squash_findings(git, root, snap, sha, pull, files, t, now) if t is not None and now is not None else ()
    return Audit(sha, verdict, pull.number, pull.author, approval.required_from, scope, red, trace)


def audit_commit(forge: ForgeAdapter, git: Git, root: Path, snap: BaseSnapshot, sha: str, *,
                 t: Translator | None = None, now: datetime | None = None) -> Audit:
    """``merge_audit`` de un commit de la base: el de su PR integrado o ``direct_push``."""
    base = snap.project.base_branch
    merged = [p for p in forge.pulls_for_commit(sha) if p.merged and p.base_branch == base]
    if not merged:
        return Audit(sha, "direct_push")
    pull = forge.get_pull_request(merged[0].number) or merged[0]  # merged_at y la cabeza completos
    return audit_pull(forge, git, root, snap, sha, pull, t=t, now=now)


# -- las marcas (REQ-PERM-23) -------------------------------------------------------------------


def _mentions(snap: BaseSnapshot) -> str:
    return " ".join(f"@{x}" for x in (snap.roles.lead, snap.roles.deputy) if x)


def render_body(t: Translator, a: Audit, snap: BaseSnapshot) -> str:
    """El comentario del PR o el cuerpo del Issue: el marcador, la clasificación y la mención."""
    lines = [
        _meta.ci_marker(MARKER_KIND),
        t(f"ci.audit.headline.{a.merge_audit}", sha=a.sha[:12], pr=a.pr or "", base=snap.project.base_branch),
    ]
    if a.missing:
        lines.append("- " + t("ci.audit.missing", logins=", ".join(f"@{x}" for x in a.missing)))
    if a.scope:
        lines.append("- " + t("ci.audit.scope", codes=", ".join(f"`{c}`" for c in a.scope)))
    if a.checks_red:
        lines.append("- " + t("ci.audit.checks_red", checks=", ".join(f"`{c}`" for c in a.checks_red)))
    lines.append(t("ci.audit.mention", mentions=_mentions(snap)) if _mentions(snap) else "")
    return "\n\n".join(line for line in lines if line) + "\n"


def mark(forge: ForgeAdapter, t: Translator, a: Audit, snap: BaseSnapshot, me: str) -> str:
    """Marca un ``merge_audit`` distinto de ``compliant``: ``labeled``, ``opened`` o ``already``.

    El marcador ``op`` es del commit y de su clasificación: un run repetido del mismo push no
    duplica la marca, y un push forzado que vuelve a dejar un commit ya marcado sí deja la suya.
    """
    op_id = f"audit-{a.merge_audit}-{a.sha}"
    marker = _op_marker(op_id)
    body = render_body(t, a, snap)
    if a.pr is not None:
        forge.add_labels(a.pr, [LABEL])
        if any(same_login(c.author, me) and marker in c.body for c in forge.list_comments(a.pr)):
            return "already"
        forge.create_comment(a.pr, body, op_id=op_id)
        return "labeled"
    found = forge.list_issues(labels=[LABEL], state="all")
    if any(same_login(i.author, me) and marker in i.body for i in found):
        return "already"
    title = t(f"ci.audit.title.{a.merge_audit}", sha=a.sha[:12], base=snap.project.base_branch)
    forge.create_issue(title, body, labels=[LABEL], op_id=op_id)
    return "opened"


def render_summary(t: Translator, audits: Sequence[Audit], snap: BaseSnapshot) -> str:
    """El resumen del job (Markdown): una línea por commit."""
    lines = [f"### {_meta.NAME}/base-audit", ""]
    if not audits:
        lines.append(t("ci.audit.nothing"))
    for a in audits:
        pr = f" (#{a.pr})" if a.pr is not None else ""
        line = f"- `{a.sha[:12]}`{pr}: `{a.merge_audit}`"
        if a.pr is not None:
            errors = sum(1 for f in a.trace if f.severity == "error")
            line += " · " + (t("ci.audit.trace_counts", errors=errors, warnings=len(a.trace) - errors) if a.trace
                             else t("ci.audit.trace_ok"))
        lines.append(line)
    return "\n".join(lines) + "\n"


# -- el subcomando ------------------------------------------------------------------------------


def _policy_ref(git: Git, args: argparse.Namespace) -> str:
    """El commit del que sale la política: ``--before`` (REQ-CI-46).

    Salvo en un push forzado cuyo ``before`` ya no está en el checkout: ninguna rama lo alcanza
    y ``fetch-depth: 0`` trae solo las ramas. Entonces ``--after``.
    """
    if args.forced and git.rev_parse(f"{args.before}^{{commit}}") is None:
        return str(args.after)
    return str(args.before)


def run(args: argparse.Namespace, ctx: OpContext, out: TextIO) -> int:
    """``ci audit``: clasifica el push ``--before..--after`` y marca lo que no es ``compliant``."""
    from ..config.base import read_policy_at
    from ..vcs.git import Git, GitError, GitUnavailable

    env = ctx.inv.env
    plain = translator(resolve_lang(cli=None, env=env, project=None))
    if args.before == NULL_SHA:  # rama nueva: no hay de dónde leer la política ni qué comparar
        _say(plain("ci.audit.new_branch"))
        return EXIT_OK
    ref = args.before
    try:
        git = Git(Path(ctx.inv.cwd), env=env)
        root = Path(git.run("rev-parse", "--show-toplevel").stdout.decode("utf-8", "replace").strip())
        ref = _policy_ref(git, args)
        snap = read_policy_at(git, ref)
    except (GitUnavailable, GitError):
        _say(plain("ci.audit.no_repo"))
        return EXIT_ERROR
    except OpError as exc:
        _say(plain("ci.audit.policy_unavailable", sha=ref[:12], code=exc.code))
        return EXIT_ERROR
    ctx.use_policy(snap)
    t = translator(resolve_lang(cli=None, env=env, project=snap.project.language))  # mensajes (REQ-ARCH-69)
    try:
        forge = ctx.forge()
        me = forge.identity().login
        if args.forced:
            audits = [Audit(args.after, "force_push")]
        else:
            shas = list(reversed(git.first_parent_chain(args.after, max_count=MAX_COMMITS, exclude=args.before)))
            audits = [audit_commit(forge, git, root, snap, sha, t=t, now=ctx.inv.now()) for sha in shas]
        for a in audits:
            if a.merge_audit != "compliant":
                _say(t("ci.audit.marked", sha=a.sha[:12], verdict=a.merge_audit,
                       action=mark(forge, t, a, snap, me)))
    except (OpError, ForgeError, GitError) as exc:
        code = exc.code if isinstance(exc, OpError) else getattr(exc, "kind", type(exc).__name__)
        _say(t("ci.audit.failed", code=code))
        return EXIT_ERROR
    for a in audits:
        out.write(f"merge_audit={a.merge_audit} sha={a.sha} pr={a.pr if a.pr is not None else '-'}\n")
    for line in _annotations(audits):
        out.write(line + "\n")
    out.flush()
    if args.summary and not append_summary(Path(args.summary), render_summary(t, audits, snap)):
        _say(t("ci.audit.summary_failed", path=str(args.summary)))
    return EXIT_OK


def _annotations(audits: Sequence[Audit]) -> list[str]:
    """Las faltas de la traza como anotaciones del run (REQ-TRACE-41), sin el tope de 50 de validate."""
    from .validate import github_annotations

    findings = [f.to_dict() for a in audits for f in a.trace]
    lines, _omitted = github_annotations([{"check": "traceability", "findings": findings}], limit=len(findings))
    return lines


def _sha(value: str) -> str:
    import argparse

    if not _SHA.fullmatch(value):
        raise argparse.ArgumentTypeError(value)
    return value


def _forced(value: str) -> bool:
    import argparse

    if value not in ("true", "false"):  # github.event.forced
        raise argparse.ArgumentTypeError(value)
    return value == "true"


def _add_arguments(p: argparse.ArgumentParser) -> None:
    p.description = "ci.command.audit"
    p.add_argument("--before", required=True, type=_sha, metavar="SHA", help="ci.audit.arg_before")
    p.add_argument("--after", required=True, type=_sha, metavar="SHA", help="ci.audit.arg_after")
    p.add_argument("--forced", required=True, type=_forced, metavar="true|false", help="ci.audit.arg_forced")
    p.add_argument("--summary", default="", metavar="FILE", help="ci.audit.arg_summary")


def main(args: argparse.Namespace, ctx: OpContext, out: TextIO) -> int:
    """``ci audit``: 0, 1 o 3; nunca 2."""
    return run(args, ctx, out)


COMMANDS: tuple[CiCommand, ...] = (CiCommand("audit", _add_arguments, main),)
