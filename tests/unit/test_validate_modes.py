# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Entradas de ``validate`` en los cuatro modos sobre repositorios git temporales.

05-operaciones §9.5, 09-ci §4.1 (REQ-CI-1, REQ-CI-24..26) y D-33. Las
reglas son sondas que registran su ``RuleInput`` (salvo donde se usan las reales), así
que las pruebas no dependen de las reglas reales.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import types
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from aucove import _meta
from aucove.cli import main as cli_main
from aucove.config.schemas import validate_result
from aucove.domain.findings import Finding
from aucove.forge.base import ChangedFile, RepoRef
from aucove.forge.fake import FakeForge
from aucove.ops import validate
from aucove.ops.context import Invocation, OpContext
from aucove.ops.registry import REGISTRY, invoke
from aucove.ops.result import Result, dumps
from aucove.ops.rules.base import RuleInput, RuleSpec, make_finding
from tests.conftest import FIXTURES, GIT_OPTIONS, GitRepo, roles_fixture

SPEC = next(s for s in REGISTRY if s.id == "validate")
PROJECT = (FIXTURES / "config" / "project" / "minimal.yaml").read_text(encoding="utf-8")
ROLES = roles_fixture("basic")
PREFIX = _meta.ENV_PREFIX


def git(cwd: Path, env: Mapping[str, str], *args: str, input: bytes | None = None) -> str:
    proc = subprocess.run(["git", *GIT_OPTIONS, *args], cwd=cwd, env=dict(env), input=input,
                          capture_output=True, check=True)
    return proc.stdout.decode("utf-8").strip()


def add_symlink_entry(repo: GitRepo, path: str, target: str, message: str = "link") -> str:
    """Commit con una entrada 120000 en el índice, sin crear el enlace en disco."""
    blob = git(repo.path, repo.env, "hash-object", "-w", "--stdin", input=target.encode("utf-8"))
    repo.git("update-index", "--add", "--cacheinfo", f"120000,{blob},{path}")
    repo.git("commit", "-q", "--no-verify", "-m", message)
    return repo.head()


# -- sondas ---------------------------------------------------------------------------


class Probe:
    """Reglas que registran su entrada y devuelven los hallazgos que se les pidan."""

    CODES = {"E_VALIDATION_STUB": frozenset({"n"})}

    def __init__(self) -> None:
        self.inputs: dict[str, RuleInput] = {}

    def spec(self, rule_id: str, modes: set[str], *, tools: tuple[str, ...] = (), after: tuple[str, ...] = (),
             findings: Callable[[RuleSpec, RuleInput], Sequence[Finding]] | None = None) -> RuleSpec:
        holder: list[RuleSpec] = []

        def run(inp: RuleInput) -> Sequence[Finding]:
            self.inputs[rule_id] = inp
            return findings(holder[0], inp) if findings else []

        spec = RuleSpec(id=rule_id, modes=frozenset(modes), run=run, codes=self.CODES, tools=tools,  # type: ignore[arg-type]
                        after=after)
        holder.append(spec)
        return spec


def use_rules(monkeypatch: pytest.MonkeyPatch, *specs: RuleSpec, real: Sequence[str] = ()) -> None:
    modules: list[str] = []
    if specs:
        name = "_p15_modes_probe"
        module = types.ModuleType(f"{_meta.NAME}.{name}")
        module.RULES = tuple(specs)  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, f"{_meta.NAME}.{name}", module)
        modules.append(name)
    modules.extend(real)
    monkeypatch.setattr(validate, "RULE_MODULES", tuple(modules))


def default_probe(monkeypatch: pytest.MonkeyPatch, *, real: Sequence[str] = ()) -> Probe:
    probe = Probe()
    use_rules(
        monkeypatch,
        probe.spec("schemas", {"tree", "pr", "base"}),
        probe.spec("secrets", {"pr"}, tools=("gitleaks",)),
        probe.spec("latex-log", {"build-log"}),
        real=real,
    )
    return probe


def run_validate(cwd: Path, env: Mapping[str, str], *, forge: FakeForge | None = None, lang: str = "es",
                 now: Callable[[], datetime] | None = None, **kwargs: object) -> Result:
    inv = Invocation(root=None, lang=lang, json=True, dry_run=False, surface="cli", env=dict(env), cwd=str(cwd),
                     **({"now": now} if now else {}))
    result = invoke(SPEC, OpContext(inv, forge=forge), **kwargs)
    assert validate_result(result.to_dict()) == [], result.to_dict()
    return result


