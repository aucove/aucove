# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Pasos y veredictos de los workflows de la plantilla (09-ci).

- PR desde forks (REQ-CI-20): el paso «Comentario localizado» de ``build.yml`` excluía
  los forks, así que nada escribía el resumen del job con el enlace al PDF. ``ci comment``
  escribe ``--summary`` antes de publicar y, con el token de solo lectura, termina en 0.
- ``aucove/validate`` (REQ-CI-11 y REQ-CI-8): el veredicto solo miraba la salida de
  ``aucove validate``; si ``pin`` fallaba antes de su paso ``plan``, ``validate`` evaluaba
  sin ``--pin-report`` y podía quedar en verde.
- latexdiff (REQ-CI-23): su timeout fijo de 8 minutos podía agotar el del job (15) y
  dejar ``aucove/build`` en rojo por un paso opcional; ahora usa lo que queda del job.
- ``astral-sh/setup-uv`` (REQ-CI-2, REQ-CI-12): sin ``working-directory`` busca la versión
  de uv en ``uv.toml`` o ``pyproject.toml`` del workspace, que es el árbol del PR.

Para decidir si un paso corre se evalúan sus ``if:`` con un evaluador mínimo de
expresiones de GitHub Actions (literales, contextos, ``!``, ``==``, ``!=``, ``&&``,
``||``, paréntesis y las funciones de estado). Los veredictos se interpretan solo si
son una lista de ``test "$VAR" = valor``, que es la forma que fija el workflow.
"""

from __future__ import annotations

import io
import json
import math
import re
import shlex
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from aucove import _meta
from aucove.cli import ci as cli_ci
from aucove.forge.base import RepoRef
from aucove.forge.fake import Fault, FakeForge
from aucove.ops.ci_comment import BOT_LOGIN, DIAGNOSTICS_FILE
from tests.conftest import FIXTURES
from tests.render.test_template_workflows import WORKFLOW_FILES, action_of, jobs, steps_of

# ---------------------------------------------------------------------------
# Evaluador mínimo de expresiones de GitHub Actions
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<num>\d+(?:\.\d+)?)|(?P<op>==|!=|<=|>=|&&|\|\||[!()<>,])"
    r"|(?P<ident>[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*))"
)
STATUS_FUNCTIONS = ("always", "success", "failure", "cancelled")


def _tokens(expr: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    expr = expr.strip()
    while pos < len(expr):
        m = _TOKEN_RE.match(expr, pos)
        assert m and m.end() > pos, f"expresión no admitida: {expr[pos:]!r}"
        kind = m.lastgroup
        assert kind is not None
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


def _number(value: object) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return math.nan


def _equal(a: object, b: object) -> bool:
    """Igualdad laxa de GitHub: cadenas sin distinguir mayúsculas; si no, como números."""
    if isinstance(a, str) and isinstance(b, str):
        return a.casefold() == b.casefold()
    if type(a) is type(b):
        return a == b
    return _number(a) == _number(b)


def truthy(value: object) -> bool:
    if value is None or value is False or value == "":
        return False
    if isinstance(value, int | float) and not isinstance(value, bool):
        return value != 0 and not math.isnan(value)
    return True


@dataclass
class Evaluator:
    context: Mapping[str, Any]
    success: bool = True  # estado de los pasos anteriores del job

    def evaluate(self, expr: str) -> object:
        self._toks = _tokens(expr)
        self._pos = 0
        value = self._or()
        assert self._pos == len(self._toks), f"sobra {self._toks[self._pos:]} en {expr!r}"
        return value

    def _peek(self) -> tuple[str, str] | None:
        return self._toks[self._pos] if self._pos < len(self._toks) else None

    def _take(self, text: str | None = None) -> tuple[str, str]:
        tok = self._peek()
        assert tok is not None and (text is None or tok[1] == text), (tok, text)
        self._pos += 1
        return tok

    def _or(self) -> object:
        left = self._and()
        while self._peek() == ("op", "||"):
            self._take()
            right = self._and()
            left = left if truthy(left) else right
        return left

    def _and(self) -> object:
        left = self._equality()
        while self._peek() == ("op", "&&"):
            self._take()
            right = self._equality()
            left = right if truthy(left) else left
        return left

    def _equality(self) -> object:
        left = self._comparison()
        while self._peek() in (("op", "=="), ("op", "!=")):
            op = self._take()[1]
            right = self._comparison()
            left = _equal(left, right) if op == "==" else not _equal(left, right)
        return left

    def _comparison(self) -> object:
        left = self._unary()
        while (tok := self._peek()) is not None and tok[0] == "op" and tok[1] in ("<", "<=", ">", ">="):
            op = self._take()[1]
            a, b = _number(left), _number(self._unary())
            left = {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]
        return left

    def _unary(self) -> object:
        if self._peek() == ("op", "!"):
            self._take()
            return not truthy(self._unary())
        return self._primary()

    def _primary(self) -> object:
        kind, text = self._take()
        if kind == "str":
            return text[1:-1].replace("''", "'")
        if kind == "num":
            return float(text)
        if kind == "op" and text == "(":
            value = self._or()
            self._take(")")
            return value
        assert kind == "ident", (kind, text)
        if self._peek() == ("op", "("):
            return self._call(text)
        if text in ("true", "false"):
            return text == "true"
        if text == "null":
            return None
        node: object = self.context
        for part in text.split("."):
            node = node.get(part) if isinstance(node, Mapping) else None
        return node

    def _call(self, name: str) -> object:
        self._take("(")
        args: list[object] = []
        while self._peek() != ("op", ")"):
            args.append(self._or())
            if self._peek() == ("op", ","):
                self._take()
        self._take(")")
        if name == "always":
            return True
        if name == "success":
            return self.success
        if name == "failure":
            return not self.success
        if name == "cancelled":
            return False
        if name == "fromJSON":
            return json.loads(str(args[0]))
        raise AssertionError(f"función no admitida: {name}")

    def runs(self, step_or_job: Mapping[str, Any]) -> bool:
        """``if:`` de un paso o job; sin función de estado, GitHub antepone ``success() &&``."""
        cond = step_or_job.get("if")
        if cond is None:
            return self.success
        cond = str(cond).strip()
        if cond.startswith("${{") and cond.endswith("}}"):
            cond = cond[3:-2]
        if not any(re.search(rf"\b{f}\s*\(", cond) for f in STATUS_FUNCTIONS):
            return self.success and truthy(self.evaluate(cond))
        return truthy(self.evaluate(cond))

    def interpolate(self, value: object) -> str:
        """Valor de un ``env:``: cada ``${{ … }}`` se reemplaza por su resultado como texto."""

        def text(m: re.Match[str]) -> str:
            result = self.evaluate(m.group(1))
            if result is None:
                return ""
            if isinstance(result, bool):
                return "true" if result else "false"
            if isinstance(result, float) and result.is_integer():
                return str(int(result))
            return str(result)

        return re.sub(r"\$\{\{(.*?)\}\}", text, str(value))


def step_named(workflow: str, job_id: str, prefix: str) -> dict[str, Any]:
    found = [s for s in steps_of(jobs(workflow)[job_id]) if str(s.get("name", "")).startswith(prefix)]
    assert len(found) == 1, (workflow, job_id, prefix, found)
    return found[0]


def step_env(ev: Evaluator, step: Mapping[str, Any]) -> dict[str, str]:
    return {str(k): ev.interpolate(v) for k, v in (step.get("env") or {}).items()}


TEST_LINE_RE = re.compile(r'^test "\$([A-Z_][A-Z0-9_]*)" = (?:"([^"$`\\]*)"|([^\s"$`\\]+))$')


def verdict_passes(run: str, env: Mapping[str, str]) -> bool:
    """Interpreta un veredicto hecho solo de ``test "$VAR" = valor`` (shell ``bash -e``)."""
    checks: list[tuple[str, str]] = []
    for raw in run.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = TEST_LINE_RE.match(line)
        assert m, f"línea de veredicto no admitida: {raw!r}"
        checks.append((m.group(1), m.group(2) if m.group(2) is not None else m.group(3)))
    assert checks
    return all(env.get(var, "") == value for var, value in checks)


# ---------------------------------------------------------------------------
# PR desde forks: el resumen del job con el enlace al PDF (REQ-CI-20)
# ---------------------------------------------------------------------------

REPO = RepoRef("example-org", "demo")
PR = 42
PDF_URL = "https://github.com/example-org/demo/actions/runs/7/artifacts/11"


def build_context(*, fork: bool, compile_result: str = "success", draft: bool = False) -> dict[str, Any]:
    head_repo = "someone/demo" if fork else "example-org/demo"
    return {
        "github": {
            "repository": "example-org/demo",
            "server_url": "https://github.com",
            "run_id": "7",
            "event": {"pull_request": {"number": PR, "draft": draft, "head": {"repo": {"full_name": head_repo}}}},
        },
        "needs": {
            "compile": {
                "result": compile_result,
                "outputs": {
                    "outcome": "success" if compile_result == "success" else "",
                    "skipped": "false",
                    "pdf_url": PDF_URL if compile_result == "success" else "",
                    "diff_url": "",
                    "language": "es",
                },
            }
        },
    }


def comment_step() -> dict[str, Any]:
    return step_named("build.yml", "report", "Comentario localizado")


@pytest.mark.parametrize("fork", [False, True], ids=["mismo-repo", "fork"])
@pytest.mark.parametrize("compile_result", ["success", "failure", "cancelled"])
def test_build_comment_step_runs_for_forks_too(fork: bool, compile_result: str) -> None:
    ev = Evaluator(build_context(fork=fork, compile_result=compile_result))
    assert ev.runs(jobs("build.yml")["report"])
    assert ev.runs(comment_step()), "el resumen del job es el único lugar del enlace en un fork (REQ-CI-20)"


def test_build_comment_step_is_skipped_only_for_drafts() -> None:
    ev = Evaluator(build_context(fork=True, compile_result="skipped", draft=True))
    assert not ev.runs(comment_step())


def test_fork_pr_gets_the_pdf_link_in_the_job_summary(tmp_path: Path) -> None:
    """El paso, tal como está escrito, con el token de solo lectura de un fork."""
    step = comment_step()
    ev = Evaluator(build_context(fork=True))
    assert ev.runs(step)
    state = tmp_path / "fake-state.json"
    forge = FakeForge(REPO, viewer=BOT_LOGIN)
    forge.seed_pull_request(files=[], number=PR, head_branch="task/17", head_sha="a" * 40, author="carla")
    forge.faults.add(Fault(method="create_comment", kind="http_403"))  # token de solo lectura
    forge.save(state)
    runner_temp = tmp_path / "runner-temp"
    (runner_temp / "reports").mkdir(parents=True)
    shutil.copyfile(FIXTURES / "diagnostics" / "build-report.sample.json", runner_temp / "reports" / DIAGNOSTICS_FILE)
    summary = tmp_path / "summary.md"
    variables = {**step_env(ev, step), "RUNNER_TEMP": str(runner_temp), "GITHUB_STEP_SUMMARY": str(summary)}
    words = [
        re.sub(r"\$\{?([A-Z_][A-Z0-9_]*)\}?", lambda m: variables[m.group(1)], word)
        for word in shlex.split(str(step["run"]))
    ]
    assert words[:3] == [_meta.NAME, "ci", "comment"], words
    p = _meta.ENV_PREFIX
    env = {f"{p}TESTING": "1", f"{p}FORGE_ADAPTER": "fake", f"{p}FORGE_FAKE_STATE": str(state)}
    out, err = io.StringIO(), io.StringIO()
    rc = cli_ci.run(words[2:], env=env, stdout=out, stderr=err)
    assert rc == 0, err.getvalue()
    text = summary.read_text(encoding="utf-8")
    assert PDF_URL in text
    assert FakeForge.load(state).list_comments(PR) == []


# ---------------------------------------------------------------------------
# Veredicto de aucove/validate con needs.pin.result (REQ-CI-11, REQ-CI-8)
# ---------------------------------------------------------------------------


def validate_verdict(pin_result: str, exit_code: str) -> bool:
    context = {"needs": {"pin": {"result": pin_result, "outputs": {"ran": ""}}},
               "steps": {"v": {"outputs": {"exit": exit_code}}}}
    ev = Evaluator(context, success=False)  # el veredicto corre aunque algo anterior falle
    step = step_named("validate.yml", "validate", "Veredicto")
    assert ev.runs(step)
    return verdict_passes(str(step["run"]), step_env(ev, step))


@pytest.mark.parametrize("pin_result", ["failure", "cancelled", "skipped", ""])
def test_validate_verdict_fails_when_pin_did_not_succeed(pin_result: str) -> None:
    assert not validate_verdict(pin_result, "0")


def test_validate_verdict_passes_only_with_pin_success_and_exit_0() -> None:
    assert validate_verdict("success", "0")
    for exit_code in ("1", "2", ""):
        assert not validate_verdict("success", exit_code)


def test_validate_job_still_runs_when_pin_fails() -> None:
    assert Evaluator({"needs": {"pin": {"result": "failure"}}}, success=False).runs(jobs("validate.yml")["validate"])


# ---------------------------------------------------------------------------
# latexdiff no puede agotar el timeout del job de compilación (REQ-CI-23)
# ---------------------------------------------------------------------------

LATEXDIFF_IF = "steps.plan.outputs.latexdiff == 'true' && inputs.mode == 'pr'"
BUDGET_LINE = "LEFT=$(( (START + (JOB_MINUTES - RESERVE_MINUTES) * 60 - $(date +%s)) / 60 ))"


def compile_steps() -> list[dict[str, Any]]:
    return steps_of(jobs("compile.yml")["compile"])


def test_latexdiff_timeout_comes_from_what_is_left_of_the_job() -> None:
    job = jobs("compile.yml")["compile"]
    steps = compile_steps()
    ids = [s.get("id") for s in steps]
    budget, diff = steps[ids.index("budget")], steps[ids.index("diff")]
    assert ids.index("budget") + 1 == ids.index("diff")  # el presupuesto se mide justo antes
    assert all(action_of(s) == "actions/upload-artifact" for s in steps[ids.index("diff") + 1:])
    # El presupuesto no puede cambiar el veredicto: si falla, latexdiff no corre.
    assert budget.get("if") == LATEXDIFF_IF and budget.get("continue-on-error") is True
    assert diff.get("if") == "steps.budget.outputs.run == 'true'" and diff.get("continue-on-error") is True
    assert diff["timeout-minutes"] == "${{ fromJSON(steps.budget.outputs.minutes || '1') }}"
    env = budget["env"]
    assert env["JOB_MINUTES"] == str(job["timeout-minutes"])
    assert 1 <= int(env["RESERVE_MINUTES"]) < int(env["JOB_MINUTES"])
    assert env["MAX_MINUTES"] == "8"  # el tope de 09-ci §3.2 sigue valiendo si sobra tiempo
    # START se toma en el primer paso con run:, antes solo está el checkout.
    first_run = next(i for i, s in enumerate(steps) if "run" in s)
    assert all(action_of(s) == "actions/checkout" for s in steps[:first_run])
    assert steps[first_run].get("id") == "policy"
    assert env["START"] == "${{ steps.policy.outputs.start }}"
    assert 'echo "start=$(date +%s)" >> "$GITHUB_OUTPUT"' in str(steps[first_run]["run"])
    run = str(budget["run"])
    assert BUDGET_LINE in run
    assert 'if [ "$LEFT" -gt "$MAX_MINUTES" ]; then LEFT="$MAX_MINUTES"; fi' in run
    assert 'if [ "$LEFT" -ge 1 ]; then' in run
    assert run.index('if [ "$LEFT" -ge 1 ]; then') < run.index('echo "run=true" >> "$GITHUB_OUTPUT"')


def test_latexdiff_ends_before_the_job_timeout() -> None:
    """Modelo de la línea del presupuesto: el paso termina antes del timeout menos la reserva."""
    env = step_named("compile.yml", "compile", "Presupuesto de latexdiff")["env"]
    job_s = int(env["JOB_MINUTES"]) * 60
    reserve_s = int(env["RESERVE_MINUTES"]) * 60
    ran = 0
    for elapsed in range(job_s + 60):  # segundos desde START hasta medir el presupuesto
        left = int((job_s - reserve_s - elapsed) / 60)  # la división de bash trunca hacia cero
        minutes = min(left, int(env["MAX_MINUTES"]))
        if minutes >= 1:
            ran += 1
            assert elapsed + minutes * 60 <= job_s - reserve_s, elapsed
    assert ran  # con tiempo de sobra, latexdiff corre


# ---------------------------------------------------------------------------
# setup-uv no lee la versión de uv del árbol del PR (REQ-CI-2, REQ-CI-12)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", WORKFLOW_FILES)
def test_setup_uv_never_looks_at_the_checkout(name: str) -> None:
    found = 0
    for job_id, job in jobs(name).items():
        steps = steps_of(job)
        for index, step in enumerate(steps):
            if action_of(step) != "astral-sh/setup-uv":
                continue
            found += 1
            with_ = step.get("with") or {}
            assert with_.get("working-directory") == "${{ runner.temp }}", f"{name}:{job_id}"
            assert "version-file" not in with_, f"{name}:{job_id}"
            for before in steps[:index]:  # nada deja un uv.toml o un pyproject.toml en $RUNNER_TEMP
                text = json.dumps(before)
                assert "uv.toml" not in text and "pyproject.toml" not in text, f"{name}:{job_id}"
    assert found >= 1
