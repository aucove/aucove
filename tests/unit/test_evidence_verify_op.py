# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Operación ``evidence verify`` (05-operaciones §9.26; REQ-OPS-65; D-50).

Por el registro y la CLI, con los registros del generador del corpus y la TSA de prueba
(``<PREFIJO>TESTING=1`` y ``<PREFIJO>EVIDENCE_FAKE``, el mismo contrato de dobles que
``ci seal``). Cada sobre valida contra ``result.schema.json``. Sin red: el calendario es el
``MockTransport`` de ese archivo de dobles, que completa las pruebas OTS pendientes. Por
omisión, con «red», la operación consulta los calendarios en memoria (05 §9.26); con
``--offline`` no.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

pytest.importorskip("rfc3161_client", reason="falta el extra evidence (el CI lo instala)")
pytest.importorskip("opentimestamps", reason="falta el extra evidence (el CI lo instala)")

from aucove import _meta  # noqa: E402
from aucove.cli import main as cli  # noqa: E402
from aucove.config.schemas import validate_result  # noqa: E402
from aucove.evidence import ots, record, timefmt  # noqa: E402
from aucove.evidence import verify as ev  # noqa: E402
from aucove.i18n import translator  # noqa: E402
from aucove.ops.context import Invocation, OpContext  # noqa: E402
from aucove.ops.registry import REGISTRY, find_tool, invoke  # noqa: E402
from aucove.ops.result import Result  # noqa: E402
from tests.conftest import REPO_ROOT  # noqa: E402
from tests.fixtures.evidence.corpus import corpus  # noqa: E402
from tests.unit.evidence.support import needs_git, new_repo  # noqa: E402

pytestmark = needs_git

SPEC = next(s for s in REGISTRY if s.id == "evidence.verify")
PREFIX = _meta.ENV_PREFIX


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[corpus.Healthy, list[corpus.Built]]:
    return corpus.build(tmp_path_factory.mktemp("corpus-op"))


@pytest.fixture(scope="module")
def healthy(built: tuple[corpus.Healthy, list[corpus.Built]]) -> corpus.Healthy:
    return built[0]


def case(built: tuple[corpus.Healthy, list[corpus.Built]], case_id: str) -> corpus.Built:
    return next(b for b in built[1] if b.case.id == case_id)


def env_for(healthy: corpus.Healthy, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in healthy.repo.env.items() if not k.startswith(PREFIX)}
    env.update({PREFIX + "TESTING": "1", PREFIX + "EVIDENCE_FAKE": str(healthy.fake_module)})
    env.update(extra)
    return env


def run_op(
    healthy: corpus.Healthy,
    path: str | Path,
    *,
    lang: str = "es",
    dry_run: bool = False,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    **kwargs: object,
) -> Result:
    params: dict[str, object] = {"repo": None, "content": None, "offline": False, "upgrade": False, "tz": None,
                                 "keys": None}
    params.update(kwargs)
    folder = cwd or healthy.root
    inv = Invocation(root=None, lang=lang, json=True, dry_run=dry_run, surface="cli",
                     env=dict(env or env_for(healthy)), cwd=str(folder))
    return invoke(SPEC, OpContext(inv), path=str(path), **params)


def valid(result: Result) -> dict[str, object]:
    envelope = result.to_dict()
    assert validate_result(envelope) == [], json.dumps(envelope, ensure_ascii=False, indent=1)
    return envelope


def checks(result: Result, index: int = 0) -> list[tuple[str, str | None, str]]:
    assert result.data is not None
    return [(c["check"], c["source"], c["status"]) for c in result.data["records"][index]["checks"]]


def rows(result: Result, index: int = 0) -> list[tuple[str, str | None, str, str | None]]:
    """``(check, source, status, reason)``: ``reason`` es el motivo estable para las máquinas."""
    assert result.data is not None
    return [(c["check"], c["source"], c["status"], c["reason"]) for c in result.data["records"][index]["checks"]]


def codes(result: Result) -> list[str]:
    return [w.code for w in result.warnings]


def ots_check(result: Result, index: int = 0) -> dict[str, object]:
    assert result.data is not None
    return dict(next(c for c in result.data["records"][index]["checks"] if c["check"] == "ots"))


def _copy(folder: Path, tmp_path: Path) -> Path:
    target = tmp_path / folder.name
    shutil.copytree(folder, target)
    return target


