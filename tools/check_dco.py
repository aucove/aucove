# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Check de DCO de los PR al repositorio del framework (11-trazabilidad REQ-TRACE-66).

Uso::

    python tools/check_dco.py --base REV --head REV [--pr-author LOGIN] [--repo DIR]

- Revisa cada commit de ``git log BASE..HEAD``, fusiones incluidas («todo commit de
  un PR»). Cada uno DEBE llevar un trailer ``Signed-off-by: Nombre <correo>`` con el
  mismo nombre y correo que su autor. El nombre se compara exacto; el correo, sin
  distinguir mayúsculas.
- Los trailers los interpreta git (``%(trailers:key=Signed-off-by)``): cuentan solo
  los del último párrafo del mensaje, como los escribe ``git commit -s`` y como los
  pide CONTRIBUTING.md.
- ``--pr-author dependabot[bot]`` exime el PR entero (00-marco D-07 c): el login es el
  del usuario que abre el PR según la API de GitHub (``github.event.pull_request.user``),
  no el correo del commit, que se puede falsificar.
- Solo usa la biblioteca estándar y git; no usa la red. Los textos del commit
  (asunto, nombre) son del autor del PR: se escriben en una línea, sin controles,
  recortados y con sangría, para que nunca formen un comando de Actions (``::``).

Salida: 0 si todos los commits cumplen o el PR está exento; 1 si alguno no cumple o
si el rango no se puede leer (falla cerrada).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

EXEMPT_AUTHORS: frozenset[str] = frozenset({"dependabot[bot]"})
SIGNOFF_KEY = "Signed-off-by"
MAX_SHOWN = 80
GIT_TIMEOUT_S = 60.0

# Campos separados por NUL: un nombre, un correo o un asunto de git no pueden contenerlo.
_FORMAT = "%an%x00%ae%x00%s%x00%(trailers:key=" + SIGNOFF_KEY + ",valueonly,unfold,separator=%x00)"
_IDENT = re.compile(r"^(?P<name>.*?)\s*<(?P<email>[^<>]*)>\s*$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")

GitRunner = Callable[[Sequence[str]], str]


class DcoError(Exception):
    """El rango no se puede leer: el check falla cerrado."""


@dataclass(frozen=True, slots=True)
class Commit:
    sha: str
    author_name: str
    author_email: str
    subject: str
    signoffs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Problem:
    sha: str
    subject: str
    reason: str  # 'missing' o 'mismatch'
    expected: str
    found: tuple[str, ...]


def shown(text: str, limit: int = MAX_SHOWN) -> str:
    """Texto del autor del PR en una línea, sin controles y recortado."""
    flat = " ".join(_CONTROL.sub(" ", text).split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def parse_ident(value: str) -> tuple[str, str] | None:
    """``'Nombre <correo>'`` -> ``(nombre, correo)``; ``None`` si no tiene esa forma."""
    m = _IDENT.match(value.strip())
    if m is None:
        return None
    return m.group("name").strip(), m.group("email").strip()


def signoff_matches(value: str, name: str, email: str) -> bool:
    """El trailer certifica al autor: mismo nombre (exacto) y mismo correo (sin mayúsculas)."""
    ident = parse_ident(value)
    if ident is None:
        return False
    s_name, s_email = ident
    return bool(s_email) and s_name == name.strip() and s_email.casefold() == email.strip().casefold()


def check_commit(commit: Commit) -> Problem | None:
    """``None`` si el commit cumple REQ-TRACE-66; si no, el problema."""
    expected = f"{commit.author_name} <{commit.author_email}>"
    if any(signoff_matches(v, commit.author_name, commit.author_email) for v in commit.signoffs):
        return None
    reason = "mismatch" if commit.signoffs else "missing"
    return Problem(commit.sha, commit.subject, reason, expected, commit.signoffs)


def is_exempt(pr_author: str | None) -> bool:
    """D-07 (c): el PR lo abrió ``dependabot[bot]`` según la API."""
    return pr_author is not None and pr_author.strip().casefold() in EXEMPT_AUTHORS


def default_git(repo: Path, env: Mapping[str, str] | None = None) -> GitRunner:
    """Ejecuta git en ``repo`` sin pedir credenciales ni tomar candados opcionales."""
    git_env = dict(os.environ if env is None else env)
    git_env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})

    def run(args: Sequence[str]) -> str:
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo), *args],
                env=git_env,
                capture_output=True,
                check=False,
                timeout=GIT_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DcoError(f"no se pudo ejecutar git: {type(exc).__name__}") from exc
        if proc.returncode != 0:
            detail = shown(proc.stderr.decode("utf-8", errors="replace"), 200)
            raise DcoError(f"git {args[0]} salió con {proc.returncode}: {detail}")
        return proc.stdout.decode("utf-8", errors="replace")

    return run


