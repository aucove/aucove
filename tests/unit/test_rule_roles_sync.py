# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Reglas roles-sync y generated-sync (09-ci §4.1; 06 REQ-PERM-31) y sus catálogos."""

from __future__ import annotations

import ast
import tomllib
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import get_args

import pytest

from aucove.config.roles import parse_roles
from aucove.domain.errors import ERRORS, PERM_CODES
from aucove.forge.base import RepoRef, TokenKind
from aucove.forge.fake import FakeForge, Fault
from aucove.i18n import CATALOG_AREAS, LANGS, Translator, load_catalog, placeholders, translator
from aucove.ops import check_path as check_path_op
from aucove.ops import doctor as doctor_op
from aucove.ops.rules import roles_sync
from aucove.ops.rules.base import RULE_MODULES, RuleInput
from aucove.render.generated import GENERATED_PATHS, generated_files
from aucove.render.mcp import McpProblem
from aucove.vcs.tree import MAX_FILE_BYTES, git_tree, worktree
from tests.conftest import FIXTURES, REPO_ROOT, GitRepo, roles_fixture

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
CODEOWNERS, TASK_YML = GENERATED_PATHS
PROJECT_ES = (FIXTURES / "config" / "project" / "minimal.yaml").read_text(encoding="utf-8")
PROJECT_EN = PROJECT_ES.replace("language: es", "language: en")
CATALOG_DIR = REPO_ROOT / "src" / "aucove" / "i18n" / "catalogs"
MY_MODULES = [
    REPO_ROOT / "src" / "aucove" / "ops" / name
    for name in ("check_path.py", "sync_roles.py", "doctor.py", "rules/roles_sync.py")
]


def expected_files(roles_text: str, lang: str) -> dict[str, bytes]:
    roles, findings = parse_roles(roles_text.encode("utf-8"))
    assert roles is not None, findings
    return generated_files(
        lead=roles.lead,
        deputy=roles.deputy,
        sections=[(s.id, s.title, s.owner) for s in roles.sections],
        source_sha12=roles.source_sha12,
        lang=lang,  # type: ignore[arg-type]
    )


def project_files(roles: str = "basic", *, lang: str = "es", generated: bool = True) -> dict[str, str | bytes]:
    roles_text = roles_fixture(roles)
    files: dict[str, str | bytes] = {
        "project.yaml": PROJECT_ES if lang == "es" else PROJECT_EN,
        "roles.yaml": roles_text,
        "sections/methods/methods.tex": "% methods\n",
    }
    if generated:
        files.update(expected_files(roles_text, lang))
    return files


def run_rule(rule_id: str, inp: RuleInput) -> list:  # type: ignore[type-arg]
    rule = {r.id: r for r in roles_sync.RULES}[rule_id]
    assert inp.mode in rule.modes
    return list(rule.run(inp))


def tree_input(repo: GitRepo, t: Translator, **kw: object) -> RuleInput:
    return RuleInput(mode="tree", t=t, now=NOW, tree=worktree(repo.path), **kw)  # type: ignore[arg-type]


def pr_input(repo: GitRepo, sha: str, t: Translator, forge: object | None, **kw: object) -> RuleInput:
    return RuleInput(
        mode="pr", t=t, now=NOW, tree=git_tree(repo.path, sha), head_sha=sha, head_ref="task/17", forge=forge, **kw  # type: ignore[arg-type]
    )


class ExplodingForge:
    """Cualquier acceso a la forja hace fallar la prueba."""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"no se esperaba usar la forja: {name}")


def fake_forge() -> FakeForge:
    return FakeForge(RepoRef("example-org", "demo"), viewer="ana")


# -- declaración -----------------------------------------------------------------------


def test_module_is_registered_and_exports_both_rules() -> None:
    assert "ops.rules.roles_sync" in RULE_MODULES
    ids = [r.id for r in roles_sync.RULES]
    assert ids == ["roles-sync", "generated-sync"]
    for rule in roles_sync.RULES:
        assert rule.modes == frozenset({"tree", "pr", "base"})
        assert rule.after == () and rule.tools == ()
    assert dict(roles_sync.ROLES_SYNC.codes) == {"E_VALIDATION_ROLES_OUT_OF_SYNC": frozenset({"reason"})}
    assert dict(roles_sync.GENERATED_SYNC.codes) == {"E_VALIDATION_GENERATED": frozenset()}