# -- veredictos y sobre --------------------------------------------------------------------------------


def test_verified_strong_envelope(healthy: corpus.Healthy) -> None:
    result = run_op(healthy, healthy.integration)
    envelope = valid(result)
    assert result.ok and result.exit_code() == 0 and codes(result) == []
    data = envelope["data"]
    assert isinstance(data, dict)
    assert (data["target"], data["verdict"], data["confidence"]) == (str(healthy.integration), "verified", "strong")
    [rec] = data["records"]
    assert (rec["kind"], rec["commit"], rec["verdict"], rec["confidence"]) == (
        "integration", healthy.sha["c2"], "verified", "strong",
    )
    assert rec["manifest_sha256"] == json.loads((healthy.integration / "record.json").read_text("utf-8"))["manifest_sha256"]
    alpha = next(c for c in rec["checks"] if c["source"] == "alpha")
    assert alpha["time_utc"] == "2026-10-06T12:00:00Z"
    if timefmt.zone_status() == "ok":
        assert alpha["time_local"] == "2026-10-06T06:00:00-06:00"  # America/Mexico_City
    t = translator("es")
    assert alpha["detail"] == t("evidence.check.tsa.ok", revocation="OCSP")
    # un pass sin motivo lleva reason null; el resto, su motivo estable. Con red, el calendario
    # de prueba completa en memoria la prueba OTS, cuyo bloque no se coteja: queda pendiente.
    assert rows(result) == [
        ("manifest", None, "pass", None),
        ("signature", None, "skip", "unsigned"),  # sin firma del proyecto (D-72)
        ("tsa", "alpha", "pass", None),
        ("tsa", "beta", "pass", None),
        ("ots", "bitcoin", "pending", "bitcoin_unverified"),
        ("openings", None, "pass", None),
        ("actors", None, "pass", None),
    ]


def test_without_a_tsa_the_records_are_pending(healthy: corpus.Healthy) -> None:
    """D1: una entrega solo trae OTS; ni la prueba completa la lleva a verified."""
    for folder in (healthy.submission, healthy.submission_complete):
        result = run_op(healthy, folder, lang="en")
        valid(result)
        assert result.ok and (result.data["verdict"], result.data["confidence"]) == ("pending", None)  # type: ignore[index]
        assert codes(result) == []
        check = ots_check(result)
        assert (check["source"], check["status"], check["reason"]) == ("bitcoin", "pending", "bitcoin_unverified")
        assert "900000" in str(check["detail"]) and "`VERIFY.md`" in str(check["detail"])


def test_a_tsa_without_revocation_data_gives_a_weak_verification(healthy: corpus.Healthy, tmp_path: Path) -> None:
    folder = _copy(healthy.integration, tmp_path)
    (folder / "tsa" / "alpha.ocsp.der").unlink()
    (folder / "tsa" / "beta.crl.der").unlink()
    result = run_op(healthy, folder, lang="en")
    valid(result)
    assert result.ok and (result.data["verdict"], result.data["confidence"]) == ("verified", "weak")  # type: ignore[index]
    assert codes(result) == ["W_VALIDATION_EVIDENCE_WEAK"]


def test_a_package_gives_one_verdict_per_record_and_a_global_one(healthy: corpus.Healthy) -> None:
    result = run_op(healthy, healthy.root)
    valid(result)
    data = result.data
    assert data is not None and (data["verdict"], data["confidence"]) == ("pending", None)
    verdicts = sorted((r["kind"], r["verdict"], r["confidence"]) for r in data["records"])
    assert verdicts == [("integration", "verified", "strong"), ("submission", "pending", None), ("submission", "pending", None)]
    assert codes(result) == []


def test_an_alteration_fails_with_the_complete_data(built: tuple[corpus.Healthy, list[corpus.Built]]) -> None:
    healthy = built[0]
    result = run_op(healthy, case(built, "manifest-byte").folder)
    envelope = valid(result)
    assert not result.ok and result.error is not None and result.error.code == "E_VALIDATION_EVIDENCE_FAILED"
    assert result.exit_code() == 3
    data = envelope["data"]
    assert isinstance(data, dict) and data["verdict"] == "failed" and data["records"][0]["verdict"] == "failed"
    assert ("manifest", None, "fail") in checks(result)


