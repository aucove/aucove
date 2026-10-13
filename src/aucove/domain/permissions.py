# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""check_path de 06-permisos §2.4 y check_change para los cambios de un diff.

Función pura (REQ-PERM-10, REQ-ARCH-18): recibe la instantánea de la política y de la tarea.
Los logins se comparan en minúsculas (REQ-SCHEMA-12); la grafía para mostrar sale de
``Policy.display``. La detección de enlaces (REQ-CONV-10) llega como ``Policy.link_probe`` y la
grafía real (REQ-CONV-9) como ``Policy.case_map``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from .paths import InvalidPath, OutsideRepo, apply_case_map, canonical, classify, is_git_path

__all__ = [
    "CONTROL_DIRS",
    "CONTROL_FILES",
    "WRITABLE",
    "Decision",
    "Mode",
    "Op",
    "Policy",
    "Task",
    "allowed",
    "check_change",
    "check_path",
    "is_control_path",
]

Op = Literal["create", "modify", "delete"]
Mode = Literal["session", "commit", "ci"]

_OPS: Final[frozenset[str]] = frozenset({"create", "modify", "delete"})
_MODES: Final[frozenset[str]] = frozenset({"session", "commit", "ci"})
_INTERNAL: Final[str] = "E_INTERNAL_UNEXPECTED"
_SYMLINK_MODE: Final[str] = "120000"
_NOTES_DIR: Final[str] = "memory/notes/"

CONTROL_FILES: Final[frozenset[str]] = frozenset(
    {"roles.yaml", "project.yaml", "AGENTS.md", "CLAUDE.md", ".mcp.json", "opencode.json"}
)
CONTROL_DIRS: Final[tuple[str, ...]] = (
    ".claude/",
    ".github/workflows/",
    ".devcontainer/",
    ".codex/",
    ".gemini/",
    ".opencode/",
    ".agents/",
)
WRITABLE: Final[frozenset[str]] = frozenset(
    {"claimed", "in_review", "changes_requested", "expired"}
)

# Las rutas de control se reconocen sin distinguir mayúsculas: solo añade confirmación
# (más restrictivo), como las comparaciones de R2 y R3 (REQ-CONV-9).
_CONTROL_FILES_LOW: Final[frozenset[str]] = frozenset(x.lower() for x in CONTROL_FILES)
_CONTROL_DIRS_LOW: Final[tuple[str, ...]] = tuple(x.lower() for x in CONTROL_DIRS)


@dataclass(frozen=True)
class Task:
    """Instantánea de la tarea (04-tareas; estado de 00-marco §5.5)."""

    number: int
    claimant: str | None
    state: str
    sections: frozenset[str]  # claim.sections ∩ TaskSpec.sections actuales (04 §1.5)


@dataclass(frozen=True)
class Policy:
    """Instantánea de la política de la base y del árbol donde se evalúa (06 §2.1).

    Al construirse normaliza los logins a minúsculas y guarda en ``display`` la grafía
    recibida de cada uno (sin pisar la que ya traiga ``display``).
    """

    people: frozenset[str]
    lead: str
    deputy: str | None
    sections: Mapping[str, str]  # id -> dueño
    base_decisions: frozenset[str]
    root: str
    ci_fs: bool
    display: Mapping[str, str] = field(default_factory=dict)  # minúsculas -> grafía
    case_map: Mapping[str, str] | None = None  # minúsculas -> grafía real (REQ-CONV-9)
    # REQ-CONV-10. Solo puede faltar en modo ci (el modo sale de ls-tree); en session y commit,
    # sin sonda check_path deniega todo con E_INTERNAL_UNEXPECTED (falla cerrada).
    link_probe: Callable[[str], bool] | None = None
    # Editor del artículo declarado en roles.yaml (00-marco D-56), solo con format: latex
    # (03 REQ-SCHEMA-95). None: los archivos de clase editor los cambian lead y deputy.
    editor: str | None = None

    def __post_init__(self) -> None:
        shown: dict[str, str] = {k.lower(): v for k, v in self.display.items()}
        names: list[str | None] = [*sorted(self.people), self.lead, self.deputy, self.editor]
        names.extend(self.sections.values())
        for name in names:
            if name:
                shown.setdefault(name.lower(), name)
        object.__setattr__(self, "display", shown)
        object.__setattr__(self, "people", frozenset(p.lower() for p in self.people))
        object.__setattr__(self, "lead", self.lead.lower())
        object.__setattr__(self, "deputy", self.deputy.lower() if self.deputy else None)
        object.__setattr__(self, "editor", self.editor.lower() if self.editor else None)
        object.__setattr__(self, "sections", {k: v.lower() for k, v in self.sections.items()})
        object.__setattr__(self, "base_decisions", frozenset(self.base_decisions))

    @property
    def approvers(self) -> frozenset[str]:
        return frozenset(x for x in (self.lead, self.deputy) if x)

    @property
    def editors(self) -> frozenset[str]:
        """Quién cambia los archivos de clase ``editor`` (06 §2.4, paso 12b)."""
        return frozenset({self.editor}) if self.editor else self.approvers

    def shown(self, login: str | None) -> str | None:
        """Grafía para mostrar de un login en minúsculas."""
        if login is None:
            return None
        return self.display.get(login, login)


