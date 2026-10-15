# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""project.yaml efectivo (03-esquemas §3, REQ-SCHEMA-14..17, 83, 84, 90..92; 01 §6.2)."""

from __future__ import annotations

import base64
import os
import re
import tomllib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aucove import _meta
from aucove.config import project as project_mod
from aucove.config import schemas
from aucove.config.project import DEFAULTS, ROOT_DEFAULTS, core_pin_line, default_require_commercial, parse_project
from aucove.domain.signing import key_blob
from aucove.i18n import Translator
from tests.conftest import FIXTURES, REPO_ROOT

EXAMPLE = (FIXTURES / "config" / "project" / "example-03.yaml").read_text(encoding="utf-8")
MINIMAL = (FIXTURES / "config" / "project" / "minimal.yaml").read_text(encoding="utf-8")


def _replace(text: str, old: str, new: str) -> str:
    assert old in text, old
    return text.replace(old, new, 1)


def _codes(findings: list) -> list[tuple[str, str | None]]:  # type: ignore[type-arg]
    return [(f.code, f.pointer) for f in findings]


# -- ejemplo de 03 §3.1 y valores por defecto ---------------------------------------


def test_example_of_03_is_valid() -> None:
    cfg, findings = parse_project(EXAMPLE)
    assert findings == [] and cfg is not None
    assert (cfg.convention, cfg.core, cfg.core_pin) == ("0.1", ">=0.1.3,<0.2", "0.1.3")
    assert (cfg.name, cfg.language, cfg.format, cfg.disclosure) == ("movilidad-teletrabajo", "es", "latex", "milestones")
    assert (cfg.forge_type, cfg.forge_repo, cfg.base_branch) == ("github", "lab-movilidad/movilidad-teletrabajo", "main")
    assert cfg.build.bib_files == ("bib/references.bib",) and cfg.build.timeout_minutes == 10
    assert cfg.data_policy["human_subjects"] is True and cfg.data_policy["ethics_ref"] == "CEC-2026-114"
    assert cfg.data_policy["require_commercial"] is True
    assert cfg.licenses == {"holder": "Los autores de movilidad-teletrabajo", "text": "LicenseRef-AllRightsReserved",
                            "code": "MIT", "data": "LicenseRef-AllRightsReserved"}
    assert cfg.ci["model_review"] == {"enabled": False, "max_turns": 4}
    assert cfg.raw["title"] == cfg.title


def test_example_fixture_is_the_spec_block_when_available() -> None:
    spec_dir = os.environ.get(_meta.ENV_PREFIX + "SPEC_DIR")
    if not spec_dir:
        pytest.skip("sin la especificación")
    text = (Path(spec_dir) / "03-esquemas.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\n(.*?)```", text, re.S)
    assert EXAMPLE in blocks


def test_minimal_file_gets_the_defaults_of_the_table() -> None:
    cfg, findings = parse_project(MINIMAL)
    assert findings == [] and cfg is not None
    assert cfg.base_branch == "main"
    build = DEFAULTS["build"]
    assert (cfg.build.engine, cfg.build.main, cfg.build.image, cfg.build.bibliography) == (
        build["engine"], build["main"], build["image"], build["bibliography"]
    )
    assert cfg.build.bib_files == build["bib_files"]
    assert (cfg.build.shell_escape, cfg.build.compile_on_submit, cfg.build.timeout_minutes) == ("disabled", "if_available", 10)
    assert dict(cfg.limits) == dict(DEFAULTS["limits"])
    assert dict(cfg.context) == dict(DEFAULTS["context"])
    # Sin disclosure vale milestones y, por eso, require_commercial vale true (REQ-SCHEMA-91).
    assert cfg.disclosure == ROOT_DEFAULTS["disclosure"] == "milestones"
    assert dict(cfg.data_policy) == {**DEFAULTS["data_policy"], "require_commercial": True}
    assert dict(cfg.coordinator) == {"require_direction_ack": False} and dict(cfg.review) == {"sla_hours": 72}
    assert cfg.ci["pdf_retention_days"] == 14 and cfg.ci["latexdiff"] is False
    assert cfg.licenses is None and cfg.advisory_acceptance is None
    # el documento no se modifica (REQ-SCHEMA-15)
    assert "build" not in cfg.raw and "disclosure" not in cfg.raw and "data_policy" not in cfg.raw


