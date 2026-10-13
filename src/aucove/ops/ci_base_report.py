# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Subcomando interno ``ci base-report`` (09-ci §7.1-7.3, REQ-CI-32..34).

Lo llama el job ``report`` de ``main-broken.yml`` después de compilar y validar
la rama base. Mantiene exactamente un Issue abierto con la etiqueta
``main-broken`` y el marcador ``_meta.ci_marker('main-broken')``:

- base rota (falla la compilación, la validación o la política de la base,
  REQ-CI-46): si no hay Issue lo crea, asignado al ``lead`` y al ``deputy`` de
  ``--policy``; si ya existe, agrega un comentario con el nuevo reporte; si hay
  más de uno, deja el más antiguo y cierra los demás como duplicados;
- base en verde: comenta «resuelto por ``<sha>``» y cierra el Issue;
- resultado no concluyente (``cancelled``, o una validación ``skipped``, sin
  ninguna falla): no toca nada. Una compilación ``skipped`` sí es una falla (ver
  ``_build_failed``), salvo en una ejecución cancelada o con ``--build-reason
  format-none``: sin manuscrito no hay nada que compilar y cuenta como éxito (REQ-CI-49).

Solo cuentan los marcadores ``op`` de los comentarios propios (los del token, que en
CI es ``github-actions[bot]``): los ``op_id`` se calculan con datos públicos y
cualquiera puede comentar en el Issue (08-forja §4.7, 10-seguridad).

Los PR sospechosos (REQ-CI-33) salen de ``last_successful_run``, ``compare`` y
``pulls_for_commit`` del adaptador (00-marco D-34 h). No emite el sobre (DC-23)
y sale con 0, 1 (error o valor inválido: falla cerrada) o 3 (uso); nunca con 2.
El workflow es de H3 (D-31 b): en H2 solo lo ejercitan sus pruebas.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, ParamSpec, TextIO, TypeVar

from .. import _meta
from ..domain.text import same_login
from ..forge.base import ForgeError, ForgeNotFound, ForgeValidationError, PullRequest
from ..i18n import Translator, resolve_lang, translator
from .diagnostics import MAX_MESSAGE
from .registry import CiCommand
from .result import OpError, read_envelope

if TYPE_CHECKING:
    from ..forge.base import ForgeAdapter, Issue
    from .context import OpContext

__all__ = [
    "COMMANDS",
    "LABEL",
    "WORKFLOW_FILE",
    "MAX_FINDINGS",
    "ReportFinding",
    "Suspects",
    "classify",
    "find_suspects",
    "load_findings",
    "main",
]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 3

LABEL = "main-broken"  # etiqueta de 06 §4.6 (la crea init)
MARKER_KIND = "main-broken"  # tipo del marcador de CI (09 §11)
WORKFLOW_FILE = "main-broken.yml"  # último run en verde de REQ-CI-33
BUILD_CHECK = _meta.NAME + "/build-compile"  # nombre del check de compile.yml (09 §3.2)
VALIDATE_CHECK = _meta.NAME + "/base-validate"  # job validate de main-broken.yml (09 §7.1)
BUILD_REPORT = "diagnostics.json"  # artefacto build-report-*
VALIDATE_REPORT = "base-validate.json"  # artefacto base-validate (09 §7.1)

MAX_FINDINGS = 20  # «los primeros 20 hallazgos» (REQ-CI-33)
MAX_SUSPECT_COMMITS = 100  # tope de llamadas a pulls_for_commit por reporte
MAX_SUSPECTS_SHOWN = 50  # tope de PR listados, para no acercarse al límite del cuerpo
MAX_TITLE = 200  # texto de un PR: truncado a 200 caracteres (REQ-CI-27)
MAX_URL = 2000