# -- roles-sync ------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["es", "en"])
def test_roles_sync_passes_when_generated_files_match(git_repo: GitRepo, t_es: Translator, lang: str) -> None:
    git_repo.commit("base", files=project_files(lang=lang))
    assert run_rule("roles-sync", tree_input(git_repo, t_es)) == []
    sha = git_repo.head()
    assert run_rule("roles-sync", pr_input(git_repo, sha, t_es, fake_forge())) == []


def test_roles_sync_fails_with_hand_edited_codeowners(git_repo: GitRepo, t_es: Translator) -> None:
    files = project_files()
    files[CODEOWNERS] = files[CODEOWNERS] + b"docs/  @carla\n"  # type: ignore[operator]
    sha = git_repo.commit("mano", files=files)
    for inp in (tree_input(git_repo, t_es), pr_input(git_repo, sha, t_es, fake_forge())):
        findings = run_rule("roles-sync", inp)
        assert [(f.code, f.path, f.severity, f.check) for f in findings] == [
            ("E_VALIDATION_ROLES_OUT_OF_SYNC", CODEOWNERS, "error", "roles-sync")
        ]
        assert "el contenido es distinto" in findings[0].message
        assert findings[0].hint is not None and "sync-roles" in findings[0].hint


def test_roles_sync_regenerates_from_the_language_of_head(git_repo: GitRepo, t_en: Translator) -> None:
    files = project_files(lang="es")
    files["project.yaml"] = PROJECT_EN  # la cabeza cambia el idioma sin regenerar task.yml
    git_repo.commit("idioma", files=files)
    findings = run_rule("roles-sync", tree_input(git_repo, t_en))
    assert [f.path for f in findings] == [TASK_YML]
    assert "the content is different" in findings[0].message


def test_roles_sync_uses_roles_yaml_of_head(git_repo: GitRepo, t_es: Translator) -> None:
    git_repo.commit("base", files=project_files())
    changed = roles_fixture("basic").replace("deputy: bruno\n", "")
    sha = git_repo.commit("sin suplente", files={"roles.yaml": changed})
    findings = run_rule("roles-sync", pr_input(git_repo, sha, t_es, fake_forge()))
    assert [f.path for f in findings] == [CODEOWNERS]  # CODEOWNERS cambia; task.yml no
    git_repo.commit("regenera", files=expected_files(changed, "es"))
    assert run_rule("roles-sync", pr_input(git_repo, git_repo.head(), t_es, fake_forge())) == []


def test_roles_sync_reports_missing_files(git_repo: GitRepo, t_es: Translator) -> None:
    git_repo.commit("sin generados", files=project_files(generated=False))
    findings = run_rule("roles-sync", tree_input(git_repo, t_es))
    assert [(f.path, f.code) for f in findings] == [
        (CODEOWNERS, "E_VALIDATION_ROLES_OUT_OF_SYNC"),
        (TASK_YML, "E_VALIDATION_ROLES_OUT_OF_SYNC"),
    ]
    assert all("falta el archivo" in f.message for f in findings)


@pytest.mark.parametrize(
    ("change", "path", "text"),
    [
        ({"roles.yaml": "version: 1\npeople: []\n"}, "roles.yaml", "no es válido"),
        ({"project.yaml": "convention: 3\n"}, "project.yaml", "no es válido"),
    ],
)
def test_roles_sync_fails_closed_when_inputs_are_invalid(
    git_repo: GitRepo, t_es: Translator, change: Mapping[str, str], path: str, text: str
) -> None:
    files = project_files()
    files.update(change)
    git_repo.commit("roto", files=files)
    findings = run_rule("roles-sync", tree_input(git_repo, t_es))
    assert [(f.path, f.severity) for f in findings] == [(path, "error")]
    assert text in findings[0].message


def test_roles_sync_fails_when_roles_yaml_is_missing(git_repo: GitRepo, t_es: Translator) -> None:
    files = project_files()
    del files["roles.yaml"]
    sha = git_repo.commit("sin roles", files=files)
    findings = run_rule("roles-sync", pr_input(git_repo, sha, t_es, fake_forge()))
    assert [f.path for f in findings] == ["roles.yaml"]


