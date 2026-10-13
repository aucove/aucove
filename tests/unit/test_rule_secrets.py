# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Regla ``secrets`` con el doble de gitleaks (09-ci §4.1; 10-seguridad REQ-SEC-36; REQ-CI-8)."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aucove import _meta
from aucove.config.datafiles import gitleaks_config
from aucove.domain.findings import Finding
from aucove.i18n import Translator, translator
from aucove.ops import validate
from aucove.ops.rules import secrets
from aucove.ops.rules.base import RuleInput
from aucove.vcs.tree import git_tree
from tests.conftest import FIXTURES, GitRepo

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
FAKE = FIXTURES / "tools" / "fake_gitleaks.py"
TOOL = (sys.executable, str(FAKE))
# El patrón del doble se arma por partes para que ningún escáner real lo vea en este archivo.
LEAK = "FAKE" + "LEAK" + "0123456789012345"


@pytest.fixture(autouse=True)
def _clean_fake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("FAKE_GITLEAKS_LOG", "FAKE_GITLEAKS_EXIT", "FAKE_GITLEAKS_REPORT", "FAKE_GITLEAKS_SLEEP",
                 "GITLEAKS_CONFIG", "GITLEAKS_CONFIG_TOML"):
        monkeypatch.delenv(name, raising=False)
    # Las GITLEAKS_* del entorno (el CI del framework define GITLEAKS_VERSION y GITLEAKS_SHA256
    # para instalarlo) llegarían al doble, que registra todas: la prueba no depende de ellas.
    for name in [n for n in os.environ if n.upper().startswith("GITLEAKS")]:
        monkeypatch.delenv(name, raising=False)


class Scenario:
    def __init__(self, repo: GitRepo, merge_base: str, head: str) -> None:
        self.repo = repo
        self.merge_base = merge_base
        self.head = head

    def input(self, *, tools: dict[str, tuple[str, ...]] | None = None, t: Translator | None = None,
              head_sha: str | None = "default", base: bool = True) -> RuleInput:
        return RuleInput(
            mode="pr",
            t=t or translator("en"),
            now=NOW,
            tree=git_tree(self.repo.path, self.head),
            base_tree=git_tree(self.repo.path, self.merge_base) if base else None,
            head_sha=self.head if head_sha == "default" else head_sha,
            tools={"gitleaks": TOOL} if tools is None else tools,
        )


def scenario(git_repo: GitRepo, *, leak: bool) -> Scenario:
    # un secreto en la base, fuera del rango del PR, nunca se informa
    merge_base = git_repo.commit("base", files={"project.yaml": "name: x\n", "old.txt": f"token {LEAK}\n"})
    git_repo.git("checkout", "-q", "-b", "task/3")
    git_repo.commit("c1", files={"tex/a.tex": "% texto\n"})
    content = f"% uno\nclave = {LEAK}\n" if leak else "% uno\n% dos\n"
    head = git_repo.commit("c2", files={"tex/b.tex": content})
    return Scenario(git_repo, merge_base, head)


def run(inp: RuleInput) -> list[Finding]:
    out = list(secrets.RULE.run(inp))
    assert all(f.check == "secrets" and f.severity == "error" for f in out)
    return out


def reason(key: str, **kw: object) -> str:
    return translator("en")(f"validate.secrets.{key}", **kw)


def test_declaration() -> None:
    assert secrets.RULE.id == "secrets" and secrets.RULE.modes == frozenset({"pr"})
    assert secrets.RULE.tools == ("gitleaks",)
    assert dict(secrets.RULE.codes) == {"E_VALIDATION_SECRET": frozenset({"rule_id", "commit"})}


def test_no_leaks_pass(git_repo: GitRepo) -> None:
    assert run(scenario(git_repo, leak=False).input()) == []


