# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Subcomando interno ``ci editor-review`` (09-ci §4.5, REQ-CI-57 a REQ-CI-60; 00-marco D-56).

Lo llama el job ``evaluate`` de ``editor-review.yml``, siempre con la definición de la rama por
defecto: en ``pull_request_target``, al abrir o actualizar el PR, y en ``workflow_run``, cuando
termina ``editor-review-trigger.yml`` tras una revisión. Decide con:

- la política de ``--policy`` (``project.yaml`` y ``roles.yaml`` del checkout de la rama por
  defecto, con el respaldo de REQ-PERM-4, como ``validate``);
- el PR ``--pr``, sus archivos y sus revisiones, leídos por la API; nada del árbol del PR.

La decisión es ``domain.approvals.editor_verdict`` (R2 de 06 §3.2). Con 3000 archivos o más,
el tope de la API, no se puede saber si faltan (REQ-PERM-15): el PR cuenta como si tocara
LaTeX y hace falta la aprobación del editor.

Publica un solo check run ``aucove/editor-review`` en la cabeza del PR, que cada evaluación
actualiza (``upsert_check_run``, REQ-CI-59): lo crea el ``GITHUB_TOKEN``, así que su fuente es
GitHub Actions. Falla cerrado (REQ-CI-8): si la política no se puede leer o la API falla al
leer los archivos o las revisiones, publica ``failure``. Un PR cerrado no se evalúa, y sin el
PR no hay cabeza donde publicar.

Salida: ``conclusion=success|failure|none`` y ``reason=<motivo>`` en stdout; 0 si el check
quedó publicado (aunque sea en ``failure``) o el PR está cerrado, 1 si no se pudo evaluar o
publicar y 3 en un error de uso; nunca 2. El resumen del job lleva el texto del check run, en el
idioma de los mensajes (REQ-ARCH-69). Lo pesado se importa dentro de las funciones: ``cli/ci.py``
carga este módulo para cualquier subcomando ``ci``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from .. import _meta
from ..domain.approvals import ApprovalPolicy, EditorVerdict, editor_verdict, touches_latex
from ..domain.checks import EDITOR_REVIEW
from ..forge.base import ForgeError
from ..i18n import Translator, resolve_lang, translator
from .ci_comment import append_summary
from .ci_plan import EXIT_ERROR, EXIT_OK
from .registry import CiCommand
from .result import OpError

if TYPE_CHECKING:
    import argparse

    from ..config.base import BaseSnapshot
    from ..forge.base import ForgeAdapter, PullRequest
    from .context import OpContext

__all__ = ["COMMANDS", "Evaluation", "evaluate", "main", "render_summary"]

_MAX_PR = 2**31 - 1
# Motivos de error: el check queda en failure y la salida es 1.
_ERRORS: frozenset[str] = frozenset({"policy_unavailable", "forge_error", "internal"})


@dataclass(frozen=True)
class Evaluation:
    """Lo que se publica: la conclusión y su motivo (los de ``editor_verdict`` o un error)."""

    conclusion: str  # success | failure
    reason: str  # EditorReason o uno de _ERRORS
    editor: str | None = None
    code: str | None = None  # el código o el tipo del error de los motivos de _ERRORS


def _say(text: str) -> None:
    try:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()
    except (OSError, UnicodeError, ValueError):
        pass


def _code(exc: BaseException) -> str:
    return exc.code if isinstance(exc, OpError) else str(getattr(exc, "kind", "unexpected"))


def _from_verdict(v: EditorVerdict) -> Evaluation:
    return Evaluation("success" if v.passed else "failure", v.reason, v.editor)


def evaluate(forge: ForgeAdapter, pull: PullRequest, pol: ApprovalPolicy) -> Evaluation:
    """La decisión sobre ``pull`` con la política ``pol``; un error de la API es ``failure``."""
    from .reviews import review_states
    from .rules.scope import MAX_PR_FILES

    try:
        verdict = editor_verdict(pull.author, pull.head_sha, False, [], pol)
        if verdict.reason != "no_latex":  # decide sin leer archivos ni revisiones
            return _from_verdict(verdict)
        files = forge.list_pull_request_files(pull.number)
        paths = [p for f in files for p in (f.path, f.previous_path) if p]
        touches = len(files) >= MAX_PR_FILES or touches_latex(paths)
        if not touches:
            return _from_verdict(verdict)
        reviews = review_states(forge.list_reviews(pull.number))
    except ForgeError as exc:
        return Evaluation("failure", "forge_error", pol.editor, exc.kind)
    return _from_verdict(editor_verdict(pull.author, pull.head_sha, True, reviews, pol))


