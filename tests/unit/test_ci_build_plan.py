# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``ci build-plan`` (09-ci §3.2, REQ-CI-49; 00-marco D-33 o y D-43)."""

from __future__ import annotations

import dataclasses
import io
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from aucove.cli import ci as cli_ci
from aucove.config.project import BuildConfig
from aucove.ops import ci_plan
from aucove.ops.ci_plan import (
    BUILD_PLAN_KEYS,
    PlanError,
    changes_need_build,
    no_manuscript_values,
    plan_values,
    unquote_git_path,
)

SHA = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "7334b00bf8e7a0996f7ddd65482363aaf7711d372e569f3ea78509619e3083ff"
IMAGE = f"registry.gitlab.com/islandoftex/images/texlive:TL2026-2026-09-20-full@sha256:{DIGEST}"


def _yaml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value)


def project_yaml(
    *,
    language: str = "es",
    core: str = ">=0.1.0,<0.2",
    core_pin: str = '"0.1.0"',
    build: Mapping[str, object] | None = None,
    ci: Mapping[str, object] | None = None,
    fmt: str = "latex",
) -> str:
    """``project.yaml`` mínimo válido, con secciones ``build`` y ``ci`` opcionales."""
    lines = [
        'convention: "0.1"',
        f'core: "{core}"',
        f"core_pin: {core_pin}",
        "name: demo",
        'title: "Demo"',
        f"language: {language}",
        f"format: {fmt}",
        "forge:",
        "  type: github",
        "  repo: example-org/demo",
    ]
    for section, values in (("build", build), ("ci", ci)):
        if values:
            lines.append(f"{section}:")
            lines += [f"  {k}: {_yaml_value(v)}" for k, v in values.items()]
    return "\n".join(lines) + "\n"


def write_policy(tmp: Path, text: str | None = None, **kw: object) -> Path:
    policy = tmp / "policy"
    policy.mkdir(exist_ok=True)
    (policy / "project.yaml").write_text(text if text is not None else project_yaml(**kw), encoding="utf-8", newline="\n")  # type: ignore[arg-type]
    return policy


def write_changed(tmp: Path, paths: Sequence[str] | str) -> Path:
    path = tmp / "changed.txt"
    data = paths if isinstance(paths, str) else "".join(p + "\n" for p in paths)
    path.write_bytes(data.encode("utf-8"))
    return path


