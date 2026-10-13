# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Manifiesto de evidencia ``evidence-manifest/v1`` y sus aperturas (11-trazabilidad §8.3; D-45).

- ``build_submission``: la cabeza de un PR frente a ``merge-base(base_sha, commit)``
  (09-ci REQ-CI-51); actores ``pr_author`` (de la API), ``author`` y ``committer``.
- ``build_integration``: un push a la base desde ``base``, el último commit de primer
  padre ya sellado, que elige quien llama con la forja (``first_parent_history`` da los
  candidatos), o desde el árbol vacío si es ``None`` (REQ-CI-52); actores ``pr_author``
  y ``merged_by`` del PR del commit, si lo hay, y ``author`` y ``committer``.

Solo se leen objetos de git: ``diff-tree -r -z --raw --no-renames``, ``cat-file --batch``
(sin ``--filters`` ni ``--textconv``), ``merge-base`` y ``rev-list --first-parent``. Nada
del árbol se ejecuta ni se aplican sus atributos (REQ-CI-51), y las huellas salen de los
blobs, nunca del disco (REQ-TRACE-77): un checkout con ``core.autocrlf=true`` da el
mismo manifiesto. Los bytes canónicos (``canon``) son lo que se sella y se guardan tal
cual (REQ-TRACE-79).

Entradas: una por archivo cambiado en ``(base, commit]``, ordenadas por ``p``, con
``c = null`` si se borró. Se omiten, y se informan en ``skipped`` para el resumen, las
rutas que no son UTF-8 válido, las de más de 4096 bytes tal como las da git (11 §8.3) y
los gitlinks (su objeto es un commit de otro repositorio, no un blob). Más de 10 000
entradas es ``ManifestError("too_many_entries")``, antes de leer contenidos.

Actores: ``author`` y ``committer`` se leen con la codificación que declara el commit
solo si es un códec de texto de ``TEXT_ENCODINGS``, los mismos en Linux, macOS y Windows;
si no, en UTF-8 estricto. Una identidad que no se puede decodificar se omite y va a
``skipped`` (``actor_not_utf8``): nunca interrumpe el manifiesto.

``commits`` son los de primer padre de ``(base, commit]``, del más antiguo al más reciente;
si hay más de 100, los 100 más recientes. ``plan`` (va en el registro, no en el
manifiesto) es ``True`` si el rango toca ``plan.md``, ``roles.yaml`` o
``memory/constitution.md``, o si ``base`` es nulo (primer registro; 11 §8.2). Se calcula
igual en los dos tipos; 11 §8.2 lo pide para el registro de integración.

