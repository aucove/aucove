# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Corpus de alteraciones de registros de evidencia (12-mvp A17 b; 05-operaciones §9.26).

Generador, no archivos sueltos: ``build(dest)`` arma en ``dest`` los registros sanos y una
copia alterada por cada caso de ``CASES``.

- **Repositorio:** una historia fija con ``git fast-import`` (``tests/unit/evidence/support``):
  ``c1`` y ``c2`` en ``main`` y la cabeza ``p1`` de un PR. Con las sales de una fuente
  determinista, los manifiestos salen idénticos byte a byte en cualquier máquina.
- **Sellos:** dos TSA de prueba (``fake_tsa``): ``alpha``, con OCSP, y ``beta``, con CRL,
  más la prueba OTS pendiente y la completa de ``make_ots`` sobre el SHA-256 de cada
  manifiesto. Las claves de la PKI son aleatorias: los bytes de los sellos cambian entre
  corridas, pero los resultados de la verificación no.
- **Registros sanos:** ``integration-<c2>`` (2 TSA y OTS pendiente: ``verified`` y
  ``strong``), ``submission-<p1>`` (OTS pendiente: ``pending``) y su copia local
  ``submission-<p1>-a2`` con la prueba OTS completa (también ``pending``: v0.1 no coteja el
  bloque de Bitcoin, así que solo una TSA lleva a ``verified``).
- **Dobles:** ``evidence_fake.py``, para ``<PREFIJO>EVIDENCE_FAKE`` con ``<PREFIJO>TESTING=1``
  (el contrato de ``ci seal``): ``catalog()`` da el catálogo de las TSA de prueba y
  ``transport()`` el calendario OTS, que completa las pruebas pendientes. Así ``evidence
  verify`` se prueba también por la CLI y en un subproceso, sin red.
- **Casos:** cada ``Case`` copia un registro sano y aplica una alteración (un byte del
  manifiesto, el ``.tsr``, la cadena, el OCSP o la CRL, una apertura, un actor, un blob,
  ``record.json``, la base...). ``expect`` dice qué check la delata: todas deben detectarse
  (100 %). Algunos casos piden ``--repo`` o ``--content``; uno trae en la carpeta de
  ``--content`` un ``.git`` con un filtro ``clean`` que devolvería lo sellado, y que la
  verificación nunca debe ejecutar. Las alteraciones del OCSP o la CRL no invalidan el
  sello (la revocación no va sellada): bajan esa TSA a ``weak``.
- **Firma del proyecto (00-marco D-72):** llaves ``ssh-ed25519`` deterministas (la semilla
  da la llave, y Ed25519 no usa azar: la firma sale igual en cualquier máquina): la vigente
  del proyecto (``KEY_ID``), una anterior que dejó de valer antes de los sellos
  (``OLD_KEY_ID``) y una ajena. ``signed/integration-<c2>-a2`` es el registro de
  integración firmado y sellado sobre la firma (``origin: project`` con ``keys``), y
  ``package/hito-1``, un paquete con los registros sanos, un PDF, ``index.txt`` y
  ``signature/`` (el índice firmado y sellado, como ``evidence sign``). ``keys.yaml`` es la
  lista publicada, para ``--keys``. Los casos de la firma alteran el registro firmado:
  un byte de la firma, otra llave, una llave que ya no valía, una firma sin sellar, el
  manifiesto cambiado después de firmar.
- **Casos del paquete (``PACKAGE_CASES``):** alteraciones del paquete firmado (un archivo,
  el índice, la firma del índice, la llave), que delata su carpeta de firma.

Para generar el corpus a mano: ``python -m tests.fixtures.evidence.corpus.corpus <carpeta>``.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID

from aucove import _meta
from aucove.domain import signing
from aucove.evidence import canon, manifest, record, sshsig, timefmt, tsa
from aucove.evidence import package as pkg
from aucove.evidence.catalog import Catalog, parse_catalog
from aucove.vcs.git import Git
from tests.conftest import GitRepo, isolated_git_env
from tests.fixtures.evidence.ots import make_ots
from tests.fixtures.evidence.tsa import fake_tsa as ft
from tests.unit.evidence.support import Stream, new_repo

REPOSITORY = "org-x/demo"
REPOSITORY_ID = 4242
CREATED = "2026-10-06T11:59:00Z"
OTHER_DIGEST = hashlib.sha256(b"otro manifiesto").digest()
KEY_ID = "corpus-2026"  # la llave vigente del proyecto
OLD_KEY_ID = "corpus-2025"  # la anterior: dejó de valer antes de los sellos
KEY_SEEDS: Mapping[str, str] = {KEY_ID: "corpus: llave del proyecto", OLD_KEY_ID: "corpus: llave anterior"}
OTHER_SEED = "corpus: llave ajena"
PAPER = "paper.pdf"  # un archivo del paquete que no es de un registro

FILES_C1: Mapping[bytes, bytes | None] = {
    b"plan.md": b"# Plan\n\nP-1 Medir.\n",
    b"roles.yaml": b"roles: []\n",
    b"sections/intro/intro.md": b"# Intro\n",
    b"data/a.csv": b"x,y\n1,2\n",
}
FILES_C2: Mapping[bytes, bytes | None] = {
    b"sections/intro/intro.md": b"# Intro\n\nTexto nuevo.\n",
    b"sections/methods/methods.md": "# Métodos\n".encode(),
    b"data/a.csv": None,  # borrado: entrada con c = null
}
FILES_P1: Mapping[bytes, bytes | None] = {
    b"sections/ana/notes.md": b"nota\n",
    b"sections/methods/methods.md": "# Métodos\n\nPaso 1.\n".encode(),
}


def salts(seed: str) -> Callable[[], bytes]:
    """Fuente de sales determinista y sin repeticiones."""
    counter = itertools.count()
    return lambda: hashlib.sha256(f"{seed}-{next(counter)}".encode()).digest()


