# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``ci comment`` contra FakeForge (09-ci REQ-CI-20, REQ-CI-27)."""

from __future__ import annotations

import io
import json
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from aucove import _meta
from aucove.cli import ci as cli_ci
from aucove.forge.base import RepoRef
from aucove.forge.fake import Fault, FakeForge
from aucove.i18n import translator
from aucove.ops import ci_comment
from aucove.ops.ci_comment import (
    BOT_LOGIN,
    FLAG_INSTRUCTIONS,
    find_ci_comment,
    inline_code,
    parse_validate_report,
    render_build,
    render_validate,
    safe_url,
    visible_text,
)
from aucove.ops.diagnostics import Diagnostic

from tests.conftest import FIXTURES
from tests.unit.test_ci_build_plan import project_yaml

PR = 42
REPO = RepoRef("example-org", "demo")
SAMPLE = FIXTURES / "diagnostics" / "build-report.sample.json"
PDF_URL = "https://github.com/example-org/demo/actions/runs/7/artifacts/11"
DIFF_URL = "https://github.com/example-org/demo/actions/runs/7/artifacts/12"
RUN_URL = "https://github.com/example-org/demo/actions/runs/7"


# -- utilidades ---------------------------------------------------------------------


def split_code_spans(md: str) -> tuple[str, list[str]]:
    """``(texto fuera de código, contenidos de los fragmentos de código)`` según CommonMark."""
    outside: list[str] = []
    spans: list[str] = []
    i = 0
    while i < len(md):
        if md[i] != "`":
            outside.append(md[i])
            i += 1
            continue
        j = i
        while j < len(md) and md[j] == "`":
            j += 1
        n = j - i
        k, close = j, -1
        while k < len(md):
            if md[k] == "`":
                m = k
                while m < len(md) and md[m] == "`":
                    m += 1
                if m - k == n:
                    close = k
                    break
                k = m
            else:
                k += 1
        if close < 0:
            outside.append(md[i:j])
            i = j
            continue
        content = md[j:close]
        if len(content) >= 2 and content[0] == " " and content[-1] == " " and content.strip():
            content = content[1:-1]
        spans.append(content)
        i = close + n
    return "".join(outside), spans


