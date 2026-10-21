# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove submit`` · ``submit_task`` (05 §9.18; 04-tareas §7; 11-trazabilidad §2.7, §2.8 y §3).

Pasos, en el orden de 05 §9.18:

1. ``preflight``: la rama es ``task/<n>`` y el reclamo es del actor; el estado de partida es
   ``claimed``, ``changes_requested``, ``expired`` o ``in_review``. Sin la confirmación de
   ``attest-v1`` (TTY, ``--sign-off`` o ``sign_off: true``) falla con
   ``E_VALIDATION_SIGNOFF_MISSING`` antes de cualquier cambio (REQ-TRACE-22). También calcula
   la traza con la declaración (§2.7), así una declaración que no alcanza falla antes de
   escribir la nota.
2. ``write_note``: agrega el bloque de ``note`` a ``memory/notes/task-<n>.md`` y reescribe
   ``ai_use`` y ``assisted_by`` del front matter (REQ-TRACE-21). Sin ``note`` y sin un bloque
   nuevo desde la ronda anterior, ``E_VALIDATION_NOTE_MISSING``.
3. ``save`` y 4. ``update``: como ``save`` y ``update``. Con un Issue ``main-broken`` abierto,
   ``update`` se omite (``W_STATE_MAIN_BROKEN``, ``update_result: "skipped"``). Sin red, el
   commit queda en local y la operación falla con ``E_NET_OFFLINE`` en ``update``; repetirla
   retoma desde el push.
5. ``local_checks``: ``check_path`` en modo ``commit`` sobre el diff de la tarea, las reglas de
   ``validate`` en modo ``tree``, la traza de §2.7 y, con ``format: latex``, ``local-build``.
6. ``find_or_create_pr`` (canónico): el PR abierto de ``task/<n>`` o uno nuevo, nunca en
   borrador (``E_STATE_PR_DRAFT`` si hay uno en borrador). Un PR cerrado no se reabre.
7. ``pr_body`` (reflejo): el cuerpo completo desde la plantilla (``ops.prbody``).
8. ``submission_comment`` (reflejo): el comentario de entrega con el marcador ``submission``;
   una ronda nueva solo si cambió la cabeza (REQ-TASK-52).
9. ``request_reviewers`` (reflejo): en una ronda nueva y si la tarea no estaba ya
   ``in_review``, al ``lead`` (o al ``deputy`` si el autor es el ``lead``) y a quien pidió
   cambios.
10. ``context``: el paquete de fase ``submission``, dentro del tope de 8 000 caracteres.

No sella (REQ-TASK-79): el sello lo pone el CI en cada cabeza del PR. Al terminar copia los
registros de evidencia de los PR propios (REQ-OPS-66).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timezone
from typing import TYPE_CHECKING, Any

from .. import _meta
from ..domain.marked import SanitizeTally
from ..domain.tasks import derive_state
from ..domain.text import same_login
from ..domain.trailers import trailer_block
from ..forge.base import ForgeError, PullRequestExists
from ..i18n import LANGS, Translator, translator
from ..vcs.git import is_offline, run_bounded, which_outside
from . import note as notes
from .board import copy_evidence, is_network_error, owner_and_sections, refresh
from .comments import own_comment, rebuild
from .coordinator import Target, build_package, common_tasks, trim_package
from .fit import envelope_size
from .gitwork import counts, merge
from .prbody import hook_denials, render_body
from .result import OpError, Result, WarningObj, make_warning, ok
from .save import (
    Change,
    Trace,
    Work,
    auto_message,
    changes,
    check_changes,
    check_trace_input,
    commit_trace,
    commit_work,
    conflict_error,
    dropped_warning,
    integrate_and_push,
    merge_message,
    parse_name_status,
    push,
    resolve_work,
    scope_check,
    sync_remote,
    trace_warnings,
)
from .snapshot import CI_ACTORS, TaskEvidence, authz_from_roles, collect_task, task_branch
from .steps import Steps
from .trace import Aggregate, CommitTrace, aggregate, branch_commits, sections_of
from .workspace import merge_in_progress