def test_tree_mode_normalizes_crlf_but_pr_mode_compares_bytes(git_repo: GitRepo, t_es: Translator) -> None:
    files = project_files()
    files[CODEOWNERS] = files[CODEOWNERS].replace(b"\n", b"\r\n")  # type: ignore[union-attr]
    sha = git_repo.commit("crlf", files=files)
    assert run_rule("roles-sync", tree_input(git_repo, t_es)) == []  # REQ-ARCH-88
    assert [f.path for f in run_rule("roles-sync", pr_input(git_repo, sha, t_es, fake_forge()))] == [CODEOWNERS]


def test_codeowners_errors_fail_the_rule_in_pr_mode(git_repo: GitRepo, t_es: Translator) -> None:
    sha = git_repo.commit("base", files=project_files())
    forge = fake_forge()
    forge.set_codeowners_errors(sha, ["Unknown owner on line 3:\nmake sure @zoe exists"])
    findings = run_rule("roles-sync", pr_input(git_repo, sha, t_es, forge))
    assert [(f.code, f.path) for f in findings] == [("E_VALIDATION_ROLES_OUT_OF_SYNC", CODEOWNERS)]
    assert "Unknown owner on line 3: make sure @zoe exists" in findings[0].message  # una sola línea
    # en modo tree no se consulta la forja (codeowners_errors solo en pr)
    assert run_rule("roles-sync", tree_input(git_repo, t_es, forge=ExplodingForge())) == []


def test_many_codeowners_errors_are_summarized(git_repo: GitRepo, t_es: Translator) -> None:
    sha = git_repo.commit("base", files=project_files())
    forge = fake_forge()
    forge.set_codeowners_errors(sha, [f"error {i}" for i in range(roles_sync.MAX_FORGE_ERRORS + 5)])
    findings = run_rule("roles-sync", pr_input(git_repo, sha, t_es, forge))
    assert len(findings) == roles_sync.MAX_FORGE_ERRORS + 1
    assert "5 errores más" in findings[-1].message


def test_pr_mode_without_forge_or_with_forge_errors_fails_closed(git_repo: GitRepo, t_es: Translator) -> None:
    sha = git_repo.commit("base", files=project_files())
    findings = run_rule("roles-sync", pr_input(git_repo, sha, t_es, None))
    assert [(f.code, f.severity, f.path) for f in findings] == [("E_INTERNAL_UNEXPECTED", "error", CODEOWNERS)]
    forge = fake_forge()
    forge.faults.add(Fault(method="codeowners_errors", kind="http_5xx"))
    findings = run_rule("roles-sync", pr_input(git_repo, sha, t_es, forge))
    assert [(f.code, f.severity) for f in findings] == [("E_INTERNAL_UNEXPECTED", "error")]


def test_rules_without_tree_fail_closed(t_es: Translator) -> None:
    inp = RuleInput(mode="tree", t=t_es, now=NOW)
    for rule_id in ("roles-sync", "generated-sync"):
        findings = run_rule(rule_id, inp)
        assert [(f.code, f.severity) for f in findings] == [("E_INTERNAL_UNEXPECTED", "error")]


def test_base_mode_does_not_query_the_forge(git_repo: GitRepo, t_es: Translator) -> None:
    sha = git_repo.commit("base", files=project_files())
    inp = RuleInput(mode="base", t=t_es, now=NOW, tree=git_tree(git_repo.path, sha), forge=ExplodingForge())  # type: ignore[arg-type]
    assert run_rule("roles-sync", inp) == []


# -- generated-sync --------------------------------------------------------------------


def test_generated_sync_passes_without_skills(git_repo: GitRepo, t_es: Translator) -> None:
    sha = git_repo.commit("base", files=project_files())
    assert run_rule("generated-sync", tree_input(git_repo, t_es)) == []
    assert run_rule("generated-sync", pr_input(git_repo, sha, t_es, None)) == []


