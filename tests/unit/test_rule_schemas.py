# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Regla ``schemas`` (09-ci §4.1; 03-esquemas §2.3-2.5, §6 y §7; REQ-CI-25).

Incluye la matriz de cuentas de 10-seguridad §8.3 con la configuración efectiva
(REQ-SEC-64, REQ-SCHEMA-91; D-48).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

import pytest

from aucove import _meta
from aucove.config.base import BaseSnapshot
from aucove.config.project import parse_project
from aucove.config.roles import parse_roles
from aucove.domain.findings import Finding
from aucove.forge.base import PullRequest
from aucove.i18n import Lang, Translator, translator
from aucove.ops.rules import schemas
from aucove.ops.rules.base import Change, RuleInput
from aucove.vcs.tree import is_regular_mode
from tests.conftest import FIXTURES, load_tool, roles_fixture

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
SHA = "0123456789abcdef0123456789abcdef01234567"
PROJECT = (FIXTURES / "config" / "project" / "minimal.yaml").read_text(encoding="utf-8")


def with_account(roles: str, login: str, account_type: str, *, active: bool = True) -> str:
    """``roles`` con el ``account_type`` de ``login`` cambiado y, si se pide, dado de baja."""
    pattern = re.compile(rf"(  - login: {login}\n(?:    (?!account_type:).*\n)*)    account_type: \S+\n")
    tail = "" if active else "    active: false\n"
    out, n = pattern.subn(lambda m: f"{m.group(1)}    account_type: {account_type}\n{tail}", roles)
    assert n == 1, login
    return out


# Con data_policy.level: internal, `unknown` no se admite (10-seguridad §8.3): dario no usa IA.
ROLES = with_account(roles_fixture("basic"), "dario", "none")
ROLES_INACTIVE = with_account(ROLES, "dario", "none", active=False)
DECISION_PATH = "memory/decisions/2026-10-01-3-usar-biblatex.md"
NOTE_PATH = "memory/notes/task-3.md"
MARK = f"<!-- {_meta.NAME}:session"


class MemTree:
    """``TreeView`` en memoria que registra qué rutas se leyeron."""

    def __init__(self, files: Mapping[str, str | bytes], *, modes: Mapping[str, str] | None = None,
                 sha: str | None = SHA) -> None:
        self._data = {p: v.encode("utf-8") if isinstance(v, str) else v for p, v in files.items()}
        self._modes = {p: "100644" for p in self._data}
        self._modes.update(modes or {})
        self.root: Path | None = None
        self.sha = sha
        self.reads: list[str] = []

    def entries(self) -> Mapping[str, str]:
        return MappingProxyType(dict(self._modes))

    def read_bytes(self, path: str, limit: int = 5 * 2**20) -> bytes | None:
        self.reads.append(path)
        data = self._data.get(path)
        if not is_regular_mode(self._modes.get(path)) or data is None or len(data) > limit:
            return None
        return data

    def read_text(self, path: str, limit: int = 5 * 2**20) -> str | None:
        data = self.read_bytes(path, limit)
        try:
            return data.decode("utf-8") if data is not None else None
        except UnicodeDecodeError:
            return None


def snapshot(*, roles: str = ROLES, decisions: Sequence[str] = ()) -> BaseSnapshot:
    cfg, f1 = parse_project(PROJECT.encode("utf-8"))
    parsed, f2 = parse_roles(roles.encode("utf-8"))
    assert cfg is not None and parsed is not None, (f1, f2)
    return BaseSnapshot(ref="origin/main", sha=SHA, project=cfg, roles=parsed, decisions=frozenset(decisions),
                        fallback_sha=None, source="local")


def pull(author: str = "carla", head_branch: str = "task/3") -> PullRequest:
    return PullRequest(number=42, node_id="n", title="t", body="", state="open", draft=False, merged=False,
                       merged_at=None, head_branch=head_branch, head_sha=SHA, base_branch="main", author=author,
                       labels=(), requested_reviewers=(), updated_at=NOW, url="https://forge.invalid/pr/42")


