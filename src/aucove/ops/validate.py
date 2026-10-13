# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Operación ``validate`` (05-operaciones §9.5; 09-ci §4.1).

El motor arma las entradas de cada modo, elige y ordena las reglas y construye el
sobre ``data_validate``:

1. Selección: ``--check`` o todas las reglas del modo. Un id desconocido es
   ``E_VALIDATION_INPUT``; una regla elegida que no corre en el modo queda ``skip``. Con
   ``format: none`` (sin manuscrito) las reglas de LaTeX (``LATEX_RULES``) también quedan
   ``skip`` y no se ejecutan (00-marco D-33 y D-43; 09-ci §4.1). El formato es el de
   ``rules.convention.tree_format``: el del árbol si lo cambia y es válido; si no, el de la base.
2. Orden topológico por ``RuleSpec.after``. Una dependencia no elegida se ejecuta
   igual y no aparece en ``results``; su estado llega en ``RuleInput.prior`` (si no
   pudo evaluarse, ``fail``).
3. ``--pin-report``: ``schemas``, ``roles-sync`` y ``generated-sync`` se toman del
   reporte del job ``pin`` como resultado y como ``prior``; si se pasa y falta o es
   ilegible, las tres fallan (REQ-CI-45, REQ-CI-8). En modo ``pr`` el núcleo de la base
   agrega lo que el pin no puede ver y no depende de la versión: los errores de
   ``codeowners/errors`` (``roles-sync``) y la coincidencia registro↔rama (``schemas``).
4. Herramientas: con ``<PREFIJO>TESTING=1`` y ``<PREFIJO><NOMBRE>`` (argv JSON) se usa ese
   prefijo; si no, el ejecutable de ``PATH`` fuera del árbol (``which_outside``). Sin
   herramienta, la clave falta en ``RuleInput.tools`` y la regla falla cerrada.
5. Entradas por modo: ``tree`` (árbol de trabajo de ``--tree`` o del
   proyecto actual, política de ``origin/<base>`` y estado anterior del merge-base con
   ella), ``pr`` (política de ``--policy``,
   contenido de ``--tree`` en ``head_sha`` y cambios de la API del PR), ``base``
   (``--after`` contra ``--before`` o el primer padre; D-33) y ``build-log``.
6. Una regla que lanza da ``E_INTERNAL_UNEXPECTED`` y ``fail``, sin el texto de la
   excepción (REQ-CI-8).

``validate`` nunca ejecuta nada del árbol que valida ni sigue sus enlaces
(REQ-CI-24): lee con ``vcs.tree`` y ``git`` resuelto fuera del repositorio, y las
herramientas se buscan fuera del árbol. ``--report`` escribe el mismo sobre que
devuelve, también cuando la operación falla.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from ..domain.findings import Finding, has_errors
from ..i18n import Translator, resolve_lang, translator
from .context import env_flag, resolve_project, testing
from .result import OpError, Result, WarningObj, dumps, fail, internal, make_error, make_warning, ok, read_envelope
from .rules import catalog
from .rules.base import (
    MODES,
    RULE_MODULES,
    UNAVAILABLE_CODE,
    Change,
    Mode,
    RuleInput,
    RuleSpec,
    Status,
    status_of,
    unavailable,
)
from .rules.catalog import PIN_RULES

if TYPE_CHECKING:
    from ..config.base import BaseSnapshot
    from ..forge.base import ChangedFile
    from ..vcs.git import Git
    from .context import OpContext

OP = "validate"
FORMATS: tuple[str, ...] = ("text", "github")
MAX_CHECKS = 50  # maxItems de checks (05 §9.5)
MAX_ANNOTATIONS = 50  # REQ-CI-26
STALE_FETCH_S = 3600  # W_NET_STALE_CACHE: base local con más de 1 h (05 §9.5)
MAX_CHARS_RANGE = (2000, 40000)  # paginación común de 05 §9.1

ResultStatus = Literal["pass", "fail", "skip"]

# Reglas que no leen el contenido del árbol: con un PR desde un fork (sin --tree)
# pueden evaluarse; todas las demás fallan cerradas si no dan ya un error.
CONTENT_FREE_RULES: frozenset[str] = frozenset({"decisions-immutable"})
# Reglas del manuscrito LaTeX: con format: none dan skip (09-ci §4.1; 00-marco D-33 y D-43).
# Los conv.latex.*, conv.main.*, conv.latexmkrc.* y conv.bib.* van dentro de convention.
LATEX_RULES: frozenset[str] = frozenset({"citations", "labels-refs", "latex-macros", "latex-log"})

# Estados de la API de archivos del PR -> Change.status (scope trata 'modified' como el más estricto).
_API_STATUS: Mapping[str, str] = MappingProxyType(
    {
        "added": "added",
        "copied": "added",
        "modified": "modified",
        "changed": "modified",
        "unchanged": "modified",
        "removed": "removed",
        "renamed": "renamed",
    }
)

_PARAMS: frozenset[str] = frozenset(
    {
        "mode", "base", "head", "checks", "changed_only", "policy", "tree", "pr", "before", "after",
        "log", "report", "pin_report", "format", "cursor", "max_chars",
    }
)
_STRING_PARAMS: tuple[str, ...] = (
    "base", "head", "policy", "tree", "before", "after", "log", "report", "pin_report", "cursor",
)


# -- resultado de cada regla ------------------------------------------------------------


