# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Subcomando interno ``ci seal`` (09-ci §7.4, REQ-CI-50 a REQ-CI-56; 11-trazabilidad §8; DC-23).

Lo llaman los dos jobs de ``seal.yml``. Arma un registro de evidencia por run y lo deja
en tres sitios: la carpeta ``<kind>-<commit>/`` en ``--out`` (el artefacto), el
comentario ``ci:evidence`` del PR y el resumen del job. Nunca escribe en una rama
(REQ-TRACE-81): de git solo lee objetos de ``--repo-dir`` (``evidence.manifest``, sin
red) y en la forja solo crea ese comentario.

- ``submission`` (``pull_request_target``): la cabeza ``--commit`` del PR ``--pr`` frente
  a ``merge-base(--base-sha, --commit)``. La tarea sale de la rama ``task/<n>`` del PR y
  ``pr_author``, de la API. Un PR que no es de una tarea, o que viene de un fork, no se
  sella: 0 registros, ``complete`` y salida 0 (REQ-CI-51, REQ-CI-55). Si la API no
  responde, la rama es la de ``GITHUB_HEAD_REF``, con la misma regla: solo si esa variable
  falta o está vacía no hay registro y la salida es 1 (``pr_unavailable``). Pide
  OpenTimestamps.
- ``integration`` (``push`` a la base): desde el commit de primer padre más reciente,
  hasta 100 atrás, que sea ``head_sha`` de un run exitoso de ``seal.yml`` con
  ``event=push`` (``successful_runs``); si no hay ninguno, o la forja no responde, desde
  el árbol vacío: un rango puede sellarse dos veces, pero nunca queda sin sello
  (REQ-CI-52). El PR sale de ``pulls_for_commit``, con ``pr_author``; su ``merged_by``,
  de ``get_pull_request``, porque las listas no lo traen (08 §4.2). Pide las dos primeras
  TSA del catálogo que respondan y OpenTimestamps (REQ-CI-53).

La carpeta lleva ``VERIFY.md`` y ``open_commitments.py`` (REQ-TRACE-82), de
``_data/evidence/``. ``VERIFY.md`` es un archivo generado: va en el idioma de la política
(``project.yaml: language``), nunca en el de ``<PREFIJO>LANG`` (REQ-ARCH-70); la línea
visible del comentario y el resumen son mensajes y siguen REQ-ARCH-69. El comentario es la
primera línea del marcador ``ci:evidence`` con ``kind``, ``sha`` y ``attempt`` (00-marco
§5.14), el JSON de ``record.comment_json`` (que ya recorta y escapa), ``-->`` y una línea
visible con la hora en UTC y en la del centro de México. Su ``op_id`` es el SHA-256 de
``kind``, ``commit``, ``GITHUB_RUN_ID`` y ``GITHUB_RUN_ATTEMPT``: otro intento comenta de
nuevo y el mismo nunca duplica. Sin PR solo quedan el artefacto y el resumen; si no se
puede comentar, el resumen lo dice y la salida no cambia (REQ-CI-54).

Salida: ``records=<k>`` y ``status=complete|partial|failed`` en stdout (REQ-CI-55); 0 si
cada registro tiene al menos una prueba ``ok`` (también sin registros, cuando no había
nada que sellar), 1 si no o si la carpeta no se pudo escribir, y 3 en un error de uso;
nunca 2. El resumen da cada motivo con su texto y, entre acentos graves, su código
estable: los de ``ci seal`` (``ci.seal.reason``), los de ``ManifestError`` y ``Skipped``
(``evidence.manifest_error`` y ``evidence.skipped``) y los de las pruebas
(``ci.seal.proof_reason`` y, si un sello no verificó, ``evidence.check.tsa``).

Con ``<PREFIJO>TESTING=1``, ``<PREFIJO>EVIDENCE_FAKE`` nombra un archivo Python que define
``transport()`` y, si quiere, ``catalog()`` y ``calendars()``: la TSA y los calendarios de
prueba de la autoprueba por subproceso (D-37), sin red. Fuera de las pruebas la variable
se ignora, como la del FakeForge (REQ-FORGE-62). Lo pesado se importa dentro de las
funciones: ``cli/ci.py`` carga este módulo para cualquier subcomando ``ci``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import importlib.util
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TextIO

from .. import _meta
from ..domain.text import same_login
from ..forge.base import ForgeError
from ..i18n import LANGS, Lang, Translator, resolve_lang, translator
from .ci_comment import BOT_LOGIN, append_summary
from .ci_plan import EXIT_ERROR, EXIT_OK, EXIT_USAGE, read_regular_file
from .context import env_flag, github_repository, testing
from .registry import CiCommand
from .result import OpError

if TYPE_CHECKING:
    import argparse

    from ..evidence.catalog import Catalog
    from ..evidence.manifest import BuiltManifest
    from ..evidence.record import Proof, Record
    from ..forge.base import ForgeAdapter, PullRequest
    from ..vcs.git import Git
    from .context import OpContext

__all__ = [
    "COMMANDS",
    "KINDS",
    "NOT_BUILT_TEXTS",
    "OUTPUT_KEYS",
    "PROOF_TEXTS",
    "SKIPPED_TEXTS",
    "WORKFLOW_FILE",
    "Outcome",
    "ProofRow",
    "Sealers",
    "comment_body",
    "evidence_json",
    "evidence_marker",
    "files_lang",
    "kit_files",
    "main",
    "op_id",
    "reason_text",
    "render_summary",
    "render_verify",
    "request_proofs",
    "seal",
    "sealers_from_env",
]

