# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Paquete de contexto del coordinador (04-tareas §8.2; ``$defs/context_package``).

Lo devuelven ``claim`` (fase ``claim``), ``submit`` (fase ``submission``) y ``context --task``.
Es la capa detectiva determinista de 04 §8.1: hechos sin modelo para que la sesión evalúe el
traslape. Todo texto va como ``marked_text`` (REQ-TASK-55): Issues y ramas no integradas,
``untrusted``; plan, ``rules``; partes y decisiones de la base, ``reviewed``.

- ``task``: la tarea, con ``summary`` y ``done`` de su ``TaskSpec`` (máx. 2000; REQ-SEC-7).
- ``plan``: los elementos de ``plan.md`` que cita la tarea.
- ``active_tasks``: las otras tareas activas, con los archivos que tocan
  (``compare:<base>...task/<k>``, máx. 50) y nada de sus cuerpos (REQ-TASK-56).
- ``related_sections``: las partes de las otras tareas activas y, con LaTeX, las demás
  partes de la misma sección del artículo (REQ-TASK-58), con dueño, ``\\label``,
  ``\\newcommand`` y un extracto de 600.
- ``recent_decisions``: las decisiones vigentes más recientes (hasta 10).
- ``deterministic_findings``: partes compartidas con otra tarea activa (``shared_section``),
  archivos de las partes propias que otra rama activa tocó (``same_file_active``) y la base
  rota (``main_broken``).
- ``diff_summary`` (solo en la fase ``submission``): archivos, líneas agregadas y quitadas, y un
  extracto del diff de la tarea frente a la base (hasta 2000), calculados con git local.

``trim_package`` recorta en el orden de REQ-TASK-59 (extractos, decisiones más allá de las
5 más recientes, tareas activas sin partes ni IDs en común) y cuenta lo quitado en
``omitted``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..domain.marked import TITLE_MAX, SanitizeTally, marked
from ..domain.plan import parse_plan
from ..domain.tasks import ACTIVE
from ..forge.base import ForgeError
from .base_docs import read_base_docs, read_sections

if TYPE_CHECKING:
    from ..config.base import BaseSnapshot
    from ..forge.base import ForgeAdapter
    from .board import Board, TaskView
    from .context import OpContext

__all__ = ["KEEP_DECISIONS", "PACKAGE_DECISIONS", "Target", "build_package", "common_tasks", "trim_package"]

PACKAGE_DECISIONS = 10
KEEP_DECISIONS = 5  # REQ-TASK-59: las más allá de 5 se recortan primero
SPEC_MAX = 2000  # REQ-SEC-7
SUMMARY_MAX = 280  # REQ-SEC-8
CHANGED_MAX = 50  # 05 §7.2
EXCERPT_MAX = 600  # REQ-TASK-58
DIFF_MAX = 2000  # extracto del diff en la fase submission (04 §8.2)


@dataclass(frozen=True)
class Target:
    """La tarea del paquete."""

    number: int
    state: str
    owner: str | None
    sections: tuple[str, ...]  # efectivas (04 §3)
    plan_refs: tuple[str, ...]
    title: str
    summary: str
    done: tuple[str, ...]


def _plan(ctx: OpContext, snap: BaseSnapshot, refs: Sequence[str], tally: SanitizeTally) -> list[dict[str, object]]:
    if not refs or not snap.sha:
        return []
    try:
        raw = ctx.git().show(snap.sha, "plan.md")
    except Exception:  # noqa: BLE001 - sin plan legible, la lista va vacía
        return []
    if raw is None:
        return []
    text = raw.decode("utf-8", "replace").lstrip("\N{ZERO WIDTH NO-BREAK SPACE}").replace("\r\n", "\n")
    items, _errors = parse_plan(text)
    by_id = {i.id: i for i in items}
    return [
        {"id": ref, "status": by_id[ref].status,
         "text": marked("rules", f"plan.md#{ref}", by_id[ref].text, limit=SPEC_MAX, tally=tally)}
        for ref in refs
        if ref in by_id
    ]


