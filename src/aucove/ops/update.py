# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove update`` · ``update_branch`` (05 §9.15; algoritmo de 04-tareas §6).

Pasos, en el orden de 05 §9.15:

1. ``save_local``: si hay cambios sin guardar, hace el commit de ``save`` (alcance y trailers
   de 11 §2.5, con un mensaje del núcleo); lo sube el ``push`` de esta operación.
2. ``fetch``: la base con la comprobación de 10-seguridad REQ-SEC-74 y ``task/<n>`` con la de
   REQ-TASK-32. Sin red sigue con la ``origin/<base>`` local, no sube y avisa
   (``W_NET_OFFLINE``). Con un Issue ``main-broken`` abierto avisa ``W_STATE_MAIN_BROKEN``.
3. ``integrate_remote_task``: avance rápido desde ``origin/task/<n>``, o fusión si divergió.
4. ``merge_base`` (canónico): ``git merge`` de ``origin/<base>`` (REQ-TASK-47: nunca rebase
   ni ``--force``), con la clasificación de 04 §6.2. Si quedan conflictos ``agent``, la
   fusión queda en curso y falla con ``E_STATE_MERGE_CONFLICT`` (``resolvable:true``,
   ``result:"conflict"`` y el resto de ``data_update``).
5. ``push``: si la local va por delante del remoto.
6. ``unblock`` (reflejo): si una escalación propia dejó ``flag:blocked`` y la última marca es
   del actor, la quita (REQ-TASK-51).

