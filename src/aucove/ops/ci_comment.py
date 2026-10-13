# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Subcomando interno ``ci comment`` (09-ci REQ-CI-20, REQ-CI-27; DC-23).

Publica o actualiza **un único** comentario por PR y tipo, identificado por su
marcador (``_meta.ci_marker(kind)``) en la primera línea y por el autor esperado
(``github-actions[bot]``), y agrega el mismo texto al resumen del job. No emite el
sobre. Tipos de H2:

- ``build``: resultado de la compilación, enlaces al PDF, al diff y al run, y los
  primeros 10 diagnósticos de ``<report-dir>/diagnostics.json``. Sin ese
  archivo, informa «diagnóstico no disponible».
- ``validate``: resumen del reporte de ``validate --mode pr``. Sin reporte legible,
  informa «validación no disponible» y sale con 0: el veredicto lo da el último
  paso del workflow (REQ-CI-8).
- ``expire-claims`` (desde H3, ``expire-claims.yml`` de 09-ci §6): resumen del sobre de
  ``expire-claims`` (05 §9.19): las tareas avisadas, vencidas, renovadas, reparadas y
  omitidas. No es de un PR: no lleva ``--pr``, no llama a la forja y solo escribe el resumen
  del job; sin reporte legible, lo dice y sale con 0 (el veredicto es el de ``expire-claims``).

``--skip-reason`` (solo ``build``) es la salida ``skip_reason`` de ``compile.yml``. Con un
motivo, ``ci comment`` escribe en stdout una anotación ``::notice::`` que lo explica. Con
``format-none`` (sin manuscrito) solo escribe el resumen del job y la anotación, y no
comenta en el PR (REQ-CI-49); con ``no-changes`` el comentario dice el motivo.

Todo texto que llega del PR o de un reporte (rutas, mensajes, sugerencias, ramas)
va escapado dentro de código, en una línea, con los caracteres invisibles escritos
como ``U+XXXX`` y truncado a 200 caracteres; así no genera menciones ``@`` ni
Markdown (REQ-CI-27). Del directorio actual no se lee nada: el idioma llega por
``--language`` o del ``project.yaml`` de ``--policy``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

from .. import _meta
from ..domain.text import same_login
from ..forge.base import ForgeError, ForgePermissionError
from ..i18n import LANGS, Lang, Translator, resolve_lang, translator
from .ci_plan import EXIT_ERROR, EXIT_OK, EXIT_USAGE, REASON_FORMAT_NONE, SKIP_REASONS, read_regular_file, say
from .diagnostics import Diagnostic, load_diagnostics
from .registry import CiCommand
from .result import OpError, read_envelope

if TYPE_CHECKING:
    import argparse

    from ..forge.base import Comment, ForgeAdapter
    from .context import OpContext

KINDS: tuple[str, ...] = ("build", "validate", "expire-claims")
PR_KINDS: tuple[str, ...] = ("build", "validate")  # los que comentan en un PR (exigen --pr)
# Listas de data_expire_claims, en el orden del resumen (05 §9.19).
EXPIRE_LISTS: tuple[str, ...] = ("warned", "expired", "unexpired", "repaired", "skipped")
MAX_EXPIRE_ITEMS = 50  # tareas por lista en el resumen
# Valores de steps.<id>.outcome de Actions; '' si el paso no llegó a correr.
OUTCOMES: tuple[str, ...] = ("success", "failure", "cancelled", "skipped", "")
BOT_LOGIN = "github-actions[bot]"  # autor de los comentarios hechos con GITHUB_TOKEN
FLAG_INSTRUCTIONS = "flag:instructions"  # REQ-SEC-16, 09-ci §4.4

MAX_TEXT = 200  # REQ-CI-27
MAX_BUILD_DIAGNOSTICS = 10  # REQ-CI-20
MAX_VALIDATE_FINDINGS = 30
MAX_BODY_CHARS = 60000  # GitHub admite 65 536 caracteres por comentario
MAX_URL = 2000
DIAGNOSTICS_FILE = "diagnostics.json"

