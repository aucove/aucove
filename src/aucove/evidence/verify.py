# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Verificación de los registros de evidencia (05-operaciones §9.26; 11-trazabilidad §8.6).

Es la lógica de ``evidence verify`` sin textos ni sobre: cada comprobación da un estado
(``pass``, ``weak``, ``pending``, ``skip`` o ``fail``), un motivo estable y los
marcadores de su texto, que localiza ``ops.evidence_verify``. Por registro:

- ``manifest``: ``record.json`` válido (esquema ``evidence-record/v1``), los bytes de
  ``manifest.json`` con el SHA-256 de ``record.json``, en el perfil canónico y en su
  forma exacta, el esquema ``evidence-manifest/v1``, lo que el esquema no expresa
  (``manifest.check_manifest``) y que ``kind``, ``commit``, ``pr`` y ``task`` sean los
  del registro. Nunca se regenera (REQ-TRACE-79): solo se comparan bytes.
- ``tsa``, una por prueba RFC 3161 (``tsa.verify_token``): la ruta de certificación la
  arma el verificador hasta una raíz del catálogo, con la cadena del registro y la del
  catálogo como candidatas (REQ-TRACE-88). Una copia local hecha desde un comentario no
  trae ``chain.pem`` ni ``root.pem`` y eso no la invalida. Además, la hora declarada en
  ``record.json`` debe ser el ``genTime`` del sello; sin hora declarada, ``fail``.
- ``ots`` (``ots.verify``): ``pending`` sin bloque y también con la atestación de un
  bloque de Bitcoin (``bitcoin_unverified``), que v0.1 no coteja: cualquiera puede
  escribirla. Con ``upgrade`` (con red), antes se completa en memoria una prueba sin
  bloque con los calendarios y, con ``--upgrade``, se reescribe ``manifest.json.ots``.
- ``openings`` y ``actors``: cada apertura reproduce un compromiso del manifiesto.
- ``git`` (con un repositorio): ``commit``, ``tree``, ``commits``, ``plan``, los blobs de
  las aperturas en sus rutas del commit, su contenido contra ``c`` y los actores
  ``author`` y ``committer`` del objeto commit. Sin el commit da ``skip``, nunca ``fail``.
  La ``base`` del manifiesto debe ser un ancestro del commit; si falta, se cotejan igual
  los blobs y los actores, y da ``fail`` salvo en un clon superficial.
- ``content`` (con una carpeta de archivos), uno por archivo: los bytes crudos contra
  ``c`` y, si no coinciden, la forma limpia de ``git hash-object --path``, calculada en un
  repositorio temporal aislado de la carpeta (:class:`CleanForm`).
- ``signature``, si el registro va firmado (11-trazabilidad §8.10; 00-marco D-72): la
  firma SSHSIG de ``manifest.json.sig`` sobre los bytes de ``manifest.json``, con una llave
  de la lista del proyecto (``keys``; nunca la que trae la firma) que valga en la fecha del
  sello: el ``genTime`` más antiguo de las TSA que dan ``pass`` o ``weak`` (REQ-TRACE-90 a
  REQ-TRACE-92). Sin lista de llaves, ``skip`` (``no_keys``) tras comprobar la firma con su
  llave; sin esa fecha, ``pending`` (``undated``). En un registro firmado, las pruebas son de
  la firma: ``tsa`` y ``ots`` comparan el SHA-256 de ``manifest.json.sig``.

Cada registro tiene un origen (``origin``): ``project`` si su firma da ``pass``, ``unsigned``
si no va firmado y ``unknown`` si la firma no se pudo cotejar. En un paquete, cada carpeta
``signature*/`` se verifica aparte (:func:`verify_signature_folder`: ``signature`` de
``index.txt.sig`` sobre ``index.txt``, ``tsa``, ``ots`` e ``index``), y
:func:`apply_coverage` sube el origen de los registros cuyo ``manifest.json`` lista el
índice firmado.

Veredicto de un registro: ``failed`` si algo da ``fail``; ``verified`` si ``manifest``
pasa y alguna TSA da ``pass`` o ``weak`` (en v0.1 es la única prueba de fecha que lo
permite), con ``strong`` si alguna da ``pass``; ``pending`` en otro caso. Sin el extra
``evidence``, ``tsa`` y ``ots`` quedan en ``skip`` con ``W_CONFIG_TOOL_MISSING`` y lo demás
se verifica igual (REQ-ARCH-99). :func:`unverifiable` da el registro ``failed`` de una
carpeta que no se pudo verificar, para que no deje sin veredicto a las demás.

Nada de lo que devuelve lleva sales (REQ-OPS-65): los marcadores son conteos, roles, ids
de TSA, punteros de JSON y, solo en ``content``, la ruta del archivo comprobado. El motivo
estable de cada check va al sobre en ``reason`` (``reason_code``: ``null`` en un ``pass``
sin motivo); su texto, en ``detail``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import secrets
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from ..domain.signing import EvidenceKey, find_key
from ..vcs.git import EMPTY_TREE, Git, GitError, GitTimeout, GitUnavailable, MissingObject, is_oid, parse_commit
from . import canon, ots, sshsig, timefmt, tsa
from .catalog import TOOL_MISSING, Catalog, Check
from .manifest import MAX_COMMITS, PLAN_PATHS, check_manifest, identity_value
from .openings import EntryOpening, Openings, blob_hasher, blob_oid, check_actor, check_entry, manifest_entries
from .package import (
    INDEX_FILE,
    SIGNATURE_FILE,
    PackageIndexError,
    index_files,
    parse_index,
    read_index,
    read_signature_dir,
)
from .record import (
    INDEX_SIG_FILE,
    KINDS,
    MANIFEST_FILE,
    MAX_FILE_BYTES,
    OPENINGS_FILE,
    RECORD_FILE,
    SIG_FILE,
    Proof,
    Record,
    RecordError,
    Signature,
    find_record_dirs,
    read_record_dir,
)
from .revocation import Revocation

__all__ = [
    "CHECKS",
    "EXTRA_MODULES",
    "REASONS",
    "CheckResult",
    "CleanForm",
    "Content",
    "RecordResult",
    "SignatureResult",
    "UnreadableRecord",
    "Upgrade",
    "apply_coverage",
    "package_root",
    "detail_key",
    "find_records",
    "missing_extra",
    "overall",
    "reason_code",
    "unverifiable",
    "verdict",
    "verify_folder",
    "verify_signature_folder",
]

CheckId = Literal["manifest", "signature", "tsa", "ots", "openings", "actors", "git", "content", "index"]
Status = Literal["pass", "weak", "pending", "skip", "fail"]
Verdict = Literal["verified", "pending", "failed"]
Confidence = Literal["strong", "weak"] | None
Origin = Literal["project", "unknown", "unsigned"]  # 11-trazabilidad §8.10
Params = Mapping[str, "str | int"]
UpgradeMode = Literal["memory", "dry_run", "write"]
# Validador de esquemas que inyecta quien llama (esta capa no importa jsonschema):
# ``validate(tipo, objeto)`` da los punteros JSON de los errores, sin los valores.
Validate = Callable[[str, object], Sequence[str]]

