# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Apoyo de las pruebas de evidencia (EV-04): historias de git por ``fast-import`` y esquemas.

- ``Stream`` arma una historia completa en un solo proceso de ``git fast-import``: es
  rápido y admite rutas en bytes (NFD, no UTF-8), gitlinks y fusiones. Autor, committer y
  fechas son fijos, así que los commits son los mismos en cualquier máquina.
- ``validate`` valida contra los esquemas empaquetados con el validador del núcleo
  (``config.schemas``: draft 2020-12 con el ``$`` de ECMA-262).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from aucove.config import schemas as schemas_mod
from aucove.config.datafiles import data_path
from tests.conftest import GIT_OPTIONS, GitRepo, isolated_git_env

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git no está disponible")

AUTHOR = b"Ana P\xc3\xa9rez <ana@example.invalid>"
COMMITTER = b"GitHub <noreply@example.invalid>"
EPOCH = 1_790_000_000  # segundos desde 1970: fechas fijas y crecientes


def new_repo(tmp: Path, name: str = "repo") -> GitRepo:
    """Repositorio vacío en ``tmp/name`` con el entorno aislado de las pruebas (sin ``origin``)."""
    env = isolated_git_env(tmp / f"{name}-env")
    path = tmp / name
    path.mkdir(parents=True)
    repo = GitRepo(path, tmp / f"{name}-origin.git", env)
    repo.git("init", "-q", "-b", "main")
    return repo


def git_bytes(repo: GitRepo, *args: str, input: bytes | None = None, cwd: Path | None = None) -> bytes:
    """git con entrada y salida en bytes; nunca ``git config``."""
    if args and args[0] == "config":
        raise AssertionError("las pruebas no ejecutan git config")
    proc = subprocess.run(
        ["git", *GIT_OPTIONS, *args],
        cwd=cwd or repo.path,
        env=dict(repo.env),
        input=input,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {args[0]}: {proc.stderr.decode('utf-8', 'replace')[:500]}")
    return proc.stdout


@dataclass(frozen=True)
class Gitlink:
    """Entrada ``160000`` (submódulo) que apunta a ``sha``."""

    sha: str


@dataclass
class Stream:
    """Historia para ``git fast-import``; ``commit`` devuelve una marca y ``run`` los sha."""

    parts: list[bytes] = field(default_factory=list)
    marks: int = 0
    when: int = EPOCH

    def blob(self, data: bytes) -> str:
        self.marks += 1
        mark = f":{self.marks}"
        self.parts.append(b"blob\nmark " + mark.encode() + b"\n" + b"data %d\n" % len(data) + data + b"\n")
        return mark

    def commit(
        self,
        branch: str,
        changes: Mapping[bytes, bytes | str | Gitlink | None],
        *,
        message: str = "m",
        parent: str | None = None,
        merge: Sequence[str] = (),
        author: bytes = AUTHOR,
        committer: bytes = COMMITTER,
        encoding: str | None = None,
    ) -> str:
        """Commit en ``branch`` con ``changes``: ruta -> bytes, marca de blob (``:n``), ``Gitlink`` o ``None`` (borrar).

        Sin ``parent``, sigue la punta de la rama (o es raíz si la rama es nueva). El árbol
        de una fusión es el del primer padre más ``changes``: fast-import no fusiona.
        """
        self.marks += 1
        mark = f":{self.marks}"
        self.when += 60
        msg = message.encode("utf-8")
        lines = [
            b"commit refs/heads/" + branch.encode(),
            b"mark " + mark.encode(),
            b"author " + author + b" %d +0000" % self.when,
            b"committer " + committer + b" %d +0000" % self.when,
        ]
        if encoding is not None:
            lines.append(b"encoding " + encoding.encode())
        lines += [b"data %d" % len(msg), msg]
        if parent is not None:
            lines.append(b"from " + parent.encode())
        lines += [b"merge " + m.encode() for m in merge]
        for path, content in changes.items():
            if content is None:
                lines.append(b"D " + path)
            elif isinstance(content, Gitlink):
                lines.append(b"M 160000 " + content.sha.encode() + b" " + path)
            elif isinstance(content, str):
                lines.append(b"M 100644 " + content.encode() + b" " + path)
            else:
                lines += [b"M 100644 inline " + path, b"data %d" % len(content), content]
        self.parts.append(b"\n".join(lines) + b"\n\n")
        return mark

    def run(self, repo: GitRepo) -> dict[str, str]:
        """Importa la historia en ``repo``; devuelve marca -> sha."""
        marks = repo.path.parent / f"{repo.path.name}-marks.txt"
        git_bytes(repo, "fast-import", "--quiet", f"--export-marks={marks}", input=b"".join(self.parts))
        out: dict[str, str] = {}
        for line in marks.read_text(encoding="ascii").splitlines():
            mark, _, sha = line.partition(" ")
            out[mark] = sha
        return out


# -- esquemas ---------------------------------------------------------------------------


def _schema(kind: str) -> dict[str, Any]:
    path = data_path("schemas") / f"{kind}.schema.json"
    return dict(json.loads(path.read_text(encoding="utf-8")))


def validate(kind: str, instance: object) -> list[str]:
    """Errores de ``instance`` contra ``schemas/<kind>.schema.json`` (vacío si valida)."""
    cls = schemas_mod.validator_class()
    validator = cls(_schema(kind), registry=schemas_mod.registry(), format_checker=schemas_mod.format_checker())
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in validator.iter_errors(instance)]
