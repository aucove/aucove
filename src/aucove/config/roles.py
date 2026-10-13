# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``roles.yaml`` (03-esquemas §4; 06-permisos §2.1).

``parse_roles`` valida contra ``roles.schema.json`` y, sobre el documento ya
validado, aplica REQ-SCHEMA-24 a REQ-SCHEMA-30 con su JSON Pointer (03 §4.5).
Los logins se comparan sin distinguir mayúsculas (``str.lower()``, REQ-SCHEMA-12);
``Roles`` guarda ``lead``, ``deputy`` y los dueños con la grafía de
``people[].login``, que es la que usan los artefactos generados.
``source_sha12`` es ``domain.text.sha12_canonical`` de los bytes del archivo (D-35 m).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from ..domain.findings import Finding, has_errors
from ..domain.text import orcid_ok, same_login, sha12_canonical
from ..i18n import Translator
from .schemas import validate
from .yamlload import SYNTAX_CODE, config_finding, default_t, line_for, load_with_lines

Role = Literal["lead", "researcher"]


@dataclass(frozen=True)
class Person:
    login: str
    name: str
    role: Role
    account_type: str
    active: bool = True
    email: str | None = None
    orcid: str | None = None
    forge_id: int | None = None
    credit: tuple[str, ...] = ()


@dataclass(frozen=True)
class SectionDecl:
    id: str
    title: str
    owner: str  # grafía de people[].login


@dataclass(frozen=True)
class Identity:
    """Resultado de :meth:`Roles.identify`: la persona de ``roles.yaml``, si la hay."""

    person: Person | None
    login_changed: bool  # True -> el llamador agrega W_AUTH_LOGIN_CHANGED (08-forja §6.4)

    @property
    def login(self) -> str | None:
        """Login con el que se evalúan los permisos: la grafía de ``people[].login``."""
        return self.person.login if self.person is not None else None


def _valid_forge_id(value: object) -> bool:
    return type(value) is int and value >= 1


@dataclass(frozen=True)
class Roles:
    version: int
    lead: str  # grafía de people[].login
    deputy: str | None
    people: tuple[Person, ...]
    sections: tuple[SectionDecl, ...]
    source_sha12: str
    editor: str | None = None  # editor del artículo declarado (D-56); None: lo es el lead

    @property
    def article_editor(self) -> str:
        """Editor del artículo: el declarado o, si falta, el ``lead`` (00-marco D-56)."""
        return self.editor or self.lead

    def person(self, login: str | None) -> Person | None:
        """Persona de ``people`` con ese login, sin distinguir mayúsculas."""
        if not isinstance(login, str):
            return None
        wanted = login.lower()
        for p in self.people:
            if p.login.lower() == wanted:
                return p
        return None

    def identify(self, login: str | None, forge_id: int | None = None) -> Identity:
        """Persona de la cuenta autenticada ``(login, forge_id)`` (08-forja §6.3 y §6.4).

        - Si alguna persona declara ``forge_id`` igual al id de la cuenta, es esa persona; si
          su login difiere, ``login_changed`` (aviso ``W_AUTH_LOGIN_CHANGED``).
        - Si no, decide el login; pero si esa persona declara un ``forge_id`` distinto del id
          conocido de la cuenta, no es ella: otra cuenta pudo tomar el login (REQ-FORGE-48).
        - Un ``forge_id`` repetido en ``people`` no identifica a nadie por id. Sin id
          conocido (``None``), solo se puede comparar el login.
        """
        if _valid_forge_id(forge_id):
            by_id = [p for p in self.people if p.forge_id == forge_id]
            if len(by_id) == 1:
                found = by_id[0]
                return Identity(found, not same_login(found.login, login))
            if by_id:  # forge_id repetido: ambiguo, se falla cerrado
                return Identity(None, False)
        elif forge_id is not None:  # id mal formado: no identifica por id ni deja pasar por login
            return Identity(None, False)
        found = self.person(login)
        if found is None:
            return Identity(None, False)
        if found.forge_id is not None and forge_id is not None and found.forge_id != forge_id:
            return Identity(None, False)
        return Identity(found, False)

    def section_owner(self) -> dict[str, str]:
        """``id`` de sección -> login del dueño con la grafía de ``people``."""
        return {s.id: s.owner for s in self.sections}


