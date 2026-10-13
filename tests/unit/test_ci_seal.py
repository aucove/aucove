# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``ci seal`` contra FakeForge, una TSA de prueba y calendarios OTS falsos (09-ci §7.4; 11-trazabilidad §8).

REQ-CI-50 a REQ-CI-56 y A17 (a) sin red: la TSA es la de
``tests/fixtures/evidence/tsa/fake_tsa.py`` (dos PKI de P-256 detrás de un
``httpx2.MockTransport``) y los cuatro calendarios por defecto responden con una
atestación pendiente. La historia de git se arma con ``git fast-import`` y los SHA reales
se siembran en el FakeForge, así que ``ci seal`` lee el mismo commit en git y en la forja.

Las pruebas de flujo sellan varias veces (cada sello lee git, firma con la TSA de prueba
y verifica la cadena): tardan algo más de 1 s cada una porque recorren E1 completo.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import io
import itertools
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("rfc3161_client", reason="falta el extra evidence (el CI lo instala)")
pytest.importorskip("opentimestamps", reason="falta el extra evidence (el CI lo instala)")

import httpx2  # noqa: E402
from opentimestamps.core.notary import PendingAttestation  # noqa: E402
from opentimestamps.core.op import OpAppend, OpSHA256  # noqa: E402
from opentimestamps.core.serialize import BytesSerializationContext  # noqa: E402
from opentimestamps.core.timestamp import Timestamp  # noqa: E402

from aucove import _meta  # noqa: E402
from aucove.cli import ci as cli_ci  # noqa: E402
from aucove.cli.ci import CiContext  # noqa: E402
from aucove.evidence import manifest as evidence_manifest  # noqa: E402
from aucove.evidence import ots  # noqa: E402
from aucove.evidence.catalog import Catalog  # noqa: E402
from aucove.evidence.record import COMMENT_LIMIT, Record, load_comment_json, read_record_dir  # noqa: E402
from aucove.forge.base import RepoRef  # noqa: E402
from aucove.forge.fake import CI_LOGIN, Fault, FakeForge  # noqa: E402
from aucove.i18n import LANGS, translator  # noqa: E402
from aucove.ops import ci_seal  # noqa: E402
from aucove.ops.context import Invocation  # noqa: E402
from tests.conftest import REPO_ROOT  # noqa: E402
from tests.fixtures.evidence.tsa import fake_tsa as ft  # noqa: E402
from tests.unit.evidence.support import Stream, needs_git, new_repo, validate  # noqa: E402
from tests.unit.test_ci_build_plan import project_yaml  # noqa: E402

pytestmark = needs_git

UTC = timezone.utc
NOW = datetime(2026, 10, 6, 12, 0, 0, tzinfo=UTC)  # reloj del runner
REPO = RepoRef("example-org", "demo")
REPOSITORY_ID = "424242"
CALENDAR_URI = "https://alice.btc.calendar.opentimestamps.org"  # de la lista blanca de upgrade
PREFIX = _meta.ENV_PREFIX
BOT = CI_LOGIN


# -- servicios de sellado sin red --------------------------------------------------------


class FakeCalendar:
    """Calendario OTS: a ``POST /digest`` responde ``append`` + ``sha256`` y una atestación pendiente."""

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        stamp = Timestamp(request.read())
        leaf = stamp.ops.add(OpAppend(b"\x01" * 8)).ops.add(OpSHA256())
        leaf.attestations.add(PendingAttestation(CALENDAR_URI))
        ctx = BytesSerializationContext()
        stamp.serialize(ctx)
        return httpx2.Response(200, content=bytes(ctx.getbytes()))


@dataclass
class Services:
    """Dos TSA de prueba del catálogo (con OCSP) y los calendarios por defecto, detrás de un ``Router``."""

    pkis: tuple[ft.Pki, ...]
    router: ft.Router
    catalog: Catalog

    @classmethod
    def make(cls, pkis: Sequence[ft.Pki], *, tsa: bool = True, calendars: bool = True) -> Services:
        router = ft.Router()
        if tsa:
            for pki in pkis:
                router.routes[pki.tsa_url] = ft.FakeTsa(pki)
                if pki.ocsp_url:
                    router.routes[pki.ocsp_url] = ft.ocsp_response(pki)
        if calendars:
            for url in ots.DEFAULT_CALENDARS:
                router.routes[url + "/digest"] = FakeCalendar()
        return cls(tuple(pkis), router, ft.catalog_for(*(p.entry() for p in pkis)))

    def sealers(self) -> ci_seal.Sealers:
        return ci_seal.Sealers(transport=self.router.transport(), catalog=self.catalog, sleep=lambda _s: None)


_HOOK_SERVICES: Services | None = None


def hook_services() -> Services:
    """Servicios del archivo de ``<PREFIJO>EVIDENCE_FAKE`` (uno por proceso)."""
    global _HOOK_SERVICES
    if _HOOK_SERVICES is None:
        _HOOK_SERVICES = Services.make([ft.make_pki("alpha"), ft.make_pki("beta")])
    return _HOOK_SERVICES


HOOK_SOURCE = """\
from tests.unit.test_ci_seal import hook_services


def transport():
    return hook_services().router.transport()


def catalog():
    return hook_services().catalog
"""


@pytest.fixture(scope="module")
def pkis() -> tuple[ft.Pki, ft.Pki]:
    return ft.make_pki("alpha"), ft.make_pki("beta")


@pytest.fixture
def services(pkis: tuple[ft.Pki, ft.Pki]) -> Services:
    return Services.make(pkis)


# -- historia, forja y corridas ------------------------------------------------------------


BASE_FILES: Mapping[bytes, bytes] = {
    b"project.yaml": b"name: demo\n",
    b"plan.md": b"# Plan\n",
    b"roles.yaml": b"version: 1\n",
    b"memory/constitution.md": b"# Constitution\n",
    b"sections/introduction/introduction.tex": b"% intro\n",
}
METHODS_V1 = b"% methods v1\n"
METHODS_V2 = b"% methods v2\n"
DATA_CSV = b"a,b\n1,2\n"