KINDS: tuple[str, ...] = ("submission", "integration")
WORKFLOW_FILE = "seal.yml"  # successful_runs de REQ-CI-52
MARKER_KIND = "evidence"  # ci:evidence (00-marco §5.14)
OUTPUT_KEYS: tuple[str, ...] = ("records", "status")
TSA_WANTED: Mapping[str, int] = {"submission": 0, "integration": 2}  # REQ-CI-53
MAX_HISTORY = 100  # commits de primer padre que se miran para la base (REQ-CI-52)
MAX_ATTEMPT = 999  # el nombre del artefacto admite a1..a999
FAKE_VAR = "EVIDENCE_FAKE"  # <PREFIJO>EVIDENCE_FAKE, solo con <PREFIJO>TESTING=1
EVIDENCE_DATA = Path(__file__).resolve().parent.parent / "_data" / "evidence"
OPEN_SCRIPT_SOURCE = "open_commitments.py"

Status = Literal["complete", "partial", "failed"]
BaseSource = Literal["merge_base", "sealed", "empty", "unknown"]

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_POSITIVE_RE = re.compile(r"[1-9][0-9]{0,15}")
_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_PLACEHOLDER_RE = re.compile(r"\{\{([a-z_][a-z0-9_]*)\}\}")
_BLOCK_RE = re.compile(r"<!-- (?P<name>[a-z-]+) -->\n(?P<body>.*?)<!-- /(?P=name) -->\n", re.S)
_NOTHING = "—"


# -- piezas sin estado ---------------------------------------------------------------


def evidence_marker(kind: str, commit: str, attempt: int) -> str:
    """Primera línea del comentario ``ci:evidence``, sin ``-->`` (forma con JSON de 00-marco §5.14)."""
    return f"<!-- {_meta.NAME}:ci:{MARKER_KIND} v1 kind={kind} sha={commit} attempt={attempt}"


def op_id(kind: str, commit: str, run_id: int, attempt: int) -> str:
    """SHA-256 de ``kind``, ``commit``, ``GITHUB_RUN_ID`` y ``GITHUB_RUN_ATTEMPT`` (REQ-CI-54)."""
    return hashlib.sha256(f"{kind}\n{commit}\n{run_id}\n{attempt}".encode("ascii")).hexdigest()


def _op_marker(op: str) -> str:
    """Marcador ``op`` que el adaptador agrega al final del cuerpo (08 §4.7; 00-marco §5.14)."""
    return f"<!-- {_meta.NAME}:op v1 id={op} -->"


def evidence_json(body: str) -> str | None:
    """JSON ``evidence-record/v1`` de un comentario ``ci:evidence`` (su segunda línea); ``None`` si no lo es.

    El analizador vive en ``evidence.local_copy``, que lo comparte con la copia local de
    REQ-OPS-66; se importa aquí para no cargar ``evidence`` con cualquier subcomando ``ci``.
    """
    from ..evidence.local_copy import evidence_json as parse

    return parse(body)


def _positive(value: object, limit: int) -> int | None:
    if not isinstance(value, str) or _POSITIVE_RE.fullmatch(value) is None:
        return None
    number = int(value)
    return number if number <= limit else None


def _valid_branch(name: object) -> str | None:
    if not isinstance(name, str) or len(name) > 255 or not _BRANCH_RE.fullmatch(name) or ".." in name:
        return None
    return name


def _say(text: str) -> None:
    """Una línea en stderr (stdout es solo para ``records`` y ``status``)."""
    try:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()
    except (OSError, UnicodeError, ValueError):
        pass


# -- clientes de las pruebas -----------------------------------------------------------


def _no_sleep(_seconds: float) -> None:
    return None


@dataclass(frozen=True)
class Sealers:
    """Con qué se piden las pruebas: por omisión, la red, el catálogo empaquetado y los
    calendarios del cliente oficial.

    ``transport`` es un ``httpx2.BaseTransport`` que se pasa tal cual a los clientes de
    ``evidence`` (este módulo no importa ``httpx2``); ``sleep`` reemplaza la espera del
    reintento de las TSA.
    """

    transport: Any = None
    catalog: Catalog | None = None
    calendars: tuple[str, ...] | None = None
    sleep: Callable[[float], None] = time.sleep