def run_ci(argv: Sequence[str], env: Mapping[str, str] | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    environ = {"GITHUB_SHA": SHA} if env is None else dict(env)
    rc = cli_ci.run(list(argv), env=environ, stdout=out, stderr=err)
    return rc, out.getvalue(), err.getvalue()


def parse_output(text: str) -> list[tuple[str, str]]:
    assert text.endswith("\n")
    pairs = []
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        assert sep == "=", line
        pairs.append((key, value))
    return pairs


def plan(tmp: Path, *, mode: str = "pr", changed: Sequence[str] | str = ("tex/main.tex",), **kw: object) -> dict[str, str]:
    policy = write_policy(tmp, None, **kw)
    argv = ["build-plan", "--mode", mode, "--policy", str(policy)]
    if mode == "pr":
        argv += ["--changed", str(write_changed(tmp, changed))]
    rc, out, err = run_ci(argv)
    assert rc == 0, err
    pairs = parse_output(out)
    assert tuple(k for k, _ in pairs) == BUILD_PLAN_KEYS
    return dict(pairs)


# -- salida y valores ---------------------------------------------------------------


def test_base_mode_emits_every_key_in_contract_order(tmp_path: Path) -> None:
    policy = write_policy(tmp_path)
    rc, out, err = run_ci(["build-plan", "--mode", "base", "--policy", str(policy)])
    assert rc == 0, err
    assert parse_output(out) == [
        ("run", "true"),
        ("image", IMAGE),
        ("engine_flag", "-pdf"),
        ("shell", "-no-shell-escape"),
        ("timeout", "10"),
        ("main", "tex/main.tex"),
        ("main_base", "main"),
        ("sha7", SHA[:7]),
        ("retention", "90"),
        ("latexdiff", "false"),
        ("language", "es"),
        ("reason", ""),  # se compila: sin motivo (REQ-CI-49)
    ]


def test_output_is_deterministic_and_never_the_envelope(tmp_path: Path) -> None:
    policy = write_policy(tmp_path)
    changed = write_changed(tmp_path, ["tex/main.tex"])
    argv = ["build-plan", "--mode", "pr", "--policy", str(policy), "--changed", str(changed)]
    first, second = run_ci(argv), run_ci(argv)
    assert first == second
    assert first[0] == 0
    assert "schema" not in first[1] and "{" not in first[1]


def test_pr_mode_uses_the_policy_retention_and_latexdiff(tmp_path: Path) -> None:
    values = plan(tmp_path, ci={"pdf_retention_days": 7, "latexdiff": True})
    assert values["retention"] == "7"
    assert values["latexdiff"] == "true"


def test_base_mode_retention_is_always_90(tmp_path: Path) -> None:
    values = plan(tmp_path, mode="base", ci={"pdf_retention_days": 7})
    assert values["retention"] == "90"


@pytest.mark.parametrize(
    ("build", "key", "expected"),
    [
        ({"engine": "lualatex"}, "engine_flag", "-pdflua"),
        ({"engine": "xelatex"}, "engine_flag", "-pdfxe"),
        ({"engine": "pdflatex"}, "engine_flag", "-pdf"),
        ({"shell_escape": "restricted"}, "shell", "-shell-restricted"),
        ({"shell_escape": "disabled"}, "shell", "-no-shell-escape"),
        ({"timeout_minutes": 12}, "timeout", "12"),
        ({"timeout_minutes": 1}, "timeout", "1"),
        ({"main": "tex/paper-v2.tex"}, "main_base", "paper-v2"),
    ],
)
def test_values_follow_the_policy(tmp_path: Path, build: dict[str, object], key: str, expected: str) -> None:
    assert plan(tmp_path, build=build)[key] == expected


def test_language_comes_from_the_policy(tmp_path: Path) -> None:
    assert plan(tmp_path, language="en")["language"] == "en"


# -- run en modo pr -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("tex/main.tex", True),
        ("tex/chapters/appendix.tex", True),
        ("sections/methods/methods.tex", True),  # D-43: las partes viven en sections/
        ("sections/methods/figures/plot.pdf", True),
        ("sections/methods/notes/n1.md", True),
        ("bib/references.bib", True),
        ("project.yaml", True),
        (".github/workflows/build.yml", True),
        (".github/workflows/compile.yml", True),
        ("README.md", False),
        ("roles.yaml", False),
        ("plan.md", False),
        ("memory/notes/task-17.md", False),
        (".github/workflows/validate.yml", False),
        (".github/CODEOWNERS", False),
        ("texts/a.tex", False),
        ("Tex/main.tex", False),
        ("Sections/methods/methods.tex", False),
        ("docs/sections/x.tex", False),
        ("sections.tex", False),
        ("docs/project.yaml", False),
        ("data/bib/x.bib", False),
    ],
)
def test_pr_mode_runs_only_for_build_inputs(tmp_path: Path, path: str, expected: bool) -> None:
    values = plan(tmp_path, changed=[path])
    assert values["run"] == ("true" if expected else "false")
    assert values["reason"] == ("" if expected else "no-changes")


def test_pr_mode_with_several_files_and_crlf(tmp_path: Path) -> None:
    assert plan(tmp_path, changed="README.md\r\nplan.md\r\n")["run"] == "false"
    assert plan(tmp_path, changed="README.md\r\nbib/references.bib\r\n")["run"] == "true"


def test_pr_mode_with_no_changes_does_not_run(tmp_path: Path) -> None:
    values = plan(tmp_path, changed="")
    assert (values["run"], values["reason"]) == ("false", "no-changes")
    assert values["image"] == IMAGE and values["timeout"] == "10"  # con LaTeX, el plan sigue completo


def test_quoted_git_paths_are_unquoted(tmp_path: Path) -> None:
    # git diff --name-only escribe entre comillas las rutas con bytes no ASCII (core.quotePath).
    assert plan(tmp_path, changed=['"tex/m\\303\\251todos/a.tex"'])["run"] == "true"
    assert plan(tmp_path, changed=['"docs/\\303\\251.md"'])["run"] == "false"


