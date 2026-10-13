# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Lo que hace el runner de GitHub Actions con un workflow del render, sin shell (00-marco D-37).

La autoprueba lee del render qué corre y con qué valores, en lugar de copiarlo:

- ``evaluate`` evalúa una expresión de Actions (``${{ … }}`` o el texto de un ``if:``):
  literales (``'texto'``, ``true``, ``false``, ``null`` y números), contextos con
  propiedades (``needs.compile.outputs.skipped``, también con guiones, y ``a['b']``), ``!``,
  ``<``, ``<=``, ``>``, ``>=``, ``==``, ``!=``, ``&&``, ``||``, paréntesis y las funciones
  ``always()``, ``success()``, ``failure()``, ``cancelled()`` y ``fromJSON()``. Sigue la
  semántica documentada de Actions: ``&&`` y ``||`` devuelven un operando; la igualdad es
  laxa (tipos distintos se comparan como números) y entre textos no distingue mayúsculas;
  una propiedad que falta es ``null``.
- ``condition`` evalúa un ``if:``: sin función de estado vale ``success() && (…)``.
- ``interpolate`` reemplaza cada ``${{ … }}`` de un valor (el ``env`` de un paso o las
  ``outputs`` de un job) por su texto.
- ``run_steps`` recorre los pasos de un job como el runner: evalúa cada ``if`` con los
  resultados anteriores, deja ``skipped`` los que no corren y llama a ``execute`` con los
  que sí; ``job_outputs`` y ``call_outputs`` arman las salidas del job y de un workflow
  reutilizable.
- ``run_test_script`` ejecuta el subconjunto de bash de un paso como «Veredicto» de
  ``build.yml``: ``if [ … ]; then …; fi``, ``[ … ] || exit N``, ``[ … ] && exit N`` y un
  ``[ … ]`` final, con ``echo`` y ``exit``.

Cualquier otra forma es un error: la prueba no adivina.
"""

from __future__ import annotations

import json
import math
import re
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

STATUS_FUNCTIONS = frozenset({"always", "success", "failure", "cancelled"})
EXPR_RE = re.compile(r"\$\{\{(.*?)\}\}", re.S)
_TOKEN_RE = re.compile(
    r"""\s*(?:
        (?P<num>0x[0-9a-fA-F]+|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
      | (?P<str>'(?:[^']|'')*')
      | (?P<op>==|!=|<=|>=|&&|\|\||[!<>().,\[\]])
      | (?P<name>[A-Za-z_][A-Za-z0-9_-]*)
    )""",
    re.X,
)


class ExpressionError(ValueError):
    """Una expresión que este evaluador no admite: la prueba falla en vez de adivinar."""


# -- valores ------------------------------------------------------------------------------


def _number(value: object) -> float:
    """Conversión a número de la igualdad laxa de Actions."""
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return 0.0
        try:
            return float(int(text, 16)) if text.lower().startswith("0x") else float(json.loads(text))
        except (ValueError, TypeError):
            return math.nan
    return math.nan


def truthy(value: object) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return not (value == 0 or math.isnan(value))
    if isinstance(value, str):
        return value != ""
    return True


def _equal(a: object, b: object) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.casefold() == b.casefold()
    if isinstance(a, (Mapping, list)) or isinstance(b, (Mapping, list)):
        return a is b
    if type(a) is type(b) and not isinstance(a, (int, float)):
        return a == b
    x, y = _number(a), _number(b)
    return not (math.isnan(x) or math.isnan(y)) and x == y


def _compare(op: str, a: object, b: object) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        x: Any = a.casefold()
        y: Any = b.casefold()
    else:
        x, y = _number(a), _number(b)
        if math.isnan(x) or math.isnan(y):
            return False
    return {"<": x < y, "<=": x <= y, ">": x > y, ">=": x >= y}[op]


def text(value: object) -> str:
    """Texto de un valor interpolado en una cadena (``null`` es vacío)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (int, float, str)):
        return str(value)
    return json.dumps(value)


def _property(value: object, key: object) -> object:
    if isinstance(value, Mapping):
        if key in value:
            return value[key]
        if isinstance(key, str):  # los nombres de propiedad no distinguen mayúsculas
            for k, v in value.items():
                if isinstance(k, str) and k.casefold() == key.casefold():
                    return v
        return None
    if isinstance(value, list) and isinstance(key, (int, float)) and not isinstance(key, bool):
        index = int(key)
        return value[index] if 0 <= index < len(value) else None
    return None


# -- análisis y evaluación ---------------------------------------------------------------------