def sealers_from_env(env: Mapping[str, str]) -> Sealers:
    """Los clientes reales o, con ``<PREFIJO>TESTING=1`` y ``<PREFIJO>EVIDENCE_FAKE``, los dobles de ese archivo."""
    path = env_flag(env, FAKE_VAR) if testing(env) else None
    if not path:
        return Sealers()
    spec = importlib.util.spec_from_file_location(f"_{_meta.NAME}_evidence_fake", path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    catalog = module.catalog() if hasattr(module, "catalog") else None
    calendars = tuple(module.calendars()) if hasattr(module, "calendars") else None
    return Sealers(transport=module.transport(), catalog=catalog, calendars=calendars, sleep=_no_sleep)


@dataclass(frozen=True)
class ProofRow:
    """Una prueba para el resumen: tipo, origen, estado, código del motivo y, en RFC 3161, ``genTime``."""

    type: str  # 'rfc3161' | 'ots'
    source: str
    status: str  # 'ok' | 'error'
    reason: str
    time_utc: str | None = None
    revocation: str | None = None  # 'ocsp' | 'crl'


def _failure_text(exc: BaseException) -> str:
    return f"internal: {type(exc).__name__}"


def request_proofs(data: bytes, kind: str, sealers: Sealers) -> tuple[list[Proof], list[ProofRow]]:
    """Pruebas de los bytes ``data`` del manifiesto: TSA (solo ``integration``) y OpenTimestamps.

    Cada prueba que no se obtiene queda en el registro con ``status: error``, también sin
    el extra ``evidence`` (``missing_extra``): así el registro dice qué se pidió. Las RFC
    3161 ya llegan verificadas por ``evidence.tsa`` (huella, nonce, política, cadena y OCSP
    o CRL), y como mucho son 3, para que el registro no pase de 4 pruebas.
    """
    from ..evidence import ots, timefmt, tsa
    from ..evidence.catalog import load_catalog
    from ..evidence.record import OTS_SOURCE, Proof, Revocation

    proofs: list[Proof] = []
    rows: list[ProofRow] = []
    wanted = TSA_WANTED[kind]
    if wanted:
        catalog = sealers.catalog if sealers.catalog is not None else load_catalog()
        try:
            batch = tsa.stamp(data, catalog=catalog, wanted=wanted, transport=sealers.transport, sleep=sealers.sleep)
        except Exception as exc:  # noqa: BLE001 - un cliente roto no tumba el sello: queda como error
            for entry in catalog.sealing()[:wanted]:
                proofs.append(Proof.failed("rfc3161", entry.id, _failure_text(exc)))
                rows.append(ProofRow("rfc3161", entry.id, "error", "internal"))
        else:
            if batch.status == "skip":
                for entry in catalog.sealing()[:wanted]:
                    proofs.append(Proof.failed("rfc3161", entry.id, "missing_extra"))
                    rows.append(ProofRow("rfc3161", entry.id, "error", "missing_extra"))
            for got in batch.proofs:
                if got.status == "ok" and got.tsr is not None and got.gen_time is not None:
                    when = timefmt.format_utc(got.gen_time, fraction=True)
                    revocation = Revocation(got.revocation.type, got.revocation.der) if got.revocation else None
                    proofs.append(Proof.rfc3161(
                        got.source, got.tsr, when, revocation=revocation,
                        chain_pem=got.chain_pem, root_pem=got.root_pem,
                    ))
                    rows.append(ProofRow(
                        "rfc3161", got.source, "ok", got.reason, when, got.revocation.type if got.revocation else None,
                    ))
                else:
                    proofs.append(Proof.failed("rfc3161", got.source, got.error or got.reason))
                    rows.append(ProofRow("rfc3161", got.source, "error", got.reason))
    try:
        if sealers.calendars is None:
            stamp = ots.stamp(data, transport=sealers.transport)
        else:
            stamp = ots.stamp(data, transport=sealers.transport, calendars=sealers.calendars)
    except Exception as exc:  # noqa: BLE001
        proofs.append(Proof.failed("ots", OTS_SOURCE, _failure_text(exc)))
        rows.append(ProofRow("ots", OTS_SOURCE, "error", "internal"))
    else:
        if stamp.status == "ok" and stamp.ots is not None:
            proofs.append(Proof.ots(stamp.ots))
            rows.append(ProofRow("ots", OTS_SOURCE, "ok", stamp.reason))
        else:
            proofs.append(Proof.failed("ots", OTS_SOURCE, stamp.error or stamp.reason))
            rows.append(ProofRow("ots", OTS_SOURCE, "error", stamp.reason))
    return proofs, rows


# -- kit del árbitro: VERIFY.md y open_commitments.py (11-trazabilidad §8.6) -------------


def _root_fingerprint(pem: bytes | None) -> str:
    """SHA-256 del DER de la raíz como lo imprime ``openssl x509 -noout -fingerprint -sha256``."""
    if not pem:
        return _NOTHING
    body = "".join(line for line in pem.decode("ascii", "replace").splitlines() if not line.startswith("-----"))
    try:
        der = base64.b64decode(body, validate=True)
    except (binascii.Error, ValueError):
        return _NOTHING
    return ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())


def _tsa_name(source: str, catalog: Catalog | None) -> tuple[str, str]:
    entry = catalog.get(source) if catalog is not None else None
    return (entry.name, entry.fingerprint_source) if entry is not None else (source, _NOTHING)


def _attime(time_utc: str | None) -> str:
    from ..evidence.timefmt import parse_utc

    if not time_utc:
        return _NOTHING
    return str(int(parse_utc(time_utc).timestamp()))


def _tsa_row(proof: Proof, catalog: Catalog | None) -> str:
    name, where = _tsa_name(proof.source, catalog)
    file_sha = hashlib.sha256(proof.root_pem).hexdigest() if proof.root_pem else _NOTHING
    link = f"<{where}>" if where.startswith("https://") else where
    return (
        f"| {name} (`{proof.source}`) | {proof.time_utc or _NOTHING} | `{_attime(proof.time_utc)}` "
        f"| `{file_sha}` | `{_root_fingerprint(proof.root_pem)}` | {link} |"
    )


def _tsa_commands(proof: Proof, catalog: Catalog | None) -> str:
    from ..evidence.record import MANIFEST_FILE, chain_file, root_file, tsr_file

    name, _where = _tsa_name(proof.source, catalog)
    root = root_file(proof.source)
    return "\n".join(
        (
            f"# {name} ({proof.source})",
            f"sha256sum {root}",
            f"openssl x509 -in {root} -noout -fingerprint -sha256",
            f"openssl ts -verify -data {MANIFEST_FILE} -in {tsr_file(proof.source)} -CAfile {root}"
            f" -untrusted {chain_file(proof.source)} -attime {_attime(proof.time_utc)}",
        )
    )