Las sales salen de ``salt`` (por defecto ``secrets.token_bytes(32)``), una por entrada y
una por actor, en un orden fijo (entradas en el orden de git y actores en el de
``ROLES``): con una fuente inyectada, el manifiesto es determinista.
"""

from __future__ import annotations

import codecs
import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from .. import _meta
from ..vcs.git import EMPTY_TREE, CommitHeader, Git, MissingObject, RawChange, parse_commit
from . import canon, timefmt
from .openings import (
    MAX_ACTOR_CHARS,
    MAX_ENTRIES,
    MAX_PATH_BYTES,
    ROLES,
    SALT_BYTES,
    ActorOpening,
    EntryOpening,
    Openings,
    actor_commitment,
    blob_commitment,
    new_salt,
    path_commitment,
)

__all__ = [
    "COMMITMENT",
    "FORGE",
    "KINDS",
    "MANIFEST_SCHEMA",
    "MAX_COMMITS",
    "PLAN_PATHS",
    "REASONS",
    "TEXT_ENCODINGS",
    "BuiltManifest",
    "Kind",
    "ManifestError",
    "SaltSource",
    "Skipped",
    "build_integration",
    "build_submission",
    "check_manifest",
    "default_generator",
    "first_parent_history",
    "identity_value",
]

MANIFEST_SCHEMA = f"{_meta.SCHEMA_BASE}/evidence-manifest/v1"
COMMITMENT = "sha256-salted-v1"
FORGE = "github"
MAX_COMMITS = 100
KINDS: tuple[str, ...] = ("submission", "integration")
# Rutas cuyo cambio marca el registro como sello del plan (11 §8.2), en bytes de git.
PLAN_PATHS: frozenset[bytes] = frozenset({b"plan.md", b"roles.yaml", b"memory/constitution.md"})
# Códecs con que se lee una identidad si el commit los declara (cabecera 'encoding'), por
# su nombre canónico en Python (codecs.lookup(x).name): la tabla «Standard Encodings», que
# está igual en Linux, macOS y Windows. Quedan fuera los que solo existen en Windows (mbcs,
# oem), los que no son de texto (base64, rot13...) y los especiales (idna, punycode,
# undefined, unicode_escape...): con ellos el mismo commit daría actores distintos según
# la plataforma, o un error en vez de un actor.
TEXT_ENCODINGS: frozenset[str] = frozenset(
    {
        "ascii", "big5", "big5hkscs", "cp037", "cp1006", "cp1026", "cp1125", "cp1140", "cp1250", "cp1251",
        "cp1252", "cp1253", "cp1254", "cp1255", "cp1256", "cp1257", "cp1258", "cp273", "cp424", "cp437",
        "cp500", "cp720", "cp737", "cp775", "cp850", "cp852", "cp855", "cp856", "cp857", "cp858", "cp860",
        "cp861", "cp862", "cp863", "cp864", "cp865", "cp866", "cp869", "cp874", "cp875", "cp932", "cp949",
        "cp950", "euc_jis_2004", "euc_jisx0213", "euc_jp", "euc_kr", "gb18030", "gb2312", "gbk", "hz",
        "iso2022_jp", "iso2022_jp_1", "iso2022_jp_2", "iso2022_jp_2004", "iso2022_jp_3", "iso2022_jp_ext",
        "iso2022_kr", "iso8859-1", "iso8859-10", "iso8859-11", "iso8859-13", "iso8859-14", "iso8859-15",
        "iso8859-16", "iso8859-2", "iso8859-3", "iso8859-4", "iso8859-5", "iso8859-6", "iso8859-7",
        "iso8859-8", "iso8859-9", "johab", "koi8-r", "koi8-t", "koi8-u", "kz1048", "mac-cyrillic",
        "mac-greek", "mac-iceland", "mac-latin2", "mac-roman", "mac-turkish", "ptcp154", "shift_jis",
        "shift_jis_2004", "shift_jisx0213", "utf-16", "utf-16-be", "utf-16-le", "utf-32", "utf-32-be",
        "utf-32-le", "utf-7", "utf-8", "utf-8-sig",
    }
)

Kind = Literal["submission", "integration"]
SaltSource = Callable[[], bytes]

_GITLINK = "160000"
_OID_RE = re.compile(r"[0-9a-f]{40}")
_CREATED_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_GENERATOR_RE = re.compile(r"[a-z][a-z0-9-]*/[0-9]+\.[0-9]+\.[0-9]+[a-z0-9.+-]*")
_REPOSITORY_RE = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*/[A-Za-z0-9._-]{1,100}")

# Códigos de ManifestError.reason.
REASONS: tuple[str, ...] = (
    "bad_input",  # argumento con otra forma (sha, número, repositorio, fecha, generador)
    "unknown_commit",  # el commit no está en el repositorio
    "not_a_commit",  # el id existe pero no es un commit
    "unknown_base",  # 'base' o 'base_sha' no está o no es un commit
    "base_not_ancestor",  # 'base' no es ancestro de 'commit' (integration)
    "no_merge_base",  # sin ancestro común con la base (submission)
    "empty_range",  # (base, commit] no tiene commits
    "too_many_entries",  # más de 10 000 entradas
    "missing_blob",  # un blob del rango no está en local
    "bad_salt",  # la fuente de sales no dio 32 bytes
    "salt_reuse",  # la fuente de sales repitió una sal
)


class ManifestError(ValueError):
    """No se puede construir el manifiesto; ``reason`` es uno de ``REASONS``."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"manifiesto: {reason}" + (f" ({detail})" if detail else ""))