# Orden de los checks en la salida (enum de $defs/evidence_check).
CHECKS: tuple[CheckId, ...] = (
    "manifest", "signature", "tsa", "ots", "openings", "actors", "git", "content", "index",
)
OTS_SOURCE = "calendars"
# Pruebas de fecha que pueden llevar un registro a verified: en v0.1, solo una TSA. OTS da
# a lo sumo pending, porque su bloque de Bitcoin no se coteja (05 §9.26).
DATED: frozenset[str] = frozenset({"tsa"})

# Módulos del extra ``evidence`` (pyproject: rfc3161-client, opentimestamps y, en
# Windows, tzdata); ``cryptography`` y ``bitcoin`` llegan con ellos.
EXTRA_MODULES: tuple[str, ...] = ("rfc3161_client", "cryptography", "opentimestamps", "bitcoin") + (
    ("tzdata",) if sys.platform == "win32" else ()
)

# Códigos de manifest.check_manifest: su texto es el de evidence.manifest_problem.<código>.
MANIFEST_PROBLEMS: tuple[str, ...] = ("entries_unsorted", "entries_duplicate", "actors_duplicate", "commits_last")

# Motivos estables de cada check (claves evidence.check.<check>.<motivo>). Los de tsa y ots
# cubren todos los ``Check.reason`` de catalog, tsa, revocation y ots, más los propios.
REASONS: Mapping[str, tuple[str, ...]] = {
    "manifest": (
        "ok", "missing", "unreadable", "sha_mismatch", "invalid_json", "not_canonical", "schema",
        "record_invalid", "record_schema", "record_mismatch", "record_unreadable", "unverifiable",
    ),
    "tsa": (
        "ok", "not_obtained", "missing", "unreadable", "time_mismatch", "missing_extra", "unknown_tsa",
        "invalid", "rejected", "digest_mismatch", "nonce_mismatch", "policy", "chain", "signature", "ess",
        "no_revocation", "ocsp_invalid", "ocsp_status", "ocsp_mismatch", "ocsp_signature", "ocsp_unknown",
        "crl_invalid", "crl_signature", "stale", "after_expiry", "revoked", "revoked_later", "subject_missing",
    ),
    "ots": (
        "pending", "bitcoin_unverified", "invalid", "digest_mismatch", "missing_extra", "not_obtained",
        "missing", "unreadable", "subject_missing",
    ),
    # 11-trazabilidad §8.10 y REQ-TRACE-90 a REQ-TRACE-92 (00-marco D-72)
    "signature": (
        "ok", "unsigned", "no_keys", "undated", "missing", "unreadable", "no_manifest", "no_index",
        "missing_extra", "invalid", "folder_invalid", "schema", "namespace", "unsupported", "bad_signature",
        "metadata_mismatch",
        "key_unknown", "key_not_valid",
    ),
    "index": ("ok", "missing", "unreadable", "changed", "bad_index", "mismatch", "missing_files"),
    "openings": ("ok", "missing", "unreadable", "invalid", "schema", "other_manifest", "no_manifest", "mismatch"),
    "actors": ("ok", "none", "missing", "no_manifest", "mismatch"),
    "git": (
        "ok", "missing_commit", "missing_base", "unknown_base", "base_not_ancestor", "not_a_commit",
        "tree_mismatch", "commits_mismatch", "plan_mismatch", "blob_mismatch", "missing_blob",
        "content_mismatch", "size_mismatch", "actor_mismatch", "repo_error",
    ),
    "content": (
        "raw", "clean", "clean_by_oid", "mismatch", "clean_unavailable", "not_sealed", "unsafe_path",
        "unreadable", "no_files", "no_openings",
    ),
}

_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_OID_RE = re.compile(r"[0-9a-f]{40}")
# <kind>-<commit> en el artefacto y <kind>-<commit>-a<n> en la copia local (REQ-OPS-66).
_DIRNAME_RE = re.compile(r"(submission|integration)-([0-9a-f]{40})(?:-a[1-9][0-9]{0,2})?")
_CHUNK = 1 << 20  # lectura de los archivos de --content
_GIT_ERRORS = (GitError, GitTimeout, GitUnavailable, MissingObject, ValueError, OSError)


class UnreadableRecord(Exception):
    """Carpeta con un ``record.json`` que no se entiende y sin datos para identificar el registro."""

    def __init__(self, folder: Path, reason: str) -> None:
        super().__init__(f"{folder.name}: {reason}")
        self.folder = folder
        self.reason = reason


