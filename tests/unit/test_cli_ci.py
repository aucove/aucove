# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Envoltura ``ci`` (00-marco DC-23): salidas 0, 1 y 3, nunca 2, y sin sobre."""

from __future__ import annotations

import argparse
import io
import os
import subprocess
import sys
import types
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import TextIO

import pytest

from aucove import _meta, entry
from aucove.cli import ci as cli_ci
from aucove.cli.ci import CiContext, KNOWN_COMMANDS, load_commands
from aucove.forge.base import RepoRef
from aucove.forge.fake import FakeForge
from aucove.ops import registry
from aucove.ops.context import Invocation, OpContext
from aucove.ops.registry import CiCommand
from aucove.ops.result import OpError

from tests.conftest import REPO_ROOT
from tests.unit.test_ci_build_plan import SHA, project_yaml

BUILD_COMMANDS = ("build-plan", "pin-plan", "comment")
FAKE_PREFIX = "ops._ci_test_"


def run_ci(
    argv: Sequence[str], *, env: dict[str, str] | None = None, modules: Sequence[str] | None = None
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    rc = cli_ci.run(list(argv), env={} if env is None else env, stdout=out, stderr=err, modules=modules)
    return rc, out.getvalue(), err.getvalue()


@pytest.fixture
def fake_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., str]]:
    """Registra un módulo de CI de prueba en ``sys.modules`` y devuelve su nombre relativo."""
    counter = iter(range(1000))

    def make(commands: object) -> str:
        rel = f"{FAKE_PREFIX}{next(counter)}"
        module = types.ModuleType(f"{_meta.NAME}.{rel}")
        module.COMMANDS = commands  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, module.__name__, module)
        return rel

    yield make


def _no_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--flag", action="store_true")


# -- reunión de los COMMANDS --------------------------------------------------------


def test_real_modules_provide_the_ci_commands() -> None:
    commands = load_commands(registry.CI_MODULES)
    assert set(BUILD_COMMANDS) <= set(commands)
    assert set(commands) <= set(KNOWN_COMMANDS)
    assert all(isinstance(c, CiCommand) for c in commands.values())


def test_ci_modules_are_the_contract_ones() -> None:
    assert registry.CI_MODULES == (
        "ops.ci_plan", "ops.ci_comment", "ops.ci_base_report", "ops.ci_seal", "ops.ci_editor_review", "ops.ci_audit",
    )


def test_help_lists_the_available_subcommands() -> None:
    rc, out, _err = run_ci(["--help"])
    assert rc == 0
    for name in BUILD_COMMANDS:
        assert name in out


def test_subcommand_help_is_localized() -> None:
    rc, out, _err = run_ci(["build-plan", "--help"], env={_meta.ENV_PREFIX + "LANG": "es"})
    assert rc == 0
    assert "Directorio con el `project.yaml` de la política" in out
    rc, out, _err = run_ci(["build-plan", "--help"], env={_meta.ENV_PREFIX + "LANG": "en"})
    assert rc == 0
    assert "Directory with the `project.yaml` of the policy" in out


# -- códigos de salida --------------------------------------------------------------


def test_no_subcommand_is_a_usage_error() -> None:
    rc, out, err = run_ci([])
    assert (rc, out) == (3, "")
    assert "usage" in err


@pytest.mark.parametrize(
    "argv",
    [
        ["frobnicate"],
        ["--bogus"],
        ["build-plan", "--bogus"],
        ["build-plan", "--mode"],
        ["pin-plan"],
        ["comment"],
        ["comment", "--kind", "build", "--pr", "x"],
        ["comment", "--kind", "nope", "--pr", "1"],
        ["build-plan", "--mode", "pr", "--policy", "/nonexistent"],  # pr sin --changed: uso
    ],
)
def test_usage_errors_exit_3_never_2(argv: list[str]) -> None:
    rc, out, _err = run_ci(argv)
    assert rc == 3
    assert out == ""


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["x"],
        ["-h"],
        ["--help", "x"],
        ["build-plan", "-h"],
        ["build-plan", "--mode", "base", "--policy", "/nonexistent"],
        ["pin-plan", "--policy", "/nonexistent"],
        ["comment", "--kind", "validate", "--pr", "1", "--report", "/nonexistent"],
        ["audit"],
        ["review-context"],
    ],
)
def test_no_subcommand_ever_exits_2(argv: list[str]) -> None:
    rc, out, _err = run_ci(argv)
    assert rc in (0, 1, 3)
    assert rc != 2
    assert _meta.RESULT_SCHEMA not in out and '"schema"' not in out  # nunca el sobre (DC-23)