@pytest.mark.parametrize("case_id", [c.id for c in corpus.CASES])
def test_every_corpus_case_through_the_operation(case_id: str, built: tuple[corpus.Healthy, list[corpus.Built]]) -> None:
    healthy = built[0]
    item = case(built, case_id)
    kwargs: dict[str, object] = {"keys": str(healthy.keys_path)}  # la lista publicada de llaves
    if item.case.repo:
        kwargs["repo"] = str(healthy.repo.path)
    if item.content is not None:
        kwargs["content"] = str(item.content)
    result = run_op(healthy, item.folder, **kwargs)
    envelope = valid(result)
    details = [c["detail"] or "" for r in envelope["data"]["records"] for c in r["checks"]]  # type: ignore[index]
    assert not any("{" in d or d.startswith("evidence.") for d in details), details  # textos completos
    got = rows(result)
    for expect in item.case.expect:  # también el motivo estable del sobre
        assert (expect.check, expect.source, expect.status, expect.reason) in got, (case_id, got)
    failing = any(e.status == "fail" for e in item.case.expect)
    assert result.ok is not failing
    if failing:
        assert result.error is not None and result.error.code == "E_VALIDATION_EVIDENCE_FAILED"


# -- firma del proyecto (00-marco D-72; 11-trazabilidad §8.10) ---------------------------------------------


def test_a_signed_package_through_the_operation(healthy: corpus.Healthy) -> None:
    """Un paquete con ``signature/``: ``signatures[]``, el origen de cada registro que cubre el
    índice firmado y el veredicto global; sin la lista de llaves, todo queda ``unknown``."""
    data = valid(run_op(healthy, healthy.package, keys=str(healthy.keys_path)))["data"]
    assert isinstance(data, dict)
    (folder,) = data["signatures"]
    assert (folder["folder"], folder["key_id"], folder["verdict"], folder["confidence"], folder["origin"]) == (
        "signature", corpus.KEY_ID, "verified", "strong", "project")
    assert [(c["check"], c["source"], c["status"]) for c in folder["checks"]] == [
        ("signature", corpus.KEY_ID, "pass"), ("tsa", "alpha", "pass"), ("tsa", "beta", "pass"),
        ("ots", "calendars", "pending"), ("index", None, "pass")]
    assert sorted((r["kind"], r["origin"]) for r in data["records"]) == [
        ("integration", "project"), ("submission", "project")]
    assert (data["verdict"], data["confidence"]) == ("pending", None)  # la entrega no tiene TSA
    unchecked = valid(run_op(healthy, healthy.package))["data"]
    assert isinstance(unchecked, dict) and unchecked["signatures"][0]["origin"] == "unknown"
    assert {r["origin"] for r in unchecked["records"]} == {"unknown"}
    # una carpeta suelta no es un paquete: sin signatures[]
    alone = valid(run_op(healthy, healthy.signed, keys=str(healthy.keys_path)))["data"]
    assert isinstance(alone, dict) and alone["signatures"] == [] and alone["records"][0]["origin"] == "project"


def test_the_keys_option(healthy: corpus.Healthy, tmp_path: Path) -> None:
    """``--keys`` es la lista publicada (solo ``keys``) o un ``project.yaml`` (su
    ``evidence_signing.keys``); si no se entiende, ``E_VALIDATION_INPUT`` sin verificar nada."""
    project = tmp_path / "project.yaml"
    project.write_text(json.dumps({"evidence_signing": {"mode": "ci", "keys": corpus.keys_document()}}),
                       encoding="utf-8")
    for keys in (healthy.keys_path, project):
        data = valid(run_op(healthy, healthy.signed, keys=str(keys)))["data"]
        assert isinstance(data, dict) and data["records"][0]["origin"] == "project", keys
    assert _input_errors(run_op(healthy, healthy.signed, keys=str(tmp_path / "nada.yaml"))) == [
        {"param": "keys", "reason": "not_found"}]
    bad = tmp_path / "bad.yaml"
    other_type = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAAAgQC"
    for text, expected in (
        ("keys: []\nextra: 1\n", {"param": "keys", "reason": "invalid", "pointer": ""}),
        ("[1, 2]\n", {"param": "keys", "reason": "invalid"}),
        (f"keys:\n  - id: x\n    public_key: {other_type}\n    since: '2026-01-01T00:00:00Z'\n",
         {"param": "keys", "reason": "invalid", "pointer": "/keys/0/public_key"}),
    ):
        bad.write_text(text, encoding="utf-8")
        assert _input_errors(run_op(healthy, healthy.signed, keys=str(bad))) == [expected], text


