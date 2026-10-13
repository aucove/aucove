# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Punto de entrada del script: despacha por el primer argumento (01 §3.3).

Solo usa la biblioteca estándar en el nivel superior; cada envoltura se importa
de forma perezosa, para que la ruta caliente de hooks no cargue el parser de la
CLI ni nada pesado. ``hook``, ``mcp`` y ``ci`` no emiten el sobre (REQ-OPS-8).
"""

from __future__ import annotations

import sys

from . import _meta

# Salida de bloqueo o retroalimentación de 'hook' (07-hooks §3.2; REQ-OPS-14).
_HOOK_BLOCK_EXIT = 2
# Salidas que admite Claude Code de 'hook' (REQ-HOOK-13): con 1, la acción pasa sin bloquear.
_HOOK_EXITS = frozenset({0, _HOOK_BLOCK_EXIT})
# Eventos que, ante un fallo propio del hook, salen con 2 (07-hooks §3.4): PreToolUse bloquea
# (REQ-HOOK-5) y PostToolUse(Failure) retroalimenta «integridad no verificada».
_HOOK_FAIL_CLOSED = frozenset({"pre-tool-use", "post-tool-use", "post-tool-use-failure"})
# Eventos de 07-hooks §3.1 que, ante un fallo, degradan a 0 con un aviso (REQ-HOOK-5,
# REQ-HOOK-24; ConfigChange falla abierto). Un evento desconocido sale con 0 y sin
# salida (REQ-HOOK-9), salvo sin módulo de hooks (abajo).
_HOOK_DEGRADE = frozenset(
    {"session-start", "post-model-switch", "stop", "session-end", "config-change", "refresh"}
)


def _say(text: str) -> None:
    """Escribe una línea en stderr sin fallar por la codificación de la consola."""
    try:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()
    except (OSError, UnicodeError, ValueError):
        pass


def _unavailable(what: str) -> str:
    # Texto bilingüe fijo: aún no hay idioma resuelto ni catálogo cargado.
    return (
        f"{_meta.NAME} {what}: no disponible en esta versión ({_meta.__version__}). / "
        f"{_meta.NAME} {what}: not available in this version ({_meta.__version__})."
    )


def _hook_failed(event: str, cause: str) -> int:
    """Salida de ``hook`` ante un fallo propio, según el evento (07-hooks §3.4; REQ-HOOK-13).

    Nunca devuelve 1: con 1, Claude Code deja pasar un PreToolUse sin bloquear.
    """
    if event == "pre-tool-use":
        _say(cause + " Se bloquea la acción por seguridad. / The action is blocked for safety.")
        return _HOOK_BLOCK_EXIT
    if event in _HOOK_FAIL_CLOSED:
        _say(cause + " No se verificó la integridad. / Integrity was not verified.")
        return _HOOK_BLOCK_EXIT
    if event in _HOOK_DEGRADE:
        _say(cause + " Se continúa sin los controles del hook. / Continuing without the hook checks.")
    return 0


def _run_hook(args: list[str]) -> int:
    # Falla cerrada en PreToolUse: cualquier problema al cargar o ejecutar el hook, incluidos
    # SystemExit y KeyboardInterrupt, se traduce según el evento y nunca sale con 1.
    event = args[0] if args else ""
    try:
        from .hooks.main import run
    except ImportError:
        if event in _HOOK_FAIL_CLOSED or event in _HOOK_DEGRADE:
            return _hook_failed(event, _unavailable("hook"))
        # Sin módulo de hooks (H2 y H3), 'hook' sin evento o con uno desconocido
        # sale con 2 y el aviso fijo «no disponible»; la sonda de T4 (tests/hooks) lo usa.
        _say(_unavailable("hook") + " Se bloquea la acción por seguridad. / The action is blocked for safety.")
        return _HOOK_BLOCK_EXIT
    except BaseException:  # noqa: BLE001 - SyntaxError, OSError, SystemExit... al importar
        return _hook_failed(
            event, f"{_meta.NAME} hook: error interno al cargar. / {_meta.NAME} hook: internal error while loading."
        )
    try:
        code = run(args)
    except BaseException:  # noqa: BLE001 - sin el texto de la excepción, que puede traer datos
        return _hook_failed(event, f"{_meta.NAME} hook: error interno. / {_meta.NAME} hook: internal error.")
    if isinstance(code, int) and not isinstance(code, bool) and code in _HOOK_EXITS:
        return code
    return _hook_failed(
        event, f"{_meta.NAME} hook: salida inválida. / {_meta.NAME} hook: invalid exit status."
    )


def _run_mcp(args: list[str]) -> int:
    try:
        from .mcp.server import run
    except ModuleNotFoundError:
        _say(_unavailable("mcp"))
        return 1
    return int(run(args))


def _run_ci(args: list[str]) -> int:
    try:
        from .cli.ci import run
    except ModuleNotFoundError:
        _say(_unavailable("ci"))
        return 1
    return int(run(args))


def _run_cli(args: list[str]) -> int:
    try:
        from .cli.main import run
    except ModuleNotFoundError:
        _say(_unavailable("cli"))
        return 1
    return int(run(args))


def main(argv: list[str] | None = None) -> int:
    """Despacha ``hook``, ``mcp`` y ``ci``; el resto va a la CLI generada del registro."""
    args = sys.argv[1:] if argv is None else list(argv)
    head = args[0] if args else ""
    if head == "hook":  # ruta caliente: sin parser de CLI ni red
        return _run_hook(args[1:])
    if head == "mcp":
        return _run_mcp(args[1:])
    if head == "ci":  # subcomandos internos del CI (09-ci); fuera de REGISTRY
        return _run_ci(args[1:])
    if args == ["--version"]:
        # Respuesta directa, sin cargar la CLI (02-convencion: el agente la usa para
        # comprobar que el núcleo está instalado); REQ-ARCH-20: sale de _meta.
        sys.stdout.write(_meta.__version__ + "\n")
        return 0
    return _run_cli(args)


if __name__ == "__main__":  # pragma: no cover - ejecución directa del módulo
    raise SystemExit(main())