@dataclass(frozen=True)
class Skipped:
    """Cambio o actor que no entra al manifiesto; el resumen lo informa.

    ``reason``: ``non_utf8``, ``path_too_long`` o ``gitlink`` (con ``path``, en los
    bytes exactos de git), o ``actor_not_utf8`` o ``actor_too_long`` (con ``role``).
    """

    reason: str
    path: bytes | None = None
    role: str | None = None


@dataclass(frozen=True)
class BuiltManifest:
    """Manifiesto construido: bytes que se sellan, objeto, aperturas privadas y notas."""

    kind: Kind
    commit: str
    base: str | None
    data: bytes  # bytes canónicos exactos de manifest.json
    sha256: str  # SHA-256 de 'data' en hex
    manifest: Mapping[str, Any]
    openings: Openings
    plan: bool
    skipped: tuple[Skipped, ...]


def default_generator() -> str:
    """``<nombre>/<versión>`` del núcleo, para el campo ``generator``."""
    return f"{_meta.NAME}/{_meta.__version__}"


# -- validación de argumentos -----------------------------------------------------------


def _oid(value: object, field: str) -> str:
    if not isinstance(value, str) or _OID_RE.fullmatch(value) is None:
        raise ManifestError("bad_input", field)
    return value


def _number(value: object, field: str, *, optional: bool) -> int | None:
    if value is None and optional:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= canon.MAX_SAFE_INTEGER:
        raise ManifestError("bad_input", field)
    return value


def _common(
    repository: object, repository_id: object, created_at: str | None, generator: str | None
) -> tuple[str, str]:
    if (
        not isinstance(repository, str)
        or len(repository) > 140
        or _REPOSITORY_RE.fullmatch(repository) is None
    ):
        raise ManifestError("bad_input", "repository")
    _number(repository_id, "repository_id", optional=False)
    created = timefmt.format_utc(timefmt.utc_now()) if created_at is None else created_at
    if not isinstance(created, str) or _CREATED_RE.fullmatch(created) is None:
        raise ManifestError("bad_input", "created_at")
    gen = default_generator() if generator is None else generator
    if not isinstance(gen, str) or len(gen) > 64 or _GENERATOR_RE.fullmatch(gen) is None:
        raise ManifestError("bad_input", "generator")
    return created, gen


def _commit_headers(git: Git, commit: str, other: str | None) -> CommitHeader:
    """Cabecera de ``commit`` y comprobación de ``other`` (la base), en una sola lectura.

    ``unknown_commit`` o ``not_a_commit`` para ``commit``; ``unknown_base`` si ``other``
    no está o no es un commit.
    """
    objects = git.cat_file_objects([commit] if other is None else [commit, other])
    for oid, missing, wrong in ((commit, "unknown_commit", "not_a_commit"), (other, "unknown_base", "unknown_base")):
        if oid is None:
            continue
        found = objects.get(oid)
        if found is None:
            raise ManifestError(missing, oid)
        if found[0].type != "commit":
            raise ManifestError(wrong, oid)
    found = objects[commit]
    assert found is not None
    try:
        header = parse_commit(found[1])
    except ValueError:
        raise ManifestError("not_a_commit", commit) from None
    if _OID_RE.fullmatch(header.tree) is None:  # repositorio SHA-256: el esquema pide SHA-1
        raise ManifestError("bad_input", "tree")
    return header


# -- identidades -------------------------------------------------------------------


