# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove save`` · ``save_work`` (05 §9.14; 04-tareas §6.2 y §11.3; 11-trazabilidad §2.5).

Pasos, en el orden de 05 §9.14:

1. ``preflight``: la rama es ``task/<n>`` (en la nube, la ligada por ``local_branch``) y el
   reclamo es del actor según la caché de la tarea; no hay una fusión en curso. Trae la base
   con la comprobación de 10-seguridad REQ-SEC-74 y trae ``task/<n>``: si la ref no está en
   el remoto, ``E_CLAIM_NOT_HELD`` con ``ref_missing`` (REQ-TASK-32); si su último commit de
   reclamo no es el de la rama local, ``claim_replaced``. Sin red sigue en local.
2. ``scope_check``: ``check_path`` en modo ``commit`` (06 §2.4) de cada archivo cambiado,
   incluidos los nuevos, los renombrados y los borrados; limitado a ``paths`` si se indica.
3. ``commit`` (canónico): con los trailers de 11 §2.5. ``Section`` sale de las rutas
   ``sections/<id>/**`` del commit (REQ-TRACE-10); sin declaración, ``AI-Use: undeclared``
   (REQ-TRACE-12).
4. ``integrate_remote``: si ``origin/task/<n>`` avanzó desde otra máquina, lo fusiona en la
   local con el algoritmo de 04 §6.2 (REQ-TASK-49).
5. ``push`` (canónico): sin forzar. Si otro push se adelantó, integra y reintenta una vez
   (04 §11.3).
6. ``renew`` (reflejo): si la tarea estaba vencida, quita ``state:expired``.

En modo git, el de todas las superficies (08 REQ-FORGE-56), ``save`` no lee la forja (08
§7.5): el reclamante, el estado y las partes salen de ``.aucove/cache/tasks/<n>.json``,
que escriben ``claim``, ``switch``, ``status`` y ``context --refresh`` (06 §2.1). Con
``<PREFIJO>FORGE_WRITE_MODE=api`` también empuja con git: ``commit_files`` queda pendiente.

``update``, ``switch`` y ``release`` reutilizan ``resolve_work``, ``commit_work`` y
``integrate_and_push`` en su paso ``save_local``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..config.base import to_policy
from ..domain.paths import InvalidPath, OutsideRepo, canonical, classify
from ..domain.permissions import Task, check_change
from ..domain.text import same_login
from ..domain.trailers import (
    DECLARABLE_USES,
    Detection,
    Entry,
    SessionModel,
    canonical_uses,
    commit_message,
    detect_tool,
    resolve_assistance,
    trailer_block,
    valid_model,
    valid_tool,
)
from ..forge.base import ForgeError
from ..i18n import LANGS, Translator, translator
from ..vcs.links import case_map, link_probe
from .board import write_task_cache
from .check_path import case_insensitive, current_task, prepare
from .gitwork import (
    MergeOutcome,
    Scope,
    check_claim,
    counts,
    fetch_base,
    fetch_task,
    guard_remote,
    merge,
    push_task,
    write_dropped,
)
from .fit import envelope_size
from .result import OpError, Result, WarningObj, make_warning, ok
from .snapshot import task_branch
from .steps import Steps
from .workspace import merge_in_progress

if TYPE_CHECKING:
    from ..config.base import BaseSnapshot
    from ..vcs.git import Git
    from .context import OpContext

__all__ = [
    "OP",
    "STEPS",
    "Change",
    "Remote",
    "Trace",
    "Work",
    "check_changes",
    "commit_trace",
    "commit_work",
    "integrate_and_push",
    "is_staff",
    "parse_name_status",
    "resolve_work",
    "run",
    "save_and_push",
    "sync_remote",
    "whoami",
]

OP = "save"
STEPS = ("preflight", "scope_check", "commit", "integrate_remote", "push", "renew")
BUDGET = 4000  # 05 §7.1, «Resto»
MESSAGE_MAX = 200
PATHS_MAX = 200
EXPIRED = "state:expired"
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_SESSION_MAX = 64 * 1024


def _bad(param: str, reason: str = "type", **extra: object) -> OpError:
    return OpError("E_VALIDATION_INPUT", details={"errors": [{"param": param, "reason": reason, **extra}]})


# -- traza (11-trazabilidad §2.5 y §2.6) ------------------------------------------------------


@dataclass(frozen=True)
class Trace:
    """Lo que va en los trailers de un commit del núcleo y los avisos que deja."""

    ai_use: tuple[str, ...]
    assisted: tuple[Entry, ...]
    detection: Detection | None
    warnings: tuple[str, ...]


def session_model(cache_root: Path, env: Mapping[str, str]) -> SessionModel | None:
    """Modelo del registro de ``CLAUDE_CODE_SESSION_ID`` (REQ-TRACE-14); ``None`` si no hay."""
    sid = env.get("CLAUDE_CODE_SESSION_ID")
    if not isinstance(sid, str) or not _SESSION_ID.fullmatch(sid):
        return None
    path = cache_root / "state" / "sessions" / f"{sid}.json"
    try:
        with open(path, "rb") as fh:
            raw = fh.read(_SESSION_MAX + 1)
        data = json.loads(raw.decode("utf-8")) if len(raw) <= _SESSION_MAX else None
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    model, event = data.get("model"), data.get("model_event")
    # el objeto trace solo admite hook:SessionStart y hook:PostModelSwitch (11 §3.3)
    return SessionModel(model if valid_model(model) else None,
                        event if event in ("SessionStart", "PostModelSwitch") else None)


def commit_trace(ctx: OpContext, *, ai_use: Sequence[str] | None, ai_tool: str | None,
                 ai_model: str | None) -> Trace:
    """``AI-Use`` y ``Assisted-by`` de un commit con el contexto de la llamada (11 §2.6)."""
    env = ctx.inv.env
    det = detect_tool(env, via_mcp=ctx.inv.surface == "mcp")
    session = session_model(ctx.store().root, env)
    entries, warnings = resolve_assistance(det, session, env, ai_tool, ai_model)
    uses = canonical_uses(ai_use) if ai_use else ("undeclared",)  # REQ-TRACE-12
    if uses == ("none",) and det is not None:
        warnings.append("W_VALIDATION_NONE_WITH_AGENT")
    return Trace(uses, tuple(entries), det, tuple(warnings))


def check_trace_input(ai_use: object, ai_tool: object, ai_model: object) -> tuple[list[str] | None, str | None, str | None]:
    """Valida ``ai_use``, ``ai_tool`` y ``ai_model`` (11 §2.5) como ``E_VALIDATION_INPUT``."""
    uses: list[str] | None = None
    if ai_use is not None:
        if not isinstance(ai_use, (list, tuple)) or not all(isinstance(u, str) for u in ai_use):
            raise _bad("ai_use")
        if any(u not in DECLARABLE_USES for u in ai_use):
            raise _bad("ai_use", "enum")
        if ai_use:  # una lista vacía es no declarar (REQ-TRACE-12)
            try:
                canonical_uses(ai_use)
            except ValueError as exc:
                raise _bad("ai_use", str(exc)) from None
            uses = list(ai_use)
    if ai_tool is not None and not valid_tool(ai_tool):
        raise _bad("ai_tool", "pattern")
    if ai_model is not None and not valid_model(ai_model):
        raise _bad("ai_model", "pattern")
    return uses, ai_tool, ai_model  # type: ignore[return-value]


def trace_warnings(trace: Trace, t: Translator) -> list[WarningObj]:
    return [make_warning(code, t) for code in trace.warnings]


# -- tarea actual y remoto ---------------------------------------------------------------------


@dataclass(frozen=True)
class Work:
    """La tarea de la rama actual y el actor que la guarda."""

    number: int
    branch: str  # task/<n>
    local: str  # rama local actual (task/<n> o, en la nube, la de la sesión)
    me: str  # login del actor (el reclamante) con la grafía de people
    state: str  # estado de la tarea (de la caché o de derive_state)
    sections: tuple[str, ...]  # partes efectivas de la tarea
    staff: bool  # lead o deputy

    def task(self) -> Task:
        return Task(self.number, self.me, self.state, frozenset(self.sections))

    def scope(self, snap: BaseSnapshot) -> Scope:
        return Scope(self.number, self.me, self.staff, frozenset(self.sections), snap.roles.section_owner())


def is_staff(snap: BaseSnapshot, login: str) -> bool:
    """``login`` es el ``lead`` o el ``deputy`` de ``roles.yaml`` de la base."""
    roles = snap.roles
    return same_login(login, roles.lead) or (roles.deputy is not None and same_login(login, roles.deputy))


def whoami(ctx: OpContext, snap: BaseSnapshot) -> str:
    """Login del actor: la identidad en caché o, sin ella, la del adaptador (08 §6.2).

    Se evalúa con la grafía de ``people[].login`` si la cuenta es una persona del proyecto.
    """
    ident = ctx.store().identity(token_fp=None)
    if ident is not None:
        login, forge_id = ident.login, ident.id
    else:
        try:
            found = ctx.forge().identity()
        except ForgeError as exc:
            raise OpError("E_AUTH_NOT_AUTHENTICATED", details={"kind": exc.kind}) from exc
        login, forge_id = found.login, found.id
    return snap.roles.identify(login, forge_id).login or login


def resolve_work(ctx: OpContext, git: Git, snap: BaseSnapshot, *, me: str | None = None) -> Work:
    """La tarea de la rama actual y su reclamo (06 §2.1; REQ-TASK-32 con la caché)."""
    store = ctx.store()
    local = git.current_branch()
    number = current_task(ctx, git, store) if local is not None else None
    if local is None or number is None:
        raise OpError("E_STATE_NOT_ON_TASK_BRANCH", details={"branch": local})
    cached = store.task(number)
    if cached is None:
        raise OpError("E_CONFIG_POLICY_UNAVAILABLE", details={"reason": "task_cache_missing", "task": number})
    actor = me if me is not None else whoami(ctx, snap)
    if cached.claimant is None or not same_login(cached.claimant, actor):
        raise OpError("E_CLAIM_NOT_HELD", details={"task": number, "reason": "not_owner"})
    return Work(number, task_branch(number), local, actor, cached.state, cached.sections, is_staff(snap, actor))


@dataclass(frozen=True)
class Remote:
    """Lo que se sabe del remoto tras traerlo; ``offline`` si no se pudo."""

    offline: bool
    base_after: str | None = None


def sync_remote(ctx: OpContext, git: Git, base: str, number: int) -> Remote:
    """Trae la base (REQ-SEC-74) y ``task/<n>``, y comprueba el reclamo remoto (REQ-TASK-32).

    Con ``--dry-run`` no trae nada (REQ-OPS-30): solo pregunta al remoto si la ref existe.
    """
    guard_remote(ctx, git)
    if ctx.inv.dry_run:
        status = _remote_exists(git, number)
    else:
        fetched = fetch_base(git, base)
        if fetched.offline:
            return Remote(True)
        status = fetch_task(git, number)
        if status == "ok":
            check_claim(git, number)
            return Remote(False, fetched.after)
    if status == "missing":
        raise OpError("E_CLAIM_NOT_HELD", details={"task": number, "reason": "ref_missing"})
    return Remote(status == "offline")


def _remote_exists(git: Git, number: int) -> str:
    if git.offline:
        return "offline"
    try:
        proc = git.run("ls-remote", "--exit-code", "origin", f"refs/heads/{task_branch(number)}",
                       check=False, timeout_s=20.0)
    except Exception:  # noqa: BLE001 - sin red o git sin responder
        return "offline"
    return "ok" if proc.returncode == 0 else ("missing" if proc.returncode == 2 else "offline")


# -- cambios y alcance (06 §2.4, modo commit) ----------------------------------------------------


@dataclass(frozen=True)
class Change:
    status: str  # letra de git: A, M, D, T o R<nn>
    path: str
    previous: str | None = None


def _pathspecs(paths: Sequence[str] | None, root: Path, cwd: str, ci_fs: bool) -> list[str]:
    """``paths`` como rutas canónicas del repositorio (``E_VALIDATION_INPUT`` si salen de él)."""
    out: list[str] = []
    for index, raw in enumerate(paths or ()):
        prepared = prepare(raw, root, cwd)
        try:
            if prepared is None:
                raise OutsideRepo(raw)
            rel = canonical(prepared, str(root), ci_fs)
        except (InvalidPath, OutsideRepo):
            raise _bad("paths", "outside", index=index) from None
        out.append(rel)
    return out


def parse_name_status(raw: bytes) -> list[Change]:
    """``Change`` de la salida de ``git diff --name-status -M -z``."""
    tokens = [t.decode("utf-8", "surrogateescape") for t in raw.split(b"\0")]
    out: list[Change] = []
    i = 0
    while i < len(tokens):
        status = tokens[i]
        if not status:
            i += 1
            continue
        if status[0] in "RC" and i + 2 < len(tokens):
            out.append(Change(status, tokens[i + 2], tokens[i + 1]))
            i += 3
        elif i + 1 < len(tokens):
            out.append(Change(status, tokens[i + 1]))
            i += 2
        else:
            break
    return out


def changes(git: Git, specs: Sequence[str]) -> list[Change]:
    """Lo que guardaría ``save``: cambios rastreados frente a ``HEAD`` y archivos nuevos."""
    tracked = git.run("--literal-pathspecs", "diff", "--name-status", "-M", "-z", "--no-color", "--no-ext-diff",
                      "--no-textconv", "HEAD", "--", *specs)
    found = parse_name_status(tracked.stdout)
    seen = {c.path for c in found}
    new = git.run("--literal-pathspecs", "ls-files", "--others", "--exclude-standard", "-z", "--", *specs)
    for raw in new.stdout.split(b"\0"):
        path = raw.decode("utf-8", "surrogateescape")
        if path and path not in seen:
            found.append(Change("A", path))
    return sorted(found, key=lambda c: c.path)


def _generated_matches(ctx: OpContext, root: Path) -> bool:
    """Los archivos ``generated`` coinciden con su regeneración (06 §2.2; como ``sync-roles``)."""
    from ..vcs.tree import worktree
    from .rules.roles_sync import compare_files, is_skill_path, regenerate, tree_reader

    view = worktree(root, git=ctx.git())
    read, exists = tree_reader(view)
    regen = regenerate(read)
    if regen.expected is None:
        return False
    states = compare_files(read, regen.expected, exists=exists, normalize_eol=True)
    return all(s.status == "same" for s in states) and not any(is_skill_path(p) for p in view.entries())


def scope_check(ctx: OpContext, git: Git, snap: BaseSnapshot, work: Work, specs: Sequence[str]) -> list[Change]:
    """Paso ``scope_check``: ``E_PERM_PATH_DENIED`` con ``details.paths[]`` si algo se deniega."""
    found = changes(git, specs)
    check_changes(ctx, git, snap, work, found)
    return found


def check_changes(ctx: OpContext, git: Git, snap: BaseSnapshot, work: Work, found: Sequence[Change]) -> None:
    """``check_path`` en modo ``commit`` de cada cambio (06 §2.4); ``E_PERM_PATH_DENIED`` si algo se
    deniega. Los ``generated`` valen para el ``lead`` si coinciden con su regeneración."""
    if not found:
        return
    project = ctx.project()
    assert project is not None
    root = project.root
    ci_fs = case_insensitive(root)
    pol = to_policy(snap, root=str(root), ci_fs=ci_fs, case_map=case_map(git) if ci_fs else None,
                    link_probe=link_probe(root))
    task = work.task()
    matches: bool | None = None
    denied: list[dict[str, object]] = []
    for ch in found:
        touched = [ch.path, *([ch.previous] if ch.previous else [])]
        if matches is None and any(classify(p, frozenset(pol.sections), symlinked=False).cls == "generated"
                                   for p in touched):
            matches = _generated_matches(ctx, root)
        for d in check_change(work.me, ch.status, ch.path, ch.previous, task, pol=pol, mode="commit",
                              generated_matches=bool(matches)):
            if not d.allow:
                denied.append({"path": d.path or ch.path, "reason_code": d.code or "E_INTERNAL_UNEXPECTED"})
    if denied:
        raise OpError("E_PERM_PATH_DENIED", details={"paths": denied})


# -- commit, integración y push ------------------------------------------------------------------


@dataclass(frozen=True)
class Committed:
    sha: str
    files: tuple[str, ...]


def _sections(found: Sequence[Change], declared: frozenset[str]) -> list[str]:
    """``Section`` de 11 §2.5: las partes de ``sections/<id>/**`` que modifica el commit."""
    out: set[str] = set()
    for ch in found:
        for path in (ch.path, ch.previous):
            if path:
                info = classify(path, declared, symlinked=False)
                if info.cls == "section" and info.section:
                    out.add(info.section)
    return sorted(out)


def commit_work(git: Git, snap: BaseSnapshot, work: Work, found: Sequence[Change], specs: Sequence[str], *,
                message: str, trace: Trace) -> Committed | None:
    """Paso ``commit``: ``git add -A`` de lo que se guarda y el commit con los trailers."""
    if not found:
        return None
    git.run("--literal-pathspecs", "add", "-A", "--", *(specs or ["."]), read=False)
    block = trailer_block(task=work.number, sections=_sections(found, frozenset(snap.roles.section_owner())),
                          ai_use=trace.ai_use, assisted=trace.assisted)
    text = commit_message(message, block)
    args = ["--literal-pathspecs", "commit", "--quiet", "--cleanup=whitespace", "-F", "-"]
    if specs:
        args += ["--", *specs]  # solo esas rutas; lo demás que estuviera en el índice queda ahí
    proc = git.run(*args, input=text.encode("utf-8"), check=False, read=False)
    if proc.returncode != 0:
        raise OpError("E_CONFIG_GIT_UNAVAILABLE", details={"reason": "commit_failed"})
    sha = git.rev_parse("HEAD")
    assert sha is not None
    files = sorted({ch.path for ch in found} | {ch.previous for ch in found if ch.previous})
    return Committed(sha, tuple(files))


def merge_message(source: str, local: str, number: int) -> str:
    """Mensaje de un commit de fusión del núcleo: solo el trailer ``Task`` (11 §2.5)."""
    return f"Merge {source} into {local}\n\nTask: #{number}\n"


def dropped_warning(ctx: OpContext, work: Work, outcome: MergeOutcome, t: Translator) -> WarningObj | None:
    """Guarda el diff descartado por ``auto_base`` y arma ``W_STATE_OUT_OF_SCOPE_DROPPED``."""
    if not outcome.dropped:
        return None
    project = ctx.project()
    assert project is not None
    rel = write_dropped(ctx.store().root, project.root, work.number, ctx.inv.now(), outcome.patch or b"")
    return make_warning("W_STATE_OUT_OF_SCOPE_DROPPED", t, details={"paths": list(outcome.dropped), "patch": rel})


def conflict_error(outcome: MergeOutcome, extra: dict[str, object] | None = None) -> OpError:
    """``E_STATE_MERGE_CONFLICT`` con la fusión en curso y ``data.conflicts`` (04 §6.2 paso 6)."""
    data: dict[str, object] = {"conflicts": [c.to_dict() for c in outcome.conflicts], "resolvable": True}
    data.update(extra or {})
    return OpError("E_STATE_MERGE_CONFLICT", details={"paths": [c.path for c in outcome.pending]}, data=data)


def integrate(ctx: OpContext, git: Git, snap: BaseSnapshot, work: Work, t: Translator,
              warnings: list[WarningObj]) -> MergeOutcome:
    """Fusiona ``origin/task/<n>`` en la local (REQ-TASK-49); conflicto ``agent``: error."""
    project = ctx.project()
    assert project is not None
    source = f"origin/{work.branch}"
    outcome = merge(git, f"refs/remotes/{source}", message=merge_message(source, work.local, work.number),
                    scope=work.scope(snap), root=project.root)
    dropped = dropped_warning(ctx, work, outcome, t)
    if dropped is not None:
        warnings.append(dropped)
    if outcome.status == "conflict":
        raise conflict_error(outcome)
    return outcome


def push(ctx: OpContext, git: Git, snap: BaseSnapshot, work: Work, t: Translator,
         warnings: list[WarningObj]) -> bool:
    """Paso ``push``: sin forzar; si otro push se adelantó, integra y reintenta una vez (04 §11.3).

    ``True`` si subió algo. Sin red, ``False`` y ``W_NET_OFFLINE`` (el commit queda en local).
    """
    result = push_task(git, work.number)
    if result.outcome == "non_ff":
        status = fetch_task(git, work.number)
        if status == "missing":  # la liberaron entre el fetch y el push (REQ-TASK-32)
            raise OpError("E_CLAIM_NOT_HELD", details={"task": work.number, "reason": "ref_missing"})
        if status == "ok":
            check_claim(git, work.number)
            integrate(ctx, git, snap, work, t, warnings)
            result = push_task(git, work.number)
    if result.outcome == "non_ff":
        raise OpError("E_FORGE_PUSH_REJECTED", details={"reason": "non_fast_forward"})
    if result.outcome == "rejected":
        raise OpError("E_FORGE_PUSH_REJECTED", details={"reason": result.reason})
    if result.outcome == "offline":
        warnings.append(make_warning("W_NET_OFFLINE", t))
        return False
    if result.outcome == "accepted":  # la ref de seguimiento queda en lo que se subió
        head = git.rev_parse("HEAD")
        if head is not None:
            git.run("update-ref", f"refs/remotes/origin/{work.branch}", head, check=False, read=False)
    return result.outcome == "accepted"


def integrate_and_push(ctx: OpContext, git: Git, snap: BaseSnapshot, work: Work, t: Translator,
                       warnings: list[WarningObj]) -> bool:
    """``integrate_remote`` y ``push`` de ``save`` para el ``save_local`` de otra operación."""
    integrate(ctx, git, snap, work, t, warnings)
    ahead, _behind = counts(git, f"refs/remotes/origin/{work.branch}")
    return push(ctx, git, snap, work, t, warnings) if ahead else False


def save_and_push(ctx: OpContext, git: Git, snap: BaseSnapshot, work: Work, t: Translator,
                  warnings: list[WarningObj], *, key: str) -> bool:
    """El ``save`` completo del paso ``save_local`` de ``switch`` y ``release``.

    Alcance, commit con un mensaje del núcleo y sin declaración de uso (``undeclared``,
    REQ-TRACE-12), y la subida, porque esas operaciones dejan el trabajo en el remoto
    (REQ-TASK-45). Sin red el commit queda en local y se avisa. ``True`` si subió algo.
    """
    if merge_in_progress(ctx):
        raise OpError("E_STATE_MERGE_IN_PROGRESS")
    found = scope_check(ctx, git, snap, work, [])
    trace = commit_trace(ctx, ai_use=None, ai_tool=None, ai_model=None)
    warnings.extend(trace_warnings(trace, t))
    commit_work(git, snap, work, found, [], message=auto_message(snap, t, key), trace=trace)
    remote = sync_remote(ctx, git, snap.project.base_branch, work.number)
    if remote.offline:
        warnings.append(make_warning("W_NET_OFFLINE", t))
        return False
    return integrate_and_push(ctx, git, snap, work, t, warnings)


def renew(ctx: OpContext, work: Work, steps: Steps, *, offline: bool) -> None:
    """Paso ``renew``: si la caché dice ``expired``, quita ``state:expired`` (reflejo)."""
    if work.state != "expired":
        steps.skip("renew")
        return
    if offline:
        steps.skip("renew", "offline")
        return
    done = steps.reflect("renew", lambda: ctx.forge().remove_label(work.number, EXPIRED))
    if done is not None:  # el estado sin la etiqueta lo rehace status; aquí basta con que no venza
        snap = ctx.base()
        write_task_cache(ctx.store(), work.number, snap.project.forge_repo, claimant=work.me,
                         state="claimed", sections=work.sections, now=ctx.inv.now())


def auto_message(snap: BaseSnapshot, t: Translator, key: str) -> str:
    """Mensaje del ``save`` que hace otra operación, en el idioma del proyecto."""
    lang = translator(snap.project.language) if snap.project.language in LANGS else t
    return lang(f"tasks.save.auto.{key}")


# -- operación -------------------------------------------------------------------------------------


def _check_input(message: object, paths: object) -> tuple[str, list[str] | None]:
    if not isinstance(message, str) or not message.strip():
        raise _bad("message", "minLength" if isinstance(message, str) else "type")
    if len(message) > MESSAGE_MAX:
        raise _bad("message", "maxLength")
    if paths is not None:
        if not isinstance(paths, (list, tuple)) or not all(isinstance(p, str) and p for p in paths):
            raise _bad("paths")
        if len(paths) > PATHS_MAX:
            raise _bad("paths", "maxItems")
    return message, list(paths) if paths else None


def run(ctx: OpContext, *, message: object = None, ai_use: object = None, ai_tool: object = None,
        ai_model: object = None, paths: object = None) -> Result:
    """``save -m <mensaje> [--ai-use <a,b>] [--ai-tool <slug>] [--ai-model <modelo>] [--path <ruta>]...``."""
    text, raw_paths = _check_input(message, paths)
    uses, tool, model = check_trace_input(ai_use, ai_tool, ai_model)
    t = ctx.t()
    project = ctx.project()
    assert project is not None
    snap = ctx.base()
    git = ctx.git()
    warnings: list[WarningObj] = []
    if snap.fallback_sha is not None:
        warnings.append(make_warning("W_CONFIG_POLICY_FALLBACK", t, details=snap.fallback_details()))
    trace = commit_trace(ctx, ai_use=uses, ai_tool=tool, ai_model=model)
    specs = _pathspecs(raw_paths, project.root, ctx.inv.cwd, case_insensitive(project.root))
    base = snap.project.base_branch
    steps = Steps(ctx, OP, STEPS)

    def preflight() -> tuple[Work, Remote]:
        work = resolve_work(ctx, git, snap)
        if merge_in_progress(ctx):
            raise OpError("E_STATE_MERGE_IN_PROGRESS")
        return work, sync_remote(ctx, git, base, work.number)

    work, remote = steps.run("preflight", preflight)
    if remote.offline:
        warnings.append(make_warning("W_NET_OFFLINE", t))
    found = steps.run("scope_check", lambda: scope_check(ctx, git, snap, work, specs))
    if steps.dry_run:
        lang = translator(snap.project.language) if snap.project.language in LANGS else t
        if found:
            steps.effect("git_commit", work.local, lang("tasks.effect.save_commit", n=len(found)))
        if not remote.offline:
            steps.effect("git_push", work.branch, lang("tasks.effect.push", task=work.number))
        if work.state == "expired" and not remote.offline:
            steps.effect("forge_update", f"issue#{work.number}", lang("tasks.effect.renew"))
        return steps.dry_result(warnings)

    committed: Committed | None = None
    if found:
        committed = steps.run("commit", lambda: commit_work(git, snap, work, found, specs, message=text, trace=trace))
        warnings.extend(trace_warnings(trace, t))  # los avisos de la traza son del commit que se hizo
    else:
        steps.skip("commit", "no_changes")
    pushed = False
    if remote.offline:
        steps.skip("integrate_remote", "offline")
        steps.skip("push", "offline")
    else:
        steps.run("integrate_remote", lambda: integrate(ctx, git, snap, work, t, warnings))
        ahead, _behind = counts(git, f"refs/remotes/origin/{work.branch}")
        if ahead:
            pushed = steps.run("push", lambda: push(ctx, git, snap, work, t, warnings))
        else:
            steps.skip("push", "up_to_date")
    renew(ctx, work, steps, offline=remote.offline)
    warnings.extend(steps.warnings(t))
    if remote.offline:  # sin push el reclamo no se renueva (W_CLAIM_EXPIRING, 05 §9.14)
        warnings.extend(_expiring(ctx, snap, work.number, t))
    ahead, behind = counts(git, f"refs/remotes/origin/{work.branch}")
    data: dict[str, object] = {
        "task": work.number,
        "branch": work.branch,
        "commit": {"sha": committed.sha, "files": list(committed.files)} if committed is not None else None,
        "pushed": pushed,
        "ahead": ahead,
        "behind": behind,
    }
    if committed is not None:
        _fit_files(data, warnings, t)
    return ok(OP, data, warnings)


def _expiring(ctx: OpContext, snap: BaseSnapshot, number: int, t: Translator) -> list[WarningObj]:
    """``W_CLAIM_EXPIRING`` sin red, con la caché del tablero: el commit quedó en local y el reclamo
    no se renovó. Con red, el push lo renueva."""
    from .board import expiring_warnings, load_cached

    repo = snap.project.forge_repo
    cached = load_cached(ctx.store(), repo) if repo else None
    if cached is None:
        return []
    days = snap.project.limits.get("claim_expiry_days", 14)
    expiry = days if isinstance(days, int) and not isinstance(days, bool) and days > 0 else 14
    rows = [r for r in cached.tasks if r.number == number]
    return expiring_warnings(t, rows, cached.login, ctx.inv.now(), expiry)


def _fit_files(data: dict[str, object], warnings: list[WarningObj], t: Translator) -> None:
    """Tope de 05 §7.1: si no cabe, recorta ``commit.files`` y avisa ``W_OUTPUT_TRUNCATED``."""
    if envelope_size(OP, data, warnings) <= BUDGET:
        return
    commit = data["commit"]
    assert isinstance(commit, dict)
    files = list(commit["files"])

    def notice(omitted: int) -> WarningObj:
        return make_warning("W_OUTPUT_TRUNCATED", t, details={
            "budget": BUDGET, "trimmed": ["commit.files"], "omitted": {"commit.files": omitted}})

    worst = notice(len(files))  # el aviso más largo: se mide con él para no pasarse

    def fits(k: int) -> bool:
        commit["files"] = files[:k]
        return envelope_size(OP, data, [*warnings, worst]) <= BUDGET

    lo, hi = 0, len(files)
    while lo < hi:  # el mayor número de rutas que cabe
        mid = (lo + hi + 1) // 2
        if fits(mid):
            lo = mid
        else:
            hi = mid - 1
    commit["files"] = files[:lo]
    warnings.append(notice(len(files) - lo))