def decision(*, id_: str = "2026-10-01-3-usar-biblatex", date: str = "2026-10-01", task: int = 3,
             author: str = "carla", status: str = "accepted", supersedes: Sequence[str] = (),
             body: str = "## Contexto\n\nTexto.\n\n## Decisión\n\nTexto.\n\n## Consecuencias\n\nTexto.\n") -> str:
    lines = ["---", f"id: {id_}", "title: Usar biblatex", f"date: {date}", f"author: {author}", "origin: human",
             f"task: {task}", f"status: {status}", "summary: Se adopta biblatex."]
    if supersedes:
        lines.append("supersedes:")
        lines.extend(f"  - {s}" for s in supersedes)
    return "\n".join(lines) + "\n---\n\n" + body


def block(n: int, *, date: str = "2026-10-01", login: str = "carla", text: str = "Trabajo hecho.") -> str:
    meta = json.dumps({"n": n, "date": date, "login": login, "door": "claude-code"})
    return f"{MARK} {meta} -->\n{text}\n"


def note(*blocks: str, task: int = 3, front: str | None = None) -> str:
    head = front if front is not None else f"---\ntask: {task}\ncreated: 2026-10-01\ncreated_by: carla\n---\n"
    return head + "\n" + "\n".join(blocks)


def run(files: Mapping[str, str | bytes], *, changes: Sequence[Change] = (), mode: str = "pr",
        t: Translator | None = None, policy: BaseSnapshot | None = None, base_tree: MemTree | None = None,
        pr: PullRequest | None = None, head_ref: str | None = "task/3",
        modes: Mapping[str, str] | None = None, tree: MemTree | None = None) -> list[Finding]:
    base = {"project.yaml": PROJECT, "roles.yaml": ROLES}
    base.update(files)  # type: ignore[arg-type]
    view = tree if tree is not None else MemTree(base, modes=modes)
    inp = RuleInput(mode=mode, t=t or translator("es"), now=NOW, policy=policy or snapshot(), tree=view,  # type: ignore[arg-type]
                    base_tree=base_tree, changes=tuple(changes), pr=pr, head_ref=head_ref)
    out = list(schemas.RULE.run(inp))
    assert all(f.check == "schemas" for f in out)
    return out


def codes(findings: Sequence[Finding]) -> list[str]:
    return [f.code for f in findings]


def added(path: str) -> Change:
    return Change(path=path, status="added", file_mode="100644")


# -- project.yaml y roles.yaml ------------------------------------------------------------


def test_valid_project_and_roles_pass() -> None:
    assert run({}) == []


def test_missing_roles_is_config_not_found(t_en: Translator) -> None:
    view = MemTree({"project.yaml": PROJECT})
    (finding,) = run({}, tree=view, t=t_en)
    assert finding.code == "E_CONFIG_NOT_FOUND" and finding.path == "roles.yaml" and finding.severity == "error"
    assert "`roles.yaml`" in finding.message


def test_invalid_project_gives_schema_findings_with_line_and_pointer() -> None:
    findings = run({"project.yaml": PROJECT.replace("format: latex", "format: latex\nunexpected: 1")})
    assert "E_VALIDATION_SCHEMA" in codes(findings)
    f = next(f for f in findings if f.code == "E_VALIDATION_SCHEMA")
    assert f.path == "project.yaml" and f.line is not None and f.pointer is not None


def test_roles_semantic_rules_run_with_the_installed_core() -> None:
    bad = ROLES.replace("lead: ana", "lead: nadie")
    assert "E_VALIDATION_ROLES_LEAD" in codes(run({"roles.yaml": bad}))


def test_symlinked_config_is_not_read() -> None:
    view = MemTree({"project.yaml": PROJECT, "roles.yaml": ROLES}, modes={"roles.yaml": "120000"})
    (finding,) = run({}, tree=view)
    assert finding.code == "E_CONFIG_INVALID" and finding.path == "roles.yaml"
    assert "roles.yaml" not in view.reads  # REQ-CI-24: no se lee el contenido de una entrada 120000


def test_schemas_of_the_tree_are_ignored() -> None:
    """Valida con los esquemas del núcleo instalado, no con un ``schemas/`` del PR."""
    bad = "---\nid: 2026-10-01-3-usar-biblatex\n---\n\n## Contexto\n## Decisión\n## Consecuencias\n"
    findings = run({DECISION_PATH: bad, "schemas/decision.schema.json": "{}"}, changes=[added(DECISION_PATH)])
    assert "E_VALIDATION_SCHEMA" in codes(findings)


def test_no_tree_fails_closed() -> None:
    inp = RuleInput(mode="pr", t=translator("es"), now=NOW, policy=snapshot())
    (finding,) = schemas.RULE.run(inp)
    assert finding.code == "E_INTERNAL_UNEXPECTED" and finding.severity == "error"