def ots_proofs(digest: bytes) -> tuple[bytes, bytes, bytes, bytes]:
    """``(pendiente, completa, compromiso del calendario, respuesta del calendario)`` para ``digest``.

    Las arma ``make_ots`` con su semilla fija: la pendiente es la completa recortada donde
    la recibe el calendario, que en ``upgrade`` responde con el resto del árbol.
    """
    detached, calendar_node, _attested = make_ots._complete_tree(digest)  # noqa: SLF001 - generador hermano
    complete = make_ots.serialize(detached)
    pending, commitment = make_ots.cut(complete, make_ots.CALENDAR_DEPTH, make_ots.CALENDAR)
    return pending, complete, commitment, make_ots.serialize(calendar_node)


def pem(cert: Any) -> bytes:
    return bytes(cert.public_bytes(serialization.Encoding.PEM))


# Dobles para ``<PREFIJO>EVIDENCE_FAKE`` (el contrato de ``ci seal``): el catálogo de las TSA
# de prueba y un transporte con las respuestas grabadas del calendario OTS, sin red.
FAKE_MODULE = '''\
"""Dobles de evidence verify y ci seal para las pruebas (generado por el corpus de EV-07)."""

import json
from pathlib import Path

import httpx2

from {package}.evidence.catalog import parse_catalog

HERE = Path(__file__).resolve().parent


def catalog():
    return parse_catalog((HERE / "tsa-catalog.json").read_text(encoding="utf-8"))


def transport():
    routes = json.loads((HERE / "calendar-routes.json").read_text(encoding="utf-8"))

    def handler(request):
        url = str(request.url)
        for prefix, body in routes.items():
            if url.startswith(prefix):
                return httpx2.Response(200, content=bytes.fromhex(body))
        raise httpx2.ConnectError("sin ruta (red simulada caída)", request=request)

    return httpx2.MockTransport(handler)
'''


@dataclass
class Healthy:
    """Registros sanos y lo que hace falta para alterarlos y verificarlos."""

    root: Path  # carpeta con las tres carpetas de registro
    repo: GitRepo  # repositorio con c1, c2 y p1 (para --repo)
    content: Path  # archivos de c2 en sus rutas (para --content)
    catalog_path: Path  # catálogo de TSA de prueba (JSON)
    fake_module: Path  # archivo para <PREFIJO>EVIDENCE_FAKE: catalog() y transport() del calendario
    catalog: Catalog
    pkis: dict[str, ft.Pki]
    sha: dict[str, str]  # c1, c2, p1 -> commit
    integration: Path
    submission: Path
    submission_complete: Path  # la copia local con la prueba OTS completa
    signed: Path  # el registro de integración firmado por el proyecto (D-72)
    package: Path  # paquete del hito: los registros sanos, un PDF, index.txt y signature/
    keys: tuple[signing.EvidenceKey, ...]  # evidence_signing.keys del proyecto
    keys_path: Path  # la lista publicada (YAML), para --keys
    router: ft.Router  # las TSA de prueba, para sellar de nuevo en las alteraciones
    calendar_routes: dict[str, bytes] = field(default_factory=dict)  # upgrade: URL -> respuesta

    def folder(self, which: str) -> Path:
        return {"integration": self.integration, "submission": self.submission, "signed": self.signed}[which]


def _write_files(root: Path, files: Mapping[bytes, bytes | None]) -> None:
    for rel, data in files.items():
        target = root.joinpath(*rel.decode("utf-8").split("/"))
        if data is None:
            target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _rfc3161(proof: tsa.TsaProof, *, subject: str = record.MANIFEST_FILE) -> record.Proof:
    """Prueba del registro desde un sello verificado (la receta de EV-06) de ``subject``."""
    assert proof.status == "ok" and proof.tsr is not None and proof.gen_time is not None, proof
    revocation = record.Revocation(proof.revocation.type, proof.revocation.der) if proof.revocation else None
    return record.Proof.rfc3161(
        proof.source,
        proof.tsr,
        timefmt.format_utc(proof.gen_time, fraction=True),
        revocation=revocation,
        chain_pem=proof.chain_pem,
        root_pem=proof.root_pem,
        subject=subject,
    )


def make_router(pkis: Mapping[str, ft.Pki]) -> ft.Router:
    """Las TSA de prueba, con su OCSP o su CRL."""
    router = ft.Router()
    for pki in pkis.values():
        router.routes[pki.tsa_url] = ft.FakeTsa(pki)
        if pki.ocsp_url:
            router.routes[pki.ocsp_url] = ft.ocsp_response(pki)
        if pki.crl_url:
            router.routes[pki.crl_url] = ft.crl(pki)
    return router


# -- firma del proyecto (00-marco D-72; 11-trazabilidad §8.10) -------------------------------------


def _ed25519(seed: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(seed.encode("utf-8")).digest())


def signer(seed: str) -> sshsig.PrivateKey:
    """Llave privada de ``seed``: siempre la misma, y Ed25519 firma igual en cualquier máquina."""
    text = _ed25519(seed).private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                        serialization.NoEncryption())
    return sshsig.load_private_key(text)


def public_line(seed: str) -> str:
    """``ssh-ed25519 AAAA…`` de la llave de ``seed``, como va en ``project.yaml``."""
    public = _ed25519(seed).public_key()
    return public.public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode("ascii")


def keys_document() -> list[dict[str, str]]:
    """``evidence_signing.keys`` del proyecto del corpus: la vigente y la anterior, que valió
    hasta febrero (los sellos de prueba son de octubre)."""
    return [
        {"id": KEY_ID, "public_key": public_line(KEY_SEEDS[KEY_ID]), "since": "2026-02-01T00:00:00Z"},
        {"id": OLD_KEY_ID, "public_key": public_line(KEY_SEEDS[OLD_KEY_ID]), "since": "2025-01-01T00:00:00Z",
         "until": "2026-02-01T00:00:00Z"},
    ]


def signature_of(data: bytes, seed: str, key_id: str, file: str = record.SIG_FILE) -> record.Signature:
    """Firma SSHSIG de ``data`` con la llave de ``seed``; ``key_id`` va en los metadatos."""
    armored = signer(seed).sign(data)
    got = sshsig.verify(armored, data)
    return record.Signature(file, got.hash, got.fingerprint, key_id, armored)


