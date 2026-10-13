# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove task propose`` · ``propose_task`` (05 §9.10).

Pasos: ``find_existing`` busca, entre los Issues del actor con ``task``, el que lleva el
marcador ``op`` con ``id=task-propose-<k>``; si no hay, ``create_issue`` (canónico) lo crea
con ``task`` y ``state:proposed`` y el cuerpo del formulario de 03-esquemas §8.2 en el idioma
del proyecto; el adaptador agrega el marcador (§6.1). La tarea queda ``proposed`` aunque la
proponga el responsable: aceptarla es una acción humana (D-09).

``k = sha256(login + "\\n" + casefold(strip(title)) + "\\n" + ",".join(sorted(sections)))[:16]``,
o ``idempotency_key`` si se indica. El texto libre (``body`` o ``--body-file``) va como
resumen de la tarea.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from .. import _meta
from ..domain.marked import TITLE_MAX
from ..domain.plan import parse_plan
from ..domain.taskbody import render_task_body
from ..domain.tasks import derive_state
from ..domain.text import same_login
from ..i18n import LANGS
from .result import OpError, Result, ok
from .snapshot import TASK_LABEL, authz_from_roles, collect_task
from .steps import Steps

if TYPE_CHECKING:
    from ..config.base import BaseSnapshot
    from ..forge.base import Issue
    from .context import OpContext

__all__ = ["OP", "check_plan_refs", "default_key", "plan_ids", "run"]

OP = "task.propose"
STEPS = ("find_existing", "create_issue")
BODY_MAX = 8000
PROPOSED = "state:proposed"


def _bad(param: str, reason: str = "type") -> OpError:
    return OpError("E_VALIDATION_INPUT", details={"errors": [{"param": param, "reason": reason}]})


def default_key(login: str, title: str, sections: Sequence[str]) -> str:
    """``k`` por defecto de 05 §9.10."""
    raw = login + "\n" + title.strip().casefold() + "\n" + ",".join(sorted(sections))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _body_text(ctx: OpContext, body: object, body_file: object) -> str:
    if body is not None and body_file is not None:
        raise _bad("body_file", "exclusive")
    if body_file is not None:
        if not isinstance(body_file, str) or not body_file:
            raise _bad("body_file")
        path = Path(body_file)
        if not path.is_absolute():
            path = Path(ctx.inv.cwd) / path
        try:
            raw = path.read_bytes()[: BODY_MAX * 4 + 1]
            text = raw.decode("utf-8-sig")
        except (OSError, UnicodeDecodeError):
            raise _bad("body_file", "unreadable") from None
    elif body is not None:
        if not isinstance(body, str):
            raise _bad("body")
        text = body
    else:
        return ""
    text = text.replace("\r\n", "\n").strip()
    if len(text) > BODY_MAX:
        raise _bad("body", "maxLength")
    return text


def plan_ids(ctx: OpContext, snap: BaseSnapshot) -> dict[str, str]:
    """``id -> status`` de ``plan.md`` de la base; sin plan legible, ninguno."""
    if not snap.sha:
        return {}
    try:
        raw = ctx.git().show(snap.sha, "plan.md")
    except Exception:  # noqa: BLE001 - sin plan legible no hay IDs
        return {}
    if raw is None:
        return {}
    text = raw.decode("utf-8", "replace").lstrip("\N{ZERO WIDTH NO-BREAK SPACE}").replace("\r\n", "\n")
    items, _errors = parse_plan(text)
    return {i.id: i.status for i in items}


def check_plan_refs(ctx: OpContext, snap: BaseSnapshot, refs: Sequence[str]) -> list[str]:
    """``refs`` sin repetir; ``E_VALIDATION_UNKNOWN_PLAN_ID`` si alguno no existe o está retirado
    en ``plan.md`` de la base (03 REQ-SCHEMA-44; 05 §12.1)."""
    wanted = list(dict.fromkeys(refs))
    if not wanted:
        return wanted
    plan = plan_ids(ctx, snap)
    bad = [r for r in wanted if plan.get(r) != "active"]
    if bad:
        raise OpError("E_VALIDATION_UNKNOWN_PLAN_ID", details={"ids": bad})
    return wanted


def run(
    ctx: OpContext,
    *,
    title: object,
    sections: object,
    body: object = None,
    body_file: object = None,
    plan_refs: object = None,
    idempotency_key: object = None,
) -> Result:
    """``task propose --title <t> --section <id>... [--body | --body-file] [--plan-ref <ID>]...``."""
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > TITLE_MAX:
        raise _bad("title")
    if not isinstance(sections, (list, tuple)) or not sections or not all(isinstance(s, str) for s in sections):
        raise _bad("sections")
    refs = list(plan_refs) if isinstance(plan_refs, (list, tuple)) else ([] if plan_refs is None else None)
    if refs is None or not all(isinstance(r, str) for r in refs):
        raise _bad("plan_refs")
    if idempotency_key is not None and not isinstance(idempotency_key, str):
        raise _bad("idempotency_key")
    text = _body_text(ctx, body, body_file)
    ctx.project()
    snap = ctx.base()
    cfg, roles = snap.project, snap.roles
    forge = ctx.forge()
    me = forge.identity().login
    if roles.person(me) is None:
        raise OpError("E_PERM_NOT_A_MEMBER", details={"login": me})
    wanted = list(dict.fromkeys(sections))
    owners = roles.section_owner()
    unknown = [s for s in wanted if s not in owners]
    if unknown:
        raise OpError("E_VALIDATION_UNKNOWN_SECTION", details={"sections": unknown})
    refs = check_plan_refs(ctx, snap, refs)
    key = idempotency_key if isinstance(idempotency_key, str) else default_key(me, title, wanted)
    op_id = f"task-propose-{key}"
    marker = f"<!-- {_meta.NAME}:op v1 id={op_id} -->"
    steps = Steps(ctx, OP, STEPS)

    def find() -> list[Issue]:
        return [i for i in forge.list_issues(labels=[TASK_LABEL], state="all")
                if same_login(i.author, me) and marker in i.body]

    found = steps.run("find_existing", find)
    if found:
        issue = min(found, key=lambda i: i.number)
        steps.skip("create_issue", "exists")
        evidence = collect_task(forge, issue.number, authz_from_roles(roles), base_branch=cfg.base_branch)
        state = derive_state(evidence.snapshot).state if evidence is not None else "proposed"
        return ok(OP, {"task": {"number": issue.number, "url": issue.url, "state": state},
                       "created": False, "idempotency_key": key})
    lang = cfg.language if cfg.language in LANGS else "en"
    rendered = render_task_body({"summary": text, "sections": wanted, "plan_refs": list(dict.fromkeys(refs)),
                                 "done": []}, lang)
    if steps.dry_run:
        steps.effect("forge_create", "issue", title.strip())
        return steps.dry_result()
    created = steps.run("create_issue", lambda: forge.create_issue(
        title.strip(), rendered, labels=[TASK_LABEL, PROPOSED], op_id=op_id))
    return ok(OP, {"task": {"number": created.number, "url": created.url, "state": "proposed"},
                   "created": True, "idempotency_key": key})