@pytest.mark.parametrize("name", ["review-context"])
def test_known_commands_without_module_exit_1(name: str) -> None:
    rc, out, err = run_ci([name, "--anything"])
    assert (rc, out) == (1, "")
    assert "not available in this version" in err
    assert _meta.__version__ in err


def test_missing_ci_module_makes_its_subcommand_exit_1() -> None:
    modules = ("ops.ci_plan", "ops.ci_comment", "ops.ci_does_not_exist")
    rc, out, err = run_ci(["base-report", "--policy", "."], modules=modules)
    assert (rc, out) == (1, "")
    assert "base-report" in err and "not available" in err
    # los demás siguen disponibles
    rc, _out, _err = run_ci(["build-plan", "--help"], modules=modules)
    assert rc == 0


def test_missing_ci_module_makes_its_subcommands_exit_1() -> None:
    modules = ("ops.ci_does_not_exist", "ops.ci_comment")
    for name in ("build-plan", "pin-plan"):
        rc, out, err = run_ci([name, "--policy", "."], modules=modules)
        assert (rc, out) == (1, "")
        assert "no disponible en esta versión" in run_ci([name], env={_meta.ENV_PREFIX + "LANG": "es"}, modules=modules)[2]


def test_module_without_valid_commands_is_unavailable(fake_module: Callable[..., str]) -> None:
    broken = fake_module(("not a CiCommand",))
    rc, _out, err = run_ci(["comment"], modules=(broken,))
    assert rc == 1 and "not available" in err
    missing_attr = fake_module(None)
    assert run_ci(["comment"], modules=(missing_attr,))[0] == 1


def test_duplicated_names_fail_closed(fake_module: Callable[..., str]) -> None:
    cmd = CiCommand("comment", _no_args, lambda args, ctx, out: 0)
    a, b = fake_module((cmd,)), fake_module((cmd,))
    assert run_ci(["comment"], modules=(a,))[0] == 0
    assert run_ci(["comment"], modules=(a, b))[0] == 1


@pytest.mark.parametrize(
    "result",
    [2, 4, -1, True, None, "0"],
)
def test_unexpected_return_values_become_1(fake_module: Callable[..., str], result: object) -> None:
    rel = fake_module((CiCommand("comment", _no_args, lambda args, ctx, out: result),))
    assert run_ci(["comment"], modules=(rel,))[0] == 1


@pytest.mark.parametrize("exc", [RuntimeError("secreto del PR"), SystemExit(2), KeyboardInterrupt()])
def test_exceptions_become_1_without_their_text(fake_module: Callable[..., str], exc: BaseException) -> None:
    def boom(args: argparse.Namespace, ctx: OpContext, out: TextIO) -> int:
        raise exc

    rel = fake_module((CiCommand("comment", _no_args, boom),))
    rc, out, err = run_ci(["comment"], modules=(rel,))
    assert (rc, out) == (1, "")
    assert "secreto" not in err


def test_context_is_ci_and_always_closed(fake_module: Callable[..., str]) -> None:
    seen: list[OpContext] = []

    def main(args: argparse.Namespace, ctx: OpContext, out: TextIO) -> int:
        seen.append(ctx)
        out.write("k=v\n")
        return 0 if args.flag else 3

    rel = fake_module((CiCommand("comment", _no_args, main),))
    rc, out, _err = run_ci(["comment", "--flag"], modules=(rel,))
    assert (rc, out) == (0, "k=v\n")
    rc, _out, _err = run_ci(["comment"], modules=(rel,))
    assert rc == 3
    assert [c.inv.surface for c in seen] == ["ci", "ci"]
    assert all(c._closed for c in seen)  # close() siempre, también con salida 3


