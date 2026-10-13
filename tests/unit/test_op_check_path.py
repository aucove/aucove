# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Operación ``check-path`` (05 §9.8; 06 §2.1 y §2.4; REQ-OPS-44; D-34 n).

Los casos 1-20 de la tabla de 06 §2.4 se siembran en un repositorio git real con los
roles del fixture ``basic``, la base en ``origin/main`` y las cachés de identidad y de
tarea en ``.aucove/cache``. La operación siempre usa ``mode="session"``: las filas que
la tabla da en ``ci`` o ``commit`` (11, 13, 16 y 16b) se comprueban con el resultado que
corresponde a ``session``.
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from aucove import _meta
from aucove.config.schemas import validate_result
from aucove.i18n import translator
from aucove.localstore.store import IdentityCache, Store, TaskCache
from aucove.ops import check_path as op
from aucove.ops import context as context_mod
from aucove.ops.context import Invocation, OpContext
from aucove.ops.registry import REGISTRY, invoke
from aucove.ops.result import Result
from aucove.render.generated import GENERATED_PATHS
from tests.conftest import FIXTURES, GitRepo, isolated_git_env, roles_fixture

SPEC = next(s for s in REGISTRY if s.id == "check-path")
FAR = "2099-01-01T00:00:00Z"
FETCHED = "2026-09-30T12:00:00Z"
PROJECT = (FIXTURES / "config" / "project" / "minimal.yaml").read_text(encoding="utf-8")
CODEOWNERS, TASK_YML = GENERATED_PATHS

# Tareas de 06 §2.4: todas en estado claimed.
TASKS: Mapping[int, tuple[str, tuple[str, ...]]] = {
    12: ("carla", ("methods",)),
    15: ("bruno", ("results", "methods")),
    18: ("ana", ("introduction",)),
    22: ("carla", ("results",)),
}

BASE_FILES: Mapping[str, str | bytes] = {
    "project.yaml": PROJECT,
    "roles.yaml": roles_fixture("basic"),
    "plan.md": "# Plan\n",
    "tex/notation.tex": "% notación\n",
    "sections/introduction/introduction.tex": "% intro\n",
    "sections/methods/main.tex": "% métodos\n",
    "sections/results/a.tex": "% resultados\n",
    "sections/results/fig1.pdf": b"%PDF-1.4\n",
    "memory/decisions/2026-09-01-scope.md": "---\ntitle: Alcance\n---\n",
    "memory/notes/task-15.md": "# Nota de la tarea 15\n",
    ".claude/settings.json": "{}\n",
    CODEOWNERS: "*   @ana @bruno\n",
}


def clean_env(env: Mapping[str, str]) -> dict[str, str]:
    """Entorno sin variables propias heredadas (``<PREFIJO>*``) ni de Claude Code."""
    drop = (_meta.ENV_PREFIX, "CLAUDE_CODE_")
    return {k: v for k, v in env.items() if not k.upper().startswith(drop)}


def new_repo(tmp: Path) -> GitRepo:
    """Repositorio ``main`` con ``origin`` bare local, igual que el fixture ``git_repo``."""
    env = isolated_git_env(tmp / "git-env")
    path = tmp / "repo"
    origin = tmp / "origin.git"
    path.mkdir(parents=True)
    repo = GitRepo(path, origin, env)
    repo._run(tmp, ["init", "-q", "--bare", "-b", "main", str(origin)], True)
    repo.git("init", "-q", "-b", "main")
    repo.git("remote", "add", "origin", str(origin))
    return repo


def publish_base(repo: GitRepo, files: Mapping[str, str | bytes] = BASE_FILES) -> str:
    sha = repo.commit("base", files=files)
    repo.push("main")
    repo.fetch()
    return sha


def seed_identity(repo: GitRepo, login: str | None, *, expires_at: str = FAR) -> None:
    store = Store(repo.path)
    if login is None:
        path = store.path("identity.json")
        if path.exists():
            path.unlink()
        return
    store.write_identity(IdentityCache(login, 1000, expires_at), token_fp=None)


