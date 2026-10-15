# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Verificación de registros de evidencia (``evidence.verify``; 05-operaciones §9.26; 12-mvp A17 b).

Los registros salen del generador ``tests/fixtures/evidence/corpus/corpus.py``: un
repositorio fijo, la TSA de prueba (``fake_tsa``) y las pruebas OTS de ``make_ots``. El
corpus de alteraciones se detecta al 100 %: cada caso da el check que lo delata y un
resultado distinto del registro sano, también los del registro firmado por el proyecto y
los del paquete con su índice firmado (00-marco D-72), que se verifican con su lista de
llaves. Sin red: el calendario es un ``MockTransport`` y git
corre sin red. Las carpetas de ``--content`` hostiles traen su propio ``.git``, escrito a
mano (las pruebas nunca ejecutan ``git config``).
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import shutil
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

pytest.importorskip("rfc3161_client", reason="falta el extra evidence (el CI lo instala)")
pytest.importorskip("opentimestamps", reason="falta el extra evidence (el CI lo instala)")

from aucove.domain.signing import EvidenceKey  # noqa: E402
from aucove.evidence import manifest, ots, record  # noqa: E402
from aucove.evidence import package as pkg  # noqa: E402
from aucove.evidence import openings as op  # noqa: E402
from aucove.evidence import verify as ev  # noqa: E402
from aucove.evidence.catalog import TOOL_MISSING  # noqa: E402
from aucove.ops.evidence_verify import _schema_pointers  # noqa: E402
from aucove.vcs.git import Git  # noqa: E402
from tests.conftest import GitRepo  # noqa: E402
from tests.fixtures.evidence.corpus import corpus  # noqa: E402
from tests.fixtures.evidence.tsa.fake_tsa import Router  # noqa: E402
from tests.unit.evidence.support import needs_git, new_repo  # noqa: E402

pytestmark = needs_git

# Filtro clean de git, por variables de entorno (lo mismo que `git -c`): la forma limpia
# aislada de --content nunca lo usa.
STRIP = "sed -e /^#strip/d"
FILTER = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "filter.strip.clean", "GIT_CONFIG_VALUE_0": STRIP}


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[corpus.Healthy, list[corpus.Built]]:
    return corpus.build(tmp_path_factory.mktemp("corpus"))


@pytest.fixture(scope="module")
def healthy(built: tuple[corpus.Healthy, list[corpus.Built]]) -> corpus.Healthy:
    return built[0]


def _git(path: Path, healthy: corpus.Healthy, **extra: str) -> Git:
    return Git(path, env={**healthy.repo.env, **extra}, offline=True)


def verify(
    folder: Path,
    healthy: corpus.Healthy,
    *,
    repo: bool = False,
    content: Path | None = None,
    upgrade: ev.Upgrade | None = None,
    keys: Sequence[EvidenceKey] | None = None,
) -> ev.RecordResult:
    repo_git = _git(healthy.repo.path, healthy) if repo else None
    with contextlib.ExitStack() as stack:
        source = None
        if content is not None:
            source = ev.Content(content, stack.enter_context(ev.CleanForm(content, env=healthy.repo.env)))
        return ev.verify_folder(
            folder, catalog=healthy.catalog, repo=repo_git, content=source, upgrade=upgrade, validate=_schema_pointers,
            keys=keys,
        )


def table(result: ev.RecordResult) -> list[tuple[str, str | None, str, str]]:
    return [(c.check, c.source, c.status, c.reason) for c in result.checks]


def _copy(folder: Path, tmp_path: Path) -> Path:
    target = tmp_path / folder.name
    shutil.copytree(folder, target)
    return target


def _forged_bitcoin(digest: bytes, height: int = 500_000) -> bytes:
    """Prueba OTS con una atestación de Bitcoin inventada: cualquiera puede escribirla."""
    from opentimestamps.core.notary import BitcoinBlockHeaderAttestation
    from opentimestamps.core.op import OpSHA256
    from opentimestamps.core.serialize import BytesSerializationContext
    from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

    detached = DetachedTimestampFile(OpSHA256(), Timestamp(digest))
    detached.timestamp.ops.add(OpSHA256()).attestations.add(BitcoinBlockHeaderAttestation(height))
    ctx = BytesSerializationContext()
    detached.serialize(ctx)
    return bytes(ctx.getbytes())


def _forge_author(folder: Path, *, base: str | None) -> None:
    """r5: el manifiesto con otro ``author`` (y su apertura, que lo reproduce) y la base dada,
    puestos al día en ``record.json`` y ``openings.json``. Los sellos no cambian."""
    salt = bytes(range(32))
    fake = "Atacante <atacante@example.invalid>"

    def change(obj: dict[str, object]) -> None:
        actors = obj["actors"]
        assert isinstance(actors, list) and any(a["r"] == "author" for a in actors)
        obj["actors"] = [
            {"r": "author", "a": op.actor_commitment(salt, "author", fake)} if a["r"] == "author" else a
            for a in actors
        ]
        obj["base"] = base

    corpus._reseal_obj(folder, change)

    def opening(obj: dict[str, list[dict[str, str]]]) -> None:
        for actor in obj["actors"]:
            if actor["r"] == "author":
                actor.update(value=fake, salt=salt.hex())

    corpus._edit_json(folder / record.OPENINGS_FILE, opening)


# -- registros sanos ------------------------------------------------------------------------------


