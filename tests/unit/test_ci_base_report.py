# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Pruebas de ``ci base-report`` contra FakeForge (09-ci §7.1-7.3, REQ-CI-32..34).

Llaman al ``CiCommand`` directamente, sin pasar por ``cli/ci.py``.
"""

from __future__ import annotations

import argparse
import importlib
import io
import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from aucove import _meta
from aucove.forge.base import ChangedFile, Issue, RepoRef
from aucove.forge.fake import CI_LOGIN, FakeForge, Fault
from aucove.i18n import load_catalog
from aucove.ops import ci_base_report as br
from aucove.ops.context import Invocation, OpContext
from aucove.ops.registry import CI_MODULES, CiCommand
from tests.conftest import FIXTURES, GitRepo, roles_fixture

COMMAND = br.COMMANDS[0]
MARKER = _meta.ci_marker("main-broken")
PROJECT = (FIXTURES / "config" / "project" / "minimal.yaml").read_text(encoding="utf-8")
REPO = RepoRef("example-org", "demo")
RUN_URL = "https://github.com/example-org/demo/actions/runs/101"
PDF_URL = "https://github.com/example-org/demo/actions/runs/101/artifacts/7"
PREFIX = _meta.ENV_PREFIX


# ---------------------------------------------------------------------------
# Escenario: base c0 (verde) -> c1 (PR a) -> c2 (PR b) -> c3 (PR c)
# ---------------------------------------------------------------------------


@dataclass
class World:
    forge: FakeForge  # vista como github-actions[bot]
    policy: Path
    c0: str
    c1: str
    c2: str
    c3: str
    pr_a: int
    pr_b: int
    pr_c: int
    pr_other_base: int


def write_policy(root: Path, *, language: str = "es", roles: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    project = PROJECT.replace("language: es", f"language: {language}")
    (root / "project.yaml").write_text(project, encoding="utf-8", newline="\n")
    (root / "roles.yaml").write_text(roles if roles is not None else roles_fixture("basic"), encoding="utf-8", newline="\n")
    return root


def make_world(tmp_path: Path, *, language: str = "es", green: bool = True, roles: str | None = None) -> World:
    fake = FakeForge(REPO, viewer=CI_LOGIN)
    for login in ("ana", "bruno", "carla"):
        fake.seed_user(login)
    c0 = fake.seed_commit({"tex/main.tex": b"v0"}, message="init")
    if green:
        fake.seed_workflow_run(br.WORKFLOW_FILE, branch="main", sha=c0, conclusion="success")
    fake.seed_workflow_run(br.WORKFLOW_FILE, branch="other", sha=c0, conclusion="success")

    def merged(parent: str, n: int, author: str, title: str) -> tuple[str, int]:
        head = fake.seed_commit({"tex/main.tex": f"h{n}".encode()}, parents=[parent], message=f"work {n}", author=author)
        squash = fake.seed_commit({"tex/main.tex": f"v{n}".encode()}, parents=[parent], message=f"squash {n}")
        pr = fake.seed_pull_request(
            files=[ChangedFile("tex/main.tex", "modified")],
            head_branch=f"task/{n}",
            head_sha=head,
            merged=True,
            merge_commit_sha=squash,
            author=author,
            title=title,
            commits=[head],
        )
        return squash, pr.number

    c1, pr_a = merged(c0, 1, "carla", "Métodos nuevos")
    fake.seed_workflow_run(br.WORKFLOW_FILE, branch="main", sha=c1, conclusion="failure")
    c2, pr_b = merged(c1, 2, "bruno", "Resultados")
    c3, pr_c = merged(c2, 3, "ana", "Introducción")
    # Un PR hacia otra rama que contiene c2: no es sospechoso de la base.
    other = fake.seed_pull_request(files=[], head_branch="main", head_sha=c2, base_branch="release", title="Release")
    fake.seed_branch("main", c3)
    policy = write_policy(tmp_path / "policy", language=language, roles=roles)
    return World(fake, policy, c0, c1, c2, c3, pr_a, pr_b, pr_c, other.number)


def parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="base-report")
    COMMAND.add_arguments(parser)
    return parser.parse_args(argv)


def argv_for(
    policy: Path,
    sha: str,
    *,
    build: str = "success",
    validate: str = "success",
    run_url: str = RUN_URL,
    pdf_url: str = "",
    reports: Path | None = None,
    summary: Path | None = None,
    build_reason: str | None = None,
) -> list[str]:
    argv = ["--policy", str(policy), "--sha", sha, "--build", build, "--validate", validate,
            "--pdf-url", pdf_url, "--run-url", run_url]
    if build_reason is not None:
        argv += ["--build-reason", build_reason]
    if reports is not None:
        argv += ["--reports", str(reports)]
    if summary is not None:
        argv += ["--summary", str(summary)]
    return argv


def invoke(
    argv: list[str],
    forge: FakeForge | None,
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> tuple[int, str]:
    inv = Invocation(root=None, lang=None, json=False, dry_run=False, surface="ci",
                     env=dict(env or {}), cwd=str(cwd) if cwd else str(Path.cwd()))
    ctx = OpContext(inv, forge=forge)
    out = io.StringIO()
    try:
        code = COMMAND.main(parse(argv), ctx, out)
    finally:
        ctx.close()
    return code, out.getvalue()


def run(w: World, sha: str, **kw: object) -> tuple[int, dict[str, str]]:
    code, out = invoke(argv_for(w.policy, sha, **kw), w.forge)  # type: ignore[arg-type]
    return code, dict(line.split("=", 1) for line in out.splitlines())


def open_marked(forge: FakeForge) -> list[Issue]:
    return [i for i in forge.list_issues(labels=[br.LABEL], state="open") if MARKER in i.body]


def calls_of(forge: FakeForge, method: str) -> list[dict[str, object]]:
    return [c.args for c in forge.calls.entries() if c.method == method]


# ---------------------------------------------------------------------------
# Registro del subcomando y su ayuda
# ---------------------------------------------------------------------------


def test_commands_are_exported_for_ci_modules() -> None:
    module = importlib.import_module("." + CI_MODULES[2], _meta.NAME)
    assert module is br
    assert isinstance(br.COMMANDS, tuple) and len(br.COMMANDS) == 1
    assert isinstance(COMMAND, CiCommand) and COMMAND.name == "base-report"


def test_help_is_localized_and_arguments_are_optional_in_argparse() -> None:
    parser = argparse.ArgumentParser(prog="base-report")
    COMMAND.add_arguments(parser)
    text = parser.format_help()
    assert "base_report.arg" not in text
    for flag in ("--policy", "--sha", "--build", "--build-reason", "--validate", "--pdf-url", "--run-url",
                 "--reports", "--summary"):
        assert flag in text
    # Ningún argumento es required ni usa choices: argparse no sale con 2 por ellos.
    assert parser.parse_args([]).sha is None


@pytest.mark.parametrize(
    ("build", "validate", "policy_ok", "expected"),
    [
        ("success", "success", True, "green"),
        ("failure", "success", True, "broken"),
        ("success", "failure", True, "broken"),
        ("", "success", True, "broken"),
        ("failure", "cancelled", True, "broken"),
        ("success", "success", False, "broken"),
        # En modo base el paso tex siempre debe correr: skipped = falló un paso anterior.
        ("skipped", "success", True, "broken"),
        ("skipped", "failure", True, "broken"),
        ("cancelled", "success", True, "inconclusive"),
        ("success", "skipped", True, "inconclusive"),
        ("skipped", "cancelled", True, "inconclusive"),
    ],
)
def test_classify(build: str, validate: str, policy_ok: bool, expected: str) -> None:
    assert br.classify(build, validate, policy_ok=policy_ok) == expected


@pytest.mark.parametrize(
    ("build", "validate", "reason", "policy_ok", "expected"),
    [
        # REQ-CI-49: sin manuscrito, la compilación omitida cuenta como éxito.
        ("skipped", "success", "format-none", True, "green"),
        ("skipped", "failure", "format-none", True, "broken"),
        ("skipped", "cancelled", "format-none", True, "inconclusive"),
        ("skipped", "success", "format-none", False, "broken"),
        # format-none solo justifica un paso tex omitido; cualquier otra combinación falla cerrada.
        ("failure", "success", "format-none", True, "broken"),
        ("", "success", "format-none", True, "broken"),
        ("skipped", "success", "no-changes", True, "broken"),  # en la base no hay «sin cambios»
        ("skipped", "success", "", True, "broken"),
    ],
)
def test_classify_with_build_reason(build: str, validate: str, reason: str, policy_ok: bool, expected: str) -> None:
    assert br.classify(build, validate, policy_ok=policy_ok, build_reason=reason) == expected


# ---------------------------------------------------------------------------
# Criterios de aceptación
# ---------------------------------------------------------------------------


def test_two_failures_leave_one_issue_with_marker_and_one_comment(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    code, out = run(w, w.c1, build="failure", pdf_url=PDF_URL)
    assert code == 0 and out["status"] == "broken" and out["action"] == "created"
    number = int(out["issue"])

    code, out = run(w, w.c2, validate="failure", run_url=RUN_URL.replace("101", "102"))
    assert code == 0 and out["action"] == "commented" and out["issue"] == str(number)

    issues = open_marked(w.forge)
    assert [i.number for i in issues] == [number]
    issue = issues[0]
    assert issue.body.startswith(MARKER + "\n")
    assert br.LABEL in issue.labels
    assert issue.assignees == ("ana", "bruno")  # lead y deputy de --policy (basic.yaml)
    assert issue.title == "La rama base main no compila o no valida"
    assert w.forge.calls.count(method="create_issue") == 1
    assert w.forge.calls.count(method="set_assignees") == 1

    # REQ-CI-33: SHA, run, checks fallidos, PDF, hallazgos, sospechosos y guía.
    assert f"`{w.c1}`" in issue.body
    assert f"<{RUN_URL}>" in issue.body
    assert f"`{br.BUILD_CHECK}`" in issue.body and br.VALIDATE_CHECK not in issue.body
    assert f"<{PDF_URL}>" in issue.body
    assert "### Hallazgos (primeros 20)" in issue.body
    assert "### PR sospechosos" in issue.body
    assert "### Guía de reversión" in issue.body
    assert f"`{_meta.NAME} submit`" in issue.body

    comments = w.forge.list_comments(number)
    assert len(comments) == 1
    body = comments[0].body
    assert f"`{w.c2}`" in body and RUN_URL.replace("101", "102") in body
    assert f"`{br.VALIDATE_CHECK}`" in body and br.BUILD_CHECK not in body
    assert "PDF" not in body  # sin --pdf-url, el PDF no existe
    assert MARKER not in body


def test_green_run_comments_resolved_by_sha_and_closes(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    _code, out = run(w, w.c1, build="failure")
    number = int(out["issue"])

    code, out = run(w, w.c2, run_url=RUN_URL.replace("101", "103"))
    assert code == 0 and out == {"status": "green", "action": "closed", "issue": str(number)}
    issue = w.forge.get_issue(number)
    assert issue is not None and issue.state == "closed" and issue.state_reason == "completed"
    last = w.forge.list_comments(number)[-1].body
    assert last.startswith(f"Resuelto por `{w.c2}`.")
    assert open_marked(w.forge) == []

    # Otra corrida verde no vuelve a escribir.
    before = w.forge.calls.count("E")
    code, out = run(w, w.c3)
    assert code == 0 and out["action"] == "none" and w.forge.calls.count("E") == before


def test_green_run_in_english(tmp_path: Path) -> None:
    w = make_world(tmp_path, language="en")
    _code, out = run(w, w.c1, validate="failure")
    number = int(out["issue"])
    issue = w.forge.get_issue(number)
    assert issue is not None and issue.title == "Base branch main fails build or validation"
    assert "### Revert guide" in issue.body
    run(w, w.c2)
    assert w.forge.list_comments(number)[-1].body.startswith(f"Resolved by `{w.c2}`.")


def test_suspects_come_from_last_successful_run_compare_and_pulls_for_commit(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    code, out = run(w, w.c3, build="failure")
    assert code == 0
    body = w.forge.get_issue(int(out["issue"])).body  # type: ignore[union-attr]

    assert calls_of(w.forge, "last_successful_run") == [{"workflow_file": "main-broken.yml", "branch": "main"}]
    assert calls_of(w.forge, "compare") == [{"base": w.c0, "head": w.c3, "with_commits": True}]
    assert [a["sha"] for a in calls_of(w.forge, "pulls_for_commit")] == [w.c1, w.c2, w.c3]

    for number in (w.pr_a, w.pr_b, w.pr_c):
        assert f"- #{number} " in body
    assert f"#{w.pr_other_base} " not in body  # PR hacia otra rama base
    assert f"`{w.c0[:12]}`" in body  # último SHA en verde
    assert "(autor: `carla`)" in body
    assert body.index(f"#{w.pr_a} ") < body.index(f"#{w.pr_b} ") < body.index(f"#{w.pr_c} ")


def test_without_a_green_run_the_suspects_are_the_prs_of_the_commit(tmp_path: Path) -> None:
    w = make_world(tmp_path, green=False)
    _code, out = run(w, w.c2, build="failure")
    body = w.forge.get_issue(int(out["issue"])).body  # type: ignore[union-attr]
    assert calls_of(w.forge, "compare") == []
    assert [a["sha"] for a in calls_of(w.forge, "pulls_for_commit")] == [w.c2]
    assert f"- #{w.pr_b} " in body and f"#{w.pr_a} " not in body
    assert "No hay una ejecución exitosa previa de `main-broken.yml` en `main`" in body


def test_same_commit_as_last_green_has_no_suspects(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    _code, out = run(w, w.c0, build="failure")
    body = w.forge.get_issue(int(out["issue"])).body  # type: ignore[union-attr]
    assert calls_of(w.forge, "pulls_for_commit") == []
    assert "falla intermitente" in body


def test_suspect_lookup_failure_does_not_block_the_issue(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.forge.faults.add(Fault("compare", "http_5xx"))
    code, out = run(w, w.c3, build="failure")
    assert code == 0 and out["action"] == "created"
    body = w.forge.get_issue(int(out["issue"])).body  # type: ignore[union-attr]
    assert "La forja no respondió" in body


def test_compare_not_found_falls_back_to_the_current_commit(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    w.forge.faults.add(Fault("compare", "http_404"))
    _code, out = run(w, w.c3, build="failure")
    body = w.forge.get_issue(int(out["issue"])).body  # type: ignore[union-attr]
    assert [a["sha"] for a in calls_of(w.forge, "pulls_for_commit")] == [w.c3]
    assert f"- #{w.pr_c} " in body and "No se pudo comparar" in body


def test_long_ranges_are_capped(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    tip = w.c3
    for n in range(br.MAX_SUSPECT_COMMITS + 5):
        tip = w.forge.seed_commit({"tex/main.tex": f"x{n}".encode()}, parents=[tip], message=f"c{n}")
    _code, out = run(w, tip, build="failure")
    body = w.forge.get_issue(int(out["issue"])).body  # type: ignore[union-attr]
    shas = [a["sha"] for a in calls_of(w.forge, "pulls_for_commit")]
    assert len(shas) == br.MAX_SUSPECT_COMMITS and shas[-1] == tip
    assert f"Solo se revisaron los {br.MAX_SUSPECT_COMMITS} commits más recientes." in body


# ---------------------------------------------------------------------------
# Exactamente un Issue e idempotencia
# ---------------------------------------------------------------------------


def test_rerun_of_the_same_run_does_not_duplicate(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    _code, out = run(w, w.c1, build="failure")
    number = int(out["issue"])
    code, out = run(w, w.c1, build="failure")  # re-run del mismo job
    assert code == 0 and out["action"] == "unchanged"
    assert w.forge.list_comments(number) == []

    run(w, w.c2, build="failure", run_url=RUN_URL.replace("101", "104"))
    code, out = run(w, w.c2, build="failure", run_url=RUN_URL.replace("101", "104"))
    assert out["action"] == "unchanged"
    assert len(w.forge.list_comments(number)) == 1
    assert len(open_marked(w.forge)) == 1


def test_duplicates_are_closed_so_exactly_one_stays_open(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    first = w.forge.seed_issue(title="a", body=f"{MARKER}\nviejo", labels=[br.LABEL], author=CI_LOGIN)
    second = w.forge.seed_issue(title="b", body=f"{MARKER}\notro", labels=[br.LABEL], author="ana")
    unmarked = w.forge.seed_issue(title="c", body="sin marcador", labels=[br.LABEL])
    closed = w.forge.seed_issue(title="d", body=MARKER, labels=[br.LABEL], state="closed")

    code, out = run(w, w.c1, build="failure")
    assert code == 0 and out["action"] == "commented" and out["issue"] == str(first.number)
    assert [i.number for i in open_marked(w.forge)] == [first.number]
    dup = w.forge.get_issue(second.number)
    assert dup is not None and dup.state == "closed" and dup.state_reason == "duplicate"
    assert w.forge.list_comments(second.number)[0].body.startswith(f"Duplicado de #{first.number}:")
    assert w.forge.get_issue(unmarked.number).state == "open"  # type: ignore[union-attr]
    assert w.forge.list_comments(unmarked.number) == []
    assert w.forge.list_comments(closed.number) == []
    assert w.forge.calls.count(method="create_issue") == 0


def test_green_closes_every_marked_issue(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    a = w.forge.seed_issue(title="a", body=MARKER, labels=[br.LABEL])
    b = w.forge.seed_issue(title="b", body=MARKER, labels=[br.LABEL])
    code, out = run(w, w.c3)
    assert code == 0 and out["action"] == "closed" and out["issue"] == f"{a.number},{b.number}"
    assert open_marked(w.forge) == []


def test_green_without_issue_writes_nothing(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    code, out = run(w, w.c3)
    assert code == 0 and out == {"status": "green", "action": "none", "issue": ""}
    assert w.forge.calls.count("E") == 0


@pytest.mark.parametrize(
    ("build", "validate"), [("cancelled", "success"), ("success", "skipped"), ("skipped", "cancelled")]
)
def test_inconclusive_touches_nothing(tmp_path: Path, build: str, validate: str) -> None:
    w = make_world(tmp_path)
    w.forge.seed_issue(title="a", body=MARKER, labels=[br.LABEL])
    w.forge.calls.clear()
    code, out = run(w, w.c3, build=build, validate=validate)
    assert code == 0 and out["status"] == "inconclusive" and out["action"] == "none"
    assert len(w.forge.calls) == 0


# ---------------------------------------------------------------------------
# Sin manuscrito: --build skipped --build-reason format-none (REQ-CI-49)
# ---------------------------------------------------------------------------


def test_format_none_does_not_open_main_broken(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    summary = tmp_path / "summary.md"
    code, out = run(w, w.c3, build="skipped", build_reason="format-none", summary=summary)
    assert code == 0 and out == {"status": "green", "action": "none", "issue": ""}
    assert open_marked(w.forge) == []
    assert w.forge.calls.count(method="create_issue") == 0 and w.forge.calls.count("E") == 0
    assert f"Base en verde en `{w.c3}`" in summary.read_text(encoding="utf-8")


def test_format_none_closes_an_open_main_broken(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    _code, out = run(w, w.c1, validate="failure")
    number = int(out["issue"])
    code, out = run(w, w.c2, build="skipped", build_reason="format-none", run_url=RUN_URL.replace("101", "105"))
    assert code == 0 and out == {"status": "green", "action": "closed", "issue": str(number)}
    assert w.forge.list_comments(number)[-1].body.startswith(f"Resuelto por `{w.c2}`.")


def test_format_none_with_a_failed_validation_lists_only_the_validation(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    code, out = run(w, w.c1, build="skipped", build_reason="format-none", validate="failure")
    assert code == 0 and out["status"] == "broken" and out["action"] == "created"
    body = w.forge.get_issue(int(out["issue"])).body  # type: ignore[union-attr]
    assert f"`{br.VALIDATE_CHECK}`" in body and br.BUILD_CHECK not in body
    assert "no llegó a ejecutarse" not in body  # no es una compilación que falló antes de TeX


@pytest.mark.parametrize("reason", ["no-changes", ""])
def test_other_build_reasons_keep_skipped_as_a_failure(tmp_path: Path, reason: str) -> None:
    w = make_world(tmp_path)
    code, out = run(w, w.c1, build="skipped", build_reason=reason)
    assert code == 0 and out["status"] == "broken" and out["action"] == "created"


@pytest.mark.parametrize("reason", ["format_none", "FORMAT-NONE", "none", "x"])
def test_unknown_build_reason_fails_closed(tmp_path: Path, reason: str, capsys: pytest.CaptureFixture[str]) -> None:
    w = make_world(tmp_path)
    code, out = run(w, w.c1, build="skipped", build_reason=reason)
    assert code == 1 and out == {}
    assert "--build-reason" in capsys.readouterr().err
    assert w.forge.calls.count("E") == 0


def test_unreadable_policy_still_opens_the_issue_unassigned(tmp_path: Path) -> None:
    # REQ-CI-46: main-broken abre su Issue también si el roles.yaml de la base es inválido.
    w = make_world(tmp_path, roles="version: 1\nlead: ana\n")
    code, out = run(w, w.c1)  # compilación y validación en verde, pero la política no se lee
    assert code == 0 and out["status"] == "broken" and out["action"] == "created"
    issue = w.forge.get_issue(int(out["issue"]))
    assert issue is not None and issue.assignees == ()
    assert "`roles.yaml`" in issue.body and "no tiene asignados" in issue.body  # idioma del project.yaml
    assert w.forge.calls.count(method="set_assignees") == 0


def test_policy_fallback_counts_as_broken_but_assigns_from_the_last_valid_version(
    tmp_path: Path, git_repo: GitRepo
) -> None:
    # --policy es un checkout con historia: read_policy_dir usa el respaldo de REQ-PERM-4.
    good = git_repo.commit("válida", files={"project.yaml": PROJECT, "roles.yaml": roles_fixture("basic")})
    git_repo.commit("rompe roles", files={"roles.yaml": "version: 1\nlead: ana\n"})
    w = make_world(tmp_path)
    code, out = invoke(argv_for(git_repo.path, w.c1), w.forge)
    parsed = dict(line.split("=", 1) for line in out.splitlines())
    assert code == 0 and parsed["status"] == "broken" and parsed["action"] == "created"
    issue = w.forge.get_issue(int(parsed["issue"]))
    assert issue is not None and issue.assignees == ("ana", "bruno")
    assert f"la última versión válida, `{good[:12]}`" in issue.body


def test_assignment_that_is_not_applied_is_only_a_warning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # eva y fede (lead y deputy) no tienen acceso al repo: la forja descarta la asignación.
    roles = roles_fixture("basic").replace("ana", "eva").replace("bruno", "fede")
    w = make_world(tmp_path, roles=roles)
    code, out = run(w, w.c1, build="failure")
    assert code == 0 and out["action"] == "created"
    assert w.forge.get_issue(int(out["issue"])).assignees == ()  # type: ignore[union-attr]
    assert "no quedó asignado a eva, fede" in capsys.readouterr().err


def test_partial_assignment_names_only_the_missing_login(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # El deputy (fede) no tiene acceso: la forja asigna solo al lead y el aviso nombra solo a fede.
    roles = roles_fixture("basic").replace("bruno", "fede")
    w = make_world(tmp_path, roles=roles)
    code, out = run(w, w.c1, build="failure")
    assert code == 0 and out["action"] == "created"
    assert w.forge.get_issue(int(out["issue"])).assignees == ("ana",)  # type: ignore[union-attr]
    err = capsys.readouterr().err
    assert "no quedó asignado a fede." in err and "ana" not in err


# ---------------------------------------------------------------------------
# Hallazgos y saneamiento del texto ajeno
# ---------------------------------------------------------------------------


def _finding(i: int, severity: str) -> dict[str, object]:
    return {"path": f"sections/methods/f{i}.tex", "line": i + 1, "severity": severity,
            "code": "E_VALIDATION_LABEL", "message": f"hallazgo {i}", "pointer": None, "hint": None}


def write_reports(root: Path, findings: list[dict[str, object]]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    env = {
        "schema": _meta.RESULT_SCHEMA, "ok": False, "op": "validate",
        "data": {"valid": False, "mode": "base", "base": {"ref": "main", "sha": None},
                 "head": {"ref": "main", "sha": None},
                 "results": [{"check": "labels-refs", "status": "fail", "findings": findings}],
                 "page": {"next_cursor": None, "returned": len(findings), "total": len(findings)}},
        "warnings": [],
        "error": {"code": "E_VALIDATION_FAILED", "message": "x", "retryable": False, "hint": None},
    }
    (root / br.VALIDATE_REPORT).write_text(json.dumps(env), encoding="utf-8")
    shutil.copyfile(FIXTURES / "diagnostics" / "build-report.sample.json", root / br.BUILD_REPORT)
    return root


def test_first_20_findings_errors_first(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    findings = [_finding(i, "warning") for i in range(3)] + [_finding(i, "error") for i in range(3, 25)]
    reports = write_reports(tmp_path / "reports", findings)
    _code, out = run(w, w.c1, build="failure", validate="failure", reports=reports)
    body = w.forge.get_issue(int(out["issue"])).body  # type: ignore[union-attr]
    lines = [ln for ln in body.splitlines() if ln.startswith("- **error**") or ln.startswith("- **aviso**")]
    assert len(lines) == br.MAX_FINDINGS
    assert all(ln.startswith("- **error**") for ln in lines)
    # Primero el diagnóstico de la compilación, luego la validación, en su orden.
    assert "`latex-log`" in lines[0] and "`sections/methods/methods.tex:12`" in lines[0]
    assert "`labels-refs` · `sections/methods/f3.tex:4` · `E_VALIDATION_LABEL`: `hallazgo 3`" in lines[1]
    assert "…y 7 hallazgos más en los reportes de la ejecución." in body  # 2 + 25 - 20


def test_missing_or_broken_reports_are_not_an_error(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / br.VALIDATE_REPORT).write_text("{no es json", encoding="utf-8")
    code, out = run(w, w.c1, build="failure", reports=reports)
    assert code == 0
    assert "No hay hallazgos disponibles" in w.forge.get_issue(int(out["issue"])).body  # type: ignore[union-attr]
    code, _out = run(w, w.c2, build="failure", reports=tmp_path / "no-existe", run_url=RUN_URL + "0")
    assert code == 0


def test_validate_envelope_without_data_reports_its_error(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    env = {"schema": _meta.RESULT_SCHEMA, "ok": False, "op": "validate", "data": None, "warnings": [],
           "error": {"code": "E_CONFIG_POLICY_UNAVAILABLE", "message": "sin política", "retryable": False, "hint": None}}
    (reports / br.VALIDATE_REPORT).write_text(json.dumps(env), encoding="utf-8")
    found = br.load_findings(reports)
    assert [(f.check, f.code, f.message) for f in found] == [("validate", "E_CONFIG_POLICY_UNAVAILABLE", "sin política")]


def test_untrusted_text_is_escaped_truncated_and_without_mentions(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    title = "@evil ``rm -rf`` \u202e<!-- x -->\n" + "y" * 400
    head = w.forge.seed_commit({"tex/main.tex": b"evil"}, parents=[w.c3], message="evil")
    tip = w.forge.seed_commit({"tex/main.tex": b"tip"}, parents=[w.c3], message="squash evil")
    w.forge.seed_pull_request(files=[], head_branch="task/9", head_sha=head, merged=True,
                              merge_commit_sha=tip, author="carla", title=title, commits=[head])
    reports = write_reports(tmp_path / "reports", [
        {"path": "tex/a.tex", "line": 1, "severity": "error", "code": "E_VALIDATION_LABEL",
         "message": "ver @someone y [x](https://evil.invalid) `code`", "pointer": None, "hint": None},
    ])
    _code, out = run(w, tip, build="failure", reports=reports)
    body = w.forge.get_issue(int(out["issue"])).body  # type: ignore[union-attr]
    assert "@evil" not in body and "@someone" not in body
    assert "@\u200bevil" in body and "@\u200bsomeone" in body
    assert "\u202e" not in body
    line = next(ln for ln in body.splitlines() if "evil ``rm" in ln)
    assert line.count("y") <= br.MAX_TITLE and "…" in line
    assert line.split(" ", 2)[2].startswith("```")  # el título va en código con una valla más larga
    # El mensaje (con un acento grave) va en código con valla doble: el enlace no se interpreta.
    line = next(ln for ln in body.splitlines() if "someone" in ln)
    assert line.endswith(": `` ver @\u200bsomeone y [x](https://evil.invalid) `code` ``")


# ---------------------------------------------------------------------------
# Salidas: sin sobre, nunca 2
# ---------------------------------------------------------------------------


def _no_envelope(out: str) -> None:
    assert _meta.RESULT_SCHEMA not in out and not out.lstrip().startswith("{")


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("missing", 3),
        ("bad_sha", 1),
        ("short_sha", 1),
        ("bad_build", 1),
        ("bad_validate", 1),
        ("bad_run_url", 1),
        ("bad_pdf_url", 1),
        ("policy_not_dir", 1),
        ("list_issues_5xx", 1),
        ("create_issue_403", 1),
        ("comment_timeout", 1),
        ("internal", 1),
        ("no_forge", 1),
    ],
)
def test_error_exits_are_1_or_3_never_2_and_without_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], case: str, expected: int
) -> None:
    w = make_world(tmp_path)
    argv = argv_for(w.policy, w.c1, build="failure")
    forge: FakeForge | None = w.forge
    if case == "missing":
        argv = ["--policy", str(w.policy), "--build", "failure"]
    elif case == "bad_sha":
        argv[3] = "HEAD"
    elif case == "short_sha":
        argv[3] = w.c1[:7]
    elif case == "bad_build":
        argv[5] = "broken"
    elif case == "bad_validate":
        argv[7] = "ok"
    elif case == "bad_run_url":
        argv[11] = "javascript:alert(1)"
    elif case == "bad_pdf_url":
        argv[9] = "https://x.invalid/a b"
    elif case == "policy_not_dir":
        argv[1] = str(tmp_path / "no-existe")
    elif case == "list_issues_5xx":
        w.forge.faults.add(Fault("list_issues", "http_5xx"))
    elif case == "create_issue_403":
        w.forge.faults.add(Fault("create_issue", "http_403"))
    elif case == "comment_timeout":
        w.forge.seed_issue(title="a", body=MARKER, labels=[br.LABEL])
        w.forge.faults.add(Fault("create_comment", "timeout_before"))
    elif case == "internal":
        def boom(*_a: object, **_k: object) -> object:
            raise RuntimeError("secreto interno")
        monkeypatch.setattr(br, "find_suspects", boom)
    elif case == "no_forge":
        forge = None  # sin adaptador de H3 ni FakeForge: OpError de context_factory
    code, out = invoke(argv, forge, cwd=tmp_path)
    err = capsys.readouterr().err
    assert code == expected and code != 2
    assert out == ""
    _no_envelope(out)
    assert err.strip() and "secreto interno" not in err and "Traceback" not in err
    if case in ("list_issues_5xx", "bad_sha", "missing", "policy_not_dir", "no_forge"):
        assert w.forge.calls.count(method="create_issue") == 0
    assert "base_report." not in err


def test_success_output_is_key_value_only(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    code, out = invoke(argv_for(w.policy, w.c1, build="failure"), w.forge)
    assert code == 0
    _no_envelope(out)
    assert [ln.split("=", 1)[0] for ln in out.splitlines()] == ["status", "action", "issue"]


def test_timeout_after_on_comment_is_recovered_by_the_op_marker(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    issue = w.forge.seed_issue(title="a", body=MARKER, labels=[br.LABEL])
    w.forge.faults.add(Fault("create_comment", "timeout_after"))
    code, out = run(w, w.c1, build="failure")
    assert code == 0 and out["action"] == "commented"
    assert len(w.forge.list_comments(issue.number)) == 1


# ---------------------------------------------------------------------------
# Resumen del paso, catálogos y estado del FakeForge por archivo
# ---------------------------------------------------------------------------


def test_summary_is_appended(tmp_path: Path) -> None:
    w = make_world(tmp_path)
    summary = tmp_path / "summary.md"
    summary.write_text("previo\n", encoding="utf-8")
    _code, out = run(w, w.c1, build="failure", summary=summary)
    text = summary.read_text(encoding="utf-8")
    assert text.startswith("previo\n")
    assert f"### {_meta.NAME}/base-report" in text
    assert f"Base rota en `{w.c1}`: se abrió el Issue #{out['issue']}." in text
    run(w, w.c2, summary=summary)
    assert f"Base en verde en `{w.c2}`: se cerró #{out['issue']}." in summary.read_text(encoding="utf-8")


def test_unwritable_summary_is_a_warning(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    w = make_world(tmp_path)
    code, out = run(w, w.c3, build="cancelled", summary=tmp_path)  # un directorio
    assert code == 0 and out["status"] == "inconclusive"
    assert "no se pudo escribir el resumen" in capsys.readouterr().err


@pytest.mark.parametrize("language", ["es", "en"])
def test_every_rendered_text_comes_from_the_catalog(tmp_path: Path, language: str) -> None:
    from aucove.i18n import translator

    t = translator(language)  # type: ignore[arg-type]
    finding = br.ReportFinding("latex-log", "warning", None, None, None, "m")
    texts: list[str] = []
    for status in ("range", "no_green", "same", "compare_failed", "no_branch", "unavailable"):
        for branch in ("main", None):
            suspects = br.Suspects(status, "a" * 40 if status != "no_green" else None, (), True, 7)  # type: ignore[arg-type]
            for fallback, not_run in ((None, False), ("e" * 40, True)):
                report = br._Report("b" * 40, branch, RUN_URL, PDF_URL, (br.BUILD_CHECK,), False,
                                    (finding,) * 3, 30, suspects, fallback, not_run)
                texts += [br.render_issue_body(report, t), br.render_failure_comment(report, t)]
    texts.append(br.render_resolved_comment("c" * 40, RUN_URL, t))
    for status, action in (("broken", "created"), ("broken", "commented"), ("broken", "unchanged"),
                           ("green", "closed"), ("green", "none"), ("inconclusive", "none")):
        texts.append(br._summary_text(status, action, [4], "d" * 40, "", "success", t))  # type: ignore[arg-type]
    for text in texts:
        assert "base_report." not in text
        assert "{" not in text.replace(MARKER, "")  # ningún marcador sin valor


def test_catalog_has_the_same_keys_in_both_languages() -> None:
    es = {k for k in load_catalog("es") if k.startswith("base_report.")}
    en = {k for k in load_catalog("en") if k.startswith("base_report.")}
    assert es == en and es


def test_fake_state_file_through_context_factory(tmp_path: Path) -> None:
    # Así lo usa cli/ci.py: OpContext sin forja, FakeForge del archivo de estado y close().
    w = make_world(tmp_path)
    state = tmp_path / "fake-state.json"
    w.forge.save(state)
    env = {PREFIX + "TESTING": "1", PREFIX + "FORGE_ADAPTER": "fake", PREFIX + "FORGE_FAKE_STATE": str(state)}
    code, out = invoke(argv_for(w.policy, w.c1, build="failure"), None, env=env, cwd=tmp_path)
    assert code == 0 and "action=created" in out
    saved = FakeForge.load(state)
    assert len(open_marked(saved)) == 1
