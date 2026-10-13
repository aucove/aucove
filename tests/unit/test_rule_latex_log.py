# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Regla ``latex-log`` y ``diagnostics.json`` (09-ci REQ-CI-18, REQ-CI-21).

El log de latexmk se lee como datos: nada se ejecuta. Los logs de ejemplo de
``tests/fixtures/build-logs`` cubren un error fatal, una cita indefinida, una
etiqueta repetida y una compilación limpia.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aucove import _meta
from aucove.config.schemas import validate_result
from aucove.domain.findings import Finding
from aucove.i18n import Translator, translator
from aucove.ops.diagnostics import MAX_DIAGNOSTICS, MAX_MESSAGE, Diagnostic, load_diagnostics
from aucove.ops.result import read_envelope
from aucove.ops.rules import latex_log
from aucove.ops.rules.base import UNAVAILABLE_CODE, RuleInput, status_of
from aucove.ops.rules.latex_log import (
    LATEX_LOG,
    diagnostics_result,
    logical_lines,
    parse_log,
    read_log,
    repo_path,
    write_diagnostics,
)
from tests.conftest import FIXTURES

LOGS = FIXTURES / "build-logs"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
INTRO = "sections/introduction/introduction.tex"
METHODS = "sections/methods/methods.tex"


def place_log(tmp_path: Path, content: bytes | str) -> Path:
    """Escribe el log donde lo deja latexmk: ``<repo>/tex/build/main.log``."""
    log = tmp_path / "repo" / "tex" / "build" / "main.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    return log


def run_log(log: Path | None, t: Translator | None = None) -> list[Finding]:
    inp = RuleInput(mode="build-log", t=t or translator("en"), now=NOW, log_path=log)
    return list(LATEX_LOG.run(inp))


def run_fixture(tmp_path: Path, name: str, t: Translator | None = None) -> list[Finding]:
    return run_log(place_log(tmp_path, (LOGS / name).read_bytes()), t)


def summary(findings: Sequence[Finding]) -> list[tuple[str, str, str | None, int | None]]:
    return [(f.code, f.severity, f.path, f.line) for f in findings]


def envelope_of(findings: Sequence[Finding], tmp_path: Path, t: Translator | None = None) -> dict:  # type: ignore[type-arg]
    result = diagnostics_result(findings, t or translator("en"), head={"ref": "task/17", "sha": "a" * 40}, pr=42)
    path = tmp_path / "out" / "diagnostics.json"
    write_diagnostics(path, result)
    env = read_envelope(path)
    assert env is not None
    assert validate_result(env) == []
    return env


# -- registro ------------------------------------------------------------------------------


def test_rule_registry() -> None:
    assert latex_log.RULES == (LATEX_LOG,)
    assert LATEX_LOG.id == "latex-log"
    assert LATEX_LOG.modes == frozenset({"build-log"})
    assert dict(LATEX_LOG.codes) == {"E_VALIDATION_LATEX": frozenset({"detail"})}


# -- clasificación de los logs de ejemplo ---------------------------------------------------


def test_fatal_error_log(tmp_path: Path) -> None:
    findings = run_fixture(tmp_path, "fatal-error.log")
    assert summary(findings) == [("E_VALIDATION_LATEX", "error", INTRO, 7)]
    # El «==> Fatal error occurred» (cortado a 79 columnas) no se repite como otro error.
    assert "Undefined control sequence." in findings[0].message
    assert "\\foo" in findings[0].message
    assert status_of(findings) == "fail"


def test_undefined_citation_log(tmp_path: Path) -> None:
    findings = run_fixture(tmp_path, "undefined-citation.log")
    assert summary(findings) == [
        ("E_VALIDATION_LATEX", "error", INTRO, 11),
        ("E_VALIDATION_LATEX", "warning", METHODS, 3),
        ("E_VALIDATION_LATEX", "warning", "tex/main.tex", None),
    ]
    assert "undefined citation 'missingKey2020'" in findings[0].message
    assert "Token not allowed in a PDF string (Unicode): removing" in findings[1].message
    assert "rerun LaTeX afterwards" in findings[2].message
    # El resumen «There were undefined references» no se duplica.
    assert not any("There were" in f.message for f in findings)


