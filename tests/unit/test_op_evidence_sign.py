# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``evidence sign <paquete> --key <archivo>`` (05-operaciones §9.27; 11-trazabilidad §8.10; 00-marco D-72).

En el proceso, con un proyecto real (git y ``origin`` en disco), la TSA de prueba de
``test_ci_seal`` (``<PREFIJO>EVIDENCE_FAKE``) y ``ssh-keygen`` para firmar (las pruebas que
firman se saltan sin él). La persona en la terminal es ``Typed``: teclea el reto
``sign <carpeta>``. Los errores de configuración, de entrada, de herramientas y de red llegan
antes del reto, y nada se escribe en el paquete salvo la carpeta de firma completa.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

pytest.importorskip("rfc3161_client", reason="falta el extra evidence (el CI lo instala)")
pytest.importorskip("opentimestamps", reason="falta el extra evidence (el CI lo instala)")

from aucove import _meta  # noqa: E402
from aucove.config.schemas import validate_result  # noqa: E402
from aucove.domain import signing  # noqa: E402
from aucove.evidence import package as pkg  # noqa: E402
from aucove.evidence import sshsig  # noqa: E402
from aucove.evidence import verify as ev  # noqa: E402
from aucove.i18n import translator  # noqa: E402
from aucove.ops import evidence_sign  # noqa: E402
from aucove.ops.context import Invocation, OpContext  # noqa: E402
from aucove.ops.registry import REGISTRY, invoke  # noqa: E402
from aucove.ops.result import Result  # noqa: E402
from tests.board_world import Typed  # noqa: E402
from tests.conftest import GitRepo  # noqa: E402
from tests.fixtures.evidence.tsa import fake_tsa as ft  # noqa: E402
from tests.unit.evidence.support import needs_git  # noqa: E402
from tests.unit.test_ci_seal import HOOK_SOURCE, Services, hook_services  # noqa: E402
from tests.unit.test_validate_modes import PROJECT, seed_base  # noqa: E402

pytestmark = needs_git

SPEC = next(s for s in REGISTRY if s.id == "evidence.sign")
PREFIX = _meta.ENV_PREFIX
KEY_ID = "admin-2026"
AGENT_VARS = frozenset({"CLAUDE_CODE_CHILD_SESSION", "CI", "GITHUB_ACTIONS"})  # fase 1 de require_human
SSH_KEYGEN = shutil.which("ssh-keygen")
needs_ssh_keygen = pytest.mark.skipif(SSH_KEYGEN is None, reason="sin ssh-keygen en el PATH")

_DOWN: Services | None = None


def down_services() -> Services:
    """Las TSA del catálogo sin ninguna ruta: ninguna prueba se obtiene."""
    global _DOWN
    if _DOWN is None:
        _DOWN = Services.make([ft.make_pki("alpha"), ft.make_pki("beta")], tsa=False, calendars=False)
    return _DOWN


HOOK_DOWN = """\
from tests.unit.test_op_evidence_sign import down_services


def transport():
    return down_services().router.transport()


def catalog():
    return down_services().catalog
"""


def signing_block(mode: str, public: str, *, since: str = "2026-02-01T00:00:00Z") -> str:
    return f'evidence_signing:\n  mode: {mode}\n  keys:\n    - id: {KEY_ID}\n      public_key: "{public}"\n      since: "{since}"\n'


def make_key(dest: Path) -> tuple[Path, str]:
    """Llave sin frase de paso creada por ``ssh-keygen`` (con sus permisos): ``(archivo, pública)``."""
    assert SSH_KEYGEN is not None
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", "", "-f", str(dest)], check=True,
                   stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    public = " ".join(dest.with_name(dest.name + ".pub").read_text(encoding="ascii").split()[:2])
    return dest, public