def test_cli_text_shows_the_origin_and_the_index_signature(healthy: corpus.Healthy) -> None:
    t = translator("es")
    result = run_cli(healthy, ["--lang", "es", "evidence", "verify", str(healthy.package),
                               "--keys", str(healthy.keys_path), "--offline"])
    assert result.code == 0, result.err
    lines = result.out.splitlines()
    # los dos registros que cubre el índice y la carpeta de firma
    assert lines.count(t("evidence.render.origin", origin=t("evidence.origin.project"))) == 3, lines
    assert any(line.startswith("Firma del índice `signature`:") for line in lines), lines
    assert any(corpus.KEY_ID in line for line in lines), lines


# -- errores de entrada y registros hostiles ----------------------------------------------------------------


def _input_errors(result: Result) -> list[dict[str, object]]:
    valid(result)
    assert not result.ok and result.error is not None and result.error.code == "E_VALIDATION_INPUT"
    assert result.data is None and result.exit_code() == 3
    return list(result.error.details["errors"])  # type: ignore[index]


def test_input_errors(healthy: corpus.Healthy, tmp_path: Path) -> None:
    empty = tmp_path / "vacia"
    empty.mkdir()
    assert _input_errors(run_op(healthy, empty)) == [{"param": "path", "reason": "no_records"}]
    assert _input_errors(run_op(healthy, tmp_path / "nada")) == [{"param": "path", "reason": "not_found"}]
    file = tmp_path / "archivo.txt"
    file.write_text("x", encoding="utf-8")
    assert _input_errors(run_op(healthy, file)) == [{"param": "path", "reason": "not_a_directory"}]
    if timefmt.zone_status("UTC") == "ok":  # sin base de zonas no se distingue una zona que no existe
        assert _input_errors(run_op(healthy, healthy.integration, tz="Marte/Olympus")) == [
            {"param": "tz", "reason": "unknown_zone"}
        ]
    assert _input_errors(run_op(healthy, healthy.integration, repo=str(tmp_path / "no"))) == [
        {"param": "repo", "reason": "not_found"}
    ]
    assert _input_errors(run_op(healthy, healthy.integration, repo=str(empty))) == [
        {"param": "repo", "reason": "not_a_repository"}
    ]


def test_relative_paths_are_taken_from_the_current_directory(healthy: corpus.Healthy) -> None:
    result = run_op(healthy, healthy.integration.name, cwd=healthy.root)
    assert result.ok and result.data is not None and result.data["target"] == healthy.integration.name


def test_an_unreadable_record_fails_alone(healthy: corpus.Healthy, tmp_path: Path) -> None:
    """No es ``E_VALIDATION_INPUT``: es un registro ``failed`` y los demás siguen."""
    package = tmp_path / "paquete"
    _copy(healthy.integration, package)
    folder = package / "cualquiera"
    folder.mkdir()
    (folder / "record.json").write_bytes(b"{")
    result = run_op(healthy, package)
    envelope = valid(result)
    assert not result.ok and result.error is not None and result.error.code == "E_VALIDATION_EVIDENCE_FAILED"
    records = {r["commit"]: r for r in envelope["data"]["records"]}  # type: ignore[index]
    assert records[healthy.sha["c2"]]["verdict"] == "verified"
    broken = records["0" * 40]
    assert (broken["kind"], broken["manifest_sha256"], broken["verdict"]) == ("submission", "0" * 64, "failed")
    assert [(c["check"], c["status"], c["reason"]) for c in broken["checks"]] == [("manifest", "fail", "record_unreadable")]