def test_duplicate_label_log(tmp_path: Path) -> None:
    findings = run_fixture(tmp_path, "duplicate-label.log")
    assert summary(findings) == [
        ("E_VALIDATION_LATEX", "error", METHODS, 14),
        ("E_VALIDATION_LATEX", "error", None, None),
    ]
    assert "fig:methods:missing-figure-with-a-rather-long-name" in findings[0].message  # línea cortada y unida
    assert "label 'sec:introduction' defined more than once" in findings[1].message


def test_clean_log(tmp_path: Path) -> None:
    findings = run_fixture(tmp_path, "clean.log")
    assert findings == []
    assert status_of(findings) == "pass"


@pytest.mark.parametrize(
    ("name", "status", "count"),
    [("fatal-error.log", "fail", 1), ("undefined-citation.log", "fail", 3), ("duplicate-label.log", "fail", 2), ("clean.log", "pass", 0)],
)
def test_envelope_is_read_by_load_diagnostics(tmp_path: Path, name: str, status: str, count: int) -> None:
    findings = run_fixture(tmp_path, name)
    env = envelope_of(findings, tmp_path)
    assert env["schema"] == _meta.RESULT_SCHEMA and env["op"] == "validate"
    assert env["ok"] is (status == "pass")
    data = env["data"]
    assert data["mode"] == "build-log" and data["valid"] is (status == "pass")
    assert data["pr"] == 42 and data["head"] == {"ref": "task/17", "sha": "a" * 40}
    assert [r["check"] for r in data["results"]] == ["latex-log"]
    assert data["results"][0]["status"] == status
    if status == "fail":
        assert env["error"]["code"] == "E_VALIDATION_FAILED"
    else:
        assert env["error"] is None
    for item in data["results"][0]["findings"]:
        assert item["code"] == "E_VALIDATION_LATEX" and item["pointer"] is None
        assert "\n" not in item["message"] and len(item["message"]) <= MAX_MESSAGE
    diags = load_diagnostics(tmp_path / "out" / "diagnostics.json")
    assert diags is not None and len(diags) == count
    # Los cinco campos de REQ-CI-21: archivo, línea, severidad, mensaje y regla.
    for diag, finding in zip(diags, findings):
        assert diag == Diagnostic(
            file=finding.path, line=finding.line, severity=finding.severity, message=finding.message, rule="latex-log"
        )


def test_envelope_bytes_are_compact_utf8(tmp_path: Path) -> None:
    findings = run_fixture(tmp_path, "undefined-citation.log", translator("es"))
    path = tmp_path / "diagnostics.json"
    write_diagnostics(path, diagnostics_result(findings, translator("es")))
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and raw.endswith(b"\n") and raw.count(b"\n") == 1
    assert "cita indefinida 'missingKey2020'".encode() in raw
    assert list(tmp_path.glob(".diagnostics-*")) == []


def test_more_than_200_findings_are_truncated(tmp_path: Path) -> None:
    lines = [f"../sections/methods/methods.tex:{i}: Undefined control sequence." for i in range(1, 181)]
    lines += [f"LaTeX Warning: Reference `r{i}' on page 1 undefined on input line {i}." for i in range(1, 61)]
    lines += [f"Package foo Warning: note number {i} on input line {i}." for i in range(1, 21)]
    findings = run_log(place_log(tmp_path, "(./main.tex\n" + "\n".join(lines) + "\n)\n"))
    assert len(findings) == 260
    env = envelope_of(findings, tmp_path)
    items = env["data"]["results"][0]["findings"]
    assert len(items) == MAX_DIAGNOSTICS
    assert {i["severity"] for i in items} == {"error"}  # primero los errores (240 de 260)
    assert env["data"]["page"] == {"next_cursor": None, "returned": 200, "total": 260}
    truncated = [w for w in env["warnings"] if w["code"] == "W_OUTPUT_TRUNCATED"]
    assert len(truncated) == 1 and truncated[0]["details"] == {"omitted": 60}
    diags = load_diagnostics(tmp_path / "out" / "diagnostics.json")
    assert diags is not None and len(diags) == 200