if TYPE_CHECKING:
    from ..config.base import BaseSnapshot
    from ..forge.base import PullRequest
    from ..vcs.git import Git
    from .board import Fetched
    from .context import OpContext

__all__ = ["BUDGET", "OP", "STEPS", "render_text", "run"]

OP = "submit"
BUDGET = 8000  # 05 §7.1
PROBE_TIMEOUT_S = 60  # latexmk -v antes de compilar (05 §9.18 paso 5)
STEPS = (
    "preflight", "write_note", "save", "update", "local_checks", "find_or_create_pr", "pr_body",
    "submission_comment", "request_reviewers", "context",
)
START = frozenset({"claimed", "changes_requested", "expired", "in_review"})
SUMMARY_MAX = 4000
TITLE_MAX = 120
AI_NOTE_MAX = 500
DONE_MAX = 2000
EXPIRED = "state:expired"
_DECISION_ID = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}-[1-9][0-9]*-[a-z0-9]+(?:-[a-z0-9]+)*")
_DECISION_FILE = re.compile(r"memory/decisions/(" + _DECISION_ID.pattern + r")\.md")
_FINDINGS_SHOWN = 10


def _bad(param: str, reason: str = "type") -> OpError:
    return OpError("E_VALIDATION_INPUT", details={"errors": [{"param": param, "reason": reason}]})


def _check_note(note: object) -> dict[str, Any] | None:
    if note is None:
        return None
    if not isinstance(note, Mapping):
        raise _bad("note")
    unknown = set(note) - {"done", "next", "decisions", "questions"}
    if unknown:
        raise _bad("note", "unknown")
    done = note.get("done")
    if not isinstance(done, str) or not done.strip():
        raise _bad("note", "required")
    if len(done) > DONE_MAX:
        raise _bad("note", "maxLength")
    for key in ("next", "questions"):
        if note.get(key) is not None and not isinstance(note[key], str):
            raise _bad("note")
    decisions = note.get("decisions")
    if decisions is not None and (not isinstance(decisions, list)
                                  or not all(isinstance(d, str) and _DECISION_ID.fullmatch(d) for d in decisions)):
        raise _bad("note", "pattern")
    total = sum(len(str(note.get(k) or "")) for k in ("done", "next", "questions")) + sum(
        len(d) for d in decisions or ())
    if total > notes.NOTE_TEXT_MAX:  # 05 §9.18
        raise _bad("note", "maxLength")
    return dict(note)


def _check_input(summary: object, title: object, ai_note: object, sign_off: object,
                 compile_: object) -> tuple[str, str | None, str | None, bool, bool | None]:
    if not isinstance(summary, str) or not summary.strip():
        raise _bad("summary", "minLength" if isinstance(summary, str) else "type")
    if len(summary) > SUMMARY_MAX:
        raise _bad("summary", "maxLength")
    if title is not None and (not isinstance(title, str) or len(title) > TITLE_MAX):
        raise _bad("title")
    if ai_note is not None and (not isinstance(ai_note, str) or len(ai_note) > AI_NOTE_MAX):
        raise _bad("ai_note")
    if not isinstance(sign_off, bool):
        raise _bad("sign_off")
    if compile_ is not None and not isinstance(compile_, bool):
        raise _bad("compile")
    shown_title = title.strip() if isinstance(title, str) and title.strip() else None
    return summary.strip(), shown_title, ai_note.strip() if isinstance(ai_note, str) and ai_note.strip() else None, \
        sign_off, compile_


def _channel(ctx: OpContext, sign_off: bool) -> str | None:
    """Canal de la confirmación de ``attest-v1`` (REQ-TRACE-22 y REQ-TRACE-25)."""
    if sign_off:
        return "mcp" if ctx.inv.surface == "mcp" else "cli-flag"
    if ctx.inv.attest_tty and ctx.inv.surface == "cli":
        return "tty"
    return None