def _changed(forge: ForgeAdapter | None, base: str, row: TaskView) -> dict[str, object]:
    source = f"compare:{base}...task/{row.number}"
    if forge is None or row.branch is None:
        return {"source": source, "paths": []}
    try:
        comparison = forge.compare(base, row.branch)
    except ForgeError:  # sin la comparación el paquete sigue; la evaluación decide
        return {"source": source, "paths": []}
    paths = sorted({f.path for f in comparison.files})
    return {"source": source, "paths": paths[:CHANGED_MAX]}


def _diff_summary(ctx: OpContext, snap: BaseSnapshot, number: int, tally: SanitizeTally) -> dict[str, object] | None:
    """``diff_summary`` de la fase ``submission``: el diff de la tarea frente a la base, con git."""
    try:
        git = ctx.git()
        base = snap.project.base_branch
        branch = f"task/{number}"
        head = f"refs/remotes/origin/{branch}"
        if git.rev_parse(head) is None:
            head = "HEAD" if git.current_branch() == branch else ""
        mb = git.merge_base(f"refs/remotes/origin/{base}", head) if head else None
        if mb is None:
            return None
        stat = git.run("diff", "--numstat", "--no-color", "--no-ext-diff", "--no-textconv", mb, head, check=False)
        patch = git.run("diff", "--no-color", "--no-ext-diff", "--no-textconv", "-U1", mb, head, check=False)
    except Exception:  # noqa: BLE001 - sin el diff, el paquete sigue sin resumen
        return None
    files = additions = deletions = 0
    for line in stat.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        files += 1
        additions += int(parts[0]) if parts[0].isdigit() else 0  # '-' en un binario
        deletions += int(parts[1]) if parts[1].isdigit() else 0
    text = patch.stdout.decode("utf-8", "replace")
    return {
        "files": files,
        "additions": additions,
        "deletions": deletions,
        "excerpt": marked("untrusted", f"compare:{base}...{branch}", text, limit=DIFF_MAX, tally=tally),
    }


def build_package(
    ctx: OpContext,
    forge: ForgeAdapter | None,
    *,
    phase: str,
    target: Target,
    board: Board | None,
    snap: BaseSnapshot,
    tally: SanitizeTally,
) -> dict[str, Any]:
    """``context_package`` de la fase ``phase`` para ``target``."""
    cfg, roles = snap.project, snap.roles
    base = cfg.base_branch
    owners = roles.section_owner()
    n = target.number
    active = sorted(
        (r for r in (board.tasks if board is not None else ()) if r.state in ACTIVE and r.number != n),
        key=lambda r: r.number,
    )
    changed = {r.number: _changed(forge, base, r) for r in active}
    active_tasks = [
        {
            "number": r.number,
            "state": r.state,
            "owner": r.owner,
            "sections": list(r.sections),
            "pr": r.pr.number if r.pr is not None and r.pr.state == "open" else None,
            "title": marked("untrusted", f"issue#{r.number}.title", r.title, limit=TITLE_MAX, tally=tally),
            "changed_files": changed[r.number],
        }
        for r in active
    ]
    docs = read_base_docs(ctx.git(), snap, decisions=PACKAGE_DECISIONS, latex=cfg.format == "latex")
    own = set(target.sections)
    related_ids: list[str] = []
    for r in active:
        related_ids.extend(s for s in r.sections if s not in own and s not in related_ids)
    if cfg.format == "latex":  # las demás partes de la misma sección del artículo (D-55)
        outlines = {docs.outline_of.get(s) for s in own} - {None}
        related_ids.extend(
            s for s, outline in sorted(docs.outline_of.items())
            if outline in outlines and s not in own and s not in related_ids
        )
    section_docs = read_sections(ctx.git(), snap, related_ids)
    related = []
    for sid in related_ids:
        doc = section_docs.get(sid)
        source = doc.excerpt_source if doc is not None and doc.excerpt_source else f"sections/{sid}/"
        related.append({
            "id": sid,
            "owner": owners.get(sid),
            "path": f"sections/{sid}/",
            "labels": list(doc.labels) if doc is not None else [],
            "macros": list(doc.macros) if doc is not None else [],
            "excerpt": marked("reviewed", source, doc.excerpt if doc is not None else "", limit=EXCERPT_MAX,
                              tally=tally),
        })
    decisions = [
        {
            "file": d.path,
            "title": marked("reviewed", f"{d.path}#title", d.title, limit=TITLE_MAX, tally=tally),
            "summary": marked("reviewed", f"{d.path}#summary", d.summary, limit=SUMMARY_MAX, tally=tally),
            "plan_ids": list(d.plan_refs),
        }
        for d in docs.decisions
    ]
    findings: list[dict[str, object]] = []
    for sid in target.sections:
        sharing = [r.number for r in active if sid in r.sections]
        if sharing:
            findings.append({"kind": "shared_section", "path": f"sections/{sid}/", "tasks": sharing})
    touched: dict[str, list[int]] = {}
    for r in active:
        for path in changed[r.number]["paths"]:  # type: ignore[union-attr]
            if any(str(path).startswith(f"sections/{sid}/") for sid in own):
                touched.setdefault(str(path), []).append(r.number)
    findings.extend({"kind": "same_file_active", "path": p, "tasks": ks} for p, ks in sorted(touched.items()))
    if board is not None and board.base.main_broken_issue is not None:
        findings.append({"kind": "main_broken", "path": None, "tasks": []})
    package: dict[str, Any] = {
        "phase": phase,
        "task": {
            "number": n,
            "state": target.state,
            "owner": target.owner,
            "sections": list(target.sections),
            "title": marked("untrusted", f"issue#{n}.title", target.title, limit=TITLE_MAX, tally=tally),
            "spec": {
                "summary": marked("untrusted", f"issue#{n}.summary", target.summary, limit=SPEC_MAX, tally=tally),
                "done": marked("untrusted", f"issue#{n}.done", "\n".join(target.done), limit=SPEC_MAX, tally=tally),
            },
        },
        "plan": _plan(ctx, snap, target.plan_refs, tally),
        "active_tasks": active_tasks,
        "related_sections": related,
        "recent_decisions": decisions,
        "deterministic_findings": findings,
        "omitted": {"active_tasks": 0, "related_sections": 0, "recent_decisions": 0},
    }
    if phase == "submission":
        summary = _diff_summary(ctx, snap, n, tally)
        if summary is not None:
            package["diff_summary"] = summary
    return package