def _text_codec(encoding: str | None) -> str | None:
    """Nombre canónico del códec ``encoding`` si está en ``TEXT_ENCODINGS``; si no, ``None``.

    El nombre sale del objeto commit: puede no existir, traer un NUL o bytes que no son
    UTF-8 (``codecs.lookup`` lanza ``LookupError`` o ``ValueError``).
    """
    if not encoding:
        return None
    try:
        name = codecs.lookup(encoding).name
    except (LookupError, ValueError):
        return None
    return name if name in TEXT_ENCODINGS else None


def identity_value(raw: bytes, encoding: str | None = None) -> str | None:
    """Texto de una identidad del objeto commit (``Nombre <correo>``), sin normalizar.

    Con la codificación que declara el commit (cabecera ``encoding``) si es uno de
    ``TEXT_ENCODINGS``; si no, o si falla, UTF-8 estricto. ``None`` si ninguna sirve: quien
    llama omite el actor y lo informa (``actor_not_utf8``). Nunca lanza.
    """
    codec = _text_codec(encoding)
    if codec is not None:
        try:
            return raw.decode(codec)
        except (LookupError, ValueError):  # UnicodeError es un ValueError
            pass
    try:
        return raw.decode("utf-8")
    except ValueError:
        return None


class _Salts:
    """Fuente de sales con control de tamaño y de repetición (REQ-TRACE-78)."""

    def __init__(self, source: SaltSource) -> None:
        self._source = source
        self._seen: set[bytes] = set()

    def take(self) -> bytes:
        value = self._source()
        if not isinstance(value, (bytes, bytearray)) or len(value) != SALT_BYTES:
            raise ManifestError("bad_salt")
        salt = bytes(value)
        if salt in self._seen:
            raise ManifestError("salt_reuse")
        self._seen.add(salt)
        return salt


def _skip_reason(change: RawChange) -> str | None:
    if change.new_mode == _GITLINK or (change.deleted and change.old_mode == _GITLINK):
        return "gitlink"
    try:
        change.path.decode("utf-8")
    except UnicodeDecodeError:
        return "non_utf8"
    if len(change.path) > MAX_PATH_BYTES:  # bytes exactos de git, no caracteres (11 §8.3)
        return "path_too_long"
    return None


def _actors(
    header: CommitHeader, given: Mapping[str, str | None], salts: _Salts
) -> tuple[list[dict[str, str]], list[ActorOpening], list[Skipped]]:
    values: dict[str, str | None] = dict(given)
    skipped: list[Skipped] = []
    for role, raw in (("author", header.author), ("committer", header.committer)):
        value = identity_value(raw, header.encoding)
        if value is None:
            skipped.append(Skipped("actor_not_utf8", role=role))
        values[role] = value
    committed: list[dict[str, str]] = []
    openings: list[ActorOpening] = []
    for role in ROLES:
        value = values.get(role)
        if not value:
            continue  # un rol sin dato no aparece
        if len(value) > MAX_ACTOR_CHARS:
            skipped.append(Skipped("actor_too_long", role=role))
            continue
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            skipped.append(Skipped("actor_not_utf8", role=role))
            continue
        salt = salts.take()
        committed.append({"r": role, "a": actor_commitment(salt, role, value)})
        openings.append(ActorOpening(role=role, value=value, salt=salt))
    return committed, openings, skipped


# -- construcción ------------------------------------------------------------------


def first_parent_history(git: Git, commit: str, limit: int = MAX_COMMITS) -> list[str]:
    """Los ``limit`` commits de primer padre anteriores a ``commit`` (sin él), del más nuevo al más viejo.

    Son los candidatos a ``base`` de ``build_integration``: el primero que tenga un sello
    exitoso en la forja (09-ci REQ-CI-52).
    """
    _oid(commit, "commit")
    if limit < 1:
        return []
    return git.first_parent_chain(commit, max_count=limit + 1)[1:]


