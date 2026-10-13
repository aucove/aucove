# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""El ``.gitignore`` de la plantilla ignora todos los secretos de REQ-SEC-35.

10-seguridad REQ-SEC-35 (DEBE) exige ``.env``, ``.env.*``, ``*.pem``, ``*.key``, ``*.p12``,
``*.pfx``, ``id_rsa*`` e ``id_ed25519*``. El bloque ``[req] secretos`` copiaba el de
02-convencion §9, que solo trae los tres primeros. Una clave SSH o un certificado PKCS#12
dejados en el árbol entraban al commit de ``aucove save``, y solo los detenía gitleaks en
CI, después del push. 02 §9 fija las líneas mínimas, así que añadir las cinco cumple los dos
capítulos.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GITIGNORE = REPO_ROOT / "template" / "base" / ".gitignore"

# Patrones de secretos de REQ-SEC-35, en el orden del requisito.
SEC35_SECRETS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
)
SECRETS_HEADER = "# [req] secretos (defensa en profundidad)"

# Rutas que el .gitignore debe ignorar (en la raíz y en subcarpetas) y rutas de control.
SECRET_PATHS: tuple[str, ...] = (
    "id_rsa",
    "id_rsa.pub",
    "id_ed25519",
    "keys/id_ed25519_sk",
    "server.key",
    "tls/server.key",
    "cert.p12",
    "certs/cert.pfx",
    "ca.pem",
    ".env",
    ".env.local",
    "code/.env",
    ".aucove/cache.json",
    ".claude/settings.local.json",
)
TRACKED_PATHS: tuple[str, ...] = (
    "tex/main.tex",
    "notes/keyboard.md",
    "memory/notes/key-ideas.md",
    "data/README.md",
    ".claude/settings.json",
)


def _lines() -> list[str]:
    return GITIGNORE.read_bytes().decode("utf-8").split("\n")


def _secrets_block() -> list[str]:
    """Líneas del bloque ``[req] secretos`` hasta el siguiente comentario."""
    lines = _lines()
    start = lines.index(SECRETS_HEADER)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("#")), len(lines))
    return [line for line in lines[start + 1 : end] if line]


def test_secrets_block_has_every_req_sec_35_pattern() -> None:
    assert _secrets_block() == list(SEC35_SECRETS)


def _ignored(repo: Path, env: dict[str, str], paths: tuple[str, ...]) -> dict[str, bool]:
    """``git check-ignore`` sobre ``paths`` (no hace falta que existan)."""
    proc = subprocess.run(
        ["git", "check-ignore", "--no-index", "-v", "-n", *paths],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
    )
    assert proc.returncode in (0, 1), proc.stderr.decode("utf-8", "replace")
    result: dict[str, bool] = {}
    for line in proc.stdout.decode("utf-8").splitlines():
        info, _, path = line.partition("\t")
        pattern = info.split(":", 2)[2] if info != "::" else ""
        result[path] = bool(pattern) and not pattern.startswith("!")
    return result


def test_git_ignores_every_secret_and_keeps_project_files(tmp_path: Path, git_env: dict[str, str]) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, env=git_env, check=True, capture_output=True)
    (repo / ".gitignore").write_bytes(GITIGNORE.read_bytes())
    found = _ignored(repo, git_env, SECRET_PATHS + TRACKED_PATHS)
    assert {p for p in SECRET_PATHS if not found.get(p)} == set()
    assert {p for p in TRACKED_PATHS if found.get(p)} == set()
