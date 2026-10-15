# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Check ``evidence_signing`` de ``doctor`` (05-operaciones §9.2; 00-marco D-72; 09-ci REQ-CI-61).

Con la política de la base: el modo, una llave vigente y, con ``mode: ci``, el environment
``<nombre>-evidence`` con la rama base como única regla. El environment se lee con el token
de uso diario; lo que no se puede leer queda como desconocido (D-65). Usa el mundo de
``test_op_doctor`` (git real y FakeForge).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aucove import _meta
from aucove.forge.fake import Fault
from aucove.i18n import translator
from aucove.ops.protections import EVIDENCE_ENVIRONMENT
from tests.unit.test_op_doctor import (
    PROJECT,
    assert_valid,
    checks_of,
    env_for,
    fake_forge,
    new_repo,
    run_op,
)

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHO4TeimWDqbJtD9DZ0jEQ7osipbPsIQKXvEpl+4J8Ve"
T = translator("es")


def project(mode: str | None, *, since: str = "2026-02-01T00:00:00Z") -> str:
    if mode is None:
        return PROJECT
    block = f"evidence_signing:\n  mode: {mode}\n"
    if mode != "none":
        block += f'  keys:\n    - id: ci-2026\n      public_key: "{KEY}"\n      since: "{since}"\n'
    return PROJECT + block


def check(tmp_path: Path, mode: str | None, *, forge: object = None, since: str = "2026-02-01T00:00:00Z",
          profile: str = "enforced") -> dict[str, object]:
    repo = new_repo(tmp_path, extra={"project.yaml": project(mode, since=since)})
    adapter = forge if forge is not None else fake_forge(profile=profile)
    result = run_op(repo.path, env_for(tmp_path, repo.env), forge=adapter)
    assert_valid(result)
    return checks_of(result)["evidence_signing"]


def test_without_signing_it_warns_with_the_guide(tmp_path: Path) -> None:
    for folder, mode in (("sin-bloque", None), ("modo-none", "none")):
        got = check(tmp_path / folder, mode)
        assert (got["status"], got["message"], got["fix"]) == (
            "warn", T("doctor.evidence_signing.warn_none"), T("doctor.evidence_signing.fix_guide"))


def test_without_a_current_key_it_fails(tmp_path: Path) -> None:
    got = check(tmp_path, "ci", since="2099-01-01T00:00:00Z")
    assert (got["status"], got["message"]) == ("fail", T("doctor.evidence_signing.fail_no_key", mode="ci"))


def ok_message(mode: str) -> str:
    return T("doctor.evidence_signing.ok", mode=mode, keys="ci-2026")


def test_ci_with_the_environment_of_the_base_is_ok(tmp_path: Path) -> None:
    forge = fake_forge()
    forge.seed_environment(EVIDENCE_ENVIRONMENT, rules=[("branch", "main")], secrets=[f"{_meta.ENV_PREFIX}EVIDENCE_KEY"])
    got = check(tmp_path, "ci", forge=forge)
    assert got["status"] == "ok"
    assert got["message"] == ok_message("ci") + " " + T(
        "doctor.evidence_signing.environment_ok", environment=EVIDENCE_ENVIRONMENT, base="main")


@pytest.mark.parametrize(
    ("policy", "rules"),
    [("all", []), ("protected", []), ("custom", [("branch", "main"), ("branch", "dev")]),
     ("custom", [("branch", "*")]), ("custom", [("tag", "main")]), ("custom", [])],
)
def test_ci_with_an_environment_open_to_other_refs_fails(tmp_path: Path, policy: str, rules: list[tuple[str, str]]) -> None:
    forge = fake_forge()
    forge.seed_environment(EVIDENCE_ENVIRONMENT, policy=policy, rules=rules)  # type: ignore[arg-type]
    got = check(tmp_path, "ci", forge=forge)
    assert (got["status"], got["message"], got["fix"]) == (
        "fail", T("doctor.evidence_signing.fail_environment_rules", environment=EVIDENCE_ENVIRONMENT, base="main"),
        T("doctor.evidence_signing.fix_init", name=_meta.NAME))


def test_ci_without_the_environment_fails_and_admin_does_not_need_it(tmp_path: Path) -> None:
    got = check(tmp_path / "ci", "ci")
    assert (got["status"], got["message"]) == (
        "fail", T("doctor.evidence_signing.fail_environment_missing", environment=EVIDENCE_ENVIRONMENT))
    assert got["fix"] == T("doctor.evidence_signing.fix_init", name=_meta.NAME)
    admin = check(tmp_path / "admin", "admin")
    assert (admin["status"], admin["message"]) == ("ok", ok_message("admin"))


def test_a_private_free_repository_has_no_environments(tmp_path: Path) -> None:
    """Sin environments (privado en el plan Free), ``ci`` no puede guardar la llave: ``admin`` o Team."""
    got = check(tmp_path / "ci", "ci", profile="advisory")
    assert (got["status"], got["message"], got["fix"]) == (
        "fail", T("doctor.evidence_signing.fail_no_environments"), T("doctor.evidence_signing.fix_no_environments"))
    admin = check(tmp_path / "admin", "admin", profile="advisory")
    assert (admin["status"], admin["message"]) == ("ok", ok_message("admin"))


def test_an_unreadable_environment_is_unknown(tmp_path: Path) -> None:
    forge = fake_forge()
    forge.faults.add(Fault("environment", "http_403"))
    got = check(tmp_path, "ci", forge=forge)
    assert (got["status"], got["message"]) == ("ok", ok_message("ci") + " " + T(
        "doctor.evidence_signing.environment_unreadable", environment=EVIDENCE_ENVIRONMENT))


def test_without_the_forge_the_environment_is_not_checked(tmp_path: Path) -> None:
    forge = fake_forge()
    forge.faults.add(Fault("identity", "unreachable"))
    got = check(tmp_path, "ci", forge=forge)
    assert got["status"] == "ok"
    assert str(got["message"]).startswith(ok_message("ci"))
    assert T("doctor.evidence_signing.forge_unchecked") in str(got["message"]) or T(
        "doctor.evidence_signing.environment_unreadable", environment=EVIDENCE_ENVIRONMENT) in str(got["message"])


def test_doctor_reads_the_environment_only_with_get(tmp_path: Path) -> None:
    forge = fake_forge()
    forge.seed_environment(EVIDENCE_ENVIRONMENT, rules=[("branch", "main")])
    check(tmp_path, "ci", forge=forge)
    calls = [c for c in forge.calls.entries() if c.method == "environment"]
    assert calls and all(c.kind == "L" for c in calls)