def test_healthy_integration_is_verified_with_strong_confidence(healthy: corpus.Healthy) -> None:
    got = verify(healthy.integration, healthy)
    assert (got.verdict, got.confidence, got.kind, got.commit) == ("verified", "strong", "integration", healthy.sha["c2"])
    assert table(got) == [
        ("manifest", None, "pass", "ok"),
        ("signature", None, "skip", "unsigned"),  # sin firma del proyecto (D-72)
        ("tsa", "alpha", "pass", "ok"),
        ("tsa", "beta", "pass", "ok"),
        ("ots", "calendars", "pending", "pending"),
        ("openings", None, "pass", "ok"),
        ("actors", None, "pass", "ok"),
    ]
    tsa_checks = [c for c in got.checks if c.check == "tsa"]
    assert [c.params["revocation"] for c in tsa_checks] == ["OCSP", "CRL"]
    assert all(c.time_utc is not None and c.time_utc.isoformat() == "2026-10-06T12:00:00+00:00" for c in tsa_checks)
    assert got.warnings == () and got.upgraded is False


def test_healthy_submission_is_pending_without_network(healthy: corpus.Healthy) -> None:
    got = verify(healthy.submission, healthy)
    assert (got.verdict, got.confidence) == ("pending", None)
    assert table(got) == [
        ("manifest", None, "pass", "ok"),
        ("signature", None, "skip", "unsigned"),  # sin firma del proyecto (D-72)
        ("ots", "calendars", "pending", "pending"),
        ("openings", None, "pass", "ok"),
        ("actors", None, "pass", "ok"),
    ]


def test_a_complete_ots_stays_pending_because_its_block_is_not_checked(healthy: corpus.Healthy) -> None:
    """D1: v0.1 no coteja el bloque de Bitcoin; solo una TSA lleva a verified (05 §9.26)."""
    got = verify(healthy.submission_complete, healthy)
    assert (got.verdict, got.confidence) == ("pending", None)
    [check] = [c for c in got.checks if c.check == "ots"]
    assert (check.source, check.status, check.reason) == ("bitcoin", "pending", "bitcoin_unverified")
    assert check.params["height"] == 900_000 and len(str(check.params["merkle_root"])) == 64  # para cotejarlo a mano


def test_a_forged_bitcoin_attestation_never_verifies(healthy: corpus.Healthy, tmp_path: Path) -> None:
    folder = _copy(healthy.submission, tmp_path)
    digest = hashlib.sha256((folder / record.MANIFEST_FILE).read_bytes()).digest()
    (folder / record.OTS_FILE).write_bytes(_forged_bitcoin(digest))
    got = verify(folder, healthy)
    assert (got.verdict, got.confidence) == ("pending", None)
    assert ("ots", "bitcoin", "pending", "bitcoin_unverified") in table(got)


def test_repo_and_content_on_healthy_records(healthy: corpus.Healthy) -> None:
    got = verify(healthy.integration, healthy, repo=True, content=healthy.content)
    assert got.verdict == "verified"
    assert ("git", None, "pass", "ok") in table(got)
    content = [(c.status, c.reason, c.params["path"]) for c in got.checks if c.check == "content"]
    assert sorted(content) == [("pass", "raw", "sections/intro/intro.md"), ("pass", "raw", "sections/methods/methods.md")]
    assert ("git", None, "pass", "ok") in table(verify(healthy.submission, healthy, repo=True))


def test_local_copy_without_chain_and_root_uses_the_catalog(healthy: corpus.Healthy, tmp_path: Path) -> None:
    """Una copia hecha desde un comentario no trae ``chain.pem`` ni ``root.pem``: no es fatal."""
    folder = _copy(healthy.integration, tmp_path)
    for name in ("alpha", "beta"):
        (folder / "tsa" / f"{name}.chain.pem").unlink()
        (folder / "tsa" / f"{name}.root.pem").unlink()
    got = verify(folder, healthy)
    assert (got.verdict, got.confidence) == ("verified", "strong")


def test_a_truncated_comment_copy_without_manifest_is_pending(healthy: corpus.Healthy, tmp_path: Path) -> None:
    folder = _copy(healthy.integration, tmp_path)
    (folder / record.MANIFEST_FILE).unlink()
    (folder / record.OPENINGS_FILE).unlink()
    got = verify(folder, healthy)
    assert (got.verdict, got.confidence) == ("pending", None)
    rows = table(got)
    assert ("manifest", None, "skip", "missing") in rows
    assert ("tsa", "alpha", "pass", "ok") in rows  # las pruebas se cotejan con la huella de record.json
    assert ("openings", None, "skip", "missing") in rows and ("actors", None, "skip", "missing") in rows


def test_a_stamp_without_declared_time_fails_even_without_the_schema(
    built: tuple[corpus.Healthy, list[corpus.Built]]
) -> None:
    """C-5: una prueba ``rfc3161`` obtenida con ``time_utc`` nulo. El esquema ya lo rechaza
    (``record_schema``); sin el validador, el check ``tsa`` también."""
    healthy, cases = built
    item = next(b for b in cases if b.case.id == "record-time-null")
    got = ev.verify_folder(item.folder, catalog=healthy.catalog)
    assert ("manifest", None, "pass", "ok") in table(got)
    assert ("tsa", "alpha", "fail", "time_mismatch") in table(got) and got.verdict == "failed"
    assert ("manifest", None, "fail", "record_schema") in table(verify(item.folder, healthy))


# -- corpus de alteraciones (A17 b) ----------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """Resultado de un caso del corpus y el del registro sano con las mismas opciones."""

    case: corpus.Case
    got: ev.RecordResult
    sane: ev.RecordResult

    @property
    def detected(self) -> bool:
        """Lo delatan sus checks esperados y el resultado no es el del registro sano."""
        rows = table(self.got)
        expected = all((e.check, e.source, e.status, e.reason) in rows for e in self.case.expect)
        shape = [(c.check, c.source, c.status) for c in self.got.checks]
        return expected and shape != [(c.check, c.source, c.status) for c in self.sane.checks]