def make_package(dest: Path) -> Path:
    """Paquete con un archivo, una carpeta e ``index.txt`` en el formato de ``sha256sum``."""
    (dest / "integration-abc").mkdir(parents=True)
    (dest / "integration-abc" / "manifest.json").write_bytes(b'{"v":1}')
    (dest / "paper.pdf").write_bytes(b"%PDF-1.7\n")
    lines = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(dest).as_posix()}\n"
             for p in sorted(dest.rglob("*")) if p.is_file()]
    (dest / pkg.INDEX_FILE).write_text("".join(lines), encoding="utf-8", newline="\n")
    return dest


class World:
    """Proyecto con ``evidence_signing`` en la base, el paquete afuera y los dobles de la TSA."""

    def __init__(self, repo: GitRepo, tmp: Path, *, mode: str = "admin", public: str | None = None,
                 since: str = "2026-02-01T00:00:00Z", hook: str = HOOK_SOURCE) -> None:
        self.repo, self.tmp = repo, tmp
        self.public = public or "ssh-ed25519 " + "AAAAC3NzaC1lZDI1NTE5AAAAI" + "A" * 43
        project = PROJECT + (signing_block(mode, self.public, since=since) if mode != "none" else "")
        seed_base(repo, {"project.yaml": project})
        self.package = make_package(tmp / "hito-1")
        hook_file = tmp / "evidence_hook.py"
        hook_file.write_text(hook, encoding="utf-8")
        # una persona en su terminal: sin las variables de un agente ni las del CI (06-permisos §6)
        person = {k: v for k, v in repo.env.items() if k not in AGENT_VARS}
        self.env: dict[str, str] = {**person, PREFIX + "TESTING": "1", PREFIX + "EVIDENCE_FAKE": str(hook_file)}
        self.typed = Typed()

    def run(self, *, package: object = None, key: object = None, surface: str = "cli", dry_run: bool = False,
            env: Mapping[str, str] | None = None, typed: Typed | None = None, lang: str = "es") -> Result:
        inv = Invocation(root=None, lang=lang, json=True, dry_run=dry_run, surface=surface,  # type: ignore[arg-type]
                         env=dict(env or self.env), cwd=str(self.repo.path), tty=True,
                         confirm=typed or self.typed)
        result = invoke(SPEC, OpContext(inv), package=str(self.package) if package is None else package,
                        key=str(self.tmp / "keys" / "id_ed25519") if key is None else key)
        assert validate_result(result.to_dict()) == [], result.to_dict()
        return result


def error(result: Result) -> tuple[str, Mapping[str, object]]:
    assert not result.ok and result.error is not None, result.to_dict()
    return result.error.code, dict(result.error.details or {})


def written(package: Path) -> list[str]:
    return [p.name for p in pkg.signature_dirs(package)]


# -- firma ------------------------------------------------------------------------------------------