@dataclass(frozen=True)
class Result:
    rc: int
    out: str
    err: str
    out_dir: Path
    summary: Path

    def outputs(self) -> dict[str, str]:
        return dict(line.split("=", 1) for line in self.out.splitlines())

    def folder(self) -> Path:
        (only,) = [p for p in self.out_dir.iterdir() if p.is_dir()]
        return only

    def summary_text(self) -> str:
        return self.summary.read_text(encoding="utf-8")


@dataclass
class World:
    """Repositorio real, FakeForge con los mismos SHA y la política de la base."""

    tmp: Path
    services: Services
    lang: str = "es"
    shas: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.repo = new_repo(self.tmp)
        self.forge = FakeForge(REPO, viewer=BOT)
        self.policy = self.tmp / "policy"
        self.policy.mkdir()
        (self.policy / "project.yaml").write_text(project_yaml(language=self.lang), encoding="utf-8", newline="\n")
        self._runs: Iterator[int] = itertools.count(7001)
        self._outs: Iterator[int] = itertools.count(1)

    def build(self, history: Callable[[Stream], Mapping[str, str]]) -> dict[str, str]:
        """Importa la historia; ``history`` devuelve nombre -> marca y aquí quedan nombre -> sha."""
        stream = Stream()
        marks = history(stream)
        found = stream.run(self.repo)
        self.shas.update({name: found[mark] for name, mark in marks.items()})
        return self.shas

    def env(self, **extra: str) -> dict[str, str]:
        """Entorno del job: git aislado, sin variables propias ni de Actions heredadas, y las del run.

        Tampoco hereda el token de ``gh`` (``GH_TOKEN`` ni la configuración de ``gh``): sin la
        forja falsa, el adaptador real no encuentra token (``no_token``), como un job sin
        ``GITHUB_TOKEN``.
        """
        drop = (PREFIX, "GITHUB_", "RUNNER_", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GH_CONFIG_DIR")
        base = {k: v for k, v in self.repo.env.items() if not k.upper().startswith(drop)}
        base["GH_CONFIG_DIR"] = str(self.tmp / "gh-config")
        base.update({"GITHUB_REPOSITORY": "example-org/demo", "GITHUB_REPOSITORY_ID": REPOSITORY_ID})
        base.update(extra)
        return base

    def argv(self, kind: str, commit: str, *, pr: int | None = None, base_sha: str | None = None,
             out: Path, summary: Path) -> list[str]:
        argv = ["--kind", kind, "--policy", str(self.policy), "--repo-dir", str(self.repo.path), "--commit", commit,
                "--out", str(out), "--summary", str(summary)]
        if pr is not None:
            argv += ["--pr", str(pr)]
        if base_sha is not None:
            argv += ["--base-sha", base_sha]
        return argv

    def seal(
        self,
        kind: str,
        commit: str,
        *,
        pr: int | None = None,
        base_sha: str | None = None,
        attempt: int = 1,
        run_id: int | None = None,
        sealers: ci_seal.Sealers | None = None,
        env: Mapping[str, str] | None = None,
        forge: bool = True,
    ) -> Result:
        n = next(self._outs)
        out, summary = self.tmp / f"out-{n}", self.tmp / f"summary-{n}.md"
        parser = argparse.ArgumentParser()
        ci_seal._add_arguments(parser)
        ns = parser.parse_args(self.argv(kind, commit, pr=pr, base_sha=base_sha, out=out, summary=summary))
        run = str(run_id if run_id is not None else next(self._runs))
        full_env = self.env(GITHUB_RUN_ID=run, GITHUB_RUN_ATTEMPT=str(attempt), **dict(env or {}))
        inv = Invocation(root=None, lang=None, json=False, dry_run=False, surface="ci", env=full_env,
                         cwd=str(self.tmp), now=lambda: NOW)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = ci_seal.seal(ns, CiContext(inv, forge=self.forge if forge else None), stdout,
                              sealers=sealers if sealers is not None else self.services.sealers())
        return Result(rc, stdout.getvalue(), stderr.getvalue(), out, summary)

    def push(self, commit: str, *, ok: bool = True, **kw: object) -> Result:
        """Push a la base: sella y registra el run de seal.yml con su conclusión real."""
        result = self.seal("integration", commit, **kw)  # type: ignore[arg-type]
        self.forge.seed_run(ci_seal.WORKFLOW_FILE, commit, "push", "success" if result.rc == 0 and ok else "failure")
        self.forge.clock.advance(60)
        return result

    def evidence(self, pr: int) -> list[tuple[str, Record]]:
        """``(cuerpo, registro)`` de los comentarios ``ci:evidence`` del bot en el PR ``pr``."""
        out = []
        for comment in self.forge.list_comments(pr):
            payload = ci_seal.evidence_json(comment.body)
            if payload is not None and comment.author == BOT:
                out.append((comment.body, load_comment_json(payload)))
        return out


def e1(stream: Stream) -> dict[str, str]:
    """E1: el primer push, dos cabezas del PR de la tarea 3 y la integración (squash)."""
    c0 = stream.commit("main", dict(BASE_FILES), message="init")
    h1 = stream.commit("task/3", {b"sections/methods/methods.tex": METHODS_V1}, parent=c0, message="methods")
    h2 = stream.commit("task/3", {b"sections/methods/methods.tex": METHODS_V2, b"sections/methods/data.csv": DATA_CSV},
                       message="data")
    s1 = stream.commit("main", {b"sections/methods/methods.tex": METHODS_V2, b"sections/methods/data.csv": DATA_CSV},
                       message="Methods (#5)")
    return {"c0": c0, "h1": h1, "h2": h2, "s1": s1}


@pytest.fixture
def world(tmp_path: Path, services: Services) -> World:
    return World(tmp_path, services)


def _e1_world(world: World) -> dict[str, str]:
    shas = world.build(e1)
    world.forge.seed_pull_request(
        files=[], number=5, head_branch="task/3", head_sha=shas["h2"], author="carla", base_branch="main",
        merged=True, merge_commit_sha=shas["s1"], merged_by="ana",
    )
    return shas


def _record_json(body: str) -> dict[str, object]:
    payload = ci_seal.evidence_json(body)
    assert payload is not None, body[:200]
    return json.loads(payload)


# -- E1 (A17 a) -----------------------------------------------------------------------------


def test_e1_one_submission_comment_per_head_and_one_integration_comment_per_push(world: World) -> None:
    shas = _e1_world(world)
    first = world.push(shas["c0"])
    assert (first.rc, first.outputs()) == (0, {"records": "1", "status": "complete"})
    for head in ("h1", "h2"):
        got = world.seal("submission", shas[head], pr=5, base_sha=shas["c0"])
        assert (got.rc, got.outputs()) == (0, {"records": "1", "status": "complete"}), got.err
    merged = world.push(shas["s1"])
    assert (merged.rc, merged.outputs()) == (0, {"records": "1", "status": "complete"}), merged.err

    found = world.evidence(5)
    assert [(r.kind, r.commit) for _b, r in found] == [
        ("submission", shas["h1"]), ("submission", shas["h2"]), ("integration", shas["s1"])
    ]
    for body, record in found:
        assert validate("evidence-record", _record_json(body)) == []
        assert body.split("\n")[0] == ci_seal.evidence_marker(record.kind, record.commit, 1)
        assert body.split("\n")[2] == "-->"
        assert record.pr == 5 and record.task == 3 and record.truncated is False
    submission, integration = found[1][1], found[2][1]
    assert [p.type for p in submission.proofs] == ["ots"]  # entrega: OpenTimestamps
    assert [(p.type, p.source, p.status) for p in integration.proofs] == [
        ("rfc3161", "alpha", "ok"), ("rfc3161", "beta", "ok"), ("ots", "calendars", "ok")
    ]
    # el registro de integración ata la entrega a las cuentas sin publicarlas
    assert integration.openings is not None
    assert {a.role: a.value for a in integration.openings.actors}["merged_by"] == "ana"
    assert {a.role: a.value for a in integration.openings.actors}["pr_author"] == "carla"
    assert sorted(e.path for e in integration.openings.entries) == ["sections/methods/data.csv",
                                                                     "sections/methods/methods.tex"]
    # el primer push no tiene PR: solo artefacto y resumen
    assert len(world.evidence(5)) == 3 and first.folder().name == f"integration-{shas['c0']}"


def test_e1_never_writes_to_branches(world: World) -> None:
    """REQ-TRACE-81 y A17 (a): 0 escrituras en ramas; en la forja, solo los comentarios."""
    shas = _e1_world(world)
    refs_before = world.repo.git("for-each-ref", "--format=%(refname) %(objectname)")
    world.forge.calls.clear()
    world.push(shas["c0"])
    world.seal("submission", shas["h2"], pr=5, base_sha=shas["c0"])
    world.push(shas["s1"])
    assert {method for method, _args in world.forge.calls.writes()} == {"create_comment"}
    assert world.forge.calls.count("E") == world.forge.calls.count("C") == 2
    assert world.repo.git("for-each-ref", "--format=%(refname) %(objectname)") == refs_before
    assert world.forge.to_state()["refs"] == {}


def test_three_pushes_with_the_second_seal_failed(world: World, pkis: tuple[ft.Pki, ft.Pki]) -> None:
    """REQ-CI-52: el registro del tercer push cubre el segundo (commits y entradas)."""
    shas = world.build(lambda s: {
        "c0": (c0 := s.commit("main", dict(BASE_FILES), message="init")),
        "c1": s.commit("main", {b"sections/methods/a.csv": b"1\n"}, parent=c0, message="second"),
        "c2": s.commit("main", {b"sections/methods/b.csv": b"2\n"}, message="third"),
    })
    assert world.push(shas["c0"]).rc == 0
    offline = Services.make(pkis, tsa=False, calendars=False)  # sin red: ninguna prueba sale
    failed = world.push(shas["c1"], sealers=offline.sealers())
    assert (failed.rc, failed.outputs()) == (1, {"records": "1", "status": "failed"})
    third = world.push(shas["c2"])
    assert third.rc == 0
    record = read_record_dir(third.folder()).record
    manifest = json.loads(record.manifest or b"{}")
    assert manifest["base"] == shas["c0"]
    assert manifest["commits"] == [shas["c1"], shas["c2"]]
    assert record.openings is not None
    assert sorted(e.path for e in record.openings.entries) == ["sections/methods/a.csv", "sections/methods/b.csv"]
    assert "último commit ya sellado" in third.summary_text()


def test_a_successful_pull_request_target_run_on_the_base_tip_is_not_a_seal(world: World) -> None:
    """REQ-FORGE-69: un run de ``pull_request_target`` lleva la punta de la base y no cuenta."""
    shas = world.build(lambda s: {
        "c0": (c0 := s.commit("main", dict(BASE_FILES), message="init")),
        "c1": s.commit("main", {b"a.txt": b"1\n"}, parent=c0, message="second"),
        "c2": s.commit("main", {b"b.txt": b"2\n"}, message="third"),
    })
    world.push(shas["c0"])
    world.forge.seed_run(ci_seal.WORKFLOW_FILE, shas["c1"], "pull_request_target", "success")
    world.forge.seed_run(ci_seal.WORKFLOW_FILE, shas["c1"], "push", None)  # en curso: tampoco cuenta
    got = world.seal("integration", shas["c2"])
    manifest = json.loads(read_record_dir(got.folder()).record.manifest or b"{}")
    assert manifest["base"] == shas["c0"] and manifest["commits"] == [shas["c1"], shas["c2"]]


def test_plan_flag_on_the_first_push_and_when_plan_md_changes(world: World) -> None:
    """11-trazabilidad §8.2: ``plan: true`` en el primer registro y cuando el rango toca el plan."""
    shas = world.build(lambda s: {
        "c0": (c0 := s.commit("main", dict(BASE_FILES), message="init")),
        "c1": s.commit("main", {b"sections/introduction/introduction.tex": b"% v2\n"}, parent=c0, message="text"),
        "c2": s.commit("main", {b"plan.md": b"# Plan v2\n"}, message="plan"),
        "c3": s.commit("main", {b"memory/constitution.md": b"# v2\n"}, message="constitution"),
    })
    plans = []
    for name in ("c0", "c1", "c2", "c3"):
        result = world.push(shas[name])
        assert result.rc == 0, result.err
        plans.append(read_record_dir(result.folder()).record.plan)
    assert plans == [True, False, True, True]


def test_commit_without_pr_leaves_artifact_and_summary_and_no_comment(world: World) -> None:
    shas = world.build(lambda s: {"c0": s.commit("main", dict(BASE_FILES), message="init")})
    got = world.seal("integration", shas["c0"])
    assert (got.rc, got.outputs()) == (0, {"records": "1", "status": "complete"})
    assert world.forge.calls.count("C") == 0
    names = sorted(p.relative_to(got.folder()).as_posix() for p in got.folder().rglob("*") if p.is_file())
    assert names == [
        "VERIFY.md", "manifest.json", "manifest.json.ots", "open_commitments.py", "openings.json", "record.json",
        "tsa/alpha.chain.pem", "tsa/alpha.ocsp.der", "tsa/alpha.root.pem", "tsa/alpha.tsr",
        "tsa/beta.chain.pem", "tsa/beta.ocsp.der", "tsa/beta.root.pem", "tsa/beta.tsr",
    ]
    text = got.summary_text()
    assert "Sin PR: el registro queda solo en el artefacto" in text
    assert "comentario" not in got.err.lower()


def test_retry_makes_another_artifact_and_comment_without_duplicating_an_attempt(world: World) -> None:
    """REQ-CI-55: otro ``run_attempt`` sella de nuevo; el mismo intento no duplica por ``op_id``."""
    shas = _e1_world(world)
    world.push(shas["c0"])
    first = world.seal("integration", shas["s1"], run_id=9001, attempt=1)
    again = world.seal("integration", shas["s1"], run_id=9001, attempt=1)  # el mismo intento, repetido
    second = world.seal("integration", shas["s1"], run_id=9001, attempt=2)
    assert [r.rc for r in (first, again, second)] == [0, 0, 0]
    found = world.evidence(5)
    assert [r.run_attempt for _b, r in found] == [1, 2]
    assert "ya estaba en el PR #5" in again.summary_text()
    ops = {ci_seal.op_id("integration", shas["s1"], 9001, n) for n in (1, 2)}
    assert len(ops) == 2 and all(any(f"id={op} -->" in b for b, _r in found) for op in ops)
    records = [read_record_dir(r.folder()).record for r in (first, second)]
    assert [r.artifact for r in records] == [
        f"evidence-integration-{shas['s1']}-a1", f"evidence-integration-{shas['s1']}-a2"
    ]
    assert records[0].manifest_sha256 != records[1].manifest_sha256  # sales nuevas


def test_submission_of_a_pr_that_is_not_a_task_or_comes_from_a_fork_is_not_sealed(world: World) -> None:
    shas = world.build(lambda s: {
        "c0": (c0 := s.commit("main", dict(BASE_FILES), message="init")),
        "h": s.commit("lead/fix", {b"a.txt": b"1\n"}, parent=c0, message="fix"),
    })
    world.forge.seed_pull_request(files=[], number=8, head_branch="lead/fix", head_sha=shas["h"], author="ana")
    world.forge.seed_pull_request(files=[], number=9, head_branch="task/4", head_sha=shas["h"], author="x",
                                  same_repo=False)
    for pr, text in ((8, "no es de una tarea"), (9, "viene de un fork")):
        got = world.seal("submission", shas["h"], pr=pr, base_sha=shas["c0"])
        assert (got.rc, got.outputs()) == (0, {"records": "0", "status": "complete"})
        assert text in got.summary_text() and not got.out_dir.exists()
    assert world.forge.calls.count("C") == 0


def test_comment_permission_denied_keeps_the_exit_code(world: World) -> None:
    """REQ-CI-54: si no puede comentar, lo dice el resumen y la salida no cambia."""
    shas = _e1_world(world)
    world.forge.faults.add(Fault(method="create_comment", kind="http_403"))
    got = world.seal("submission", shas["h1"], pr=5, base_sha=shas["c0"])
    assert (got.rc, got.outputs()) == (0, {"records": "1", "status": "complete"})
    assert "No se pudo comentar en el PR #5 (`permission`)" in got.summary_text()
    assert world.evidence(5) == [] and got.folder().is_dir()


def test_without_a_forge(world: World) -> None:
    """Sin forja se sella igual: la integración desde el árbol vacío; la entrega, con ``GITHUB_HEAD_REF``."""
    shas = _e1_world(world)
    integration = world.seal("integration", shas["s1"], forge=False)
    assert (integration.rc, integration.outputs()) == (0, {"records": "1", "status": "complete"})
    record = read_record_dir(integration.folder()).record
    assert json.loads(record.manifest or b"{}")["base"] is None and record.plan is True and record.pr is None
    assert "(`no_token`)" in integration.summary_text()  # sin token: el adaptador real falla en cada consulta
    assert "no se pudo saber qué commit se selló antes" in integration.summary_text()
    submission = world.seal("submission", shas["h1"], pr=5, base_sha=shas["c0"], forge=False,
                            env={"GITHUB_HEAD_REF": "task/3"})
    assert submission.rc == 0
    record = read_record_dir(submission.folder()).record
    assert (record.task, record.pr) == (3, 5)
    assert record.openings is not None and "pr_author" not in {a.role for a in record.openings.actors}
    assert "GITHUB_HEAD_REF" in submission.summary_text()
    lost = world.seal("submission", shas["h1"], pr=5, base_sha=shas["c0"], forge=False)
    assert (lost.rc, lost.outputs()) == (1, {"records": "0", "status": "failed"})
    assert "`pr_unavailable`" in lost.summary_text()


def test_without_a_forge_a_head_ref_that_is_not_a_task_is_not_sealed(world: World) -> None:
    """C-1 (REQ-CI-51 y REQ-CI-55): sin forja, ``GITHUB_HEAD_REF`` decide como la API: si no es
    ``task/<n>`` no hay nada que sellar (0 registros, ``complete``, salida 0). Solo si la
    variable falta o está vacía no se sabe de qué PR se trata (``pr_unavailable``, salida 1)."""
    shas = _e1_world(world)
    for head in ("lead/fix", "task/3/extra", "main"):
        got = world.seal("submission", shas["h1"], pr=5, base_sha=shas["c0"], forge=False,
                         env={"GITHUB_HEAD_REF": head})
        assert (got.rc, got.outputs()) == (0, {"records": "0", "status": "complete"}), head
        assert "El PR #5 no es de una tarea" in got.summary_text() and not got.out_dir.exists()
        assert "`pr_unavailable`" not in got.summary_text()
    for head in ("", "   "):
        lost = world.seal("submission", shas["h1"], pr=5, base_sha=shas["c0"], forge=False,
                          env={"GITHUB_HEAD_REF": head})
        assert (lost.rc, lost.outputs()) == (1, {"records": "0", "status": "failed"}), repr(head)
        text = lost.summary_text()
        assert "(`pr_unavailable`): No se pudo leer el PR y `GITHUB_HEAD_REF` falta o está vacía" in text
    assert world.forge.calls.count("C") == 0  # sin forja no hay comentarios


def test_merged_by_comes_from_get_pull_request(world: World) -> None:
    """R2 (REQ-CI-52; 08 §4.2 y CT-SEAL-01): ``pulls_for_commit`` da el PR integrado sin
    ``merged_by``, como GitHub; ``ci seal`` lo pide a ``get_pull_request``. Si esa consulta
    falla, el registro sale igual, sin ``merged_by``, y el resumen lo anota."""
    shas = _e1_world(world)
    world.push(shas["c0"])
    (listed,) = world.forge.pulls_for_commit(shas["s1"])
    assert listed.number == 5 and listed.merged and listed.merged_by is None
    world.forge.calls.clear()
    got = world.push(shas["s1"])
    assert got.rc == 0, got.err
    assert [c.args for c in world.forge.calls.entries() if c.method == "get_pull_request"] == [{"number": 5}]
    record = read_record_dir(got.folder()).record
    assert record.openings is not None
    assert {a.role: a.value for a in record.openings.actors}["merged_by"] == "ana"
    world.forge.faults.add(Fault(method="get_pull_request", kind="http_5xx"))
    again = world.seal("integration", shas["s1"])
    assert (again.rc, again.outputs()) == (0, {"records": "1", "status": "complete"})
    record = read_record_dir(again.folder()).record
    assert record.openings is not None and record.pr == 5
    assert [a.role for a in record.openings.actors] == ["pr_author", "author", "committer"]
    assert "`get_pull_request` (`unavailable`)" in again.summary_text()


def test_forge_errors_are_noted_and_the_seal_goes_on(world: World) -> None:
    shas = _e1_world(world)
    world.push(shas["c0"])
    world.forge.faults.add(Fault(method="successful_runs", kind="http_5xx"))
    got = world.seal("integration", shas["s1"])
    assert got.rc == 0
    assert json.loads(read_record_dir(got.folder()).record.manifest or b"{}")["base"] is None
    assert "`successful_runs` (`unavailable`)" in got.summary_text()


def test_missing_actions_variables_and_unknown_commit_exit_1(world: World) -> None:
    shas = world.build(lambda s: {"c0": s.commit("main", dict(BASE_FILES), message="init")})
    got = world.seal("integration", shas["c0"], env={"GITHUB_REPOSITORY_ID": "0"})
    assert (got.rc, got.outputs()) == (1, {"records": "0", "status": "failed"})
    assert "GITHUB_REPOSITORY_ID" in got.summary_text() and "`env`" in got.summary_text()
    unknown = world.seal("integration", "f" * 40)
    assert (unknown.rc, unknown.outputs()) == (1, {"records": "0", "status": "failed"})
    assert "No se pudo armar el registro" in unknown.summary_text()
    missing_pr = world.seal("submission", shas["c0"], pr=77, base_sha=shas["c0"])
    assert missing_pr.rc == 1 and "`pr_not_found`" in missing_pr.summary_text()


def test_partial_when_no_tsa_answers(world: World, pkis: tuple[ft.Pki, ft.Pki]) -> None:
    """REQ-CI-55: con las TSA caídas queda OTS: ``partial`` y salida 0."""
    shas = world.build(lambda s: {"c0": s.commit("main", dict(BASE_FILES), message="init")})
    got = world.seal("integration", shas["c0"], sealers=Services.make(pkis, tsa=False).sealers())
    assert (got.rc, got.outputs()) == (0, {"records": "1", "status": "partial"})
    record = read_record_dir(got.folder()).record
    assert [(p.source, p.status) for p in record.proofs] == [("alpha", "error"), ("beta", "error"), ("calendars", "ok")]
    assert "no obtenida (`offline`)" in got.summary_text()


def test_without_the_extra_every_requested_proof_is_an_error(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sin el extra ``evidence``: el registro dice qué se pidió (``missing_extra``) y la salida es 1."""
    from aucove.evidence import tsa
    from aucove.evidence.catalog import TOOL_MISSING

    monkeypatch.setattr(tsa, "stamp", lambda data, **kw: tsa.TsaBatch((), kw.get("wanted", 2), (TOOL_MISSING,)))
    monkeypatch.setattr(ots, "stamp", lambda data, **kw: ots.OtsStamp(
        "skip", "missing_extra", error="missing_extra: falta el extra evidence", warnings=(TOOL_MISSING,)))
    shas = world.build(lambda s: {"c0": s.commit("main", dict(BASE_FILES), message="init")})
    got = world.seal("integration", shas["c0"])
    assert (got.rc, got.outputs()) == (1, {"records": "1", "status": "failed"})
    record = read_record_dir(got.folder()).record
    assert [(p.type, p.source, p.status, p.error) for p in record.proofs] == [
        ("rfc3161", "alpha", "error", "missing_extra"),
        ("rfc3161", "beta", "error", "missing_extra"),
        ("ots", "calendars", "error", "missing_extra: falta el extra evidence"),
    ]
    assert got.summary_text().count("no obtenida (`missing_extra`)") == 3


def test_unwritable_out_exits_1_but_still_comments(world: World) -> None:
    shas = _e1_world(world)
    blocker = world.tmp / "out-1"
    blocker.write_text("no es una carpeta", encoding="utf-8")  # la siguiente corrida usa out-1
    got = world.seal("submission", shas["h1"], pr=5, base_sha=shas["c0"])
    assert (got.rc, got.outputs()) == (1, {"records": "1", "status": "complete"})
    assert "No se pudo escribir la carpeta del registro" in got.summary_text()
    assert len(world.evidence(5)) == 1  # el comentario guarda el registro igual


def test_an_unexpected_failure_still_writes_summary_and_outputs(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: object, **_kw: object) -> object:
        raise RuntimeError("datos privados del PR")

    monkeypatch.setattr(ci_seal, "request_proofs", broken)
    shas = world.build(lambda s: {"c0": s.commit("main", dict(BASE_FILES), message="init")})
    got = world.seal("integration", shas["c0"])
    assert (got.rc, got.outputs()) == (1, {"records": "0", "status": "failed"})
    assert "Error interno al sellar (RuntimeError)" in got.summary_text()
    assert "datos privados" not in got.summary_text() + got.err


def test_summary_has_utc_and_central_mexico_times_in_english_too(tmp_path: Path, services: Services) -> None:
    world = World(tmp_path, services, lang="en")
    shas = world.build(lambda s: {"c0": s.commit("main", dict(BASE_FILES), message="init")})
    text = world.seal("integration", shas["c0"]).summary_text()
    assert text.startswith(f"### {_meta.NAME}/seal-integration\n")
    assert "Sealing time (runner clock): 2026-10-06T12:00:00Z (UTC); 2026-10-06T06:00:00-06:00 (Central Mexico time)" in text
    gen = ft.GEN_TIME.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert f"| RFC 3161 | `alpha` | obtained, with OCSP | {gen} | 2026-10-06T06:00:00-06:00 |" in text
    assert "| OpenTimestamps | `calendars` | obtained, pending on Bitcoin |" in text
    assert "Plan sealed: yes" in text and "first record" in text


def test_comment_line_is_localized_with_both_times(world: World) -> None:
    shas = _e1_world(world)
    world.seal("submission", shas["h1"], pr=5, base_sha=shas["c0"])
    ((body, _record),) = world.evidence(5)
    visible = body.split("\n-->\n", 1)[1]
    assert "2026-10-06T12:00:00Z (UTC)" in visible and "2026-10-06T06:00:00-06:00" in visible
    assert "hora del centro de México" in visible and f"evidence-submission-{shas['h1']}-a1" in visible
    assert "fecha cierta" not in body


VERIFY_TITLES = {"es": "# Cómo verificar este registro de evidencia\n", "en": "# How to verify this evidence record\n"}
SUMMARY_TIMES = {"es": "Hora del sellado (reloj del runner)", "en": "Sealing time (runner clock)"}


@pytest.mark.parametrize(("policy", "env"), [("es", "en"), ("en", "es")])
def test_verify_md_follows_the_policy_and_the_messages_follow_the_env(
    tmp_path: Path, services: Services, policy: str, env: str
) -> None:
    """C-6 (REQ-ARCH-70 y REQ-TRACE-82): ``VERIFY.md`` es un archivo generado y sale en
    ``project.yaml: language`` de la política, nunca en ``<PREFIJO>LANG``; el resumen y la
    línea visible del comentario son mensajes y siguen REQ-ARCH-69."""
    world = World(tmp_path, services, lang=policy)
    shas = _e1_world(world)
    got = world.seal("submission", shas["h1"], pr=5, base_sha=shas["c0"], env={f"{PREFIX}LANG": env})
    assert got.rc == 0, got.err
    assert (got.folder() / "VERIFY.md").read_text(encoding="utf-8").startswith(VERIFY_TITLES[policy])
    assert SUMMARY_TIMES[env] in got.summary_text() and SUMMARY_TIMES[policy] not in got.summary_text()
    ((body, _record),) = world.evidence(5)
    assert SUMMARY_TIMES[env] in body.split("\n-->\n", 1)[1]


def test_verify_md_without_a_readable_policy_is_in_english_even_with_the_env(world: World) -> None:
    """C-6: sin ``project.yaml`` legible, ``VERIFY.md`` va en ``en`` (el idioma por omisión), no en
    el de ``<PREFIJO>LANG``."""
    (world.policy / "project.yaml").write_text("{ no es yaml válido", encoding="utf-8")
    shas = world.build(lambda s: {"c0": s.commit("main", dict(BASE_FILES), message="init")})
    got = world.seal("integration", shas["c0"], env={f"{PREFIX}LANG": "es"})
    assert got.rc == 0, got.err
    assert (got.folder() / "VERIFY.md").read_text(encoding="utf-8").startswith(VERIFY_TITLES["en"])
    assert SUMMARY_TIMES["es"] in got.summary_text()


# -- motivos del resumen con su texto (C-8) -------------------------------------------------


def _literal_errors() -> set[str]:
    """Códigos literales de ``o.error = "..."`` en ``ops/ci_seal.py``."""
    tree = ast.parse(Path(ci_seal.__file__).read_text(encoding="utf-8"))
    return {
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Attribute) and target.attr == "error" for target in node.targets)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


# Motivos de una prueba que no se obtuvo: los de la red (catalog.classify_http_error), los de
# la verificación previa del sello (tsa), revoked, calendars (ots) y los que agrega ci seal.
PROOF_ERRORS: Mapping[str, tuple[str, ...]] = {
    "rfc3161": (
        "http", "timeout", "offline", "tls", "invalid", "rejected", "digest_mismatch", "nonce_mismatch", "policy",
        "chain", "signature", "ess", "revoked", "missing_extra", "internal",
    ),
    "ots": ("calendars", "missing_extra", "internal"),
}


def test_the_reason_codes_of_the_code_are_the_listed_ones() -> None:
    """Las listas de abajo cubren lo que el código puede poner en el resumen."""
    assert _literal_errors() == {"pr_unavailable", "pr_not_found", "env", "git_unavailable", "git_error", "internal"}
    source = (Path(ci_seal.__file__).parent.parent / "evidence" / "tsa.py").read_text(encoding="utf-8")
    token_errors = {
        node.args[0].value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_TokenError"
        and node.args and isinstance(node.args[0], ast.Constant)
    }
    assert token_errors <= set(PROOF_ERRORS["rfc3161"]), sorted(token_errors - set(PROOF_ERRORS["rfc3161"]))


@pytest.mark.parametrize("lang", LANGS)
def test_every_reason_of_the_summary_has_a_text(lang: str) -> None:
    t = translator(lang)  # type: ignore[arg-type]
    for code in (*_literal_errors(), *evidence_manifest.REASONS):
        assert ci_seal.reason_text(t, code, ci_seal.NOT_BUILT_TEXTS) != code, code
    for kind, codes in PROOF_ERRORS.items():
        for code in codes:
            assert ci_seal.reason_text(t, code, ci_seal.PROOF_TEXTS[kind]) != code, (kind, code)
    for code in ("non_utf8", "path_too_long", "gitlink", "actor_not_utf8", "actor_too_long"):
        assert ci_seal.reason_text(t, code, ci_seal.SKIPPED_TEXTS) != code, code
    assert ci_seal.reason_text(t, "no-such-code", ci_seal.NOT_BUILT_TEXTS) == "no-such-code"


def test_summary_reasons_go_with_their_text_and_their_code(world: World, pkis: tuple[ft.Pki, ft.Pki]) -> None:
    """C-8: cada motivo del resumen lleva su texto, y el código estable entre acentos graves."""
    shas = world.build(lambda s: {"c0": s.commit("main", dict(BASE_FILES), message="init")})
    offline = world.seal("integration", shas["c0"], sealers=Services.make(pkis, tsa=False, calendars=False).sealers())
    text = offline.summary_text()
    assert "no obtenida (`offline`): Sin conexión de red." in text
    assert "no obtenida (`calendars`): Respondieron menos calendarios de OpenTimestamps que el mínimo." in text
    unknown = world.seal("integration", "f" * 40)  # rev-list --first-parent falla antes del manifiesto
    assert "(`git_error`): git no pudo leer los objetos del repositorio de `--repo-dir`." in unknown.summary_text()
    world.forge.seed_pull_request(files=[], number=8, head_branch="task/8", head_sha=shas["c0"], author="ana")
    absent = world.seal("submission", "f" * 40, pr=8, base_sha=shas["c0"])
    assert (
        "No se pudo armar el registro del commit `ffffffffffff` (`unknown_commit`): "
        "El commit no está en el repositorio."
    ) in absent.summary_text()
    env = world.seal("integration", shas["c0"], attempt=1000)  # fuera de 1..999
    assert "(`env`): Faltan variables de Actions del run o no son válidas." in env.summary_text()
    missing_pr = world.seal("submission", shas["c0"], pr=77, base_sha=shas["c0"])
    assert "(`pr_not_found`): La forja responde que el PR no existe." in missing_pr.summary_text()


def test_summary_reasons_in_english(tmp_path: Path, services: Services) -> None:
    world = World(tmp_path, services, lang="en")
    shas = world.build(lambda s: {"c0": s.commit("main", dict(BASE_FILES), message="init")})
    world.forge.seed_pull_request(files=[], number=8, head_branch="task/8", head_sha=shas["c0"], author="ana")
    got = world.seal("submission", "e" * 40, pr=8, base_sha=shas["c0"])
    assert (
        "Could not build the record of commit `eeeeeeeeeeee` (`unknown_commit`): "
        "The commit is not in the repository."
    ) in got.summary_text()
    lost = world.seal("integration", shas["c0"], env={"GITHUB_REPOSITORY_ID": ""})
    assert "(`env`): Actions variables of the run are missing or invalid." in lost.summary_text()


def test_an_undecodable_identity_is_left_out_and_the_seal_goes_on(world: World) -> None:
    """11 §8.3: con ``encoding undefined``, o un códec que solo existe en Windows
    (``mbcs``), el actor que no se puede decodificar queda fuera y se informa; ``ci seal``
    sella igual, y en cualquier plataforma da el mismo manifiesto."""
    shas = world.build(lambda s: {
        "c0": s.commit("main", dict(BASE_FILES), message="init", author=b"Jos\xe9 <j@x.invalid>", encoding="undefined"),
        "c1": s.commit("main", {b"a.txt": b"1\n"}, message="mbcs", author=b"Jos\xe9 <j@x.invalid>", encoding="mbcs"),
    })
    for name in ("c0", "c1"):
        got = world.seal("integration", shas[name])
        assert (got.rc, got.outputs()) == (0, {"records": "1", "status": "complete"}), got.err
        record = read_record_dir(got.folder()).record
        assert record.openings is not None and [a.role for a in record.openings.actors] == ["committer"]
        assert (
            "Fuera del manifiesto (1, `actor_not_utf8`): Un actor con una identidad que no se puede decodificar"
            in got.summary_text()
        )


def test_big_record_is_trimmed_in_the_comment_but_complete_in_the_artifact(world: World) -> None:
    """REQ-CI-54: el comentario cabe en GitHub; quita las aperturas y el manifiesto, no las pruebas."""
    files = {f"sections/data/file-{i:04d}.csv".encode(): f"{i}\n".encode() for i in range(450)}
    shas = world.build(lambda s: {"c0": s.commit("main", files, message="data")})
    world.forge.seed_pull_request(files=[], number=6, head_branch="task/6", head_sha=shas["c0"], author="carla",
                                  merged=True, merge_commit_sha=shas["c0"], merged_by="ana")
    got = world.seal("integration", shas["c0"])
    assert got.rc == 0
    ((body, record),) = world.evidence(6)
    assert record.truncated is True and record.openings is None and record.manifest is None
    assert all(p.data is not None for p in record.proofs)  # las pruebas siguen en el comentario
    assert validate("evidence-record", _record_json(body)) == []
    assert len(ci_seal.evidence_json(body) or "") <= COMMENT_LIMIT
    folder = read_record_dir(got.folder()).record
    assert folder.openings is not None and len(folder.openings.entries) == 450 and folder.manifest is not None


def test_comment_json_never_carries_double_dashes(world: World) -> None:
    """00-marco §5.14: dentro del JSON de un marcador no va ``--`` (git en Windows no admite ``>`` en una ruta)."""
    shas = world.build(lambda s: {
        "c0": (c0 := s.commit("main", dict(BASE_FILES), message="init")),
        "h": s.commit("task/2", {b"sections/a--b---c.txt": b"x\n"}, parent=c0, message="dashes"),
    })
    world.forge.seed_pull_request(files=[], number=2, head_branch="task/2", head_sha=shas["h"], author="carla")
    assert world.seal("submission", shas["h"], pr=2, base_sha=shas["c0"]).rc == 0
    ((body, record),) = world.evidence(2)
    payload = ci_seal.evidence_json(body) or ""
    assert "--" not in payload and "<" not in payload and ">" not in payload
    assert record.openings is not None and [e.path for e in record.openings.entries] == ["sections/a--b---c.txt"]


# -- piezas sueltas ---------------------------------------------------------------------


def test_marker_op_id_and_evidence_json() -> None:
    sha = "a" * 40
    marker = ci_seal.evidence_marker("integration", sha, 3)
    assert marker == f"<!-- {_meta.NAME}:ci:evidence v1 kind=integration sha={sha} attempt=3"
    assert ci_seal.op_id("integration", sha, 10, 1) != ci_seal.op_id("integration", sha, 10, 2)
    assert len(ci_seal.op_id("submission", sha, 10, 1)) == 64
    assert ci_seal.evidence_json(f"{marker}\n{{}}\n-->\n\nline\n") == "{}"
    for bad in (f"{marker} -->\n{{}}\n-->", f"x\n{marker}\n{{}}\n-->", f"{marker}\n{{}}\n", "plain"):
        assert ci_seal.evidence_json(bad) is None


def test_sealers_from_env_only_with_testing(tmp_path: Path) -> None:
    hook = tmp_path / "hook.py"
    hook.write_text(HOOK_SOURCE, encoding="utf-8")
    assert ci_seal.sealers_from_env({f"{PREFIX}EVIDENCE_FAKE": str(hook)}) == ci_seal.Sealers()
    got = ci_seal.sealers_from_env({f"{PREFIX}TESTING": "1", f"{PREFIX}EVIDENCE_FAKE": str(hook)})
    assert got.catalog is not None and [e.id for e in got.catalog] == ["alpha", "beta"]
    assert got.transport is not None and got.calendars is None
    got.sleep(15.0)  # sin esperas reales en las pruebas


# -- envoltura ci, subproceso y errores de uso ---------------------------------------------------


def test_seal_is_a_ci_subcommand_with_localized_help() -> None:
    out, err = io.StringIO(), io.StringIO()
    assert cli_ci.run(["--help"], env={}, stdout=out, stderr=err) == 0
    assert "seal" in out.getvalue()
    out = io.StringIO()
    assert cli_ci.run(["seal", "--help"], env={f"{PREFIX}LANG": "es"}, stdout=out, stderr=err) == 0
    assert "cabeza de un PR de tarea" in out.getvalue() and "--repo-dir" in out.getvalue()
    assert "seal" in cli_ci.KNOWN_COMMANDS and "ops.ci_seal" in cli_ci.CI_MODULES
    assert [c.name for c in ci_seal.COMMANDS] == ["seal"]


@pytest.mark.parametrize(
    "extra",
    [
        ["--kind", "submission"],  # sin --pr ni --base-sha
        ["--kind", "submission", "--pr", "5"],
        ["--kind", "integration", "--pr", "5"],
        ["--kind", "integration", "--base-sha", "a" * 40],
        ["--kind", "nope"],
        ["--kind", "integration", "--commit", "abc"],
        ["--kind", "submission", "--pr", "0", "--base-sha", "a" * 40],
    ],
)
def test_usage_errors_exit_3_and_print_nothing(extra: list[str], tmp_path: Path) -> None:
    argv = ["seal", "--policy", str(tmp_path), "--repo-dir", str(tmp_path), "--out", str(tmp_path / "o")]
    if "--commit" not in extra:
        argv += ["--commit", "a" * 40]
    out, err = io.StringIO(), io.StringIO()
    assert cli_ci.run([*argv, *extra], env={}, stdout=out, stderr=err) == 3
    assert out.getvalue() == ""


def _cli_env(world: World, state: Path, hook: Path, **extra: str) -> dict[str, str]:
    return world.env(**{
        f"{PREFIX}TESTING": "1", f"{PREFIX}FORGE_ADAPTER": "fake", f"{PREFIX}FORGE_FAKE_STATE": str(state),
        f"{PREFIX}EVIDENCE_FAKE": str(hook), "GITHUB_RUN_ID": "5150", "GITHUB_RUN_ATTEMPT": "1", **extra,
    })


def test_ci_wrapper_with_the_fake_state_and_the_evidence_hook(world: World) -> None:
    shas = _e1_world(world)
    state, hook = world.tmp / "fake-state.json", world.tmp / "hook.py"
    world.forge.save(state)
    hook.write_text(HOOK_SOURCE, encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    argv = ["seal", *world.argv("submission", shas["h1"], pr=5, base_sha=shas["c0"], out=world.tmp / "o",
                                summary=world.tmp / "s.md")]
    rc = cli_ci.run(argv, env=_cli_env(world, state, hook), stdout=out, stderr=err)
    assert (rc, out.getvalue()) == (0, "records=1\nstatus=complete\n"), err.getvalue()
    comments = FakeForge.load(state).list_comments(5)
    assert len(comments) == 1 and ci_seal.evidence_json(comments[0].body) is not None


def test_ci_seal_in_a_subprocess(world: World) -> None:
    """Como lo ejecuta la autoprueba de D-37: ``<nombre> ci seal`` en otro proceso, con el FakeForge y el
    archivo de ``<PREFIJO>EVIDENCE_FAKE``. Arrancar Python y la PKI de prueba toma un par de segundos."""
    shas = _e1_world(world)
    state, hook = world.tmp / "fake-state.json", world.tmp / "hook.py"
    world.forge.save(state)
    hook.write_text(HOOK_SOURCE, encoding="utf-8")
    env = _cli_env(world, state, hook, PYTHONPATH=os.pathsep.join((str(REPO_ROOT / "src"), str(REPO_ROOT))))
    argv = world.argv("integration", shas["s1"], out=world.tmp / "o", summary=world.tmp / "s.md")
    proc = subprocess.run([sys.executable, "-m", f"{_meta.NAME}.entry", "ci", "seal", *argv], env=env,
                          cwd=world.tmp, capture_output=True, timeout=180, check=False)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert proc.stdout.replace(b"\r\n", b"\n") == b"records=1\nstatus=complete\n"
    ((comment),) = FakeForge.load(state).list_comments(5)
    record = load_comment_json(ci_seal.evidence_json(comment.body) or "")
    assert record.kind == "integration" and [p.status for p in record.proofs] == ["ok", "ok", "ok"]
    assert (world.tmp / "o" / f"integration-{shas['s1']}" / "VERIFY.md").is_file()