def stamp(data: bytes, subject: str, catalog: Catalog, router: ft.Router) -> tuple[record.Proof, ...]:
    """Las pruebas de ``data`` (``subject``): las dos TSA de prueba y OTS pendiente."""
    batch = tsa.stamp(data, catalog=catalog, transport=router.transport(), sleep=lambda _s: None)
    assert batch.status == "complete" and [p.source for p in batch.proofs] == ["alpha", "beta"], batch
    pending = ots_proofs(hashlib.sha256(data).digest())[0]
    return (*(_rfc3161(p, subject=subject) for p in batch.proofs), record.Proof.ots(pending, subject=subject))


def sign_record(rec: record.Record, seed: str, key_id: str, catalog: Catalog, router: ft.Router,
                **changes: Any) -> record.Record:
    """``rec`` firmado con la llave de ``seed`` y sellado sobre la firma, como en ``ci seal``
    (REQ-TRACE-91)."""
    assert rec.manifest is not None
    signature = signature_of(rec.manifest, seed, key_id)
    assert signature.data is not None
    return replace(rec, signature=signature, proofs=stamp(signature.data, record.SIG_FILE, catalog, router),
                   **changes)


def write_index(package: Path) -> bytes:
    """``index.txt`` del paquete: la huella de cada archivo, sin las carpetas de firma, en el
    formato de ``sha256sum`` (11-trazabilidad §8.8)."""
    files = sorted(
        p for p in package.rglob("*")
        if p.is_file() and p.relative_to(package).as_posix() != pkg.INDEX_FILE
        and pkg.SIGNATURE_DIR_RE.fullmatch(p.relative_to(package).parts[0]) is None
    )
    data = "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(package).as_posix()}\n"
                   for p in files).encode("utf-8")
    (package / pkg.INDEX_FILE).write_bytes(data)
    return data


def sign_package(package: Path, seed: str, key_id: str, catalog: Catalog, router: ft.Router) -> Path:
    """``signature/`` (o la siguiente) con ``index.txt`` firmado y sellado, como ``evidence sign``."""
    data = (package / pkg.INDEX_FILE).read_bytes()
    signature = signature_of(data, seed, key_id, record.INDEX_SIG_FILE)
    assert signature.data is not None
    folder = pkg.SignatureFolder(hashlib.sha256(data).hexdigest(), len(pkg.parse_index(data)), signature,
                                 stamp(signature.data, record.INDEX_SIG_FILE, catalog, router))
    return pkg.write_signature_dir(package, folder, name=pkg.next_signature_dirname(package))


def build_healthy(dest: Path) -> Healthy:
    """Repositorio, TSA de prueba y los tres registros sanos en ``dest``."""
    repo = new_repo(dest, "repo")
    stream = Stream()
    marks = {
        "c1": stream.commit("main", dict(FILES_C1)),  # type: ignore[arg-type]
        "c2": stream.commit("main", dict(FILES_C2)),  # type: ignore[arg-type]
    }
    marks["p1"] = stream.commit("pr", dict(FILES_P1), parent=marks["c2"])  # type: ignore[arg-type]
    by_mark = stream.run(repo)
    sha = {name: by_mark[mark] for name, mark in marks.items()}
    git = Git(repo.path, env=repo.env)
    integration = manifest.build_integration(
        git, commit=sha["c2"], base=sha["c1"], repository=REPOSITORY, repository_id=REPOSITORY_ID,
        pr=7, task=3, pr_author="ana", merged_by="lead-x", created_at=CREATED, salt=salts("integration"),
    )
    submission = manifest.build_submission(
        git, commit=sha["p1"], base_sha=sha["c2"], pr=8, task=4, repository=REPOSITORY,
        repository_id=REPOSITORY_ID, pr_author="ana", created_at=CREATED, salt=salts("submission"),
    )

    pkis = {"alpha": ft.make_pki("alpha"), "beta": ft.make_pki("beta", ocsp=False)}
    catalog_doc = {"version": 1, "tsa": [pkis["alpha"].entry(), pkis["beta"].entry()]}
    catalog_path = dest / "tsa-catalog.json"
    catalog_path.write_text(json.dumps(catalog_doc, indent=2) + "\n", encoding="utf-8")
    catalog = parse_catalog(catalog_path.read_text(encoding="utf-8"))
    router = make_router(pkis)
    batch = tsa.stamp(integration.data, catalog=catalog, transport=router.transport(), sleep=lambda _s: None)
    assert batch.status == "complete" and [p.source for p in batch.proofs] == ["alpha", "beta"], batch

    root = dest / "records"
    i_pending, _i_complete, i_commitment, i_reply = ots_proofs(bytes.fromhex(integration.sha256))
    s_pending, s_complete, s_commitment, s_reply = ots_proofs(bytes.fromhex(submission.sha256))
    integ = record.Record(
        kind="integration", commit=sha["c2"], pr=7, task=3, plan=integration.plan,
        manifest_sha256=integration.sha256, run_id=101, run_attempt=1,
        proofs=(*(_rfc3161(p) for p in batch.proofs), record.Proof.ots(i_pending)),
        manifest=integration.data, openings=integration.openings,
    )
    sub = record.Record(
        kind="submission", commit=sha["p1"], pr=8, task=4, plan=submission.plan,
        manifest_sha256=submission.sha256, run_id=102, run_attempt=1,
        proofs=(record.Proof.ots(s_pending),), manifest=submission.data, openings=submission.openings,
    )
    complete = record.Record(
        kind="submission", commit=sha["p1"], pr=8, task=4, plan=submission.plan,
        manifest_sha256=submission.sha256, run_id=103, run_attempt=2,
        proofs=(record.Proof.ots(s_complete),), manifest=submission.data, openings=submission.openings,
    )
    content = dest / "content"
    _write_files(content, FILES_C1)
    _write_files(content, FILES_C2)
    calendar = make_ots.CALENDAR
    routes = {
        f"{calendar}/timestamp/{i_commitment.hex()}": i_reply,
        f"{calendar}/timestamp/{s_commitment.hex()}": s_reply,
    }
    (dest / "calendar-routes.json").write_text(
        json.dumps({url: body.hex() for url, body in routes.items()}, indent=2) + "\n", encoding="utf-8"
    )
    fake_module = dest / "evidence_fake.py"
    fake_module.write_text(FAKE_MODULE.format(package=_meta.NAME), encoding="utf-8")
    integration_dir = record.write_record_dir(root, integ)
    submission_dir = record.write_record_dir(root, sub)
    complete_dir = record.write_record_dir(root, complete, name=complete.local_dirname)

    # La firma del proyecto (D-72): su lista de llaves, el registro firmado y el paquete del hito.
    keys_doc = keys_document()
    keys, problems = signing.parse_keys(keys_doc)
    assert not problems, problems
    keys_path = dest / "keys.yaml"
    keys_path.write_text(json.dumps({"keys": keys_doc}, indent=2) + "\n", encoding="utf-8")
    signed = sign_record(integ, KEY_SEEDS[KEY_ID], KEY_ID, catalog, router, run_id=104, run_attempt=2)
    (dest / "signed").mkdir()
    signed_dir = record.write_record_dir(dest / "signed", signed, name=signed.local_dirname)
    package = dest / "package" / "hito-1"
    for folder in (integration_dir, submission_dir):
        shutil.copytree(folder, package / folder.name)
    (package / PAPER).write_bytes(b"%PDF-1.7\n% borrador del hito\n")
    write_index(package)
    sign_package(package, KEY_SEEDS[KEY_ID], KEY_ID, catalog, router)
    return Healthy(
        root=root,
        repo=repo,
        content=content,
        catalog_path=catalog_path,
        fake_module=fake_module,
        catalog=catalog,
        pkis=pkis,
        sha=sha,
        integration=integration_dir,
        submission=submission_dir,
        submission_complete=complete_dir,
        signed=signed_dir,
        package=package,
        keys=keys,
        keys_path=keys_path,
        router=router,
        calendar_routes=routes,
    )


