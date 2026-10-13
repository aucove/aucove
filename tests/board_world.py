# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Mundo de prueba de ``status`` y ``context``: repositorio con la base publicada y un FakeForge.

La base lleva los roles del fixture ``basic`` (ana lead, bruno suplente, carla investigadora)
y las partes ``introduction``, ``methods`` y ``results``. El FakeForge se pasa directo
al ``OpContext`` (sin archivo de estado), así su registro de llamadas sirve para medir el
presupuesto de 08 §7.5.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aucove import _meta
from aucove.domain.taskbody import parse_task_body, render_task_body
from aucove.forge.base import NULL_SHA, LabelEvent, RefActivity, RepoRef
from aucove.forge.fake import FakeClock, FakeForge
from aucove.ops.context import Invocation, OpContext
from aucove.ops.registry import REGISTRY, invoke
from aucove.cli.main import HumanConfirmation
from aucove.ops.result import OpError, Result
from tests.conftest import FIXTURES, GitRepo, isolated_git_env, roles_fixture

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
CI = "github-actions[bot]"
PROJECT = (FIXTURES / "config" / "project" / "minimal.yaml").read_text(encoding="utf-8")
PLAN = (
    "# Plan del proyecto\n\n## Objetivo\n\nEstimar cómo cambió la movilidad diaria.\n\n"
    "## Preguntas de investigación\n\n- RQ-1: ¿Cuánto se redujeron los viajes?\n"
    "- RQ-2 (retired): ~~Pregunta vieja.~~\n\n## Hipótesis\n\n- HYP-1: Baja más en ingresos altos.\n"
)
GUIDE = (
    "# Guía del artículo\n\nTexto general.\n\n## Secciones del artículo\n\n"
    "### Métodos (`tex/outline/methods.tex`)\n- Qué cubre: los datos y el muestreo.\n- Partes: `methods`.\n\n"
    "### Resultados (`tex/outline/results.tex`)\n- Qué cubre: las tablas.\n- Partes: `results`.\n"
)
BASE_FILES: dict[str, str | bytes] = {
    "project.yaml": PROJECT,
    "roles.yaml": roles_fixture("basic"),
    "plan.md": PLAN,
    "tex/guide.md": GUIDE,
    "tex/outline/methods.tex": "\\section{Métodos}\n\\label{sec:methods}\n\n\\input{../sections/methods/methods}\n",
    "tex/outline/results.tex": "\\section{Resultados}\n\\label{sec:results}\n\n\\input{../sections/results/results}\n",
    "sections/methods/methods.tex": "% métodos\n",
    "sections/results/results.tex": "% resultados\n",
    "sections/introduction/introduction.tex": "% intro\n",
    "memory/notes/task-3.md": "# Nota 3\n",
    "memory/notes/task-7.md": "# Nota 7\n",
}


def decision(name: str, *, title: str, date: str, status: str = "accepted", summary: str = "Resumen.",
             supersedes: Sequence[str] = ()) -> tuple[str, str]:
    """Ruta y texto de un registro de decisión (03 §2.5)."""
    sup = "".join(f"\n  - {x}" for x in supersedes)
    text = (
        f"---\nid: {name}\ntitle: {json.dumps(title, ensure_ascii=False)}\ndate: {date}\nauthor: ana\n"
        f"origin: human\ntask: 3\nstatus: {status}\nsummary: {json.dumps(summary, ensure_ascii=False)}\n"
        f"supersedes:{sup if sup else ' []'}\n---\nCuerpo.\n"
    )
    return f"memory/decisions/{name}.md", text


# Las variables que un runner de CI define y que hacen negar las operaciones solo humanas
# (06-permisos §6; 05-operaciones REQ-OPS-49): la persona de las pruebas está en su terminal.
CI_SIGNALS = frozenset({"CI", "GITHUB_ACTIONS"})


def clean_env(env: Mapping[str, str]) -> dict[str, str]:
    drop = (_meta.ENV_PREFIX, "CLAUDE_CODE_", "CLAUDECODE", "GEMINI_CLI")  # sin agente detectado (11 §2.6)
    return {k: v for k, v in env.items() if not k.upper().startswith(drop) and k.upper() not in CI_SIGNALS}


