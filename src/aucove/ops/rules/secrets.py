# SPDX-FileCopyrightText: 2026 The aucove Authors
# SPDX-License-Identifier: Apache-2.0
"""Regla ``secrets`` (09-ci §4.1; 10-seguridad REQ-SEC-36).

Ejecuta gitleaks sobre los commits del PR (``<merge-base>..<head_sha>``) con la
configuración empaquetada (``config.datafiles.gitleaks_config()``) y ``--redact``.
gitleaks analiza la salida de ``git log -p``, y para un archivo que git trata como
binario (atributos ``binary`` o ``-diff`` del ``.gitattributes`` del propio PR, o un
NUL en sus primeros 8000 bytes) git solo imprime "Binary files ... differ". Por eso
``--log-opts`` agrega ``--text --no-textconv --no-ext-diff``: el diff es siempre el
texto de los objetos, sin filtros que el PR pueda declarar. Además, ``git log -p`` no
muestra diff para un commit de fusión salvo que se pida, así que lo que se agrega al
fusionar dentro de la rama de la tarea (un archivo nuevo durante ``git merge
--no-commit`` o la resolución de un conflicto, flujo normal de ``update``) no llegaba a
gitleaks; ``--diff-merges=first-parent`` muestra cada fusión contra su primer padre, la
rama de la tarea. Vuelve a analizar lo que la fusión trae de la base, que ya pasó por
sus PR.
El ejecutable llega en ``RuleInput.tools['gitleaks']`` (lo resuelve el motor fuera
del árbol, REQ-ARCH-97); el directorio de trabajo es un temporal fuera del árbol,
así que gitleaks no encuentra ``.gitleaks.toml`` ni ``.gitleaksignore`` del PR en el
cwd. Además:

- ``--config`` explícito gana sobre un ``.gitleaks.toml`` del árbol, y se quitan del
  entorno ``GITLEAKS_CONFIG`` y ``GITLEAKS_CONFIG_TOML``;
- ``--ignore-gitleaks-allow`` hace que un comentario ``gitleaks:allow`` del PR no
  oculte un hallazgo;
- gitleaks también lee ``.gitleaksignore`` en la raíz del repositorio que analiza,
  así que si el checkout lo trae la regla falla cerrada en vez de dejar que lo lea.

Falla cerrada (REQ-CI-8): sin herramienta, sin rango, con un código de salida
distinto de 0 o del de hallazgos, con tiempo agotado o con un reporte ilegible, la
regla da ``unavailable`` y el check falla. Cada hallazgo es ``E_VALIDATION_SECRET``
con la ruta, la línea, la regla de gitleaks y el commit; nunca el secreto.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from ...domain.findings import PATH_PATTERN, Finding
from .base import RuleInput, RuleSpec, make_finding, unavailable

RULE_ID = "secrets"
CODE = "E_VALIDATION_SECRET"
TOOL = "gitleaks"
LEAKS_EXIT_CODE = 3  # --exit-code: distingue "hay hallazgos" de un error de gitleaks (sale con 1)
TIMEOUT_S = 300.0
MAX_REPORT_BYTES = 20 * 2**20
IGNORE_FILE = ".gitleaksignore"

_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
_RULE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_DROP_ENV = frozenset({"GITLEAKS_CONFIG", "GITLEAKS_CONFIG_TOML"})
# Opciones de ``git log`` que pasan por ``--log-opts`` antes del rango: diff textual de
# todo archivo, sin conversiones ni diff externo, y diff de cada commit de fusión contra
# su primer padre (sin esto, ``git log -p`` no muestra diff para las fusiones). gitleaks
# separa el valor por espacios.
LOG_OPTS: tuple[str, ...] = ("--text", "--no-textconv", "--no-ext-diff", "--diff-merges=first-parent")


def log_opts(merge_base: str, head_sha: str) -> str:
    """Valor de ``--log-opts``: las opciones fijas de ``LOG_OPTS`` y el rango del PR."""
    return " ".join((*LOG_OPTS, f"{merge_base}..{head_sha}"))


def gitleaks_argv(tool: Sequence[str], *, merge_base: str, head_sha: str, config: Path, report: Path,
                  source: Path) -> list[str]:
    """argv completo: el prefijo de ``RuleInput.tools`` más las opciones fijas de REQ-SEC-36."""
    return [
        *tool,
        "git",
        f"--log-opts={log_opts(merge_base, head_sha)}",
        "--redact",
        "--config", str(config),
        "--report-format", "json",
        "--report-path", str(report),
        "--exit-code", str(LEAKS_EXIT_CODE),
        "--no-banner",
        "--ignore-gitleaks-allow",
        str(source),
    ]


def _child_env() -> dict[str, str]:
    from ...vcs.git import child_env

    env = child_env(os.environ, read=True)
    return {k: v for k, v in env.items() if k.upper() not in _DROP_ENV}


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _read_report(path: Path) -> list[Mapping[str, object]] | None:
    """Lista de hallazgos del reporte JSON de gitleaks; ``None`` si falta o no vale."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_REPORT_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_REPORT_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(data, list) or not all(isinstance(item, Mapping) for item in data):
        return None
    return data