def mode_problems(roles: Roles, mode: str) -> list[tuple[str, Literal["people", "deputy"]]]:
    """Faltas a REQ-SCHEMA-94 de ``roles`` con ``project.yaml: mode`` = ``mode``: ``(puntero, motivo)``.

    Con ``mode: individual`` (00-marco D-57) hay una sola persona activa, la del ``lead``, y
    no hay ``deputy``; las inactivas pueden quedar, porque conservan su crédito. Con
    ``group`` no hay faltas. La usan la regla ``schemas`` (``E_VALIDATION_ROLES_MODE``) e
    ``init --individual``.
    """
    if mode != "individual":
        return []
    problems: list[tuple[str, Literal["people", "deputy"]]] = []
    active = [p for p in roles.people if p.active]
    if len(active) != 1 or not same_login(active[0].login, roles.lead):
        problems.append(("/people", "people"))
    if roles.deputy is not None:
        problems.append(("/deputy", "deputy"))
    return problems


def _source_bytes(text: str | bytes) -> bytes:
    return text if isinstance(text, bytes) else text.encode("utf-8")


class _Checker:
    """Reglas REQ-SCHEMA-24 a 30 sobre un ``roles.yaml`` que ya cumple el esquema."""

    def __init__(self, raw: Mapping[str, Any], lines: Mapping[str, int], *, file: str, t: Translator, check: str):
        self.raw = raw
        self.lines = lines
        self.file = file
        self.t = t
        self.check = check
        self.findings: list[Finding] = []
        self.people: list[dict[str, Any]] = list(raw["people"])
        self.by_login: dict[str, dict[str, Any]] = {}
        for person in self.people:  # con logins repetidos manda la primera aparición
            self.by_login.setdefault(person["login"].lower(), person)

    def add(self, code: str, pointer: str, *, severity: Literal["error", "warning"] = "error", **params: str) -> None:
        markers: dict[str, Any] = dict(params)
        self.findings.append(
            config_finding(
                code, t=self.t, file=self.file, line=line_for(self.lines, pointer), pointer=pointer,
                severity=severity, check=self.check, **markers,
            )
        )

    def case(self, written: str, pointer: str) -> None:
        """REQ-SCHEMA-30: la referencia difiere de ``people[].login`` solo en mayúsculas."""
        person = self.by_login.get(written.lower())
        if person is not None and person["login"] != written:
            self.add("W_VALIDATION_LOGIN_CASE", pointer, severity="warning", written=written, login=person["login"])

    def duplicates(self) -> None:  # REQ-SCHEMA-26 y REQ-SCHEMA-27
        seen: set[str] = set()
        for i, person in enumerate(self.people):
            key = person["login"].lower()
            if key in seen:
                self.add("E_VALIDATION_DUPLICATE_LOGIN", f"/people/{i}/login", login=person["login"])
            seen.add(key)
        ids: set[str] = set()
        for i, section in enumerate(self.raw["sections"]):
            if section["id"] in ids:
                self.add("E_VALIDATION_DUPLICATE_ID", f"/sections/{i}/id", id=section["id"])
            ids.add(section["id"])

    def lead(self) -> None:  # REQ-SCHEMA-24 (el esquema ya exige exactamente un role: lead)
        t = self.t
        lead = self.raw["lead"]
        index, holder = next((i, p) for i, p in enumerate(self.people) if p["role"] == "lead")
        if holder["login"].lower() != lead.lower():
            detail = t("config.roles.lead_mismatch", login=lead, person=holder["login"])
            self.add("E_VALIDATION_ROLES_LEAD", "/lead", detail=detail)
            return
        self.case(lead, "/lead")
        if holder.get("active", True) is not True:
            detail = t("config.roles.lead_inactive", login=holder["login"])
            self.add("E_VALIDATION_ROLES_LEAD", f"/people/{index}/active", detail=detail)

    def deputy(self) -> None:  # REQ-SCHEMA-25 y REQ-PERM-7
        t = self.t
        deputy = self.raw.get("deputy")
        if deputy is None:
            return
        person = self.by_login.get(deputy.lower())
        if person is None:
            self.add("E_VALIDATION_ROLES_DEPUTY", "/deputy", detail=t("config.roles.deputy_unknown", login=deputy))
            return
        self.case(deputy, "/deputy")
        if deputy.lower() == self.raw["lead"].lower():
            self.add("E_VALIDATION_ROLES_DEPUTY", "/deputy", detail=t("config.roles.deputy_is_lead", login=deputy))
        elif person["role"] != "researcher":
            self.add("E_VALIDATION_ROLES_DEPUTY", "/deputy", detail=t("config.roles.deputy_role", login=deputy))
        if person.get("active", True) is not True:
            self.add("E_VALIDATION_ROLES_DEPUTY", "/deputy", detail=t("config.roles.deputy_inactive", login=deputy))

    def editor(self) -> None:  # REQ-SCHEMA-95: persona activa de people, de cualquier rol
        editor = self.raw.get("editor")
        if editor is None:
            return
        ref = self.t("config.roles.ref_editor")
        person = self.by_login.get(editor.lower())
        if person is None:
            self.add("E_VALIDATION_UNKNOWN_PERSON", "/editor", login=editor, ref=ref)
            return
        self.case(editor, "/editor")
        if person.get("active", True) is not True:
            self.add("E_VALIDATION_INACTIVE_PERSON", "/editor", login=editor, ref=ref)

    def owners(self) -> None:  # REQ-SCHEMA-28 y REQ-SCHEMA-30
        for i, section in enumerate(self.raw["sections"]):
            owner = section["owner"]
            pointer = f"/sections/{i}/owner"
            ref = self.t("config.roles.ref_owner", section=section["id"])
            person = self.by_login.get(owner.lower())
            if person is None:
                self.add("E_VALIDATION_UNKNOWN_PERSON", pointer, login=owner, ref=ref)
                continue
            self.case(owner, pointer)
            if person.get("active", True) is not True:
                self.add("E_VALIDATION_INACTIVE_PERSON", pointer, login=owner, ref=ref)

    def orcids(self) -> None:  # REQ-SCHEMA-29
        for i, person in enumerate(self.people):
            orcid = person.get("orcid")
            if orcid is not None and not orcid_ok(orcid):
                self.add("E_VALIDATION_ORCID", f"/people/{i}/orcid", orcid=orcid)

    def run(self) -> list[Finding]:
        self.duplicates()
        self.lead()
        self.deputy()
        self.editor()
        self.owners()
        self.orcids()
        return self.findings


