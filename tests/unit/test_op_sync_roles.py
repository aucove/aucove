# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Operación ``sync-roles`` (05 §9.4; 06 §4.6; D-34 n; D-35)."""

from __future__ import annotations

import io
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from aucove import _meta
from aucove.config.roles import parse_roles
from aucove.config.schemas import validate_result
from aucove.i18n import LANGS, translator
from aucove.localstore.store import IdentityCache, Store
from aucove.ops import context as context_mod
from aucove.ops import sync_roles as op
from aucove.ops.context import Invocation, OpContext
from aucove.ops.registry import REGISTRY, invoke
from aucove.ops.result import Result, WarningObj, fail, make_error, make_warning, ok
from aucove.ops.rules.roles_sync import FileState
from aucove.render.generated import GENERATED_PATHS, generated_files
from tests.conftest import FIXTURES, GitRepo, isolated_git_env, load_tool, roles_fixture

SPEC = next(s for s in REGISTRY if s.id == "sync-roles")
CODEOWNERS, TASK_YML = GENERATED_PATHS
FAR = "2099-01-01T00:00:00Z"
PROJECT_ES = (FIXTURES / "config" / "project" / "minimal.yaml").read_text(encoding="utf-8")
PROJECT_EN = PROJECT_ES.replace("language: es", "language: en")


def clean_env(env: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if not k.upper().startswith((_meta.ENV_PREFIX, "CLAUDE_CODE_"))}


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


def new_repo(tmp: Path, *, generated: bool = False, lang: str = "es") -> GitRepo:
    """Proyecto con la base publicada en ``origin/main``; sin generados por defecto."""
    env = isolated_git_env(tmp / "git-env")
    path, origin = tmp / "repo", tmp / "origin.git"
    path.mkdir(parents=True)
    repo = GitRepo(path, origin, env)
    repo._run(tmp, ["init", "-q", "--bare", "-b", "main", str(origin)], True)
    repo.git("init", "-q", "-b", "main")
    repo.git("remote", "add", "origin", str(origin))
    files: dict[str, str | bytes] = {
        "project.yaml": PROJECT_ES if lang == "es" else PROJECT_EN,
        "roles.yaml": roles_fixture("basic"),
        "sections/methods/methods.tex": "% métodos\n",
    }
    if generated:
        files.update(expected_files(roles_fixture("basic"), lang))
    repo.commit("base", files=files)
    repo.push("main")
    repo.fetch()
    return repo


@pytest.fixture
def repo(tmp_path: Path) -> GitRepo:
    return new_repo(tmp_path)


def seed_identity(repo: GitRepo, login: str) -> None:
    Store(repo.path).write_identity(IdentityCache(login, 7, FAR), token_fp=None)


def run_op(
    repo: GitRepo | Path,
    *,
    check: bool = False,
    dry_run: bool = False,
    lang: str | None = "es",
    env: Mapping[str, str] | None = None,
) -> Result:
    root = repo.path if isinstance(repo, GitRepo) else repo
    base_env = clean_env(repo.env) if isinstance(repo, GitRepo) else {}
    inv = Invocation(
        root=str(root),
        lang=lang,
        json=True,
        dry_run=dry_run,
        surface="cli",
        env={**base_env, **(env or {})},
        cwd=str(root),
    )
    return invoke(SPEC, OpContext(inv), check=check)


def assert_valid(result: Result) -> dict[str, object]:
    envelope = result.to_dict()
    assert validate_result(envelope) == [], json.dumps(envelope, ensure_ascii=False, indent=1)
    return envelope


def codes(warnings: tuple[WarningObj, ...]) -> list[str]:
    return [w.code for w in warnings]


# -- escritura e idempotencia ------------------------------------------------------------