# -- matriz de cuentas (10-seguridad §8.3; REQ-SEC-64; REQ-SCHEMA-91; D-48) -----------------


def carla(account_type: str) -> str:
    """``roles.yaml`` con la cuenta de carla (la tercera persona, ``/people/2``) cambiada."""
    return with_account(ROLES, "carla", account_type)


def line_of(text: str, fragment: str) -> int:
    (n,) = [i for i, line in enumerate(text.split("\n"), 1) if fragment in line]
    return n


def project(*, disclosure: str | None = None, level: str | None = None, require_commercial: str | None = None) -> str:
    text = PROJECT + (f"disclosure: {disclosure}\n" if disclosure else "")
    if level is not None or require_commercial is not None:
        text += "data_policy:\n"
        text += f"  level: {level}\n" if level is not None else ""
        text += f"  require_commercial: {require_commercial}\n" if require_commercial is not None else ""
    return text


def accounts(project_text: str, roles_text: str, *, t: Translator | None = None) -> list[Finding]:
    found = run({"project.yaml": project_text, "roles.yaml": roles_text}, t=t)
    return [f for f in found if f.code.endswith("_ACCOUNT_TYPE")]


def test_consumer_account_without_disclosure_is_an_error(t_en: Translator) -> None:
    """Sin ``disclosure`` vale ``milestones`` y ``require_commercial`` efectivo, ``true`` (D-48)."""
    roles = carla("consumer_training_off")
    (finding,) = run({"roles.yaml": roles}, t=t_en)
    assert (finding.code, finding.severity, finding.path) == ("E_VALIDATION_ACCOUNT_TYPE", "error", "roles.yaml")
    assert finding.pointer == "/people/2/account_type"
    assert finding.line == line_of(roles, "account_type: consumer_training_off")
    assert "`carla`" in finding.message and "`consumer_training_off`" in finding.message
    assert "by default" in finding.message and "`disclosure`" in finding.message
    assert finding.hint and "`require_commercial: false`" in finding.hint


@pytest.mark.parametrize("disclosure", [None, "private", "milestones"])
def test_explicit_false_leaves_only_the_c1_warning(disclosure: str | None, t_es: Translator) -> None:
    text = project(disclosure=disclosure, require_commercial="false")
    roles = carla("consumer_training_off")
    (finding,) = run({"project.yaml": text, "roles.yaml": roles}, t=t_es)
    assert (finding.code, finding.severity) == ("W_VALIDATION_ACCOUNT_TYPE", "warning")
    assert finding.pointer == "/people/2/account_type"
    assert finding.line == line_of(roles, "account_type: consumer_training_off")
    assert "`carla`" in finding.message and "términos de consumo" in finding.message
    assert finding.hint and "`memory/decisions/`" in finding.hint


def test_explicit_true_names_the_project_setting(t_en: Translator) -> None:
    (finding,) = accounts(project(require_commercial="true"), carla("consumer_training_off"), t=t_en)
    assert finding.code == "E_VALIDATION_ACCOUNT_TYPE"
    assert "`data_policy.require_commercial: true`" in finding.message and "by default" not in finding.message


def test_open_disclosure_with_internal_data_fails_the_schema() -> None:
    """REQ-SCHEMA-92: el esquema lo rechaza; sin configuración efectiva no se aplica la matriz."""
    found = run({"project.yaml": project(disclosure="open", level="internal"), "roles.yaml": carla("unknown")})
    assert {f.code for f in found} == {"E_VALIDATION_SCHEMA"}
    assert all(f.path == "project.yaml" for f in found)


@pytest.mark.parametrize("account_type", ["consumer_training_on", "consumer_training_off", "unknown"])
def test_open_and_public_admit_consumer_accounts(account_type: str) -> None:
    """Con ``disclosure: open`` y ``public``, ``require_commercial`` efectivo es ``false``."""
    text = project(disclosure="open", level="public")
    cfg, _ = parse_project(text)
    assert cfg is not None and cfg.data_policy["require_commercial"] is False
    assert run({"project.yaml": text, "roles.yaml": carla(account_type)}) == []


