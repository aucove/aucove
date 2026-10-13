# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Registro local de eventos (01 §11; REQ-ARCH-90..95)."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import pytest

from aucove import _meta
from aucove.obs import events as events_mod
from aucove.obs.events import FIELDS, LOG_MAX_BYTES, emit, event_line, log_dir

LOG_ENV = _meta.ENV_PREFIX + "LOG"
TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")


@pytest.fixture(autouse=True)
def _default_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(LOG_ENV, raising=False)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    return root


def _lines(project: Path, name: str = "events.jsonl") -> list[dict[str, object]]:
    path = project / _meta.CACHE_DIR / "log" / name
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_bytes().decode("utf-8").splitlines()]


def test_emit_writes_one_json_line(project: Path) -> None:
    emit(
        project / _meta.CACHE_DIR,
        kind="hook",
        name="pre-tool-use",
        result="deny",
        code="E_PERM_SECTION_NOT_OWNED",
        tool="Edit",
        path="sections/methods/main.tex",
        path_class="section",
        task=12,
        session="9f86d081884c",
        ms=41,
    )
    raw = (project / _meta.CACHE_DIR / "log" / "events.jsonl").read_bytes()
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    (event,) = _lines(project)
    assert list(event) == [
        "v", "ts", "core", "kind", "name", "result", "code", "tool", "path", "path_class", "task", "session", "ms",
    ]
    assert event["v"] == 1 and event["core"] == _meta.__version__
    assert isinstance(event["ts"], str) and TS.fullmatch(event["ts"])
    assert event["path"] == "sections/methods/main.tex" and event["task"] == 12
    assert (project / _meta.CACHE_DIR / ".gitignore").read_bytes() == b"*\n"


def test_emit_accepts_project_root(project: Path) -> None:
    emit(project, kind="op", name="validate", result="ok")
    emit(project, kind="op", name="doctor", result="error", code="E_CONFIG_NOT_A_PROJECT")
    assert [e["name"] for e in _lines(project)] == ["validate", "doctor"]
    assert log_dir(project) == project / _meta.CACHE_DIR / "log"
    assert log_dir(project / _meta.CACHE_DIR) == project / _meta.CACHE_DIR / "log"


def test_no_event_has_keys_outside_the_closed_list(project: Path) -> None:
    """REQ-ARCH-95: ninguna clave fuera de la tabla de 01 §11 llega al registro."""
    junk = {
        "token": "ghp_secret",
        "message": "Traceback: https://x-access-token:abc@github.com",
        "login": "ana",
        "email": "ana@example.invalid",
        "env": {"GH_TOKEN": "x"},
        "args": ["--force"],
        "title": "Issue",
        "body": "texto",
        "url": "https://api.github.com/repos/o/r?token=x",
        "transcript_path": "/home/ana/.claude/x.jsonl",
        "session_id": "0b8d0f8c-2f46-4ab0-9d0b-2a1f6d7c1f00",
        "V": 2,
        "Kind": "op",
    }
    combos: list[dict[str, object]] = [
        {"kind": "op", "name": "claim", "result": "ok", **junk},
        {"kind": "net", "net": {"host": "api.github.com", "status": 502}, "ms": 12.6, **junk},
        {"kind": "cache", "name": "policy.json", "result": "skip", "extra": 1},
        {"kind": "hook", "tool": "Bash", "path": "a.tex", "session": "raw-session-id", "detail": "rm -rf /"},
        {"kind": "audit", "v": 9, "ts": "x", "core": "9.9.9"},
    ]
    for fields in combos:
        emit(project, **fields)
    events = _lines(project)
    assert len(events) == len(combos)
    for event in events:
        assert set(event) <= FIELDS, set(event) - FIELDS
        assert {"v", "ts", "core", "kind"} <= set(event)
        assert event["v"] == 1 and event["core"] == _meta.__version__
    text = (project / _meta.CACHE_DIR / "log" / "events.jsonl").read_text(encoding="utf-8")
    for secret in ("ghp_secret", "abc@", "ana@example", "GH_TOKEN", "token=x", "rm -rf", "0b8d0f8c"):
        assert secret not in text


def test_closed_list_matches_the_spec_table() -> None:
    assert FIELDS == frozenset(
        {"v", "ts", "core", "kind", "name", "result", "code", "tool", "path", "path_class", "task", "session", "net", "ms"}
    )


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("path", "tex/main.tex", "tex/main.tex"),
        ("path", "sections/métodos/a.tex", "sections/métodos/a.tex"),
        ("path", "/home/ana/p/tex/main.tex", None),
        ("path", "C:/Users/ana/p/main.tex", None),
        ("path", "C:\\Users\\ana\\main.tex", None),
        ("path", "\\\\srv\\share\\x", None),
        ("path", "~/p/main.tex", None),
        ("path", "../outside.tex", None),
        ("path", "a//b", None),
        ("path", "a\nb", None),
        ("path", "a\udcffb", None),
        ("path", 7, None),
        ("code", "E_PERM_SYMLINK", "E_PERM_SYMLINK"),
        ("code", "W_NET_OFFLINE", "W_NET_OFFLINE"),
        ("code", "KeyError: 'x'", None),
        ("code", "e_perm", None),
        ("tool", "Edit", "Edit"),
        ("tool", "mcp__x__claim", "mcp__x__claim"),
        ("tool", "Bash(rm -rf /)", None),
        ("name", "pre-tool-use", "pre-tool-use"),
        ("name", "tasks/12.json", "tasks/12.json"),
        ("name", "claim --force now", None),
        ("result", "deny", "deny"),
        ("result", "maybe", None),
        ("path_class", "outside", "outside"),
        ("path_class", "R7", None),
        ("task", 12, 12),
        ("task", 0, None),
        ("task", True, None),
        ("task", "12", None),
        ("session", "9f86d081884c", "9f86d081884c"),
        ("session", "9F86D081884C", hashlib.sha256(b"9F86D081884C").hexdigest()[:12]),
        ("session", "abc-123", hashlib.sha256(b"abc-123").hexdigest()[:12]),
        ("net", {"host": "API.GitHub.com", "status": 200}, {"host": "api.github.com", "status": 200}),
        ("net", "api.github.com", {"host": "api.github.com", "status": None}),
        ("net", ("api.github.com", 404), {"host": "api.github.com", "status": 404}),
        ("net", {"host": "https://api.github.com/x?y=1", "status": 200}, None),
        ("net", {"host": "api.github.com", "status": 999}, None),
        ("net", {"host": "api.github.com", "headers": {"a": "b"}}, None),
        ("net", {"host": "user:pw@host", "status": 200}, None),
        ("ms", 41, 41),
        ("ms", 12.6, 13),
        ("ms", -1, None),
        ("ms", float("nan"), None),
        ("ms", True, None),
    ],
)
def test_field_validation(field: str, value: object, expected: object) -> None:
    line = event_line(kind="op", **{field: value})
    assert line is not None
    event = json.loads(line)
    assert event.get(field) == expected