def render_verify(record: Record, lang: str, catalog: Catalog | None = None) -> str:
    """``VERIFY.md`` del registro en ``lang`` (``es`` o ``en``): la guía en lenguaje llano.

    Rellena la plantilla de ``_data/evidence/VERIFY.<lang>.md``: los marcadores ``{{x}}``
    y los bloques ``<!-- tsa -->`` (con sellos RFC 3161) y ``<!-- no-tsa -->`` (sin ellos).
    Las órdenes de OpenSSL van con los archivos y el ``-attime`` de cada sello obtenido.
    """
    template = (EVIDENCE_DATA / f"VERIFY.{lang if lang in LANGS else 'en'}.md").read_text(encoding="utf-8")
    stamps = [p for p in record.proofs if p.type == "rfc3161" and p.status == "ok"]
    values = {
        "name": _meta.NAME,
        "version": _meta.__version__,
        "kind": record.kind,
        "commit": record.commit,
        "attempt": str(record.run_attempt),
        "artifact": record.artifact,
        "manifest_sha256": record.manifest_sha256,
        "tsa_table": "\n".join(_tsa_row(p, catalog) for p in stamps),
        "tsa_commands": "\n\n".join(_tsa_commands(p, catalog) for p in stamps),
    }

    def block(m: re.Match[str]) -> str:
        if m["name"] == "tsa":
            return m["body"] if stamps else ""
        if m["name"] == "no-tsa":
            return "" if stamps else m["body"]
        raise ValueError(f"bloque desconocido en la plantilla de VERIFY.md: {m['name']}")

    return _PLACEHOLDER_RE.sub(lambda m: values[m[1]], _BLOCK_RE.sub(block, template))


def kit_files(record: Record, lang: str, catalog: Catalog | None = None) -> dict[str, bytes]:
    """``VERIFY.md`` y ``open_commitments.py`` para la carpeta del registro (REQ-TRACE-82)."""
    from ..evidence.record import OPEN_SCRIPT, VERIFY_FILE

    return {
        VERIFY_FILE: render_verify(record, lang, catalog).encode("utf-8"),
        OPEN_SCRIPT: (EVIDENCE_DATA / OPEN_SCRIPT_SOURCE).read_bytes(),
    }


# -- política, entorno y forja ------------------------------------------------------------


@dataclass(frozen=True)
class _Policy:
    """Lo que el sello usa de ``--policy``: idioma, rama base y repositorio."""

    readable: bool
    language: str | None = None
    branch: str | None = None
    repository: str | None = None


def read_policy(policy_dir: str) -> _Policy:
    """``project.yaml`` de ``--policy``, leído como datos; si no se puede, valores vacíos (se sella igual)."""
    from ..config.project import parse_project

    data = read_regular_file(Path(policy_dir) / "project.yaml")
    cfg = None
    if data is not None:
        try:
            cfg, _findings = parse_project(data, file="project.yaml")
        except Exception:  # noqa: BLE001 - una política ilegible solo deja los valores por omisión
            cfg = None
    if cfg is None:
        return _Policy(False)
    language = cfg.language if cfg.language in LANGS else None
    repo = cfg.forge_repo if isinstance(cfg.forge_repo, str) else None
    return _Policy(True, language, _valid_branch(cfg.base_branch), github_repository({"GITHUB_REPOSITORY": repo or ""}))


def files_lang(policy: _Policy) -> Lang:
    """Idioma de los archivos que genera el sello (``VERIFY.md``): ``project.yaml: language``
    de la política, o ``en`` si no se pudo leer; nunca ``<PREFIJO>LANG`` (REQ-ARCH-70)."""
    return resolve_lang(cli=None, env={}, project=policy.language)


@dataclass(frozen=True)
class _RunEnv:
    """Datos del run de Actions que van en el registro y en el manifiesto."""

    run_id: int
    attempt: int
    repository: str
    repository_id: int


def read_run_env(env: Mapping[str, str], policy: _Policy) -> _RunEnv | list[str]:
    """``GITHUB_RUN_ID``, ``GITHUB_RUN_ATTEMPT``, ``GITHUB_REPOSITORY_ID`` y el repositorio, o los que faltan."""
    from ..evidence.canon import MAX_SAFE_INTEGER

    run_id = _positive(env.get("GITHUB_RUN_ID"), MAX_SAFE_INTEGER)
    attempt = _positive(env.get("GITHUB_RUN_ATTEMPT"), MAX_ATTEMPT)
    repository_id = _positive(env.get("GITHUB_REPOSITORY_ID"), MAX_SAFE_INTEGER)
    repository = github_repository(env) or policy.repository
    missing = [
        name
        for name, value in (
            ("GITHUB_RUN_ID", run_id),
            ("GITHUB_RUN_ATTEMPT", attempt),
            ("GITHUB_REPOSITORY", repository),
            ("GITHUB_REPOSITORY_ID", repository_id),
        )
        if value is None
    ]
    if missing:
        return missing
    assert run_id is not None and attempt is not None and repository is not None and repository_id is not None
    return _RunEnv(run_id, attempt, repository, repository_id)