@pytest.mark.parametrize("login", ["ana", "bruno"], ids=["lead", "deputy"])
def test_write_is_idempotent(repo: GitRepo, login: str) -> None:
    seed_identity(repo, login)
    first = run_op(repo)
    data = assert_valid(first)["data"]
    assert first.ok and first.exit_code() == 0
    assert data == {
        "check_only": False,
        "changed": True,
        "files": [{"path": CODEOWNERS, "action": "updated"}, {"path": TASK_YML, "action": "updated"}],
        "diff": data["diff"],  # type: ignore[index]
        "unknown_logins": [],
    }
    want = expected_files(roles_fixture("basic"), "es")
    for path, payload in want.items():
        assert (repo.path / path).read_bytes() == payload
    second = run_op(repo)
    data2 = assert_valid(second)["data"]
    assert data2 == {
        "check_only": False,
        "changed": False,
        "files": [{"path": CODEOWNERS, "action": "unchanged"}, {"path": TASK_YML, "action": "unchanged"}],
        "diff": None,
        "unknown_logins": [],
    }
    assert codes(second.warnings) == ["W_FORGE_CAPABILITY_UNKNOWN"]
    # no hay temporales sueltos
    assert sorted(p.name for p in (repo.path / ".github").rglob("*") if p.is_file()) == ["CODEOWNERS", "task.yml"]


def test_reads_roles_yaml_of_the_working_tree(repo: GitRepo) -> None:
    seed_identity(repo, "ana")
    changed = roles_fixture("basic").replace("    owner: bruno\n", "    owner: carla\n")
    repo.write({"roles.yaml": changed})  # sin commit: la entrada es el árbol de trabajo
    result = run_op(repo)
    assert result.ok
    assert (repo.path / CODEOWNERS).read_bytes() == expected_files(changed, "es")[CODEOWNERS]
    assert b"# section results: owner @carla\n" in (repo.path / CODEOWNERS).read_bytes()


def test_language_comes_from_project_yaml_of_the_tree(repo: GitRepo) -> None:
    seed_identity(repo, "ana")
    repo.write({"project.yaml": PROJECT_EN})
    assert run_op(repo).ok
    assert (repo.path / TASK_YML).read_bytes() == expected_files(roles_fixture("basic"), "en")[TASK_YML]


def test_crlf_checkout_counts_as_unchanged(tmp_path: Path) -> None:
    repo = new_repo(tmp_path, generated=True)
    target = repo.path / CODEOWNERS
    target.write_bytes(target.read_bytes().replace(b"\n", b"\r\n"))
    result = run_op(repo, check=True)
    assert result.ok and result.data is not None and result.data["changed"] is False


def test_dry_run_plans_without_writing(repo: GitRepo) -> None:
    seed_identity(repo, "ana")
    result = run_op(repo, dry_run=True)
    envelope = assert_valid(result)
    assert result.ok
    data = envelope["data"]
    assert isinstance(data, dict) and data["dry_run"] is True
    assert [(e["kind"], e["target"]) for e in data["plan"]] == [("file_write", CODEOWNERS), ("file_write", TASK_YML)]
    assert not (repo.path / CODEOWNERS).exists()


# -- --check ------------------------------------------------------------------------


def test_check_with_differences_fails_with_diff_and_full_data(repo: GitRepo) -> None:
    result = run_op(repo, check=True)  # sin identidad: --check funciona igual (D-34 n)
    envelope = assert_valid(result)
    assert not result.ok and result.error is not None
    assert result.error.code == "E_VALIDATION_ROLES_OUT_OF_SYNC"
    assert result.error.details == {"paths": [CODEOWNERS, TASK_YML]}
    assert result.exit_code() == 3
    data = envelope["data"]
    assert isinstance(data, dict)
    assert data["check_only"] is True and data["changed"] is True
    assert data["files"] == [{"path": CODEOWNERS, "action": "would_update"}, {"path": TASK_YML, "action": "would_update"}]
    assert data["unknown_logins"] == []
    diff = data["diff"]
    assert isinstance(diff, str)
    assert f"--- /dev/null\n+++ b/{CODEOWNERS}\n" in diff
    assert "+*   @ana @bruno\n" in diff
    assert not (repo.path / CODEOWNERS).exists()  # --check no escribe
    assert "W_FORGE_CAPABILITY_UNKNOWN" in codes(result.warnings)


