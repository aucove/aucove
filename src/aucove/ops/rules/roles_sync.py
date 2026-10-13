# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Reglas ``roles-sync`` y ``generated-sync`` de ``validate`` (09-ci §4.1).

- ``roles-sync``: ``.github/CODEOWNERS`` y ``.github/ISSUE_TEMPLATE/task.yml`` del
  contenido validado son idénticos a su regeneración desde ``roles.yaml`` y
  ``language`` de ``project.yaml`` del mismo árbol (06 REQ-PERM-31; la regeneración
  usa la cabeza a propósito). En modo ``pr`` también falla si la forja informa errores
  de ``CODEOWNERS`` para la cabeza (``codeowners_errors``). En modo ``tree`` el árbol
  de trabajo se compara normalizado a LF (01 REQ-ARCH-88), porque un checkout con
  ``core.autocrlf`` puede traer CRLF; en ``pr`` y ``base`` la comparación es byte a byte
  sobre los objetos de git.
- ``generated-sync``: en esta versión el núcleo no genera skills, así que cualquier
  archivo bajo ``.claude/skills/`` o ``.agents/skills/`` difiere de la regeneración.

El módulo exporta además la regeneración compartida (:func:`regenerate`,
:func:`compare_files` y :func:`tree_reader`), que usan ``sync-roles`` y ``doctor``
para producir exactamente los mismos bytes y el mismo veredicto que esta regla.
Los tres leen el árbol de trabajo con la misma vista (``vcs.tree.worktree``): las
entradas son las de git (índice y archivos sin rastrear), que distinguen mayúsculas, y
el contenido sale del disco sin seguir enlaces. Así ``.github/codeowners`` no pasa por
``.github/CODEOWNERS`` en un sistema de archivos que no distingue mayúsculas.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from ...config.project import parse_project
from ...config.roles import Roles, parse_roles
from ...domain.findings import PATH_PATTERN, Finding
from ...forge.base import ForgeError
from ...i18n import Translator
from ...render.codeowners import CODEOWNERS_PATH
from ...render.generated import RenderError, generated_files
from ..result import clip
from .base import RuleInput, RuleSpec, make_finding, unavailable

if TYPE_CHECKING:
    from ...vcs.tree import TreeView

__all__ = [
    "GENERATED_CODE",
    "GENERATED_SYNC",
    "ROLES_SYNC",
    "ROLES_SYNC_CODE",
    "RULES",
    "SKILL_DIRS",
    "FileState",
    "Regeneration",
    "compare_files",
    "is_skill_path",
    "regenerate",
    "tree_reader",
]

ROLES_FILE = "roles.yaml"
PROJECT_FILE = "project.yaml"
ROLES_SYNC_CODE = "E_VALIDATION_ROLES_OUT_OF_SYNC"
GENERATED_CODE = "E_VALIDATION_GENERATED"
SKILL_DIRS: tuple[str, ...] = (".claude/skills/", ".agents/skills/")
MAX_FORGE_ERRORS = 20  # hallazgos de codeowners_errors; el resto se resume en uno
MAX_FORGE_TEXT = 200  # caracteres de cada error de la forja citado (REQ-CI-27)

Reader = Callable[[str], bytes | None]
Problem = Literal["roles_missing", "roles_invalid", "project_missing", "project_invalid", "render_failed"]
FileStatus = Literal["same", "differs", "missing", "not_regular"]

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]+")


@dataclass(frozen=True)
class Regeneration:
    """Resultado de regenerar los archivos de ``roles.yaml``.

    ``expected`` es ``None`` si las entradas faltan o no son válidas; entonces
    ``problem`` dice cuál y ``problem_path`` qué archivo, y ``findings`` trae los
    hallazgos de ``config`` que lo explican.
    """

    expected: Mapping[str, bytes] | None
    problem: Problem | None = None
    problem_path: str | None = None
    findings: tuple[Finding, ...] = ()
    roles: Roles | None = None
    language: str | None = None