# -- utilidades de las alteraciones ------------------------------------------------------------


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump(path: Path, obj: object) -> None:
    """Mismo formato que ``record.json`` y ``openings.json``: indentado, UTF-8 y salto final."""
    path.write_bytes((json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _edit_json(path: Path, change: Callable[[Any], None]) -> None:
    obj = _load(path)
    change(obj)
    _dump(path, obj)


def _flip_last(path: Path) -> None:
    data = path.read_bytes()
    path.write_bytes(data[:-1] + bytes([data[-1] ^ 0x01]))


def _reseal(folder: Path, data: bytes) -> None:
    """Escribe otro manifiesto y pone su huella en ``record.json`` y ``openings.json``,
    como haría quien quiere que la carpeta parezca coherente (los sellos no cambian)."""
    (folder / record.MANIFEST_FILE).write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    _edit_json(folder / record.RECORD_FILE, lambda o: o.__setitem__("manifest_sha256", digest))
    _edit_json(folder / record.OPENINGS_FILE, lambda o: o.__setitem__("manifest_sha256", digest))


def _manifest(folder: Path) -> dict[str, Any]:
    return dict(canon.loads((folder / record.MANIFEST_FILE).read_bytes()))


def _reseal_obj(folder: Path, change: Callable[[dict[str, Any]], None]) -> None:
    obj = _manifest(folder)
    change(obj)
    _reseal(folder, canon.dumps(obj))


def _digest(folder: Path) -> bytes:
    return hashlib.sha256((folder / record.MANIFEST_FILE).read_bytes()).digest()


# -- alteraciones ------------------------------------------------------------------------------


def _manifest_byte(folder: Path, h: Healthy) -> None:
    data = bytearray((folder / record.MANIFEST_FILE).read_bytes())
    pos = data.index(b'"c":"') + 5
    data[pos] = ord("0") if data[pos] != ord("0") else ord("1")
    (folder / record.MANIFEST_FILE).write_bytes(bytes(data))


def _manifest_newline(folder: Path, h: Healthy) -> None:
    path = folder / record.MANIFEST_FILE
    path.write_bytes(path.read_bytes() + b"\n")


def _manifest_pretty(folder: Path, h: Healthy) -> None:
    _reseal(folder, json.dumps(_manifest(folder), indent=1, sort_keys=True).encode("utf-8"))


def _entry_removed(folder: Path, h: Healthy) -> None:
    _reseal_obj(folder, lambda o: o["entries"].pop(0))


def _entry_duplicated(folder: Path, h: Healthy) -> None:
    _reseal_obj(folder, lambda o: o["entries"].insert(0, dict(o["entries"][0])))


def _entries_unsorted(folder: Path, h: Healthy) -> None:
    def swap(o: dict[str, Any]) -> None:
        o["entries"][0], o["entries"][1] = o["entries"][1], o["entries"][0]

    _reseal_obj(folder, swap)


def _commits_altered(folder: Path, h: Healthy) -> None:
    _reseal_obj(folder, lambda o: o.__setitem__("commits", [h.sha["c1"], h.sha["c2"]]))


def _base_unknown(folder: Path, h: Healthy) -> None:
    """Una base que no existe: antes apagaba el cotejo de commits, blobs y actores."""
    _reseal_obj(folder, lambda o: o.__setitem__("base", "f" * 40))


def _base_not_ancestor(folder: Path, h: Healthy) -> None:
    """La base es un commit del repositorio, pero posterior: la cabeza del PR, hija de c2."""
    _reseal_obj(folder, lambda o: o.__setitem__("base", h.sha["p1"]))


def _actor_dropped(folder: Path, h: Healthy) -> None:
    _reseal_obj(folder, lambda o: o["actors"].pop(0))


def _record_sha(folder: Path, h: Healthy) -> None:
    _edit_json(folder / record.RECORD_FILE, lambda o: o.__setitem__("manifest_sha256", "ab" * 32))


def _record_commit(folder: Path, h: Healthy) -> None:
    def change(o: dict[str, Any]) -> None:
        o["commit"] = h.sha["c1"]
        o["artifact"] = record.artifact_name(o["kind"], h.sha["c1"], o["run_attempt"])

    _edit_json(folder / record.RECORD_FILE, change)


def _record_time(folder: Path, h: Healthy) -> None:
    def change(o: dict[str, Any]) -> None:
        o["proofs"][0]["time_utc"] = "2026-01-01T00:00:00Z"  # alpha: más temprano que el sello

    _edit_json(folder / record.RECORD_FILE, change)


def _record_time_null(folder: Path, h: Healthy) -> None:
    """Sin la hora declarada de un sello obtenido (el esquema la exige; verify, también)."""
    _edit_json(folder / record.RECORD_FILE, lambda o: o["proofs"][0].__setitem__("time_utc", None))


def _record_plan(folder: Path, h: Healthy) -> None:
    _edit_json(folder / record.RECORD_FILE, lambda o: o.__setitem__("plan", not o["plan"]))


def _record_truncated(folder: Path, h: Healthy) -> None:
    path = folder / record.RECORD_FILE
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])