def _signer(snap: BaseSnapshot, me: str, forge_id: int | None) -> tuple[str, str]:
    """Nombre y correo de ``Signed-off-by`` desde ``roles.yaml`` de la base (REQ-TRACE-24)."""
    person = snap.roles.person(me)
    login = person.login if person is not None else me
    name = (person.name if person is not None and person.name else login).replace("<", "").replace(">", "").strip()
    if person is not None and person.email:
        return name, person.email
    uid = (person.forge_id if person is not None and person.forge_id else None) or forge_id
    return name, f"{uid}+{login}@users.noreply.github.com" if uid else f"{login}@users.noreply.github.com"


@dataclass
class _Pre:
    work: Work
    channel: str
    me: str
    fetched: Fetched | None
    ev: TaskEvidence | None
    state: str
    agg: Aggregate
    trace: Trace
    rounds: list[dict[str, Any]] = field(default_factory=list)
    pr_number: int | None = None


def _pending(git: Git, uses: Sequence[str] | None) -> CommitTrace | None:
    """Lo que el ``save`` de la entrega guardaría, como un commit más para la traza previa."""
    found = changes(git, [])
    if not found:
        return None
    paths = frozenset(p for ch in found for p in (ch.path, ch.previous) if p)
    return CommitTrace("pending", False, False, paths, tuple(uses) if uses else None)


def _open_pull(ev: TaskEvidence | None, base: str):  # type: ignore[no-untyped-def]
    if ev is None:
        return None
    found = [p for p in ev.snapshot.pulls if p.state == "open" and p.same_repo and p.base_ref == base]
    return min(found, key=lambda p: p.created_at) if found else None


def _previous_rounds(ev: TaskEvidence | None, base: str, me: str) -> tuple[list[dict[str, Any]], int | None]:
    """Rondas del marcador ``submission`` propio del PR abierto (04 §7 paso 9)."""
    pull = _open_pull(ev, base)
    if pull is None:
        return [], None
    subs = [m for m in pull.markers if m.kind == "submission" and same_login(m.author, me)]
    rounds = subs[-1].data.get("rounds") if subs else None
    return ([dict(r) for r in rounds if isinstance(r, Mapping)] if isinstance(rounds, list) else []), pull.number


def _content(body: str) -> str:
    """Lo redactado de un bloque, sin el encabezado (número, fecha y login)."""
    text = body.strip()
    return text.split("\n", 1)[1].strip() if "\n" in text else ""


def _diff(git: Git, base: str) -> tuple[str | None, list[Change]]:
    """Cambios de la tarea frente a la base (``git diff --name-status merge-base HEAD``)."""
    mb = git.merge_base(f"refs/remotes/origin/{base}", "HEAD")
    if mb is None:
        return None, []
    proc = git.run("--literal-pathspecs", "diff", "--name-status", "-M", "-z", "--no-color", "--no-ext-diff",
                   "--no-textconv", mb, "HEAD", "--")
    return mb, parse_name_status(proc.stdout)


