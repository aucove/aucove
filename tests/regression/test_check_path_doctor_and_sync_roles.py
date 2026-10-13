# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Check-path, doctor, sync-roles y la regla roles-sync.

Cada sección reproduce un defecto ya corregido:

1. ``check-path`` quitaba ``\\\\?\\`` también delante de ``UNC\\`` y de los volúmenes, y
   tomaba ``D:foo`` (ruta relativa a una unidad) como relativa al directorio actual: las
   autorizaba como rutas del repositorio (02 REQ-CONV-7; 07-hooks §7).
2. ``doctor`` daba verde con la política de la base ilegible: volvía en silencio al
   ``roles.yaml`` y al ``project.yaml`` del árbol (D-08; 01 REQ-ARCH-46).
3. ``sync-roles`` escribía una parte y fallaba sin ``data``; ``--dry-run`` decía ok en el
   mismo caso (05 REQ-OPS-30).
4. En un sistema de archivos sin distinción de mayúsculas, ``sync-roles`` y ``doctor`` veían
   sincronizado un ``.github/codeowners`` que la regla rechaza.
5. ``E_CONFIG_INVALID`` de ``sync-roles`` traía texto traducido en ``details`` y no traía
   ``details.code`` (05 REQ-OPS-6; 00-marco §5.11).
6. ``E_PERM_ROLE_REQUIRED`` usaba ``details.required`` en lugar de ``details.required_role``
   (05 §12.1).
"""

from __future__ import annotations

import ntpath
import os
import stat
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from aucove.config.roles import parse_roles
from aucove.domain.paths import OutsideRepo, canonical
from aucove.i18n import translator
from aucove.localstore.store import IdentityCache, Store
from aucove.ops import check_path as cp_op
from aucove.ops import sync_roles as sr_op
from aucove.ops.result import Result
from aucove.ops.rules import roles_sync
from aucove.ops.rules.base import RuleInput
from aucove.vcs.tree import worktree
from tests.conftest import GitRepo, roles_fixture
from tests.unit import test_op_check_path as cp
from tests.unit import test_op_doctor as dr
from tests.unit import test_op_sync_roles as sr

CODEOWNERS, TASK_YML = sr.CODEOWNERS, sr.TASK_YML
OUTSIDE = "E_PERM_OUTSIDE_REPO"
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def _ci_fs(path: Path) -> bool:
    return cp_op.case_insensitive(path)


def _dir_link(link: Path, target: Path) -> str:
    """Enlace a una carpeta: simbólico si se puede; en Windows, si no, una unión."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError):
        if os.name != "nt":
            raise
    import _winapi

    _winapi.CreateJunction(str(target), str(link))  # no requiere privilegios
    return "junction"


# -- 1. check-path: \\?\UNC\, volúmenes y rutas relativas a una unidad -------------------


WIN_ROOT = Path("C:\\repo")
WIN_CWD = "C:\\repo\\sections\\methods"


@pytest.mark.parametrize("raw", ["\\\\?\\UNC\\server\\share\\x.tex", "\\\\?\\unc\\server\\share\\x.tex"])
def test_prepare_turns_the_long_unc_prefix_into_a_unc_path(raw: str) -> None:
    prepared = cp_op.prepare(raw, WIN_ROOT, WIN_CWD)
    assert prepared == "\\\\server\\share\\x.tex"
    with pytest.raises(OutsideRepo):
        canonical(prepared, str(WIN_ROOT), True)


@pytest.mark.parametrize(
    "raw", ["\\\\?\\Volume{0f1e2d3c}\\x.tex", "\\\\?\\GLOBALROOT\\Device\\x.tex", "\\\\?\\C:x.tex", "\\\\?\\UNC"]
)
def test_prepare_keeps_other_long_prefixes_absolute(raw: str) -> None:
    prepared = cp_op.prepare(raw, WIN_ROOT, WIN_CWD)
    assert prepared == raw
    with pytest.raises(OutsideRepo):
        canonical(prepared, str(WIN_ROOT), True)