def test_public_data_that_is_not_open_still_excludes_consumer_accounts() -> None:
    """El pseudocódigo de 10 §8.4: con ``require_commercial`` efectivo ``true``, ninguna cuenta de consumo."""
    (finding,) = accounts(project(level="public"), carla("consumer_training_on"))
    assert finding.code == "E_VALIDATION_ACCOUNT_TYPE"
    assert accounts(project(level="public", require_commercial="false"), carla("consumer_training_on")) == []
    assert accounts(project(level="public"), carla("unknown")) == []  # ✓ en public


@pytest.mark.parametrize("require_commercial", [None, "false"])
def test_matrix_rejections_do_not_depend_on_require_commercial(require_commercial: str | None, t_en: Translator) -> None:
    for account_type in ("unknown", "consumer_training_on"):
        (finding,) = accounts(project(require_commercial=require_commercial), carla(account_type), t=t_en)
        assert finding.code == "E_VALIDATION_ACCOUNT_TYPE"
        assert f"`{account_type}` is not allowed with `data_policy.level: internal`" in finding.message


@pytest.mark.parametrize("account_type", ["team", "enterprise", "api", "cloud_provider", "local", "none"])
def test_commercial_local_and_none_pass_with_internal(account_type: str) -> None:
    assert run({"roles.yaml": carla(account_type)}) == []


def test_every_person_is_checked_even_if_inactive() -> None:
    """Como en el pseudocódigo de 10 §8.4, también las bajas (dario, ``/people/3``)."""
    roles = with_account(carla("consumer_training_off"), "dario", "unknown", active=False)
    found = accounts(project(), roles)
    assert [(f.code, f.pointer) for f in found] == [
        ("E_VALIDATION_ACCOUNT_TYPE", "/people/2/account_type"),
        ("E_VALIDATION_ACCOUNT_TYPE", "/people/3/account_type"),
    ]


def test_no_matrix_without_both_valid_files() -> None:
    bad_roles = carla("unknown").replace("lead: ana", "lead: nadie")
    assert accounts(PROJECT, bad_roles) == []  # roles.yaml no válido
    bad_project = PROJECT.replace("language: es", "language: fr")
    assert accounts(bad_project, carla("unknown")) == []  # project.yaml no válido


@pytest.mark.parametrize("lang", ["es", "en"])
def test_rendered_template_passes_with_require_commercial_true(lang: Lang) -> None:
    """La plantilla no escribe ``require_commercial`` (D-48) y sus personas de ejemplo son ``team``."""
    files = load_tool("render_template").render_files(lang)
    cfg, problems = parse_project(files["project.yaml"])
    assert problems == [] and cfg is not None
    assert cfg.disclosure == "milestones" and cfg.data_policy["require_commercial"] is True
    roles, problems = parse_roles(files["roles.yaml"])
    assert problems == [] and roles is not None
    assert {p.account_type for p in roles.people} == {"team"}
    found = run({}, tree=MemTree(files), t=translator(lang), mode="tree", head_ref=None)
    assert found == []


# -- registros de decisión -------------------------------------------------------------------


def test_valid_decision_passes() -> None:
    assert run({DECISION_PATH: decision()}, changes=[added(DECISION_PATH)], pr=pull()) == []


def test_only_added_or_changed_records_are_checked() -> None:
    broken = {DECISION_PATH: "no es un registro\n"}
    assert run(broken) == []  # sin cambios no se revisa
    assert run(broken, changes=[Change(path=DECISION_PATH, status="removed")]) == []
    assert "E_VALIDATION_FRONT_MATTER" in codes(run(broken, changes=[added(DECISION_PATH)]))


@pytest.mark.parametrize(
    "path",
    [
        "memory/decisions/2026-10-01-3-Usar.md",
        "memory/decisions/2026-10-01-0-x.md",
        "memory/decisions/x.md",
        "memory/decisions/2026-10-01-3-" + "a" * 49 + ".md",
        "memory/decisions/sub/2026-10-01-3-x.md",
    ],
)
def test_bad_record_names(path: str) -> None:
    findings = run({path: decision()}, changes=[added(path)])
    assert codes(findings) == ["E_VALIDATION_DECISION_NAME"]


def test_gitkeep_must_be_empty() -> None:
    gitkeep = "memory/decisions/.gitkeep"
    assert run({gitkeep: ""}, changes=[added(gitkeep)]) == []
    assert codes(run({gitkeep: "x"}, changes=[added(gitkeep)])) == ["E_VALIDATION_DECISION_NAME"]