@dataclass
class _Parser:
    tokens: list[tuple[str, str]]
    contexts: Mapping[str, Any]
    status: str  # estado del job para success()/failure(): 'success', 'failure' o 'cancelled'
    pos: int = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self, value: str | None = None) -> tuple[str, str]:
        token = self.peek()
        if token is None or (value is not None and token[1] != value):
            raise ExpressionError(f"se esperaba {value or 'algo'} en la posición {self.pos}")
        self.pos += 1
        return token

    def accept(self, value: str) -> bool:
        token = self.peek()
        if token is not None and token[0] == "op" and token[1] == value:
            self.pos += 1
            return True
        return False

    def parse_or(self) -> object:
        left = self.parse_and()
        while self.accept("||"):
            right = self.parse_and()
            left = left if truthy(left) else right
        return left

    def parse_and(self) -> object:
        left = self.parse_equality()
        while self.accept("&&"):
            right = self.parse_equality()
            left = right if truthy(left) else left
        return left

    def parse_equality(self) -> object:
        left = self.parse_comparison()
        while True:
            if self.accept("=="):
                left = _equal(left, self.parse_comparison())
            elif self.accept("!="):
                left = not _equal(left, self.parse_comparison())
            else:
                return left

    def parse_comparison(self) -> object:
        left = self.parse_unary()
        while True:
            token = self.peek()
            if token is None or token[0] != "op" or token[1] not in ("<", "<=", ">", ">="):
                return left
            self.pos += 1
            left = _compare(token[1], left, self.parse_unary())

    def parse_unary(self) -> object:
        if self.accept("!"):
            return not truthy(self.parse_unary())
        return self.parse_postfix()

    def parse_postfix(self) -> object:
        value = self.parse_primary()
        while True:
            if self.accept("."):
                kind, name = self.take()
                if kind != "name":
                    raise ExpressionError(f"propiedad inválida: {name!r}")
                value = _property(value, name)
            elif self.accept("["):
                key = self.parse_or()
                self.take("]")
                value = _property(value, key)
            else:
                return value

    def parse_primary(self) -> object:
        kind, value = self.take()
        if kind == "num":
            return float(int(value, 16)) if value.lower().startswith("0x") else float(value)
        if kind == "str":
            return value[1:-1].replace("''", "'")
        if kind == "op" and value == "(":
            inner = self.parse_or()
            self.take(")")
            return inner
        if kind != "name":
            raise ExpressionError(f"símbolo inesperado: {value!r}")
        if value in ("true", "false"):
            return value == "true"
        if value == "null":
            return None
        if self.accept("("):
            args: list[object] = []
            if not self.accept(")"):
                args.append(self.parse_or())
                while self.accept(","):
                    args.append(self.parse_or())
                self.take(")")
            return self.call(value, args)
        if value not in self.contexts:
            raise ExpressionError(f"contexto desconocido: {value!r}")
        return self.contexts[value]

    def call(self, name: str, args: Sequence[object]) -> object:
        if name == "always" and not args:
            return True
        if name == "success" and not args:
            return self.status == "success"
        if name == "failure" and not args:
            return self.status == "failure"
        if name == "cancelled" and not args:
            return self.status == "cancelled"
        if name == "fromJSON" and len(args) == 1 and isinstance(args[0], str):
            return json.loads(args[0])
        raise ExpressionError(f"función no admitida: {name}({len(args)} argumentos)")