def _local_build(ctx: OpContext, snap: BaseSnapshot, override: bool | None) -> str:
    """``local-build`` (05 §9.18 paso 5): ``pass``, ``fail`` o ``skip`` según ``compile_on_submit``."""
    build = snap.project.build
    mode = {True: "always", False: "never"}.get(override, build.compile_on_submit)  # type: ignore[arg-type]
    if mode == "never":
        return "skip"
    project = ctx.project()
    assert project is not None
    env = dict(ctx.inv.env)
    exe = which_outside("latexmk", project.root, path=env.get("PATH", ""), pathext=env.get("PATHEXT"))
    if exe is not None:
        # El de MiKTeX sin Perl existe pero no corre: cuenta como ausente, y con if_available la entrega no se
        # detiene por algo de la computadora, porque el CI compila igual (sesión de prueba del 20/10).
        try:
            runs = run_bounded([exe, "-v"], cwd=project.root, env=env, timeout=PROBE_TIMEOUT_S).returncode == 0
        except Exception:  # noqa: BLE001 - un timeout o un fallo al lanzar: no corre
            runs = False
        exe = exe if runs else None
    if exe is None:
        return "fail" if mode == "always" else "skip"
    from .ci_plan import ENGINE_FLAGS, SHELL_FLAGS

    env.update({"shell_escape": "p" if build.shell_escape == "restricted" else "f", "openout_any": "p"})
    env.pop("openin_any", None)
    cmd = [exe, "-norc", "-cd", ENGINE_FLAGS.get(build.engine, "-pdf"),
           SHELL_FLAGS.get(build.shell_escape, "-no-shell-escape"), "-interaction=nonstopmode", "-halt-on-error",
           "-file-line-error", "-outdir=build", build.main]
    try:
        proc = run_bounded(cmd, cwd=project.root, env=env, timeout=build.timeout_minutes * 60)
    except Exception:  # noqa: BLE001 - un timeout o un fallo al lanzar es una compilación fallida
        return "fail"
    return "pass" if proc.returncode == 0 else "fail"