def test_exactly_200_findings_are_not_truncated(tmp_path: Path) -> None:
    lines = [f"Package foo Warning: note {i} on input line {i}." for i in range(1, 201)]
    findings = run_log(place_log(tmp_path, "\n".join(lines) + "\n"))
    env = envelope_of(findings, tmp_path)
    assert env["ok"] is True and env["warnings"] == []
    assert len(env["data"]["results"][0]["findings"]) == 200


def test_messages_are_one_line_and_clipped(tmp_path: Path) -> None:
    long = "../sections/methods/methods.tex:3: " + "Very long error text " * 80
    findings = run_log(place_log(tmp_path, long + "\n"))
    assert len(findings) == 1
    assert len(findings[0].message) <= MAX_MESSAGE and "\n" not in findings[0].message


# -- lectura del log como datos -------------------------------------------------------------


def test_missing_log_path_or_file_is_unavailable(tmp_path: Path) -> None:
    assert summary(run_log(None)) == [(UNAVAILABLE_CODE, "error", None, None)]
    assert summary(run_log(tmp_path / "nope.log")) == [(UNAVAILABLE_CODE, "error", None, None)]
    assert read_log(tmp_path / "nope.log") == (None, False, "no_log")


def test_directory_or_symlink_is_not_read(tmp_path: Path) -> None:
    (tmp_path / "dir.log").mkdir()
    assert read_log(tmp_path / "dir.log") == (None, False, "log_unreadable")
    assert summary(run_log(tmp_path / "dir.log")) == [(UNAVAILABLE_CODE, "error", None, None)]
    target = place_log(tmp_path, "! Undefined control sequence.\n")
    link = tmp_path / "link.log"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("no se pueden crear enlaces simbólicos aquí")
    assert read_log(link) == (None, False, "log_unreadable")


def test_os_errors_while_reading_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    log = place_log(tmp_path, "! Undefined control sequence.\n")

    def denied(*_a: object, **_k: object) -> object:
        raise PermissionError("denied")

    with monkeypatch.context() as m:
        m.setattr(os, "lstat", denied)
        assert read_log(log) == (None, False, "log_unreadable")
    with monkeypatch.context() as m:
        m.setattr(os, "open", denied)
        assert read_log(log) == (None, False, "log_unreadable")
    with monkeypatch.context() as m:
        m.setattr(os, "read", denied)
        assert read_log(log) == (None, False, "log_unreadable")
    real_lstat = os.lstat
    with monkeypatch.context() as m:
        # El archivo cambió entre lstat y open: no se lee.
        other = place_log(tmp_path / "other", "x\n")
        m.setattr(os, "lstat", lambda p, *a, **k: real_lstat(other))
        assert read_log(log) == (None, False, "log_unreadable")


def test_long_context_is_shortened() -> None:
    log = b"./a.tex:3: Undefined control sequence.\nl.3 " + b"word " * 40 + b"\\oops\n"
    (entry,) = parse_log(log)
    assert entry.text.startswith("Undefined control sequence. \u2026") and entry.text.endswith("\\oops")
    assert len(entry.text) <= len("Undefined control sequence. ") + latex_log.CONTEXT_TAIL
    # Una última línea de 79 columnas no se pierde.
    assert logical_lines(b"y" * 79) == ["y" * 79]


def test_log_over_5_mb_keeps_the_tail(tmp_path: Path) -> None:
    filler = ("(./x.tex) " * 7 + "\n").encode() * 80_000  # ~6 MB
    tail = b"../sections/methods/methods.tex:9: LaTeX Error: Something broke.\n"
    log = place_log(tmp_path, filler + tail)
    assert log.stat().st_size > 5 * 2**20
    data, truncated, problem = read_log(log)
    assert truncated and problem is None and data is not None and len(data) <= 5 * 2**20
    findings = run_log(log)
    assert summary(findings) == [
        ("E_VALIDATION_LATEX", "error", METHODS, 9),
        ("E_VALIDATION_LATEX", "warning", None, None),
    ]
    assert "5 MB" in findings[1].message
    data, truncated, _ = read_log(log, max_bytes=10)
    assert truncated and data == b""