def test_unparseable_lines_count_as_build_inputs() -> None:
    assert changes_need_build(['"docs/sin-cierre']) is True
    assert changes_need_build(['"docs/\\q.md"']) is True
    assert changes_need_build(["README.md", ""]) is False


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("tex/main.tex", "tex/main.tex"),
        ('"tex/a b\\t.tex"', "tex/a b\t.tex"),
        ('"tex/\\"q\\".tex"', 'tex/"q".tex'),
        ('"tex/\\\\x.tex"', "tex/\\x.tex"),
        ('"tex/m\\303\\251.tex"', "tex/mé.tex"),
        ('"tex/abc', None),
        ('"tex/a"b"', None),
        ('"tex/\\9.tex"', None),
        ('"tex/\\', None),
    ],
)
def test_unquote_git_path(line: str, expected: str | None) -> None:
    assert unquote_git_path(line) == expected


# -- format: none (REQ-CI-49; 00-marco D-43) -------------------------------------------

NO_MANUSCRIPT = [
    ("run", "false"),
    ("image", ""),
    ("engine_flag", ""),
    ("shell", ""),
    ("timeout", ""),
    ("main", ""),
    ("main_base", ""),
    ("sha7", ""),
    ("retention", ""),
    ("latexdiff", ""),
    ("language", "es"),
    ("reason", "format-none"),
]


@pytest.mark.parametrize("mode", ["pr", "base"])
def test_format_none_never_builds_in_either_mode(tmp_path: Path, mode: str) -> None:
    policy = write_policy(tmp_path, fmt="none")
    argv = ["build-plan", "--mode", mode, "--policy", str(policy)]
    if mode == "pr":
        argv += ["--changed", str(write_changed(tmp_path, ["sections/methods/methods.md", "tex/main.tex"]))]
    rc, out, err = run_ci(argv)
    assert rc == 0, err
    assert parse_output(out) == NO_MANUSCRIPT  # todas las salidas, en orden
    assert "format: none" in err  # el registro del job explica por qué no se compila


def test_format_none_needs_neither_build_values_nor_the_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """09-ci §3.2: con none no se exigen ni validan build.*; sin GITHUB_SHA ni git, igual responde."""
    policy = write_policy(tmp_path, fmt="none", language="en", ci={"pdf_retention_days": 7, "latexdiff": True})
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    env = {"GIT_CEILING_DIRECTORIES": str(tmp_path), "PATH": os.environ.get("PATH", "")}
    rc, out, err = run_ci(["build-plan", "--mode", "base", "--policy", str(policy)], env=env)
    assert rc == 0, err
    values = dict(parse_output(out))
    assert values["language"] == "en" and values["reason"] == "format-none"
    assert values["retention"] == "" and values["latexdiff"] == "" and values["sha7"] == ""


def test_format_none_with_a_build_block_is_an_invalid_policy(tmp_path: Path) -> None:
    """REQ-SCHEMA-90: el esquema rechaza build con format: none; build-plan falla cerrado."""
    policy = write_policy(tmp_path, fmt="none", build={"engine": "pdflatex"})
    rc, out, err = run_ci(["build-plan", "--mode", "base", "--policy", str(policy)])
    assert (rc, out) == (1, "")
    assert "E_VALIDATION_SCHEMA" in err


def test_no_manuscript_values_validates_the_language() -> None:
    assert no_manuscript_values("en")[-2:] == [("language", "en"), ("reason", "format-none")]
    with pytest.raises(PlanError) as info:
        no_manuscript_values("fr")
    assert info.value.key == "language"


def test_reason_follows_run_with_latex() -> None:
    assert plan_values(_build(), CI, "es", mode="pr", run=True, sha=SHA)[-1] == ("reason", "")
    assert plan_values(_build(), CI, "es", mode="pr", run=False, sha=SHA)[-1] == ("reason", "no-changes")
    assert [k for k, _ in plan_values(_build(), CI, "es", mode="base", run=True, sha=SHA)] == list(BUILD_PLAN_KEYS)


def test_empty_values_are_only_for_the_allowed_keys() -> None:
    pairs = [(k, "") for k in BUILD_PLAN_KEYS]
    with pytest.raises(PlanError) as info:
        ci_plan._check_output(pairs, BUILD_PLAN_KEYS, empty=("reason",))
    assert info.value.key == "run"
    ci_plan._check_output(pairs, BUILD_PLAN_KEYS, empty=BUILD_PLAN_KEYS)


