# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Token de la forja GitHub: fuentes, clase y huella (08-forja §5).

- ``resolve_token`` consulta las fuentes en el orden de REQ-FORGE-37: la variable propia
  (``<PREFIJO>GITHUB_TOKEN``), ``GH_TOKEN``, ``GITHUB_TOKEN`` y, al final, ``gh auth
  token``, sin shell y con 5 s de timeout (REQ-FORGE-38). Si ninguna da token, lanza
  ``NoTokenError``; nunca abre un aviso interactivo.
- ``classify_token`` da el ``token_kind`` por el prefijo (§5.2) y ``token_fp`` la huella
  ``sha256(token)[:12]`` de las claves de caché (REQ-FORGE-6).
- El valor del token no aparece en ``repr`` ni en los mensajes: ``Token`` lo oculta.

``gh`` lo ejecuta quien llama, por medio de un ``ToolRunner``: la fábrica de ``ops`` lo
resuelve fuera del repositorio y del directorio actual (01-arquitectura REQ-ARCH-64; S4).
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from .. import _meta
from .base import ForgeAuthError, NoTokenError, TokenKind, TokenSource, token_sources

__all__ = [
    "GH_AUTH_ARGS",
    "GH_TIMEOUT_S",
    "PROXY_TOKEN",
    "Token",
    "ToolRunner",
    "classify_token",
    "is_ci_token",
    "make_token",
    "resolve_token",
    "token_fp",
]

GH_TIMEOUT_S = 5.0  # REQ-FORGE-38
GH_AUTH_ARGS: tuple[str, ...] = ("auth", "token", "--hostname", "github.com")
PROXY_TOKEN = "proxy-injected"  # marcador de las sesiones web (08 §8.1)

# Prefijo -> token_kind (08 §5.2).
_PREFIXES: tuple[tuple[str, TokenKind], ...] = (
    ("github_pat_", "fine_grained_pat"),
    ("ghp_", "classic_pat"),
    ("gho_", "oauth"),
    ("ghu_", "app_user"),
    ("ghs_", "app_installation"),
)


class ToolRunner(Protocol):
    """Ejecuta ``gh <args>`` sin shell y con timeout; ``extra_env`` se suma a su entorno.

    Lanza ``OSError`` (``FileNotFoundError`` si ``gh`` no está) si no se puede lanzar y
    ``subprocess.TimeoutExpired`` si vence el plazo.
    """

    def __call__(
        self,
        args: Sequence[str],
        *,
        timeout: float,
        input: bytes | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[bytes]: ...


@dataclass(frozen=True, slots=True)
class Token:
    """Token resuelto. ``value`` no sale en ``repr`` (REQ-FORGE-6)."""

    value: str = field(repr=False)
    source: TokenSource
    kind: TokenKind
    fp: str


def classify_token(value: str) -> TokenKind:
    """``token_kind`` por el prefijo (08 §5.2); ``proxy`` para el marcador de la web."""
    if value == PROXY_TOKEN:
        return "proxy"
    for prefix, kind in _PREFIXES:
        if value.startswith(prefix):
            return kind
    return "unknown"


def token_fp(value: str) -> str:
    """Huella del token para las claves de caché: ``sha256(token).hexdigest()[:12]``."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def make_token(value: str, source: TokenSource) -> Token:
    """``Token`` de ``value``; ``ForgeAuthError`` si no es imprimible ASCII sin espacios.

    Un token con espacios o saltos de línea rompería la cabecera ``Authorization``.
    """
    if not value or any(not 0x21 <= ord(c) <= 0x7E for c in value):
        raise ForgeAuthError(f"the token from {source} has invalid characters")
    return Token(value, source, classify_token(value), token_fp(value))


def resolve_token(
    env: Mapping[str, str], run: ToolRunner | None = None, *, hint: str | None = None
) -> Token:
    """Primer token de las fuentes de REQ-FORGE-37; ``NoTokenError`` si ninguna lo da.

    Sin ``run`` no se consulta ``gh``. Un ``gh`` ausente, que falla o que no responde en
    5 s cuenta como fuente vacía (REQ-FORGE-38).
    """
    sources = token_sources(_meta.ENV_PREFIX)
    for var in sources[:-1]:
        value = (env.get(var) or "").strip()
        if value:
            return make_token(value, var)
    if run is not None:
        try:
            proc = run(GH_AUTH_ARGS, timeout=GH_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError):
            proc = None
        if proc is not None and proc.returncode == 0:
            lines = proc.stdout.decode("utf-8", "replace").strip().splitlines()
            value = lines[0].strip() if lines else ""
            if value:
                return make_token(value, sources[-1])
    raise NoTokenError("no GitHub token found", hint=hint)


def is_ci_token(env: Mapping[str, str], token: Token) -> bool:
    """¿Es el token de Actions de un job? (REQ-FORGE-43).

    Solo si ``GITHUB_ACTIONS == "true"``, ``GITHUB_RUN_ID`` no está vacío y el token es
    de instalación (``ghs_``).
    """
    return (
        env.get("GITHUB_ACTIONS") == "true"
        and bool((env.get("GITHUB_RUN_ID") or "").strip())
        and token.kind == "app_installation"
    )