def _tsr_signature(folder: Path, h: Healthy) -> None:
    _flip_last(folder / "tsa" / "alpha.tsr")


def _tsr_nonce(folder: Path, h: Healthy) -> None:
    """Cambia el nonce del ``TSTInfo`` (firmado) sin tocar la longitud del DER."""
    import rfc3161_client

    path = folder / "tsa" / "alpha.tsr"
    data = path.read_bytes()
    nonce = rfc3161_client.decode_timestamp_response(data).tst_info.nonce
    old, new = ft.integer(nonce), ft.integer(nonce ^ 1)
    assert data.count(old) == 1 and len(old) == len(new)
    path.write_bytes(data.replace(old, new))


def _tsr_truncated(folder: Path, h: Healthy) -> None:
    path = folder / "tsa" / "alpha.tsr"
    path.write_bytes(path.read_bytes()[:200])


def _tsr_other_digest(folder: Path, h: Healthy) -> None:
    (folder / "tsa" / "alpha.tsr").write_bytes(ft.FakeTsa(h.pkis["alpha"]).token(OTHER_DIGEST, 7))


def _tsr_other_policy(folder: Path, h: Healthy) -> None:
    token = ft.FakeTsa(h.pkis["alpha"], policy="2.999.9.9").token(_digest(folder), 7)
    (folder / "tsa" / "alpha.tsr").write_bytes(token)


def _tsr_swapped(folder: Path, h: Healthy) -> None:
    a, b = folder / "tsa" / "alpha.tsr", folder / "tsa" / "beta.tsr"
    first, second = a.read_bytes(), b.read_bytes()
    a.write_bytes(second)
    b.write_bytes(first)


def _forged_chain(folder: Path, h: Healthy) -> None:
    """Sello falso de ``beta`` con una intermedia autofirmada en ``chain.pem`` (REQ-TRACE-88).

    La intermedia lleva el nombre de la verdadera; con ``rfc3161-client`` solo, que trata las
    intermedias como anclas, este sello verificaría.
    """
    ca_key, tsa_key = ft.new_key(), ft.new_key()
    fake_ca = ft.make_cert("beta CA", ca_key, None, None, ca=True, valid=(ft.NOT_BEFORE, ft.NOT_AFTER), path_length=0)
    fake_tsa = ft.make_cert(
        "beta TSA", tsa_key, fake_ca, ca_key, ca=False, valid=(ft.NOT_BEFORE, ft.TSA_NOT_AFTER),
        eku=(ExtendedKeyUsageOID.TIME_STAMPING,),
    )
    forger = ft.Pki("beta", fake_ca, ca_key, fake_ca, ca_key, fake_tsa, tsa_key, None, None)
    token = ft.FakeTsa(h.pkis["beta"], signer=forger).token(_digest(folder), 9)
    (folder / "tsa" / "beta.tsr").write_bytes(token)
    (folder / "tsa" / "beta.chain.pem").write_bytes(pem(fake_tsa) + pem(fake_ca))


def _ocsp_other_cert(folder: Path, h: Healthy) -> None:
    alpha = h.pkis["alpha"]
    other = ft.make_cert(
        "alpha otro", ft.new_key(), alpha.ca, alpha.ca_key, ca=False, valid=(ft.NOT_BEFORE, ft.TSA_NOT_AFTER),
        eku=(ExtendedKeyUsageOID.TIME_STAMPING,),
    )
    (folder / "tsa" / "alpha.ocsp.der").write_bytes(ft.ocsp_response(alpha, cert=other))


def _ocsp_byte(folder: Path, h: Healthy) -> None:
    _flip_last(folder / "tsa" / "alpha.ocsp.der")


def _ocsp_removed(folder: Path, h: Healthy) -> None:
    (folder / "tsa" / "alpha.ocsp.der").unlink()


def _crl_other_issuer(folder: Path, h: Healthy) -> None:
    (folder / "tsa" / "beta.crl.der").write_bytes(ft.crl(h.pkis["alpha"]))


def _ots_other_digest(folder: Path, h: Healthy) -> None:
    (folder / record.OTS_FILE).write_bytes(ots_proofs(OTHER_DIGEST)[0])


def _ots_truncated(folder: Path, h: Healthy) -> None:
    path = folder / record.OTS_FILE
    path.write_bytes(path.read_bytes()[:40])


def _opening(change: Callable[[dict[str, Any]], None], index: int = 0) -> Callable[[Path, Healthy], None]:
    def apply(folder: Path, h: Healthy) -> None:
        _edit_json(folder / record.OPENINGS_FILE, lambda o: change(o["entries"][index]))

    return apply


def _deleted_with_content(folder: Path, h: Healthy) -> None:
    def change(o: dict[str, Any]) -> None:
        deleted = next(e for e in o["entries"] if e["blob"] is None)
        donor = next(e for e in o["entries"] if e["blob"] is not None)
        deleted["blob"], deleted["size"] = donor["blob"], donor["size"]

    _edit_json(folder / record.OPENINGS_FILE, change)


def _opening_blob(folder: Path, h: Healthy) -> None:
    """La apertura de una ruta pasa a nombrar el blob de otro archivo."""
    def change(o: dict[str, Any]) -> None:
        present = [e for e in o["entries"] if e["blob"] is not None]
        present[0]["blob"], present[0]["size"] = present[1]["blob"], present[1]["size"]

    _edit_json(folder / record.OPENINGS_FILE, change)


def _openings_other_manifest(folder: Path, h: Healthy) -> None:
    _edit_json(folder / record.OPENINGS_FILE, lambda o: o.__setitem__("manifest_sha256", "cd" * 32))