def render_summary(t: Translator, e: Evaluation, pull: PullRequest) -> str:
    """Texto del check run y del resumen del job (Markdown)."""
    reason = t(f"ci.editor_review.reason.{e.reason}", editor=e.editor or "", code=e.code or "")
    lines = [t("ci.editor_review.headline", check=EDITOR_REVIEW, sha=pull.head_sha[:12], verdict=reason)]
    if e.reason in ("missing", "changes_requested"):
        cli = f"{_meta.NAME} review approve {pull.number}"
        lines.append(t(f"ci.editor_review.hint.{e.reason}", cli=cli, editor=e.editor or ""))
    return "\n\n".join(lines) + "\n"


def _policy(args: argparse.Namespace) -> tuple[BaseSnapshot | None, str | None]:
    from ..config.base import read_policy_dir

    try:
        return read_policy_dir(Path(args.policy)), None
    except OpError as exc:
        return None, exc.code
    except Exception:  # noqa: BLE001 - sin política se publica failure (REQ-CI-8)
        return None, "E_INTERNAL_UNEXPECTED"


def _finish(out: TextIO, conclusion: str, reason: str, code: int) -> int:
    out.write(f"conclusion={conclusion}\nreason={reason}\n")
    out.flush()
    return code


def run(args: argparse.Namespace, ctx: OpContext, out: TextIO) -> int:
    """``ci editor-review``: evalúa el PR ``--pr`` y publica el check en su cabeza."""
    from .reviews import approval_policy

    env = ctx.inv.env
    snap, policy_error = _policy(args)
    language = snap.project.language if snap is not None else None
    t = translator(resolve_lang(cli=None, env=env, project=language))  # mensajes (REQ-ARCH-69)
    try:
        forge = ctx.forge()
        pull = forge.get_pull_request(args.pr)
    except (OpError, ForgeError) as exc:
        _say(t("ci.editor_review.pr_unavailable", pr=args.pr, code=_code(exc)))
        return _finish(out, "none", "pr_unavailable", EXIT_ERROR)
    if pull is None:
        _say(t("ci.editor_review.pr_unavailable", pr=args.pr, code="not_found"))
        return _finish(out, "none", "pr_unavailable", EXIT_ERROR)
    if pull.state != "open":
        _say(t("ci.editor_review.not_open", pr=pull.number))
        return _finish(out, "none", "not_open", EXIT_OK)
    if snap is None:
        e = Evaluation("failure", "policy_unavailable", code=policy_error)
    else:
        try:
            e = evaluate(forge, pull, approval_policy(snap))
        except Exception as exc:  # noqa: BLE001 - REQ-CI-8: un fallo interno publica failure
            e = Evaluation("failure", "internal", code=type(exc).__name__)
    summary = render_summary(t, e, pull)
    rc = EXIT_ERROR if e.reason in _ERRORS else EXIT_OK
    try:
        forge.upsert_check_run(pull.head_sha, EDITOR_REVIEW, conclusion=e.conclusion, summary=summary)
    except ForgeError as exc:
        _say(t("ci.editor_review.publish_failed", check=EDITOR_REVIEW, code=exc.kind))
        rc = EXIT_ERROR
    if args.summary and not append_summary(Path(args.summary), summary):
        _say(t("ci.editor_review.summary_failed", path=str(args.summary)))
    _say(t("ci.editor_review.done", check=EDITOR_REVIEW, sha=pull.head_sha[:12], conclusion=e.conclusion,
           reason=e.reason))
    return _finish(out, e.conclusion, e.reason, rc)


def _pr_number(value: str) -> int:
    import argparse

    if not value.isascii() or not value.isdigit() or value.startswith("0") or int(value) > _MAX_PR:
        raise argparse.ArgumentTypeError(value)
    return int(value)


def _add_arguments(p: argparse.ArgumentParser) -> None:
    p.description = "ci.command.editor-review"
    p.add_argument("--pr", required=True, type=_pr_number, metavar="N", help="ci.editor_review.arg_pr")
    p.add_argument("--policy", required=True, metavar="DIR", help="ci.editor_review.arg_policy")
    p.add_argument("--summary", default="", metavar="FILE", help="ci.editor_review.arg_summary")


def main(args: argparse.Namespace, ctx: OpContext, out: TextIO) -> int:
    """``ci editor-review``: 0, 1 o 3; nunca 2."""
    return run(args, ctx, out)


COMMANDS: tuple[CiCommand, ...] = (CiCommand("editor-review", _add_arguments, main),)