def _build(
    git: Git,
    *,
    kind: Kind,
    commit: str,
    header: CommitHeader,
    base: str | None,
    pr: int | None,
    task: int | None,
    repository: str,
    repository_id: int,
    actors: Mapping[str, str | None],
    created_at: str,
    generator: str,
    salt: SaltSource,
) -> BuiltManifest:
    chain = git.first_parent_chain(commit, max_count=MAX_COMMITS, exclude=base)
    if not chain or chain[0] != commit:
        raise ManifestError("empty_range", f"{base}..{commit}")
    commits = list(reversed(chain))
    changes = git.diff_tree_raw(base or EMPTY_TREE, commit)
    plan = base is None or any(change.path in PLAN_PATHS for change in changes)

    skipped: list[Skipped] = []
    kept: list[tuple[RawChange, str]] = []
    for change in changes:
        reason = _skip_reason(change)
        if reason is not None:
            skipped.append(Skipped(reason, path=change.path))
        else:
            kept.append((change, change.path.decode("utf-8")))
    if len(kept) > MAX_ENTRIES:
        raise ManifestError("too_many_entries", str(len(kept)))

    salts = _Salts(salt)
    entry_salts = [salts.take() for _ in kept]
    # Contenido: una lectura por blob aunque varias rutas lo compartan; cada una con su sal.
    by_oid: dict[str, list[int]] = {}
    for i, (change, _path) in enumerate(kept):
        if not change.deleted:
            by_oid.setdefault(change.new_oid, []).append(i)
    contents: list[tuple[str, int] | None] = [None] * len(kept)
    try:
        for info, data in git.cat_file_batch(list(by_oid)):
            if info.type != "blob":
                raise ManifestError("missing_blob", info.oid)
            for i in by_oid[info.oid]:
                contents[i] = (blob_commitment(entry_salts[i], data), info.size)
    except MissingObject as exc:
        raise ManifestError("missing_blob", exc.oid) from None

    rows: list[tuple[str, str | None, EntryOpening]] = []
    for i, (change, path) in enumerate(kept):
        content = contents[i]
        rows.append(
            (
                path_commitment(entry_salts[i], change.path),
                content[0] if content is not None else None,
                EntryOpening(
                    path=path,
                    salt=entry_salts[i],
                    blob=None if change.deleted else change.new_oid,
                    size=content[1] if content is not None else None,
                ),
            )
        )
    rows.sort(key=lambda row: row[0])
    committed, actor_openings, actor_skips = _actors(header, actors, salts)
    skipped.extend(actor_skips)

    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "kind": kind,
        "generator": generator,
        "created_at": created_at,
        "forge": FORGE,
        "repository_id": repository_id,
        "commit": commit,
        "tree": header.tree,
        "base": base,
        "commits": commits,
        "task": task,
        "pr": pr,
        "commitment": COMMITMENT,
        "actors": committed,
        "entries": [{"p": p, "c": c} for p, c, _opening in rows],
    }
    data = canon.dumps(manifest)
    sha = hashlib.sha256(data).hexdigest()
    openings = Openings(
        manifest_sha256=sha,
        repository=repository,
        entries=tuple(opening for _p, _c, opening in rows),
        actors=tuple(actor_openings),
    )
    return BuiltManifest(
        kind=kind,
        commit=commit,
        base=base,
        data=data,
        sha256=sha,
        manifest=MappingProxyType(manifest),
        openings=openings,
        plan=plan,
        skipped=tuple(skipped),
    )


