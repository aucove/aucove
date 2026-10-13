# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Motor de ``validate`` y reglas ``secrets`` y ``ci-policy``.

Cada prueba reproduce un defecto ya corregido:

1. ``secrets``: gitleaks recibía de ``git log -p`` solo "Binary files ... differ" para los
   archivos que git trata como binarios (atributo ``binary`` o ``-diff`` del
   ``.gitattributes`` del PR, o un NUL en los primeros 8000 bytes), así que un secreto
   en ellos nunca se informaba (REQ-SEC-36). Ahora el diff se fuerza a texto.
2. ``plan-ids`` en modo ``tree`` comparaba con la punta de ``origin/<base>`` y no con el
   merge-base (09-ci §4.1): una rama atrasada daba ``E_VALIDATION_PLAN_ID_REMOVED``.
3. Con ``--pin-report`` se perdían las comprobaciones de ``roles-sync`` y ``schemas`` que
   no dependen de la versión del núcleo y que solo existen en modo ``pr``:
   ``codeowners/errors?ref=<head>`` y la coincidencia registro↔rama ``task/<n>``.
4. Modo ``base`` en un clon superficial: un ``HEAD^1`` que no se resuelve se tomaba
   como commit raíz y las reglas entre versiones pasaban sin evaluar (REQ-CI-8).
5. ``ci-policy`` no veía datos del PR con ``toJSON``, con notación de índice ni
   ``github.ref_name``; tampoco ``toJSON(secrets)`` ni el checkout de
   ``merge_commit_sha`` en ``pull_request_target`` (REQ-CI-3, REQ-CI-7, REQ-CI-29).
6. Una ruta con salto de línea inyectaba comandos de flujo en ``--format github``
   (REQ-CI-26, REQ-CI-27).
7. ``--changed-only`` descartaba los hallazgos "no se pudo evaluar" con ruta y la regla
   quedaba en ``pass`` (REQ-CI-8).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aucove.forge.base import ChangedFile
from aucove.i18n import translator
from aucove.ops import validate
from aucove.ops.result import dumps, fail, make_error, ok
from aucove.ops.rules import ci_policy, secrets
from aucove.ops.rules.base import Change, RuleInput, RuleSpec, unavailable
from aucove.vcs.tree import git_tree
from tests.conftest import FIXTURES, GIT_OPTIONS, GitRepo
from tests.unit.test_rule_ci_policy import GOOD, PRT, VALIDATE_WF, WF, detail, messages
from tests.unit.test_rule_ci_policy import run as run_ci_policy
from tests.unit.test_validate_modes import by_check, clone, pr_forge, run_validate, seed_base, use_rules

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
FAKE_GITLEAKS = (sys.executable, str(FIXTURES / "tools" / "fake_gitleaks.py"))


def _leak(n: int) -> str:
    # El patrón del doble se arma por partes para que ningún escáner real lo vea aquí.
    return "FAKE" + "LEAK" + f"{n:016d}"


@pytest.fixture(autouse=True)
def _clean_fake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("FAKE_GITLEAKS_LOG", "FAKE_GITLEAKS_EXIT", "FAKE_GITLEAKS_REPORT", "FAKE_GITLEAKS_SLEEP",
                 "GITLEAKS_CONFIG", "GITLEAKS_CONFIG_TOML"):
        monkeypatch.delenv(name, raising=False)


# -- 1. secrets: archivos que git trata como binarios ------------------------------------