def seed_tasks(repo: GitRepo, tasks: Mapping[int, tuple[str, tuple[str, ...]]] = TASKS, **kw: object) -> None:
    store = Store(repo.path)
    for n, (claimant, sections) in tasks.items():
        branch = kw.get("local_branch", f"task/{n}")
        store.write_task(TaskCache(n, claimant, "claimed", sections, branch, FETCHED))  # type: ignore[arg-type]


def run_op(
    repo: GitRepo,
    *paths: str,
    task: int | None = None,
    lang: str = "es",
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Result:
    inv = Invocation(
        root=str(repo.path),
        lang=lang,
        json=True,
        dry_run=False,
        surface="mcp",
        env={**clean_env(repo.env), **(env or {})},
        cwd=str(cwd or repo.path),
    )
    return invoke(SPEC, OpContext(inv), paths=list(paths), task=task)


def assert_valid(result: Result) -> dict[str, object]:
    envelope = result.to_dict()
    assert validate_result(envelope) == [], json.dumps(envelope, ensure_ascii=False, indent=1)
    return envelope


@pytest.fixture(scope="module")
def base_repo(tmp_path_factory: pytest.TempPathFactory) -> GitRepo:
    """Base compartida por los casos de la tabla; siempre en ``main``."""
    repo = new_repo(tmp_path_factory.mktemp("check-path"))
    publish_base(repo)
    seed_tasks(repo)
    return repo


@pytest.fixture
def repo(tmp_path: Path) -> GitRepo:
    """Repositorio propio para las pruebas que cambian de rama o de base."""
    r = new_repo(tmp_path)
    publish_base(r)
    seed_tasks(r)
    return r


# -- tabla de 06 §2.4 ------------------------------------------------------------------


@dataclass(frozen=True)
class Case:
    id: str
    login: str
    raw: str
    task: int | None
    allowed: bool
    code: str | None
    cls: str
    path: str | None = None  # ruta canónica esperada (por defecto, la entrada)
    section: str | None = None
    owner: str | None = None
    confirm: bool = False


CASES: tuple[Case, ...] = (
    Case("1", "carla", "sections/methods/main.tex", 12, True, None, "section", section="methods", owner="carla"),
    Case("2", "carla", "sections/results/fig1.pdf", 12, False, "E_PERM_OUTSIDE_TASK", "section", section="results", owner="bruno"),
    Case("3", "bruno", "sections/methods/main.tex", 15, False, "E_PERM_SECTION_NOT_OWNED", "section", section="methods", owner="carla"),
    Case("4", "carla", "sections/results/a.tex", 22, False, "E_PERM_SECTION_NOT_OWNED", "section", section="results", owner="bruno"),
    Case("5", "bruno", "sections/methods/main.tex", 12, False, "E_CLAIM_NOT_HELD", "section", section="methods", owner="carla"),
    Case("6", "carla", "sections/methods/main.tex", None, False, "E_STATE_NOT_ON_TASK_BRANCH", "section", section="methods", owner="carla"),
    Case("7", "carla", "tex\\sections\\methods\\..\\..\\..\\plan.md", 12, False, "E_PERM_LEAD_ONLY_PATH", "lead", path="plan.md"),
    Case("8", "ana", "plan.md", 18, True, None, "lead"),
    Case("9", "ana", "roles.yaml", 18, True, None, "lead", confirm=True),
    Case("9b", "ana", ".claude/settings.json", 18, True, None, "lead", confirm=True),
    Case("10", "bruno", "tex/notation.tex", 15, True, None, "editor"),
    Case("11", "carla", "roles.yaml", 12, False, "E_PERM_LEAD_ONLY_PATH", "lead"),  # ci en la tabla
    Case("12", "carla", "memory/decisions/2026-09-29-prior.md", 12, True, None, "append"),
    Case("13", "carla", "memory/decisions/2026-09-01-scope.md", 12, False, "E_PERM_APPEND_ONLY", "append"),  # ci
    Case("14", "carla", "memory/notes/task-15.md", 12, False, "E_PERM_OUTSIDE_TASK", "task"),
    Case("15", "ana", CODEOWNERS, 18, False, "E_PERM_GENERATED_PATH", "generated"),
    Case("16", "ana", CODEOWNERS, 18, False, "E_PERM_GENERATED_PATH", "generated"),  # ci: en session no
    Case("16b", "ana", CODEOWNERS, 18, False, "E_PERM_GENERATED_PATH", "generated"),  # commit: en session no
    Case("17a", "carla", ".claude/settings.local.json", None, False, "E_PERM_LOCAL_PATH", "local"),
    Case("17b", "carla", f"{_meta.CACHE_DIR}/cache/identity.json", None, False, "E_PERM_LOCAL_PATH", "local"),
    Case("18", "ana", "sections/discussion/draft.tex", 18, True, None, "lead"),
    Case("19", "dave", "sections/methods/main.tex", None, False, "E_PERM_NOT_A_MEMBER", "section", section="methods", owner="carla"),
    Case("20", "carla", ".git/hooks/pre-commit", 12, False, "E_PERM_OUTSIDE_REPO", "lead"),
)


def test_table_covers_rows_1_to_20() -> None:
    rows = {c.id.rstrip("ab") for c in CASES}
    assert rows == {str(n) for n in range(1, 21)}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_table_of_06(base_repo: GitRepo, case: Case) -> None:
    seed_identity(base_repo, case.login)
    result = run_op(base_repo, case.raw, task=case.task)
    envelope = assert_valid(result)
    assert result.ok and result.error is None and result.exit_code() == 0  # una denegación no es error
    data = envelope["data"]
    assert isinstance(data, dict)
    assert data["basis"] == {
        "base_ref": "origin/main",
        "base_sha": base_repo.git("rev-parse", "origin/main"),
        "identity": case.login,
        "task": case.task,
        "source": "cache" if case.task is not None else "local",
    }
    (item,) = data["results"]  # type: ignore[misc]
    assert item == {
        "path": case.path or case.raw,
        "class": case.cls,
        "allowed": case.allowed,
        "reason_code": case.code,
        "section": case.section,
        "owner": case.owner,
        "confirm": case.confirm,
        "reason": item["reason"],
    }
    assert data["allowed_all"] is case.allowed
    if case.allowed and not case.confirm:
        assert item["reason"] is None
    else:
        assert isinstance(item["reason"], str) and item["reason"]


def test_reasons_are_localized(base_repo: GitRepo) -> None:
    seed_identity(base_repo, "bruno")
    es = run_op(base_repo, "sections/methods/main.tex", task=15, lang="es").data
    en = run_op(base_repo, "sections/methods/main.tex", task=15, lang="en").data
    assert es is not None and en is not None
    assert es["results"][0]["reason"] == "La parte methods es de @carla."
    assert en["results"][0]["reason"] == "Part methods belongs to @carla."
    seed_identity(base_repo, "ana")
    confirm = run_op(base_repo, "roles.yaml", task=18).data
    assert confirm is not None and "confirmación" in confirm["results"][0]["reason"]


def test_several_paths_keep_their_order(base_repo: GitRepo) -> None:
    seed_identity(base_repo, "carla")
    raws = ["sections/methods/main.tex", "sections/results/a.tex", "../fuera.txt", "plan.md"]
    result = run_op(base_repo, *raws, task=12)
    data = assert_valid(result)["data"]
    assert isinstance(data, dict)
    items = data["results"]
    assert [i["path"] for i in items] == raws
    assert [i["allowed"] for i in items] == [True, False, False, False]
    assert items[2]["class"] == "outside" and items[2]["reason_code"] == "E_PERM_OUTSIDE_REPO"
    assert items[2]["section"] is None and items[2]["owner"] is None
    assert data["allowed_all"] is False


def test_relative_paths_are_taken_from_the_current_directory(base_repo: GitRepo) -> None:
    seed_identity(base_repo, "carla")
    cwd = base_repo.path / "sections" / "methods"
    data = run_op(base_repo, "main.tex", "../results/a.tex", task=12, cwd=cwd).data
    assert data is not None
    assert [(i["path"], i["allowed"]) for i in data["results"]] == [
        ("sections/methods/main.tex", True),
        ("sections/results/a.tex", False),
    ]
    absolute = str(base_repo.path / "sections" / "methods" / "main.tex")
    data = run_op(base_repo, absolute, task=12).data
    assert data is not None and data["results"][0]["path"] == "sections/methods/main.tex"


@pytest.mark.skipif(not op.case_insensitive(Path.cwd()), reason="sistema de archivos que distingue mayúsculas")
def test_case_map_on_case_insensitive_fs(base_repo: GitRepo) -> None:
    seed_identity(base_repo, "carla")
    data = run_op(base_repo, "Sections/METHODS/main.tex", task=12).data
    assert data is not None
    assert data["results"][0]["path"] == "sections/methods/main.tex"
    assert data["results"][0]["allowed"] is True


# -- sin identidad (D-34 n) -----------------------------------------------------------


@pytest.mark.parametrize("expired", [False, True], ids=["missing", "expired"])
def test_without_identity_every_path_is_not_a_member(base_repo: GitRepo, expired: bool) -> None:
    if expired:
        seed_identity(base_repo, "ana", expires_at="2020-01-01T00:00:00Z")
    else:
        seed_identity(base_repo, None)
    raws = ["plan.md", "sections/methods/main.tex", ".git/config", "../fuera.txt", CODEOWNERS]
    result = run_op(base_repo, *raws, task=18)
    data = assert_valid(result)["data"]
    assert result.ok
    assert isinstance(data, dict)
    assert data["basis"]["identity"] is None
    assert data["allowed_all"] is False
    items = data["results"]
    assert {i["reason_code"] for i in items} == {"E_PERM_NOT_A_MEMBER"}
    assert {i["reason"] for i in items} == {translator("es")("roles.check_path.reason.no_identity")}
    assert [i["class"] for i in items] == ["lead", "section", "lead", "outside", "generated"]
    assert items[1]["section"] == "methods" and items[1]["owner"] == "carla"


def test_without_identity_and_without_task_cache_still_answers(repo: GitRepo) -> None:
    seed_identity(repo, None)
    repo.git("checkout", "-q", "-b", "task/31")
    result = run_op(repo, "plan.md")
    assert result.ok and result.data is not None
    assert result.data["results"][0]["reason_code"] == "E_PERM_NOT_A_MEMBER"
    assert result.data["basis"]["task"] == 31


# -- tarea de la rama y caché de tarea --------------------------------------------------


def test_task_comes_from_the_current_branch(repo: GitRepo) -> None:
    seed_identity(repo, "carla")
    repo.git("checkout", "-q", "-b", "task/12")
    data = assert_valid(run_op(repo, "sections/methods/main.tex", "sections/results/a.tex"))["data"]
    assert isinstance(data, dict)
    assert data["basis"]["task"] == 12 and data["basis"]["source"] == "cache"
    assert [i["allowed"] for i in data["results"]] == [True, False]
    # --task tiene prioridad sobre la rama
    data = run_op(repo, "sections/results/a.tex", task=22).data
    assert data is not None and data["basis"]["task"] == 22
    assert data["results"][0]["reason_code"] == "E_PERM_SECTION_NOT_OWNED"


def test_detached_head_or_other_branch_has_no_task(repo: GitRepo) -> None:
    seed_identity(repo, "carla")
    repo.git("checkout", "-q", "--detach", "HEAD")
    data = run_op(repo, "sections/methods/main.tex").data
    assert data is not None and data["basis"]["task"] is None
    assert data["results"][0]["reason_code"] == "E_STATE_NOT_ON_TASK_BRANCH"
    repo.git("checkout", "-q", "-b", "feature/x")
    data = run_op(repo, "sections/methods/main.tex").data
    assert data is not None and data["results"][0]["reason_code"] == "E_STATE_NOT_ON_TASK_BRANCH"


def test_cloud_session_finds_the_task_by_local_branch(repo: GitRepo) -> None:
    seed_identity(repo, "carla")
    seed_tasks(repo, {12: TASKS[12]}, local_branch="claude/fix-methods")
    repo.git("checkout", "-q", "-b", "claude/fix-methods")
    data = run_op(repo, "sections/methods/main.tex", env={"CLAUDE_CODE_REMOTE": "true"}).data
    assert data is not None and data["basis"]["task"] == 12
    assert data["results"][0]["allowed"] is True
    # fuera de la nube, una rama que no es task/<n> no da tarea
    data = run_op(repo, "sections/methods/main.tex").data
    assert data is not None and data["basis"]["task"] is None


@pytest.mark.parametrize("via", ["branch", "flag"])
def test_active_task_without_cache_fails_with_switch_hint(repo: GitRepo, via: str) -> None:
    seed_identity(repo, "carla")
    if via == "branch":
        repo.git("checkout", "-q", "-b", "task/30")
        result = run_op(repo, "sections/methods/main.tex")
    else:
        result = run_op(repo, "sections/methods/main.tex", task=30)
    envelope = assert_valid(result)
    assert not result.ok and result.error is not None
    assert result.error.code == "E_CONFIG_POLICY_UNAVAILABLE"
    assert result.error.hint is not None and f"{_meta.NAME} switch <n>" in result.error.hint
    assert result.error.details == {"reason": "task_cache_missing", "task": 30}
    assert result.exit_code() == 8
    assert envelope["data"] is None


def test_invalid_task_cache_counts_as_missing(repo: GitRepo) -> None:
    seed_identity(repo, "carla")
    path = Store(repo.path).path("tasks/12.json")
    path.write_text("{no es json", encoding="utf-8")
    result = run_op(repo, "sections/methods/main.tex", task=12)
    assert result.error is not None and result.error.code == "E_CONFIG_POLICY_UNAVAILABLE"


@pytest.mark.parametrize("state", ["in_review", "merged"])
def test_task_state_comes_from_the_cache(repo: GitRepo, state: str) -> None:
    seed_identity(repo, "carla")
    Store(repo.path).write_task(TaskCache(12, "carla", state, ("methods",), "task/12", FETCHED))
    data = run_op(repo, "sections/methods/main.tex", task=12).data
    assert data is not None
    item = data["results"][0]
    if state == "in_review":
        assert item["allowed"] is True  # in_review admite escrituras (06 §2.4, WRITABLE)
    else:
        assert item["reason_code"] == "E_STATE_INVALID_TRANSITION"
        assert "merged" in item["reason"]


# -- política de la base local (REQ-OPS-44, D-08) ----------------------------------------


def test_policy_is_the_local_base_not_the_working_tree(repo: GitRepo) -> None:
    seed_identity(repo, "carla")
    hacked = roles_fixture("basic").replace("owner: bruno", "owner: carla")
    repo.write({"roles.yaml": hacked})  # sin commit ni push: no cambia la base
    data = run_op(repo, "sections/results/a.tex", task=22).data
    assert data is not None
    assert data["results"][0]["reason_code"] == "E_PERM_SECTION_NOT_OWNED"
    assert data["results"][0]["owner"] == "bruno"


def test_policy_fallback_adds_its_warning(repo: GitRepo) -> None:
    seed_identity(repo, "carla")
    repo.commit("roles roto", files={"roles.yaml": "version: 1\npeople: 3\n"})
    repo.push("main")
    repo.fetch()
    result = run_op(repo, "sections/methods/main.tex", task=12)
    assert_valid(result)
    assert result.ok and result.data is not None and result.data["results"][0]["allowed"] is True
    assert [w.code for w in result.warnings] == ["W_CONFIG_POLICY_FALLBACK"]
    assert result.warnings[0].details is not None and len(str(result.warnings[0].details["sha"])) == 40


def test_fallback_warning_carries_each_file_sha(tmp_path: Path) -> None:
    """REQ-PERM-4: el aviso da el sha usado de cada archivo, no solo el de ``roles.yaml``."""
    from tests.unit.test_base_snapshot import BAD_PROJECT, BAD_ROLES

    repo = new_repo(tmp_path)
    c1 = publish_base(repo)
    c2 = publish_base(repo, {"roles.yaml": roles_fixture("no-deputy")})
    publish_base(repo, {"project.yaml": BAD_PROJECT, "roles.yaml": BAD_ROLES})
    seed_tasks(repo)
    seed_identity(repo, "carla")
    repo.write({"project.yaml": PROJECT})  # el árbol válido: solo la base usa el respaldo
    result = run_op(repo, "sections/methods/main.tex", task=12)
    assert_valid(result)
    assert result.ok and result.data is not None and result.data["results"][0]["allowed"] is True
    warns = [w for w in result.warnings if w.code == "W_CONFIG_POLICY_FALLBACK"]
    assert len(warns) == 1, result.to_dict()
    assert warns[0].details == {"sha": c2, "files": {"project.yaml": c1, "roles.yaml": c2}}, result.to_dict()


# -- identidad por forge_id (08-forja §6.3 y §6.4; REQ-FORGE-48) --------------------------

FORGE_ID_ROLES = roles_fixture("basic").replace("  - login: bruno\n", "  - login: bruno\n    forge_id: 111\n")


def forge_id_repo(tmp: Path) -> GitRepo:
    """Base con ``forge_id: 111`` para bruno y las tareas de 06 §2.4 en la caché."""
    assert FORGE_ID_ROLES != roles_fixture("basic")
    r = new_repo(tmp)
    publish_base(r, {**BASE_FILES, "roles.yaml": FORGE_ID_ROLES})
    seed_tasks(r)
    return r


def seed_account(repo: GitRepo, login: str, ident: int | None) -> None:
    """Identidad en caché con el ``id`` de la cuenta (08-forja §6.2)."""
    Store(repo.path).write_identity(IdentityCache(login, ident, FAR), token_fp=None)


def test_same_login_other_id_is_denied(tmp_path: Path) -> None:
    """Otra cuenta (id 999) con el login de bruno no hereda sus rutas: no es miembro."""
    repo = forge_id_repo(tmp_path)
    seed_account(repo, "bruno", 999)
    raws = ["sections/results/a.tex", "plan.md", "sections/methods/main.tex"]
    result = run_op(repo, *raws, task=15)
    data = assert_valid(result)["data"]
    assert result.ok and isinstance(data, dict), result.to_dict()
    assert data["basis"]["identity"] == "bruno"  # el login de la caché
    assert data["allowed_all"] is False
    items = data["results"]
    assert [i["allowed"] for i in items] == [False, False, False], result.to_dict()
    assert {i["reason_code"] for i in items} == {"E_PERM_NOT_A_MEMBER"}
    assert {i["reason"] for i in items} == {translator("es")("roles.check_path.reason.not_a_member")}
    assert (items[0]["class"], items[0]["section"], items[0]["owner"]) == ("section", "results", "bruno")
    assert "W_AUTH_LOGIN_CHANGED" not in [w.code for w in result.warnings]


def test_same_id_other_login_is_bruno_with_a_warning(tmp_path: Path) -> None:
    """bruno renombró su cuenta a bruno2 (mismo id 111): sigue siendo bruno, con aviso."""
    repo = forge_id_repo(tmp_path)
    seed_account(repo, "bruno2", 111)
    result = run_op(repo, "sections/results/a.tex", "sections/methods/main.tex", task=15)
    data = assert_valid(result)["data"]
    assert result.ok and isinstance(data, dict), result.to_dict()
    assert data["basis"]["identity"] == "bruno2"  # basis muestra el login de la caché
    items = data["results"]
    assert items[0]["allowed"] is True, result.to_dict()
    assert items[1]["reason_code"] == "E_PERM_SECTION_NOT_OWNED"  # se evalúa como bruno
    warns = [w for w in result.warnings if w.code == "W_AUTH_LOGIN_CHANGED"]
    assert len(warns) == 1, result.to_dict()
    assert warns[0].details == {"login": "bruno2", "roles_login": "bruno"}


def test_same_id_and_login_has_no_warning(tmp_path: Path) -> None:
    repo = forge_id_repo(tmp_path)
    seed_account(repo, "Bruno", 111)
    result = run_op(repo, "sections/results/a.tex", task=15)
    assert result.ok and result.data is not None and result.data["results"][0]["allowed"] is True
    assert [w.code for w in result.warnings] == []


def test_without_id_in_the_cache_the_login_decides(tmp_path: Path) -> None:
    """Sin ``id`` en ``identity.json`` no se puede comparar por id: decide el login."""
    repo = forge_id_repo(tmp_path)
    seed_account(repo, "bruno", None)
    result = run_op(repo, "sections/results/a.tex", task=15)
    assert result.ok and result.data is not None and result.data["results"][0]["allowed"] is True


def test_non_member_still_needs_the_task_cache(tmp_path: Path) -> None:
    """Con identidad, la tarea se lee de la caché aunque la cuenta no sea miembro (05 §9.8)."""
    repo = forge_id_repo(tmp_path)
    seed_account(repo, "bruno", 999)
    result = run_op(repo, "plan.md", task=30)
    assert result.error is not None and result.error.code == "E_CONFIG_POLICY_UNAVAILABLE"


# -- paso 1 de 06 §2.4 y nombres no portables -------------------------------------------


GIT_PATHS = (".git/config", "x/.GIT/hooks/y", "sections/methods/.git/config")


@pytest.mark.parametrize("raw", GIT_PATHS)
def test_git_paths_are_lead_like_in_the_domain(base_repo: GitRepo, raw: str) -> None:
    seed_identity(base_repo, "carla")
    result = run_op(base_repo, raw, task=12)
    assert_valid(result)
    assert result.data is not None
    (item,) = result.data["results"]
    assert (item["class"], item["reason_code"], item["section"], item["owner"]) == (
        "lead", "E_PERM_OUTSIDE_REPO", None, None,
    )


def test_classify_delegates_step_1_to_the_domain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_classify`` no repite el paso 1 con su propia comparación: lo resuelve ``classify`` (R0)."""
    from aucove.domain.permissions import Policy

    pol = Policy(
        people=frozenset({"carla"}), lead="ana", deputy=None, sections={"methods": "carla"},
        base_decisions=frozenset(), root=str(tmp_path), ci_fs=False,
    )
    seen: list[str] = []
    real = op.classify

    def spy(rel: str, declared: frozenset[str], symlinked: bool):  # type: ignore[no-untyped-def]
        seen.append(rel)
        return real(rel, declared, symlinked)

    monkeypatch.setattr(op, "classify", spy)
    for raw in GIT_PATHS:
        assert op._classify(raw, pol) == (raw, "lead", None)
    assert seen == list(GIT_PATHS)


@pytest.mark.parametrize("name", ["aux.tex", "con.tex", "nul", "x.tex."])
@pytest.mark.parametrize("lang", ["es", "en"])
def test_non_portable_name_reason_mentions_windows_names(base_repo: GitRepo, name: str, lang: str) -> None:
    """Un nombre que Windows no admite da ``outside`` y el motivo lo dice (no solo «fuera»)."""
    seed_identity(base_repo, "carla")
    raw = f"sections/methods/{name}"
    result = run_op(base_repo, raw, task=12, lang=lang)
    assert_valid(result)
    assert result.data is not None
    (item,) = result.data["results"]
    assert (item["class"], item["reason_code"], item["path"]) == ("outside", "E_PERM_OUTSIDE_REPO", raw)
    reason = str(item["reason"])
    assert reason == translator(lang)("roles.check_path.reason.outside_repo")
    for token in ("Windows", "`.git`", "`CON`", "`AUX`", "`NUL`", "`COM1`", "`:`", "`GIT~1`"):
        assert token in reason, (token, reason)


def test_without_fetched_base_the_policy_is_unavailable(tmp_path: Path) -> None:
    repo = new_repo(tmp_path)
    repo.commit("local", files=BASE_FILES)  # nunca se hizo push ni fetch
    seed_identity(repo, "ana")
    result = run_op(repo, "plan.md")
    assert_valid(result)
    assert result.error is not None and result.error.code == "E_CONFIG_POLICY_UNAVAILABLE"


def test_outside_a_project(tmp_path: Path) -> None:
    inv = Invocation(root=str(tmp_path), lang="es", json=True, dry_run=False, surface="cli", env={}, cwd=str(tmp_path))
    result = invoke(SPEC, OpContext(inv), paths=["plan.md"], task=None)
    assert_valid(result)
    assert result.error is not None and result.error.code == "E_CONFIG_NOT_A_PROJECT"


def test_never_calls_the_forge(base_repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("check-path no debe llamar a la forja (REQ-OPS-44)")

    monkeypatch.setattr(OpContext, "forge", boom)
    monkeypatch.setattr(context_mod, "context_factory", boom)
    seed_identity(base_repo, "carla")
    for case in CASES[:6]:
        seed_identity(base_repo, case.login)
        result = run_op(base_repo, case.raw, task=case.task)
        assert result.ok, result.to_dict()
    seed_identity(base_repo, None)
    assert run_op(base_repo, "plan.md").ok


# -- entrada --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("paths", "task", "param", "reason"),
    [
        ([], None, "paths", "minItems"),
        (["x"] * 101, None, "paths", "maxItems"),
        ([""], None, "paths", "minLength"),
        ([3], None, "paths", "type"),
        (None, None, "paths", "required"),
        (["plan.md"], 0, "task", "minimum"),
        (["plan.md"], True, "task", "type"),
    ],
)
def test_invalid_input(base_repo: GitRepo, paths: object, task: object, param: str, reason: str) -> None:
    inv = Invocation(
        root=str(base_repo.path), lang="es", json=True, dry_run=False, surface="mcp",
        env=clean_env(base_repo.env), cwd=str(base_repo.path),
    )
    result = invoke(SPEC, OpContext(inv), paths=paths, task=task)
    assert_valid(result)
    assert result.error is not None and result.error.code == "E_VALIDATION_INPUT"
    assert result.error.details == {"errors": [{"param": param, "reason": reason}]}
    assert result.exit_code() == 3


def test_prepare_handles_long_and_relative_paths(tmp_path: Path) -> None:
    root = tmp_path / "r"
    (root / "tex").mkdir(parents=True)
    assert op.prepare("a.tex", root, str(root / "tex")) == "tex/a.tex"
    assert op.prepare("a.tex", root, str(root)) == "a.tex"
    assert op.prepare("a.tex", root, str(tmp_path)) == "a.tex"  # cwd fuera: desde la raíz
    assert op.prepare("/abs/x", root, str(root / "tex")) == "/abs/x"
    assert op.prepare("\\\\?\\C:\\x\\y", root, str(root)) == "C:\\x\\y"
    assert op.prepare("\\\\?\\UNC\\srv\\share\\y", root, str(root)) == "\\\\srv\\share\\y"  # UNC, no relativa
    assert op.prepare("\\\\?\\Volume{1}\\y", root, str(root)) == "\\\\?\\Volume{1}\\y"  # absoluta: fuera
    assert op.prepare("Q:y", Path("C:\\r"), "C:\\r\\tex") is None  # otra unidad: destino desconocido
    assert op.prepare("C:y", Path("C:\\r"), "C:\\r\\tex") == "C:\\r\\tex\\y"  # misma unidad: desde cwd


# -- superficies ----------------------------------------------------------------------


def test_cli_json_exits_0_with_a_denied_path(base_repo: GitRepo) -> None:
    from aucove.cli.main import run as cli_run

    seed_identity(base_repo, "carla")
    out, err = io.StringIO(), io.StringIO()
    code = cli_run(
        ["--json", "--lang", "es", "--root", str(base_repo.path), "check-path", "sections/results/a.tex", "--task", "12"],
        env=clean_env(base_repo.env),
        stdout=out,
        stderr=err,
    )
    assert code == 0, err.getvalue()
    envelope = json.loads(out.getvalue())
    assert validate_result(envelope) == []
    assert envelope["ok"] is True and envelope["data"]["allowed_all"] is False
    assert envelope["data"]["results"][0]["reason_code"] == "E_PERM_OUTSIDE_TASK"


def test_render_text(base_repo: GitRepo) -> None:
    seed_identity(base_repo, "carla")
    result = run_op(base_repo, "sections/methods/main.tex", "sections/results/a.tex", task=12)
    text = op.render_text(result, translator("es"))
    lines = text.splitlines()
    assert lines[0] == "Base origin/main · identidad @carla · tarea #12"
    assert lines[1] == "permitida: sections/methods/main.tex (section)"
    assert lines[2] == "denegada: sections/results/a.tex (section)"
    assert lines[3].startswith("  [E_PERM_OUTSIDE_TASK] ")
    seed_identity(base_repo, None)
    text = op.render_text(run_op(base_repo, "plan.md", lang="en"), translator("en"))
    assert text.splitlines()[0] == "Base origin/main · identity unknown · task none"


def test_module_does_not_import_the_forge() -> None:
    source = Path(op.__file__).read_text(encoding="utf-8")
    imported = {
        seg
        for line in source.splitlines()
        if line.startswith(("from ", "import "))
        for seg in line.replace(".", " ").split()
    }
    assert "forge" not in imported