_URL_RE = re.compile(r"https://[A-Za-z0-9._~:/?#!$&*+,;=%-]+")
_CODE_RE = re.compile(r"conv\.[a-z0-9-]+\.[a-z0-9-]+|[EW]_[A-Z]+(_[A-Z0-9]+)+|(IL|TX|SB)[0-9]{2}")
_HEX_RE = re.compile(r"[0-9a-f]{7,64}")
_ELLIPSIS = "…"
_INVISIBLE = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
# Marcador op que agrega la forja al final del cuerpo de un comentario creado (08 §4.7, 00-marco §5.14).
_OP_TAIL_RE = re.compile(r"\n*<!-- " + re.escape(_meta.NAME) + r":op v1 id=[A-Za-z0-9._-]+ -->\s*\Z")


# -- escape del texto que viene del PR ---------------------------------------------


def visible_text(text: object, limit: int = MAX_TEXT) -> str:
    """Texto en una línea, con los invisibles como ``U+XXXX`` y de ``limit`` caracteres o menos."""
    raw = text if isinstance(text, str) else str(text)
    chars: list[str] = []
    for ch in raw:
        if ch in "\t\n\r":
            chars.append(" ")
            continue
        cat = unicodedata.category(ch)
        if cat in _INVISIBLE:
            chars.append(f"U+{ord(ch):04X}")
        elif cat == "Zs":
            chars.append(" ")
        else:
            chars.append(ch)
    flat = " ".join("".join(chars).split())
    if len(flat) > limit:
        flat = flat[: limit - 1] + _ELLIPSIS
    return flat


def inline_code(text: object, limit: int = MAX_TEXT) -> str:
    """Fragmento de código de Markdown con ``text`` escapado (REQ-CI-27).

    Dentro de código, GitHub no crea menciones, enlaces ni HTML. La valla es más
    larga que cualquier racha de acentos graves del texto.
    """
    content = visible_text(text, limit)
    if not content:
        return "` `"
    longest = max((len(m) for m in re.findall(r"`+", content)), default=0)
    fence = "`" * (longest + 1)
    if content.startswith("`") or content.endswith("`"):
        content = f" {content} "
    return f"{fence}{content}{fence}"