@pytest.mark.parametrize("kind", [None, "", "operation", 1])
def test_invalid_kind_writes_nothing(project: Path, kind: object) -> None:
    fields = {} if kind is None else {"kind": kind}
    assert event_line(name="x", **fields) is None
    emit(project, name="x", **fields)
    assert _lines(project) == []


def test_each_event_is_a_single_write(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    emit(project, kind="op", name="warmup")
    writes: list[bytes] = []
    real_write = os.write

    def spy(fd: int, data: bytes) -> int:
        writes.append(bytes(data))
        return real_write(fd, data)

    monkeypatch.setattr(events_mod.os, "write", spy)
    emit(project, kind="op", name="validate", result="ok", ms=3)
    assert len(writes) == 1 and writes[0].endswith(b"\n") and writes[0].count(b"\n") == 1


def test_rotation_keeps_three_files(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert LOG_MAX_BYTES == 2**20
    directory = project / _meta.CACHE_DIR / "log"
    directory.mkdir(parents=True)
    for name, marker in [("events.jsonl", "cur"), ("events.1.jsonl", "one"), ("events.2.jsonl", "two"), ("events.3.jsonl", "three")]:
        (directory / name).write_bytes(json.dumps({"marker": marker}).encode() + b"\n")
    base = directory / "events.jsonl"
    base.write_bytes(base.read_bytes() + b"x" * (LOG_MAX_BYTES - base.stat().st_size - 5))
    emit(project, kind="op", name="after")
    assert [e["name"] for e in _lines(project)] == ["after"]
    assert (directory / "events.1.jsonl").read_bytes().startswith(b'{"marker": "cur"}')
    assert _lines(project, "events.2.jsonl") == [{"marker": "one"}]
    assert _lines(project, "events.3.jsonl") == [{"marker": "two"}]  # el más antiguo se borró
    assert not (directory / "events.4.jsonl").exists()


def test_files_never_exceed_the_limit(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(events_mod, "LOG_MAX_BYTES", 400)
    for i in range(60):
        emit(project, kind="op", name=f"op-{i}", result="ok", ms=i)
    directory = project / _meta.CACHE_DIR / "log"
    names = sorted(p.name for p in directory.iterdir())
    assert names == ["events.1.jsonl", "events.2.jsonl", "events.3.jsonl", "events.jsonl"]
    for name in names:
        assert (directory / name).stat().st_size <= 400
    assert _lines(project)[-1]["name"] == "op-59"


def test_log_off_writes_nothing(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LOG_ENV, "off")
    emit(project, kind="op", name="validate")
    assert not (project / _meta.CACHE_DIR).exists()
    for level in ("info", "debug", "loud"):
        monkeypatch.setenv(LOG_ENV, level)
        emit(project, kind="op", name=level)
    assert [e["name"] for e in _lines(project)] == ["info", "debug", "loud"]


def test_emit_never_raises(tmp_path: Path, project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    emit(None, kind="op", name="x")
    afile = tmp_path / "file.txt"
    afile.write_bytes(b"x")
    emit(afile, kind="op", name="x")  # no es una carpeta
    emit(tmp_path / "missing" / _meta.CACHE_DIR, kind="op", name="x")  # sin checkout
    assert not (tmp_path / "missing").exists()
    emit(project, kind="op", name="x", path=object(), net=object(), ms=object())

    def boom(*args: object, **kwargs: object) -> None:
        raise PermissionError("bloqueado")

    monkeypatch.setattr(events_mod, "_append", boom)
    emit(project, kind="op", name="y")
    monkeypatch.setattr(events_mod, "event_line", lambda **kw: (_ for _ in ()).throw(RuntimeError("x")))
    emit(project, kind="op", name="z")
    monkeypatch.undo()
    monkeypatch.setattr(events_mod.os, "open", boom)
    emit(project, kind="op", name="w")
    monkeypatch.undo()
    assert [e["name"] for e in _lines(project)] == ["x"]


def test_rotation_failure_keeps_logging(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(events_mod, "LOG_MAX_BYTES", 150)
    emit(project, kind="op", name="a", result="ok")
    emit(project, kind="op", name="b", result="ok")

    def locked(src: object, dst: object) -> None:
        raise PermissionError("en uso por otro proceso")

    monkeypatch.setattr(events_mod.os, "replace", locked)
    emit(project, kind="op", name="c", result="ok")
    monkeypatch.undo()
    assert [e["name"] for e in _lines(project)][-1] == "c"