# Valores de needs.<job>.result y steps.<id>.outcome de Actions. '' = el job compile no
# fijó la salida (p. ej. no llegó a correr), y cuenta como falla. Si falla un paso anterior
# al de TeX, el paso ``tex`` no corre y su outcome es 'skipped', no '' (ver _build_failed).
OUTCOMES: frozenset[str] = frozenset({"success", "failure", "cancelled", "skipped", ""})
_FAILED: frozenset[str] = frozenset({"failure", ""})
# --build-reason: skip_reason de compile.yml, la salida reason de build-plan (09-ci §3.2, §7.1).
FORMAT_NONE = "format-none"  # sin manuscrito: la compilación omitida cuenta como éxito (REQ-CI-49)
BUILD_REASONS: frozenset[str] = frozenset({"", FORMAT_NONE, "no-changes"})

Status = Literal["green", "broken", "inconclusive"]
SuspectStatus = Literal["range", "no_green", "same", "compare_failed", "no_branch", "unavailable"]

_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
# URL conservadora: sin espacios, comillas, paréntesis, corchetes, <>, ` ni \ (va en Markdown).
_URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#@!$&*+,;=%-]+")
_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_BACKTICKS = re.compile(r"`+")

_P = ParamSpec("_P")
_T = TypeVar("_T")


# ---------------------------------------------------------------------------
# Datos
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReportFinding:
    """Un hallazgo leído de los sobres de ``--reports`` (validación o compilación)."""

    check: str
    severity: Literal["error", "warning"]
    path: str | None
    line: int | None
    code: str | None
    message: str


@dataclass(frozen=True, slots=True)
class Suspects:
    """PR sospechosos de REQ-CI-33 y cómo se obtuvieron."""

    status: SuspectStatus
    green_sha: str | None
    pulls: tuple[PullRequest, ...] = ()
    truncated: bool = False
    examined: int = 0


@dataclass(frozen=True, slots=True)
class _Policy:
    """Lo que base-report necesita de ``--policy``."""

    ok: bool  # False: la política actual no valida (REQ-CI-46: la base cuenta como rota)
    language: str | None
    branch: str | None
    assignees: tuple[str, ...]  # lead y deputy con la grafía de people[].login
    fallback_sha: str | None = None  # versión válida anterior usada (REQ-PERM-4), si la hubo


@dataclass(frozen=True, slots=True)
class _Report:
    sha: str
    branch: str | None
    run_url: str
    pdf_url: str
    failed_checks: tuple[str, ...]
    policy_ok: bool
    findings: tuple[ReportFinding, ...]
    findings_total: int
    suspects: Suspects
    policy_fallback: str | None = None
    build_not_run: bool = False  # el job compile falló antes del paso de TeX


class _ForgeStep(Exception):
    """Falla de la forja en un paso que impide garantizar el Issue."""

    def __init__(self, method: str, exc: ForgeError) -> None:
        super().__init__(method)
        self.method = method
        self.kind = exc.kind


# ---------------------------------------------------------------------------
# Lógica pura
# ---------------------------------------------------------------------------


def _no_manuscript(build: str, reason: str) -> bool:
    """Compilación omitida porque la base no tiene manuscrito (``format: none``, REQ-CI-49)."""
    return build == "skipped" and reason == FORMAT_NONE


def _build_failed(build: str, validate: str, reason: str = "") -> bool:
    """¿Falló la compilación de la base? (``--build`` es ``steps.tex.outcome``, 09-ci §3.2 y §7.1).

    En modo ``base``, ``ci build-plan`` emite ``run=true`` salvo con ``format: none``, así
    que un paso ``tex`` en ``skipped`` sin ``--build-reason format-none`` no es una omisión
    legítima: falló un paso anterior del job (build-plan, la descarga de la imagen o la
    instalación del núcleo) y cuenta como falla (REQ-CI-32, fallo cerrado de REQ-CI-8). La
    otra excepción es una ejecución cancelada, que se ve en ``--validate cancelled``.
    """
    if _no_manuscript(build, reason):
        return False
    return build in _FAILED or (build == "skipped" and validate != "cancelled")


def _build_not_run(build: str, validate: str, reason: str = "") -> bool:
    """El paso de TeX no corrió porque falló uno anterior: no hay diagnóstico de LaTeX."""
    return build == "skipped" and _build_failed(build, validate, reason)