@pytest.fixture(scope="module")
def outcomes(built: tuple[corpus.Healthy, list[corpus.Built]]) -> dict[str, Outcome]:
    """Cada caso del corpus se verifica una vez por módulo (git es lento en Windows), con la
    lista de llaves del proyecto (``--keys``)."""
    healthy, cases = built
    sane: dict[tuple[str, bool, bool], ev.RecordResult] = {}
    out: dict[str, Outcome] = {}
    for item in cases:
        key = (item.case.record, item.case.repo, item.content is not None)
        if key not in sane:
            content = healthy.content if item.content is not None else None
            sane[key] = verify(healthy.folder(item.case.record), healthy, repo=item.case.repo, content=content,
                               keys=healthy.keys)
        got = verify(item.folder, healthy, repo=item.case.repo, content=item.content, keys=healthy.keys)
        out[item.case.id] = Outcome(item.case, got, sane[key])
    return out


@pytest.mark.parametrize("case_id", [c.id for c in corpus.CASES])
def test_every_alteration_is_detected(case_id: str, outcomes: dict[str, Outcome]) -> None:
    outcome = outcomes[case_id]
    rows = table(outcome.got)
    for expect in outcome.case.expect:
        assert (expect.check, expect.source, expect.status, expect.reason) in rows, (case_id, rows)
    assert outcome.detected
    if any(e.status == "fail" for e in outcome.case.expect):
        assert (outcome.got.verdict, outcome.got.confidence) == ("failed", None)


def test_the_corpus_is_detected_at_100_percent(
    outcomes: dict[str, Outcome], package_outcomes: dict[str, PackageOutcome]
) -> None:
    detected = [o.detected for o in outcomes.values()] + [o.detected for o in package_outcomes.values()]
    assert len(detected) >= 25
    assert sum(detected) / len(detected) == 1.0
    # cubre lo que pide A17 b: byte del manifiesto, .tsr, cadena, OCSP, apertura, actor y blob;
    # byte de la firma, llave equivocada, llave que ya no valía, firma sin sellar y manifiesto
    # cambiado después de firmar; y los casos hostiles: filtro de la carpeta, base ausente u
    # otra, hora nula
    for wanted in ("manifest-byte", "tsr-signature", "chain-self-signed", "ocsp-other-cert", "opening-salt",
                   "actor-value", "opening-blob", "content-modified", "content-filter", "manifest-base-unknown",
                   "manifest-base-not-ancestor", "record-time-null", "signature-byte", "signature-other-key",
                   "signature-key-expired", "signature-unsealed", "signature-manifest-changed"):
        assert wanted in outcomes, wanted
    # e índice alterado
    for wanted in ("index-altered", "index-file-changed"):
        assert wanted in package_outcomes, wanted


# -- firma del proyecto (00-marco D-72; 11-trazabilidad §8.10) ---------------------------------------


def test_a_signed_record_is_from_the_project_with_its_keys(healthy: corpus.Healthy) -> None:
    """Las pruebas sellan la firma; con la lista de llaves, ``origin: project``; sin ella, la
    firma queda sin cotejar (``unknown``). La llave vale en la fecha de la TSA más antigua."""
    result = verify(healthy.signed, healthy, keys=healthy.keys)
    assert (result.verdict, result.confidence, result.origin) == ("verified", "strong", "project")
    rows = table(result)
    assert ("signature", corpus.KEY_ID, "pass", "ok") in rows
    assert [r[:3] for r in rows if r[0] in ("tsa", "ots")] == [
        ("tsa", "alpha", "pass"), ("tsa", "beta", "pass"), ("ots", "calendars", "pending")]
    (signature,) = [c for c in result.checks if c.check == "signature"]
    stamps = [c.time_utc for c in result.checks if c.check == "tsa"]
    assert signature.time_utc is not None and signature.time_utc == min(t for t in stamps if t is not None)
    unchecked = verify(healthy.signed, healthy)
    assert (unchecked.verdict, unchecked.confidence, unchecked.origin) == ("verified", "strong", "unknown")
    assert ("signature", corpus.KEY_ID, "skip", "no_keys") in table(unchecked)
    # un registro sin firma es unsigned, con llaves o sin ellas
    for keys in (None, healthy.keys):
        plain = verify(healthy.integration, healthy, keys=keys)
        assert plain.origin == "unsigned" and ("signature", None, "skip", "unsigned") in table(plain)


def test_a_signature_without_its_file_is_missing_and_its_proofs_lose_their_subject(
    healthy: corpus.Healthy, tmp_path: Path
) -> None:
    """Sin ``manifest.json.sig``, la firma falta y las pruebas no tienen qué sellaron: nada se verifica."""
    folder = _copy(healthy.signed, tmp_path)
    (folder / record.SIG_FILE).unlink()
    result = verify(folder, healthy, keys=healthy.keys)
    rows = table(result)
    assert ("signature", corpus.KEY_ID, "skip", "missing") in rows
    assert result.verdict != "verified" and result.origin != "project", rows


def verify_package(
    package: Path, healthy: corpus.Healthy, *, keys: Sequence[EvidenceKey] | None = None
) -> tuple[list[ev.RecordResult], list[ev.SignatureResult]]:
    """Como ``evidence verify <paquete>``: cada registro y cada ``signature*/``, con la cobertura del índice."""
    folders = sorted(p for p in package.iterdir() if p.is_dir() and pkg.SIGNATURE_DIR_RE.fullmatch(p.name) is None)
    records = [ev.verify_folder(f, catalog=healthy.catalog, validate=_schema_pointers, keys=keys) for f in folders]
    signatures = [
        ev.verify_signature_folder(d, package, catalog=healthy.catalog, validate=_schema_pointers, keys=keys)
        for d in pkg.signature_dirs(package)
    ]
    return ev.apply_coverage(records, signatures, package), signatures


def sig_table(result: ev.SignatureResult) -> list[tuple[str, str | None, str, str]]:
    return [(c.check, c.source, c.status, c.reason) for c in result.checks]