def _actor(change: Callable[[list[dict[str, Any]]], None]) -> Callable[[Path, Healthy], None]:
    def apply(folder: Path, h: Healthy) -> None:
        _edit_json(folder / record.OPENINGS_FILE, lambda o: change(o["actors"]))

    return apply


def _set_value(actors: list[dict[str, Any]]) -> None:
    next(a for a in actors if a["r"] == "pr_author")["value"] = "eva"


def _set_actor_salt(actors: list[dict[str, Any]]) -> None:
    actors[0]["salt"] = "11" * 32


def _swap_roles(actors: list[dict[str, Any]]) -> None:
    author = next(a for a in actors if a["r"] == "author")
    committer = next(a for a in actors if a["r"] == "committer")
    author["value"], committer["value"] = committer["value"], author["value"]


def _content_modified(content: Path) -> None:
    path = content / "sections" / "intro" / "intro.md"
    path.write_bytes(path.read_bytes() + b"Agregado despues del sello.\n")


# Filtro clean de quien reclama: devuelve los bytes sellados en lugar del archivo presentado.
EVIL_FILTER = '[filter "evil"]\n\tclean = cat .sealed\n'


def _content_filter(content: Path) -> None:
    """La carpeta de ``--content`` trae un ``.git`` propio: un filtro ``clean`` en su
    ``config`` y sus ``.gitattributes``, más el blob sellado en su almacén de objetos. Con la
    configuración y los objetos de la carpeta, el archivo cambiado pasaría por el sellado
    (y el filtro ejecutaría un comando en la máquina de quien verifica)."""
    _content_modified(content)
    repo = GitRepo(content, content.parent / "content-origin.git", isolated_git_env(content.parent / "content-env"))
    repo.git("init", "-q")
    (content / ".sealed").write_bytes(FILES_C2[b"sections/intro/intro.md"] or b"")
    repo.git("hash-object", "-w", "--no-filters", ".sealed")
    with (content / ".git" / "config").open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(EVIL_FILTER)  # a mano: las pruebas nunca ejecutan git config
    (content / ".gitattributes").write_bytes(b"sections/intro/intro.md filter=evil\n")


# -- alteraciones de la firma del proyecto ----------------------------------------------------------


def _rewrite(folder: Path, rec: record.Record) -> None:
    """Reescribe la carpeta del registro con ``rec``, con el mismo nombre."""
    shutil.rmtree(folder)
    record.write_record_dir(folder.parent, rec, name=folder.name)


def _flip_base64(path: Path) -> None:
    """Un carácter de la última línea del cuerpo de una firma ASCII: cambia un byte de la firma."""
    lines = path.read_bytes().split(b"\n")  # [..., última del cuerpo, -----END…-----, ""]
    body = bytearray(lines[-3])
    body[5] = ord("A") if body[5] != ord("A") else ord("B")
    lines[-3] = bytes(body)
    path.write_bytes(b"\n".join(lines))


def _signature_byte(folder: Path, h: Healthy) -> None:
    _flip_base64(folder / record.SIG_FILE)


def _resign(seed: str, key_id: str) -> Callable[[Path, Healthy], None]:
    """Firma el manifiesto con la llave de ``seed`` (``key_id`` en los metadatos) y lo sella de
    nuevo con sellos válidos, como haría quien tiene otra llave o una que ya no vale."""

    def alter(folder: Path, h: Healthy) -> None:
        _rewrite(folder, sign_record(record.read_record_dir(folder).record, seed, key_id, h.catalog, h.router))

    return alter


def _signature_unsealed(folder: Path, h: Healthy) -> None:
    """Una firma válida del proyecto agregada a un registro ya sellado, sin sellos nuevos: las
    pruebas siguen siendo del manifiesto, no de la firma (REQ-TRACE-91)."""
    rec = record.read_record_dir(folder).record
    assert rec.manifest is not None
    signature = signature_of(rec.manifest, KEY_SEEDS[KEY_ID], KEY_ID)
    proofs = tuple(replace(p, subject=record.SIG_FILE) for p in rec.proofs)
    _rewrite(folder, replace(rec, signature=signature, proofs=proofs))


def _signed_manifest_changed(folder: Path, h: Healthy) -> None:
    """El manifiesto cambiado después de firmar, con sus huellas puestas al día: los sellos
    son de la firma, y la firma ya no es de él."""
    _reseal_obj(folder, lambda o: o.__setitem__("created_at", "2026-10-06T12:00:00Z"))


def _signature_metadata(folder: Path, h: Healthy) -> None:
    other = signer(OTHER_SEED).fingerprint
    _edit_json(folder / record.RECORD_FILE, lambda o: o["signature"].__setitem__("key", other))


def _signature_stripped(folder: Path, h: Healthy) -> None:
    """La firma quitada de un registro firmado, para que pase por uno sin firma."""
    rec = record.read_record_dir(folder).record
    proofs = tuple(replace(p, subject=record.MANIFEST_FILE) for p in rec.proofs)
    _rewrite(folder, replace(rec, signature=None, proofs=proofs))


# -- alteraciones del paquete firmado ----------------------------------------------------------------


def _paper_changed(package: Path, h: Healthy) -> None:
    _flip_last(package / PAPER)


def _paper_removed(package: Path, h: Healthy) -> None:
    (package / PAPER).unlink()


def _index_altered(package: Path, h: Healthy) -> None:
    """``paper.pdf`` cambiado y su huella puesta al día en ``index.txt``: el índice ya no es el firmado."""
    _flip_last(package / PAPER)
    write_index(package)


def _index_signature_byte(package: Path, h: Healthy) -> None:
    _flip_base64(package / "signature" / record.INDEX_SIG_FILE)


def _index_resigned(seed: str, key_id: str) -> Callable[[Path, Healthy], None]:
    """El índice firmado de nuevo, con sellos válidos, por la llave de ``seed``."""

    def alter(package: Path, h: Healthy) -> None:
        shutil.rmtree(package / "signature")
        sign_package(package, seed, key_id, h.catalog, h.router)

    return alter