def common_tasks(target: Target, board: Board | None) -> frozenset[int]:
    """Tareas activas con alguna parte o algún ID de plan en común con ``target`` (REQ-TASK-59)."""
    sections, refs = set(target.sections), set(target.plan_refs)
    return frozenset(
        r.number for r in (board.tasks if board is not None else ())
        if r.state in ACTIVE and r.number != target.number and (sections & set(r.sections) or refs & set(r.plan_refs))
    )


def trim_package(package: dict[str, Any], *, fits: Callable[[], bool], keep: frozenset[int]) -> bool:
    """Recorta ``package`` en su lugar hasta que ``fits()``; ``True`` si recortó algo.

    Orden de REQ-TASK-59: los extractos (de la última parte a la primera), las decisiones
    más allá de las 5 más recientes y las tareas activas sin partes ni IDs de plan en común.
    Si aún no cabe, sigue con las partes relacionadas, las demás decisiones y las demás
    tareas activas, para que el sobre quepa siempre.
    """
    if fits():
        return False
    omitted = package["omitted"]
    excerpts = [item["excerpt"] for item in reversed(package["related_sections"])]
    if "diff_summary" in package:  # el extracto del diff, después de los de las partes
        excerpts.append(package["diff_summary"]["excerpt"])
    for excerpt in excerpts:
        if excerpt["text"]:
            excerpt["text"] = ""
            excerpt["truncated"] = True
            if fits():
                return True
    decisions = package["recent_decisions"]
    while len(decisions) > KEEP_DECISIONS and not fits():
        decisions.pop()
        omitted["recent_decisions"] += 1
    if fits():
        return True
    tasks = package["active_tasks"]
    for i in range(len(tasks) - 1, -1, -1):
        if fits():
            return True
        if tasks[i]["number"] not in keep:
            tasks.pop(i)
            omitted["active_tasks"] += 1
    for key in ("related_sections", "recent_decisions", "active_tasks"):
        items = package[key]
        while items and not fits():
            items.pop()
            omitted[key] += 1
    return True