def seed_base(repo: GitRepo, extra: Mapping[str, str | bytes] | None = None) -> str:
    files: dict[str, str | bytes] = {
        "project.yaml": PROJECT,
        "roles.yaml": ROLES,
        "sections/methods/methods.tex": "% methods\n",
        "sections/results/results.tex": "% results\n",
    }
    files.update(extra or {})
    sha = repo.commit("base", files=files)
    repo.push("HEAD:refs/heads/main")
    repo.fetch()
    return sha


def clone(repo: GitRepo, dest: Path, *, at: str | None = None) -> Path:
    git(dest.parent, repo.env, "clone", "-q", str(repo.origin), str(dest))
    if at is not None:
        git(dest, repo.env, "checkout", "-q", "--detach", at)
    return dest


def by_check(result: Result) -> dict[str, dict[str, object]]:
    assert result.data is not None
    return {r["check"]: r for r in result.data["results"]}  # type: ignore[index]


# -- modo tree ------------------------------------------------------------------------


def test_tree_mode_uses_worktree_and_origin_base(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = default_probe(monkeypatch)
    base = seed_base(git_repo)
    git_repo.git("checkout", "-q", "-b", "task/3")
    git_repo.write({"sections/methods/methods.tex": "% methods v2\n", "notes.txt": "nuevo\n"})
    (git_repo.path / "sections/results/results.tex").unlink()
    result = run_validate(git_repo.path, git_repo.env)
    assert result.ok, result.to_dict()
    data = result.data
    assert data is not None
    assert data["mode"] == "tree" and data["pr"] is None
    assert data["base"] == {"ref": "origin/main", "sha": base}
    assert data["head"] == {"ref": "task/3", "sha": base}
    assert [r["check"] for r in data["results"]] == ["schemas"]
    inp = probe.inputs["schemas"]
    assert inp.tree is not None and inp.tree.root == git_repo.path
    assert inp.base_tree is not None and inp.base_tree.sha == base
    assert inp.policy is not None and inp.policy.roles.lead == "ana"
    changes = {(c.path, c.status) for c in inp.changes}
    assert changes == {
        ("sections/methods/methods.tex", "modified"),
        ("notes.txt", "added"),
        ("sections/results/results.tex", "removed"),
    }
    assert inp.tools == {} and inp.forge is None and inp.head_ref == "task/3"


def test_tree_mode_with_explicit_tree_on_a_full_clone(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch,
                                                      tmp_path: Path) -> None:
    """Como el job pin: ``--tree pr`` relativo al cwd y la política de ``origin/<base>`` del clon."""
    probe = default_probe(monkeypatch)
    merge_base = seed_base(git_repo)
    git_repo.git("checkout", "-q", "-b", "task/3")
    head = git_repo.commit("pr", files={"roles.yaml": ROLES.replace("lead: ana", "lead: bruno")})
    git_repo.push("task/3")
    git_repo.checkout("main")
    main2 = git_repo.commit("main avanza", files={"README.md": "x\n"})
    git_repo.push("main")
    work = tmp_path / "work"
    work.mkdir()
    clone(git_repo, work / "pr", at=head)
    report = work / "out" / "pin.json"
    result = run_validate(work, git_repo.env, tree="pr", checks=["schemas"], report=str(report))
    assert result.ok
    assert report.read_bytes() == dumps(result)  # --report escribe el mismo sobre
    inp = probe.inputs["schemas"]
    assert inp.policy is not None and inp.policy.roles.lead == "ana"  # de origin/main, no del árbol
    # el estado anterior es el merge-base, no la punta de origin/main (09-ci §4.1, como en pr)
    assert inp.base_tree is not None and inp.base_tree.sha == merge_base != main2
    assert result.data is not None and result.data["base"]["sha"] == main2  # type: ignore[index]
    assert inp.tree is not None and inp.tree.root == work / "pr"
    assert inp.tree.read_text("roles.yaml") is not None and "lead: bruno" in inp.tree.read_text("roles.yaml")  # type: ignore[operator]
    assert {c.path for c in inp.changes} == {"roles.yaml"}  # merge-base..árbol de trabajo
    assert result.data is not None and result.data["head"]["sha"] == head  # type: ignore[index]


def test_tree_mode_with_head_validates_the_commit(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = default_probe(monkeypatch)
    seed_base(git_repo)
    head = git_repo.commit("c2", files={"sections/methods/methods.tex": "% v2\n"})
    git_repo.write({"sections/methods/methods.tex": "% sin commit\n"})
    result = run_validate(git_repo.path, git_repo.env, head="HEAD")
    assert result.ok
    inp = probe.inputs["schemas"]
    assert inp.tree is not None and inp.tree.sha == head
    assert inp.tree.read_text("sections/methods/methods.tex") == "% v2\n"
    bad = run_validate(git_repo.path, git_repo.env, head="no-such-ref")
    assert bad.error is not None and bad.error.code == "E_VALIDATION_INPUT"


def test_tree_mode_without_remote_base_fails(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    default_probe(monkeypatch)
    git_repo.commit("base", files={"project.yaml": PROJECT, "roles.yaml": ROLES})  # sin push ni fetch
    result = run_validate(git_repo.path, git_repo.env)
    assert result.error is not None and result.error.code == "E_CONFIG_POLICY_UNAVAILABLE"
    assert result.exit_code() == 8


def test_tree_mode_warns_about_a_stale_base(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    default_probe(monkeypatch)
    seed_base(git_repo)
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    result = run_validate(git_repo.path, git_repo.env, now=lambda: later)
    assert result.ok
    assert [w.code for w in result.warnings] == ["W_NET_STALE_CACHE"]
    fresh = run_validate(git_repo.path, git_repo.env)
    assert fresh.warnings == ()


def test_unknown_check_is_input_error_before_reading_anything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                              git_env: dict[str, str]) -> None:
    default_probe(monkeypatch)
    report = tmp_path / "r.json"
    result = run_validate(tmp_path, git_env, checks=["schemas", "nope"], report=str(report))
    assert result.error is not None and result.error.code == "E_VALIDATION_INPUT"
    assert result.exit_code() == 3
    assert report.read_bytes() == dumps(result)  # el sobre se escribe también al fallar


def test_unwritable_report_fails_closed(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    default_probe(monkeypatch)
    seed_base(git_repo)
    target = tmp_path / "taken"
    target.mkdir()
    (target / "x").write_text("x", encoding="utf-8")
    result = run_validate(git_repo.path, git_repo.env, report=str(target))
    assert result.ok is False and result.error is not None
    assert result.error.code == "E_INTERNAL_UNEXPECTED" and result.error.details["reason"] == "report_unwritable"  # type: ignore[index]
    assert result.data is not None  # data completa


def test_tree_mode_with_real_rules_produces_a_valid_envelope(git_repo: GitRepo) -> None:
    seed_base(git_repo)
    result = run_validate(git_repo.path, git_repo.env)  # todas las reglas reales del modo tree
    assert result.data is not None
    checks = [r["check"] for r in result.data["results"]]
    assert checks[:1] == ["schemas"] and "ci-policy" in checks
    assert "scope" not in checks and "secrets" not in checks and "decisions-immutable" not in checks
    assert by_check(result)["schemas"]["status"] == "pass", by_check(result)["schemas"]


# -- modo pr --------------------------------------------------------------------------


def pr_scenario(git_repo: GitRepo, tmp_path: Path) -> tuple[Path, Path, str, str, str]:
    """Base y PR (task/3) con una entrada 120000; devuelve (base_dir, pr_dir, merge_base, head, main_tip)."""
    merge_base = seed_base(git_repo)
    git_repo.git("checkout", "-q", "-b", "task/3")
    git_repo.commit("pr", files={"sections/methods/methods.tex": "% v2\n", "sections/methods/new.tex": "% n\n"},
                    remove=["sections/results/results.tex"])
    head = add_symlink_entry(git_repo, "sections/methods/link.tex", "../../../roles.yaml")
    git_repo.push("task/3")
    git_repo.checkout("main")
    tip = git_repo.commit("main avanza", files={"README.md": "x\n"})
    git_repo.push("main")
    base_dir = clone(git_repo, tmp_path / "base")
    pr_dir = clone(git_repo, tmp_path / "pr", at=head)
    return base_dir, pr_dir, merge_base, head, tip


def pr_forge(head: str, *, default_branch: str = "main", files: Sequence[ChangedFile] | None = None) -> FakeForge:
    forge = FakeForge(RepoRef("example-org", "demo"), viewer="github-actions[bot]", default_branch=default_branch)
    forge.seed_pull_request(
        number=42, head_branch="task/3", head_sha=head, base_branch="main", author="carla", title="PR",
        files=list(files) if files is not None else [
            ChangedFile("sections/methods/methods.tex", "modified"),
            ChangedFile("sections/methods/new.tex", "added"),
            ChangedFile("sections/methods/link.tex", "added"),
            ChangedFile("sections/results/results.tex", "removed"),
        ],
    )
    return forge


def test_pr_mode_reads_policy_tree_and_api_files(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch,
                                                 tmp_path: Path) -> None:
    probe = default_probe(monkeypatch)
    base_dir, pr_dir, merge_base, head, tip = pr_scenario(git_repo, tmp_path)
    forge = pr_forge(head)
    result = run_validate(tmp_path, git_repo.env, forge=forge, mode="pr", pr=42, policy="base", tree="pr")
    data = result.data
    assert data is not None and data["mode"] == "pr" and data["pr"] == 42
    assert data["base"] == {"ref": "main", "sha": tip} and data["head"] == {"ref": "task/3", "sha": head}
    inp = probe.inputs["schemas"]
    assert inp.policy is not None and inp.policy.ref == base_dir.as_posix()
    assert inp.tree is not None and inp.tree.sha == head and inp.tree.root == pr_dir
    assert inp.base_tree is not None and inp.base_tree.sha == merge_base
    assert inp.pr is not None and inp.pr.number == 42 and inp.forge is forge
    modes = {c.path: (c.status, c.file_mode) for c in inp.changes}
    assert modes == {
        "sections/methods/methods.tex": ("modified", "100644"),
        "sections/methods/new.tex": ("added", "100644"),
        "sections/methods/link.tex": ("added", "120000"),  # modo de git ls-tree (REQ-CI-24)
        "sections/results/results.tex": ("removed", None),
    }
    assert inp.tree.read_bytes("sections/methods/link.tex") is None  # nunca se lee un enlace


def test_pr_mode_api_statuses_are_normalized(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    probe = default_probe(monkeypatch)
    _base, _pr, _mb, head, _tip = pr_scenario(git_repo, tmp_path)
    files = [ChangedFile("a.tex", "copied", "b.tex"), ChangedFile("c.tex", "changed"),
             ChangedFile("d.tex", "renamed", "e.tex"), ChangedFile("f.tex", "unchanged")]
    run_validate(tmp_path, git_repo.env, forge=pr_forge(head, files=files), mode="pr", pr=42, policy="base", tree="pr")
    got = {c.path: (c.status, c.previous_path) for c in probe.inputs["schemas"].changes}
    assert got == {"a.tex": ("added", None), "c.tex": ("modified", None), "d.tex": ("renamed", "e.tex"),
                   "f.tex": ("modified", None)}


def test_pr_mode_without_tree_fails_content_rules(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch,
                                                  tmp_path: Path) -> None:
    """PR desde un fork: sin ``--tree`` las reglas de contenido fallan; decisions-immutable no lo necesita."""
    probe = Probe()
    use_rules(monkeypatch, probe.spec("schemas", {"tree", "pr", "base"}), real=["ops.rules.decisions"])
    _base, _pr, _mb, head, _tip = pr_scenario(git_repo, tmp_path)
    files = [ChangedFile("memory/decisions/2026-10-01-3-nueva.md", "added")]
    result = run_validate(tmp_path, git_repo.env, forge=pr_forge(head, files=files), mode="pr", pr=42, policy="base")
    assert result.ok is False and result.error is not None and result.error.code == "E_VALIDATION_FAILED"
    checks = by_check(result)
    assert checks["schemas"]["status"] == "fail"
    assert [f["code"] for f in checks["schemas"]["findings"]] == ["E_INTERNAL_UNEXPECTED"]  # type: ignore[union-attr]
    assert checks["decisions-immutable"]["status"] == "pass"
    assert probe.inputs["schemas"].tree is None and probe.inputs["schemas"].base_tree is None


def test_pr_mode_rejects_a_default_branch_other_than_base(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch,
                                                          tmp_path: Path) -> None:
    default_probe(monkeypatch)
    _base, _pr, _mb, head, _tip = pr_scenario(git_repo, tmp_path)
    report = tmp_path / "validate.json"
    result = run_validate(tmp_path, git_repo.env, forge=pr_forge(head, default_branch="develop"), mode="pr", pr=42,
                          policy="base", tree="pr", report=str(report))
    assert result.error is not None and result.error.code == "E_CONFIG_BASE_NOT_DEFAULT"  # REQ-CI-1
    assert result.error.details == {"base_branch": "main", "default_branch": "develop"}
    assert result.exit_code() == 8
    assert report.read_bytes() == dumps(result)


@pytest.mark.parametrize(
    ("kwargs", "param"),
    [({"pr": 42}, "policy"), ({"policy": "base"}, "pr"), ({"pr": 42, "policy": "nope"}, "policy"),
     ({"pr": 42, "policy": "base", "tree": "nope"}, "tree")],
)
def test_pr_mode_input_errors(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                              kwargs: dict[str, object], param: str) -> None:
    default_probe(monkeypatch)
    _base, _pr, _mb, head, _tip = pr_scenario(git_repo, tmp_path)
    result = run_validate(tmp_path, git_repo.env, forge=pr_forge(head), mode="pr", **kwargs)
    assert result.error is not None and result.error.code == "E_VALIDATION_INPUT"
    assert result.error.details["errors"][0]["param"] == param  # type: ignore[index]


def test_pr_mode_unknown_pr_and_head_missing_in_tree(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch,
                                                     tmp_path: Path) -> None:
    default_probe(monkeypatch)
    _base, _pr, _mb, head, _tip = pr_scenario(git_repo, tmp_path)
    missing = run_validate(tmp_path, git_repo.env, forge=pr_forge(head), mode="pr", pr=7, policy="base", tree="pr")
    assert missing.error is not None and missing.error.code == "E_FORGE_NOT_FOUND"
    other = "f" * 40
    forge = FakeForge(RepoRef("example-org", "demo"), viewer="github-actions[bot]")
    forge.seed_pull_request(number=42, head_branch="task/3", head_sha=other, base_branch="main", author="carla", files=[])
    stale = run_validate(tmp_path, git_repo.env, forge=forge, mode="pr", pr=42, policy="base", tree="pr")
    assert stale.error is not None and stale.error.code == "E_VALIDATION_INPUT"


def test_pr_mode_policy_fallback_warning(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    default_probe(monkeypatch)
    _base, _pr, _mb, head, _tip = pr_scenario(git_repo, tmp_path)
    git_repo.checkout("main")
    git_repo.commit("roles rotos", files={"roles.yaml": "version: [\n"})
    git_repo.push("main")
    broken = clone(git_repo, tmp_path / "base2")
    result = run_validate(tmp_path, git_repo.env, forge=pr_forge(head), mode="pr", pr=42, policy=broken.name, tree="pr")
    assert result.ok
    assert [w.code for w in result.warnings] == ["W_CONFIG_POLICY_FALLBACK"]
    # un solo archivo en respaldo: ``files`` tiene una sola clave, con el sha de esa versión
    assert result.warnings[0].details == {"sha": _mb, "files": {"roles.yaml": _mb}}


# -- W_CONFIG_POLICY_FALLBACK: el sha de cada archivo (06-permisos REQ-PERM-4) ---------

ROLES_V2 = roles_fixture("no-deputy")
BAD_ROLES = ROLES.replace("lead: ana", "lead: zoe")  # cumple el esquema y rompe REQ-SCHEMA-24
BAD_PROJECT = PROJECT.replace('core_pin: "0.1.0"', "core_pin: 0.1.0")  # core_pin deja de ser texto


def publish(repo: GitRepo, files: Mapping[str, str], message: str) -> str:
    sha = repo.commit(message, files=files)
    repo.push("HEAD:refs/heads/main")
    repo.fetch()
    return sha


def split_fallback(repo: GitRepo) -> tuple[str, str]:
    """c1 (ambos válidos), c2 (roles.yaml válido) y c3 (ambos inválidos); devuelve (c1, c2)."""
    assert BAD_ROLES != ROLES and BAD_PROJECT != PROJECT
    c1 = seed_base(repo)
    c2 = publish(repo, {"roles.yaml": ROLES_V2}, "c2")
    publish(repo, {"project.yaml": BAD_PROJECT, "roles.yaml": BAD_ROLES}, "c3")
    return c1, c2


def fallback_details(result: Result) -> object:
    found = [w for w in result.warnings if w.code == "W_CONFIG_POLICY_FALLBACK"]
    assert len(found) == 1, result.to_dict()
    return found[0].details


def test_fallback_details_keep_the_sha_of_each_file_in_tree_mode(git_repo: GitRepo,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """Cada archivo sale de un commit distinto: no se atribuye a los dos el sha de roles.yaml."""
    probe = default_probe(monkeypatch)
    c1, c2 = split_fallback(git_repo)
    git_repo.git("checkout", "-q", "-b", "task/3")
    result = run_validate(git_repo.path, git_repo.env)
    assert result.ok, result.to_dict()
    assert fallback_details(result) == {"sha": c2, "files": {"project.yaml": c1, "roles.yaml": c2}}
    policy = probe.inputs["schemas"].policy
    assert policy is not None and policy.roles.deputy is None  # roles.yaml de c2 (no-deputy)


def test_fallback_details_keep_the_sha_of_each_file_in_pr_mode(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch,
                                                               tmp_path: Path) -> None:
    default_probe(monkeypatch)
    c1, c2 = split_fallback(git_repo)
    git_repo.git("checkout", "-q", "-b", "task/3", c1)
    head = git_repo.commit("pr", files={"sections/methods/methods.tex": "% v2\n"})
    git_repo.push("task/3")
    clone(git_repo, tmp_path / "base")
    clone(git_repo, tmp_path / "pr", at=head)
    files = [ChangedFile("sections/methods/methods.tex", "modified")]
    result = run_validate(tmp_path, git_repo.env, forge=pr_forge(head, files=files), mode="pr", pr=42, policy="base",
                          tree="pr")
    assert result.ok, result.to_dict()
    assert fallback_details(result) == {"sha": c2, "files": {"project.yaml": c1, "roles.yaml": c2}}


def test_fallback_details_keep_the_sha_of_each_file_in_base_mode(git_repo: GitRepo,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    default_probe(monkeypatch)
    c1, c2 = split_fallback(git_repo)
    result = run_validate(git_repo.path, git_repo.env, mode="base", policy=str(git_repo.path))
    assert fallback_details(result) == {"sha": c2, "files": {"project.yaml": c1, "roles.yaml": c2}}


def test_fallback_details_with_only_project_yaml_in_tree_mode(git_repo: GitRepo,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    default_probe(monkeypatch)
    c1 = seed_base(git_repo)
    publish(git_repo, {"project.yaml": BAD_PROJECT}, "c2")
    result = run_validate(git_repo.path, git_repo.env)
    assert result.ok, result.to_dict()
    assert fallback_details(result) == {"sha": c1, "files": {"project.yaml": c1}}


def test_pr_mode_never_executes_from_the_tree(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch,
                                              tmp_path: Path) -> None:
    """REQ-CI-24: un ``git`` o ``gitleaks`` del árbol en el PATH no se usa y los enlaces no se leen."""
    probe = default_probe(monkeypatch, real=["ops.rules.schemas"])
    _base, pr_dir, _mb, head, _tip = pr_scenario(git_repo, tmp_path)
    names = ("git.exe", "gitleaks.exe") if os.name == "nt" else ("git", "gitleaks")
    for name in names:
        exe = pr_dir / name
        exe.write_bytes(b"#!/bin/sh\necho pwned > pwned.txt\n")
        exe.chmod(0o755)
    env = dict(git_repo.env)
    env["PATH"] = f"{pr_dir}{os.pathsep}{env.get('PATH', '')}"
    result = run_validate(tmp_path, env, forge=pr_forge(head), mode="pr", pr=42, policy="base", tree="pr")
    assert not (pr_dir / "pwned.txt").exists() and not (tmp_path / "pwned.txt").exists()
    tools = probe.inputs["secrets"].tools
    assert all(not Path(argv[0]).resolve().is_relative_to(pr_dir.resolve()) for argv in tools.values())
    assert by_check(result)["secrets"]["status"] in ("pass", "fail")
    assert result.data is not None


# -- modo base ------------------------------------------------------------------------


DECISION = "memory/decisions/2026-10-01-1-usar-latex.md"


def test_base_mode_without_before_uses_first_parent(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = Probe()
    use_rules(monkeypatch, probe.spec("schemas", {"tree", "pr", "base"}), real=["ops.rules.decisions"])
    first = seed_base(git_repo)
    git_repo.git("checkout", "-q", "-b", "task/1")
    git_repo.commit("decisión", files={DECISION: "x\n"})
    git_repo.checkout("main")
    merge = git_repo.merge_no_ff("task/1")
    result = run_validate(git_repo.path, git_repo.env, mode="base", policy=str(git_repo.path))
    assert result.ok, result.to_dict()
    assert result.data is not None
    assert result.data["base"] == {"ref": "HEAD^1", "sha": first} and result.data["head"] == {"ref": "HEAD", "sha": merge}
    inp = probe.inputs["schemas"]
    assert [(c.path, c.status) for c in inp.changes] == [(DECISION, "added")]
    assert inp.base_tree is not None and inp.base_tree.sha == first and inp.tree is not None and inp.tree.sha == merge
    assert by_check(result)["decisions-immutable"]["status"] == "pass"
    git_repo.commit("editar", files={DECISION: "y\n"})
    edited = run_validate(git_repo.path, git_repo.env, mode="base", tree=str(git_repo.path))
    assert edited.error is not None and edited.error.code == "E_VALIDATION_FAILED"
    finding = by_check(edited)["decisions-immutable"]["findings"][0]  # type: ignore[index]
    assert finding["code"] == "E_VALIDATION_DECISION_IMMUTABLE" and finding["path"] == DECISION  # type: ignore[index]


def test_base_mode_root_commit_has_no_changes(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = Probe()
    use_rules(monkeypatch, probe.spec("schemas", {"tree", "pr", "base"}), real=["ops.rules.decisions"])
    root = seed_base(git_repo, {DECISION: "x\n"})
    result = run_validate(git_repo.path, git_repo.env, mode="base", policy=str(git_repo.path))
    assert result.ok, result.to_dict()
    inp = probe.inputs["schemas"]
    assert inp.changes == () and inp.base_tree is None and inp.tree is not None and inp.tree.sha == root
    assert result.data is not None and result.data["base"] == {"ref": "HEAD^1", "sha": None}
    assert by_check(result)["decisions-immutable"] == {"check": "decisions-immutable", "status": "pass", "findings": []}


def test_base_mode_with_explicit_before_and_after(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = default_probe(monkeypatch)
    first = seed_base(git_repo)
    second = git_repo.commit("c2", files={"a.txt": "a\n"})
    git_repo.commit("c3", files={"b.txt": "b\n"})
    result = run_validate(git_repo.path, git_repo.env, mode="base", policy=str(git_repo.path), before=first,
                          after=second)
    assert result.ok
    assert [c.path for c in probe.inputs["schemas"].changes] == ["a.txt"]
    bad = run_validate(git_repo.path, git_repo.env, mode="base", policy=str(git_repo.path), before="nope")
    assert bad.error is not None and bad.error.code == "E_VALIDATION_INPUT"
    missing = run_validate(git_repo.path, git_repo.env, mode="base")
    assert missing.error is not None and missing.error.code == "E_VALIDATION_INPUT"


# -- modo build-log -------------------------------------------------------------------


def test_build_log_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, git_env: dict[str, str]) -> None:
    probe = default_probe(monkeypatch)
    log = tmp_path / "tex" / "build" / "main.log"
    log.parent.mkdir(parents=True)
    log.write_text("This is a log\n", encoding="utf-8")
    result = run_validate(tmp_path, git_env, mode="build-log", log="tex/build/main.log", pr=5)
    assert result.ok and result.data is not None
    assert result.data["mode"] == "build-log" and result.data["pr"] == 5
    assert probe.inputs["latex-log"].log_path == log
    assert probe.inputs["latex-log"].tree is None
    missing = run_validate(tmp_path, git_env, mode="build-log")
    assert missing.error is not None and missing.error.code == "E_VALIDATION_INPUT"


def test_build_log_mode_with_the_real_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                           git_env: dict[str, str]) -> None:
    use_rules(monkeypatch, real=["ops.rules.latex_log"])
    log = tmp_path / "main.log"
    log.write_text("This is pdfTeX\n! Undefined control sequence.\nl.12 \\foo\n", encoding="utf-8")
    report = tmp_path / "diagnostics.json"
    result = run_validate(tmp_path, git_env, mode="build-log", log=str(log), report=str(report))
    assert result.data is not None and [r["check"] for r in result.data["results"]] == ["latex-log"]
    assert json.loads(report.read_text(encoding="utf-8")) == json.loads(dumps(result))


# -- CLI y --format github ------------------------------------------------------------


def test_cli_format_github_emits_annotations(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def findings(spec: RuleSpec, inp: RuleInput) -> Sequence[Finding]:
        return [make_finding(spec, inp, "E_VALIDATION_STUB", severity="error", path="tex/a.tex", line=3, n=i)
                for i in range(60)]

    probe = Probe()
    use_rules(monkeypatch, probe.spec("schemas", {"tree", "pr", "base"}, findings=findings))
    _base, _pr, _mb, head, _tip = pr_scenario(git_repo, tmp_path)
    state = tmp_path / "fake.json"
    pr_forge(head).save(state)
    env = dict(git_repo.env)
    env.update({PREFIX + "TESTING": "1", PREFIX + "FORGE_ADAPTER": "fake", PREFIX + "FORGE_FAKE_STATE": str(state)})
    monkeypatch.chdir(tmp_path)
    out, err = io.StringIO(), io.StringIO()
    code = cli_main.run(["--lang", "en", "validate", "--mode", "pr", "--pr", "42", "--policy", "base", "--tree", "pr",
                         "--report", "validate.json", "--format", "github"], env=env, stdout=out, stderr=err)
    assert code == 3, err.getvalue()
    lines = out.getvalue().splitlines()
    annotations = [line for line in lines if line.startswith("::error ")]
    assert len(annotations) == 50
    assert annotations[0].startswith("::error file=tex/a.tex,line=3,title=schemas E_VALIDATION_STUB::")
    env_report = json.loads((tmp_path / "validate.json").read_text(encoding="utf-8"))
    assert env_report["op"] == "validate" and env_report["error"]["code"] == "E_VALIDATION_FAILED"
    assert len(env_report["data"]["results"][0]["findings"]) == 60  # data completa en el reporte


# -- --pin-report, falla cerrada e idioma ---------------------------------------------


def test_pr_mode_with_unreadable_pin_report(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    probe = Probe()
    scope = probe.spec("scope", {"pr"}, after=("roles-sync", "generated-sync"))
    use_rules(monkeypatch, scope, probe.spec("schemas", {"tree", "pr", "base"}),
              probe.spec("roles-sync", {"tree", "pr", "base"}), probe.spec("generated-sync", {"tree", "pr", "base"}),
              probe.spec("ci-policy", {"tree", "pr", "base"}))
    _base, _pr, _mb, head, _tip = pr_scenario(git_repo, tmp_path)
    result = run_validate(tmp_path, git_repo.env, forge=pr_forge(head), mode="pr", pr=42, policy="base", tree="pr",
                          pin_report="missing/pin.json")
    status = {c: r["status"] for c, r in by_check(result).items()}
    assert status == {"scope": "pass", "schemas": "fail", "roles-sync": "fail", "generated-sync": "fail",
                      "ci-policy": "pass"}
    assert set(probe.inputs) == {"scope", "ci-policy"}  # las tres del pin no se ejecutan
    assert dict(probe.inputs["scope"].prior) == {"roles-sync": "fail", "generated-sync": "fail"}
    assert result.error is not None and result.error.code == "E_VALIDATION_FAILED"


def test_missing_rule_module_fails_closed(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_base(git_repo)
    use_rules(monkeypatch, real=["ops.rules.schemas", "ops.rules._no_such_module"])
    result = run_validate(git_repo.path, git_repo.env)
    assert result.error is not None and result.error.code == "E_INTERNAL_UNEXPECTED"
    assert result.error.details is not None and result.error.details["reason"] == "rules_unavailable"
    assert result.data is None


def test_errors_after_reading_the_policy_use_its_language(git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch,
                                                         tmp_path: Path) -> None:
    from aucove.i18n import translator

    default_probe(monkeypatch)
    _base, _pr, _mb, head, _tip = pr_scenario(git_repo, tmp_path)
    env = {k: v for k, v in git_repo.env.items() if k != PREFIX + "LANG"}
    inv = Invocation(root=None, lang=None, json=True, dry_run=False, surface="ci", env=env, cwd=str(tmp_path))
    result = invoke(SPEC, OpContext(inv, forge=pr_forge(head, default_branch="develop")), mode="pr", pr=42,
                    policy="base", tree="pr")
    assert result.error is not None and result.error.code == "E_CONFIG_BASE_NOT_DEFAULT"
    assert result.error.message == translator("es")("error.E_CONFIG_BASE_NOT_DEFAULT.message")  # language: es
