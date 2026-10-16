# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Operación ``doctor`` · ``check_environment`` (05 §9.2; REQ-ARCH-33; D-34 l; 08-forja §3, §5 y §7.5)."""

from __future__ import annotations

import dataclasses
import io
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from aucove import _meta
from aucove.config.roles import parse_roles
from aucove.config.schemas import validate_result
from aucove.domain.checks import required_checks
from aucove.evidence.verify import missing_extra
from aucove.domain.taskbody import parse_task_body, render_task_body
from aucove.forge.base import NULL_SHA, BaseNotDefault, Capabilities, Identity, LabelEvent, NoTokenError, RefActivity, RepoRef
from aucove.forge.fake import FakeClock, FakeForge, Fault
from aucove.i18n import translator
from aucove.localstore.store import IdentityCache, Store
from aucove.ops import context as context_mod
from aucove.ops import doctor as op
from aucove.ops.context import Invocation, OpContext
from aucove.ops.protections import (
    BASE_RULESET,
    LABELS,
    ORG_RECOMMENDED,
    TASK_RULESET,
    base_ruleset,
    desired_settings,
    labels_for,
    task_ruleset,
)
from aucove.ops.registry import REGISTRY, invoke
from aucove.ops.result import Result
from aucove.render.generated import GENERATED_PATHS, generated_files
from aucove.render.mcp import MCP_JSON_PATH, mcp_json
from aucove.ops.doctor import EXPIRE_FILE
from aucove.ops.init_files import core_range
from tests.conftest import FIXTURES, REPO_ROOT, GitRepo, isolated_git_env, roles_fixture

SPEC = next(s for s in REGISTRY if s.id == "doctor")
CODEOWNERS, TASK_YML = GENERATED_PATHS
FAR = "2099-01-01T00:00:00Z"
MINIMAL = (FIXTURES / "config" / "project" / "minimal.yaml").read_text(encoding="utf-8")
GITHUB_URL = "https://github.com/example-org/demo.git"
REPO_REF = RepoRef("example-org", "demo")
NOW = "2026-10-09T12:00:00Z"
IDS = {"ana": 1, "bruno": 2, "carla": 3, "dario": 4}
SOURCE = _meta.ENV_PREFIX + "GITHUB_TOKEN"  # la fuente del token que informa el falso (08 §5.1)
FORGE_CHECKS = ("identity", "membership", "forge_reachable", "profile", "merge-settings")


def pinned_to(version: str) -> str:
    """El proyecto mínimo con el ``core`` y el ``core_pin`` que init escribe para ``version`` (REQ-ARCH-44)."""
    core, pin = 'core: ">=0.1.0,<0.2"', 'core_pin: "0.1.0"'
    assert MINIMAL.count(core) == MINIMAL.count(pin) == 1
    return MINIMAL.replace(core, f'core: "{core_range(version)}"').replace(pin, f'core_pin: "{version}"')


# Fija el núcleo instalado, para que ``convention`` dé ``ok`` con cualquier versión del paquete (01 §6.3).
PROJECT = pinned_to(_meta.__version__)
# Los checks que esta versión todavía deja en skip, y fuera de un proyecto, también los de la forja.
OUTSIDE_SKIPPED = {*op.SKIPPED, *FORGE_CHECKS, "mcp_config"}
# Check evidence_tools (REQ-ARCH-99): el CI instala el extra; sin él, warn y W_CONFIG_TOOL_MISSING.
EXTRA_STATUS = "ok" if not missing_extra() else "warn"
EXTRA_WARNINGS = [] if not missing_extra() else ["W_CONFIG_TOOL_MISSING"]
_GIT = shutil.which("git")
GIT_DIR = os.path.dirname(_GIT) if _GIT else ""


def noreply(login: str) -> str:
    return f"{IDS[login]}+{login}@users.noreply.github.com"


def clean_env(env: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if not k.upper().startswith((_meta.ENV_PREFIX, "CLAUDE_CODE_"))}


def fake_launcher(folder: Path, *, suffix: str | None = None) -> Path:
    """Carpeta con un lanzador falso del núcleo (``.exe`` en Windows)."""
    folder.mkdir(parents=True, exist_ok=True)
    name = _meta.NAME + (suffix if suffix is not None else (".exe" if os.name == "nt" else ""))
    target = folder / name
    target.write_bytes(b"")
    target.chmod(0o755)
    return folder


def expected_files(roles_text: str, lang: str = "es") -> dict[str, bytes]:
    roles, findings = parse_roles(roles_text.encode("utf-8"))
    assert roles is not None, findings
    return generated_files(
        lead=roles.lead,
        deputy=roles.deputy,
        sections=[(s.id, s.title, s.owner) for s in roles.sections],
        source_sha12=roles.source_sha12,
        lang=lang,  # type: ignore[arg-type]
    )