def test_base_mode_ignores_the_changed_list(tmp_path: Path) -> None:
    policy = write_policy(tmp_path)
    changed = write_changed(tmp_path, ["README.md"])
    rc, out, _err = run_ci(["build-plan", "--mode", "base", "--policy", str(policy), "--changed", str(changed)])
    assert rc == 0
    assert dict(parse_output(out))["run"] == "true"


def test_a_huge_changed_list_builds_anyway(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ci_plan, "MAX_CHANGED_BYTES", 10)
    assert plan(tmp_path, changed=["README.md", "plan.md", "roles.yaml"])["run"] == "true"


# -- falla cerrada ------------------------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        {"engine": "luatex"},  # motor inválido (el esquema ya lo rechaza)
        {"engine": "latex"},
        {"main": "tex/chapters/intro.tex"},  # válido para el esquema, no para 09-ci §3.2
        {"main": "main.tex"},
        {"main": "doc/main.tex"},
        {"timeout_minutes": 13},  # D-33 (o): 1..12
        {"timeout_minutes": 0},
        {"timeout_minutes": 60},
        {"image": "registry.gitlab.com/islandoftex/images/texlive:latest"},  # sin @sha256:
        {"image": "texlive@sha256:abc"},
        {"shell_escape": "enabled"},
    ],
)
def test_invalid_values_exit_nonzero_and_emit_nothing(tmp_path: Path, build: dict[str, object]) -> None:
    policy = write_policy(tmp_path, build=build)
    changed = write_changed(tmp_path, ["tex/main.tex"])
    for argv in (
        ["build-plan", "--mode", "pr", "--policy", str(policy), "--changed", str(changed)],
        ["build-plan", "--mode", "base", "--policy", str(policy)],
    ):
        rc, out, err = run_ci(argv)
        assert rc != 0 and rc != 2
        assert rc == 1
        assert out == ""
        assert err


def test_main_outside_tex_root_names_the_key(tmp_path: Path) -> None:
    policy = write_policy(tmp_path, build={"main": "tex/chapters/intro.tex"})
    rc, out, err = run_ci(["build-plan", "--mode", "base", "--policy", str(policy)])
    assert (rc, out) == (1, "")
    assert "`main`" in err


def _build(**changes: object) -> BuildConfig:
    base = BuildConfig(
        engine="pdflatex",
        main="tex/main.tex",
        image=IMAGE,
        bibliography="biber",
        bib_files=("bib/references.bib",),
        shell_escape="disabled",
        compile_on_submit="if_available",
        timeout_minutes=10,
    )
    return dataclasses.replace(base, **changes)  # type: ignore[arg-type]


CI = {"pdf_retention_days": 14, "latexdiff": False}


@pytest.mark.parametrize(
    ("changes", "key"),
    [
        ({"engine": "luatex"}, "engine_flag"),
        ({"engine": ""}, "engine_flag"),
        ({"shell_escape": "unrestricted"}, "shell"),
        ({"timeout_minutes": 13}, "timeout"),
        ({"timeout_minutes": 0}, "timeout"),
        ({"timeout_minutes": True}, "timeout"),
        ({"timeout_minutes": 10.0}, "timeout"),
        ({"timeout_minutes": "10"}, "timeout"),
        ({"main": "tex/sub/main.tex"}, "main"),
        ({"main": "tex/main.tex\n"}, "main"),
        ({"main": "tex/ma in.tex"}, "main"),
        ({"main": "tex/máin.tex"}, "main"),
        ({"main": "tex/main.ltx"}, "main"),
        ({"image": "texlive:latest"}, "image"),
        ({"image": f"-texlive@sha256:{DIGEST}"}, "image"),  # sería una opción de docker pull
        ({"image": f"texlive@sha256:{DIGEST.upper()}"}, "image"),
        ({"image": f"texlive@sha256:{DIGEST}\nrun=true"}, "image"),
        ({"image": f"tex live@sha256:{DIGEST}"}, "image"),
    ],
)
def test_plan_values_validates_each_value(changes: dict[str, object], key: str) -> None:
    with pytest.raises(PlanError) as info:
        plan_values(_build(**changes), CI, "es", mode="pr", run=True, sha=SHA)
    assert info.value.key == key


