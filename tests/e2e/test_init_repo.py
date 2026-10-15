# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""``init --repo`` con el lanzador instalado: en CI, la rueda (00-marco D-73; 12-mvp A1 a y A6).

Sin terminal, la barrera de las operaciones H se niega antes de escribir nada. Con una terminal
(un pseudo-TTY, solo en Linux y macOS), ``--dry-run`` recorre el plan entero con la plantilla y los
valores que trae el paquete, la forja falsa y un repo bare vacío, y tampoco escribe nada. La URL de
GitHub llega al repo bare por ``url.<base>.insteadOf``, en el entorno.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from aucove.forge.base import RepoRef
from aucove.forge.fake import FakeClock, FakeForge
from tests.board_world import NOW
from tests.e2e.conftest import PREFIX, Launcher, base_env, git

REPO = RepoRef("lab-x", "estudio")
SLUG = f"{REPO.owner}/{REPO.name}"
URL = f"https://github.com/{SLUG}.git"


def fresh(tmp: Path) -> tuple[Path, dict[str, str], Path]:
    """La carpeta vacía, el entorno del lanzador con el falso guardado y el repo bare sin ramas."""
    folder = tmp / "estudio"
    folder.mkdir()
    env = base_env(tmp)
    origin = tmp / "origin.git"
    git(tmp, env, "init", "-q", "--bare", str(origin))
    env.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": f"url.{origin.as_uri()}.insteadOf",
                "GIT_CONFIG_VALUE_0": URL})
    forge = FakeForge(REPO, viewer="ana", clock=FakeClock(NOW), profile="enforced")
    forge.seed_user("ana", permission="admin")
    forge.seed_admin(fresh=True)
    forge.save(Path(env[PREFIX + "FORGE_FAKE_STATE"]))
    return folder, env, origin


def heads(tmp: Path, env: dict[str, str], origin: Path) -> str:
    return git(tmp, env, "ls-remote", "--heads", str(origin)).strip()


def in_terminal(argv: Sequence[str], *, cwd: Path, env: dict[str, str], timeout_s: float = 300) -> tuple[int, str]:
    """Corre ``argv`` con stdin, stdout y stderr en un pseudo-TTY; devuelve la salida y lo que escribió en él.

    stderr también va a la terminal: con ``--json``, la CLI manda stdout a stderr mientras corre la
    operación (05 REQ-OPS-9), así que la barrera de las operaciones H mira stdin y stderr.
    """
    import pty

    master, slave = pty.openpty()
    proc = subprocess.Popen(list(argv), cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    chunks: list[bytes] = []

    def pump() -> None:
        while True:
            try:
                data = os.read(master, 65536)
            except OSError:  # EIO en Linux: el otro extremo se cerró
                return
            if not data:
                return
            chunks.append(data)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=timeout_s)
    finally:
        if proc.poll() is None:
            proc.kill()
        reader.join(timeout=10)
        os.close(master)
    return proc.returncode, b"".join(chunks).decode("utf-8", "replace").replace("\r\n", "\n")


def test_without_a_terminal_nothing_is_written(launcher: Launcher, tmp_path: Path) -> None:
    folder, env, origin = fresh(tmp_path)
    run = launcher.run(["init", "--repo", SLUG, "--language", "es", "--json"], cwd=folder, env=env)
    assert run.returncode != 0, run.explain()
    envelope = json.loads(run.stdout)
    assert envelope["ok"] is False and envelope["error"]["code"] == "E_HUMAN_ONLY_NO_TTY", run.explain()
    assert os.listdir(folder) == [] and heads(tmp_path, env, origin) == ""


@pytest.mark.skipif(sys.platform == "win32", reason="pseudo-TTY: solo en Linux y macOS")
def test_dry_run_in_a_terminal_lists_the_plan_and_writes_nothing(launcher: Launcher, tmp_path: Path) -> None:
    folder, env, origin = fresh(tmp_path)
    code, out = in_terminal([launcher.path, "init", "--repo", SLUG, "--language", "es", "--dry-run", "--json"],
                            cwd=folder, env=env)
    line = next((ln for ln in out.splitlines() if ln.startswith('{"schema"')), None)  # el sobre, en una línea
    assert line is not None, out
    envelope = json.loads(line)
    assert code == 0 and envelope["ok"] is True, out
    plan = envelope["data"]["plan"]
    targets = {(e["kind"], e["target"]) for e in plan}
    assert ("local_write", ".git") in targets and ("file_write", "project.yaml") in targets
    assert ("file_write", ".mcp.json") in targets and ("git_push", "main") in targets
    assert os.listdir(folder) == [] and heads(tmp_path, env, origin) == ""
