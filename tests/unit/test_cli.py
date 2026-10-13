# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""CLI generada del registro (05 §3.4, §4, §5; REQ-OPS-9, -12, -15, -41; REQ-ARCH-21, -23, -53).

Las operaciones de H2 llegan en la ola 3, así que casi todas las pruebas usan un
registro falso cuyos módulos se inyectan en ``sys.modules``. Las que usan el
``REGISTRY`` real no llegan a importar ningún módulo de operación: fallan antes,
en el análisis de argumentos, o solo piden la ayuda.
"""

from __future__ import annotations

import argparse
import dataclasses
import io
import json
import sys
import types
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from aucove import _meta, entry
from aucove.cli import main as cli
from aucove.i18n import Translator, catalog_keys, check_parity, translator
from aucove.ops.context import Invocation, OpContext
from aucove.ops.registry import REGISTRY, OpSpec, Param
from aucove.ops.result import OpError, Result, dumps, make_warning, ok
from tests.conftest import REPO_ROOT

LANG_VAR = _meta.ENV_PREFIX + "LANG"
MOD_PREFIX = "ops._cli_test_"


# -- registro falso ---------------------------------------------------------------


@dataclass
class Calls:
    """Lo que recibió cada operación falsa: (op, kwargs, Invocation)."""

    items: list[tuple[str, dict[str, object], Invocation]] = field(default_factory=list)

    def last(self) -> tuple[str, dict[str, object], Invocation]:
        assert self.items, "la operación no se ejecutó"
        return self.items[-1]


def _spec(op_id: str, cli_words: tuple[str, ...], module: str, *, params: tuple[Param, ...] = (),
          human_only: bool = False, effects: str = "none") -> OpSpec:
    return OpSpec(
        id=op_id, cli=cli_words, mcp_tool=None if human_only else op_id.replace("-", "_").replace(".", "_"),
        human_only=human_only, effects=effects, offline="local",  # type: ignore[arg-type]
        module=MOD_PREFIX + module, params=params,
    )


ECHO = _spec("check-path", ("check-path",), "echo", params=(
    Param("paths", "array", required=True, positional=True, repeatable=True,
          schema={"items": {"type": "string", "minLength": 1}, "minItems": 1, "maxItems": 3}),
    Param("task", "integer", schema={"minimum": 1}),
    Param("checks", "array", repeatable=True, cli_flag="--check", schema={"items": {"type": "string"}, "maxItems": 2}),
    Param("plan_refs", "array", repeatable=True, schema={"items": {"type": "string"}}),
    Param("changed_only", "boolean", default=False),
    Param("mode", "string", default="a", cli_only=True, choices=("a", "b")),
    Param("numbers", "array", cli_only=True, schema={"items": {"type": "integer"}}),
))
RENDER = _spec("validate", ("validate",), "render", params=(
    Param("format", "string", default="text", cli_only=True, choices=("text", "github")),
))
PLAIN = _spec("doctor", ("doctor",), "plain", params=(Param("refresh", "boolean", default=False),))
FAILING = _spec("sync-roles", ("sync-roles",), "failing")
GROUP = _spec("task.propose", ("task", "propose"), "group", params=(Param("title", "string", required=True),))
MISSING = _spec("status", ("status",), "missing")
HUMAN = _spec("review.approve", ("review", "approve"), "human", human_only=True)
DRY = _spec("save", ("save",), "dry", effects="local")
INTERRUPT = _spec("release", ("release",), "interrupt")
NOISY = _spec("context", ("context",), "noisy")
BAD_RENDER = _spec("metrics", ("metrics",), "badrender")

FAKE_REGISTRY: tuple[OpSpec, ...] = (ECHO, RENDER, PLAIN, FAILING, GROUP, MISSING, HUMAN, DRY, INTERRUPT, NOISY, BAD_RENDER)


def _install(monkeypatch: pytest.MonkeyPatch, name: str, run: Callable[..., Result],
             render: Callable[..., str] | None = None) -> None:
    full = f"{_meta.NAME}.{MOD_PREFIX}{name}"
    module = types.ModuleType(full)
    module.run = run  # type: ignore[attr-defined]
    if render is not None:
        module.render_text = render  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, full, module)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    record = Calls()

    def recorder(op_id: str, data: dict[str, object] | None = None) -> Callable[..., Result]:
        def run(ctx: OpContext, **kwargs: object) -> Result:
            record.items.append((op_id, dict(kwargs), ctx.inv))
            return ok(op_id, data if data is not None else {"n": len(record.items)})
        return run

    def plain(ctx: OpContext, **kwargs: object) -> Result:
        record.items.append(("doctor", dict(kwargs), ctx.inv))
        data = {"project_root": None, "checks": [{"id": "git", "status": "ok"}, {"id": "cache", "status": "skip"}],
                "note": "rojo \x1b[31m‮oculto", "empty": []}
        return ok("doctor", data, warnings=[make_warning("W_NET_OFFLINE", ctx.t())])

    def failing(ctx: OpContext, **kwargs: object) -> Result:
        record.items.append(("sync-roles", dict(kwargs), ctx.inv))
        raise OpError("E_FORGE_RATE_LIMITED", details={"retry_after_s": 30})

    def dry(ctx: OpContext, **kwargs: object) -> Result:
        record.items.append(("save", dict(kwargs), ctx.inv))
        if ctx.inv.dry_run:
            return ok("save", {"dry_run": True, "plan": [
                {"kind": "git_commit", "target": "task/12", "summary": "2 archivos"},
                {"kind": "git_push", "target": "origin task/12", "summary": "1 commit"}]})
        return ok("save", {"done": True})

    def interrupt(ctx: OpContext, **kwargs: object) -> Result:
        raise KeyboardInterrupt

    def noisy(ctx: OpContext, **kwargs: object) -> Result:
        print("ruido en stdout")
        return ok("context", {"quiet": False})

    def render(result: Result, t: Translator, *, fmt: str = "text") -> str:
        assert result.data is not None
        return f"rendered:{fmt}:{t.lang}:{result.data['n']}"

    def bad_render(result: Result, t: Translator, *, fmt: str = "text") -> str:
        raise RuntimeError("secreto del render")

    _install(monkeypatch, "echo", recorder("check-path"))
    _install(monkeypatch, "render", recorder("validate"), render)
    _install(monkeypatch, "plain", plain)
    _install(monkeypatch, "failing", failing)
    _install(monkeypatch, "group", recorder("task.propose"))
    _install(monkeypatch, "human", recorder("review.approve"))
    _install(monkeypatch, "dry", dry)
    _install(monkeypatch, "interrupt", interrupt)
    _install(monkeypatch, "noisy", noisy)
    _install(monkeypatch, "badrender", recorder("metrics", {"value": 1}), bad_render)
    monkeypatch.setitem(sys.modules, f"{_meta.NAME}.{MOD_PREFIX}missing", None)  # no existe
    return record


@dataclass
class Run:
    code: int
    out: str
    err: str


def _run(argv: Sequence[str], *, lang: str = "en", registry: Sequence[OpSpec] | None = FAKE_REGISTRY,
         stdin: str | None = None, env: dict[str, str] | None = None) -> Run:
    out, err = io.StringIO(), io.StringIO()
    environ = {LANG_VAR: lang} if env is None else env
    code = cli.run(list(argv), registry=registry, env=environ, stdout=out, stderr=err,
                   stdin=io.StringIO(stdin) if stdin is not None else io.StringIO(""))
    return Run(code, out.getvalue(), err.getvalue())


@pytest.fixture(autouse=True)
def _outside_any_project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    # El idioma nunca debe leerse de un proyecto real durante estas pruebas.
    monkeypatch.chdir(tmp_path)
    yield


# -- REQ-OPS-15: errores de uso con 3, nunca con 2 ----------------------------------


USAGE_CASES: list[list[str]] = [
    ["nope"],
    ["upgrade"],
    ["task"],
    ["task", "nope"],
    ["--lang"],
    ["--root"],
    ["--lang", "fr", "doctor"],
    ["doctor", "extra"],
    ["doctor", "--bogus"],
    ["--bogus", "doctor"],
    ["check-path"],
    ["check-path", ""],
    ["check-path", "a", "b", "c", "d"],
    ["check-path", "a", "--task"],
    ["check-path", "a", "--task", "x"],
    ["check-path", "a", "--task", "0"],
    ["check-path", "a", "--task", "1_0"],
    ["check-path", "a", "--mode", "z"],
    ["check-path", "a", "--check", "x", "--check", "y", "--check", "z"],
    ["check-path", "a", "--number", "uno"],
    ["check-path", "a", "--changed-only=yes"],
    ["check-path", "a", "--chan"],  # sin abreviaturas
    ["task", "propose"],
    ["validate", "--format", "xml"],
]


@pytest.mark.parametrize("argv", USAGE_CASES, ids=lambda a: " ".join(a) or "<vacío>")
def test_usage_errors_exit_3_never_2(calls: Calls, argv: list[str]) -> None:
    result = _run(argv)
    assert result.code == 3
    assert result.err.splitlines()[0].startswith("error[E_VALIDATION_INPUT]: ")
    assert calls.items == []  # nunca llega a la operación


@pytest.mark.parametrize("argv", [
    ["validate", "--mode", "bogus"],
    ["validate", "--max-chars", "10"],
    ["validate", "--pr", "0"],
    ["check-path"],
    ["sync-roles", "--check", "x"],
    ["doctor", "--refresh", "--refresh=1"],
    ["upgrade"],
    ["report", "ai-use"],
    ["report", "credit"],
    ["section", "read"],
    ["section", "propose-change"],
    ["ci-review-context"],
    ["evidence"],
    ["evidence", "verify"],
    ["evidence", "verify", "a", "b"],
    ["evidence", "verify", "a", "--tz"],
    ["evidence", "export"],  # reservadas por D-50: export, deposit y disclose
    ["evidence", "deposit"],
    ["evidence", "disclose"],
], ids=lambda a: " ".join(a))
def test_real_registry_usage_errors_exit_3(argv: list[str]) -> None:
    # REQ-OPS-41: las reservadas no se registran e invocarlas da E_VALIDATION_INPUT.
    result = _run(argv, registry=None)
    assert result.code == 3
    assert "error[E_VALIDATION_INPUT]: " in result.err


# -- REQ-OPS-68: la pantalla de inicio ---------------------------------------------------


def test_no_command_shows_the_home_screen_outside_a_project(calls: Calls) -> None:
    result = _run([], lang="es", registry=None)
    assert result.code == 0 and result.err == "" and calls.items == []
    lines = result.out.splitlines()
    assert lines[0] == f"{_meta.NAME} {_meta.__version__}"
    assert result.out.isascii() and "No estas dentro de un proyecto" in result.out
    english = _run(["--lang", "en"], registry=None)
    assert english.code == 0 and "You are not inside a project" in english.out


def test_no_command_with_json_is_the_status_envelope(calls: Calls) -> None:
    result = _run(["--json"], registry=None)
    envelope = json.loads(result.out)
    assert envelope["op"] == "status" and envelope["error"]["code"] == "E_CONFIG_NOT_A_PROJECT"
    assert result.code == 8


def test_no_command_still_needs_a_complete_command_path(calls: Calls) -> None:
    for argv in (["evidence"], ["--bogus"]):
        result = _run(argv, registry=None)
        assert result.code == 3 and "error[E_VALIDATION_INPUT]" in result.err


def test_parser_errors_never_raise_systemexit(calls: Calls) -> None:
    parser = cli.build_parser(FAKE_REGISTRY, translator("en"))
    with pytest.raises(Exception) as info:
        parser.parse_args(["check-path", "--task"])
    assert not isinstance(info.value, SystemExit)
    for argv in USAGE_CASES:
        try:
            cli.run(argv, registry=FAKE_REGISTRY, env={LANG_VAR: "en"}, stdout=io.StringIO(), stderr=io.StringIO())
        except SystemExit:  # pragma: no cover - sería el defecto que se prueba
            pytest.fail(f"SystemExit con {argv}")


def test_usage_error_details_are_structured(calls: Calls) -> None:
    result = _run(["--json", "check-path", "a", "--task", "0", "--mode", "z"])
    envelope = json.loads(result.out)
    assert envelope["op"] == "check-path"
    assert envelope["error"]["details"] == {"errors": [
        {"param": "mode", "reason": "enum"}, {"param": "task", "reason": "minimum"}]}
    result = _run(["--json", "check-path"])
    assert json.loads(result.out)["error"]["details"] == {"errors": [{"param": "paths", "reason": "required"}]}
    result = _run(["--json", "check-path", "a", "--number", "1", "--number", "dos"])
    assert json.loads(result.out)["error"]["details"] == {"errors": [{"param": "numbers", "reason": "type", "index": 1}]}


# -- REQ-OPS-9: con --json, exactamente un sobre compacto y un salto de línea ------------


def _assert_one_envelope(text: str) -> dict[str, object]:
    assert text.endswith("\n") and text.count("\n") == 1
    envelope = json.loads(text)
    assert isinstance(envelope, dict)
    assert json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n" == text
    assert list(envelope) == ["schema", "ok", "op", "data", "warnings", "error"]
    assert envelope["schema"] == _meta.RESULT_SCHEMA
    return envelope


def test_json_ok_is_one_compact_envelope(calls: Calls) -> None:
    result = _run(["--json", "check-path", "a"])
    assert result.code == 0 and result.err == ""
    envelope = _assert_one_envelope(result.out)
    assert envelope["ok"] is True and envelope["op"] == "check-path"


def test_json_keeps_stdout_clean_when_the_operation_prints(calls: Calls) -> None:
    result = _run(["context", "--json"])
    assert result.code == 0
    _assert_one_envelope(result.out)
    assert "ruido en stdout" in result.err


def test_json_error_envelope_and_exit_code(calls: Calls) -> None:
    result = _run(["--json", "sync-roles"])
    assert result.code == 7  # FORGE
    envelope = _assert_one_envelope(result.out)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "E_FORGE_RATE_LIMITED"  # type: ignore[index]
    assert result.err == ""


def test_json_usage_error_envelope_validates_against_schema(calls: Calls) -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((REPO_ROOT / "schemas" / "result.schema.json").read_text(encoding="utf-8"))
    result = _run(["--json", "--lang", "es", "check-path", "a", "--task", "x"])
    assert result.code == 3 and result.err == ""
    envelope = _assert_one_envelope(result.out)
    assert envelope["error"]["code"] == "E_VALIDATION_INPUT"  # type: ignore[index]
    assert envelope["error"]["message"] == translator("es")("error.E_VALIDATION_INPUT.message")  # type: ignore[index]
    jsonschema.Draft202012Validator(schema).validate(envelope)


def test_json_goes_to_the_byte_buffer_in_utf8(calls: Calls) -> None:
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="cp1252", newline="\r\n")
    code = cli.run(["--json", "--lang", "es", "check-path", "a", "--task", "x"], registry=FAKE_REGISTRY,
                   env={LANG_VAR: "es"}, stdout=out, stderr=io.StringIO())
    out.flush()
    data = raw.getvalue()
    assert code == 3
    assert not data.startswith(b"\xef\xbb\xbf") and b"\r" not in data and data.endswith(b"}\n")
    assert "válidos".encode("utf-8") in data
    assert json.loads(data.decode("utf-8"))["op"] == "check-path"


def test_unidentified_operation_writes_no_envelope(calls: Calls) -> None:
    # Sin operación no hay un 'op' válido en $defs/op: el error va solo a stderr. (``--json``
    # solo ya no es un error: es el sobre de status de la pantalla de inicio, REQ-OPS-68.)
    for argv in (["--json", "upgrade"], ["--json", "task"]):
        result = _run(argv)
        assert result.code == 3
        assert result.out == ""
        assert result.err.startswith("error[E_VALIDATION_INPUT]: ")


# -- REQ-OPS-41 y --input ---------------------------------------------------------------


def test_input_file_gives_the_mcp_arguments(calls: Calls, tmp_path: Path) -> None:
    source = tmp_path / "args.json"
    source.write_bytes(b"\xef\xbb\xbf" + json.dumps(
        {"paths": ["tex/a.tex"], "task": 2, "checks": ["scope"], "project_root": str(tmp_path)}).encode("utf-8"))
    result = _run(["--input", str(source), "check-path"])
    assert result.code == 0, result.err
    op_id, kwargs, inv = calls.last()
    assert op_id == "check-path"
    assert kwargs == {"paths": ["tex/a.tex"], "task": 2, "checks": ["scope"], "plan_refs": None,
                      "changed_only": False, "mode": "a", "numbers": None}
    assert inv.root == str(tmp_path)


def test_input_from_stdin(calls: Calls) -> None:
    result = _run(["check-path", "--input", "-"], stdin='{"paths": ["x"], "changed_only": true}')
    assert result.code == 0, result.err
    assert calls.last()[1]["paths"] == ["x"] and calls.last()[1]["changed_only"] is True


def test_input_dry_run_and_root_priority(calls: Calls, tmp_path: Path) -> None:
    result = _run(["--root", "otra", "save", "--input", "-"], stdin=json.dumps({"dry_run": True, "project_root": "raiz"}))
    assert result.code == 0, result.err
    inv = calls.last()[2]
    assert inv.dry_run is True and inv.root == "raiz"


@pytest.mark.parametrize(("argv", "stdin", "expected"), [
    (["check-path", "a", "--input", "-"], '{"paths": ["b"]}', {"param": "input", "reason": "exclusive", "with": ["paths"]}),
    (["check-path", "--task", "3", "--input", "-"], '{"paths": ["b"]}',
     {"param": "input", "reason": "exclusive", "with": ["task"]}),
    (["check-path", "--input", "-"], "{no es json", {"param": "input", "reason": "json"}),
    (["check-path", "--input", "-"], '{"paths": [NaN]}', {"param": "input", "reason": "json"}),
    (["check-path", "--input", "-"], '["a"]', {"param": "input", "reason": "type"}),
    (["check-path", "--input", "-"], '{"paths": ["a"], "mode": "b"}', {"param": "mode", "reason": "unknown"}),
    (["check-path", "--input", "-"], '{"paths": ["a"], "dry_run": true}', {"param": "dry_run", "reason": "unknown"}),
    (["check-path", "--input", "-"], '{"task": 1}', {"param": "paths", "reason": "required"}),
    (["check-path", "--input", "-"], '{"paths": []}', {"param": "paths", "reason": "minItems"}),
    (["check-path", "--input", "no-existe.json"], None, {"param": "input", "reason": "unreadable"}),
])
def test_input_errors(calls: Calls, argv: list[str], stdin: str | None, expected: dict[str, object]) -> None:
    result = _run(["--json", *argv], stdin=stdin)
    assert result.code == 3
    envelope = _assert_one_envelope(result.out)
    assert envelope["error"]["code"] == "E_VALIDATION_INPUT"  # type: ignore[index]
    assert envelope["error"]["details"] == {"errors": [expected]}  # type: ignore[index]
    assert calls.items == []


def test_input_too_large(calls: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "MAX_INPUT_BYTES", 10)
    result = _run(["--json", "check-path", "--input", "-"], stdin='{"paths": ["abcdefgh"]}')
    assert json.loads(result.out)["error"]["details"] == {"errors": [{"param": "input", "reason": "too_large"}]}


# -- REQ-OPS-12: flags derivados de los parámetros ------------------------------------


def test_flags_derive_from_params(calls: Calls) -> None:
    result = _run(["check-path", "a", "b", "--task", "3", "--check", "x", "--check", "y", "--plan-ref", "r1",
                   "--plan-ref", "r2", "--changed-only", "--mode", "b", "--number", "4", "--number", "-5"])
    assert result.code == 0, result.err
    assert calls.last()[1] == {"paths": ["a", "b"], "task": 3, "checks": ["x", "y"], "plan_refs": ["r1", "r2"],
                               "changed_only": True, "mode": "b", "numbers": [4, -5]}


def test_defaults_fill_missing_params(calls: Calls) -> None:
    assert _run(["check-path", "a"]).code == 0
    assert calls.last()[1] == {"paths": ["a"], "task": None, "checks": None, "plan_refs": None,
                               "changed_only": False, "mode": "a", "numbers": None}


def test_short_flag_and_comma_separated_lists(calls: Calls) -> None:
    """05 §9.14: ``save -m <mensaje> --ai-use <a,b>`` con los parámetros reales de ``save``."""
    real = next(s for s in REGISTRY if s.id == "save")
    fields = {f: getattr(real, f) for f in real.__slots__}
    spec = OpSpec(**{**fields, "module": DRY.module, "locks": ()})  # sin candados: fuera de un proyecto
    result = _run(["save", "-m", "Texto", "--ai-use", "drafting, editing", "--ai-use", "code", "--path", "a"],
                  registry=(spec,))
    assert result.code == 0, result.err
    assert calls.last()[1] == {"message": "Texto", "ai_use": ["drafting", "editing", "code"], "ai_tool": None,
                               "ai_model": None, "paths": ["a"]}
    assert _run(["save", "--message", "Otro"], registry=(spec,)).code == 0
    assert _run(["save", "--ai-use", "poesía", "-m", "x"], registry=(spec,)).code == 3


def _recording(monkeypatch: pytest.MonkeyPatch, calls: Calls, op_id: str, name: str) -> OpSpec:
    """La operación real ``op_id`` con un módulo que solo registra lo que recibe."""
    def run(ctx: OpContext, **kwargs: object) -> Result:
        calls.items.append((op_id, dict(kwargs), ctx.inv))
        return ok(op_id, {"n": len(calls.items)})

    _install(monkeypatch, name, run)
    real = next(s for s in REGISTRY if s.id == op_id)
    fields = {f: getattr(real, f) for f in real.__slots__}
    return OpSpec(**{**fields, "module": MOD_PREFIX + name, "locks": ()})


def test_record_assessment_takes_n_and_phase_with_input(calls: Calls, monkeypatch: pytest.MonkeyPatch,
                                                         tmp_path: Path) -> None:
    """05 §9.17: ``record-assessment <n> --phase <f> --input <archivo>``; si el JSON los trae, deben coincidir."""
    spec = _recording(monkeypatch, calls, "record-assessment", "ra")
    overlap = {"target": {"task": 12}, "kind": "scope", "reason": "Las dos tocan lo mismo."}
    source = tmp_path / "evaluacion.json"
    source.write_text(json.dumps({"task": 11, "verdict": "possible", "overlaps": [overlap]}), encoding="utf-8")
    result = _run(["record-assessment", "11", "--phase", "claim", "--input", str(source)], registry=(spec,))
    assert result.code == 0, result.err
    got = calls.last()[1]
    assert (got["task"], got["phase"], got["verdict"], got["overlaps"]) == (11, "claim", "possible", [overlap])
    assert _run(["record-assessment", "12", "--phase", "claim", "--input", str(source)], registry=(spec,)).code == 3
    assert _run(["record-assessment", "11", "--phase", "claim"], registry=(spec,)).code == 3  # falta verdict
    # con phase submission, head_sha es obligatorio (el if/then del inputSchema)
    assert _run(["record-assessment", "11", "--phase", "submission", "--input", str(source)], registry=(spec,)).code == 3


def test_submit_reads_files_and_compile_flags(calls: Calls, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """05 §9.18: ``--summary-file``, ``--note-file`` (JSON) y ``--compile | --no-compile``."""
    spec = _recording(monkeypatch, calls, "submit", "sb")
    summary = tmp_path / "resumen.md"
    summary.write_bytes("Resumen\r\nde la entrega.".encode())  # CRLF tal cual (write_text lo duplicaría en Windows)
    note = tmp_path / "nota.json"
    note.write_text(json.dumps({"done": "- Hecho."}), encoding="utf-8")
    args = ["submit", "--summary-file", str(summary), "--note-file", str(note), "--no-compile", "--sign-off"]
    assert _run(args, registry=(spec,)).code == 0
    got = calls.last()[1]
    assert (got["summary"], got["note"], got["compile"], got["sign_off"]) == (
        "Resumen\nde la entrega.", {"done": "- Hecho."}, False, True)
    assert got["summary_file"] is None and got["note_file"] is None
    assert _run(["submit", "--summary", "x", "--compile"], registry=(spec,)).code == 0
    assert calls.last()[1]["compile"] is True
    assert _run(["submit", "--summary", "x"], registry=(spec,)).code == 0
    assert calls.last()[1]["compile"] is None
    both = _run(["submit", "--summary", "x", "--summary-file", str(summary)], registry=(spec,))
    assert both.code == 3


def test_submit_asks_for_the_attestation_on_a_terminal(calls: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    """11-trazabilidad REQ-TRACE-22: sin --sign-off, la terminal pide confirmar attest-v1 (canal tty)."""
    spec = _recording(monkeypatch, calls, "submit", "sb")
    monkeypatch.setattr(cli._Cli, "_tty", lambda self: True)
    accepted = _run(["submit", "--summary", "x"], registry=(spec,), stdin="y\n")
    assert accepted.code == 0 and "I declare that I have personally reviewed" in accepted.out
    assert calls.last()[2].attest_tty is True
    assert _run(["submit", "--summary", "x"], registry=(spec,), stdin="n\n").code == 0
    assert calls.last()[2].attest_tty is False
    flagged = _run(["submit", "--summary", "x", "--sign-off"], registry=(spec,), stdin="")
    assert "I declare" not in flagged.out and calls.last()[2].attest_tty is False
    quiet = _run(["submit", "--json", "--summary", "x"], registry=(spec,), stdin="y\n")
    assert "I declare" not in quiet.out and calls.last()[2].attest_tty is False


def test_a_missing_review_body_is_asked_on_a_terminal(calls: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    """05 §9.24: si falta el cuerpo de request-changes, se pide en la terminal (Param.cli_ask)."""
    real = _recording(monkeypatch, calls, "review.request-changes", "rc")
    spec = dataclasses.replace(real, human_only=False)  # aquí se prueba la pregunta, no la barrera H
    monkeypatch.setattr(cli._Cli, "_tty", lambda self: True)
    asked = _run(["review", "request-changes", "40"], registry=(spec,), stdin="Falta la tabla 2.\n")
    assert asked.code == 0 and "Write the changes you request" in asked.err
    assert calls.last()[1]["body"] == "Falta la tabla 2."
    given = _run(["review", "request-changes", "40", "--body", "Otra cosa."], registry=(spec,), stdin="")
    assert given.code == 0 and "Write the changes" not in given.err and calls.last()[1]["body"] == "Otra cosa."
    quiet = _run(["--json", "review", "request-changes", "40"], registry=(spec,), stdin="no\n")
    assert quiet.code == 0 and calls.last()[1]["body"] is None  # con --json no se pregunta


def test_init_asks_the_language_first(calls: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    """01 REQ-ARCH-71: init pregunta primero el idioma, con un texto bilingüe fijo, y lo usa en sus mensajes."""
    real = _recording(monkeypatch, calls, "init", "init_rec")
    spec = dataclasses.replace(real, human_only=False)  # aquí se prueba la pregunta, no la barrera H
    monkeypatch.setattr(cli._Cli, "_tty", lambda self: True)
    asked = _run(["init"], registry=(spec,), stdin="es\n", env={})
    assert asked.code == 0 and "Idioma del proyecto / Project language" in asked.err
    assert calls.last()[1]["language"] == "es" and calls.last()[2].lang == "es"
    fixed = _run(["init"], registry=(spec,), stdin="es\n", env={LANG_VAR: "en"})
    assert fixed.code == 0 and calls.last()[2].lang is None  # AUCOVE_LANG sigue mandando en los mensajes
    empty = _run(["init"], registry=(spec,), stdin="\n", env={})
    assert empty.code == 0 and calls.last()[1]["language"] is None  # la operación usa el del proyecto
    regenerate = _run(["init", "--regenerate"], registry=(spec,), stdin="es\n", env={})
    assert regenerate.code == 0 and "Project language" not in regenerate.err and calls.last()[1]["language"] is None
    assert _run(["init"], registry=(spec,), stdin="fr\n", env={}).code == 3


def test_init_phrases_and_the_admin_token_never_reach_the_output() -> None:
    """06 REQ-PERM-40 y 10 REQ-SEC-30: la frase se compara sin distinguir mayúsculas; el token no se escribe."""
    err = io.StringIO()
    shell = cli._Cli(REGISTRY, {}, io.StringIO(), err, io.StringIO("Acepto  el perfil ADVISORY\nghp_secret-value\n"))
    assert shell._type_phrase("tabla", "acepto el perfil advisory") is True
    assert shell._ask_secret("Token: ") == "ghp_secret-value"
    assert "ghp_secret" not in err.getvalue() and "acepto el perfil advisory" in err.getvalue()
    wrong = cli._Cli(REGISTRY, {}, io.StringIO(), io.StringIO(), io.StringIO("otra cosa\n"))
    assert wrong._type_phrase("tabla", "acepto el perfil advisory") is False


def test_init_questions_take_the_default_on_an_empty_line() -> None:
    """11-trazabilidad §7.3: las preguntas de las licencias, con su valor por omisión."""
    err = io.StringIO()
    shell = cli._Cli(REGISTRY, {}, io.StringIO(), err, io.StringIO("  GPL-3.0-or-later \n\n"))
    assert shell._ask_line("Licencia de código [MIT]: ", "MIT") == "GPL-3.0-or-later"
    assert shell._ask_line("Licencia de datos [CC0-1.0]: ", "CC0-1.0") == "CC0-1.0"
    assert shell._ask_line("Titular [Ana]: ", "Ana") == "Ana"  # EOF
    assert err.getvalue().count("[") == 3 and not err.getvalue().endswith("\n")


def test_positionals_after_a_flag_are_collected(calls: Calls) -> None:
    assert _run(["check-path", "a", "--task", "3", "b", "c"]).code == 0
    assert calls.last()[1]["paths"] == ["a", "b", "c"] and calls.last()[1]["task"] == 3


def test_cli_flag_names() -> None:
    assert cli.cli_flag(Param("plan_refs", "array")) == "--plan-ref"
    assert cli.cli_flag(Param("checks", "array", cli_flag="--check")) == "--check"
    assert cli.cli_flag(Param("changed_only", "boolean")) == "--changed-only"
    assert cli.cli_flag(Param("max_chars", "integer")) == "--max-chars"
    assert cli.cli_flag(Param("pin_report", "string")) == "--pin-report"


def _options(parser: argparse.ArgumentParser, path: tuple[str, ...]) -> dict[str, argparse.Action]:
    by_path = getattr(parser, "by_path")
    return dict(by_path[path]._option_string_actions)


def test_real_registry_flags() -> None:
    parser = cli.build_parser(REGISTRY, translator("en"))
    validate = _options(parser, ("validate",))
    for flag in ("--mode", "--base", "--head", "--check", "--changed-only", "--policy", "--tree", "--pr",
                 "--before", "--after", "--log", "--report", "--pin-report", "--format", "--cursor", "--max-chars"):
        assert flag in validate, flag
    assert "--checks" not in validate
    assert isinstance(validate["--check"], argparse._AppendAction)
    assert isinstance(_options(parser, ("sync-roles",))["--check"], argparse._StoreTrueAction)
    assert "--refresh" in _options(parser, ("doctor",))
    check_path = _options(parser, ("check-path",))
    assert "--task" in check_path and "--path" not in check_path
    positional = [a for a in getattr(parser, "by_path")[("check-path",)]._actions if not a.option_strings]
    assert [a.metavar for a in positional] == ["PATH"] and positional[0].nargs == "*"
    evidence = _options(parser, ("evidence", "verify"))
    for flag in ("--repo", "--content", "--offline", "--upgrade", "--tz"):
        assert flag in evidence, flag
    assert isinstance(evidence["--offline"], argparse._StoreTrueAction)
    assert isinstance(evidence["--upgrade"], argparse._StoreTrueAction)
    positional = [a for a in getattr(parser, "by_path")[("evidence", "verify")]._actions if not a.option_strings]
    assert [a.metavar for a in positional] == ["PATH"] and positional[0].nargs == "?"
    for path in (("doctor",), ("validate",), ("check-path",), ("sync-roles",), ("evidence", "verify")):
        assert {"--json", "--dry-run", "--lang", "--root", "--input", "--version", "--help"} <= set(_options(parser, path))


def test_boolean_with_true_default_gets_negative_flag() -> None:
    spec = OpSpec(id="doctor", cli=("doctor",), mcp_tool=None, human_only=False, effects="none", offline="local",
                  module="ops.none", params=(Param("fetch", "boolean", default=True),))
    options = _options(cli.build_parser([spec], translator("en")), ("doctor",))
    assert "--fetch" in options and "--no-fetch" in options


@pytest.mark.parametrize("registry", [
    (PLAIN, PLAIN),
    (GROUP, _spec("task", ("task",), "x")),
    (_spec("task", ("task",), "x"), GROUP),
    (_spec("doctor", ("doctor",), "x", params=(Param("project_root", "string"),)),),
    (_spec("doctor", ("doctor",), "x", params=(Param("json", "boolean"),)),),
    (_spec("doctor", ("doctor",), "x", params=(Param("a", "string", cli_flag="--x"), Param("b", "string", cli_flag="--x"))),),
    (_spec("doctor", ("doctor",), "x", params=(Param("Bad", "string"),)),),
    (_spec("doctor", ("Doctor",), "x"),),
    (_spec("doctor", ("doctor",), "x", params=(Param("a", "string", cli_short="-h"),)),),
    (_spec("doctor", ("doctor",), "x", params=(Param("a", "string", cli_short="--a"),)),),
    (_spec("doctor", ("doctor",), "x", params=(Param("a", "string", cli_csv=True),)),),
], ids=["repetida", "grupo-y-op", "op-y-grupo", "reservado", "global", "cli-flag", "nombre", "palabra",
        "corto-global", "corto-largo", "csv-sin-lista"])
def test_build_parser_rejects_bad_registries(registry: tuple[OpSpec, ...]) -> None:
    with pytest.raises(ValueError):
        cli.build_parser(registry, translator("en"))


# -- ayuda y versión -------------------------------------------------------------------


def test_help_lists_only_registered_operations() -> None:
    result = _run(["--help"], registry=None)
    assert result.code == 0 and result.err == ""
    assert ("{doctor,init,sync-roles,validate,status,context,check-path,metrics,evidence,task,claim,release,save,update,"
            "switch,record-assessment,submit,expire-claims,decision,direction,review}") in result.out
    for reserved in ("upgrade", "ai-use", "credit", "propose-change", "review-context", "section"):
        assert reserved not in result.out
    parser = cli.build_parser(REGISTRY, translator("en"))
    # cada operación y los grupos de las de varias palabras (``evidence`` de ``evidence verify``)
    assert set(getattr(parser, "by_path")) == {(), *(spec.cli[:i] for spec in REGISTRY for i in range(1, len(spec.cli) + 1))}


def test_help_is_localized_and_generated(calls: Calls) -> None:
    es = _run(["--lang", "es", "--help"])
    en = _run(["-h"], lang="en")
    assert es.code == en.code == 0
    assert es.out.startswith("uso: ") and "opciones globales" in es.out
    assert en.out.startswith("usage: ") and "global options" in en.out
    # El resumen de check-path sale del catálogo; las operaciones falsas se listan sin texto.
    assert translator("es")("cli.op.check-path.summary").split()[0] in es.out
    for word in ("check-path", "validate", "doctor", "task", "review", "metrics"):
        assert word in es.out
    group = _run(["task", "--help"], lang="es")
    assert group.code == 0 and "propose" in group.out and group.out.startswith("uso: ")


def test_operation_help(calls: Calls) -> None:
    result = _run(["validate", "--help"], registry=None, lang="es")
    assert result.code == 0
    assert result.out.startswith(f"uso: {_meta.NAME} validate ")
    assert "--check CHECK" in result.out and "--changed-only" in result.out and "--pin-report" in result.out
    assert "opciones de la operación" in result.out
    assert calls.items == []


def test_help_language_follows_env_then_flag(calls: Calls) -> None:
    assert _run(["--help"], lang="es").out.startswith("uso: ")
    assert _run(["--help", "--lang", "en"], lang="es").out.startswith("usage: ")


@pytest.mark.parametrize("argv", [["--version"], ["--json", "--version"], ["--lang", "es", "--root", ".", "--version"],
                                  ["doctor", "--version"]])
def test_version(calls: Calls, argv: list[str]) -> None:
    result = _run(argv)
    assert (result.code, result.out, result.err) == (0, _meta.__version__ + "\n", "")
    assert calls.items == []


def test_entry_routes_version_with_globals(capsys: pytest.CaptureFixture[str]) -> None:
    assert entry.main(["--json", "--version"]) == 0
    assert capsys.readouterr().out == _meta.__version__ + "\n"


# -- salida legible y errores en stderr ------------------------------------------------------


def test_errors_go_to_stderr_in_spanish(calls: Calls) -> None:
    result = _run(["--lang", "es", "sync-roles"])
    assert result.code == 7 and result.out == ""
    lines = result.err.splitlines()
    t = translator("es")
    assert lines[0] == "error[E_FORGE_RATE_LIMITED]: " + t("error.E_FORGE_RATE_LIMITED.message")
    assert lines[1] == "sugerencia: " + t("error.E_FORGE_RATE_LIMITED.hint")
    assert lines[2] == 'detalles: {"retry_after_s":30}'


def test_errors_go_to_stderr_in_english(calls: Calls) -> None:
    result = _run(["sync-roles"], lang="en")
    lines = result.err.splitlines()
    assert lines[0].startswith("error[E_FORGE_RATE_LIMITED]: ")
    assert lines[1] == "hint: " + translator("en")("error.E_FORGE_RATE_LIMITED.hint")
    assert lines[2].startswith("details: ")


def test_usage_error_in_human_mode(calls: Calls) -> None:
    result = _run(["check-path", "a", "--task", "x"], lang="es")
    assert result.code == 3 and result.out == ""
    lines = result.err.splitlines()
    assert lines[0].startswith("error[E_VALIDATION_INPUT]: ")
    assert lines[1].startswith("sugerencia: ")
    assert lines[2] == 'detalles: {"errors":[{"param":"task","reason":"type"}]}'
    assert f"`{_meta.NAME} check-path --help`" in lines[3]


def test_render_text_is_used_when_exported(calls: Calls) -> None:
    result = _run(["validate"], lang="es")
    assert (result.code, result.out, result.err) == (0, "rendered:text:es:1\n", "")
    result = _run(["validate", "--format", "github"], lang="en")
    assert result.out == "rendered:github:en:2\n"


def test_generic_output_without_render_text(calls: Calls) -> None:
    result = _run(["doctor"], lang="es")
    assert result.code == 0
    lines = result.out.splitlines()
    assert lines[0] == translator("es")("cli.output.ok", op="doctor")
    assert "  project_root: null" in lines
    assert "  checks:" in lines and "      id: git" in lines and "      status: skip" in lines
    assert "  empty: []" in lines
    # Los controles y los caracteres bidireccionales se escapan (terminal).
    assert "\x1b" not in result.out and "‮" not in result.out
    assert "\\x1b[31m\\u202eoculto" in result.out
    assert result.err == "aviso[W_NET_OFFLINE]: " + translator("es")("warning.W_NET_OFFLINE") + "\n"


def test_failing_render_text_falls_back_to_generic(calls: Calls) -> None:
    result = _run(["metrics"], lang="en")
    assert result.code == 0
    assert result.out.splitlines() == [translator("en")("cli.output.ok", op="metrics"), "  value: 1"]
    assert "secreto" not in result.out + result.err


def test_render_text_gets_every_detail_in_one_line(calls: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    """Un ``detail`` puede citar datos ajenos (la ruta de un registro de evidencia): sus saltos de
    línea llegan escapados al ``render_text`` y no agregan líneas falsas. Los demás textos de
    varias líneas, como un diff, siguen igual, y el sobre no cambia."""
    data: dict[str, object] = {"checks": [{"detail": "a.md\n[pasa] falso\r\n"}], "diff": "-a\n+b\n", "n": 1}

    def run(ctx: OpContext, **kwargs: object) -> Result:
        return ok("validate", data)

    def render(result: Result, t: Translator, *, fmt: str = "text") -> str:
        assert result.data is not None
        return "\n".join([result.data["checks"][0]["detail"], result.data["diff"]])

    _install(monkeypatch, "render", run, render)
    result = _run(["validate"], lang="es")
    assert result.code == 0
    assert result.out.splitlines() == ["a.md\\x0a[pasa] falso\\x0d\\x0a", "-a", "+b"]
    assert data["checks"] == [{"detail": "a.md\n[pasa] falso\r\n"}]
    as_json = _run(["--json", "validate"])
    assert json.loads(as_json.out)["data"]["checks"][0]["detail"] == "a.md\n[pasa] falso\r\n"


def test_dry_run_plan_is_rendered(calls: Calls) -> None:
    result = _run(["--dry-run", "save"], lang="es")
    assert result.code == 0
    assert result.out.splitlines() == [
        translator("es")("cli.output.dry_run", op="save"),
        "  - git_commit task/12: 2 archivos",
        "  - git_push origin task/12: 1 commit",
    ]
    assert calls.last()[2].dry_run is True


def test_read_only_operation_accepts_dry_run(calls: Calls) -> None:
    # REQ-OPS-13: se acepta sin efecto; la CLI solo lo pasa en Invocation.
    assert _run(["check-path", "a", "--dry-run"]).code == 0
    assert calls.last()[2].dry_run is True


# -- invocación, códigos de salida y barrera humana --------------------------------------------


def test_invocation_fields_and_globals_after_the_command(calls: Calls, tmp_path: Path) -> None:
    result = _run(["check-path", "a", "--json", "--lang", "es", "--root", str(tmp_path)], lang="en")
    assert result.code == 0
    inv = calls.last()[2]
    assert (inv.surface, inv.human_confirmed, inv.json, inv.lang, inv.root, inv.dry_run) == (
        "cli", False, True, "es", str(tmp_path), False)
    assert inv.env == {LANG_VAR: "en"}


def test_multiword_operation(calls: Calls) -> None:
    assert _run(["task", "propose", "--title", "Nuevo"]).code == 0
    assert calls.last()[:2] == ("task.propose", {"title": "Nuevo"})


def test_human_only_is_never_confirmed_by_the_cli(calls: Calls) -> None:
    # REQ-ARCH-23: la CLI nunca pone human_confirmed; sin terminal, la fase 1 rechaza con salida 9.
    result = _run(["--json", "review", "approve"], env={LANG_VAR: "en"})
    assert result.code == 9
    envelope = json.loads(result.out)
    assert envelope["error"]["code"].startswith("E_HUMAN_ONLY_")
    assert calls.items == []


def test_the_cli_offers_phase_2_only_to_human_operations() -> None:
    c = cli._Cli(REGISTRY, {LANG_VAR: "es"}, io.StringIO(), io.StringIO(), io.StringIO())
    c.spec = next(s for s in REGISTRY if s.id == "task.accept")
    assert c._context().inv.confirm is not None and c._context().inv.human_confirmed is False
    c.spec = next(s for s in REGISTRY if s.id == "claim")
    assert c._context().inv.confirm is None


def test_phase_2_shows_the_plan_and_reads_one_line() -> None:
    # 06-permisos §6: el resumen y el reto van a stderr; un solo intento; EOF no confirma
    err = io.StringIO()
    c = cli._Cli(REGISTRY, {LANG_VAR: "es"}, io.StringIO(), err, io.StringIO("accept 5\naccept 5\n"))
    proof = c._human_confirm("accept", "5", "Vas a aceptar la tarea #5 «Título\x1b[2J»")
    assert proof.operation == "accept" and proof.confirmed_at.tzinfo is not None
    shown = err.getvalue()
    assert "\\x1b[2J" in shown and "\x1b" not in shown  # el título es texto ajeno
    assert "«accept 5»" in shown
    for typed in ("", "accept 6\n", "aceptar 5\n", "\n"):
        c = cli._Cli(REGISTRY, {LANG_VAR: "es"}, io.StringIO(), io.StringIO(), io.StringIO(typed))
        with pytest.raises(OpError) as exc:
            c._human_confirm("accept", "5", "plan")
        assert exc.value.code == "E_HUMAN_ONLY_NOT_CONFIRMED"


def test_missing_operation_module_is_internal_error(calls: Calls) -> None:
    result = _run(["status"])
    assert result.code == 1
    assert result.err.startswith("error[E_INTERNAL_UNEXPECTED]: ")
    assert "trace_id" in result.err


def test_keyboard_interrupt_exits_130(calls: Calls) -> None:
    result = _run(["release"], lang="es")
    assert result.code == 130
    assert result.err == translator("es")("cli.output.interrupted") + "\n"


def test_wrapper_failure_exits_1_without_the_exception_text(calls: Calls, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(result: Result, t: Translator) -> str:
        raise RuntimeError("secreto")

    monkeypatch.setattr(cli, "render_generic", boom)
    result = _run(["doctor"])
    assert result.code == 1
    assert "error[E_INTERNAL_UNEXPECTED]: " in result.err and "secreto" not in result.err
    json_result = _run(["--json", "doctor"])  # con --json no se usa el render: sale bien
    assert json_result.code == 0


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_windows_streams_are_reconfigured_to_utf8(calls: Calls, monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    err = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    code = cli.run(["doctor"], registry=FAKE_REGISTRY, env={LANG_VAR: "es"}, stdout=out, stderr=err)
    assert code == 0
    expected = "utf-8" if platform == "win32" else "cp1252"
    assert out.encoding == expected and err.encoding == expected


def test_exit_code_zero_for_ok(calls: Calls) -> None:
    assert _run(["check-path", "a"]).code == 0


def test_run_envelope_matches_dumps(calls: Calls) -> None:
    result = _run(["--json", "validate"])
    assert result.out.encode("utf-8") == dumps(ok("validate", {"n": 1}))


# -- catálogos ----------------------------------------------------------------------------------


def test_cli_catalog_covers_the_real_registry() -> None:
    assert check_parity() == []
    keys = {lang: catalog_keys(lang) for lang in ("es", "en")}
    needed = {g.key for g in cli._GLOBALS}
    needed |= {f"cli.help.{k}" for k in ("usage_prefix", "description", "epilog", "commands", "globals", "options")}
    needed |= {f"cli.output.{k}" for k in ("ok", "failed", "dry_run", "hint", "details", "warning", "interrupted",
                                           "see_help")}
    needed |= {"cli.attest.text", "cli.attest.question"}  # confirmación de attest-v1 por TTY (11 REQ-TRACE-22)
    needed.add("cli.confirm.type_exactly")  # el reto de la fase 2 de require_human (06 §6)
    needed |= {f"cli.ask.{s.id}.{p.name}" for s in REGISTRY for p in s.params if p.cli_ask}  # 05 §9.24
    for spec in REGISTRY:
        needed.add(f"cli.op.{spec.id}.summary")
        needed |= {f"cli.op.{spec.id}.param.{p.name}" for p in spec.params if not p.cli_input_only}
        # grupos de las operaciones de varias palabras (``evidence`` de ``evidence verify``)
        needed |= {"cli.group." + ".".join(spec.cli[:i]) + ".summary" for i in range(1, len(spec.cli))}
    for lang in ("es", "en"):
        assert needed <= keys[lang], sorted(needed - keys[lang])
        assert {k for k in keys[lang] if k.startswith("cli.")} == needed
    assert translator("es")("cli.output.hint") == "sugerencia"
    assert translator("en")("cli.output.hint") == "hint"
