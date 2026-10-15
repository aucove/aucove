# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``ci seal`` con la firma del proyecto (09-ci REQ-CI-61; 11-trazabilidad §8.10; 00-marco D-72).

Con ``evidence_signing.mode: ci`` en la política, el registro ``integration`` va firmado con
la llave de ``<PREFIJO>EVIDENCE_KEY`` y las pruebas sellan la firma (``manifest.json.sig``).
Sin el secreto, con una llave que no es una vigente del proyecto o si no se puede firmar, el
registro se sella igual, sin firma, y una anotación de Actions dice por qué, antes de las
salidas. ``submission`` y los modos ``admin`` y ``none`` nunca firman. La llave privada no
aparece en ninguna salida, archivo ni comentario. Usa el mundo de ``test_ci_seal`` (git real,
FakeForge y la TSA de prueba).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("rfc3161_client", reason="falta el extra evidence (el CI lo instala)")
pytest.importorskip("opentimestamps", reason="falta el extra evidence (el CI lo instala)")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from aucove import _meta  # noqa: E402
from aucove.domain import signing  # noqa: E402
from aucove.evidence import sshsig  # noqa: E402
from aucove.evidence import verify as ev  # noqa: E402
from aucove.evidence.record import MANIFEST_FILE, SIG_FILE, read_record_dir  # noqa: E402
from aucove.i18n import translator  # noqa: E402
from aucove.ops import ci_seal  # noqa: E402
from tests.unit.evidence.support import needs_git  # noqa: E402
from tests.unit.test_ci_seal import NOW, Result, World, _e1_world  # noqa: E402
from tests.unit.test_ci_seal import services as services  # noqa: E402, F401 - fixtures del mundo
from tests.unit.test_ci_seal import pkis as pkis  # noqa: E402, F401
from tests.unit.test_ci_seal import world as world  # noqa: E402, F401

pytestmark = needs_git

SECRET_VAR = f"{_meta.ENV_PREFIX}EVIDENCE_KEY"
KEY_ID = "ci-2026"
TITLE = f"::warning title={_meta.NAME}/seal-integration::"


def key_pair(seed: str) -> tuple[str, str]:
    """``(privada OpenSSH, pública)`` de una llave ``ssh-ed25519`` que sale de ``seed``."""
    key = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(seed.encode("utf-8")).digest())
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                serialization.NoEncryption()).decode("ascii")
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH,
                                           serialization.PublicFormat.OpenSSH).decode("ascii")
    return private, public


PRIVATE, PUBLIC = key_pair("la llave del proyecto")
OTHER_PRIVATE, _OTHER_PUBLIC = key_pair("una llave ajena")


def use_signing(world: World, mode: str, *, since: str = "2026-02-01T00:00:00Z", until: str | None = None,
                public: str = PUBLIC) -> None:
    """Agrega ``evidence_signing`` al ``project.yaml`` de la política (03-esquemas)."""
    path = world.policy / "project.yaml"
    block = f"evidence_signing:\n  mode: {mode}\n"
    if mode != "none":
        block += f'  keys:\n    - id: {KEY_ID}\n      public_key: "{public}"\n      since: "{since}"\n'
        if until is not None:
            block += f'      until: "{until}"\n'
    path.write_text(path.read_text(encoding="utf-8") + block, encoding="utf-8", newline="\n")


def project_keys() -> tuple[signing.EvidenceKey, ...]:
    keys, problems = signing.parse_keys([{"id": KEY_ID, "public_key": PUBLIC, "since": "2026-02-01T00:00:00Z"}])
    assert problems == ()
    return keys


def texts(world: World, result: Result) -> Iterator[str]:
    """Todo lo que sale del run: salidas, resumen, archivos de la carpeta y comentarios en la forja."""
    yield result.out
    yield result.err
    if result.summary.exists():
        yield result.summary_text()
    for path in result.out_dir.rglob("*"):
        if path.is_file():
            yield path.read_bytes().decode("utf-8", "replace")
    for number in (5,):
        for comment in world.forge.list_comments(number):
            yield comment.body


def assert_no_secret(world: World, result: Result, private: str = PRIVATE) -> None:
    lines = [line for line in private.splitlines() if line.strip() and not line.startswith("-----")]
    for text in texts(world, result):
        assert "PRIVATE KEY" not in text
        for line in lines:
            assert line not in text


def files(folder: Path) -> set[str]:
    return {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}


# -- firmado ------------------------------------------------------------------------------------------


