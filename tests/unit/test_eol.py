# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Finales de línea del repositorio (.gitattributes; 11-trazabilidad §7).

- ``git check-attr`` da ``eol=lf`` a todo archivo de texto no ignorado y ``binary``
  a los binarios.
- Ningún archivo de texto tiene CRLF en el árbol de trabajo.
- Con el índice no vacío (CI), ``git ls-files --eol`` no muestra ``w/crlf`` ni ``i/crlf``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT, isolated_git_env

BINARY_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".gz", ".tgz", ".zip"})

EXPECTED_GITATTRIBUTES = (
    "* text=auto eol=lf\n"
    "*.png binary\n*.jpg binary\n*.jpeg binary\n*.gif binary\n*.ico binary\n"
    "*.pdf binary\n*.gz binary\n*.tgz binary\n*.zip binary\n"
)


@pytest.fixture(scope="module")
def git(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    if shutil.which("git") is None:
        pytest.skip("git no está disponible")
    env = isolated_git_env(tmp_path_factory.mktemp("eol-git"))

    def run(*args: str, stdin: bytes | None = None) -> bytes:
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=off", *args],
            cwd=REPO_ROOT, env=env, input=stdin, capture_output=True, check=False,
        )
        if proc.returncode != 0:
            pytest.skip(f"git {' '.join(args[:2])} falló: {proc.stderr.decode(errors='replace')[:200]}")
        return proc.stdout

    if run("rev-parse", "--is-inside-work-tree").strip() != b"true":
        pytest.skip("la raíz no es un árbol de trabajo de git")
    return run


def _files(git) -> list[str]:  # type: ignore[no-untyped-def]
    out = git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return sorted({name for name in out.decode("utf-8").split("\0") if name and (REPO_ROOT / name).is_file()})


def _attrs(git, files: list[str]) -> dict[str, dict[str, str]]:  # type: ignore[no-untyped-def]
    out = git("check-attr", "-z", "--stdin", "eol", "binary", "text", stdin="\0".join(files).encode("utf-8") + b"\0")
    fields = out.decode("utf-8").split("\0")
    attrs: dict[str, dict[str, str]] = {}
    for i in range(0, len(fields) - 2, 3):
        path, attr, value = fields[i : i + 3]
        attrs.setdefault(path, {})[attr] = value
    return attrs


def test_gitattributes_content() -> None:
    assert (REPO_ROOT / ".gitattributes").read_bytes() == EXPECTED_GITATTRIBUTES.encode("ascii")


def test_check_attr_gives_lf_and_binary(git) -> None:  # type: ignore[no-untyped-def]
    files = _files(git)
    assert files, "no hay archivos que revisar"
    attrs = _attrs(git, files)
    wrong = []
    for name in files:
        a = attrs.get(name, {})
        if Path(name).suffix.lower() in BINARY_SUFFIXES:
            if a.get("binary") != "set":
                wrong.append(f"{name}: binary={a.get('binary')}")
        elif a.get("eol") != "lf":
            wrong.append(f"{name}: eol={a.get('eol')}")
    assert wrong == []


def test_no_crlf_in_text_files(git) -> None:  # type: ignore[no-untyped-def]
    offenders = []
    for name in _files(git):
        if Path(name).suffix.lower() in BINARY_SUFFIXES:
            continue
        data = (REPO_ROOT / name).read_bytes()
        if b"\0" in data[:8000]:
            continue  # binario sin extensión conocida: text=auto no lo convierte
        if b"\r\n" in data:
            offenders.append(name)
    assert offenders == []


def _crlf_lines(listing: str) -> list[str]:
    bad = []
    for line in listing.splitlines():
        info = line.split("\t", 1)[0]
        if "w/crlf" in info or "i/crlf" in info:
            bad.append(line)
    return bad


def test_index_has_no_crlf(git) -> None:  # type: ignore[no-untyped-def]
    listing = git("ls-files", "--eol").decode("utf-8")
    if not listing.strip():
        pytest.skip("índice vacío: sin commits ni archivos agregados (se comprueba en CI)")
    assert _crlf_lines(listing) == []


def test_temporary_index_has_no_crlf(tmp_path: Path) -> None:
    """Igual que la anterior, con un índice y un almacén de objetos temporales.

    No toca el índice ni los objetos del repositorio: ``GIT_INDEX_FILE`` y
    ``GIT_OBJECT_DIRECTORY`` apuntan a ``tmp_path``.
    """
    if shutil.which("git") is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("sin git o sin repositorio")
    env = isolated_git_env(tmp_path / "env")
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=REPO_ROOT, env=env, capture_output=True, check=False,
    )
    if common.returncode != 0:
        pytest.skip("no se puede ubicar el directorio de git")
    objects = Path(common.stdout.decode("utf-8").strip()) / "objects"
    (tmp_path / "objects").mkdir()
    env.update(
        {
            "GIT_INDEX_FILE": str(tmp_path / "index"),
            "GIT_OBJECT_DIRECTORY": str(tmp_path / "objects"),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(objects),
        }
    )
    add = subprocess.run(["git", "add", "-A"], cwd=REPO_ROOT, env=env, capture_output=True, check=False)
    assert add.returncode == 0, add.stderr.decode(errors="replace")
    listing = subprocess.run(
        ["git", "-c", "core.quotepath=off", "ls-files", "--eol"], cwd=REPO_ROOT, env=env, capture_output=True, check=True
    ).stdout.decode("utf-8")
    assert listing.strip()
    assert _crlf_lines(listing) == []
    wrong_attr = []
    for line in listing.splitlines():
        info, _, name = line.partition("\t")
        if Path(name).suffix.lower() not in BINARY_SUFFIXES and "eol=lf" not in info:
            wrong_attr.append(line)
    assert wrong_attr == []
