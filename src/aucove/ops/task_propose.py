# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove task propose`` · ``propose_task`` (05 §9.10).

Pasos: ``find_existing`` busca, entre los Issues del actor con ``task``, el que lleva el
marcador ``op`` con ``id=task-propose-<k>``; si no hay, ``create_issue`` (canónico) lo crea
con ``task`` y ``state:proposed`` y el cuerpo del formulario de 03-esquemas §8.2 en el idioma
del proyecto; el adaptador agrega el marcador (§6.1). La tarea queda ``proposed`` aunque la
proponga el responsable: aceptarla es una acción humana (D-09).

``k = sha256(login + "\\n" + casefold(strip(title)) + "\\n" + ",".join(sorted(sections)))[:16]``,
o ``idempotency_key`` si se indica. El texto libre (``body`` o ``--body-file``) es el resumen de la
tarea, y ``done``, su criterio de terminado: las dos cosas que REQ-SCHEMA-69 exige a un cuerpo válido.
El resumen no lleva los encabezados del formulario (``### Summary``...): repetiría un campo y la tarea
quedaría ilegible (03 §8.3), como pasó en una sesión de prueba del 20/10 con un ``### Definition of done``.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from .. import _meta
from ..domain.marked import TITLE_MAX
from ..domain.plan import parse_plan
from ..domain.taskbody import CANON, MAX_DONE, MAX_SUMMARY, render_task_body
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
BODY_MAX = MAX_SUMMARY  # el cuerpo es el resumen de la tarea (03 REQ-SCHEMA-69)
DONE_ITEM_MAX = 500  # cada condición del criterio de terminado, en una línea
_FORM_HEADING = re.compile(r"^### (.+?)\s*$")  # como el de domain.taskbody
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
    if any((m := _FORM_HEADING.match(line)) and m[1] in CANON for line in text.split("\n")):
        raise _bad("body", "form_heading")  # repetiría un campo del formulario (03 §8.3)
    return text


def _done_items(done: object) -> list[str]:
    """Las condiciones de ``done``: de 1 a ``MAX_DONE``, cada una de una línea (03 REQ-SCHEMA-69)."""
    if not isinstance(done, (list, tuple)) or not done or len(done) > MAX_DONE:
        raise _bad("done")
    items = [x.strip() if isinstance(x, str) else "" for x in done]
    if not all(items) or any("\n" in x or "\r" in x or len(x) > DONE_ITEM_MAX for x in items):
        raise _bad("done")
    return items


def _ids(raw: bytes) -> dict[str, str]:
    text = raw.decode("utf-8", "replace").lstrip("\N{ZERO WIDTH NO-BREAK SPACE}").replace("\r\n", "\n")
    items, _errors = parse_plan(text)
    return {i.id: i.status for i in items}


def plan_ids(ctx: OpContext, snap: BaseSnapshot) -> dict[str, str]:
    """``id -> status`` de ``plan.md`` de la base; sin plan legible, ninguno."""
    if not snap.sha:
        return {}
    try:
        raw = ctx.git().show(snap.sha, "plan.md")
    except Exception:  # noqa: BLE001 - sin plan legible no hay IDs
        return {}
    return _ids(raw) if raw is not None else {}


def _forge_plan_ids(ctx: OpContext, snap: BaseSnapshot) -> dict[str, str] | None:
    """``id -> status`` de ``plan.md`` en la cabeza de la base en la forja, si va delante de la copia local.

    ``None`` si es la misma cabeza o no se puede leer (sin red, sin forja): decide la copia local. Solo
    lee, así que vale igual con ``--dry-run`` (REQ-OPS-30).
    """
    try:
        forge = ctx.forge()
        head = forge.get_ref(snap.project.base_branch)
        if head is None or head.sha == snap.sha:
            return None
        found = forge.get_file("plan.md", head.sha)
    except Exception:  # noqa: BLE001 - sin la forja decide la copia local
        return None
    return _ids(found.content) if found is not None else {}


def check_plan_refs(ctx: OpContext, snap: BaseSnapshot, refs: Sequence[str]) -> list[str]:
    """``refs`` sin repetir; ``E_VALIDATION_UNKNOWN_PLAN_ID`` si alguno no existe o está retirado
    en ``plan.md`` de la base (03 REQ-SCHEMA-44; 05 §9.10 y §12.1).

    Un ID que la copia local de la base no tiene se busca también en la base de la forja: tras integrar
    un cambio al plan, ``origin/<base>`` queda atrás hasta el siguiente fetch, y en una sesión de prueba
    (20/10/2026) el agente tuvo que traerla a mano para proponer tareas con los IDs nuevos.
    """
    wanted = list(dict.fromkeys(refs))
    if not wanted:
        return wanted
    plan = plan_ids(ctx, snap)
    bad = [r for r in wanted if plan.get(r) != "active"]
    if bad and (fresh := _forge_plan_ids(ctx, snap)) is not None:
        bad = [r for r in bad if fresh.get(r) != "active"]
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
    done: object = None,
    plan_refs: object = None,
    idempotency_key: object = None,
) -> Result:
    """``task propose --title <t> --section <id>... --body <resumen> | --body-file <f> --done <condición>...
    [--plan-ref <ID>]...``."""
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
    if not text:
        raise _bad("body", "required")  # un resumen vacío deja la tarea inválida (REQ-SCHEMA-69)
    items = _done_items(done)
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
                                 "done": items}, lang)
    if steps.dry_run:
        steps.effect("forge_create", "issue", title.strip())
        return steps.dry_result()
    created = steps.run("create_issue", lambda: forge.create_issue(
        title.strip(), rendered, labels=[TASK_LABEL, PROPOSED], op_id=op_id))
    return ok(OP, {"task": {"number": created.number, "url": created.url, "state": "proposed"},
                   "created": True, "idempotency_key": key})