def run(ctx: OpContext, *, summary: object = None, summary_file: object = None, title: object = None,
        note: object = None, note_file: object = None, ai_use: object = None, ai_tool: object = None,
        ai_model: object = None, ai_note: object = None, sign_off: object = False, compile: object = None) -> Result:
    """``submit (--summary <texto> | --summary-file <f>) [...] [--sign-off]``."""
    del summary_file, note_file  # la CLI ya los convirtió en summary y note
    text, title_in, note_text, signed, compile_override = _check_input(summary, title, ai_note, sign_off, compile)
    note_obj = _check_note(note)
    uses, tool, model = check_trace_input(ai_use, ai_tool, ai_model)
    t = ctx.t()
    project = ctx.project()
    assert project is not None
    root = project.root
    snap = ctx.base()
    cfg, roles = snap.project, snap.roles
    store = ctx.store()
    git = ctx.git()
    now = ctx.inv.now()
    base = cfg.base_branch
    lang = translator(cfg.language) if cfg.language in LANGS else t
    warnings: list[WarningObj] = []
    if snap.fallback_sha is not None:
        warnings.append(make_warning("W_CONFIG_POLICY_FALLBACK", t, details=snap.fallback_details()))
    steps = Steps(ctx, OP, STEPS)
    declared = frozenset(roles.section_owner())
    note_path = notes.note_path

    def preflight() -> _Pre:
        work = resolve_work(ctx, git, snap)
        channel = _channel(ctx, signed)
        if channel is None:
            raise OpError("E_VALIDATION_SIGNOFF_MISSING")
        if merge_in_progress(ctx):
            raise OpError("E_STATE_MERGE_IN_PROGRESS")
        fetched: Fetched | None = None
        ev: TaskEvidence | None = None
        state = work.state
        if not is_offline(ctx.inv.env):
            forge = ctx.forge()
            try:
                fetched = refresh(forge, cfg=cfg, roles=roles, store=store, now=now)
                pre = fetched.prefetch(work.number)
                authz = authz_from_roles(roles, ci_actors=CI_ACTORS)
                ev = collect_task(forge, work.number, authz, base_branch=base, prefetch=pre)
            except ForgeError as exc:
                if not is_network_error(exc):
                    raise
                fetched, ev = None, None
            if fetched is not None:
                if ev is None:
                    raise OpError("E_STATE_NOT_A_TASK", details={"task": work.number})
                derived = derive_state(ev.snapshot)
                if not same_login(derived.owner, work.me):
                    raise OpError("E_CLAIM_NOT_HELD", details={"task": work.number, "owner": derived.owner})
                state = derived.state
        if state not in START:
            raise OpError("E_STATE_INVALID_TRANSITION", details={"task": work.number, "state": state})
        trace = commit_trace(ctx, ai_use=uses, ai_tool=tool, ai_model=model)
        _mb, commits = branch_commits(git, f"refs/remotes/origin/{base}")
        pending = _pending(git, uses)
        agg = aggregate([*commits, *([pending] if pending else [])], note_path=note_path(work.number),
                        declared=declared, decl_uses=uses, decl_entries=trace.assisted)
        rounds, pr_number = _previous_rounds(ev, base, work.me)
        return _Pre(work, channel, work.me, fetched, ev, state, agg, trace, rounds, pr_number)

    pre = steps.run("preflight", preflight)
    work = pre.work
    n = work.number
    branch = work.branch
    if steps.dry_run:
        return _dry_run(steps, pre, lang, n, note_obj is not None, warnings)

    def write_note() -> bool:
        current = notes.read_note(root, n)
        blocks = list(current.blocks) if current is not None else []
        previous = 0
        if pre.rounds:
            head = str(pre.rounds[-1].get("head_sha") or "")
            try:
                raw = git.show(head, note_path(n)) if head else None
            except Exception:  # noqa: BLE001 - sin el objeto, se cuenta desde cero
                raw = None
            if raw is not None:
                previous = len(notes.parse_note(raw.decode("utf-8", "replace"), note_path(n)).blocks)
        block: str | None = None
        if note_obj is not None:
            det = pre.trace.detection
            model_used = next((e.model for e in pre.trace.assisted if e.model != "unknown"), None)
            date = now.astimezone().strftime("%Y-%m-%d")
            block = notes.render_block(n=len(blocks) + 1, date=date, login=pre.me, door=det.tool if det else "manual",
                                       model=model_used, note=note_obj, lang=lang)
            if blocks and same_login(str(blocks[-1].meta.get("login", "")), pre.me) \
                    and _content(blocks[-1].body) == _content(block.split("\n", 1)[1]):
                block = None  # la misma nota que el último bloque: repetir no la duplica (REQ-TASK-52)
        elif len(blocks) <= previous:
            raise OpError("E_VALIDATION_NOTE_MISSING", details={"reason": "no_new_block"})
        return notes.write(root, n, created=now.astimezone().strftime("%Y-%m-%d"), created_by=pre.me, block=block,
                           ai_use=pre.agg.ai_use, assisted=pre.agg.assisted_values)

    steps.run("write_note", write_note)

    offline = pre.fetched is None
    commit_sha: str | None = None

    def save_step() -> str | None:
        found = scope_check(ctx, git, snap, work, [])
        warnings.extend(trace_warnings(pre.trace, t))
        done = commit_work(git, snap, work, found, [], message=auto_message(snap, t, "submit"), trace=pre.trace)
        if not offline:
            sync_remote(ctx, git, base, n)
            integrate_and_push(ctx, git, snap, work, t, warnings)
        return done.sha if done is not None else None

    commit_sha = steps.run("save", save_step)
    if offline:
        raise steps.failure("update", "E_NET_OFFLINE", details={"task": n})
    assert pre.fetched is not None and pre.ev is not None
    fetched, ev = pre.fetched, pre.ev
    forge = ctx.forge()
    if pre.state == "expired":  # la entrega renueva el reclamo (05 §9.18)
        try:
            forge.remove_label(n, EXPIRED)
        except ForgeError:
            warnings.append(make_warning("W_FORGE_REFLECTION_FAILED", t, details={"step": "save"}))

    update_result = "up_to_date"
    if fetched.board.base.main_broken_issue is not None:
        warnings.append(make_warning("W_STATE_MAIN_BROKEN", t))
        steps.skip("update", "main_broken")
        update_result = "skipped"
    else:
        def update_step() -> str:
            tracking = f"refs/remotes/origin/{base}"
            _ahead, behind = counts(git, tracking)
            if not behind:
                return "up_to_date"
            outcome = merge(git, tracking, message=merge_message(f"origin/{base}", work.local, n),
                            scope=work.scope(snap), root=root)
            dropped = dropped_warning(ctx, work, outcome, t)
            if dropped is not None:
                warnings.append(dropped)
            if outcome.status == "conflict":
                raise conflict_error(outcome)
            push(ctx, git, snap, work, t, warnings)
            return "merged"

        update_result = steps.run("update", update_step)

    def local_checks() -> tuple[list[dict[str, str]], Aggregate, str | None, list[Change]]:
        checks: list[dict[str, str]] = []
        mb, diff = _diff(git, base)
        check_changes(ctx, git, snap, work, diff)  # todo el diff de la tarea (04 §7 paso 4)
        checks.append({"check": "scope", "status": "pass"})
        from . import validate as validate_op
        from .registry import REGISTRY

        spec = next(s for s in REGISTRY if s.id == "validate")
        args = {p.name: p.default for p in spec.params}
        args.update(mode="tree", head="HEAD")
        result = validate_op.run(ctx, **args)
        results = (result.data or {}).get("results", []) if isinstance(result.data, Mapping) else []
        checks.extend({"check": str(r.get("check")), "status": str(r.get("status"))} for r in results)
        if not result.ok:
            failing = [{"check": r.get("check"), "findings": list(r.get("findings", []))[:_FINDINGS_SHOWN]}
                       for r in results if r.get("status") == "fail"]
            raise OpError("E_VALIDATION_FAILED", data={"checks": checks, "results": failing})
        _base, commits = branch_commits(git, f"refs/remotes/origin/{base}")
        agg = aggregate(commits, note_path=note_path(n), declared=declared, decl_uses=uses,
                        decl_entries=pre.trace.assisted)
        checks.append({"check": "traceability", "status": "pass"})
        if cfg.format == "latex":
            status = _local_build(ctx, snap, compile_override)
            checks.append({"check": "local-build", "status": status})
            if status == "fail":
                raise OpError("E_VALIDATION_FAILED", data={"checks": checks})
        return checks, agg, mb, diff

    checks, agg, merge_base_sha, diff = steps.run("local_checks", local_checks)
    head = git.rev_parse("HEAD")
    assert head is not None
    pr_sections = sorted(sections_of((p for ch in diff for p in (ch.path, ch.previous) if p), declared))
    parts: dict[str, list[str]] = {}
    for ch in diff:
        for s in sections_of([ch.path], declared):
            parts.setdefault(s, []).append(ch.path)
    decisions = sorted(m.group(1) for ch in diff if ch.status.startswith(("A", "R", "C"))
                       for m in [_DECISION_FILE.fullmatch(ch.path)] if m)
    spec = ev.body.spec or {}
    name, email = _signer(snap, pre.me, forge.identity().id)
    at = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    trace_obj: dict[str, Any] = {
        "version": 1,
        "task": n,
        "sections": pr_sections,
        "ai_use": list(agg.ai_use),
        "ai_use_by_section": {s: list(v) for s, v in agg.by_section.items()},
        "assisted_by": [{"tool": e.tool, "model": e.model, "tool_source": e.tool_source,
                         "model_source": e.model_source} for e in agg.assisted],
    }
    if note_text:
        trace_obj["ai_note"] = note_text
    trace_obj.update({
        "sign_off": {"login": pre.me, "name": name, "email": email, "channel": pre.channel,
                     "attestation": "attest-v1", "at": at},
        "commits": {"base": merge_base_sha or "", "head": head, "count": agg.count, "undeclared": agg.undeclared},
        "core_version": _meta.__version__,
    })
    meta = {"schema": 1, "task": n, "sections": pr_sections,
            "plan_refs": sorted(set(spec.get("plan_refs", ()))),  # type: ignore[arg-type]
            "decisions": decisions, "trace": trace_obj}
    trailers = trailer_block(task=n, sections=pr_sections, ai_use=agg.ai_use, assisted=agg.assisted) \
        + f"Signed-off-by: {name} <{email}>\n"
    claims = [m for m in ev.snapshot.issue_markers if m.kind == "claim" and same_login(m.author, pre.me)]
    since = str(claims[-1].data.get("claimed_at")) if claims and claims[-1].data.get("claimed_at") else None
    replaced = sorted(p.number for p in ev.snapshot.pulls if p.state == "closed" and not p.merged and p.same_repo)

    def body_for(replaces: int | None) -> str:
        return render_body(lang=lang, number=n, summary=text, replaces=replaces, parts=parts, ai_use=agg.ai_use,
                           assisted=agg.assisted_values, ai_note=note_text, decisions=decisions,
                           hook_count=hook_denials(store.root, n, since), meta=meta, trailers=trailers)

    replaces = replaced[-1] if replaced else None

    def find_or_create() -> tuple[PullRequest, bool]:
        existing = forge.find_pull_request(branch, state="open")
        if existing is not None:
            if existing.draft:
                raise OpError("E_STATE_PR_DRAFT", details={"pr": existing.number, "url": existing.url})
            return existing, False
        try:
            made = forge.create_pull_request(head=branch, base=base, title=title_in or ev.issue.title,
                                             body=body_for(replaces), draft=False)
        except PullRequestExists as exc:  # otro intento lo creó: se usa ese
            if exc.existing.draft:
                raise OpError("E_STATE_PR_DRAFT", details={"pr": exc.existing.number}) from exc
            return exc.existing, False
        return made, True

    pr, created = steps.run("find_or_create_pr", find_or_create)
    body = body_for(replaces if replaces is not None and replaces < pr.number else None)
    if pr.body != body or (title_in is not None and pr.title != title_in):
        steps.reflect("pr_body", lambda: forge.update_pull_request(pr.number, body=body, title=title_in))
    else:
        steps.skip("pr_body", "unchanged")

    previous = pre.rounds if pre.pr_number == pr.number else []
    new_round = not previous or previous[-1].get("head_sha") != head
    number_round = (int(previous[-1].get("round", len(previous))) + 1) if previous and new_round else (
        int(previous[-1].get("round", 1)) if previous else 1)
    rounds = list(previous)
    if new_round:
        rounds.append({"round": number_round, "head_sha": head, "submitted_at": at,
                       "local_checks": {c["check"]: c["status"] for c in checks}, "note_path": note_path(n)})

    def submission_comment() -> None:
        comments = forge.list_comments(pr.number)
        own = own_comment(comments, pre.me, "submission")
        data = {"task": n, "rounds": rounds}
        new_body = rebuild(own.body if own is not None else None, "submission", data, lang, n)
        if own is None:
            forge.create_comment(pr.number, new_body, op_id=f"submission-{n}-{head[:16]}")
        elif own.body != new_body:
            forge.update_comment(own.id, new_body)

    steps.reflect("submission_comment", submission_comment)
    requested: list[str] = []
    if new_round and pre.state != "in_review":
        lead, deputy = roles.lead, roles.deputy
        wanted: list[str] = []
        if not same_login(pre.me, lead):
            wanted.append(lead)
        elif deputy:
            wanted.append(deputy)
        elif cfg.mode != "individual":  # 00-marco D-57: sin suplente ni bypass, no hay nada que avisar
            warnings.append(make_warning("W_PERM_DEPUTY_MISSING", t))
        pull = _open_pull(ev, base)
        latest: dict[str, tuple[str, str]] = {}  # autor -> (grafía, último estado que cuenta)
        for review in pull.reviews if pull is not None and pull.number == pr.number else ():
            if review.state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                latest[review.author.casefold()] = (review.author, review.state)
        for author, state in latest.values():  # 04 §7 paso 10: también quien pidió cambios
            if state == "CHANGES_REQUESTED" and not same_login(author, pre.me) \
                    and not any(same_login(author, w) for w in wanted):
                wanted.append(author)
        if wanted and steps.reflect("request_reviewers", lambda: forge.request_reviewers(pr.number, wanted)) is not None:
            requested = wanted
        elif not wanted:
            steps.skip("request_reviewers", "nobody")
    else:
        steps.skip("request_reviewers", "same_round" if not new_round else "in_review")
    try:
        if forge.capabilities().profile == "advisory":
            warnings.append(make_warning("W_FORGE_PROFILE_ADVISORY", t))
    except ForgeError:
        pass

    tally = SanitizeTally()
    derived = derive_state(ev.snapshot)
    _owner, sections = owner_and_sections(ev.snapshot, derived)
    target = Target(
        number=n, state="in_review", owner=pre.me, sections=sections,
        plan_refs=tuple(spec.get("plan_refs", ())),  # type: ignore[arg-type]
        title=ev.issue.title, summary=str(spec.get("summary", "")),
        done=tuple(spec.get("done", ())),  # type: ignore[arg-type]
    )
    package = steps.run("context", lambda: build_package(ctx, forge, phase="submission", target=target,
                                                         board=fetched.board, snap=snap, tally=tally))
    copy_evidence(ctx, forge, fetched, now=now)
    warnings.extend(steps.warnings(t))
    data: dict[str, object] = {
        "task": {"number": n, "url": ev.issue.url, "state": "in_review"},
        "pr": {"number": pr.number, "url": pr.url, "created": created},
        "round": number_round,
        "state": "in_review",
        "commit": commit_sha,
        "update_result": update_result,
        "checks": checks,
        "reviewers_requested": requested,
        "steps": steps.as_list(),
        "context": package,
        "next": ["record-assessment"],
    }
    sanitized = tally.details()
    if sanitized is not None:
        warnings.append(make_warning("W_VALIDATION_UNTRUSTED_SANITIZED", t, details=sanitized))
    placeholder = make_warning("W_OUTPUT_TRUNCATED", t, details={"budget": BUDGET, "trimmed": ["context"]})

    def fits() -> bool:
        return envelope_size(OP, data, [*warnings, placeholder]) <= BUDGET

    if trim_package(package, fits=fits, keep=common_tasks(target, fetched.board)):
        warnings.append(make_warning("W_OUTPUT_TRUNCATED", t, details={
            "budget": BUDGET, "trimmed": ["context"], "omitted": dict(package["omitted"])}))
    return ok(OP, data, warnings)