def _build(raw: Mapping[str, Any], sha12: str) -> Roles:
    people = tuple(
        Person(
            login=p["login"],
            name=p["name"],
            role=p["role"],
            account_type=p["account_type"],
            active=p.get("active", True),
            email=p.get("email"),
            orcid=p.get("orcid"),
            forge_id=p.get("forge_id"),
            credit=tuple(p.get("credit", ())),
        )
        for p in raw["people"]
    )
    spelled: dict[str, str] = {}
    for p in people:
        spelled.setdefault(p.login.lower(), p.login)
    deputy = raw.get("deputy")
    editor = raw.get("editor")
    return Roles(
        version=raw["version"],
        lead=spelled[raw["lead"].lower()],
        deputy=spelled[deputy.lower()] if deputy is not None else None,
        people=people,
        sections=tuple(
            SectionDecl(id=s["id"], title=s["title"], owner=spelled[s["owner"].lower()]) for s in raw["sections"]
        ),
        source_sha12=sha12,
        editor=spelled.get(editor.lower(), editor) if editor is not None else None,
    )


def parse_roles(
    text: str | bytes,
    *,
    file: str = "roles.yaml",
    t: Translator | None = None,
    check: str = "schemas",
) -> tuple[Roles | None, list[Finding]]:
    """``Roles`` de ``roles.yaml`` y todos sus hallazgos; ``None`` si hay algún error.

    Las advertencias (``W_VALIDATION_LOGIN_CASE``) no impiden construir ``Roles``.
    Sin ``t``, los mensajes salen en ``en``.
    """
    t = default_t(t)
    raw, lines, findings, _norm = load_with_lines(text, file=file, syntax_code=SYNTAX_CODE, t=t, check=check)
    if findings:
        return None, findings
    findings = validate("roles", raw, file=file, check=check, t=t, lines=lines)
    if findings or not isinstance(raw, dict):  # las reglas corren sobre el documento ya validado
        return None, findings
    findings = _Checker(raw, lines, file=file, t=t, check=check).run()
    findings.sort(key=lambda f: (f.line or 0, f.pointer or "", f.code))
    if has_errors(findings):
        return None, findings
    return _build(raw, sha12_canonical(_source_bytes(text))), findings