def _schema_defaults(node: object, prefix: str, out: dict[str, object]) -> None:
    if not isinstance(node, dict):
        return
    for key, sub in node.get("properties", {}).items():
        if isinstance(sub, dict) and "$ref" in sub:
            sub = schemas.schema("project")["$defs"][sub["$ref"].rsplit("/", 1)[1]]
        if isinstance(sub, dict) and "default" in sub:
            out[f"{prefix}{key}"] = sub["default"]
        _schema_defaults(sub, f"{prefix}{key}.", out)


def test_defaults_agree_with_the_informative_schema_defaults() -> None:
    expected: dict[str, object] = {}
    _schema_defaults(schemas.schema("project"), "", expected)
    ours: dict[str, object] = dict(ROOT_DEFAULTS)
    for section, values in DEFAULTS.items():
        for key, value in values.items():
            ours[f"{section}.{key}"] = list(value) if isinstance(value, tuple) else value
    assert ours == expected
    # El único valor por defecto condicional no está en el esquema ni en DEFAULTS (REQ-SCHEMA-91).
    assert "data_policy.require_commercial" not in expected


# -- REQ-SCHEMA-90 a 92: format, disclosure y require_commercial ------------------------


@pytest.mark.parametrize(
    ("disclosure", "given", "effective"),
    [
        (None, None, True),
        ("private", None, True),
        ("milestones", None, True),
        ("open", None, False),
        (None, "false", False),
        ("milestones", "false", False),
        ("open", "true", True),
    ],
)
def test_require_commercial_depends_on_disclosure(disclosure: str | None, given: str | None, effective: bool) -> None:
    """REQ-SCHEMA-91: si falta, ``true`` salvo con ``disclosure: open``; si se escribe, manda el archivo."""
    text = MINIMAL
    if disclosure is not None:
        text = _replace(text, "format: latex\n", f"format: latex\ndisclosure: {disclosure}\n")
    level = "public" if disclosure == "open" else "internal"  # REQ-SCHEMA-92
    text += f"data_policy:\n  level: {level}\n"
    if given is not None:
        text += f"  require_commercial: {given}\n"
    cfg, findings = parse_project(text)
    assert findings == [] and cfg is not None
    assert cfg.disclosure == (disclosure or "milestones")
    assert cfg.data_policy["require_commercial"] is effective
    assert default_require_commercial(cfg.disclosure) is (cfg.disclosure != "open")
    assert ("require_commercial" in cfg.raw["data_policy"]) is (given is not None)  # type: ignore[operator]


@pytest.mark.parametrize(
    ("data_policy", "ok"),
    [
        ("", False),  # sin data_policy, el nivel efectivo sería internal
        ("data_policy:\n  human_subjects: false\n", False),
        ("data_policy:\n  level: internal\n", False),
        ("data_policy:\n  level: public\n", True),
    ],
)
def test_open_disclosure_requires_public_data(data_policy: str, ok: bool) -> None:
    """REQ-SCHEMA-92: con ``disclosure: open``, ``data_policy.level`` debe ser ``public`` (lo exige el esquema)."""
    text = _replace(MINIMAL, "format: latex\n", "format: latex\ndisclosure: open\n") + data_policy
    cfg, findings = parse_project(text)
    assert (cfg is not None) is ok
    if not ok:
        assert findings and {f.code for f in findings} == {"E_VALIDATION_SCHEMA"}
    else:
        assert cfg is not None and cfg.data_policy["require_commercial"] is False


@pytest.mark.parametrize("disclosure", ["private", "milestones"])
def test_internal_data_is_fine_when_not_open(disclosure: str) -> None:
    text = _replace(MINIMAL, "format: latex\n", f"format: latex\ndisclosure: {disclosure}\n")
    cfg, findings = parse_project(text + "data_policy:\n  level: internal\n")
    assert findings == [] and cfg is not None and cfg.disclosure == disclosure


@pytest.mark.parametrize("value", ["public", "Open", "", "null", "true"])
def test_unknown_disclosure_is_a_schema_error(value: str) -> None:
    cfg, findings = parse_project(_replace(MINIMAL, "format: latex\n", f"format: latex\ndisclosure: {value}\n"))
    assert cfg is None and _codes(findings) == [("E_VALIDATION_SCHEMA", "/disclosure")]