# -- casos ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Expect:
    """Check que delata la alteración: ``(check, source, status, reason)``."""

    check: str
    source: str | None
    status: str
    reason: str


@dataclass(frozen=True)
class Case:
    """Una alteración de un registro sano y lo que la delata."""

    id: str
    what: str  # qué se altera (A17 b: manifiesto, .tsr, cadena, OCSP, apertura, actor, blob)
    expect: tuple[Expect, ...]
    alter: Callable[[Path, Healthy], None] | None = None
    record: str = "integration"  # el registro sano que se altera: integration, submission o signed
    repo: bool = False  # verificar con --repo
    alter_content: Callable[[Path], None] | None = None  # verificar con --content alterado


def _fail(check: str, reason: str, source: str | None = None) -> Expect:
    return Expect(check, source, "fail", reason)


def _weak(source: str, reason: str) -> Expect:
    return Expect("tsa", source, "weak", reason)


CASES: tuple[Case, ...] = (
    # manifiesto
    Case("manifest-byte", "un byte de manifest.json", (_fail("manifest", "sha_mismatch"), _fail("tsa", "digest_mismatch", "alpha"), _fail("ots", "digest_mismatch", "calendars")), _manifest_byte),
    Case("manifest-newline", "salto de línea final en manifest.json", (_fail("manifest", "sha_mismatch"), _fail("tsa", "digest_mismatch", "beta")), _manifest_newline),
    Case("manifest-noncanonical", "manifest.json reescrito con espacios y huellas puestas al día", (_fail("manifest", "not_canonical"), _fail("tsa", "digest_mismatch", "alpha")), _manifest_pretty),
    Case("manifest-entry-removed", "una entrada quitada del manifiesto", (_fail("openings", "mismatch"), _fail("tsa", "digest_mismatch", "alpha"), _fail("ots", "digest_mismatch", "calendars")), _entry_removed),
    Case("manifest-entry-duplicated", "una entrada duplicada en el manifiesto", (_fail("manifest", "entries_duplicate"),), _entry_duplicated),
    Case("manifest-entries-unsorted", "entradas del manifiesto fuera de orden", (_fail("manifest", "entries_unsorted"),), _entries_unsorted),
    Case("manifest-commits", "commits del manifiesto alterados", (_fail("git", "commits_mismatch"), _fail("tsa", "digest_mismatch", "beta")), _commits_altered, repo=True),
    Case("manifest-base-unknown", "una base que no existe en el manifiesto", (_fail("git", "unknown_base"), _fail("tsa", "digest_mismatch", "alpha")), _base_unknown, repo=True),
    Case("manifest-base-not-ancestor", "una base que no es ancestro del commit", (_fail("git", "base_not_ancestor"), _fail("tsa", "digest_mismatch", "beta")), _base_not_ancestor, repo=True),
    Case("manifest-actor-dropped", "un actor quitado del manifiesto", (_fail("actors", "mismatch"), _fail("tsa", "digest_mismatch", "alpha")), _actor_dropped),
    # record.json
    Case("record-sha", "el SHA-256 del manifiesto en record.json", (_fail("manifest", "sha_mismatch"),), _record_sha),
    Case("record-commit", "el commit de record.json", (_fail("manifest", "record_mismatch"),), _record_commit),
    Case("record-time", "la hora declarada de un sello en record.json", (_fail("tsa", "time_mismatch", "alpha"),), _record_time),
    Case("record-time-null", "la hora de un sello obtenido quitada de record.json", (_fail("tsa", "time_mismatch", "alpha"),), _record_time_null),
    Case("record-plan", "la marca plan de record.json", (_fail("git", "plan_mismatch"),), _record_plan, repo=True),
    Case("record-truncated", "record.json cortado a la mitad", (_fail("manifest", "record_invalid"),), _record_truncated),
    # .tsr y cadena
    Case("tsr-signature", "un byte de la firma del .tsr", (_fail("tsa", "signature", "alpha"),), _tsr_signature),
    Case("tsr-nonce", "el nonce del .tsr", (_fail("tsa", "signature", "alpha"),), _tsr_nonce),
    Case("tsr-truncated", ".tsr cortado", (_fail("tsa", "invalid", "alpha"),), _tsr_truncated),
    Case("tsr-other-digest", "el .tsr de otra huella", (_fail("tsa", "digest_mismatch", "alpha"),), _tsr_other_digest),
    Case("tsr-other-policy", "un .tsr con una política fuera del catálogo", (_fail("tsa", "policy", "alpha"),), _tsr_other_policy),
    Case("tsr-swapped", "los .tsr de las dos TSA intercambiados", (_fail("tsa", "chain", "alpha"), _fail("tsa", "chain", "beta")), _tsr_swapped),
    Case("chain-self-signed", "sello falso con una intermedia autofirmada en chain.pem", (_fail("tsa", "chain", "beta"),), _forged_chain),
    # revocación (no va sellada: baja la TSA a weak)
    Case("ocsp-other-cert", "un OCSP de otro certificado", (_weak("alpha", "ocsp_mismatch"),), _ocsp_other_cert),
    Case("ocsp-byte", "un byte de la firma del OCSP", (_weak("alpha", "ocsp_signature"),), _ocsp_byte),
    Case("ocsp-removed", "el OCSP borrado", (_weak("alpha", "no_revocation"),), _ocsp_removed),
    Case("crl-other-issuer", "una CRL de otra emisora", (_weak("beta", "crl_signature"),), _crl_other_issuer),
    # OTS
    Case("ots-other-digest", "un .ots de otro digest", (_fail("ots", "digest_mismatch", "calendars"),), _ots_other_digest),
    Case("ots-truncated", ".ots cortado", (_fail("ots", "invalid", "calendars"),), _ots_truncated),
    # aperturas
    Case("opening-salt", "una apertura con otra sal", (_fail("openings", "mismatch"),), _opening(lambda e: e.__setitem__("salt", "22" * 32))),
    Case("opening-path", "la ruta de una apertura", (_fail("openings", "mismatch"),), _opening(lambda e: e.__setitem__("path", e["path"] + ".bak"))),
    Case("opening-deleted-with-content", "un borrado que la apertura presenta con contenido", (_fail("openings", "mismatch"),), _deleted_with_content),
    Case("opening-blob", "el blob de una apertura", (_fail("git", "blob_mismatch"),), _opening_blob, repo=True),
    Case("openings-other-manifest", "aperturas de otro manifiesto", (_fail("openings", "other_manifest"),), _openings_other_manifest),
    # actores
    Case("actor-value", "el login de un actor", (_fail("actors", "mismatch"),), _actor(_set_value)),
    Case("actor-salt", "la sal de un actor", (_fail("actors", "mismatch"),), _actor(_set_actor_salt)),
    Case("actor-roles-swapped", "autor y committer intercambiados", (_fail("actors", "mismatch"),), _actor(_swap_roles)),
    # blob en disco (--content)
    Case("content-modified", "un archivo cambiado después del sello", (_fail("content", "mismatch"),), alter_content=_content_modified),
    Case("content-filter", "un archivo cambiado y un filtro clean de la carpeta que devuelve el sellado", (_fail("content", "mismatch"),), alter_content=_content_filter),
    # entrega
    Case("submission-manifest-byte", "un byte del manifiesto de una entrega", (_fail("manifest", "sha_mismatch"), _fail("ots", "digest_mismatch", "calendars")), _manifest_byte, record="submission"),
    Case("submission-ots-other-digest", "el .ots de una entrega cambiado por el de otro digest", (_fail("ots", "digest_mismatch", "calendars"),), _ots_other_digest, record="submission"),
    # firma del proyecto (00-marco D-72; 11-trazabilidad §8.10)
    Case("signature-byte", "un byte de la firma del proyecto", (_fail("signature", "bad_signature", KEY_ID), _fail("tsa", "digest_mismatch", "alpha")), _signature_byte, record="signed"),
    Case("signature-other-key", "el manifiesto firmado y sellado con una llave que no es del proyecto", (_fail("signature", "key_unknown", KEY_ID),), _resign(OTHER_SEED, KEY_ID), record="signed"),
    Case("signature-key-expired", "firmado y sellado con una llave del proyecto que ya no valía en la fecha del sello", (_fail("signature", "key_not_valid", OLD_KEY_ID),), _resign(KEY_SEEDS[OLD_KEY_ID], OLD_KEY_ID), record="signed"),
    Case("signature-unsealed", "una firma del proyecto agregada a un registro sellado sin ella", (_fail("tsa", "digest_mismatch", "alpha"), Expect("signature", KEY_ID, "pending", "undated")), _signature_unsealed),
    Case("signature-manifest-changed", "el manifiesto cambiado después de firmar", (_fail("signature", "bad_signature", KEY_ID),), _signed_manifest_changed, record="signed"),
    Case("signature-metadata", "la huella de la llave en record.json", (_fail("signature", "metadata_mismatch", KEY_ID),), _signature_metadata, record="signed"),
    Case("signature-stripped", "la firma quitada para que el registro pase por uno sin firma", (_fail("tsa", "digest_mismatch", "alpha"),), _signature_stripped, record="signed"),
)