def test_rule_executes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("latex-log intentó ejecutar algo")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr("os.system", boom)
    for name in ("fatal-error.log", "undefined-citation.log", "duplicate-label.log", "clean.log"):
        run_fixture(tmp_path, name)


def test_deadline_gives_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(latex_log, "RULE_TIME_BUDGET_S", -1.0)
    log = place_log(tmp_path, "x\n" * 5000)
    assert summary(run_log(log)) == [(UNAVAILABLE_CODE, "error", None, None)]


def test_logical_lines_join_lines_cut_at_79() -> None:
    first = "LaTeX Warning: Citation `averyveryverylongkeyname2020' on page 12 undefined on"
    first = first + " " * (79 - len(first))
    assert len(first) == 79
    data = (first + "\ninput line 4.\n" + "x" * 79 + "\n! Next message\nend").encode()
    lines = logical_lines(data)
    assert lines[0] == first + "input line 4."
    assert lines[1:] == ["x" * 79, "! Next message", "end"]
    assert logical_lines(b"a\r\nb\r\n") == ["a", "b", ""]
    assert logical_lines("\xe9t\xe9".encode("latin-1")) == ["\ufffdt\ufffd"]


def test_bang_errors_take_file_from_the_stack_and_line_from_context() -> None:
    log = (
        b"(./main.tex (../sections/results/results.tex\n"
        b"! LaTeX Error: Environment foo undefined.\n\n"
        b"See the LaTeX manual or LaTeX Companion for explanation.\n"
        b"Type  H <return>  for immediate help.\n"
        b" ...                                              \n"
        b"l.12 \\begin{foo}\n"
        b"                 (\n"
        b")\n"
        b"! Emergency stop.\n"
        b"<*> main.tex\n"
    )
    entries = parse_log(log)
    assert [(e.kind, e.file, e.line) for e in entries] == [("error", "../sections/results/results.tex", 12)]
    assert entries[0].text == "LaTeX Error: Environment foo undefined. \\begin{foo}"
    # Solo un error fatal: se informa como tal.
    only = parse_log(b"(./main.tex\n! Emergency stop.\n<*> main.tex\n*** (job aborted, no legal \\end found)\n")
    assert [(e.kind, e.file, e.line) for e in only] == [("fatal", "./main.tex", None)]


def test_fatal_alone_is_an_error_finding(tmp_path: Path, t_es: Translator) -> None:
    findings = run_log(place_log(tmp_path, "(./main.tex\n! Emergency stop.\n<*> main.tex\n"), t_es)
    assert summary(findings) == [("E_VALIDATION_LATEX", "error", "tex/main.tex", None)]
    assert "error fatal" in findings[0].message


def test_warning_kinds_and_dedupe() -> None:
    log = (
        "(./main.tex\n"
        "Package natbib Warning: Citation `nat1' on page 1 undefined on input line 3.\n"
        "Package natbib Warning: Citation `nat1' on page 1 undefined on input line 3.\n"
        "Class article Warning: Unused global option(s):\n"
        "(article)              [foo] on input line 2.\n"
        "LaTeX Warning: You have requested package `x',\n"
        "               but the package provides `y'.\n"
        "LaTeX Warning: There were undefined references.\n"
        "Package natbib Warning: There were multiply defined citations.\n"
        ")\n"
    )
    entries = parse_log(log.encode())
    assert [(e.kind, e.key, e.line) for e in entries] == [
        ("undefined-citation", "nat1", 3),
        ("warning", None, 2),
        ("warning", None, None),
        ("multiply-summary", None, None),
    ]
    assert entries[1].text == "Unused global option(s): [foo] on input line 2."
    assert entries[2].text == "You have requested package `x', but the package provides `y'."
    alone = parse_log(b"LaTeX Warning: There were undefined references.\n")
    assert [e.kind for e in alone] == ["undefined-summary"]


def test_summaries_alone_are_errors(tmp_path: Path) -> None:
    log = "LaTeX Warning: There were undefined references.\nLaTeX Warning: There were multiply-defined labels.\n"
    findings = run_log(place_log(tmp_path, log))
    assert [(f.severity, f.path) for f in findings] == [("error", None), ("error", None)]