def test_integration_is_signed_and_the_proofs_stamp_the_signature(world: World) -> None:
    use_signing(world, "ci")
    shas = _e1_world(world)
    first = world.push(shas["c0"], env={SECRET_VAR: PRIVATE})
    assert (first.rc, first.out) == (0, "records=1\nstatus=complete\n"), first.err  # sin anotación
    folder = first.folder()
    record = read_record_dir(folder).record
    assert record.sealed == SIG_FILE and record.signature is not None
    assert (record.signature.key_id, record.signature.key, record.signature.hash) == (
        KEY_ID, project_keys()[0].fingerprint, "sha512")
    assert {SIG_FILE, f"{SIG_FILE}.ots"} <= files(folder) and f"{MANIFEST_FILE}.ots" not in files(folder)
    assert all(p.subject == SIG_FILE for p in record.proofs)
    assert record.manifest is not None and record.signature.data is not None
    assert sshsig.verify(record.signature.data, record.manifest).fingerprint == record.signature.key
    # evidence verify con las llaves del proyecto: del proyecto, y las TSA fechan la firma
    result = ev.verify_folder(folder, catalog=world.services.catalog, keys=project_keys())
    rows = {(c.check, c.source): (c.status, c.reason) for c in result.checks}
    assert rows[("signature", KEY_ID)] == ("pass", "ok")
    assert rows[("tsa", "alpha")][0] == "pass" and rows[("tsa", "beta")][0] == "pass"
    assert (result.verdict, result.confidence, result.origin) == ("verified", "strong", "project")
    t = translator("es")
    assert t("ci.seal.summary.signed", key_id=KEY_ID, key=record.signature.key) in first.summary_text()
    verify_md = (folder / "VERIFY.md").read_text(encoding="utf-8")
    assert f"-n {signing.NAMESPACE} -s {SIG_FILE}" in verify_md and KEY_ID in verify_md
    assert "-O verify-time=" in verify_md and "{{" not in verify_md
    # el push de la integración del PR: su comentario lleva la firma completa (REQ-CI-54)
    merged = world.push(shas["s1"], env={SECRET_VAR: PRIVATE})
    assert (merged.rc, merged.out) == (0, "records=1\nstatus=complete\n"), merged.err
    ((body, comment),) = world.evidence(5)
    signed = read_record_dir(merged.folder()).record.signature
    assert comment.signature is not None and signed is not None and comment.signature.data == signed.data
    assert t("ci.seal.comment.signed", key_id=KEY_ID, key=signed.key) in body
    for result_ in (first, merged):
        assert_no_secret(world, result_)


# -- sin firma: se sella igual y se anota ----------------------------------------------------------------

UNSIGNED_CASES = {
    "no_secret": ({}, {}),
    "blank_secret": ({SECRET_VAR: "  \n"}, {}),
    "other_key": ({SECRET_VAR: OTHER_PRIVATE}, {}),
    "key_not_yet_valid": ({SECRET_VAR: PRIVATE}, {"since": "2026-11-01T00:00:00Z"}),
    "key_revoked": ({SECRET_VAR: PRIVATE}, {"since": "2026-01-01T00:00:00Z", "until": "2026-10-01T00:00:00Z"}),
    "garbage": ({SECRET_VAR: "no es una llave"}, {}),
    "ecdsa_key": ({SECRET_VAR: ec.derive_private_key(7, ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()).decode()}, {}),
}
REASONS = {"no_secret": "no_secret", "blank_secret": "no_secret", "other_key": "key_mismatch",
           "key_not_yet_valid": "key_mismatch", "key_revoked": "key_mismatch", "garbage": "sign_failed",
           "ecdsa_key": "sign_failed"}


@pytest.mark.parametrize("case", sorted(UNSIGNED_CASES))
def test_an_unsigned_integration_is_sealed_and_annotated(world: World, case: str) -> None:
    env, policy = UNSIGNED_CASES[case]
    reason = REASONS[case]
    use_signing(world, "ci", **policy)
    shas = _e1_world(world)
    result = world.push(shas["c0"], env=env)
    lines = result.out.splitlines()
    assert result.rc == 0 and lines[1:] == ["records=1", "status=complete"], (result.out, result.err)
    t = translator("es")
    expected = t("ci.seal.warning.unsigned", reason=reason, text=ci_seal.unsigned_text(t, reason))
    assert lines[0] == TITLE + expected
    record = read_record_dir(result.folder()).record
    assert (record.sealed, record.signature) == (MANIFEST_FILE, None)
    assert f"{MANIFEST_FILE}.ots" in files(result.folder()) and SIG_FILE not in files(result.folder())
    assert t("ci.seal.summary.unsigned", reason=reason, text=ci_seal.unsigned_text(t, reason)) in result.summary_text()
    verify_md = (result.folder() / "VERIFY.md").read_text(encoding="utf-8")
    assert "-Y verify" not in verify_md and "<!--" not in verify_md  # sin los pasos de la firma
    secret = env.get(SECRET_VAR, "")
    assert_no_secret(world, result, secret if "PRIVATE KEY" in secret else PRIVATE)