def _leak_finding(inp: RuleInput, leak: Mapping[str, object]) -> Finding:
    file = leak.get("File")
    path = file.replace("\\", "/") if isinstance(file, str) else None
    if path is not None and not PATH_PATTERN.match(path):
        path = None
    line = leak.get("StartLine")
    line_no = line if isinstance(line, int) and not isinstance(line, bool) and line >= 1 else None
    rule = leak.get("RuleID")
    rule_id = rule if isinstance(rule, str) and _RULE_NAME.match(rule) else "?"
    commit = leak.get("Commit")
    short = commit[:12] if isinstance(commit, str) and _SHA.match(commit) else "?"
    return make_finding(RULE, inp, CODE, severity="error", path=path, line=line_no, rule_id=rule_id, commit=short)


def _run(inp: RuleInput) -> Sequence[Finding]:
    from ...config.datafiles import gitleaks_config

    tool = inp.tools.get(TOOL)
    if not tool:
        return [unavailable(RULE, inp, inp.t("validate.secrets.no_tool"))]
    tree = inp.tree
    if tree is None or tree.root is None:
        return [unavailable(RULE, inp, inp.t("validate.reason.no_tree"))]
    merge_base = inp.base_tree.sha if inp.base_tree is not None else None
    head_sha = inp.head_sha
    if not (isinstance(merge_base, str) and _SHA.match(merge_base) and isinstance(head_sha, str)
            and _SHA.match(head_sha)):
        return [unavailable(RULE, inp, inp.t("validate.secrets.no_range"))]
    source = Path(tree.root)
    if os.path.lexists(source / IGNORE_FILE):
        return [unavailable(RULE, inp, inp.t("validate.secrets.ignore_file"), path=IGNORE_FILE)]
    try:
        config = gitleaks_config()
    except FileNotFoundError:
        return [unavailable(RULE, inp, inp.t("validate.secrets.no_config"))]
    with tempfile.TemporaryDirectory(prefix="validate-secrets-") as tmp:
        work = Path(tmp)
        if _within(work, source):  # el cwd nunca queda dentro del árbol validado
            return [unavailable(RULE, inp, inp.t("validate.secrets.workdir"))]
        report = work / "gitleaks.json"
        argv = gitleaks_argv(tool, merge_base=merge_base, head_sha=head_sha, config=config, report=report,
                             source=source)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            proc = subprocess.run(
                argv, cwd=work, env=_child_env(), stdin=subprocess.DEVNULL, capture_output=True,
                timeout=TIMEOUT_S, check=False, creationflags=flags,
            )
        except subprocess.TimeoutExpired:
            return [unavailable(RULE, inp, inp.t("validate.secrets.timeout"))]
        except OSError:
            return [unavailable(RULE, inp, inp.t("validate.secrets.no_tool"))]
        if proc.returncode not in (0, LEAKS_EXIT_CODE):
            return [unavailable(RULE, inp, inp.t("validate.secrets.exit", code=proc.returncode))]
        leaks = _read_report(report)
    if leaks is None or (proc.returncode == LEAKS_EXIT_CODE and not leaks):
        return [unavailable(RULE, inp, inp.t("validate.secrets.report"))]
    findings: dict[tuple[object, ...], Finding] = {}
    for leak in leaks:
        f = _leak_finding(inp, leak)
        findings.setdefault((f.path, f.line, f.message), f)
    return sorted(findings.values(), key=lambda f: (f.path or "", f.line or 0, f.message))


RULE = RuleSpec(
    id=RULE_ID,
    modes=frozenset({"pr"}),
    run=_run,
    codes={CODE: frozenset({"rule_id", "commit"})},
    tools=(TOOL,),
)

RULES: tuple[RuleSpec, ...] = (RULE,)