def at(minutes: float) -> datetime:
    return NOW + timedelta(minutes=minutes)


def marker(kind: str, data: Mapping[str, Any]) -> str:
    return f"<!-- {_meta.NAME}:{kind} v1\n{json.dumps(dict(data))}\n-->"


class Typed:
    """La persona en la terminal: teclea el reto, u otra cosa si se indica ``answer``."""

    def __init__(self, answer: str | None = None) -> None:
        self.answer = answer
        self.seen: list[tuple[str, str, str]] = []

    def __call__(self, op: str, target: str, summary: str) -> HumanConfirmation:
        self.seen.append((op, target, summary))
        if self.answer is not None and self.answer != f"{op} {target}":
            raise OpError("E_HUMAN_ONLY_NOT_CONFIRMED")
        return HumanConfirmation(op, NOW)


def human(w: World, op: str, typed: Typed | None = None, *, login: str = "ana", dry_run: bool = False,
          **kwargs: object) -> Result:
    """La operación H desde la CLI con terminal (la fase 1 pasa)."""
    return w.run(op, login=login, dry_run=dry_run, inv={"surface": "cli", "tty": True, "confirm": typed or Typed()},
                 **kwargs)


@dataclass
class World:
    repo: GitRepo
    forge: FakeForge
    base_sha: str
    env: dict[str, str] = field(default_factory=dict)
    now: datetime = NOW

    def as_user(self, login: str) -> FakeForge:
        return self.forge.as_user(login)

    def run(self, op_id: str, *, login: str = "carla", lang: str = "es", forge: FakeForge | None = None,
            env: Mapping[str, str] | None = None, now: datetime | None = None, dry_run: bool = False,
            inv: Mapping[str, Any] | None = None, **kwargs: object) -> Result:
        """``inv``: campos de la ``Invocation`` además de los de MCP (p. ej. ``surface``, ``tty`` y
        ``confirm`` de las operaciones H)."""
        spec = next(s for s in REGISTRY if s.id == op_id)
        when = now or self.now
        fields: dict[str, Any] = {
            "root": str(self.repo.path),
            "lang": lang,
            "json": True,
            "dry_run": dry_run,
            "surface": "mcp",
            "env": {**self.env, **(env or {})},
            "cwd": str(self.repo.path),
            "now": lambda: when,
        }
        fields.update(inv or {})
        inv_obj = Invocation(**fields)
        args = {p.name: p.default for p in spec.params}
        args.update(kwargs)
        return invoke(spec, OpContext(inv_obj, forge=forge if forge is not None else self.as_user(login)), **args)

    def context(self, *, login: str = "carla", offline: bool = False, refresh: bool = False,
                forge: FakeForge | None = None, env: Mapping[str, str] | None = None,
                now: datetime | None = None) -> Result:
        """``get_context.build`` con el parámetro interno ``offline`` (SessionStart)."""
        from aucove.ops import get_context

        when = now or self.now
        inv = Invocation(root=str(self.repo.path), lang="es", json=True, dry_run=False, surface="hook",
                         env={**self.env, **(env or {})}, cwd=str(self.repo.path), now=lambda: when)
        ctx = OpContext(inv, forge=forge if forge is not None else self.as_user(login))
        try:
            counter = itertools.count()
            return get_context.build(ctx, refresh=refresh, offline=offline,
                                     nonce_factory=lambda: f"{next(counter):08x}")
        finally:
            ctx.close()

    # -- siembra ------------------------------------------------------------------------

    def task(
        self,
        number: int,
        *,
        title: str = "Tarea",
        author: str = "carla",
        sections: Sequence[str] = ("methods",),
        plan_refs: Sequence[str] = (),
        accepted: bool = True,
        claimant: str | None = None,
        pr: int | None = None,
        pr_author: str | None = None,
        pr_state: str = "open",
        merged: bool = False,
        merged_at: datetime | None = None,
        draft: bool = False,
        issue_state: str = "open",
        closed_at: datetime | None = None,
        labels: Sequence[str] = (),
        updated: datetime | None = None,
        reviewers: Sequence[str] = (),
        work: bool = True,
        accepter: str = "ana",
    ) -> dict[str, str | None]:
        """Una tarea con sus eventos, su reclamo (ref, commit y marcador) y su PR.

        Con ``work=False`` la rama solo tiene el commit de reclamo.
        """
        f = self.forge
        body = render_task_body(
            {"summary": "Resumen.", "sections": list(sections), "plan_refs": list(plan_refs), "done": ["Hecho."]}, "es"
        )
        spec_hash = str(parse_task_body(body)["spec_hash"])
        events = [LabelEvent(1, "labeled", "task", author, at(-600)), LabelEvent(2, "labeled", "state:proposed",
                                                                                 author, at(-600))]
        all_labels = ["task", *labels]
        if accepted:
            events.append(LabelEvent(3, "unlabeled", "state:proposed", accepter, at(-590)))
        else:
            all_labels.append("state:proposed")
        f.seed_issue(number=number, title=title, body=body, labels=all_labels, author=author,
                     assignees=[claimant] if claimant else [], created_at=at(-600), events=events,
                     state=issue_state, closed_at=closed_at, updated_at=updated or at(-500),
                     state_reason="not_planned" if issue_state == "closed" else None)
        out: dict[str, str | None] = {"claim": None, "head": None}
        if claimant is not None:
            claim = f.seed_commit({"README.md": b"x\n"}, [self.base_fake], f"{_meta.CLAIM_SUBJECT}{number}", claimant,
                                  date=at(-400))
            head = claim
            if work:
                head = f.seed_commit({"README.md": b"x\n", f"sections/{sections[0]}/x.tex": b"y\n"}, [claim],
                                     "trabajo", claimant, date=at(-300))
            f.seed_branch(f"task/{number}", head)
            f.seed_ref_activity(f"task/{number}", [
                RefActivity("branch_creation", claimant, at(-400), NULL_SHA, claim),
                *([RefActivity("push", claimant, at(-300), claim, head)] if work else []),
            ])
            data = {"task": number, "login": claimant, "claim_sha": claim, "base_sha": self.base_fake,
                    "sections": list(sections), "spec_hash": spec_hash, "claimed_at": "2026-10-08T05:20:00Z",
                    "renewed_at": None}
            f.seed_comment(number, author=claimant, body=marker("claim", data), created_at=at(-399))
            out.update(claim=claim, head=head)
            if pr is not None:
                f.seed_pull_request(files=[], number=pr, head_branch=f"task/{number}", head_sha=head,
                                    author=pr_author or claimant, created_at=at(-200), state="closed" if merged else pr_state,
                                    merged=merged, merged_at=merged_at, draft=draft, updated_at=at(-100),
                                    requested_reviewers=list(reviewers))
        return out

    @property
    def base_fake(self) -> str:
        ref = self.forge.get_ref("main")
        assert ref is not None
        return ref.sha


def new_world(tmp: Path, *, extra_files: Mapping[str, str | bytes] | None = None, viewer: str = "carla",
              profile: str = "enforced") -> World:
    """Repositorio con la base publicada en ``origin/main`` y un FakeForge con ``main``."""
    env = isolated_git_env(tmp / "git-env")
    path = tmp / "repo"
    origin = tmp / "origin.git"
    path.mkdir(parents=True)
    repo = GitRepo(path, origin, env)
    repo._run(tmp, ["init", "-q", "--bare", "-b", "main", str(origin)], True)
    repo.git("init", "-q", "-b", "main")
    repo.git("remote", "add", "origin", str(origin))
    sha = repo.commit("base", files={**BASE_FILES, **(extra_files or {})})
    repo.push("main")
    repo.fetch()
    forge = FakeForge(RepoRef("example-org", "demo"), viewer=viewer, clock=FakeClock(NOW), profile=profile)
    for login in ("ana", "bruno", "carla", "dario"):
        forge.seed_user(login, permission="write")
    base = forge.seed_commit({"README.md": b"x\n"}, [], "base", "ana", date=at(-1000))
    forge.seed_branch("main", base)
    return World(repo=repo, forge=forge, base_sha=sha, env=clean_env(env))