class _Forge:
    """La forja del run, perezosa; cada falla queda como nota del resumen y nunca interrumpe el sello."""

    def __init__(self, ctx: OpContext, notes: list[tuple[str, dict[str, object]]]) -> None:
        self._ctx = ctx
        self._notes = notes
        self._forge: ForgeAdapter | None = None
        self._failed = False

    def get(self) -> ForgeAdapter | None:
        if self._forge is None and not self._failed:
            try:
                self._forge = self._ctx.forge()
            except (OpError, ForgeError) as exc:
                self._failed = True
                code = exc.code if isinstance(exc, OpError) else exc.kind
                self._notes.append(("forge_unavailable", {"code": code}))
        return self._forge

    def call(self, method: str, *args: object, **kwargs: object) -> tuple[bool, Any]:
        """``(True, resultado)`` o ``(False, None)`` si no hay forja o respondió con un error."""
        forge = self.get()
        if forge is None:
            return False, None
        try:
            return True, getattr(forge, method)(*args, **kwargs)
        except ForgeError as exc:
            self._notes.append(("forge_error", {"method": method, "kind": exc.kind}))
            return False, None


# -- el registro --------------------------------------------------------------------


@dataclass
class Outcome:
    """Lo que pasó en un run de ``ci seal``: da la salida, el resumen y el comentario."""

    kind: str
    commit: str
    pr: int | None = None
    task: int | None = None
    attempt: int | None = None
    skip: str | None = None  # 'not_task' | 'fork': no había nada que sellar (salida 0)
    error: str | None = None  # código del motivo por el que no hay registro
    built: BuiltManifest | None = None
    base: BaseSource | None = None
    record: Record | None = None
    status: Status = "failed"
    rows: list[ProofRow] = field(default_factory=list)
    folder: Path | None = None
    out_failed: bool = False
    comment: str | None = None  # 'created' | 'unchanged' | 'failed' | 'no_forge' | 'no_pr'
    comment_error: str | None = None
    notes: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    @property
    def records(self) -> int:
        return 0 if self.record is None else 1

    @property
    def printed_status(self) -> Status:
        """``status`` de la salida: sin nada que sellar no falta ninguna prueba (``complete``)."""
        return "complete" if self.skip is not None else self.status

    def exit_code(self) -> int:
        if self.skip is not None:
            return EXIT_OK
        if self.record is None or self.out_failed or self.error is not None:
            return EXIT_ERROR
        return EXIT_OK if any(p.status == "ok" for p in self.record.proofs) else EXIT_ERROR


def _submission_pull(
    args: argparse.Namespace, env: Mapping[str, str], forge: _Forge, o: Outcome
) -> tuple[int, str | None] | None:
    """``(tarea, pr_author)`` del PR ``--pr``; ``None`` si no se sella (``o.skip`` u ``o.error`` quedan fijados).

    Sin forja, la rama es la de ``GITHUB_HEAD_REF`` (como datos) y vale la misma regla que
    con la API: si no es ``task/<n>``, no hay nada que sellar (``not_task``, salida 0;
    REQ-CI-51 y REQ-CI-55). Solo si la variable falta o está vacía no se sabe de qué PR se
    trata: ``pr_unavailable`` y salida 1.
    """
    from .snapshot import task_number

    ok, pull = forge.call("get_pull_request", args.pr)
    if ok and pull is None:
        o.error = "pr_not_found"
        return None
    if pull is None:  # sin forja: la rama del evento, como datos
        head = env.get("GITHUB_HEAD_REF") or ""
        if not head.strip():
            o.error = "pr_unavailable"
            return None
        task = task_number(head)
        if task is None:
            o.skip = "not_task"
            return None
        o.notes.append(("head_ref", {}))
        return task, None
    if not pull.same_repo:
        o.skip = "fork"
        return None
    task = task_number(pull.head_branch)
    if task is None:
        o.skip = "not_task"
        return None
    return task, pull.author or None


def _merged_pull(pulls: list[PullRequest], branch: str | None) -> PullRequest | None:
    """El PR integrado en la base que trajo el commit (el más reciente si hubiera varios)."""
    merged = [p for p in pulls if p.merged and (branch is None or p.base_branch == branch)]
    if not merged:
        return None
    return max(merged, key=lambda p: (p.merged_at.timestamp() if p.merged_at else 0.0, p.number))


def _integration_context(
    git: Git, commit: str, env: Mapping[str, str], policy: _Policy, forge: _Forge, o: Outcome
) -> tuple[str | None, PullRequest | None, str | None]:
    """``(base, PR, merged_by)`` de REQ-CI-52; fija ``o.base`` y las notas.

    El PR integrado sale de ``pulls_for_commit``, que como toda lista de GitHub no trae
    ``merged_by``: ese login sale de ``get_pull_request`` (08 §4.2, CT-SEAL-01). Si esa
    consulta falla, el registro va sin ``merged_by`` y la nota de la forja lo dice.
    """
    from ..evidence.manifest import first_parent_history

    history = first_parent_history(git, commit, MAX_HISTORY)
    branch = policy.branch or _valid_branch(env.get("GITHUB_REF_NAME"))
    base: str | None = None
    if branch is None:
        o.base = "unknown"
        o.notes.append(("no_branch", {}))
    else:
        ok, runs = forge.call("successful_runs", WORKFLOW_FILE, branch, event="push", limit=MAX_HISTORY)
        if ok:
            sealed = {run.head_sha for run in runs}
            base = next((c for c in history if c in sealed), None)
            o.base = "sealed" if base is not None else "empty"
        else:
            o.base = "unknown"
    ok, pulls = forge.call("pulls_for_commit", commit)
    pull = _merged_pull(list(pulls), branch) if ok and pulls else None
    merged_by: str | None = None
    if pull is not None:  # GET pulls/{n}: el único que trae merged_by
        ok, detail = forge.call("get_pull_request", pull.number)
        merged_by = (detail.merged_by or None) if ok and detail is not None else None
    return base, pull, merged_by