def test_check_shows_a_diff_against_a_hand_edit(tmp_path: Path) -> None:
    repo = new_repo(tmp_path, generated=True)
    target = repo.path / CODEOWNERS
    target.write_bytes(target.read_bytes() + b"docs/  @carla\n")
    result = run_op(repo, check=True)
    assert result.error is not None and result.error.code == "E_VALIDATION_ROLES_OUT_OF_SYNC"
    assert result.data is not None
    assert result.data["files"] == [{"path": CODEOWNERS, "action": "would_update"}, {"path": TASK_YML, "action": "unchanged"}]
    assert f"--- a/{CODEOWNERS}\n+++ b/{CODEOWNERS}\n" in result.data["diff"]
    assert "-docs/  @carla\n" in result.data["diff"]


def test_check_without_differences_passes(tmp_path: Path) -> None:
    repo = new_repo(tmp_path, generated=True)
    result = run_op(repo, check=True)
    data = assert_valid(result)["data"]
    assert result.ok and result.exit_code() == 0
    assert data == {
        "check_only": True,
        "changed": False,
        "files": [{"path": CODEOWNERS, "action": "unchanged"}, {"path": TASK_YML, "action": "unchanged"}],
        "diff": None,
        "unknown_logins": [],
    }


@pytest.mark.parametrize("lang", LANGS)
def test_check_on_the_template_render_exits_0(tmp_path: Path, lang: str) -> None:
    from aucove.cli.main import run as cli_run

    tool = load_tool("render_template")
    out = tmp_path / f"render-{lang}"
    tool.render_to(out, lang)
    env = isolated_git_env(tmp_path / "git-env")
    GitRepo(out, tmp_path / "unused.git", env).git("init", "-q", "-b", "main")
    stdout, stderr = io.StringIO(), io.StringIO()
    code = cli_run(["--json", "--root", str(out), "sync-roles", "--check"], env=clean_env(env), stdout=stdout, stderr=stderr)
    assert code == 0, stdout.getvalue() + stderr.getvalue()
    envelope = json.loads(stdout.getvalue())
    assert validate_result(envelope) == []
    assert envelope["data"]["changed"] is False


# -- autorización (D-08, D-34 n) -----------------------------------------------------


def test_write_without_identity_fails_with_auth_error_and_hint(repo: GitRepo) -> None:
    result = run_op(repo)
    assert_valid(result)
    assert result.error is not None and result.error.code.startswith("E_AUTH_")
    assert result.error.code == "E_AUTH_NOT_AUTHENTICATED"
    assert result.error.hint
    assert result.exit_code() == 5
    assert not (repo.path / CODEOWNERS).exists()


def test_write_with_expired_identity_fails_like_without_it(repo: GitRepo) -> None:
    Store(repo.path).write_identity(IdentityCache("ana", 7, "2020-01-01T00:00:00Z"), token_fp=None)
    result = run_op(repo)
    assert result.error is not None and result.error.code == "E_AUTH_NOT_AUTHENTICATED"


def test_researcher_cannot_write(repo: GitRepo) -> None:
    seed_identity(repo, "carla")
    result = run_op(repo)
    assert_valid(result)
    assert result.error is not None and result.error.code == "E_PERM_ROLE_REQUIRED"
    assert result.error.details == {"login": "carla", "role": "researcher", "required_role": ["lead", "deputy"]}
    assert result.exit_code() == 4
    assert not (repo.path / CODEOWNERS).exists()


def test_non_member_cannot_write(repo: GitRepo) -> None:
    seed_identity(repo, "dave")
    result = run_op(repo)
    assert result.error is not None and result.error.code == "E_PERM_NOT_A_MEMBER"