def test_format_none_is_read_and_exposed() -> None:
    """D-43: ``format: none`` es válido sin ``build``; la configuración efectiva lo expone."""
    cfg, findings = parse_project(_replace(MINIMAL, "format: latex", "format: none"))
    assert findings == [] and cfg is not None
    assert cfg.format == "none" and cfg.raw["format"] == "none"


def test_format_none_rejects_the_build_block() -> None:
    """REQ-SCHEMA-90: con ``format: none``, el esquema no admite ``build``."""
    text = _replace(MINIMAL, "format: latex", "format: none") + "build:\n  engine: pdflatex\n"
    cfg, findings = parse_project(text)
    assert cfg is None and findings and {f.code for f in findings} == {"E_VALIDATION_SCHEMA"}
    assert parse_project(MINIMAL + "build:\n  engine: pdflatex\n")[0] is not None  # con latex, sí


@pytest.mark.parametrize("value", ["markdown", "quarto", "LaTeX"])
def test_reserved_or_unknown_formats_are_rejected(value: str) -> None:
    cfg, findings = parse_project(_replace(MINIMAL, "format: latex", f"format: {value}"))
    assert cfg is None and _codes(findings) == [("E_VALIDATION_SCHEMA", "/format")]


def test_format_is_required() -> None:
    cfg, findings = parse_project(_replace(MINIMAL, "format: latex\n", ""))
    assert cfg is None and _codes(findings) == [("E_VALIDATION_SCHEMA", "")]


def test_evidence_key_is_reserved() -> None:
    """03 §3.2: la clave ``evidence`` está reservada (el sellado no se configura en v0.1)."""
    cfg, findings = parse_project(MINIMAL + "evidence:\n  tsa: []\n")
    assert cfg is None and _codes(findings) == [("E_VALIDATION_SCHEMA", "/evidence")]


# -- evidence_signing (00-marco D-72; 03 §3.2; REQ-SCHEMA-96) ---------------------------------------

KEY_A = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHO4TeimWDqbJtD9DZ0jEQ7osipbPsIQKXvEpl+4J8Ve"
KEY_B = "ssh-ed25519 " + base64.b64encode(key_blob(b"\x02" * 32)).decode("ascii")


def signing_block(*keys: str, mode: str = "ci") -> str:
    return f"evidence_signing:\n  mode: {mode}\n" + ("  keys:\n" + "".join(keys) if keys else "")


def key_item(key_id: str, public: str, since: str = "2026-02-01T00:00:00Z", until: str | None = None) -> str:
    text = f'    - id: {key_id}\n      public_key: "{public}"\n      since: "{since}"\n'
    return text + (f'      until: "{until}"\n' if until is not None else "")


def test_without_evidence_signing_nobody_signs() -> None:
    cfg, findings = parse_project(MINIMAL)
    assert findings == [] and cfg is not None
    assert (cfg.evidence_signing.mode, cfg.evidence_signing.keys) == ("none", ())
    none_cfg, findings = parse_project(MINIMAL + signing_block(mode="none"))
    assert findings == [] and none_cfg is not None and none_cfg.evidence_signing.mode == "none"


def test_evidence_signing_gives_the_mode_and_the_keys() -> None:
    text = MINIMAL + signing_block(
        key_item("ci-2025", KEY_A + " ana@laptop", "2025-01-01T00:00:00Z", "2026-02-01T00:00:00Z"),
        key_item("ci-2026", KEY_B),
    )
    cfg, findings = parse_project(text)
    assert findings == [] and cfg is not None
    signing_cfg = cfg.evidence_signing
    assert signing_cfg.mode == "ci" and [k.id for k in signing_cfg.keys] == ["ci-2025", "ci-2026"]
    assert signing_cfg.keys[0].until == datetime(2026, 2, 1, tzinfo=timezone.utc)
    assert signing_cfg.keys[1].until is None and signing_cfg.keys[1].public_key == KEY_B
    # sin comillas, las fechas siguen siendo texto (REQ-SCHEMA-3)
    bare = MINIMAL + signing_block(key_item("ci", KEY_A).replace('"2026-02-01T00:00:00Z"', "2026-02-01T00:00:00Z"))
    cfg, findings = parse_project(bare)
    assert findings == [] and cfg is not None and cfg.evidence_signing.keys[0].id == "ci"