def test_close_failure_exits_1(fake_module: Callable[..., str]) -> None:
    def main(args: argparse.Namespace, ctx: OpContext, out: TextIO) -> int:
        def fail() -> None:
            raise OSError("disco lleno")

        ctx.close = fail  # type: ignore[method-assign]
        return 0

    rel = fake_module((CiCommand("comment", _no_args, main),))
    assert run_ci(["comment"], modules=(rel,))[0] == 1


def test_stderr_of_the_subcommand_goes_to_the_given_stream(fake_module: Callable[..., str]) -> None:
    def main(args: argparse.Namespace, ctx: OpContext, out: TextIO) -> int:
        print("mensaje", file=sys.stderr)
        return 0

    rel = fake_module((CiCommand("comment", _no_args, main),))
    rc, out, err = run_ci(["comment"], modules=(rel,))
    assert (rc, out) == (0, "")
    assert "mensaje" in err


# -- contexto de CI -----------------------------------------------------------------


def test_ci_context_does_not_resolve_the_project_from_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "project.yaml").write_text(project_yaml(), encoding="utf-8")
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    inv = Invocation(root=None, lang=None, json=False, dry_run=False, surface="ci", env={}, cwd=str(tmp_path))
    assert OpContext(inv).project(required=False) is not None  # el contexto normal sí lo encontraría
    ctx = CiContext(inv)
    assert ctx.project(required=False) is None
    with pytest.raises(OpError):
        ctx.project()
    assert ctx.t().lang == "en"
    rooted = CiContext(Invocation(root=str(tmp_path), lang=None, json=False, dry_run=False, surface="ci", env={}))
    assert rooted.project(required=False) is not None


# -- punto de entrada y subproceso --------------------------------------------------


def test_entry_dispatches_ci(capsys: pytest.CaptureFixture[str]) -> None:
    assert entry.main(["ci", "--help"]) == 0
    assert "build-plan" in capsys.readouterr().out
    assert entry.main(["ci", "frobnicate"]) == 3


def _subprocess_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(_meta.ENV_PREFIX)}
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env.pop("GITHUB_SHA", None)
    env.update(extra)
    return env


def _run_process(argv: Sequence[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[bytes]:
    cmd = [sys.executable, "-m", f"{_meta.NAME}.entry", "ci", *argv]
    return subprocess.run(cmd, env=env, cwd=cwd, capture_output=True, timeout=120, check=False)


def test_build_plan_in_a_subprocess(tmp_path: Path) -> None:
    policy = tmp_path / "policy"
    policy.mkdir()
    (policy / "project.yaml").write_text(project_yaml(), encoding="utf-8", newline="\n")
    proc = _run_process(["build-plan", "--mode", "base", "--policy", str(policy)], _subprocess_env(GITHUB_SHA=SHA), tmp_path)
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.replace(b"\r\n", b"\n").decode("ascii").splitlines()
    assert [line.split("=", 1)[0] for line in lines] == list(cli_ci_keys())
    proc = _run_process(["build-plan", "--mode", "nope"], _subprocess_env(), tmp_path)
    assert proc.returncode == 3 and proc.stdout == b""


def cli_ci_keys() -> tuple[str, ...]:
    from aucove.ops.ci_plan import BUILD_PLAN_KEYS

    return BUILD_PLAN_KEYS


def test_comment_in_a_subprocess_saves_the_fake_state(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    forge = FakeForge(RepoRef("example-org", "demo"), viewer="github-actions[bot]")
    forge.seed_pull_request(files=[], number=7, head_branch="task/3", head_sha="b" * 40, author="carla")
    forge.save(state)
    p = _meta.ENV_PREFIX
    env = _subprocess_env(**{f"{p}TESTING": "1", f"{p}FORGE_ADAPTER": "fake", f"{p}FORGE_FAKE_STATE": str(state)})
    argv = ["comment", "--kind", "validate", "--pr", "7", "--report", str(tmp_path / "missing.json")]
    for _ in range(2):
        proc = _run_process(argv, env, tmp_path)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == b""
    comments = FakeForge.load(state).list_comments(7)
    assert len(comments) == 1
    assert comments[0].body.startswith(_meta.ci_marker("validate") + "\n")
    assert "Validation not available" in comments[0].body