def test_authorization_uses_the_base_not_the_working_tree(repo: GitRepo) -> None:
    seed_identity(repo, "carla")
    promoted = roles_fixture("basic").replace("lead: ana", "lead: carla")
    promoted = promoted.replace("    role: lead\n", "    role: researcher\n", 1)
    promoted = promoted.replace("  - login: carla\n    name: \"Carla Soto\"\n    role: researcher\n", "  - login: carla\n    name: \"Carla Soto\"\n    role: lead\n")
    roles, findings = parse_roles(promoted.encode("utf-8"))
    assert roles is not None and roles.lead == "carla", findings
    repo.write({"roles.yaml": promoted})
    result = run_op(repo)
    assert result.error is not None and result.error.code == "E_PERM_ROLE_REQUIRED"


def test_login_case_does_not_matter(repo: GitRepo) -> None:
    seed_identity(repo, "ANA")
    assert run_op(repo).ok


# -- identidad por forge_id (08-forja §6.3 y §6.4; REQ-FORGE-48) --------------------------

FORGE_ID_ROLES = (
    roles_fixture("basic")
    .replace("  - login: ana\n", "  - login: ana\n    forge_id: 7\n")
    .replace("  - login: bruno\n", "  - login: bruno\n    forge_id: 111\n")
)


def forge_id_repo(tmp: Path) -> GitRepo:
    """Base con ``forge_id`` para ana (lead, 7) y bruno (deputy, 111)."""
    assert FORGE_ID_ROLES.count("forge_id:") == 2
    repo = new_repo(tmp)
    repo.commit("forge_id", files={"roles.yaml": FORGE_ID_ROLES})
    repo.push("main")
    repo.fetch()
    return repo


def seed_account(repo: GitRepo, login: str, ident: int | None) -> None:
    Store(repo.path).write_identity(IdentityCache(login, ident, FAR), token_fp=None)


def test_same_login_other_id_is_not_a_member(tmp_path: Path) -> None:
    """Otra cuenta con el login del suplente (id 999 en vez de 111) no puede escribir."""
    repo = forge_id_repo(tmp_path)
    seed_account(repo, "bruno", 999)
    result = run_op(repo)
    assert_valid(result)
    assert result.error is not None and result.error.code == "E_PERM_NOT_A_MEMBER", result.to_dict()
    assert result.error.details == {"login": "bruno"}
    assert result.exit_code() == 4
    assert not (repo.path / CODEOWNERS).exists()


def test_renamed_lead_with_the_same_id_writes_with_a_warning(tmp_path: Path) -> None:
    """El lead renombró su cuenta (mismo id 7): sigue siendo ana, escribe y se avisa."""
    repo = forge_id_repo(tmp_path)
    seed_account(repo, "ana-nueva", 7)
    result = run_op(repo)
    assert_valid(result)
    assert result.ok and result.error is None, result.to_dict()
    assert codes(result.warnings) == ["W_AUTH_LOGIN_CHANGED", "W_FORGE_CAPABILITY_UNKNOWN"]
    assert result.warnings[0].details == {"login": "ana-nueva", "roles_login": "ana"}
    for path, payload in expected_files(FORGE_ID_ROLES, "es").items():
        assert (repo.path / path).read_bytes() == payload


def test_renamed_researcher_keeps_the_researcher_role(tmp_path: Path) -> None:
    """El aviso también acompaña al error: carla no tiene forge_id, pero bruno2 (111) es bruno."""
    roles_text = FORGE_ID_ROLES.replace("deputy: bruno\n", "deputy: carla\n")
    repo = new_repo(tmp_path)
    repo.commit("deputy carla", files={"roles.yaml": roles_text})
    repo.push("main")
    repo.fetch()
    seed_account(repo, "bruno2", 111)
    result = run_op(repo)
    assert_valid(result)
    assert result.error is not None and result.error.code == "E_PERM_ROLE_REQUIRED", result.to_dict()
    assert result.error.details == {"login": "bruno", "role": "researcher", "required_role": ["lead", "deputy"]}
    assert codes(result.warnings) == ["W_AUTH_LOGIN_CHANGED"]