def test_an_unexpected_error_fails_only_its_record(
    healthy: corpus.Healthy, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si los archivos de una carpeta provocan un error que nadie previó, ese registro da
    ``failed`` (``unverifiable``, con el tipo del error y nunca su mensaje) y los demás siguen."""
    package = tmp_path / "paquete"
    _copy(healthy.integration, package)
    broken = _copy(healthy.submission, package)
    real = ev.verify_folder

    def flaky(folder: Path, **options: object) -> ev.RecordResult:
        if Path(folder).name == broken.name:
            raise RecursionError("maximum recursion depth exceeded (dato del registro)")
        return real(folder, **options)  # type: ignore[arg-type]

    monkeypatch.setattr(ev, "verify_folder", flaky)
    result = run_op(healthy, package)
    envelope = valid(result)
    assert result.error is not None and result.error.code == "E_VALIDATION_EVIDENCE_FAILED"
    records = {r["kind"]: r for r in envelope["data"]["records"]}  # type: ignore[index]
    assert records["integration"]["verdict"] == "verified"
    failed = records["submission"]
    assert (failed["commit"], failed["verdict"]) == (healthy.sha["p1"], "failed")
    [check] = failed["checks"]
    assert (check["check"], check["status"], check["reason"]) == ("manifest", "fail", "unverifiable")
    assert "RecursionError" in check["detail"] and "dato del registro" not in check["detail"]


def _hostile_ots(digest: bytes) -> bytes:
    """r3: prueba del manifiesto con una pendiente en una URI que pasa la lista blanca y no es una URL."""
    from opentimestamps.core.notary import PendingAttestation
    from opentimestamps.core.op import OpSHA256
    from opentimestamps.core.serialize import BytesSerializationContext
    from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

    detached = DetachedTimestampFile(OpSHA256(), Timestamp(digest))
    detached.timestamp.ops.add(OpSHA256()).attestations.add(PendingAttestation("https://x:y.calendar.opentimestamps.org"))
    ctx = BytesSerializationContext()
    detached.serialize(ctx)
    return bytes(ctx.getbytes())


def test_hostile_records_do_not_leave_the_package_without_a_verdict(healthy: corpus.Healthy, tmp_path: Path) -> None:
    """Un entero de más de 4300 dígitos, un anidamiento profundo y una URI de
    calendario inválida. Cada carpeta da su veredicto; ninguna excepción sale de la operación."""
    package = tmp_path / "paquete"
    _copy(healthy.integration, package)
    huge = package / f"integration-{'a' * 40}"
    huge.mkdir()
    (huge / "record.json").write_text('{"kind": "integration", "n": ' + "9" * 5000 + "}", encoding="utf-8")
    deep = _copy(healthy.submission, package)
    (deep / "record.json").write_bytes(b'{"kind": ' + b"[" * 100_000 + b"]" * 100_000 + b"}")
    nested = _copy(healthy.submission_complete, package)
    corpus._reseal(nested, b"[" * 100_000 + b"]" * 100_000)  # con su huella en record.json y openings.json
    calendar = package / "calendario"
    shutil.copytree(healthy.submission, calendar)
    digest = hashlib.sha256((calendar / record.MANIFEST_FILE).read_bytes()).digest()
    (calendar / record.OTS_FILE).write_bytes(_hostile_ots(digest))

    result = run_op(healthy, package)
    envelope = valid(result)
    assert not result.ok and result.error is not None and result.error.code == "E_VALIDATION_EVIDENCE_FAILED"
    records = envelope["data"]["records"]  # type: ignore[index]
    assert len(records) == 5
    by_folder = dict(zip(sorted(p.name for p in package.iterdir()), records))
    assert by_folder[healthy.integration.name]["verdict"] == "verified"
    for name in (huge.name, deep.name, nested.name):
        assert by_folder[name]["verdict"] == "failed", by_folder[name]
    assert (by_folder[huge.name]["kind"], by_folder[huge.name]["commit"]) == ("integration", "a" * 40)
    hostile = by_folder["calendario"]
    assert hostile["verdict"] == "pending" and ("ots", "calendars", "pending") in [
        (c["check"], c["source"], c["status"]) for c in hostile["checks"]
    ]


# -- sin el extra (REQ-ARCH-99) ----------------------------------------------------------------------------


def test_without_the_extra(healthy: corpus.Healthy, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "rfc3161_client", None)
    monkeypatch.setitem(sys.modules, "opentimestamps", None)
    result = run_op(healthy, healthy.integration)
    valid(result)
    assert result.ok and result.data["verdict"] == "pending"  # type: ignore[index]
    assert rows(result) == [
        ("manifest", None, "pass", None),
        ("signature", None, "skip", "unsigned"),
        ("tsa", "alpha", "skip", "missing_extra"),
        ("tsa", "beta", "skip", "missing_extra"),
        ("ots", "calendars", "skip", "missing_extra"),
        ("openings", None, "pass", None),
        ("actors", None, "pass", None),
    ]
    assert codes(result) == ["W_CONFIG_TOOL_MISSING"]


# -- --repo, --content y --tz ------------------------------------------------------------------------------


def test_repo_option(healthy: corpus.Healthy, tmp_path: Path) -> None:
    result = run_op(healthy, healthy.integration, repo=str(healthy.repo.path))
    assert result.ok and ("git", None, "pass", None) in rows(result)
    other = new_repo(tmp_path, "otro")
    missing = run_op(healthy, healthy.submission, repo=str(other.path))
    valid(missing)
    assert missing.ok and ("git", None, "skip", "missing_commit") in rows(missing)
    git = next(c for c in missing.data["records"][0]["checks"] if c["check"] == "git")  # type: ignore[index]
    assert "refs/pull/*/head" in git["detail"]


def test_content_option_uses_an_isolated_clean_form(built: tuple[corpus.Healthy, list[corpus.Built]]) -> None:
    """Con la carpeta del caso ``content-filter`` (su ``.git`` trae un filtro que
    devolvería lo sellado), la operación compara la forma limpia sin ese filtro."""
    healthy = built[0]
    item = case(built, "content-filter")
    result = run_op(healthy, item.folder, content=str(item.content))
    valid(result)
    content = {c["detail"].split(":")[0]: (c["status"], c["reason"]) for c in result.data["records"][0]["checks"]  # type: ignore[index]
               if c["check"] == "content"}
    assert content["sections/intro/intro.md"] == ("fail", "mismatch")
    assert content["sections/methods/methods.md"] == ("pass", "raw")


def test_tz_controls_the_local_time(healthy: corpus.Healthy) -> None:
    result = run_op(healthy, healthy.integration, tz="UTC")
    alpha = next(c for c in result.data["records"][0]["checks"] if c["source"] == "alpha")  # type: ignore[index]
    if timefmt.zone_status("UTC") == "ok":
        assert alpha["time_local"] == "2026-10-06T12:00:00+00:00"


# -- calendarios: en memoria, --upgrade, --offline y --dry-run ------------------------------------------------


def test_with_network_the_calendars_complete_the_proof_in_memory(healthy: corpus.Healthy, tmp_path: Path) -> None:
    """C-2 (05 §9.26): sin ``--offline`` se consultan los calendarios, pero nada se escribe."""
    folder = _copy(healthy.submission, tmp_path)
    before = (folder / record.OTS_FILE).read_bytes()
    result = run_op(healthy, folder)
    valid(result)
    assert result.ok and codes(result) == [] and (folder / record.OTS_FILE).read_bytes() == before
    check = ots_check(result)
    assert (check["source"], check["reason"]) == ("bitcoin", "bitcoin_unverified")
    assert str(check["detail"]).endswith(translator("es")("evidence.note.completed"))


def test_upgrade_rewrites_the_ots(healthy: corpus.Healthy, tmp_path: Path) -> None:
    """El calendario de prueba es el ``transport()`` del archivo de ``<PREFIJO>EVIDENCE_FAKE``."""
    folder = _copy(healthy.submission, tmp_path)
    result = run_op(healthy, folder, upgrade=True)
    valid(result)
    assert result.ok and (result.data["verdict"], result.data["confidence"]) == ("pending", None)  # type: ignore[index]
    assert ots.read((folder / record.OTS_FILE).read_bytes()).bitcoin
    assert codes(result) == []
    assert str(ots_check(result)["detail"]).endswith(translator("es")("evidence.note.upgraded"))


def test_upgrade_with_dry_run_writes_nothing(healthy: corpus.Healthy, tmp_path: Path) -> None:
    folder = _copy(healthy.submission, tmp_path)
    before = (folder / record.OTS_FILE).read_bytes()
    result = run_op(healthy, folder, upgrade=True, dry_run=True)
    assert result.ok and (folder / record.OTS_FILE).read_bytes() == before
    assert str(ots_check(result)["detail"]).endswith(translator("es")("evidence.note.upgrade_dry_run"))


def _fake_without_routes(healthy: corpus.Healthy, tmp_path: Path) -> Path:
    """Archivo de dobles con el mismo catálogo y sin rutas de calendario: la red está caída."""
    corpus_fake = tmp_path / "fake.py"
    corpus_fake.write_text(
        healthy.fake_module.read_text(encoding="utf-8").replace("HERE = Path(__file__).resolve().parent",
                                                                 f"HERE = Path(r'{tmp_path / 'vacio'}')"),
        encoding="utf-8",
    )
    (tmp_path / "vacio").mkdir()
    (tmp_path / "vacio" / "calendar-routes.json").write_text("{}", encoding="utf-8")
    shutil.copy(healthy.catalog_path, tmp_path / "vacio" / "tsa-catalog.json")
    return corpus_fake


@pytest.mark.parametrize("upgrade", [False, True])
def test_unreachable_calendars_warn(healthy: corpus.Healthy, tmp_path: Path, upgrade: bool) -> None:
    folder = _copy(healthy.submission, tmp_path)
    fake = _fake_without_routes(healthy, tmp_path)
    result = run_op(healthy, folder, upgrade=upgrade, env=env_for(healthy, **{PREFIX + "EVIDENCE_FAKE": str(fake)}))
    valid(result)
    assert result.ok and result.data["verdict"] == "pending" and codes(result) == ["W_NET_OFFLINE"]  # type: ignore[index]


@pytest.mark.parametrize("how", ["flag", "env"])
def test_offline_never_queries_the_calendars(
    healthy: corpus.Healthy, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    folder = _copy(healthy.submission, tmp_path)
    before = (folder / record.OTS_FILE).read_bytes()

    def never(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("sin red no se consultan los calendarios")

    monkeypatch.setattr(ots, "upgrade", never)
    env = env_for(healthy, **({PREFIX + "OFFLINE": "1"} if how == "env" else {}))
    for upgrade in (True, False):  # con o sin --upgrade: queda una prueba sin bloque
        result = run_op(healthy, folder, upgrade=upgrade, offline=how == "flag", env=env)
        valid(result)
        assert result.ok and result.data["verdict"] == "pending"  # type: ignore[index]
        assert (folder / record.OTS_FILE).read_bytes() == before
        assert codes(result) == ["W_NET_OFFLINE"]
        assert (ots_check(result)["source"], ots_check(result)["reason"]) == ("calendars", "pending")
    # una prueba que ya tiene bloque no necesita los calendarios: sin aviso
    assert codes(run_op(healthy, healthy.submission_complete, offline=how == "flag", env=env)) == []


# -- REQ-OPS-65: sin MCP y sin sales ---------------------------------------------------------------------------


def test_no_mcp_tool() -> None:
    assert SPEC.mcp_tool is None and SPEC.cli == ("evidence", "verify")
    assert all(find_tool(s.mcp_tool) is not SPEC for s in REGISTRY if s.mcp_tool)
    assert all(p.cli_only for p in SPEC.params)


def _secrets(folder: Path) -> tuple[list[str], list[str]]:
    """Sales (hex) y rutas de las aperturas de un registro."""
    openings = json.loads((folder / record.OPENINGS_FILE).read_text(encoding="utf-8"))
    salts = [e["salt"] for e in openings["entries"]] + [a["salt"] for a in openings["actors"]]
    return salts, [e["path"] for e in openings["entries"]]


def test_the_output_never_carries_salts(built: tuple[corpus.Healthy, list[corpus.Built]]) -> None:
    healthy = built[0]
    runs = [
        (healthy.integration, run_op(healthy, healthy.integration, repo=str(healthy.repo.path), content=str(healthy.content))),
        (healthy.submission, run_op(healthy, healthy.submission, repo=str(healthy.repo.path))),
    ]
    for item in built[1]:  # también con aperturas alteradas: sus sales tampoco salen
        if item.case.id in ("opening-salt", "opening-path", "actor-salt", "actor-value", "manifest-byte"):
            runs.append((item.folder, run_op(healthy, item.folder)))
    for folder, result in runs:
        text = json.dumps(result.to_dict(), ensure_ascii=False)
        salts, paths = _secrets(folder)
        assert salts and not any(s in text for s in salts)
        assert not any(s.upper() in text.upper() for s in salts)
        details = [c["detail"] or "" for r in (result.data or {}).get("records", []) for c in r["checks"]
                   if c["check"] != "content"]
        assert not any(p in d for p in paths for d in details), "rutas fuera de los checks content"
        reasons = [c["reason"] for r in (result.data or {}).get("records", []) for c in r["checks"]]
        assert all(r is None or (re.fullmatch(r"[a-z][a-z0-9_]*", r) and len(r) <= 64) for r in reasons), reasons


# -- CLI ----------------------------------------------------------------------------------------------------------


@dataclass
class Cli:
    code: int
    out: str
    err: str


def run_cli(healthy: corpus.Healthy, argv: Sequence[str]) -> Cli:
    out, err = io.StringIO(), io.StringIO()
    code = cli.run(list(argv), env=env_for(healthy), stdout=out, stderr=err)
    return Cli(code, out.getvalue(), err.getvalue())


def test_cli_json(built: tuple[corpus.Healthy, list[corpus.Built]]) -> None:
    healthy = built[0]
    ok = run_cli(healthy, ["--json", "evidence", "verify", str(healthy.integration), "--repo", str(healthy.repo.path),
                           "--content", str(healthy.content), "--tz", "UTC", "--offline"])
    assert ok.code == 0 and ok.err == ""
    envelope = json.loads(ok.out)
    assert validate_result(envelope) == [] and envelope["op"] == "evidence.verify"
    assert [w["code"] for w in envelope["warnings"]] == ["W_NET_OFFLINE"]  # la prueba OTS quedó sin bloque
    failed = run_cli(healthy, ["--json", "evidence", "verify", str(case(built, "tsr-signature").folder)])
    assert failed.code == 3 and json.loads(failed.out)["error"]["code"] == "E_VALIDATION_EVIDENCE_FAILED"
    usage = run_cli(healthy, ["--json", "evidence", "verify"])
    assert usage.code == 3 and json.loads(usage.out)["error"]["details"]["errors"] == [{"param": "path", "reason": "required"}]


def test_cli_text_is_localized(built: tuple[corpus.Healthy, list[corpus.Built]]) -> None:
    healthy = built[0]
    t = translator("es")
    result = run_cli(healthy, ["--lang", "es", "evidence", "verify", str(healthy.integration)])
    assert result.code == 0
    lines = result.out.splitlines()
    assert lines[0].startswith(t("evidence.verify")) and lines[0].endswith(t("evidence.verdict.verified") + " (confianza alta)")
    assert any(line.strip().startswith("[pasa] TSA alpha:") for line in lines)
    failed = run_cli(healthy, ["--lang", "es", "evidence", "verify", str(case(built, "opening-salt").folder)])
    assert failed.code == 3 and "error[E_VALIDATION_EVIDENCE_FAILED]" in failed.err
    assert "[falla] aperturas:" in failed.out


def test_cli_text_never_lets_a_path_add_lines(healthy: corpus.Healthy, monkeypatch: pytest.MonkeyPatch) -> None:
    """La ruta de un archivo comprobado viene de ``openings.json`` (de terceros); un salto
    de línea en ella no agrega una línea falsa a la salida legible."""
    spoof = "a.md\n  [pasa] contenido: falso.md: coinciden los bytes crudos.\r\n  [pasa] git: ok"

    def fake(folder: Path, **_options: object) -> ev.RecordResult:
        check = ev.CheckResult("content", "pass", "raw", params={"path": spoof})
        manifest_ok = ev.CheckResult("manifest", "pass", "ok", params={"entries": 1, "actors": 0})
        return ev.RecordResult(folder, "integration", "a" * 40, "b" * 64, "pending", None, (manifest_ok, check))

    monkeypatch.setattr(ev, "verify_folder", fake)
    result = run_cli(healthy, ["--lang", "es", "evidence", "verify", str(healthy.integration), "--offline"])
    assert result.code == 0
    lines = result.out.splitlines()
    assert not any(line.lstrip().startswith("[pasa] contenido: falso.md") or line.strip() == "[pasa] git: ok"
                   for line in lines), lines
    content = [line for line in lines if "contenido: a.md" in line]
    assert len(content) == 1 and "\\x0a" in content[0] and "\\x0d" in content[0]


def test_cli_help_lists_the_options() -> None:
    out, err = io.StringIO(), io.StringIO()
    code = cli.run(["evidence", "verify", "--help"], env={PREFIX + "LANG": "en"}, stdout=out, stderr=err)
    assert code == 0
    for flag in ("--repo", "--content", "--offline", "--upgrade", "--tz", "--keys", "PATH"):
        assert flag in out.getvalue(), flag


def test_the_operation_does_not_load_the_extra_until_needed() -> None:
    """Importar la operación (y el registro) no carga el extra (REQ-ARCH-99)."""
    code = (
        "import sys\n"
        "import aucove.ops.evidence_verify, aucove.ops.registry\n"
        "print(sorted(m for m in ('rfc3161_client', 'opentimestamps', 'cryptography', 'bitcoin') if m in sys.modules))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT / "src"))
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, env=env, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert proc.stdout.decode().strip() == "[]"