@dataclass(frozen=True)
class CheckResult:
    """Un elemento de ``records[].checks`` antes de localizarlo.

    ``params`` son los marcadores del texto ``detail_key(check, reason)``; ``refs``, los que
    son a su vez una clave del catálogo (``marcador -> clave``); ``notes``, textos que se
    agregan al detalle con sus marcadores. Nunca llevan sales (REQ-OPS-65).
    """

    check: CheckId
    status: Status
    reason: str
    source: str | None = None
    time_utc: datetime | None = None
    params: Params = field(default_factory=dict)
    refs: Mapping[str, str] = field(default_factory=dict)
    notes: tuple[tuple[str, Params], ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecordResult:
    """Resultado de una carpeta de registro."""

    folder: Path
    kind: str
    commit: str
    manifest_sha256: str
    verdict: Verdict
    confidence: Confidence
    checks: tuple[CheckResult, ...]
    upgraded: bool = False  # se reescribió la prueba .ots con la prueba completa
    warnings: tuple[str, ...] = ()
    origin: Origin = "unsigned"  # 11-trazabilidad §8.10


@dataclass(frozen=True)
class SignatureResult:
    """Resultado de una carpeta de firma de un paquete (``signature*/``; 11-trazabilidad §8.10).

    ``origin`` es ``project`` si su check ``signature`` da ``pass`` y ``unknown`` si no.
    """

    folder: Path
    index_sha256: str
    key: str | None
    key_id: str | None
    verdict: Verdict
    confidence: Confidence
    origin: Literal["project", "unknown"]
    checks: tuple[CheckResult, ...]
    upgraded: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Upgrade:
    """Consulta a los calendarios para completar las pruebas OTS sin bloque (05 §9.26).

    ``mode``: ``memory`` (con red y sin ``--upgrade``: la prueba completa solo se usa en
    memoria), ``dry_run`` (``--upgrade --dry-run``: tampoco se escribe) o ``write``
    (``--upgrade``: se reescribe ``manifest.json.ots``). ``budget`` son los topes de la
    corrida y lo comparten todas las carpetas (``ots.Budget``). ``transport`` se inyecta
    en las pruebas (``httpx2.MockTransport``).
    """

    mode: UpgradeMode = "write"
    transport: Any = None
    timeout: float = 15.0
    budget: ots.Budget = field(default_factory=ots.Budget)


def _inside(path: str | Path, root: str | Path) -> bool:
    """``path`` es ``root`` o está debajo, con las rutas reales (en otra unidad de Windows, no)."""
    try:
        real, base = os.path.realpath(path), os.path.realpath(root)
        return os.path.commonpath([real, base]) == base
    except ValueError:
        return False


# Variables GIT_* que se conservan en el repositorio temporal de la forma limpia: solo la
# que dice dónde están los programas de git (una instalación que la necesite).
_KEEP_GIT_VARS = frozenset({"GIT_EXEC_PATH"})


class CleanForm:
    """Forma limpia de los archivos de ``--content`` (``git hash-object --path``), aislada de
    esa carpeta (05 §9.26).

    La carpeta es de quien reclama: su ``.git/config`` podría definir un filtro ``clean`` que
    ejecuta comandos, y su almacén de objetos, traer el blob sellado. Por eso la forma limpia
    se calcula en un repositorio temporal vacío, creado para la ocasión con ``git init``:
    ``--git-dir`` temporal, ``--work-tree`` en la carpeta y ``-c core.autocrlf=true``, sin la
    configuración del sistema ni la global (``GIT_CONFIG_NOSYSTEM=1`` y ``GIT_CONFIG_GLOBAL``
    vacío), sin los atributos del sistema ni los globales, y sin variables ``GIT_*``
    heredadas (``GIT_CONFIG_*``, ``GIT_DIR``...). Así solo aplican las conversiones nativas de
    los ``.gitattributes`` de la carpeta (``text``, ``eol``, ``ident`` y
    ``working-tree-encoding``): un ``filter`` no tiene driver y no hace nada. Los bytes
    limpios salen del repositorio temporal (``hash-object -w`` y ``cat-file``), nunca del
    almacén de la carpeta. git arranca en el directorio temporal y entra en la carpeta con
    ``-C`` solo para que ``hash-object`` lea sus ``.gitattributes``.

    El repositorio se crea la primera vez que hace falta y ``close`` lo borra; también sirve
    como gestor de contexto.
    """

    def __init__(self, root: Path, *, env: Mapping[str, str] | None = None) -> None:
        self.root = Path(os.path.abspath(root))
        self._env = dict(os.environ if env is None else env)
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        self._git: Git | None = None
        self._git_dir = self._empty = Path()
        self._tried = False

    def __enter__(self) -> CleanForm:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Borra el repositorio temporal, si se creó."""
        tmp, self._tmp, self._git = self._tmp, None, None
        if tmp is not None:
            tmp.cleanup()

    def _repo(self) -> Git | None:
        """git del repositorio temporal; ``None`` si no se pudo crear (no se reintenta)."""
        if self._tried:
            return self._git
        self._tried = True
        try:
            self._tmp = tempfile.TemporaryDirectory(prefix="evidence-clean-", ignore_cleanup_errors=True)
            base = Path(self._tmp.name)
            if _inside(base, self.root):  # el directorio temporal nunca queda dentro de la carpeta
                return None
            empty = base / "empty"
            empty.write_bytes(b"")
            env = {k: v for k, v in self._env.items() if not k.upper().startswith("GIT_") or k.upper() in _KEEP_GIT_VARS}
            env.update(
                {
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_CONFIG_GLOBAL": str(empty),
                    "GIT_ATTR_NOSYSTEM": "1",
                    "HOME": str(base),  # git anterior a 2.32 no conoce GIT_CONFIG_GLOBAL
                    "XDG_CONFIG_HOME": str(base),
                }
            )
            git = Git(base, env=env, offline=True)
            if _inside(git.exe, self.root):  # el git que corre nunca sale de la carpeta de quien reclama
                return None
            git.run("init", "-q", "--template=", str(base / "repo"), read=False)
        except _GIT_ERRORS:  # incluye OSError: sin directorio temporal tampoco hay forma limpia
            return None
        self._git, self._git_dir, self._empty = git, base / "repo" / ".git", empty
        return git

    def clean(self, rel: str, path: Path) -> tuple[str, bytes | None] | None:
        """``(id, bytes)`` de la forma limpia de ``path`` como si estuviera en ``rel`` (ruta
        de git); ``None`` si git no la da. ``bytes`` es ``None`` si no se pudieron leer."""
        git = self._repo()
        if git is None:
            return None
        root = str(self.root)
        try:
            proc = git.run(
                "-C", root, f"--git-dir={self._git_dir}", f"--work-tree={root}",
                "-c", "core.autocrlf=true", "-c", "core.safecrlf=false", "-c", f"core.attributesFile={self._empty}",
                "hash-object", "-w", f"--path={rel}", "--", os.path.abspath(path),
                read=False, check=False,
            )
            oid = proc.stdout.decode("ascii", "replace").strip()
            if proc.returncode != 0 or not is_oid(oid):
                return None
            blob = git.run(f"--git-dir={self._git_dir}", "cat-file", "blob", oid, check=False)
        except _GIT_ERRORS:
            return None
        return oid, blob.stdout if blob.returncode == 0 else None


@dataclass(frozen=True)
class Content:
    """``--content``: carpeta con los archivos en sus rutas del repositorio.

    ``clean`` calcula la forma limpia de un archivo, aislada de la carpeta
    (:class:`CleanForm`); sin él (sin git), un archivo cuyos bytes crudos no coinciden queda
    en ``skip``.
    """

    root: Path
    clean: CleanForm | None = None


def detail_key(check: str, reason: str) -> str:
    """Clave del catálogo con el texto del motivo ``reason`` de ``check``."""
    if check == "manifest" and reason in MANIFEST_PROBLEMS:
        return f"evidence.manifest_problem.{reason}"
    return f"evidence.check.{check}.{reason}"


def reason_code(check: CheckResult) -> str | None:
    """``reason`` del check en el sobre (``$defs/evidence_check``): el motivo estable, o
    ``None`` en un ``pass`` sin motivo (``ok``). Un ``pass`` de ``content`` sí lo lleva:
    dice qué forma coincidió (``raw`` o ``clean``). Nunca lleva sales ni rutas."""
    return None if check.status == "pass" and check.reason == "ok" else check.reason


def missing_extra() -> tuple[str, ...]:
    """Módulos del extra ``evidence`` que no están instalados (``find_spec``, sin importarlos)."""
    out: list[str] = []
    for name in EXTRA_MODULES:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):  # sys.modules[name] = None o sin __spec__
            spec = None
        if spec is None:
            out.append(name)
    return tuple(out)


def find_records(root: Path) -> list[Path]:
    """Carpetas de registro en ``root`` o debajo (un artefacto, un paquete o ``.aucove/evidence/``)."""
    return find_record_dirs(Path(root))


# -- veredictos ----------------------------------------------------------------------------


def verdict(checks: Sequence[CheckResult]) -> tuple[Verdict, Confidence]:
    """Veredicto y confianza de un registro (05 §9.26)."""
    if any(c.status == "fail" for c in checks):
        return "failed", None
    manifest_ok = any(c.check == "manifest" and c.status == "pass" for c in checks)
    dated = [c for c in checks if c.check in DATED and c.status in ("pass", "weak")]
    if manifest_ok and dated:
        strong = any(c.check == "tsa" and c.status == "pass" for c in dated)
        return "verified", "strong" if strong else "weak"
    return "pending", None


def overall(
    results: Sequence[RecordResult], signatures: Sequence[SignatureResult] = ()
) -> tuple[Verdict, Confidence]:
    """Veredicto global de los registros y las carpetas de firma: ``failed`` si falla alguno,
    ``verified`` si se verifican todos, si no ``pending``."""
    every: list[RecordResult | SignatureResult] = [*results, *signatures]
    if any(r.verdict == "failed" for r in every):
        return "failed", None
    if every and all(r.verdict == "verified" for r in every):
        return "verified", "strong" if all(r.confidence == "strong" for r in every) else "weak"
    return "pending", None


# -- lectura ---------------------------------------------------------------------------------


def _read_bytes(path: Path, limit: int = MAX_FILE_BYTES) -> bytes | None:
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    return data if len(data) <= limit else None


def _json_file(path: Path) -> object | None:
    """Objeto JSON de un archivo de la carpeta, para validarlo con su esquema; ``None`` si no se lee."""
    raw = _read_bytes(path)
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None


def _identity_parts(folder: Path, raw_record: object) -> dict[str, str]:
    """``kind``, ``commit`` y ``sha`` (la huella del manifiesto) que se pueden leer de un
    registro dañado, sin lanzar nada: los que no se encuentran, faltan.

    Cada dato sale de lo primero que lo tenga válido: ``record.json``, ``manifest.json``
    (su SHA-256 y sus campos) o el nombre de la carpeta.
    """
    found: dict[str, str] = {}

    def offer(name: str, value: object, pattern: re.Pattern[str] | None = None) -> None:
        if name in found or not isinstance(value, str):
            return
        if (pattern is None and value in KINDS) or (pattern is not None and pattern.fullmatch(value)):
            found[name] = value

    if isinstance(raw_record, Mapping):
        offer("kind", raw_record.get("kind"))
        offer("commit", raw_record.get("commit"), _OID_RE)
        offer("sha", raw_record.get("manifest_sha256"), _HEX64_RE)
    data = _read_bytes(folder / MANIFEST_FILE)
    if data is not None:
        offer("sha", hashlib.sha256(data).hexdigest(), _HEX64_RE)
        try:
            obj = canon.loads(data)
        except (ValueError, RecursionError):  # CanonicalError es un ValueError
            obj = None
        if isinstance(obj, Mapping):
            offer("kind", obj.get("kind"))
            offer("commit", obj.get("commit"), _OID_RE)
    m = _DIRNAME_RE.fullmatch(folder.name)
    if m is not None:
        offer("kind", m.group(1))
        offer("commit", m.group(2), _OID_RE)
    return found


def _identity(folder: Path, raw_record: object) -> tuple[str, str, str]:
    """``(kind, commit, manifest_sha256)`` de un registro con ``record.json`` ilegible;
    ``UnreadableRecord`` si falta alguno (:func:`_identity_parts`)."""
    found = _identity_parts(folder, raw_record)
    if len(found) != 3:
        raise UnreadableRecord(folder, "record_unreadable")
    return found["kind"], found["commit"], found["sha"]


def unverifiable(folder: Path, reason: str = "unverifiable", *, error: str | None = None) -> RecordResult:
    """Registro ``failed`` de una carpeta que no se pudo verificar, con un solo check
    ``manifest`` en ``fail`` y el motivo estable ``reason``.

    Con él ``evidence verify`` aísla cada carpeta: un registro hostil o roto no deja sin
    veredicto a los demás. ``kind``, ``commit`` y la huella salen de lo que se pueda leer
    (:func:`_identity_parts`); lo que falte va en ceros, que no son un commit ni una huella
    reales, y ``kind`` en ``submission``. ``error`` es el nombre del tipo de la excepción,
    nunca su mensaje, que podría citar datos del registro.
    """
    folder = Path(folder)
    try:
        found = _identity_parts(folder, _json_file(folder / RECORD_FILE))
    except Exception:  # noqa: BLE001 - la carpeta ya falló una vez: se identifica con lo que haya
        found = {}
    params: dict[str, str | int] = {"error": error} if error is not None else {}
    check = CheckResult("manifest", "fail", reason, params=params)
    kind, commit, sha = found.get("kind", "submission"), found.get("commit", "0" * 40), found.get("sha", "0" * 64)
    return RecordResult(folder, kind, commit, sha, "failed", None, (check,), origin="unknown")


# -- verificación de una carpeta ---------------------------------------------------------


def verify_folder(
    folder: Path,
    *,
    catalog: Catalog | None = None,
    repo: Git | None = None,
    content: Content | None = None,
    upgrade: Upgrade | None = None,
    validate: Validate | None = None,
    keys: Sequence[EvidenceKey] | None = None,
) -> RecordResult:
    """Verifica la carpeta de un registro (``<kind>-<commit>/`` o ``<kind>-<commit>-a<n>/``).

    Solo lee, salvo ``upgrade`` en modo ``write``; sin ``upgrade`` no consulta los
    calendarios. ``catalog`` es el de TSA (por defecto, el del núcleo). ``UnreadableRecord``
    si ``record.json`` no se entiende y el registro no se puede identificar con el
    manifiesto ni con el nombre de la carpeta. ``keys`` es la lista de llaves del proyecto
    (``None``: sin lista, la firma no se coteja). Los datos de la carpeta son de terceros:
    quien verifica varias aísla cada una (:func:`unverifiable`).
    """
    folder = Path(folder)
    raw_record = _json_file(folder / RECORD_FILE)
    try:
        read = read_record_dir(folder)
    except RecordError as exc:
        kind, commit, sha = _identity(folder, raw_record)
        broken = CheckResult("manifest", "fail", "record_invalid", refs={"problem": f"evidence.record_error.{exc.reason}"})
        return RecordResult(folder, kind, commit, sha, "failed", None, (broken,))
    rec = read.record
    problems = dict(read.problems)
    manifest_check, manifest_obj, sha = _check_manifest(rec, raw_record, problems, validate)
    digest = _sealed_digest(rec.signature, sha)

    proofs, upgraded, extra_warnings = _check_proofs(folder, rec.proofs, digest, catalog, problems, upgrade)
    checks: list[CheckResult] = [manifest_check]
    if rec.signature is not None:
        checks.append(_check_signature(
            rec.signature, problems.get(SIG_FILE), rec.manifest, "no_manifest", keys, _dated_at(proofs)
        ))
    else:
        checks.append(CheckResult("signature", "skip", "unsigned"))
    checks.extend(proofs)
    raw_openings = _json_file(folder / OPENINGS_FILE) if (folder / OPENINGS_FILE).is_file() else None
    if raw_openings is None and isinstance(raw_record, Mapping):
        raw_openings = raw_record.get("openings")
    checks.append(_check_openings(rec.openings, problems.get(OPENINGS_FILE), manifest_obj, sha, raw_openings, validate))
    checks.append(_check_actors(rec.openings, manifest_obj))
    if repo is not None:
        checks.append(_check_git(repo, rec, manifest_obj))
    if content is not None:
        checks.extend(_check_content(content, rec.openings, manifest_obj))
    final, confidence = verdict(checks)
    warnings = tuple(dict.fromkeys([w for c in checks for w in c.warnings] + extra_warnings))
    return RecordResult(
        folder, rec.kind, rec.commit, rec.manifest_sha256, final, confidence, tuple(checks), upgraded, warnings,
        origin=_origin(checks[1]),
    )


def _sealed_digest(signature: Signature | None, manifest_sha: str) -> bytes | None:
    """SHA-256 de lo sellado: el del manifiesto o, si el registro va firmado, el de la firma
    (REQ-TRACE-91); ``None`` si falta la firma, que es lo sellado."""
    if signature is None:
        return bytes.fromhex(manifest_sha)
    return hashlib.sha256(signature.data).digest() if signature.data is not None else None


def _check_proofs(
    folder: Path, proofs: Sequence[Proof], digest: bytes | None, catalog: Catalog | None,
    problems: Mapping[str, str], upgrade: Upgrade | None,
) -> tuple[list[CheckResult], bool, list[str]]:
    """Checks ``tsa`` y ``ots`` de las pruebas de lo sellado, si se reescribió alguna prueba
    ``.ots`` y los avisos de la consulta a los calendarios."""
    checks: list[CheckResult] = []
    upgraded = False
    warnings: list[str] = []
    for proof in proofs:
        if proof.type == "rfc3161":
            checks.append(_check_tsa(proof, digest, catalog, problems))
    for proof in proofs:
        if proof.type == "ots":
            result, written, found = _check_ots(folder, proof, digest, problems, upgrade)
            checks.append(result)
            upgraded = upgraded or written
            warnings.extend(found)
    return checks, upgraded, warnings


def _dated_at(checks: Sequence[CheckResult]) -> datetime | None:
    """Fecha del sello para la firma: el ``genTime`` más antiguo de las TSA que dan ``pass`` o
    ``weak`` (REQ-TRACE-90); ``None`` sin ninguna (por ejemplo, con solo OTS)."""
    times = [c.time_utc for c in checks if c.check == "tsa" and c.status in ("pass", "weak") and c.time_utc]
    return min(times) if times else None


def _origin(signature: CheckResult) -> Origin:
    """Origen de un registro por su check ``signature`` (11-trazabilidad §8.10)."""
    if signature.status == "pass":
        return "project"
    return "unsigned" if signature.reason == "unsigned" else "unknown"


def _check_signature(
    meta: Signature,
    problem: str | None,
    signed: bytes | None,
    no_signed: str,
    keys: Sequence[EvidenceKey] | None,
    dated_at: datetime | None,
) -> CheckResult:
    """Check ``signature`` de la firma ``meta`` (con sus bytes) sobre ``signed`` (REQ-TRACE-90 a
    REQ-TRACE-92). ``no_signed`` es el motivo si faltan los bytes firmados. La llave sale de
    ``keys``, nunca de la firma: la que trae solo se compara con la de los metadatos."""
    source = meta.key_id
    if meta.data is None:
        return _absent("signature", source, problem, file=meta.file)
    if signed is None:
        return CheckResult("signature", "skip", no_signed, source)
    try:
        got = sshsig.verify(meta.data, signed)
    except sshsig.SignatureError as exc:
        if exc.reason == "missing_extra":
            return CheckResult("signature", "skip", "missing_extra", source, warnings=(TOOL_MISSING,))
        return CheckResult("signature", "fail", exc.reason, source)
    params: dict[str, str | int] = {"key": got.fingerprint, "key_id": meta.key_id}
    if got.fingerprint != meta.key or got.hash != meta.hash:
        return CheckResult("signature", "fail", "metadata_mismatch", source, params=params)
    if keys is None:
        return CheckResult("signature", "skip", "no_keys", source, params=params)
    known = find_key(keys, got.public_key)
    if known is None:
        return CheckResult("signature", "fail", "key_unknown", source, params=params)
    params["key_id"] = known.id
    if dated_at is None:
        return CheckResult("signature", "pending", "undated", known.id, params=params)
    if not known.valid_at(dated_at):
        return CheckResult("signature", "fail", "key_not_valid", known.id, dated_at, params=params)
    return CheckResult("signature", "pass", "ok", known.id, dated_at, params=params)


def _absent(check: CheckId, source: str | None, problem: str | None, *, file: str | None = None) -> CheckResult:
    """Prueba o archivo que no está (``missing``) o no se pudo leer (``unreadable``); ``file`` va
    al detalle cuando el nombre depende de lo sellado."""
    params: dict[str, str | int] = {"file": file} if file is not None else {}
    if problem in (None, "missing"):
        return CheckResult(check, "skip", "missing", source, params=params)
    return CheckResult(check, "skip", "unreadable", source, params=params,
                       refs={"problem": f"evidence.folder.{problem}"})


# -- paquete: carpetas de firma y cobertura (11-trazabilidad §8.10) ---------------------------


def verify_signature_folder(
    folder: Path,
    package: Path,
    *,
    catalog: Catalog | None = None,
    upgrade: Upgrade | None = None,
    validate: Validate | None = None,
    keys: Sequence[EvidenceKey] | None = None,
) -> SignatureResult:
    """Verifica una carpeta ``signature*/`` del paquete ``package``: la firma de ``index.txt``
    (``signature``), sus sellos (``tsa`` y ``ots``, sobre ``index.txt.sig``) y las huellas del
    índice (``index``). Solo lee, salvo ``upgrade`` en modo ``write``."""
    folder, package = Path(folder), Path(package)
    raw = _json_file(folder / SIGNATURE_FILE)
    try:
        read = read_signature_dir(folder)
    except RecordError as exc:
        broken = CheckResult(
            "signature", "fail", "folder_invalid", refs={"problem": f"evidence.record_error.{exc.reason}"}
        )
        return SignatureResult(folder, "0" * 64, None, None, "failed", None, "unknown", (broken,))
    doc, problems = read.folder, dict(read.problems)
    schema: list[CheckResult] = []
    if validate is not None and raw is not None:
        pointers = validate("evidence-signature", raw)
        if pointers:
            schema.append(CheckResult("signature", "fail", "schema", doc.signature.key_id, params={"pointer": pointers[0]}))
    index_data, index_problem = read_index(package)
    digest = hashlib.sha256(doc.signature.data).digest() if doc.signature.data is not None else None
    proofs, upgraded, extra = _check_proofs(folder, doc.proofs, digest, catalog, problems, upgrade)
    signature = schema[0] if schema else _check_signature(
        doc.signature, problems.get(INDEX_SIG_FILE), index_data, "no_index", keys, _dated_at(proofs)
    )
    checks = [signature, *proofs, _check_index(package, index_data, index_problem, doc.signed_sha256)]
    if any(c.status == "fail" for c in checks):
        final, confidence = "failed", None
    else:
        dated = [c for c in checks if c.check in DATED and c.status in ("pass", "weak")]
        if dated:
            final, confidence = "verified", "strong" if any(c.status == "pass" for c in dated) else "weak"
        else:
            final, confidence = "pending", None
    warnings = tuple(dict.fromkeys([w for c in checks for w in c.warnings] + extra))
    return SignatureResult(
        folder, doc.signed_sha256, doc.signature.key, doc.signature.key_id, final, confidence,  # type: ignore[arg-type]
        "project" if signature.status == "pass" else "unknown", tuple(checks), upgraded, warnings,
    )


def _check_index(package: Path, data: bytes | None, problem: str | None, signed_sha256: str) -> CheckResult:
    """Check ``index``: el índice es el firmado y cada archivo que lista tiene su huella."""
    if data is None:
        return _absent("index", None, problem)
    if hashlib.sha256(data).hexdigest() != signed_sha256:
        return CheckResult("index", "fail", "changed")
    try:
        index = parse_index(data)
    except PackageIndexError as exc:
        return CheckResult("index", "fail", "bad_index", params={"line": exc.line or 0})
    same, differ, missing = index_files(package, index)
    total = len(index)
    if differ:
        return CheckResult("index", "fail", "mismatch", params={"n": differ, "total": total})
    if missing:
        return CheckResult("index", "skip", "missing_files", params={"n": missing, "total": total})
    return CheckResult("index", "pass", "ok", params={"n": same})


def _file_sha256(path: Path) -> str | None:
    data = _read_bytes(path)
    return hashlib.sha256(data).hexdigest() if data is not None else None


def apply_coverage(results: Sequence[RecordResult], signatures: Sequence[SignatureResult],
                   package: Path) -> list[RecordResult]:
    """Origen de los registros de un paquete con la firma de su índice (11-trazabilidad §8.10).

    Un registro lo cubre el índice si lista su ``manifest.json`` (la ruta relativa a la raíz
    del paquete) con la misma huella. Las carpetas de firma que no dan ``failed`` firman el
    mismo índice: si alguna tiene origen ``project``, los registros cubiertos pasan a
    ``project``; si no, los ``unsigned`` cubiertos pasan a ``unknown``.
    """
    good = [s for s in signatures if s.verdict != "failed"]
    data, _problem = read_index(package)
    if not good or data is None:
        return list(results)
    try:
        index = parse_index(data)
    except PackageIndexError:
        return list(results)
    best: Origin = "project" if any(s.origin == "project" for s in good) else "unknown"
    base = Path(os.path.abspath(package))
    out: list[RecordResult] = []
    for result in results:
        try:
            rel = Path(os.path.abspath(result.folder)).relative_to(base).as_posix()
        except ValueError:
            out.append(result)
            continue
        covered = index.get(f"{rel}/{MANIFEST_FILE}") == _file_sha256(result.folder / MANIFEST_FILE)
        if covered and (result.origin == "unsigned" or (result.origin == "unknown" and best == "project")):
            result = replace(result, origin=best)
        out.append(result)
    return out


def package_root(path: Path) -> bool:
    """``path`` es la raíz de un paquete: tiene ``index.txt``."""
    return (Path(path) / INDEX_FILE).is_file()


# -- manifest -----------------------------------------------------------------------------


def _check_manifest(
    rec: Record, raw_record: object, problems: Mapping[str, str], validate: Validate | None
) -> tuple[CheckResult, Mapping[str, Any] | None, str]:
    """Check ``manifest``, el manifiesto leído (o ``None``) y el SHA-256 contra el que se
    verifican las pruebas: el de los bytes de ``manifest.json`` si están; si no, el de
    ``record.json``."""
    found: list[CheckResult] = []

    def problem(reason: str, **params: str | int) -> None:
        found.append(CheckResult("manifest", "fail", reason, params=params))

    if validate is not None and raw_record is not None:
        pointers = validate("evidence-record", raw_record)
        if pointers:
            problem("record_schema", pointer=pointers[0])
    data = rec.manifest
    if data is None:
        if found:
            return _first(found), None, rec.manifest_sha256
        return _absent("manifest", None, problems.get(MANIFEST_FILE)), None, rec.manifest_sha256
    sha = hashlib.sha256(data).hexdigest()
    if sha != rec.manifest_sha256:
        problem("sha_mismatch")
    obj: Mapping[str, Any] | None = None
    try:
        loaded = canon.loads(data)
    except canon.CanonicalError as exc:
        found.append(
            CheckResult("manifest", "fail", "invalid_json", refs={"problem": f"evidence.canonical.{exc.reason}"})
        )
    else:
        if canon.dumps(loaded) != data:
            problem("not_canonical")
        if isinstance(loaded, Mapping):
            obj = loaded
        if validate is not None:
            pointers = validate("evidence-manifest", loaded)
            if pointers:
                problem("schema", pointer=pointers[0])
    if obj is not None:
        for code in check_manifest(obj):
            problem(code)
        for name in ("kind", "commit", "pr", "task"):
            if obj.get(name) != getattr(rec, name):
                problem("record_mismatch", field=name)
        if obj.get("base", False) is None and not rec.plan:  # el primer registro sella el plan (11 §8.2)
            problem("record_mismatch", field="plan")
    if found:
        return _first(found), obj, sha
    entries = obj.get("entries") if obj is not None else None
    actors = obj.get("actors") if obj is not None else None
    params = {
        "entries": len(entries) if isinstance(entries, list) else 0,
        "actors": len(actors) if isinstance(actors, list) else 0,
    }
    return CheckResult("manifest", "pass", "ok", params=params), obj, sha


def _first(found: Sequence[CheckResult]) -> CheckResult:
    """El primer problema, con la cuenta de los demás en una nota."""
    head = found[0]
    if len(found) == 1:
        return head
    return CheckResult(
        head.check, head.status, head.reason, head.source, head.time_utc, head.params, head.refs,
        (("evidence.note.more", {"n": len(found) - 1}),),
    )


# -- pruebas de fecha ------------------------------------------------------------------------


def _check_tsa(
    proof: Proof, digest: bytes | None, catalog: Catalog | None, problems: Mapping[str, str]
) -> CheckResult:
    source = proof.source
    if proof.status != "ok":
        return CheckResult("tsa", "skip", "not_obtained", source)
    if proof.data is None:
        return _absent("tsa", source, problems.get(proof.file or ""))
    if digest is None:
        return CheckResult("tsa", "skip", "subject_missing", source, params={"file": proof.subject})
    revocation = None
    if proof.revocation is not None and proof.revocation.data is not None:
        revocation = Revocation(proof.revocation.type, proof.revocation.data)
    got = tsa.verify_token(
        proof.data, digest, source=source, catalog=catalog, chain_pem=proof.chain_pem, revocation=revocation
    )
    if got.status != "fail" and not _declared_time_ok(proof.time_utc, got):
        return CheckResult("tsa", "fail", "time_mismatch", source, got.time_utc, warnings=got.warnings)
    params: dict[str, str | int] = {}
    if got.status == "pass" and revocation is not None:
        params["revocation"] = revocation.type.upper()
    return CheckResult("tsa", got.status, got.reason, source, got.time_utc, params, warnings=got.warnings)


def _declared_time_ok(declared: str | None, got: Check) -> bool:
    """La hora de ``record.json`` es declarativa: debe estar (el esquema la exige en una
    prueba obtenida) y, si el sello se pudo leer, ser su ``genTime``."""
    if declared is None:
        return False
    if got.status not in ("pass", "weak") or got.time_utc is None:
        return True  # sin el extra no hay genTime con qué compararla
    try:
        return timefmt.parse_utc(declared) == got.time_utc
    except ValueError:
        return False


def _check_ots(
    folder: Path, proof: Proof, digest: bytes | None, problems: Mapping[str, str], upgrade: Upgrade | None
) -> tuple[CheckResult, bool, tuple[str, ...]]:
    """Check ``ots``, si se reescribió la prueba (``<sellado>.ots``) y los avisos de la consulta
    a los calendarios, que solo se hace con una prueba válida que todavía no tiene bloque."""
    if proof.status != "ok":
        return CheckResult("ots", "skip", "not_obtained", OTS_SOURCE), False, ()
    data = proof.data
    ots_file = proof.file or f"{proof.subject}.ots"
    if data is None:
        return _absent("ots", OTS_SOURCE, problems.get(ots_file), file=ots_file), False, ()
    if digest is None:
        return CheckResult("ots", "skip", "subject_missing", OTS_SOURCE, params={"file": proof.subject}), False, ()
    got = ots.verify(data, digest)
    notes: list[tuple[str, Params]] = []
    warnings: tuple[str, ...] = ()
    written = False
    if upgrade is not None and got.status == "pending" and got.reason == "pending":
        up = ots.upgrade(data, transport=upgrade.transport, timeout=upgrade.timeout, budget=upgrade.budget)
        warnings = up.warnings
        better = ots.verify(up.ots, digest) if up.status == "complete" and up.changed else None
        if better is not None and better.status != "fail":
            data, got = up.ots, better
            if upgrade.mode == "memory":
                notes.append(("evidence.note.completed", {}))
            elif upgrade.mode == "dry_run":
                notes.append(("evidence.note.upgrade_dry_run", {}))
            elif _replace_file(folder / ots_file, data):
                written = True
                notes.append(("evidence.note.upgraded", {}))
            else:
                notes.append(("evidence.note.upgrade_not_written", {}))
    params: dict[str, str | int] = {}
    if got.reason in ("bitcoin_unverified", "pending"):
        info = ots.read(data)
        if got.reason == "bitcoin_unverified" and info.bitcoin:  # para cotejarlo a mano (VERIFY.md)
            params = {"height": info.bitcoin[0].height, "merkle_root": info.bitcoin[0].merkle_root}
        elif got.reason == "pending":
            params = {"n": len(info.pending)}
    result = CheckResult("ots", got.status, got.reason, got.source, None, params, notes=tuple(notes), warnings=got.warnings)
    return result, written, warnings


def _replace_file(target: Path, data: bytes) -> bool:
    """Reescribe ``target`` de forma atómica (temporal hermano y ``os.replace``); ``False`` si no se pudo."""
    tmp = target.with_name(f".{target.name}.tmp-{secrets.token_hex(4)}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


# -- aperturas y actores -------------------------------------------------------------------


def _check_openings(
    openings: Openings | None,
    problem: str | None,
    manifest_obj: Mapping[str, Any] | None,
    sha: str,
    raw_openings: object,
    validate: Validate | None,
) -> CheckResult:
    if openings is None:
        if problem == "invalid":
            return CheckResult("openings", "fail", "invalid")
        return _absent("openings", None, problem)
    if validate is not None and isinstance(raw_openings, Mapping):
        pointers = validate("evidence-openings", raw_openings)
        if pointers:
            return CheckResult("openings", "fail", "schema", params={"pointer": pointers[0]})
    if openings.manifest_sha256 != sha:
        return CheckResult("openings", "fail", "other_manifest")
    if manifest_obj is None:
        return CheckResult("openings", "skip", "no_manifest")
    entries = manifest_entries(manifest_obj)
    bad = 0
    for opening in openings.entries:
        try:
            got = check_entry(opening, entries)
        except ValueError:
            bad += 1
            continue
        if not got.found or got.content is False:  # content False: borrado en un lado y no en el otro
            bad += 1
    total = len(openings.entries)
    if bad:
        return CheckResult("openings", "fail", "mismatch", params={"n": bad, "total": total})
    return CheckResult("openings", "pass", "ok", params={"n": total})


def _check_actors(openings: Openings | None, manifest_obj: Mapping[str, Any] | None) -> CheckResult:
    if openings is None:
        return CheckResult("actors", "skip", "missing")
    if manifest_obj is None:
        return CheckResult("actors", "skip", "no_manifest")
    if not openings.actors:
        return CheckResult("actors", "skip", "none")
    committed = manifest_obj.get("actors")
    actors = committed if isinstance(committed, list) else []
    bad = [a.role for a in openings.actors if not check_actor(a, actors)]
    if bad:
        return CheckResult("actors", "fail", "mismatch", params={"roles": ", ".join(bad)})
    return CheckResult("actors", "pass", "ok", params={"roles": ", ".join(a.role for a in openings.actors)})


# -- git ------------------------------------------------------------------------------------


def _check_git(repo: Git, rec: Record, manifest_obj: Mapping[str, Any] | None) -> CheckResult:
    try:
        return _git_unguarded(repo, rec, manifest_obj)
    except _GIT_ERRORS:
        return CheckResult("git", "skip", "repo_error")


def _shallow(repo: Git) -> bool:
    """El repositorio es un clon superficial (``git rev-parse --is-shallow-repository``)."""
    proc = repo.run("rev-parse", "--is-shallow-repository", check=False)
    return proc.returncode == 0 and proc.stdout.strip() == b"true"


def _git_unguarded(repo: Git, rec: Record, manifest_obj: Mapping[str, Any] | None) -> CheckResult:
    def fail(reason: str, **params: str | int) -> CheckResult:
        return CheckResult("git", "fail", reason, params=params)

    commit = rec.commit
    base = manifest_obj.get("base") if manifest_obj is not None else None
    wanted = [commit] + ([base] if isinstance(base, str) and is_oid(base) and base != commit else [])
    objects = repo.cat_file_objects(wanted)  # una sola lectura: cada llamada a git cuenta
    found = objects.get(commit)
    if found is None:
        return CheckResult("git", "skip", "missing_commit")
    info, raw = found
    if info.type != "commit":
        return fail("not_a_commit")
    header = parse_commit(raw)
    base_missing = False  # sin la base no se cotejan commits ni plan, pero sí lo demás

    def finish(result: CheckResult) -> CheckResult:
        """Sin la base: ``fail`` si el repositorio tiene la historia completa, donde debería
        estar; en un clon superficial, ``skip`` (no se cotejaron commits ni plan)."""
        if not base_missing:
            return result
        if not _shallow(repo):
            return fail("unknown_base")
        return result if result.status == "skip" else CheckResult("git", "skip", "missing_base", params=result.params)

    if manifest_obj is not None:
        if header.tree != manifest_obj.get("tree"):
            return fail("tree_mismatch")
        known = objects.get(base) if isinstance(base, str) and is_oid(base) else None
        if base is not None and known is None:
            base_missing = True
        else:
            if known is not None and (known[0].type != "commit" or not repo.is_ancestor(base, commit)):
                return fail("base_not_ancestor")
            chain = repo.first_parent_chain(commit, max_count=MAX_COMMITS, exclude=base)
            if list(reversed(chain)) != manifest_obj.get("commits"):
                return fail("commits_mismatch")
            changes = repo.diff_tree_raw(base or EMPTY_TREE, commit)
            if rec.plan != (base is None or any(change.path in PLAN_PATHS for change in changes)):
                return fail("plan_mismatch")
    openings = rec.openings
    if openings is None:
        return finish(CheckResult("git", "pass", "ok", params={"blobs": 0, "actors": 0}))
    tree = repo.ls_tree(commit)
    misplaced = 0
    for entry in openings.entries:
        here = tree.get(entry.path)
        if entry.blob is None:
            misplaced += here is not None  # se borró: no puede seguir en el commit
        elif here is None or here[1] != entry.blob:
            misplaced += 1
    if misplaced:
        return fail("blob_mismatch", n=misplaced)
    by_oid: dict[str, list[EntryOpening]] = {}
    for entry in openings.entries:
        if entry.blob is not None:
            by_oid.setdefault(entry.blob, []).append(entry)
    entries = manifest_entries(manifest_obj) if manifest_obj is not None else None
    wrong_content = wrong_size = 0
    try:
        for blob, data in repo.cat_file_batch(list(by_oid)):  # comprueba antes que estén todos
            for entry in by_oid[blob.oid]:
                if blob.type != "blob" or (entries is not None and entries.get(entry.p()) != entry.c(data)):
                    wrong_content += 1
                elif entry.size is not None and entry.size != blob.size:
                    wrong_size += 1
    except MissingObject:  # p. ej., un clon parcial sin red
        absent = sum(found is None for found in repo.cat_file_check(list(by_oid)).values())
        return finish(CheckResult("git", "skip", "missing_blob", params={"n": absent}))
    if wrong_content:
        return fail("content_mismatch", n=wrong_content)
    if wrong_size:
        return fail("size_mismatch", n=wrong_size)
    identities = {"author": header.author, "committer": header.committer}
    bad = [
        a.role
        for a in openings.actors
        if a.role in identities and identity_value(identities[a.role], header.encoding) != a.value
    ]
    if bad:
        return fail("actor_mismatch", roles=", ".join(bad))
    checked_actors = sum(a.role in identities for a in openings.actors)
    return finish(CheckResult("git", "pass", "ok", params={"blobs": len(by_oid), "actors": checked_actors}))


# -- contenido --------------------------------------------------------------------------------


def _content_file(root: Path, rel: str) -> Path | None:
    """Archivo de ``rel`` (ruta de git, con ``/``) dentro de ``root``; ``None`` si saldría de ahí."""
    parts = rel.split("/")
    if not rel or "\0" in rel or any(p in ("", ".", "..") for p in parts):
        return None
    if os.name == "nt" and any(ch in rel for ch in ("\\", ":")):
        return None
    target = root.joinpath(*parts)
    return target if _inside(target, root) else None


def _hash_file(path: Path, salt: bytes) -> tuple[str, str]:
    """``(c, id del blob)`` de los bytes crudos de ``path``, leídos por partes."""
    hasher = blob_hasher(salt)
    sha1 = hashlib.sha1(b"blob %d\0" % path.stat().st_size, usedforsecurity=False)
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            hasher.update(chunk)
            sha1.update(chunk)
    return str(hasher.hexdigest()), sha1.hexdigest()


def _check_content(
    content: Content, openings: Openings | None, manifest_obj: Mapping[str, Any] | None
) -> Iterator[CheckResult]:
    if openings is None or manifest_obj is None:
        yield CheckResult("content", "skip", "no_openings")
        return
    entries = manifest_entries(manifest_obj)
    checked = unsafe = 0
    for entry in openings.entries:
        if entry.blob is None:
            continue  # borrado: no hay contenido que comparar
        target = _content_file(content.root, entry.path)
        if target is None:
            unsafe += 1  # git nunca da una ruta así; no se lee ni se muestra (REQ-OPS-65)
            continue
        if not target.is_file() or target.is_symlink():
            continue
        path = {"path": entry.path}  # solo la de un archivo comprobado aparece (REQ-OPS-65)
        checked += 1
        try:
            expected = entries.get(entry.p())
            raw_c, raw_oid = _hash_file(target, entry.salt)
        except (OSError, ValueError):
            yield CheckResult("content", "skip", "unreadable", params=path)
            continue
        if expected is None:
            yield CheckResult("content", "skip", "not_sealed", params=path)
        elif raw_c == expected:
            yield CheckResult("content", "pass", "raw", params=path)
        else:
            yield _clean_form(content, entry, target, expected, raw_oid, path)
    if unsafe:
        yield CheckResult("content", "skip", "unsafe_path", params={"n": unsafe})
    elif not checked:
        yield CheckResult("content", "skip", "no_files")


def _clean_form(
    content: Content, entry: EntryOpening, target: Path, expected: str, raw_oid: str, path: Params
) -> CheckResult:
    """Los bytes crudos no coinciden: se compara la forma limpia del archivo, calculada sin
    la configuración ni los objetos de la carpeta (:class:`CleanForm`)."""
    got = content.clean.clean(entry.path, target) if content.clean is not None else None
    if got is None:
        return CheckResult("content", "skip", "clean_unavailable", params=path)
    oid, data = got
    if oid == raw_oid:  # las conversiones no cambian nada: es otro contenido
        return CheckResult("content", "fail", "mismatch", params=path)
    if data is not None:
        status = "pass" if blob_oid(data) == oid and entry.c(data) == expected else "fail"
        return CheckResult("content", status, "clean" if status == "pass" else "mismatch", params=path)
    if oid == entry.blob:  # sin los bytes limpios, solo se coteja con el id de la apertura
        return CheckResult("content", "weak", "clean_by_oid", params=path)
    return CheckResult("content", "fail", "mismatch", params=path)