@dataclass(frozen=True)
class PackageCase:
    """Una alteración del paquete firmado sano y lo que la delata en su carpeta de firma."""

    id: str
    what: str
    expect: tuple[Expect, ...]
    alter: Callable[[Path, Healthy], None]


PACKAGE_CASES: tuple[PackageCase, ...] = (
    PackageCase("index-file-changed", "un archivo del paquete cambiado después de firmar el índice", (_fail("index", "mismatch"),), _paper_changed),
    PackageCase("index-file-removed", "un archivo del índice borrado del paquete", (Expect("index", None, "skip", "missing_files"),), _paper_removed),
    PackageCase("index-altered", "index.txt puesto al día después de firmarlo", (_fail("index", "changed"), _fail("signature", "bad_signature", KEY_ID)), _index_altered),
    PackageCase("index-signature-byte", "un byte de la firma del índice", (_fail("signature", "bad_signature", KEY_ID), _fail("tsa", "digest_mismatch", "alpha")), _index_signature_byte),
    PackageCase("index-other-key", "el índice firmado y sellado con una llave que no es del proyecto", (_fail("signature", "key_unknown", KEY_ID),), _index_resigned(OTHER_SEED, KEY_ID)),
    PackageCase("index-key-expired", "el índice firmado y sellado con una llave del proyecto que ya no valía", (_fail("signature", "key_not_valid", OLD_KEY_ID),), _index_resigned(KEY_SEEDS[OLD_KEY_ID], OLD_KEY_ID)),
)


@dataclass(frozen=True)
class Built:
    """Un caso ya generado: la carpeta alterada y, si lo pide, la de ``--content``."""

    case: Case
    folder: Path
    content: Path | None


def build_case(case: Case, healthy: Healthy, dest: Path) -> Built:
    """Copia el registro sano de ``case`` en ``dest/<id>/`` y le aplica la alteración."""
    source = healthy.folder(case.record)
    base = dest / case.id
    folder = base / source.name
    shutil.copytree(source, folder)
    if case.alter is not None:
        case.alter(folder, healthy)
    content: Path | None = None
    if case.alter_content is not None:
        content = base / "content"
        shutil.copytree(healthy.content, content)
        case.alter_content(content)
    return Built(case, folder, content)


def build(dest: Path) -> tuple[Healthy, list[Built]]:
    """Registros sanos en ``dest/healthy`` y un caso alterado por elemento de ``CASES``."""
    healthy = build_healthy(dest / "healthy")
    altered = dest / "altered"
    return healthy, [build_case(case, healthy, altered) for case in CASES]


def build_packages(healthy: Healthy, dest: Path) -> dict[str, Path]:
    """Una copia alterada del paquete sano por elemento de ``PACKAGE_CASES``, en ``dest/<id>/``."""
    out: dict[str, Path] = {}
    for case in PACKAGE_CASES:
        target = dest / case.id / healthy.package.name
        shutil.copytree(healthy.package, target)
        case.alter(target, healthy)
        out[case.id] = target
    return out


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "evidence-corpus")
    healthy, cases = build(target)
    packages = build_packages(healthy, target / "packages")
    print(f"{len(cases)} casos de registro y {len(packages)} de paquete en {target}")