def test_prepare_still_strips_the_long_prefix_before_a_drive() -> None:
    assert cp_op.prepare("\\\\?\\C:\\repo\\plan.md", WIN_ROOT, WIN_CWD) == "C:\\repo\\plan.md"


@pytest.mark.parametrize("raw", ["D:foo\\bar.tex", "d:bar.tex", "Q:", "Z:..\\repo\\plan.md"])
def test_prepare_drive_relative_on_another_drive_is_outside(raw: str) -> None:
    assert cp_op.prepare(raw, WIN_ROOT, WIN_CWD) is None


@pytest.mark.parametrize(
    ("raw", "rel"),
    [
        ("C:..\\x.tex", "sections/x.tex"),
        ("C:main.tex", "sections/methods/main.tex"),
        ("c:..\\..\\plan.md", "plan.md"),
    ],
)
def test_prepare_drive_relative_on_the_same_drive_joins_the_cwd(raw: str, rel: str) -> None:
    prepared = cp_op.prepare(raw, WIN_ROOT, WIN_CWD)
    assert prepared is not None and ntpath.isabs(prepared)
    assert canonical(prepared, str(WIN_ROOT), True) == rel


def test_prepare_drive_relative_with_the_cwd_on_a_unc_share_is_outside() -> None:
    root = Path("\\\\server\\share\\p")
    assert cp_op.prepare("C:x.tex", root, "\\\\server\\share\\p\\tex") is None


@pytest.fixture(scope="module")
def cp_repo(tmp_path_factory: pytest.TempPathFactory) -> GitRepo:
    repo = cp.new_repo(tmp_path_factory.mktemp("p14-check-path"))
    cp.publish_base(repo)
    cp.seed_tasks(repo)
    return repo


def _other_drive(path: Path) -> str:
    drive = os.path.splitdrive(str(path))[0].upper()
    return "R:" if drive == "Q:" else "Q:"


@pytest.mark.parametrize(("login", "task", "inside"), [("carla", 12, True), ("ana", 18, False)])
def test_check_path_denies_unc_volume_and_other_drive_paths(cp_repo: GitRepo, login: str, task: int, inside: bool) -> None:
    cp.seed_identity(cp_repo, login)
    cwd = cp_repo.path / "sections" / "methods" if inside else cp_repo.path
    raws = [
        "\\\\?\\UNC\\server\\share\\x.tex",
        "\\\\?\\Volume{0f1e2d3c}\\x.tex",
        f"{_other_drive(cp_repo.path)}foo\\bar.tex",
    ]
    result = cp.run_op(cp_repo, *raws, task=task, cwd=cwd)
    data = cp.assert_valid(result)["data"]
    assert result.ok and isinstance(data, dict)
    assert [(i["path"], i["class"], i["allowed"], i["reason_code"]) for i in data["results"]] == [
        (raw, "outside", False, OUTSIDE) for raw in raws
    ]
    assert data["allowed_all"] is False


def test_check_path_resolves_a_drive_relative_path_on_the_cwd_drive(cp_repo: GitRepo) -> None:
    cp.seed_identity(cp_repo, "carla")
    cwd = cp_repo.path / "sections" / "methods"
    drive = os.path.splitdrive(str(cwd))[0]
    raw = f"{drive or 'C:'}..\\x.tex"
    data = cp.run_op(cp_repo, raw, task=12, cwd=cwd).data
    assert data is not None
    (item,) = data["results"]
    if drive:  # Windows: el destino real es sections/x.tex, de clase lead
        assert (item["path"], item["class"], item["reason_code"]) == ("sections/x.tex", "lead", "E_PERM_LEAD_ONLY_PATH")
    else:  # sin unidades, 'C:' no es la unidad del directorio actual
        assert (item["path"], item["class"], item["reason_code"]) == (raw, "outside", OUTSIDE)
    assert item["allowed"] is False


# -- 2. doctor con la política de la base ilegible -------------------------------------