@dataclass(frozen=True)
class FileState:
    """Estado de un archivo generado frente a su regeneración."""

    path: str
    status: FileStatus
    current: bytes | None
    expected: bytes


def regenerate(read: Reader, *, t: Translator | None = None) -> Regeneration:
    """Regenera CODEOWNERS y task.yml desde ``roles.yaml`` y ``project.yaml`` que da ``read``.

    ``read(ruta)`` devuelve los bytes de un archivo regular o ``None``. Los bytes se
    pasan tal cual a ``parse_roles``, para que ``source_sha12`` sea el de D-35 (m).
    """
    roles_bytes = read(ROLES_FILE)
    if roles_bytes is None:
        return Regeneration(None, "roles_missing", ROLES_FILE)
    roles, roles_findings = parse_roles(roles_bytes, file=ROLES_FILE, t=t)
    if roles is None:
        return Regeneration(None, "roles_invalid", ROLES_FILE, tuple(roles_findings))
    project_bytes = read(PROJECT_FILE)
    if project_bytes is None:
        return Regeneration(None, "project_missing", PROJECT_FILE, roles=roles)
    cfg, project_findings = parse_project(project_bytes, file=PROJECT_FILE, t=t)
    if cfg is None:
        return Regeneration(None, "project_invalid", PROJECT_FILE, tuple(project_findings), roles=roles)
    try:
        expected = generated_files(
            lead=roles.lead,
            deputy=roles.deputy,
            sections=[(s.id, s.title, s.owner) for s in roles.sections],
            source_sha12=roles.source_sha12,
            lang=cfg.language,
        )
    except RenderError:
        # roles.yaml ya validó; un RenderError aquí es una entrada que el render no admite
        return Regeneration(None, "render_failed", ROLES_FILE, roles=roles, language=cfg.language)
    return Regeneration(
        MappingProxyType(dict(expected)),
        findings=tuple(roles_findings) + tuple(project_findings),
        roles=roles,
        language=cfg.language,
    )


def compare_files(
    read: Reader,
    expected: Mapping[str, bytes],
    *,
    exists: Callable[[str], bool] | None = None,
    normalize_eol: bool,
) -> list[FileState]:
    """Compara cada archivo de ``expected`` con lo que da ``read``, en orden de ruta.

    ``exists(ruta)`` distingue un archivo que falta de una entrada que no es un
    archivo regular (enlace, carpeta). Con ``normalize_eol``, CRLF cuenta como LF.
    """
    out: list[FileState] = []
    for path in sorted(expected):
        want = expected[path]
        current = read(path)
        status: FileStatus
        if current is None:
            status = "not_regular" if exists is not None and exists(path) else "missing"
        else:
            got = current.replace(b"\r\n", b"\n") if normalize_eol else current
            status = "same" if got == want else "differs"
        out.append(FileState(path, status, current, want))
    return out


def tree_reader(tree: TreeView) -> tuple[Reader, Callable[[str], bool]]:
    """``(read, exists)`` de una vista de árbol, tal como los usa la regla ``roles-sync``.

    ``read`` es ``tree.read_bytes``: los bytes de una entrada regular (sin enlaces y con el
    tope de 5 MB de REQ-CI-24) o ``None``. ``exists`` dice si la ruta es una entrada de la
    vista, con su grafía exacta. ``sync-roles`` y ``doctor`` llaman a esta misma función
    sobre ``vcs.tree.worktree`` para dar el mismo veredicto que la regla en modo ``tree``.
    """

    def exists(path: str) -> bool:
        return path in tree.entries()

    return tree.read_bytes, exists


def is_skill_path(path: str) -> bool:
    """``True`` si la ruta cae bajo ``.claude/skills/`` o ``.agents/skills/``.

    Se compara sin distinguir mayúsculas: en Windows y macOS ``.Claude/Skills`` es la
    misma carpeta para quien carga los skills.
    """
    return path.lower().startswith(SKILL_DIRS)


# -- roles-sync ---------------------------------------------------------------------


