# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Autoprueba de ``seal.yml`` y de ``evidence verify`` (A17 a y b; 00-marco D-37 y D-50 c; 09-ci §7.4).

El escenario es E1 sobre el render ``latex`` en ``es`` y en ``en``
(``template_repo.build_template_ci``); cada prueba trabaja sobre su propia copia de
``origin`` y del estado del fake. Los dos jobs de ``seal.yml`` corren como en Actions: la
condición del job, el directorio de trabajo y el ``env`` del paso se evalúan del render
(``actions``), con los mismos checkouts (en ``submission``, ``base/`` es la rama por defecto
con profundidad 1 y ``pr/``, la cabeza con toda la historia, solo como datos; en
``integration``, un clon completo de ``main``), las variables de Actions y la línea de
``WORKFLOW_COMMANDS``, por subproceso con el lanzador instalado.

Sin red: ``<PREFIJO>TESTING=1`` y ``<PREFIJO>EVIDENCE_FAKE`` nombran un archivo que se genera
aquí (``FAKE_MODULE``) y que da a ``ci seal`` y a ``evidence verify`` la misma PKI de prueba,
con las claves y los certificados en archivos: dos TSA de
``tests/fixtures/evidence/tsa/fake_tsa.py`` (``alpha``, con OCSP, y ``beta``, con CRL) y
cuatro calendarios OTS falsos que responden con una atestación pendiente. Otra copia del
mismo archivo, sin servicios, simula la red caída.

- ``submission`` (A17 a): un registro con OTS por cabeza de PR de E1 (21, 22 y 23), con su
  carpeta, exactamente un comentario ``ci:evidence`` por cabeza que valida contra
  ``evidence-record/v1`` y el resumen del job.
- ``integration`` (A17 a): los PR de E1 se integran uno por uno y cada push se sella con 2
  TSA y OTS desde el último commit sellado (``seed_run`` registra cada run); un push con el
  sello fallido queda cubierto por el siguiente, y ``plan`` vale ``true`` en el primero y
  cuando cambia ``plan.md``.
- Ningún sello escribe en una rama: no cambian las referencias de ``origin`` ni las ramas o
  los commits del fake, y la única escritura en la forja es el comentario (REQ-CI-50,
  REQ-TRACE-81).