@pytest.mark.parametrize(
    ("kwargs", "pointer"),
    [({"id_": "2026-10-01-3-otra"}, "/id"), ({"date": "2026-10-02"}, "/date"), ({"task": 4}, "/task")],
)
def test_identity_must_match_the_name(kwargs: dict[str, object], pointer: str) -> None:
    findings = run({DECISION_PATH: decision(**kwargs)}, changes=[added(DECISION_PATH)])  # type: ignore[arg-type]
    (finding,) = [f for f in findings if f.code == "E_VALIDATION_DECISION_NAME"]
    assert finding.pointer == pointer and finding.line is not None


def test_record_must_come_from_its_task_branch() -> None:
    findings = run({DECISION_PATH: decision()}, changes=[added(DECISION_PATH)], head_ref="task/4")
    assert codes(findings) == ["E_VALIDATION_DECISION_NAME"]
    assert "`task/4`" in findings[0].message


def test_missing_body_sections(t_en: Translator) -> None:
    body = "## Context\n\nx\n\n## Decision\n\nx\n"
    (finding,) = run({DECISION_PATH: decision(body=body)}, changes=[added(DECISION_PATH)], t=t_en)
    assert finding.code == "E_VALIDATION_DECISION_BODY" and "## Consequences" in finding.message
    (es,) = run({DECISION_PATH: decision(body=body)}, changes=[added(DECISION_PATH)])
    assert "## Consecuencias" in es.message


def test_supersedes_is_resolved_against_the_base() -> None:
    old = "2026-09-01-1-anterior"
    newer = "2026-09-15-2-otra"
    old_path = f"memory/decisions/{old}.md"
    newer_path = f"memory/decisions/{newer}.md"
    rec = decision(supersedes=[old])
    # no existe en la base
    (finding,) = run({DECISION_PATH: rec}, changes=[added(DECISION_PATH)])
    assert finding.code == "E_VALIDATION_DECISION_SUPERSEDES" and finding.pointer == "/supersedes/0"
    # existe y está vigente
    base_tree = MemTree({old_path: decision(id_=old, date="2026-09-01", task=1)})
    assert run({DECISION_PATH: rec}, changes=[added(DECISION_PATH)], policy=snapshot(decisions=[old_path]),
               base_tree=base_tree) == []
    # otro registro aceptado de la base ya lo reemplazó
    base_tree = MemTree({
        old_path: decision(id_=old, date="2026-09-01", task=1),
        newer_path: decision(id_=newer, date="2026-09-15", task=2, supersedes=[old]),
    })
    (finding,) = run({DECISION_PATH: rec}, changes=[added(DECISION_PATH)],
                     policy=snapshot(decisions=[old_path, newer_path]), base_tree=base_tree)
    assert finding.code == "E_VALIDATION_DECISION_SUPERSEDES"


def test_base_mode_resolves_supersedes_with_the_previous_state() -> None:
    old = "2026-09-01-1-anterior"
    old_path = f"memory/decisions/{old}.md"
    base_tree = MemTree({old_path: decision(id_=old, date="2026-09-01", task=1)})
    rec = decision(supersedes=[old])
    assert run({DECISION_PATH: rec, old_path: base_tree.read_text(old_path) or ""}, changes=[added(DECISION_PATH)],
               mode="base", head_ref=None, base_tree=base_tree) == []


def test_author_must_be_an_active_person_of_the_base() -> None:
    unknown = run({DECISION_PATH: decision(author="zoe")}, changes=[added(DECISION_PATH)])
    assert codes(unknown) == ["E_VALIDATION_UNKNOWN_PERSON"]
    inactive = run({DECISION_PATH: decision(author="dario")}, changes=[added(DECISION_PATH)],
                   policy=snapshot(roles=ROLES_INACTIVE))
    assert codes(inactive) == ["E_VALIDATION_INACTIVE_PERSON"]


def test_author_other_than_pr_author_is_a_warning() -> None:
    (finding,) = run({DECISION_PATH: decision(author="bruno")}, changes=[added(DECISION_PATH)], pr=pull("carla"))
    assert finding.code == "W_VALIDATION_DECISION_AUTHOR" and finding.severity == "warning"
    assert run({DECISION_PATH: decision(author="Carla")}, changes=[added(DECISION_PATH)], pr=pull("carla")) == []


# -- notas de sesión -----------------------------------------------------------------------


