# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``aucove direction propose`` · ``propose_direction`` (05 §9.21; 04-tareas §9, REQ-TASK-72).

Pasos: ``find_existing`` busca, entre los Issues del actor con ``direction``, el que lleva el
marcador ``op`` con ``id=direction-propose-<k>``; si no hay, ``create_issue`` (canónico) crea un
Issue con la etiqueta ``direction`` que cita los IDs del plan, y el adaptador agrega el
marcador (05 §6.1). ``plan.md`` no se toca: es de clase ``lead``. Si se acepta, el ``lead`` crea
y reclama una tarea en la que edita ``plan.md`` y registra la decisión.

``k = sha256(login + "\\n" + kind + "\\n" + casefold(strip(title)))[:16]``. Con ``kind`` distinto de
``add``, ``plan_refs`` lleva al menos un ID (``E_VALIDATION_INPUT``); todos deben estar
vigentes en ``plan.md`` de la base (``E_VALIDATION_UNKNOWN_PLAN_ID``). 03-esquemas no fija el
cuerpo: lleva el tipo de cambio, el cambio, la justificación y los IDs, en el idioma del
proyecto, con el texto de la persona neutralizado (``prbody.user_text``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import TYPE_CHECKING

from .. import _meta
from ..domain.marked import TITLE_MAX
from ..domain.text import same_login
from ..i18n import LANGS, Translator, translator
from .prbody import user_text
from .result import OpError, Result, ok
from .steps import Steps
from .task_propose import check_plan_refs

if TYPE_CHECKING:
    from ..forge.base import Issue
    from .context import OpContext

__all__ = ["DIRECTION_LABEL", "OP", "STEPS", "default_key", "render_body", "run"]

OP = "direction.propose"
STEPS = ("find_existing", "create_issue")
DIRECTION_LABEL = "direction"
KINDS = ("add", "modify", "retire")
TEXT_MAX = 4000
REFS_MAX = 20


def _bad(param: str, reason: str = "type") -> OpError:
    return OpError("E_VALIDATION_INPUT", details={"errors": [{"param": param, "reason": reason}]})


def default_key(login: str, kind: str, title: str) -> str:
    """``k`` de 05 §9.21."""
    raw = login + "\n" + kind + "\n" + title.strip().casefold()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _bad(name, "minLength" if isinstance(value, str) else "type")
    if len(value) > TEXT_MAX:
        raise _bad(name, "maxLength")
    return value.strip()


def _check_input(title: object, kind: object, change: object, rationale: object,
                 plan_refs: object) -> tuple[str, str, str, str, list[str]]:
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > TITLE_MAX:
        raise _bad("title")
    if kind not in KINDS:
        raise _bad("kind", "enum")
    refs = list(plan_refs) if isinstance(plan_refs, (list, tuple)) else ([] if plan_refs is None else None)
    if refs is None or not all(isinstance(r, str) for r in refs):
        raise _bad("plan_refs")
    if len(refs) > REFS_MAX:
        raise _bad("plan_refs", "maxItems")
    if kind != "add" and not refs:  # modificar o retirar cita lo que cambia (05 §9.21)
        raise _bad("plan_refs", "minItems")
    return title.strip(), str(kind), _text("change", change), _text("rationale", rationale), refs


def render_body(lang: Translator, *, kind: str, change: str, rationale: str, refs: Sequence[str]) -> str:
    """Cuerpo del Issue de dirección, en el idioma del proyecto."""
    ids = "\n".join(f"- {r}" for r in refs) if refs else lang("tasks.direction.no_ids")
    return "\n\n".join([
        lang("tasks.direction.kind_line", kind=lang(f"tasks.direction.kind.{kind}")),
        lang("tasks.direction.change"), user_text(change),
        lang("tasks.direction.rationale"), user_text(rationale),
        lang("tasks.direction.ids"), ids,
    ]) + "\n"


def run(ctx: OpContext, *, title: object = None, kind: object = None, change: object = None,
        rationale: object = None, plan_refs: object = None) -> Result:
    """``direction propose --title <t> --kind add|modify|retire --change <x> --rationale <x> [--plan-ref <ID>]...``."""
    head, kind_s, change_s, rationale_s, refs = _check_input(title, kind, change, rationale, plan_refs)
    t = ctx.t()
    ctx.project()
    snap = ctx.base()
    cfg, roles = snap.project, snap.roles
    forge = ctx.forge()
    me = forge.identity().login
    if roles.person(me) is None:
        raise OpError("E_PERM_NOT_A_MEMBER", details={"login": me})
    refs = check_plan_refs(ctx, snap, refs)
    key = default_key(me, kind_s, head)
    op_id = f"direction-propose-{key}"
    marker = f"<!-- {_meta.NAME}:op v1 id={op_id} -->"
    steps = Steps(ctx, OP, STEPS)

    def find() -> list[Issue]:
        return [i for i in forge.list_issues(labels=[DIRECTION_LABEL], state="all")
                if same_login(i.author, me) and marker in i.body]

    found = steps.run("find_existing", find)
    if found:
        issue = min(found, key=lambda i: i.number)
        steps.skip("create_issue", "exists")
        return ok(OP, {"issue": {"number": issue.number, "url": issue.url}, "created": False,
                       "idempotency_key": key})
    lang = translator(cfg.language) if cfg.language in LANGS else t
    body = render_body(lang, kind=kind_s, change=change_s, rationale=rationale_s, refs=refs)
    if steps.dry_run:
        steps.effect("forge_create", "issue", head)
        return steps.dry_result()
    created = steps.run("create_issue", lambda: forge.create_issue(head, body, labels=[DIRECTION_LABEL], op_id=op_id))
    return ok(OP, {"issue": {"number": created.number, "url": created.url}, "created": True, "idempotency_key": key})
