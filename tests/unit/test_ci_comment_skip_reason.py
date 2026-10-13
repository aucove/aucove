# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``ci comment --skip-reason`` (09-ci §3.3 y REQ-CI-49; 00-marco D-43).

Con ``format-none`` (sin manuscrito) ``ci comment`` escribe solo el resumen del job y una
anotación ``::notice::``, sin tocar la forja; con ``no-changes`` comenta como siempre, con
el motivo, y agrega la anotación. Reutiliza el entorno de ``test_ci_comment``.
"""

from __future__ import annotations

import io
from collections.abc import Mapping

import pytest

from aucove import _meta
from aucove.cli import ci as cli_ci
from aucove.i18n import translator
from aucove.ops.ci_comment import notice_line, render_build, render_no_manuscript
from tests.unit.test_ci_comment import PR, Env, build_args, ci  # noqa: F401  (fixture ci)

CHECK = f"{_meta.NAME}/build"
REASON_ES = "Sin manuscrito (`format: none`): no hay nada que compilar."
REASON_EN = "No manuscript (`format: none`): nothing to compile."


def run(ci: Env, *argv: str, env: Mapping[str, str] | None = None) -> tuple[int, str, str]:
    """``ci comment`` con stdout capturado (la anotación ``::notice::`` va ahí)."""
    out, err = io.StringIO(), io.StringIO()
    args = ["comment", *argv, "--summary", str(ci.summary)]
    rc = cli_ci.run(args, env=ci.env() if env is None else env, stdout=out, stderr=err)
    return rc, out.getvalue(), err.getvalue()


def skipped_args(ci: Env, reason: str, *, language: str = "es") -> list[str]:
    args = build_args(ci, outcome="skipped", language=language, report_dir=ci.report_dir(None))
    return [*args, "--skip-reason", reason]


def test_format_none_writes_only_the_summary_and_a_notice(ci: Env) -> None:
    before = ci.forge().calls.entries()
    rc, out, err = run(ci, *skipped_args(ci, "format-none"))
    assert rc == 0, err
    assert ci.comments() == []  # REQ-CI-49: ningún comentario en el PR
    assert ci.forge().calls.entries() == before  # ni siquiera se lista la conversación
    assert out == f"::notice title={CHECK}::{REASON_ES}\n"
    summary = ci.summary.read_text(encoding="utf-8")
    assert summary == f"### {CHECK}: sin manuscrito\n\n{REASON_ES}\n"
    # El registro del job (idioma del entorno, no del PR) dice por qué no hay comentario.
    assert "No manuscript" in err or "Sin manuscrito" in err


def test_format_none_needs_no_forge(ci: Env) -> None:
    """Sin adaptador de la forja (ni token) igual termina en éxito: no la necesita."""
    rc, out, err = run(ci, *skipped_args(ci, "format-none", language="en"), env={})
    assert rc == 0, err
    assert out == f"::notice title={CHECK}::{REASON_EN}\n"
    assert ci.summary.read_text(encoding="utf-8") == f"### {CHECK}: no manuscript\n\n{REASON_EN}\n"


def test_format_none_leaves_an_earlier_comment_untouched(ci: Env) -> None:
    assert run(ci, *build_args(ci, outcome="failure"))[0] == 0
    [old] = ci.comments("build")
    rc, _out, err = run(ci, *skipped_args(ci, "format-none"))
    assert rc == 0, err
    assert ci.comments("build") == [old]


def test_format_none_with_an_unwritable_summary_fails(ci: Env) -> None:
    ci.summary.mkdir()  # un directorio no se puede abrir para agregar
    rc, out, _err = run(ci, *skipped_args(ci, "format-none"))
    assert rc == 1
    assert out.startswith("::notice ")
    assert ci.comments() == []


def test_no_changes_comments_with_the_reason_and_a_notice(ci: Env) -> None:
    rc, out, err = run(ci, *skipped_args(ci, "no-changes"))
    assert rc == 0, err
    [body] = ci.comments("build")
    assert f"### {CHECK}: compilación omitida\n" in body
    assert "El PR no cambia archivos que se compilen" in body and "`sections/`" in body
    assert "falló un paso anterior" not in body  # el motivo es conocido: no el texto genérico
    assert out.startswith(f"::notice title={CHECK}::El PR no cambia archivos que se compilen")
    assert out.count("\n") == 1


def test_empty_skip_reason_keeps_the_current_behavior(ci: Env) -> None:
    rc, out, err = run(ci, *skipped_args(ci, ""))
    assert rc == 0, err
    assert out == ""  # sin motivo no hay anotación
    [body] = ci.comments("build")
    assert "compilación omitida (el PR no cambia archivos que se compilen, o falló un paso anterior" in body


@pytest.mark.parametrize("reason", ["format_none", "none", "FORMAT-NONE", "skipped"])
def test_unknown_skip_reason_is_a_usage_error(ci: Env, reason: str) -> None:
    rc, out, _err = run(ci, *skipped_args(ci, reason))
    assert (rc, out) == (3, "")
    assert ci.comments() == []


def test_skip_reason_is_only_for_build(ci: Env) -> None:
    report = str(ci.report({"op": "validate"}))
    rc, out, err = run(ci, "--kind", "validate", "--pr", str(PR), "--report", report, "--skip-reason", "format-none")
    assert (rc, out) == (3, "")
    assert "--skip-reason" in err
    assert ci.comments() == []


def test_render_build_names_the_reason_only_for_a_skipped_build() -> None:
    t = translator("en")
    skipped = render_build(t, outcome="skipped", pdf_url=None, diff_url=None, run_url=None, diagnostics=None,
                           skip_reason="no-changes")
    assert f"### {CHECK}: build skipped\n" in skipped and "The PR changes no files that are compiled" in skipped
    failed = render_build(t, outcome="failure", pdf_url=None, diff_url=None, run_url=None, diagnostics=[],
                          skip_reason="no-changes")
    assert "The PR changes no files" not in failed and "the build failed" in failed


def test_notice_line_escapes_newlines() -> None:
    class Fake:
        def __call__(self, key: str, /, **kw: object) -> str:
            return "a%b\r\n::error::x"

    line = notice_line(Fake(), "format-none")  # type: ignore[arg-type]
    assert line == f"::notice title={CHECK}::a%25b%0D%0A::error::x\n"
    assert line.count("\n") == 1
    assert render_no_manuscript(translator("es")).endswith(f"{REASON_ES}\n")