def _build(
    args: argparse.Namespace, ctx: OpContext, o: Outcome, policy: _Policy, forge: _Forge
) -> tuple[BuiltManifest, _RunEnv] | None:
    """Manifiesto del run; ``None`` (con ``o.skip`` u ``o.error``) si no hay nada que sellar o no se pudo."""
    from ..evidence import timefmt
    from ..evidence.manifest import ManifestError, build_integration, build_submission
    from ..vcs.git import Git, GitError, GitUnavailable, MissingObject
    from .snapshot import task_number

    env = ctx.inv.env
    run_env = read_run_env(env, policy)
    if isinstance(run_env, list):
        o.error = "env"
        o.notes.append(("env_invalid", {"names": ", ".join(run_env)}))
        return None
    o.attempt = run_env.attempt
    try:
        git = Git(Path(args.repo_dir), env=env, offline=True)
    except GitUnavailable:
        o.error = "git_unavailable"
        return None
    created_at = timefmt.format_utc(ctx.inv.now())
    try:
        if o.kind == "submission":
            found = _submission_pull(args, env, forge, o)
            if found is None:
                return None
            o.task, author = found
            o.base = "merge_base"
            built = build_submission(
                git, commit=args.commit, base_sha=args.base_sha, pr=args.pr, task=o.task,
                repository=run_env.repository, repository_id=run_env.repository_id, pr_author=author,
                created_at=created_at,
            )
        else:
            base, pull, merged_by = _integration_context(git, args.commit, env, policy, forge, o)
            if pull is not None:
                o.pr = pull.number
                o.task = task_number(pull.head_branch) if pull.same_repo else None
            built = build_integration(
                git, commit=args.commit, base=base, repository=run_env.repository,
                repository_id=run_env.repository_id, pr=o.pr, task=o.task,
                pr_author=(pull.author or None) if pull is not None else None, merged_by=merged_by,
                created_at=created_at,
            )
    except ManifestError as exc:
        o.error = exc.reason
        return None
    except (GitError, MissingObject, OSError):
        o.error = "git_error"
        return None
    return built, run_env


def _write_folder(out: Path, record: Record, extra: Mapping[str, bytes]) -> Path:
    """``<kind>-<commit>/`` en ``out``; si ya existe (otro intento en el mismo directorio), con ``-a<n>``."""
    from ..evidence.record import write_record_dir

    try:
        return write_record_dir(out, record, extra=extra)
    except FileExistsError:
        return write_record_dir(out, record, extra=extra, name=record.local_dirname)


def _run(args: argparse.Namespace, ctx: OpContext, o: Outcome, policy: _Policy, sealers: Sealers,
         t: Translator) -> None:
    from ..evidence.catalog import load_catalog
    from ..evidence.record import Record
    from ..evidence.tsa import seal_status

    forge = _Forge(ctx, o.notes)
    found = _build(args, ctx, o, policy, forge)
    if found is None:
        return
    built, run_env = found
    o.built = built
    proofs, o.rows = request_proofs(built.data, o.kind, sealers)
    record = Record(
        kind=o.kind, commit=args.commit, pr=o.pr, task=o.task, plan=built.plan, manifest_sha256=built.sha256,
        run_id=run_env.run_id, run_attempt=run_env.attempt, proofs=tuple(proofs), manifest=built.data,
        openings=built.openings,
    )
    o.record = record
    tsa_ok = sum(p.type == "rfc3161" and p.status == "ok" for p in proofs)
    ots_ok = any(p.type == "ots" and p.status == "ok" for p in proofs)
    o.status = seal_status(tsa_ok, ots_ok, tsa_wanted=TSA_WANTED[o.kind])
    catalog = sealers.catalog
    if catalog is None:
        try:
            catalog = load_catalog()
        except (OSError, ValueError):
            catalog = None
    try:
        o.folder = _write_folder(Path(args.out), record, kit_files(record, files_lang(policy), catalog))
    except OSError:
        o.out_failed = True
    target = o.pr
    if target is None:
        o.comment = "no_pr"
        return
    adapter = forge.get()
    if adapter is None:
        o.comment = "no_forge"
        return
    body = comment_body(record, visible_line(o, t))
    op = op_id(o.kind, record.commit, record.run_id, record.run_attempt)
    o.comment, o.comment_error = _publish(adapter, target, body, op)


# -- comentario ci:evidence (REQ-CI-54) ---------------------------------------------------


def comment_body(record: Record, line: str) -> str:
    """Marcador, JSON ``evidence-record/v1`` recortado, ``-->`` y la línea visible."""
    from ..evidence.record import comment_json

    payload = comment_json(record)
    return f"{evidence_marker(record.kind, record.commit, record.run_attempt)}\n{payload.text}\n-->\n\n{line}\n"


def _times(text: str | None) -> tuple[str, str | None]:
    """``(UTC, hora del centro de México)`` de una hora RFC 3339; la segunda, ``None`` sin base de zonas."""
    from ..evidence.timefmt import parse_utc, times

    if not text:
        return _NOTHING, None
    try:
        got = times(parse_utc(text))
    except ValueError:
        return text, None
    return got.utc_text, got.local_text


