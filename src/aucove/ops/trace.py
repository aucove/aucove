# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Agregación de la traza de una entrega (11-trazabilidad §2.7).

``branch_commits`` lee de git los commits de ``merge-base..HEAD``: si son de fusión, si están
vacíos (mismo árbol que su padre, como el commit de reclamo), qué rutas tocan y sus trailers.
``aggregate`` aplica el pseudocódigo de §2.7 con la declaración de ``submit``:

- cuenta los commits que no son de fusión, no están vacíos y no tocan solo la nota de la
  tarea (REQ-TRACE-18);
- un commit sin ``AI-Use`` cuenta como ``undeclared``, y un ``Co-authored-by`` de
  ``noreply@anthropic.com`` como ``claude-code:unknown`` (REQ-TRACE-29);
- ``E_VALIDATION_AI_USE_UNDECLARED`` (REQ-TRACE-19), ``E_VALIDATION_AI_USE_CONFLICT``
  (REQ-TRACE-20) y ``E_VALIDATION_ASSISTED_BY_MISSING``;
- ``ai_use_by_section`` sale de las rutas de cada commit, no de sus trailers.

Con ``AI-Use: none`` la traza no lleva entradas ``Assisted-by`` (REQ-TRACE-6): las mismas en
la nota, en el objeto ``trace`` y en los trailers del PR (REQ-TRACE-21).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..domain.paths import classify
from ..domain.trailers import ANTHROPIC_NOREPLY, Entry, canonical_uses, parse_trailers
from ..vcs.git import EMPTY_TREE, parse_commit
from .result import OpError

if TYPE_CHECKING:
    from ..vcs.git import Git

__all__ = ["Aggregate", "CommitTrace", "aggregate", "branch_commits", "sections_of"]

MAX_COMMITS = 1000  # tope de lectura; una rama de tarea no llega


@dataclass(frozen=True)
class CommitTrace:
    """Lo que cuenta de un commit para la traza."""

    sha: str
    merge: bool
    empty: bool  # mismo árbol que su padre
    paths: frozenset[str]
    uses: tuple[str, ...] | None  # None: sin AI-Use válido (undeclared)
    assisted: tuple[tuple[str, str], ...] = ()
    claude_coauthor: bool = False  # Co-authored-by de noreply@anthropic.com (REQ-TRACE-29)


@dataclass(frozen=True)
class Aggregate:
    """La traza de la entrega."""

    ai_use: tuple[str, ...]
    assisted: tuple[Entry, ...]
    by_section: dict[str, tuple[str, ...]] = field(default_factory=dict)
    count: int = 0  # commits de la rama (todos)
    undeclared: int = 0  # commits contados sin declaración

    @property
    def assisted_values(self) -> list[str]:
        """``<tool>:<model>`` en el orden de los trailers (REQ-TRACE-4)."""
        return [e.value for e in self.assisted]


def sections_of(paths: Iterable[str], declared: frozenset[str]) -> set[str]:
    """Partes de ``roles.yaml`` cuyas rutas ``sections/<id>/**`` aparecen en ``paths``."""
    out: set[str] = set()
    for path in paths:
        info = classify(path, declared, symlinked=False)
        if info.cls == "section" and info.section:
            out.add(info.section)
    return out