def test_the_unsigned_texts_name_the_secret_and_the_environment() -> None:
    for lang in ("es", "en"):
        t = translator(lang)  # type: ignore[arg-type]
        for reason in ci_seal.UNSIGNED_REASONS:
            text = ci_seal.unsigned_text(t, reason)
            assert text != reason and "{" not in text, (lang, reason)
        assert SECRET_VAR in ci_seal.unsigned_text(t, "no_secret")
        assert signing.NAMESPACE in ci_seal.unsigned_text(t, "no_secret")
    assert ci_seal.unsigned_text(translator("es"), "otro") == "otro"


class Fixed:
    """Traductor que da siempre el mismo texto, como lo pediría un catálogo con ``%`` y saltos de línea."""

    def __init__(self, text: str) -> None:
        self.text = text

    def __call__(self, _key: str, **_values: object) -> str:
        return self.text

    def get(self, _key: str, **_values: object) -> str:
        return self.text


def test_the_annotation_is_one_line_with_its_data_escaped() -> None:
    """Un comando de flujo de Actions por línea: ningún ``%`` ni salto de línea del texto empieza otro."""
    outcome = SimpleNamespace(signing=True, record=SimpleNamespace(signature=None), kind="integration",
                              unsigned="no_secret")
    line = ci_seal.warning_line(outcome, Fixed("100% listo\r\n::error::falso"))  # type: ignore[arg-type]
    assert line == f"{TITLE}100%25 listo%0D%0A::error::falso\n"
    assert line.count("\n") == 1
    signed = SimpleNamespace(signing=True, record=SimpleNamespace(signature=object()), kind="integration",
                             unsigned=None)
    assert ci_seal.warning_line(signed, translator("es")) is None  # type: ignore[arg-type]
    not_tried = SimpleNamespace(signing=False, record=SimpleNamespace(signature=None), kind="integration",
                                unsigned=None)
    assert ci_seal.warning_line(not_tried, translator("es")) is None  # type: ignore[arg-type]


# -- quién nunca firma ------------------------------------------------------------------------------------


def test_a_submission_never_signs(world: World) -> None:
    use_signing(world, "ci")
    shas = _e1_world(world)
    got = world.seal("submission", shas["h2"], pr=5, base_sha=shas["c0"], env={SECRET_VAR: PRIVATE})
    assert (got.rc, got.out) == (0, "records=1\nstatus=complete\n"), got.err
    record = read_record_dir(got.folder()).record
    assert record.signature is None and SIG_FILE not in files(got.folder())
    assert_no_secret(world, got)


@pytest.mark.parametrize("mode", ["admin", "none", None])
def test_admin_and_none_never_sign_in_ci(world: World, mode: str | None) -> None:
    """Con ``admin`` firma el admin el índice del hito; con ``none``, nadie: el CI no anota nada."""
    if mode is not None:
        use_signing(world, mode)
    shas = _e1_world(world)
    got = world.push(shas["c0"], env={SECRET_VAR: PRIVATE})
    assert (got.rc, got.out) == (0, "records=1\nstatus=complete\n"), got.err
    assert read_record_dir(got.folder()).record.signature is None
    t = translator("es")
    summary = got.summary_text()
    for reason in ci_seal.UNSIGNED_REASONS:
        assert t("ci.seal.summary.unsigned", reason=reason, text=ci_seal.unsigned_text(t, reason)) not in summary
    assert_no_secret(world, got)


# -- sign_manifest -----------------------------------------------------------------------------------------


def test_sign_manifest_reasons() -> None:
    data = b'{"v":1}'
    keys = project_keys()
    now = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
    signature, reason = ci_seal.sign_manifest(data, keys, {SECRET_VAR: PRIVATE}, now)
    assert reason is None and signature is not None and signature.data is not None
    assert (signature.file, signature.key_id, signature.key) == (SIG_FILE, KEY_ID, keys[0].fingerprint)
    assert sshsig.verify(signature.data, data).fingerprint == keys[0].fingerprint
    assert ci_seal.sign_manifest(data, keys, {}, now) == (None, "no_secret")
    assert ci_seal.sign_manifest(data, keys, {SECRET_VAR: OTHER_PRIVATE}, now) == (None, "key_mismatch")
    assert ci_seal.sign_manifest(data, (), {SECRET_VAR: PRIVATE}, now) == (None, "key_mismatch")
    early = datetime(2026, 1, 31, tzinfo=timezone.utc)
    assert ci_seal.sign_manifest(data, keys, {SECRET_VAR: PRIVATE}, early) == (None, "key_mismatch")
    assert ci_seal.sign_manifest(data, keys, {SECRET_VAR: "x"}, now) == (None, "sign_failed")
    assert NOW == now  # el reloj del mundo de test_ci_seal
