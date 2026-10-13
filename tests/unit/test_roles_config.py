# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""roles.yaml (03-esquemas §4, REQ-SCHEMA-23..30; 06-permisos REQ-PERM-7; D-35 m)."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from aucove import _meta
from aucove.config.roles import Person, SectionDecl, parse_roles
from aucove.domain.text import sha12_canonical
from aucove.i18n import Translator
from tests.conftest import FIXTURES, roles_fixture

EXAMPLE = (FIXTURES / "config" / "roles" / "example-03.yaml").read_text(encoding="utf-8")
SEMANTIC = (FIXTURES / "config" / "roles" / "semantic-errors.yaml").read_text(encoding="utf-8")

PERSON = "  - login: {login}\n    name: \"{login}\"\n    role: {role}\n    account_type: team\n{extra}"
SECTION = "  - id: {id}\n    title: \"{id}\"\n    owner: {owner}\n"


def _roles(lead: str = "ana", deputy: str | None = None, people: str = "", sections: str = "") -> str:
    head = f"version: 1\nlead: {lead}\n" + (f"deputy: {deputy}\n" if deputy is not None else "")
    return head + "people:\n" + people + ("sections:\n" + sections if sections else "sections: []\n")


def _person(login: str, role: str = "researcher", extra: str = "") -> str:
    return PERSON.format(login=login, role=role, extra=extra)


def _section(id: str, owner: str) -> str:
    return SECTION.format(id=id, owner=owner)


def _codes(findings: list) -> list[tuple[str, str | None]]:  # type: ignore[type-arg]
    return [(f.code, f.pointer) for f in findings]


BASE_PEOPLE = _person("ana", "lead") + _person("bruno") + _person("carla")


# -- ejemplo de 03 §4.1 y fixtures compartidos ---------------------------------


def test_example_of_03_is_valid_with_one_login_case_warning() -> None:
    roles, findings = parse_roles(EXAMPLE)
    assert roles is not None
    assert _codes(findings) == [("W_VALIDATION_LOGIN_CASE", "/deputy")]
    assert findings[0].severity == "warning" and findings[0].line == 4 and findings[0].path == "roles.yaml"
    assert roles.lead == "ana-perez" and roles.deputy == "Bruno-Diaz"  # grafía de people
    assert roles.section_owner() == {"introduccion": "ana-perez", "metodos": "Bruno-Diaz", "resultados": "carla-soto"}
    diego = roles.person("DIEGO-LUNA")
    assert diego is not None and diego.active is False and diego.credit == ("software",)
    ana = roles.person("ana-perez")
    assert ana == Person(
        login="ana-perez", name="Ana Pérez Rojas", role="lead", account_type="team", active=True,
        email="ana.perez@uni.example", orcid="0000-0002-1825-0097", forge_id=None,
        credit=("conceptualization", "funding-acquisition", "supervision", "writing-review-editing"),
    )
    assert roles.person("nadie") is None and roles.person(None) is None
    assert roles.version == 1 and len(roles.people) == 4
    assert roles.sections[1] == SectionDecl(id="metodos", title="Métodos", owner="Bruno-Diaz")


def test_example_fixture_is_the_spec_block_when_available() -> None:
    spec_dir = os.environ.get(_meta.ENV_PREFIX + "SPEC_DIR")
    if not spec_dir:
        pytest.skip("sin la especificación")
    text = (Path(spec_dir) / "03-esquemas.md").read_text(encoding="utf-8")
    assert EXAMPLE in re.findall(r"```yaml\n(.*?)```", text, re.S)


@pytest.mark.parametrize("name", ["basic", "no-deputy"])
def test_shared_fixtures_are_clean(name: str) -> None:
    roles, findings = parse_roles(roles_fixture(name))
    assert findings == [] and roles is not None
    assert roles.lead == "ana"
    assert roles.deputy == ("bruno" if name == "basic" else None)
    assert roles.section_owner() == {"introduction": "ana", "methods": "carla", "results": "bruno"}


def test_mixed_case_fixture_gives_exactly_one_warning() -> None:
    roles, findings = parse_roles(roles_fixture("mixed-case"))
    assert roles is not None
    assert _codes(findings) == [("W_VALIDATION_LOGIN_CASE", "/sections/1/owner")]
    assert roles.section_owner()["methods"] == "Carla" and roles.lead == "Ana"