def visible_line(o: Outcome, t: Translator) -> str:
    """Línea localizada del comentario, con la hora del sellado en UTC y en la del centro de México."""
    assert o.record is not None and o.built is not None
    record = o.record
    utc, local = _times(str(o.built.manifest.get("created_at") or ""))
    ok = sum(p.status == "ok" for p in record.proofs)
    common = {
        "check": f"{_meta.NAME}/seal-{o.kind}", "record": t(f"ci.seal.kind.{o.kind}"), "sha": record.commit[:12],
        "attempt": record.run_attempt, "status": o.status, "ok": ok, "total": len(record.proofs),
        "utc": utc, "artifact": record.artifact,
    }
    if local is None:
        return t("ci.seal.comment.line_utc", **common)
    return t("ci.seal.comment.line", local=local, **common)


def _publish(forge: ForgeAdapter, number: int, body: str, op: str) -> tuple[str, str | None]:
    """Crea el comentario salvo que este intento ya lo haya creado: ``(estado, tipo de error)``."""
    marker = _op_marker(op)
    try:
        me = forge.identity().login
    except ForgeError:
        me = BOT_LOGIN
    try:
        if any(same_login(c.author, me) and marker in c.body for c in forge.list_comments(number)):
            return "unchanged", None
    except ForgeError:
        pass  # se intenta crear igual: tras una falla ambigua el adaptador busca su marcador op
    try:
        forge.create_comment(number, body, op_id=op)
    except ForgeError as exc:
        return "failed", exc.kind
    return "created", None


# -- resumen del job --------------------------------------------------------------------


# Familias del catálogo (prefijos de clave) con el texto de cada código estable del resumen,
# en el orden en que se buscan: un registro que no se armó (los de ci seal y los de
# ManifestError), una prueba que no se obtuvo (red, calendarios y extra; si el sello no
# verificó, los de evidence verify) y lo que quedó fuera del manifiesto (Skipped).
NOT_BUILT_TEXTS: tuple[str, ...] = ("ci.seal.reason.", "evidence.manifest_error.")
PROOF_TEXTS: Mapping[str, tuple[str, ...]] = {
    "rfc3161": ("ci.seal.proof_reason.", "evidence.check.tsa."),
    "ots": ("ci.seal.proof_reason.", "evidence.check.ots."),
}
SKIPPED_TEXTS: tuple[str, ...] = ("evidence.skipped.",)


def reason_text(t: Translator, code: str, families: Sequence[str]) -> str:
    """Texto del código ``code`` en la primera familia de ``families`` que lo tenga; si ninguna, el código.

    El resumen lo muestra junto al código entre acentos graves, que no se traduce.
    """
    for family in families:
        text = t.get(family + code)
        if text is not None:
            return text
    return code


def _proof_result(row: ProofRow, t: Translator) -> str:
    if row.status != "ok":
        return t("ci.seal.summary.proof_error", reason=row.reason,
                 text=reason_text(t, row.reason, PROOF_TEXTS.get(row.type, ())))
    if row.type == "ots":
        return t("ci.seal.summary.proof_pending")
    if row.revocation is None:
        return t("ci.seal.summary.proof_no_revocation", reason=row.reason)
    return t("ci.seal.summary.proof_ok", revocation=row.revocation.upper())


def _proof_table(rows: list[ProofRow], t: Translator) -> list[str]:
    lines = [t("ci.seal.summary.table"), "|---|---|---|---|---|"]
    for row in rows:
        label = "RFC 3161" if row.type == "rfc3161" else "OpenTimestamps"
        utc, local = _times(row.time_utc) if row.time_utc else (_NOTHING, None)
        lines.append(f"| {label} | `{row.source}` | {_proof_result(row, t)} | {utc} | {local or _NOTHING} |")
    return lines


def _comment_item(o: Outcome, t: Translator) -> str | None:
    if o.comment in (None, "no_pr"):
        return None
    if o.comment == "failed":
        return t("ci.seal.summary.comment_failed", pr=o.pr, kind=o.comment_error or "unknown")
    return t(f"ci.seal.summary.comment_{o.comment}", pr=o.pr)


def _record_lines(o: Outcome, t: Translator) -> list[str]:
    assert o.record is not None and o.built is not None
    record, built = o.record, o.built
    ok = sum(p.status == "ok" for p in record.proofs)
    lines = [
        t("ci.seal.summary.headline", record=t(f"ci.seal.title.{o.kind}"), sha=record.commit[:12],
          attempt=record.run_attempt, status=o.status, ok=ok, total=len(record.proofs)),
        "",
    ]
    utc, local = _times(str(built.manifest.get("created_at") or ""))
    items = [
        t("ci.seal.summary.sealed_at", utc=utc, local=local) if local else t("ci.seal.summary.sealed_at_utc", utc=utc)
    ]
    if record.pr is None:
        items.append(t("ci.seal.summary.no_pr"))
    elif record.task is None:
        items.append(t("ci.seal.summary.pr", pr=record.pr))
    else:
        items.append(t("ci.seal.summary.pr_task", pr=record.pr, task=record.task))
    commits = t.plural("ci.seal.summary.commits", len(built.manifest.get("commits") or ()))
    entries = t.plural("ci.seal.summary.entries", len(built.manifest.get("entries") or ()))
    items.append(t(f"ci.seal.summary.base_{o.base or 'unknown'}", base=(built.base or "")[:12], commits=commits,
                   entries=entries))
    items.append(t("ci.seal.summary.plan_yes" if record.plan else "ci.seal.summary.plan_no"))
    items.append(t("ci.seal.summary.manifest", sha256=record.manifest_sha256))
    if o.folder is not None:
        items.append(t("ci.seal.summary.artifact", artifact=record.artifact, folder=o.folder.name))
    else:
        items.append(t("ci.seal.summary.out_failed", artifact=record.artifact))
    skipped = Counter(s.reason for s in built.skipped)
    for reason, n in sorted(skipped.items()):
        items.append(t("ci.seal.summary.skipped", n=n, reason=reason, text=reason_text(t, reason, SKIPPED_TEXTS)))
    comment = _comment_item(o, t)
    if comment is not None:
        items.append(comment)
    lines += [f"- {item}" for item in items]
    lines += ["", f"#### {t('ci.seal.summary.proofs')}", "", *_proof_table(o.rows, t)]
    return lines