@pytest.mark.parametrize(
    ("keys", "pointer", "reason"),
    [
        ((key_item("ci", KEY_A), key_item("ci", KEY_B)), "/evidence_signing/keys/1/id", "duplicate_id"),
        ((key_item("a", KEY_A), key_item("b", KEY_A + " otra copia")), "/evidence_signing/keys/1/public_key",
         "duplicate_key"),
        ((key_item("a", KEY_A, until="2026-02-01T00:00:00Z"),), "/evidence_signing/keys/0/until",
         "until_not_after_since"),
        ((key_item("a", KEY_A, until="2025-02-01T00:00:00Z"),), "/evidence_signing/keys/0/until",
         "until_not_after_since"),
    ],
)
def test_evidence_key_rules_are_config_invalid(keys: tuple[str, ...], pointer: str, reason: str,
                                               t_es: Translator) -> None:
    cfg, findings = parse_project(MINIMAL + signing_block(*keys), t=t_es)
    assert cfg is None and _codes(findings) == [("E_CONFIG_INVALID", pointer)]
    assert t_es(f"config.project.evidence_key.{reason}") in findings[0].message


@pytest.mark.parametrize(
    ("block", "pointer"),
    [
        (signing_block(mode="ci"), "/evidence_signing"),  # ci o admin sin llaves
        (signing_block(mode="admin"), "/evidence_signing"),
        ("evidence_signing:\n  mode: ci\n  keys: []\n", "/evidence_signing/keys"),
        (signing_block(key_item("a", KEY_A), mode="siempre"), "/evidence_signing/mode"),
        (signing_block(key_item("a", "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAAAgQC")), "/evidence_signing/keys/0/public_key"),
        (signing_block(key_item("Ana", KEY_A)), "/evidence_signing/keys/0/id"),
        (signing_block(key_item("a", KEY_A, "2026-02-01")), "/evidence_signing/keys/0/since"),
        (signing_block(key_item("a", KEY_A, "2026-02-01T00:00:00+00:00")), "/evidence_signing/keys/0/since"),
        (signing_block(key_item("a", KEY_A) + "      revoked: true\n"), "/evidence_signing/keys/0"),
        ("evidence_signing:\n  mode: ci\n  llave: x\n", "/evidence_signing"),
    ],
)
def test_evidence_signing_schema_errors(block: str, pointer: str) -> None:
    cfg, findings = parse_project(MINIMAL + block)
    assert cfg is None
    schema = [p or "" for code, p in _codes(findings) if code == "E_VALIDATION_SCHEMA"]
    assert any(p.startswith(pointer) for p in schema), _codes(findings)


def test_more_than_fifty_keys_is_a_schema_error() -> None:
    keys = [key_item(f"k{i}", "ssh-ed25519 " + base64.b64encode(key_blob(bytes([i + 1]) * 32)).decode("ascii"))
            for i in range(51)]
    cfg, findings = parse_project(MINIMAL + signing_block(*keys))
    assert cfg is None and ("E_VALIDATION_SCHEMA", "/evidence_signing/keys") in _codes(findings)


def test_effective_config_is_read_only() -> None:
    cfg, _ = parse_project(MINIMAL)
    assert cfg is not None
    with pytest.raises(TypeError):
        cfg.limits["max_active_claims"] = 9  # type: ignore[index]
    with pytest.raises(TypeError):
        cfg.raw["name"] = "x"  # type: ignore[index]


def test_integral_floats_become_ints() -> None:
    cfg, findings = parse_project(MINIMAL + "build:\n  timeout_minutes: 12.0\nlimits:\n  claim_expiry_days: 20.0\n")
    assert findings == [] and cfg is not None
    assert cfg.build.timeout_minutes == 12 and isinstance(cfg.build.timeout_minutes, int)
    assert cfg.limits["claim_expiry_days"] == 20 and isinstance(cfg.limits["claim_expiry_days"], int)


def test_advisory_acceptance_and_model_review() -> None:
    text = _replace(EXAMPLE, "  base_branch: main\n",
                    '  base_branch: main\n  advisory_acceptance:\n    by: ana-perez\n    at: "2026-10-01T14:05:00Z"\n')
    text = _replace(text, "    enabled: false\n", "    enabled: true\n    auth: oidc\n    model: claude-sonnet-5\n")
    cfg, findings = parse_project(text)
    assert findings == [] and cfg is not None
    assert cfg.advisory_acceptance == {"by": "ana-perez", "at": "2026-10-01T14:05:00Z"}
    assert cfg.ci["model_review"] == {"enabled": True, "auth": "oidc", "model": "claude-sonnet-5", "max_turns": 4}