def _tokens(expr: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(expr):
        if expr[pos:].strip() == "":
            break
        m = _TOKEN_RE.match(expr, pos)
        if m is None or m.end() == pos:
            raise ExpressionError(f"no se entiende {expr[pos:]!r}")
        kind = m.lastgroup
        assert kind is not None
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


def evaluate(expr: str, contexts: Mapping[str, Any], *, status: str = "success") -> object:
    """Valor de una expresión, con o sin ``${{ }}`` alrededor."""
    inner = expr.strip()
    whole = EXPR_RE.fullmatch(inner)
    if whole is not None:
        inner = whole.group(1)
    parser = _Parser(_tokens(inner), contexts, status)
    value = parser.parse_or()
    if parser.peek() is not None:
        raise ExpressionError(f"sobra texto en {expr!r}")
    return value


def condition(expr: object, contexts: Mapping[str, Any], *, status: str = "success") -> bool:
    """Un ``if:``; sin él, o sin función de estado, corre solo si nada falló antes (``success()``)."""
    if expr is None:
        return status == "success"
    if isinstance(expr, bool):
        return expr and status == "success"
    source = str(expr)
    names = {value for kind, value in _tokens(EXPR_RE.sub(r"\1", source)) if kind == "name"}
    value = truthy(evaluate(source, contexts, status=status))
    if names & STATUS_FUNCTIONS:
        return value
    return status == "success" and value


def interpolate(value: object, contexts: Mapping[str, Any]) -> str:
    """Texto de un valor del workflow con cada ``${{ … }}`` reemplazado por su resultado."""
    if not isinstance(value, str):
        return text(value)
    return EXPR_RE.sub(lambda m: text(evaluate(m.group(1), contexts)), value)


# -- pasos y jobs ----------------------------------------------------------------------------


@dataclass
class StepRun:
    """Resultado de un paso: si corrió, su ``outcome`` (antes de ``continue-on-error``) y sus salidas."""

    index: int
    step: Mapping[str, Any]
    ran: bool
    outcome: str  # 'success', 'failure' o 'skipped'
    outputs: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return str(self.step.get("name") or self.step.get("id") or self.step.get("uses") or f"#{self.index}")


Execute = Callable[[int, Mapping[str, Any], Mapping[str, Any]], tuple[str, Mapping[str, str]]]


@dataclass
class JobRun:
    steps: list[StepRun]
    contexts: dict[str, Any]  # con el contexto 'steps' final

    @property
    def result(self) -> str:
        """``needs.<job>.result``: ``failure`` si un paso falló sin ``continue-on-error``."""
        for run in self.steps:
            if run.outcome == "failure" and run.step.get("continue-on-error") is not True:
                return "failure"
        return "success"

    def ran(self) -> list[str]:
        return [run.name for run in self.steps if run.ran]


def run_steps(steps: Sequence[Mapping[str, Any]], contexts: Mapping[str, Any], execute: Execute) -> JobRun:
    """Recorre ``steps`` como el runner; ``execute(i, paso, contextos)`` da ``(outcome, salidas)``."""
    steps_ctx: dict[str, Any] = {}
    runs: list[StepRun] = []
    status = "success"
    for index, step in enumerate(steps):
        ctx = {**contexts, "steps": steps_ctx}
        if condition(step.get("if"), ctx, status=status):
            outcome, outputs = execute(index, step, ctx)
            run = StepRun(index, step, True, outcome, dict(outputs))
        else:
            run = StepRun(index, step, False, "skipped")
        conclusion = "success" if run.outcome == "failure" and step.get("continue-on-error") is True else run.outcome
        if conclusion == "failure":
            status = "failure"
        if "id" in step:
            steps_ctx[str(step["id"])] = {"outcome": run.outcome, "conclusion": conclusion, "outputs": run.outputs}
        runs.append(run)
    return JobRun(runs, {**contexts, "steps": steps_ctx})


def job_outputs(job: Mapping[str, Any], run: JobRun) -> dict[str, str]:
    """``outputs`` de un job, evaluadas con los pasos que corrieron."""
    return {str(k): interpolate(v, run.contexts) for k, v in (job.get("outputs") or {}).items()}


def call_outputs(workflow: Mapping[Any, Any], jobs: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """Salidas de ``on.workflow_call`` de un workflow reutilizable, con ``jobs.<id>.outputs``."""
    on = workflow.get("on", workflow.get(True))
    outputs = ((on or {}).get("workflow_call") or {}).get("outputs") or {}
    return {str(k): interpolate(v["value"], {"jobs": jobs}) for k, v in outputs.items()}


def step_env(step: Mapping[str, Any], contexts: Mapping[str, Any]) -> dict[str, str]:
    """``env`` de un paso con sus expresiones evaluadas."""
    return {str(k): interpolate(v, contexts) for k, v in (step.get("env") or {}).items()}


# -- el subconjunto de bash de «Veredicto» --------------------------------------------------------

_TEST = r"\[\s+(?P<left>\S+)\s+(?P<op>=|!=)\s+(?P<right>\S+)\s+\]"
_IF_RE = re.compile(rf"if\s+{_TEST}\s*;\s*then\s+(?P<body>.+?)\s*;?\s*fi")
_OR_EXIT_RE = re.compile(rf"{_TEST}\s*\|\|\s*exit\s+(?P<code>\d+)")
_AND_EXIT_RE = re.compile(rf"{_TEST}\s*&&\s*exit\s+(?P<code>\d+)")
_BARE_RE = re.compile(_TEST)


@dataclass(frozen=True)
class ScriptRun:
    code: int
    output: list[str]


def _word(raw: str, env: Mapping[str, str]) -> str:
    (word,) = shlex.split(raw)
    m = re.fullmatch(r"\$([A-Za-z_][A-Za-z0-9_]*)", word)
    if m is not None:
        return env.get(m.group(1), "")
    if "$" in word:
        raise ExpressionError(f"expansión no admitida en {raw!r}")
    return word


def _test(m: re.Match[str], env: Mapping[str, str]) -> bool:
    equal = _word(m["left"], env) == _word(m["right"], env)
    return equal if m["op"] == "=" else not equal


def run_test_script(script: str, env: Mapping[str, str]) -> ScriptRun:
    """Código de salida de un script hecho solo de pruebas ``[ "$X" = valor ]`` (ver el docstring)."""
    output: list[str] = []
    lines = [line.strip() for line in script.splitlines() if line.strip()]
    for number, line in enumerate(lines, 1):
        if m := _IF_RE.fullmatch(line):
            if _test(m, env):
                for command in (c.strip() for c in m["body"].split(";") if c.strip()):
                    words = shlex.split(command)
                    if words[0] == "echo":
                        output.append(" ".join(words[1:]))
                    elif words[0] == "exit" and len(words) == 2:
                        return ScriptRun(int(words[1]), output)
                    else:
                        raise ExpressionError(f"orden no admitida: {command!r}")
        elif m := _OR_EXIT_RE.fullmatch(line):
            if not _test(m, env):
                return ScriptRun(int(m["code"]), output)
        elif m := _AND_EXIT_RE.fullmatch(line):
            if _test(m, env):
                return ScriptRun(int(m["code"]), output)
        elif (m := _BARE_RE.fullmatch(line)) and number == len(lines):
            return ScriptRun(0 if _test(m, env) else 1, output)
        else:
            raise ExpressionError(f"línea no admitida: {line!r}")
    return ScriptRun(0, output)