BROKEN_ROLES = roles_fixture("basic").replace("lead: ana", "lead: nadie")


def test_broken_roles_fixture_is_invalid() -> None:
    roles, _findings = parse_roles(BROKEN_ROLES.encode("utf-8"), file="roles.yaml")
    assert roles is None


def test_doctor_fails_when_the_base_policy_is_unavailable(tmp_path: Path) -> None:
    repo = dr.new_repo(tmp_path, extra={"roles.yaml": BROKEN_ROLES})  # ninguna versión de la base valida
    repo.write({"roles.yaml": roles_fixture("basic")})  # el árbol sí valida y los generados coinciden
    Store(repo.path).write_identity(IdentityCache("ana", 1, dr.FAR), token_fp=None)
    result = dr.run_op(repo.path, dr.env_for(tmp_path, repo.env))
    envelope = dr.assert_valid(result)
    assert not result.ok and result.error is not None
    assert result.error.code == "E_CONFIG_DOCTOR_FAILED"
    st = dr.statuses(result)
    assert st["convention"] == "fail" and st["deputy"] == "fail"
    assert st["roles_yaml"] == "ok" and st["codeowners_sync"] == "ok"  # el árbol está bien
    assert result.error.details == {"failed": ["convention", "deputy"]}
    checks = dr.checks_of(result)
    for check_id in ("convention", "deputy"):
        assert "no_valid_version" in str(checks[check_id]["message"])
        assert checks[check_id]["fix"]
    data = envelope["data"]
    assert isinstance(data, dict)
    assert data["identity"] == {"login": "ana", "source": "cache"}
    assert data["role"] is None and data["is_deputy"] is False  # el rol solo sale de la base
    assert data["convention"]["project"] is None  # type: ignore[index]


def test_doctor_without_fetched_base_does_not_use_the_tree(tmp_path: Path) -> None:
    repo = dr.new_repo(tmp_path)
    repo.git("remote", "set-head", "origin", "-d", check=False)
    repo.git("update-ref", "-d", "refs/remotes/origin/main")
    Store(repo.path).write_identity(IdentityCache("bruno", 2, dr.FAR), token_fp=None)
    result = dr.run_op(repo.path, dr.env_for(tmp_path, repo.env))
    dr.assert_valid(result)
    st = dr.statuses(result)
    assert st["convention"] == "fail" and st["deputy"] == "fail"
    assert result.data is not None and result.data["role"] is None and result.data["is_deputy"] is False


# -- 3. sync-roles: comprobar los destinos antes de escribir y también en --dry-run -------


def _errors(result: Result) -> tuple[str | None, object]:
    return (result.error.code if result.error else None, result.error.details if result.error else None)


def test_task_yml_as_a_folder_fails_the_same_in_dry_run_and_writes_nothing(tmp_path: Path) -> None:
    repo = sr.new_repo(tmp_path)
    sr.seed_identity(repo, "ana")
    (repo.path / TASK_YML).mkdir(parents=True)
    (repo.path / TASK_YML / "x.txt").write_text("x\n", encoding="utf-8")
    dry = sr.run_op(repo, dry_run=True)
    sr.assert_valid(dry)
    real = sr.run_op(repo)
    sr.assert_valid(real)
    assert not dry.ok and not real.ok
    assert _errors(dry) == _errors(real) == ("E_STATE_DIRTY_WORKTREE", {"path": TASK_YML, "reason": "not_regular"})
    assert not (repo.path / CODEOWNERS).exists()  # ninguna escritura antes de comprobar todo


def test_linked_issue_template_fails_the_same_in_dry_run_and_writes_nothing(tmp_path: Path) -> None:
    repo = sr.new_repo(tmp_path)
    sr.seed_identity(repo, "ana")
    outside = tmp_path / "fuera"
    outside.mkdir()
    (repo.path / ".github").mkdir()
    _dir_link(repo.path / ".github" / "ISSUE_TEMPLATE", outside)
    dry = sr.run_op(repo, dry_run=True)
    real = sr.run_op(repo)
    sr.assert_valid(real)
    assert _errors(dry) == _errors(real) == ("E_PERM_SYMLINK", {"path": TASK_YML})
    assert not (repo.path / CODEOWNERS).exists()
    assert list(outside.iterdir()) == []