def test_model_review_enabled_needs_auth_and_model() -> None:
    cfg, findings = parse_project(_replace(EXAMPLE, "    enabled: false\n", "    enabled: true\n"))
    assert cfg is None and {p for _c, p in _codes(findings)} == {"/ci/model_review"}


# -- REQ-SCHEMA-16: sin comillas -----------------------------------------------------


def test_unquoted_convention_gets_the_quote_hint(t_es: Translator) -> None:
    cfg, findings = parse_project(_replace(EXAMPLE, 'convention: "0.1"', "convention: 0.1"), t=t_es)
    assert cfg is None
    assert _codes(findings) == [("E_VALIDATION_SCHEMA", "/convention")]
    assert findings[0].line == 2 and "escríbelo entre comillas" in (findings[0].hint or "").lower()


@pytest.mark.parametrize("value", [">=0.1.3,<0.2", "!=0.1.0", ">0.1", "&x 0.1"])
def test_unquoted_core_syntax_error_gets_the_quote_hint(value: str, t_es: Translator) -> None:
    cfg, findings = parse_project(_replace(EXAMPLE, 'core: ">=0.1.3,<0.2"', f"core: {value}"), t=t_es)
    assert cfg is None and [c for c, _p in _codes(findings)] == ["E_CONFIG_YAML"]
    assert findings[0].line == 3 and "entre comillas" in (findings[0].hint or "")


def test_other_syntax_errors_keep_the_generic_hint(t_es: Translator) -> None:
    cfg, findings = parse_project(_replace(EXAMPLE, "title: ", "title: [unclosed "), t=t_es)
    assert cfg is None and findings[0].code == "E_CONFIG_YAML"
    assert "entre comillas" not in (findings[0].hint or "")


def test_unquoted_core_pin_is_invalid_with_the_quote_hint(t_en: Translator) -> None:
    cfg, findings = parse_project(_replace(EXAMPLE, 'core_pin: "0.1.3"', "core_pin: 0.1.3"), t=t_en)
    assert cfg is None and _codes(findings) == [("E_CONFIG_INVALID", "/core_pin")]
    assert findings[0].line == 4 and "quotes" in (findings[0].hint or "")


def test_numeric_core_pin_is_a_schema_error_with_the_quote_hint(t_en: Translator) -> None:
    cfg, findings = parse_project(_replace(EXAMPLE, 'core_pin: "0.1.3"', "core_pin: 0.1"), t=t_en)
    assert cfg is None and _codes(findings) == [("E_VALIDATION_SCHEMA", "/core_pin")]
    assert "quotes" in (findings[0].hint or "")


# -- REQ-SCHEMA-83 y REQ-ARCH-45 -----------------------------------------------------


@pytest.mark.parametrize("line", ["core_pin: '0.1.3'", 'core_pin:  "0.1.3"', 'core_pin : "0.1.3"', '"core_pin": "0.1.3"'])
def test_core_pin_line_out_of_format_is_config_invalid(line: str) -> None:
    cfg, findings = parse_project(_replace(EXAMPLE, 'core_pin: "0.1.3"', line))
    assert cfg is None and _codes(findings) == [("E_CONFIG_INVALID", "/core_pin")]
    assert findings[0].line == 4


@pytest.mark.parametrize("line", ['core_pin: "0.1.3" # fijado por init', 'core_pin: "0.1.3"'])
def test_core_pin_line_with_comment_is_valid(line: str) -> None:
    cfg, findings = parse_project(_replace(EXAMPLE, 'core_pin: "0.1.3"', line))
    assert findings == [] and cfg is not None


def test_core_pin_prerelease_is_valid() -> None:
    """00-marco D-66: una pre-release de PEP 440 en su forma normalizada, dentro de ``core``."""
    text = _replace(EXAMPLE, 'core: ">=0.1.3,<0.2"', 'core: ">=0.1.0rc1,<0.2"')
    cfg, findings = parse_project(_replace(text, 'core_pin: "0.1.3"', 'core_pin: "0.1.0rc1"'))
    assert findings == [] and cfg is not None and cfg.core_pin == "0.1.0rc1"