@pytest.mark.parametrize(
    "path",
    [".claude/skills/status/SKILL.md", ".agents/skills/status/SKILL.md", ".Claude/Skills/x/SKILL.md"],
)
def test_generated_sync_fails_with_any_file_under_skills(git_repo: GitRepo, t_es: Translator, path: str) -> None:
    files = project_files()
    files[path] = "---\nname: status\n---\n"
    sha = git_repo.commit("skill", files=files)
    for inp in (tree_input(git_repo, t_es), pr_input(git_repo, sha, t_es, None)):
        findings = run_rule("generated-sync", inp)
        assert [(f.code, f.path, f.severity, f.check) for f in findings] == [
            ("E_VALIDATION_GENERATED", path, "error", "generated-sync")
        ]
        assert "no genera skills" in findings[0].message


def test_generated_sync_ignores_other_claude_files(git_repo: GitRepo, t_es: Translator) -> None:
    files = project_files()
    files[".claude/settings.json"] = "{}\n"
    files["docs/skills/readme.md"] = "x\n"
    git_repo.commit("otros", files=files)
    assert run_rule("generated-sync", tree_input(git_repo, t_es)) == []


# -- regeneración compartida -----------------------------------------------------------


def test_tree_reader_reads_regular_entries_with_their_exact_spelling(git_repo: GitRepo) -> None:
    git_repo.commit("archivos", files={"a.txt": "x", "dir/b.txt": "y", ".github/codeowners": "z\n"})
    big = git_repo.path / "big.bin"
    with open(big, "wb") as fh:
        fh.truncate(MAX_FILE_BYTES + 1)
    read, exists = roles_sync.tree_reader(worktree(git_repo.path))
    assert read("a.txt") == b"x" and exists("a.txt")
    assert read("dir") is None and not exists("dir")  # una carpeta no es una entrada
    assert read("big.bin") is None and exists("big.bin")  # supera el tope de 5 MB
    assert read("missing") is None and not exists("missing")
    assert read("../a.txt") is None and read("") is None
    # la grafía exacta manda, también donde el disco no distingue mayúsculas
    assert read(".github/codeowners") == b"z\n"
    assert read(".github/CODEOWNERS") is None and not exists(".github/CODEOWNERS")


def test_regenerate_reports_each_problem() -> None:
    def reader(files: Mapping[str, bytes]) -> roles_sync.Reader:
        return lambda p: files.get(p)

    roles = roles_fixture("basic").encode("utf-8")
    project = PROJECT_ES.encode("utf-8")
    cases = [
        ({}, "roles_missing", "roles.yaml"),
        ({"roles.yaml": b"people: 3\n"}, "roles_invalid", "roles.yaml"),
        ({"roles.yaml": roles}, "project_missing", "project.yaml"),
        ({"roles.yaml": roles, "project.yaml": b"name: [\n"}, "project_invalid", "project.yaml"),
    ]
    for files, problem, path in cases:
        regen = roles_sync.regenerate(reader(files))
        assert (regen.expected, regen.problem, regen.problem_path) == (None, problem, path)
    regen = roles_sync.regenerate(reader({"roles.yaml": roles, "project.yaml": project}))
    assert regen.problem is None and regen.expected is not None
    assert dict(regen.expected) == expected_files(roles_fixture("basic"), "es")
    assert regen.language == "es"


def test_regeneration_matches_the_template_tool(t_en: Translator) -> None:
    from tests.conftest import load_tool

    tool = load_tool("render_template")
    roles = roles_fixture("mixed-case").encode("utf-8")
    for lang in LANGS:
        expected = tool.generated_from_roles(roles, lang)
        project = (PROJECT_ES if lang == "es" else PROJECT_EN).encode("utf-8")
        regen = roles_sync.regenerate(lambda p: {"roles.yaml": roles, "project.yaml": project}.get(p), t=t_en)
        assert regen.expected is not None and dict(regen.expected) == expected


# -- catálogos -------------------------------------------------------------------


def _flat(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}

    def walk(table: Mapping[str, object], prefix: str) -> None:
        for key, value in table.items():
            if isinstance(value, dict):
                walk(value, f"{prefix}{key}.")
            else:
                out[f"{prefix}{key}"] = str(value)

    walk(tomllib.loads(path.read_text(encoding="utf-8")), "")
    return out