def build_submission(
    git: Git,
    *,
    commit: str,
    base_sha: str,
    pr: int,
    task: int,
    repository: str,
    repository_id: int,
    pr_author: str | None,
    created_at: str | None = None,
    salt: SaltSource = new_salt,
    generator: str | None = None,
) -> BuiltManifest:
    """Manifiesto de entrega: la cabeza ``commit`` de un PR de tarea frente a ``merge-base(base_sha, commit)``.

    ``pr`` y ``task`` son obligatorios (el esquema los exige en ``submission``);
    ``pr_author`` es el login de quien abrió el PR (de la API), o ``None`` si no se
    conoce. ``repository`` es ``owner/name`` (va solo en las aperturas) y
    ``repository_id``, el id numérico de la forja. ``created_at`` (RFC 3339 en UTC, sin
    fracciones) y ``generator`` se inyectan en las pruebas; ``salt`` también.
    """
    _oid(commit, "commit")
    _oid(base_sha, "base_sha")
    _number(pr, "pr", optional=False)
    _number(task, "task", optional=False)
    created, gen = _common(repository, repository_id, created_at, generator)
    header = _commit_headers(git, commit, base_sha)
    base = git.merge_base(base_sha, commit)
    if base is None:
        raise ManifestError("no_merge_base", base_sha)
    return _build(
        git,
        kind="submission",
        commit=commit,
        header=header,
        base=base,
        pr=pr,
        task=task,
        repository=repository,
        repository_id=repository_id,
        actors={"pr_author": pr_author},
        created_at=created,
        generator=gen,
        salt=salt,
    )


def build_integration(
    git: Git,
    *,
    commit: str,
    base: str | None,
    repository: str,
    repository_id: int,
    pr: int | None = None,
    task: int | None = None,
    pr_author: str | None = None,
    merged_by: str | None = None,
    created_at: str | None = None,
    salt: SaltSource = new_salt,
    generator: str | None = None,
) -> BuiltManifest:
    """Manifiesto de integración: el push que dejó ``commit`` en la base, desde ``base``.

    ``base`` es el último commit de primer padre ya sellado (de ``first_parent_history``
    y la forja) o ``None`` (árbol vacío: primer registro, ``plan`` verdadero). Debe ser
    ancestro de ``commit``. ``pr``, ``task``, ``pr_author`` y ``merged_by`` vienen del PR
    del commit (``pulls_for_commit``), si lo hay. Lo demás, como en ``build_submission``.
    """
    _oid(commit, "commit")
    if base is not None:
        _oid(base, "base")
    _number(pr, "pr", optional=True)
    _number(task, "task", optional=True)
    created, gen = _common(repository, repository_id, created_at, generator)
    header = _commit_headers(git, commit, base)
    if base is not None and not git.is_ancestor(base, commit):
        raise ManifestError("base_not_ancestor", base)
    return _build(
        git,
        kind="integration",
        commit=commit,
        header=header,
        base=base,
        pr=pr,
        task=task,
        repository=repository,
        repository_id=repository_id,
        actors={"pr_author": pr_author, "merged_by": merged_by},
        created_at=created,
        generator=gen,
        salt=salt,
    )


# -- comprobaciones del núcleo (las que el esquema no expresa) ------------------------------


def check_manifest(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """Problemas de un manifiesto ya leído que el esquema no comprueba (su ``description``).

    Códigos: ``entries_unsorted`` y ``entries_duplicate`` (orden por ``p`` ascendente y
    unicidad), ``actors_duplicate`` (unicidad de ``actors[].r``) y ``commits_last`` (el
    último de ``commits`` debe ser ``commit``). Tupla vacía si no hay ninguno.
    """
    problems: list[str] = []
    entries = manifest.get("entries")
    ps = [e.get("p") for e in entries if isinstance(e, Mapping)] if isinstance(entries, list) else []
    keys = [p for p in ps if isinstance(p, str)]
    if len(set(keys)) != len(keys):
        problems.append("entries_duplicate")
    if any(a > b for a, b in zip(keys, keys[1:])):
        problems.append("entries_unsorted")
    actors = manifest.get("actors")
    roles = [a.get("r") for a in actors if isinstance(a, Mapping)] if isinstance(actors, list) else []
    if len(set(roles)) != len(roles):
        problems.append("actors_duplicate")
    commits = manifest.get("commits")
    if not isinstance(commits, Sequence) or isinstance(commits, str) or not commits:
        problems.append("commits_last")
    elif commits[-1] != manifest.get("commit"):
        problems.append("commits_last")
    return tuple(problems)