@dataclass(frozen=True)
class RuleOutcome:
    """Un elemento de ``results[]``: id de regla, estado y hallazgos."""

    check: str
    status: ResultStatus
    findings: tuple[Finding, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {"check": self.check, "status": self.status, "findings": [f.to_dict() for f in self.findings]}


@dataclass(frozen=True)
class PinReport:
    """Reglas de ``PIN_RULES`` leídas del reporte del job ``pin`` (REQ-CI-45).

    ``problem`` no es ``None`` si el reporte falta o no es legible: entonces las tres
    reglas fallan con ``unavailable``.
    """

    outcomes: Mapping[str, RuleOutcome] = field(default_factory=dict)
    problem: str | None = None


def _finding_from_dict(check: str, item: object) -> Finding:
    if not isinstance(item, Mapping):
        raise ValueError("hallazgo que no es un objeto")
    return Finding(
        check=check,
        code=item["code"],
        severity=item["severity"],
        path=item.get("path"),
        line=item.get("line"),
        message=item["message"],
        hint=item.get("hint"),
        pointer=item.get("pointer"),
    )


def read_pin_report(path: Path) -> PinReport:
    """Lee el sobre de ``validate --mode tree`` que dejó el job ``pin``.

    Debe ser un sobre válido contra ``result.schema.json`` (``validate_result``) con
    ``op: validate``, ``data.mode: tree`` y ``results[]``. Un estado distinto de
    ``pass`` o un hallazgo de error cuenta como ``fail``.
    """
    from ..config.schemas import validate_result

    envelope = read_envelope(path)
    if envelope is None:
        return PinReport(problem="missing" if not os.path.lexists(path) else "unreadable")
    try:
        problems = validate_result(envelope)
    except Exception:
        problems = ["error"]
    data = envelope.get("data")
    if problems or envelope.get("op") != OP or not isinstance(data, Mapping) or data.get("mode") != "tree":
        return PinReport(problem="invalid")
    outcomes: dict[str, RuleOutcome] = {}
    results = data.get("results")
    if not isinstance(results, list):
        return PinReport(problem="invalid")
    for item in results:
        if not isinstance(item, Mapping):
            return PinReport(problem="invalid")
        check = item.get("check")
        if check not in PIN_RULES:
            continue
        if check in outcomes:
            return PinReport(problem="invalid")
        try:
            findings = tuple(_finding_from_dict(str(check), f) for f in item.get("findings") or ())
        except (KeyError, TypeError, ValueError):
            return PinReport(problem="invalid")
        status: ResultStatus = "fail" if item.get("status") != "pass" or has_errors(findings) else "pass"
        outcomes[str(check)] = RuleOutcome(str(check), status, findings)
    return PinReport(outcomes=MappingProxyType(outcomes))


# -- motor ----------------------------------------------------------------------------


def _in_mode(spec: RuleSpec, mode: Mode) -> bool:
    return mode in spec.modes


def without_manuscript(inp: RuleInput) -> bool:
    """``True`` si el árbol se juzga con ``format: none`` (D-43). Nunca lanza.

    En ``build-log`` no hay árbol ni política: ahí solo corre ``latex-log`` y la compilación
    que lo produce ya no existe sin manuscrito (``build-plan`` da ``run=false``).
    """
    if inp.mode == "build-log":
        return False
    try:
        from .rules.convention import FORMAT_NONE, tree_format

        return tree_format(inp) == FORMAT_NONE
    except Exception:  # sin el módulo no se salta nada: las reglas de LaTeX corren
        return False


def rule_ids(rules: Sequence[RuleSpec]) -> list[str]:
    """Ids de regla en el orden de primera aparición (orden de ``results[]``)."""
    return list(dict.fromkeys(spec.id for spec in rules))


def select(rules: Sequence[RuleSpec], mode: Mode, checks: Sequence[str] | None) -> list[str]:
    """Ids elegidos en el orden de ``results[]``; ``E_VALIDATION_INPUT`` si alguno no existe."""
    order = rule_ids(rules)
    if not checks:
        return [rid for rid in order if any(_in_mode(s, mode) for s in rules if s.id == rid)]
    known = set(order)
    bad = [{"param": "checks", "reason": "unknown", "index": i} for i, c in enumerate(checks) if c not in known]
    if bad:
        raise OpError("E_VALIDATION_INPUT", details={"errors": bad})
    wanted = set(checks)
    return [rid for rid in order if rid in wanted]


def _run_spec(spec: RuleSpec, inp: RuleInput, statuses: Mapping[str, Status]) -> list[Finding]:
    """Ejecuta una regla con su ``prior`` y sus herramientas; nunca lanza (REQ-CI-8)."""
    t = inp.t
    prior = MappingProxyType({dep: statuses.get(dep, "fail") for dep in spec.after})
    tools = MappingProxyType({name: inp.tools[name] for name in spec.tools if name in inp.tools})
    rinp = replace(inp, prior=prior, tools=tools)
    if spec.needs_forge and rinp.forge is None:
        return [unavailable(spec, rinp, t("validate.reason.no_forge"))]
    try:
        findings = list(spec.run(rinp))
        if not all(isinstance(f, Finding) for f in findings):
            raise TypeError("la regla no devolvió hallazgos")
    except Exception:  # el texto de la excepción nunca llega al sobre
        return [unavailable(spec, rinp, t("validate.reason.exception"))]
    if (
        rinp.tree is None
        and rinp.mode != "build-log"
        and spec.id not in CONTENT_FREE_RULES
        and not has_errors(findings)
    ):  # sin contenido (fork) una regla de contenido no puede pasar
        findings.append(unavailable(spec, rinp, t("validate.reason.no_tree")))
    return findings


def _pin_supplement(rule_id: str, specs: Sequence[RuleSpec], inp: RuleInput) -> list[Finding]:
    """Lo que el núcleo de la base agrega en modo ``pr`` a una regla tomada del pin.

    El job ``pin`` corre ``validate --mode tree`` sin forja y con HEAD separado
    (REQ-CI-45), así que no ve dos comprobaciones de 09-ci §4.1 que solo existen en modo
    ``pr`` y no dependen de la versión del núcleo: ``codeowners/errors?ref=<head>`` de
    ``roles-sync`` (06 REQ-PERM-31) y la coincidencia registro↔rama ``task/<n>`` de
    ``schemas``. Nunca lanza: un fallo es ``unavailable`` (REQ-CI-8).
    """
    if inp.mode != "pr":
        return []
    try:
        if rule_id == "roles-sync":
            from .rules import roles_sync

            found = list(roles_sync._codeowners_errors(inp))
        elif rule_id == "schemas":
            from .rules import schemas

            found = list(schemas.branch_findings(inp))
        else:
            return []
        if not all(isinstance(f, Finding) for f in found):
            raise TypeError("el complemento no devolvió hallazgos")
    except Exception:  # el texto de la excepción nunca llega al sobre
        return [unavailable(specs[0], inp, inp.t("validate.reason.exception"))]
    return found


def _pin_outcome(rule_id: str, specs: Sequence[RuleSpec], inp: RuleInput, pin: PinReport) -> RuleOutcome:
    found = pin.outcomes.get(rule_id) if pin.problem is None else None
    if found is None:
        reason = inp.t(f"validate.reason.pin_{pin.problem}" if pin.problem else "validate.reason.pin_rule_missing")
        return RuleOutcome(rule_id, "fail", (unavailable(specs[0], inp, reason),))
    seen = {(f.code, f.path, f.line, f.message) for f in found.findings}
    extra = [f for f in _pin_supplement(rule_id, specs, inp) if (f.code, f.path, f.line, f.message) not in seen]
    if not extra:
        return found
    status: ResultStatus = "fail" if found.status == "fail" or has_errors(extra) else "pass"
    return RuleOutcome(rule_id, status, (*found.findings, *extra))


def evaluate(
    rules: Sequence[RuleSpec],
    inp: RuleInput,
    *,
    checks: Sequence[str] | None = None,
    pin: PinReport | None = None,
) -> tuple[RuleOutcome, ...]:
    """Resultados de las reglas elegidas para ``inp.mode``."""
    by_id: dict[str, list[RuleSpec]] = {}
    for spec in rules:
        by_id.setdefault(spec.id, []).append(spec)
    selected = select(rules, inp.mode, checks)
    outcomes: dict[str, RuleOutcome] = {}
    statuses: dict[str, Status] = {}
    visiting: set[str] = set()
    no_manuscript: list[bool] = []  # se calcula una vez y solo si hace falta

    def skipped_by_format(rule_id: str) -> bool:
        if rule_id not in LATEX_RULES:
            return False
        if not no_manuscript:
            no_manuscript.append(without_manuscript(inp))
        return no_manuscript[0]

    def compute(rule_id: str) -> None:
        if rule_id in outcomes or rule_id in visiting:
            return  # ya calculada, o ciclo: la dependencia que lo cierra llega como 'fail'
        specs = by_id.get(rule_id)
        if specs is None:
            return  # dependencia desconocida: su prior es 'fail'
        visiting.add(rule_id)
        active = [s for s in specs if _in_mode(s, inp.mode)]
        if not active or skipped_by_format(rule_id):
            outcome = RuleOutcome(rule_id, "skip")
        elif pin is not None and rule_id in PIN_RULES:
            outcome = _pin_outcome(rule_id, specs, inp, pin)
        else:
            for spec in active:
                for dep in spec.after:
                    compute(dep)
            findings: list[Finding] = []
            for spec in active:
                findings.extend(_run_spec(spec, inp, statuses))
            outcome = RuleOutcome(rule_id, status_of(findings), tuple(findings))
        visiting.discard(rule_id)
        outcomes[rule_id] = outcome
        if outcome.status != "skip":
            statuses[rule_id] = outcome.status  # type: ignore[assignment]

    for rule_id in selected:
        compute(rule_id)
    return tuple(outcomes[rule_id] for rule_id in selected)


def only_changed(outcomes: Sequence[RuleOutcome], changes: Sequence[Change]) -> tuple[RuleOutcome, ...]:
    """``--changed-only``: deja los hallazgos sin ruta o en archivos cambiados.

    Un hallazgo ``unavailable`` (``E_INTERNAL_UNEXPECTED``) se conserva siempre, tenga
    la ruta que tenga: "no se pudo evaluar" nunca pasa a ``pass`` (REQ-CI-8).
    """
    paths = {c.path for c in changes} | {c.previous_path for c in changes if c.previous_path}
    out = []
    for o in outcomes:
        if o.status == "skip":
            out.append(o)
            continue
        kept = tuple(f for f in o.findings if f.path is None or f.path in paths or f.code == UNAVAILABLE_CODE)
        out.append(RuleOutcome(o.check, status_of(kept), kept))
    return tuple(out)


# -- herramientas ------------------------------------------------------


def _env_get(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None and os.name == "nt":
        upper = name.upper()
        value = next((v for k, v in env.items() if k.upper() == upper), None)
    return value


def _argv_from_json(raw: str) -> tuple[str, ...] | None:
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(value, list) or not value or not all(isinstance(x, str) and x and "\0" not in x for x in value):
        return None
    return tuple(value)


def resolve_tools(
    names: Iterable[str], *, env: Mapping[str, str], root: Path | None, cwd: str
) -> dict[str, tuple[str, ...]]:
    """argv de prefijo de cada herramienta; las que no se encuentran no aparecen.

    Con ``<PREFIJO>TESTING=1`` y ``<PREFIJO><NOMBRE>`` definida, se usa ese argv JSON (si no
    es una lista de textos no vacía, la herramienta falta: falla cerrada). Si no,
    ``which_outside(nombre, root)``, que excluye el árbol validado y el directorio actual.
    """
    from ..vcs.git import which_outside

    out: dict[str, tuple[str, ...]] = {}
    for name in dict.fromkeys(names):
        var = name.upper().replace("-", "_")
        if testing(env):
            raw = env_flag(env, var)
            if raw is not None:
                argv = _argv_from_json(raw)
                if argv is not None:
                    out[name] = argv
                continue
        found = which_outside(
            name,
            root if root is not None else Path(cwd),
            path=_env_get(env, "PATH"),
            pathext=_env_get(env, "PATHEXT"),
            cwd=cwd,
        )
        if found is not None:
            out[name] = (found,)
    return out


# -- argumentos -----------------------------------------------------------------------


@dataclass(frozen=True)
class Args:
    """Parámetros de ``validate`` ya comprobados."""

    mode: Mode = "tree"
    base: str | None = None
    head: str | None = None
    checks: tuple[str, ...] | None = None
    changed_only: bool = False
    policy: str | None = None
    tree: str | None = None
    pr: int | None = None
    before: str | None = None
    after: str | None = None
    log: str | None = None
    report: str | None = None
    pin_report: str | None = None
    format: str = "text"
    cursor: str | None = None
    max_chars: int | None = None


def _input_error(param: str, reason: str, **extra: object) -> OpError:
    return OpError("E_VALIDATION_INPUT", details={"errors": [{"param": param, "reason": reason, **extra}]})


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_args(kwargs: Mapping[str, object]) -> Args:
    """Comprueba tipos y valores (la CLI y MCP ya validan, pero el motor no confía)."""
    errors: list[dict[str, object]] = []
    unknown = sorted(set(kwargs) - _PARAMS)
    errors.extend({"param": name, "reason": "unknown"} for name in unknown)
    values: dict[str, object] = {}
    for name in _STRING_PARAMS:
        value = kwargs.get(name)
        if value is None:
            continue
        if not isinstance(value, str) or not value or "\0" in value:
            errors.append({"param": name, "reason": "type"})
        else:
            values[name] = value
    mode = kwargs.get("mode", "tree")
    if mode is None:
        mode = "tree"
    if mode not in MODES:
        errors.append({"param": "mode", "reason": "enum"})
    else:
        values["mode"] = mode
    fmt = kwargs.get("format", "text")
    if fmt is None:
        fmt = "text"
    if fmt not in FORMATS:
        errors.append({"param": "format", "reason": "enum"})
    else:
        values["format"] = fmt
    changed_only = kwargs.get("changed_only", False)
    if changed_only is None:
        changed_only = False
    if not isinstance(changed_only, bool):
        errors.append({"param": "changed_only", "reason": "type"})
    else:
        values["changed_only"] = changed_only
    checks = kwargs.get("checks")
    if checks is not None:
        if not isinstance(checks, (list, tuple)) or not all(isinstance(c, str) for c in checks):
            errors.append({"param": "checks", "reason": "type"})
        elif len(checks) > MAX_CHECKS:
            errors.append({"param": "checks", "reason": "maxItems"})
        elif checks:
            values["checks"] = tuple(dict.fromkeys(checks))
    pr = kwargs.get("pr")
    if pr is not None:
        if not _is_int(pr):
            errors.append({"param": "pr", "reason": "type"})
        elif pr < 1:  # type: ignore[operator]
            errors.append({"param": "pr", "reason": "minimum"})
        else:
            values["pr"] = pr
    max_chars = kwargs.get("max_chars")
    if max_chars is not None:
        if not _is_int(max_chars):
            errors.append({"param": "max_chars", "reason": "type"})
        elif not MAX_CHARS_RANGE[0] <= max_chars <= MAX_CHARS_RANGE[1]:  # type: ignore[operator]
            errors.append({"param": "max_chars", "reason": "minimum" if max_chars < MAX_CHARS_RANGE[0] else "maximum"})  # type: ignore[operator]
        else:
            values["max_chars"] = max_chars
    if errors:
        raise OpError("E_VALIDATION_INPUT", details={"errors": errors})
    return Args(**values)  # type: ignore[arg-type]


# -- entradas por modo ----------------------------------------------------------------


@dataclass
class Prepared:
    """Entradas de las reglas y datos del sobre que dependen del modo."""

    inp: RuleInput
    base: dict[str, object]
    head: dict[str, object]
    warnings: list[WarningObj] = field(default_factory=list)
    tool_root: Path | None = None


def _abs(ctx: OpContext, raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = Path(ctx.inv.cwd) / path
    return Path(os.path.normpath(path))


def _dir_arg(ctx: OpContext, name: str, raw: str) -> Path:
    path = _abs(ctx, raw)
    if not path.is_dir():
        raise _input_error(name, "not_found")
    return path


def _policy_t(ctx: OpContext, snap: BaseSnapshot) -> Translator:
    """Idioma de los mensajes: ``--lang``, variable de entorno, ``language`` de la base, ``en``."""
    lang = getattr(snap.project, "language", None)
    return translator(resolve_lang(cli=ctx.inv.lang, env=ctx.inv.env, project=lang))


def _git(ctx: OpContext, root: Path) -> Git:
    from ..vcs.git import Git, GitUnavailable

    try:
        return Git(root, env=ctx.inv.env)
    except GitUnavailable as exc:
        raise OpError("E_CONFIG_GIT_UNAVAILABLE") from exc


def _commit(git: Git, rev: str) -> str | None:
    return git.rev_parse(f"{rev}^{{commit}}")


def _declares_parents(git: Git, sha: str) -> bool:
    """``True`` si el objeto commit ``sha`` declara algún padre, esté o no en el clon.

    En un clon superficial git trata el commit del borde como raíz (``^1`` no se
    resuelve y ``rev-list --parents`` no da padres), pero el objeto conserva sus líneas
    ``parent``. Si el objeto no se puede leer, también ``True``: la duda falla cerrada.
    """
    from ..vcs.git import GitError

    try:
        proc = git.run("cat-file", "commit", sha, check=False)
    except GitError:
        return True
    if proc.returncode != 0:
        return True
    header = proc.stdout.split(b"\n\n", 1)[0]
    return any(line.startswith(b"parent ") for line in header.split(b"\n"))


def _fallback_warnings(snap: BaseSnapshot, t: Translator) -> list[WarningObj]:
    """``W_CONFIG_POLICY_FALLBACK`` con el sha usado de cada archivo (06-permisos REQ-PERM-4).

    ``details.files`` es ``archivo -> sha``: si ``project.yaml`` y ``roles.yaml`` salen de
    commits distintos, una lista de nombres junto a un solo ``sha`` atribuiría a los dos
    el de ``roles.yaml`` y perdería el de ``project.yaml``.
    """
    details = snap.fallback_details()
    if details is None:
        return []
    return [make_warning("W_CONFIG_POLICY_FALLBACK", t, details=details)]


def _stale_warning(common_dir: Path, base_branch: str, now: datetime, t: Translator) -> WarningObj | None:
    """``W_NET_STALE_CACHE`` si el último fetch de la base tiene más de 1 h (05 §9.5)."""
    candidates = (
        common_dir / "FETCH_HEAD",
        common_dir / "refs" / "remotes" / "origin" / base_branch,
        common_dir / "packed-refs",
    )
    mtimes = []
    for path in candidates:
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            continue
    if not mtimes:
        return None
    age = now.timestamp() - max(mtimes)
    if age <= STALE_FETCH_S:
        return None
    return make_warning("W_NET_STALE_CACHE", t, details={"age_s": int(age), "ref": f"origin/{base_branch}"})


def _change_of_git(status: str, path: str, previous: str | None, entries: Mapping[str, str]) -> Change:
    letter = status[:1]
    if letter == "D":
        return Change(path=path, status="removed")
    if letter == "R":
        return Change(path=path, status="renamed", previous_path=previous, file_mode=entries.get(path))
    kind = "added" if letter in ("A", "C") else "modified"  # T (tipo), U o X: el caso más estricto
    return Change(path=path, status=kind, file_mode=entries.get(path))  # type: ignore[arg-type]


def changes_from_git(
    rows: Sequence[tuple[str, str, str | None]], entries: Mapping[str, str], untracked: Sequence[str] = ()
) -> tuple[Change, ...]:
    """``Change`` de ``git diff --name-status`` más los archivos no rastreados (agregados)."""
    out = [_change_of_git(status, path, previous, entries) for status, path, previous in rows]
    seen = {c.path for c in out}
    out.extend(Change(path=p, status="added", file_mode=entries.get(p)) for p in untracked if p not in seen)
    return tuple(sorted(out, key=lambda c: (c.path, c.previous_path or "")))


def changes_from_api(files: Sequence[ChangedFile], entries: Mapping[str, str]) -> tuple[Change, ...]:
    """``Change`` de la API de archivos del PR, con los modos de ``git ls-tree`` de la cabeza."""
    out = []
    for f in files:
        status = _API_STATUS.get(f.status, "modified")
        previous = f.previous_path if status == "renamed" else None
        mode = None if status == "removed" else entries.get(f.path)
        out.append(Change(path=f.path, status=status, previous_path=previous, file_mode=mode))  # type: ignore[arg-type]
    return tuple(sorted(out, key=lambda c: (c.path, c.previous_path or "")))


def _prepare_tree(ctx: OpContext, args: Args, holder: list[Translator]) -> Prepared:
    """Modo ``tree``: árbol de trabajo y política de ``origin/<base>`` con ``git show``.

    ``base_tree`` y los cambios salen del merge-base entre la cabeza y ``origin/<base>``
    (la punta si no hay cabeza o no hay ancestro común).
    """
    from ..config.base import read_tree_base
    from ..vcs.tree import git_tree, worktree

    if args.tree is not None:
        project = resolve_project(str(_dir_arg(ctx, "tree", args.tree)))
        if project is None:
            raise OpError("E_CONFIG_NOT_A_PROJECT")
        snap = read_tree_base(project.root, env=ctx.inv.env, t=holder[0])
        git = _git(ctx, project.root)
    else:
        found = ctx.project()
        assert found is not None
        project = found
        snap = ctx.base()
        git = ctx.git()
    t = holder[0] = _policy_t(ctx, snap)
    root = project.root
    base_branch = snap.project.base_branch
    compare_ref = args.base if args.base is not None else f"refs/remotes/{snap.ref}"
    compare_sha = _commit(git, compare_ref)
    if compare_sha is None:
        raise _input_error("base", "unknown_ref")
    committed = args.head is not None
    head_sha = _commit(git, args.head if args.head is not None else "HEAD")
    if committed and head_sha is None:
        raise _input_error("head", "unknown_ref")
    tree = git_tree(root, head_sha, git=git) if committed and head_sha else worktree(root, git=git)
    # La política es la de origin/<base>, pero el estado anterior es el merge-base, como
    # en modo pr (09-ci §4.1: "todo ID de plan.md del merge-base sigue en head"). Con la
    # punta, una rama solo atrasada vería como borrado lo que integró otro PR.
    start = (git.merge_base(head_sha, compare_sha) if head_sha else None) or compare_sha
    base_tree = git_tree(root, start, git=git)
    entries = tree.entries()
    if committed:
        changes = changes_from_git(git.diff_name_status(start, head_sha), entries)
    else:
        changes = changes_from_git(git.diff_name_status(start, None), entries, git.ls_files(others=True))
    head_ref = args.head if committed else git.current_branch()
    warnings = _fallback_warnings(snap, t)
    stale = _stale_warning(project.common_dir, base_branch, ctx.inv.now(), t)
    if stale is not None:
        warnings.append(stale)
    inp = RuleInput(
        mode="tree",
        t=t,
        now=ctx.inv.now(),
        policy=snap,
        tree=tree,
        base_tree=base_tree,
        changes=changes,
        head_ref=head_ref,
        head_sha=head_sha,
        base_ref=base_branch,
        base_sha=compare_sha,
    )
    return Prepared(
        inp=inp,
        base={"ref": args.base if args.base is not None else snap.ref, "sha": compare_sha},
        head={"ref": head_ref or "HEAD", "sha": head_sha},
        warnings=warnings,
        tool_root=root,
    )


def _forge_error(exc: Exception) -> OpError:
    from ..domain.errors import code_for_forge_kind
    from ..forge.base import BaseNotDefault

    if isinstance(exc, BaseNotDefault):
        return OpError("E_CONFIG_BASE_NOT_DEFAULT", details={"default_branch": exc.default_branch})
    kind = str(getattr(exc, "kind", "unexpected"))
    return OpError(code_for_forge_kind(kind, OP) or "E_FORGE_UNEXPECTED", details={"kind": kind})


def _prepare_pr(ctx: OpContext, args: Args, holder: list[Translator]) -> Prepared:
    """Modo ``pr``: autorización de ``--policy``; contenido de ``--tree`` en ``head_sha``."""
    from ..config.base import read_policy_dir
    from ..forge.base import ForgeError
    from ..vcs.git import UnknownRevision
    from ..vcs.tree import git_tree

    if args.policy is None:
        raise _input_error("policy", "required")
    if args.pr is None:
        raise _input_error("pr", "required")
    snap = read_policy_dir(_dir_arg(ctx, "policy", args.policy), t=holder[0])
    t = holder[0] = _policy_t(ctx, snap)
    base_branch = snap.project.base_branch
    forge = ctx.forge()
    try:
        caps = forge.capabilities()
        if caps.default_branch != base_branch:  # REQ-CI-1
            raise OpError(
                "E_CONFIG_BASE_NOT_DEFAULT",
                details={"base_branch": base_branch, "default_branch": caps.default_branch},
            )
        pr = forge.get_pull_request(args.pr)
        if pr is None:
            raise OpError("E_FORGE_NOT_FOUND", details={"pr": args.pr})
        files = forge.list_pull_request_files(args.pr)
    except ForgeError as exc:
        raise _forge_error(exc) from exc
    tree = base_tree = None
    base_sha = snap.sha
    tool_root: Path | None = None
    if args.tree is not None:
        tree_dir = _dir_arg(ctx, "tree", args.tree)
        tool_root = tree_dir
        git = _git(ctx, tree_dir)
        try:
            tree = git_tree(tree_dir, pr.head_sha, git=git)
        except UnknownRevision as exc:
            raise _input_error("tree", "head_missing") from exc
        tip = _commit(git, base_sha) if base_sha else None
        if tip is None:
            tip = _commit(git, f"refs/remotes/origin/{base_branch}")
        merge_base = git.merge_base(tip, pr.head_sha) if tip else None
        base_tree = git_tree(tree_dir, merge_base, git=git) if merge_base else None
        base_sha = base_sha or tip
    changes = changes_from_api(files, tree.entries() if tree is not None else {})
    inp = RuleInput(
        mode="pr",
        t=t,
        now=ctx.inv.now(),
        policy=snap,
        tree=tree,
        base_tree=base_tree,
        changes=changes,
        pr=pr,
        head_ref=pr.head_branch,
        head_sha=pr.head_sha,
        base_ref=pr.base_branch,
        base_sha=base_sha,
        forge=forge,
    )
    return Prepared(
        inp=inp,
        base={"ref": pr.base_branch, "sha": base_sha},
        head={"ref": pr.head_branch, "sha": pr.head_sha},
        warnings=_fallback_warnings(snap, t),
        tool_root=tool_root or _abs(ctx, args.policy),
    )


def _prepare_base(ctx: OpContext, args: Args, holder: list[Translator]) -> Prepared:
    """Modo ``base``: ``--after`` (o ``HEAD``) contra ``--before`` (o su primer padre; D-33).

    Sin ``--before``, solo un commit sin padres es raíz. Si el primer padre existe pero
    no está en el clon (``fetch-depth`` 1), ``E_VALIDATION_INPUT`` con ``before`` y
    ``parent_unavailable``: las reglas entre versiones no pueden pasar sin evaluar.
    """
    from ..config.base import read_policy_dir
    from ..vcs.tree import git_tree

    policy_raw = args.policy if args.policy is not None else args.tree
    tree_raw = args.tree if args.tree is not None else args.policy
    if policy_raw is None or tree_raw is None:
        raise _input_error("policy", "required")
    snap = read_policy_dir(_dir_arg(ctx, "policy" if args.policy else "tree", policy_raw), t=holder[0])
    t = holder[0] = _policy_t(ctx, snap)
    tree_dir = _dir_arg(ctx, "tree" if args.tree else "policy", tree_raw)
    git = _git(ctx, tree_dir)
    after_rev = args.after if args.after is not None else "HEAD"
    after_sha = _commit(git, after_rev)
    if after_sha is None:
        raise _input_error("after" if args.after is not None else "tree", "unknown_ref")
    if args.before is not None:
        before_sha = _commit(git, args.before)
        if before_sha is None:
            raise _input_error("before", "unknown_ref")
    else:
        before_sha = _commit(git, f"{after_sha}^1")  # None en un commit raíz: sin cambios (D-33)
        if before_sha is None and _declares_parents(git, after_sha):
            # el padre falta (clon superficial): no es una raíz y no hay con qué comparar (REQ-CI-8)
            raise _input_error("before", "parent_unavailable")
    tree = git_tree(tree_dir, after_sha, git=git)
    base_tree = git_tree(tree_dir, before_sha, git=git) if before_sha else None
    changes = changes_from_git(git.diff_name_status(before_sha, after_sha), tree.entries()) if before_sha else ()
    inp = RuleInput(
        mode="base",
        t=t,
        now=ctx.inv.now(),
        policy=snap,
        tree=tree,
        base_tree=base_tree,
        changes=changes,
        head_sha=after_sha,
        base_ref=snap.project.base_branch,
        base_sha=before_sha,
    )
    return Prepared(
        inp=inp,
        base={"ref": args.before if args.before is not None else f"{after_rev}^1", "sha": before_sha},
        head={"ref": after_rev, "sha": after_sha},
        warnings=_fallback_warnings(snap, t),
        tool_root=tree_dir,
    )


def _prepare_build_log(ctx: OpContext, args: Args, holder: list[Translator]) -> Prepared:
    """Modo ``build-log``: solo el log de la compilación (``--log``)."""
    if args.log is None:
        raise _input_error("log", "required")
    inp = RuleInput(mode="build-log", t=holder[0], now=ctx.inv.now(), log_path=_abs(ctx, args.log))
    return Prepared(
        inp=inp,
        base={"ref": args.base or "HEAD", "sha": None},
        head={"ref": args.head or "HEAD", "sha": None},
    )


_PREPARE = {
    "tree": _prepare_tree,
    "pr": _prepare_pr,
    "base": _prepare_base,
    "build-log": _prepare_build_log,
}


# -- operación ------------------------------------------------------------------------


def _early_t(ctx: OpContext, mode: object, explicit_tree: bool) -> Translator:
    """Traductor antes de conocer la política: ``--lang``, entorno o, en local, la base del proyecto."""
    inv = ctx.inv
    lang = resolve_lang(cli=inv.lang, env=inv.env, project=None)
    decided = inv.lang in ("es", "en") or env_flag(inv.env, "LANG") in ("es", "en")
    if not decided and (mode == "build-log" or (mode in (None, "tree") and not explicit_tree)):
        try:
            return ctx.t()
        except Exception:
            return translator(lang)
    return translator(lang)


def load_rules() -> tuple[RuleSpec, ...]:
    """Reglas de ``RULE_MODULES`` (las pruebas pueden sustituir ``RULE_MODULES`` de este módulo)."""
    return catalog.load_rules(RULE_MODULES)


def _build_result(prepared: Prepared, outcomes: Sequence[RuleOutcome], args: Args, t: Translator) -> Result:
    inp = prepared.inp
    if inp.mode == "build-log" and [o.check for o in outcomes] == ["latex-log"]:
        try:
            from .rules.latex_log import diagnostics_result
        except ImportError:
            diagnostics_result = None  # type: ignore[assignment]
        if diagnostics_result is not None:  # sobre de diagnostics.json
            return diagnostics_result(
                outcomes[0].findings, t, base=prepared.base, head=prepared.head, pr=args.pr,
                warnings=prepared.warnings,
            )
    total = sum(len(o.findings) for o in outcomes)
    valid = all(o.status != "fail" for o in outcomes)
    data: dict[str, object] = {
        "valid": valid,
        "mode": inp.mode,
        "pr": args.pr,
        "base": prepared.base,
        "head": prepared.head,
        "results": [o.to_dict() for o in outcomes],
        "page": {"next_cursor": None, "returned": total, "total": total},
    }
    if valid:
        return ok(OP, data, prepared.warnings)
    return fail(OP, make_error("E_VALIDATION_FAILED", t), data=data, warnings=prepared.warnings)


def _validate(ctx: OpContext, args: Args, holder: list[Translator]) -> Result:
    try:
        rules = load_rules()
    except ImportError:  # falla cerrada: sin todas las reglas no se valida nada
        t = holder[0]
        details = {"trace_id": uuid.uuid4().hex[:12], "reason": "rules_unavailable"}
        return fail(OP, make_error("E_INTERNAL_UNEXPECTED", t, details=details))
    select(rules, args.mode, args.checks)  # un --check desconocido falla antes de leer nada
    prepared = _PREPARE[args.mode](ctx, args, holder)
    t = holder[0]
    tool_names = [n for spec in rules if _in_mode(spec, args.mode) for n in spec.tools]
    tools = resolve_tools(tool_names, env=ctx.inv.env, root=prepared.tool_root, cwd=ctx.inv.cwd) if tool_names else {}
    inp = replace(prepared.inp, tools=MappingProxyType(tools))
    prepared.inp = inp
    pin = read_pin_report(_abs(ctx, args.pin_report)) if args.pin_report is not None else None
    outcomes = evaluate(rules, inp, checks=args.checks, pin=pin)
    if args.changed_only:
        outcomes = only_changed(outcomes, inp.changes)
    return _build_result(prepared, outcomes, args, t)


def _from_op_error(exc: OpError, t: Translator) -> Result:
    try:
        err = make_error(exc.code, t, details=exc.details, **exc.fmt)
        return fail(OP, err, data=exc.data, warnings=exc.warnings)
    except Exception as inner:
        return internal(OP, inner, t)


def write_report(path: Path, result: Result) -> None:
    """Escribe el sobre en ``path`` de forma atómica (UTF-8 sin BOM, una línea y ``\\n``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".validate-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(dumps(result))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def run(ctx: OpContext, **kwargs: object) -> Result:
    """``validate`` (05 §9.5). Devuelve siempre un ``Result`` con ``op: validate``."""
    raw_report = kwargs.get("report")
    report = raw_report if isinstance(raw_report, str) and raw_report and "\0" not in raw_report else None
    holder = [_early_t(ctx, kwargs.get("mode"), kwargs.get("tree") is not None)]
    try:
        args = parse_args(kwargs)
        result = _validate(ctx, args, holder)
    except OpError as exc:
        result = _from_op_error(exc, holder[0])
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # nunca se propaga ni lleva el texto de la excepción (REQ-OPS-3)
        result = internal(OP, exc, holder[0])
    if report is not None:
        try:
            write_report(_abs(ctx, report), result)
        except Exception:  # sin reporte el CI no puede comentar: la operación falla cerrada
            details = {"trace_id": uuid.uuid4().hex[:12], "reason": "report_unwritable"}
            err = make_error("E_INTERNAL_UNEXPECTED", holder[0], details=details)
            result = fail(OP, err, data=result.data, warnings=result.warnings)
    return result


# -- salida legible -------------------------------------------------------------------


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _no_controls(text: str) -> str:
    """Escapa los caracteres de control (C0, DEL y C1) como ``\\xNN``.

    Lo que viene del árbol (rutas, mensajes que citan contenido) no puede partir una
    línea: en ``--format github`` una línea que empieza con ``::`` es un comando de flujo
    del runner (REQ-CI-26, REQ-CI-27), y en un terminal un ESC es una secuencia de control.
    """
    return _CONTROL_CHARS.sub(lambda m: f"\\x{ord(m.group(0)):02x}", text)


def _one_line(value: object) -> str:
    """Texto en una línea: espacios colapsados y sin caracteres de control."""
    return _no_controls(" ".join(str(value).split()))


def _escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")


def github_annotations(results: Sequence[Mapping[str, object]], limit: int = MAX_ANNOTATIONS) -> tuple[list[str], int]:
    """Comandos ``::error``/``::warning`` de GitHub Actions (REQ-CI-26), errores primero.

    Devuelve las líneas (como máximo ``limit``) y cuántas se omitieron.
    """
    items: list[tuple[int, int, str]] = []
    for r_index, result in enumerate(results):
        check = str(result.get("check", ""))
        findings = result.get("findings")
        if not isinstance(findings, list):
            continue
        for f in findings:
            if not isinstance(f, Mapping):
                continue
            severity = "error" if f.get("severity") == "error" else "warning"
            props = []
            path = f.get("path")
            if isinstance(path, str) and path:
                props.append(f"file={_escape_property(path)}")
                line = f.get("line")
                if isinstance(line, int) and not isinstance(line, bool) and line >= 1:
                    props.append(f"line={line}")
            title = f"{check} {f.get('code', '')}".strip()
            props.append(f"title={_escape_property(title)}")
            message = " ".join(str(f.get("message", "")).split())
            text = _no_controls(f"::{severity} {','.join(props)}::{_escape_data(message)}")
            items.append((0 if severity == "error" else 1, r_index, text))
    items.sort(key=lambda x: (x[0], x[1]))  # orden estable: errores, luego avisos
    lines = [text for _rank, _r, text in items[:limit]]
    return lines, max(0, len(items) - limit)


def _where(f: Mapping[str, object]) -> str:
    path = f.get("path")
    if not isinstance(path, str) or not path:
        return "-"
    line = f.get("line")
    where = f"{path}:{line}" if isinstance(line, int) and not isinstance(line, bool) else path
    return _no_controls(where)


def render_text(result: Result, t: Translator, *, fmt: str = "text") -> str:
    """Vista legible de ``data`` (la CLI escribe aparte avisos y error). ``github`` agrega anotaciones.

    Todo valor del sobre va en una línea y sin caracteres de control (``_no_controls``):
    solo las anotaciones pueden empezar con ``::``.
    """
    data = result.data if isinstance(result.data, Mapping) else {}
    results = [r for r in data.get("results") or () if isinstance(r, Mapping)]
    lines: list[str] = []
    if fmt == "github":
        annotations, omitted = github_annotations(results)
        lines.extend(annotations)
        if omitted:
            lines.append(t.plural("validate.text.annotations_omitted", omitted, max=MAX_ANNOTATIONS))
    mode = _one_line(data.get("mode", ""))
    lines.append(t("validate.text.valid" if data.get("valid") else "validate.text.invalid", mode=mode))
    for r in results:
        status = str(r.get("status", ""))
        lines.append(t("validate.text.result", check=_one_line(r.get("check", "")),
                       status=t(f"validate.text.status.{status}")))
        for f in r.get("findings") or ():
            if not isinstance(f, Mapping):
                continue
            severity = t("validate.text.severity.error" if f.get("severity") == "error" else "validate.text.severity.warning")
            lines.append(
                t("validate.text.finding", severity=severity, code=_one_line(f.get("code", "")), where=_where(f),
                  message=_one_line(f.get("message", "")))
            )
            hint = f.get("hint")
            if isinstance(hint, str) and hint:
                lines.append(t("validate.text.hint", hint=_one_line(hint)))
    return "\n".join(lines)