def _one_line(text: object) -> str:
    """Texto de la forja en una línea, sin controles y recortado (REQ-CI-27)."""
    flat = _CONTROL.sub(" ", str(text))
    return clip(" ".join(flat.split()), MAX_FORGE_TEXT)


def _out_of_sync(inp: RuleInput, path: str | None, reason: str) -> Finding:
    return make_finding(ROLES_SYNC, inp, ROLES_SYNC_CODE, severity="error", path=path, reason=reason)


def _codeowners_errors(inp: RuleInput) -> list[Finding]:
    """Errores de ``GET /repos/{o}/{r}/codeowners/errors?ref=<head>`` (06 REQ-PERM-31)."""
    t = inp.t
    ref = inp.head_sha or inp.head_ref
    forge = inp.forge
    if forge is None or not ref:  # sin forja no se puede verificar: falla cerrada
        return [unavailable(ROLES_SYNC, inp, t("roles.reason.forge_unavailable"), path=CODEOWNERS_PATH)]
    try:
        errors = list(forge.codeowners_errors(ref))
    except ForgeError:
        return [unavailable(ROLES_SYNC, inp, t("roles.reason.forge_error"), path=CODEOWNERS_PATH)]
    findings = [
        _out_of_sync(inp, CODEOWNERS_PATH, t("roles.reason.codeowners_error", error=_one_line(e)))
        for e in errors[:MAX_FORGE_ERRORS]
    ]
    if len(errors) > MAX_FORGE_ERRORS:
        more = len(errors) - MAX_FORGE_ERRORS
        findings.append(_out_of_sync(inp, CODEOWNERS_PATH, t("roles.reason.codeowners_more", n=more)))
    return findings


def _run_roles_sync(inp: RuleInput) -> Sequence[Finding]:
    tree = inp.tree
    if tree is None:
        return [unavailable(ROLES_SYNC, inp, inp.t("roles.reason.no_tree"))]
    read, exists = tree_reader(tree)
    regen = regenerate(read, t=inp.t)
    findings: list[Finding] = []
    if regen.expected is None:
        assert regen.problem is not None
        findings.append(_out_of_sync(inp, regen.problem_path, inp.t(f"roles.reason.{regen.problem}")))
    else:
        states = compare_files(read, regen.expected, exists=exists, normalize_eol=inp.mode == "tree")
        findings.extend(
            _out_of_sync(inp, s.path, inp.t(f"roles.reason.{s.status}")) for s in states if s.status != "same"
        )
    if inp.mode == "pr":
        findings.extend(_codeowners_errors(inp))
    return findings


# -- generated-sync -----------------------------------------------------------------


def _run_generated_sync(inp: RuleInput) -> Sequence[Finding]:
    tree = inp.tree
    if tree is None:
        return [unavailable(GENERATED_SYNC, inp, inp.t("roles.reason.no_tree"))]
    findings: list[Finding] = []
    for path in sorted(p for p in tree.entries() if is_skill_path(p)):
        shown = path if PATH_PATTERN.match(path) else None  # una ruta con '\' no es repo_path
        findings.append(make_finding(GENERATED_SYNC, inp, GENERATED_CODE, severity="error", path=shown))
    return findings


# roles-sync solo usa la forja en modo pr (y falla cerrada si no la tiene), así que no
# declara needs_forge: en tree y base no la necesita.
ROLES_SYNC = RuleSpec(
    id="roles-sync",
    modes=frozenset({"tree", "pr", "base"}),
    run=_run_roles_sync,
    codes=MappingProxyType({ROLES_SYNC_CODE: frozenset({"reason"})}),
)

GENERATED_SYNC = RuleSpec(
    id="generated-sync",
    modes=frozenset({"tree", "pr", "base"}),
    run=_run_generated_sync,
    codes=MappingProxyType({GENERATED_CODE: frozenset()}),
)

RULES: tuple[RuleSpec, ...] = (ROLES_SYNC, GENERATED_SYNC)