def _dry_run(steps: Steps, pre: _Pre, lang: Translator, n: int, with_note: bool,
             warnings: list[WarningObj]) -> Result:
    """Plan de ``--dry-run`` (REQ-OPS-30): lo que escribiría la entrega."""
    branch = task_branch(n)
    if with_note:
        steps.effect("file_write", notes.note_path(n), lang("tasks.effect.note", task=n))
    steps.effect("git_commit", pre.work.local, lang("tasks.effect.submit_commit"))
    steps.effect("git_push", branch, lang("tasks.effect.push", task=n))
    if pre.pr_number is None:
        steps.effect("forge_create", branch, lang("tasks.effect.create_pr", task=n))
    steps.effect("forge_update", f"pr:{branch}", lang("tasks.effect.pr_body"))
    steps.effect("forge_update", f"pr:{branch}", lang("tasks.effect.submission_comment"))
    return steps.dry_result(warnings)


def render_text(result: Result, t: Translator, *, fmt: str = "text") -> str:
    """Vista legible: el PR, la ronda y el aviso del sello (04-tareas REQ-TASK-79)."""
    del fmt
    data = result.data if isinstance(result.data, Mapping) else {}
    pr = data.get("pr") if isinstance(data.get("pr"), Mapping) else None
    if not result.ok or pr is None:
        return ""
    lines = [t("tasks.submit.done", round=data.get("round", 1), task=(data.get("task") or {}).get("number", ""),
               url=pr.get("url", "")), t("tasks.submit.seal")]
    return "\n".join(lines)