def test_valid_note_passes() -> None:
    text = note(block(1), block(2, date="2026-10-02"))
    assert run({NOTE_PATH: text}, changes=[added(NOTE_PATH)]) == []


def test_other_files_in_notes_are_not_this_rule() -> None:
    assert run({"memory/notes/otro.md": "x"}, changes=[added("memory/notes/otro.md")]) == []


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        (note(block(1), task=4), "`task`"),
        (note(f"{MARK} roto -->\nx\n"), None),
        (note(f"{MARK} {{no json}} -->\nx\n"), None),
        (note(f'{MARK} {{"n": 1}} -->\nx\n'), None),
        (note(block(2)), None),
        (note(block(1, date="2026-10-03"), block(2, date="2026-10-01")), None),
        (note(block(1, text="x" * 4100)), None),
    ],
)
def test_note_problems(text: str, fragment: str | None) -> None:
    findings = run({NOTE_PATH: text}, changes=[added(NOTE_PATH)])
    assert "E_VALIDATION_NOTE" in codes(findings), findings
    if fragment:
        assert any(fragment in f.message for f in findings)


def test_note_front_matter_must_match_schema() -> None:
    bad = note(block(1), front="---\ntask: 3\ncreated: hoy\ncreated_by: carla\n---\n")
    assert "E_VALIDATION_SCHEMA" in codes(run({NOTE_PATH: bad}, changes=[added(NOTE_PATH)]))


def test_note_too_large() -> None:
    big = note(block(1, text="x" * (65 * 1024)))
    assert codes(run({NOTE_PATH: big}, changes=[added(NOTE_PATH)])) == ["E_VALIDATION_NOTE"]


def test_note_block_logins_are_people_of_the_base() -> None:
    findings = run({NOTE_PATH: note(block(1, login="zoe"))}, changes=[added(NOTE_PATH)])
    assert codes(findings) == ["E_VALIDATION_UNKNOWN_PERSON"]


def test_rewritten_blocks_are_a_warning() -> None:
    before = note(block(1, text="Original."))
    base_tree = MemTree({NOTE_PATH: before})
    after = note(block(1, text="Reescrito."), block(2, date="2026-10-02"))
    (finding,) = run({NOTE_PATH: after}, changes=[Change(path=NOTE_PATH, status="modified")], base_tree=base_tree)
    assert finding.code == "W_VALIDATION_NOTE_REWRITTEN" and finding.severity == "warning"
    grown = note(block(1, text="Original."), block(2, date="2026-10-02"))
    assert run({NOTE_PATH: grown}, changes=[Change(path=NOTE_PATH, status="modified")], base_tree=base_tree) == []


# -- mode: individual (REQ-SCHEMA-94; 00-marco D-57) ---------------------------------------

SOLO = """version: 1
lead: ana
people:
  - login: ana
    name: "Ana Pérez"
    role: lead
    account_type: team
  - login: bruno
    name: "Bruno Díaz"
    role: researcher
    account_type: team
    active: false
sections:
  - id: introduction
    title: "Introduction"
    owner: ana
"""
INDIVIDUAL = PROJECT + "mode: individual\n"


def mode_findings(project_text: str, roles_text: str, *, t: Translator | None = None) -> list[Finding]:
    return [f for f in run({"project.yaml": project_text, "roles.yaml": roles_text}, t=t)
            if f.code == "E_VALIDATION_ROLES_MODE"]


def test_individual_with_one_active_person_passes() -> None:
    assert run({"project.yaml": INDIVIDUAL, "roles.yaml": SOLO}) == []  # la inactiva conserva su crédito
    assert mode_findings(PROJECT, ROLES) == []  # group: sin límite de personas


def test_individual_with_more_people_or_a_deputy_fails(t_en: Translator) -> None:
    people, deputy = mode_findings(INDIVIDUAL, ROLES, t=t_en)
    assert (people.pointer, people.path, people.severity) == ("/people", "roles.yaml", "error")
    assert people.line == line_of(ROLES, "people:")
    assert "there must be a single active person, the lead `ana`" in people.message
    assert (deputy.pointer, deputy.line) == ("/deputy", line_of(ROLES, "deputy: bruno"))
    other = SOLO.replace("    active: false\n", "")  # bruno activo: dos personas
    (finding,) = mode_findings(INDIVIDUAL, other)
    assert finding.pointer == "/people"