def test_a_signed_package_covers_its_records(healthy: corpus.Healthy) -> None:
    """El índice firmado y sellado (``evidence sign``) da ``origin: project`` a los registros que
    lista con su huella; sin la lista de llaves, ``unknown``. Fuera del paquete, nada cambia."""
    records, (folder,) = verify_package(healthy.package, healthy, keys=healthy.keys)
    assert (folder.folder.name, folder.key_id, folder.verdict, folder.confidence, folder.origin) == (
        "signature", corpus.KEY_ID, "verified", "strong", "project")
    assert [r[:3] for r in sig_table(folder)] == [
        ("signature", corpus.KEY_ID, "pass"), ("tsa", "alpha", "pass"), ("tsa", "beta", "pass"),
        ("ots", "calendars", "pending"), ("index", None, "pass")]
    assert folder.index_sha256 == hashlib.sha256((healthy.package / pkg.INDEX_FILE).read_bytes()).hexdigest()
    assert sorted((r.kind, r.verdict, r.origin) for r in records) == [
        ("integration", "verified", "project"), ("submission", "pending", "project")]
    assert ev.overall(records, [folder]) == ("pending", None)  # la entrega, sin TSA
    records, (folder,) = verify_package(healthy.package, healthy)
    assert folder.origin == "unknown" and ("signature", corpus.KEY_ID, "skip", "no_keys") in sig_table(folder)
    assert {r.origin for r in records} == {"unknown"}


def test_a_record_the_index_does_not_list_keeps_its_origin(healthy: corpus.Healthy, tmp_path: Path) -> None:
    """Un registro agregado al paquete después de firmar el índice no queda cubierto."""
    package = tmp_path / healthy.package.name
    shutil.copytree(healthy.package, package)
    shutil.copytree(healthy.submission_complete, package / healthy.submission_complete.name)
    records, (folder,) = verify_package(package, healthy, keys=healthy.keys)
    origins = {r.folder.name: r.origin for r in records}
    assert origins[healthy.submission_complete.name] == "unsigned"
    assert origins[healthy.integration.name] == "project" and folder.verdict == "verified"


@dataclass(frozen=True)
class PackageOutcome:
    """Resultado de la carpeta de firma de un paquete alterado y el de la del paquete sano."""

    case: corpus.PackageCase
    got: ev.SignatureResult
    sane: ev.SignatureResult
    records: tuple[ev.RecordResult, ...]  # los registros del paquete alterado, con la cobertura

    @property
    def detected(self) -> bool:
        rows = sig_table(self.got)
        expected = all((e.check, e.source, e.status, e.reason) in rows for e in self.case.expect)
        return expected and [r[:3] for r in rows] != [r[:3] for r in sig_table(self.sane)]


@pytest.fixture(scope="module")
def package_outcomes(
    healthy: corpus.Healthy, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, PackageOutcome]:
    packages = corpus.build_packages(healthy, tmp_path_factory.mktemp("packages"))
    _records, (sane,) = verify_package(healthy.package, healthy, keys=healthy.keys)
    out: dict[str, PackageOutcome] = {}
    for case in corpus.PACKAGE_CASES:
        records, (got,) = verify_package(packages[case.id], healthy, keys=healthy.keys)
        out[case.id] = PackageOutcome(case, got, sane, tuple(records))
    return out


@pytest.mark.parametrize("case_id", [c.id for c in corpus.PACKAGE_CASES])
def test_every_package_alteration_is_detected(case_id: str, package_outcomes: dict[str, PackageOutcome]) -> None:
    outcome = package_outcomes[case_id]
    rows = sig_table(outcome.got)
    for expect in outcome.case.expect:
        assert (expect.check, expect.source, expect.status, expect.reason) in rows, (case_id, rows)
    assert outcome.detected
    if any(e.status == "fail" for e in outcome.case.expect):
        assert (outcome.got.verdict, outcome.got.confidence) == ("failed", None)
    if outcome.got.verdict == "failed":  # una carpeta de firma que falla no cubre a nadie
        assert {r.origin for r in outcome.records} == {"unsigned"}


# -- sin el extra (REQ-ARCH-99) ----------------------------------------------------------------------


def test_without_the_extra_tsa_and_ots_skip_and_the_rest_is_verified(
    healthy: corpus.Healthy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "rfc3161_client", None)
    monkeypatch.setitem(sys.modules, "opentimestamps", None)
    got = verify(healthy.integration, healthy)
    assert (got.verdict, got.confidence) == ("pending", None)
    assert table(got) == [
        ("manifest", None, "pass", "ok"),
        ("signature", None, "skip", "unsigned"),  # sin firma del proyecto (D-72)
        ("tsa", "alpha", "skip", "missing_extra"),
        ("tsa", "beta", "skip", "missing_extra"),
        ("ots", "calendars", "skip", "missing_extra"),
        ("openings", None, "pass", "ok"),
        ("actors", None, "pass", "ok"),
    ]
    assert got.warnings == (TOOL_MISSING,)