def test_absolute_paths_inside_the_repo_are_mapped(tmp_path: Path) -> None:
    log_path = place_log(tmp_path, "")
    tex_dir = log_path.parent.parent.resolve().as_posix()
    content = f"{tex_dir}/appendix.tex:5: Missing $ inserted.\n/usr/share/texmf/tex/latex/x.sty:3: Bad thing.\n"
    log_path.write_text(content, encoding="utf-8")
    findings = run_log(log_path)
    assert summary(findings) == [
        ("E_VALIDATION_LATEX", "error", "tex/appendix.tex", 5),
        ("E_VALIDATION_LATEX", "error", None, 3),
    ]


@pytest.mark.parametrize(
    ("name", "tex_dir", "expected"),
    [
        ("../sections/a/a.tex", None, "sections/a/a.tex"),
        ("main.tex", None, "tex/main.tex"),
        ('"./preamble.tex"', None, "tex/preamble.tex"),
        (None, None, None),
        ("", None, None),
        ("  ", None, None),
        (".\\sections\\a.tex", None, None),
        ("../outside.tex", None, "outside.tex"),
        ("../../etc/x.tex", None, None),
        ("./build/main.aux", None, None),
        ("./build-diff/diff.tex", None, None),
        ("/usr/share/x.sty", None, None),
        ("C:/texlive/x.sty", None, None),
        ("/repo/tex/appendix.tex", "/repo/tex", "tex/appendix.tex"),
        ("/elsewhere/a.tex", "/repo/tex", None),
    ],
)
def test_repo_path(name: str | None, tex_dir: str | None, expected: str | None) -> None:
    assert repo_path(name, tex_dir) == expected


def test_log_outside_a_build_folder_keeps_relative_paths(tmp_path: Path) -> None:
    log = tmp_path / "main.log"
    log.write_text(f"{tmp_path.as_posix()}/x.tex:2: Oops.\n../sections/a/a.tex:4: Oops.\n", encoding="utf-8")
    assert summary(run_log(log)) == [
        ("E_VALIDATION_LATEX", "error", None, 2),
        ("E_VALIDATION_LATEX", "error", "sections/a/a.tex", 4),
    ]


def test_file_line_error_lookalikes_are_not_errors() -> None:
    log = (
        b"Package: foo 2024/01/01 v1.0: not an error: 12: x\n"
        b"(./main.tex) [1] stamp:12: x\n"
        b" ./indented.tex:3: x\n"
        b"noext:3: x\n"
    )
    assert parse_log(log) == []


def test_parenthesis_stack_ignores_help_text_and_non_files() -> None:
    log = (
        b"(./main.tex (../sections/a/a.tex (see the manual) [1] (e.g., `\\hobx')\n"
        b"LaTeX Warning: Reference `x' on page 1 undefined on input line 2.\n"
        b") (C:/texlive/fonts/x.fd)\n"
        b"LaTeX Warning: Reference `y' on page 1 undefined on input line 3.\n"
        b")\n"
    )
    entries = parse_log(log)
    assert [(e.file, e.key) for e in entries] == [("../sections/a/a.tex", "x"), ("./main.tex", "y")]


def test_fixture_logs_are_plain_data() -> None:
    # Los fixtures no contienen etiquetas de licencia y son texto LF.
    for path in LOGS.glob("*.log"):
        raw = path.read_bytes()
        assert b"\r" not in raw and raw.endswith(b"\n")
        assert b"SPDX" + b"-" not in raw


def test_write_diagnostics_cleans_up_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = diagnostics_result([], translator("en"))

    def fail_replace(_src: object, _dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        write_diagnostics(tmp_path / "d" / "diagnostics.json", result)
    assert list((tmp_path / "d").iterdir()) == []


def test_unknown_lines_and_copy_of_fixtures(tmp_path: Path) -> None:
    # Una copia del log en otra carpeta da los mismos hallazgos (no depende de la ruta).
    src = LOGS / "undefined-citation.log"
    other = tmp_path / "elsewhere" / "tex" / "build"
    other.mkdir(parents=True)
    shutil.copy(src, other / "main.log")
    assert summary(run_log(other / "main.log")) == summary(run_fixture(tmp_path, "undefined-citation.log"))
