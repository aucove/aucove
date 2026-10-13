# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Infraestructura de pruebas compartida.

- Los repositorios de prueba son temporales y aislados: la identidad va por
  variables de entorno, la configuración global y la del sistema se neutralizan
  (``GIT_CONFIG_GLOBAL`` a un archivo vacío y ``GIT_CONFIG_NOSYSTEM=1``) y nunca
  se ejecuta ``git config``; las opciones puntuales van con ``git -c``.
- Las herramientas de ``tools/`` se importan por ruta, no como paquete ``tools``.
- Un fixture automático bloquea ``socket.connect`` hacia fuera de la máquina,
  salvo en pruebas ``live`` o de ``tests/e2e``.
- Las pruebas que se comparan con la especificación la buscan en ``<PREFIJO>SPEC_DIR``;
  si no está definida, usan ``spec/`` del repo (00-marco D-51), también en el CI.
"""

from __future__ import annotations

import importlib.util
import ipaddress
import os
import socket
import subprocess
import sys
import types
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pytest

from aucove import _meta
from aucove.i18n import Translator, translator

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
FIXTURES: Path = REPO_ROOT / "tests" / "fixtures"

# La especificación vive en spec/ (00-marco D-51): sin <PREFIJO>SPEC_DIR, las pruebas que se
# comparan con ella usan la del repo en vez de omitirse.
SPEC_DIR: Path = REPO_ROOT / "spec"
if (SPEC_DIR / "00-marco.md").is_file():
    os.environ.setdefault(f"{_meta.ENV_PREFIX}SPEC_DIR", str(SPEC_DIR))

# Identidad y fechas fijas: el mismo contenido da siempre los mismos commits.
GIT_IDENTITY: Mapping[str, str] = {
    "GIT_AUTHOR_NAME": "Test Author",
    "GIT_AUTHOR_EMAIL": "author@example.invalid",
    "GIT_AUTHOR_DATE": "2026-09-29T12:00:00+00:00",
    "GIT_COMMITTER_NAME": "Test Committer",
    "GIT_COMMITTER_EMAIL": "committer@example.invalid",
    "GIT_COMMITTER_DATE": "2026-09-29T12:00:00+00:00",
}

# Opciones puntuales de cada llamada a git (nunca 'git config').
GIT_OPTIONS: tuple[str, ...] = (
    "-c", "commit.gpgsign=false",
    "-c", "tag.gpgsign=false",
    "-c", "core.autocrlf=false",
    "-c", "core.eol=lf",
    "-c", "core.fsmonitor=false",
    "-c", "init.defaultBranch=main",
)


def load_tool(name: str) -> types.ModuleType:
    """Importa ``tools/<name>.py`` por ruta, con un nombre de módulo propio."""
    path = REPO_ROOT / "tools" / f"{name}.py"
    module_name = f"_repo_tools_{name}"
    cached = sys.modules.get(module_name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"no se puede cargar {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def roles_fixture(name: str) -> str:
    """Texto de ``tests/fixtures/roles/<name>.yaml``."""
    return (FIXTURES / "roles" / f"{name}.yaml").read_text(encoding="utf-8")


def isolated_git_env(tmp: Path) -> dict[str, str]:
    """Entorno de git aislado bajo ``tmp`` (sin configuración global ni del sistema)."""
    home = tmp / "home"
    home.mkdir(parents=True, exist_ok=True)
    global_config = tmp / "gitconfig-global"
    if not global_config.exists():
        global_config.write_bytes(b"")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update(GIT_IDENTITY)
    env.update(
        {
            "GIT_CONFIG_GLOBAL": str(global_config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
        }
    )
    return env


@pytest.fixture
def git_env(tmp_path: Path) -> dict[str, str]:
    """Entorno aislado para ejecutar git en pruebas."""
    return isolated_git_env(tmp_path / "git-env")


class GitRepo:
    """Repositorio temporal con un remoto bare ``origin`` en disco (sin red)."""

    def __init__(self, path: Path, origin: Path, env: Mapping[str, str]) -> None:
        self.path = path
        self.origin = origin
        self.env: Mapping[str, str] = dict(env)
        self.calls: list[tuple[str, ...]] = []  # subórdenes de git ejecutadas

    def _run(self, cwd: Path, args: Sequence[str], check: bool) -> subprocess.CompletedProcess[bytes]:
        if args and args[0] == "config":
            raise AssertionError("las pruebas no ejecutan git config")
        self.calls.append(tuple(args))
        proc = subprocess.run(
            ["git", *GIT_OPTIONS, *args],
            cwd=cwd,
            env=dict(self.env),
            capture_output=True,
            check=False,
        )
        if check and proc.returncode != 0:
            raise subprocess.CalledProcessError(proc.returncode, ["git", *args], proc.stdout, proc.stderr)
        return proc

    def git(self, *args: str, check: bool = True) -> str:
        """Ejecuta git en el repositorio; devuelve stdout sin el salto final."""
        proc = self._run(self.path, args, check)
        return proc.stdout.decode("utf-8", errors="replace").rstrip("\n")

    def write(self, files: Mapping[str, str | bytes]) -> None:
        """Escribe archivos relativos a la raíz; ``str`` se guarda en UTF-8 con LF."""
        for rel, content in files.items():
            target = self.path / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, str):
                data = content.replace("\r\n", "\n").encode("utf-8")
            else:
                data = content
            target.write_bytes(data)

    def commit(
        self,
        message: str = "commit",
        *,
        files: Mapping[str, str | bytes] | None = None,
        remove: Sequence[str] = (),
        author: str | None = None,
        allow_empty: bool = False,
    ) -> str:
        """Escribe ``files``, borra ``remove``, agrega todo y hace un commit; devuelve el sha."""
        if files:
            self.write(files)
        for rel in remove:
            self.git("rm", "-r", "-q", "--", rel)
        self.git("add", "-A")
        args = ["commit", "-q", "--no-verify", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        if author is not None:
            ident = author if "<" in author else f"{author} <{author}@example.invalid>"
            args.append(f"--author={ident}")
        self.git(*args)
        return self.head()

    def branch(self, name: str, at: str | None = None) -> None:
        self.git("branch", name, *([at] if at else []))

    def checkout(self, name: str) -> None:
        self.git("checkout", "-q", name)

    def merge_no_ff(self, branch: str, message: str = "merge") -> str:
        """Commit de fusión con ``--no-ff``; su primer padre es la rama actual."""
        self.git("merge", "-q", "--no-ff", "--no-edit", "-m", message, branch)
        return self.head()

    def push(self, *refspecs: str) -> None:
        self.git("push", "-q", "origin", *(refspecs or ("HEAD",)))

    def fetch(self) -> None:
        self.git("fetch", "-q", "--prune", "origin")

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def git_repo(tmp_path: Path, git_env: dict[str, str]) -> GitRepo:
    """Repositorio ``main`` sin commits, con ``origin`` bare local."""
    path = tmp_path / "repo"
    origin = tmp_path / "origin.git"
    path.mkdir()
    repo = GitRepo(path, origin, git_env)
    repo._run(tmp_path, ["init", "-q", "--bare", "-b", "main", str(origin)], True)
    repo.git("init", "-q", "-b", "main")
    repo.git("remote", "add", "origin", str(origin))
    return repo


@pytest.fixture
def t_es() -> Translator:
    return translator("es")


@pytest.fixture
def t_en() -> Translator:
    return translator("en")


# -- sin red en las pruebas unitarias -----------------------------------------------


def _is_local_address(address: object) -> bool:
    if isinstance(address, (str, bytes)):  # AF_UNIX
        return True
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(str(host).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _block_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Bloquea conexiones fuera de la máquina salvo en pruebas live o de tests/e2e."""
    node_path = Path(str(request.node.path)).resolve()
    e2e = REPO_ROOT / "tests" / "e2e"
    if request.node.get_closest_marker("live") is not None or e2e in node_path.parents:
        yield
        return
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def guarded_connect(self: socket.socket, address: object) -> None:
        if not _is_local_address(address):
            raise OSError("red bloqueada en pruebas unitarias (usa el marcador live)")
        return real_connect(self, address)  # type: ignore[arg-type]

    def guarded_connect_ex(self: socket.socket, address: object) -> int:
        if not _is_local_address(address):
            raise OSError("red bloqueada en pruebas unitarias (usa el marcador live)")
        return real_connect_ex(self, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    yield