@dataclass(frozen=True)
class Decision:
    allow: bool
    code: str | None
    path: str | None
    path_class: str | None = None
    section: str | None = None
    owner: str | None = None
    confirm: bool = False
    requires_approval_of: frozenset[str] = frozenset()  # reservado; siempre vacío en v0.1
    warning: str | None = None


def is_control_path(rel: str) -> bool:
    """Ruta de control de 06 §2.2 (cambia los controles o las instrucciones del agente)."""
    low = rel.lower()
    return low in _CONTROL_FILES_LOW or low.startswith(_CONTROL_DIRS_LOW)


def check_path(
    login: str,
    raw_path: str,
    op: Op,
    task: Task | None,
    *,
    pol: Policy,
    mode: Mode,
    file_mode: str | None = None,
    generated_matches: bool = False,
) -> Decision:
    """Decide si ``login`` puede hacer ``op`` sobre ``raw_path`` (06 §2.4).

    ``generated_matches`` lo calcula el llamador: indica que el contenido de la ruta
    ``generated`` es exactamente la regeneración (en CI, la regla scope desde el prior de
    roles-sync y generated-sync).
    """
    try:
        return _check_path(login, raw_path, op, task, pol, mode, file_mode, generated_matches)
    except Exception:  # REQ-PERM-11: ante cualquier fallo, denegar; nunca autorizar
        return Decision(False, _INTERNAL, raw_path if isinstance(raw_path, str) else None)


def _check_path(
    login: str,
    raw_path: str,
    op: str,
    task: Task | None,
    pol: Policy,
    mode: str,
    file_mode: str | None,
    generated_matches: bool,
) -> Decision:
    if op not in _OPS:
        raise ValueError(op)
    if mode not in _MODES:
        raise ValueError(mode)
    probe = pol.link_probe
    if mode != "ci" and probe is None:
        # 06 §2.4: en session y commit la instantánea incluye los enlaces del árbol; sin
        # sonda no se puede descartar uno, y REQ-PERM-11 manda denegar.
        raise ValueError("link_probe")
    who = login.lower()
    try:
        rel = canonical(raw_path, pol.root, pol.ci_fs)
    except (InvalidPath, OutsideRepo):
        return Decision(False, "E_PERM_OUTSIDE_REPO", raw_path, "outside")
    if pol.ci_fs:
        rel = apply_case_map(rel, pol.case_map)  # grafía real: 02 REQ-CONV-9
    if is_git_path(rel):  # '.git' en cualquier segmento, como R0
        return Decision(False, "E_PERM_OUTSIDE_REPO", rel, "lead")
    if file_mode == _SYMLINK_MODE or (mode != "ci" and probe is not None and probe(rel)):
        return Decision(False, "E_PERM_SYMLINK", rel, "lead")  # 02 REQ-CONV-10
    info = classify(rel, frozenset(pol.sections), symlinked=False)
    cls, sec = info.cls, info.section

    def deny(code: str, owner: str | None = None) -> Decision:
        return Decision(False, code, rel, cls, sec, pol.shown(owner))

    if who not in pol.people:
        return deny("E_PERM_NOT_A_MEMBER")
    staff = who in pol.approvers
    if cls == "local":
        return deny("E_PERM_LOCAL_PATH")
    if cls == "generated":
        # generated_matches debe ser exactamente True: un valor no booleano no autoriza
        ok = (
            op in ("create", "modify")
            and mode in ("commit", "ci")
            and staff
            and generated_matches is True
        )
        return Decision(True, None, rel, cls) if ok else deny("E_PERM_GENERATED_PATH")
    if task is None:
        return deny("E_STATE_NOT_ON_TASK_BRANCH")
    if (task.claimant or "").lower() != who:
        return deny("E_CLAIM_NOT_HELD")
    if task.state not in WRITABLE:
        return deny("E_STATE_INVALID_TRANSITION")  # el llamador pone task.state en details.state
    if cls == "task" or (mode == "session" and rel.startswith(_NOTES_DIR)):
        # Regla 9: en session el agente no edita memory/notes/** (la nota la escribe submit,
        # DC-09); en commit y ci, la nota propia admite create y modify.
        own = (
            cls == "task"
            and mode != "session"
            and info.task == task.number
            and op in ("create", "modify")
        )
        return Decision(True, None, rel, cls) if own else deny("E_PERM_OUTSIDE_TASK")
    if cls == "append":  # create; modify/delete solo si no está en la base
        if rel in pol.base_decisions:
            return deny("E_PERM_APPEND_ONLY")
        return Decision(True, None, rel, cls)
    if cls == "section":
        assert sec is not None
        owner = pol.sections[sec]
        if sec not in task.sections:
            return deny("E_PERM_OUTSIDE_TASK", owner)
        if owner != who:
            return deny("E_PERM_SECTION_NOT_OWNED", owner)
        return Decision(True, None, rel, cls, sec, pol.shown(owner))
    if cls == "editor":  # paso 12b, desde H3 (02 §3.3 R6a; 00-marco D-56)
        if who not in pol.editors:
            return deny("E_PERM_EDITOR_ONLY_PATH", pol.editor or pol.lead)
        return Decision(True, None, rel, cls)
    if not staff:  # cls == "lead"
        return deny("E_PERM_LEAD_ONLY_PATH")
    return Decision(True, None, rel, cls, confirm=(mode == "session" and is_control_path(rel)))