# -- REQ-SCHEMA-24..30: todas las reglas, con su pointer --------------------------------


def test_all_semantic_problems_are_reported() -> None:
    roles, findings = parse_roles(SEMANTIC)
    assert roles is None
    assert sorted(_codes(findings)) == sorted(
        [
            ("E_VALIDATION_ROLES_LEAD", "/people/0/active"),
            ("E_VALIDATION_ROLES_DEPUTY", "/deputy"),
            ("E_VALIDATION_ROLES_DEPUTY", "/deputy"),
            ("E_VALIDATION_ORCID", "/people/1/orcid"),
            ("E_VALIDATION_DUPLICATE_LOGIN", "/people/2/login"),
            ("E_VALIDATION_UNKNOWN_PERSON", "/sections/0/owner"),
            ("E_VALIDATION_DUPLICATE_ID", "/sections/1/id"),
            ("E_VALIDATION_INACTIVE_PERSON", "/sections/1/owner"),
        ]
    )
    lines = {(f.code, f.pointer): f.line for f in findings}
    assert lines[("E_VALIDATION_ROLES_DEPUTY", "/deputy")] == 4
    assert lines[("E_VALIDATION_UNKNOWN_PERSON", "/sections/0/owner")] == 28
    assert [f.line for f in findings] == sorted(f.line for f in findings)  # type: ignore[type-var]


def test_lead_must_match_the_person_with_role_lead() -> None:
    roles, findings = parse_roles(_roles(lead="bruno", people=BASE_PEOPLE))
    assert roles is None and _codes(findings) == [("E_VALIDATION_ROLES_LEAD", "/lead")]


def test_lead_written_in_other_case_is_only_a_warning() -> None:
    roles, findings = parse_roles(_roles(lead="ANA", people=BASE_PEOPLE))
    assert roles is not None and roles.lead == "ana"
    assert _codes(findings) == [("W_VALIDATION_LOGIN_CASE", "/lead")]


def test_lead_must_be_active() -> None:
    people = _person("ana", "lead", "    active: false\n") + _person("bruno")
    roles, findings = parse_roles(_roles(people=people))
    assert roles is None and _codes(findings) == [("E_VALIDATION_ROLES_LEAD", "/people/0/active")]


def test_exactly_one_role_lead_is_enforced_by_the_schema() -> None:
    roles, findings = parse_roles(_roles(people=_person("ana") + _person("bruno")))
    assert roles is None and _codes(findings) == [("E_VALIDATION_SCHEMA", "/people")]


@pytest.mark.parametrize(
    ("deputy", "people", "count"),
    [
        ("zoe", BASE_PEOPLE, 1),  # no está en people
        ("ana", BASE_PEOPLE, 1),  # es el lead
        ("bruno", _person("ana", "lead") + _person("bruno", extra="    active: false\n"), 1),  # inactivo
    ],
)
def test_deputy_rules(deputy: str, people: str, count: int) -> None:
    """REQ-SCHEMA-25 y REQ-PERM-7."""
    roles, findings = parse_roles(_roles(deputy=deputy, people=people))
    assert roles is None and _codes(findings) == [("E_VALIDATION_ROLES_DEPUTY", "/deputy")] * count


def test_deputy_null_and_case_warning() -> None:
    roles, findings = parse_roles(_roles(deputy="null", people=BASE_PEOPLE))
    assert findings == [] and roles is not None and roles.deputy is None
    roles, findings = parse_roles(_roles(deputy="Bruno", people=BASE_PEOPLE))
    assert roles is not None and roles.deputy == "bruno"
    assert _codes(findings) == [("W_VALIDATION_LOGIN_CASE", "/deputy")]


def test_logins_are_unique_without_case() -> None:
    roles, findings = parse_roles(_roles(people=BASE_PEOPLE + _person("CARLA")))
    assert roles is None and _codes(findings) == [("E_VALIDATION_DUPLICATE_LOGIN", "/people/3/login")]


def test_section_ids_are_unique() -> None:
    sections = _section("methods", "ana") + _section("methods", "bruno")
    roles, findings = parse_roles(_roles(people=BASE_PEOPLE, sections=sections))
    assert roles is None and _codes(findings) == [("E_VALIDATION_DUPLICATE_ID", "/sections/1/id")]