def test_missing_extra_uses_find_spec_without_importing(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ev.missing_extra() == ()
    monkeypatch.setitem(sys.modules, "opentimestamps", None)
    assert ev.missing_extra() == ("opentimestamps",)
    assert set(ev.EXTRA_MODULES) >= {"rfc3161_client", "opentimestamps", "cryptography"}


# -- --repo -----------------------------------------------------------------------------------------


def test_repo_without_the_commit_skips_with_the_fetch_hint(healthy: corpus.Healthy, tmp_path: Path) -> None:
    empty = new_repo(tmp_path, "vacio")
    got = ev.verify_folder(healthy.submission, catalog=healthy.catalog, repo=_git(empty.path, healthy))
    [check] = [c for c in got.checks if c.check == "git"]
    assert (check.status, check.reason) == ("skip", "missing_commit")
    assert got.verdict == "pending"  # nunca fail por falta del commit


def test_a_closed_pr_is_verified_with_a_clone_that_fetches_the_pr_heads(
    healthy: corpus.Healthy, tmp_path: Path
) -> None:
    """A17 b: la cabeza de un PR cerrado sin integrar, con su rama borrada, solo vive en
    ``refs/pull/<n>/head``: un clon normal no la trae (``skip`` con la pista) y uno que trae
    esas refs la verifica."""
    origin = tmp_path / "origin.git"
    healthy.repo.git("init", "-q", "--bare", str(origin))
    healthy.repo.git("push", "-q", str(origin), "main", f"{healthy.sha['p1']}:refs/pull/8/head")
    clone_path = tmp_path / "clon"
    healthy.repo.git("clone", "-q", "--no-local", str(origin), str(clone_path))
    clone = GitRepo(clone_path, origin, healthy.repo.env)

    def git_check() -> tuple[str, str]:
        got = ev.verify_folder(healthy.submission, catalog=healthy.catalog, repo=_git(clone_path, healthy))
        [check] = [c for c in got.checks if c.check == "git"]
        return check.status, check.reason

    assert git_check() == ("skip", "missing_commit")
    clone.git("fetch", "-q", "origin", "+refs/pull/*/head:refs/pull/*/head")
    assert git_check() == ("pass", "ok")


def test_repo_with_another_blob_fails(
    built: tuple[corpus.Healthy, list[corpus.Built]], outcomes: dict[str, Outcome]
) -> None:
    healthy, cases = built
    item = next(b for b in cases if b.case.id == "opening-blob")
    without_repo = verify(item.folder, healthy)
    assert ("openings", None, "pass", "ok") in table(without_repo)  # sin --repo no se ve
    got = outcomes["opening-blob"].got
    assert ("git", None, "fail", "blob_mismatch") in table(got) and got.verdict == "failed"


def test_a_missing_base_does_not_turn_off_the_git_check(healthy: corpus.Healthy, tmp_path: Path) -> None:
    """Un registro de solo OTS con otro autor, una base que no existe y una
    atestación de Bitcoin inventada. Antes, la base ausente hacía salir el check ``git`` sin
    cotejar los actores, y la atestación contaba como ``weak``: el registro salía verificado."""
    folder = _copy(healthy.submission, tmp_path)
    _forge_author(folder, base="f" * 40)
    digest = hashlib.sha256((folder / record.MANIFEST_FILE).read_bytes()).digest()
    (folder / record.OTS_FILE).write_bytes(_forged_bitcoin(digest))
    got = verify(folder, healthy, repo=True)
    rows = table(got)
    assert ("manifest", None, "pass", "ok") in rows and ("actors", None, "pass", "ok") in rows  # coherente
    assert ("ots", "bitcoin", "pending", "bitcoin_unverified") in rows
    assert ("git", None, "fail", "actor_mismatch") in rows and got.verdict == "failed"


def test_a_shallow_clone_without_the_base_checks_all_but_commits_and_plan(
    healthy: corpus.Healthy, tmp_path: Path
) -> None:
    """En un clon superficial la base puede faltar; se cotejan igual el árbol, los
    blobs y los actores, y el check queda en ``skip`` (no se cotejaron commits ni plan)."""
    origin = tmp_path / "origin.git"
    healthy.repo.git("init", "-q", "--bare", str(origin))
    healthy.repo.git("push", "-q", str(origin), "main")
    shallow = tmp_path / "superficial"
    healthy.repo.git("clone", "-q", "--depth", "1", "--branch", "main", origin.as_uri(), str(shallow))
    repo = _git(shallow, healthy)
    assert repo.run("rev-parse", "--is-shallow-repository").stdout.strip() == b"true"

    def git_row(folder: Path) -> tuple[str, str, dict[str, object]]:
        got = ev.verify_folder(folder, catalog=healthy.catalog, repo=repo)
        [check] = [c for c in got.checks if c.check == "git"]
        return check.status, check.reason, dict(check.params)

    status, reason, params = git_row(healthy.integration)  # la base (c1) quedó fuera del clon
    assert (status, reason) == ("skip", "missing_base") and params["blobs"] == 2 and params["actors"] == 2
    forged = _copy(healthy.integration, tmp_path / "falso")
    _forge_author(forged, base=healthy.sha["c1"])
    assert git_row(forged)[:2] == ("fail", "actor_mismatch")


# -- --content: bytes crudos y forma limpia ------------------------------------------------------------


@pytest.fixture(scope="module")
def attributed(tmp_path_factory: pytest.TempPathFactory) -> tuple[GitRepo, Path]:
    """Repositorio con atributos y el registro de su primer commit: ``*.u16`` va en UTF-16 en
    el árbol de trabajo y en UTF-8 en el blob (``working-tree-encoding``, una conversión
    nativa), y ``*.nb`` pasó por un filtro ``clean`` que solo se definió al hacer el commit."""
    tmp = tmp_path_factory.mktemp("atributos")
    repo = new_repo(tmp, "nb")
    repo.write({
        ".gitattributes": "*.nb filter=strip\n*.u16 text working-tree-encoding=UTF-16LE eol=lf\n",
        "d/x.nb": "keep\n#strip esto\nkeep2\n",
        "d/y.txt": "a\nb\n",
        "d/z.u16": "hola\n".encode("utf-16-le"),
    })
    repo.git("-c", f"filter.strip.clean={STRIP}", "add", "-A")
    repo.git("commit", "-q", "--no-verify", "-m", "c")
    head = repo.head()
    built = manifest.build_integration(
        Git(repo.path, env=repo.env), commit=head, base=None, repository=corpus.REPOSITORY,
        repository_id=corpus.REPOSITORY_ID, created_at=corpus.CREATED, salt=corpus.salts("nb"),
    )
    rec = record.Record(
        kind="integration", commit=head, pr=None, task=None, plan=built.plan, manifest_sha256=built.sha256,
        run_id=9, run_attempt=1, proofs=(record.Proof.ots(corpus.ots_proofs(bytes.fromhex(built.sha256))[0]),),
        manifest=built.data, openings=built.openings,
    )
    assert repo.git("show", "HEAD:d/x.nb") == "keep\nkeep2"  # el blob es la forma filtrada
    assert repo.git("show", "HEAD:d/z.u16") == "hola"  # y este, la forma en UTF-8
    return repo, record.write_record_dir(tmp / "records", rec)


def _content_rows(result: ev.RecordResult) -> dict[str, tuple[str, str]]:
    return {str(c.params.get("path")): (c.status, c.reason) for c in result.checks if c.check == "content"}


def _with_content(folder: Path, content: Path, env: dict[str, str] | None) -> ev.RecordResult:
    """``--content`` con la forma limpia de ``CleanForm`` (con ``env``) o, con ``None``, sin git."""
    if env is None:
        return ev.verify_folder(folder, content=ev.Content(content, None))
    with ev.CleanForm(content, env=env) as clean:
        return ev.verify_folder(folder, content=ev.Content(content, clean))


def test_content_matches_by_the_native_clean_form_only(attributed: tuple[GitRepo, Path]) -> None:
    """Las conversiones nativas de los atributos de la carpeta valen; un filtro no se ejecuta,
    aunque el entorno de quien verifica lo defina (``GIT_CONFIG_*`` no llega a git)."""
    repo, folder = attributed
    got = _content_rows(_with_content(folder, repo.path, {**repo.env, **FILTER}))
    assert got == {
        ".gitattributes": ("pass", "raw"),
        "d/x.nb": ("fail", "mismatch"),
        "d/y.txt": ("pass", "raw"),
        "d/z.u16": ("pass", "clean"),
    }


def test_without_git_there_is_no_clean_form(attributed: tuple[GitRepo, Path]) -> None:
    repo, folder = attributed
    assert _content_rows(_with_content(folder, repo.path, None))["d/z.u16"] == ("skip", "clean_unavailable")
    no_git = {k: v for k, v in repo.env.items() if k.upper() != "PATH"} | {"PATH": ""}
    assert _content_rows(_with_content(folder, repo.path, no_git))["d/z.u16"] == ("skip", "clean_unavailable")


@dataclass
class _NoBytes:
    """Forma limpia que da el id pero no los bytes (``cat-file`` falló en el repositorio temporal)."""

    oid: str

    def clean(self, rel: str, path: Path) -> tuple[str, bytes | None]:
        return self.oid, None


def test_without_the_clean_bytes_only_the_opening_blob_id_is_checked(attributed: tuple[GitRepo, Path]) -> None:
    repo, folder = attributed
    same = ev.Content(repo.path, _NoBytes(op.blob_oid(b"hola\n")))  # type: ignore[arg-type]
    assert _content_rows(ev.verify_folder(folder, content=same))["d/z.u16"] == ("weak", "clean_by_oid")
    other = ev.Content(repo.path, _NoBytes("1" * 40))  # type: ignore[arg-type]
    assert _content_rows(ev.verify_folder(folder, content=other))["d/z.u16"] == ("fail", "mismatch")


def test_content_with_crlf_matches_by_the_clean_form(attributed: tuple[GitRepo, Path]) -> None:
    """El caso de Windows: checkout con CRLF y blob con LF (``core.autocrlf=true``, que la forma
    limpia fija sin depender de la configuración de nadie)."""
    repo, folder = attributed
    target = repo.path / "d" / "y.txt"
    original = target.read_bytes()
    target.write_bytes(b"a\r\nb\r\n")
    try:
        assert _content_rows(_with_content(folder, repo.path, dict(repo.env)))["d/y.txt"] == ("pass", "clean")
    finally:
        target.write_bytes(original)


def test_the_content_folder_never_runs_its_filters_nor_lends_its_objects(tmp_path: Path) -> None:
    """La carpeta de ``--content`` trae un ``.git/config`` con un filtro ``clean``
    que ejecuta un comando y devuelve lo sellado, sus ``.gitattributes`` y el blob sellado en
    su almacén. La forma limpia se calcula aislada: el comando no corre y el archivo distinto
    no pasa por el sellado."""
    sealed, presented = b"version sellada en T\n", b"version escrita despues de T\n"
    repo = new_repo(tmp_path, "sellado")
    repo.write({"doc.txt": sealed})
    repo.git("add", "-A")
    repo.git("commit", "-q", "--no-verify", "-m", "c1")
    built = manifest.build_integration(
        Git(repo.path, env=repo.env), commit=repo.head(), base=None, repository=corpus.REPOSITORY,
        repository_id=corpus.REPOSITORY_ID, created_at=corpus.CREATED, salt=corpus.salts("seg1"),
    )
    rec = record.Record(
        kind="integration", commit=repo.head(), pr=None, task=None, plan=built.plan, manifest_sha256=built.sha256,
        run_id=1, run_attempt=1, proofs=(record.Proof.ots(corpus.ots_proofs(bytes.fromhex(built.sha256))[0]),),
        manifest=built.data, openings=built.openings,
    )
    folder = record.write_record_dir(tmp_path / "records", rec)

    claimant = new_repo(tmp_path, "reclamante")
    (claimant.path / ".sealed").write_bytes(sealed)
    claimant.git("hash-object", "-w", "--no-filters", ".sealed")  # el blob sellado, en su almacén
    marker = tmp_path / "EJECUTADO"
    with (claimant.path / ".git" / "config").open("a", encoding="utf-8", newline="\n") as fh:
        fh.write('[filter "x"]\n\tclean = touch \\"%s\\" && cat .sealed\n' % marker.as_posix())
    (claimant.path / ".gitattributes").write_bytes(b"doc.txt filter=x\n")
    (claimant.path / "doc.txt").write_bytes(presented)
    # con su propia configuración, git sí ejecuta el filtro y da el blob sellado
    assert claimant.git("hash-object", "--path=doc.txt", "doc.txt") == op.blob_oid(sealed) and marker.exists()
    marker.unlink()

    got = _with_content(folder, claimant.path, dict(claimant.env))
    assert _content_rows(got)["doc.txt"] == ("fail", "mismatch")
    assert not marker.exists() and got.verdict == "failed"


def test_the_clean_form_repository_is_temporary(
    attributed: tuple[GitRepo, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El repositorio vacío se crea solo cuando hace falta, fuera de la carpeta, y se borra."""
    repo, _folder = attributed
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    with ev.CleanForm(repo.path, env=dict(repo.env)) as clean:
        assert list(scratch.iterdir()) == []  # todavía no hizo falta
        got = clean.clean("d/z.u16", repo.path / "d" / "z.u16")
        assert got == (op.blob_oid(b"hola\n"), b"hola\n")
        assert len(list(scratch.iterdir())) == 1
    assert list(scratch.iterdir()) == []


def test_content_never_reads_outside_its_folder(healthy: corpus.Healthy, tmp_path: Path) -> None:
    folder = _copy(healthy.integration, tmp_path / "rec")
    corpus._edit_json(folder / record.OPENINGS_FILE, lambda o: o["entries"][0].update(path="../fuera.txt", blob="1" * 40, size=1))
    content = tmp_path / "content"
    shutil.copytree(healthy.content, content)
    (tmp_path / "fuera.txt").write_bytes(b"secreto\n")
    got = _with_content(folder, content, None)
    unsafe = [c for c in got.checks if c.check == "content" and c.reason == "unsafe_path"]
    assert [(c.status, dict(c.params)) for c in unsafe] == [("skip", {"n": 1})]  # sin la ruta (REQ-OPS-65)
    assert "../fuera.txt" not in _content_rows(got)  # ni se lee ni aparece


# -- calendarios: en memoria y --upgrade ------------------------------------------------------------


def test_upgrade_completes_the_ots_and_rewrites_it(healthy: corpus.Healthy, tmp_path: Path) -> None:
    folder = _copy(healthy.submission, tmp_path)
    before = (folder / record.OTS_FILE).read_bytes()
    router = Router(dict(healthy.calendar_routes))
    got = verify(folder, healthy, upgrade=ev.Upgrade(transport=router.transport()))
    assert (got.verdict, got.confidence, got.upgraded) == ("pending", None, True)  # el bloque no se coteja
    after = (folder / record.OTS_FILE).read_bytes()
    assert after != before and ots.read(after).bitcoin
    [check] = [c for c in got.checks if c.check == "ots"]
    assert (check.source, check.status, check.reason) == ("bitcoin", "pending", "bitcoin_unverified")
    assert check.notes == (("evidence.note.upgraded", {}),)
    assert not list(folder.glob(".*.tmp-*"))


def test_upgrade_with_dry_run_does_not_write(healthy: corpus.Healthy, tmp_path: Path) -> None:
    folder = _copy(healthy.submission, tmp_path)
    before = (folder / record.OTS_FILE).read_bytes()
    router = Router(dict(healthy.calendar_routes))
    got = verify(folder, healthy, upgrade=ev.Upgrade(mode="dry_run", transport=router.transport()))
    assert (got.verdict, got.upgraded) == ("pending", False)  # verifica la prueba completa en memoria
    assert (folder / record.OTS_FILE).read_bytes() == before
    [check] = [c for c in got.checks if c.check == "ots"]
    assert (check.reason, check.notes) == ("bitcoin_unverified", (("evidence.note.upgrade_dry_run", {}),))


def test_with_network_the_ots_is_completed_in_memory(healthy: corpus.Healthy, tmp_path: Path) -> None:
    """C-2: sin ``--upgrade`` también se consultan los calendarios, pero nada se escribe."""
    folder = _copy(healthy.submission, tmp_path)
    before = (folder / record.OTS_FILE).read_bytes()
    router = Router(dict(healthy.calendar_routes))
    got = verify(folder, healthy, upgrade=ev.Upgrade(mode="memory", transport=router.transport()))
    assert (got.verdict, got.upgraded, got.warnings) == ("pending", False, ())
    assert (folder / record.OTS_FILE).read_bytes() == before and len(router.calls) == 1
    [check] = [c for c in got.checks if c.check == "ots"]
    assert (check.source, check.reason, check.notes) == ("bitcoin", "bitcoin_unverified", (("evidence.note.completed", {}),))


def test_a_run_asks_each_calendar_pair_once(healthy: corpus.Healthy, tmp_path: Path) -> None:
    """Dos copias de la misma prueba pendiente en una corrida (un solo ``Budget``) piden
    una vez; una prueba que ya tiene bloque, o que no es del manifiesto, no pide nada."""
    first, second = _copy(healthy.submission, tmp_path / "a"), _copy(healthy.submission, tmp_path / "b")
    router = Router({"https://": 404})  # el calendario todavía no lo tiene
    calendars = ev.Upgrade(mode="memory", transport=router.transport())
    for folder in (first, second, healthy.submission_complete):
        got = verify(folder, healthy, upgrade=calendars)
        assert got.verdict == "pending"
    assert len(router.calls) == 1 and calendars.budget.requests == 1
    broken = _copy(healthy.submission, tmp_path / "c")
    (broken / record.OTS_FILE).write_bytes(corpus.ots_proofs(corpus.OTHER_DIGEST)[0])
    assert ("ots", "calendars", "fail", "digest_mismatch") in table(verify(broken, healthy, upgrade=calendars))
    assert len(router.calls) == 1


def test_upgrade_without_network_keeps_the_proof(healthy: corpus.Healthy, tmp_path: Path) -> None:
    folder = _copy(healthy.submission, tmp_path)
    before = (folder / record.OTS_FILE).read_bytes()
    got = verify(folder, healthy, upgrade=ev.Upgrade(transport=Router().transport()))
    assert (got.verdict, got.upgraded) == ("pending", False)
    assert "W_NET_OFFLINE" in got.warnings and (folder / record.OTS_FILE).read_bytes() == before


# -- veredictos e identidad ----------------------------------------------------------------------------


def _c(check: str, status: str) -> ev.CheckResult:
    return ev.CheckResult(check, status, "x")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([("manifest", "pass"), ("tsa", "pass"), ("ots", "pending")], ("verified", "strong")),
        ([("manifest", "pass"), ("tsa", "weak"), ("ots", "pending")], ("verified", "weak")),
        ([("manifest", "pass"), ("ots", "pending")], ("pending", None)),
        ([("manifest", "pass"), ("ots", "weak")], ("pending", None)),  # en v0.1, solo una TSA verifica
        ([("manifest", "skip"), ("tsa", "pass")], ("pending", None)),
        ([("manifest", "pass"), ("tsa", "pass"), ("content", "fail")], ("failed", None)),
        ([("manifest", "pass"), ("tsa", "skip"), ("ots", "skip")], ("pending", None)),
    ],
)
def test_record_verdict(statuses: list[tuple[str, str]], expected: tuple[str, str | None]) -> None:
    assert ev.verdict([_c(check, status) for check, status in statuses]) == expected


def _r(verdict: str, confidence: str | None) -> ev.RecordResult:
    return ev.RecordResult(Path("."), "integration", "0" * 40, "0" * 64, verdict, confidence, ())  # type: ignore[arg-type]


def test_overall_verdict() -> None:
    assert ev.overall([_r("verified", "strong"), _r("verified", "strong")]) == ("verified", "strong")
    assert ev.overall([_r("verified", "strong"), _r("verified", "weak")]) == ("verified", "weak")
    assert ev.overall([_r("verified", "strong"), _r("pending", None)]) == ("pending", None)
    assert ev.overall([_r("verified", "strong"), _r("failed", None)]) == ("failed", None)
    assert ev.overall([]) == ("pending", None)


def test_unidentifiable_record_raises(tmp_path: Path) -> None:
    folder = tmp_path / "carpeta"
    folder.mkdir()
    (folder / record.RECORD_FILE).write_bytes(b"{")
    with pytest.raises(ev.UnreadableRecord):
        ev.verify_folder(folder)
    named = tmp_path / f"integration-{'a' * 40}-a2"
    named.mkdir()
    (named / record.RECORD_FILE).write_bytes(b"{")
    (named / record.MANIFEST_FILE).write_bytes(b'{"kind":"integration"}')
    got = ev.verify_folder(named)
    assert (got.kind, got.commit, got.verdict) == ("integration", "a" * 40, "failed")
    assert table(got) == [("manifest", None, "fail", "record_invalid")]


def test_an_unverifiable_folder_gives_a_failed_record(tmp_path: Path) -> None:
    """Lo que se puede leer identifica el registro; lo que falta va en ceros. El motivo
    lleva el tipo del error, nunca su mensaje."""
    named = tmp_path / f"integration-{'a' * 40}"
    named.mkdir()
    (named / record.RECORD_FILE).write_text('{"kind": "integration", "n": ' + "9" * 5000 + "}", encoding="utf-8")
    got = ev.unverifiable(named, error="ValueError")
    assert (got.kind, got.commit, got.manifest_sha256) == ("integration", "a" * 40, "0" * 64)
    assert (got.verdict, got.confidence) == ("failed", None)
    assert [(c.check, c.status, c.reason, dict(c.params)) for c in got.checks] == [
        ("manifest", "fail", "unverifiable", {"error": "ValueError"})
    ]
    anonymous = tmp_path / "cualquiera"
    anonymous.mkdir()
    (anonymous / record.RECORD_FILE).write_bytes(b"[" * 5000 + b"]" * 5000)
    got = ev.unverifiable(anonymous, "record_unreadable")
    assert (got.kind, got.commit, got.manifest_sha256, got.verdict) == ("submission", "0" * 40, "0" * 64, "failed")
    assert table(got) == [("manifest", None, "fail", "record_unreadable")]


def test_detail_keys() -> None:
    assert ev.detail_key("manifest", "commits_last") == "evidence.manifest_problem.commits_last"
    assert ev.detail_key("manifest", "ok") == "evidence.check.manifest.ok"
    assert ev.detail_key("tsa", "chain") == "evidence.check.tsa.chain"


def test_reason_code_for_the_envelope() -> None:
    """``reason`` del sobre: ``null`` en un pass sin motivo; si no, el motivo estable."""
    assert ev.reason_code(ev.CheckResult("tsa", "pass", "ok", "alpha")) is None
    assert ev.reason_code(ev.CheckResult("content", "pass", "clean")) == "clean"  # qué forma coincidió
    assert ev.reason_code(ev.CheckResult("tsa", "weak", "no_revocation", "alpha")) == "no_revocation"
    assert ev.reason_code(ev.CheckResult("manifest", "fail", "entries_duplicate")) == "entries_duplicate"
    pattern = re.compile(r"[a-z][a-z0-9_]*")  # $defs/evidence_check.reason
    codes = {code for reasons in ev.REASONS.values() for code in reasons} | set(ev.MANIFEST_PROBLEMS)
    assert all(pattern.fullmatch(code) and len(code) <= 64 for code in codes)