# Estado de un cambio -> operaciones de check_path. Acepta los estados de la API de archivos
# del PR (09 §4.2) y las letras de git diff --name-status (con puntuación en R y C).
_STATUS_OPS: Final[Mapping[str, tuple[Op, ...]]] = {
    "added": ("create",),
    "A": ("create",),
    "modified": ("modify",),
    "changed": ("modify",),  # la API lo usa para un cambio de modo o de tipo
    "unchanged": ("modify",),
    "M": ("modify",),
    "T": ("modify",),  # cambiar el tipo o el bit de ejecución es modificar (02 §3.4)
    "removed": ("delete",),
    "deleted": ("delete",),
    "D": ("delete",),
    "renamed": ("delete", "create"),
    "R": ("delete", "create"),
    "copied": ("create",),
    "C": ("create",),
}


# Las rutas de check_change salen de git (API de archivos del PR o git diff --name-status): ahí
# '/' es el único separador y '\' es un carácter más del nombre (válido en Linux; git para
# Windows no puede extraerlo). canonical (02 §3.1) convierte '\' en '/' porque check_path también
# recibe rutas del SO (session, en Windows), así que check_change deniega antes la ruta como
# inválida (REQ-CONV-7): si no, 'sections\m\x.tex', un archivo de la raíz en git (lead), se
# leería como de la parte m.
_GIT_NOT_SEP: Final[str] = "\\"


def _ops_for(status: str) -> tuple[Op, ...] | None:
    ops = _STATUS_OPS.get(status)
    score = status[1:]
    if ops is None and status[:1] in ("R", "C") and score.isascii() and score.isdigit():
        ops = _STATUS_OPS[status[0]]
    return ops


def check_change(
    login: str,
    status: str,
    path: str,
    previous_path: str | None,
    task: Task | None,
    *,
    pol: Policy,
    mode: Mode,
    file_mode: str | None = None,
    generated_matches: bool = False,
) -> list[Decision]:
    """Decisiones de un cambio: un renombre es delete(origen) + create(destino) (06 §2.4).

    Basta una denegación de la lista para rechazar el cambio. ``file_mode`` es el modo del
    destino; el origen de un renombre ya no existe en la cabeza y se evalúa sin modo. Un estado
    desconocido o un renombre sin origen se deniegan con E_INTERNAL_UNEXPECTED.

    ``path`` y ``previous_path`` son rutas de git, en cualquier modo: una ruta evaluada que
    contiene ``\\`` se deniega con E_PERM_OUTSIDE_REPO (clase ``outside``, ruta literal) sin
    llamar a ``check_path``. En un renombre con un solo lado así, el otro lado se evalúa igual.
    Las rutas del SO, donde ``\\`` puede ser el separador, van por ``check_path``.
    """
    try:
        ops = _ops_for(status)
        if ops is None:
            raise ValueError(status)
        if mode not in _MODES:  # como check_path: un modo inválido es un fallo del llamador
            raise ValueError(mode)
        rename = ops == ("delete", "create")
        if rename and not previous_path:
            raise ValueError("renamed without previous_path")
        out: list[Decision] = []
        for op in ops:
            target, fm = (previous_path, None) if rename and op == "delete" else (path, file_mode)
            assert target is not None
            if _GIT_NOT_SEP in target:
                out.append(Decision(False, "E_PERM_OUTSIDE_REPO", target, "outside"))
                continue
            out.append(
                check_path(
                    login,
                    target,
                    op,
                    task,
                    pol=pol,
                    mode=mode,
                    file_mode=fm,
                    generated_matches=generated_matches,
                )
            )
        return out
    except Exception:  # REQ-PERM-11
        return [Decision(False, _INTERNAL, path if isinstance(path, str) else None)]


def allowed(decisions: Iterable[Decision]) -> bool:
    """True si ninguna decisión deniega."""
    return all(d.allow for d in decisions)