def classify(build: str, validate: str, *, policy_ok: bool = True, build_reason: str = "") -> Status:
    """Veredicto de la base: ``broken`` si algo falló, ``green`` si todo pasó, si no ``inconclusive``.

    Con ``build_reason="format-none"`` una compilación ``skipped`` cuenta como éxito (REQ-CI-49).
    """
    if not policy_ok or _build_failed(build, validate, build_reason) or validate in _FAILED:
        return "broken"
    build_ok = build == "success" or _no_manuscript(build, build_reason)
    if build_ok and validate == "success":
        return "green"
    return "inconclusive"


def _op_key(*parts: object) -> str:
    """Clave de idempotencia: 16 hex del SHA-256 de los argumentos (05 §6.1)."""
    return hashlib.sha256("\n".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def _op_marker(op_id: str) -> str:
    """Marcador ``op`` que el adaptador agrega al final del cuerpo (08 §4.7; 00-marco §5.14)."""
    return f"<!-- {_meta.NAME}:op v1 id={op_id} -->"


def _one_line(text: str) -> str:
    """Una línea imprimible: controles y caracteres de formato (Cc, Cf) pasan a espacio."""
    return " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())


def _code(text: str, limit: int) -> str:
    """Texto ajeno como fragmento de código de una línea, truncado y sin @menciones (REQ-CI-27)."""
    flat = _one_line(text)
    if len(flat) > limit:
        flat = flat[: max(limit - 1, 0)].rstrip() + "…"
    flat = flat.replace("@", "@\u200b") or "-"
    longest = max((len(m) for m in _BACKTICKS.findall(flat)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if flat.startswith("`") or flat.endswith("`") else ""
    return f"{fence}{pad}{flat}{pad}{fence}"


def _valid_branch(name: str | None) -> str | None:
    if not name or len(name) > 255 or not _BRANCH_RE.fullmatch(name) or ".." in name:
        return None
    return name


# ---------------------------------------------------------------------------
# Entradas: política y reportes
# ---------------------------------------------------------------------------


def _read_policy(policy_dir: Path, env: Mapping[str, str]) -> _Policy:
    """Política de ``--policy``; si no se puede leer, idioma y rama salen de lo que se pueda."""
    from ..config.base import read_policy_dir

    try:
        snap = read_policy_dir(policy_dir)
    except Exception:  # OpError u otro fallo: REQ-CI-46 pide abrir el Issue igual
        language, branch = _project_only(policy_dir)
        return _Policy(False, language, branch or _valid_branch(env.get("GITHUB_REF_NAME")), ())
    roles = snap.roles
    assignees: list[str] = []
    for login in (roles.lead, roles.deputy):
        if login and all(login.casefold() != other.casefold() for other in assignees):
            assignees.append(login)
    # Con el respaldo de REQ-PERM-4, el archivo actual de la base no valida: la base
    # cuenta como rota (REQ-CI-46), pero el lead y el deputy salen de la versión válida.
    fallback = snap.fallback_sha
    return _Policy(fallback is None, snap.project.language, snap.project.base_branch, tuple(assignees), fallback)


def _project_only(policy_dir: Path) -> tuple[str | None, str | None]:
    """Idioma y rama base del ``project.yaml`` de ``--policy`` aunque ``roles.yaml`` no valide."""
    from ..config.project import parse_project

    try:
        text = (policy_dir / "project.yaml").read_bytes().decode("utf-8-sig")
        cfg, _findings = parse_project(text, file="project.yaml")
    except Exception:  # mejor esfuerzo: la base ya cuenta como rota
        return None, None
    if cfg is None:
        return None, None
    return cfg.language, _valid_branch(cfg.base_branch)


def _envelope_findings(env: Mapping[str, object]) -> list[ReportFinding]:
    """Hallazgos de un sobre de ``validate`` (modo ``base`` o ``build-log``); ignora lo mal formado."""
    if env.get("op") != "validate":
        return []
    data = env.get("data")
    out: list[ReportFinding] = []
    if isinstance(data, Mapping):
        results = data.get("results")
        for result in results if isinstance(results, list) else []:
            if not isinstance(result, Mapping):
                continue
            check = result.get("check")
            findings = result.get("findings")
            if not isinstance(check, str) or not check or not isinstance(findings, list):
                continue
            for f in findings:
                item = _finding(check, f)
                if item is not None:
                    out.append(item)
        return out
    error = env.get("error")
    if isinstance(error, Mapping) and isinstance(error.get("code"), str) and isinstance(error.get("message"), str):
        # Sin data: la validación no llegó a correr (p. ej. E_CONFIG_POLICY_UNAVAILABLE).
        out.append(ReportFinding("validate", "error", None, None, str(error["code"]), str(error["message"])))
    return out


def _finding(check: str, f: object) -> ReportFinding | None:
    if not isinstance(f, Mapping):
        return None
    severity = f.get("severity")
    message = f.get("message")
    path = f.get("path")
    line = f.get("line")
    code = f.get("code")
    if severity not in ("error", "warning") or not isinstance(message, str):
        return None
    if path is not None and not isinstance(path, str):
        path = None
    if isinstance(line, bool) or not isinstance(line, int) or line < 1:
        line = None
    if code is not None and not isinstance(code, str):
        code = None
    return ReportFinding(check, severity, path, line, code, message)


def load_findings(reports: Path | None) -> list[ReportFinding]:
    """Hallazgos de ``diagnostics.json`` y ``base-validate.json``: primero los errores, en orden."""
    if reports is None or not reports.is_dir():
        return []
    found: list[ReportFinding] = []
    for name in (BUILD_REPORT, VALIDATE_REPORT):
        env = read_envelope(reports / name)
        if env is not None:
            found.extend(_envelope_findings(env))
    return sorted(found, key=lambda f: f.severity != "error")  # estable: conserva el orden


# ---------------------------------------------------------------------------
# PR sospechosos (REQ-CI-33)
# ---------------------------------------------------------------------------


def find_suspects(forge: ForgeAdapter, sha: str, branch: str | None) -> Suspects:
    """PR asociados a los commits entre el último SHA en verde de ``main-broken.yml`` y ``sha``.

    Si no hay run en verde previo o la comparación no se puede hacer, se usan los
    PR del propio ``sha``. Un error de la forja deja la lista como no disponible:
    el Issue se garantiza igual.
    """
    if branch is None:
        return Suspects("no_branch", None)
    try:
        run = forge.last_successful_run(WORKFLOW_FILE, branch)
        green = run.head_sha if run is not None else None
        truncated = False
        status: SuspectStatus
        if green is None:
            status, shas = "no_green", [sha]
        elif green == sha:
            return Suspects("same", green)
        else:
            try:
                cmp = forge.compare(green, sha, with_commits=True)
            except ForgeNotFound:  # p. ej. el último verde desapareció con un force push
                status, shas = "compare_failed", [sha]
            else:
                status = "range"
                shas = [c.sha for c in cmp.commits]
                truncated = cmp.ahead_by > len(shas)
                if len(shas) > MAX_SUSPECT_COMMITS:
                    shas = shas[-MAX_SUSPECT_COMMITS:]
                    truncated = True
                if sha not in shas:
                    shas.append(sha)
        pulls: dict[int, PullRequest] = {}
        for commit in shas:
            for pr in forge.pulls_for_commit(commit):
                if pr.number not in pulls and pr.base_branch == branch:
                    pulls[pr.number] = pr
        return Suspects(status, green, tuple(pulls.values()), truncated, len(shas))
    except ForgeError:
        return Suspects("unavailable", None)


# ---------------------------------------------------------------------------
# Render (Markdown localizado)
# ---------------------------------------------------------------------------


def _details(r: _Report, t: Translator) -> list[str]:
    unavailable = t("base_report.value.unavailable")
    run = f"<{r.run_url}>" if r.run_url else unavailable
    checks = ", ".join(f"`{c}`" for c in r.failed_checks) or t("base_report.value.none")
    lines = [
        f"- **{t('base_report.field.commit')}:** `{r.sha}`",
        f"- **{t('base_report.field.run')}:** {run}",
        f"- **{t('base_report.field.checks')}:** {checks}",
    ]
    if r.pdf_url:  # «el PDF si existe»
        lines.append(f"- **{t('base_report.field.pdf')}:** <{r.pdf_url}>")
    return lines


def _finding_line(f: ReportFinding, t: Translator) -> str:
    parts = [_code(f.check, 80)]
    if f.path:
        parts.append(_code(f.path + (f":{f.line}" if f.line else ""), 200))
    if f.code:
        parts.append(_code(f.code, 80))
    severity = t(f"base_report.severity.{f.severity}")
    return f"- **{severity}** " + " · ".join(parts) + ": " + _code(f.message, MAX_MESSAGE)


def _findings_section(r: _Report, t: Translator) -> list[str]:
    lines = [f"### {t('base_report.findings.heading', n=MAX_FINDINGS)}", ""]
    if not r.findings:
        lines.append(t("base_report.findings.none"))
        return lines
    lines.extend(_finding_line(f, t) for f in r.findings)
    omitted = r.findings_total - len(r.findings)
    if omitted > 0:
        lines += ["", t.plural("base_report.findings.more", omitted)]
    return lines


def _suspects_section(r: _Report, t: Translator) -> list[str]:
    s = r.suspects
    lines = [f"### {t('base_report.suspects.heading')}", ""]
    short = r.sha[:12]
    green = (s.green_sha or "")[:12]
    if s.status == "range":
        lines.append(t("base_report.suspects.range", green=green, sha=short))
    elif s.status == "no_green":
        lines.append(t("base_report.suspects.no_green", workflow=WORKFLOW_FILE, branch=r.branch or "", sha=short))
    elif s.status == "compare_failed":
        lines.append(t("base_report.suspects.compare_failed", green=green, sha=short))
    else:
        lines.append(t(f"base_report.suspects.{s.status}"))
        return lines  # same, no_branch, unavailable: no hay lista
    if s.truncated:
        lines.append(t("base_report.suspects.truncated", n=s.examined))
    lines.append("")
    if not s.pulls:
        lines.append(t("base_report.suspects.none"))
        return lines
    for pr in s.pulls[:MAX_SUSPECTS_SHOWN]:
        author = t("base_report.suspects.author", author=_code(pr.author, 100))
        lines.append(f"- #{pr.number} {_code(pr.title, MAX_TITLE)} ({author})")
    omitted = len(s.pulls) - MAX_SUSPECTS_SHOWN
    if omitted > 0:
        lines += ["", t.plural("base_report.suspects.more", omitted)]
    return lines


def _report_sections(r: _Report, t: Translator) -> list[str]:
    lines = _details(r, t)
    if r.build_not_run:
        lines += ["", t("base_report.build_not_run")]
    if r.policy_fallback:
        lines += ["", t("base_report.policy_fallback", sha=r.policy_fallback[:12])]
    elif not r.policy_ok:
        lines += ["", t("base_report.policy_unreadable")]
    return [*lines, "", *_findings_section(r, t), "", *_suspects_section(r, t)]


def render_issue_body(r: _Report, t: Translator) -> str:
    """Cuerpo del Issue: marcador, datos de REQ-CI-33 y la guía de reversión de 09 §7.3."""
    heading = (
        t("base_report.heading", branch=r.branch) if r.branch else t("base_report.heading_nobranch")
    )
    guide = [
        f"{i}. {t(f'base_report.guide.{step}', name=_meta.NAME)}"
        for i, step in enumerate(("freeze", "identify", "revert", "forward", "record"), start=1)
    ]
    lines = [
        _meta.ci_marker(MARKER_KIND),
        "",
        f"## {heading}",
        "",
        *_report_sections(r, t),
        "",
        f"### {t('base_report.guide.heading')}",
        "",
        *guide,
    ]
    return "\n".join(lines) + "\n"


def render_failure_comment(r: _Report, t: Translator) -> str:
    """Comentario de una falla posterior sobre el Issue ya abierto."""
    lines = [f"### {t('base_report.still_broken', sha=r.sha)}", "", *_report_sections(r, t)]
    return "\n".join(lines) + "\n"


def render_resolved_comment(sha: str, run_url: str, t: Translator) -> str:
    """Comentario «resuelto por ``<sha>``» (REQ-CI-34)."""
    lines = [t("base_report.resolved", sha=sha)]
    if run_url:
        lines += ["", f"- **{t('base_report.field.run')}:** <{run_url}>"]
    return "\n".join(lines) + "\n"


def _title(branch: str | None, t: Translator) -> str:
    return t("base_report.title", branch=branch) if branch else t("base_report.title_nobranch")


# ---------------------------------------------------------------------------
# Acciones sobre la forja
# ---------------------------------------------------------------------------


def _step(method: str, fn: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
    """Llama a la forja; un ``ForgeError`` pasa a ``_ForgeStep`` con el nombre del método."""
    try:
        return fn(*args, **kwargs)
    except ForgeError as exc:
        raise _ForgeStep(method, exc) from exc


def _open_issues(forge: ForgeAdapter) -> list[Issue]:
    """Issues abiertos con la etiqueta y el marcador, del más antiguo al más nuevo."""
    marker = _meta.ci_marker(MARKER_KIND)
    issues = _step("list_issues", forge.list_issues, labels=[LABEL], state="open")
    return sorted((i for i in issues if i.state == "open" and marker in i.body), key=lambda i: i.number)


def _viewer(forge: ForgeAdapter) -> str:
    """Login del token; en CI es ``github-actions[bot]`` y no hay llamada (REQ-FORGE-43)."""
    return _step("identity", forge.identity).login


def _has_op(forge: ForgeAdapter, number: int, op_id: str, me: str) -> bool:
    """¿Ya hay un comentario propio con el marcador ``op``?

    Solo cuentan los comentarios de ``me``, como en la recuperación del adaptador (08-forja
    §4.7, REQ-FORGE-35): el ``op_id`` sale de datos públicos y cualquiera puede comentar en el
    Issue, así que un marcador ajeno no prueba que el reporte ya se publicó.
    """
    marker = _op_marker(op_id)
    comments = _step("list_comments", forge.list_comments, number)
    return any(same_login(c.author, me) and marker in c.body for c in comments)


def _close(forge: ForgeAdapter, number: int, reason: str) -> None:
    _step("update_issue", forge.update_issue, number, state="closed", state_reason=reason)


def _assign(forge: ForgeAdapter, number: int, logins: Sequence[str]) -> list[str]:
    """Asigna el Issue al lead y al deputy; devuelve los logins que no quedaron asignados.

    GitHub descarta en silencio a quien no puede recibir la asignación (p. ej. sin acceso
    al repo) y el adaptador lo informa con ``ForgeValidationError`` (REQ-FORGE-11). Eso es
    solo un aviso: el Issue ya existe. Otro error de la forja impide garantizarlo.
    """
    try:
        updated: Issue | None = forge.set_assignees(number, list(logins))
    except ForgeValidationError:
        try:
            updated = forge.get_issue(number)  # mejor esfuerzo, para nombrar solo a los que faltan
        except ForgeError:
            updated = None
    except ForgeError as exc:
        raise _ForgeStep("set_assignees", exc) from exc
    got = {login.casefold() for login in updated.assignees} if updated is not None else set()
    return [login for login in logins if login.casefold() not in got]


def _report_broken(
    forge: ForgeAdapter, r: _Report, policy: _Policy, t: Translator, stderr: TextIO
) -> tuple[str, list[int]]:
    key = _op_key(r.sha, r.run_url)
    issues = _open_issues(forge)
    if not issues:
        issue = _step(
            "create_issue", forge.create_issue, _title(r.branch, t), render_issue_body(r, t),
            labels=[LABEL], op_id=f"base-report-open-{key}",
        )
        if policy.assignees:
            missing = _assign(forge, issue.number, policy.assignees)
            if missing:
                logins = ", ".join(missing)
                print(t("base_report.warning.assign", number=issue.number, logins=logins), file=stderr)
        return "created", [issue.number]
    canonical, duplicates = issues[0], issues[1:]
    me = _viewer(forge)
    open_op = f"base-report-open-{key}"
    fail_op = f"base-report-fail-{key}"
    opened_here = same_login(canonical.author, me) and _op_marker(open_op) in canonical.body
    if opened_here or _has_op(forge, canonical.number, fail_op, me):
        action = "unchanged"  # la misma ejecución ya lo reportó (p. ej. un re-run del job)
    else:
        _step("create_comment", forge.create_comment, canonical.number, render_failure_comment(r, t), op_id=fail_op)
        action = "commented"
    for dup in duplicates:  # «exactamente un» Issue abierto (REQ-CI-32)
        dup_op = f"base-report-duplicate-{_op_key(dup.number, canonical.number)}"
        if not _has_op(forge, dup.number, dup_op, me):
            body = t("base_report.duplicate", number=canonical.number, label=LABEL) + "\n"
            _step("create_comment", forge.create_comment, dup.number, body, op_id=dup_op)
        _close(forge, dup.number, "duplicate")
    return action, [canonical.number]


def _report_green(forge: ForgeAdapter, sha: str, run_url: str, t: Translator) -> tuple[str, list[int]]:
    issues = _open_issues(forge)
    me = _viewer(forge) if issues else ""
    for issue in issues:
        op_id = f"base-report-resolve-{_op_key(issue.number, sha)}"
        if not _has_op(forge, issue.number, op_id, me):
            body = render_resolved_comment(sha, run_url, t)
            _step("create_comment", forge.create_comment, issue.number, body, op_id=op_id)
        _close(forge, issue.number, "completed")
    return ("closed" if issues else "none"), [i.number for i in issues]


# ---------------------------------------------------------------------------
# Subcomando
# ---------------------------------------------------------------------------


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Opciones del subcomando. Ninguna es ``required`` ni usa ``choices``: ``main`` las valida (salida 3 o 1)."""
    t = translator(resolve_lang(cli=None, env=os.environ, project=None))
    for flag, metavar in (
        ("--policy", "DIR"),
        ("--sha", "SHA"),
        ("--build", "OUTCOME"),
        ("--build-reason", "REASON"),
        ("--validate", "RESULT"),
        ("--pdf-url", "URL"),
        ("--run-url", "URL"),
        ("--reports", "DIR"),
        ("--summary", "FILE"),
    ):
        dest = flag[2:].replace("-", "_")
        parser.add_argument(flag, dest=dest, metavar=metavar, default=None, help=t(f"base_report.arg.{dest}"))


def _fallback_t(ctx: OpContext) -> Translator:
    try:
        return translator(resolve_lang(cli=ctx.inv.lang, env=ctx.inv.env, project=None))
    except Exception:
        return translator("en")


def _write_summary(path: Path | None, text: str, t: Translator, stderr: TextIO) -> None:
    if path is None:
        return
    try:
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    except OSError:
        print(t("base_report.warning.summary", path=str(path)), file=stderr)


def _summary_text(
    status: Status, action: str, numbers: Sequence[int], sha: str, build: str, validate: str, t: Translator
) -> str:
    """Resumen del paso (``GITHUB_STEP_SUMMARY``) en Markdown localizado."""
    number = numbers[0] if numbers else 0
    if status == "inconclusive":
        line = t("base_report.summary.inconclusive", sha=sha, build=build or "-", validate=validate or "-")
    elif action == "closed":
        line = t("base_report.summary.closed", sha=sha, numbers=", ".join(f"#{n}" for n in numbers))
    elif action == "none":
        line = t("base_report.summary.green", sha=sha)
    else:
        line = t(f"base_report.summary.{action}", sha=sha, number=number)
    return f"### {_meta.NAME}/base-report\n\n{line}\n\n"


def _validate_args(args: argparse.Namespace, t: Translator, stderr: TextIO) -> int | None:
    """``None`` si los argumentos sirven; si no, el código de salida (3 uso, 1 valor inválido)."""
    required = ("policy", "sha", "build", "validate")
    missing = [f"--{name}" for name in required if getattr(args, name, None) is None]
    if missing:
        print(t("base_report.error.missing", args=", ".join(missing)), file=stderr)
        return EXIT_USAGE
    if not isinstance(args.sha, str) or not _SHA_RE.fullmatch(args.sha):
        print(t("base_report.error.sha"), file=stderr)
        return EXIT_ERROR
    for name in ("build", "validate"):
        if getattr(args, name).strip() not in OUTCOMES:
            print(t("base_report.error.outcome", arg=f"--{name}"), file=stderr)
            return EXIT_ERROR
    if (getattr(args, "build_reason", None) or "").strip() not in BUILD_REASONS:
        print(t("base_report.error.reason", arg="--build-reason"), file=stderr)
        return EXIT_ERROR
    for name in ("pdf_url", "run_url"):
        value = (getattr(args, name, None) or "").strip()
        if value and (len(value) > MAX_URL or not _URL_RE.fullmatch(value)):
            print(t("base_report.error.url", arg="--" + name.replace("_", "-")), file=stderr)
            return EXIT_ERROR
    if not Path(args.policy).is_dir():
        print(t("base_report.error.dir", arg="--policy"), file=stderr)
        return EXIT_ERROR
    return None


def _run(args: argparse.Namespace, ctx: OpContext, stdout: TextIO, stderr: TextIO) -> int:
    code = _validate_args(args, _fallback_t(ctx), stderr)
    if code is not None:
        return code
    sha: str = args.sha
    build = args.build.strip()
    build_reason = (getattr(args, "build_reason", None) or "").strip()
    validate = args.validate.strip()
    run_url = (args.run_url or "").strip()
    pdf_url = (args.pdf_url or "").strip()
    policy = _read_policy(Path(args.policy), ctx.inv.env)
    t = translator(resolve_lang(cli=ctx.inv.lang, env=ctx.inv.env, project=policy.language))
    summary = Path(args.summary) if args.summary else None
    status = classify(build, validate, policy_ok=policy.ok, build_reason=build_reason)
    action: str = "none"
    numbers: list[int] = []
    if status != "inconclusive":
        try:
            forge = ctx.forge()
        except OpError as exc:
            print(t("base_report.error.forge_missing", code=exc.code), file=stderr)
            return EXIT_ERROR
        try:
            if status == "green":
                action, numbers = _report_green(forge, sha, run_url, t)
            else:
                findings = load_findings(Path(args.reports) if args.reports else None)
                checks = (
                    (BUILD_CHECK, _build_failed(build, validate, build_reason)),
                    (VALIDATE_CHECK, validate in _FAILED),
                )
                failed = tuple(name for name, did_fail in checks if did_fail)
                report = _Report(
                    sha=sha,
                    branch=policy.branch,
                    run_url=run_url,
                    pdf_url=pdf_url,
                    failed_checks=failed,
                    policy_ok=policy.ok,
                    findings=tuple(findings[:MAX_FINDINGS]),
                    findings_total=len(findings),
                    suspects=find_suspects(forge, sha, policy.branch),
                    policy_fallback=policy.fallback_sha,
                    build_not_run=_build_not_run(build, validate, build_reason),
                )
                action, numbers = _report_broken(forge, report, policy, t, stderr)
        except _ForgeStep as exc:
            print(t("base_report.error.forge", method=exc.method, kind=exc.kind, label=LABEL), file=stderr)
            return EXIT_ERROR
    _write_summary(summary, _summary_text(status, action, numbers, sha, build, validate, t), t, stderr)
    stdout.write(f"status={status}\naction={action}\nissue={','.join(str(n) for n in numbers)}\n")
    return EXIT_OK


def main(args: argparse.Namespace, ctx: OpContext, stdout: TextIO) -> int:
    """``ci base-report``: 0 bien, 1 error o valor inválido (falla cerrada), 3 uso; nunca 2."""
    stderr = sys.stderr
    try:
        return _run(args, ctx, stdout, stderr)
    except KeyboardInterrupt:
        raise
    except (Exception, SystemExit):  # sin el texto de la excepción (REQ-OPS-3)
        try:
            print(_fallback_t(ctx)("base_report.error.internal"), file=stderr)
        except Exception:
            pass
        return EXIT_ERROR


COMMANDS: tuple[CiCommand, ...] = (CiCommand("base-report", add_arguments, main),)