class Env:
    """Estado de FakeForge en un archivo, como lo usa el CI en las pruebas por subproceso."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.state = tmp / "fake-state.json"
        forge = FakeForge(REPO, viewer=BOT_LOGIN)
        forge.seed_pull_request(files=[], number=PR, head_branch="task/17", head_sha="a" * 40, author="carla")
        forge.save(self.state)
        self.summary = tmp / "summary.md"

    def env(self, **extra: str) -> dict[str, str]:
        p = _meta.ENV_PREFIX
        base = {f"{p}TESTING": "1", f"{p}FORGE_ADAPTER": "fake", f"{p}FORGE_FAKE_STATE": str(self.state)}
        base.update(extra)
        return base

    def forge(self) -> FakeForge:
        return FakeForge.load(self.state)

    def edit(self, fn) -> None:  # type: ignore[no-untyped-def]
        forge = self.forge()
        fn(forge)
        forge.save(self.state)

    def run(self, *argv: str, env: Mapping[str, str] | None = None, summary: bool = True) -> tuple[int, str]:
        args = ["comment", *argv]
        if summary:
            args += ["--summary", str(self.summary)]
        out, err = io.StringIO(), io.StringIO()
        rc = cli_ci.run(args, env=self.env() if env is None else env, stdout=out, stderr=err)
        assert out.getvalue() == ""  # ci comment no escribe en stdout (ni el sobre)
        return rc, err.getvalue()

    def comments(self, kind: str | None = None) -> list[str]:
        forge = self.forge()
        bodies = [c.body for c in forge.list_comments(PR)]
        if kind is None:
            return bodies
        marker = _meta.ci_marker(kind)
        return [b for b in bodies if b.splitlines()[0] == marker]

    def report_dir(self, source: Path | None = SAMPLE) -> Path:
        d = self.tmp / "reports"
        d.mkdir(exist_ok=True)
        if source is not None:
            shutil.copyfile(source, d / "diagnostics.json")
        return d

    def policy(self, language: str = "es") -> Path:
        d = self.tmp / f"policy-{language}"
        d.mkdir(exist_ok=True)
        (d / "project.yaml").write_text(project_yaml(language=language), encoding="utf-8", newline="\n")
        return d

    def report(self, envelope: Mapping[str, object] | str, name: str = "validate.json") -> Path:
        path = self.tmp / name
        text = envelope if isinstance(envelope, str) else json.dumps(envelope, ensure_ascii=False)
        path.write_text(text, encoding="utf-8")
        return path


@pytest.fixture
def ci(tmp_path: Path) -> Env:
    return Env(tmp_path)


def build_args(ci: Env, *, outcome: str = "failure", language: str = "es", report_dir: Path | None = None) -> list[str]:
    return [
        "--kind", "build", "--pr", str(PR), "--language", language, "--outcome", outcome,
        "--pdf-url", PDF_URL, "--diff-url", "", "--run-url", RUN_URL,
        "--report-dir", str(report_dir if report_dir is not None else ci.report_dir()),
    ]


def finding(
    path: str | None,
    message: str,
    *,
    severity: str = "error",
    code: str = "E_PERM_OUTSIDE_TASK",
    line: int | None = None,
    hint: str | None = None,
) -> dict[str, object]:
    return {"path": path, "line": line, "severity": severity, "code": code, "message": message, "pointer": None, "hint": hint}


def envelope(results: Sequence[Mapping[str, object]], *, head_ref: str = "task/17") -> dict[str, object]:
    has_errors = any(f["severity"] == "error" for r in results for f in r["findings"])  # type: ignore[attr-defined]
    return {
        "schema": _meta.RESULT_SCHEMA,
        "ok": not has_errors,
        "op": "validate",
        "data": {
            "valid": not has_errors,
            "base": {"ref": "main", "sha": "9f2c1e0" + "0" * 33},
            "head": {"ref": head_ref, "sha": "4ab77d1" + "0" * 33},
            "mode": "pr",
            "pr": PR,
            "results": list(results),
            "page": {"next_cursor": None, "returned": len(results), "total": len(results)},
        },
        "warnings": [],
        "error": (
            {"code": "E_VALIDATION_FAILED", "message": "x", "retryable": False, "hint": None} if has_errors else None
        ),
    }


def validate_args(ci: Env, report: Path, *, language: str | None = "es") -> list[str]:
    args = ["--kind", "validate", "--pr", str(PR), "--report", str(report)]
    if language is not None:
        args += ["--policy", str(ci.policy(language))]
    return args


# -- build (REQ-CI-20) ---------------------------------------------------------


def test_build_lists_the_c29_fixture_diagnostics(ci: Env) -> None:
    rc, err = ci.run(*build_args(ci))
    assert rc == 0, err
    [body] = ci.comments("build")
    assert body.startswith(_meta.ci_marker("build") + "\n")
    assert f"{_meta.NAME}/build: la compilación falló" in body
    assert "`sections/methods/methods.tex:12`" in body
    assert "Undefined control sequence \\foo." in body
    assert "`tex/main.tex`" in body  # aviso sin línea
    assert "**error**" in body and "**aviso**" in body
    assert "`latex-log`" in body
    assert "Se muestran 2 de 5." in body  # 2 hallazgos más los 3 omitidos de W_OUTPUT_TRUNCATED
    assert f"[PDF]({PDF_URL})" in body and f"({RUN_URL})" in body
    assert "diferencias" not in body  # sin diff no hay enlace


def test_build_without_diagnostics_file_says_unavailable(ci: Env) -> None:
    rc, _err = ci.run(*build_args(ci, report_dir=ci.report_dir(None)))
    assert rc == 0
    [body] = ci.comments("build")
    assert "diagnóstico no disponible" in body.lower()


def test_build_without_report_dir_in_english(ci: Env) -> None:
    args = build_args(ci, language="en")
    args[args.index("--report-dir") + 1] = str(ci.tmp / "missing-dir")
    rc, _err = ci.run(*args)
    assert rc == 0
    [body] = ci.comments("build")
    assert "Diagnostics not available." in body
    assert f"{_meta.NAME}/build: the build failed" in body


def test_build_with_an_invalid_diagnostics_file_says_unavailable(ci: Env) -> None:
    d = ci.report_dir(None)
    (d / "diagnostics.json").write_text('{"schema": "otro"}', encoding="utf-8")
    rc, _err = ci.run(*build_args(ci, report_dir=d))
    assert rc == 0
    assert "Diagnóstico no disponible." in ci.comments("build")[0]


def test_build_skipped_has_no_diagnostics_section(ci: Env) -> None:
    rc, _err = ci.run(*build_args(ci, outcome="skipped", report_dir=ci.report_dir(None)))
    assert rc == 0
    [body] = ci.comments("build")
    assert "compilación omitida" in body
    assert "Diagnóstico" not in body


def test_build_diff_link_when_present(ci: Env) -> None:
    args = build_args(ci, outcome="success")
    args[args.index("--diff-url") + 1] = DIFF_URL
    assert ci.run(*args)[0] == 0
    body = ci.comments("build")[0]
    assert f"({DIFF_URL})" in body
    assert "la compilación terminó bien" in body


@pytest.mark.parametrize(
    "url",
    ["javascript:alert(1)", "http://example.invalid/x", "https://x.invalid/a b", "https://x.invalid/(x)", "", "https://x/`y`"],
)
def test_unsafe_urls_are_not_linked(url: str) -> None:
    assert safe_url(url) is None
    body = render_build(translator("es"), outcome="failure", pdf_url=url, diff_url=None, run_url=url, diagnostics=[])
    assert "](" not in body
    assert "PDF: no disponible" in body


def test_build_only_first_ten_diagnostics() -> None:
    diags = [Diagnostic(f"tex/a{i}.tex", i + 1, "error", f"m{i}", "latex-log") for i in range(15)]
    body = render_build(translator("en"), outcome="failure", pdf_url=None, diff_url=None, run_url=None, diagnostics=diags)
    assert "`tex/a9.tex:10`" in body and "tex/a10.tex" not in body
    assert "Showing 10 of 15." in body


def test_build_writes_the_job_summary_without_the_marker(ci: Env) -> None:
    rc, _err = ci.run(*build_args(ci))
    assert rc == 0
    summary = ci.summary.read_text(encoding="utf-8")
    assert f"### {_meta.NAME}/build" in summary
    assert "<!--" not in summary
    assert "\r" not in summary


# -- comentario único ---------------------------------------------------------------


def test_repeated_comment_leaves_a_single_comment(ci: Env) -> None:
    assert ci.run(*build_args(ci, outcome="failure"))[0] == 0
    assert ci.run(*build_args(ci, outcome="success"))[0] == 0
    bodies = ci.comments("build")
    assert len(bodies) == 1
    assert "la compilación terminó bien" in bodies[0]  # se actualizó
    forge = ci.forge()
    assert forge.calls.count(method="create_comment") == 1
    assert forge.calls.count(method="update_comment") == 1


def test_identical_comment_is_not_rewritten(ci: Env) -> None:
    report = ci.tmp / "missing.json"
    for _ in range(3):
        assert ci.run(*validate_args(ci, report))[0] == 0
    forge = ci.forge()
    assert len(ci.comments("validate")) == 1
    assert forge.calls.count(method="create_comment") == 1
    # El cuerpo creado lleva además el marcador op de la forja; no cuenta como cambio.
    assert forge.calls.count(method="update_comment") == 0
    assert "\n<!-- " + _meta.NAME + ":op v1 id=" in ci.comments("validate")[0]


def test_without_op_marker_only_strips_the_trailing_marker() -> None:
    op = "<!-- " + _meta.NAME + ":op v1 id=ci-build-42 -->"
    assert ci_comment.without_op_marker("a\nb\n\n" + op) == "a\nb"
    assert ci_comment.without_op_marker("a\n" + op + "\n") == "a"
    assert ci_comment.without_op_marker(op + "\nb\n") == op + "\nb"  # solo al final
    assert ci_comment.without_op_marker("a\n<!-- otro:op v1 id=x -->") == "a\n<!-- otro:op v1 id=x -->"


def test_marker_from_another_author_is_ignored(ci: Env) -> None:
    marker = _meta.ci_marker("build")
    ci.edit(lambda f: f.seed_comment(PR, author="mallory", body=f"{marker}\nfalso"))
    assert ci.run(*build_args(ci))[0] == 0
    forge = ci.forge()
    comments = forge.list_comments(PR)
    assert [c.author for c in comments] == ["mallory", BOT_LOGIN]
    assert comments[0].body == f"{marker}\nfalso"


def test_build_and_validate_comments_are_separate(ci: Env) -> None:
    assert ci.run(*build_args(ci))[0] == 0
    assert ci.run(*validate_args(ci, ci.tmp / "missing.json"))[0] == 0
    assert ci.run(*build_args(ci, outcome="success"))[0] == 0
    assert len(ci.comments("build")) == 1
    assert len(ci.comments("validate")) == 1
    assert len(ci.comments()) == 2


def test_pr_text_with_a_marker_does_not_capture_another_comment(ci: Env) -> None:
    # Una ruta del PR que contiene el marcador de build no convierte el comentario de validate en el de build.
    evil = _meta.ci_marker("build")
    report = ci.report(envelope([{"check": "scope", "status": "fail", "findings": [finding(evil, "m")]}]))
    assert ci.run(*validate_args(ci, report))[0] == 0
    assert ci.run(*build_args(ci))[0] == 0
    assert len(ci.comments()) == 2
    assert len(ci.comments("build")) == 1


def test_find_ci_comment_picks_the_oldest_bot_comment(ci: Env) -> None:
    marker = _meta.ci_marker("validate")
    ci.edit(lambda f: f.seed_comment(PR, author=BOT_LOGIN, body=f"{marker}\nuno", created_at="2026-09-01T00:00:00Z"))
    ci.edit(lambda f: f.seed_comment(PR, author=BOT_LOGIN, body=f"{marker}\ndos", created_at="2026-09-02T00:00:00Z"))
    ci.edit(lambda f: f.seed_comment(PR, author=BOT_LOGIN, body=f"texto\n{marker}", created_at="2026-08-01T00:00:00Z"))
    found = find_ci_comment(ci.forge().list_comments(PR), "validate")
    assert found is not None and found.body.endswith("uno")
    assert find_ci_comment(ci.forge().list_comments(PR), "build") is None


# -- validate (REQ-CI-27) -----------------------------------------------------------


def test_validate_without_report_says_unavailable_and_exits_0(ci: Env) -> None:
    rc, _err = ci.run(*validate_args(ci, ci.tmp / "missing.json"))
    assert rc == 0
    [body] = ci.comments("validate")
    assert "validación no disponible" in body.lower()
    assert "validación no disponible" in ci.summary.read_text(encoding="utf-8").lower()


@pytest.mark.parametrize(
    "content",
    [
        "no es json",
        json.dumps({"schema": "otro/v1", "op": "validate"}),
        json.dumps({"schema": _meta.RESULT_SCHEMA, "op": "doctor", "data": {}}),
        json.dumps({"schema": _meta.RESULT_SCHEMA, "op": "validate", "data": {"results": [{"check": "x"}]}}),
        json.dumps({"schema": _meta.RESULT_SCHEMA, "op": "validate", "data": {"results": "x"}}),
    ],
)
def test_validate_with_an_unreadable_report_says_unavailable(ci: Env, content: str) -> None:
    rc, _err = ci.run(*validate_args(ci, ci.report(content)))
    assert rc == 0
    assert "Validación no disponible" in ci.comments("validate")[0]


def test_validate_report_is_required(ci: Env) -> None:
    rc, err = ci.run("--kind", "validate", "--pr", str(PR))
    assert rc == 3
    assert "--report" in err
    assert ci.comments() == []


def test_validate_summarizes_rules_and_findings(ci: Env) -> None:
    results: list[dict[str, object]] = [
        {"check": "scope", "status": "fail", "findings": [
            finding("sections/methods/methods.tex", "La parte 'methods' no está declarada en la tarea #17.",
                    hint="Pide al responsable que agregue la parte."),
        ]},
        {"check": "assessment", "status": "pass", "findings": [
            finding(None, "El PR no tiene evaluación de traslape registrada.", severity="warning",
                    code="E_VALIDATION_ASSESSMENT"),
        ]},
        {"check": "schemas", "status": "pass", "findings": []},
        {"check": "latex-log", "status": "skip", "findings": []},
    ]
    rc, _err = ci.run(*validate_args(ci, ci.report(envelope(results))))
    assert rc == 0
    [body] = ci.comments("validate")
    assert f"### {_meta.NAME}/validate: con errores" in body
    assert "1 error y 1 aviso." in body
    assert "- `scope`: falla (1 error)" in body
    assert "- `assessment`: pasa (1 aviso)" in body
    assert "- `schemas`: pasa" in body
    assert "- `latex-log`: omitida" in body
    assert "`sections/methods/methods.tex`: `La parte 'methods' no está declarada en la tarea #17.`" in body
    assert "(`E_PERM_OUTSIDE_TASK`, `scope`)" in body
    assert "Sugerencia: `Pide al responsable que agregue la parte.`" in body
    assert "Cabeza: `task/17` (`4ab77d1`)" in body
    assert body.index("**error**") < body.index("**aviso**")


def test_validate_all_pass(ci: Env) -> None:
    results = [{"check": "scope", "status": "pass", "findings": []}]
    rc, _err = ci.run(*validate_args(ci, ci.report(envelope(results)), language="en"))
    assert rc == 0
    body = ci.comments("validate")[0]
    assert f"{_meta.NAME}/validate: passed" in body
    assert "All rules pass." in body


def test_validate_incomplete_envelope(ci: Env) -> None:
    env = {
        "schema": _meta.RESULT_SCHEMA, "ok": False, "op": "validate", "data": None, "warnings": [],
        "error": {"code": "E_CONFIG_POLICY_UNAVAILABLE", "message": "No se pudo leer la política.",
                  "retryable": False, "hint": None},
    }
    rc, _err = ci.run(*validate_args(ci, ci.report(env)))
    assert rc == 0
    body = ci.comments("validate")[0]
    assert "La validación no terminó: `E_CONFIG_POLICY_UNAVAILABLE`" in body


def test_validate_language_comes_from_the_policy_or_the_override(ci: Env) -> None:
    report = ci.tmp / "missing.json"
    assert ci.run(*validate_args(ci, report, language="en"))[0] == 0
    assert "Validation not available" in ci.comments("validate")[0]
    assert ci.run(*validate_args(ci, report, language="en"), "--language", "es")[0] == 0
    assert "Validación no disponible" in ci.comments("validate")[0]
    assert ci.run(*validate_args(ci, report, language=None))[0] == 0  # sin política: en
    assert "Validation not available" in ci.comments("validate")[0]


def test_pr_text_is_escaped_truncated_and_without_mentions(ci: Env) -> None:
    long_path = "sections/@mallory/‮`evil`<b>x</b>" + "a" * 300 + ".tex"
    results = [{"check": "scope", "status": "fail", "findings": [
        finding(long_path, "Mira @everyone <img src=x onerror=alert(1)> **negrita** #1\n\n# título", line=3,
                hint="Avisa a @admin y a ``@octocat`` [x](https://evil.invalid)"),
        finding("tex/a.tex", "`" * 250, severity="warning", code="E_VALIDATION_LABEL"),
    ]}]
    rc, _err = ci.run(*validate_args(ci, ci.report(envelope(results, head_ref="task/17-@octocat"))))
    assert rc == 0
    [body] = ci.comments("validate")
    body = body.split("<!-- " + _meta.NAME + ":op", 1)[0]  # el marcador op lo agrega la forja falsa
    outside, spans = split_code_spans(body.split("\n", 1)[1])  # sin la línea del marcador ci
    assert "@" not in outside
    assert "<img" not in outside and "<b>" not in outside and "](https://evil" not in outside
    assert "‮" not in body and "U+202E" in body
    assert all(len(s) <= 200 for s in spans)
    [path_span] = [s for s in spans if s.startswith("sections/@mallory")]
    assert len(path_span) == 200 and path_span.endswith("…")
    assert any(s.startswith("Mira @everyone") and "\n" not in s for s in spans)
    assert "`task/17-@octocat`" in body
    for line in body.splitlines():
        assert not line.startswith("# ")  # el salto de línea del mensaje no crea un título


def test_build_diagnostics_are_escaped_too(ci: Env) -> None:
    diags = [Diagnostic("tex/@x.tex", 1, "error", "@everyone " + "z" * 400, "latex-log")]
    body = render_build(translator("es"), outcome="failure", pdf_url=None, diff_url=None, run_url=None, diagnostics=diags)
    outside, spans = split_code_spans(body.split("\n", 1)[1])
    assert "@" not in outside
    assert all(len(s) <= 200 for s in spans)


def test_many_findings_are_capped() -> None:
    findings = [finding(f"tex/f{i}.tex", "`" * 199, line=i + 1) for i in range(40)]
    env = envelope([{"check": "scope", "status": "fail", "findings": findings}])
    report = parse_validate_report(env)
    body = render_validate(translator("es"), report)
    assert len(body) <= ci_comment.MAX_BODY_CHARS
    assert "Se muestran 30 de 40 hallazgos" in body
    assert body.count("**error**") == ci_comment.MAX_VALIDATE_FINDINGS


def test_body_stays_under_the_github_limit_with_adversarial_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ci_comment, "MAX_VALIDATE_FINDINGS", 1000)
    findings = [finding("`" * 199, "`" * 199, hint="`" * 199) for _ in range(200)]
    body = render_validate(translator("en"), parse_validate_report(envelope([{"check": "s", "status": "fail", "findings": findings}])))
    assert len(body) <= ci_comment.MAX_BODY_CHARS
    assert "Showing" in body


def test_instructions_lint_warning_adds_the_label(ci: Env) -> None:
    results = [{"check": "instructions-lint", "status": "pass", "findings": [
        finding("AGENTS.md", "Carácter invisible.", severity="warning", code="IL03", line=2),
    ]}]
    assert ci.run(*validate_args(ci, ci.report(envelope(results))))[0] == 0
    pr = ci.forge().get_pull_request(PR)
    assert pr is not None and FLAG_INSTRUCTIONS in pr.labels


def test_no_label_without_instruction_warnings(ci: Env) -> None:
    results = [{"check": "scope", "status": "fail", "findings": [finding("a.tex", "m")]}]
    assert ci.run(*validate_args(ci, ci.report(envelope(results))))[0] == 0
    pr = ci.forge().get_pull_request(PR)
    assert pr is not None and FLAG_INSTRUCTIONS not in pr.labels


# -- errores de la forja y del resumen ----------------------------------------------


def test_read_only_token_keeps_the_summary_and_exits_0(ci: Env) -> None:
    ci.edit(lambda f: f.faults.add(Fault(method="create_comment", kind="http_403")))
    rc, err = ci.run(*build_args(ci))
    assert rc == 0
    assert "resumen del job" in err or "job summary" in err
    assert "Diagnósticos" in ci.summary.read_text(encoding="utf-8")
    assert ci.comments() == []


@pytest.mark.parametrize("kind", ["unreachable", "http_5xx", "http_401", "timeout_before"])
def test_forge_failures_exit_1(ci: Env, kind: str) -> None:
    ci.edit(lambda f: f.faults.add(Fault(method="list_comments", kind=kind)))  # type: ignore[arg-type]
    rc, err = ci.run(*build_args(ci))
    assert rc == 1
    assert err
    assert ci.summary.is_file()  # el resumen se escribe antes de publicar


def test_missing_pull_request_exits_1(ci: Env) -> None:
    rc, _err = ci.run("--kind", "build", "--pr", "999", "--outcome", "success")
    assert rc == 1


def test_without_a_forge_exits_1_but_writes_the_summary(ci: Env) -> None:
    rc, err = ci.run(*validate_args(ci, ci.tmp / "missing.json"), env={})
    assert rc == 1
    assert "E_" in err
    assert ci.summary.is_file()


def test_unwritable_summary_exits_1_but_still_comments(ci: Env) -> None:
    ci.summary.mkdir()
    rc, err = ci.run(*build_args(ci))
    assert rc == 1
    assert err
    assert len(ci.comments("build")) == 1


def test_summary_is_appended(ci: Env) -> None:
    ci.summary.write_text("previo\n", encoding="utf-8")
    assert ci.run(*build_args(ci))[0] == 0
    text = ci.summary.read_text(encoding="utf-8")
    assert text.startswith("previo\n### ")


@pytest.mark.parametrize(
    "args",
    [
        ["--kind", "build"],
        ["--pr", "1"],
        ["--kind", "model-review", "--pr", "1"],
        ["--kind", "build", "--pr", "0"],
        ["--kind", "build", "--pr", "-1"],
        ["--kind", "build", "--pr", "abc"],
        ["--kind", "build", "--pr", "1", "--language", "fr"],
        ["--kind", "build", "--pr", "1", "--outcome", "ok"],
        ["--kind", "build", "--pr", "1", "--bogus"],
        ["--kind", "expire-claims"],  # sin --report
        ["--kind", "expire-claims", "--report", "x.json", "--pr", "1"],  # no es de un PR
        ["--kind", "validate", "--report", "x.json"],  # sin --pr
    ],
)
def test_usage_errors_exit_3(ci: Env, args: list[str]) -> None:
    rc, _err = ci.run(*args, summary=False)
    assert rc == 3
    assert ci.comments() == []


# -- expire-claims (09-ci §6; 05 §9.19) ---------------------------------------------------------


def expire_envelope(**data: object) -> dict[str, object]:
    base: dict[str, object] = {"evaluated": 6, "threshold_days": 14, "warned": [], "expired": [],
                               "unexpired": [], "repaired": [], "skipped": []}
    base.update(data)
    return {"schema": f"{_meta.NAME}.result/v1", "ok": True, "op": "expire-claims", "data": base,
            "warnings": [], "error": None}


def write_report(ci: Env, envelope: object) -> Path:
    path = ci.tmp / "expire.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    return path


def test_expire_claims_writes_only_the_job_summary(ci: Env) -> None:
    """Sin PR ni forja: el resumen lista cada tarea con su reclamante, escapado (REQ-CI-27)."""
    envelope = expire_envelope(
        warned=[{"task": 12, "claimed_by": "carla", "last_activity": "2026-10-01T00:00:00Z"}],
        expired=[{"task": 9, "claimed_by": "x`@everyone", "last_activity": "2026-09-01T00:00:00Z"}],
        unexpired=[{"task": 7, "claimed_by": None, "last_activity": "2026-10-08T00:00:00Z"}],
        repaired=[{"task": 3, "action": "close_merged_issue"}],
        skipped=[{"task": 4, "reason": "blocked"}, {"task": 5, "reason": "otra"}],
    )
    calls = len(ci.forge().calls.entries())
    rc, _err = ci.run("--kind", "expire-claims", "--policy", str(ci.policy("es")), "--report", str(write_report(ci, envelope)))
    assert rc == 0 and ci.comments() == [] and len(ci.forge().calls.entries()) == calls
    text = ci.summary.read_text(encoding="utf-8")
    assert text.startswith(f"### {_meta.NAME}/expire-claims\n")
    assert "6 tareas evaluadas; un reclamo vence a los 14 días sin actividad." in text
    assert "- Avisadas: #12 (`@carla`)" in text and "- Renovadas (se quitó `state:expired`): #7 (sin reclamante)" in text
    assert "#9 (``@x`@everyone``)" in text  # el login va dentro de código: no menciona a nadie
    assert "- Reparadas: #3 (se cerró el Issue de un PR integrado)" in text
    assert "- Omitidas: #4 (`flag:blocked`), #5 (`otra`)" in text


@pytest.mark.parametrize(("envelope", "expected"), [
    (expire_envelope(), "Sin cambios"),
    ({"schema": f"{_meta.NAME}.result/v1", "ok": False, "op": "expire-claims", "data": None, "warnings": [],
      "error": {"code": "E_NET_OFFLINE", "message": "x", "retryable": True, "details": {}}}, "`E_NET_OFFLINE`"),
    ({"op": "validate", "data": {}}, "no disponible"),
    (expire_envelope(warned=[{"task": 0}]), "no disponible"),
    ("no es un sobre", "no disponible"),
])
def test_expire_claims_summary_cases(ci: Env, envelope: object, expected: str) -> None:
    rc, _err = ci.run("--kind", "expire-claims", "--policy", str(ci.policy("es")), "--report", str(write_report(ci, envelope)))
    assert rc == 0 and expected in ci.summary.read_text(encoding="utf-8")


def test_expire_claims_summary_language(ci: Env) -> None:
    report = str(write_report(ci, expire_envelope(evaluated=1)))
    assert ci.run("--kind", "expire-claims", "--policy", str(ci.policy("en")), "--report", report)[0] == 0
    assert "1 task evaluated" in ci.summary.read_text(encoding="utf-8")


def test_expire_claims_without_a_report_file(ci: Env) -> None:
    rc, _err = ci.run("--kind", "expire-claims", "--report", str(ci.tmp / "missing.json"))
    assert rc == 0 and "not available" in ci.summary.read_text(encoding="utf-8")


def test_empty_language_falls_back(ci: Env) -> None:
    rc, _err = ci.run(*build_args(ci, language=""))
    assert rc == 0
    assert "the build failed" in ci.comments("build")[0]


def test_empty_language_uses_the_policy_when_given(ci: Env) -> None:
    rc, _err = ci.run(*build_args(ci, language=""), "--policy", str(ci.policy("es")))
    assert rc == 0
    assert "la compilación falló" in ci.comments("build")[0]
    # --language gana a la política
    rc, _err = ci.run(*build_args(ci, language="en"), "--policy", str(ci.policy("es")))
    assert rc == 0
    assert "the build failed" in ci.comments("build")[0]


# -- escape -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a", "`a`"),
        ("a`b", "``a`b``"),
        ("`a", "`` `a ``"),
        ("a``", "``` a`` ```"),
        ("", "` `"),
        ("  a \n b\t", "`a b`"),
        ("@x", "`@x`"),
    ],
)
def test_inline_code(text: str, expected: str) -> None:
    assert inline_code(text) == expected


def test_visible_text_marks_invisible_characters_and_truncates() -> None:
    assert visible_text("a​b‮c\x07d") == "aU+200BbU+202EcU+0007d"
    assert visible_text("x" * 250) == "x" * 199 + "…"
    assert len(visible_text(" " * 100)) == 200
    assert visible_text("a b") == "a b"