@pytest.mark.parametrize("lang", LANGS)
def test_catalog_keys_stay_in_their_areas(lang: str) -> None:
    roles_keys = _flat(CATALOG_DIR / lang / "roles.toml")
    doctor_keys = _flat(CATALOG_DIR / lang / "doctor.toml")
    assert all(k.startswith(CATALOG_AREAS["roles"]) for k in roles_keys)
    assert all(k.startswith(CATALOG_AREAS["doctor"]) for k in doctor_keys)
    finding_codes = {k.split(".")[1] for k in roles_keys if k.startswith("finding.")}
    assert finding_codes == {"E_VALIDATION_ROLES_OUT_OF_SYNC", "E_VALIDATION_GENERATED"}
    # nadie más define claves de estas áreas ni los finding.* de estos códigos
    merged = load_catalog(lang)  # type: ignore[arg-type]
    for key in merged:
        parts = key.split(".")
        if parts[0] in ("roles", "doctor") or (parts[0] == "finding" and parts[1] in finding_codes):
            assert key in roles_keys or key in doctor_keys, key


@pytest.mark.parametrize("lang", LANGS)
def test_finding_placeholders_match_the_rule_codes(lang: str) -> None:
    t = translator(lang)  # type: ignore[arg-type]
    catalog = load_catalog(lang)  # type: ignore[arg-type]
    for rule in roles_sync.RULES:
        for code, markers in rule.codes.items():
            assert placeholders(catalog[f"finding.{code}.message"]) == markers
            assert placeholders(catalog[f"finding.{code}.hint"]) <= markers
            assert t.get(f"finding.{code}.hint") is not None


def _literal_keys(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    keys: set[str] = set()
    # los fragmentos fijos de un f-string no son claves: las familias van en _dynamic_keys
    fragments = {id(v) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr) for v in node.values}
    for node in ast.walk(tree):
        if id(node) in fragments:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            looks_like_key = " " not in value and not value.endswith((".", ".yaml"))
            if value.startswith(("roles.", "doctor.", "finding.")) and looks_like_key:
                keys.add(value)
    return keys


def _dynamic_keys() -> set[str]:
    keys = {f"roles.reason.{p}" for p in ("roles_missing", "roles_invalid", "project_missing", "project_invalid", "render_failed")}
    keys |= {f"roles.reason.{s}" for s in ("differs", "missing", "not_regular")}
    keys |= {f"roles.check_path.reason.{k}" for k in check_path_op._REASON_KEYS.values()}
    keys |= {f"roles.check_path.reason.{k}" for k in ("outside_task_notes", "outside_task_section", "no_identity", "confirm")}
    keys |= {f"roles.sync.render.{a}" for a in ("updated", "unchanged", "would_update")}
    keys |= {f"doctor.{c}.skip" for c in doctor_op.SKIPPED}
    keys |= {f"doctor.forge.skip_{r}" for r in ("offline", "network", "policy", "unsupported", "rate_limited")}
    keys |= {f"doctor.identity.kind.{k}" for k in get_args(TokenKind)}
    keys |= {f"doctor.identity.scope.{s}" for s in ("repo_restricted", "owner_wide", "broad", "unknown", "ci")}
    keys |= {f"doctor.forge_reachable.visibility.{v}" for v in ("public", "private", "internal")}
    keys |= {f"doctor.mcp_config.fail_{p}" for p in get_args(McpProblem)}
    keys |= {f"doctor.render.status.{s}" for s in ("ok", "warn", "fail", "skip")}
    return keys


@pytest.mark.parametrize("lang", LANGS)
def test_every_key_used_by_the_package_exists(lang: str) -> None:
    own = _flat(CATALOG_DIR / lang / "roles.toml") | _flat(CATALOG_DIR / lang / "doctor.toml")
    used: set[str] = set().union(*(_literal_keys(p) for p in MY_MODULES)) | _dynamic_keys()
    assert used, "no se encontraron claves"
    assert sorted(used - set(own)) == []


def test_reason_keys_cover_every_perm_code() -> None:
    assert set(check_path_op._REASON_KEYS) == set(PERM_CODES)
    assert all(code in ERRORS for code in PERM_CODES)