def test_without_id_in_the_cache_the_login_decides(tmp_path: Path) -> None:
    repo = forge_id_repo(tmp_path)
    seed_account(repo, "ana", None)
    assert run_op(repo).ok


# -- respaldo de la política (REQ-PERM-4) ------------------------------------------------


def test_fallback_warning_carries_each_file_sha(tmp_path: Path) -> None:
    """``W_CONFIG_POLICY_FALLBACK`` da el sha usado de ``project.yaml`` y de ``roles.yaml``."""
    from tests.unit.test_base_snapshot import BAD_PROJECT, BAD_ROLES

    repo = new_repo(tmp_path)
    c1 = repo.git("rev-parse", "HEAD")
    no_deputy = roles_fixture("no-deputy")
    c2 = repo.commit("sin suplente", files={"roles.yaml": no_deputy})
    repo.commit("rota", files={"project.yaml": BAD_PROJECT, "roles.yaml": BAD_ROLES})
    repo.push("main")
    repo.fetch()
    repo.write({"project.yaml": PROJECT_ES, "roles.yaml": no_deputy})  # el árbol, válido
    seed_identity(repo, "ana")
    result = run_op(repo)
    assert_valid(result)
    assert result.ok, result.to_dict()
    warns = [w for w in result.warnings if w.code == "W_CONFIG_POLICY_FALLBACK"]
    assert len(warns) == 1, result.to_dict()
    assert warns[0].details == {"sha": c2, "files": {"project.yaml": c1, "roles.yaml": c2}}


def test_write_without_fetched_base_is_unavailable(tmp_path: Path) -> None:
    env = isolated_git_env(tmp_path / "git-env")
    path = tmp_path / "repo"
    path.mkdir()
    repo = GitRepo(path, tmp_path / "origin.git", env)
    repo.git("init", "-q", "-b", "main")
    repo.commit("local", files={"project.yaml": PROJECT_ES, "roles.yaml": roles_fixture("basic")})
    seed_identity(repo, "ana")
    result = run_op(repo)
    assert result.error is not None and result.error.code == "E_CONFIG_POLICY_UNAVAILABLE"
    check = run_op(repo, check=True)  # --check no necesita la base: solo informa la diferencia
    assert check.error is not None and check.error.code == "E_VALIDATION_ROLES_OUT_OF_SYNC"