``--continue`` exige que no queden marcas de conflicto, hace el commit de fusión y sigue con
``push`` y ``unblock``. ``--abort`` ejecuta ``git merge --abort`` (sin fusión en curso, termina
igual con ``ok=true``); con ``--escalate <motivo>``, el paso ``escalate`` agrega
``flag:blocked`` y después publica el comentario con el marcador ``escalation`` que menciona
al ``lead`` (REQ-TASK-50): en ese orden, porque la etiqueta solo vale con un marcador
posterior (04 §1.3).
"""

from __future__ import annotations

import hashlib
from datetime import timezone
from typing import TYPE_CHECKING

from ..domain.markers import replace_marker
from ..domain.paths import classify
from ..domain.text import same_login
from ..forge.base import ForgeError
from ..i18n import LANGS, translator
from .board import MAIN_BROKEN
from .gitwork import (
    MergeOutcome,
    check_claim,
    conflict_markers,
    counts,
    fetch_task,
    guard_remote,
    merge,
    unmerged_paths,
)
from .fit import fit
from .result import OpError, Result, WarningObj, make_warning, ok
from .save import (
    Work,
    auto_message,
    changes,
    commit_trace,
    commit_work,
    conflict_error,
    dropped_warning,
    integrate,
    merge_message,
    push,
    resolve_work,
    scope_check,
    sync_remote,
    trace_warnings,
)
from .steps import Steps
from .workspace import merge_in_progress

if TYPE_CHECKING:
    from ..config.base import BaseSnapshot
    from ..i18n import Translator
    from ..vcs.git import Git
    from .context import OpContext

__all__ = ["OP", "run"]

OP = "update"
STEPS = ("save_local", "fetch", "integrate_remote_task", "merge_base", "push", "unblock")
CONTINUE_STEPS = ("merge_base", "push", "unblock")
ABORT_STEPS = ("abort", "escalate")
ESCALATE_MAX = 500
BUDGET = 4000  # 05 §7.1, «Resto»
BLOCKED = "flag:blocked"
_FILES_MAX = 50
_SUSPECTS_MAX = 20


def _bad(param: str, reason: str = "type") -> OpError:
    return OpError("E_VALIDATION_INPUT", details={"errors": [{"param": param, "reason": reason}]})


def _check_input(cont: object, abort: object, escalate: object) -> tuple[bool, bool, str | None]:
    if not isinstance(cont, bool):
        raise _bad("continue")
    if not isinstance(abort, bool):
        raise _bad("abort")
    if cont and abort:
        raise _bad("abort", "exclusive")
    if escalate is not None:
        if not isinstance(escalate, str) or not escalate.strip():
            raise _bad("escalate", "minLength" if isinstance(escalate, str) else "type")
        if len(escalate) > ESCALATE_MAX:
            raise _bad("escalate", "maxLength")
        if not abort:
            raise _bad("escalate", "requires_abort")
    return cont, abort, escalate.strip() if isinstance(escalate, str) else None


def _data(work: Work, result: str, *, integrated: bool = False, behind: int = 0, commit: str | None = None,
          outcome: MergeOutcome | None = None, pushed: bool = False) -> dict[str, object]:
    conflicts = [c.to_dict() for c in outcome.conflicts] if outcome is not None else []
    return {
        "task": work.number,
        "result": result,
        "remote_task_integrated": integrated,
        "behind_before": behind,
        "merge_commit": commit,
        "conflicts": conflicts,
        "resolvable": True if result == "conflict" else None,
        "pushed": pushed,
    }


def _escalation_key(store_repo: str) -> dict[str, object]:
    return {"repo": store_repo}


def _unblock(ctx: OpContext, work: Work, snap: BaseSnapshot, steps: Steps, *, offline: bool) -> None:
    """Paso ``unblock`` (REQ-TASK-51): quita ``flag:blocked`` si la puso una escalación propia."""
    store = ctx.store()
    rel = f"escalations/{work.number}.json"
    if store.read(rel, key=_escalation_key(snap.project.forge_repo)) is None:
        steps.skip("unblock")
        return
    if offline:
        steps.skip("unblock", "offline")
        return

    def go() -> bool:
        forge = ctx.forge()
        issue = forge.get_issue(work.number)
        if issue is None or not any(x.lower() == BLOCKED for x in issue.labels):
            return True
        labeled = [e for e in forge.list_issue_events(work.number) if e.label == BLOCKED and e.event == "labeled"]
        if labeled and same_login(labeled[-1].actor, work.me):  # si la puso el lead, la quita el lead
            forge.remove_label(work.number, BLOCKED)
        return True

    if steps.reflect("unblock", go):
        store.remove(rel)


def _main_broken(ctx: OpContext) -> bool:
    try:
        return bool(ctx.forge().list_issues(labels=[MAIN_BROKEN], state="open"))
    except (ForgeError, OpError):
        return False  # el aviso es informativo: sin forja no se da


def _save_local(ctx: OpContext, git: Git, snap: BaseSnapshot, work: Work, t: Translator,
                warnings: list[WarningObj]) -> str | None:
    found = scope_check(ctx, git, snap, work, [])
    trace = commit_trace(ctx, ai_use=None, ai_tool=None, ai_model=None)
    warnings.extend(trace_warnings(trace, t))
    done = commit_work(git, snap, work, found, [], message=auto_message(snap, t, "update"), trace=trace)
    return done.sha if done is not None else None


def _merge_base(ctx: OpContext, git: Git, snap: BaseSnapshot, work: Work, t: Translator,
                warnings: list[WarningObj], *, integrated: bool, behind: int) -> MergeOutcome:
    project = ctx.project()
    assert project is not None
    base = snap.project.base_branch
    source = f"origin/{base}"
    outcome = merge(git, f"refs/remotes/{source}", message=merge_message(source, work.local, work.number),
                    scope=work.scope(snap), root=project.root)
    dropped = dropped_warning(ctx, work, outcome, t)
    if dropped is not None:
        warnings.append(dropped)
    if outcome.status == "conflict":
        raise conflict_error(outcome, _data(work, "conflict", integrated=integrated, behind=behind, outcome=outcome))
    return outcome


def _escalate(ctx: OpContext, git: Git, snap: BaseSnapshot, work: Work, reason: str, files: list[str]) -> None:
    """Paso ``escalate`` (REQ-TASK-50): ``flag:blocked`` y después el comentario ``escalation``."""
    forge = ctx.forge()
    tracking = f"refs/remotes/origin/{snap.project.base_branch}"
    base_sha = git.rev_parse(tracking)
    suspects: list[str] = []
    if base_sha is not None:
        mb = git.merge_base("HEAD", tracking)
        rng = f"{mb}..{tracking}" if mb else tracking
        proc = git.run("--literal-pathspecs", "log", f"--max-count={_SUSPECTS_MAX}", "--format=%H", rng, "--",
                       *files, check=False)
        suspects = proc.stdout.decode("ascii", "replace").split() if proc.returncode == 0 else []
    forge.add_labels(work.number, [BLOCKED])
    lead = snap.roles.lead
    cfg = snap.project
    lang = translator(cfg.language) if cfg.language in LANGS else ctx.t()
    data = {"task": work.number, "reason": reason, "files": files[:_FILES_MAX], "base_sha": base_sha,
            "suspect_commits": suspects}
    visible = lang("tasks.escalation.comment", task=work.number, lead=lead, reason=reason)
    key = hashlib.sha256(f"{reason}\n{base_sha}".encode()).hexdigest()[:16]
    forge.create_comment(work.number, replace_marker(visible, "escalation", data), op_id=f"escalate-{work.number}-{key}")
    now = ctx.inv.now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ctx.store().write(f"escalations/{work.number}.json", {"task": work.number, "escalated_at": now},
                      key=_escalation_key(cfg.forge_repo))


def _abort(ctx: OpContext, git: Git, snap: BaseSnapshot, work: Work, reason: str | None,
           warnings: list[WarningObj]) -> Result:
    steps = Steps(ctx, OP, ABORT_STEPS)
    t = ctx.t()
    in_merge = merge_in_progress(ctx)
    files = sorted(unmerged_paths(git)) if in_merge else []
    if steps.dry_run:
        cfg = snap.project
        lang = translator(cfg.language) if cfg.language in LANGS else t
        if in_merge:
            steps.effect("git_merge", work.local, lang("tasks.effect.merge_abort"))
        if reason is not None:
            steps.effect("forge_update", f"issue#{work.number}", lang("tasks.effect.blocked"))
            steps.effect("forge_create", f"issue#{work.number}", lang("tasks.effect.escalation"))
        return steps.dry_result(warnings)

    def abort() -> None:
        if git.run("merge", "--abort", check=False, read=False).returncode != 0:
            raise OpError("E_CONFIG_GIT_UNAVAILABLE", details={"reason": "merge_abort_failed"})

    if in_merge:
        steps.run("abort", abort)
    else:
        steps.skip("abort", "no_merge_in_progress")
    if reason is not None:
        steps.run("escalate", lambda: _escalate(ctx, git, snap, work, reason, files))
    else:
        steps.skip("escalate")
    return ok(OP, _data(work, "escalated" if reason is not None else "aborted"), warnings)


def _push_step(ctx: OpContext, git: Git, snap: BaseSnapshot, work: Work, steps: Steps, t: Translator,
               warnings: list[WarningObj], *, offline: bool) -> bool:
    if offline:
        steps.skip("push", "offline")
        return False
    ahead, _behind = counts(git, f"refs/remotes/origin/{work.branch}")
    if not ahead:
        steps.skip("push", "up_to_date")
        return False
    return bool(steps.run("push", lambda: push(ctx, git, snap, work, t, warnings)))


def _continue(ctx: OpContext, git: Git, snap: BaseSnapshot, work: Work, warnings: list[WarningObj]) -> Result:
    """``--continue``: el commit de la fusión que dejó un conflicto ``agent`` y el push."""
    steps = Steps(ctx, OP, CONTINUE_STEPS)
    t = ctx.t()
    lang = translator(snap.project.language) if snap.project.language in LANGS else t
    project = ctx.project()
    assert project is not None
    commit: str | None = None
    if merge_in_progress(ctx):
        pending = sorted(unmerged_paths(git))
        changed = git.run("diff", "--name-only", "-z", "HEAD", check=False).stdout.decode("utf-8", "surrogateescape")
        candidates = sorted(set(pending) | {p for p in changed.split("\0") if p})
        marked = conflict_markers(git, project.root, candidates)
        if marked:
            declared = frozenset(snap.roles.section_owner())
            raise steps.failure("merge_base", "E_STATE_MERGE_CONFLICT", details={"paths": marked}, data={
                **_data(work, "conflict"),
                "conflicts": [{"path": p, "class": classify(p, declared, symlinked=False).cls, "resolution": "agent"}
                              for p in marked],
            })
        if steps.dry_run:
            steps.effect("git_commit", work.local, lang("tasks.effect.merge_commit"))
        else:
            def finish() -> str | None:
                if pending:  # -A: también si el agente resolvió borrando el archivo
                    git.run("--literal-pathspecs", "add", "-A", "--", *pending, read=False)
                proc = git.run("commit", "--quiet", "--no-edit", "--cleanup=strip", check=False, read=False)
                if proc.returncode != 0:
                    raise OpError("E_CONFIG_GIT_UNAVAILABLE", details={"reason": "commit_failed"})
                return git.rev_parse("HEAD")

            commit = steps.run("merge_base", finish)
    else:
        steps.skip("merge_base", "no_merge_in_progress")
    guard_remote(ctx, git)
    status = "offline" if steps.dry_run else fetch_task(git, work.number)
    if status == "missing":
        raise steps.failure("push", "E_CLAIM_NOT_HELD", details={"task": work.number, "reason": "ref_missing"})
    offline = status != "ok"
    if steps.dry_run:
        steps.effect("git_push", work.branch, lang("tasks.effect.push", task=work.number))
        return steps.dry_result(warnings)
    if offline:
        warnings.append(make_warning("W_NET_OFFLINE", t))
    else:
        check_claim(git, work.number)
    pushed = _push_step(ctx, git, snap, work, steps, t, warnings, offline=offline)
    _unblock(ctx, work, snap, steps, offline=offline)
    warnings.extend(steps.warnings(t))
    return ok(OP, _data(work, "merged" if commit else "up_to_date", commit=commit, pushed=pushed), warnings)


def run(ctx: OpContext, **kwargs: object) -> Result:
    """``update [--continue | --abort [--escalate <motivo>]]``."""
    unknown = set(kwargs) - {"continue", "abort", "escalate"}
    if unknown:
        raise _bad(sorted(unknown)[0], "unknown")
    cont, abort, reason = _check_input(kwargs.get("continue", False), kwargs.get("abort", False),
                                       kwargs.get("escalate"))
    t = ctx.t()
    project = ctx.project()
    assert project is not None
    snap = ctx.base()
    git = ctx.git()
    warnings: list[WarningObj] = []
    if snap.fallback_sha is not None:
        warnings.append(make_warning("W_CONFIG_POLICY_FALLBACK", t, details=snap.fallback_details()))
    work = resolve_work(ctx, git, snap)
    if abort:
        return _abort(ctx, git, snap, work, reason, warnings)
    if cont:
        return _continue(ctx, git, snap, work, warnings)
    if merge_in_progress(ctx):
        raise OpError("E_STATE_MERGE_IN_PROGRESS")
    base = snap.project.base_branch
    steps = Steps(ctx, OP, STEPS)
    cfg = snap.project
    lang = translator(cfg.language) if cfg.language in LANGS else t

    pending = changes(git, [])
    if pending and steps.dry_run:
        steps.run("save_local", lambda: scope_check(ctx, git, snap, work, []))
        steps.effect("git_commit", work.local, lang("tasks.effect.save_commit", n=len(pending)))
    elif pending:
        steps.run("save_local", lambda: _save_local(ctx, git, snap, work, t, warnings))
    else:
        steps.skip("save_local", "clean")
    remote = steps.run("fetch", lambda: sync_remote(ctx, git, base, work.number))
    if remote.offline:
        warnings.append(make_warning("W_NET_OFFLINE", t))
    elif _main_broken(ctx):
        warnings.append(make_warning("W_STATE_MAIN_BROKEN", t))
    tracking_task = f"refs/remotes/origin/{work.branch}"
    tracking_base = f"refs/remotes/origin/{base}"
    _ahead, behind_remote = counts(git, tracking_task)
    _mine, behind = counts(git, tracking_base)
    if steps.dry_run:
        if behind_remote:
            steps.effect("git_merge", work.local, lang("tasks.effect.merge_remote", task=work.number))
        if behind:
            steps.effect("git_merge", work.local, lang("tasks.effect.merge_base", base=base))
        if not remote.offline:
            steps.effect("git_push", work.branch, lang("tasks.effect.push", task=work.number))
        return steps.dry_result(warnings)

    integrated = False
    if remote.offline:
        steps.skip("integrate_remote_task", "offline")
    elif behind_remote:
        first = steps.run("integrate_remote_task", lambda: _integrate_remote(ctx, git, snap, work, t, warnings, behind))
        integrated = first.status in ("fast_forward", "merged")
    else:
        steps.skip("integrate_remote_task", "up_to_date")
    _mine, behind = counts(git, tracking_base)
    commit: str | None = None
    outcome: MergeOutcome | None = None
    if behind:
        outcome = steps.run("merge_base", lambda: _merge_base(ctx, git, snap, work, t, warnings,
                                                              integrated=integrated, behind=behind))
        commit = outcome.commit if outcome.status == "merged" else None
    else:
        steps.skip("merge_base", "up_to_date")
    pushed = _push_step(ctx, git, snap, work, steps, t, warnings, offline=remote.offline)
    _unblock(ctx, work, snap, steps, offline=remote.offline)
    warnings.extend(steps.warnings(t))
    result = "merged" if outcome is not None and outcome.status in ("merged", "fast_forward") else "up_to_date"
    data = _data(work, result, integrated=integrated, behind=behind, commit=commit, outcome=outcome, pushed=pushed)
    placeholder = make_warning("W_OUTPUT_TRUNCATED", t, details={"budget": BUDGET, "trimmed": ["conflicts"],
                                                                 "omitted": {"conflicts": len(data["conflicts"])}})
    trimmed = fit(OP, data, budget=BUDGET, trim_order=["conflicts"], warnings=warnings, truncated_warning=placeholder)
    if trimmed.trimmed:
        warnings.append(make_warning("W_OUTPUT_TRUNCATED", t, details=trimmed.warning_details(BUDGET)))
    return ok(OP, data, warnings)


def _integrate_remote(ctx: OpContext, git: Git, snap: BaseSnapshot, work: Work, t: Translator,
                      warnings: list[WarningObj], behind: int) -> MergeOutcome:
    try:
        return integrate(ctx, git, snap, work, t, warnings)
    except OpError as exc:
        if exc.code != "E_STATE_MERGE_CONFLICT":
            raise
        data = dict(exc.data or {})
        conflicts = data.get("conflicts", [])
        full = _data(work, "conflict", behind=behind)
        full["conflicts"] = conflicts
        raise OpError(exc.code, details=exc.details, data=full) from exc