def test_leak_is_reported_without_the_secret(git_repo: GitRepo) -> None:
    sc = scenario(git_repo, leak=True)
    (finding,) = run(sc.input())
    assert finding.code == "E_VALIDATION_SECRET"
    assert finding.path == "tex/b.tex" and finding.line == 2
    assert "fake-leak" in finding.message and sc.head[:12] in finding.message
    assert LEAK not in json.dumps(finding.to_dict())


def test_argv_uses_packaged_config_redact_and_the_pr_range(git_repo: GitRepo, tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    sc = scenario(git_repo, leak=False)
    (git_repo.path / ".gitleaks.toml").write_text("[allowlist]\npaths = ['.*']\n", encoding="utf-8")
    log = tmp_path / "call.json"
    monkeypatch.setenv("FAKE_GITLEAKS_LOG", str(log))
    monkeypatch.setenv("GITLEAKS_CONFIG", str(git_repo.path / ".gitleaks.toml"))
    assert run(sc.input()) == []
    call = json.loads(log.read_text(encoding="utf-8"))
    argv = call["argv"]
    assert argv[0] == "git"
    assert "--redact" in argv and "--ignore-gitleaks-allow" in argv and "--no-banner" in argv
    assert argv[argv.index("--config") + 1] == str(gitleaks_config())
    # diff textual de todo archivo: un atributo del PR no oculta contenido (REQ-SEC-36), y
    # diff de las fusiones contra la rama de la tarea
    assert (f"--log-opts=--text --no-textconv --no-ext-diff --diff-merges=first-parent "
            f"{sc.merge_base}..{sc.head}") in argv
    assert argv[argv.index("--report-format") + 1] == "json"
    assert argv[argv.index("--exit-code") + 1] == str(secrets.LEAKS_EXIT_CODE)
    assert Path(argv[-1]) == git_repo.path
    cwd = Path(call["cwd"]).resolve()
    assert not cwd.is_relative_to(git_repo.path.resolve())  # el cwd queda fuera del árbol
    assert call["env"] == {}  # GITLEAKS_CONFIG no llega a gitleaks


def test_gitleaks_argv_shape() -> None:
    argv = secrets.gitleaks_argv(("gl",), merge_base="a" * 40, head_sha="b" * 40, config=Path("c.toml"),
                                 report=Path("r.json"), source=Path("src"))
    assert argv[:2] == ["gl", "git"]
    assert argv[2] == ("--log-opts=--text --no-textconv --no-ext-diff --diff-merges=first-parent "
                       f"{'a' * 40}..{'b' * 40}")


def test_packaged_config_extends_the_default_rules() -> None:
    import tomllib

    data = tomllib.loads(gitleaks_config().read_text(encoding="utf-8"))
    assert data["extend"] == {"useDefault": True}
    assert "rules" not in data and "allowlist" not in data and "allowlists" not in data


@pytest.mark.parametrize("code", ["1", "2", "126"])
def test_unexpected_exit_code_fails_closed(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    sc = scenario(git_repo, leak=False)
    monkeypatch.setenv("FAKE_GITLEAKS_EXIT", code)
    (finding,) = run(sc.input())
    assert finding.code == "E_INTERNAL_UNEXPECTED"
    assert reason("exit", code=int(code)) in finding.message


@pytest.mark.parametrize(
    ("exit_code", "report"),
    [("0", "garbage"), ("0", "none"), ("3", "[]"), ("0", '{"not": "a list"}'), ("3", "[1]")],
)
def test_bad_report_fails_closed(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch, exit_code: str, report: str) -> None:
    sc = scenario(git_repo, leak=False)
    monkeypatch.setenv("FAKE_GITLEAKS_EXIT", exit_code)
    monkeypatch.setenv("FAKE_GITLEAKS_REPORT", report)
    (finding,) = run(sc.input())
    assert finding.code == "E_INTERNAL_UNEXPECTED" and reason("report") in finding.message


def test_report_fields_are_sanitized(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    sc = scenario(git_repo, leak=False)
    leaks = [
        {"File": "tex\\b.tex", "StartLine": 4, "RuleID": "generic-api-key", "Commit": sc.head, "Secret": LEAK},
        {"File": "tex\\b.tex", "StartLine": 4, "RuleID": "generic-api-key", "Commit": sc.head, "Secret": LEAK},
        {"File": "/abs/x", "StartLine": 0, "RuleID": "bad id!", "Commit": "zzz"},
    ]
    monkeypatch.setenv("FAKE_GITLEAKS_EXIT", str(secrets.LEAKS_EXIT_CODE))
    monkeypatch.setenv("FAKE_GITLEAKS_REPORT", json.dumps(leaks))
    first, second = run(sc.input())
    assert (first.path, first.line) == (None, None) and "`?`" in first.message
    assert (second.path, second.line) == ("tex/b.tex", 4) and "generic-api-key" in second.message
    assert LEAK not in first.message + second.message


def test_missing_tool_fails_closed(git_repo: GitRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sc = scenario(git_repo, leak=True)
    log = tmp_path / "call.json"
    monkeypatch.setenv("FAKE_GITLEAKS_LOG", str(log))
    (finding,) = run(sc.input(tools={}))
    assert finding.code == "E_INTERNAL_UNEXPECTED" and reason("no_tool") in finding.message
    (finding,) = run(sc.input(tools={"gitleaks": (str(tmp_path / "does-not-exist.exe"),)}))
    assert reason("no_tool") in finding.message
    assert not log.exists()


def test_gitleaksignore_in_the_tree_fails_closed(git_repo: GitRepo, tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    sc = scenario(git_repo, leak=True)
    (git_repo.path / ".gitleaksignore").write_text("*\n", encoding="utf-8")
    log = tmp_path / "call.json"
    monkeypatch.setenv("FAKE_GITLEAKS_LOG", str(log))
    (finding,) = run(sc.input())
    assert finding.code == "E_INTERNAL_UNEXPECTED" and finding.path == ".gitleaksignore"
    assert not log.exists()  # no se ejecuta gitleaks


def test_unknown_range_fails_closed(git_repo: GitRepo) -> None:
    sc = scenario(git_repo, leak=False)
    for inp in (sc.input(base=False), sc.input(head_sha=None), sc.input(head_sha="HEAD")):
        (finding,) = run(inp)
        assert reason("no_range") in finding.message
    no_tree = RuleInput(mode="pr", t=translator("en"), now=NOW, tools={"gitleaks": TOOL})
    (finding,) = run(no_tree)
    assert finding.code == "E_INTERNAL_UNEXPECTED"


def test_timeout_fails_closed(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    sc = scenario(git_repo, leak=False)
    monkeypatch.setattr(secrets, "TIMEOUT_S", 0.5)
    monkeypatch.setenv("FAKE_GITLEAKS_SLEEP", "5")
    (finding,) = run(sc.input())
    assert reason("timeout") in finding.message


def test_engine_resolves_the_testing_stub(git_repo: GitRepo) -> None:
    """``AUCOVE_GITLEAKS`` solo con ``AUCOVE_TESTING=1``; sin herramienta, la regla falla cerrada."""
    sc = scenario(git_repo, leak=True)
    prefix = _meta.ENV_PREFIX
    env = {prefix + "TESTING": "1", prefix + "GITLEAKS": json.dumps(list(TOOL)), "PATH": ""}
    tools = validate.resolve_tools(["gitleaks"], env=env, root=git_repo.path, cwd=os.getcwd())
    out = validate.evaluate([secrets.RULE], sc.input(tools=tools))
    assert [(o.check, o.status) for o in out] == [("secrets", "fail")]
    assert [f.code for f in out[0].findings] == ["E_VALIDATION_SECRET"]
    no_tools = validate.resolve_tools(["gitleaks"], env={"PATH": ""}, root=git_repo.path, cwd=os.getcwd())
    out = validate.evaluate([secrets.RULE], sc.input(tools=no_tools))
    assert out[0].status == "fail" and [f.code for f in out[0].findings] == ["E_INTERNAL_UNEXPECTED"]