def test_never_calls_the_forge(repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("sync-roles no consulta la forja en esta versión (D-34 n)")

    monkeypatch.setattr(OpContext, "forge", boom)
    monkeypatch.setattr(context_mod, "context_factory", boom)
    seed_identity(repo, "ana")
    result = run_op(repo)
    assert result.ok and result.data is not None and result.data["unknown_logins"] == []
    assert codes(result.warnings) == ["W_FORGE_CAPABILITY_UNKNOWN"]
    assert result.warnings[0].details == {"reason": "forge_not_consulted"}


def test_offline_adds_its_warning(repo: GitRepo) -> None:
    result = run_op(repo, check=True, env={_meta.ENV_PREFIX + "OFFLINE": "1"})
    assert codes(result.warnings) == ["W_FORGE_CAPABILITY_UNKNOWN", "W_NET_OFFLINE"]


# -- entradas inválidas -------------------------------------------------------------


@pytest.mark.parametrize(
    ("files", "path"),
    [
        ({"roles.yaml": "version: 1\npeople: 3\n"}, "roles.yaml"),
        ({"project.yaml": "convention: 3\n"}, "project.yaml"),
    ],
)
def test_invalid_inputs_give_config_invalid(repo: GitRepo, files: Mapping[str, str], path: str) -> None:
    repo.write(files)
    result = run_op(repo, check=True)
    assert_valid(result)
    assert result.error is not None and result.error.code == "E_CONFIG_INVALID"
    assert result.error.details is not None and result.error.details["path"] == path
    findings = result.error.details["findings"]
    assert isinstance(findings, list) and findings and findings[0]["code"].startswith(("E_VALIDATION_", "E_CONFIG_"))
    assert result.error.details["code"] == findings[0]["code"]  # 00-marco §5.11
    assert all("message" not in f for f in findings)  # details igual en todos los idiomas (REQ-OPS-6)
    assert result.exit_code() == 8


def test_missing_roles_yaml_gives_config_invalid(repo: GitRepo) -> None:
    (repo.path / "roles.yaml").unlink()
    result = run_op(repo, check=True)
    assert result.error is not None and result.error.code == "E_CONFIG_INVALID"
    assert result.error.details == {"path": "roles.yaml", "reason": "roles_missing"}


def test_outside_a_project(tmp_path: Path) -> None:
    result = run_op(tmp_path, check=True)
    assert result.error is not None and result.error.code == "E_CONFIG_NOT_A_PROJECT"


def test_check_must_be_boolean(repo: GitRepo) -> None:
    inv = Invocation(root=str(repo.path), lang="es", json=True, dry_run=False, surface="cli", env=clean_env(repo.env))
    result = invoke(SPEC, OpContext(inv), check="yes")
    assert result.error is not None and result.error.code == "E_VALIDATION_INPUT"


# -- tamaño del sobre -----------------------------------------------------------------


def test_fit_diff_trims_to_the_budget() -> None:
    t = translator("es")
    diff = "".join(f"+línea {i} con algo de texto\n" for i in range(2000))

    def build(text: str | None, truncated: bool) -> Result:
        data = {"check_only": True, "changed": True, "files": [], "diff": text, "unknown_logins": []}
        extra = [make_warning("W_OUTPUT_TRUNCATED", t, details={"trimmed": ["diff"]})] if truncated else []
        return fail(op.OP, make_error("E_VALIDATION_ROLES_OUT_OF_SYNC", t), data=data, warnings=extra)

    result = op.fit_diff(build, diff, budget=4000)
    size = len(json.dumps(result.to_dict(), ensure_ascii=False, separators=(",", ":")))
    assert size <= 4000
    assert codes(result.warnings) == ["W_OUTPUT_TRUNCATED"]
    assert result.data is not None and "[...truncated" in result.data["diff"]
    assert validate_result(result.to_dict()) == []
    small = op.fit_diff(lambda text, tr: ok(op.OP, {"diff": text}), "+x\n", budget=4000)
    assert small.data == {"diff": "+x\n"} and small.warnings == ()


def test_large_diff_is_truncated_in_the_operation(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    many = roles_fixture("basic") + "".join(
        f"  - id: extra-{i}\n    title: \"Sección adicional número {i}\"\n    owner: carla\n" for i in range(60)
    )
    repo.write({"roles.yaml": many})
    result = run_op(repo, check=True)
    envelope = assert_valid(result)
    assert len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))) <= SPEC.budget_chars  # type: ignore[operator]
    assert "W_OUTPUT_TRUNCATED" in codes(result.warnings)
    assert result.error is not None and result.error.code == "E_VALIDATION_ROLES_OUT_OF_SYNC"


def test_unified_diff_of_equal_files_is_none() -> None:
    assert op.unified_diff([FileState(CODEOWNERS, "same", b"x\n", b"x\n")]) is None
    text = op.unified_diff([FileState(CODEOWNERS, "differs", b"a\r\nb\n", b"a\nc\n")])
    assert text is not None and "-b\n+c\n" in text and "\r" not in text


# -- salida humana ----------------------------------------------------------------------


def test_render_text(repo: GitRepo) -> None:
    result = run_op(repo, check=True)
    text = op.render_text(result, translator("es"))
    lines = text.splitlines()
    assert lines[:2] == [f"por actualizar: {CODEOWNERS}", f"por actualizar: {TASK_YML}"]
    assert any(line.startswith("+++ b/") for line in lines)
    seed_identity(repo, "ana")
    run_op(repo)
    text = op.render_text(run_op(repo, lang="en"), translator("en"))
    assert text.splitlines() == [f"unchanged: {CODEOWNERS}", f"unchanged: {TASK_YML}"]