def test_parent_that_is_a_file_fails_before_writing(tmp_path: Path) -> None:
    repo = sr.new_repo(tmp_path)
    sr.seed_identity(repo, "ana")
    (repo.path / ".github").mkdir()
    (repo.path / ".github" / "ISSUE_TEMPLATE").write_text("no soy una carpeta\n", encoding="utf-8")
    real = sr.run_op(repo)
    assert _errors(real) == ("E_STATE_DIRTY_WORKTREE", {"path": ".github/ISSUE_TEMPLATE", "reason": "not_a_directory"})
    assert _errors(sr.run_op(repo, dry_run=True)) == _errors(real)
    assert not (repo.path / CODEOWNERS).exists()


def test_read_only_target_fails_before_writing_where_it_blocks_the_write(tmp_path: Path) -> None:
    repo = sr.new_repo(tmp_path)
    sr.seed_identity(repo, "ana")
    target = repo.path / TASK_YML
    target.parent.mkdir(parents=True)
    target.write_text("viejo\n", encoding="utf-8")
    os.chmod(target, stat.S_IREAD)
    try:
        real = sr.run_op(repo)
        sr.assert_valid(real)
        if os.name == "nt":  # el atributo de solo lectura de NTFS impide os.replace
            assert _errors(real) == ("E_STATE_DIRTY_WORKTREE", {"path": TASK_YML, "reason": "read_only"})
            assert _errors(sr.run_op(repo, dry_run=True)) == _errors(real)
            assert not (repo.path / CODEOWNERS).exists()
        else:  # en POSIX el modo del archivo no impide reemplazarlo
            assert real.ok
    finally:
        os.chmod(target, stat.S_IREAD | stat.S_IWRITE)


def test_a_failed_write_reports_what_was_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = sr.new_repo(tmp_path)
    sr.seed_identity(repo, "ana")
    real_write = sr_op.write_file

    def flaky(root: Path, rel: str, payload: bytes) -> None:
        if rel == TASK_YML:
            raise OSError("disco lleno")
        real_write(root, rel, payload)

    monkeypatch.setattr(sr_op, "write_file", flaky)
    result = sr.run_op(repo)
    envelope = sr.assert_valid(result)
    assert not result.ok and result.error is not None
    assert result.error.code == "E_INTERNAL_UNEXPECTED" and "trace_id" in (result.error.details or {})
    data = envelope["data"]
    assert isinstance(data, dict)
    assert data["files"] == [{"path": CODEOWNERS, "action": "updated"}, {"path": TASK_YML, "action": "would_update"}]
    assert data["check_only"] is False and data["changed"] is True
    assert (repo.path / CODEOWNERS).exists()


# -- 4. mayúsculas: sync-roles, doctor y la regla dan el mismo veredicto ------------


def _rule_paths(repo: GitRepo) -> list[str]:
    inp = RuleInput(mode="tree", t=translator("es"), now=NOW, tree=worktree(repo.path))
    rule = {r.id: r for r in roles_sync.RULES}["roles-sync"]
    return sorted(f.path or "" for f in rule.run(inp))


def _verdicts(tmp_path: Path, repo: GitRepo) -> tuple[bool, bool, bool]:
    """``(doctor ok, sync-roles --check ok, regla sin hallazgos)``."""
    doctor = dr.checks_of(dr.run_op(repo.path, dr.env_for(tmp_path, repo.env)))["codeowners_sync"]["status"] == "ok"
    check = sr.run_op(repo, check=True)
    return doctor, check.ok, _rule_paths(repo) == []