def test_owner_must_exist_and_be_active(t_es: Translator) -> None:
    people = BASE_PEOPLE + _person("dario", extra="    active: false\n")
    sections = _section("intro", "zoe") + _section("methods", "dario") + _section("results", "Carla")
    roles, findings = parse_roles(_roles(people=people, sections=sections), t=t_es)
    assert roles is None
    assert _codes(findings) == [
        ("E_VALIDATION_UNKNOWN_PERSON", "/sections/0/owner"),
        ("E_VALIDATION_INACTIVE_PERSON", "/sections/1/owner"),
        ("W_VALIDATION_LOGIN_CASE", "/sections/2/owner"),
    ]
    assert "intro" in findings[0].message and "zoe" in findings[0].message
    assert findings[0].hint is not None and "people" in findings[0].hint


def test_inactive_person_without_a_role_is_fine() -> None:
    roles, findings = parse_roles(_roles(people=BASE_PEOPLE + _person("dario", extra="    active: false\n")))
    assert findings == [] and roles is not None


@pytest.mark.parametrize(("orcid", "ok"), [("0000-0002-1825-0097", True), ("0000-0002-1825-0098", False),
                                          ("0000-0001-5109-3700", True), ("0000-0002-9079-593X", True)])
def test_orcid_check_digit(orcid: str, ok: bool) -> None:
    people = _person("ana", "lead", f'    orcid: "{orcid}"\n') + _person("bruno")
    roles, findings = parse_roles(_roles(people=people))
    assert (roles is not None) is ok
    if not ok:
        assert _codes(findings) == [("E_VALIDATION_ORCID", "/people/0/orcid")] and orcid in findings[0].message


def test_semantic_rules_wait_for_the_schema() -> None:
    """Las reglas corren sobre el documento ya validado (03 §4.5)."""
    sections = _section("none", "zoe")
    roles, findings = parse_roles(_roles(people=BASE_PEOPLE, sections=sections))
    assert roles is None and _codes(findings) == [("E_VALIDATION_SCHEMA", "/sections/0/id")]


def test_yaml_problems() -> None:
    roles, findings = parse_roles("version: 1\nlead: ana\nlead: bea\n")
    assert roles is None and _codes(findings) == [("E_CONFIG_YAML", None)] and findings[0].line == 3


def test_schema_problems_are_all_reported() -> None:
    roles, findings = parse_roles("version: 2\nlead: -ana\npeople: []\n")
    assert roles is None
    assert {p for _c, p in _codes(findings)} == {"/version", "/lead", "/people", ""}


def test_optional_person_fields() -> None:
    people = _person("ana", "lead", "    forge_id: 12345\n    email: ana@uni.example\n    credit: [software]\n")
    roles, findings = parse_roles(_roles(people=people))
    assert findings == [] and roles is not None
    ana = roles.people[0]
    assert (ana.forge_id, ana.email, ana.credit, ana.active) == (12345, "ana@uni.example", ("software",), True)


# -- source_sha12 (D-35 m) -----------------------------------------------------------


def test_source_sha12_is_the_canonical_hash_of_the_bytes() -> None:
    raw = roles_fixture("basic").encode("utf-8")
    roles, _ = parse_roles(roles_fixture("basic"))
    assert roles is not None and roles.source_sha12 == sha12_canonical(raw)
    assert re.fullmatch(r"[0-9a-f]{12}", roles.source_sha12)
    crlf, _ = parse_roles(b"\xef\xbb\xbf" + raw.replace(b"\n", b"\r\n"))
    assert crlf is not None and crlf.source_sha12 == roles.source_sha12
    other, _ = parse_roles(roles_fixture("basic") + "# otro comentario\n")
    assert other is not None and other.source_sha12 != roles.source_sha12


def test_messages_follow_the_translator(t_es: Translator, t_en: Translator) -> None:
    _r, es = parse_roles(EXAMPLE, t=t_es)
    _r, en = parse_roles(EXAMPLE, t=t_en)
    assert "mayúsculas" in es[0].message and "letter case" in en[0].message
    assert "Bruno-Diaz" in (es[0].hint or "") and "Bruno-Diaz" in (en[0].hint or "")


def test_file_and_check_are_passed_through() -> None:
    _r, findings = parse_roles(EXAMPLE, file="pr/roles.yaml", check="roles-sync")
    assert findings[0].path == "pr/roles.yaml" and findings[0].check == "roles-sync"