@pytest.mark.parametrize(
    ("ci", "language", "sha", "key"),
    [
        ({"pdf_retention_days": 0, "latexdiff": False}, "es", SHA, "retention"),
        ({"pdf_retention_days": 91, "latexdiff": False}, "es", SHA, "retention"),
        ({"pdf_retention_days": 14, "latexdiff": "false"}, "es", SHA, "latexdiff"),
        (CI, "fr", SHA, "language"),
        (CI, "es", SHA[:39], "sha7"),
        (CI, "es", SHA.upper(), "sha7"),
    ],
)
def test_plan_values_validates_ci_language_and_sha(ci: dict[str, object], language: str, sha: str, key: str) -> None:
    with pytest.raises(PlanError) as info:
        plan_values(_build(), ci, language, mode="pr", run=False, sha=sha)
    assert info.value.key == key


def test_plan_values_accepts_sha256_repositories() -> None:
    pairs = dict(plan_values(_build(), CI, "en", mode="base", run=True, sha="ab" * 32))
    assert pairs["sha7"] == "abababa"


@pytest.mark.parametrize("sha", ["xyz", SHA.upper(), SHA + "0", "0123456"])
def test_invalid_github_sha_fails_closed(tmp_path: Path, sha: str) -> None:
    policy = write_policy(tmp_path)
    rc, out, err = run_ci(["build-plan", "--mode", "base", "--policy", str(policy)], env={"GITHUB_SHA": sha})
    assert (rc, out) == (1, "")
    assert "GITHUB_SHA" in err


def test_sha_falls_back_to_git_rev_parse_in_cwd(tmp_path: Path, git_repo, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    head = git_repo.commit("inicio", files={"README.md": "x\n"})
    policy = write_policy(tmp_path)
    monkeypatch.chdir(git_repo.path)
    env = {k: v for k, v in git_repo.env.items() if k != "GITHUB_SHA"}
    rc, out, err = run_ci(["build-plan", "--mode", "base", "--policy", str(policy)], env=env)
    assert rc == 0, err
    assert dict(parse_output(out))["sha7"] == head[:7]


def test_no_sha_and_no_repository_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    policy = write_policy(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    env = {"GIT_CEILING_DIRECTORIES": str(tmp_path), "PATH": os.environ.get("PATH", "")}
    rc, out, _err = run_ci(["build-plan", "--mode", "base", "--policy", str(policy)], env=env)
    assert (rc, out) == (1, "")


def test_pr_mode_without_changed_is_a_usage_error(tmp_path: Path) -> None:
    policy = write_policy(tmp_path)
    rc, out, err = run_ci(["build-plan", "--mode", "pr", "--policy", str(policy)])
    assert (rc, out) == (3, "")
    assert "--changed" in err


def test_missing_changed_file_fails_closed(tmp_path: Path) -> None:
    policy = write_policy(tmp_path)
    rc, out, _err = run_ci(
        ["build-plan", "--mode", "pr", "--policy", str(policy), "--changed", str(tmp_path / "nope.txt")]
    )
    assert (rc, out) == (1, "")


def test_missing_or_invalid_policy_fails_closed(tmp_path: Path) -> None:
    rc, out, _err = run_ci(["build-plan", "--mode", "base", "--policy", str(tmp_path / "nope")])
    assert (rc, out) == (1, "")
    policy = write_policy(tmp_path, text="convention: [\n")
    rc, out, err = run_ci(["build-plan", "--mode", "base", "--policy", str(policy)])
    assert (rc, out) == (1, "")
    assert "E_CONFIG_YAML" in err


def test_policy_symlink_is_not_followed(tmp_path: Path) -> None:
    real = tmp_path / "real.yaml"
    real.write_text(project_yaml(), encoding="utf-8")
    policy = tmp_path / "policy"
    policy.mkdir()
    try:
        os.symlink(real, policy / "project.yaml")
    except (OSError, NotImplementedError):
        pytest.skip("este sistema no permite crear enlaces simbólicos")
    rc, out, _err = run_ci(["build-plan", "--mode", "base", "--policy", str(policy)])
    assert (rc, out) == (1, "")


@pytest.mark.parametrize(
    "argv",
    [
        ["build-plan"],
        ["build-plan", "--mode", "tree", "--policy", "x"],
        ["build-plan", "--mode", "pr"],
        ["build-plan", "--mode", "base", "--policy", "x", "--extra"],
        ["build-plan", "--mod", "base", "--policy", "x"],  # sin abreviaturas
    ],
)
def test_usage_errors_exit_3(argv: list[str]) -> None:
    rc, out, _err = run_ci(argv)
    assert (rc, out) == (3, "")