def new_repo(tmp: Path, *, roles: str = "basic", extra: Mapping[str, str | bytes] | None = None, github: bool = True,
             date: str | None = None) -> GitRepo:
    env = isolated_git_env(tmp / "git-env")
    if date is not None:  # la fecha del commit raíz, el que creó el proyecto
        env.update({"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    path, origin = tmp / "repo", tmp / "origin.git"
    path.mkdir(parents=True)
    repo = GitRepo(path, origin, env)
    repo._run(tmp, ["init", "-q", "--bare", "-b", "main", str(origin)], True)
    repo.git("init", "-q", "-b", "main")
    repo.git("remote", "add", "origin", str(origin))
    roles_text = roles_fixture(roles)
    files: dict[str, str | bytes] = {
        "project.yaml": PROJECT, "roles.yaml": roles_text, MCP_JSON_PATH: mcp_json(), **expected_files(roles_text),
    }
    files.update(extra or {})
    repo.commit("base", files=files)
    repo.push("main")
    repo.fetch()
    if github:  # origin apunta al repositorio de project.yaml; la ref remota ya existe
        repo.git("remote", "set-url", "origin", GITHUB_URL)
    return repo


@pytest.fixture
def repo(tmp_path: Path) -> GitRepo:
    return new_repo(tmp_path)


def env_for(tmp: Path, base: Mapping[str, str] | None = None, *, launcher: bool = True, git: bool = True,
            email: str | None = noreply("bruno")) -> dict[str, str]:
    """Entorno de la corrida; ``email`` es el correo de los commits (D-44), noreply por omisión."""
    parts = []
    if launcher:
        parts.append(str(fake_launcher(tmp / "bin")))
    if git:
        parts.append(GIT_DIR)
    env = clean_env(base if base is not None else os.environ)
    env["PATH"] = os.pathsep.join(parts) if parts else str(tmp / "empty")
    env["LOCALAPPDATA"] = str(tmp / "localappdata")  # sin las apps empaquetadas de quien corre las pruebas
    if email is not None:
        env["GIT_AUTHOR_EMAIL"] = email
    return env


def fake_forge(viewer: str = "bruno", *, profile: str = "enforced", rules: str = "ruleset", individual: bool = False,
               fmt: str = "latex", applied: bool = True, **options: object) -> FakeForge:
    """El repositorio de ``project.yaml`` en un FakeForge, visto por ``viewer``.

    ana administra y los demás de ``roles.yaml`` escriben, con los ids de ``IDS``. Con
    ``applied``, el repositorio está como lo deja ``init`` (06 §4): los ajustes de §4.2, las
    etiquetas de §4.5 y la organización con los ajustes de §4.10; y ``expire-claims`` acaba de
    correr bien en la base (09-ci REQ-CI-31). ``rules``: ``ruleset`` aplica
    ``aucove-base`` (§4.7) y ``aucove-task-branches`` (REQ-PERM-52) en ``enforced``,
    ``none`` deja la base sin reglas y ``default`` usa la base protegida por omisión del falso,
    sin checks requeridos.
    """
    forge = FakeForge(REPO_REF, viewer="ana", profile=profile, clock=FakeClock(NOW), **options)  # type: ignore[arg-type]
    forge.seed_user("ana", permission="admin", id=IDS["ana"])
    for login in ("bruno", "carla", "dario"):
        forge.seed_user(login, permission="write", id=IDS[login])
    admin = forge.admin("admin-token")
    base = forge.seed_commit({"README.md": b"x\n"}, [], "base", "ana")
    forge.seed_branch("main", base)
    if applied:
        visibility = str(options.get("visibility") or ("public" if profile == "enforced" else "private"))
        forge.seed_admin(settings=desired_settings(visibility), org=dict(ORG_RECOMMENDED))
        forge.ensure_labels(labels_for(translator("es")))
        # 09-ci §6: expire-claims corre a diario en la base (REQ-CI-31)
        forge.seed_workflow_run(EXPIRE_FILE, branch="main", sha=base, conclusion="success", event="schedule")
    if rules == "ruleset" and profile == "enforced":
        admin.upsert_ruleset(BASE_RULESET, base_ruleset("main", checks=required_checks(fmt), individual=individual,
                                                        bypass_id=None))
        admin.upsert_ruleset(TASK_RULESET, task_ruleset())
    elif rules == "none":
        forge.seed_admin(fresh=True)
    view = forge.as_user(viewer)
    forge.calls.clear()
    return view


_DEFAULT = object()


def run_op(
    root: Path,
    env: Mapping[str, str],
    *,
    lang: str | None = "es",
    refresh: object = False,
    cwd: Path | None = None,
    forge: object = _DEFAULT,
) -> Result:
    """``doctor`` con un FakeForge sano por omisión; ``forge=None`` deja la fábrica real."""
    inv = Invocation(root=str(root), lang=lang, json=True, dry_run=False, surface="mcp", env=dict(env), cwd=str(cwd or root))
    adapter = fake_forge() if forge is _DEFAULT else forge
    return invoke(SPEC, OpContext(inv, forge=adapter), refresh=refresh)  # type: ignore[arg-type]


def checks_of(result: Result) -> dict[str, dict[str, object]]:
    assert result.data is not None
    return {c["id"]: c for c in result.data["checks"]}


def statuses(result: Result) -> dict[str, str]:
    return {k: str(v["status"]) for k, v in checks_of(result).items()}


def codes(result: Result) -> list[str]:
    return [w.code for w in result.warnings]


def assert_valid(result: Result) -> dict[str, object]:
    envelope = result.to_dict()
    assert validate_result(envelope) == [], json.dumps(envelope, ensure_ascii=False, indent=1)
    return envelope


pytestmark = pytest.mark.skipif(_GIT is None, reason="git no está instalado")


# -- fuera de un proyecto ---------------------------------------------------------------


def test_outside_a_project(tmp_path: Path) -> None:
    outside = tmp_path / "fuera"
    outside.mkdir()
    result = run_op(outside, env_for(tmp_path))
    envelope = assert_valid(result)
    assert result.ok and result.exit_code() == 0
    data = envelope["data"]
    assert isinstance(data, dict)
    assert data["project_root"] is None
    assert data["core_version"] == _meta.__version__
    assert data["convention"] == {"project": None, "supported": list(_meta.SUPPORTED_CONVENTIONS)}
    assert data["identity"] == {"login": None, "source": None}
    assert data["role"] is None and data["is_deputy"] is False
    assert data["profile"] == "advisory"  # D-34 (l): sin detección
    assert data["half_states"] == []
    assert [c["id"] for c in data["checks"]] == list(op.CHECK_ORDER)  # type: ignore[union-attr]
    st = statuses(result)
    assert st["python"] == "ok" and st["git"] == "ok" and st["project_root"] == "warn"
    for check_id in ("project_yaml", "roles_yaml", "convention", "remote", "deputy", "codeowners_sync", "generated_files", "cache"):
        assert st[check_id] == "skip", check_id
    assert {k for k, v in st.items() if v == "skip"} >= OUTSIDE_SKIPPED
    assert st["evidence_tools"] == EXTRA_STATUS  # también fuera de un proyecto
    assert codes(result) == [*EXTRA_WARNINGS, "W_FORGE_CAPABILITY_UNKNOWN"]


def test_cli_outside_a_project_exits_0(tmp_path: Path) -> None:
    from aucove.cli.main import run as cli_run

    outside = tmp_path / "fuera"
    outside.mkdir()
    out, err = io.StringIO(), io.StringIO()
    code = cli_run(["--json", "--root", str(outside), "doctor"], env=env_for(tmp_path), stdout=out, stderr=err)
    assert code == 0, err.getvalue()
    envelope = json.loads(out.getvalue())
    assert validate_result(envelope) == [] and envelope["data"]["project_root"] is None


def test_outside_a_project_and_offline_never_call_the_forge(tmp_path: Path, repo: GitRepo,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("doctor no debe consultar la forja aquí")

    monkeypatch.setattr(OpContext, "forge", boom)
    monkeypatch.setattr(context_mod, "context_factory", boom)
    outside = tmp_path / "fuera"
    outside.mkdir()
    assert run_op(outside, env_for(tmp_path), forge=None).ok
    env = env_for(tmp_path, repo.env)
    env[_meta.ENV_PREFIX + "OFFLINE"] = "1"
    assert run_op(repo.path, env, forge=None).ok


# -- proyecto sano ------------------------------------------------------------------


def test_healthy_project(tmp_path: Path, repo: GitRepo) -> None:
    forge = fake_forge()
    result = run_op(repo.path, env_for(tmp_path, repo.env), forge=forge)
    envelope = assert_valid(result)
    assert result.ok and result.error is None, envelope
    data = envelope["data"]
    assert isinstance(data, dict)
    assert data["project_root"] == str(repo.path)
    assert data["convention"]["project"] == "0.1"  # type: ignore[index]
    assert data["identity"] == {"login": "bruno", "source": SOURCE}
    assert data["role"] == "researcher" and data["is_deputy"] is True
    assert data["profile"] == "enforced"
    expected = {check_id: "skip" for check_id in op.SKIPPED}
    expected.update(
        {
            "python": "ok", "git": "ok", "project_root": "ok", "project_yaml": "ok", "roles_yaml": "ok",
            "convention": "ok", "remote": "ok", "identity": "ok", "membership": "ok", "forge_reachable": "ok",
            "profile": "ok", "deputy": "ok", "codeowners_sync": "ok", "merge-settings": "ok", "generated_files": "ok",
            "mcp_config": "ok", "half_states": "ok", "cache": "ok", "evidence_tools": EXTRA_STATUS,
            "evidence_signing": "warn",  # sin evidence_signing: sin firma del proyecto (00-marco D-72)
        }
    )
    assert statuses(result) == expected
    for check in checks_of(result).values():
        if check["status"] in ("ok", "skip"):
            assert check["fix"] is None, check
        assert isinstance(check["message"], str) and check["message"]
    # el falso no tiene push rulesets: el aviso de REQ-PERM-53 acompaña a enforced
    assert codes(result) == ["W_FORGE_CHECKS_SPOOFABLE", *EXTRA_WARNINGS]


PEOPLE = 4  # personas activas de roles.yaml (basic)


BOARD_READS = 6  # el tablero de status sin PR abiertos ni tareas activas (08 §7.5: 6 + 2·P + E)


@pytest.mark.parametrize("viewer", ["bruno", "ana"])
def test_the_forge_reads_fit_the_budget(tmp_path: Path, repo: GitRepo, viewer: str) -> None:
    """08 §7.5: a lo sumo 19 + P + R lecturas (+1 por branches/{base}), más las del tablero de
    status si su caché venció, y ninguna escritura."""
    forge = fake_forge(viewer)
    run_op(repo.path, env_for(tmp_path, repo.env, email=noreply(viewer)), forge=forge)
    assert forge.calls.count("E") == 0
    rulesets = 2  # aucove-base y aucove-task-branches
    assert 0 < forge.calls.count("L") <= 19 + PEOPLE + rulesets + BOARD_READS
    methods = [c.method for c in forge.calls.entries()]
    assert methods[:3] == ["identity", "capabilities", "codeowners_errors"]
    assert methods.count("collaborator_permission") == PEOPLE
    assert ("outside_collaborators" in methods) is (viewer == "ana")  # REQ-PERM-55: solo el lead
    assert "last_successful_run" in methods  # REQ-CI-31


def test_the_board_cache_saves_its_reads(tmp_path: Path, repo: GitRepo) -> None:
    """Como context: con el tablero de menos de 10 min no se vuelve a leer; con --refresh, sí."""
    forge = fake_forge()
    env = env_for(tmp_path, repo.env)
    run_op(repo.path, env, forge=forge)
    first = forge.calls.count("L")
    forge.calls.clear()
    run_op(repo.path, env, forge=forge)
    cached = forge.calls.count("L")
    forge.calls.clear()
    run_op(repo.path, env, forge=forge, refresh=True)
    assert cached < first and forge.calls.count("L") == first


def test_skipped_checks_have_their_message(tmp_path: Path, repo: GitRepo) -> None:
    checks = checks_of(run_op(repo.path, env_for(tmp_path, repo.env), lang="en"))
    for check_id in op.SKIPPED:
        assert checks[check_id]["status"] == "skip" and checks[check_id]["fix"] is None
        assert checks[check_id]["message"] == translator("en")(f"doctor.{check_id}.skip")


def test_refresh_is_accepted_and_must_be_boolean(tmp_path: Path, repo: GitRepo) -> None:
    env = env_for(tmp_path, repo.env)
    assert run_op(repo.path, env, refresh=True).ok
    bad = run_op(repo.path, env, refresh="yes")
    assert bad.error is not None and bad.error.code == "E_VALIDATION_INPUT"


def test_lead_identity(tmp_path: Path, repo: GitRepo) -> None:
    data = run_op(repo.path, env_for(tmp_path, repo.env, email=noreply("ana")), forge=fake_forge("ana")).data
    assert data is not None
    assert data["identity"] == {"login": "ana", "source": SOURCE}
    assert data["role"] == "lead" and data["is_deputy"] is False


def test_identity_of_a_non_member(tmp_path: Path, repo: GitRepo) -> None:
    result = run_op(repo.path, env_for(tmp_path, repo.env), forge=fake_forge("dave"))
    assert result.data is not None and result.data["role"] is None and result.data["is_deputy"] is False
    membership = checks_of(result)["membership"]
    assert membership["status"] == "warn" and "@dave" in str(membership["message"])
    assert "init" in str(membership["fix"])


def test_without_the_forge_the_identity_comes_from_the_cache(tmp_path: Path, repo: GitRepo) -> None:
    Store(repo.path).write_identity(IdentityCache("ANA", 1, FAR), token_fp=None)
    env = env_for(tmp_path, repo.env)
    env[_meta.ENV_PREFIX + "OFFLINE"] = "1"
    result = run_op(repo.path, env, forge=None)
    assert_valid(result)
    assert result.data is not None
    assert result.data["identity"] == {"login": "ANA", "source": "cache"}
    assert result.data["role"] == "lead" and result.data["profile"] == "advisory"
    for check_id in FORGE_CHECKS:
        check = checks_of(result)[check_id]
        assert check["status"] == "skip" and check["message"] == translator("es")("doctor.forge.skip_offline")
    assert codes(result) == [*EXTRA_WARNINGS, "W_FORGE_CAPABILITY_UNKNOWN", "W_NET_OFFLINE"]


# -- identidad por forge_id (08-forja §6.3 y §6.4; REQ-FORGE-48) --------------------------

FORGE_ID_ROLES = roles_fixture("basic").replace("  - login: bruno\n", "  - login: bruno\n    forge_id: 111\n")


def forge_id_repo(tmp: Path) -> GitRepo:
    """Base con ``forge_id: 111`` para bruno (el suplente) y los generados al día."""
    assert FORGE_ID_ROLES != roles_fixture("basic")
    return new_repo(tmp, extra={"roles.yaml": FORGE_ID_ROLES, **expected_files(FORGE_ID_ROLES)})


def test_same_login_other_id_has_no_role(tmp_path: Path) -> None:
    """Otra cuenta (id 999) con el login de bruno no es bruno: sin rol ni suplencia."""
    repo = forge_id_repo(tmp_path)
    forge = fake_forge()
    forge.seed_user("bruno", id=999)
    result = run_op(repo.path, env_for(tmp_path, repo.env), forge=forge)
    envelope = assert_valid(result)
    data = envelope["data"]
    assert isinstance(data, dict)
    assert data["identity"] == {"login": "bruno", "source": SOURCE}
    assert data["role"] is None and data["is_deputy"] is False
    assert "W_AUTH_LOGIN_CHANGED" not in codes(result)
    assert checks_of(result)["membership"]["status"] == "warn"


def test_same_id_other_login_is_bruno_with_a_warning(tmp_path: Path) -> None:
    """bruno renombró su cuenta a bruno2 (mismo id 111): sigue siendo el suplente, con aviso."""
    repo = forge_id_repo(tmp_path)
    forge = fake_forge("bruno2")
    forge.seed_user("bruno2", id=111)
    result = run_op(repo.path, env_for(tmp_path, repo.env, email="111+bruno2@users.noreply.github.com"), forge=forge)
    envelope = assert_valid(result)
    assert result.ok, envelope
    data = envelope["data"]
    assert isinstance(data, dict)
    assert data["identity"] == {"login": "bruno2", "source": SOURCE}  # el login de la forja
    assert data["role"] == "researcher" and data["is_deputy"] is True
    assert codes(result) == ["W_AUTH_LOGIN_CHANGED", "W_FORGE_CHECKS_SPOOFABLE", *EXTRA_WARNINGS]
    changed = next(w for w in result.warnings if w.code == "W_AUTH_LOGIN_CHANGED")
    assert changed.details == {"login": "bruno2", "roles_login": "bruno"}


def test_same_id_and_login_has_no_warning(tmp_path: Path) -> None:
    repo = forge_id_repo(tmp_path)
    forge = fake_forge("BRUNO")
    forge.seed_user("bruno", id=111)
    result = run_op(repo.path, env_for(tmp_path, repo.env, email="111+bruno@users.noreply.github.com"), forge=forge)
    assert result.data is not None and result.data["role"] == "researcher" and result.data["is_deputy"] is True
    assert codes(result) == ["W_FORGE_CHECKS_SPOOFABLE", *EXTRA_WARNINGS]


class _Renamed(FakeForge):
    """La forja informa el login anterior de la cuenta (08 §6.4, mismo token y mismo id)."""

    def identity(self, *, refresh: bool = False) -> Identity:
        ident = super().identity(refresh=refresh)
        return dataclasses.replace(ident, login="carla2", previous_login="carla")


def test_a_renamed_login_without_forge_id_is_not_associated(tmp_path: Path, repo: GitRepo) -> None:
    """REQ-FORGE-48: sin ``forge_id`` no se asocia el login nuevo; se avisa y no hay rol."""
    forge = fake_forge("carla")
    renamed = _Renamed.__new__(_Renamed)
    renamed._bind(forge._s, "carla")
    result = run_op(repo.path, env_for(tmp_path, repo.env, email=noreply("carla")), forge=renamed)
    assert_valid(result)
    assert result.data is not None and result.data["role"] is None
    changed = next(w for w in result.warnings if w.code == "W_AUTH_LOGIN_CHANGED")
    assert changed.details == {"login": "carla2", "roles_login": "carla"}
    membership = checks_of(result)["membership"]
    assert membership["status"] == "warn" and "@carla" in str(membership["message"])
    assert "sync-roles" in str(membership["fix"])


# -- identity: token y correo (08 §5.2; 10-seguridad REQ-SEC-27 y REQ-SEC-29; D-44) ---------


def test_the_token_link_is_the_one_of_the_spec() -> None:
    """10-seguridad §5.2: el enlace prellenado, con los permisos de 08 REQ-FORGE-41."""
    spec = (REPO_ROOT / "spec" / "10-seguridad.md").read_text(encoding="utf-8")
    quoted = next(line.strip() for line in spec.splitlines() if line.startswith(op.TOKEN_URL))
    expected = quoted.replace("aucove", _meta.NAME).replace("<repo>", "demo").replace("<org>", "example-org")
    assert op.token_link("example-org/demo") == expected
    query = dict(parse_qsl(urlsplit(op.token_link("example-org/demo")).query))
    assert {k: query[k] for k in ("contents", "issues", "pull_requests", "actions", "statuses")} == {
        "contents": "write", "issues": "write", "pull_requests": "write", "actions": "read", "statuses": "read",
    }
    assert len(dict(parse_qsl(urlsplit(op.token_link("o/" + "x" * 80)).query))["name"]) == op.TOKEN_NAME_MAX


def test_a_fine_grained_token_limited_to_the_repo_is_ok(tmp_path: Path, repo: GitRepo) -> None:
    identity = checks_of(run_op(repo.path, env_for(tmp_path, repo.env)))["identity"]
    t = translator("es")
    assert identity == {
        "id": "identity", "status": "ok", "fix": None,
        "message": t("doctor.identity.ok", login="bruno", kind=t("doctor.identity.kind.fine_grained_pat"),
                     source=SOURCE, scope=t("doctor.identity.scope.repo_restricted")),
    }


@pytest.mark.parametrize("kind", ["classic_pat", "oauth"])
def test_a_broad_token_warns(tmp_path: Path, repo: GitRepo, kind: str) -> None:
    forge = fake_forge()
    forge.seed_user("bruno", token_kind=kind)
    result = run_op(repo.path, env_for(tmp_path, repo.env), forge=forge)
    assert result.ok  # REQ-FORGE-39: en v0.1 no bloquea
    identity = checks_of(result)["identity"]
    assert identity["status"] == "warn"
    assert op.token_link("example-org/demo") in str(identity["fix"])
    broad = next(w for w in result.warnings if w.code == "W_AUTH_BROAD_TOKEN")
    assert broad.details == {"reason": "token_scope", "token_kind": kind}
    assert "W_AUTH_WORKFLOW_SCOPE" not in codes(result)  # ni repo ni lo de gh auth login lo incluyen (08 §5.2)


def test_a_workflow_scope_warns(tmp_path: Path, repo: GitRepo) -> None:
    forge = fake_forge()
    forge.seed_user("bruno", token_kind="classic_pat", scopes=["repo", "workflow"])
    result = run_op(repo.path, env_for(tmp_path, repo.env), forge=forge)
    assert "W_AUTH_WORKFLOW_SCOPE" in codes(result)
    assert translator("es")("doctor.identity.warn_workflow") in str(checks_of(result)["identity"]["message"])


def test_a_fine_grained_token_for_every_repo_warns(tmp_path: Path, repo: GitRepo) -> None:
    forge = fake_forge()
    forge.seed_user("bruno", token_scope="owner_wide")
    result = run_op(repo.path, env_for(tmp_path, repo.env), forge=forge)
    identity = checks_of(result)["identity"]
    assert identity["status"] == "warn" and translator("es")("doctor.identity.warn_owner_wide") in str(identity["message"])
    assert "W_AUTH_BROAD_TOKEN" not in codes(result)


def test_the_commit_email_must_be_noreply(tmp_path: Path, repo: GitRepo) -> None:
    """D-44: con otro correo, warn y el ``git config`` exacto con la dirección noreply."""
    result = run_op(repo.path, env_for(tmp_path, repo.env, email="bruno@example.org"))
    identity = checks_of(result)["identity"]
    assert identity["status"] == "warn"
    assert "bruno@example.org" not in str(identity["message"])  # no se repite el correo personal
    assert f"git config user.email {noreply('bruno')}" in str(identity["fix"])
    assert result.ok


def test_the_roles_email_must_be_noreply(tmp_path: Path) -> None:
    roles = roles_fixture("basic").replace('    name: "Bruno Díaz"\n', '    name: "Bruno Díaz"\n    email: bruno@example.org\n')
    repo = new_repo(tmp_path, extra={"roles.yaml": roles, **expected_files(roles)})
    identity = checks_of(run_op(repo.path, env_for(tmp_path, repo.env)))["identity"]
    assert identity["status"] == "warn"
    assert translator("es")("doctor.identity.warn_roles_email") in str(identity["message"])
    assert noreply("bruno") in str(identity["fix"])


class _NoToken(FakeForge):
    def identity(self, *, refresh: bool = False) -> Identity:
        raise NoTokenError("no token", hint="Define GH_TOKEN.")


def test_without_a_token_identity_fails(tmp_path: Path, repo: GitRepo) -> None:
    forge = _NoToken.__new__(_NoToken)
    forge._bind(fake_forge()._s, "bruno")
    result = run_op(repo.path, env_for(tmp_path, repo.env), forge=forge)
    assert_valid(result)
    assert result.error is not None and result.error.details == {"failed": ["identity"]}
    identity = checks_of(result)["identity"]
    assert identity["status"] == "fail" and "GH_TOKEN" in str(identity["message"])
    assert op.token_link("example-org/demo") in str(identity["fix"])
    for check_id in ("membership", "forge_reachable", "profile"):
        assert checks_of(result)[check_id]["message"] == translator("es")("doctor.forge.skip_identity")
    assert result.data is not None and result.data["profile"] == "advisory"
    assert "W_FORGE_CAPABILITY_UNKNOWN" in codes(result)


def test_a_rejected_token_fails(tmp_path: Path, repo: GitRepo) -> None:
    forge = fake_forge()
    forge.faults.add(Fault("identity", "http_401"))
    result = run_op(repo.path, env_for(tmp_path, repo.env), forge=forge)
    identity = checks_of(result)["identity"]
    assert identity["status"] == "fail" and identity["message"] == translator("es")("doctor.identity.fail_auth")
    assert result.error is not None and result.error.details == {"failed": ["identity"]}


# -- forge_reachable (08 §3.1 y §5.4) -------------------------------------------------------


def test_the_repository_is_described(tmp_path: Path, repo: GitRepo) -> None:
    check = checks_of(run_op(repo.path, env_for(tmp_path, repo.env), lang="en"))["forge_reachable"]
    assert check["message"] == "example-org/demo: public repository, default branch `main`; your permission: write."


@pytest.mark.parametrize(("fault", "key"), [("http_404", "fail_not_found"), ("http_403", "fail_permission")])
def test_an_unreadable_repository_fails(tmp_path: Path, repo: GitRepo, fault: str, key: str) -> None:
    forge = fake_forge()
    forge.faults.add(Fault("capabilities", fault))  # type: ignore[arg-type]
    result = run_op(repo.path, env_for(tmp_path, repo.env), forge=forge)
    check = checks_of(result)["forge_reachable"]
    assert check["status"] == "fail"
    assert check["message"] == translator("es")(f"doctor.forge_reachable.{key}", repo="example-org/demo")
    if fault == "http_403":  # 08 §5.4: la aprobación de tokens de la organización
        assert "Pending requests" in str(check["fix"])
    assert result.error is not None and result.error.details == {"failed": ["forge_reachable"]}
    assert checks_of(result)["profile"]["message"] == translator("es")("doctor.forge.skip_repo")


class _OtherDefault(FakeForge):
    def capabilities(self, *, refresh: bool = False) -> Capabilities:
        raise BaseNotDefault("base", default_branch="trunk")


def test_a_base_that_is_not_the_default_branch_fails(tmp_path: Path, repo: GitRepo) -> None:
    forge = _OtherDefault.__new__(_OtherDefault)
    forge._bind(fake_forge()._s, "bruno")
    check = checks_of(run_op(repo.path, env_for(tmp_path, repo.env), forge=forge))["forge_reachable"]
    assert check["status"] == "fail"
    assert check["message"] == translator("es")("doctor.forge_reachable.fail_base", base="main", default="trunk")


def test_without_network_the_forge_checks_are_skipped(tmp_path: Path, repo: GitRepo) -> None:
    forge = fake_forge()
    forge.faults.add(Fault("identity", "unreachable"))
    result = run_op(repo.path, env_for(tmp_path, repo.env), forge=forge)
    assert_valid(result)
    assert result.ok
    for check_id in FORGE_CHECKS:
        assert checks_of(result)[check_id]["message"] == translator("es")("doctor.forge.skip_network")
    assert codes(result)[-2:] == ["W_FORGE_CAPABILITY_UNKNOWN", "W_NET_OFFLINE"]


def test_a_rate_limited_forge_skips_the_checks(tmp_path: Path, repo: GitRepo) -> None:
    forge = fake_forge()
    forge.faults.add(Fault("identity", "rate_limit_primary"))
    result = run_op(repo.path, env_for(tmp_path, repo.env), forge=forge)
    assert result.ok
    expected = translator("es")("doctor.forge.skip_rate_limited", name=_meta.NAME)
    assert all(checks_of(result)[c]["message"] == expected for c in FORGE_CHECKS)


def test_codeowners_errors_from_the_forge_fail(tmp_path: Path, repo: GitRepo) -> None:
    """06 REQ-PERM-31: la forja informa errores en CODEOWNERS de la base."""
    forge = fake_forge()
    forge.set_codeowners_errors("main", ["Unknown owner on line 3: make sure @carla exists and has write access"])
    result = run_op(repo.path, env_for(tmp_path, repo.env), forge=forge)
    check = checks_of(result)["codeowners_sync"]
    assert check["status"] == "fail" and "Unknown owner on line 3" in str(check["message"])
    assert "sync-roles" in str(check["fix"])
    assert result.error is not None and result.error.details == {"failed": ["codeowners_sync"]}


# -- membership (06 REQ-PERM-28) -----------------------------------------------------------


def test_a_member_without_write_access_fails(tmp_path: Path, repo: GitRepo) -> None:
    forge = fake_forge()
    forge.seed_user("bruno", permission="read")
    result = run_op(repo.path, env_for(tmp_path, repo.env), forge=forge)
    check = checks_of(result)["membership"]
    assert check["status"] == "fail" and "read" in str(check["message"])
    assert "init" in str(check["fix"])
    assert result.error is not None and result.error.details == {"failed": ["membership"]}


def test_an_inactive_person_warns(tmp_path: Path) -> None:
    roles = roles_fixture("basic").replace('    name: "Darío Luna"\n', '    name: "Darío Luna"\n    active: false\n')
    repo = new_repo(tmp_path, extra={"roles.yaml": roles, **expected_files(roles)})
    result = run_op(repo.path, env_for(tmp_path, repo.env, email=noreply("dario")), forge=fake_forge("dario"))
    assert checks_of(result)["membership"]["status"] == "warn"
    assert result.data is not None and result.data["role"] is None


# -- profile (08 §3.3; 06 §4.7 y §5; REQ-PERM-53 y REQ-PERM-60) --------------------------------


def test_enforced_with_the_base_ruleset_is_ok(tmp_path: Path, repo: GitRepo) -> None:
    profile = checks_of(run_op(repo.path, env_for(tmp_path, repo.env)))["profile"]
    expected = translator("es")("doctor.profile.ok_enforced", ruleset=BASE_RULESET, checks=", ".join(required_checks("latex")))
    assert profile == {"id": "profile", "status": "ok", "message": expected, "fix": None}


def test_enforced_without_rules_warns(tmp_path: Path, repo: GitRepo) -> None:
    result = run_op(repo.path, env_for(tmp_path, repo.env), forge=fake_forge(rules="none"))
    profile = checks_of(result)["profile"]
    assert profile["status"] == "warn" and "init" in str(profile["fix"])
    assert translator("es")("doctor.profile.warn_no_rules", base="main") in str(profile["message"])
    assert "W_FORGE_RULES_NOT_APPLIED" in codes(result)


def test_missing_required_checks_are_listed(tmp_path: Path, repo: GitRepo) -> None:
    result = run_op(repo.path, env_for(tmp_path, repo.env), forge=fake_forge(rules="default"))
    profile = checks_of(result)["profile"]
    assert profile["status"] == "warn"
    missing = [f"check:{c}" for c in required_checks("latex")]
    assert translator("es")("doctor.profile.warn_missing", missing=", ".join(missing)) in str(profile["message"])
    rules = next(w for w in result.warnings if w.code == "W_FORGE_RULES_NOT_APPLIED")
    assert rules.details == {"missing": missing}


def test_group_mode_with_the_individual_ruleset_warns(tmp_path: Path, repo: GitRepo) -> None:
    """REQ-PERM-60: pasó a ``group`` y la base sigue sin aprobaciones."""
    result = run_op(repo.path, env_for(tmp_path, repo.env), forge=fake_forge(individual=True))
    profile = checks_of(result)["profile"]
    assert profile["status"] == "warn"
    rules = next(w for w in result.warnings if w.code == "W_FORGE_RULES_NOT_APPLIED")
    assert rules.details == {"missing": ["required_approving_review_count", "require_code_owner_review",
                                         "require_last_push_approval"]}


INDIVIDUAL_ROLES = (
    "version: 1\nlead: ana\npeople:\n  - login: ana\n    name: \"Ana Pérez\"\n    role: lead\n    account_type: team\n"
    "sections:\n  - id: introduction\n    title: \"Introduction\"\n    owner: ana\n"
)


def test_individual_mode_needs_no_deputy_nor_approvals(tmp_path: Path) -> None:
    """00-marco D-57: deputy ok sin alerta, y las reglas de REQ-PERM-59 están completas."""
    project = PROJECT + "mode: individual\n"
    repo = new_repo(tmp_path, extra={"project.yaml": project, "roles.yaml": INDIVIDUAL_ROLES,
                                     **expected_files(INDIVIDUAL_ROLES)})
    result = run_op(repo.path, env_for(tmp_path, repo.env, email=noreply("ana")), forge=fake_forge("ana", individual=True))
    assert_valid(result)
    st = statuses(result)
    assert st["deputy"] == "ok" and st["profile"] == "ok"
    assert checks_of(result)["deputy"]["message"] == translator("es")("doctor.deputy.ok_individual")
    assert "W_PERM_DEPUTY_MISSING" not in codes(result) and "W_FORGE_RULES_NOT_APPLIED" not in codes(result)


ACCEPTANCE = "  advisory_acceptance:\n    by: ana\n    at: \"2026-10-01T10:00:00Z\"\n"


def test_advisory_with_the_acceptance_shows_the_fixed_line(tmp_path: Path) -> None:
    """06 §5: la línea fija del perfil advisory y W_FORGE_PROFILE_ADVISORY (12-mvp A5)."""
    repo = new_repo(tmp_path, extra={"project.yaml": PROJECT + ACCEPTANCE})
    result = run_op(repo.path, env_for(tmp_path, repo.env), forge=fake_forge(profile="advisory"))
    assert_valid(result)
    profile = checks_of(result)["profile"]
    assert profile == {"id": "profile", "status": "ok", "message": translator("es")("doctor.profile.advisory_line"), "fix": None}
    assert result.data is not None and result.data["profile"] == "advisory"
    assert "W_FORGE_PROFILE_ADVISORY" in codes(result) and "W_FORGE_CAPABILITY_UNKNOWN" not in codes(result)
    assert "W_FORGE_CHECKS_SPOOFABLE" not in codes(result)


def test_advisory_without_the_acceptance_warns(tmp_path: Path, repo: GitRepo) -> None:
    profile = checks_of(run_op(repo.path, env_for(tmp_path, repo.env), forge=fake_forge(profile="advisory")))["profile"]
    t = translator("es")
    assert profile["status"] == "warn" and str(profile["message"]).startswith(t("doctor.profile.warn_not_accepted"))
    assert t("doctor.profile.advisory_line") in str(profile["message"])


def test_an_upgrade_to_enforced_is_detected(tmp_path: Path) -> None:
    """06 §5, compensación 5: la forja ya ofrece enforced y project.yaml aceptó advisory."""
    repo = new_repo(tmp_path, extra={"project.yaml": PROJECT + ACCEPTANCE})
    profile = checks_of(run_op(repo.path, env_for(tmp_path, repo.env)))["profile"]
    assert profile["status"] == "warn" and translator("es")("doctor.profile.warn_upgrade") in str(profile["message"])
    assert "init" in str(profile["fix"])


def test_an_inconclusive_detection_is_advisory(tmp_path: Path, repo: GitRepo) -> None:
    """REQ-FORGE-14: sin ver el plan, un repo privado con reglas se informa advisory."""
    forge = fake_forge(visibility="private")
    forge.seed_admin(plan_visible=False)
    result = run_op(repo.path, env_for(tmp_path, repo.env), forge=forge)
    assert_valid(result)
    profile = checks_of(result)["profile"]
    assert profile["status"] == "warn" and "doctor" in str(profile["fix"])
    unknown = next(w for w in result.warnings if w.code == "W_FORGE_CAPABILITY_UNKNOWN")
    assert unknown.details == {"reason": "inconclusive"}
    assert result.data is not None and result.data["profile"] == "advisory"


# -- la deriva de lo que aplica init (06 §4.11, REQ-PERM-39; 00-marco D-65) ---------------------


def admin_of(forge: FakeForge) -> object:
    """El ``ForgeAdmin`` de ana sobre el mismo estado (para cambiar la configuración en una prueba)."""
    forge.seed_admin_token("admin-token", "ana")
    return forge.admin("admin-token")


def administering_lead(**options: object) -> FakeForge:
    """ana con el token de ``gh auth login`` (la excepción de D-25): ve lo reservado a la
    administración, que el token fine-grained de uso diario no ve (08 REQ-FORGE-41)."""
    forge = fake_forge("ana", **options)
    forge.seed_user("ana", permission="admin", token_kind="oauth")
    return forge


def test_github_default_settings_fail_merge_settings(tmp_path: Path, repo: GitRepo) -> None:
    """REQ-TRACE-36: con los ajustes de un repo recién creado, merge-settings falla."""
    result = run_op(repo.path, env_for(tmp_path, repo.env), forge=fake_forge(applied=False))
    check = checks_of(result)["merge-settings"]
    assert check["status"] == "fail" and "init" in str(check["fix"])
    message = str(check["message"])
    assert "allow_merge_commit=true" in message and "squash_merge_commit_message=COMMIT_MESSAGES" in message
    assert "delete_branch_on_merge=false" in message and "has_wiki=true" in message  # el resto de 06 §4.2
    assert result.error is not None and "merge-settings" in result.error.details["failed"]  # type: ignore[index]


def test_other_settings_only_warn(tmp_path: Path, repo: GitRepo) -> None:
    forge = fake_forge()
    forge.seed_admin(settings={"delete_branch_on_merge": False})
    check = checks_of(run_op(repo.path, env_for(tmp_path, repo.env), forge=forge))["merge-settings"]
    t = translator("es")
    assert check == {"id": "merge-settings", "status": "warn", "fix": t("doctor.profile.fix_init", name=_meta.NAME),
                     "message": t("doctor.merge-settings.warn", settings="delete_branch_on_merge=false")}


def test_merge_settings_need_write_access(tmp_path: Path, repo: GitRepo) -> None:
    """GitHub solo muestra los ajustes de integración con escritura (Contents W): skip."""
    forge = fake_forge()
    forge.seed_user("bruno", permission="read")
    check = checks_of(run_op(repo.path, env_for(tmp_path, repo.env), forge=forge))["merge-settings"]
    assert check["status"] == "skip" and check["message"] == translator("es")("doctor.merge-settings.skip_unreadable")


def test_a_disabled_task_ruleset_warns(tmp_path: Path, repo: GitRepo) -> None:
    """REQ-PERM-52: aucove-task-branches activo, con su regla."""
    forge = fake_forge()
    admin_of(forge).upsert_ruleset(TASK_RULESET, {**task_ruleset(), "enforcement": "disabled"})  # type: ignore[attr-defined]
    profile = checks_of(run_op(repo.path, env_for(tmp_path, repo.env), forge=forge))["profile"]
    assert profile["status"] == "warn"
    assert translator("es")("doctor.profile.warn_task_ruleset", ruleset=TASK_RULESET) in str(profile["message"])


def test_a_base_ruleset_with_other_rules_warns(tmp_path: Path, repo: GitRepo) -> None:
    """REQ-PERM-32 y REQ-PERM-33: exactamente las reglas de 06 §4.7 (aquí sobra una ``update``)."""
    forge = fake_forge()
    payload = base_ruleset("main", checks=required_checks("latex"), individual=False, bypass_id=None)
    payload["rules"].append({"type": "update"})
    admin_of(forge).upsert_ruleset(BASE_RULESET, payload)  # type: ignore[attr-defined]
    result = run_op(repo.path, env_for(tmp_path, repo.env), forge=forge)
    profile = checks_of(result)["profile"]
    assert profile["status"] == "warn"
    assert translator("es")("doctor.profile.warn_ruleset_differs", ruleset=BASE_RULESET) in str(profile["message"])
    assert "W_FORGE_RULES_NOT_APPLIED" not in codes(result)  # las reglas que exige 06 §4.7 sí están


def test_a_disabled_base_ruleset_is_not_applied(tmp_path: Path, repo: GitRepo) -> None:
    forge = fake_forge()
    payload = base_ruleset("main", checks=required_checks("latex"), individual=False, bypass_id=None)
    admin_of(forge).upsert_ruleset(BASE_RULESET, {**payload, "enforcement": "disabled"})  # type: ignore[attr-defined]
    result = run_op(repo.path, env_for(tmp_path, repo.env), forge=forge)
    profile = checks_of(result)["profile"]
    expected = translator("es")("doctor.profile.warn_ruleset_inactive", ruleset=BASE_RULESET, enforcement="disabled")
    assert profile["status"] == "warn" and expected in str(profile["message"])
    assert "W_FORGE_RULES_NOT_APPLIED" in codes(result)


def test_with_a_deputy_the_base_ruleset_has_no_bypass(tmp_path: Path, repo: GitRepo) -> None:
    """REQ-PERM-21: el lead lo ve y avisa; quien no puede editar el ruleset no ve los actores."""
    forge = administering_lead()
    payload = base_ruleset("main", checks=required_checks("latex"), individual=False, bypass_id=IDS["ana"])
    admin_of(forge).upsert_ruleset(BASE_RULESET, payload)  # type: ignore[attr-defined]
    lead = checks_of(run_op(repo.path, env_for(tmp_path, repo.env, email=noreply("ana")), forge=forge))["profile"]
    assert lead["status"] == "warn"
    assert translator("es")("doctor.profile.warn_bypass", ruleset=BASE_RULESET) in str(lead["message"])
    researcher = checks_of(run_op(repo.path, env_for(tmp_path, repo.env), forge=forge.as_user("bruno")))["profile"]
    assert researcher["status"] == "ok"


def test_without_deputy_the_lead_may_bypass_pull_requests(tmp_path: Path) -> None:
    """REQ-PERM-19 y REQ-PERM-20: sin suplente, el lead como único actor, con ``pull_request``."""
    repo = new_repo(tmp_path, roles="no-deputy")
    forge = administering_lead()
    payload = base_ruleset("main", checks=required_checks("latex"), individual=False, bypass_id=IDS["ana"])
    admin_of(forge).upsert_ruleset(BASE_RULESET, payload)  # type: ignore[attr-defined]
    result = run_op(repo.path, env_for(tmp_path, repo.env, email=noreply("ana")), forge=forge)
    assert checks_of(result)["profile"]["status"] == "ok"
    assert translator("es")("doctor.profile.warn_bypass", ruleset=BASE_RULESET) not in str(checks_of(result)["profile"]["message"])


def test_people_without_write_access_are_listed(tmp_path: Path, repo: GitRepo) -> None:
    """REQ-PERM-28: doctor informa a quien no tenga escritura directa."""
    forge = fake_forge()
    forge.seed_user("carla", permission="read")
    membership = checks_of(run_op(repo.path, env_for(tmp_path, repo.env), forge=forge))["membership"]
    assert membership["status"] == "warn" and "@carla" in str(membership["message"])
    assert "init" in str(membership["fix"])


def test_the_lead_sees_outside_collaborators(tmp_path: Path, repo: GitRepo) -> None:
    """REQ-PERM-55: con el lead, W_AUTH_BROAD_TOKEN con ``outside_collaborator``; los demás no las leen."""
    forge = fake_forge("ana")
    forge.seed_admin(outside=["carla"])
    result = run_op(repo.path, env_for(tmp_path, repo.env, email=noreply("ana")), forge=forge)
    broad = next(w for w in result.warnings if w.code == "W_AUTH_BROAD_TOKEN")
    assert broad.details == {"reason": "outside_collaborator", "logins": ["carla"]}
    membership = checks_of(result)["membership"]
    assert membership["status"] == "warn" and "@carla" in str(membership["message"])
    assert "owner" in str(membership["fix"])
    other = run_op(repo.path, env_for(tmp_path, repo.env), forge=forge.as_user("bruno"))
    assert "W_AUTH_BROAD_TOKEN" not in codes(other)


def test_missing_labels_warn(tmp_path: Path, repo: GitRepo) -> None:
    check = checks_of(run_op(repo.path, env_for(tmp_path, repo.env), forge=fake_forge(applied=False)))["forge_reachable"]
    labels = ", ".join(name for name, _color in LABELS)
    assert check["status"] == "warn"
    assert translator("es")("doctor.forge_reachable.warn_labels", labels=labels) in str(check["message"])


def test_the_lead_sees_the_organization_and_workflow_settings(tmp_path: Path, repo: GitRepo) -> None:
    """06 §4.10 (REQ-PERM-37) y REQ-PERM-27: lo que el token del lead puede leer."""
    forge = administering_lead()
    forge.seed_admin(org={**ORG_RECOMMENDED, "members_can_delete_repositories": True},
                     workflow_repo={"default_workflow_permissions": "write", "can_approve_pull_request_reviews": False})
    check = checks_of(run_op(repo.path, env_for(tmp_path, repo.env, email=noreply("ana")), forge=forge))["forge_reachable"]
    t = translator("es")
    assert check["status"] == "warn"
    assert "members_can_delete_repositories=true" in str(check["message"])
    assert t("doctor.forge_reachable.warn_workflow_repo") in str(check["message"])
    assert t("doctor.forge_reachable.fix_owner") in str(check["fix"]) and "init" in str(check["fix"])
    other = checks_of(run_op(repo.path, env_for(tmp_path, repo.env), forge=forge.as_user("bruno")))["forge_reachable"]
    assert other["status"] == "ok"  # sin administración no se leen: desconocido, sin aviso


CHECKOUT = "actions/checkout@" + "a" * 40
SETUP_UV = "astral-sh/setup-uv@" + "b" * 40
WORKFLOW = (
    "on: push\npermissions: {}\njobs:\n  x:\n    runs-on: ubuntu-latest\n    steps:\n"
    f"      - uses: {CHECKOUT}\n      - uses: {SETUP_UV}\n"
)


def test_a_restricted_actions_policy_warns(tmp_path: Path) -> None:
    """REQ-OPS-64: la política de la org no permite setup-uv; sin administración, desconocida y sin aviso."""
    repo = new_repo(tmp_path, extra={".github/workflows/x.yml": WORKFLOW})
    forge = administering_lead()
    forge.seed_admin(actions_org={"enabled_repositories": "all", "allowed_actions": "selected", "sha_pinning_required": True,
                                  "selected": {"github_owned_allowed": True, "verified_allowed": False,
                                               "patterns_allowed": []}})
    result = run_op(repo.path, env_for(tmp_path, repo.env, email=noreply("ana")), forge=forge)
    restricted = next(w for w in result.warnings if w.code == "W_FORGE_ACTIONS_RESTRICTED")
    assert restricted.details == {"level": "org", "allowed_actions": "selected", "missing": [SETUP_UV]}
    check = checks_of(result)["forge_reachable"]
    assert check["status"] == "warn" and SETUP_UV in str(check["message"])
    other = run_op(repo.path, env_for(tmp_path, repo.env), forge=forge.as_user("bruno"))
    assert "W_FORGE_ACTIONS_RESTRICTED" not in codes(other)


def test_a_network_failure_during_the_drift_stops_reading(tmp_path: Path, repo: GitRepo) -> None:
    forge = fake_forge()
    forge.faults.add(Fault("repo_settings", "unreachable"))
    result = run_op(repo.path, env_for(tmp_path, repo.env), forge=forge)
    assert "W_NET_OFFLINE" in codes(result)
    methods = [c.method for c in forge.calls.entries()]
    assert "repo_settings" in methods and "list_labels" not in methods
    assert checks_of(result)["merge-settings"]["status"] == "skip"
    assert checks_of(result)["identity"]["status"] == "ok"  # lo leído antes sigue valiendo


# -- half_states (05 §6.3, REQ-OPS-27; 09-ci REQ-CI-31) --------------------------------------------


def seed_task(forge: FakeForge, number: int, *, claimant: str, assignees: Sequence[str] | None = None,
              marker: bool = True, actor: str | None = None) -> None:
    """Una tarea reclamada por ``claimant`` (Issue, commit de reclamo, ref, actividad y marcador).

    ``assignees`` reemplaza los asignados (por omisión, el reclamante); sin ``marker`` no hay
    comentario ``claim``; ``actor`` es quien creó la ref según la actividad (por omisión, el
    reclamante).
    """
    body = render_task_body({"summary": "Resumen.", "sections": ["methods"], "plan_refs": [], "done": ["Hecho."]}, "es")
    spec_hash = str(parse_task_body(body)["spec_hash"])
    when = forge.clock.now() - timedelta(hours=10)
    events = [LabelEvent(1, "labeled", "task", "carla", when), LabelEvent(2, "labeled", "state:proposed", "carla", when),
              LabelEvent(3, "unlabeled", "state:proposed", "ana", when + timedelta(minutes=10))]
    forge.seed_issue(number=number, title="Tarea", body=body, labels=["task"], author="carla",
                     assignees=[claimant] if assignees is None else list(assignees), created_at=when, events=events,
                     updated_at=when + timedelta(hours=1))
    base = forge.get_ref("main")
    assert base is not None
    claim = forge.seed_commit({"README.md": b"x\n"}, [base.sha], f"{_meta.CLAIM_SUBJECT}{number}", claimant,
                              date=when + timedelta(hours=2))
    forge.seed_branch(f"task/{number}", claim)
    forge.seed_ref_activity(f"task/{number}", [
        RefActivity("branch_creation", actor or claimant, when + timedelta(hours=2), NULL_SHA, claim)])
    if marker:
        data = {"task": number, "login": claimant, "claim_sha": claim, "base_sha": base.sha, "sections": ["methods"],
                "spec_hash": spec_hash, "claimed_at": "2026-10-09T04:00:00Z", "renewed_at": None}
        text = f"<!-- {_meta.NAME}:claim v1\n{json.dumps(data)}\n-->"
        forge.seed_comment(number, author=claimant, body=text, created_at=when + timedelta(hours=2, minutes=1))


def test_the_own_half_finished_claim_suggests_claim(tmp_path: Path, repo: GitRepo) -> None:
    """Un reclamo sin asignación ni marcador: repetir claim completa los reflejos (REQ-OPS-27)."""
    forge = fake_forge()
    seed_task(forge, 12, claimant="bruno", assignees=[], marker=False)
    seed_task(forge, 13, claimant="carla", assignees=[])  # de otra persona: no es de bruno
    result = run_op(repo.path, env_for(tmp_path, repo.env), forge=forge)
    assert_valid(result)
    assert result.data is not None
    assert result.data["half_states"] == [
        {"task": 12, "kind": "assignees_mismatch", "suggested_op": "claim"},
        {"task": 12, "kind": "claim_without_marker", "suggested_op": "claim"},
    ] or result.data["half_states"] == [
        {"task": 12, "kind": "claim_without_marker", "suggested_op": "claim"},
        {"task": 12, "kind": "assignees_mismatch", "suggested_op": "claim"},
    ]
    check = checks_of(result)["half_states"]
    assert check["status"] == "warn" and "#12" in str(check["message"]) and "#13" not in str(check["message"])
    assert f"{_meta.NAME} claim <n>" in str(check["fix"])
    assert result.ok  # warn no es fail


def test_the_lead_sees_what_only_the_lead_can_close(tmp_path: Path, repo: GitRepo) -> None:
    """Fuentes del dueño en desacuerdo: solo el lead puede liberar (04-tareas §1.5 y §1.6)."""
    forge = fake_forge("ana")
    seed_task(forge, 21, claimant="carla", actor="bruno")  # la actividad dice bruno; el commit y el marcador, carla
    lead = run_op(repo.path, env_for(tmp_path, repo.env, email=noreply("ana")), forge=forge)
    assert lead.data is not None
    assert {"task": 21, "kind": "owner_sources_disagree", "suggested_op": "release"} in lead.data["half_states"]
    other = run_op(repo.path, env_for(tmp_path, repo.env), forge=forge.as_user("bruno"))
    assert other.data is not None and other.data["half_states"] == []


def test_without_a_recent_expire_claims_run_it_warns(tmp_path: Path, repo: GitRepo) -> None:
    """09-ci REQ-CI-31: sin run exitoso, o con el último de hace más de 2 días."""
    t = translator("es")
    never = checks_of(run_op(repo.path, env_for(tmp_path, repo.env), forge=fake_forge(applied=False)))["half_states"]
    assert never["status"] == "warn" and never["message"] == t("doctor.half_states.warn_expire_never", base="main")
    assert never["fix"] == t("doctor.half_states.fix_expire")
    forge = fake_forge()
    forge.clock.advance(timedelta(days=3, hours=1))
    stale = checks_of(run_op(repo.path, env_for(tmp_path, repo.env), forge=forge))["half_states"]
    assert stale["status"] == "warn" and stale["message"] == t("doctor.half_states.warn_expire_stale", base="main", days=3)


def test_a_new_project_without_an_expire_claims_run_is_ok(tmp_path: Path) -> None:
    """09-ci REQ-CI-31: en los primeros 2 días del proyecto, expire-claims todavía no tuvo ocasión de correr."""
    t = translator("es")
    young = new_repo(tmp_path / "nuevo", date="2026-10-08T12:00:00+00:00")  # un día antes del reloj del falso
    check = checks_of(run_op(young.path, env_for(tmp_path, young.env), forge=fake_forge(applied=False)))["half_states"]
    assert check["status"] == "ok", check
    assert str(check["message"]).endswith(t("doctor.half_states.ok_expire_new", base="main"))


def test_a_recent_expire_claims_run_and_no_half_states_is_ok(tmp_path: Path, repo: GitRepo) -> None:
    check = checks_of(run_op(repo.path, env_for(tmp_path, repo.env)))["half_states"]
    t = translator("es")
    assert check == {"id": "half_states", "status": "ok", "fix": None,
                     "message": f"{t('doctor.half_states.ok')} {t('doctor.half_states.ok_expire', base='main')}"}


# -- mcp_config (10-seguridad SB04; 01 §10.3) -----------------------------------------------


@pytest.mark.parametrize(("content", "status", "key"), [
    (None, "warn", "warn_missing"),
    (b"{no es json", "fail", "fail_invalid"),
    (json.dumps({"mcpServers": {_meta.MCP_SERVER_NAME: {"command": _meta.NAME, "args": ["mcp", "--stdio"]},
                                "otro": {"command": "otro"}}}).encode(), "fail", "fail_servers"),
    (json.dumps({"mcpServers": {_meta.MCP_SERVER_NAME: {"command": "sh", "args": ["-c", "x"]}}}).encode(), "fail", "fail_command"),
    (json.dumps({"mcpServers": {_meta.MCP_SERVER_NAME: {"command": _meta.NAME, "args": ["mcp", "--stdio"],
                                                        "env": {"PATH": "."}}}}).encode(), "fail", "fail_command"),
    (json.dumps({"mcpServers": {_meta.MCP_SERVER_NAME: {"type": "stdio", "command": _meta.NAME,
                                                        "args": ["mcp", "--stdio"]}}}).encode(), "ok", "ok"),
])
def test_mcp_config(tmp_path: Path, repo: GitRepo, content: bytes | None, status: str, key: str) -> None:
    target = repo.path / MCP_JSON_PATH
    if content is None:
        target.unlink()
    else:
        target.write_bytes(content)
    result = run_op(repo.path, env_for(tmp_path, repo.env))
    check = checks_of(result)["mcp_config"]
    assert check["status"] == status
    assert check["message"] == translator("es")(f"doctor.mcp_config.{key}", name=_meta.NAME)
    assert (check["fix"] is None) is (status == "ok")
    assert (result.error is not None) is (status == "fail")


# -- avisos y fallas ----------------------------------------------------------------


def test_without_deputy_warns(tmp_path: Path) -> None:
    repo = new_repo(tmp_path, roles="no-deputy")
    result = run_op(repo.path, env_for(tmp_path, repo.env))
    assert_valid(result)
    assert result.ok  # un warn no es fail
    assert "W_PERM_DEPUTY_MISSING" in codes(result)
    deputy = checks_of(result)["deputy"]
    assert deputy["status"] == "warn" and deputy["fix"] is not None and "sync-roles" in str(deputy["fix"])


def test_a_failing_check_gives_doctor_failed_with_full_data(tmp_path: Path, repo: GitRepo) -> None:
    target = repo.path / CODEOWNERS
    target.write_bytes(target.read_bytes() + b"docs/  @carla\n")
    result = run_op(repo.path, env_for(tmp_path, repo.env))
    envelope = assert_valid(result)
    assert not result.ok and result.error is not None
    assert result.error.code == "E_CONFIG_DOCTOR_FAILED"
    assert result.error.details == {"failed": ["codeowners_sync"]}
    assert result.exit_code() == 8
    data = envelope["data"]
    assert isinstance(data, dict) and len(data["checks"]) == len(op.CHECK_ORDER)  # type: ignore[arg-type]
    codeowners = checks_of(result)["codeowners_sync"]
    assert codeowners["status"] == "fail" and CODEOWNERS in str(codeowners["message"])
    assert "sync-roles" in str(codeowners["fix"])


def test_skill_files_fail_generated_files(tmp_path: Path) -> None:
    repo = new_repo(tmp_path, extra={".claude/skills/status/SKILL.md": "---\nname: status\n---\n"})
    result = run_op(repo.path, env_for(tmp_path, repo.env))
    assert result.error is not None and result.error.code == "E_CONFIG_DOCTOR_FAILED"
    assert result.error.details == {"failed": ["generated_files"]}


def test_invalid_roles_yaml_fails(tmp_path: Path, repo: GitRepo) -> None:
    repo.write({"roles.yaml": "version: 1\npeople: 3\n"})
    result = run_op(repo.path, env_for(tmp_path, repo.env))
    assert_valid(result)
    st = statuses(result)
    assert st["roles_yaml"] == "fail" and st["codeowners_sync"] == "skip"
    assert st["deputy"] == "ok" and st["membership"] == "ok"  # salen de la base (D-08)
    assert result.error is not None and result.error.details == {"failed": ["roles_yaml"]}


def test_invalid_project_yaml_fails(tmp_path: Path, repo: GitRepo) -> None:
    repo.write({"project.yaml": "convention: 3\n"})
    result = run_op(repo.path, env_for(tmp_path, repo.env))
    assert_valid(result)
    st = statuses(result)
    assert st["project_yaml"] == "fail"
    assert st["remote"] == "skip" and st["codeowners_sync"] == "skip"  # leen el árbol
    # D-08 y REQ-ARCH-46: OpContext.base() no lee el project.yaml del árbol, así que la
    # convención, el suplente y el perfil salen de la base, que sigue siendo válida.
    assert st["convention"] == "ok" and st["deputy"] == "ok" and st["profile"] == "ok"
    assert result.data is not None and result.data["convention"]["project"] == "0.1"
    assert result.error is not None and result.error.details == {"failed": ["project_yaml"]}


def test_incompatible_convention_fails(tmp_path: Path) -> None:
    newer = pinned_to("0.2.0")
    repo = new_repo(tmp_path, extra={"project.yaml": newer})
    result = run_op(repo.path, env_for(tmp_path, repo.env))
    convention = checks_of(result)["convention"]
    assert convention["status"] == "fail" and convention["fix"]
    assert result.error is not None and "convention" in result.error.details["failed"]  # type: ignore[index]


def test_python_launcher_missing_warns_with_install_command(tmp_path: Path, repo: GitRepo) -> None:
    result = run_op(repo.path, env_for(tmp_path, repo.env, launcher=False))
    python = checks_of(result)["python"]
    assert python["status"] == "warn"
    fix = str(python["fix"])
    assert f"uv tool install \"{_meta.NAME}=={_meta.__version__}\"" in fix and "uv tool update-shell" in fix  # core_pin; REQ-ARCH-33
    assert "--constraints requirements-lock.txt" in fix  # con el lock de la Release (10-seguridad REQ-SEC-45)


@pytest.mark.skipif(os.name != "nt", reason="solo Windows exige un .exe")
def test_python_launcher_must_be_an_exe_on_windows(tmp_path: Path) -> None:
    folder = fake_launcher(tmp_path / "bat", suffix=".bat")
    env = clean_env(os.environ)
    env["PATH"] = os.pathsep.join([str(folder), GIT_DIR])
    outside = tmp_path / "fuera"
    outside.mkdir()
    python = checks_of(run_op(outside, env))["python"]
    assert python["status"] == "warn" and str(python["message"]).lower().endswith(".bat.")


def test_a_copy_inside_a_packaged_app_is_reported(tmp_path: Path) -> None:
    """05 §9.2: lo que se instala desde una app empaquetada de Windows queda en su copia privada, y esa app la
    usa en lugar de la instalada; ``doctor`` la nombra con su versión y cómo quitarla."""
    env = env_for(tmp_path)
    tools = Path(env["LOCALAPPDATA"], "Packages", "Claude_abc123", "LocalCache", "Roaming", "uv", "tools", _meta.NAME)
    (tools / "Lib" / "site-packages" / f"{_meta.NAME}-0.1.0rc2.dist-info").mkdir(parents=True)
    outside = tmp_path / "fuera"
    outside.mkdir()
    python = checks_of(run_op(outside, env))["python"]
    assert python["status"] == "warn"
    assert "Claude_abc123" in str(python["message"]) and "0.1.0rc2" in str(python["message"])
    assert f'Remove-Item -Recurse -Force "{tools}"' in str(python["fix"])


def test_without_a_packaged_copy_there_is_nothing_to_report(tmp_path: Path) -> None:
    outside = tmp_path / "fuera"
    outside.mkdir()
    python = checks_of(run_op(outside, env_for(tmp_path)))["python"]
    assert python["status"] == "ok" and "Claude" not in str(python["message"])


def test_outside_a_project_doctor_points_to_the_dry_run_of_init(tmp_path: Path) -> None:
    """Fuera de un proyecto no hay repo contra el cual revisar el token: la pista es ``init --repo --dry-run``."""
    outside = tmp_path / "fuera"
    outside.mkdir()
    root = checks_of(run_op(outside, env_for(tmp_path)))["project_root"]
    assert root["status"] == "warn" and f"{_meta.NAME} init --repo <owner>/<repo> --dry-run" in str(root["fix"])


def test_python_version_is_reported(tmp_path: Path) -> None:
    outside = tmp_path / "fuera"
    outside.mkdir()
    message = str(checks_of(run_op(outside, env_for(tmp_path)))["python"]["message"])
    assert f"{sys.version_info.major}.{sys.version_info.minor}." in message


def test_git_missing_fails(tmp_path: Path) -> None:
    outside = tmp_path / "fuera"
    outside.mkdir()
    result = run_op(outside, env_for(tmp_path, git=False))
    assert_valid(result)
    assert checks_of(result)["git"]["status"] == "fail"
    assert result.error is not None and result.error.details == {"failed": ["git"]}


def test_remote_problems(tmp_path: Path) -> None:
    repo = new_repo(tmp_path, github=False)  # origin es una ruta local
    env = env_for(tmp_path, repo.env)
    assert checks_of(run_op(repo.path, env))["remote"]["status"] == "warn"
    repo.git("remote", "set-url", "origin", GITHUB_URL)
    repo.git("update-ref", "-d", "refs/remotes/origin/main")
    remote = checks_of(run_op(repo.path, env))["remote"]
    assert remote["status"] == "warn" and "git fetch" in str(remote["fix"])
    repo.git("remote", "remove", "origin")
    result = run_op(repo.path, env)
    assert checks_of(result)["remote"]["status"] == "fail"
    assert_valid(result)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/example-org/demo.git",
        "https://github.com/Example-Org/demo",
        "git@github.com:example-org/demo.git",
        "ssh://git@github.com/example-org/demo.git",
        "https://token@github.com/example-org/demo/",
    ],
)
def test_matches_repo_accepts_github_forms(url: str) -> None:
    assert op.matches_repo(url, "example-org/demo")


@pytest.mark.parametrize("url", ["https://gitlab.com/example-org/demo.git", "https://github.com/example-org/other", "/tmp/x.git"])
def test_matches_repo_rejects_others(url: str) -> None:
    assert not op.matches_repo(url, "example-org/demo")


@pytest.mark.parametrize(("email", "ok"), [
    ("2+bruno@users.noreply.github.com", True), ("bruno@users.noreply.github.com", True),
    ("BRUNO@USERS.NOREPLY.GITHUB.COM", True), ("@users.noreply.github.com", False),
    ("bruno@example.org", False), ("bruno@users.noreply.github.com.evil", False), (None, False),
])
def test_is_noreply(email: str | None, ok: bool) -> None:
    assert op.is_noreply(email) is ok


def test_cache_problems(tmp_path: Path, repo: GitRepo) -> None:
    env = env_for(tmp_path, repo.env)
    cache = repo.path / _meta.CACHE_DIR
    assert not cache.exists()
    assert checks_of(run_op(repo.path, env))["cache"]["status"] == "ok"
    Store(repo.path).write_identity(IdentityCache("ana", 1, FAR), token_fp=None)  # crea la caché con .gitignore
    (cache / "work").mkdir()
    (cache / "work" / "pending.patch").write_text("x\n", encoding="utf-8")
    work = checks_of(run_op(repo.path, env))["cache"]
    assert work["status"] == "warn" and "work/" in str(work["message"])
    shutil.rmtree(cache / "work")
    (cache / ".gitignore").write_text("", encoding="utf-8")
    assert checks_of(run_op(repo.path, env))["cache"]["status"] == "warn"
    shutil.rmtree(cache)
    cache.write_text("no soy una carpeta\n", encoding="utf-8")
    result = run_op(repo.path, env)
    assert checks_of(result)["cache"]["status"] == "fail"
    assert result.error is not None and "cache" in result.error.details["failed"]  # type: ignore[index]


def test_without_fetched_base_the_policy_checks_fail(tmp_path: Path) -> None:
    env_git = isolated_git_env(tmp_path / "git-env")
    path = tmp_path / "repo"
    path.mkdir()
    repo = GitRepo(path, tmp_path / "origin.git", env_git)
    repo.git("init", "-q", "-b", "main")
    roles_text = roles_fixture("basic")
    repo.commit("local", files={"project.yaml": PROJECT, "roles.yaml": roles_text, MCP_JSON_PATH: mcp_json(),
                                **expected_files(roles_text)})
    Store(repo.path).write_identity(IdentityCache("ana", 1, FAR), token_fp=None)
    result = run_op(repo.path, env_for(tmp_path, repo.env))
    assert_valid(result)
    st = statuses(result)
    # D-08 y REQ-ARCH-46: sin política de la base no se usa la del árbol (REQ-PERM-3)
    assert st["deputy"] == "fail" and st["convention"] == "fail"
    assert st["codeowners_sync"] == "ok"  # la regeneración sí lee el árbol (05 §9.4)
    assert st["remote"] == "fail"  # sin origin
    assert all(st[c] == "skip" for c in FORGE_CHECKS)  # sin política no se sabe qué repositorio consultar
    assert checks_of(result)["identity"]["message"] == translator("es")("doctor.forge.skip_policy")
    assert result.error is not None and result.error.details == {"failed": ["convention", "remote", "deputy"]}
    assert result.data is not None and result.data["role"] is None and result.data["is_deputy"] is False
    convention = checks_of(result)["convention"]
    assert "ref_missing" in str(convention["message"]) and "git fetch" in str(convention["fix"])


def test_offline_adds_net_offline(tmp_path: Path, repo: GitRepo) -> None:
    env = env_for(tmp_path, repo.env)
    env[_meta.ENV_PREFIX + "OFFLINE"] = "1"
    forge = fake_forge()
    result = run_op(repo.path, env, forge=forge)
    assert codes(result) == [*EXTRA_WARNINGS, "W_FORGE_CAPABILITY_UNKNOWN", "W_NET_OFFLINE"]
    assert len(forge.calls) == 0


def test_policy_fallback_warning(tmp_path: Path, repo: GitRepo) -> None:
    repo.git("remote", "set-url", "origin", str(repo.origin))
    repo.commit("roles roto", files={"roles.yaml": "version: 1\npeople: 3\n"})
    repo.push("main")
    repo.fetch()
    repo.write({"roles.yaml": roles_fixture("basic")})
    result = run_op(repo.path, env_for(tmp_path, repo.env))
    assert "W_CONFIG_POLICY_FALLBACK" in codes(result)
    assert_valid(result)


def test_policy_fallback_warning_carries_each_file_sha(tmp_path: Path, repo: GitRepo) -> None:
    """REQ-PERM-4: el aviso da el sha usado de ``project.yaml`` y de ``roles.yaml``."""
    from tests.unit.test_base_snapshot import BAD_PROJECT, BAD_ROLES

    repo.git("remote", "set-url", "origin", str(repo.origin))
    c1 = repo.git("rev-parse", "HEAD")
    no_deputy = roles_fixture("no-deputy")
    c2 = repo.commit("sin suplente", files={"roles.yaml": no_deputy})
    repo.commit("rota", files={"project.yaml": BAD_PROJECT, "roles.yaml": BAD_ROLES})
    repo.push("main")
    repo.fetch()
    repo.write({"project.yaml": PROJECT, "roles.yaml": no_deputy})
    result = run_op(repo.path, env_for(tmp_path, repo.env))
    assert_valid(result)
    warns = [w for w in result.warnings if w.code == "W_CONFIG_POLICY_FALLBACK"]
    assert len(warns) == 1, result.to_dict()
    assert warns[0].details == {"sha": c2, "files": {"project.yaml": c1, "roles.yaml": c2}}
    assert checks_of(result)["deputy"]["status"] == "warn"  # la política es la de c2, sin suplente


def test_output_is_trimmed_to_the_budget(tmp_path: Path, repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    env = env_for(tmp_path, repo.env)
    full = run_op(repo.path, env)
    assert "W_OUTPUT_TRUNCATED" not in codes(full)
    budget = len(json.dumps(full.to_dict(), ensure_ascii=False, separators=(",", ":"))) - 300
    monkeypatch.setattr(op, "BUDGET", budget)
    result = run_op(repo.path, env)
    envelope = assert_valid(result)
    assert len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))) <= budget
    assert "W_OUTPUT_TRUNCATED" in codes(result)
    kept = checks_of(result)
    assert all(kept[c]["status"] == "skip" for c in op.SKIPPED)  # se recortan primero los ok
    assert len(kept) < len(op.CHECK_ORDER)