def render_summary(o: Outcome, t: Translator) -> str:
    """Resumen del job (``GITHUB_STEP_SUMMARY``) en Markdown localizado; horas en UTC y del centro de México."""
    lines = [f"### {_meta.NAME}/seal-{o.kind}", ""]
    if o.skip is not None:
        lines.append(t(f"ci.seal.summary.skip_{o.skip}", pr=o.pr))
    elif o.record is None:
        code = o.error or "unknown"
        lines.append(t("ci.seal.summary.not_built", sha=o.commit[:12], reason=code,
                       text=reason_text(t, code, NOT_BUILT_TEXTS)))
    else:
        lines += _record_lines(o, t)
    if o.notes:
        lines.append("")
        lines += [f"- {t(f'ci.seal.note.{key}', **kw)}" for key, kw in o.notes]
    return "\n".join(lines) + "\n\n"


# -- subcomando ----------------------------------------------------------------------------


def _sha(value: str) -> str:
    import argparse

    if _SHA_RE.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(value)
    return value


def _pr_number(value: str) -> int:
    import argparse

    number = _positive(value, 2**31 - 1)
    if number is None:
        raise argparse.ArgumentTypeError(value)
    return number


def _add_arguments(p: argparse.ArgumentParser) -> None:
    p.description = "ci.command.seal"
    p.add_argument("--kind", required=True, choices=KINDS, help="ci.seal.arg_kind")
    p.add_argument("--policy", required=True, metavar="DIR", help="ci.seal.arg_policy")
    p.add_argument("--repo-dir", required=True, metavar="DIR", help="ci.seal.arg_repo_dir")
    p.add_argument("--commit", required=True, type=_sha, metavar="SHA", help="ci.seal.arg_commit")
    p.add_argument("--base-sha", type=_sha, metavar="SHA", help="ci.seal.arg_base_sha")
    p.add_argument("--pr", type=_pr_number, metavar="N", help="ci.seal.arg_pr")
    p.add_argument("--out", required=True, metavar="DIR", help="ci.seal.arg_out")
    p.add_argument("--summary", default="", metavar="FILE", help="ci.seal.arg_summary")


def seal(args: argparse.Namespace, ctx: OpContext, out: TextIO, *, sealers: Sealers | None = None) -> int:
    """``ci seal`` con los clientes ``sealers`` (por omisión, los reales): 0, 1 o 3; nunca 2."""
    env = ctx.inv.env
    if args.kind == "submission" and (args.pr is None or args.base_sha is None):
        _say(translator(resolve_lang(cli=None, env=env, project=None))("ci.seal.submission_args"))
        return EXIT_USAGE
    if args.kind == "integration" and (args.pr is not None or args.base_sha is not None):
        _say(translator(resolve_lang(cli=None, env=env, project=None))("ci.seal.integration_args"))
        return EXIT_USAGE
    policy = read_policy(args.policy)
    t = translator(resolve_lang(cli=None, env=env, project=policy.language))  # mensajes (REQ-ARCH-69)
    o = Outcome(kind=args.kind, commit=args.commit, pr=args.pr)
    if not policy.readable:
        o.notes.append(("policy_unreadable", {}))
    try:
        _run(args, ctx, o, policy, sealers or Sealers(), t)
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001 - sin su texto; el resumen y la salida se escriben igual
        o.error = "internal"  # salida 1 aunque el registro ya exista
        o.notes.append(("internal", {"type": type(exc).__name__}))
    summary = render_summary(o, t)
    if args.summary and not append_summary(Path(args.summary), summary):
        _say(t("ci.seal.summary_failed", path=str(args.summary)))
    for key, kw in o.notes:
        _say(t(f"ci.seal.note.{key}", **kw))
    if o.comment not in (None, "no_pr"):
        _say(_comment_item(o, t) or "")
    _say(t("ci.seal.done", check=f"{_meta.NAME}/seal-{o.kind}", records=o.records, status=o.printed_status))
    out.write(f"records={o.records}\nstatus={o.printed_status}\n")
    out.flush()
    return o.exit_code()


def main(args: argparse.Namespace, ctx: OpContext, out: TextIO) -> int:
    """``ci seal``: los clientes reales, o los de ``<PREFIJO>EVIDENCE_FAKE`` en las pruebas."""
    return seal(args, ctx, out, sealers=sealers_from_env(ctx.inv.env))


COMMANDS: tuple[CiCommand, ...] = (CiCommand("seal", _add_arguments, main),)