def _check_rev(rev: str) -> str:
    # una revisión que empieza por '-' sería una opción de git
    if not rev or rev.startswith("-") or any(c in rev for c in "\x00\r\n"):
        raise DcoError(f"revisión inválida: {shown(rev)!r}")
    return rev


def commits_in_range(git: GitRunner, base: str, head: str) -> list[Commit]:
    """Commits de ``base..head`` (fusiones incluidas), del más nuevo al más viejo."""
    base_sha = git(["rev-parse", "--verify", "--quiet", _check_rev(base) + "^{commit}"]).strip()
    head_sha = git(["rev-parse", "--verify", "--quiet", _check_rev(head) + "^{commit}"]).strip()
    if not _SHA.match(base_sha) or not _SHA.match(head_sha):
        raise DcoError("el rango no resuelve a commits")
    shas = [line.strip() for line in git(["rev-list", f"{base_sha}..{head_sha}"]).splitlines() if line.strip()]
    out: list[Commit] = []
    for sha in shas:
        if not _SHA.match(sha):
            raise DcoError("salida inesperada de git rev-list")
        fields = git(["log", "-1", f"--format={_FORMAT}", sha]).rstrip("\n").split("\x00")
        if len(fields) < 3:
            raise DcoError(f"salida inesperada de git log para {sha[:12]}")
        name, email, subject = fields[0], fields[1], fields[2]
        signoffs = tuple(v.strip() for v in fields[3:] if v.strip())
        out.append(Commit(sha, name, email, subject, signoffs))
    return out


def report(problems: Sequence[Problem], total: int) -> str:
    """Informe legible; cada línea con texto del autor va con sangría (nunca empieza por ``::``)."""
    if not problems:
        return f"DCO: los {total} commits llevan Signed-off-by del autor (REQ-TRACE-66).\n"
    lines = [f"DCO: {len(problems)} de {total} commits no cumplen REQ-TRACE-66:"]
    for p in problems:
        lines.append(f"  {p.sha[:12]}  {shown(p.subject)}")
        lines.append(f"    se esperaba: {SIGNOFF_KEY}: {shown(p.expected, 200)}")
        if p.reason == "missing":
            lines.append(f"    no lleva {SIGNOFF_KEY} en el último párrafo del mensaje")
        else:
            for value in p.found:
                lines.append(f"    lleva:       {SIGNOFF_KEY}: {shown(value, 200)}")
    lines.append(
        "  Corrige con 'git commit --amend -s' (o 'git rebase --signoff <base>') y vuelve a hacer push."
    )
    return "\n".join(lines) + "\n"


def run_check(
    base: str, head: str, *, pr_author: str | None, git: GitRunner
) -> tuple[int, str]:
    """``(código de salida, informe)``."""
    if is_exempt(pr_author):
        return 0, f"DCO: PR abierto por {shown(pr_author or '')}; exento por 00-marco D-07 (c).\n"
    try:
        commits = commits_in_range(git, base, head)
    except DcoError as exc:
        return 1, f"DCO: no se pudo leer el rango ({exc}); el check falla cerrado.\n"
    problems = [p for c in commits if (p := check_commit(c)) is not None]
    return (1 if problems else 0), report(problems, len(commits))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_dco.py",
        description="Comprueba el Signed-off-by (DCO 1.1) de cada commit de BASE..HEAD (REQ-TRACE-66).",
    )
    parser.add_argument("--base", required=True, help="revisión base (excluida)")
    parser.add_argument("--head", required=True, help="revisión de la cabeza del PR (incluida)")
    parser.add_argument("--pr-author", default=None, help="login del autor del PR según la API de GitHub")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="checkout con la historia (por defecto, el cwd)")
    return parser


def main(argv: Sequence[str] | None = None, *, git: GitRunner | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # error de uso: falla cerrada con 1, como el resto de tools/
        return 0 if exc.code == 0 else 1
    runner = git if git is not None else default_git(args.repo)
    code, text = run_check(args.base, args.head, pr_author=args.pr_author, git=runner)
    stream = sys.stdout if code == 0 else sys.stderr
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):  # asuntos con cualquier carácter, también en una consola cp1252
        reconfigure(encoding="utf-8", errors="replace")
    stream.write(text)
    stream.flush()
    return code


if __name__ == "__main__":
    sys.exit(main())