def test_render_text(tmp_path: Path, repo: GitRepo) -> None:
    target = repo.path / CODEOWNERS
    target.write_bytes(b"# a mano\n")
    result = run_op(repo.path, env_for(tmp_path, repo.env))
    text = op.render_text(result, translator("es"))
    lines = text.splitlines()
    assert lines[0] == f"Núcleo {_meta.__version__} · {repo.path} · perfil enforced"
    assert any(line.startswith("[falla] codeowners_sync: ") for line in lines)
    assert any(line.startswith("  solución: ") and "sync-roles" in line for line in lines)
    outside = tmp_path / "fuera"
    outside.mkdir()
    text_en = op.render_text(run_op(outside, env_for(tmp_path), lang="en"), translator("en"))
    assert text_en.splitlines()[0] == f"Core {_meta.__version__} · outside a project · profile advisory"


# -- evidence_tools (05 §9.2; REQ-ARCH-99) -------------------------------------------------------


def test_evidence_tools_ok_with_the_extra(tmp_path: Path) -> None:
    if missing_extra():
        pytest.skip("falta el extra evidence (el CI lo instala)")
    outside = tmp_path / "fuera"
    outside.mkdir()
    check = checks_of(run_op(outside, env_for(tmp_path)))["evidence_tools"]
    assert check == {"id": "evidence_tools", "status": "ok", "message": translator("es")("doctor.evidence_tools.ok"), "fix": None}