def test_secrets_scans_files_that_git_treats_as_binary(git_repo: GitRepo, tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    base = git_repo.commit("base", files={"project.yaml": "name: x\n"})
    git_repo.git("checkout", "-q", "-b", "task/3")
    head = git_repo.commit("pr", files={
        # el PR declara binarios sus propios archivos: gitleaks no debe dejar de verlos
        ".gitattributes": "*.tex -diff\n*.dat binary\n",
        "sections/methods/methods.tex": f"% {_leak(1)}\n",
        "data/raw.dat": f"clave = {_leak(2)}\n",
        # un NUL en los primeros 8000 bytes hace que git lo trate como binario
        "tables/data.csv": b"\0\n" + f"clave = {_leak(3)}\n".encode("ascii"),
    })
    log = tmp_path / "call.json"
    monkeypatch.setenv("FAKE_GITLEAKS_LOG", str(log))
    inp = RuleInput(mode="pr", t=translator("en"), now=NOW, tree=git_tree(git_repo.path, head),
                    base_tree=git_tree(git_repo.path, base), head_sha=head, tools={"gitleaks": FAKE_GITLEAKS})
    found = list(secrets.RULE.run(inp))
    assert {(f.code, f.path, f.line) for f in found} == {
        ("E_VALIDATION_SECRET", "data/raw.dat", 1),
        ("E_VALIDATION_SECRET", "tables/data.csv", 2),
        ("E_VALIDATION_SECRET", "sections/methods/methods.tex", 1),
    }
    argv = json.loads(log.read_text(encoding="utf-8"))["argv"]
    (opts,) = [a for a in argv if a.startswith("--log-opts=")]
    assert opts.split("=", 1)[1].split(" ") == ["--text", "--no-textconv", "--no-ext-diff",
                                                "--diff-merges=first-parent", f"{base}..{head}"]


# -- 2. plan-ids en modo tree contra el merge-base ---------------------------------------


PLAN_1 = "# Plan\n\n- RQ-1: primera\n"
PLAN_2 = "# Plan\n\n- RQ-1: primera\n- RQ-2: segunda\n"


def test_tree_mode_plan_ids_compares_with_the_merge_base(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    use_rules(monkeypatch, real=["ops.rules.plan_ids"])
    seed_base(git_repo, {"plan.md": PLAN_1})
    git_repo.git("checkout", "-q", "-b", "task/3")
    git_repo.commit("methods", files={"sections/methods/methods.tex": "% v2\n"})
    git_repo.checkout("main")
    git_repo.commit("otro PR integra RQ-2", files={"plan.md": PLAN_2})
    git_repo.push("main")
    git_repo.fetch()
    git_repo.checkout("task/3")
    behind = run_validate(git_repo.path, git_repo.env, checks=["plan-ids"])
    assert by_check(behind)["plan-ids"] == {"check": "plan-ids", "status": "pass", "findings": []}
    assert behind.ok, behind.to_dict()
    # la regla sigue viendo un retiro real respecto del merge-base
    git_repo.commit("quita RQ-1", files={"plan.md": "# Plan\n"})
    removed = run_validate(git_repo.path, git_repo.env, checks=["plan-ids"])
    findings: list[dict[str, object]] = by_check(removed)["plan-ids"]["findings"]  # type: ignore[assignment]
    assert [(f["code"], f["path"]) for f in findings] == [("E_VALIDATION_PLAN_ID_REMOVED", "plan.md")]
    assert "RQ-1" in str(findings[0]["message"])


# -- 3. --pin-report conserva lo que no depende de la versión ----------------------------


DECISION_3 = "memory/decisions/2026-10-01-3-usar-latex.md"
DECISION_4 = "memory/decisions/2026-10-01-4-usar-latex.md"


def _pin_report(path: Path) -> Path:
    results = [{"check": c, "status": "pass", "findings": []} for c in ("generated-sync", "roles-sync", "schemas")]
    data = {
        "valid": True, "mode": "tree", "pr": None,
        "base": {"ref": "origin/main", "sha": None}, "head": {"ref": "HEAD", "sha": None},
        "results": results, "page": {"next_cursor": None, "returned": 0, "total": 0},
    }
    path.write_bytes(dumps(ok("validate", data)))
    return path


def test_pin_report_keeps_version_independent_checks_in_pr_mode(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch,
                                                                tmp_path: Path) -> None:
    use_rules(monkeypatch, real=["ops.rules.schemas", "ops.rules.roles_sync"])
    seed_base(git_repo)
    git_repo.git("checkout", "-q", "-b", "task/3")
    head = git_repo.commit("pr", files={DECISION_3: "x\n", DECISION_4: "x\n"})
    git_repo.push("task/3")
    clone(git_repo, tmp_path / "base")
    clone(git_repo, tmp_path / "pr", at=head)
    pin = _pin_report(tmp_path / "pin.json")
    kwargs = dict(mode="pr", pr=42, policy="base", tree="pr", pin_report=str(pin), checks=["roles-sync", "schemas"])

    # control: sin errores de CODEOWNERS y con el registro de la tarea 3 en task/3, todo pasa
    clean = pr_forge(head, files=[ChangedFile(DECISION_3, "added")])
    result = run_validate(tmp_path, git_repo.env, forge=clean, lang="en", **kwargs)
    assert result.ok, result.to_dict()

    forge = pr_forge(head, files=[ChangedFile(DECISION_3, "added"), ChangedFile(DECISION_4, "added")])
    forge.set_codeowners_errors(head, ["Unknown owner on line 3: @nadie"])
    result = run_validate(tmp_path, git_repo.env, forge=forge, lang="en", **kwargs)
    checks = by_check(result)
    roles = checks["roles-sync"]
    assert roles["status"] == "fail"
    assert [(f["code"], f["path"]) for f in roles["findings"]] == [  # type: ignore[index,union-attr]
        ("E_VALIDATION_ROLES_OUT_OF_SYNC", ".github/CODEOWNERS")
    ]
    assert "@nadie" in roles["findings"][0]["message"]  # type: ignore[index]
    schemas = checks["schemas"]
    assert schemas["status"] == "fail"
    assert [(f["code"], f["path"]) for f in schemas["findings"]] == [  # type: ignore[index,union-attr]
        ("E_VALIDATION_DECISION_NAME", DECISION_4)
    ]
    assert "task/3" in schemas["findings"][0]["message"]  # type: ignore[index]


def test_pin_supplement_never_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Un fallo del complemento falla cerrado, como una regla que lanza (REQ-CI-8)."""
    from aucove.ops.rules import schemas

    def boom(_inp: RuleInput) -> list[object]:
        raise RuntimeError("secreto interno")

    monkeypatch.setattr(schemas, "branch_findings", boom)
    spec = RuleSpec(id="schemas", modes=frozenset({"tree", "pr", "base"}), run=lambda _i: [], codes={})
    pin = validate.read_pin_report(_pin_report(tmp_path / "pin.json"))
    inp = RuleInput(mode="pr", t=translator("en"), now=NOW, head_ref="task/3")
    (outcome,) = validate.evaluate([spec], inp, pin=pin)
    assert outcome.status == "fail"
    assert [f.code for f in outcome.findings] == ["E_INTERNAL_UNEXPECTED"]
    assert "secreto interno" not in outcome.findings[0].message


# -- 4. modo base en un clon superficial ---------------------------------------------------


DECISION = "memory/decisions/2026-09-01-1-algo.md"


def test_base_mode_in_a_shallow_clone_fails_closed(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch,
                                                   tmp_path: Path) -> None:
    use_rules(monkeypatch, real=["ops.rules.decisions", "ops.rules.plan_ids"])
    seed_base(git_repo, {DECISION: "x\n", "plan.md": PLAN_2})
    git_repo.commit("edita la decisión y borra RQ-2", files={DECISION: "y\n", "plan.md": PLAN_1})
    git_repo.push("HEAD:refs/heads/main")
    full = run_validate(git_repo.path, git_repo.env, mode="base", policy=str(git_repo.path))
    assert {c: r["status"] for c, r in by_check(full).items()} == {"decisions-immutable": "fail", "plan-ids": "fail"}

    shallow = tmp_path / "shallow"
    subprocess.run(["git", *GIT_OPTIONS, "clone", "-q", "--depth", "1", git_repo.origin.as_uri(), str(shallow)],
                   cwd=tmp_path, env=dict(git_repo.env), capture_output=True, check=True)
    assert (shallow / ".git" / "shallow").is_file()
    result = run_validate(shallow, git_repo.env, mode="base", policy=str(shallow))
    assert result.error is not None and result.error.code == "E_VALIDATION_INPUT", result.to_dict()
    assert result.error.details == {"errors": [{"param": "before", "reason": "parent_unavailable"}]}


# -- 5. ci-policy: formas de expresión que no se veían -------------------------------------


@pytest.mark.parametrize(
    "expr",
    [
        "${{ toJSON(github.event.pull_request) }}",
        "${{ toJSON(github.event.pull_request.head) }}",
        "${{ github.event['pull_request']['title'] }}",
        '${{ github.event["pull_request"]["body"] }}',
        "${{ github.event.pull_request['head']['ref'] }}",
        "${{ github['head_ref'] }}",
        "${{ github.ref_name }}",
        "${{ github.ref }}",
        "${{ toJSON(github) }}",
        "${{ github.event[format('pull_{0}', 'request')] }}",
        "${{ github.event.changes.title.from }}",
    ],
)
def test_ci_policy_sees_untrusted_data_in_any_notation(expr: str) -> None:
    (finding,) = run_ci_policy({WF: GOOD.replace("run: echo ok", f"run: echo '{expr}'")})
    assert detail("injection", expr=expr) in finding.message


@pytest.mark.parametrize(
    "expr",
    [
        "${{ github.event.pull_request.number }}",
        "${{ github.event.pull_request.head.sha }}",
        "${{ github.event['pull_request']['head']['sha'] }}",
        "${{ github.event_name }}",
        "${{ steps.secrets.outputs.ok }}",
        "${{ github.repository }}",
    ],
)
def test_ci_policy_still_accepts_trusted_data(expr: str) -> None:
    assert run_ci_policy({WF: GOOD.replace("run: echo ok", f"run: echo '{expr}'")}) == []


@pytest.mark.parametrize("expr", ["${{ toJSON(secrets) }}", "${{ secrets['TOKEN'] }}", "${{ format('{0}', secrets) }}"])
def test_ci_policy_sees_secrets_as_an_identifier(expr: str) -> None:
    text = GOOD.replace("      - run: echo ok\n", f"      - run: echo ok\n        env: {{ T: \"{expr}\" }}\n")
    (finding,) = run_ci_policy({WF: text})
    assert detail("secrets") in finding.message


def test_ci_policy_sees_secrets_in_a_bare_if() -> None:
    """``if:`` siempre es una expresión, aunque no lleve ``${{ }}``."""
    text = GOOD.replace("      - run: echo ok\n", "      - run: echo ok\n        if: secrets.TOKEN != ''\n")
    (finding,) = run_ci_policy({WF: text})
    assert detail("secrets") in finding.message
    plain = GOOD.replace("      - run: echo ok\n", "      - run: echo ok\n        if: github.event_name == 'push'\n")
    assert run_ci_policy({WF: plain}) == []


@pytest.mark.parametrize(
    "ref",
    [
        "${{ github.event.pull_request.merge_commit_sha }}",
        "${{ github.event['pull_request']['head']['sha'] }}",
        "${{ github.event.after }}",
        "${{ toJSON(github.event.pull_request) }}",
    ],
)
def test_ci_policy_sees_any_checkout_of_pr_content_in_pull_request_target(ref: str) -> None:
    text = PRT.replace("${{ github.event.pull_request.head.sha }}", ref)
    assert run_ci_policy({VALIDATE_WF: text}) == []  # con path propio y condición de mismo repositorio
    bare = text.replace("          path: pr\n", "").replace(
        "        if: github.event.pull_request.head.repo.full_name == github.repository\n", "")
    found = messages(run_ci_policy({VALIDATE_WF: bare}))
    assert detail("prt_checkout_path", job="validate") in found
    assert detail("prt_checkout_same_repo", job="validate") in found


def test_ci_policy_same_repo_condition_in_index_notation() -> None:
    text = PRT.replace(
        "github.event.pull_request.head.repo.full_name == github.repository",
        "github.event['pull_request']['head']['repo']['full_name'] == github['repository']",
    )
    assert run_ci_policy({VALIDATE_WF: text}) == []
    # Un ref no literal es del PR aunque nombre la base, porque el autor del PR elige su
    # base entre todas las ramas (también una task/<n>). La política sale de un checkout sin ref.
    base_checkout = PRT.replace("${{ github.event.pull_request.head.sha }}", "${{ github.event.pull_request.base.sha }}")
    assert run_ci_policy({VALIDATE_WF: base_checkout}) == []  # con path propio y mismo repositorio
    in_root = base_checkout.replace("          path: pr\n", "")
    assert messages(run_ci_policy({VALIDATE_WF: in_root})) == "CI policy: " + detail("prt_checkout_path", job="validate") + "."


# -- 6. --format github: sin comandos de flujo inyectados ---------------------------------


def _result_with_path(path: str) -> object:
    t = translator("en")
    finding = {"path": path, "line": None, "severity": "error", "code": "conv.path.invalid-name",
               "message": "The segment is not valid\n::warning::otra", "hint": "Rename it\n::add-mask::x",
               "pointer": None}
    data = {
        "valid": False, "mode": "base", "pr": None,
        "base": {"ref": "HEAD^1", "sha": None}, "head": {"ref": "HEAD", "sha": None},
        "results": [{"check": "convention", "status": "fail", "findings": [finding]}],
        "page": {"next_cursor": None, "returned": 1, "total": 1},
    }
    return fail("validate", make_error("E_VALIDATION_FAILED", t), data=data)


@pytest.mark.parametrize("fmt", ["github", "text"])
def test_render_text_neutralizes_control_characters(fmt: str) -> None:
    t = translator("en")
    result = _result_with_path("tex/x\n::warning title=pwn::INJECTED\r::stop-commands::tok\x1b[2J")
    out = validate.render_text(result, t, fmt=fmt)  # type: ignore[arg-type]
    lines = out.split("\n")
    commands = [line for line in lines if line.lstrip().startswith("::")]
    if fmt == "github":
        assert len(commands) == 1 and commands[0].startswith("::error file=tex/x%0A%3A%3Awarning")
    else:
        assert commands == []
    assert not any(ch in out for ch in "\r\x1b")
    assert "tex/x\\x0a::warning title=pwn::INJECTED\\x0d::stop-commands::tok\\x1b[2J" in out


def test_render_text_keeps_ordinary_paths() -> None:
    t = translator("en")
    out = validate.render_text(_result_with_path("sections/a b.tex"), t)  # type: ignore[arg-type]
    assert " sections/a b.tex: " in out


# -- 7. --changed-only no convierte "no evaluada" en pass ----------------------------------


def test_changed_only_keeps_unavailable_findings_with_a_path() -> None:
    spec = RuleSpec(id="citations", modes=frozenset({"tree"}), run=lambda _i: [], codes={})
    inp = RuleInput(mode="tree", t=translator("en"), now=NOW)
    unreadable = unavailable(spec, inp, "bib/references.bib supera 5 MB", path="bib/references.bib")
    outcomes = (validate.RuleOutcome("citations", "fail", (unreadable,)),)
    changes = (Change(path="sections/methods/methods.tex", status="modified"),)
    (kept,) = validate.only_changed(outcomes, changes)
    assert kept.status == "fail" and kept.findings == (unreadable,)