def test_core_pin_prerelease_below_core_is_config_invalid() -> None:
    cfg, findings = parse_project(_replace(EXAMPLE, 'core_pin: "0.1.3"', 'core_pin: "0.1.3rc1"'))
    assert cfg is None and _codes(findings) == [("E_CONFIG_INVALID", "/core_pin")]  # 0.1.3rc1 < 0.1.3


@pytest.mark.parametrize(
    "pin", ["0.1.3-rc1", "0.1.3.rc1", "0.1.3RC1", "0.1.3c1", "0.1.3rc", "0.1.3.dev1", "0.1.3rc1.post1", "0.1.3+x"]
)
def test_core_pin_outside_the_normalized_forms_is_a_schema_error(pin: str) -> None:
    cfg, findings = parse_project(_replace(EXAMPLE, 'core_pin: "0.1.3"', f'core_pin: "{pin}"'))
    assert cfg is None and _codes(findings) == [("E_VALIDATION_SCHEMA", "/core_pin")]  # D-66


def test_core_pin_in_a_flow_mapping_is_config_invalid() -> None:
    cfg, _ = parse_project(MINIMAL)
    assert cfg is not None
    import json

    flow = json.dumps(dict(cfg.raw), ensure_ascii=False) + "\n"
    cfg2, findings = parse_project(flow)
    assert cfg2 is None and _codes(findings) == [("E_CONFIG_INVALID", "/core_pin")]


def test_core_pin_must_satisfy_core(t_es: Translator) -> None:
    cfg, findings = parse_project(_replace(EXAMPLE, 'core_pin: "0.1.3"', 'core_pin: "0.2.0"'), t=t_es)
    assert cfg is None and _codes(findings) == [("E_CONFIG_INVALID", "/core_pin")]
    assert "0.2.0" in findings[0].message and ">=0.1.3,<0.2" in findings[0].message


@pytest.mark.parametrize("core", [">=0.1.*", "==0.1rc1.*", "~=1", ">=1!0.1"])
def test_core_outside_pep440_is_config_invalid(core: str) -> None:
    cfg, findings = parse_project(_replace(EXAMPLE, 'core: ">=0.1.3,<0.2"', f'core: "{core}"'))
    assert cfg is None
    assert ("E_CONFIG_INVALID", "/core") in _codes(findings) or ("E_VALIDATION_SCHEMA", "/core") in _codes(findings)


def test_core_with_triple_equal_is_a_schema_error() -> None:
    cfg, findings = parse_project(_replace(EXAMPLE, 'core: ">=0.1.3,<0.2"', 'core: "===0.1.3"'))
    assert cfg is None and _codes(findings) == [("E_VALIDATION_SCHEMA", "/core")]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('core_pin: "0.1.3"\n', "0.1.3"),
        ('core_pin: "10.20.30"  # fijado por init\n', "10.20.30"),
        ('﻿name: x\r\ncore_pin: "0.1.3"\r\n', "0.1.3"),
        ('# core_pin: "9.9.9"\ncore_pin: "0.1.3"\n', "0.1.3"),
        ('build:\n  core_pin: "9.9.9"\ncore_pin: "0.1.3"\n', "0.1.3"),
        ("core_pin: '0.1.3'\n", None),
        ('core_pin: "0.1"\n', None),
        ('core_pin: "0.1.3"\ncore_pin: "0.1.3"\n', None),
        ('"core_pin": "0.1.3"\n', None),
        ("name: x\n", None),
        ('core_pin: "0.1.3" trailing\n', None),
        ('core_pin: "0.1.0rc1"\n', "0.1.0rc1"),  # 00-marco D-66
        ('core_pin: "1.2.3b4"  # beta\n', "1.2.3b4"),
        ('core_pin: "2.0.0a10"\n', "2.0.0a10"),
        ('core_pin: "0.1.0-rc1"\n', None),
        ('core_pin: "0.1.0.dev1"\n', None),
        ('core_pin: "0.1.0rc"\n', None),
    ],
)
def test_core_pin_line(text: str, expected: str | None) -> None:
    assert core_pin_line(text) == expected


# -- REQ-SCHEMA-84 y rangos -----------------------------------------------------------