def test_evidence_tools_warns_without_the_extra(tmp_path: Path, repo: GitRepo, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "rfc3161_client", None)
    monkeypatch.setitem(sys.modules, "opentimestamps", None)
    result = run_op(repo.path, env_for(tmp_path, repo.env))
    assert_valid(result)
    assert result.ok  # warn no es fail: doctor sigue en éxito
    check = checks_of(result)["evidence_tools"]
    assert check["status"] == "warn"
    assert "rfc3161_client" in str(check["message"]) and "opentimestamps" in str(check["message"])
    assert f'"{_meta.NAME}[evidence]=={_meta.__version__}" --constraints requirements-lock.txt' in str(check["fix"])  # core_pin
    assert codes(result)[-1:] == ["W_CONFIG_TOOL_MISSING"]


def test_evidence_tools_only_looks_for_the_modules() -> None:
    """El check usa ``find_spec``: ni doctor ni el check cargan el extra (REQ-ARCH-99)."""
    code = (
        "import sys\n"
        f"from {_meta.NAME}.ops import doctor\n"
        f"from {_meta.NAME}.evidence.verify import missing_extra\n"
        "missing_extra()\n"
        "print(sorted(m for m in ('rfc3161_client', 'opentimestamps', 'cryptography', 'bitcoin') if m in sys.modules))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(Path(op.__file__).resolve().parents[2]))
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert proc.stdout.decode().strip() == "[]"