- ``evidence verify`` (A17 b) con el lanzador sobre cada registro, con ``--repo`` y
  ``--json``: ``verified`` en la integración (TSA de prueba), ``pending`` en la entrega (OTS)
  y un sobre que valida contra ``result``. Sin ``--offline`` consulta los calendarios; los
  falsos solo atienden ``POST /digest``, así que avisa ``W_NET_OFFLINE``. El PR 21 se cierra
  sin integrar y pierde su rama: su registro se verifica con un clon que trae
  ``refs/pull/*/head``; con un clon común, ``git`` queda en ``skip`` y ``--content`` (los
  archivos del checkout) lo abre igual.

El mismo manifiesto, salvo las sales, con ``core.autocrlf=true`` y sin él lo prueba
``tests/unit/evidence/test_manifest.py::test_autocrlf_checkout_gives_the_same_manifest``, y
el corpus de alteraciones (41 de 41), ``tests/unit/test_evidence_verify.py::
test_the_corpus_is_detected_at_100_percent``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
import json
import shutil
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography.hazmat.primitives import serialization

from aucove import _meta
from aucove.config.schemas import validate_result
from aucove.evidence.record import Record, load_comment_json, read_record_dir
from aucove.forge.fake import FakeForge
from aucove.ops import ci_seal
from aucove.ops.context import Invocation, OpContext
from aucove.ops.registry import REGISTRY, invoke
from tests.e2e.actions import condition, interpolate, step_env
from tests.e2e.conftest import (
    CI_LOGIN,
    PREFIX,
    Launcher,
    Run,
    git,
    seed_git_history,
    seed_pull,
    workflow_command,
)
from tests.e2e.template_repo import (
    INTRO,
    MOVED_NOTATION,
    NOTATION,
    PR_CONTENT,
    PR_PIN,
    PR_RENAME,
    Pr,
    TemplateCi,
    runner_temp,
)
from tests.fixtures.evidence.tsa import fake_tsa as ft
from tests.unit.evidence.support import validate

# Qué líneas de los workflows ejerce este módulo (test_template_ci::test_every_workflow_command_is_exercised).
EXERCISED: tuple[str, ...] = ("ci seal --kind submission", "ci seal --kind integration")

NAME = _meta.NAME
KINDS: tuple[str, ...] = ("submission", "integration")
REPOSITORY_ID = "424242"
TOKEN = "fake-token"
CALENDAR_URI = "https://alice.btc.calendar.opentimestamps.org"  # el de las atestaciones pendientes
REFS_FORMAT = "--format=%(refname) %(objectname)"
SUBMISSION_FILES = frozenset(
    {"VERIFY.md", "manifest.json", "manifest.json.ots", "open_commitments.py", "openings.json", "record.json"}
)
TSA_FILES = frozenset(
    {"tsa/alpha.tsr", "tsa/alpha.chain.pem", "tsa/alpha.root.pem", "tsa/alpha.ocsp.der",
     "tsa/beta.tsr", "tsa/beta.chain.pem", "tsa/beta.root.pem", "tsa/beta.crl.der"}
)
PROOFS_OK = [("rfc3161", "alpha", "ok"), ("rfc3161", "beta", "ok"), ("ots", "calendars", "ok")]
PR_PLAN, PLAN_BRANCH = 26, "lead/plan"  # PR del lead que cambia plan.md
PLAN = "plan.md"

# Dobles para <PREFIJO>EVIDENCE_FAKE (el contrato de ci seal y de evidence verify). Lee la
# configuración de su carpeta para no llevar ningún nombre ni ruta escritos.
FAKE_MODULE = '''\
"""Dobles de ci seal y evidence verify para la autoprueba (generado por tests/e2e/test_template_seal.py).

Lee config.json de su carpeta: el paquete, la ruta de fake_tsa.py y si hay servicios. Con
"online", las TSA de prueba firman con la PKI de pki.json (las mismas claves en cada proceso,
así que evidence verify reconoce lo que selló ci seal) y los calendarios OTS responden con
una atestación pendiente; sin "online", ninguna petición tiene ruta (la red caída).
"""

import importlib
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))


def _fake_tsa():
    name = "_" + CONFIG["package"] + "_e2e_fake_tsa"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, CONFIG["fake_tsa"])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module  # dataclasses busca ahí el módulo de cada clase
        spec.loader.exec_module(module)
    return module


def _pkis(ft):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    def cert(pem):
        return x509.load_pem_x509_certificate(pem.encode("ascii"))

    def key(pem):
        return serialization.load_pem_private_key(pem.encode("ascii"), password=None)

    data = json.loads((HERE / "pki.json").read_text(encoding="utf-8"))
    return [
        ft.Pki(name, cert(p["root"]), key(p["root_key"]), cert(p["ca"]), key(p["ca_key"]), cert(p["tsa"]),
               key(p["tsa_key"]), p["ocsp_url"], p["crl_url"])
        for name, p in data.items()
    ]


def _calendar(request):
    import httpx2
    from opentimestamps.core.notary import PendingAttestation
    from opentimestamps.core.op import OpAppend, OpSHA256
    from opentimestamps.core.serialize import BytesSerializationContext
    from opentimestamps.core.timestamp import Timestamp

    stamp = Timestamp(request.read())
    leaf = stamp.ops.add(OpAppend(bytes([1]) * 8)).ops.add(OpSHA256())
    leaf.attestations.add(PendingAttestation(CONFIG["calendar"]))
    ctx = BytesSerializationContext()
    stamp.serialize(ctx)
    return httpx2.Response(200, content=bytes(ctx.getbytes()))


def catalog():
    module = importlib.import_module(CONFIG["package"] + ".evidence.catalog")
    return module.parse_catalog((HERE / "tsa-catalog.json").read_text(encoding="utf-8"))


def transport():
    ft = _fake_tsa()
    router = ft.Router()
    if CONFIG["online"]:
        ots = importlib.import_module(CONFIG["package"] + ".evidence.ots")
        for pki in _pkis(ft):
            router.routes[pki.tsa_url] = ft.FakeTsa(pki)
            if pki.ocsp_url:
                router.routes[pki.ocsp_url] = ft.ocsp_response(pki)
            if pki.crl_url:
                router.routes[pki.crl_url] = ft.crl(pki)
        for url in ots.DEFAULT_CALENDARS:
            router.routes[url + "/digest"] = _calendar
    return router.transport()
'''


# -- dobles de los servicios de sellado --------------------------------------------------------


@dataclass(frozen=True)
class Fakes:
    online: Path  # archivo de <PREFIJO>EVIDENCE_FAKE con las TSA y los calendarios de prueba
    offline: Path  # el mismo contrato y la misma PKI, sin ningún servicio que responda


def _pem_key(key: Any) -> str:
    return str(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption()).decode("ascii"))


def _pem_cert(cert: Any) -> str:
    return str(cert.public_bytes(serialization.Encoding.PEM).decode("ascii"))


def make_fakes(root: Path) -> Fakes:
    """PKI de prueba en archivos (la misma en el proceso de ``ci seal`` y en el de ``evidence verify``)."""
    pkis = (ft.make_pki("alpha"), ft.make_pki("beta", ocsp=False))
    pki_data = {
        p.name: {
            "root": _pem_cert(p.root), "root_key": _pem_key(p.root_key), "ca": _pem_cert(p.ca),
            "ca_key": _pem_key(p.ca_key), "tsa": _pem_cert(p.tsa), "tsa_key": _pem_key(p.tsa_key),
            "ocsp_url": p.ocsp_url, "crl_url": p.crl_url,
        }
        for p in pkis
    }
    catalog = {"version": 1, "tsa": [p.entry() for p in pkis]}
    files: dict[str, Path] = {}
    for kind, online in (("online", True), ("offline", False)):
        folder = root / kind
        folder.mkdir()
        config = {"package": NAME, "fake_tsa": str(Path(ft.__file__).resolve()), "calendar": CALENDAR_URI,
                  "online": online}
        for name, data in (("config.json", config), ("pki.json", pki_data), ("tsa-catalog.json", catalog)):
            (folder / name).write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8", newline="\n")
        (folder / "evidence_fake.py").write_text(FAKE_MODULE, encoding="utf-8", newline="\n")
        files[kind] = folder / "evidence_fake.py"
    return Fakes(files["online"], files["offline"])


@pytest.fixture(scope="module")
def fakes(tmp_path_factory: pytest.TempPathFactory) -> Fakes:
    return make_fakes(tmp_path_factory.mktemp("evidence-fakes"))


# -- el mundo de una prueba: remoto, forja y runs de seal.yml ------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    """Lo que un sello no debe cambiar: las referencias de ``origin`` y las ramas y commits del fake."""

    origin_refs: str
    forge_refs: Mapping[str, str]
    forge_commits: frozenset[str]
    calls: int


@dataclass(frozen=True)
class Sealed:
    """Un run de un job de ``seal.yml``: la salida de ``ci seal``, el artefacto y el resumen."""

    kind: str
    sha: str
    run_id: int
    run: Run
    repo: Path  # --repo-dir: el checkout del job
    out: Path  # $RUNNER_TEMP/evidence: lo que sube upload-artifact
    artifact: str  # nombre del artefacto, evaluado del paso upload-artifact del render
    summary: Path
    writes: tuple[str, ...]  # escrituras en la forja durante el run

    @property
    def folder(self) -> Path:
        return self.out / f"{self.kind}-{self.sha}"

    def record(self) -> Record:
        return read_record_dir(self.folder).record

    def manifest(self) -> dict[str, Any]:
        data = json.loads(self.record().manifest or b"{}")
        assert isinstance(data, dict)
        return data

    def paths(self) -> list[str]:
        openings = self.record().openings
        assert openings is not None
        return sorted(e.path for e in openings.entries)


@dataclass
class World:
    """Copia propia del remoto y del estado del fake; ``dev`` es un clon de trabajo para integrar."""

    ci: TemplateCi
    launcher: Launcher
    fakes: Fakes
    tmp: Path
    env: dict[str, str]
    workflow: Mapping[str, Any]
    dev: Path
    runs: Iterator[int] = field(default_factory=lambda: itertools.count(7001))

    @property
    def state(self) -> Path:
        return Path(self.env[PREFIX + "FORGE_FAKE_STATE"])

    @property
    def repository(self) -> str:
        return self.ci.project.forge_repo

    def g(self, *args: str) -> str:
        return git(self.dev, self.ci.git_env, *args)

    @contextlib.contextmanager
    def forge(self) -> Iterator[FakeForge]:
        forge = FakeForge.load(self.state)
        yield forge
        forge.save(self.state)

    def edit_state(self, change: Callable[[dict[str, Any]], None]) -> None:
        """Cambia el estado del fake como lo haría la forja fuera de los jobs (integrar, cerrar)."""
        state = json.loads(self.state.read_text(encoding="utf-8"))
        change(state)
        FakeForge.from_state(state).save(self.state)

    def snapshot(self) -> Snapshot:
        state = json.loads(self.state.read_text(encoding="utf-8"))
        refs = git(self.ci.origin, self.ci.git_env, "for-each-ref", REFS_FORMAT)
        return Snapshot(refs, dict(state["refs"]), frozenset(state["commits"]), len(state["calls"]))

    def comments(self, number: int) -> list[tuple[str, Record]]:
        """``(cuerpo, registro)`` de los comentarios ``ci:evidence`` del bot en el PR ``number``."""
        out = []
        for comment in FakeForge.load(self.state).list_comments(number):
            payload = ci_seal.evidence_json(comment.body)
            if payload is not None and comment.author == CI_LOGIN:
                out.append((comment.body, load_comment_json(payload)))
        return out


def make_world(ci: TemplateCi, launcher: Launcher, fakes: Fakes, tmp: Path, *, dev: bool = False) -> World:
    origin = tmp / "origin.git"
    shutil.copytree(ci.origin, origin)
    copy = dataclasses.replace(ci, root=tmp, origin=origin)
    env = copy.env(tmp)
    clone = tmp / "dev"
    if dev:
        git(tmp, ci.git_env, "clone", "-q", "--branch", "main", origin.as_uri(), str(clone))
    workflow = yaml.safe_load((ci.root / "dev" / ".github" / "workflows" / "seal.yml").read_text(encoding="utf-8"))
    return World(copy, launcher, fakes, tmp, env, workflow, clone)


def pr_event(world: World, pr: Pr, base_sha: str) -> dict[str, Any]:
    """Contexto ``github`` de ``pull_request_target`` de un PR del mismo repositorio."""
    return {
        "event_name": "pull_request_target", "repository": world.repository, "sha": base_sha, "token": TOKEN,
        "ref_name": "main", "head_ref": pr.branch, "run_attempt": 1,
        "event": {"pull_request": {
            "number": pr.number,
            "head": {"sha": pr.head_sha, "ref": pr.branch, "repo": {"full_name": world.repository}},
            "base": {"sha": base_sha, "ref": "main"},
        }},
    }


def push_event(world: World, sha: str) -> dict[str, Any]:
    return {"event_name": "push", "repository": world.repository, "sha": sha, "token": TOKEN, "ref_name": "main",
            "head_ref": "", "run_attempt": 1, "event": {"after": sha}}


def seal(world: World, kind: str, github: Mapping[str, Any], *, pr: Pr | None = None,
         hook: Path | None = None) -> Sealed:
    """El job ``kind`` de ``seal.yml`` del render para el evento ``github``: checkouts, línea y entorno.

    Comprueba de paso que el run no escribe en ninguna rama (``Snapshot``).
    """
    jobs = world.workflow["jobs"]
    contexts: dict[str, Any] = {"github": github}
    assert condition(jobs[kind].get("if"), contexts), f"el job {kind} no corre con {github['event_name']}"
    other = KINDS[1 - KINDS.index(kind)]
    assert not condition(jobs[other].get("if"), contexts), f"el job {other} también correría"
    run_id = next(world.runs)
    tmp = world.tmp / f"run-{run_id}"
    rt = runner_temp(tmp)
    ws = tmp / "workspace"
    if kind == "submission":
        assert pr is not None
        ws.mkdir(parents=True)
        world.ci.clone_main(ws / "base", depth=1)  # Política = rama por defecto
        repo = world.ci.checkout_head(ws / "pr", pr)  # Historia del PR como datos
    else:
        repo = world.ci.clone_main(ws)  # fetch-depth: 0 en la rama del push
    (step,) = [s for s in jobs[kind]["steps"] if "ci seal" in str(s.get("run", ""))]
    contexts["runner"] = {"temp": str(rt)}
    assert interpolate(step["working-directory"], contexts) == str(rt)
    # El artefacto que sube el job (también si el sello falla) es la carpeta --out de la línea.
    (upload,) = [s for s in jobs[kind]["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
    assert condition(upload.get("if"), contexts, status="failure")
    assert Path(interpolate(upload["with"]["path"], contexts)) == rt / "evidence"
    artifact = interpolate(upload["with"]["name"], contexts)
    summary = rt / "step-summary.md"
    variables = {
        "GITHUB_WORKSPACE": str(ws), "RUNNER_TEMP": str(rt), "GITHUB_STEP_SUMMARY": str(summary),
        "GITHUB_SHA": github["sha"], "GITHUB_EVENT_NAME": github["event_name"], "GITHUB_REF_NAME": github["ref_name"],
        "GITHUB_HEAD_REF": github["head_ref"], "GITHUB_REPOSITORY": world.repository,
        "GITHUB_REPOSITORY_ID": REPOSITORY_ID, "GITHUB_RUN_ID": str(run_id), "GITHUB_RUN_ATTEMPT": "1",
        **step_env(step, contexts),
    }
    if kind == "submission":
        line = workflow_command("ci seal --kind submission")
    else:
        line = workflow_command("ci seal --kind integration")
    before = world.snapshot()
    run = world.launcher.run_line(line, variables, cwd=rt, env={
        **world.env, **variables, PREFIX + "EVIDENCE_FAKE": str(hook or world.fakes.online),
    })
    after = world.snapshot()
    # REQ-CI-50 y REQ-TRACE-81: ningún sello escribe en una rama, ni en git ni en la forja.
    assert after.origin_refs == before.origin_refs, run.explain()
    assert (after.forge_refs, after.forge_commits) == (before.forge_refs, before.forge_commits)
    state = json.loads(world.state.read_text(encoding="utf-8"))
    writes = tuple(c["method"] for c in state["calls"][before.calls:] if c["kind"] in ("E", "C"))
    assert set(writes) <= {"create_comment"}, writes
    sha = pr.head_sha if pr is not None else str(github["sha"])
    return Sealed(kind, sha, run_id, run, repo, rt / "evidence", artifact, summary, writes)


def assert_outputs(sealed: Sealed, status: str = "complete") -> None:
    """REQ-CI-55: stdout es exactamente ``records`` y ``status``; 0 si hay alguna prueba ``ok``.

    La carpeta ``<kind>-<commit>/`` es lo único que sube el job, y el registro nombra el
    artefacto como el paso upload-artifact del render (``VERIFY.md`` y el comentario lo citan).
    """
    assert sealed.run.stdout == f"records=1\nstatus={status}\n", sealed.run.explain()
    assert sealed.run.returncode == (1 if status == "failed" else 0), sealed.run.explain()
    text = sealed.summary.read_text(encoding="utf-8")
    assert text.startswith(f"### {NAME}/seal-{sealed.kind}\n"), text
    assert [p.name for p in sealed.out.iterdir()] == [sealed.folder.name]
    assert sealed.record().artifact == sealed.artifact == f"evidence-{sealed.kind}-{sealed.sha}-a1"


def assert_comment(world: World, number: int, sealed: Sealed) -> Record:
    """Exactamente un comentario ``ci:evidence`` del run en el PR, que valida contra evidence-record/v1."""
    found = [(body, record) for body, record in world.comments(number)
             if (record.kind, record.commit) == (sealed.kind, sealed.sha)]
    assert len(found) == 1, [r.commit for _b, r in world.comments(number)]
    ((body, record),) = found
    lines = body.split("\n")
    assert lines[0] == ci_seal.evidence_marker(sealed.kind, sealed.sha, 1) and lines[2] == "-->"
    assert validate("evidence-record", json.loads(lines[1])) == []
    assert record.manifest_sha256 == sealed.record().manifest_sha256 and record.run_id == sealed.run_id
    return record


def evidence_files(folder: Path) -> set[str]:
    return {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}


# -- forja e historia fuera de los jobs: abrir, integrar y cerrar PR -----------------------------------------


def open_lead_pull(world: World, number: int, branch: str, path: str) -> str:
    """PR del ``lead`` desde ``branch`` (no es de una tarea) que agrega una línea a ``path``; su cabeza."""
    lead = world.ci.lead
    world.g("checkout", "-q", "-b", branch)
    target = world.dev / path
    target.write_bytes(target.read_bytes() + f"\nLine added by pull request {number}.\n".encode("ascii"))
    world.g("commit", "-q", "--no-verify", "-a", f"--author={lead} <{lead}@example.invalid>", "-m",
            f"Pull request {number}")
    head = world.g("rev-parse", "HEAD")
    world.g("push", "-q", "origin", branch, f"{head}:refs/pull/{number}/head")  # como GitHub
    world.g("checkout", "-q", "main")
    with world.forge() as forge:
        seed_git_history(forge, world.dev, world.ci.git_env, ("--no-walk", head))
        forge.seed_branch(branch, head)
        seed_pull(forge, number=number, author=lead, head_branch=branch, head_sha=head, files=(("modified", path),))
    return head


def merge_pull(world: World, number: int, branch: str) -> str:
    """Integra el PR con un commit de fusión en ``main`` y lo empuja; en el fake, el PR queda integrado."""
    owner = world.repository.split("/")[0]
    world.g("merge", "-q", "--no-ff", "--no-edit", "-m", f"Merge pull request #{number} from {owner}/{branch}",
            f"origin/{branch}")
    sha = world.g("rev-parse", "HEAD")
    world.g("push", "-q", "origin", "main")
    with world.forge() as forge:
        seed_git_history(forge, world.dev, world.ci.git_env, ("--no-walk", sha))
        forge.seed_branch("main", sha)
        forge.clock.advance(60)

    def merged(state: dict[str, Any]) -> None:
        (pull,) = [p for p in state["pulls"] if p["number"] == number]
        now = state["clock"]["now"]
        pull.update(state="closed", merged=True, merged_at=now, closed_at=now, updated_at=now,
                    merge_commit_sha=sha, merged_by=world.ci.lead)

    world.edit_state(merged)
    return sha


def record_run(world: World, sealed: Sealed) -> None:
    """``seal.yml`` en ``push`` terminó: su conclusión alimenta ``successful_runs`` (REQ-CI-52)."""
    with world.forge() as forge:
        conclusion = "success" if sealed.run.returncode == 0 else "failure"
        forge.seed_run(ci_seal.WORKFLOW_FILE, sealed.sha, "push", conclusion)
        forge.clock.advance(60)


def close_and_delete_branch(world: World, pr: Pr) -> None:
    """Cierra el PR sin integrar y borra su rama; como GitHub, el remoto conserva ``refs/pull/<n>/head``."""
    git(world.ci.origin, world.ci.git_env, "update-ref", "-d", f"refs/heads/{pr.branch}")

    def closed(state: dict[str, Any]) -> None:
        (pull,) = [p for p in state["pulls"] if p["number"] == pr.number]
        pull.update(state="closed", closed_at=state["clock"]["now"])
        del state["refs"][f"heads/{pr.branch}"]

    world.edit_state(closed)


# -- verificación (A17 b) --------------------------------------------------------------------------


def arbiter_clone(world: World, dest: Path, *, pull_heads: bool) -> Path:
    """Clon de quien verifica (sin checkout); con ``pull_heads``, también ``refs/pull/*/head``."""
    git(dest.parent, world.ci.git_env, "clone", "-q", "--no-checkout", world.ci.origin_url, str(dest))
    if pull_heads:
        git(dest, world.ci.git_env, "fetch", "-q", "origin", "+refs/pull/*/head:refs/remotes/origin/pull/*/head")
    return dest


def download(sealed: list[Sealed], dest: Path) -> Path:
    """Los artefactos ``evidence-*`` de los runs, juntos en una carpeta (como los bajaría un árbitro)."""
    dest.mkdir(parents=True)
    for item in sealed:
        shutil.copytree(item.folder, dest / item.folder.name)
    return dest


def verify(world: World, target: Path, repo: Path, *, content: Path | None = None) -> tuple[Run, dict[str, Any]]:
    """``evidence verify <ruta> --repo <clon> --json`` por el lanzador, con los dobles de la PKI."""
    args = ["evidence", "verify", str(target), "--repo", str(repo), "--json"]
    if content is not None:
        args += ["--content", str(content)]
    cwd = runner_temp(world.tmp / "verify")
    run = world.launcher.run(args, cwd=cwd, env={**world.env, PREFIX + "EVIDENCE_FAKE": str(world.fakes.online)})
    envelope = json.loads(run.stdout)
    assert validate_result(envelope) == [], envelope
    assert envelope["op"] == "evidence.verify", envelope
    return run, envelope


def records_by_commit(envelope: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    data = envelope["data"]
    found = {str(r["commit"]): r for r in data["records"]}
    assert len(found) == len(data["records"])
    return found


def checks(record: Mapping[str, Any]) -> dict[tuple[str, str | None], str]:
    """``(check, origen) -> estado`` de un registro verificado."""
    out = {(str(c["check"]), c["source"]): str(c["status"]) for c in record["checks"]}
    assert len(out) == len(record["checks"]), record["checks"]
    return out


# -- submission (A17 a y b) --------------------------------------------------------------------------


def test_submission_job_seals_each_pr_head_and_the_records_verify(
    launcher: Launcher, ci: TemplateCi, fakes: Fakes, tmp_path: Path
) -> None:
    world = make_world(ci, launcher, fakes, tmp_path)
    heads = [ci.prs[n] for n in (PR_CONTENT, PR_PIN, PR_RENAME)]
    sealed = [seal(world, "submission", pr_event(world, pr, ci.root_sha), pr=pr) for pr in heads]

    for pr, item in zip(heads, sealed):
        assert_outputs(item)
        assert evidence_files(item.folder) == SUBMISSION_FILES  # sin TSA: la entrega solo pide OTS
        record = item.record()
        assert (record.kind, record.commit, record.pr, record.task) == ("submission", pr.head_sha, pr.number, pr.task)
        assert [(p.type, p.source, p.status) for p in record.proofs] == [("ots", "calendars", "ok")]
        manifest = item.manifest()
        assert (manifest["base"], manifest["commits"]) == (ci.root_sha, [pr.claim_sha, pr.head_sha])  # merge-base
        assert item.paths() == pr.changed and record.plan is False
        openings = record.openings
        assert openings is not None
        assert {a.role: a.value for a in openings.actors}["pr_author"] == ci.lead
        assert item.writes == ("create_comment",)
        comment = assert_comment(world, pr.number, item)
        assert [(p.type, p.status) for p in comment.proofs] == [("ots", "ok")]
    # Exactamente un comentario ci:evidence por cabeza, también después de sellar las demás.
    for pr in heads:
        assert [r.commit for _b, r in world.comments(pr.number)] == [pr.head_sha]

    # A17 b: evidence verify de cada registro, con un clon que trae refs/pull/*/head. El PR 21
    # se cierra sin integrar y pierde su rama: su cabeza solo sigue en refs/pull/21/head.
    closed, closed_seal = heads[0], sealed[0]
    close_and_delete_branch(world, closed)
    artifacts = download(sealed, tmp_path / "artifacts")
    arbiter = arbiter_clone(world, tmp_path / "arbiter", pull_heads=True)
    run, envelope = verify(world, artifacts, arbiter)
    assert run.returncode == 0, run.explain()
    assert (envelope["data"]["verdict"], envelope["data"]["confidence"]) == ("pending", None)
    assert [w["code"] for w in envelope["warnings"]] == ["W_NET_OFFLINE"]  # calendarios consultados, sin respuesta
    found = records_by_commit(envelope)
    assert sorted(found) == sorted(pr.head_sha for pr in heads)
    for record in found.values():
        assert (record["kind"], record["verdict"]) == ("submission", "pending")  # OTS sin red
        assert checks(record) == {("manifest", None): "pass", ("ots", "calendars"): "pending",
                                  ("openings", None): "pass", ("actors", None): "pass", ("git", None): "pass"}

    # Con un clon común, la cabeza del PR cerrado no está: git queda en skip, nunca en fail. Los
    # archivos de la parte (--content, los del checkout del job) abren los compromisos igual.
    plain = arbiter_clone(world, tmp_path / "plain", pull_heads=False)
    assert git(plain, ci.git_env, "cat-file", "-t", closed.head_sha, check=False) == ""
    run, envelope = verify(world, artifacts / closed_seal.folder.name, plain, content=closed_seal.repo)
    assert run.returncode == 0, run.explain()
    ((record),) = envelope["data"]["records"]
    assert record["verdict"] == "pending"
    git_check = next(c for c in record["checks"] if c["check"] == "git")
    assert (git_check["status"], git_check["reason"]) == ("skip", "missing_commit")
    # la parte y la nota de sesión que deja submit
    assert [(c["status"], c["reason"]) for c in record["checks"] if c["check"] == "content"] == [("pass", "raw")] * 2


# -- integration (A17 a y b) ------------------------------------------------------------------------


def test_integration_job_seals_each_push_and_the_records_verify(
    launcher: Launcher, ci: TemplateCi, fakes: Fakes, tmp_path: Path
) -> None:
    world = make_world(ci, launcher, fakes, tmp_path, dev=True)
    lead = ci.lead
    pushes: list[tuple[int | None, Sealed]] = []

    def push(number: int | None, sha: str, *, hook: Path | None = None) -> Sealed:
        sealed = seal(world, "integration", push_event(world, sha), hook=hook)
        record_run(world, sealed)
        pushes.append((number, sealed))
        return sealed

    first = push(None, ci.root_sha)  # el primer push: el commit raíz, sin PR
    p21 = push(PR_CONTENT, merge_pull(world, PR_CONTENT, ci.prs[PR_CONTENT].branch))
    failed = push(PR_PIN, merge_pull(world, PR_PIN, ci.prs[PR_PIN].branch), hook=fakes.offline)  # red caída
    p23 = push(PR_RENAME, merge_pull(world, PR_RENAME, ci.prs[PR_RENAME].branch))
    open_lead_pull(world, PR_PLAN, PLAN_BRANCH, PLAN)
    plan = push(PR_PLAN, merge_pull(world, PR_PLAN, PLAN_BRANCH))

    # Salida y pruebas: 2 TSA y OTS en cada registro; el del push con la red caída, sin ninguna.
    for _number, sealed in pushes:
        assert_outputs(sealed, "failed" if sealed is failed else "complete")
        record = sealed.record()
        assert (record.kind, record.commit) == ("integration", sealed.sha)
        if sealed is failed:
            assert [(p.type, p.source, p.status) for p in record.proofs] == [
                (t, s, "error") for t, s, _ok in PROOFS_OK
            ]
            assert evidence_files(sealed.folder) == SUBMISSION_FILES - {"manifest.json.ots"}
        else:
            assert [(p.type, p.source, p.status) for p in record.proofs] == PROOFS_OK
            assert evidence_files(sealed.folder) == SUBMISSION_FILES | TSA_FILES
            assert [p.revocation.type for p in record.proofs if p.revocation] == ["ocsp", "crl"]

    # Rango: desde el último commit sellado con éxito (REQ-CI-52); el push fallido queda cubierto.
    shas = {name: s.sha for name, s in (("first", first), ("p21", p21), ("failed", failed), ("p23", p23),
                                         ("plan", plan))}
    ranges = [(s.manifest()["base"], s.manifest()["commits"], s.record().plan) for _n, s in pushes]
    assert ranges == [
        (None, [shas["first"]], True),  # el primer registro sella el plan (base nula)
        (shas["first"], [shas["p21"]], False),
        (shas["p21"], [shas["failed"]], False),
        (shas["p21"], [shas["failed"], shas["p23"]], False),  # cubre el push del sello fallido
        (shas["p23"], [shas["plan"]], True),  # cambia plan.md
    ]
    assert first.paths() == sorted(ci.rendered)
    note = {task: f"memory/notes/task-{task}.md" for task in (7, 8, 9)}  # las notas de sesión que deja submit
    assert p21.paths() == sorted([INTRO, note[7]])
    assert failed.paths() == sorted(["project.yaml", note[8]])
    assert p23.paths() == sorted({"project.yaml", note[8], MOVED_NOTATION, NOTATION, note[9]})
    assert set(failed.paths()) <= set(p23.paths())
    assert plan.paths() == [PLAN]
    openings = p23.record().openings
    assert openings is not None and [e.path for e in openings.entries if e.blob is None] == [NOTATION]  # borrado

    # PR y actores: el PR integrado del commit, con pr_author y merged_by.
    assert [(s.record().pr, s.record().task) for _n, s in pushes] == [
        (None, None), (PR_CONTENT, 7), (PR_PIN, 8), (PR_RENAME, 9), (PR_PLAN, None)
    ]
    for number, sealed in pushes:
        openings = sealed.record().openings
        assert openings is not None
        roles = {a.role: a.value for a in openings.actors}
        if number is None:  # sin PR: solo el artefacto y el resumen (REQ-CI-54)
            assert "pr_author" not in roles and "merged_by" not in roles
            assert sealed.writes == ()
            continue
        assert (roles["pr_author"], roles["merged_by"]) == (lead, lead)
        assert sealed.writes == ("create_comment",)
        assert_comment(world, number, sealed)
    for number in (PR_CONTENT, PR_PIN, PR_RENAME, PR_PLAN):
        assert len(world.comments(number)) == 1

    # A17 b: evidence verify de cada registro. Con 2 TSA de prueba, verified y strong; el del
    # sello fallido, sin pruebas de fecha, queda pending.
    artifacts = download([s for _n, s in pushes], tmp_path / "artifacts")
    arbiter = arbiter_clone(world, tmp_path / "arbiter", pull_heads=True)
    run, envelope = verify(world, artifacts, arbiter)
    assert run.returncode == 0, run.explain()
    assert (envelope["data"]["verdict"], envelope["data"]["confidence"]) == ("pending", None)
    assert [w["code"] for w in envelope["warnings"]] == ["W_NET_OFFLINE"]  # calendarios consultados, sin respuesta
    found = records_by_commit(envelope)
    assert sorted(found) == sorted(shas.values())
    sealed_ok = {("manifest", None): "pass", ("tsa", "alpha"): "pass", ("tsa", "beta"): "pass",
                 ("ots", "calendars"): "pending", ("openings", None): "pass", ("actors", None): "pass",
                 ("git", None): "pass"}
    for sha, record in found.items():
        if sha == failed.sha:
            assert (record["verdict"], record["confidence"]) == ("pending", None)
            assert checks(record) == {**sealed_ok, ("tsa", "alpha"): "skip", ("tsa", "beta"): "skip",
                                      ("ots", "calendars"): "skip"}
        else:
            assert (record["verdict"], record["confidence"]) == ("verified", "strong"), record
            assert checks(record) == sealed_ok

    # EV-12 (05 REQ-OPS-66): status, como autora de los PR integrados, copia sus ci:evidence a
    # .aucove/evidence/ del clon de trabajo, y evidence verify da sobre esas copias lo mismo que
    # sobre los artefactos. El primer push no tiene PR ni comentario: no tiene copia.
    FakeForge.load(world.state).as_user(lead).save(world.state)
    when = FakeForge.load(world.state).clock.now() + timedelta(hours=1)
    status = next(s for s in REGISTRY if s.id == "status")
    inv = Invocation(root=str(world.dev), lang="es", json=True, dry_run=False, surface="cli",
                     env=world.env, cwd=str(world.dev), now=lambda: when)
    result = invoke(status, OpContext(inv), **{p.name: p.default for p in status.params})
    assert result.ok, result.error
    copies = world.dev / _meta.CACHE_DIR / "evidence"
    with_pr = {s.sha for number, s in pushes if number is not None}
    assert sorted(p.name for p in copies.iterdir()) == sorted(f"integration-{sha}-a1" for sha in with_pr)
    run, envelope = verify(world, copies, arbiter)
    assert run.returncode == 0, run.explain()
    copied = records_by_commit(envelope)
    assert sorted(copied) == sorted(with_pr)
    for sha, record in copied.items():
        assert (record["verdict"], record["confidence"]) == (found[sha]["verdict"], found[sha]["confidence"])
        assert checks(record) == checks(found[sha])