def branch_commits(git: Git, base_ref: str, head: str = "HEAD") -> tuple[str | None, list[CommitTrace]]:
    """``(merge-base, commits de merge-base..head)`` en orden, del más viejo al más nuevo."""
    base = git.merge_base(base_ref, head)
    rng = [f"{base}..{head}"] if base else [head]
    listed = git.run("rev-list", "--reverse", "--topo-order", f"--max-count={MAX_COMMITS}", *rng, check=False)
    shas = listed.stdout.decode("ascii", "replace").split() if listed.returncode == 0 else []
    if not shas:
        return base, []
    raws = git.cat_file_objects(shas)
    headers = {}
    messages = {}
    for sha in shas:
        found = raws.get(sha)
        if found is None:
            continue
        _info, raw = found
        headers[sha] = parse_commit(raw)
        parts = raw.split(b"\n\n", 1)
        messages[sha] = parts[1].decode("utf-8", "replace") if len(parts) == 2 else ""
    parents = sorted({h.parents[0] for h in headers.values() if h.parents and h.parents[0] not in headers})
    parent_trees = {p: parse_commit(r[1]).tree for p, r in git.cat_file_objects(parents).items() if r is not None}
    out: list[CommitTrace] = []
    for sha in shas:
        header = headers.get(sha)
        if header is None:
            continue
        merge = len(header.parents) > 1
        first = header.parents[0] if header.parents else None
        parent_tree = (headers[first].tree if first in headers else parent_trees.get(first or "")) if first else EMPTY_TREE
        empty = parent_tree is not None and header.tree == parent_tree
        paths: frozenset[str] = frozenset()
        if not merge and not empty:
            changes = git.diff_tree_raw(first or EMPTY_TREE, sha)
            paths = frozenset(c.path.decode("utf-8", "surrogateescape") for c in changes)
        t = parse_trailers(messages.get(sha, ""))
        out.append(CommitTrace(
            sha=sha, merge=merge, empty=empty, paths=paths, uses=t.ai_use, assisted=t.assisted,
            claude_coauthor=ANTHROPIC_NOREPLY in t.co_authors,
        ))
    return base, out


def aggregate(
    commits: Sequence[CommitTrace],
    *,
    note_path: str,
    declared: frozenset[str],
    decl_uses: Sequence[str] | None,
    decl_entries: Sequence[Entry],
) -> Aggregate:
    """§2.7: ``AI-Use``, ``Assisted-by`` y ``ai_use_by_section`` de la entrega."""
    relevant = [c for c in commits if not c.merge and not c.empty and c.paths != frozenset({note_path})]
    uses: set[str] = set()
    entries: dict[tuple[str, str], Entry] = {}
    by_section: dict[str, set[str]] = {}
    undeclared = 0
    for c in relevant:
        cu = set(c.uses) if c.uses else {"undeclared"}
        if not c.uses or "undeclared" in c.uses:
            undeclared += 1
        for tool, model in c.assisted:
            entries.setdefault((tool, model), Entry(tool, model, "declared", "declared"))
        if c.claude_coauthor:  # REQ-TRACE-29: asistencia, nunca autoría
            entries.setdefault(("claude-code", "unknown"), Entry("claude-code", "unknown", "co-authored-by", "none"))
        uses |= cu
        for s in sections_of(c.paths, declared):
            by_section.setdefault(s, set()).update(cu)
    concrete = uses - {"none", "undeclared"}
    declared_uses = set(decl_uses or ())
    if "undeclared" in uses and not declared_uses:
        raise OpError("E_VALIDATION_AI_USE_UNDECLARED", details={"commits": undeclared})
    if declared_uses and not concrete <= declared_uses - {"none"}:
        raise OpError("E_VALIDATION_AI_USE_CONFLICT", details={"missing": sorted(concrete - declared_uses)})
    final = declared_uses or concrete or {"none"}
    sections: dict[str, tuple[str, ...]] = {}
    for s, values in sorted(by_section.items()):
        values = (values - {"undeclared"}) | (final if "undeclared" in values else set())
        sections[s] = canonical_uses((values - {"none"}) or {"none"})
    for e in decl_entries:  # las de la llamada traen el origen de la detección (§2.6)
        entries[(e.tool, e.model)] = e
    if final == {"none"}:
        entries = {}  # REQ-TRACE-6: sin Assisted-by con AI-Use: none
    known = {tool for tool, model in entries if model != "unknown"}
    kept = sorted((e for (tool, model), e in entries.items() if model != "unknown" or tool not in known),
                  key=lambda e: (e.tool, e.model))
    if final - {"none"} and not kept:
        raise OpError("E_VALIDATION_ASSISTED_BY_MISSING")
    return Aggregate(canonical_uses(final), tuple(kept), sections, len(commits), undeclared)