def safe_url(url: object) -> str | None:
    """``url`` si es una URL https sin caracteres que rompan un enlace de Markdown; si no, ``None``."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL:
        return None
    return url if _URL_RE.fullmatch(url) else None


def _link(label: str, url: object, t: Translator) -> str:
    good = safe_url(url)
    if good is None:
        return f"{label}: {t('ci.comment.unavailable_value')}"
    return f"[{label}]({good})"


def _plural(t: Translator, key: str, n: int) -> str:
    return t.plural(key, n, n=n)


# -- comentario de build -----------------------------------------------------------


def _where(path: object, line: object) -> str:
    if not isinstance(path, str) or not path:
        return ""
    if isinstance(line, int) and not isinstance(line, bool) and line >= 1:
        return inline_code(f"{path}:{line}") + ": "
    return inline_code(path) + ": "


def _omitted(path: Path) -> int:
    """``details.omitted`` del aviso ``W_OUTPUT_TRUNCATED`` del sobre; 0 si no hay."""
    env = read_envelope(path)
    warnings = env.get("warnings") if env else None
    if not isinstance(warnings, list):
        return 0
    for w in warnings:
        if isinstance(w, Mapping) and w.get("code") == "W_OUTPUT_TRUNCATED":
            details = w.get("details")
            n = details.get("omitted") if isinstance(details, Mapping) else None
            if isinstance(n, int) and not isinstance(n, bool) and n > 0:
                return n
    return 0


def render_build(
    t: Translator,
    *,
    outcome: str,
    pdf_url: str | None,
    diff_url: str | None,
    run_url: str | None,
    diagnostics: Sequence[Diagnostic] | None,
    omitted: int = 0,
    skip_reason: str = "",
) -> str:
    """Cuerpo del comentario ``build`` (REQ-CI-20), con el marcador en la primera línea.

    Una compilación ``skipped`` con un motivo conocido (``skip_reason``) lo dice en el título
    y en la primera línea, en lugar del texto genérico de ``skipped``.
    """
    key = outcome if outcome in OUTCOMES and outcome else "unknown"
    reason = skip_reason if key == "skipped" and skip_reason in SKIP_REASONS else ""
    check = f"{_meta.NAME}/build"
    result = t(f"ci.comment.build.outcome.{reason or key}")
    lines = [_meta.ci_marker("build"), "", f"### {t('ci.comment.build.title', check=check, result=result)}", ""]
    if reason:
        lines += [t(f"ci.comment.build.reason.{reason}"), ""]
    lines.append("- " + _link(t("ci.comment.build.pdf"), pdf_url, t))
    if safe_url(diff_url) is not None:
        lines.append("- " + _link(t("ci.comment.build.diff"), diff_url, t))
    lines.append("- " + _link(t("ci.comment.build.run"), run_url, t))
    if key != "skipped":
        lines += ["", f"#### {t('ci.comment.build.diagnostics')}", ""]
        if diagnostics is None:
            lines.append(t("ci.comment.build.diagnostics_unavailable"))
        elif not diagnostics:
            lines.append(t("ci.comment.build.no_diagnostics"))
        else:
            shown = list(diagnostics[:MAX_BUILD_DIAGNOSTICS])
            for d in shown:
                severity = t(f"ci.comment.severity.{d.severity}")
                lines.append(f"- **{severity}** {_where(d.file, d.line)}{inline_code(d.message)} ({inline_code(d.rule)})")
            total = len(diagnostics) + max(omitted, 0)
            if total > len(shown):
                lines += ["", t("ci.comment.shown", shown=len(shown), total=total)]
    if safe_url(pdf_url) is not None or safe_url(diff_url) is not None:
        lines += ["", t("ci.comment.artifact_note")]
    return "\n".join(lines) + "\n"


_LINK_ITEM = re.compile(r"- \[[^\]\n]*\]\(([^)\s]+)\)")


def build_pdf_url(body: str) -> str | None:
    """El enlace del PDF de un comentario ``build``: la primera viñeta de ``render_build``."""
    for line in body.splitlines():
        if line.startswith("- "):
            m = _LINK_ITEM.fullmatch(line.strip())
            return safe_url(m.group(1)) if m else None  # sin enlace: no estaba disponible
    return None


def render_no_manuscript(t: Translator) -> str:
    """Resumen del job con ``format: none`` (REQ-CI-49): el check y el motivo; no hay comentario."""
    check = f"{_meta.NAME}/build"
    title = t("ci.comment.build.title", check=check, result=t(f"ci.comment.build.outcome.{REASON_FORMAT_NONE}"))
    return f"### {title}\n\n{t(f'ci.comment.build.reason.{REASON_FORMAT_NONE}')}\n"


def notice_line(t: Translator, reason: str) -> str:
    """Anotación ``::notice::`` de Actions con el motivo de la compilación omitida (REQ-CI-49).

    Mensaje y título van escapados como datos y propiedad de un comando de flujo: ningún
    salto de línea puede empezar otro comando.
    """
    def data(text: str) -> str:
        return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")

    title = data(f"{_meta.NAME}/build").replace(":", "%3A").replace(",", "%2C")
    return f"::notice title={title}::{data(t(f'ci.comment.build.reason.{reason}'))}\n"


# -- comentario de validate --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Finding:
    check: str
    severity: str
    code: str
    path: str | None
    line: int | None
    message: str
    hint: str | None


@dataclass(frozen=True, slots=True)
class _Rule:
    check: str
    status: str
    findings: tuple[_Finding, ...]


@dataclass(frozen=True, slots=True)
class ValidateReport:
    """Lo que el comentario usa del sobre de ``validate`` (05 §9.5)."""

    valid: bool
    rules: tuple[_Rule, ...]
    head_ref: str | None
    head_sha: str | None


@dataclass(frozen=True, slots=True)
class ValidateIncomplete:
    """El sobre no trae ``data``: la validación terminó con un error propio."""

    code: str
    message: str


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _parse_finding(check: str, raw: object) -> _Finding | None:
    if not isinstance(raw, Mapping):
        return None
    severity, code, message = raw.get("severity"), raw.get("code"), raw.get("message")
    path, line, hint = raw.get("path"), raw.get("line"), raw.get("hint")
    if severity not in ("error", "warning") or not isinstance(code, str) or not isinstance(message, str):
        return None
    if path is not None and not isinstance(path, str):
        return None
    if line is not None and (isinstance(line, bool) or not isinstance(line, int) or line < 1):
        return None
    if hint is not None and not isinstance(hint, str):
        return None
    return _Finding(check, str(severity), code, path, line, message, hint)


def parse_validate_report(env: Mapping[str, object] | None) -> ValidateReport | ValidateIncomplete | None:
    """Datos del sobre de ``validate``; ``None`` si falta o no tiene la forma esperada."""
    if env is None or env.get("op") != "validate":
        return None
    data = env.get("data")
    if data is None:
        err = env.get("error")
        if isinstance(err, Mapping) and isinstance(err.get("code"), str):
            return ValidateIncomplete(str(err["code"]), _opt_str(err.get("message")) or "")
        return None
    if not isinstance(data, Mapping) or not isinstance(data.get("results"), list):
        return None
    rules: list[_Rule] = []
    for raw in data["results"]:
        if not isinstance(raw, Mapping):
            return None
        check, status, findings = raw.get("check"), raw.get("status"), raw.get("findings")
        if not isinstance(check, str) or not check or status not in ("pass", "fail", "skip"):
            return None
        if not isinstance(findings, list):
            return None
        parsed = [_parse_finding(check, f) for f in findings]
        if any(p is None for p in parsed):
            return None
        rules.append(_Rule(check, str(status), tuple(p for p in parsed if p is not None)))
    head = data.get("head")
    head_ref = _opt_str(head.get("ref")) if isinstance(head, Mapping) else None
    head_sha = _opt_str(head.get("sha")) if isinstance(head, Mapping) else None
    valid = data.get("valid") is True and env.get("ok") is True
    return ValidateReport(valid, tuple(rules), head_ref, head_sha)


def needs_instructions_flag(report: ValidateReport) -> bool:
    """REQ-SEC-16: algún ``warning`` de ``instructions-lint`` o de SB07 en ``settings-integrity``."""
    for rule in report.rules:
        for f in rule.findings:
            if f.severity != "warning":
                continue
            if f.check == "instructions-lint" or (f.check == "settings-integrity" and f.code == "SB07"):
                return True
    return False


def _code_text(code: str) -> str:
    return inline_code(code if _CODE_RE.fullmatch(code) else visible_text(code))


def _finding_block(t: Translator, f: _Finding) -> list[str]:
    severity = t(f"ci.comment.severity.{f.severity}")
    head = (
        f"- **{severity}** {_where(f.path, f.line)}{inline_code(f.message)} "
        f"({_code_text(f.code)}, {inline_code(f.check)})"
    )
    block = [head]
    if f.hint:
        block.append("  - " + t("ci.comment.hint", hint=inline_code(f.hint)))
    return block


def render_validate(t: Translator, report: ValidateReport | ValidateIncomplete | None) -> str:
    """Cuerpo del comentario ``validate`` (REQ-CI-27), con el marcador en la primera línea."""
    check = f"{_meta.NAME}/validate"
    marker = _meta.ci_marker("validate")

    def title(result: str) -> str:
        return f"### {t('ci.comment.validate.title', check=check, result=t(f'ci.comment.validate.result.{result}'))}"

    if report is None:
        body = [marker, "", title("unavailable"), "", t("ci.comment.validate.unavailable")]
        return "\n".join(body) + "\n"
    if isinstance(report, ValidateIncomplete):
        body = [marker, "", title("incomplete"), "", t("ci.comment.validate.incomplete", code=_code_text(report.code))]
        if report.message:
            body += ["", inline_code(report.message)]
        return "\n".join(body) + "\n"

    all_findings = [f for rule in report.rules for f in rule.findings]
    errors = sum(1 for f in all_findings if f.severity == "error")
    warnings = len(all_findings) - errors
    lines = [marker, "", title("pass" if report.valid else "fail"), ""]
    if report.head_ref:
        sha = report.head_sha[:7] if report.head_sha and _HEX_RE.fullmatch(report.head_sha) else "?"
        lines += [t("ci.comment.validate.head", ref=inline_code(report.head_ref), sha=inline_code(sha)), ""]
    if not all_findings and report.valid:
        lines.append(t("ci.comment.validate.all_pass"))
    else:
        counts = t(
            "ci.comment.validate.counts",
            errors=_plural(t, "ci.comment.validate.errors", errors),
            warnings=_plural(t, "ci.comment.validate.warnings", warnings),
        )
        lines.append(counts)
    if report.rules:
        lines += ["", f"#### {t('ci.comment.validate.rules')}", ""]
        for rule in report.rules:
            n_err = sum(1 for f in rule.findings if f.severity == "error")
            n_warn = len(rule.findings) - n_err
            parts = []
            if n_err:
                parts.append(_plural(t, "ci.comment.validate.errors", n_err))
            if n_warn:
                parts.append(_plural(t, "ci.comment.validate.warnings", n_warn))
            suffix = f" ({', '.join(parts)})" if parts else ""
            status = t(f"ci.comment.validate.status.{rule.status}")
            lines.append(f"- {inline_code(rule.check)}: {status}{suffix}")
    if all_findings:
        ordered = [f for f in all_findings if f.severity == "error"] + [
            f for f in all_findings if f.severity == "warning"
        ]
        lines += ["", f"#### {t('ci.comment.validate.findings')}", ""]
        size = sum(len(x) + 1 for x in lines)
        shown = 0
        for f in ordered[:MAX_VALIDATE_FINDINGS]:
            block = _finding_block(t, f)
            block_size = sum(len(x) + 1 for x in block)
            if size + block_size > MAX_BODY_CHARS - 2000:  # margen para el pie y el marcador op
                break
            lines += block
            size += block_size
            shown += 1
        if shown < len(ordered):
            lines += ["", t("ci.comment.validate.more", shown=shown, total=len(ordered))]
    return "\n".join(lines) + "\n"


# -- resumen de expire-claims ------------------------------------------------------------


@dataclass(frozen=True)
class ExpireReport:
    """Lo que el resumen usa de ``data_expire_claims`` (05 §9.19)."""

    evaluated: int
    threshold_days: int
    items: Mapping[str, tuple[str, ...]]  # lista -> una línea por tarea, ya escapada


def _task_ref(raw: Mapping[str, object]) -> str | None:
    task = raw.get("task")
    if isinstance(task, bool) or not isinstance(task, int) or task < 1:
        return None
    return f"#{task}"


def parse_expire_report(env: Mapping[str, object] | None, t: Translator) -> ExpireReport | ValidateIncomplete | None:
    """Datos del sobre de ``expire-claims``; ``None`` si falta o no tiene la forma esperada."""
    if env is None or env.get("op") != "expire-claims":
        return None
    data = env.get("data")
    if data is None:
        err = env.get("error")
        if isinstance(err, Mapping) and isinstance(err.get("code"), str):
            return ValidateIncomplete(str(err["code"]), _opt_str(err.get("message")) or "")
        return None
    if not isinstance(data, Mapping):
        return None
    evaluated, days = data.get("evaluated"), data.get("threshold_days")
    if isinstance(evaluated, bool) or not isinstance(evaluated, int) or isinstance(days, bool) or not isinstance(days, int):
        return None
    items: dict[str, tuple[str, ...]] = {}
    for name in EXPIRE_LISTS:
        raw = data.get(name)
        if not isinstance(raw, list):
            return None
        lines: list[str] = []
        for entry in raw:
            ref = _task_ref(entry) if isinstance(entry, Mapping) else None
            if ref is None:
                return None
            assert isinstance(entry, Mapping)
            if name == "repaired":
                detail = t(f"ci.comment.expire.action.{entry.get('action')}") if entry.get("action") in (
                    "assign_owner", "clear_assignees", "remove_stale_expired", "close_merged_issue") else inline_code(entry.get("action"))
            elif name == "skipped":
                reason = entry.get("reason")
                detail = t(f"ci.comment.expire.reason.{reason}") if reason in ("blocked", "no_owner") else inline_code(reason)
            else:
                who = entry.get("claimed_by")
                detail = inline_code(f"@{who}") if isinstance(who, str) and who else t("ci.comment.expire.nobody")
            lines.append(f"{ref} ({detail})")
        items[name] = tuple(lines)
    return ExpireReport(evaluated, days, items)


def render_expire(t: Translator, report: ExpireReport | ValidateIncomplete | None) -> str:
    """Resumen de ``expire-claims`` (sin marcador: no es un comentario de PR)."""
    title = t("ci.comment.expire.title", check=f"{_meta.NAME}/expire-claims")
    lines = [f"### {title}", ""]
    if report is None:
        return "\n".join([*lines, t("ci.comment.expire.unavailable")]) + "\n"
    if isinstance(report, ValidateIncomplete):
        return "\n".join([*lines, t("ci.comment.expire.incomplete", code=_code_text(report.code))]) + "\n"
    lines.append(t("ci.comment.expire.head", tasks=_plural(t, "ci.comment.expire.tasks", report.evaluated),
                   days=report.threshold_days))
    if not any(report.items.values()):
        return "\n".join([*lines, "", t("ci.comment.expire.nothing")]) + "\n"
    lines.append("")
    for name in EXPIRE_LISTS:
        entries = report.items[name]
        if not entries:
            continue
        shown = ", ".join(entries[:MAX_EXPIRE_ITEMS])
        more = t("ci.comment.shown", shown=MAX_EXPIRE_ITEMS, total=len(entries)) if len(entries) > MAX_EXPIRE_ITEMS else ""
        lines.append(f"- {t(f'ci.comment.expire.list.{name}')}: {shown}" + (f" {more}" if more else ""))
    return "\n".join(lines) + "\n"


# -- publicación -------------------------------------------------------------------


def _first_line(body: str) -> str:
    lines = body.lstrip("﻿").splitlines()
    return lines[0].strip() if lines else ""


def find_ci_comment(comments: Sequence[Comment], kind: str) -> Comment | None:
    """El comentario más antiguo del bot cuya primera línea es el marcador de ``kind``.

    Se compara la primera línea, no una subcadena, para que un texto del PR citado
    dentro de otro comentario del CI no se confunda con su marcador.
    """
    marker = _meta.ci_marker(kind)
    mine = [c for c in comments if same_login(c.author, BOT_LOGIN) and _first_line(c.body) == marker]
    if not mine:
        return None
    return min(mine, key=lambda c: (c.created_at, c.id))


def without_op_marker(body: str) -> str:
    """``body`` sin el marcador ``op`` que el adaptador agrega al final al crear (08 §4.7)."""
    return _OP_TAIL_RE.sub("", body).rstrip("\n")


def upsert_comment(forge: ForgeAdapter, pr: int, kind: str, body: str) -> str:
    """Crea o actualiza el comentario único; devuelve ``'created'``, ``'updated'`` o ``'unchanged'``."""
    existing = find_ci_comment(forge.list_comments(pr), kind)
    if existing is None:
        forge.create_comment(pr, body, op_id=f"ci-{kind}-{pr}")
        return "created"
    if without_op_marker(existing.body) == body.rstrip("\n"):
        return "unchanged"
    forge.update_comment(existing.id, body)
    return "updated"


def summary_text(body: str) -> str:
    """El cuerpo sin la línea del marcador, para ``$GITHUB_STEP_SUMMARY``."""
    first, _sep, rest = body.partition("\n")
    return rest.lstrip("\n") if first.startswith("<!--") else body


def append_summary(path: Path, text: str) -> bool:
    """Agrega ``text`` al resumen del job (UTF-8, LF); ``False`` si no se puede escribir."""
    try:
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
    except (OSError, ValueError):
        return False
    return True


def policy_language(policy: str | None) -> str | None:
    """``language`` del ``project.yaml`` de ``--policy``; ``None`` si no se puede leer o no es válido."""
    if not policy:
        return None
    from ..config.project import parse_project

    data = read_regular_file(Path(policy) / "project.yaml")
    if data is None:
        return None
    try:
        cfg, _findings = parse_project(data, file="project.yaml")
    except Exception:  # un project.yaml ilegible solo deja el idioma por defecto
        return None
    lang = getattr(cfg, "language", None)
    return lang if lang in LANGS else None


def _body_lang(args: argparse.Namespace, ctx: OpContext) -> Lang:
    """``--language`` > ``AUCOVE_LANG`` > ``language`` de ``--policy`` > ``en`` (D-32 e).

    ``build.yml`` pasa ``--language`` (salida de build-plan), que llega vacía si la
    compilación no llegó al plan; ``validate.yml`` pasa ``--policy``.
    """
    project = policy_language(getattr(args, "policy", None)) if not args.language else None
    return resolve_lang(cli=args.language or None, env=ctx.inv.env, project=project)


def _pr_number(value: str) -> int:
    import argparse

    if not value.isascii() or not value.isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError(value)
    return int(value)


def _language(value: str) -> str:
    import argparse

    if value not in ("", *LANGS):
        raise argparse.ArgumentTypeError(value)
    return value


def _outcome(value: str) -> str:
    import argparse

    if value not in OUTCOMES:
        raise argparse.ArgumentTypeError(value)
    return value


def _skip_reason(value: str) -> str:
    import argparse

    if value not in ("", *SKIP_REASONS):
        raise argparse.ArgumentTypeError(value)
    return value


def _add_arguments(p: argparse.ArgumentParser) -> None:
    p.description = "ci.command.comment"
    p.add_argument("--kind", required=True, choices=KINDS, help="ci.comment.arg_kind")
    p.add_argument("--pr", type=_pr_number, metavar="N", help="ci.comment.arg_pr")
    p.add_argument("--language", type=_language, default="", metavar="es|en", help="ci.comment.arg_language")
    p.add_argument("--outcome", type=_outcome, default="", metavar="OUTCOME", help="ci.comment.arg_outcome")
    p.add_argument("--pdf-url", default="", metavar="URL", help="ci.comment.arg_pdf_url")
    p.add_argument("--diff-url", default="", metavar="URL", help="ci.comment.arg_diff_url")
    p.add_argument("--skip-reason", type=_skip_reason, default="", metavar="REASON", help="ci.comment.arg_skip_reason")
    p.add_argument("--run-url", default="", metavar="URL", help="ci.comment.arg_run_url")
    p.add_argument("--report-dir", default="", metavar="DIR", help="ci.comment.arg_report_dir")
    p.add_argument("--policy", default="", metavar="DIR", help="ci.comment.arg_policy")
    p.add_argument("--report", default="", metavar="FILE", help="ci.comment.arg_report")
    p.add_argument("--summary", default="", metavar="FILE", help="ci.comment.arg_summary")


def _body(args: argparse.Namespace, t: Translator) -> tuple[str, ValidateReport | ValidateIncomplete | None]:
    if args.kind == "build":
        diagnostics: list[Diagnostic] | None = None
        omitted = 0
        if args.report_dir:
            path = Path(args.report_dir) / DIAGNOSTICS_FILE
            diagnostics = load_diagnostics(path)
            if diagnostics is not None:
                omitted = _omitted(path)
        body = render_build(
            t,
            outcome=args.outcome,
            pdf_url=args.pdf_url,
            diff_url=args.diff_url,
            run_url=args.run_url,
            diagnostics=diagnostics,
            omitted=omitted,
            skip_reason=args.skip_reason,
        )
        return body, None
    report = parse_validate_report(read_envelope(Path(args.report)))
    return render_validate(t, report), report


def _expire(args: argparse.Namespace, ctx: OpContext, t: Translator) -> int:
    """``--kind expire-claims``: solo el resumen del job (09-ci §6)."""
    body = render_expire(t, parse_expire_report(read_envelope(Path(args.report)), t))
    if args.summary and not append_summary(Path(args.summary), body):
        say(ctx, "ci.comment.summary_failed", path=str(args.summary))
        return EXIT_ERROR
    return EXIT_OK


def main(args: argparse.Namespace, ctx: OpContext, out: TextIO) -> int:
    """``ci comment``: comentario único localizado y resumen del job."""
    if args.kind in PR_KINDS and args.pr is None:
        say(ctx, "ci.comment.pr_required", kind=args.kind)
        return EXIT_USAGE
    if args.kind not in PR_KINDS and args.pr is not None:
        say(ctx, "ci.comment.pr_not_allowed", kind=args.kind)
        return EXIT_USAGE
    if args.kind in ("validate", "expire-claims") and not args.report:
        say(ctx, "ci.comment.report_required", kind=args.kind)
        return EXIT_USAGE
    if args.kind != "build" and args.skip_reason:
        say(ctx, "ci.comment.skip_reason_build_only")
        return EXIT_USAGE
    t = translator(_body_lang(args, ctx))
    if args.kind == "expire-claims":
        return _expire(args, ctx, t)
    if args.skip_reason:
        out.write(notice_line(t, args.skip_reason))
        out.flush()
    if args.skip_reason == REASON_FORMAT_NONE:
        # REQ-CI-49: sin manuscrito no hay nada que comentar; el resultado queda en el resumen.
        say(ctx, "ci.comment.no_manuscript")
        if args.summary and not append_summary(Path(args.summary), render_no_manuscript(t)):
            say(ctx, "ci.comment.summary_failed", path=str(args.summary))
            return EXIT_ERROR
        return EXIT_OK
    body, report = _body(args, t)
    rc = EXIT_OK
    if args.summary and not append_summary(Path(args.summary), summary_text(body)):
        say(ctx, "ci.comment.summary_failed", path=str(args.summary))
        rc = EXIT_ERROR
    try:
        forge = ctx.forge()
        action = upsert_comment(forge, args.pr, args.kind, body)
        say(ctx, f"ci.comment.{action}")
        if isinstance(report, ValidateReport) and needs_instructions_flag(report):
            forge.add_labels(args.pr, [FLAG_INSTRUCTIONS])
            say(ctx, "ci.comment.labeled", label=FLAG_INSTRUCTIONS)
    except OpError as exc:
        say(ctx, "ci.comment.forge_failed", code=exc.code)
        return EXIT_ERROR
    except ForgePermissionError:
        # Token de solo lectura (PR desde un fork, REQ-CI-20): el resultado queda en el resumen.
        say(ctx, "ci.comment.no_permission")
    except ForgeError as exc:
        say(ctx, "ci.comment.forge_error", kind=exc.kind)
        return EXIT_ERROR
    return rc


COMMANDS: tuple[CiCommand, ...] = (CiCommand("comment", _add_arguments, main),)