@pytest.mark.parametrize(
    ("limits", "ok"),
    [
        ("  claim_expiry_days: 14\n  expiry_warning_days: 13\n", True),
        ("  claim_expiry_days: 14\n  expiry_warning_days: 14\n", False),
        ("  claim_expiry_days: 2\n", False),  # contra el expiry_warning_days por defecto (3)
        ("  expiry_warning_days: 20\n", False),  # contra el claim_expiry_days por defecto (14)
    ],
)
def test_expiry_warning_must_be_less_than_expiry(limits: str, ok: bool) -> None:
    cfg, findings = parse_project(MINIMAL + "limits:\n" + limits)
    assert (cfg is not None) is ok
    if not ok:
        assert _codes(findings) == [("E_CONFIG_INVALID", "/limits/expiry_warning_days")]


@pytest.mark.parametrize(("minutes", "ok"), [(0, False), (1, True), (10, True), (12, True), (13, False), (60, False)])
def test_timeout_minutes_is_1_to_12(minutes: int, ok: bool) -> None:
    cfg, findings = parse_project(MINIMAL + f"build:\n  timeout_minutes: {minutes}\n")
    assert (cfg is not None) is ok
    if not ok:
        assert _codes(findings) == [("E_VALIDATION_SCHEMA", "/build/timeout_minutes")]
    else:
        assert cfg is not None and cfg.build.timeout_minutes == minutes


def test_all_schema_errors_are_reported_and_rules_wait_for_the_schema() -> None:
    text = (FIXTURES / "config" / "project" / "many-errors.yaml").read_text(encoding="utf-8")
    cfg, findings = parse_project(text)
    assert cfg is None and len(findings) == 8
    assert all(f.code == "E_VALIDATION_SCHEMA" for f in findings)


def test_non_mapping_and_empty_documents_are_invalid() -> None:
    for text in ("- a\n- b\n", "", "# nada\n"):
        cfg, findings = parse_project(text)
        assert cfg is None and findings and findings[0].pointer == ""


def test_findings_carry_file_and_check() -> None:
    cfg, findings = parse_project("convention: 0.1\n", file="pr/project.yaml", check="ci-policy")
    assert cfg is None and all(f.path == "pr/project.yaml" and f.check == "ci-policy" for f in findings)


def test_bytes_with_bom_and_crlf_are_accepted() -> None:
    raw = b"\xef\xbb\xbf" + MINIMAL.replace("\n", "\r\n").encode("utf-8")
    cfg, findings = parse_project(raw)
    assert findings == [] and cfg is not None


def test_messages_follow_the_translator(t_es: Translator, t_en: Translator) -> None:
    text = _replace(MINIMAL, "language: es", "language: fr")
    _c, es = parse_project(text, t=t_es)
    _c, en = parse_project(text, t=t_en)
    _c, default = parse_project(text)
    assert es[0].message != en[0].message and default[0].message == en[0].message


# -- compatibilidad (REQ-SCHEMA-17, 01 §6.3) -----------------------------------------


def test_compat_of_a_config() -> None:
    cfg, _ = parse_project(EXAMPLE)
    assert cfg is not None
    assert project_mod.compat(cfg, "0.1.3").level == "ok"
    assert project_mod.compat(cfg, "0.1.4").level == "warning"
    assert project_mod.compat(cfg, "0.2.0").code == "E_CONFIG_CORE_MISMATCH"
    cfg_min, _ = parse_project(MINIMAL)
    assert cfg_min is not None and project_mod.compat(cfg_min, cfg_min.core_pin).level == "ok"
    assert project_mod.compat(cfg_min) == project_mod.compat(cfg_min, _meta.__version__)  # por defecto, el núcleo instalado


# -- la plantilla ---------------------------------------------------------------------


def test_template_project_yaml_is_valid_once_rendered() -> None:
    variables = tomllib.loads((REPO_ROOT / "tools" / "template_vars.toml").read_text(encoding="utf-8"))
    text = (REPO_ROOT / "template" / "base" / "project.yaml").read_text(encoding="utf-8")
    for key, value in variables.items():
        text = text.replace("{{var:" + key + "}}", str(value))
    assert "{{var:" not in text
    cfg, findings = parse_project(text)
    assert findings == [] and cfg is not None
    assert core_pin_line(text) == cfg.core_pin == variables["core_pin"]
    # D-48: la plantilla no escribe require_commercial, así que su valor efectivo es true.
    assert cfg.raw["disclosure"] == cfg.disclosure == "milestones"
    assert "require_commercial" not in cfg.raw["data_policy"]  # type: ignore[operator]
    assert cfg.data_policy["require_commercial"] is True