@pytest.fixture
def lower_repo(tmp_path: Path) -> Iterator[GitRepo]:
    """``.github/codeowners`` en minúsculas, con el contenido correcto y commiteado."""
    repo = sr.new_repo(tmp_path)
    want = sr.expected_files(roles_fixture("basic"), "es")
    repo.commit("generados", files={".github/codeowners": want[CODEOWNERS], TASK_YML: want[TASK_YML]})
    yield repo


def test_lowercase_codeowners_is_out_of_sync_for_everyone(tmp_path: Path, lower_repo: GitRepo) -> None:
    assert _verdicts(tmp_path, lower_repo) == (False, False, False)
    check = sr.run_op(lower_repo, check=True)
    assert check.data is not None
    assert check.data["files"] == [{"path": CODEOWNERS, "action": "would_update"}, {"path": TASK_YML, "action": "unchanged"}]
    assert _rule_paths(lower_repo) == [CODEOWNERS]


def test_sync_roles_does_not_leave_the_lowercase_name_silently(tmp_path: Path, lower_repo: GitRepo) -> None:
    sr.seed_identity(lower_repo, "ana")
    result = sr.run_op(lower_repo)
    sr.assert_valid(result)
    if _ci_fs(lower_repo.path):  # el mismo archivo: hay que renombrarlo en git (git mv)
        assert _errors(result) == ("E_STATE_DIRTY_WORKTREE", {"path": ".github/codeowners", "reason": "case_mismatch"})
        assert _errors(sr.run_op(lower_repo, dry_run=True)) == _errors(result)
    else:  # dos archivos distintos: se escribe CODEOWNERS y la regla queda conforme
        assert result.ok
        assert _verdicts(tmp_path, lower_repo) == (True, True, True)


def test_renamed_on_disk_but_not_in_git_keeps_one_verdict(tmp_path: Path, lower_repo: GitRepo) -> None:
    os.rename(lower_repo.path / ".github" / "codeowners", lower_repo.path / ".github" / "tmp-name")
    os.rename(lower_repo.path / ".github" / "tmp-name", lower_repo.path / CODEOWNERS)
    verdicts = _verdicts(tmp_path, lower_repo)
    assert len(set(verdicts)) == 1, verdicts
    if _ci_fs(lower_repo.path):  # el índice sigue en minúsculas: la regla no ve CODEOWNERS
        assert verdicts == (False, False, False)
        sr.seed_identity(lower_repo, "ana")
        assert _errors(sr.run_op(lower_repo)) == (
            "E_STATE_DIRTY_WORKTREE",
            {"path": ".github/codeowners", "reason": "case_mismatch"},
        )


# -- 5. E_CONFIG_INVALID: details igual en todos los idiomas y con details.code -----------


def test_config_invalid_details_do_not_depend_on_the_language(tmp_path: Path) -> None:
    repo = sr.new_repo(tmp_path)
    repo.write({"roles.yaml": roles_fixture("basic").replace("    owner: carla\n", "    owner: zoe\n")})
    es = sr.run_op(repo, check=True, lang="es")
    en = sr.run_op(repo, check=True, lang="en")
    sr.assert_valid(es)
    assert es.error is not None and en.error is not None
    assert es.error.code == en.error.code == "E_CONFIG_INVALID"
    assert es.error.message != en.error.message
    assert es.error.details == en.error.details
    details = es.error.details
    assert details is not None
    findings = details["findings"]
    assert isinstance(findings, list) and findings
    assert all(set(f) == {"code", "line", "pointer"} for f in findings)
    assert details["code"] == findings[0]["code"]
    assert details["path"] == "roles.yaml" and details["reason"] == "roles_invalid"


# -- 6. E_PERM_ROLE_REQUIRED con details.required_role -------------------------------------


def test_role_required_uses_required_role(tmp_path: Path) -> None:
    repo = sr.new_repo(tmp_path)
    sr.seed_identity(repo, "carla")
    result = sr.run_op(repo, dry_run=True)
    sr.assert_valid(result)
    assert result.error is not None and result.error.code == "E_PERM_ROLE_REQUIRED"
    assert result.error.details == {"login": "carla", "role": "researcher", "required_role": ["lead", "deputy"]}