@needs_ssh_keygen
def test_signs_the_index_and_stamps_the_signature(git_repo: GitRepo, tmp_path: Path) -> None:
    key, public = make_key(tmp_path / "keys" / "id_ed25519")
    world = World(git_repo, tmp_path, public=public)
    result = world.run()
    assert result.ok, result.to_dict()
    data = result.data
    assert data is not None
    index = (world.package / pkg.INDEX_FILE).read_bytes()
    fingerprint = signing.fingerprint(signing.parse_public_key(public))
    assert (data["package"], data["folder"], data["files"], data["key_id"], data["key"]) == (
        str(world.package), "signature", 2, KEY_ID, fingerprint)
    assert data["index_sha256"] == hashlib.sha256(index).hexdigest()
    assert [(p["type"], p["source"], p["status"]) for p in data["proofs"]] == [
        ("rfc3161", "alpha", "ok"), ("rfc3161", "beta", "ok"), ("ots", "calendars", "ok")]
    # el reto es sign <carpeta>, y el plan dice qué se firma y con qué llave
    ((op, target, summary),) = world.typed.seen
    assert (op, target) == ("sign", "hito-1")
    assert data["index_sha256"] in summary and fingerprint in summary and str(key) in summary
    folder = world.package / "signature"
    assert sshsig.verify((folder / "index.txt.sig").read_bytes(), index).fingerprint == fingerprint
    assert "VERIFY.md" in {p.name for p in folder.iterdir()}
    keys, _problems = signing.parse_keys([{"id": KEY_ID, "public_key": public, "since": "2026-02-01T00:00:00Z"}])
    checked = ev.verify_signature_folder(folder, world.package, catalog=hook_services().catalog, keys=keys)
    assert (checked.verdict, checked.confidence, checked.origin) == ("verified", "strong", "project")
    # una segunda firma va en signature-2 y no toca la primera
    before = {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    again = world.run()
    assert again.ok and again.data is not None and again.data["folder"] == "signature-2"
    assert written(world.package) == ["signature", "signature-2"]
    assert before == {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    text = evidence_sign.render_text(result, translator("es"))
    assert "signature" in text and KEY_ID in text and "alpha" in text


@needs_ssh_keygen
def test_a_key_that_is_not_of_the_project_writes_nothing(git_repo: GitRepo, tmp_path: Path) -> None:
    _project_key, public = make_key(tmp_path / "project" / "id_ed25519")
    other, other_public = make_key(tmp_path / "keys" / "id_ed25519")
    world = World(git_repo, tmp_path, public=public)
    code, details = error(world.run(key=str(other)))
    assert (code, details["reason"]) == ("E_CONFIG_EVIDENCE_KEY", "not_in_project")
    assert details["key"] == signing.fingerprint(signing.parse_public_key(other_public))
    assert written(world.package) == []


@needs_ssh_keygen
def test_a_key_file_that_ssh_keygen_cannot_use(git_repo: GitRepo, tmp_path: Path) -> None:
    _key, public = make_key(tmp_path / "project" / "id_ed25519")
    world = World(git_repo, tmp_path, public=public)
    bad = tmp_path / "keys" / "id_ed25519"
    bad.parent.mkdir()
    bad.write_text("no es una llave\n", encoding="ascii")
    code, details = error(world.run(key=str(bad)))
    assert (code, details["reason"]) == ("E_CONFIG_EVIDENCE_KEY", "sign_failed")
    assert written(world.package) == []


@needs_ssh_keygen
def test_without_any_proof_nothing_is_written(git_repo: GitRepo, tmp_path: Path) -> None:
    key, public = make_key(tmp_path / "keys" / "id_ed25519")
    world = World(git_repo, tmp_path, public=public, hook=HOOK_DOWN)
    code, details = error(world.run(key=str(key)))
    assert code == "E_NET_TIMEOUT"
    proofs = details["proofs"]
    assert isinstance(proofs, list) and {p["status"] for p in proofs} == {"error"}
    assert written(world.package) == [] and [p.name for p in world.package.iterdir() if p.name.startswith(".")] == []


# -- antes del reto ----------------------------------------------------------------------------------


@pytest.mark.parametrize(("mode", "since", "reason"), [
    ("none", "2026-02-01T00:00:00Z", "mode_none"),
    ("admin", "2099-01-01T00:00:00Z", "no_current_key"),
])
def test_the_project_must_have_a_current_key(git_repo: GitRepo, tmp_path: Path, mode: str, since: str,
                                             reason: str) -> None:
    world = World(git_repo, tmp_path, mode=mode, since=since)
    assert error(world.run()) == ("E_CONFIG_EVIDENCE_KEY", {"reason": reason})
    assert world.typed.seen == []


def test_input_errors(git_repo: GitRepo, tmp_path: Path) -> None:
    world = World(git_repo, tmp_path)
    code, details = error(world.run(package=str(tmp_path / "no-existe"), key=str(tmp_path / "tampoco")))
    assert code == "E_VALIDATION_INPUT"
    assert details["errors"] == [{"param": "package", "reason": "not_found"}, {"param": "key", "reason": "not_found"}]
    key = tmp_path / "keys" / "id_ed25519"
    key.parent.mkdir()
    key.write_text("x", encoding="ascii")
    # el paquete va fuera del árbol de trabajo (11-trazabilidad REQ-TRACE-85)
    inside = make_package(git_repo.path / "evidencia")
    assert error(world.run(package=str(inside))) == (
        "E_VALIDATION_INPUT", {"errors": [{"param": "package", "reason": "inside_project"}]})
    (world.package / pkg.INDEX_FILE).unlink()
    assert error(world.run()) == (
        "E_VALIDATION_INPUT", {"errors": [{"param": "package", "reason": "no_index", "problem": "missing"}]})
    (world.package / pkg.INDEX_FILE).write_text("esto no es un índice\n", encoding="utf-8")
    assert error(world.run()) == ("E_VALIDATION_INPUT", {"errors": [{"param": "package", "reason": "bad_index",
                                                                      "line": 1}]})
    assert world.typed.seen == []


def test_tools_and_network_are_checked_before_the_challenge(
    git_repo: GitRepo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World(git_repo, tmp_path)
    key = tmp_path / "keys" / "id_ed25519"
    key.parent.mkdir()
    key.write_text("x", encoding="ascii")
    with monkeypatch.context() as patch:  # git sigue en el PATH: solo falta la herramienta de firma
        patch.setattr(evidence_sign, "SSH_KEYGEN", "ssh-keygen-que-no-existe")
        assert error(world.run()) == ("E_CONFIG_TOOL_MISSING", {"tool": "ssh-keygen-que-no-existe"})
    if SSH_KEYGEN is not None:
        offline = world.run(env={**world.env, PREFIX + "OFFLINE": "1"})
        assert error(offline)[0] == "E_NET_OFFLINE"
    monkeypatch.setattr(ev, "missing_extra", lambda: True)
    assert error(world.run()) == ("E_CONFIG_TOOL_MISSING", {"tool": "evidence"})
    assert world.typed.seen == [] and written(world.package) == []


@needs_ssh_keygen
def test_dry_run_plans_the_folder_without_asking(git_repo: GitRepo, tmp_path: Path) -> None:
    _key, public = make_key(tmp_path / "keys" / "id_ed25519")
    world = World(git_repo, tmp_path, public=public)
    result = world.run(dry_run=True)
    assert result.ok and result.data is not None and result.data.get("dry_run") is True, result.to_dict()
    effects = [e for item in result.data["plan"] for e in item.get("effects", [])] or result.data["plan"]
    assert any("signature" in str(e) for e in effects), result.data
    assert world.typed.seen == [] and written(world.package) == []


@needs_ssh_keygen
def test_only_a_person_at_the_terminal_signs(git_repo: GitRepo, tmp_path: Path) -> None:
    _key, public = make_key(tmp_path / "keys" / "id_ed25519")
    world = World(git_repo, tmp_path, public=public)
    wrong = Typed(answer="sign otra-carpeta")
    assert error(world.run(typed=wrong))[0] == "E_HUMAN_ONLY_NOT_CONFIRMED"
    assert wrong.seen == [("sign", "hito-1", wrong.seen[0][2])]
    assert error(world.run(surface="mcp"))[0] == "E_HUMAN_ONLY_NO_TTY"
    # una sesión de un agente, o el CI, no firma aunque tenga terminal (10-seguridad SB10)
    for name, value in (("CLAUDE_CODE_CHILD_SESSION", "1"), ("CI", "true"), ("GITHUB_ACTIONS", "true")):
        assert error(world.run(env={**world.env, name: value}))[0] == "E_HUMAN_ONLY_AGENT_ENV"
    assert world.typed.seen == [] and written(world.package) == []


def test_the_operation_is_human_only_and_cli_only() -> None:
    assert SPEC.human_only and SPEC.mcp_tool is None and SPEC.cli == ("evidence", "sign")
    assert [(p.name, p.required, p.cli_only) for p in SPEC.params] == [("package", True, True), ("key", True, True)]
    assert evidence_sign.STEPS == ("sign", "seal", "write")
