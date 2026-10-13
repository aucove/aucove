# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""schemas/ es la copia verificada de spec/schemas (SHA256SUMS) y los fixtures de roles la cumplen."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import jsonschema
import pytest
import yaml

from aucove import _meta
from aucove.config.datafiles import data_path
from aucove.config.schemas import SCHEMA_KINDS
from tests.conftest import FIXTURES, REPO_ROOT, load_tool, roles_fixture

SCHEMAS = REPO_ROOT / "schemas"
SPEC_ENV = _meta.ENV_PREFIX + "SPEC_DIR"
EXPECTED = {
    "project.schema.json", "roles.schema.json", "result.schema.json",
    "decision.schema.json", "pr-meta.schema.json", "session-note.schema.json",
    # Evidencia de autoría (03-esquemas REQ-SCHEMA-93; 11-trazabilidad §8).
    "evidence-manifest.schema.json", "evidence-openings.schema.json", "evidence-record.schema.json",
}


def _sums() -> dict[str, str]:
    tool = load_tool("sync_schemas")
    return tool.parse_sums((SCHEMAS / "SHA256SUMS").read_text(encoding="ascii"))


def test_sha256sums_matches_copy() -> None:
    raw = (SCHEMAS / "SHA256SUMS").read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")
    sums = _sums()
    assert set(sums) == {p.name for p in SCHEMAS.glob("*.json")} == EXPECTED
    for name, digest in sums.items():
        assert hashlib.sha256((SCHEMAS / name).read_bytes()).hexdigest() == digest, name
    lines = raw.decode("ascii").splitlines()
    assert lines == sorted(lines, key=lambda line: line.split("  ", 1)[1])
    assert all(len(line.split("  ", 1)[0]) == 64 for line in lines)


def test_copy_follows_file_conventions() -> None:
    for path in SCHEMAS.glob("*.json"):
        data = path.read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf") and b"\r" not in data, path.name
        assert data.endswith(b"\n") and not data.endswith(b"\n\n"), path.name
        schema = json.loads(data.decode("utf-8"))
        jsonschema.Draft202012Validator.check_schema(schema)
        assert schema["$id"].startswith(_meta.SCHEMA_BASE + "/")


def test_copy_matches_spec_when_available() -> None:
    spec_dir = os.environ.get(SPEC_ENV)
    if not spec_dir:
        pytest.skip(f"sin {SPEC_ENV}: rige la copia versionada")
    tool = load_tool("sync_schemas")
    expected = tool.expected_files(Path(spec_dir))
    sums = _sums()
    assert set(expected) == set(sums)
    for name, content in expected.items():
        assert hashlib.sha256(content).hexdigest() == sums[name], name
    assert tool.diff(Path(spec_dir), SCHEMAS) == []


def test_data_path_finds_schemas() -> None:
    assert (data_path("schemas") / "result.schema.json").is_file()


def test_the_core_loads_every_copied_schema_and_sync_protects_them() -> None:
    """Cada esquema de la copia es un ``SchemaKind`` del registro local y ``sync`` no lo borra."""
    assert {f"{kind}.schema.json" for kind in SCHEMA_KINDS} == EXPECTED
    assert set(load_tool("sync_schemas").KNOWN_SCHEMAS) == EXPECTED


# -- tools/sync_schemas.py ------------------------------------------------------------


def _fake_spec(root: Path) -> Path:
    src = root / "spec" / "schemas"
    src.mkdir(parents=True)
    (src / "a.schema.json").write_bytes(b'\xef\xbb\xbf{\r\n  "a": 1\r\n}')  # BOM, CRLF y sin salto final
    (src / "b.schema.json").write_bytes(b'{"b": "\xc3\xb1"}\n\n\n')
    return root / "spec"


def test_sync_tool_normalizes_and_writes_sums(tmp_path: Path) -> None:
    tool = load_tool("sync_schemas")
    spec = _fake_spec(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "stale.schema.json").write_text("{}\n", encoding="utf-8")
    assert tool.main(["--spec-dir", str(spec), "--dest", str(dest)]) == 0
    a_bytes = b'{\n  "a": 1\n}\n'
    b_bytes = b'{"b": "\xc3\xb1"}\n'
    assert (dest / "a.schema.json").read_bytes() == a_bytes
    assert (dest / "b.schema.json").read_bytes() == b_bytes
    assert not (dest / "stale.schema.json").exists()
    sums = (dest / "SHA256SUMS").read_text(encoding="ascii")
    a_sum = hashlib.sha256(a_bytes).hexdigest()
    b_sum = hashlib.sha256(b_bytes).hexdigest()
    assert sums == f"{a_sum}  a.schema.json\n{b_sum}  b.schema.json\n"
    assert tool.main(["--spec-dir", str(spec), "--dest", str(dest), "--check"]) == 0
    (dest / "a.schema.json").write_bytes(b'{"a": 2}\n')
    assert tool.main(["--spec-dir", str(spec), "--dest", str(dest), "--check"]) == 1


def test_sync_tool_rejects_invalid_json(tmp_path: Path) -> None:
    tool = load_tool("sync_schemas")
    src = tmp_path / "schemas"
    src.mkdir()
    (src / "x.schema.json").write_text("{nope", encoding="utf-8")
    assert tool.main(["--spec-dir", str(src), "--dest", str(tmp_path / "out")]) == 1
    assert not (tmp_path / "out").exists()


# -- Fixtures de roles ------------------------------------------------------------


@pytest.mark.parametrize("name", ["basic", "no-deputy", "mixed-case"])
def test_roles_fixtures_validate(name: str) -> None:
    schema = json.loads((SCHEMAS / "roles.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER)
    data = yaml.safe_load(roles_fixture(name))
    errors = [e.message for e in validator.iter_errors(data)]
    assert errors == []
    raw = (FIXTURES / "roles" / f"{name}.yaml").read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n") and b"SPDX" + b"-" not in raw


def test_roles_fixtures_content() -> None:
    basic = yaml.safe_load(roles_fixture("basic"))
    assert (basic["lead"], basic["deputy"]) == ("ana", "bruno")
    assert [p["login"] for p in basic["people"]] == ["ana", "bruno", "carla", "dario"]
    assert {s["id"]: s["owner"] for s in basic["sections"]} == {"introduction": "ana", "methods": "carla", "results": "bruno"}
    no_deputy = yaml.safe_load(roles_fixture("no-deputy"))
    assert "deputy" not in no_deputy
    assert {k: v for k, v in basic.items() if k != "deputy"} == no_deputy


def test_mixed_case_gives_exactly_one_case_warning() -> None:
    """REQ-SCHEMA-30: una referencia que difiere de people[].login solo en mayúsculas."""
    data = yaml.safe_load(roles_fixture("mixed-case"))
    logins = {p["login"].lower(): p["login"] for p in data["people"]}
    refs = [("/lead", data["lead"])]
    if data.get("deputy"):
        refs.append(("/deputy", data["deputy"]))
    refs += [(f"/sections/{i}/owner", s["owner"]) for i, s in enumerate(data["sections"])]
    case_only = [ptr for ptr, ref in refs if ref.lower() in logins and logins[ref.lower()] != ref]
    assert case_only == ["/sections/1/owner"]
    assert {"Ana", "Carla"} <= {p["login"] for p in data["people"]}
